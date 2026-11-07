"""Opt-in Chromium tests; all map/data requests are intercepted, never live FAA queries.

DRONES_BROWSER_TESTS=1 uv run --with geopy --with requests --with playwright \
    python -m unittest discover -s tests -p test_map_browser.py -v

Downloads only pinned Leaflet assets; uses an installed Chrome/Edge/Chromium
(set DRONES_BROWSER to its path if it isn't found).
"""
import base64
import importlib.util
import json
import os
import shutil
import unittest
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

SPEC = importlib.util.spec_from_file_location("drones_browser_test", Path(__file__).resolve().parents[1] / "skills" / "drones" / "scripts" / "drones.py")
drones = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drones)
LOCATION = {"query": "Midway", "latitude": 41.79, "longitude": -87.74}
BROWSERS = [
    "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "msedge", "microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=")


@unittest.skipUnless(os.environ.get("DRONES_BROWSER_TESTS") == "1", "opt-in browser tests")
class MapBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("install playwright for browser tests")
        cls.browser_path = os.environ.get("DRONES_BROWSER") or next(
            filter(None, (shutil.which(name) for name in BROWSERS)), None)
        if not cls.browser_path:
            raise unittest.SkipTest("no installed Chromium-family browser")
        cls.assets = {}
        for name in ["leaflet.js", "leaflet.css"]:
            url = "https://unpkg.com/leaflet@1.9.4/dist/" + name
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            cls.assets[name] = response.text

    async def asyncSetUp(self):
        from playwright.async_api import Error, async_playwright
        self.route_error = Error
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=self.browser_path, headless=True)
        self.context = await self.browser.new_context(viewport={"width": 1100, "height": 800})
        self.page = await self.context.new_page()
        self.calls = []
        self.tfr_mode = "fresh"
        self.tfr_start_ms = None
        self.tfr_end_ms = None
        self.search_delayed = None
        self.held_search = None
        self.hold_class = False
        self.held_class = None
        self.adsb = False
        self.radar = False
        await self.page.route("**/*", self.route)
        await self.page.clock.install(time=datetime.now(timezone.utc))

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def route(self, route):
        url = urlsplit(route.request.url)
        if url.hostname == "unpkg.com":
            name = url.path.rsplit("/", 1)[-1]
            await route.fulfill(body=self.assets.get(name, ""),
                                content_type="text/css" if name.endswith("css") else "application/javascript")
            return
        if url.hostname != "127.0.0.1":
            await route.fulfill(body=PNG, content_type="image/png")
            return
        query = parse_qs(url.query)
        self.calls.append((url.path, query))
        data = {}
        if url.path == "/":
            await route.fulfill(body=drones.map_page(LOCATION, "street", adsb=self.adsb, radar=self.radar),
                                content_type="text/html")
            return
        if url.path == "/data.json":
            data = {"type": "FeatureCollection", "features": drones.map_features(LOCATION, {}, 5)}
        elif url.path == "/airspace.json":
            bbox = [float(v) for v in query["bbox"][0].split(",")]
            key = query["layer"][0]
            west, south, east, north = bbox
            data = {"type": "FeatureCollection", "bbox": bbox,
                    "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
                        [[west, south], [east, south], [east, north], [west, north], [west, south]]]},
                        "properties": {"_layer": key, "title": key + ":" + str(round(south, 2)),
                                       **({"_ceiling": 100} if key == "laanc" else {})}}]}
        elif url.path == "/tfrs.json":
            if self.tfr_mode == "error":
                await route.fulfill(status=502, json={"detail": "FAA fixture failure"})
                return
            data = {"type": "FeatureCollection", "stale": False, "warnings": [], "features": []}
            if self.tfr_mode != "empty":
                data["features"] = [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
                    [[-87.8, 41.7], [-87.7, 41.7], [-87.7, 41.8], [-87.8, 41.7]]]},
                    "properties": {"_layer": "tfrs", "title": "Fixture TFR"}}]
        elif url.path == "/traffic.json":
            aircraft = drones.aircraft_feature({"hex": "a12345", "flight": "FIXTURE",
                "lat": float(query["lat"][0]), "lon": float(query["lng"][0]),
                "alt_baro": 1200, "track": 90, "seen_pos": 0})
            data = {"type": "FeatureCollection", "features": [aircraft]}
        elif url.path == "/radar.json":
            data = {"latest": "2026-11-04T12:00:00Z"}
        elif url.path == "/search.json":
            text = query["q"][0]
            if text == self.search_delayed:
                self.held_search = route
                return
            if text == "ambiguous":
                data = {"results": [{"name": "<b>literal name</b>", "lat": 40, "lng": -105},
                                    {"name": "Other result", "lat": 39, "lng": -104}]}
            else:
                lat, lng = map(float, text.split(","))
                data = {"results": [{"name": text, "lat": lat, "lng": lng}]}
        else:
            await route.fulfill(body=PNG, content_type="image/png")
            return
        if url.path in {"/airspace.json", "/tfrs.json"}:
            milliseconds = await self.page.evaluate("Date.now()")
            data["fetched_at"] = datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
            if url.path == "/tfrs.json" and self.tfr_start_ms is not None:
                next_ms = self.tfr_start_ms if milliseconds < self.tfr_start_ms else self.tfr_end_ms
                data["next_change_at"] = (datetime.fromtimestamp(next_ms / 1000, timezone.utc).isoformat()
                                          if next_ms and next_ms > milliseconds else None)
                if self.tfr_end_ms and milliseconds >= self.tfr_end_ms:
                    data["features"] = []
                elif milliseconds < self.tfr_start_ms:
                    data["features"][0]["properties"]["title"] += " (scheduled)"
        if url.path == "/airspace.json" and query["layer"] == ["class"] and self.hold_class:
            self.hold_class = False
            self.held_class = (route, data)
            return
        await route.fulfill(body=json.dumps(data), content_type="application/json")

    async def open_map(self):
        await self.page.goto("http://127.0.0.1:8765/")
        await self.page.wait_for_function("document.querySelector('.map-status') && "
                                          "document.querySelector('.map-status').textContent.includes('TFR data:') && "
                                          "!document.querySelector('.map-status').textContent.includes('Loading')")

    def count(self, path, layer=None):
        return sum(p == path and (layer is None or q.get("layer") == [layer]) for p, q in self.calls)

    async def test_viewport_loads_preserves_toggles_and_rebuilds_labels(self):
        await self.open_map()
        self.assertEqual(self.count("/airspace.json"), 8)
        await self.page.evaluate("map.removeLayer(layers.class); map.setView([40, -105], 12)")
        await self.page.clock.run_for(600)
        await self.page.wait_for_function("layers.laanc.toGeoJSON().features[0].properties.title.startsWith('laanc:39')")
        self.assertEqual(self.count("/airspace.json", "class"), 1)
        self.assertFalse(await self.page.evaluate("map.hasLayer(layers.class)"))
        self.assertEqual(await self.page.evaluate("ceilingCells.length"), 1)
        await self.page.evaluate("map.addLayer(layers.class)")
        await self.page.clock.run_for(600)
        await self.page.wait_for_function("layers.class.toGeoJSON().features[0].properties.title.startsWith('class:39')")
        self.assertEqual(self.count("/airspace.json", "class"), 2)
        await self.page.evaluate("map.setView([40, -105], 5)")
        await self.page.clock.run_for(400)
        self.assertIn("Zoom in", await self.page.locator('.map-status').inner_text())

    async def test_obsolete_viewport_response_cannot_replace_newer_view(self):
        await self.open_map()
        self.hold_class = True
        await self.page.evaluate("map.setView([42, -88], 12, {animate: false})")
        await self.page.clock.run_for(600)
        self.assertIsNotNone(self.held_class)
        await self.page.evaluate("map.setView([40, -105], 12, {animate: false})")
        await self.page.clock.run_for(600)
        await self.page.wait_for_function("layers.class.toGeoJSON().features[0].properties.title.startsWith('class:39')")
        route, data = self.held_class
        # The old request can already be aborted.
        with suppress(self.route_error):
            await route.fulfill(body=json.dumps(data), content_type="application/json")
        await self.page.clock.run_for(300)
        title = await self.page.evaluate("layers.class.toGeoJSON().features[0].properties.title")
        self.assertTrue(title.startswith("class:39"), title)

    async def test_tfr_fifteen_minute_refresh_retains_on_error_then_clears(self):
        await self.open_map()
        self.assertEqual(self.count("/tfrs.json"), 1)
        self.assertEqual(await self.page.evaluate("layers.tfrs.getLayers().length"), 1)
        self.tfr_mode = "error"
        await self.page.clock.fast_forward(900_100)
        await self.page.wait_for_function("document.querySelector('.map-status').textContent.includes('TFR refresh failed')")
        self.assertGreaterEqual(self.count("/tfrs.json"), 2)
        self.assertEqual(await self.page.evaluate("layers.tfrs.getLayers().length"), 1)
        self.tfr_mode = "empty"
        await self.page.clock.fast_forward(30_100)
        await self.page.wait_for_function("layers.tfrs.getLayers().length === 0")
        self.assertNotIn("failed", await self.page.locator('.map-status').inner_text())

    async def test_status_deadlines_survive_hidden_tabs_and_disabled_overlay(self):
        now = await self.page.evaluate("Date.now()")
        self.tfr_start_ms = now + 60_000
        self.tfr_end_ms = now + 120_000
        await self.open_map()
        self.assertIn("scheduled", await self.page.evaluate("layers.tfrs.toGeoJSON().features[0].properties.title"))
        await self.page.evaluate("window.fixtureHidden = true; Object.defineProperty(document, 'hidden', "
                                 "{configurable: true, get: () => window.fixtureHidden})")
        await self.page.clock.fast_forward(90_000)
        self.assertEqual(self.count("/tfrs.json"), 1)
        await self.page.evaluate("window.fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange'))")
        await self.page.wait_for_function("!layers.tfrs.toGeoJSON().features[0].properties.title.includes('scheduled')")
        self.assertEqual(self.count("/tfrs.json"), 2)
        await self.page.evaluate("map.removeLayer(layers.tfrs)")
        await self.page.clock.fast_forward(40_000)
        self.assertEqual(self.count("/tfrs.json"), 2)
        await self.page.evaluate("map.addLayer(layers.tfrs)")
        await self.page.clock.run_for(400)
        await self.page.wait_for_function("layers.tfrs.getLayers().length === 0")
        self.assertEqual(self.count("/tfrs.json"), 3)

    async def test_enter_only_search_results_and_stale_search_response(self):
        await self.open_map()
        original = await self.page.evaluate("layers.radius.toGeoJSON()")
        field = self.page.get_by_role("searchbox")
        await field.fill("ambiguous")
        await self.page.clock.run_for(400)
        self.assertEqual(self.count("/search.json"), 0)
        await field.press("Enter")
        await self.page.get_by_role("button", name="<b>literal name</b>").wait_for()
        self.assertEqual(await self.page.locator('.map-results b').count(), 0)
        await self.page.get_by_role("button", name="Other result").click()
        self.assertEqual(await self.page.evaluate("map.getCenter().lat"), 39)
        self.search_delayed = "old search"
        await field.fill("old search")
        await field.press("Enter")
        await self.page.wait_for_timeout(100)
        self.assertIsNotNone(self.held_search)
        await field.fill("40,-105")
        await field.press("Enter")
        await self.page.wait_for_function("map.getCenter().lat === 40")
        # AbortController may already have cancelled the older request.
        with suppress(self.route_error):
            await self.held_search.fulfill(json={"results": [{"name": "old", "lat": 20, "lng": -80}]})
        await self.page.clock.run_for(300)
        self.assertEqual(await self.page.evaluate("map.getCenter().lat"), 40)
        self.assertEqual(await self.page.evaluate("layers.radius.toGeoJSON()"), original)

    async def test_kestrel_controls_basemaps_clear_and_live_layer_toggles(self):
        self.adsb = self.radar = True
        await self.open_map()
        panel = self.page.locator('.kpanel')
        self.assertTrue(await panel.evaluate("el => el.parentElement.classList.contains('leaflet-right')"))
        self.assertEqual(await panel.evaluate("el => getComputedStyle(el).borderRadius"), '12px')
        self.assertFalse(await self.page.locator('.leaflet-control-layers').count())
        await self.page.wait_for_function("document.querySelector('.ac') !== null")
        self.assertGreaterEqual(self.count('/traffic.json'), 1)
        self.assertGreaterEqual(self.count('/radar.json'), 1)
        self.assertTrue(await self.page.get_by_role('checkbox', name='Air traffic').is_checked())
        self.assertTrue(await self.page.get_by_role('checkbox', name='Weather radar').is_checked())
        await self.page.get_by_text('Air traffic', exact=True).click()
        self.assertFalse(await self.page.get_by_role('checkbox', name='Air traffic').is_checked())
        self.assertEqual(await self.page.locator('.ac').count(), 0)
        await self.page.get_by_text('Air traffic', exact=True).click()
        await self.page.wait_for_function("document.querySelector('.ac') !== null")
        await self.page.get_by_text('Weather radar', exact=True).click()
        self.assertFalse(await self.page.get_by_role('checkbox', name='Weather radar').is_checked())
        await self.page.get_by_text('Satellite', exact=True).click()
        self.assertTrue(await self.page.evaluate('map.hasLayer(bases.satellite)'))
        await self.page.get_by_role('button', name='Hide layers').click()
        self.assertFalse(await self.page.get_by_role('radiogroup', name='Basemap').is_visible())
        await self.page.get_by_role('button', name='Show layers').click()
        self.assertTrue(await self.page.get_by_role('radiogroup', name='Basemap').is_visible())
        field = self.page.get_by_role('searchbox')
        await field.fill('40,-105')
        await field.press('Enter')
        await self.page.wait_for_function('map.getCenter().lat === 40')
        count = self.count('/search.json')
        await self.page.get_by_role('button', name='Clear search').click()
        self.assertEqual(await field.input_value(), '')
        self.assertEqual(self.count('/search.json'), count)
        before = await self.page.evaluate('map.getZoom()')
        await self.page.get_by_role('button', name='Zoom in', exact=True).click()
        await self.page.clock.run_for(400)
        self.assertGreater(await self.page.evaluate('map.getZoom()'), before)
        await self.context.grant_permissions(['geolocation'])
        await self.context.set_geolocation({'latitude': 26.014229, 'longitude': -80.1877804})
        await self.page.get_by_role('button', name='Show my location').click()
        await self.page.wait_for_function('Math.abs(map.getCenter().lat - 26.014229) < 0.0001')
        self.assertTrue(await self.page.locator('.leaflet-control-attribution').is_visible())

    async def test_mobile_panel_does_not_overlap_map_buttons(self):
        await self.page.set_viewport_size({'width': 375, 'height': 812})
        await self.open_map()
        self.assertTrue(await self.page.get_by_role('button', name='Show layers').is_visible())
        await self.page.get_by_role('button', name='Show layers').click()
        panel = await self.page.locator('.kpanel').bounding_box()
        buttons = await self.page.locator('.kbuttons').bounding_box()
        self.assertGreaterEqual(panel['x'], buttons['x'] + buttons['width'])
        self.assertLessEqual(panel['x'] + panel['width'], 375)
        self.assertEqual(await self.page.locator('.kp-grid').evaluate(
            "el => getComputedStyle(el).gridTemplateColumns.split(' ').length"), 1)


if __name__ == "__main__":
    unittest.main()
