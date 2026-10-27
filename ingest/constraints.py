"""Site constraints per parcel: unbuildable acres, net buildable acres, and flag acres.

Run with `make constraints` (after `make ingest` and `make utilities`). Layers, slope bands,
and their classes live in config/constraints.yaml.

- constraint_features: every vector constraint shape, with `layer`, `class`, source file and
  date, plus USDA septic soil ratings (fetched once and saved under `derived_dir`).
- Slope comes from USGS 3DEP elevation: a raster of slope bands is built once at
  `resolution_ft` and saved under `derived_dir`. Per-parcel band acres are coverage-weighted
  pixel counts (exactextract), with no minimum patch size.
- parcel_constraints: per parcel, acres in each layer and slope band, unbuildable acres,
  net buildable acres, floodplain flag acres, stream length, septic soil acres (only where
  sewer_status is not "served"), and each layer's download date.

Unbuildable acres never double count: the vector unbuildable layers are unioned inside the
parcel, and unbuildable slope pixels are added only where they fall outside that union
(the vector layers are rasterized onto the slope grid by pixel center for this). Floodplain
flag acres are the union of the 1% floodplain layers outside the unbuildable area and outside
open water (NWI lakes and ponds, reported as floodplain_open_water_acres). Gross acres
are the parcel shape's own area, and net = gross - unbuildable, kept between 0 and gross.
"""

import http.client
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import shapely
import yaml
from exactextract import exact_extract
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.windows import Window, bounds as window_bounds
from sqlalchemy import Engine, text

from ingest.common import fix_geometries
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
FT_PER_M = 3.280839895
TILE_PX = 2000  # the 3DEP service errors on larger F32 exports
NODATA = 255
FLOOD_PARTS_TOL = 0.001  # acres; floodplain parts must sum to the floodplain total within this
SEPTIC_CLASSES = {"Not limited": "not_limited", "Somewhat limited": "somewhat_limited", "Very limited": "very_limited"}


def file_date(path: Path) -> date:
    return datetime.fromtimestamp(os.path.getmtime(path)).date()


def read_layer(name: str, spec: dict) -> gpd.GeoDataFrame:
    """One configured layer as [layer, class, attrs, geometry] in EPSG:2264."""
    gdf = pyogrio.read_dataframe(ROOT / spec["file"], layer=spec.get("layer"))
    for col, value in (spec.get("where") or {}).items():
        gdf = gdf[gdf[col] == value]
    for col, values in (spec.get("exclude") or {}).items():
        gdf = gdf[~gdf[col].isin(values)]
    gdf = gdf.to_crs(CRS_EPSG) if gdf.crs.to_epsg(min_confidence=20) != CRS_EPSG else gdf.set_crs(CRS_EPSG, allow_override=True)
    attrs = gdf.drop(columns="geometry").astype(str).to_dict("records")
    geoms = gdf.geometry
    if geoms.geom_type.str.contains("Polygon").all():
        geoms, _ = fix_geometries(geoms)
    return gpd.GeoDataFrame({
        "layer": name, "class": spec["class"],
        "attrs": [json.dumps({k: v for k, v in a.items() if v not in ("None", "nan", "")}) for a in attrs],
    }, geometry=shapely.force_2d(geoms.values), crs=CRS_EPSG)


def _export_tile(url: str, bbox: tuple, px: int, attempts: int = 4) -> np.ndarray:
    q = urllib.parse.urlencode({
        "bbox": ",".join(f"{v:.2f}" for v in bbox), "bboxSR": CRS_EPSG, "imageSR": CRS_EPSG,
        "size": f"{px},{px}", "format": "tiff", "pixelType": "F32", "noData": -9999,
        "interpolation": "RSP_BilinearInterpolation", "f": "image",
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(f"{url}/exportImage?{q}", timeout=600) as r:
                data = r.read()
            with rasterio.MemoryFile(data) as m, m.open() as ds:
                return ds.read(1)
        except (OSError, http.client.HTTPException, rasterio.errors.RasterioIOError):
            if attempt == attempts:
                raise
            time.sleep(15 * attempt)  # dropped connections from the USGS service are usually brief


def slope_bands(slope_pct: np.ndarray, bands: dict) -> np.ndarray:
    """Band index per pixel: 1..n in config order, 0 below every band, NODATA where unknown."""
    out = np.zeros(slope_pct.shape, "uint8")
    for i, spec in enumerate(bands.values(), 1):
        hit = slope_pct > spec["min_pct"]
        if spec.get("max_pct") is not None:
            hit &= slope_pct <= spec["max_pct"]
        out[hit] = i
    out[np.isnan(slope_pct)] = NODATA
    return out


def build_slope_raster(url: str, extent: tuple, res_ft: float, bands: dict, out_path: Path,
                       keep_tile=lambda bbox: True, log=print) -> None:
    """County raster of slope band indexes, from 3DEP elevation (meters, converted to feet)."""
    xmin, ymin, xmax, ymax = extent
    tile_ft = TILE_PX * res_ft
    nx, ny = int(np.ceil((xmax - xmin) / tile_ft)), int(np.ceil((ymax - ymin) / tile_ft))
    top = ymin + ny * tile_ft
    profile = dict(driver="GTiff", width=nx * TILE_PX, height=ny * TILE_PX, count=1, dtype="uint8", nodata=NODATA,
                   crs=f"EPSG:{CRS_EPSG}", transform=from_origin(xmin, top, res_ft, res_ft),
                   tiled=True, blockxsize=512, blockysize=512, compress="deflate")
    tmp = out_path.with_suffix(".partial.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        for row in range(ny):
            for col in range(nx):
                x0, y1 = xmin + col * tile_ft, top - row * tile_ft
                window = Window(col * TILE_PX, row * TILE_PX, TILE_PX, TILE_PX)
                # one extra pixel on every side so edge slopes use real neighbors
                bbox = (x0 - res_ft, y1 - tile_ft - res_ft, x0 + tile_ft + res_ft, y1 + res_ft)
                if not keep_tile(bbox):
                    dst.write(np.full((TILE_PX, TILE_PX), NODATA, "uint8"), 1, window=window)
                    continue
                z = _export_tile(url, bbox, TILE_PX + 2)
                zf = np.where(z > -1000, z, np.nan).astype("float64") * FT_PER_M
                dzdy, dzdx = np.gradient(zf, res_ft)
                slope = (np.hypot(dzdx, dzdy) * 100)[1:-1, 1:-1]
                dst.write(slope_bands(slope, bands), 1, window=window)
                log(f"  slope tile {row * nx + col + 1}/{nx * ny}")
    tmp.replace(out_path)


def slope_acres(raster: Path, parcels_in, unbuildable_in, bands: dict, log=print) -> pd.DataFrame:
    """Per parcel: acres in each band, and for unbuildable bands also acres outside the vector
    unbuildable layers. parcels_in(bounds) / unbuildable_in(bounds) return GeoDataFrames."""
    names = list(bands)
    unb_bands = [n for n, b in bands.items() if b["class"] == "unbuildable"]
    parts = []
    with rasterio.open(raster) as src:
        px_acres = abs(src.transform.a * src.transform.e) / 43560
        for window in _tiles(src):
            idx = src.read(1, window=window)
            if (idx == NODATA).all():
                continue
            b = window_bounds(window, src.transform)
            parcels = parcels_in(b)
            if parcels.empty:
                continue
            transform = src.window_transform(window)
            layers = [(idx == i + 1).astype("float32") for i in range(len(names))]
            unb = unbuildable_in(b)
            covered = (rasterize(((g, 1) for g in unb.geometry), out_shape=idx.shape, transform=transform, fill=0,
                                 dtype="uint8").astype(bool) if len(unb) else np.zeros(idx.shape, bool))
            layers += [((idx == names.index(n) + 1) & ~covered).astype("float32") for n in unb_bands]
            with rasterio.MemoryFile() as m:
                with m.open(driver="GTiff", width=idx.shape[1], height=idx.shape[0], count=len(layers),
                            dtype="float32", crs=src.crs, transform=transform) as ds:
                    ds.write(np.stack(layers))
                with m.open() as ds:
                    r = exact_extract(ds, parcels, ["sum"], include_cols=["pin"], output="pandas")
            parts.append(r)
            log(f"  slope zonal window at col {window.col_off}, row {window.row_off}: {len(parcels):,} parcels")
    cols = [f"{n}_acres" for n in names] + [f"{n}_outside_vector_acres" for n in unb_bands]
    if not parts:
        return pd.DataFrame(columns=["pin"] + cols)
    df = pd.concat(parts, ignore_index=True)
    df.columns = ["pin"] + cols
    df = df.groupby("pin", as_index=False)[cols].sum()
    df[cols] = df[cols] * px_acres
    return df


def _tiles(src):
    for row in range(0, src.height, TILE_PX):
        for col in range(0, src.width, TILE_PX):
            yield Window(col, row, min(TILE_PX, src.width - col), min(TILE_PX, src.height - row))


def _sda(url: str, query: str) -> list:
    req = urllib.request.Request(url, data=json.dumps({"query": query, "format": "JSON"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r).get("Table", [])


def septic_soils(url: str, area: str, rule: str) -> gpd.GeoDataFrame:
    """Soil map unit polygons with the septic rating of each map unit's dominant component."""
    ratings = _sda(url, f"""
        SELECT mu.mukey, mu.musym, mu.muname, c.comppct_r, coalesce(i.interphrc, 'Not rated')
        FROM legend l JOIN mapunit mu ON mu.lkey = l.lkey
        JOIN component c ON c.mukey = mu.mukey
        LEFT JOIN cointerp i ON i.cokey = c.cokey AND i.mrulename = '{rule}' AND i.ruledepth = 0
        WHERE l.areasymbol = '{area}'""")
    r = pd.DataFrame(ratings, columns=["mukey", "musym", "muname", "comppct", "rating"])
    r["comppct"] = pd.to_numeric(r["comppct"])
    r = r.sort_values(["mukey", "comppct"], ascending=[True, False]).drop_duplicates("mukey")
    polys = _sda(url, f"SELECT mukey, mupolygongeo.STAsText() FROM mupolygon WHERE areasymbol = '{area}'")
    g = gpd.GeoDataFrame({"mukey": [p[0] for p in polys]},
                         geometry=shapely.from_wkt(np.array([p[1] for p in polys], dtype=object)),
                         crs=4326).to_crs(CRS_EPSG)
    g = g.merge(r, on="mukey", how="left")
    g["geometry"], _ = fix_geometries(g.geometry)
    return gpd.GeoDataFrame({
        "layer": "septic_soils",
        "attrs": [json.dumps({"mukey": m, "musym": s, "muname": n, "rating": rt if isinstance(rt, str) else "Not rated"})
                  for m, s, n, rt in zip(g["mukey"], g["musym"], g["muname"], g["rating"])],
        "rating": g["rating"].fillna("Not rated"),
    }, geometry=g.geometry.values, crs=CRS_EPSG)


def _feature_rows(chunk: pd.DataFrame) -> pd.DataFrame:
    geoms = shapely.set_srid(chunk["geom"].values, CRS_EPSG)
    rows = pd.DataFrame(chunk.drop(columns="geom")).assign(geom=shapely.to_wkb(geoms, hex=True, include_srid=True))
    rows.insert(0, "feature_id", row_hash(rows[["layer", "attrs", "geom"]]))
    return rows.assign(row_hash=row_hash(rows))


def load_features(features: gpd.GeoDataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.constraint_features"
    df = features.rename_geometry("geom")[["layer", "class", "rating", "source", "source_date", "attrs", "geom"]]
    with engine.begin() as conn:
        # small chunks: some soil and wetland shapes are hundreds of thousands of vertices
        copy_dataframe(conn, df, f"{table}_staging", {"geom": f"geometry(Geometry, {CRS_EPSG})"}, _feature_rows,
                       chunk_rows=2000)
        conn.execute(text(f"DELETE FROM {table}_staging a USING {table}_staging b "
                          "WHERE a.feature_id = b.feature_id AND a.ctid > b.ctid"))  # identical duplicate shapes
        changes = apply_versioned(conn, table, ["feature_id"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS constraint_features_geom_idx ON {table} USING gist (geom)"))
    return changes


ASSIGN_SQL = """
WITH per_layer AS (
    SELECT pin, layer, ST_Area(ST_UnaryUnion(ST_Collect(g), 0.01)) / 43560 AS acres
    FROM {s}.constraint_hits GROUP BY pin, layer
),
layers AS (
    SELECT pin,
{layer_pivot}
    FROM per_layer GROUP BY pin
),
unb AS (
    SELECT pin, ST_UnaryUnion(ST_Collect(g), 0.01) AS g FROM {s}.constraint_hits WHERE class = 'unbuildable' GROUP BY pin
),
flood AS (
    SELECT pin, ST_UnaryUnion(ST_Collect(g), 0.01) AS g FROM {s}.constraint_hits
    WHERE layer = ANY(:floodplain_layers) GROUP BY pin
),
water AS (
    SELECT pin, ST_UnaryUnion(ST_Collect(g), 0.01) AS g FROM {s}.constraint_hits WHERE class = 'water' GROUP BY pin
),
flood_net AS (
    -- floodplain outside the unbuildable area
    SELECT f.pin, CASE WHEN u.g IS NULL THEN f.g ELSE ST_Difference(f.g, u.g) END AS g
    FROM flood f LEFT JOIN unb u USING (pin)
),
streams AS (
    SELECT p.pin, sum(ST_Length(ST_Intersection(p.geom, f.geom))) AS ft
    FROM {s}.parcels p JOIN {s}.constraint_features f ON f.layer = 'streams' AND ST_Intersects(p.geom, f.geom)
    GROUP BY p.pin
),
septic AS (
    SELECT p.pin, f.rating, sum(ST_Area(ST_Intersection(p.geom, f.geom))) / 43560 AS acres
    FROM {s}.parcels p
    JOIN {s}.parcel_utilities u ON u.pin = p.pin AND u.sewer_status <> 'served'
    JOIN {s}.constraint_features f ON f.layer = 'septic_soils' AND ST_Intersects(p.geom, f.geom)
    GROUP BY p.pin, f.rating
),
septic_p AS (
    SELECT pin,
{septic_pivot},
           (array_agg(rating ORDER BY acres DESC, rating))[1] AS dominant
    FROM septic GROUP BY pin
),
gross AS (SELECT pin, ST_Area(geom) / 43560 AS acres FROM {s}.parcels),
calc AS (
    SELECT g.pin, g.acres AS gross,
           coalesce(ST_Area(unb.g) / 43560, 0) + {slope_unbuildable_extra} AS unbuildable_raw,
           coalesce(ST_Area(CASE WHEN w.g IS NULL THEN fn.g ELSE ST_Difference(fn.g, w.g) END) / 43560, 0) AS flood_raw,
           coalesce(ST_Area(ST_Intersection(fn.g, w.g)) / 43560, 0) AS open_water_raw,
           coalesce(ST_Area(flood.g) / 43560, 0) AS flood_total_raw,
           coalesce(ST_Area(ST_Intersection(flood.g, unb.g)) / 43560, 0) AS flood_unbuildable_raw
    FROM gross g LEFT JOIN unb USING (pin) LEFT JOIN flood USING (pin) LEFT JOIN flood_net fn USING (pin)
    LEFT JOIN water w USING (pin)
    LEFT JOIN {s}.constraint_slope sl USING (pin)
)
SELECT c.pin, c.gross AS gross_acres,
{layer_cols},
{slope_cols},
       least(c.unbuildable_raw, c.gross) AS unbuildable_acres,
       greatest(c.gross - c.unbuildable_raw, 0) AS net_buildable_acres,
       -- floodplain_total = floodplain_unbuildable + floodplain_open_water + floodplain_flag
       least(c.flood_total_raw, c.gross) AS floodplain_total_acres,
       least(c.flood_unbuildable_raw, c.gross) AS floodplain_unbuildable_acres,
       least(c.open_water_raw, c.gross) AS floodplain_open_water_acres,
       least(c.flood_raw, c.gross) AS floodplain_flag_acres,
       coalesce(st.ft, 0) AS stream_length_ft,
       coalesce(u.sewer_status <> 'served', false) AS septic_checked,
{septic_cols},
       CASE WHEN u.sewer_status <> 'served' THEN sp.dominant END AS septic_dominant_rating,
{date_cols}
FROM calc c
LEFT JOIN layers la USING (pin)
LEFT JOIN {s}.constraint_slope sl USING (pin)
LEFT JOIN septic_p sp USING (pin)
LEFT JOIN streams st USING (pin)
LEFT JOIN {s}.parcel_utilities u USING (pin)
"""


def assign_parcels(engine: Engine, as_of: date, cfg: dict, slope: pd.DataFrame, dates: dict[str, date],
                   schema: str = "public") -> dict:
    """cfg is config/constraints.yaml; slope comes from slope_acres()."""
    s = schema
    area_layers = [l for l, spec in cfg["layers"].items() if spec["class"] != "info"]
    floodplain = [l for l, spec in cfg["layers"].items() if spec.get("flag_group") == "floodplain"]
    ow = cfg["floodplain_open_water"]
    bands = cfg["slope"]["bands"]
    unb_bands = [n for n, b in bands.items() if b["class"] == "unbuildable"]
    layer_pivot = ",\n".join(f"           sum(acres) FILTER (WHERE layer = '{l}') AS {l}" for l in area_layers)
    layer_cols = ",\n".join(f"       least(coalesce(la.{l}, 0), c.gross) AS {l}_acres" for l in area_layers)
    slope_cols = ",\n".join(f"       least(coalesce(sl.{n}_acres, 0), c.gross) AS {n}_acres" for n in bands)
    extra = " + ".join(f"coalesce(sl.{n}_outside_vector_acres, 0)" for n in unb_bands) or "0"
    septic_pivot = ",\n".join(
        f"           sum(acres) FILTER (WHERE rating = '{label}') AS {col}" for label, col in SEPTIC_CLASSES.items())
    septic_cols = ",\n".join(
        f"       CASE WHEN u.sewer_status <> 'served' THEN coalesce(sp.{col}, 0) END AS septic_{col}_acres"
        for col in SEPTIC_CLASSES.values())
    date_cols = ",\n".join(f"       CAST(:d_{l} AS date) AS {l}_date" for l in dates)
    with engine.begin() as conn:
        copy_dataframe(conn, slope, f"{s}.constraint_slope")
        conn.execute(text(f"CREATE INDEX ON {s}.constraint_slope (pin)"))
        conn.execute(text(f"DROP TABLE IF EXISTS {s}.constraint_pieces, {s}.constraint_hits"))
        conn.execute(text(
            f"CREATE UNLOGGED TABLE {s}.constraint_pieces AS SELECT layer, class, ST_Subdivide(geom, 256) AS geom "
            f"FROM {s}.constraint_features WHERE class IN ('unbuildable', 'flag') AND GeometryType(geom) LIKE '%POLYGON' "
            # open water, a working layer of class 'water': subtracted from the floodplain flag
            f"UNION ALL SELECT '_open_water', 'water', ST_Subdivide(geom, 256) FROM {s}.constraint_features "
            f"WHERE layer = :wl AND attrs::jsonb ->> :wc = ANY(:wv)"
        ), {"wl": ow["layer"], "wc": ow["column"], "wv": list(ow["values"])})
        conn.execute(text(f"CREATE INDEX ON {s}.constraint_pieces USING gist (geom)"))
        conn.execute(text(f"ANALYZE {s}.constraint_pieces"))
        conn.execute(text(f"""
            CREATE UNLOGGED TABLE {s}.constraint_hits AS
            SELECT p.pin, c.layer, c.class,
                   CASE WHEN ST_CoveredBy(p.geom, c.geom) THEN p.geom
                        ELSE ST_CollectionExtract(ST_Intersection(p.geom, c.geom), 3) END AS g
            FROM {s}.parcels p JOIN {s}.constraint_pieces c ON ST_Intersects(p.geom, c.geom)
        """))
        conn.execute(text(f"DELETE FROM {s}.constraint_hits WHERE ST_IsEmpty(g)"))
        conn.execute(text(f"CREATE INDEX ON {s}.constraint_hits (pin)"))
        conn.execute(text(f"ANALYZE {s}.constraint_hits"))
        sql = ASSIGN_SQL.format(s=s, layer_pivot=layer_pivot, layer_cols=layer_cols, slope_cols=slope_cols,
                                slope_unbuildable_extra=extra, septic_pivot=septic_pivot, septic_cols=septic_cols,
                                date_cols=date_cols)
        params = {"floodplain_layers": floodplain, **{f"d_{l}": d for l, d in dates.items()}}
        create_staging_from_query(conn, f"{s}.parcel_constraints_staging", sql, params)
        conn.execute(text(f"DROP TABLE {s}.constraint_pieces, {s}.constraint_hits, {s}.constraint_slope"))
        return apply_versioned(conn, f"{s}.parcel_constraints", ["pin"], as_of)


def read_vectors(cfg: dict, log=print) -> tuple[gpd.GeoDataFrame, dict[str, date]]:
    """Configured vector layers plus USDA septic soils, with source and source_date."""
    frames, dates = [], {}
    for name, spec in cfg["layers"].items():
        g = read_layer(name, spec)
        dates[name] = file_date(ROOT / spec["file"])
        frames.append(g.assign(source=spec["file"], source_date=dates[name]))
        log(f"  {name}: {len(g):,} shapes ({spec['class']})")
    so = cfg["septic_soils"]
    derived = ROOT / cfg["derived_dir"]
    derived.mkdir(parents=True, exist_ok=True)
    soils_path = derived / f"septic_soils_{so['survey_area']}.gpkg"
    if not soils_path.exists():
        pyogrio.write_dataframe(septic_soils(so["source"], so["survey_area"], so["rule"]), soils_path, layer="soils")
    soils = pyogrio.read_dataframe(soils_path)
    dates["septic_soils"] = file_date(soils_path)
    frames.append(soils.assign(**{"class": "info", "source": f"{so['source']} -> {soils_path.relative_to(ROOT).as_posix()}",
                                  "source_date": dates["septic_soils"]}))
    log(f"  septic_soils: {len(soils):,} soil polygons (info)")
    features = pd.concat(frames, ignore_index=True)
    if "rating" not in features:
        features["rating"] = None
    return gpd.GeoDataFrame(features, geometry="geometry", crs=CRS_EPSG), dates


def _db_lookups(engine: Engine, schema: str = "public"):
    def parcels_in(b):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT pin, ST_AsBinary(geom) FROM {schema}.parcels WHERE geom && ST_MakeEnvelope(:a, :b, :c, :d, 2264)"
            ), dict(zip("abcd", b))).all()
        return gpd.GeoDataFrame({"pin": [r[0] for r in rows]},
                                geometry=shapely.from_wkb([bytes(r[1]) for r in rows]), crs=CRS_EPSG)

    def unbuildable_in(b):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT ST_AsBinary(geom) FROM {schema}.constraint_features WHERE class = 'unbuildable' "
                f"AND geom && ST_MakeEnvelope(:a, :b, :c, :d, 2264)"), dict(zip("abcd", b))).all()
        return gpd.GeoDataFrame(geometry=shapely.from_wkb([bytes(r[0]) for r in rows]), crs=CRS_EPSG)

    def has_parcels(b):
        with engine.connect() as conn:
            return conn.execute(text(
                f"SELECT EXISTS (SELECT 1 FROM {schema}.parcels WHERE geom && ST_MakeEnvelope(:a, :b, :c, :d, 2264))"
            ), dict(zip("abcd", b))).scalar_one()

    return parcels_in, unbuildable_in, has_parcels


def quality_report(engine: Engine, cfg: dict, schema: str = "public") -> dict:
    s = schema
    cols = ([f"{l}_acres" for l, spec in cfg["layers"].items() if spec["class"] != "info"]
            + [f"{n}_acres" for n in cfg["slope"]["bands"]])
    with engine.connect() as conn:
        t = conn.execute(text(
            f"SELECT count(*) AS parcels, sum(gross_acres) AS gross, sum(unbuildable_acres) AS unbuildable, "
            f"sum(net_buildable_acres) AS net, sum(floodplain_flag_acres) AS flood, "
            f"sum(floodplain_open_water_acres) AS open_water, "
            f"count(*) FILTER (WHERE abs(floodplain_total_acres - floodplain_unbuildable_acres "
            f"- floodplain_open_water_acres - floodplain_flag_acres) > {FLOOD_PARTS_TOL}) AS flood_parts_mismatch, "
            f"count(*) FILTER (WHERE floodplain_flag_acres > 0) AS with_flood, "
            f"count(*) FILTER (WHERE unbuildable_acres > 0) AS with_unbuildable, "
            f"count(*) FILTER (WHERE net_buildable_acres < 0 OR net_buildable_acres > gross_acres) AS bad_net, "
            f"min(net_buildable_acres - 0) AS min_net, max(net_buildable_acres - gross_acres) AS max_net_over_gross, "
            f"count(*) FILTER (WHERE septic_checked) AS septic_checked FROM {s}.parcel_constraints")).one()._asdict()
        per = {c: conn.execute(text(f"SELECT count(*) FILTER (WHERE {c} > 0), coalesce(sum({c}), 0) "
                                    f"FROM {s}.parcel_constraints")).one() for c in cols}
    return {"totals": {k: float(v) for k, v in t.items()},
            "per_layer": {c: {"parcels": int(n), "acres": float(a)} for c, (n, a) in per.items()}}


def format_report(r: dict, changes: dict) -> str:
    t = r["totals"]
    lines = [
        "Constraints quality report",
        f"  parcels                          {t['parcels']:>12,.0f}",
        f"  gross acres                      {t['gross']:>12,.0f}",
        f"  unbuildable acres                {t['unbuildable']:>12,.0f}  on {t['with_unbuildable']:,.0f} parcels",
        f"  net buildable acres              {t['net']:>12,.0f}",
        f"  floodplain flag acres            {t['flood']:>12,.0f}  on {t['with_flood']:,.0f} parcels "
        f"(1% only, outside unbuildable and open water; not deducted)",
        f"  floodplain over open water       {t['open_water']:>12,.0f}  (NWI lakes/ponds; removed from the flag)",
        f"  floodplain parts not adding up   {t['flood_parts_mismatch']:>12,.0f}  (total = unbuildable + water + flag)",
        f"  parcels with net < 0 or > gross  {t['bad_net']:>12,.0f}  (min net {t['min_net']:.4f}; "
        f"max net - gross {t['max_net_over_gross']:.6f})",
        f"  parcels checked for septic       {t['septic_checked']:>12,.0f}",
        "  per layer (parcels touched, acres; overlaps between layers count in each):",
    ]
    lines += [f"    {c:<36} {v['parcels']:>9,}  {v['acres']:>11,.0f}" for c, v in r["per_layer"].items()]
    lines.append("  changes since last load:")
    for table, c in changes.items():
        lines.append(f"    {table:<20} +{c['added']:,} new, {c['changed']:,} changed, -{c['removed']:,} removed -> {c['current']:,}")
    return "\n".join(lines)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
    engine = get_engine()
    parcels_in, unbuildable_in, has_parcels = _db_lookups(engine)

    print("Reading constraint layers ...")
    features, dates = read_vectors(cfg)
    sl = cfg["slope"]
    raster = ROOT / cfg["derived_dir"] / f"slope_bands_{int(sl['resolution_ft'])}ft.tif"
    if not raster.exists():
        with engine.connect() as conn:
            ext = conn.execute(text("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                                    "FROM (SELECT ST_Extent(geom)::geometry AS e FROM parcels) x")).one()
        print("Building slope raster ...")
        build_slope_raster(sl["source"], tuple(ext), sl["resolution_ft"], sl["bands"], raster, has_parcels)
    for band in sl["bands"]:
        dates[band] = file_date(raster)
    as_of = max(dates.values())
    print(f"As-of date {as_of}")

    changes = {"constraint_features": load_features(features, engine, as_of)}
    print(f"  constraint_features loaded: {changes['constraint_features']}")
    print("Slope acres per parcel ...")
    slope = slope_acres(raster, parcels_in, unbuildable_in, sl["bands"], log=lambda *a: None)
    changes["parcel_constraints"] = assign_parcels(engine, as_of, cfg, slope, dates)
    report = quality_report(engine, cfg)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('constraints', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": json.dumps({k: str(v) for k, v in dates.items()}),
             "w": any(c["added"] or c["changed"] or c["removed"] for c in changes.values()),
             "r": json.dumps({**report, "changes": changes}, default=str)},
        )
    print()
    print(format_report(report, changes))
    bad = int(report["totals"]["bad_net"] + report["totals"]["flood_parts_mismatch"])
    if bad:
        raise SystemExit(f"{bad} parcels fail the net-acre or floodplain-parts checks; see the report above")


if __name__ == "__main__":
    main()
