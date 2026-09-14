"""
WeedManager NZ -> Leaflet Dashboard
====================================
1. Downloads weed/track/work-area layers for each configured project from the
   WeedManager WFS API.
2. Combines everything by layer type (points / lines / polygons / work areas).
3. Builds a single-file Leaflet HTML dashboard with:
     - a heatmap per species
     - a per-species summary panel (count / area / date range)
     - hotspot ranking (densest ~300m clusters per species)
     - a collapsible, tabbed, mobile-friendly control panel
4. Pushes the generated HTML to a GitHub repo (served via GitHub Pages).

Configuration
-------------
Project credentials are NOT hardcoded here. Provide them one of two ways:

  1. Environment variable WM_PROJECTS as a JSON array, e.g.:
       export WM_PROJECTS='[{"api_key":"...","project_id":"445"}, ...]'

  2. A local projects.json file (same shape) next to this script:
       [
         {"api_key": "xxxx", "project_id": "445"},
         {"api_key": "yyyy", "project_id": "314"}
       ]
     Add projects.json to .gitignore so it never gets committed.

GITHUB_TOKEN and GITHUB_REPO are read from the environment as before.
"""

import os
import json
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter, Retry

# ==============================================================================
# CONFIGURATION
# ==============================================================================

BASE_WFS_URL = "https://io.weedmanager.nz/geo/wm/wfs"
PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "mafew-lgtm/weedmanager-map")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
FILE_PATH_IN_REPO = "index.html"

LAYER_TYPES = {
    "points": "wm:default-project-weeds-points",
    "polygons": "wm:default-project-weeds-polygons",
    "lines": "wm:default-project-weeds-linestrings",
    "tracks": "wm:default-project-tracks",
    "work_areas": "wm:default-project-work-areas",
}

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weedmanager_map")


@dataclass(frozen=True)
class ProjectConfig:
    api_key: str
    project_id: str


def load_project_config() -> list[ProjectConfig]:
    """Load project credentials from WM_PROJECTS env var, else projects.json."""
    raw = os.getenv("WM_PROJECTS")
    source = "WM_PROJECTS env var"

    if not raw:
        if os.path.exists(PROJECTS_FILE):
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                raw = f.read()
            source = PROJECTS_FILE
        else:
            raise RuntimeError(
                "No project config found. Set WM_PROJECTS env var or create "
                f"{PROJECTS_FILE} — see the module docstring for the format."
            )

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse project config from {source}: {e}") from e

    configs = []
    for entry in entries:
        key = str(entry.get("api_key", "")).strip()
        proj_id = str(entry.get("project_id", "")).strip()
        if not key or not proj_id:
            log.warning("Skipping incomplete project entry: %r", entry)
            continue
        configs.append(ProjectConfig(api_key=key, project_id=proj_id))

    if not configs:
        raise RuntimeError(f"Project config from {source} contained no usable entries.")

    return configs


def build_session() -> requests.Session:
    """HTTP session with automatic retries on transient failures."""
    session = requests.Session()
    retries = Retry(
        total=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


# ==============================================================================
# STEP 1: FETCH
# ==============================================================================
def fetch_wfs_layer(session: requests.Session, api_key: str, project_id: str, layer_name: str) -> dict:
    """Fetch one GeoJSON layer for one project. Never raises — logs and returns empty on failure."""
    url = f"{BASE_WFS_URL}/{api_key}/{project_id}"
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer_name,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    empty = {"type": "FeatureCollection", "features": []}

    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as e:
        log.error("Fetch failed for %s (project %s): %s", layer_name, project_id, e)
        return empty

    content_type = response.headers.get("Content-Type", "").lower()
    if "json" not in content_type:
        log.warning("Non-JSON payload for %s (project %s), Content-Type=%s", layer_name, project_id, content_type)
        return empty

    try:
        return response.json()
    except ValueError as e:
        log.error("Invalid JSON for %s (project %s): %s", layer_name, project_id, e)
        return empty


def aggregate_data(projects: list[ProjectConfig]) -> dict:
    """Download every layer for every project and combine by layer type."""
    combined = {key: {"type": "FeatureCollection", "features": []} for key in LAYER_TYPES}
    session = build_session()

    log.info("--- 1/3: Fetching data for %d project(s) ---", len(projects))

    for cfg in projects:
        log.info("Project %s (key %s...)", cfg.project_id, cfg.api_key[:4])

        for combined_key, layer_name in LAYER_TYPES.items():
            data = fetch_wfs_layer(session, cfg.api_key, cfg.project_id, layer_name)
            features = data.get("features", [])
            combined[combined_key]["features"].extend(features)

    log.info("Summary:")
    for key, fc in combined.items():
        log.info("  %-11s %d", key + ":", len(fc["features"]))

    return combined


# ==============================================================================
# STEP 2: BUILD LEAFLET HTML
# ==============================================================================
def build_leaflet_html(geo: dict, build_time: str) -> str:
    """Generate a single-file Leaflet dashboard with heatmaps, summary panel, and hotspots."""
    map_payload = {
        "points": geo["points"],
        "lines": geo["lines"],
        "polygons": geo["polygons"],
        "work_areas": geo["work_areas"],
    }

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WeedManager NZ Dashboard</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.heat/0.2.0/leaflet-heat.js"></script>

    <style>
        html, body, #map {{ width: 100%; height: 100%; margin: 0; padding: 0; }}
        * {{ box-sizing: border-box; }}

        .info-panel {{
            background: rgba(255,255,255,0.97);
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 13px;
            width: 290px;
            max-width: 88vw;
            overflow: hidden;
        }}
        .panel-header {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px 12px; background: #2c3e50; color: #fff; font-weight: 600;
            cursor: pointer;
        }}
        .panel-toggle {{
            background: none; border: none; color: #fff; font-size: 16px; cursor: pointer;
            line-height: 1; padding: 0 4px;
        }}
        .panel-tabs {{ display: flex; border-bottom: 1px solid #ddd; }}
        .tab-btn {{
            flex: 1; padding: 8px 4px; border: none; background: #f5f5f5; cursor: pointer;
            font-size: 12px; border-right: 1px solid #ddd;
        }}
        .tab-btn:last-child {{ border-right: none; }}
        .tab-btn.active {{ background: #fff; font-weight: 600; border-bottom: 2px solid #3498db; }}
        .panel-body {{ max-height: 340px; overflow-y: auto; padding: 10px 12px; }}
        .info-panel.collapsed .panel-tabs,
        .info-panel.collapsed .panel-body,
        .info-panel.collapsed .panel-footer {{ display: none; }}
        .panel-footer {{ padding: 6px 12px; font-size: 10px; color: #888; border-top: 1px solid #eee; }}

        .tab-content select {{ width: 100%; margin: 4px 0 8px 0; padding: 4px; }}
        .legend-box {{ margin-top: 4px; line-height: 18px; }}
        .legend-row {{ display: flex; align-items: center; font-size: 12px; margin-bottom: 3px; }}
        .badge {{ width: 12px; height: 12px; margin-right: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}

        .summary-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
        .summary-table th, .summary-table td {{ text-align: left; padding: 4px 6px; border-bottom: 1px solid #eee; }}
        .summary-table th {{ font-size: 11px; color: #666; }}

        .hotspot-note, .empty-msg {{ color: #888; font-size: 11px; margin-bottom: 8px; }}
        .hotspot-controls {{ margin-bottom: 8px; }}
        .sort-group {{ display: flex; gap: 4px; margin-bottom: 6px; }}
        .sort-btn {{
            flex: 1; padding: 4px 6px; font-size: 11px; border: 1px solid #ddd;
            background: #f5f5f5; border-radius: 4px; cursor: pointer;
        }}
        .sort-btn.active {{ background: #3498db; color: #fff; border-color: #3498db; }}
        .marker-toggle {{ display: block; font-size: 11px; color: #444; }}
        .hotspot-row {{ display: flex; align-items: center; gap: 6px; padding: 6px 0; border-bottom: 1px solid #eee; font-size: 12px; }}
        .hotspot-rank {{
            width: 18px; height: 18px; border-radius: 50%; background: #eee; color: #333;
            font-size: 10px; font-weight: 700; display: flex; align-items: center;
            justify-content: center; flex-shrink: 0;
        }}
        .hotspot-info {{ flex: 1; min-width: 0; }}
        .hotspot-label {{ font-weight: 600; }}
        .hotspot-sub {{ color: #888; font-size: 10.5px; }}
        .view-btn {{
            padding: 3px 8px; border: 1px solid #3498db; background: #fff; color: #3498db;
            border-radius: 4px; cursor: pointer; font-size: 11px; white-space: nowrap;
        }}
        .view-btn:hover {{ background: #3498db; color: #fff; }}

        #data-warning {{
            position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
            background: #fff3cd; border: 1px solid #ffc107; padding: 6px 12px;
            border-radius: 4px; font-family: sans-serif; font-size: 12px; z-index: 1000;
            display: none;
        }}

        @media (max-width: 600px) {{
            .info-panel {{ width: 230px; font-size: 12px; }}
            .panel-body {{ max-height: 220px; }}
        }}
    </style>
</head>
<body>

<div id="map"></div>
<div id="data-warning">No point features were returned — check the WFS credentials/project IDs.</div>

<script>
    const geoData = {json.dumps(map_payload)};
    const BUILD_TIME = {json.dumps(build_time)};

    const map = L.map('map', {{ center: [-41.2865, 174.7762], zoom: 6 }});

    const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        attribution: 'Tiles &copy; Esri &mdash; WeedManager NZ'
    }}).addTo(map);

    const osmBase = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap'
    }});

    if (!geoData.points || !geoData.points.features || geoData.points.features.length === 0) {{
        document.getElementById('data-warning').style.display = 'block';
    }}

    // ---- Species color assignment ----
    const PALETTE = ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#1abc9c', '#3498db', '#9b59b6'];
    const speciesColors = {{}};
    let paletteIdx = 0;

    function speciesOf(feature) {{
        return (feature.properties && (feature.properties.species || feature.properties.name)) || 'Unspecified';
    }}

    function colorFor(name) {{
        if (!speciesColors[name]) {{
            speciesColors[name] = PALETTE[paletteIdx % PALETTE.length];
            paletteIdx++;
        }}
        return speciesColors[name];
    }}

    (geoData.points.features || []).forEach(f => colorFor(speciesOf(f)));

    // ---- Vector layers ----
    const pointLayer = L.geoJSON(geoData.points, {{
        pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
            radius: 5,
            fillColor: colorFor(speciesOf(f)),
            color: '#ffffff',
            weight: 1,
            fillOpacity: 0.8
        }}),
        onEachFeature: (f, l) => l.bindPopup(`<b>Species:</b> ${{speciesOf(f)}}`)
    }}).addTo(map);

    const lineLayer = L.geoJSON(geoData.lines, {{ style: {{ color: '#f39c12', weight: 3 }} }}).addTo(map);
    const polyLayer = L.geoJSON(geoData.polygons, {{ style: {{ color: '#e74c3c', fillOpacity: 0.3 }} }}).addTo(map);
    const workLayer = L.geoJSON(geoData.work_areas, {{ style: {{ color: '#3498db', dashArray: '4', fillOpacity: 0.1 }} }}).addTo(map);

    const bufferGroup = L.layerGroup().addTo(map);

    // ---- Per-species heatmaps ----
    const heatPointsBySpecies = {{}};
    (geoData.points.features || []).forEach(f => {{
        if (!f.geometry) return;
        let coordsList = [];
        if (f.geometry.type === 'Point') coordsList = [f.geometry.coordinates];
        else if (f.geometry.type === 'MultiPoint') coordsList = f.geometry.coordinates;
        else return;

        const spName = speciesOf(f);
        coordsList.forEach(coords => {{
            const lng = Number(coords[0]);
            const lat = Number(coords[1]);
            if (isNaN(lat) || isNaN(lng)) return;
            if (!heatPointsBySpecies[spName]) heatPointsBySpecies[spName] = [];
            heatPointsBySpecies[spName].push([lat, lng, 1.0]);
        }});
    }});

    // Heatmap layers are created but NOT added to the map or the layer
    // control here — visibility is driven entirely by the species dropdown
    // (updateHeatmaps below) so the two controls never fight over state.
    const speciesHeatmaps = {{}};
    Object.keys(heatPointsBySpecies).forEach(spName => {{
        const baseColor = colorFor(spName);
        speciesHeatmaps[spName] = L.heatLayer(heatPointsBySpecies[spName], {{
            radius: 25,
            blur: 15,
            minOpacity: 0.35,
            maxZoom: 15,
            gradient: {{ 0.2: '#0000ff', 0.6: baseColor, 1.0: '#ff0000' }}
        }});
    }});

    function updateHeatmaps() {{
        const sel = document.getElementById('spSelect') ? document.getElementById('spSelect').value : 'ALL';
        Object.keys(speciesHeatmaps).forEach(spName => {{
            const layer = speciesHeatmaps[spName];
            const shouldShow = (sel === 'ALL' || sel === spName);
            const isShown = map.hasLayer(layer);
            if (shouldShow && !isShown) map.addLayer(layer);
            else if (!shouldShow && isShown) map.removeLayer(layer);
        }});
    }}

    function updateBuffers() {{
        bufferGroup.clearLayers();
        const sel = document.getElementById('spSelect') ? document.getElementById('spSelect').value : 'ALL';

        (geoData.points.features || []).forEach(f => {{
            if (!f.geometry || (f.geometry.type !== 'Point' && f.geometry.type !== 'MultiPoint')) return;
            const spName = speciesOf(f);
            if (sel !== 'ALL' && spName !== sel) return;

            try {{
                const buffered = turf.buffer(f, 0.2, {{ units: 'kilometers' }});
                L.geoJSON(buffered, {{
                    style: {{
                        color: colorFor(spName),
                        weight: 1,
                        fillColor: colorFor(spName),
                        fillOpacity: 0.25
                    }}
                }}).addTo(bufferGroup);
            }} catch (e) {{
                console.warn('Buffer calculation failed for feature', e);
            }}
        }});
    }}

    function updateMap() {{
        updateBuffers();
        updateHeatmaps();
        refreshActiveTab();
    }}

    // ---- Per-species summary (Summary tab) ----
    const DATE_KEYS = ['date', 'obs_date', 'observation_date', 'recorded_on', 'recordeddate',
                        'created', 'created_at', 'timestamp', 'survey_date', 'datecreated'];

    function extractDate(props) {{
        if (!props) return null;
        for (const key of Object.keys(props)) {{
            if (DATE_KEYS.includes(key.toLowerCase())) {{
                const d = new Date(props[key]);
                if (!isNaN(d.getTime())) return d;
            }}
        }}
        return null;
    }}

    function computeSpeciesSummary() {{
        const summary = {{}};

        function ensure(name) {{
            if (!summary[name]) {{
                summary[name] = {{ name, color: colorFor(name), pointCount: 0, areaM2: 0, minDate: null, maxDate: null }};
            }}
            return summary[name];
        }}

        function applyDate(entry, d) {{
            if (!d) return;
            if (!entry.minDate || d < entry.minDate) entry.minDate = d;
            if (!entry.maxDate || d > entry.maxDate) entry.maxDate = d;
        }}

        (geoData.points.features || []).forEach(f => {{
            const entry = ensure(speciesOf(f));
            entry.pointCount++;
            applyDate(entry, extractDate(f.properties));
        }});

        (geoData.polygons.features || []).forEach(f => {{
            const entry = ensure(speciesOf(f));
            try {{ entry.areaM2 += turf.area(f); }} catch (e) {{ /* skip invalid geometry */ }}
            applyDate(entry, extractDate(f.properties));
        }});

        return Object.values(summary).sort((a, b) => b.pointCount - a.pointCount);
    }}

    function renderSummaryTab() {{
        const stats = computeSpeciesSummary();
        const container = document.getElementById('summaryContent');
        if (stats.length === 0) {{
            container.innerHTML = '<div class="empty-msg">No species data available.</div>';
            return;
        }}

        let html = '<table class="summary-table"><thead><tr><th></th><th>Species</th><th>Pts</th><th>Area (ha)</th><th>Dates</th></tr></thead><tbody>';
        stats.forEach(s => {{
            const area = s.areaM2 > 0 ? (s.areaM2 / 10000).toFixed(2) : '—';
            const dateRange = (s.minDate && s.maxDate)
                ? `${{s.minDate.toLocaleDateString()}} – ${{s.maxDate.toLocaleDateString()}}`
                : '—';
            html += `<tr><td><span class="badge" style="background:${{s.color}}"></span></td><td>${{s.name}}</td><td>${{s.pointCount}}</td><td>${{area}}</td><td>${{dateRange}}</td></tr>`;
        }});
        html += '</tbody></table>';
        container.innerHTML = html;
    }}

    // ---- Hotspot ranking (Hotspots tab) ----
    // Adjacent ~300m grid cells (0.003 degrees) are merged into connected
    // clusters (8-neighbor flood fill) so one real infestation isn't
    // artificially split by grid lines. This is still a coarse heuristic,
    // not true geodesic clustering — good enough to flag "go look here" spots.
    const HOTSPOT_CELL_DEG = 0.003;
    const HOTSPOT_MIN_CLUSTER_POINTS = 3;
    const HOTSPOT_MAX_PER_SPECIES = 5;
    const HOTSPOT_MAX_TOTAL = 12;

    let hotspotSortBy = 'count';
    let hotspotMarkersVisible = false;
    const hotspotMarkersGroup = L.layerGroup(); // controlled only by the tab's own checkbox

    function computeHotspots(sortBy) {{
        const cellsBySpecies = {{}};
        const totalsBySpecies = {{}};

        (geoData.points.features || []).forEach(f => {{
            if (!f.geometry) return;
            let coordsList = [];
            if (f.geometry.type === 'Point') coordsList = [f.geometry.coordinates];
            else if (f.geometry.type === 'MultiPoint') coordsList = f.geometry.coordinates;
            else return;

            const spName = speciesOf(f);
            const date = extractDate(f.properties);

            coordsList.forEach(coords => {{
                const lng = Number(coords[0]);
                const lat = Number(coords[1]);
                if (isNaN(lat) || isNaN(lng)) return;

                totalsBySpecies[spName] = (totalsBySpecies[spName] || 0) + 1;

                const cellLat = Math.floor(lat / HOTSPOT_CELL_DEG);
                const cellLng = Math.floor(lng / HOTSPOT_CELL_DEG);
                const key = cellLat + '_' + cellLng;

                if (!cellsBySpecies[spName]) cellsBySpecies[spName] = {{}};
                if (!cellsBySpecies[spName][key]) {{
                    cellsBySpecies[spName][key] = {{ cellLat: cellLat, cellLng: cellLng, points: [] }};
                }}
                cellsBySpecies[spName][key].points.push({{ lat: lat, lng: lng, date: date }});
            }});
        }});

        const clustersBySpecies = {{}};
        Object.keys(cellsBySpecies).forEach(spName => {{
            const cells = cellsBySpecies[spName];
            const visited = {{}};
            const clusters = [];

            Object.keys(cells).forEach(key => {{
                if (visited[key]) return;
                const queue = [key];
                visited[key] = true;
                const clusterCells = [];

                while (queue.length) {{
                    const k = queue.shift();
                    const cell = cells[k];
                    clusterCells.push(cell);
                    for (let dLat = -1; dLat <= 1; dLat++) {{
                        for (let dLng = -1; dLng <= 1; dLng++) {{
                            if (dLat === 0 && dLng === 0) continue;
                            const nKey = (cell.cellLat + dLat) + '_' + (cell.cellLng + dLng);
                            if (cells[nKey] && !visited[nKey]) {{
                                visited[nKey] = true;
                                queue.push(nKey);
                            }}
                        }}
                    }}
                }}

                const allPoints = [];
                clusterCells.forEach(c => allPoints.push.apply(allPoints, c.points));
                if (allPoints.length < HOTSPOT_MIN_CLUSTER_POINTS) return;

                let latSum = 0, lngSum = 0, lastSeen = null;
                allPoints.forEach(p => {{
                    latSum += p.lat;
                    lngSum += p.lng;
                    if (p.date && (!lastSeen || p.date > lastSeen)) lastSeen = p.date;
                }});

                clusters.push({{
                    species: spName,
                    count: allPoints.length,
                    lat: latSum / allPoints.length,
                    lng: lngSum / allPoints.length,
                    pctOfSpecies: totalsBySpecies[spName] ? (allPoints.length / totalsBySpecies[spName] * 100) : 0,
                    lastSeen: lastSeen
                }});
            }});

            clusters.sort((a, b) => b.count - a.count);
            clustersBySpecies[spName] = clusters.slice(0, HOTSPOT_MAX_PER_SPECIES);
        }});

        let combined = [];
        Object.keys(clustersBySpecies).forEach(sp => {{
            combined = combined.concat(clustersBySpecies[sp]);
        }});

        const sel = document.getElementById('spSelect') ? document.getElementById('spSelect').value : 'ALL';
        if (sel !== 'ALL') {{
            combined = combined.filter(c => c.species === sel);
        }}

        if (sortBy === 'recent') {{
            combined.sort((a, b) => {{
                const at = a.lastSeen ? a.lastSeen.getTime() : -Infinity;
                const bt = b.lastSeen ? b.lastSeen.getTime() : -Infinity;
                return bt - at;
            }});
        }} else {{
            combined.sort((a, b) => b.count - a.count);
        }}

        return combined.slice(0, HOTSPOT_MAX_TOTAL);
    }}

    function renderHotspotMarkers(hotspots) {{
        hotspotMarkersGroup.clearLayers();
        if (!hotspotMarkersVisible) return;

        hotspots.forEach((h, i) => {{
            const color = colorFor(h.species);
            const icon = L.divIcon({{
                className: '',
                html: '<div style="background:' + color + ';color:#fff;border-radius:50%;width:24px;height:24px;' +
                      'display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:bold;' +
                      'border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.4);">' + (i + 1) + '</div>',
                iconSize: [24, 24],
                iconAnchor: [12, 12]
            }});

            const lastSeenText = h.lastSeen ? h.lastSeen.toLocaleDateString() : 'unknown';
            L.marker([h.lat, h.lng], {{ icon: icon }})
                .bindPopup('<b>#' + (i + 1) + ' ' + h.species + '</b><br>' + h.count + ' points (' +
                           h.pctOfSpecies.toFixed(0) + '% of species)<br>Last seen: ' + lastSeenText)
                .addTo(hotspotMarkersGroup);
        }});

        if (!map.hasLayer(hotspotMarkersGroup)) {{
            map.addLayer(hotspotMarkersGroup);
        }}
    }}

    function renderHotspotsTab() {{
        const hotspots = computeHotspots(hotspotSortBy);
        const container = document.getElementById('hotspotContent');

        let controlsHtml = '<div class="hotspot-controls">' +
            '<div class="sort-group">' +
                '<button class="sort-btn ' + (hotspotSortBy === 'count' ? 'active' : '') + '" data-sort="count">Most points</button>' +
                '<button class="sort-btn ' + (hotspotSortBy === 'recent' ? 'active' : '') + '" data-sort="recent">Most recent</button>' +
            '</div>' +
            '<label class="marker-toggle"><input type="checkbox" id="hotspotMarkersToggle" ' +
                (hotspotMarkersVisible ? 'checked' : '') + '> Show numbered markers on map</label>' +
        '</div>';

        if (hotspots.length === 0) {{
            container.innerHTML = controlsHtml + '<div class="empty-msg">No dense clusters detected (~300m grid, min 3 pts).</div>';
            wireHotspotControls([]);
            return;
        }}

        let html = controlsHtml;
        hotspots.forEach((h, i) => {{
            const lastSeenText = h.lastSeen ? h.lastSeen.toLocaleDateString() : 'no date data';
            html += '<div class="hotspot-row" data-idx="' + i + '">' +
                '<span class="hotspot-rank">' + (i + 1) + '</span>' +
                '<span class="badge" style="background:' + colorFor(h.species) + '"></span>' +
                '<div class="hotspot-info">' +
                    '<div class="hotspot-label">' + h.species + '</div>' +
                    '<div class="hotspot-sub">' + h.count + ' pts &middot; ' + h.pctOfSpecies.toFixed(0) +
                        '% of species &middot; last seen ' + lastSeenText + '</div>' +
                '</div>' +
                '<button class="view-btn" data-idx="' + i + '">View</button>' +
            '</div>';
        }});

        container.innerHTML = html;
        wireHotspotControls(hotspots);
    }}

    function wireHotspotControls(hotspots) {{
        const container = document.getElementById('hotspotContent');

        container.querySelectorAll('.sort-btn').forEach(btn => {{
            btn.addEventListener('click', () => {{
                hotspotSortBy = btn.dataset.sort;
                renderHotspotsTab();
            }});
        }});

        const markerToggle = document.getElementById('hotspotMarkersToggle');
        if (markerToggle) {{
            markerToggle.addEventListener('change', () => {{
                hotspotMarkersVisible = markerToggle.checked;
                if (!hotspotMarkersVisible) {{
                    map.removeLayer(hotspotMarkersGroup);
                    hotspotMarkersGroup.clearLayers();
                }} else {{
                    renderHotspotMarkers(hotspots);
                }}
            }});
        }}

        container.querySelectorAll('.view-btn').forEach(btn => {{
            btn.addEventListener('click', () => {{
                const h = hotspots[parseInt(btn.dataset.idx, 10)];
                map.flyTo([h.lat, h.lng], 16, {{ duration: 0.75 }});

                const select = document.getElementById('spSelect');
                if (select) {{
                    select.value = h.species;
                    updateMap();
                }}

                const lastSeenText = h.lastSeen ? h.lastSeen.toLocaleDateString() : 'unknown';
                L.popup()
                    .setLatLng([h.lat, h.lng])
                    .setContent('<b>' + h.species + '</b> hotspot &mdash; ' + h.count + ' points (' +
                                h.pctOfSpecies.toFixed(0) + '% of species)<br>Last seen: ' + lastSeenText)
                    .openOn(map);
            }});
        }});

        if (hotspotMarkersVisible) {{
            renderHotspotMarkers(hotspots);
        }}
    }}

    function refreshActiveTab() {{
        const activeBtn = document.querySelector('.tab-btn.active');
        if (!activeBtn) return;
        const tab = activeBtn.dataset.tab;
        if (tab === 'summary') renderSummaryTab();
        if (tab === 'hotspots') renderHotspotsTab();
    }}

    // ---- Control panel (Filter / Summary / Hotspots) ----
    const infoControl = L.control({{ position: 'bottomleft' }});
    infoControl.onAdd = function () {{
        const div = L.DomUtil.create('div', 'info-panel');

        let filterOptions = '<option value="ALL">-- All Species --</option>';
        Object.keys(speciesColors).sort().forEach(sp => {{
            filterOptions += `<option value="${{sp}}">${{sp}}</option>`;
        }});

        let legendRows = '';
        Object.keys(speciesColors).sort().forEach(sp => {{
            legendRows += `<div class="legend-row"><span class="badge" style="background:${{speciesColors[sp]}}"></span>${{sp}}</div>`;
        }});

        div.innerHTML = `
            <div class="panel-header" id="panelHeader">
                <span>WeedManager</span>
                <button class="panel-toggle" id="panelToggle">&minus;</button>
            </div>
            <div class="panel-tabs">
                <button class="tab-btn active" data-tab="filter">Filter</button>
                <button class="tab-btn" data-tab="summary">Summary</button>
                <button class="tab-btn" data-tab="hotspots">Hotspots</button>
            </div>
            <div class="panel-body">
                <div class="tab-content" data-tab-content="filter">
                    <label style="font-weight:600;">Species</label>
                    <select id="spSelect">${{filterOptions}}</select>
                    <div class="legend-box"><b>200m Buffer Key</b>${{legendRows}}</div>
                </div>
                <div class="tab-content" data-tab-content="summary" style="display:none">
                    <div id="summaryContent"></div>
                </div>
                <div class="tab-content" data-tab-content="hotspots" style="display:none">
                    <div id="hotspotContent"></div>
                </div>
            </div>
            <div class="panel-footer">Last updated: ${{BUILD_TIME}}</div>
        `;

        L.DomEvent.disableScrollPropagation(div);
        L.DomEvent.disableClickPropagation(div);
        return div;
    }};
    infoControl.addTo(map);

    // Wire up the dropdown (re-created dynamically inside the control, so this
    // has to happen after infoControl.addTo(map)).
    document.getElementById('spSelect').addEventListener('change', updateMap);

    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {{
        btn.addEventListener('click', () => {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
            btn.classList.add('active');

            const tab = btn.dataset.tab;
            document.querySelector(`.tab-content[data-tab-content="${{tab}}"]`).style.display = 'block';

            if (tab === 'summary') renderSummaryTab();
            if (tab === 'hotspots') renderHotspotsTab();
        }});
    }});

    // Collapsible panel (defaults collapsed on narrow/mobile screens)
    const panelEl = document.querySelector('.info-panel');
    document.getElementById('panelToggle').addEventListener('click', (e) => {{
        e.stopPropagation();
        panelEl.classList.toggle('collapsed');
    }});
    document.getElementById('panelHeader').addEventListener('click', () => {{
        panelEl.classList.toggle('collapsed');
    }});
    if (window.innerWidth < 700) {{
        panelEl.classList.add('collapsed');
    }}

    // ---- Layer toggle control (top-right) ----
    const overlayLayers = {{
        "200m Buffers": bufferGroup,
        "Points": pointLayer,
        "Lines": lineLayer,
        "Polygons": polyLayer,
        "Work Areas": workLayer
    }};

    L.control.layers(
        {{ "Satellite Imagery": esriSat, "OpenStreetMap": osmBase }},
        overlayLayers,
        {{ collapsed: true, position: 'topright' }}
    ).addTo(map);

    updateMap();

    const allBounds = L.featureGroup([pointLayer, lineLayer, polyLayer, workLayer]).getBounds();
    if (allBounds.isValid()) {{
        map.fitBounds(allBounds, {{ padding: [20, 20] }});
    }} else {{
        map.fitBounds([[-47.0, 166.0], [-34.0, 179.0]]);
    }}
</script>
</body>
</html>
"""


# ==============================================================================
# STEP 3: GITHUB DEPLOYMENT
# ==============================================================================
def upload_to_github(html_content: str) -> None:
    """Create or update index.html in the target GitHub repo."""
    log.info("--- 3/3: Pushing update to GitHub ---")

    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not set — export it before running this script.")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError(f"GITHUB_REPO looks invalid: {GITHUB_REPO!r} (expected 'owner/repo').")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH_IN_REPO}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    session = build_session()

    sha = None
    existing = session.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=REQUEST_TIMEOUT)
    if existing.status_code == 200:
        sha = existing.json().get("sha")
    elif existing.status_code != 404:
        log.warning("Unexpected status checking for existing file: %s %s", existing.status_code, existing.text)

    payload = {
        "message": "Update weed map dashboard",
        "content": base64.b64encode(html_content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    put_res = session.put(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)

    if put_res.status_code in (200, 201):
        owner, repo = GITHUB_REPO.split("/", 1)
        log.info("Success! Map updated: https://%s.github.io/%s/", owner, repo)
    else:
        raise RuntimeError(f"GitHub upload failed: {put_res.status_code} - {put_res.text}")


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main() -> None:
    projects = load_project_config()
    geo = aggregate_data(projects)
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_str = build_leaflet_html(geo, build_time)
    upload_to_github(html_str)


if __name__ == "__main__":
    main()