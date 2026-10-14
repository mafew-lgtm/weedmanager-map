"""
WeedManager NZ -> Leaflet Dashboards (public + password-gated power-user)
==========================================================================
1. Downloads weed/track/work-area layers for each configured project from the
   WeedManager WFS API.
2. Combines everything by layer type (points / lines / polygons / work areas).
3. Builds TWO single-file Leaflet dashboards from the same data:

   - PUBLIC (index.html) — simplified, light-theme, and the single entry
     point people actually visit. Shows a combined species heatmap and
     weed polygons only — exact point locations, tracks, work areas,
     hotspot coordinates, and the per-project breakdown are stripped
     out of the data entirely before it's embedded, not just hidden from
     view. It also has a small lock button: enter the power-user password
     and it navigates to the power-user page.

   - POWER USER (unlisted filename, see POWER_FILE_PATH_IN_REPO) — the
     full dashboard: every layer, exact points, per-species heatmaps,
     hotspot ranking, and the full ArcGIS-style insights dashboard
     including the per-project breakdown and embedded map widget.

4. Pushes both generated HTML files to a GitHub repo (served via GitHub
   Pages).

IMPORTANT — what the password button actually protects
--------------------------------------------------------
There is deliberately NOT one merged file with everything embedded behind a
password check. That would defeat the purpose: with no server involved,
"hidden until password entered" only means "hidden by JavaScript" — the full
data would still be sitting in index.html's source the moment anyone loads
the public URL, readable via dev tools regardless of whether the prompt was
ever answered.

Instead: index.html only ever contains the sanitized public data. The lock
button hashes (SHA-256) whatever the visitor types and compares it to a
hash baked in at build time; the plaintext password itself never appears in
the page. On a match, it navigates to the power-user file — which is the
first and only point at which the full data gets fetched at all.

That said, be clear-eyed about what this still doesn't do:

  - The SHA-256 hash IS in index.html's source. A weak/short/guessable
    password can be brute-forced offline against that hash with no rate
    limiting to stop it — this is a speed bump, not real access control.
  - Once past the prompt, the power-user page is a plain static file like
    before: no session, no expiry, and if GITHUB_REPO is a public repo,
    still visible in the repo's file browser to anyone regardless of the
    password.

If you need real access control, that means either making the repo private
on a GitHub plan that supports private Pages, or hosting the power-user
page separately behind something like Cloudflare Access / Netlify password
protection instead of GitHub Pages.

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
Set POWER_PAGE_PATH to override the unlisted page's filename/path
(defaults to a randomish name so it isn't easy to guess).

POWER_PAGE_PASSWORD (required) is the plaintext password visitors type into
the public page's lock button. Set it as an environment variable — never
commit it to source control:
    export POWER_PAGE_PASSWORD='something-only-your-team-knows'

Optional: priority segments layer (power-user page only)
--------------------------------------------------------
Put a WGS84 GeoJSON called priority_segments.geojson next to this script (make
it from an ArcGIS Pro .lpkx with lpkx_to_geojson.py, which also reprojects from
NZTM). The power page then gets a bottom-right "Priority Segments" panel that
ranks segments highest-priority first, zooms to a segment on "View", and moves
a segment to the bottom of the list when its x is pressed (↺ puts it back).
Ticks are stored in each visitor's own browser (localStorage), so they are NOT
shared between people. Environment overrides: PRIORITY_GEOJSON (file path),
PRIORITY_FIELD (priority attribute; auto-detected if blank),
PRIORITY_LABEL_FIELD (list label attribute; auto-detected if blank).
If the file is absent the panel is simply omitted.

Optional: value and vulnerability layers (power-user page only)
---------------------------------------------------------------
Two more WGS 1984 GeoJSON files next to this script:
    valuelayer.geojson          score field "value"
    vulnerability_layer.geojson score field "gridcode"
Each is drawn as a shaded layer (darker = higher score) underneath the weed data,
and is OFF until its preset button is pressed. The top-left "Layers" buttons toggle:
Buffers & Hotspots (together), Priority, Value and Vulnerability. A missing file just
greys out its button. Overrides: VALUE_GEOJSON / VALUE_FIELD and
VULNERABILITY_GEOJSON / VULNERABILITY_FIELD. Only the score (and a name, if the data
has an obvious name field) is kept from each feature. Big layers make the page slow, so
the script warns above SCORE_LAYER_WARN_MB; dissolve or simplify them in ArcGIS Pro first.
"""

import os
import json
import base64
import hashlib
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

PUBLIC_FILE_PATH_IN_REPO = "index.html"
# Not a security boundary — see the module docstring. Override with an env
# var so the real filename doesn't need to live in source control either.
POWER_FILE_PATH_IN_REPO = os.getenv("POWER_PAGE_PATH", "view-7f2ac91d.html")

# Gates the "power user" link on the public page. Only the SHA-256 hash of
# this ends up in index.html's source, never the plaintext — but see the
# module docstring for exactly what that does and doesn't protect against.
POWER_PAGE_PASSWORD = os.getenv("POWER_PAGE_PASSWORD", "weedwatch2026")

# Optional ArcGIS Pro layer: priority-ranked segments (power-user page only).
# Produce the GeoJSON with lpkx_to_geojson.py (reprojects NZTM -> WGS84).
# If the file doesn't exist the script just skips the priority panel.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRIORITY_GEOJSON_PATH = os.getenv("PRIORITY_GEOJSON", os.path.join(_SCRIPT_DIR, "priority_segments.geojson"))
# Leave blank to auto-detect the numeric priority field; set to force one.
PRIORITY_FIELD = os.getenv("PRIORITY_FIELD", "sum_gridcode").strip()
# Leave blank to auto-detect a name/ID field for the list labels.
PRIORITY_LABEL_FIELD = os.getenv("PRIORITY_LABEL_FIELD", "").strip()

# Two more optional ArcGIS Pro layers (power-user page only), both WGS 1984 GeoJSON:
#   value layer          - score field "value" (cultural + ecological significance etc.)
#   vulnerability layer  - score field "gridcode" (vulnerability of each segment)
# A missing file is skipped (its preset button is greyed out on the page).
VALUE_GEOJSON_PATH = os.getenv("VALUE_GEOJSON", os.path.join(_SCRIPT_DIR, "valuelayer.geojson"))
VALUE_FIELD = os.getenv("VALUE_FIELD", "gridcode").strip()
VULNERABILITY_GEOJSON_PATH = os.getenv("VULNERABILITY_GEOJSON", os.path.join(_SCRIPT_DIR, "vulnerability_layer.geojson"))
VULNERABILITY_FIELD = os.getenv("VULNERABILITY_FIELD", "gridcode").strip()
# Warn when a layer's embedded GeoJSON gets this big (MB): the page gets slow to load.
SCORE_LAYER_WARN_MB = 8

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
            # Tag each feature with its source project so the dashboard can
            # break totals down by project — the WFS data itself has no idea
            # which of your projects it came from once everything is merged.
            for feat in features:
                if feat.get("properties") is None:
                    feat["properties"] = {}
                feat["properties"]["_wm_project_id"] = cfg.project_id
            combined[combined_key]["features"].extend(features)

    log.info("Summary:")
    for key, fc in combined.items():
        log.info("  %-11s %d", key + ":", len(fc["features"]))

    return combined


# ==============================================================================
# OPTIONAL LAYER: PRIORITY SEGMENTS (exported from ArcGIS Pro)
# ==============================================================================
_PRIORITY_SUPPORTED_GEOMS = {
    "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon",
}
_PRIORITY_NAME_HINTS = [
    "name", "segment", "segment_name", "segmentname", "seg_name", "segid", "seg_id",
    "label", "id", "objectid", "fid",
]
_PRIORITY_VALUE_HINTS = ["rank", "ranking", "score", "gridcode", "grid_code", "pri", "value"]


def _to_float(value):
    """Return value as a finite float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _round_coords(coords, ndigits: int = 6):
    """Round nested GeoJSON coordinates (~0.1 m) and drop any Z values."""
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return [round(c, ndigits) for c in coords[:2]]
        return [_round_coords(c, ndigits) for c in coords]
    return coords


def _first_xy(coords):
    """Dig out the first [x, y] pair from nested GeoJSON coordinates."""
    while isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return coords
    return None


def _guess_priority_field(numeric_fields: list) -> str:
    """Pick the most likely priority field from the numeric property names."""
    lowered = {name.lower(): name for name in numeric_fields}
    if "priority" in lowered:
        return lowered["priority"]
    containing = sorted((n for n in numeric_fields if "priority" in n.lower()), key=len)
    if containing:
        return containing[0]
    for hint in _PRIORITY_VALUE_HINTS:
        if hint in lowered:
            return lowered[hint]
    return ""


def load_priority_segments() -> dict:
    """Load the priority-segments GeoJSON for the power-user page.

    Returns a FeatureCollection whose features carry only three properties:
      _seg_id   stable ID (hash of the geometry) so "done" ticks survive rebuilds
      _priority numeric priority (higher = more important), or null
      _label    display name for the list
    Everything else in the source attributes is dropped on purpose.
    Returns an empty collection if the file doesn't exist.
    """
    import hashlib as _hashlib

    empty = {"type": "FeatureCollection", "features": []}
    path = PRIORITY_GEOJSON_PATH

    if not os.path.exists(path):
        log.info("No priority layer at %s — skipping (power page gets no priority panel).", path)
        return empty

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid GeoJSON: {e}") from e

    raw_features = data.get("features") if isinstance(data, dict) else None
    if not raw_features:
        log.warning("Priority file %s contains no features — skipping.", path)
        return empty

    features = [
        f for f in raw_features
        if isinstance(f, dict)
        and isinstance(f.get("geometry"), dict)
        and f["geometry"].get("type") in _PRIORITY_SUPPORTED_GEOMS
        and f["geometry"].get("coordinates")
    ]
    skipped = len(raw_features) - len(features)
    if skipped:
        log.warning("Priority layer: skipped %d feature(s) with missing/unsupported geometry.", skipped)
    if not features:
        return empty

    for feat in features:
        xy = _first_xy(feat["geometry"]["coordinates"])
        if xy is None or not (-180 <= xy[0] <= 180 and -90 <= xy[1] <= 90):
            raise RuntimeError(
                f"{path} doesn't look like WGS84 lat/lng (found coordinate {xy}). "
                "It's probably still in NZTM (EPSG:2193). Re-export it with "
                "lpkx_to_geojson.py, which reprojects to WGS84."
            )

    # Which properties exist, and which are numeric?
    all_fields, numeric_fields = [], []
    for feat in features:
        for key, value in (feat.get("properties") or {}).items():
            if key not in all_fields:
                all_fields.append(key)
            if key not in numeric_fields and _to_float(value) is not None:
                numeric_fields.append(key)

    if PRIORITY_FIELD:
        if PRIORITY_FIELD not in numeric_fields:
            raise RuntimeError(
                f"PRIORITY_FIELD={PRIORITY_FIELD!r} isn't a numeric field in {path}. "
                f"Numeric fields found: {numeric_fields or 'none'}"
            )
        priority_field = PRIORITY_FIELD
    else:
        priority_field = _guess_priority_field(numeric_fields)
        if not priority_field:
            raise RuntimeError(
                f"Couldn't tell which field in {path} holds the priority value. "
                f"Numeric fields found: {numeric_fields or 'none'}. "
                "Set the PRIORITY_FIELD environment variable to the right one."
            )

    label_field = ""
    if PRIORITY_LABEL_FIELD and PRIORITY_LABEL_FIELD in all_fields:
        label_field = PRIORITY_LABEL_FIELD
    else:
        lowered = {name.lower(): name for name in all_fields}
        for hint in _PRIORITY_NAME_HINTS:
            if hint in lowered and lowered[hint] != priority_field:
                label_field = lowered[hint]
                break

    out, seen_ids, missing_priority = [], set(), 0
    for index, feat in enumerate(features, start=1):
        props = feat.get("properties") or {}
        geometry = {
            "type": feat["geometry"]["type"],
            "coordinates": _round_coords(feat["geometry"]["coordinates"]),
        }

        geom_key = json.dumps(geometry, separators=(",", ":"), sort_keys=True)
        seg_id = _hashlib.md5(geom_key.encode("utf-8")).hexdigest()[:10]
        while seg_id in seen_ids:           # identical geometries -> disambiguate
            seg_id += "x"
        seen_ids.add(seg_id)

        priority = _to_float(props.get(priority_field))
        if priority is None:
            missing_priority += 1

        label = ""
        if label_field and props.get(label_field) not in (None, ""):
            label = str(props[label_field])
        if not label:
            label = f"Segment {index}"

        out.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {"_seg_id": seg_id, "_priority": priority, "_label": label},
        })

    values = [f["properties"]["_priority"] for f in out if f["properties"]["_priority"] is not None]
    log.info(
        "Priority layer: %d segment(s), priority field %r (range %s–%s), label field %s",
        len(out), priority_field,
        min(values) if values else "n/a", max(values) if values else "n/a",
        repr(label_field) if label_field else "none (using 'Segment N')",
    )
    if missing_priority:
        log.warning("Priority layer: %d segment(s) have no numeric priority and sort last.", missing_priority)

    return {"type": "FeatureCollection", "features": out}


_SCORE_LABEL_HINTS = ["name", "segment_name", "segmentname", "seg_name", "segment", "site", "label", "title"]


def load_score_layer(path: str, layer_name: str, field: str) -> dict:
    """Load a scored WGS84 GeoJSON layer (value / vulnerability) for the power page.

    Returns a FeatureCollection whose features carry only:
      _score  numeric score from `field` (matched case-insensitively), or null
      _label  a name, only if the data has an obvious name-like field
    Everything else in the source attributes is dropped to keep the page small.
    Coordinates are rounded to 5 decimals (~1 m). Returns an empty collection if
    the file doesn't exist, so the layer is simply left out.
    """
    empty = {"type": "FeatureCollection", "features": []}

    if not os.path.exists(path):
        log.info("No %s layer at %s — skipping (its button will be greyed out).", layer_name, path)
        return empty

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid GeoJSON: {e}") from e

    raw_features = data.get("features") if isinstance(data, dict) else None
    if not raw_features:
        log.warning("%s file %s contains no features — skipping.", layer_name.capitalize(), path)
        return empty

    features = [
        f for f in raw_features
        if isinstance(f, dict)
        and isinstance(f.get("geometry"), dict)
        and f["geometry"].get("type") in _PRIORITY_SUPPORTED_GEOMS
        and f["geometry"].get("coordinates")
    ]
    if len(features) < len(raw_features):
        log.warning("%s layer: skipped %d feature(s) with missing/unsupported geometry.",
                    layer_name.capitalize(), len(raw_features) - len(features))
    if not features:
        return empty

    # Only the first coordinate is checked: a layer is in one coordinate system.
    xy = _first_xy(features[0]["geometry"]["coordinates"])
    if xy is None or not (-180 <= xy[0] <= 180 and -90 <= xy[1] <= 90):
        raise RuntimeError(
            f"{path} doesn't look like WGS84 lat/lng (found coordinate {xy}). It's probably "
            "still in NZTM (EPSG:2193). Re-export it in WGS 1984 (EPSG:4326)."
        )

    all_fields, numeric_fields = [], []
    for feat in features:
        for key, value in (feat.get("properties") or {}).items():
            if key not in all_fields:
                all_fields.append(key)
            if key not in numeric_fields and _to_float(value) is not None:
                numeric_fields.append(key)

    by_lower = {name.lower(): name for name in numeric_fields}
    score_field = by_lower.get(field.lower())
    if not score_field:
        raise RuntimeError(
            f"No numeric field called {field!r} in {path}. Numeric fields found: {numeric_fields or 'none'}. "
            f"Set the {layer_name.upper()}_FIELD environment variable to the right one."
        )

    all_lower = {name.lower(): name for name in all_fields}
    label_field = next((all_lower[h] for h in _SCORE_LABEL_HINTS
                        if h in all_lower and all_lower[h] != score_field), "")

    out, unscored = [], 0
    for feat in features:
        props = feat.get("properties") or {}
        score = _to_float(props.get(score_field))
        if score is None:
            unscored += 1
        new_props = {"_score": score}
        if label_field and props.get(label_field) not in (None, ""):
            new_props["_label"] = str(props[label_field])
        out.append({
            "type": "Feature",
            "geometry": {"type": feat["geometry"]["type"],
                         "coordinates": _round_coords(feat["geometry"]["coordinates"], 5)},
            "properties": new_props,
        })

    scores = [f["properties"]["_score"] for f in out if f["properties"]["_score"] is not None]
    size_mb = len(json.dumps(out, separators=(",", ":"))) / 1_000_000
    log.info("%s layer: %d feature(s), field %r (range %s–%s), ~%.1f MB embedded",
             layer_name.capitalize(), len(out), score_field,
             min(scores) if scores else "n/a", max(scores) if scores else "n/a", size_mb)
    if unscored:
        log.warning("%s layer: %d feature(s) have no numeric %r and are drawn grey.",
                    layer_name.capitalize(), unscored, score_field)
    if size_mb > SCORE_LAYER_WARN_MB:
        log.warning(
            "%s layer is large (~%.0f MB), so the power page will be slow to load. In ArcGIS Pro, "
            "dissolve by the score field or simplify the geometry, then re-export.",
            layer_name.capitalize(), size_mb)

    return {"type": "FeatureCollection", "features": out}


# Styles and behaviour for the priority panel. Kept as plain (non-f) strings and
# injected into the power page below, so the CSS/JS braces need no escaping.
POWER_PRIORITY_CSS = r"""
        /* ---- Priority segments panel (bottom-right) ---- */
        .prio-panel {
            background: rgba(255,255,255,0.97);
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 13px;
            width: 300px;
            max-width: 88vw;
            overflow: hidden;
        }
        .prio-header {
            display: flex; align-items: center; gap: 8px;
            padding: 10px 12px; background: #2c3e50; color: #fff; font-weight: 600; cursor: pointer;
        }
        .prio-header > span:first-child { flex: 1; }
        .prio-summary { font-size: 11px; font-weight: 400; opacity: 0.85; }
        .prio-toggle { background: none; border: none; color: #fff; font-size: 16px; cursor: pointer; line-height: 1; padding: 0 4px; }
        .prio-panel.collapsed .prio-body { display: none; }
        .prio-panel.collapsed { width: auto; }
        .prio-scale { display: flex; align-items: center; gap: 6px; padding: 8px 12px 4px 12px; font-size: 10.5px; color: #777; }
        .prio-scale-bar { flex: 1; height: 6px; border-radius: 3px; background: linear-gradient(to right, hsl(55,90%,50%), hsl(0,90%,50%)); }
        .prio-list { max-height: 300px; overflow-y: auto; padding: 4px 12px 6px 12px; }
        .prio-row { display: flex; align-items: center; gap: 6px; padding: 6px 0; border-bottom: 1px solid #eee; }
        .prio-row.done { opacity: 0.55; }
        .prio-row.flash { background: #e8f8ff; }
        .prio-rank {
            width: 20px; height: 20px; border-radius: 50%; background: #eee; color: #333;
            font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        }
        .prio-swatch { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
        .prio-info { flex: 1; min-width: 0; }
        .prio-name { font-weight: 600; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .prio-meta { color: #888; font-size: 10.5px; }
        .prio-view {
            padding: 3px 8px; border: 1px solid #3498db; background: #fff; color: #3498db;
            border-radius: 4px; cursor: pointer; font-size: 11px; white-space: nowrap;
        }
        .prio-view:hover { background: #3498db; color: #fff; }
        .prio-x {
            width: 24px; height: 24px; border: 1px solid #ddd; background: #fff; color: #888;
            border-radius: 4px; cursor: pointer; font-size: 15px; line-height: 1; flex-shrink: 0; padding: 0;
        }
        .prio-x:hover { background: #e74c3c; border-color: #e74c3c; color: #fff; }
        .prio-row.done .prio-x:hover { background: #2ecc71; border-color: #2ecc71; }
        .prio-divider { margin-top: 8px; padding: 4px 0; font-size: 10.5px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; color: #999; border-bottom: 1px solid #ddd; }
        .prio-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 12px 8px 12px; border-top: 1px solid #eee; font-size: 10px; color: #aaa; }
        .prio-reset { border: 1px solid #ddd; background: #fff; color: #666; border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer; }
        .prio-reset:hover { background: #f5f5f5; }
        @media (max-width: 600px) {
            .prio-panel { width: 230px; font-size: 12px; }
            .prio-list { max-height: 220px; }
            .prio-panel.collapsed .prio-summary { display: none; }
        }
"""

POWER_PRIORITY_JS = r"""
    // ---- Priority segments: map layer + ranked list (bottom-right) ----
    const PRIO_STORAGE_KEY = 'wm_priority_done_v1';
    const prioData = geoData.priority || {type: 'FeatureCollection', features: []};
    const prioFeatures = (prioData.features || []).filter(f => f && f.geometry && f.properties);
    const prioHasData = prioFeatures.length > 0;
    const prioLayerById = {};

    function prioEsc(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    function prioNum(v) {
        if (v === null || v === undefined || v === '') return -Infinity;   // Number(null) would be 0
        v = Number(v);
        return isFinite(v) ? v : -Infinity;
    }
    function prioFmt(v) {
        return isFinite(v) ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : 'n/a';
    }

    // "Done" ticks live in this browser's localStorage only (not shared between people).
    let prioDone = new Set();
    try {
        const saved = JSON.parse(localStorage.getItem(PRIO_STORAGE_KEY) || '[]');
        if (Array.isArray(saved)) prioDone = new Set(saved);
    } catch (e) { /* storage unavailable: ticks last for this visit only */ }
    function prioSave() {
        try { localStorage.setItem(PRIO_STORAGE_KEY, JSON.stringify(Array.from(prioDone))); } catch (e) {}
    }

    let prioMin = Infinity, prioMax = -Infinity;
    prioFeatures.forEach(f => {
        const v = prioNum(f.properties._priority);
        if (isFinite(v)) { prioMin = Math.min(prioMin, v); prioMax = Math.max(prioMax, v); }
    });
    function prioColor(v) {
        v = prioNum(v);
        if (!isFinite(v)) return '#95a5a6';
        if (prioMax === prioMin) return 'hsl(0,90%,50%)';
        const t = (v - prioMin) / (prioMax - prioMin);   // 0 = lowest, 1 = highest
        return 'hsl(' + (55 - 55 * t).toFixed(0) + ',90%,50%)';   // yellow -> red
    }
    function prioStyle(f) {
        const done = prioDone.has(f.properties._seg_id);
        const isLine = /Line/.test(f.geometry.type);
        return {
            color: done ? '#7f8c8d' : prioColor(f.properties._priority),
            weight: isLine ? 5 : 2,
            opacity: done ? 0.5 : 0.95,
            fillOpacity: done ? 0.08 : 0.35,
            dashArray: done ? '6 6' : null
        };
    }

    const prioLayer = L.geoJSON(prioFeatures, {
        style: prioStyle,
        pointToLayer: (f, latlng) => L.circleMarker(latlng, {radius: 7}),
        onEachFeature: (f, layer) => {
            const id = f.properties._seg_id;
            prioLayerById[id] = layer;
            layer.bindPopup(() => '<b>' + prioEsc(f.properties._label) + '</b><br>Priority: ' +
                prioFmt(prioNum(f.properties._priority)) + (prioDone.has(id) ? '<br><i>Marked done</i>' : ''));
            layer.on('click', () => prioHighlightRow(id));
        }
    });
    if (prioHasData) {
        prioLayer.addTo(map);
        overlayLayers['Priority Segments'] = prioLayer;
    }

    function prioItems() {
        return prioFeatures.map(f => ({
            id: f.properties._seg_id,
            label: f.properties._label || 'Segment',
            v: prioNum(f.properties._priority),
            done: prioDone.has(f.properties._seg_id)
        })).sort((a, b) => {
            if (a.done !== b.done) return a.done ? 1 : -1;            // ticked ones sink to the bottom
            if (a.v !== b.v) return a.v > b.v ? -1 : 1;               // higher priority first
            return String(a.label).localeCompare(String(b.label), undefined, {numeric: true});
        });
    }

    function renderPrioList() {
        const items = prioItems();
        const doneCount = items.filter(i => i.done).length;
        let html = '', rank = 0, dividerShown = false;

        items.forEach(it => {
            if (it.done && !dividerShown) {
                html += '<div class="prio-divider">Done (' + doneCount + ')</div>';
                dividerShown = true;
            }
            const id = prioEsc(it.id);
            if (!it.done) rank++;
            html += '<div class="prio-row' + (it.done ? ' done' : '') + '" id="prio-row-' + id + '">' +
                '<span class="prio-rank">' + (it.done ? '&#10003;' : rank) + '</span>' +
                '<span class="prio-swatch" style="background:' + (it.done ? '#95a5a6' : prioColor(it.v)) + '"></span>' +
                '<div class="prio-info"><div class="prio-name" title="' + prioEsc(it.label) + '">' + prioEsc(it.label) + '</div>' +
                '<div class="prio-meta">Priority ' + prioFmt(it.v) + '</div></div>' +
                '<button class="prio-view" data-act="view" data-id="' + id + '">View</button>' +
                (it.done
                    ? '<button class="prio-x" data-act="undo" data-id="' + id + '" title="Put back in the list">&#8634;</button>'
                    : '<button class="prio-x" data-act="done" data-id="' + id + '" title="Mark as done (moves to bottom)">&times;</button>') +
                '</div>';
        });

        document.getElementById('prioList').innerHTML = html;
        document.getElementById('prioSummary').textContent = doneCount + ' / ' + items.length + ' done';
    }

    function prioRefresh() {
        prioLayer.setStyle(prioStyle);
        renderPrioList();
    }

    function prioView(id) {
        const layer = prioLayerById[id];
        if (!layer) return;
        if (!map.hasLayer(prioLayer)) map.addLayer(prioLayer);   // in case it was toggled off

        if (layer.getBounds) {
            map.fitBounds(layer.getBounds(), {padding: [50, 50], maxZoom: 18});
        } else if (layer.getLatLng) {
            map.flyTo(layer.getLatLng(), 17);
        }

        // Brief highlight so it's obvious which segment this is.
        if (layer.setStyle) {
            layer.setStyle({color: '#00e5ff', weight: 7, opacity: 1});
            if (layer.bringToFront) layer.bringToFront();
            setTimeout(() => layer.setStyle(prioStyle(layer.feature)), 1800);
        }
        try { layer.openPopup(); } catch (e) { /* popup anchor unavailable: ignore */ }
    }

    function prioHighlightRow(id) {
        const row = document.getElementById('prio-row-' + id);
        if (!row) return;
        row.scrollIntoView({block: 'nearest'});
        row.classList.add('flash');
        setTimeout(() => row.classList.remove('flash'), 1800);
    }

    if (prioHasData) {
        const prioControl = L.control({position: 'bottomright'});
        prioControl.onAdd = function () {
            const div = L.DomUtil.create('div', 'prio-panel');
            div.innerHTML =
                '<div class="prio-header" id="prioHeader"><span>Priority Segments</span>' +
                '<span class="prio-summary" id="prioSummary"></span>' +
                '<button class="prio-toggle" id="prioToggle" title="Collapse / expand">&minus;</button></div>' +
                '<div class="prio-body">' +
                '<div class="prio-scale"><span>Low</span><div class="prio-scale-bar"></div><span>High</span></div>' +
                '<div class="prio-list" id="prioList"></div>' +
                '<div class="prio-footer"><button class="prio-reset" id="prioReset">Reset all</button>' +
                '<span>Ticks are saved in this browser only</span></div></div>';
            L.DomEvent.disableScrollPropagation(div);
            L.DomEvent.disableClickPropagation(div);
            return div;
        };
        prioControl.addTo(map);

        const prioPanel = document.querySelector('.prio-panel');
        function prioSetCollapsed(collapsed) {
            prioPanel.classList.toggle('collapsed', collapsed);
            // On narrow screens the two bottom panels would overlap, so keep one open at a time.
            if (!collapsed && window.innerWidth < 700) {
                const other = document.querySelector('.info-panel');
                if (other) other.classList.add('collapsed');
            }
        }
        document.getElementById('prioHeader').addEventListener('click', () => {
            prioSetCollapsed(!prioPanel.classList.contains('collapsed'));
        });
        document.getElementById('prioToggle').addEventListener('click', (e) => {
            e.stopPropagation();
            prioSetCollapsed(!prioPanel.classList.contains('collapsed'));
        });
        if (window.innerWidth < 700) prioPanel.classList.add('collapsed');

        document.getElementById('prioList').addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-act]');
            if (!btn) return;
            const id = btn.dataset.id;
            if (btn.dataset.act === 'view') {
                prioView(id);
            } else if (btn.dataset.act === 'done') {
                prioDone.add(id); prioSave(); prioRefresh();
            } else if (btn.dataset.act === 'undo') {
                prioDone.delete(id); prioSave(); prioRefresh();
            }
        });
        document.getElementById('prioReset').addEventListener('click', () => {
            if (prioDone.size === 0) return;
            if (confirm('Put all ' + prioDone.size + ' ticked segment(s) back in the list?')) {
                prioDone.clear(); prioSave(); prioRefresh();
            }
        });

        renderPrioList();
    }
"""


POWER_PRESET_CSS = r"""
        /* ---- Preset layer buttons (top-left, under the zoom buttons) ---- */
        .preset-bar {
            background: rgba(255,255,255,0.97);
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 12px;
            padding: 6px;
            width: 158px;
            display: flex; flex-direction: column; gap: 4px;
        }
        .preset-title { font-size: 10px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; color: #999; padding: 2px 4px; }
        .preset-btn {
            display: flex; align-items: center; gap: 7px; width: 100%;
            padding: 6px 8px; border: 1px solid #ddd; background: #fff; color: #555;
            border-radius: 6px; cursor: pointer; text-align: left; font: inherit;
        }
        .preset-btn:hover:not(:disabled) { background: #f5f5f5; }
        .preset-btn.on { background: #eaf4fb; border-color: #3498db; color: #1f2d3a; font-weight: 600; }
        .preset-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .preset-swatch { width: 24px; height: 8px; border-radius: 4px; flex-shrink: 0; }
        .preset-text { display: flex; flex-direction: column; min-width: 0; flex: 1; line-height: 1.25; }
        .preset-range { font-size: 10px; color: #999; font-weight: 400; }
        .preset-check { visibility: hidden; color: #3498db; font-size: 13px; }
        .preset-btn.on .preset-check { visibility: visible; }
        @media (max-width: 600px) {
            .preset-bar { width: 138px; font-size: 11px; }
        }
"""

POWER_PRESET_JS = r"""
    // ---- Value & vulnerability layers, plus the preset toggle buttons ----
    function scoreEsc(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    function scoreFmt(v) {
        return Number.isInteger(v) ? String(v) : v.toFixed(2);
    }
    function scoreRangeText(min, max) {
        return (isFinite(min) && isFinite(max)) ? scoreFmt(min) + ' – ' + scoreFmt(max) : '';
    }

    const SCORE_LAYERS = {
        value: {key: 'value', title: 'Value', scoreName: 'Value score', pane: 'valuePane', z: 350, hue: 275, sat: 55, light0: 80, light1: 28},
        vulnerability: {key: 'vulnerability', title: 'Vulnerability', scoreName: 'Vulnerability (gridcode)', pane: 'vulnerabilityPane', z: 351, hue: 210, sat: 80, light0: 80, light1: 28}
    };
    // Their own panes keep these area layers underneath the weed points, lines and polygons.
    Object.keys(SCORE_LAYERS).forEach(k => {
        map.createPane(SCORE_LAYERS[k].pane);
        map.getPane(SCORE_LAYERS[k].pane).style.zIndex = SCORE_LAYERS[k].z;
    });

    function makeScoreLayer(cfg) {
        const fc = geoData[cfg.key] || {type: 'FeatureCollection', features: []};
        const feats = (fc.features || []).filter(f => f && f.geometry && f.properties);
        let min = Infinity, max = -Infinity;
        function scoreOf(f) {
            const s = f.properties._score;
            if (s === null || s === undefined) return null;   // Number(null) would be 0
            const n = Number(s);
            return isFinite(n) ? n : null;
        }
        feats.forEach(f => {
            const n = scoreOf(f);
            if (n !== null) { min = Math.min(min, n); max = Math.max(max, n); }
        });
        function shade(f, darker) {
            const n = scoreOf(f);
            if (n === null) return '#95a5a6';
            const frac = max === min ? 0.5 : (n - min) / (max - min);
            const light = cfg.light0 + (cfg.light1 - cfg.light0) * frac - (darker ? 8 : 0);
            return 'hsl(' + cfg.hue + ',' + cfg.sat + '%,' + light.toFixed(0) + '%)';
        }
        const layer = L.geoJSON(feats, {
            pane: cfg.pane,
            style: f => ({
                color: shade(f, true), weight: /Line/.test(f.geometry.type) ? 4 : 1, opacity: 0.85,
                fillColor: shade(f, false), fillOpacity: 0.55
            }),
            pointToLayer: (f, latlng) => L.circleMarker(latlng, {pane: cfg.pane, radius: 6}),
            onEachFeature: (f, l) => {
                const n = scoreOf(f);
                l.bindPopup('<b>' + scoreEsc(cfg.title) + '</b>' +
                    (f.properties._label ? '<br>' + scoreEsc(f.properties._label) : '') +
                    '<br>' + scoreEsc(cfg.scoreName) + ': ' + (n === null ? 'n/a' : scoreFmt(n)));
            }
        });
        return {layer: layer, hasData: feats.length > 0, min: min, max: max};
    }

    const valueScore = makeScoreLayer(SCORE_LAYERS.value);
    const vulnScore = makeScoreLayer(SCORE_LAYERS.vulnerability);
    // Off by default (they cover a lot of the map); the buttons below turn them on.
    if (valueScore.hasData) overlayLayers['Value Layer'] = valueScore.layer;
    if (vulnScore.hasData) overlayLayers['Vulnerability Layer'] = vulnScore.layer;

    function rampCss(cfg) {
        return 'linear-gradient(to right, hsl(' + cfg.hue + ',' + cfg.sat + '%,' + cfg.light0 + '%), hsl(' +
            cfg.hue + ',' + cfg.sat + '%,' + cfg.light1 + '%))';
    }
    function layerToggle(layer) {
        return on => { if (on) map.addLayer(layer); else map.removeLayer(layer); };
    }

    const PRESETS = [
        {
            id: 'bufhot', label: 'Buffers & Hotspots', swatch: '#e67e22', enabled: true, note: '',
            tip: 'Show or hide the 200 m buffers and the numbered hotspot markers together',
            isOn: () => map.hasLayer(bufferGroup) && hotspotMarkersVisible,
            set: on => { layerToggle(bufferGroup)(on); setHotspotMarkers(on); }
        },
        {
            id: 'priority', label: 'Priority', swatch: 'linear-gradient(to right, hsl(55,90%,50%), hsl(0,90%,50%))',
            enabled: prioHasData, note: prioHasData ? scoreRangeText(prioMin, prioMax) : 'No data',
            tip: prioHasData ? 'Show or hide the priority segments' : 'No priority layer was found when the page was built',
            isOn: () => map.hasLayer(prioLayer), set: layerToggle(prioLayer)
        },
        {
            id: 'value', label: 'Value', swatch: rampCss(SCORE_LAYERS.value),
            enabled: valueScore.hasData, note: valueScore.hasData ? scoreRangeText(valueScore.min, valueScore.max) : 'No data',
            tip: valueScore.hasData ? 'Show or hide the value layer' : 'No value layer was found when the page was built',
            isOn: () => map.hasLayer(valueScore.layer), set: layerToggle(valueScore.layer)
        },
        {
            id: 'vulnerability', label: 'Vulnerability', swatch: rampCss(SCORE_LAYERS.vulnerability),
            enabled: vulnScore.hasData, note: vulnScore.hasData ? scoreRangeText(vulnScore.min, vulnScore.max) : 'No data',
            tip: vulnScore.hasData ? 'Show or hide the vulnerability layer' : 'No vulnerability layer was found when the page was built',
            isOn: () => map.hasLayer(vulnScore.layer), set: layerToggle(vulnScore.layer)
        }
    ];

    const presetControl = L.control({position: 'topleft'});
    presetControl.onAdd = function () {
        const div = L.DomUtil.create('div', 'preset-bar');
        let html = '<div class="preset-title">Layers</div>';
        PRESETS.forEach(p => {
            html += '<button type="button" class="preset-btn" id="preset-' + p.id + '" aria-pressed="false"' +
                (p.enabled ? '' : ' disabled') + ' title="' + scoreEsc(p.tip) + '">' +
                '<span class="preset-swatch" style="background:' + p.swatch + '"></span>' +
                '<span class="preset-text"><span>' + scoreEsc(p.label) + '</span>' +
                (p.note ? '<span class="preset-range">' + scoreEsc(p.note) + '</span>' : '') + '</span>' +
                '<span class="preset-check">&#10003;</span></button>';
        });
        div.innerHTML = html;
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);
        return div;
    };
    presetControl.addTo(map);

    // The buttons always reflect what is really on the map, so they stay correct when a
    // layer is also toggled from the top-right layer control, the Hotspots tab or "View".
    function syncPresetButtons() {
        PRESETS.forEach(p => {
            const btn = document.getElementById('preset-' + p.id);
            if (!btn) return;
            const on = !!(p.enabled && p.isOn());
            btn.classList.toggle('on', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }
    let presetSyncQueued = false;
    function schedulePresetSync() {
        if (presetSyncQueued) return;
        presetSyncQueued = true;
        requestAnimationFrame(() => { presetSyncQueued = false; syncPresetButtons(); });
    }
    const presetWatched = new Set([bufferGroup, hotspotMarkersGroup, prioLayer, valueScore.layer, vulnScore.layer]);
    map.on('layeradd layerremove', e => { if (presetWatched.has(e.layer)) schedulePresetSync(); });

    PRESETS.forEach(p => {
        const btn = document.getElementById('preset-' + p.id);
        if (!btn) return;
        btn.addEventListener('click', () => {
            if (!p.enabled) return;
            p.set(!p.isOn());
            syncPresetButtons();
        });
    });
    syncPresetButtons();
"""


# ==============================================================================
# STEP 2: BUILD LEAFLET HTML
# ==============================================================================
def _js_json(obj) -> str:
    """json.dumps for embedding inside an HTML <script> block.

    Plain json.dumps leaves '<' alone, so a value containing "</script>" (a species or
    segment name, say) would end the script early and break the page. '\\u003c' is the
    same string to JavaScript but can't be mistaken for a tag.
    """
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_power_dashboard_html(geo: dict, build_time: str) -> str:
    """Generate the full "power user" single-file Leaflet dashboard: every layer,
    exact point locations, per-species heatmaps, hotspot ranking, and the full
    ArcGIS-style insights dashboard (including the per-project breakdown).

    This file is intentionally NOT linked from the public page. It's still a
    plain static file in the repo though — if the repo is public, anyone who
    finds the URL (or just browses the repo's file list) can open it. See the
    module docstring for what that does and doesn't protect against.
    """
    map_payload = {
        "points": geo["points"],
        "lines": geo["lines"],
        "polygons": geo["polygons"],
        "work_areas": geo["work_areas"],
        "tracks": geo["tracks"],
        "priority": geo.get("priority") or {"type": "FeatureCollection", "features": []},
        "value": geo.get("value") or {"type": "FeatureCollection", "features": []},
        "vulnerability": geo.get("vulnerability") or {"type": "FeatureCollection", "features": []},
    }

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WeedManager NZ Dashboard</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="noindex, nofollow">

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.heat/0.2.0/leaflet-heat.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>

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

        /* ---- Insights dashboard (ArcGIS Dashboards-style) ---- */
        .dashboard-overlay {{
            display: none;
            position: fixed; inset: 0;
            background: #181b21;
            z-index: 2000;
            flex-direction: column;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            color: #e7ebf0;
            overflow-y: auto;
        }}
        .dash-topbar {{
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 24px; background: #12151a; border-bottom: 1px solid #2e333d;
            position: sticky; top: 0; z-index: 1;
        }}
        .dash-topbar h2 {{ margin: 0; font-size: 17px; font-weight: 600; letter-spacing: 0.3px; }}
        .dash-subtitle {{ font-size: 11px; color: #8b93a1; margin-top: 2px; }}
        .dash-back-btn {{
            background: #20242c; border: 1px solid #2e333d; color: #e7ebf0;
            padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;
        }}
        .dash-back-btn:hover {{ background: #2a3038; }}

        .dash-body {{ padding: 20px 24px 32px 24px; flex: 1; }}

        .dash-kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }}
        .dash-kpi {{
            background: #20242c; border: 1px solid #2e333d; border-left: 3px solid #00bcd4;
            border-radius: 4px; padding: 12px 14px;
        }}
        .dash-kpi-value {{ font-size: 24px; font-weight: 700; color: #e7ebf0; line-height: 1.1; }}
        .dash-kpi-label {{ font-size: 10.5px; color: #8b93a1; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 4px; }}

        .dash-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
        .dash-grid .dash-panel.wide {{ grid-column: span 2; }}
        .dash-grid .dash-panel.full {{ grid-column: span 3; }}
        .dash-panel {{
            background: #20242c; border: 1px solid #2e333d; border-radius: 4px;
            display: flex; flex-direction: column; min-height: 280px;
        }}
        .dash-panel-header {{
            padding: 10px 14px; border-bottom: 1px solid #2e333d;
            font-size: 11px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase; color: #8b93a1;
        }}
        .dash-panel-body {{ padding: 14px; flex: 1; position: relative; }}
        .dash-panel-body canvas {{ max-height: 230px; }}

        .dash-map-panel {{ min-height: 400px; }}
        .dash-map-panel .dash-panel-body {{ padding: 0; }}
        #dashMapContainer {{ width: 100%; height: 360px; border-radius: 0 0 4px 4px; background: #12151a; }}
        /* Leaflet's own light-themed controls need a dark-friendly tweak here */
        #dashMapContainer .leaflet-control-layers,
        #dashMapContainer .leaflet-bar {{ background: #20242c; color: #e7ebf0; border-color: #2e333d; }}

        .dash-hotspot-list {{ display: flex; flex-direction: column; gap: 8px; }}
        .dash-hotspot-row {{ display: flex; align-items: center; gap: 10px; padding: 8px 10px; background: #262b34; border-radius: 4px; }}
        .dash-hotspot-rank {{
            width: 20px; height: 20px; border-radius: 50%; background: #00bcd4; color: #0b0f14;
            font-weight: 700; font-size: 10.5px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        }}
        .dash-hotspot-swatch {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
        .dash-hotspot-info {{ flex: 1; min-width: 0; }}
        .dash-hotspot-name {{ font-weight: 600; font-size: 12.5px; }}
        .dash-hotspot-meta {{ font-size: 10.5px; color: #8b93a1; }}
        .dash-hotspot-count {{ font-size: 16px; font-weight: 700; color: #00bcd4; flex-shrink: 0; }}

        .dashboard-empty-note {{ color: #8b93a1; font-size: 12px; padding: 14px; }}

        @media (max-width: 900px) {{
            .dash-kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
            .dash-grid {{ grid-template-columns: 1fr; }}
            .dash-grid .dash-panel.wide,
            .dash-grid .dash-panel.full {{ grid-column: span 1; }}
        }}

        @media (max-width: 600px) {{
            .info-panel {{ width: 230px; font-size: 12px; }}
            .panel-body {{ max-height: 220px; }}
        }}
{POWER_PRIORITY_CSS}
{POWER_PRESET_CSS}
    </style>
</head>
<body>

<div id="map"></div>
<div id="data-warning">No point features were returned — check the WFS credentials/project IDs.</div>

<div id="dashboardOverlay" class="dashboard-overlay">
    <div class="dash-topbar">
        <div>
            <h2>Weed Insights Dashboard</h2>
            <div class="dash-subtitle" id="dashSubtitle">Last updated —</div>
        </div>
        <button id="dashboardClose" class="dash-back-btn">&larr; Back to Map</button>
    </div>
    <div class="dash-body">
        <div class="dash-kpi-row">
            <div class="dash-kpi"><div class="dash-kpi-value" id="statTotalPoints">0</div><div class="dash-kpi-label">Total Sightings</div></div>
            <div class="dash-kpi"><div class="dash-kpi-value" id="statSpecies">0</div><div class="dash-kpi-label">Species Tracked</div></div>
            <div class="dash-kpi"><div class="dash-kpi-value" id="statArea">0 ha</div><div class="dash-kpi-label">Mapped Area</div></div>
            <div class="dash-kpi"><div class="dash-kpi-value" id="statProjects">0</div><div class="dash-kpi-label">Projects</div></div>
            <div class="dash-kpi"><div class="dash-kpi-value" id="statDateRange" style="font-size:15px;">—</div><div class="dash-kpi-label">Date Range</div></div>
        </div>
        <div class="dash-grid">
            <div class="dash-panel full dash-map-panel">
                <div class="dash-panel-header">Map Overview</div>
                <div class="dash-panel-body"><div id="dashMapContainer"></div></div>
            </div>
            <div class="dash-panel">
                <div class="dash-panel-header">Species Distribution</div>
                <div class="dash-panel-body"><canvas id="speciesChart"></canvas></div>
            </div>
            <div class="dash-panel wide" id="timelineWrap">
                <div class="dash-panel-header">Sightings Over Time</div>
                <div class="dash-panel-body"><canvas id="timelineChart"></canvas></div>
            </div>
            <div class="dash-panel" id="areaWrap">
                <div class="dash-panel-header">Area by Species (ha)</div>
                <div class="dash-panel-body"><canvas id="areaChart"></canvas></div>
            </div>
            <div class="dash-panel">
                <div class="dash-panel-header">Points by Project</div>
                <div class="dash-panel-body"><canvas id="projectChart"></canvas></div>
            </div>
            <div class="dash-panel">
                <div class="dash-panel-header">Top Hotspots</div>
                <div class="dash-panel-body"><div class="dash-hotspot-list" id="dashHotspotList"></div></div>
            </div>
        </div>
    </div>
</div>

<script>
    const geoData = {_js_json(map_payload)};
    const BUILD_TIME = {_js_json(build_time)};

    // Dark-theme defaults for every Chart.js chart in the dashboard.
    if (typeof Chart !== 'undefined') {{
        Chart.defaults.color = '#c5cbd3';
        Chart.defaults.borderColor = 'rgba(255,255,255,0.08)';
    }}

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

    // Register every species found in points, lines AND polygons so the
    // dropdown can select species that only appear in the line/polygon layers.
    [geoData.points, geoData.lines, geoData.polygons].forEach(fc => {{
        ((fc && fc.features) || []).forEach(f => colorFor(speciesOf(f)));
    }});

    // ---- Species filter (applies to points, lines, polygons, buffers,
    // heatmaps and hotspots). Tracks and work areas aren't species data, so
    // they're always shown. ----
    function selectedSpecies() {{
        const el = document.getElementById('spSelect');
        return el ? el.value : 'ALL';
    }}

    function matchesSelectedSpecies(feature) {{
        const sel = selectedSpecies();
        return sel === 'ALL' || speciesOf(feature) === sel;
    }}

    // ---- Vector layers ----
    const pointLayer = L.geoJSON(geoData.points, {{
        filter: matchesSelectedSpecies,
        pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
            radius: 5,
            fillColor: colorFor(speciesOf(f)),
            color: '#ffffff',
            weight: 1,
            fillOpacity: 0.8
        }}),
        onEachFeature: (f, l) => l.bindPopup(`<b>Species:</b> ${{speciesOf(f)}}`)
    }}).addTo(map);

    const lineLayer = L.geoJSON(geoData.lines, {{ filter: matchesSelectedSpecies, style: {{ color: '#f39c12', weight: 3 }} }}).addTo(map);
    const trackLayer = L.geoJSON(geoData.tracks, {{ style: {{ color: '#2ecc71', weight: 2, dashArray: '4' }} }}).addTo(map);
    const polyLayer = L.geoJSON(geoData.polygons, {{ filter: matchesSelectedSpecies, style: {{ color: '#e74c3c', fillOpacity: 0.3 }} }}).addTo(map);
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

    // Re-apply the species filter to the point/line/polygon layers. addData()
    // runs each feature back through the layer's `filter` option.
    function refreshVectorLayers() {{
        [[pointLayer, geoData.points], [lineLayer, geoData.lines], [polyLayer, geoData.polygons]].forEach(pair => {{
            pair[0].clearLayers();
            if (pair[1]) pair[0].addData(pair[1]);
        }});
    }}

    function updateMap() {{
        refreshVectorLayers();
        updateBuffers();
        updateHeatmaps();
        refreshActiveTab();
        refreshHotspotMarkers();
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
    // On by default so it matches the "Buffers & Hotspots" preset button, which starts "on".
    // Driven by the Hotspots tab checkbox and that button (setHotspotMarkers keeps them in step).
    let hotspotMarkersVisible = true;
    const hotspotMarkersGroup = L.layerGroup();

    function computeHotspots(sortBy, ignoreFilter) {{
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
        if (!ignoreFilter && sel !== 'ALL') {{
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

    // Turn the numbered hotspot markers on/off (used by the preset button) and keep the
    // Hotspots tab checkbox in step.
    function setHotspotMarkers(on) {{
        hotspotMarkersVisible = on;
        if (on) {{
            renderHotspotMarkers(computeHotspots(hotspotSortBy));
        }} else {{
            map.removeLayer(hotspotMarkersGroup);
            hotspotMarkersGroup.clearLayers();
        }}
        const cb = document.getElementById('hotspotMarkersToggle');
        if (cb) cb.checked = on;
    }}

    // Re-draw the markers after the species filter changes. When the Hotspots tab is open it
    // re-renders them itself, so skip that case to avoid doing the work twice.
    function refreshHotspotMarkers() {{
        if (!hotspotMarkersVisible) return;
        const activeBtn = document.querySelector('.tab-btn.active');
        if (activeBtn && activeBtn.dataset.tab === 'hotspots') return;
        renderHotspotMarkers(computeHotspots(hotspotSortBy));
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

    // ---- Insights dashboard (full-screen overlay) ----
    let dashboardCharts = {{}};

    function destroyDashboardCharts() {{
        Object.keys(dashboardCharts).forEach(key => {{
            if (dashboardCharts[key]) dashboardCharts[key].destroy();
        }});
        dashboardCharts = {{}};
    }}

    function computeDashboardData() {{
        const speciesCounts = {{}};
        const projectCounts = {{}};
        const monthlyCounts = {{}};
        const areaBySpecies = {{}};
        let totalArea = 0;
        let minDate = null, maxDate = null;

        (geoData.points.features || []).forEach(f => {{
            const sp = speciesOf(f);
            speciesCounts[sp] = (speciesCounts[sp] || 0) + 1;

            const proj = (f.properties && f.properties._wm_project_id) || 'Unknown';
            projectCounts[proj] = (projectCounts[proj] || 0) + 1;

            const d = extractDate(f.properties);
            if (d) {{
                const key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
                monthlyCounts[key] = (monthlyCounts[key] || 0) + 1;
                if (!minDate || d < minDate) minDate = d;
                if (!maxDate || d > maxDate) maxDate = d;
            }}
        }});

        (geoData.polygons.features || []).forEach(f => {{
            const sp = speciesOf(f);
            try {{
                const a = turf.area(f);
                areaBySpecies[sp] = (areaBySpecies[sp] || 0) + a;
                totalArea += a;
            }} catch (e) {{ /* skip invalid geometry */ }}
        }});

        return {{
            totalPoints: (geoData.points.features || []).length,
            speciesCounts, projectCounts, monthlyCounts, areaBySpecies, totalArea,
            minDate, maxDate,
            speciesTotal: Object.keys(speciesCounts).length,
            projectTotal: Object.keys(projectCounts).length
        }};
    }}

    function renderDashboard() {{
        destroyDashboardCharts();
        const data = computeDashboardData();

        document.getElementById('dashSubtitle').textContent = 'Data built ' + BUILD_TIME;
        document.getElementById('statTotalPoints').textContent = data.totalPoints;
        document.getElementById('statSpecies').textContent = data.speciesTotal;
        document.getElementById('statArea').textContent = (data.totalArea / 10000).toFixed(1) + ' ha';
        document.getElementById('statProjects').textContent = data.projectTotal;
        document.getElementById('statDateRange').textContent = (data.minDate && data.maxDate)
            ? (data.minDate.toLocaleDateString() + ' – ' + data.maxDate.toLocaleDateString())
            : 'No date data available';

        renderDashHotspots();

        // Species distribution
        const spLabels = Object.keys(data.speciesCounts).sort((a, b) => data.speciesCounts[b] - data.speciesCounts[a]);
        if (spLabels.length > 0) {{
            dashboardCharts.species = new Chart(document.getElementById('speciesChart'), {{
                type: 'doughnut',
                data: {{
                    labels: spLabels,
                    datasets: [{{ data: spLabels.map(l => data.speciesCounts[l]), backgroundColor: spLabels.map(l => colorFor(l)) }}]
                }},
                options: {{ plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }} }} }}
            }});
        }}

        // Sightings over time
        const monthKeys = Object.keys(data.monthlyCounts).sort();
        const timelineWrap = document.getElementById('timelineWrap');
        if (monthKeys.length > 1) {{
            timelineWrap.style.display = 'block';
            dashboardCharts.timeline = new Chart(document.getElementById('timelineChart'), {{
                type: 'line',
                data: {{
                    labels: monthKeys,
                    datasets: [{{
                        label: 'Sightings',
                        data: monthKeys.map(k => data.monthlyCounts[k]),
                        borderColor: '#3498db',
                        backgroundColor: 'rgba(52,152,219,0.15)',
                        fill: true,
                        tension: 0.25
                    }}]
                }},
                options: {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
            }});
        }} else {{
            timelineWrap.style.display = 'none';
        }}

        // Area by species
        const areaLabels = Object.keys(data.areaBySpecies)
            .filter(k => data.areaBySpecies[k] > 0)
            .sort((a, b) => data.areaBySpecies[b] - data.areaBySpecies[a]);
        const areaWrap = document.getElementById('areaWrap');
        if (areaLabels.length > 0) {{
            areaWrap.style.display = 'block';
            dashboardCharts.area = new Chart(document.getElementById('areaChart'), {{
                type: 'bar',
                data: {{
                    labels: areaLabels,
                    datasets: [{{
                        label: 'Area (ha)',
                        data: areaLabels.map(l => data.areaBySpecies[l] / 10000),
                        backgroundColor: areaLabels.map(l => colorFor(l))
                    }}]
                }},
                options: {{ indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ beginAtZero: true }} }} }}
            }});
        }} else {{
            areaWrap.style.display = 'none';
        }}

        // Points by project
        const projLabels = Object.keys(data.projectCounts).sort();
        if (projLabels.length > 0) {{
            dashboardCharts.projects = new Chart(document.getElementById('projectChart'), {{
                type: 'bar',
                data: {{
                    labels: projLabels,
                    datasets: [{{ label: 'Points', data: projLabels.map(p => data.projectCounts[p]), backgroundColor: '#9b59b6' }}]
                }},
                options: {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
            }});
        }}
    }}

    function renderDashHotspots() {{
        // Ignores the sidebar's species filter — this widget always shows the
        // overall top hotspots across every species, like an ArcGIS "top N" list.
        const hotspots = computeHotspots('count', true).slice(0, 6);
        const container = document.getElementById('dashHotspotList');

        if (hotspots.length === 0) {{
            container.innerHTML = '<div class="dashboard-empty-note">No dense clusters detected.</div>';
            return;
        }}

        let html = '';
        hotspots.forEach((h, i) => {{
            const lastSeenText = h.lastSeen ? h.lastSeen.toLocaleDateString() : 'no date data';
            html += '<div class="dash-hotspot-row">' +
                '<span class="dash-hotspot-rank">' + (i + 1) + '</span>' +
                '<span class="dash-hotspot-swatch" style="background:' + colorFor(h.species) + '"></span>' +
                '<div class="dash-hotspot-info">' +
                    '<div class="dash-hotspot-name">' + h.species + '</div>' +
                    '<div class="dash-hotspot-meta">' + h.pctOfSpecies.toFixed(0) + '% of species &middot; last seen ' + lastSeenText + '</div>' +
                '</div>' +
                '<div class="dash-hotspot-count">' + h.count + '</div>' +
            '</div>';
        }});
        container.innerHTML = html;
    }}

    // ---- Interactive map widget inside the dashboard ----
    // A second, independent Leaflet map (Leaflet layers can't be shared
    // between two map instances, so this rebuilds its own copies of the
    // vector layers from the same geoData). Initialized once, lazily, the
    // first time the dashboard is opened — Leaflet needs a visible,
    // correctly-sized container to measure itself against.
    let dashMap = null;

    function initDashMap() {{
        if (dashMap) {{
            // Already built — just make sure it re-measures the container,
            // since it may have been created while the overlay was hidden.
            setTimeout(() => dashMap.invalidateSize(), 50);
            return;
        }}

        dashMap = L.map('dashMapContainer', {{ center: [-41.2865, 174.7762], zoom: 6 }});

        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap'
        }}).addTo(dashMap);

        const dashPointLayer = L.geoJSON(geoData.points, {{
            pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
                radius: 4,
                fillColor: colorFor(speciesOf(f)),
                color: '#fff',
                weight: 1,
                fillOpacity: 0.85
            }}),
            onEachFeature: (f, l) => l.bindPopup('<b>Species:</b> ' + speciesOf(f))
        }}).addTo(dashMap);

        const dashLineLayer = L.geoJSON(geoData.lines, {{ style: {{ color: '#f39c12', weight: 2 }} }}).addTo(dashMap);
        const dashTrackLayer = L.geoJSON(geoData.tracks, {{ style: {{ color: '#2ecc71', weight: 2, dashArray: '4' }} }}).addTo(dashMap);
        const dashPolyLayer = L.geoJSON(geoData.polygons, {{ style: {{ color: '#e74c3c', fillOpacity: 0.3 }} }}).addTo(dashMap);
        const dashWorkLayer = L.geoJSON(geoData.work_areas, {{ style: {{ color: '#3498db', dashArray: '4', fillOpacity: 0.1 }} }}).addTo(dashMap);

        L.control.layers(null, {{
            "Points": dashPointLayer,
            "Lines": dashLineLayer,
            "Tracks": dashTrackLayer,
            "Polygons": dashPolyLayer,
            "Work Areas": dashWorkLayer
        }}, {{ collapsed: true, position: 'topright' }}).addTo(dashMap);

        const dashBounds = L.featureGroup([dashPointLayer, dashLineLayer, dashTrackLayer, dashPolyLayer, dashWorkLayer]).getBounds();
        if (dashBounds.isValid()) {{
            dashMap.fitBounds(dashBounds, {{ padding: [15, 15] }});
        }} else {{
            dashMap.fitBounds([[-47.0, 166.0], [-34.0, 179.0]]);
        }}

        // The container had zero size while the overlay was display:none —
        // force Leaflet to re-measure now that it's actually visible.
        setTimeout(() => dashMap.invalidateSize(), 50);
    }}

    function openDashboard() {{
        document.getElementById('dashboardOverlay').style.display = 'flex';
        renderDashboard();
        initDashMap();
    }}

    function closeDashboard() {{
        document.getElementById('dashboardOverlay').style.display = 'none';
    }}

    document.getElementById('dashboardClose').addEventListener('click', closeDashboard);
    document.getElementById('dashboardOverlay').addEventListener('click', (e) => {{
        if (e.target.id === 'dashboardOverlay') closeDashboard();
    }});
    document.addEventListener('keydown', (e) => {{
        if (e.key === 'Escape') closeDashboard();
    }});

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
                <button class="tab-btn" data-tab="dashboard">Insights</button>
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

    // Tab switching. "Dashboard" is special-cased: it opens the full-screen
    // insights overlay rather than swapping the sidebar's own content, so it
    // never becomes the "active" sidebar tab.
    document.querySelectorAll('.tab-btn').forEach(btn => {{
        btn.addEventListener('click', () => {{
            if (btn.dataset.tab === 'dashboard') {{
                openDashboard();
                return;
            }}

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
        "Tracks": trackLayer,
        "Polygons": polyLayer,
        "Work Areas": workLayer
    }};

{POWER_PRIORITY_JS}

{POWER_PRESET_JS}

    L.control.layers(
        {{ "Satellite Imagery": esriSat, "OpenStreetMap": osmBase }},
        overlayLayers,
        {{ collapsed: true, position: 'topright' }}
    ).addTo(map);

    updateMap();

    const allBounds = L.featureGroup([pointLayer, lineLayer, trackLayer, polyLayer, workLayer, prioLayer]).getBounds();
    if (allBounds.isValid()) {{
        map.fitBounds(allBounds, {{ padding: [20, 20] }});
    }} else {{
        map.fitBounds([[-47.0, 166.0], [-34.0, 179.0]]);
    }}
</script>
</body>
</html>
"""


def _sanitize_points_for_public(points_fc: dict, precision: int = 3) -> dict:
    """Strip and coarsen point data before it ever reaches the public page.

    Not rendering exact markers isn't enough on its own — the full-precision
    GeoJSON would still be embedded verbatim in the page's JavaScript and
    readable via "View Source", including every original property (dates,
    _wm_project_id, etc.). This rounds coordinates to ~100m precision and
    keeps only the species name, so that's genuinely the most anyone can
    recover from the public page, not just what happens to get drawn.
    """
    sanitized = []
    for f in points_fc.get("features", []):
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Point" and coords:
            new_coords = [round(coords[0], precision), round(coords[1], precision)]
        elif gtype == "MultiPoint" and coords:
            new_coords = [[round(c[0], precision), round(c[1], precision)] for c in coords]
        else:
            continue  # unrecognized geometry — drop rather than pass through un-sanitized

        props = f.get("properties") or {}
        species = props.get("species") or props.get("name") or "Unspecified"
        sanitized.append({
            "type": "Feature",
            "geometry": {"type": gtype, "coordinates": new_coords},
            "properties": {"species": species},
        })
    return {"type": "FeatureCollection", "features": sanitized}


def _sanitize_polygons_for_public(polygons_fc: dict) -> dict:
    """Strip extraneous properties (dates, project IDs, etc.) from polygons.

    Coordinates are left as-is — a polygon already describes an area rather
    than a pinpoint, so precision isn't the concern here, just not leaking
    unrelated attributes.
    """
    sanitized = []
    for f in polygons_fc.get("features", []):
        props = f.get("properties") or {}
        species = props.get("species") or props.get("name") or "Unspecified"
        sanitized.append({
            "type": "Feature",
            "geometry": f.get("geometry"),
            "properties": {"species": species},
        })
    return {"type": "FeatureCollection", "features": sanitized}


def build_public_html(geo: dict, build_time: str, power_password_hash: str, power_page_path: str) -> str:
    """Generate the simplified, public-facing dashboard.

    Deliberately excludes: exact point markers, tracks, work areas, hotspot
    coordinates, and any per-project breakdown. Shows a combined per-species
    heatmap (still communicates roughly WHERE weeds are common, without
    publishing precise infestation coordinates) plus the weed polygons layer
    (already an area rather than a pinpoint, so lower-sensitivity by nature).

    The underlying data is sanitized in Python (see the two helpers above)
    before it's embedded, not just left out of what gets rendered — the
    embedded GeoJSON is the actual attack surface here, since it's plain
    JSON sitting in the page's source.

    Also includes a lock button: visitors who enter a password matching
    power_password_hash (SHA-256, computed client-side — the plaintext
    password is never embedded) get navigated to power_page_path. See the
    module docstring for exactly what this does and doesn't protect.
    """
    map_payload = {
        "points": _sanitize_points_for_public(geo["points"]),
        "polygons": _sanitize_polygons_for_public(geo["polygons"]),
    }

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Weed Watch — Public Map</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.heat/0.2.0/leaflet-heat.js"></script>

    <style>
        html, body, #map {{ width: 100%; height: 100%; margin: 0; padding: 0; }}
        * {{ box-sizing: border-box; }}

        .pub-panel {{
            background: rgba(255,255,255,0.97);
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.25);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 13px;
            width: 250px;
            max-width: 85vw;
            padding: 14px;
        }}
        .pub-panel h3 {{ margin: 0 0 8px 0; font-size: 15px; color: #2c3e50; }}
        .pub-panel select {{ width: 100%; padding: 5px; margin-bottom: 10px; }}
        .pub-legend-list {{
            max-height: 140px;
            overflow-y: auto;
            padding-right: 4px;
            border-top: 1px solid #eee;
            border-bottom: 1px solid #eee;
            margin-bottom: 4px;
        }}
        .pub-legend-row {{ display: flex; align-items: center; font-size: 12px; padding: 4px 0; }}
        .pub-badge {{ width: 11px; height: 11px; border-radius: 50%; margin-right: 7px; flex-shrink: 0; }}
        .pub-stats {{ margin-top: 10px; padding-top: 10px; font-size: 11px; color: #777; }}
        .pub-footer {{ margin-top: 8px; font-size: 10px; color: #aaa; }}

        .pub-lock-btn {{
            position: fixed; bottom: 14px; right: 14px;
            width: 34px; height: 34px; border-radius: 50%;
            background: rgba(0,0,0,0.55); color: #fff; border: none;
            font-size: 15px; cursor: pointer; z-index: 500;
            display: flex; align-items: center; justify-content: center;
        }}
        .pub-lock-btn:hover {{ background: rgba(0,0,0,0.75); }}

        @media (max-width: 600px) {{
            .pub-panel {{ width: 200px; font-size: 12px; }}
            .pub-legend-list {{ max-height: 110px; }}
        }}
    </style>
</head>
<body>

<div id="map"></div>
<button id="powerLockBtn" class="pub-lock-btn" title="Power user view">&#128274;</button>

<script>
    const geoData = {_js_json(map_payload)};
    const BUILD_TIME = {_js_json(build_time)};

    const map = L.map('map', {{ center: [-41.2865, 174.7762], zoom: 6 }});

    const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        attribution: 'Tiles &copy; Esri'
    }}).addTo(map);

    const osmBase = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap'
    }});

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

    // Register species from both points and polygons so the dropdown can
    // select species that only appear as polygons.
    [geoData.points, geoData.polygons].forEach(fc => {{
        ((fc && fc.features) || []).forEach(f => colorFor(speciesOf(f)));
    }});

    function matchesSelectedSpecies(feature) {{
        const el = document.getElementById('spSelect');
        const sel = el ? el.value : 'ALL';
        return sel === 'ALL' || speciesOf(feature) === sel;
    }}

    // Weed polygons only — an area, not a pinpoint, so lower sensitivity.
    const polyLayer = L.geoJSON(geoData.polygons, {{
        filter: matchesSelectedSpecies,
        style: (f) => ({{ color: colorFor(speciesOf(f)), fillOpacity: 0.35, weight: 1 }}),
        onEachFeature: (f, l) => l.bindPopup('<b>' + speciesOf(f) + '</b>')
    }}).addTo(map);

    // Heatmaps built from point data, but individual point markers are never
    // rendered on this page — only the smoothed density surface.
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

    const speciesHeatmaps = {{}};
    Object.keys(heatPointsBySpecies).forEach(spName => {{
        const baseColor = colorFor(spName);
        speciesHeatmaps[spName] = L.heatLayer(heatPointsBySpecies[spName], {{
            radius: 30,
            blur: 20,
            minOpacity: 0.3,
            maxZoom: 13,
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

    const panelControl = L.control({{ position: 'bottomleft' }});
    panelControl.onAdd = function () {{
        const div = L.DomUtil.create('div', 'pub-panel');

        let options = '<option value="ALL">-- All Species --</option>';
        Object.keys(speciesColors).sort().forEach(sp => {{
            options += '<option value="' + sp + '">' + sp + '</option>';
        }});

        let legendRows = '';
        Object.keys(speciesColors).sort().forEach(sp => {{
            legendRows += '<div class="pub-legend-row"><span class="pub-badge" style="background:' + speciesColors[sp] + '"></span>' + sp + '</div>';
        }});

        div.innerHTML =
            '<h3>Weed Watch</h3>' +
            '<select id="spSelect">' + options + '</select>' +
            '<div class="pub-legend-list">' + legendRows + '</div>' +
            '<div class="pub-stats">' + Object.keys(speciesColors).length + ' species tracked</div>' +
            '<div class="pub-footer">Updated ' + BUILD_TIME + '</div>';

        L.DomEvent.disableScrollPropagation(div);
        L.DomEvent.disableClickPropagation(div);
        return div;
    }};
    panelControl.addTo(map);

    function updateAll() {{
        polyLayer.clearLayers();
        polyLayer.addData(geoData.polygons);
        updateHeatmaps();
    }}

    document.getElementById('spSelect').addEventListener('change', updateAll);

    L.control.layers(
        {{ "Satellite": esriSat, "Map": osmBase }},
        {{ "Weed Areas": polyLayer }},
        {{ collapsed: true, position: 'topright' }}
    ).addTo(map);

    updateHeatmaps();

    // Fit bounds from the raw point coordinates (no turf on this lighter page).
    let allCoords = [];
    Object.values(heatPointsBySpecies).forEach(arr => arr.forEach(p => allCoords.push([p[0], p[1]])));
    if (allCoords.length > 0) {{
        map.fitBounds(L.latLngBounds(allCoords), {{ padding: [30, 30] }});
    }} else {{
        map.fitBounds([[-47.0, 166.0], [-34.0, 179.0]]);
    }}

    // ---- Power-user lock button ----
    // The plaintext password is never embedded — only its SHA-256 hash. See
    // the module docstring for exactly what this does and doesn't protect.
    const POWER_PAGE_URL = {_js_json(power_page_path)};
    const POWER_PASSWORD_HASH = {_js_json(power_password_hash)};

    async function sha256Hex(text) {{
        const enc = new TextEncoder().encode(text);
        const digestBuffer = await crypto.subtle.digest('SHA-256', enc);
        return Array.from(new Uint8Array(digestBuffer))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('');
    }}

    document.getElementById('powerLockBtn').addEventListener('click', async () => {{
        const input = window.prompt('Power user password:');
        if (input === null) return; // cancelled

        const hash = await sha256Hex(input);
        if (hash === POWER_PASSWORD_HASH) {{
            window.location.href = POWER_PAGE_URL;
        }} else {{
            alert('Incorrect password.');
        }}
    }});
</script>
</body>
</html>
"""


# ==============================================================================
# STEP 3: GITHUB DEPLOYMENT
# ==============================================================================
def upload_to_github(html_content: str, file_path: str, commit_message: str, session: requests.Session) -> None:
    """Create or update a single file in the target GitHub repo."""
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not set — export it before running this script.")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError(f"GITHUB_REPO looks invalid: {GITHUB_REPO!r} (expected 'owner/repo').")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    sha = None
    existing = session.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=REQUEST_TIMEOUT)
    if existing.status_code == 200:
        sha = existing.json().get("sha")
    elif existing.status_code != 404:
        log.warning("Unexpected status checking for existing file %s: %s %s", file_path, existing.status_code, existing.text)

    payload = {
        "message": commit_message,
        "content": base64.b64encode(html_content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    # Bigger pages (value / vulnerability layers) take longer than a normal request to push.
    put_res = session.put(url, headers=headers, json=payload, timeout=max(REQUEST_TIMEOUT, 120))

    if put_res.status_code in (200, 201):
        owner, repo = GITHUB_REPO.split("/", 1)
        log.info("Success! %s updated: https://%s.github.io/%s/%s", file_path, owner, repo, "" if file_path == PUBLIC_FILE_PATH_IN_REPO else file_path)
    else:
        raise RuntimeError(f"GitHub upload failed for {file_path}: {put_res.status_code} - {put_res.text}")


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main() -> None:
    if not POWER_PAGE_PASSWORD:
        raise RuntimeError(
            "POWER_PAGE_PASSWORD is not set — export it before running this script "
            "(this is the password visitors enter on the public page's lock button)."
        )

    projects = load_project_config()
    geo = aggregate_data(projects)
    geo["priority"] = load_priority_segments()    # power-user page only
    geo["value"] = load_score_layer(VALUE_GEOJSON_PATH, "value", VALUE_FIELD)
    geo["vulnerability"] = load_score_layer(VULNERABILITY_GEOJSON_PATH, "vulnerability", VULNERABILITY_FIELD)
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    password_hash = hashlib.sha256(POWER_PAGE_PASSWORD.encode("utf-8")).hexdigest()

    public_html = build_public_html(geo, build_time, password_hash, POWER_FILE_PATH_IN_REPO)
    power_html = build_power_dashboard_html(geo, build_time)

    power_mb = len(power_html.encode("utf-8")) / 1_000_000
    log.info("Power page size: %.1f MB", power_mb)
    if power_mb > 90:
        raise RuntimeError(
            f"The power page is {power_mb:.0f} MB, over GitHub's ~100 MB file limit. Dissolve or "
            "simplify the value / vulnerability layers in ArcGIS Pro and re-export them."
        )

    log.info("--- 3/3: Pushing updates to GitHub ---")
    session = build_session()
    upload_to_github(public_html, PUBLIC_FILE_PATH_IN_REPO, "Update public weed map", session)
    upload_to_github(power_html, POWER_FILE_PATH_IN_REPO, "Update power-user weed dashboard", session)

    owner, repo = GITHUB_REPO.split("/", 1)
    log.info("Public map:      https://%s.github.io/%s/  (lock button gates the power view)", owner, repo)
    log.info("Power dashboard: https://%s.github.io/%s/%s  (unlisted — not a security boundary, see module docstring)", owner, repo, POWER_FILE_PATH_IN_REPO)


if __name__ == "__main__":
    main()