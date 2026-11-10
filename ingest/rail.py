"""Railroad track centerlines (US DOT NTAD North American Rail Network) for the constraints.

Run with `make rail`, then `make constraints`. Source and fields live under `rail_download` in
config/constraints.yaml; the `rail` layer there buffers the newest file into unbuildable land.
Each run saves a dated GeoJSON (never overwritten on another day) and logs to ingest_runs.
"""

import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import text

from ingest.db import get_engine

ROOT = Path(__file__).resolve().parent.parent
PAGE = 1000


def county_extent(engine) -> tuple[float, float, float, float]:
    """The parcels' extent in WGS84 (xmin, ymin, xmax, ymax)."""
    with engine.connect() as conn:
        return tuple(conn.execute(text(
            "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM (SELECT ST_Transform("
            "ST_SetSRID(ST_Extent(geom)::geometry, 2264), 4326) AS e FROM parcels) x")).one())


def download(url: str, fields: list[str], extent: tuple, out_dir: Path, today: date) -> tuple[Path, int]:
    """Save every line intersecting the extent as GeoJSON, paging through results."""
    features, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "geometry": ",".join(map(str, extent)), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": ",".join(fields), "outSR": 4326,
            "f": "geojson", "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID"})
        with urllib.request.urlopen(f"{url}/query?{q}", timeout=300) as r:
            page = json.load(r)
        if "error" in page:
            raise RuntimeError(f"{url}: {page['error']}")
        features += page["features"]
        if len(page["features"]) < PAGE:
            break
        offset += PAGE
    if not features:
        raise RuntimeError(f"{url}: no rail lines in the county extent {extent}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rail_lines_{today:%Y-%m-%d}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path, len(features)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())["rail_download"]
    engine, today = get_engine(), date.today()
    path, n = download(cfg["url"], cfg["fields"], county_extent(engine), ROOT / cfg["download_dir"], today)
    rel = path.relative_to(ROOT).as_posix()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('rail', :d, :f, true, CAST(:r AS jsonb))"),
            {"d": today, "f": rel, "r": json.dumps({"lines": n})})
    print(f"Rail: {n:,} track segments -> {rel}. Run `make constraints` to apply them.")


if __name__ == "__main__":
    main()
