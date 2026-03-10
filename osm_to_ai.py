#!/usr/bin/env python3
"""
osm_to_ai.py - Convert OSM data to Adobe Illustrator-compatible SVG

Generates a layered SVG that Illustrator recognizes as proper layers:
  - Hillshade (optional)
  - Water / Waterways
  - Utilities (power lines & towers)
  - Roads (major, minor, railroad)
  - Trails (grouped by OSM route relation, colored by colour= tag)
  - Information (guideposts, map boards)
  - Amenities (drinking water, bicycle repair, toilets, parking)

Usage:
  python osm_to_ai.py --file input.osm --output map.svg
  python osm_to_ai.py --bbox "-122.45,37.75,-122.43,37.77" --output map.svg
  python osm_to_ai.py --overpass query.overpassql --output map.svg

Requirements:
  pip install requests
  pip install rasterio numpy Pillow   # only needed for --dem

Authors:
  Steve Vigneau <steve@nuxx.net>
  Claude (Anthropic) — https://claude.ai

License:
  MIT License — see LICENSE or the block comment below.
"""

# MIT License
#
# Copyright (c) 2026 Steve Vigneau
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import argparse
import base64
import io
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Coordinate projection (WGS84 -> Web Mercator / EPSG:3857)
# ---------------------------------------------------------------------------

def lon_to_mercator_x(lon):
    return lon * 20037508.34 / 180.0


def lat_to_mercator_y(lat):
    lat_rad = math.radians(lat)
    return math.log(math.tan(math.pi / 4 + lat_rad / 2)) * 20037508.34 / math.pi


def project(lon, lat):
    return lon_to_mercator_x(lon), lat_to_mercator_y(lat)


# ---------------------------------------------------------------------------
# OSM data model & parser
# ---------------------------------------------------------------------------

class OSMData:
    def __init__(self):
        self.nodes = {}      # id -> (lon, lat)
        self.node_tags = {}  # id -> {k: v}  (only nodes that carry tags)
        self.ways = {}       # id -> {'nodes': [ref, ...], 'tags': {k: v}}
        self.relations = {}  # id -> {'members': [...], 'tags': {k: v}}


def parse_osm(osm_string):
    root = ET.fromstring(osm_string)
    data = OSMData()

    for elem in root.findall('node'):
        nid = elem.get('id')
        lat = elem.get('lat')
        lon = elem.get('lon')
        if lat is not None and lon is not None:
            data.nodes[nid] = (float(lon), float(lat))
        tags = {t.get('k'): t.get('v') for t in elem.findall('tag')}
        if tags:
            data.node_tags[nid] = tags

    for elem in root.findall('way'):
        wid = elem.get('id')
        refs = [nd.get('ref') for nd in elem.findall('nd')]
        tags = {t.get('k'): t.get('v') for t in elem.findall('tag')}
        data.ways[wid] = {'nodes': refs, 'tags': tags}

    for elem in root.findall('relation'):
        rid = elem.get('id')
        members = [
            {'type': m.get('type'), 'ref': m.get('ref'), 'role': m.get('role', '')}
            for m in elem.findall('member')
        ]
        tags = {t.get('k'): t.get('v') for t in elem.findall('tag')}
        data.relations[rid] = {'members': members, 'tags': tags}

    return data


def compute_data_bbox(data):
    """Return (min_lon, min_lat, max_lon, max_lat) spanning all nodes."""
    lons = [lon for lon, lat in data.nodes.values()]
    lats = [lat for lon, lat in data.nodes.values()]
    if not lons:
        sys.exit("ERROR: No nodes found in OSM data.")
    return min(lons), min(lats), max(lons), max(lats)


# ---------------------------------------------------------------------------
# Overpass API
# ---------------------------------------------------------------------------

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

BBOX_QUERY_TEMPLATE = """\
[out:xml][timeout:60];
(
  way["highway"]({bb});
  way["waterway"]({bb});
  way["natural"="water"]({bb});
  way["natural"="wetland"]({bb});
  way["landuse"="reservoir"]({bb});
  way["landuse"="basin"]({bb});
  relation["type"="route"]["route"~"hiking|foot|bicycle|mtb|horse|trail"]({bb});
  relation["type"="multipolygon"]["natural"="water"]({bb});
  relation["waterway"]({bb});
  way["railway"="rail"]({bb});
  way["railway"="disused"]({bb});
  way["power"="line"]({bb});
  node["power"="tower"]({bb});
  node["tourism"="information"]({bb});
  node["amenity"="drinking_water"]({bb});
  node["amenity"="bicycle_repair_stand"]({bb});
  node["amenity"="toilets"]({bb});
  way["amenity"="toilets"]({bb});
  node["amenity"="parking"]({bb});
  way["amenity"="parking"]({bb});
);
(._;>;);
out body;
"""


def fetch_overpass(query, retries=4, backoff_seconds=(10, 30, 60, 120)):
    """POST to Overpass API with exponential-ish backoff on 429/504 errors."""
    if requests is None:
        sys.exit("ERROR: 'requests' library not installed. Run: pip install requests")
    for attempt in range(retries + 1):
        try:
            resp = requests.post(OVERPASS_URL, data={'data': query}, timeout=90)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (429, 504) and attempt < retries:
                wait = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                print(f"  Overpass {status} — retrying in {wait}s "
                      f"(attempt {attempt + 1}/{retries}) ...")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.Timeout:
            if attempt < retries:
                wait = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                print(f"  Overpass request timed out — retrying in {wait}s "
                      f"(attempt {attempt + 1}/{retries}) ...")
                time.sleep(wait)
            else:
                sys.exit("ERROR: Overpass API timed out after all retries. "
                         "Try a smaller bbox or run again later.")


def bbox_to_overpass(bbox_str):
    """Accepts 'min_lon,min_lat,max_lon,max_lat' (standard GIS order)."""
    parts = bbox_str.split(',')
    if len(parts) != 4:
        sys.exit("ERROR: --bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    min_lon, min_lat, max_lon, max_lat = [p.strip() for p in parts]
    # Overpass uses lat,lon order
    bb = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    return BBOX_QUERY_TEMPLATE.format(bb=bb)


# ---------------------------------------------------------------------------
# USGS 3DEP DEM download
# ---------------------------------------------------------------------------

USGS_3DEP_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services"
    "/3DEPElevation/ImageServer/exportImage"
)
DEFAULT_DEM_RES_M = 3    # default: ~3 m pixels (matches 1/9 arc-second source data)
MAX_DEM_PIXELS    = 4000  # USGS ImageServer hard limit is ~4096×4096


def fetch_usgs_dem(min_lon, min_lat, max_lon, max_lat, out_path, res_m=DEFAULT_DEM_RES_M):
    """
    Download a float32 GeoTIFF from the USGS 3DEP 1/3 arc-second (~10 m)
    elevation service for the given WGS84 bbox.  Saves to out_path and returns it.
    No API key required.
    """
    if requests is None:
        sys.exit("ERROR: 'requests' library not installed. Run: pip install requests")

    lat_mid      = (min_lat + max_lat) / 2
    metres_wide  = (max_lon - min_lon) * 111_320 * math.cos(math.radians(lat_mid))
    metres_tall  = (max_lat - min_lat) * 110_540

    px_w = max(64, min(MAX_DEM_PIXELS, int(metres_wide / res_m)))
    px_h = max(64, min(MAX_DEM_PIXELS, int(metres_tall / res_m)))

    params = {
        'bbox':                  f"{min_lon},{min_lat},{max_lon},{max_lat}",
        'bboxSR':                '4326',
        'size':                  f"{px_w},{px_h}",
        'imageSR':               '4326',
        'format':                'tiff',
        'pixelType':             'F32',
        'noDataInterpretation':  'esriNoDataMatchAny',
        'interpolation':         '+RSP_BilinearInterpolation',
        'f':                     'image',
    }

    print(f"  Downloading USGS 3DEP DEM ({px_w}×{px_h} px) ...")
    resp = requests.get(USGS_3DEP_URL, params=params, timeout=120)
    resp.raise_for_status()

    with open(out_path, 'wb') as fh:
        fh.write(resp.content)

    print(f"  DEM saved: {out_path}  ({len(resp.content) / 1024:.0f} KB)")
    return out_path


# ---------------------------------------------------------------------------
# Hillshade from a local DEM GeoTIFF (Option C)
# ---------------------------------------------------------------------------

def _compute_hillshade(elevation, res_x, res_y, azimuth=315, altitude=45, z_factor=1.0):
    """
    Compute an 8-bit hillshade array from a 2-D elevation array.
    Uses the standard cartographic formula (same as GDAL/QGIS defaults).
    res_x / res_y are the pixel sizes in the same units as the elevation values
    (metres for EPSG:3857).
    """
    import numpy as np

    zenith_rad   = math.radians(90.0 - altitude)
    # Convert geographic azimuth (CW from north) to math angle (CCW from east)
    az_math_rad  = math.radians(360.0 - azimuth + 90.0)

    dz_dx = np.gradient(elevation.astype(np.float64), res_x, axis=1)
    dz_dy = np.gradient(elevation.astype(np.float64), res_y, axis=0)

    slope_rad  = np.arctan(z_factor * np.sqrt(dz_dx**2 + dz_dy**2))
    aspect_rad = np.arctan2(-dz_dy, dz_dx)   # math convention (CCW from east)

    hs = (np.cos(zenith_rad) * np.cos(slope_rad) +
          np.sin(zenith_rad) * np.sin(slope_rad) * np.cos(az_math_rad - aspect_rad))

    return np.clip(hs, 0.0, 1.0)


def hillshade_from_dem(dem_path, min_lon, min_lat, max_lon, max_lat,
                       azimuth=315, altitude=45, z_factor=1.0):
    """
    Read a local DEM GeoTIFF (any CRS/projection), reproject to Web Mercator,
    crop to the bbox, compute hillshade, and return (base64_png, (width, height)).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.crs import CRS
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        from PIL import Image
    except ImportError as e:
        sys.exit(f"ERROR: Missing dependency — {e}\nRun: pip install rasterio numpy Pillow")

    dst_crs = CRS.from_epsg(3857)

    # Bbox corners in Web Mercator (metres)
    min_mx = lon_to_mercator_x(min_lon)
    max_mx = lon_to_mercator_x(max_lon)
    min_my = lat_to_mercator_y(min_lat)
    max_my = lat_to_mercator_y(max_lat)

    print(f"  Reading DEM: {dem_path}")
    with rasterio.open(dem_path) as src:
        print(f"  DEM CRS: {src.crs}  size: {src.width}x{src.height}  res: {src.res}")

        # Full reprojection transform to EPSG:3857
        dst_transform, dst_w, dst_h = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )

        # Allocate full reprojected array
        elev_full = np.empty((dst_h, dst_w), dtype=np.float32)
        nodata    = src.nodata if src.nodata is not None else -9999.0

        reproject(
            source=rasterio.band(src, 1),
            destination=elev_full,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
            dst_nodata=nodata,
        )

    # Pixel size in metres (EPSG:3857 units)
    res_x =  dst_transform.a   # positive, metres/pixel in X
    res_y = -dst_transform.e   # positive, metres/pixel in Y

    # Convert bbox (metres) to row/col offsets in the reprojected array
    origin_x = dst_transform.c   # left edge of reprojected raster
    origin_y = dst_transform.f   # top  edge of reprojected raster

    col_min = max(0,     int((min_mx - origin_x) / res_x))
    col_max = min(dst_w, int(math.ceil((max_mx - origin_x) / res_x)))
    row_min = max(0,     int((origin_y - max_my) / res_y))   # max_my = north = smaller row
    row_max = min(dst_h, int(math.ceil((origin_y - min_my) / res_y)))

    if col_min >= col_max or row_min >= row_max:
        sys.exit("ERROR: DEM does not overlap the map bbox.")

    elev_crop = elev_full[row_min:row_max, col_min:col_max].astype(np.float64)

    # Replace nodata with local mean so gradients don't blow up at edges
    nodata_mask = (elev_crop == nodata) | np.isnan(elev_crop)
    if nodata_mask.any():
        fill = float(np.nanmean(elev_crop[~nodata_mask])) if (~nodata_mask).any() else 0.0
        elev_crop[nodata_mask] = fill

    print(f"  Elevation range in bbox: {elev_crop.min():.1f} – {elev_crop.max():.1f} m")

    hs = _compute_hillshade(elev_crop, res_x, res_y, azimuth, altitude, z_factor)
    hs_uint8 = (hs * 255).astype(np.uint8)

    img = Image.fromarray(hs_uint8, mode='L').convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return b64, img.size


# ---------------------------------------------------------------------------
# Feature classification
# ---------------------------------------------------------------------------

MAJOR_ROAD_TYPES = {
    'motorway', 'trunk', 'primary', 'secondary',
    'motorway_link', 'trunk_link', 'primary_link', 'secondary_link',
}
MINOR_ROAD_TYPES = {
    'tertiary', 'unclassified', 'residential', 'service',
    'tertiary_link', 'living_street', 'road',
}
TRAIL_HIGHWAY_TYPES = {
    'path', 'footway', 'cycleway', 'bridleway', 'steps', 'track',
}

WATER_TAGS = {
    'waterway': {'river', 'stream', 'canal', 'drain', 'ditch', 'brook', 'tidal_channel'},
    'natural': {'water', 'wetland', 'bay'},
    'landuse': {'reservoir', 'basin'},
}


def classify_way(tags):
    hw = tags.get('highway')
    if hw in MAJOR_ROAD_TYPES:
        return 'major_road'
    if hw in MINOR_ROAD_TYPES:
        return 'minor_road'
    if hw in TRAIL_HIGHWAY_TYPES:
        return 'trail'
    railway = tags.get('railway')
    if railway == 'rail':
        return 'railway_rail'
    if railway == 'disused':
        return 'railway_disused'
    if tags.get('power') == 'line':
        return 'power_line'
    for key, values in WATER_TAGS.items():
        if tags.get(key) in values:
            return 'water'
    return None


def is_closed_way(way):
    nodes = way['nodes']
    return len(nodes) >= 4 and nodes[0] == nodes[-1]


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

def safe_id(text):
    """Make a string safe for use as an SVG/XML id attribute."""
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(text)).strip('_')


def xml_escape(text):
    return (str(text)
            .replace('&', '&amp;')
            .replace('"', '&quot;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


class SVGCanvas:
    def __init__(self, min_x, min_y, max_x, max_y, target_width=800):
        self.min_x = min_x
        self.min_y = min_y
        data_w = max_x - min_x
        data_h = max_y - min_y
        if data_w == 0 or data_h == 0:
            sys.exit("ERROR: Bounding box has zero width or height.")
        self.scale = target_width / data_w
        self.svg_width = target_width
        self.svg_height = data_h * self.scale

    def to_svg(self, mx, my):
        """Convert Web Mercator coords to SVG pixel coords (Y-flipped)."""
        sx = (mx - self.min_x) * self.scale
        sy = self.svg_height - (my - self.min_y) * self.scale
        return sx, sy

    def way_to_path_d(self, way, nodes):
        coords = []
        for ref in way['nodes']:
            if ref in nodes:
                mx, my = project(*nodes[ref])
                coords.append(self.to_svg(mx, my))
        if len(coords) < 2:
            return None
        d = f"M {coords[0][0]:.2f},{coords[0][1]:.2f}"
        for x, y in coords[1:]:
            d += f" L {x:.2f},{y:.2f}"
        if is_closed_way(way):
            d += " Z"
        return d


# ---------------------------------------------------------------------------
# SVG layer/group output
# ---------------------------------------------------------------------------

def open_group(lines, layer_id, layer_name=None, top_level=False):
    indent = '  ' if top_level else '    '
    extra  = ' i:dimmedPercent="50"' if top_level else ''
    label  = xml_escape(layer_name or layer_id)
    lines.append(f'{indent}<g i:layer="yes"{extra} id="{safe_id(layer_id)}" data-name="{label}">')

def close_group(lines, top_level=False):
    lines.append('  </g>' if top_level else '    </g>')


def path_element(canvas, wid, way, nodes, style, indent='      '):
    d = canvas.way_to_path_d(way, nodes)
    if d is None:
        return None
    name = xml_escape(way['tags'].get('name', ''))
    title_attr = f' data-name="{name}"' if name else ''
    return f'{indent}<path id="way_{wid}"{title_attr} style="{style}" d="{d}"/>'


# ---------------------------------------------------------------------------
# Default styles
# ---------------------------------------------------------------------------

STYLES = {
    'water_area':       'fill:#a8d4e6;stroke:#3a7ea8;stroke-width:0.5px;',
    'waterway':         'fill:none;stroke:#3a7ea8;stroke-width:1px;',
    'major_road':       'fill:none;stroke:#e8a040;stroke-width:2px;',
    'minor_road':       'fill:none;stroke:#cccccc;stroke-width:1.5px;',
    'railway_rail':     'fill:none;stroke:#444444;stroke-width:1.5px;',
    'railway_disused':  'fill:none;stroke:#aaaaaa;stroke-width:1px;stroke-dasharray:4,2;',
    'power_line':       'fill:none;stroke:#9e9e9e;stroke-width:0.5px;',
}

TRAIL_DEFAULT_COLOR = '#808080'  # no colour tag - 50% grey

# ---------------------------------------------------------------------------
# Colour normalisation (OSM colour= tag → valid SVG colour string)
# ---------------------------------------------------------------------------

_HEX_RE  = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')
_RGB_RE  = re.compile(r'^rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)$', re.I)

# Full CSS3 named colour set (lowercase, no spaces)
_CSS_COLOR_NAMES = {
    'aliceblue', 'antiquewhite', 'aqua', 'aquamarine', 'azure', 'beige',
    'bisque', 'black', 'blanchedalmond', 'blue', 'blueviolet', 'brown',
    'burlywood', 'cadetblue', 'chartreuse', 'chocolate', 'coral',
    'cornflowerblue', 'cornsilk', 'crimson', 'cyan', 'darkblue',
    'darkcyan', 'darkgoldenrod', 'darkgray', 'darkgreen', 'darkgrey',
    'darkkhaki', 'darkmagenta', 'darkolivegreen', 'darkorange',
    'darkorchid', 'darkred', 'darksalmon', 'darkseagreen', 'darkslateblue',
    'darkslategray', 'darkslategrey', 'darkturquoise', 'darkviolet',
    'deeppink', 'deepskyblue', 'dimgray', 'dimgrey', 'dodgerblue',
    'firebrick', 'floralwhite', 'forestgreen', 'fuchsia', 'gainsboro',
    'ghostwhite', 'gold', 'goldenrod', 'gray', 'green', 'greenyellow',
    'grey', 'honeydew', 'hotpink', 'indianred', 'indigo', 'ivory',
    'khaki', 'lavender', 'lavenderblush', 'lawngreen', 'lemonchiffon',
    'lightblue', 'lightcoral', 'lightcyan', 'lightgoldenrodyellow',
    'lightgray', 'lightgreen', 'lightgrey', 'lightpink', 'lightsalmon',
    'lightseagreen', 'lightskyblue', 'lightslategray', 'lightslategrey',
    'lightsteelblue', 'lightyellow', 'lime', 'limegreen', 'linen',
    'magenta', 'maroon', 'mediumaquamarine', 'mediumblue', 'mediumorchid',
    'mediumpurple', 'mediumseagreen', 'mediumslateblue', 'mediumspringgreen',
    'mediumturquoise', 'mediumvioletred', 'midnightblue', 'mintcream',
    'mistyrose', 'moccasin', 'navajowhite', 'navy', 'oldlace', 'olive',
    'olivedrab', 'orange', 'orangered', 'orchid', 'palegoldenrod',
    'palegreen', 'paleturquoise', 'palevioletred', 'papayawhip',
    'peachpuff', 'peru', 'pink', 'plum', 'powderblue', 'purple',
    'rebeccapurple', 'red', 'rosybrown', 'royalblue', 'saddlebrown',
    'salmon', 'sandybrown', 'seagreen', 'seashell', 'sienna', 'silver',
    'skyblue', 'slateblue', 'slategray', 'slategrey', 'snow',
    'springgreen', 'steelblue', 'tan', 'teal', 'thistle', 'tomato',
    'turquoise', 'violet', 'wheat', 'white', 'whitesmoke', 'yellow',
    'yellowgreen',
}


def normalize_color(value):
    """Return a valid SVG colour string from an OSM colour= tag value, or None.

    Accepts hex (#rgb / #rrggbb), rgb(...), and CSS named colours.
    Multi-word names like 'dark green' are collapsed to 'darkgreen'.
    Returns None if the value cannot be recognised.
    """
    if not value:
        return None
    v = value.strip()
    if _HEX_RE.match(v):
        return v.lower()
    if _RGB_RE.match(v):
        return v.lower()
    # Normalise name: lowercase, remove spaces/hyphens/underscores
    name = re.sub(r'[\s\-_]+', '', v.lower())
    if name in _CSS_COLOR_NAMES:
        return name
    return None


def trail_style(color=None):
    """Return a solid stroke style for a trail, using the relation's colour= tag."""
    stroke = color if color else TRAIL_DEFAULT_COLOR
    return f'fill:none;stroke:{stroke};stroke-width:1px;'


# Information node colours
_INFO_BROWN = '#795548'   # guidepost marker
_INFO_BLUE  = '#1565C0'   # map board marker
_INFO_TEXT  = '#212121'


def info_node_elements(canvas, nid, lon_lat, tags, indent='      '):
    """Return a list of SVG element strings for a tourism=information node."""
    lon, lat = lon_lat
    mx, my = project(lon, lat)
    sx, sy = canvas.to_svg(mx, my)
    info_type = tags.get('information', '')
    elems = []

    if info_type == 'guidepost':
        label = xml_escape(tags.get('name') or tags.get('ref') or '')
        # Upward-pointing triangle marker
        pts = f"{sx:.2f},{sy - 6:.2f} {sx - 4:.2f},{sy + 3:.2f} {sx + 4:.2f},{sy + 3:.2f}"
        elems.append(
            f'{indent}<polygon id="info_{safe_id(nid)}_marker"'
            f' points="{pts}"'
            f' style="fill:{_INFO_BROWN};stroke:none;"/>'
        )
        if label:
            elems.append(
                f'{indent}<text id="info_{safe_id(nid)}_label"'
                f' x="{sx + 7:.2f}" y="{sy + 3:.2f}"'
                f' font-family="sans-serif" font-size="8"'
                f' fill="{_INFO_TEXT}">{label}</text>'
            )

    elif info_type == 'map':
        # Small rectangle representing a map board, with an "i" glyph
        rx, ry = sx - 4, sy - 6
        elems.append(
            f'{indent}<rect id="info_{safe_id(nid)}_board"'
            f' x="{rx:.2f}" y="{ry:.2f}" width="8" height="11" rx="1"'
            f' style="fill:{_INFO_BLUE};stroke:none;"/>'
        )
        elems.append(
            f'{indent}<text id="info_{safe_id(nid)}_label"'
            f' x="{sx:.2f}" y="{sy + 3:.2f}"'
            f' font-family="sans-serif" font-size="8" font-weight="bold"'
            f' fill="white" text-anchor="middle">i</text>'
        )

    return elems


# Amenity node colours
_AMENITY_WATER_COLOR  = '#0277BD'  # drinking water — blue
_AMENITY_BIKE_COLOR   = '#2E7D32'  # bicycle repair — green
_AMENITY_TOILET_COLOR = '#6A1B9A'  # toilets — purple
_AMENITY_PARK_COLOR   = '#1565C0'  # parking — dark blue

# (amenity_tag, human_label, area_fill_style_or_None)
AMENITY_CONFIG = [
    ('drinking_water',       'Drinking Water', None),
    ('bicycle_repair_stand', 'Bicycle Repair', None),
    ('toilets', 'Toilets', 'fill:#EDE7F6;stroke:#6A1B9A;stroke-width:0.5px;'),
    ('parking', 'Parking', 'fill:#ECEFF1;stroke:#90A4AE;stroke-width:0.5px;'),
]
AMENITY_LABELS      = {tag: label for tag, label, _ in AMENITY_CONFIG}
AMENITY_AREA_STYLES = {tag: style for tag, _, style in AMENITY_CONFIG if style}


def amenity_node_elements(canvas, nid, lon_lat, amenity_type, indent='      '):
    """Return SVG element strings for an amenity node icon."""
    lon, lat = lon_lat
    sx, sy = canvas.to_svg(*project(lon, lat))
    pid = safe_id(nid)
    elems = []

    if amenity_type == 'drinking_water':
        # Teardrop: pointed top, rounded bottom
        d = (f"M {sx:.2f},{sy - 7:.2f} "
             f"Q {sx + 5:.2f},{sy - 1:.2f} {sx:.2f},{sy + 4:.2f} "
             f"Q {sx - 5:.2f},{sy - 1:.2f} {sx:.2f},{sy - 7:.2f} Z")
        elems.append(
            f'{indent}<path id="amenity_{pid}"'
            f' d="{d}" style="fill:{_AMENITY_WATER_COLOR};stroke:none;"/>'
        )

    elif amenity_type == 'bicycle_repair_stand':
        # Two small wheels + simple triangle frame
        lx, rx, wy = sx - 4.5, sx + 4.5, sy + 2.5   # wheel centres
        elems += [
            f'{indent}<circle id="amenity_{pid}_wl"'
            f' cx="{lx:.2f}" cy="{wy:.2f}" r="3"'
            f' style="fill:none;stroke:{_AMENITY_BIKE_COLOR};stroke-width:1.5;"/>',
            f'{indent}<circle id="amenity_{pid}_wr"'
            f' cx="{rx:.2f}" cy="{wy:.2f}" r="3"'
            f' style="fill:none;stroke:{_AMENITY_BIKE_COLOR};stroke-width:1.5;"/>',
            # Frame: seat post top → rear wheel → front wheel → seat post top
            f'{indent}<polyline id="amenity_{pid}_frame"'
            f' points="{sx:.2f},{sy - 4:.2f} {lx:.2f},{wy:.2f}'
            f' {rx:.2f},{wy:.2f} {sx:.2f},{sy - 4:.2f}"'
            f' style="fill:none;stroke:{_AMENITY_BIKE_COLOR};stroke-width:1.5;"/>',
            # Saddle dot
            f'{indent}<circle id="amenity_{pid}_saddle"'
            f' cx="{sx:.2f}" cy="{sy - 4:.2f}" r="1.5"'
            f' style="fill:{_AMENITY_BIKE_COLOR};stroke:none;"/>',
        ]

    elif amenity_type == 'toilets':
        # Purple rounded rect with white "WC"
        elems += [
            f'{indent}<rect id="amenity_{pid}_bg"'
            f' x="{sx - 7:.2f}" y="{sy - 6:.2f}" width="14" height="10" rx="2"'
            f' style="fill:{_AMENITY_TOILET_COLOR};stroke:none;"/>',
            f'{indent}<text id="amenity_{pid}_label"'
            f' x="{sx:.2f}" y="{sy + 2:.2f}"'
            f' font-family="sans-serif" font-size="7" font-weight="bold"'
            f' fill="white" text-anchor="middle">WC</text>',
        ]

    elif amenity_type == 'parking':
        # Dark-blue square with white "P"
        elems += [
            f'{indent}<rect id="amenity_{pid}_bg"'
            f' x="{sx - 5:.2f}" y="{sy - 6:.2f}" width="10" height="11" rx="1"'
            f' style="fill:{_AMENITY_PARK_COLOR};stroke:none;"/>',
            f'{indent}<text id="amenity_{pid}_label"'
            f' x="{sx:.2f}" y="{sy + 3:.2f}"'
            f' font-family="sans-serif" font-size="9" font-weight="bold"'
            f' fill="white" text-anchor="middle">P</text>',
        ]

    return elems


def power_tower_elements(canvas, nid, lon_lat, indent='      '):
    """Return a single SVG path for a power=tower node (simplified pylon icon)."""
    lon, lat = lon_lat
    sx, sy = canvas.to_svg(*project(lon, lat))
    pid = safe_id(nid)
    # Subpath 1: left-leg bottom → apex → right-leg bottom (the inverted V)
    # Subpath 2: left end of crossarm → right end
    d = (f"M {sx - 4:.2f},{sy + 4:.2f} L {sx:.2f},{sy - 6:.2f} L {sx + 4:.2f},{sy + 4:.2f}"
         f" M {sx - 5:.2f},{sy - 2:.2f} L {sx + 5:.2f},{sy - 2:.2f}")
    return [
        f'{indent}<path id="tower_{pid}"'
        f' d="{d}"'
        f' style="fill:none;stroke:#9e9e9e;stroke-width:1;"/>',
    ]


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_svg(data, output_path, target_width=800,
              dem_path=None, sun_azimuth=315, sun_altitude=45,
              clip_bbox=None):
    # --- Compute bounding box ---
    # If the caller supplied an explicit clip bbox, use it for the canvas and
    # hillshade extents.  Otherwise fall back to the extent of all OSM nodes.
    if clip_bbox:
        min_lon, min_lat, max_lon, max_lat = clip_bbox
    else:
        min_lon, min_lat, max_lon, max_lat = compute_data_bbox(data)

    min_mx = lon_to_mercator_x(min_lon)
    max_mx = lon_to_mercator_x(max_lon)
    min_my = lat_to_mercator_y(min_lat)
    max_my = lat_to_mercator_y(max_lat)

    canvas = SVGCanvas(min_mx, min_my, max_mx, max_my, target_width)

    # --- Classify ways ---
    _WAY_CLASSES = ('major_road', 'minor_road', 'trail', 'water',
                    'railway_rail', 'railway_disused', 'power_line')
    way_buckets = {k: {} for k in _WAY_CLASSES}
    for wid, way in data.ways.items():
        cls = classify_way(way['tags'])
        if cls in way_buckets:
            way_buckets[cls][wid] = way
    major_roads      = way_buckets['major_road']
    minor_roads      = way_buckets['minor_road']
    trail_ways       = way_buckets['trail']
    water_ways       = way_buckets['water']
    railway_rail     = way_buckets['railway_rail']
    railway_disused  = way_buckets['railway_disused']
    power_lines      = way_buckets['power_line']

    # Separate water ways into areas vs lines
    water_areas = {wid: w for wid, w in water_ways.items() if is_closed_way(w)}
    water_lines  = {wid: w for wid, w in water_ways.items() if not is_closed_way(w)}

    # --- Group trails by relation ---
    # Each relation that contains trail ways becomes a named sublayer.
    trail_relations = {}   # name -> {'rid': ..., 'ways': [wid, ...]}
    way_to_rel_name = {}   # wid -> relation name (first assigned wins)

    for rid, rel in data.relations.items():
        tags = rel['tags']
        rel_type = tags.get('type', '')
        route    = tags.get('route', '')
        name     = tags.get('name', '').strip() or f"Relation {rid}"

        # Include route relations AND any relation that contains trail ways
        is_route_rel = rel_type == 'route' and route in {
            'hiking', 'foot', 'bicycle', 'mtb', 'horse', 'trail'
        }
        member_trail_ways = [
            m['ref'] for m in rel['members']
            if m['type'] == 'way' and m['ref'] in trail_ways
        ]
        if not is_route_rel and not member_trail_ways:
            continue

        # Deduplicate relation names (append id if collision)
        unique_name = name
        if unique_name in trail_relations and trail_relations[unique_name]['rid'] != rid:
            unique_name = f"{name} ({rid})"

        if unique_name not in trail_relations:
            raw_color = tags.get('colour') or tags.get('color') or None
            color = normalize_color(raw_color)
            trail_relations[unique_name] = {'rid': rid, 'ways': [], 'color': color}

        for wid in member_trail_ways:
            trail_relations[unique_name]['ways'].append(wid)
            if wid not in way_to_rel_name:
                way_to_rel_name[wid] = unique_name

    # Deduplicate way lists within each relation (a way can appear in multiple members)
    for entry in trail_relations.values():
        seen = set()
        entry['ways'] = [w for w in entry['ways'] if not (w in seen or seen.add(w))]

    # Ways not assigned to any relation
    unnamed_trails = [wid for wid in trail_ways if wid not in way_to_rel_name]

    # --- Water relations (multipolygons) ---
    water_relations = {}  # name -> [wid, ...]
    for rid, rel in data.relations.items():
        tags = rel['tags']
        is_water_rel = (
            tags.get('natural') == 'water' or
            (tags.get('type') == 'multipolygon' and tags.get('natural') == 'water') or
            tags.get('waterway') in {'river', 'stream', 'canal'}
        )
        if not is_water_rel:
            continue
        name = tags.get('name', '').strip() or f"Water {rid}"
        member_ways = [
            m['ref'] for m in rel['members']
            if m['type'] == 'way' and m['ref'] in data.ways
        ]
        if member_ways:
            water_relations[name] = {'rid': rid, 'ways': member_ways}

    # --- Collect amenity closed ways (toilets and parking are often mapped as areas) ---
    amenity_ways = {tag: {} for tag, _, style in AMENITY_CONFIG if style}  # type -> {wid -> way}

    # --- Collect tagged nodes in a single pass ---
    _amenity_types = tuple(tag for tag, _, __ in AMENITY_CONFIG)
    info_guideposts = {}
    info_maps       = {}
    amenity_nodes   = {tag: {} for tag, _, __ in AMENITY_CONFIG}
    power_towers    = {}

    for nid, tags in data.node_tags.items():
        if nid not in data.nodes:
            continue
        tourism = tags.get('tourism')
        amenity = tags.get('amenity')
        power   = tags.get('power')
        if tourism == 'information':
            info_type = tags.get('information', '')
            if info_type == 'guidepost' and (tags.get('name') or tags.get('ref')):
                info_guideposts[nid] = tags
            elif info_type == 'map':
                info_maps[nid] = tags
        elif amenity and amenity in amenity_nodes:
            amenity_nodes[amenity][nid] = tags
        elif power == 'tower':
            power_towers[nid] = tags

    for wid, way in data.ways.items():
        amenity = way['tags'].get('amenity', '')
        if amenity in amenity_ways and is_closed_way(way):
            amenity_ways[amenity][wid] = way

    # --- Print summary ---
    print(f"  Ways classified: {len(major_roads)} major road, {len(minor_roads)} minor road, "
          f"{len(trail_ways)} trail, {len(water_ways)} water, "
          f"{len(railway_rail)} rail, {len(railway_disused)} disused rail, "
          f"{len(power_lines)} power line")
    print(f"  Trail relations: {len(trail_relations)} named + {len(unnamed_trails)} unnamed ways")
    print(f"  Water relations: {len(water_relations)}")
    print(f"  Information nodes: {len(info_guideposts)} guidepost, {len(info_maps)} map")
    amenity_counts = ', '.join(f"{len(v)} {k}" for k, v in amenity_nodes.items() if v)
    amenity_way_counts = ', '.join(f"{len(v)} {k}" for k, v in amenity_ways.items() if v)
    if amenity_counts:
        print(f"  Amenity nodes: {amenity_counts}")
    if amenity_way_counts:
        print(f"  Amenity areas: {amenity_way_counts}")

    # --- Build SVG lines ---
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(
        f'<svg version="1.1"\n'
        f'     xmlns="http://www.w3.org/2000/svg"\n'
        f'     xmlns:xlink="http://www.w3.org/1999/xlink"\n'
        f'     xmlns:i="http://ns.adobe.com/AdobeIllustrator/10.0/"\n'
        f'     xmlns:x="http://ns.adobe.com/Extensibility/1.0/"\n'
        f'     x="0px" y="0px"\n'
        f'     width="{canvas.svg_width:.2f}px" height="{canvas.svg_height:.2f}px"\n'
        f'     viewBox="0 0 {canvas.svg_width:.2f} {canvas.svg_height:.2f}"\n'
        f'     xml:space="preserve">'
    )
    lines.append('<defs/>')

    def emit_way(wid, way, style, indent='      '):
        elem = path_element(canvas, wid, way, data.nodes, style, indent)
        if elem:
            lines.append(elem)

    # ---- HILLSHADE LAYER (bottom) ----
    if dem_path:
        print("Computing hillshade from DEM...")
        hs_b64, (hs_px_w, hs_px_h) = hillshade_from_dem(
            dem_path, min_lon, min_lat, max_lon, max_lat,
            azimuth=sun_azimuth, altitude=sun_altitude,
        )
        print(f"  Hillshade image: {hs_px_w} x {hs_px_h} px")
        open_group(lines, 'Hillshade', 'Hillshade', top_level=True)
        lines.append(
            f'    <image x="0" y="0"'
            f' width="{canvas.svg_width:.2f}" height="{canvas.svg_height:.2f}"'
            f' preserveAspectRatio="none"'
            f' xlink:href="data:image/png;base64,{hs_b64}"/>'
        )
        close_group(lines, top_level=True)

    # ---- WATER LAYER ----
    open_group(lines, 'Water', 'Water', top_level=True)

    if water_areas:
        open_group(lines, 'Water_Areas', 'Water Areas')
        for wid, way in water_areas.items():
            emit_way(wid, way, STYLES['water_area'])
        close_group(lines)

    if water_lines:
        open_group(lines, 'Waterways', 'Waterways')
        for wid, way in water_lines.items():
            emit_way(wid, way, STYLES['waterway'])
        close_group(lines)

    for rel_name, entry in sorted(water_relations.items()):
        sublayer_id = f"Water_{safe_id(rel_name)}"
        open_group(lines, sublayer_id, rel_name)
        for wid in entry['ways']:
            way = data.ways[wid]
            style = STYLES['water_area'] if is_closed_way(way) else STYLES['waterway']
            emit_way(wid, way, style)
        close_group(lines)

    close_group(lines, top_level=True)

    # ---- UTILITIES LAYER (power lines, below roads) ----
    if power_lines or power_towers:
        open_group(lines, 'Utilities', 'Utilities', top_level=True)

        if power_lines:
            open_group(lines, 'Power_Lines', 'Power Lines')
            for wid, way in power_lines.items():
                emit_way(wid, way, STYLES['power_line'])
            close_group(lines)

        if power_towers:
            open_group(lines, 'Power_Towers', 'Power Towers')
            for nid in power_towers:
                for elem in power_tower_elements(canvas, nid, data.nodes[nid]):
                    lines.append(elem)
            close_group(lines)

        close_group(lines, top_level=True)

    # ---- ROADS LAYER ----
    open_group(lines, 'Roads', 'Roads', top_level=True)

    if major_roads:
        open_group(lines, 'Major_Roads', 'Major Roads')
        for wid, way in major_roads.items():
            emit_way(wid, way, STYLES['major_road'])
        close_group(lines)

    if minor_roads:
        open_group(lines, 'Minor_Roads', 'Minor Roads')
        for wid, way in minor_roads.items():
            emit_way(wid, way, STYLES['minor_road'])
        close_group(lines)

    if railway_rail:
        open_group(lines, 'Railroad_Tracks', 'Railroad Tracks')
        for wid, way in railway_rail.items():
            emit_way(wid, way, STYLES['railway_rail'])
        close_group(lines)

    if railway_disused:
        open_group(lines, 'Disused_Railroad', 'Disused Railroad')
        for wid, way in railway_disused.items():
            emit_way(wid, way, STYLES['railway_disused'])
        close_group(lines)

    close_group(lines, top_level=True)

    # ---- TRAILS LAYER ----
    open_group(lines, 'Trails', 'Trails', top_level=True)

    for trail_name, entry in sorted(trail_relations.items()):
        sublayer_id = f"Trail_{safe_id(trail_name)}"
        open_group(lines, sublayer_id, trail_name)
        style = trail_style(entry['color'])
        for wid in entry['ways']:
            if wid in trail_ways:
                emit_way(wid, trail_ways[wid], style)
        close_group(lines)

    if unnamed_trails:
        open_group(lines, 'Unnamed_Paths', 'Unnamed Paths')
        for wid in unnamed_trails:
            emit_way(wid, trail_ways[wid], trail_style())
        close_group(lines)

    close_group(lines, top_level=True)

    # ---- INFORMATION LAYER (top) ----
    if info_guideposts or info_maps:
        open_group(lines, 'Information', 'Information', top_level=True)

        if info_guideposts:
            open_group(lines, 'Guideposts', 'Guideposts')
            for nid, tags in info_guideposts.items():
                for elem in info_node_elements(canvas, nid, data.nodes[nid], tags):
                    lines.append(elem)
            close_group(lines)

        if info_maps:
            open_group(lines, 'Map_Boards', 'Map Boards')
            for nid, tags in info_maps.items():
                for elem in info_node_elements(canvas, nid, data.nodes[nid], tags):
                    lines.append(elem)
            close_group(lines)

        close_group(lines, top_level=True)

    # ---- AMENITIES LAYER (top) ----
    if any(amenity_nodes.values()) or any(amenity_ways.values()):
        open_group(lines, 'Amenities', 'Amenities', top_level=True)
        for amenity_type in _amenity_types:
            nodes = amenity_nodes[amenity_type]
            ways  = amenity_ways.get(amenity_type, {})
            if not nodes and not ways:
                continue
            sublayer_id = f"Amenity_{safe_id(amenity_type)}"
            open_group(lines, sublayer_id, AMENITY_LABELS[amenity_type])
            # Render closed-way areas first so node icons sit on top
            area_style = AMENITY_AREA_STYLES.get(amenity_type)
            if area_style:
                for wid, way in ways.items():
                    emit_way(wid, way, area_style)
            for nid, tags in nodes.items():
                for elem in amenity_node_elements(canvas, nid, data.nodes[nid], amenity_type):
                    lines.append(elem)
            close_group(lines)
        close_group(lines, top_level=True)

    lines.append('</svg>')

    # --- Write output ---
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"Saved: {output_path}")
    print(f"  Canvas: {canvas.svg_width:.0f} x {canvas.svg_height:.0f} px")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert OSM data to an Adobe Illustrator-compatible layered SVG.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python osm_to_ai.py --file mypark.osm --output mypark.svg
  python osm_to_ai.py --bbox "-71.12,42.36,-71.10,42.38" --output mypark.svg
  python osm_to_ai.py --overpass query.overpassql --output mypark.svg
  python osm_to_ai.py --file mypark.osm --dem elevation.tif --output mypark.svg
  python osm_to_ai.py --file mypark.osm --fetch-dem --output mypark.svg
  python osm_to_ai.py --file mypark.osm --fetch-dem --sun-azimuth 270 --sun-altitude 35 --output mypark.svg
        """
    )

    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument('--file',      metavar='PATH',  help='.osm file to read')
    src.add_argument('--bbox',      metavar='BBOX',  help='Bounding box: min_lon,min_lat,max_lon,max_lat')
    src.add_argument('--overpass',  metavar='FILE',  help='File containing an Overpass QL query')

    parser.add_argument('--output', metavar='PATH', required=True, help='Output .svg file')
    parser.add_argument('--width',  metavar='PX',   type=int, default=800,
                        help='SVG width in pixels (height is auto-calculated, default: 800)')
    parser.add_argument('--dem',          metavar='PATH',    default=None,
                        help='GeoTIFF DEM file to generate a hillshade layer (any CRS)')
    parser.add_argument('--fetch-dem',      action='store_true',
                        help='Download a USGS 3DEP DEM automatically and use it for hillshade. '
                             'Saves a sidecar .tif next to --output for reuse.')
    parser.add_argument('--dem-resolution', metavar='METERS', type=float, default=DEFAULT_DEM_RES_M,
                        help=f'Target DEM pixel size in metres for --fetch-dem '
                             f'(default: {DEFAULT_DEM_RES_M}). '
                             f'Use 1 for lidar-quality where available, 3 for 1/9 arc-second, '
                             f'10 for 1/3 arc-second.')
    parser.add_argument('--sun-azimuth',  metavar='DEGREES', type=float, default=315,
                        help='Sun azimuth in degrees clockwise from north (default: 315 = NW)')
    parser.add_argument('--sun-altitude', metavar='DEGREES', type=float, default=45,
                        help='Sun altitude above horizon in degrees (default: 45)')
    parser.add_argument('--save-osm', metavar='PATH', default=None,
                        help='Save the downloaded OSM XML to a file for later reuse with --file')

    args = parser.parse_args()

    # --- Load OSM data ---
    # Also capture a user-supplied bbox (used later to scope DEM downloads)
    user_bbox = None   # (min_lon, min_lat, max_lon, max_lat) float tuple, or None

    if args.file:
        print(f"Reading {args.file}...")
        with open(args.file, 'r', encoding='utf-8') as f:
            osm_string = f.read()

    elif args.bbox:
        parts = [float(p.strip()) for p in args.bbox.split(',')]
        user_bbox = tuple(parts)   # (min_lon, min_lat, max_lon, max_lat)
        print(f"Fetching OSM data for bbox {args.bbox} ...")
        query = bbox_to_overpass(args.bbox)
        osm_string = fetch_overpass(query)

    elif args.overpass:
        print(f"Reading Overpass query from {args.overpass}...")
        with open(args.overpass, 'r', encoding='utf-8') as f:
            query = f.read()
        print("Fetching from Overpass API...")
        osm_string = fetch_overpass(query)

    # --- Optionally save downloaded OSM XML ---
    if args.save_osm and not args.file:
        with open(args.save_osm, 'w', encoding='utf-8') as f:
            f.write(osm_string)
        print(f"OSM data saved to {args.save_osm}")

    # --- Parse ---
    print("Parsing OSM data...")
    data = parse_osm(osm_string)
    print(f"  Nodes: {len(data.nodes):,}  Ways: {len(data.ways):,}  Relations: {len(data.relations):,}")

    # --- Resolve DEM path ---
    dem_path = args.dem
    if args.fetch_dem and not dem_path:
        import os
        stem = os.path.splitext(args.output)[0]
        dem_path = stem + '_dem.tif'
        if os.path.exists(dem_path):
            print(f"Reusing cached DEM: {dem_path}")
        else:
            print("Downloading DEM from USGS 3DEP...")
            if user_bbox:
                dem_min_lon, dem_min_lat, dem_max_lon, dem_max_lat = user_bbox
            else:
                dem_min_lon, dem_min_lat, dem_max_lon, dem_max_lat = compute_data_bbox(data)
            fetch_usgs_dem(dem_min_lon, dem_min_lat, dem_max_lon, dem_max_lat,
                           dem_path, res_m=args.dem_resolution)

    # --- Build SVG ---
    print("Building SVG...")
    build_svg(data, args.output, target_width=args.width,
              dem_path=dem_path, sun_azimuth=args.sun_azimuth, sun_altitude=args.sun_altitude,
              clip_bbox=user_bbox)


if __name__ == '__main__':
    main()
