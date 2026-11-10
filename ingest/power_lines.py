"""Electric power transmission lines (public copy of the federal HIFLD layer) for land checks.

Run with `make power-lines`, then `make constraints`. Source and fields live under
`power_lines_download` in config/constraints.yaml; the `power_lines` layer there (class info)
loads the newest file into constraint_features. Positions are generalized (off by 100 ft or more),
so the lines are never deducted as unbuildable: routing holds a pick within routing
land_checks.power_line_ft for a look (check_land). Each run saves a dated GeoJSON.
"""

import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import text

from ingest.db import get_engine
from ingest.rail import county_extent

ROOT = Path(__file__).resolve().parent.parent
PAGE = 1000


def download(url: str, fields: list[str], extent: tuple, out_dir: Path, today: date) -> tuple[Path, int]:
    """Save every line intersecting the extent as GeoJSON. The service answers Esri JSON only, so
    each feature's paths become a MultiLineString."""
    features, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "geometry": ",".join(map(str, extent)), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": ",".join(fields), "outSR": 4326,
            "f": "json", "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID_1"})
        with urllib.request.urlopen(f"{url}/query?{q}", timeout=300) as r:
            page = json.load(r)
        if "error" in page:
            raise RuntimeError(f"{url}: {page['error']}")
        features += [{"type": "Feature", "properties": f["attributes"],
                      "geometry": {"type": "MultiLineString", "coordinates": f["geometry"]["paths"]}}
                     for f in page["features"] if f.get("geometry")]
        if len(page["features"]) < PAGE:
            break
        offset += PAGE
    if not features:
        raise RuntimeError(f"{url}: no power lines in the county extent {extent}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"power_lines_{today:%Y-%m-%d}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path, len(features)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())["power_lines_download"]
    engine, today = get_engine(), date.today()
    path, n = download(cfg["url"], cfg["fields"], county_extent(engine), ROOT / cfg["download_dir"], today)
    rel = path.relative_to(ROOT).as_posix()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('power_lines', :d, :f, true, CAST(:r AS jsonb))"),
            {"d": today, "f": rel, "r": json.dumps({"lines": n})})
    print(f"Power lines: {n:,} lines -> {rel}. Run `make constraints` to apply them.")


if __name__ == "__main__":
    main()
