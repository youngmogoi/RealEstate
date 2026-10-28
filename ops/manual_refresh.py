"""Checklist of layers that must be downloaded by hand, with their last download dates.

Run with `make refresh-manual`. The county's map server refuses scripts, so these come from
Mecklenburg Open Mapping (gis.mecknc.gov/data-center). Paths come from config/ingest.yaml
and config/constraints.yaml; the age limit from config/features.yaml.
"""

import os
from datetime import date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def manual_layers() -> list[tuple[str, str]]:
    """(portal dataset name, file path) for every hand-downloaded file the ingests read."""
    ingest = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    constraints = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
    c, z = ingest["county"], ingest["zoning"]
    layers = [
        ("Tax Parcel Boundaries", c["boundaries"]),
        ("Tax Parcel Ownership Data", c["owners"]),
        ("Tax Parcel Sales", c["sales"]),
        ("City of Charlotte Zoning", z["charlotte"]),
        ("Unincorporated County & Towns Zoning (in the Tax bundle)", z["towns"]),
        ("Parcels with zoning and zipcode (cross-check)", z["county_check"]),
    ]
    names = {
        "community_encroachment": "Community Floodway", "fema_floodway": "FEMA Floodway",
        "possible_unmapped_wetland": "Wetlands", "water_quality_buffers": "Surface Water Protection",
        "fema_floodplain": "FEMA Floodplain", "community_floodplain": "Community Floodplain",
        "streams": "Creeks and Streams",
    }
    layers += [(names.get(k, k), spec["file"]) for k, spec in constraints["layers"].items()]
    return layers


def checklist(max_age_days: int, today: date | None = None) -> tuple[list[str], list[str]]:
    """Printable lines, and warnings for files missing or older than max_age_days."""
    today = today or date.today()
    lines, warnings = [], []
    for name, rel in manual_layers():
        path = ROOT / rel
        if not path.exists():
            lines.append(f"  [ ] {name:<56} MISSING  {rel}")
            warnings.append(f"{name} is missing ({rel})")
            continue
        downloaded = datetime.fromtimestamp(os.path.getmtime(path)).date()
        age = (today - downloaded).days
        stale = age > max_age_days
        lines.append(f"  [{'!' if stale else ' '}] {name:<56} {downloaded}  {age:>4} days  {rel}")
        if stale:
            warnings.append(f"{name} is {age} days old (downloaded {downloaded}; limit {max_age_days})")
    return lines, warnings


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())["weekly"]
    lines, warnings = checklist(cfg["manual_max_age_days"])
    print("Download these from Mecklenburg Open Mapping (gis.mecknc.gov/data-center) and replace the files;")
    print(f"then run `make weekly`. [!] = older than {cfg['manual_max_age_days']} days.\n")
    print("\n".join(lines))
    if warnings:
        print("\nWarnings:\n  " + "\n  ".join(warnings))


if __name__ == "__main__":
    main()
