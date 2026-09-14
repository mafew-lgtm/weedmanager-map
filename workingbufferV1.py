"""
WeedManager NZ -> Leaflet Dashboard
====================================
1. Downloads weed/track/work-area layers for each configured project from the
   WeedManager WFS API.
2. Combines everything by layer type (points / lines / polygons / work areas).
3. Builds a single-file Leaflet HTML dashboard with a heatmap per species.
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
# STEP 2: BUILD LEAFLET HTML (PER-SPECIES HEATMAPS)
# ==============================================================================
def build_leaflet_html(geo: dict) -> str:
    """Generate a single-file Leaflet dashboard with one heatmap layer per species."""
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

        .leaflet-custom-control {{
            background: rgba(255, 255, 255, 0.95);
            padding: 12px 14px;
            border-radius: 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 13px;
            max-width: 240px;
        }}
        .leaflet-custom-control select {{ width: 100%; margin-top: 4px; padding: 4px; }}
        .legend-box {{ margin-top: 8px; max-height: 140px; overflow-y: auto; line-height: 18px; }}
        .legend-row {{ display: flex; align-items: center; font-size: 12px; margin-bottom: 3px; }}
        .badge {{ width: 12px; height: 12px; margin-right: 6px; border-radius: 50%; display: inline-block; }}
        #data-warning {{
            position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
            background: #fff3cd; border: 1px solid #ffc107; padding: 6px 12px;
            border-radius: 4px; font-family: sans-serif; font-size: 12px; z-index: 1000;
            display: none;
        }}
    </style>
</head>
<body>

<div id="map"></div>
<div id="data-warning">No point features were returned — check the WFS credentials/project IDs.</div>

<script>
    const geoData = {json.dumps(map_payload)};

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

    (geoData.points.features || []).forEach(f => {{
        const name = speciesOf(f);
        if (!speciesColors[name]) {{
            speciesColors[name] = PALETTE[paletteIdx % PALETTE.length];
            paletteIdx++;
        }}
    }});

    // ---- Vector layers ----
    const pointLayer = L.geoJSON(geoData.points, {{
        pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
            radius: 5,
            fillColor: speciesColors[speciesOf(f)] || '#ffffff',
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
        if (f.geometry.type === 'Point') {{
            coordsList = [f.geometry.coordinates];
        }} else if (f.geometry.type === 'MultiPoint') {{
            coordsList = f.geometry.coordinates;
        }} else {{
            return;
        }}

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
        const baseColor = speciesColors[spName] || '#e74c3c';
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
                        color: speciesColors[spName] || '#f1c40f',
                        weight: 1,
                        fillColor: speciesColors[spName] || '#f1c40f',
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
    }}

    // ---- Legend / species filter (bottom-left) ----
    const legendControl = L.control({{ position: 'bottomleft' }});
    legendControl.onAdd = function () {{
        const div = L.DomUtil.create('div', 'leaflet-custom-control');
        let html = '<b>Species Filter</b><br/><select id="spSelect" onchange="updateMap()"><option value="ALL">-- All Species --</option>';

        Object.keys(speciesColors).sort().forEach(sp => {{
            html += `<option value="${{sp}}">${{sp}}</option>`;
        }});
        html += '</select><div class="legend-box"><b>200m Buffer Key</b><br/>';

        Object.keys(speciesColors).sort().forEach(sp => {{
            html += `<div class="legend-row"><span class="badge" style="background:${{speciesColors[sp]}}"></span>${{sp}}</div>`;
        }});
        html += '</div>';

        div.innerHTML = html;
        L.DomEvent.disableScrollPropagation(div);
        L.DomEvent.disableClickPropagation(div);
        return div;
    }};
    legendControl.addTo(map);

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
    html_str = build_leaflet_html(geo)
    upload_to_github(html_str)


if __name__ == "__main__":
    main()