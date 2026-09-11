import os
import json
import base64
import requests

# ==============================================================================
# CONFIGURATION
# ==============================================================================

PROJECT_CONFIG = [
        {"api_key": "0bfa6ced8b020a0da65bb907db6d2256", "project_id": "445"},
        {"api_key": "a9d87d9b8c6b5efb70e2c9d6252c0c20", "project_id": "314"},
        {"api_key": "a9d87d9b8c6b5efb70e2c9d6252c0c20", "project_id": "310"},
        {"api_key": "5be907f4d201dfe4ed63666c1868922e", "project_id": "441"},
        {"api_key": "5fe8ff9a58ac528a6a90450fc8713030", "project_id": "455"},
    ]

# 2. GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "mafew-lgtm/weedmanager-map")  # e.g., "johndoe/weedmap"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
FILE_PATH_IN_REPO = "index.html"

BASE_WFS_URL = "https://io.weedmanager.nz/geo/wm/wfs"

LAYER_TYPES = [
    "wm:default-project-weeds-points",
    "wm:default-project-weeds-polygons",
    "wm:default-project-weeds-linestrings",
    "wm:default-project-tracks",
    "wm:default-project-work-areas"
]

# ==============================================================================
# STEP 1: FETCH DATA WITH STRICT JSON SANITIZATION
# ==============================================================================
def fetch_wfs_layer(api_key, project_id, layer_name):
    """Fetch GeoJSON features safely from WeedManager WFS endpoint."""
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
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        
        # Verify response is valid GeoJSON content rather than XML error text
        if "json" in response.headers.get("Content-Type", "").lower():
            return response.json()
        else:
            print(f"  └─ [WFS Warning] Non-JSON payload received from {layer_name}")
            return {"type": "FeatureCollection", "features": []}
            
    except Exception as e:
        print(f"  └─ [Error] Fetch failed for {layer_name} (Proj: {project_id}): {e}")
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
        key = str(entry.get("api_key", "")).strip()
        proj_id = str(entry.get("project_id", "")).strip()

        # Debug print to verify what python is actually reading
        print(f"-> Attempting Project ID: '{proj_id}' with Key: '{key[:4]}...'")

        if not key or not proj_id:
            print(f"  └─ [Warning] Skipping empty configuration entry.")
            continue

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
# STEP 2: BUILD LEAFLET HTML (BOTTOM-LEFT LEGEND & UNSTUCK UI)
# ==============================================================================
def build_leaflet_html(geojson_data):
    """Generate HTML webmap with anchored custom controls."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WeedManager NZ Dashboard</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>

    <style>
        html, body, #map {{ width: 100%; height: 100%; margin: 0; padding: 0; }}
        
        /* Custom UI Legend Container docked inside Leaflet Control Framework */
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
    </style>
</head>
<body>

<div id="map"></div>

<script>
    const geoData = {json.dumps(geojson_data)};

    // Map Initialization
    const map = L.map('map', {{ center: [-41.2865, 174.7762], zoom: 6 }});

    // Tile Layers
    const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        attribution: 'Tiles &copy; Esri &mdash; WeedManager NZ'
    }}).addTo(map);

    const osmBase = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap'
    }});

    // Color assignment for Species
    const PALETTE = ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#1abc9c', '#3498db', '#9b59b6'];
    const speciesColors = {{}};
    let idx = 0;

    if (geoData.points && geoData.points.features) {{
        geoData.points.features.forEach(f => {{
            const name = f.properties.species || f.properties.name || 'Unspecified';
            if (!speciesColors[name]) {{
                speciesColors[name] = PALETTE[idx % PALETTE.length];
                idx++;
            }}
        }});
    }}

    // Vector Layers
    const pointLayer = L.geoJSON(geoData.points, {{
        pointToLayer: (f, latlng) => {{
            const name = f.properties.species || f.properties.name || 'Unspecified';
            return L.circleMarker(latlng, {{
                radius: 6,
                fillColor: speciesColors[name] || '#ffffff',
                color: '#ffffff',
                weight: 1,
                fillOpacity: 0.9
            }});
        }},
        onEachFeature: (f, l) => l.bindPopup(`<b>Species:</b> ${{f.properties.species || f.properties.name || 'N/A'}}`)
    }}).addTo(map);

    const lineLayer = L.geoJSON(geoData.lines, {{ style: {{ color: '#f39c12', weight: 3 }} }}).addTo(map);
    const polyLayer = L.geoJSON(geoData.polygons, {{ style: {{ color: '#e74c3c', fillOpacity: 0.3 }} }}).addTo(map);
    const workLayer = L.geoJSON(geoData.work_areas, {{ style: {{ color: '#3498db', dashArray: '4', fillOpacity: 0.1 }} }}).addTo(map);

    const bufferGroup = L.layerGroup().addTo(map);

    function updateBuffers() {{
        bufferGroup.clearLayers();
        const sel = document.getElementById('spSelect') ? document.getElementById('spSelect').value : 'ALL';

        if (!geoData.points || !geoData.points.features.length) return;

        geoData.points.features.forEach(f => {{
            if (!f.geometry || f.geometry.type !== 'Point') return;
            const spName = f.properties.species || f.properties.name || 'Unspecified';
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
            }} catch(e) {{
                console.warn('Buffer calculation failed for feature', e);
            }}
        }});
    }}

    // DOCK LEGEND PANEL TO BOTTOM-LEFT
    const legendControl = L.control({{ position: 'bottomleft' }});
    legendControl.onAdd = function() {{
        const div = L.DomUtil.create('div', 'leaflet-custom-control');
        let html = '<b>Species Filter</b><br/><select id="spSelect" onchange="updateBuffers()"><option value="ALL">-- All Species --</option>';
        
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

    // DOCK LAYER TOGGLE CONTROL TO TOP-RIGHT
    L.control.layers(
        {{ "Satellite Imagery": esriSat, "OpenStreetMap": osmBase }},
        {{ "200m Buffers": bufferGroup, "Points": pointLayer, "Lines": lineLayer, "Polygons": polyLayer, "Work Areas": workLayer }},
        {{ collapsed: true, position: 'topright' }}
    ).addTo(map);

    // Initial buffer draw
    updateBuffers();

    // Auto-fit geometry bounds or default to New Zealand region
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
def upload_to_github(html_content):
    """Upload or update index.html in GitHub repo."""
    print("\n--- 3/3: Pushing Update to GitHub ---")
    if GITHUB_TOKEN.startswith("YOUR_") or GITHUB_REPO.startswith("YOUR_"):
        print("[Error] Set GITHUB_TOKEN and GITHUB_REPO prior to running.")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH_IN_REPO}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    sha = None
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        sha = res.json().get("sha")

    encoded_content = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": "Fix Leaflet control collision and add safe WFS fallback handling",
        "content": encoded_content,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, headers=headers, json=payload)
    if put_res.status_code in [200, 201]:
        owner, repo = GITHUB_REPO.split('/')
        print(f" Success! Map Updated: https://{owner}.github.io/{repo}/")
    else:
        print(f"[Error] Upload failed: {put_res.status_code} - {put_res.text}")

if __name__ == "__main__":
    geojson_data = aggregate_data()
    html_str = build_leaflet_html(geojson_data)
    upload_to_github(html_str)