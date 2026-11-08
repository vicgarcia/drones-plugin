"""Offline regression tests for the interactive map backend."""
import copy
import http.client
import importlib.util
import json
import socket
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "drones_map_under_test", Path(__file__).resolve().parents[1] / "skills" / "drones" / "scripts" / "drones.py")
drones = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drones)
LOCATION = {"latitude": 41.5, "longitude": -87.5}
VIEW = {"bbox": ["-87.61,41.41,-87.39,41.59"]}


def shape(notam="1/0001", lat=41.5, lng=-87.5):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"notam": notam, "title": "TEST TFR"}}


class Clock:
    def __init__(self):
        self.seconds = 0
        self.when = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.seconds += seconds
        self.when += timedelta(seconds=seconds)

    def cache(self, **kwargs):
        return drones.MapCache(clock=lambda: self.seconds, now=lambda: self.when, **kwargs)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.clock = Clock()
        self.faa = Mock()
        self.locator = Mock()
        self.locator.parse_coordinates = drones.Locator.parse_coordinates
        self.data = drones.MapData(LOCATION, faa=self.faa, locator=self.locator,
                                   cache=self.clock.cache(), now=lambda: self.clock.when)


class CacheTests(OfflineCase):
    def test_expiry_timestamp_and_lru(self):
        cache = self.clock.cache(max_entries=2)
        factory = Mock(side_effect=["a", "b", "c", "new-b", "new-a"])
        first = cache.get("a", 10, factory)
        cache.get("b", 10, factory)
        self.assertEqual(cache.get("a", 10, factory), first)
        cache.get("c", 10, factory)
        self.assertEqual(cache.get("b", 10, factory)["value"], "new-b")
        self.clock.advance(10)
        refreshed = cache.get("a", 10, factory)
        self.assertEqual(refreshed["value"], "new-a")
        self.assertNotEqual(first["fetched_at"], refreshed["fetched_at"])

    def test_stale_failure_retries_and_recovers(self):
        cache = self.clock.cache()
        original = cache.get("key", 5, lambda: [1])
        self.clock.advance(5)
        broken = Mock(side_effect=drones.DronesError("feed unavailable"))
        for _ in range(2):
            stale = cache.get("key", 5, broken, stale=True)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["warning"], "feed unavailable")
            self.assertEqual(stale["fetched_at"], original["fetched_at"])
        self.assertEqual(broken.call_count, 2)
        recovered = cache.get("key", 5, list, stale=True)
        self.assertEqual(recovered["value"], [])
        self.assertFalse(recovered["stale"])
        self.assertNotIn("warning", recovered)
        with self.assertRaises(drones.DronesError):
            cache.get("uncached", 5, broken, stale=True)
        with self.assertRaises(RuntimeError):
            cache.get("bug", 5, Mock(side_effect=RuntimeError("bug")), stale=True)

    def test_identical_concurrent_misses_share_one_factory(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()

        class ObservedFuture(Future):
            def result(self, *args, **kwargs):
                waiting.set()
                return super().result(*args, **kwargs)

        def load():
            entered.set()
            if not release.wait(5):
                raise AssertionError("factory was not released")
            return "shared"

        factory = Mock(side_effect=load)
        with patch.object(drones, "Future", ObservedFuture), ThreadPoolExecutor(2) as pool:
            owner = pool.submit(self.data.cache.get, "key", 10, factory)
            try:
                self.assertTrue(entered.wait(5))
                follower = pool.submit(self.data.cache.get, "key", 10, factory)
                self.assertTrue(waiting.wait(5))
            finally:
                release.set()
            self.assertEqual(owner.result(5), follower.result(5))
        factory.assert_called_once()


class AirspaceTests(OfflineCase):
    def test_invalid_viewports_fail_before_upstream(self):
        bad = [None, "", "1,2,3", "nan,41,-87,42", "-88,41,inf,42",
               "-181,41,-87,42", "-88,-86,-87,-85", "-88,42,-87,41",
               "-88,41,-88,42", "-90,40,-80,45", "-1,-1,1,1.01"]
        for bbox in bad:
            with self.subTest(bbox=bbox), self.assertRaises(ValueError):
                query = {} if bbox is None else {"bbox": [bbox]}
                self.data.airspace({**query, "layer": ["class"]})
            with self.subTest(tfr_bbox=bbox), self.assertRaises(ValueError):
                self.data.tfrs({} if bbox is None else {"bbox": [bbox]})
        with self.assertRaises(ValueError):
            self.data.airspace({**VIEW, "layer": ["not-a-layer"]})
        self.assertEqual(self.faa.mock_calls, [])

    def test_rounded_envelope_cache_and_six_hour_expiry(self):
        self.faa.query_envelope.return_value = []
        query = {**VIEW, "layer": ["class"]}
        result = self.data.airspace(query)
        self.assertEqual(result["bbox"], (-87.65, 41.4, -87.35, 41.6))
        self.faa.query_envelope.assert_called_once_with("class", result["bbox"])
        self.data.airspace({"bbox": ["-87.62,41.42,-87.38,41.58"], "layer": ["class"]})
        self.assertEqual(self.faa.query_envelope.call_count, 1)
        self.clock.advance(6 * 3600)
        self.data.airspace(query)
        self.assertEqual(self.faa.query_envelope.call_count, 2)

    def test_dateline_splits_and_deduplicates(self):
        feature = shape()
        feature["id"] = 7
        self.faa.query_envelope.return_value = [feature]
        with patch.object(drones, "map_layer_features", side_effect=lambda loc, data: data["class"]):
            result = self.data.airspace({"bbox": ["179.81,0.01,-179.81,0.19"], "layer": ["class"]})
        self.assertEqual(len(result["features"]), 1)
        calls = self.faa.query_envelope.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1], (179.8, 0.0, 180.0, 0.2))
        self.assertEqual(calls[1].args[1], (-180.0, 0.0, -179.8, 0.2))

    def test_stadium_query_expands_three_nm(self):
        self.faa.query_envelope.return_value = []
        result = self.data.airspace({**VIEW, "layer": ["stadiums"]})
        west, south, east, north = self.faa.query_envelope.call_args.args[1]
        bw, bs, be, bn = result["bbox"]
        self.assertLessEqual(west, bw - 0.05)
        self.assertLessEqual(south, bs - 0.05)
        self.assertGreaterEqual(east, be + 0.05)
        self.assertGreaterEqual(north, bn + 0.05)

    def test_envelope_uses_existing_pagination(self):
        client = drones.FAAClient()
        self.addCleanup(client.session.close)
        client._get_json = Mock(side_effect=[
            {"features": [{"properties": {"OBJECTID": 1}}],
             "properties": {"exceededTransferLimit": True}},
            {"features": [{"properties": {"OBJECTID": 2}}]}])
        bounds = (-88, 41, -87, 42)
        result = client.query_envelope("class", bounds)
        self.assertEqual(len(result), 2)
        params = [call.args[1] for call in client._get_json.call_args_list]
        self.assertEqual([p["resultOffset"] for p in params], [0, 1])
        for param in params:
            self.assertEqual(param["geometryType"], "esriGeometryEnvelope")
            self.assertEqual(param["returnGeometry"], "true")
            self.assertIn("OBJECTID", param["outFields"].split(","))
            self.assertEqual(json.loads(param["geometry"])["xmin"], -88)
        client._get_json.return_value = {"error": {"message": "broken"}}
        client._get_json.side_effect = None
        with self.assertRaises(drones.DronesError):
            client.query_envelope("class", bounds)

    def test_airport_popup_explicitly_labels_the_fixed_reference(self):
        airport = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-87.6, 41.6]},
                   "properties": {"OBJECTID": 1, "IDENT": "TEST", "NAME": "Test", "TYPE_CODE": "AD",
                                  "PRIVATEUSE": 0}}
        self.faa.query_envelope.return_value = [airport]
        result = self.data.airspace({**VIEW, "layer": ["airports"]})
        props = result["features"][0]["properties"]
        self.assertIn("Distance from launch", props)
        self.assertNotIn("Distance", props)

    def test_arcgis_transient_query_refusal_retries(self):
        client = drones.FAAClient()
        self.addCleanup(client.session.close)
        error = Mock(status_code=200, json=lambda: {"error": {"details": ["Unable to perform query."]}})
        success = Mock(status_code=200, json=lambda: {"features": []})
        client.session = Mock()
        client.session.get.side_effect = [error, success]
        with patch.object(drones.time, "sleep") as sleep:
            result = client._get_json(drones.FAA_SERVICES + "/Class_Airspace/FeatureServer/0/query", {})
        self.assertEqual(result, {"features": []})
        sleep.assert_called_once_with(drones.RETRY_DELAYS[0])

    def test_shared_faa_concurrency_is_four(self):
        lock, release, full = threading.Lock(), threading.Event(), threading.Event()
        counts = {"active": 0, "peak": 0}

        def fetch(*args):
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
                if counts["active"] == 4:
                    full.set()
            try:
                if not release.wait(5):
                    raise AssertionError("FAA calls were not released")
                return []
            finally:
                with lock:
                    counts["active"] -= 1

        self.faa.query_envelope.side_effect = fetch
        with ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(self.data._fetch, "query_envelope", "class", VIEW) for _ in range(8)]
            try:
                self.assertTrue(full.wait(5))
            finally:
                release.set()
            for future in futures:
                future.result(5)
        self.assertEqual(counts["peak"], 4)

    def test_default_faa_clients_are_per_thread_and_reused(self):
        rendezvous = threading.Barrier(2)
        created = []
        lock = threading.Lock()

        def make_client():
            client = Mock()
            client.tfrs.return_value = id(client)
            with lock:
                created.append(client)
            return client

        data = drones.MapData(LOCATION, locator=self.locator)

        def fetch_twice():
            first = data._fetch("tfrs")
            rendezvous.wait(timeout=5)
            return first, data._fetch("tfrs")

        with patch.object(drones, "FAAClient", side_effect=make_client), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(fetch_twice) for _ in range(2)]
            results = [future.result(5) for future in futures]
        self.assertEqual(len(created), 2)
        self.assertNotEqual(results[0][0], results[1][0])
        self.assertTrue(all(first == second for first, second in results))


class TFRTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.raw = [shape()]
        self.faa.tfrs.return_value = self.raw
        self.details = {"begins": self.clock.when + timedelta(seconds=60),
                        "ends": self.clock.when + timedelta(seconds=120),
                        "immediate": False, "permanent": False, "floor": "SFC", "ceiling": "400 ft"}
        self.faa.tfr_details.return_value = self.details

    def test_cached_raw_details_reannotated_without_mutation(self):
        before = copy.deepcopy(self.raw)
        scheduled = self.data.tfrs(VIEW)
        self.assertIn("scheduled", scheduled["features"][0]["properties"]["title"])
        self.clock.advance(60)
        active = self.data.tfrs(VIEW)
        self.assertNotIn("scheduled", active["features"][0]["properties"]["title"])
        self.clock.advance(60)
        self.assertEqual(self.data.tfrs(VIEW)["features"], [])
        self.assertEqual(self.raw, before)
        self.faa.tfrs.assert_called_once()
        self.faa.tfr_details.assert_called_once_with("1/0001")

    def test_feed_failure_unknown_then_empty_success_clears(self):
        original = self.data.tfrs(VIEW)
        self.clock.advance(drones.MAP_TFR_TTL)
        self.faa.tfrs.side_effect = drones.DronesError("feed failed")
        stale = self.data.tfrs(VIEW)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["fetched_at"], original["fetched_at"])
        self.assertIn("feed failed", stale["warnings"])
        self.assertEqual(stale["features"][0]["properties"]["stroke"], "#d7191c")
        self.faa.tfrs.side_effect = None
        self.faa.tfrs.return_value = []
        fresh = self.data.tfrs(VIEW)
        self.assertEqual(fresh["features"], [])
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["warnings"], [])

    def test_detail_failure_unknown_and_recovery(self):
        self.data.tfrs(VIEW)
        self.clock.advance(drones.MAP_TFR_TTL)
        self.faa.tfr_details.side_effect = drones.DronesError("detail failed")
        stale = self.data.tfrs(VIEW)
        self.assertTrue(stale["stale"])
        self.assertIn("detail failed", stale["warnings"][0])
        self.assertEqual(len(stale["features"]), 1)  # expired cached timing cannot hide a restriction
        self.faa.tfr_details.side_effect = None
        self.assertEqual(self.data.tfrs(VIEW)["features"], [])

    def test_no_cached_feed_raises_but_no_details_keeps_shape(self):
        self.faa.tfrs.side_effect = drones.DronesError("no feed")
        with self.assertRaises(drones.DronesError):
            self.data.tfrs(VIEW)
        self.faa.tfrs.side_effect = None
        self.faa.tfr_details.side_effect = drones.DronesError("no details")
        result = self.data.tfrs(VIEW)
        self.assertTrue(result["stale"])
        self.assertEqual(len(result["features"]), 1)

    def test_fetch_age_uses_oldest_detail_and_reports_next_transition(self):
        first = self.data.tfrs(VIEW)
        self.assertEqual(first["next_change_at"], self.details["begins"].isoformat())
        self.clock.advance(60)
        active = self.data.tfrs(VIEW)
        self.assertEqual(active["next_change_at"], self.details["ends"].isoformat())
        # Details first fetched on a later pan can outlive the feed's entry.
        self.data.cache = self.clock.cache()
        self.data.cache.get("tfr-shapes", drones.MAP_TFR_TTL, lambda: self.raw)
        self.clock.advance(60)
        detail_time = self.clock.when.isoformat()
        self.data.tfrs(VIEW)
        self.clock.advance(drones.MAP_TFR_TTL - 60)
        refreshed = self.data.tfrs(VIEW)
        self.assertEqual(refreshed["fetched_at"], detail_time)
        self.assertIsNone(refreshed["next_change_at"])

    def test_bbox_or_thirty_nm_center_filter(self):
        self.faa.tfrs.return_value = [shape("near", lat=41.9), shape("far", lat=43)]
        self.assertEqual(len(self.data.tfrs(VIEW)["features"]), 1)
        self.faa.tfr_details.assert_called_once_with("near")


class SearchTests(OfflineCase):
    def test_coordinates_and_validation_skip_geocoder(self):
        self.assertEqual(self.data.search({"q": ["41.5,-87.5"]})["results"][0]["lat"], 41.5)
        for query in ({}, {"q": [""]}, {"q": [" "]}, {"q": ["x" * 201]}, {"q": ["91,0"]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.data.search(query)
        self.locator.geocode.assert_not_called()

    def test_alternatives_normalized_cache_and_day_expiry(self):
        self.locator.geocode.return_value = {
            "address": "Chicago", "latitude": 41.8, "longitude": -87.6,
            "alternatives": [{"address": "Other Chicago", "latitude": 42, "longitude": -88}]}
        first = self.data.search({"q": ["  Chicago   Illinois "]})
        self.assertEqual([r["name"] for r in first["results"]], ["Chicago", "Other Chicago"])
        self.assertEqual(self.data.search({"q": ["CHICAGO Illinois"]}), first)
        self.locator.geocode.assert_called_once_with("Chicago Illinois")
        self.clock.advance(24 * 3600)
        self.data.search({"q": ["Chicago Illinois"]})
        self.assertEqual(self.locator.geocode.call_count, 2)

    def test_zero_results_cached_and_errors_retry(self):
        self.locator.geocode.return_value = None
        self.assertEqual(self.data.search({"q": ["nowhere"]}), {"results": []})
        self.data.search({"q": ["NOWHERE"]})
        self.locator.geocode.assert_called_once()
        self.locator.geocode.side_effect = drones.DronesError("geocoder failed")
        with self.assertRaises(drones.DronesError):
            self.data.search({"q": ["another"]})
        self.locator.geocode.side_effect = None
        self.assertEqual(self.data.search({"q": ["another"]}), {"results": []})

    def test_distinct_searches_serialize_geocoder(self):
        entered, release, second_waiting = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        attempts = []

        class ObservedLock:
            def __enter__(self):
                attempts.append(threading.get_ident())
                if len(attempts) == 2:
                    second_waiting.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        def geocode(text):
            entered.set()
            if not release.wait(5):
                raise AssertionError("geocoder was not released")

        self.data._search_lock = ObservedLock()
        self.locator.geocode.side_effect = geocode
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.data.search, {"q": ["first"]})
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.data.search, {"q": ["second"]})
                self.assertTrue(second_waiting.wait(5))
                self.assertEqual(self.locator.geocode.call_count, 1)
            finally:
                release.set()
            self.assertEqual(first.result(5), {"results": []})
            self.assertEqual(second.result(5), {"results": []})
        self.assertEqual(self.locator.geocode.call_count, 2)


class RouteTests(unittest.TestCase):
    def setUp(self):
        original_connect = socket.socket.connect

        def localhost_only(sock, address):
            if address[0] != "127.0.0.1":
                raise AssertionError("External network forbidden")
            return original_connect(sock, address)

        guard = patch.object(socket.socket, "connect", localhost_only)
        guard.start()
        self.addCleanup(guard.stop)

        def bad(query):
            raise ValueError("invalid viewport")

        def down(query):
            raise drones.DronesError("FAA unavailable")

        self.server = drones._map_server("page", {"features": []}, 0, {
            "/search.json": lambda query: {"results": query.get("q", [])},
            "/bad.json": bad, "/down.json": down})
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), json.loads(response.read())
        finally:
            conn.close()

    def test_live_queries_decode_and_snapshot_stays_available(self):
        status, content_type, body = self.request("/search.json?q=Chicago%20Midway")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(body, {"results": ["Chicago Midway"]})
        self.assertEqual(self.request("/data.json")[2], {"features": []})

    def test_live_errors_are_json_with_http_status(self):
        self.assertEqual(self.request("/bad.json"),
                         (400, "application/json", {"detail": "invalid viewport"}))
        self.assertEqual(self.request("/down.json"),
                         (502, "application/json", {"detail": "FAA unavailable"}))


class InteractiveCommandTests(OfflineCase):
    def test_browser_uses_dynamic_routes_and_png_stays_snapshot(self):
        here = {**LOCATION, "query": "Midway"}
        for png in (False, True):
            with self.subTest(png=png), patch.object(drones, "_locate", return_value=here), \
                    patch.object(drones, "survey", return_value={}), \
                    patch.object(drones, "serve_map", return_value=0) as serve, \
                    patch.object(drones, "screenshot_map", return_value="map.png") as shot:
                args = drones.build_parser().parse_args(["map", "here", *(["--png", "map.png"] if png else [])])
                drones.cmd_map(args)
                if png:
                    serve.assert_not_called()
                    self.assertIn("const INTERACTIVE = null;", shot.call_args.args[0])
                    self.assertEqual(shot.call_args.kwargs["live"], {})
                else:
                    shot.assert_not_called()
                    self.assertNotIn("const INTERACTIVE = null;", serve.call_args.args[0])
                    self.assertEqual(set(serve.call_args.kwargs["live"]),
                                     {"/airspace.json", "/tfrs.json", "/search.json"})


if __name__ == "__main__":
    unittest.main()
