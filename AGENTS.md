# drones-plugin

The `drones` agent skill and `drones.py`, the single-file CLI behind it: FAA airspace, TFRs, drone
rules, aviation weather and an interactive airspace map for US drone pilots. Two consumers: people
running `drones.py <command>` in a terminal, and agents reading `SKILL.md` and running the same
commands. The repo root is also a Claude plugin and a one-plugin marketplace for Claude Desktop.

The README covers usage. This file covers how the tool is built, what its data sources really
return, and the decisions behind the defaults.

## Layout

| Path | What's in it |
|---|---|
| `skills/drones/scripts/drones.py` | the whole CLI: PEP 723 uv script, ~4,100 lines |
| `skills/drones/SKILL.md` | agent-facing contract, including the FAA rules reference |
| `skills/drones/scripts/requirements.txt` | the script's deps, for environments without uv |
| `tests/test_drones.py` | offline CLI and logic tests (all network mocked) |
| `tests/test_map.py` | offline tests for the interactive map backend (`MapData`, `MapCache`, routes) |
| `tests/test_map_browser.py` | opt-in Chromium tests of the map page, all requests intercepted |
| `.claude-plugin/` | `plugin.json` (plugin `drones`) and `marketplace.json` (`drones-plugin`, source `./`) |
| `.github/workflows/release.yml` | on push to main: date-versioned release with the plugin and skill zips |
| `screenshot.png` | README image: `map "4406 Fillmore St, Hollywood, FL" --tiles light --adsb --size 1200x800` |

## Commands

- `uv run --script skills/drones/scripts/drones.py --help`: run the CLI from the repo
- `uv run --with 'geopy>=2.4.0' --with 'requests>=2.31.0' python -m unittest discover -s tests -v`:
  offline suite (108 tests, 8 skipped browser tests)
- `DRONES_BROWSER_TESTS=1 uv run --with geopy --with requests --with playwright python -m unittest discover -s tests -p test_map_browser.py -v`:
  browser tests; needs an installed Chrome/Edge/Chromium and downloads only the pinned Leaflet assets
- `claude plugin validate .`: check the plugin and marketplace manifests

Run the offline suite after every change. There is no ruff config; the script predates it and
`ruff check` reports style issues, so don't reformat the file wholesale.

## Code structure

Sections in `drones.py` are split by `# ====` banners. Top to bottom:

- **Constants**: every threshold and URL is a named module constant with a comment saying why
  (`DEFAULT_RADIUS_NM`, `TFR_REACH_NM`, `RETRY_DELAYS`, `MAP_TFR_TTL`, ...). `CLI_EPILOG` holds the
  examples shown by `--help`. `MAP_PAGE` is the whole Leaflet page as a template string.
- **Clients**, each owning its I/O: `Locator` (Nominatim geocoding, paced 1.1 s), `FAAClient`
  (ArcGIS layers and tfr.faa.gov), `WeatherClient` (AWC), `TrafficClient` (adsb.fi).
- **Logic**: `check_point()` (one point, the `airspace` verdict via `assess()`), `survey()` (a
  radius, for `nearby` and map snapshots), `preflight()` (airspace plus weather at a time).
- **Map**: `map_features()` builds GeoJSON with simplestyle properties, `map_page()` fills
  `MAP_PAGE`, `serve_map()`/`_map_server()` serve it on `127.0.0.1`, `screenshot_map()` drives
  headless Chrome (`find_browser()`, `DRONES_BROWSER`). `MapData` + `MapCache` back the live
  viewport routes.
- **Commands**: `cmd_<name>(args) -> int` handlers, `build_parser()`, `main()`.

Errors raise `DronesError` and print `Error: ...` to stderr with exit 1. `preflight` exits 3 on
NO-GO. Every read command takes `--json`.

When adding a command: client method → logic → `cmd_` handler → parser entry → `CLI_EPILOG`
example → `SKILL.md` → `README.md` → tests.

## Plugin packaging

- Keep `SKILL.md` in `skills/drones/`. At the repo root, Cowork installs the skill without
  `scripts/`.
- `SKILL.md` runs `uv run --script ${CLAUDE_SKILL_DIR}/scripts/drones.py`, and its `allowed-tools`
  rule matches that exact command. Keep the two in sync.
- The plugin build targets Claude Desktop, including Cowork. Coding harnesses such as Claude Code
  and pi run `drones.py` from PATH with the skill's invocation rewritten (README, Installation).
- Cowork runs the script in a cloud sandbox. PyPI is reachable by default, but the service
  domains in `compatibility` must be allowed, and nothing served on localhost reaches the user.
- The release builds `drones-plugin-v*.zip` (the plugin) and `drones-skill-v*.zip` (one top-level
  `drones/` folder, for Customize > Skills). GitHub adds its own source archives; they can't be
  turned off.

## FAA airspace (UAS Data Delivery System)

The feature services behind the FAA UAS Facility Map and the
[UDDS open data hub](https://udds-faa.opendata.arcgis.com/), credited to *FAA/ATO/Aeronautical
Information Services*. Verified live 2026-10-19. Service directory:
<https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services?f=json>. Every layer used is
layer `0` of its `FeatureServer`.

| `LAYERS` key | Service | Geometry | Fields read | Used for |
|---|---|---|---|---|
| `laanc` | `FAA_UAS_FacilityMap_Data` | Polygon (grid cell) | `CEILING`, `MAP_EFF`, `APT1..5_{FAAID,ICAO,NAME,LAANC,Enabled}`, `AIRSPACE_1..5` | LAANC ceiling (ft AGL) and which airports and programs cover the cell |
| `class` | `Class_Airspace` | Polygon | `NAME`, `ICAO_ID`, `LOCAL_TYPE`, `LOWER_*`, `UPPER_*`, `WKHR_*` | Class B/C/D/E-surface areas |
| `sua` | `Special_Use_Airspace` | Polygon | `NAME`, `TYPE_CODE`, `LOWER_*`, `UPPER_*`, `TIMESOFUSE` | Restricted, warning, alert and military operations areas |
| `prohibited` | `Prohibited_Areas` | Polygon | same as `sua` | P-areas (e.g. P-56 over the National Mall) |
| `defense` | `National_Defense_Airspace_TFR_Areas` | Polygon | `NAME`, `WKHR_CODE`, `WKHR_RMK` | National defense airspace, mostly "BY NOTAM" |
| `nsufr` | `Part_Time_National_Security_UAS_Flight_Restrictions` | Polygon | `Facility`, `Base`, `Floor`, `Ceiling`, `ALERTYPE` | Part-time national security UAS restrictions |
| `stadiums` | `Stadiums` | Point | `NAME`, `CITY`, `STATE` | 3 NM / 3,000 ft event TFRs |
| `airports` | `US_Airport` | Point | `IDENT`, `ICAO_ID`, `NAME`, `TYPE_CODE`, `PRIVATEUSE`, `ELEVATION` | Nearby airports and heliports |

Not used: `Digital_Obstacle_File`, `Airspace_Schedule`, `Boundary_Airspace`, `Route_Airspace`,
`Runways`, and the `Pending_*` layers (published changes not yet effective). The `_V5`, `_Test`,
`_Dev`, `_Primary` and `_Secondary` facility map variants look like FAA internal copies; stick to
`FAA_UAS_FacilityMap_Data`.

### Query pattern

Every lookup is a spatial query against `<service>/FeatureServer/0/query`:

```
geometry=<lng>,<lat>            # note: x,y order
geometryType=esriGeometryPoint
inSR=4326 & outSR=4326
spatialRel=esriSpatialRelIntersects
distance=10 & units=esriSRUnit_NauticalMile   # omit both for "at this point"
outFields=<comma list>
returnGeometry=true|false
orderByFields=OBJECTID          # stable paging
f=geojson
```

- With `f=geojson` the server does the coordinate conversion and returns standard GeoJSON.
- Map queries also send `geometryPrecision=6&maxAllowableOffset=0.0002`. That simplifies shapes by
  about 20 m, which keeps large polygons small without distorting the LAANC grid cells.
- Interactive maps query by envelope instead (`query_envelope()`, `esriGeometryEnvelope`).
- **Paging:** a page is capped at the layer's `maxRecordCount`: 2,000 for most layers, 1,000 for
  `US_Airport`. A truncated page carries `properties.exceededTransferLimit: true`, and the client
  re-requests with `resultOffset`. A 10 NM radius around Chicago Midway returns 550 grid cells.
- **Throttling:** ArcGIS Online answers bursts with HTTP 429, or with a 200 whose body is an error
  mentioning "Too many requests".
  - drones.py runs at most 4 layer queries at once and retries after 2, 5 and 10 seconds. The same
    backoff covers the transient "Unable to perform query" errors.
  - Back-to-back heavy radius queries, such as the dense LAANC grid in downtown DC, can still trip
    it. Waiting a minute clears it.

### Field notes

- **LAANC grid cells** are 30 x 30 arc-second squares, about 0.5 x 0.4 NM.
  - Cells only exist in controlled airspace near airports. No cell at a point usually means
    Class G, but always check `Class_Airspace` too.
  - `CEILING` is the most the FAA will pre-approve through LAANC, in **feet AGL**, in multiples of
    50 ft. `0` means no automatic approval.
  - `APTn_LAANC = 1` means the airport takes part in LAANC. `APTn_Enabled` lists its programs:
    `107-AA` (Part 107 automatic approval), `107-FC` (Part 107 further coordination, handled
    manually above the ceiling) and `44809-AA` (recreational automatic approval).
  - `MAP_EFF` is the effective date of the facility map publication. The FAA republishes every
    56 days.
- **Class airspace** includes types that don't matter for drones.
  - drones.py keeps `CLASS_B`, `CLASS_C`, `CLASS_D`, `CLASS_E2` (surface area) and
    `CLASS_E3`/`CLASS_E4` (surface extensions); `DRONE_CLASSES` lists them.
  - It drops Class A, the `MODE C` veil, and the `CLASS_E5`/`CLASS_E6` floors at 700 or 1,200 ft.
  - Class B and C shelves come back as separate polygons with a non-zero `LOWER_VAL`. They are
    shown as overhead, not at the surface.
- **Altitude fields:** `*_VAL` is a number, `*_UOM` is `FT` or `FL`, and `*_CODE` is `SFC`, `MSL`,
  `AGL` or `STD`. `UPPER_VAL = -9998` means "up to Class A". In `Special_Use_Airspace` and
  `Prohibited_Areas` the values are **strings** (`'4500'`).
- **SUA floors** are mostly MSL, and without ground elevation an MSL floor can't be compared to
  400 ft AGL. So only `SFC` floors, and AGL floors at or below 400 ft, count as reaching drone
  altitudes. The rest are listed as overhead.
- `WKHR_CODE = H24` means continuous operation. Any other code, such as a part-time Class D
  tower, carries its schedule in `WKHR_RMK`.
- **Stadiums** carry `LATITUDE`/`LONGITUDE` as DMS strings (`40-26-49.38N`); use the point
  geometry instead. There is no event schedule, so the 3 NM TFR is only "active during qualifying
  events".

## TFRs (tfr.faa.gov)

**Shapes** come from GeoServer WFS:

```
https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature
    &typeName=TFR:V_TFR_LOC&outputFormat=application/json&srsname=EPSG:4326
```

- Every published TFR nationwide as GeoJSON polygons in `[lng, lat]` order: about 75 features and
  95 KB in October 2026. drones.py fetches the whole feed and does the point-in-polygon and
  distance tests itself.
- Properties:
  - `NOTAM_KEY`, e.g. `6/9169-1-FDC-F`; the NOTAM number is the part before the first `-`.
  - `TITLE`: place and date range.
  - `LEGAL`: `SECURITY`, `HAZARDS`, `VIP`, `SPACE OPERATIONS`, `AIR SHOWS/SPORTS` or
    `UAS PUBLIC GATHERING`.
  - `CNS_LOCATION_ID` (the ARTCC), `STATE` and `LAST_MODIFICATION_DATETIME`.
- The feed includes **scheduled** TFRs, not just active ones, and has **no times or altitudes**.
  A renewed TFR is a new NOTAM on an identical shape, so the same polygon can appear several
  times. The map draws it once and links every NOTAM.

**Lists, text and detail pages:**

| Endpoint | Returns |
|---|---|
| `https://tfr.faa.gov/tfrapi/exportTfrList` | JSON list: `notam_id`, `type`, `facility`, `state`, `description`, `creation_date` |
| `https://tfr.faa.gov/tfrapi/getWebText?notamId=4/9383` | JSON `{notam_id, text}`, where `text` is the NOTAM's HTML page |
| `https://tfr.faa.gov/tfr3/?page=detail_4_9383` | Human detail page (`/` in the NOTAM id becomes `_`); drones.py links here |

**Reading TFR times.** The shape feed can't say whether a TFR is in force. So drones.py fetches
`getWebText` for each TFR that matters (one containing the point, or within the radius) and reads
the page text:

- `Beginning Date and Time` / `Ending Date and Time` give the overall window across all areas.
  - The format is `October 26, 2026 at 1300 UTC`, with `Effective Immediately` and `Permanent`
    for open ends.
  - A multi-area TFR (VIP movements) can give each area a narrower `Effective Date(s)` inside that
    window. drones.py uses the overall window, which is the conservative choice.
- Each area has an `Altitude:` line, such as `From the surface up to and including 4999 feet AGL`.
  drones.py reports the lowest floor and highest ceiling across areas.
- Status is judged at the time checked: now, or `preflight --at`.
  - **Active** is NO-FLY.
  - **Scheduled** is CAUTION with its window.
  - **Expired** is dropped (the feed can lag).
  - If the text can't be fetched or parsed, the TFR is treated as active.
- Known gap: some NOTAM texts (e.g. a UAS public gathering TFR at Fort Lauderdale) parse to no
  times and show as "unknown, treat as active".

## Aviation weather (NOAA Aviation Weather Center)

AWC is the free, public source that nearly every aviation weather product is built on. Consumer
weather APIs lack the primitives that gate a flight: surface wind and gusts, visibility, cloud
ceiling, flight category and convective hazards. `api.weather.gov` has observations but no TAFs.

| Property | Value |
|---|---|
| Base URL | `https://aviationweather.gov/api/data/` |
| Authentication | None |
| Rate limit | 100 requests a minute globally; about 1 a minute per thread recommended |
| Max results per query | 400 |
| Formats | `raw` (default), `json`, `geojson`, `xml`, `iwxxm`, `decoded` (varies by endpoint) |
| OpenAPI | <https://aviationweather.gov/data/schema/openapi.yaml>, the authoritative parameter reference |
| Cache files | gzipped XML at `/data/cache/*.cache.xml.gz`, refreshed every 1-10 min, for bulk consumers |

Errors are standard HTTP: `204` is an empty result (**not** `[]`), then `400`, `404`, `429` and
`500-504`. Sustained abuse triggers temporary IP blocks.

| Endpoint | Purpose | Used |
|---|---|---|
| `/metar` | Current observations at airports and AWOS sites | `metar`, `preflight` |
| `/taf` | Terminal Aerodrome Forecast (24-30 h) | `taf`, `preflight` |
| `/stationinfo` | Station metadata, including which reports a station files | nearest-station search |
| `/pirep` | Pilot reports: turbulence, icing, low ceilings | no |
| `/airsigmet` | SIGMETs and legacy AIRMETs; convective SIGMETs are the drone-relevant ones | no |
| `/gairmet` | Graphical AIRMETs, CONUS: surface wind, low-level wind shear, IFR | no |
| `/cwa` | Center Weather Advisories: short-fuse hazards (next 2 h) over an ARTCC region | no |
| `/airport` | Airport info | no |

### Query parameters

- **`/metar`**
  - Location: `ids` (ICAO `KCHO`, a comma list `KCHO,KIAD`, or a state `@VA`) or `bbox`
    (`lat0,lon0,lat1,lon1`). One of them is required.
  - `format`.
  - `taf=true` attaches the station's TAF as raw `rawTaf` text, not decoded.
  - `hours`: how far back (default 1.5).
  - `date`: `yyyymmdd_hhmm` or ISO, up to 15 days back.
- **`/taf`**
  - `ids` or `bbox`, as above.
  - `format`: `raw`, `json`, `geojson`, `xml` or `iwxxm`.
  - `metar`: include the latest observation.
  - `time`: `valid` (the forecast in force now, the default) or `issue`.
  - `date`.
- **`/stationinfo`**: `ids` or `bbox`. `format=json` gives `icaoId`, `site`, `state`, `lat`,
  `lon`, `elev`, `priority` and `siteType` (e.g. `["METAR", "TAF"]`; buoys come back with `[]`).
- **`/pirep`**
  - `id` (an airport as the center), `distance` (statute miles) or `bbox`.
  - `format`, and `age` (hours back).
  - `level`: flight level +/- 3,000 ft.
  - `inten`: `lgt`, `mod` or `sev`.
  - `date`.
- **`/airsigmet`**: `format`, `types` (`sigmet`; `airmet` is deprecated in favour of `/gairmet`),
  `hazard` (`conv`, `turb`, `ice`, `ifr`) and `level`.
- **`/gairmet`**
  - `product`: `sierra` (IFR, mountain obscuration), `tango` (turbulence, wind) or `zulu` (icing,
    freezing level).
  - `format`.
  - `hazard`: `turb-hi`, `turb-lo`, `llws`, `sfc_wind`, `ifr`, `mtn_obs`, `ice` or `fzlvl`.
    `sfc_wind`, `llws` and `ifr` are the three hazards that ground small drones most often.
  - `fore`: forecast hour `0`, `3`, `6`, `9` or `12`.
  - `date`.
- **`/cwa`**: `hazard` (`ts`, `turb`, `ice`, `ifr`, `pcpn`, `unk`), `format` and `date`.

AWC has removed parameters before: `recent`, `order`, `vfr`, `output`, `filter`, `fields`, and
`tempo` on METAR/TAF. Re-check the OpenAPI spec before relying on an edge-case field.

### METAR and TAF JSON

```jsonc
{
  "icaoId": "KFLL",
  "obsTime": 1792972380,                 // Unix seconds
  "temp": 25, "dewp": 20.6,              // deg C
  "wdir": 40,                            // degrees true, or "VRB"
  "wspd": 13,                            // knots, sustained
  "wgst": 22,                            // knots, gust; absent when no gust
  "visib": "10+",                        // statute miles; "10+" unrestricted, also 3, 0.5, "1/2"
  "altim": 1013.6,                       // hPa
  "rawOb": "METAR KFLL 252353Z 04013G22KT 10SM SCT026CB BKN036 BKN090 25/21 A2993 RMK ... CB ALQDS MOV SW ...",
  "lat": 26.0787, "lon": -80.1622, "elev": 1,
  "clouds": [ {"cover": "SCT", "base": 2600}, {"cover": "BKN", "base": 3600} ],   // base ft AGL
  "fltCat": "VFR"                        // VFR / MVFR / IFR / LIFR
}
```

- Optional METAR fields (`wgst`, `wxString`, `precip`, ...) are **omitted** when absent, not
  `null`.
- **The METAR cloud list drops the CB/TCU suffix.**
  - `SCT026CB` arrives as plain `SCT 2600`.
  - Remarks such as `CB ALQDS MOV SW` only exist in `rawOb`.
  - drones.py parses the raw report to restore both, since cumulonimbus is the cloud detail that
    matters most to a drone.
- There is no ceiling field. The ceiling is the lowest `BKN`, `OVC` or obscured layer (`OVX`, or
  vertical visibility).
- TAF top level: `icaoId`, `name`, `issueTime`, `validTimeFrom`/`validTimeTo` (Unix), `rawTAF`
  and `fcsts`, with one entry per period.
- Each TAF period:
  - `timeFrom`/`timeTo` (Unix) and `fcstChange` (`null` for the first period, else `FM`, `TEMPO`,
    `BECMG` or `PROB`).
  - `probability`, `wdir`/`wspd`/`wgst`, `visib` (`"6+"` = P6SM), `wxString` and `vertVis`.
  - `clouds`, which carry a `type` for CB/TCU, unlike METAR.
  - Wind shear fields, `icgTurb` and `temp`.
- **`FM` starts a new prevailing period. `TEMPO`, `PROB` and `BECMG` overlay the one in force** and
  list only what changes. An overlay's unchanged visibility comes back as `""`, not absent.
- TAF periods have no flight category; drones.py computes one from ceiling and visibility.

### Stations

- About **2,900** US stations file METARs, and about **860** of them issue TAFs. A station's TAF is
  attached to its METAR record, so a station with no METAR can never answer.
- **TAF-capable is not TAF-issuing.** At any moment a few dozen flagged stations have no current
  TAF. Causes include:
  - part-time fields, and military bases overnight and at weekends;
  - `AMD NOT SKED`;
  - stations whose METARs have stopped.

  North Perry (KHWO) is one. What AWC returns decides whether a forecast exists, never the flag
  alone.
- **Nearest observation is not nearest forecast.** Across the lower 48, the nearest station is
  observation-only at about two thirds of locations.
  - Median distance to the nearest METAR station is about 17 NM, and to the nearest TAF airport
    about 30 NM. In open country the nearest TAF can be 50-70 NM away.
  - In a city both usually land on the same airport (Miami: KMIA). Elsewhere they split: Boca
    Raton is observed at KBCT and forecast at KFXE.
- **Picking a station:**
  - Query `/stationinfo?bbox=` over rings of 15, 30, 60 and 120 NM until a qualifying station
    turns up.
  - Sort by true distance. The bbox result is unsorted, and its corners reach past the circle.
  - Send the nearest five candidates in one `ids=` request. The first that returns a report wins,
    and skipped stations are noted on stderr.
  - A TAF officially covers 5 SM around its airport. Ones farther than 10 NM get a "treat it as
    regional" warning.
- **Update cadence:** METARs roughly hourly (SPECI sooner on significant changes), TAFs every 6
  hours, G-AIRMETs on a 3-hour cycle.

## Live traffic and radar (adsb.fi, NOAA MRMS)

These are map-only layers, off unless `map --adsb` or `--radar` is passed. Neither feeds a
verdict or `--geojson`. The local server proxies both (`traffic.json`, `radar.json`), so the page
needs no keys and no CORS.

- **adsb.fi**:
  - Endpoint: `https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}`, with `dist` in NM
    and capped at 250. Free open data, about 1 request a second, attribution requested.
  - drones.py sends one request at a time across all open tabs (`TRAFFIC_MIN_INTERVAL` 1.1 s). The
    page polls every 3 s for its current view and pauses when hidden.
  - Fields used from `ac[]`:
    - `hex` is the marker id; the label is `flight`, else `r` (registration).
    - `lat`/`lon` and `alt_baro`.
    - `track`, falling back to `true_heading`, then `nav_heading`.
    - `category`: `A7` is rotorcraft.
    - `squawk` and `emergency`. Emergency squawks 7500/7600/7700, or a non-`none` `emergency`,
      get highlighted.
  - `alt_baro` is feet MSL barometric, or the string `"ground"`. Ground aircraft are dropped, and
    so are positions older than 60 s (`seen_pos`).
  - Known gap: a taxiing aircraft that doesn't set the ground flag reports a numeric altitude,
    even a negative one (`-275 ft` at DCA), so it still draws. Filtering on low altitude plus low
    ground speed would also hide a helicopter hovering low.
- **NOAA MRMS radar**:
  - WMS `https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows`, layer
    `conus_bref_qcd`: the quality-controlled base reflectivity mosaic for the lower 48, updated
    every 2 minutes.
  - The browser loads tiles straight from NOAA. drones.py only reads `GetCapabilities` for the
    `time` dimension and pins the newest frame, so a long-open map moves forward. The page
    re-checks every 2 minutes.
  - It draws at 60% opacity with `zIndex` 10, between the basemap and the airspace overlays.
    Without the `zIndex`, switching basemap covers the radar.
  - NOAA data is public; attribution is courteous, not required.

## Drone rules sources

The skill answers rules questions from an "at a glance" summary plus a reference of FAA links,
both in `SKILL.md`. It covers rules **in effect only**; proposed rules stay out until they are
final and effective.

**Status, checked 2026-10-26:**

| Item | Status | In the skill |
|---|---|---|
| 14 CFR Part 107 (with the 2021 over-people, night and recurrent-training changes) | in effect | yes |
| 49 USC 44809, the recreational exception; TRUST | in effect | yes |
| 14 CFR Part 89 Remote ID (compliance required since 2023-09-16) | in effect | yes |
| 14 CFR Part 48 registration ($5, 3 years) | in effect | yes |
| AC 107-2A (issued 2021-02-01) | active guidance | yes |
| Section 927 waivers (2024 Reauthorization Act; FAA notice 2026-04-01) | in effect | yes, under waivers |
| Part 107 knowledge test: image-based chart questions from 2026-10-26 | testing change only | no |
| Part 108 BVLOS rule (RIN 2120-AL82) | final rule at OIRA review since 2026-07-10, not published | no |
| Section 2209 critical infrastructure restrictions (proposed 14 CFR Part 74, NPRM 2026-05-06) | proposed; final expected late 2027 | no |

When Part 108 or the 2209 rule is published, check its effective date before adding it.

- **Part 108**, as proposed, adds a BVLOS path and would end most Part 107 BVLOS waivers.
- **Section 2209**, as proposed, adds UAS restrictions over critical infrastructure, which the
  airspace check would then need to read.

Sources for that status: [UAS Dashboard on Part 108](https://uasdashboard.com/guides/part-108-guide),
[Venable on the Section 2209 NPRM](https://www.venable.com/insights/publications/2026/05/faa-releases-section-2209-proposed-rule-on-drone),
[DroneLife on the 2026 knowledge test change](https://dronelife.com/2026/10/09/faa-part-107-test-changes-october-2026/).

**Rule facts the summary relies on**, verified against the eCFR text and FAA pages on 2026-10-26:

| Fact | Source |
|---|---|
| 400 ft AGL, or within 400 ft of a structure and no higher than 400 ft above its top | 14 CFR 107.51(b) |
| 87 kt (100 mph) groundspeed | 107.51(a) |
| 3 SM flight visibility from the control station | 107.51(c) |
| 500 ft below, 2,000 ft horizontally from clouds | 107.51(d) |
| Night and civil twilight: anti-collision lighting visible 3 SM, may be dimmed but not off | 107.29 |
| VLOS by the pilot and the person on the controls, or by a visual observer | 107.31 |
| Over people only if participating, under cover or in a stationary vehicle, or under Subpart D | 107.39 |
| Moving vehicle only over sparsely populated areas, never carrying others' property for hire | 107.25 |
| Class B, C, D and surface Class E need prior ATC authorization | 107.41 |
| Report serious injury or loss of consciousness, or damage over $500, within 10 calendar days | 107.9 |
| Recurrent knowledge within the previous 24 calendar months | 107.65 |
| Certificate: 16 or older, English proficiency, the UAG knowledge test | FAA "Become a drone pilot" page |
| Registration $5 for 3 years: per drone under Part 107, one for all drones when recreational; a recreational registration can't be used for Part 107 | FAA registration page |
| Recreational registration needed at 250 g (0.55 lb) or more | FAA registration page |
| Remote ID: standard Remote ID, a broadcast module, or fly inside a FRIA | FAA Remote ID page |
| Recreational conditions: fun only, TRUST, CBO guidelines, VLOS, give way, 400 ft in Class G, authorization in controlled airspace | 44809; FAA recreational flyers page |
| "Compensation is not the deciding factor; when in doubt assume Part 107" | FAA recreational flyers page |

**Fetching the sources:**

- **faa.gov** answers 403 to agent fetch tools and to plain `curl`. A browser user agent works:
  `curl -sL -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36" URL`.
  Strip the HTML down to `<main>` to read a page.
- **eCFR**:
  - Section URLs (`https://www.ecfr.gov/current/title-14/section-107.51`) redirect to the full
    path, and are the stable form to link.
  - The versioner API returns a section's text as XML:
    `https://www.ecfr.gov/api/versioner/v1/full/{YYYY-MM-DD}/title-14.xml?section=107.51`.
  - The API refuses requests without compression, so pass `curl --compressed`.
- **notams.aim.faa.gov** answers 403 to scripts. The NOTAM search link works in a browser.
- **Moved FAA URLs:**
  - The Remote Pilot Study Guide now lives under
    `/sites/faa.gov/files/regulations_policies/handbooks_manuals/aviation/remote_pilot_study_guide.pdf`.
  - The Aviation Weather Handbook is under
    `/regulationspolicies/handbooksmanuals/aviation/faa-h-8083-28-aviation-weather-handbook`,
    with no underscores.
  - The National Park Service has no stable drone page. The reference cites 36 CFR 1.5, the
    closure authority behind each park's drone rule.
- To find current FAA links, harvest the `href`s from `https://www.faa.gov/uas` and its section
  pages rather than guessing paths.

**Refreshing the reference:**

1. Check that every link in SKILL.md's FAA rules reference returns 200 with a browser user agent.
2. Check the Federal Register and the FAA UAS pages for final rules (Part 108, Section 2209) and
   new advisory circulars (AC 107-2A's successor).
3. Re-read the eCFR text of the sections in the facts table through the versioner API with a
   current date. Update the summary if a number changed.
4. Update the "checked" date in SKILL.md and here.

## Skill design

- **Airspace first.** Most questions are "can I fly here", so `airspace` is the default answer.
  - Weather (`metar`, `taf`, `preflight`) runs only when the user asks about weather, wind or a
    time.
  - Station weather rarely changes the answer while exploring.
- **Verdict card.** Answers are a few lines:
  - the verdict and the resolved address;
  - the surface class and LAANC ceiling, with how to get approval;
  - TFRs here and within 10 NM;
  - only the extra lines that apply;
  - a Google Maps link.

  Full output only on request.
- **Map on request only.** It is interactive and runs in the background with its URL handed over.
  - One map at a time, stopped when the user is done.
  - A PNG only when an image is asked for.
  - **Live view is opt-in:** `--adsb --radar` only for "live", "traffic" or "radar" requests.
- **Mission areas:** `airspace` checks one point. A site that straddles a LAANC grid or class
  boundary needs `nearby` with a radius about the site's size, planning to the lowest ceiling.
- **One skill file.** SKILL.md holds the FAA rules reference too, so installing is one plugin
  install or one folder copy.

| Setting | Value | Why |
|---|---|---|
| Default search radius (`map`, `nearby`) | 5 NM | the neighborhood; at 10 NM the map around an airport carried 900+ LAANC cells |
| Maximum radius | 30 NM | wider is regional planning, not a drone question |
| TFR reach | 30 NM, whatever the radius | few TFRs, and they decide whether you can fly at all; a TFR 6.4 NM out fell outside a 5 NM radius |
| Nearby TFRs in `airspace` | 10 NM | close enough that a change of plan or spot could put the flight inside |

## Map design

- **Airspace only by default.**
  - Kestrel-style controls: a translucent white, 12 px rounded search/layers card at top right,
    with segmented basemaps and two-column layer switches.
  - Matching zoom/location buttons sit at top left, and the card's bottom handle collapses the
    options.
  - Phones start collapsed, reserve room for the map buttons and use one column when opened.
- **Panel order differs from draw order.** Everyday layers come first in the panel: radar and
  traffic when enabled, then TFRs, LAANC and airports. The draw order keeps TFRs on top so they
  stay clickable. No legends or layer counts.
  - The custom control mirrors Leaflet's `overlayadd`/`overlayremove` events, so programmatic
    toggles, traffic polling and viewport refresh keep working.
- **Remembered state.** Only the panel's open/closed state is stored in localStorage, guarded for
  private modes.
  - The CLI launch coordinates, basemap and opt-in layers are never overridden by a remembered
    view.
  - Provider attribution stays visible.
  - A bottom status pill keeps fetch-age and failure warnings outside the collapsed card.
- **Always-listed toggles.**
  - Interactive maps list every thematic toggle, even when empty, so panning into a new area can
    fill them without rebuilding the control.
  - Exports keep the smaller snapshot panel (`ALWAYS_LISTED` keeps LAANC and TFRs).
  - The original center and radius stay fixed and unlisted (`FIXED_OVERLAYS`). In-map search adds
    a separate pin.
- **Styling.**
  - The radius ring and a tiny center dot are `#737373`.
  - Airports are blue dots without a ring: public `#2166ac` at 8.5 px, private `#6baed6` at 6 px.
  - `marker-size` takes a number, for sizes between simplestyle's names.
- **Performance.** Leaflet repositions every bound tooltip on each zoom step, and binding a
  permanent label to all 924 LAANC cells made zooming lag.
  - Labels are bound only to cells in view at zoom 14 and above.
  - Tiles skip loading mid-zoom (`updateWhenZooming: false`).
  - Wheel zoom uses `@luomus/leaflet-smooth-wheel-zoom@1.0.0` (MIT) for continuous zoom.
- **Title and attribution.** The page title is just the place. The attribution line credits the
  tile, traffic and radar providers without Leaflet's prefix.

### Interactive navigation and freshness

Browser maps use `MapData`; `--geojson` and `--png` keep the `survey()` snapshot around the launch
point.

- **`airspace.json?layer=KEY&bbox=W,S,E,N`** queries one enabled FAA layer by envelope.
  - Pan and zoom are debounced 250 ms, with four client workers and a shared four-request server
    limit.
  - Envelopes are rounded outward to a 0.05-degree grid, and those envelopes are the cache keys.
    The cache holds at most 256 entries and coalesces identical in-flight misses.
  - FAA results live six hours. Views over 120 NM across get a zoom-in warning.
  - Envelopes across the dateline split into two queries.
  - Stadium lookups expand by 3 NM, so a ring whose center is just outside the view is still
    fetched.
  - Old responses can't overwrite a newer viewport. Replacing LAANC data also rebuilds its
    ceiling-label references.
- **`tfrs.json?bbox=W,S,E,N`** shares a fifteen-minute nationwide shape cache, with separate
  fifteen-minute NOTAM-detail entries.
  - It selects shapes that intersect the viewport or lie within 30 NM of its center.
  - It annotates copies at the current UTC time, so scheduled/active/expired is never frozen into
    the raw cache.
  - `fetched_at` is the oldest relevant successful fetch. The browser schedules its refresh from
    that, not from each pan, so browsing can't postpone the deadline.
  - `next_change_at` triggers a redraw at a known start or end time. Hidden tabs check freshness
    when they return.
  - Failures carry stale/incomplete warnings and keep unknown timing conservative. Expired cached
    timing can't erase a restriction after a failed refresh.
  - The page keeps old shapes on errors and retries after 30 seconds. A successful empty response
    clears old TFRs.
- **`search.json?q=TEXT`** uses one shared, serialized `Locator` with its 1.1-second Nominatim
  pacing.
  - It returns up to five results, with a normalized 24-hour result cache.
  - Only Enter or the Search button submits. Raw coordinates skip geocoding.
  - Stale responses can't override a newer search.
  - Search never calls `check_point()` or gives a verdict.
- **Errors.** Live route errors are JSON with a `detail`: HTTP 400 for invalid input, 502 for
  upstream failures without usable cached data.
- **Startup snapshot.** `data.json` is the startup snapshot, and the fallback if the interactive
  source fails.

## Gotchas

- Map server processes are matched by `pkill -f "drones.py map"`. Keep `drones.py` in the command
  line when changing how the skill runs it.
- Network access is refused in the offline tests. A test that reaches the network fails rather
  than hits a live FAA service.
- Browser tests and `--png` use an installed browser; set `DRONES_BROWSER` if it isn't found.

## Commits

- One logical change per commit. Split larger work into several commits, and split by hunk when
  two changes share a file. Docs that describe a change ship with it.
- The message is a single line: lowercase, imperative, terse, no trailing period, no body.
- Never credit a coding agent, model, or harness: no `Co-Authored-By`, no "Generated with", in
  commits or PR titles and descriptions. This overrides any default attribution.
- Don't commit or push unless asked. Every push to main cuts a release.
