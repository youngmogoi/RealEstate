"""Cleaning rules shared by the parcel ingests."""

from datetime import date

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import MultiPolygon


def strip_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Trim text columns, collapse repeated spaces, and turn blanks into nulls."""
    for col in df.select_dtypes(include=["object", "str"]).columns:
        df[col] = df[col].str.strip().str.replace(r"\s{2,}", " ", regex=True).replace("", None)
    return df


def acres_mismatch(gis_acres: pd.Series, other_acres: pd.Series, threshold: float) -> pd.Series:
    """True where GIS acres differ from `other_acres` by more than `threshold` (a fraction of other)."""
    return (other_acres > 0) & ((gis_acres - other_acres).abs() / other_acres > threshold)


def clean_dates(
    values: pd.Series, placeholders: list[str], as_of: date, fmt: str | None = None
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Parse dates; null out placeholders and dates after `as_of`.

    Returns (dates, was_placeholder, was_after_as_of).
    """
    dates = pd.to_datetime(values, format=fmt, errors="coerce")
    placeholder = dates.isin(pd.to_datetime(placeholders))
    future = dates > pd.Timestamp(as_of)
    return dates.mask(placeholder | future), placeholder, future


def _polygonal(geom) -> MultiPolygon:
    """Polygon parts of a geometry (make_valid can return collections) as a MultiPolygon."""
    parts = [p for p in shapely.get_parts(geom) if p.geom_type in ("Polygon", "MultiPolygon")]
    return MultiPolygon([q for p in parts for q in shapely.get_parts(p)])


def fix_geometries(geoms: gpd.GeoSeries) -> tuple[gpd.GeoSeries, pd.Series]:
    """Repair invalid polygons. Returns (geometries, was_invalid)."""
    invalid = pd.Series(~shapely.is_valid(geoms.values), index=geoms.index)
    if invalid.any():
        geoms = geoms.copy()
        geoms[invalid] = [_polygonal(shapely.make_valid(g)) for g in geoms[invalid]]
    return geoms, invalid
