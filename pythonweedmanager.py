import os
import json
import base64
import requests

# ==============================================================================
# CONFIGURATION
# Set your WeedManager credentials & GitHub details below.
# Alternatively, set them as environment variables.
# ==============================================================================

# 1. WeedManager Projects & API Keys
PROJECT_CONFIG = json.loads(os.getenv("WEEDMANAGER_PROJECT_MAPPING", json.dumps([
        {"api_key1": "0bfa6ced8b020a0da65bb907db6d2256", "445": "445"},
        {"api_key2": "a9d87d9b8c6b5efb70e2c9d6252c0c20", "314": "314"},
        {"api_key3": "a9d87d9b8c6b5efb70e2c9d6252c0c20", "310": "310"},
        {"api_key4": "5be907f4d201dfe4ed63666c1868922e", "441": "441"},
        {"api_key5": "5fe8ff9a58ac528a6a90450fc8713030", "455": "455"},
    ])))

# 2. GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "mafew-lgtm/weedmanager-map")  # e.g., "johndoe/weedmap"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
FILE_PATH_IN_REPO = "index.html"  # Path in the repository (e.g., index.html)

BASE_WFS_URL = "https://io.weedmanager.nz/geo/wm/wfs"

LAYER_TYPES = [
    "wm:default-project-weeds-points",
    "wm:default-project-weeds-polygons",
    "wm:default-project-weeds-linestrings",
    "wm:default-project-tracks",
    "wm:default-project-work-areas"
]

# ==============================================================================
# STEP 1: FETCH DATA FROM WEEDMANAGER
# ==============================================================================
def fetch_wfs_layer(api_key, project_id, layer_name):
    """Fetch GeoJSON features from WeedManager WFS endpoint."""
    url = f"{BASE_WFS_URL}/{api_key}/{project_id}"
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer_name,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326"
    }
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"  └─ [Error] Failed {layer_name} (Project: {project_id}): {e}")
        return {"type": "FeatureCollection", "features": []}

def aggregate_data():
    """Download and aggregate layer geometries from WeedManager."""
    combined = {
        "points": {"type": "FeatureCollection", "features": []},
        "lines": {"type": "FeatureCollection", "features": []},
        "polygons": {"type": "FeatureCollection", "features": []},
        "work_areas": {"type": "FeatureCollection", "features": []}
    }

    print("\n--- 1/3: Fetching Data from WeedManager API ---")
    for entry in PROJECT_CONFIG:
        key = entry.get("api_key")
        proj_id = entry.get("project_id")

        if not key or not proj_id or key.startswith("YOUR_"):
            print(f"[Warning] Skipping unconfigured project: {proj_id}")
            continue

        print(f"-> Processing Project ID: {proj_id}")
        for layer in LAYER_TYPES:
            data = fetch_wfs_layer(key, proj_id, layer)
            features = data.get("features", [])

            if "work-areas" in layer:
                combined["work_areas"]["features"].extend(features)
            elif "points" in layer:
                combined["points"]["features"].extend(features)
            elif "lines" in layer:
                combined["lines"]["features"].extend(features)
            elif "polygons" in layer:
                combined["polygons"]["features"].extend(features)

    print(f"\nSummary:")
    print(f"  • Points:     {len(combined['points']['features'])}")
    print(f"  • Lines:      {len(combined['lines']['features'])}")
    print(f"  • Polygons:   {len(combined['polygons']['features'])}")
    print(f"  • Work Areas: {len(combined['work_areas']['features'])}")

    return combined

# ==============================================================================
# STEP 2: BUILD LEAFLET HTML MAP
# ==============================================================================
def build_leaflet_html(geojson_data):
    """Generate interactive Leaflet HTML code."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WeedManager NZ Map Dashboard</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>

    <style>
        body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        #map {{ width: 100vw; height: 100vh; }}
        .control-panel {{
            position: absolute; top: 12px; right: 12px; z-index: 1000;
            background: rgba(255, 255, 255, 0.95); padding: 12px 16px; border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15); font-size: 14px;
        }}
        select {{ margin-top: 6px; width: 100%; padding: 6px; border-radius: 4px; border: 1px solid #ccc; }}
    </style>
</head>
<body>

<div class="control-panel">
    <label for="speciesFilter"><b>Heatmap Species Filter:</b></label><br/>
    <select id="speciesFilter" onchange="updateHeatmap()">
        <option value="ALL">-- All Species --</option>
    </select>
</div>

<div id="map"></div>

<script>
    const geoData = {json.dumps(geojson_data)};
    const map = L.map('map').setView([-41.2865, 174.7762], 6);

    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap contributors | WeedManager NZ'
    }}).addTo(map);

    const layerPoints = L.geoJSON(geoData.points, {{
        onEachFeature: (f, l) => l.bindPopup(
            `<b>Species:</b> ${{f.properties.species || f.properties.name || 'Unknown'}}<br/>` +
            `<b>Notes:</b> ${{f.properties.notes || 'N/A'}}`
        )
    }});

    const layerLines = L.geoJSON(geoData.lines, {{ style: {{ color: '#e67e22', weight: 3 }} }});
    const layerPolygons = L.geoJSON(geoData.polygons, {{ style: {{ color: '#e74c3c', fillColor: '#e74c3c', fillOpacity: 0.4 }} }});
    const layerWorkAreas = L.geoJSON(geoData.work_areas, {{ style: {{ color: '#3498db', weight: 2, dashArray: '4', fillOpacity: 0.1 }} }});

    // Populate Species Filter Dropdown
    const speciesSet = new Set();
    geoData.points.features.forEach(f => {{
        const sp = f.properties.species || f.properties.name;
        if (sp) speciesSet.add(sp);
    }});

    const select = document.getElementById('speciesFilter');
    Array.from(speciesSet).sort().forEach(sp => {{
        const opt = document.createElement('option');
        opt.value = sp;
        opt.textContent = sp;
        select.appendChild(opt);
    }});

    let heatLayer = L.heatLayer([], {{ radius: 25, blur: 15, maxZoom: 17 }}).addTo(map);

    function updateHeatmap() {{
        const selected = document.getElementById('speciesFilter').value;
        const heatPoints = [];

        geoData.points.features.forEach(f => {{
            if (f.geometry && f.geometry.type === "Point") {{
                const sp = f.properties.species || f.properties.name;
                if (selected === "ALL" || sp === selected) {{
                    const [lng, lat] = f.geometry.coordinates;
                    heatPoints.push([lat, lng, 1.0]);
                }}
            }}
        }});

        heatLayer.setLatLngs(heatPoints);
    }}

    updateHeatmap();

    const overlayMaps = {{
        "Species Heatmap": heatLayer,
        "Weed Points": layerPoints,
        "Weed LineStrings": layerLines,
        "Weed Polygons": layerPolygons,
        "Work Areas": layerWorkAreas
    }};

    L.control.layers(null, overlayMaps, {{ collapsed: false }}).addTo(map);

    if (geoData.points.features.length > 0) {{
        map.fitBounds(layerPoints.getBounds());
    }}
</script>
</body>
</html>
"""

# ==============================================================================
# STEP 3: DIRECT UPLOAD TO GITHUB VIA REST API
# ==============================================================================
def upload_to_github(html_content):
    """Uploads or updates the index.html file in your GitHub repository."""
    print("\n--- 3/3: Uploading to GitHub ---")

    if GITHUB_TOKEN.startswith("YOUR_") or GITHUB_REPO.startswith("YOUR_"):
        print("[Error] Please set your GITHUB_TOKEN and GITHUB_REPO credentials in the script.")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH_IN_REPO}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    # 1. Check if the file already exists (required to get its SHA blob for updating)
    sha = None
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        sha = response.json().get("sha")
        print(f"Existing file found in repository (SHA: {sha[:7]}). Updating...")
    elif response.status_code == 404:
        print("File does not exist yet. Creating a new file...")
    else:
        print(f"[Error] Failed to check GitHub file: {response.status_code} - {response.text}")
        return

    # 2. Convert HTML content to base64 encoding (required by GitHub API)
    encoded_content = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")

    # 3. Payload preparation
    payload = {
        "message": "Update WeedManager interactive webmap",
        "content": encoded_content,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    # 4. Commit and push file via API PUT request
    put_response = requests.put(url, headers=headers, json=payload)
    if put_response.status_code in [200, 201]:
        print("\n Success! Map successfully uploaded/updated on GitHub.")
        owner, repo = GITHUB_REPO.split('/')
        print(f" Live Map URL (GitHub Pages): https://{owner}.github.io/{repo}/")
    else:
        print(f"\n[Error] Failed to upload: {put_response.status_code} - {put_response.text}")

# ==============================================================================
# MAIN EXECUTION PIPELINE
# ==============================================================================
if __name__ == "__main__":
    # 1. Fetch & combine layers
    geojson_data = aggregate_data()

    # 2. Render HTML
    print("\n--- 2/3: Generating Webmap HTML ---")
    html_str = build_leaflet_html(geojson_data)

    # 3. Commit & upload to GitHub
    upload_to_github(html_str)