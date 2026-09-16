"""Local Leaflet preview for smoke-test tiles."""

from __future__ import annotations

import json
from pathlib import Path

from mpwg_radar.config import CookerConfig
from mpwg_radar.geo import CENTRAL_TEXAS

_PREVIEW = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MPWG radar smoke preview</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body, #map { height: 100%; margin: 0; background: #0b1320; }
    .legend {
      position: absolute; z-index: 500; right: 12px; bottom: 24px;
      background: rgba(10,16,28,.88); color: #e8eef7; padding: 8px 10px;
      font: 12px/1.4 system-ui, sans-serif; border-radius: 8px;
    }
    .legend img { display: block; width: 220px; height: auto; margin-top: 4px; }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="legend">
    MPWG Clean · 512px XYZ · NOAA MRMS · &lt;15 dBZ transparent<br/>
    <img src="radar/colorbar.png" alt="dBZ colorbar"/>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const bbox = __BBOX__;
    const minZoom = __MIN_ZOOM__;
    const maxZoom = __MAX_ZOOM__;
    const template = __TEMPLATE__;
    const map = L.map('map', { zoomSnap: 1 });
    map.fitBounds([[bbox.south, bbox.west], [bbox.north, bbox.east]]);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap'
    }).addTo(map);
    // Leaflet CRS is 256px; PNGs are 512px covering the same OSM XYZ extent,
    // so Leaflet scales them into 256 CSS pixels (sharp on retina). MapLibre
    // clients should set tileSize: 512 instead — see README.
    L.tileLayer(template, {
      tileSize: 256,
      minZoom: minZoom,
      maxZoom: maxZoom,
      maxNativeZoom: maxZoom,
      opacity: 0.85,
      attribution: 'NOAA MRMS · MPWG Clean'
    }).addTo(map);
  </script>
</body>
</html>
"""


def write_preview(radar_root: Path, cfg: CookerConfig) -> Path:
    """Write preview.html next to the radar output directory's parent."""
    smoke_root = radar_root.parent
    template = "radar/clean/latest/{z}/{x}/{y}.png"
    html = (
        _PREVIEW.replace("__BBOX__", json.dumps(cfg.bbox.as_dict()))
        .replace("__MIN_ZOOM__", str(cfg.min_zoom))
        .replace("__MAX_ZOOM__", str(cfg.max_zoom))
        .replace("__TEMPLATE__", json.dumps(template))
    )
    dest = smoke_root / "preview.html"
    dest.write_text(html, encoding="utf-8")
    return dest
