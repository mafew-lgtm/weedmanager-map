import base64
import http.server
import json
import os
import socket
import socketserver
import tempfile
import threading
import webbrowser
from functools import partial
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsJsonExporter,
    QgsProject,
    QgsVectorLayer,
)
from qgis.utils import iface
import requests

# --- CONFIGURATION ---
api_keys = [
    "0bfa6ced8b020a0da65bb907db6d2256",
    "a9d87d9b8c6b5efb70e2c9d6252c0c20",
    "a9d87d9b8c6b5efb70e2c9d6252c0c20",
]
project_ids = ["445", "314", "310"]

# --- GITHUB CONFIGURATION ---
GITHUB_TOKEN = ""
GITHUB_REPO = "mafew-lgtm/weedmanager-map"
BRANCH = "main"

# Cross-platform temp output folder
OUTPUT_FOLDER = os.path.join(tempfile.gettempdir(), "WeedManager_WebMap")
WEBMAP_FILE = os.path.join(OUTPUT_FOLDER, "index.html")
DASHBOARD_FILE = os.path.join(OUTPUT_FOLDER, "dashboard.html")

typenames = [
    "wm:default-project-weeds-points",
    "wm:default-project-weeds-polygons",
    "wm:default-project-weeds-linestrings",
    "wm:default-project-tracks",
    "wm:default-project-work-areas",
]

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
project = QgsProject.instance()

print("Starting batch download from Weedmanager for all projects...")

collected_layers = {
    "points": [],
    "polygons": [],
    "linestrings": [],
    "tracks": [],
    "work-areas": [],
}


def get_type_key(typename):
  if "points" in typename:
    return "points"
  if "polygons" in typename:
    return "polygons"
  if "linestrings" in typename:
    return "linestrings"
  if "tracks" in typename:
    return "tracks"
  if "work-areas" in typename:
    return "work-areas"
  return None


temp_files_to_clean = []

# ==========================================
# 1. FETCH WFS DATA FROM WEEDMANAGER
# ==========================================
for api_key, project_id in zip(api_keys, project_ids):
  base_url = f"https://io.weedmanager.nz/geo/wm/wfs/{api_key}/{project_id}"
  for typename in typenames:
    layer_title = (
        f"Weedmanager - Project {project_id} - {typename.replace('wm:', '')}"
    )
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": typename,
        "outputFormat": "application/json",
    }
    try:
      response = requests.get(base_url, params=params, timeout=20)
      if response.status_code == 200 and len(response.content) > 100:
        fd, temp_path = tempfile.mkstemp(suffix=".geojson")
        os.close(fd)
        temp_files_to_clean.append(temp_path)
        with open(temp_path, "wb") as f:
          f.write(response.content)
        vlayer = QgsVectorLayer(temp_path, layer_title, "ogr")
        if vlayer.isValid() and vlayer.featureCount() > 0:
          category = get_type_key(typename)
          if category:
            collected_layers[category].append(vlayer)
    except Exception as e:
      print(f"Error fetching {typename} for project {project_id}: {e}")

# ==========================================
# 2. MERGE LAYERS BY CATEGORY
# ==========================================
combined_layers_list = []
for category, layers in collected_layers.items():
  if not layers:
    continue
  master_layer_name = f"Weedmanager - Combined {category.capitalize()}"
  try:
    result = processing.run(
        "native:mergevectorlayers", {"LAYERS": layers, "OUTPUT": "TEMPORARY_OUTPUT"}
    )
    merged_layer = result["OUTPUT"]
    merged_layer.setName(master_layer_name)
    old_layers = [
        l.id()
        for l in project.mapLayers().values()
        if l.name() == master_layer_name
    ]
    if old_layers:
      project.removeMapLayers(old_layers)
    project.addMapLayer(merged_layer)
    combined_layers_list.append(merged_layer)
  except Exception as e:
    print(f"Error merging category {category}: {e}")

for t_file in temp_files_to_clean:
  if os.path.exists(t_file):
    try:
      os.remove(t_file)
    except OSError:
      pass

# ==========================================
# 3. GENERATE LEAFLET WEBMAP WITH HEATMAPS
# ==========================================
geojson_js_vars = []
leaflet_layers_js = []
layer_control_entries = []
COLOR_MAP = {
    "Points": "#e31a1c",
    "Polygons": "#33a02c",
    "Linestrings": "#ff7f00",
    "Tracks": "#1f78b4",
    "Work-areas": "#6a3d9a",
}

overall_extent = None
exporter = QgsJsonExporter()

# We will collect GeoJSON data objects to process species heatmaps in JS
combined_geojson_objects = []

for layer in combined_layers_list:
  cat_title = layer.name().replace("Weedmanager - Combined ", "")
  var_name = f"data_{cat_title.lower().replace('-', '_')}"
  color = COLOR_MAP.get(cat_title, "#3388ff")

  exporter.setSourceCrs(layer.crs())
  geojson_str = exporter.exportFeatures(layer.getFeatures())
  geojson_js_vars.append(f"var {var_name} = {geojson_str};")

  # Store for client-side heatmap processing (weed categories only)
  if cat_title.lower() in ["points", "polygons", "linestrings"]:
    combined_geojson_objects.append(var_name)

  js_layer = f"""
    var layer_{cat_title.lower().replace('-', '_')} = L.geoJson({var_name}, {{
        style: function(feature) {{
            return {{ color: "{color}", weight: 3, opacity: 0.8, fillColor: "{color}", fillOpacity: 0.35 }};
        }},
        pointToLayer: function(feature, latlng) {{
            return L.circleMarker(latlng, {{ radius: 6, fillColor: "{color}", color: "#000", weight: 1, opacity: 1, fillOpacity: 0.8 }});
        }},
        onEachFeature: function(feature, layer) {{
            var popupContent = '<div style="max-height: 200px; overflow-y: auto;"><b>{cat_title}</b><hr>';
            if (feature.properties) {{
                for (var key in feature.properties) {{
                    popupContent += '<b>' + key + ':</b> ' + feature.properties[key] + '<br>';
                }}
            }}
            popupContent += '</div>';
            layer.bindPopup(popupContent);
        }}
    }}).addTo(map);
    """
  leaflet_layers_js.append(js_layer)
  layer_control_entries.append(
      f'"{cat_title}": layer_{cat_title.lower().replace("-", "_")}'
  )

  if overall_extent is None:
    overall_extent = layer.extent()
  else:
    overall_extent.combineExtentWith(layer.extent())

if overall_extent and not overall_extent.isEmpty():
  source_crs = combined_layers_list[0].crs()
  target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
  transform = QgsCoordinateTransform(source_crs, target_crs, project)
  transformed_extent = transform.transformBoundingBox(overall_extent)
  center_lat = (
      transformed_extent.yMinimum() + transformed_extent.yMaximum()
  ) / 2
  center_lng = (
      transformed_extent.xMinimum() + transformed_extent.xMaximum()
  ) / 2
  zoom_level = 12
else:
  center_lat, center_lng, zoom_level = -41.2865, 174.7762, 6

html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>WeedManager Combined Web Map</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <!-- Leaflet.heat Plugin -->
    <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
        #map {{ width: 100vw; height: 100vh; }}
        .map-title {{
            position: absolute; top: 10px; left: 50px; z-index: 1000;
            background: rgba(255, 255, 255, 0.9); padding: 8px 15px;
            border-radius: 5px; box-shadow: 0 0 10px rgba(0,0,0,0.2);
            margin: 0; font-size: 16px; font-weight: bold;
        }}
    </style>
</head>
<body>
    <div class="map-title">WeedManager - Unified Projects Map</div>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([{center_lat}, {center_lng}], {zoom_level});
        var carto = L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png', {{ maxZoom: 19, attribution: '&copy; OpenStreetMap &copy; CARTO' }}).addTo(map);
        var esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{ attribution: 'Tiles &copy; Esri' }});
        
        {"".join(geojson_js_vars)}
        {"".join(leaflet_layers_js)}
        
        var baseMaps = {{ "Streets (CartoDB)": carto, "Satellite (Esri)": esriSat }};
        var overlayMaps = {{ {", ".join(layer_control_entries)} }};

        // --- HEATMAP GENERATION PER SPECIES ---
        var weedGeoJsonData = [{", ".join(combined_geojson_objects)}];
        var speciesPoints = {{}};

        // Extract coordinates and group by species name
        weedGeoJsonData.forEach(function(geoJson) {{
            if (!geoJson || !geoJson.features) return;
            geoJson.features.forEach(function(feature) {{
                if (!feature.properties) return;
                
                // Inspect common species field names used in WeedManager
                var species = feature.properties.species || 
                              feature.properties.species_name || 
                              feature.properties.weed_type || 
                              feature.properties.name || 
                              "Unspecified Species";
                
                if (!speciesPoints[species]) {{
                    speciesPoints[species] = [];
                }}

                // Helper to extract Lat/Lng coordinates from Point, Polygon, or LineString
                function addCoords(coords) {{
                    if (typeof coords[0] === 'number') {{
                        // Format is [lng, lat]
                        speciesPoints[species].push([coords[1], coords[0]]);
                    }} else if (Array.isArray(coords)) {{
                        coords.forEach(addCoords);
                    }}
                }}

                if (feature.geometry && feature.geometry.coordinates) {{
                    addCoords(feature.geometry.coordinates);
                }}
            }});
        }});

        // Generate Leaflet Heatmap layer for each species
        Object.keys(speciesPoints).forEach(function(species) {{
            var pts = speciesPoints[species];
            if (pts.length > 0) {{
                var heatLayer = L.heatLayer(pts, {{
                    radius: 20,
                    blur: 15,
                    maxZoom: 17
                }});
                overlayMaps["Heatmap: " + species] = heatLayer;
            }}
        }});

        L.control.layers(baseMaps, overlayMaps, {{ collapsed: false }}).addTo(map);
    </script>
</body>
</html>
"""

with open(WEBMAP_FILE, "w", encoding="utf-8") as f:
  f.write(html_content)

# Dashboard template
dashboard_content = """<!DOCTYPE html>
<html>
<head>
    <title>WeedManager Dashboard</title>
    <meta charset="utf-8" />
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f4f4f9; }
        h1 { color: #333; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
    </style>
</head>
<body>
    <h1>WeedManager Operations Dashboard</h1>
    <div class="card">
        <h3>Overview</h3>
        <p>Welcome to the WeedManager multi-project dashboard. <a href="index.html">View Interactive Map</a></p>
    </div>
</body>
</html>
"""
with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
  f.write(dashboard_content)


# ==========================================
# 4. GITHUB UPLOAD FUNCTION
# ==========================================
def upload_to_github(local_file_path, repo_file_path, commit_message):
  url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_file_path}"
  headers = {
      "Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
  }

  with open(local_file_path, "rb") as f:
    content_bytes = f.read()
  content_encoded = base64.b64encode(content_bytes).decode("utf-8")

  response = requests.get(url, headers=headers, params={"ref": BRANCH})
  sha = response.json().get("sha") if response.status_code == 200 else None

  payload = {
      "message": commit_message,
      "content": content_encoded,
      "branch": BRANCH,
  }
  if sha:
    payload["sha"] = sha

  put_response = requests.put(url, json=payload, headers=headers)
  if put_response.status_code in [200, 201]:
    print(f" -> Successfully uploaded {repo_file_path} to GitHub!")
  else:
    print(
        f" -> Failed to upload {repo_file_path}:"
        f" {put_response.status_code} - {put_response.text}"
    )


print("\n----------------------------------------")
print("Uploading files to GitHub repository...")
print("----------------------------------------")
upload_to_github(
    WEBMAP_FILE, "index.html", "Update map with species heatmap layers"
)
upload_to_github(DASHBOARD_FILE, "dashboard.html", "Upload Dashboard")