"""Weekly refresh: reload every source, rebuild parcel_features, write changes, log the run.

Run with `make weekly`; scripts/weekly.cmd runs it from Windows Task Scheduler on Sunday nights.
Scriptable sources (street centerlines, city capital projects) are downloaded fresh; the
county layers are reloaded from whatever was last downloaded by hand (`make refresh-manual`
lists them). Every step is idempotent, so a week with no new data records no changes.

The log goes to logs/weekly_<date>_<time>.log. Its first line is the status; on failure it
names the step and the error before anything else.
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import text

from ingest.db import get_engine
from ops.manual_refresh import checklist

ROOT = Path(__file__).resolve().parent.parent

STEPS = [
    ("county parcels (manual files)", "ingest.county_parcels"),
    ("zoning (manual files)", "ingest.zoning"),
    ("street centerlines (download)", "ingest.streets"),
    ("utilities + city capital projects (download)", "ingest.utilities"),
    ("constraints (manual files + cached slope/soils)", "ingest.constraints"),
    ("NC OneMap check", "ingest.nconemap_check"),
    ("parcel features", "features.build"),
    ("changes", "features.changes"),
]
COUNT_TABLES = ["parcels", "parcel_owners", "parcel_sales", "parcel_zoning", "parcel_utilities", "parcel_constraints",
                "streets", "parcel_features", "changes"]


def run_step(module: str) -> tuple[int, str, float]:
    t = time.time()
    p = subprocess.run([sys.executable, "-m", module], cwd=ROOT, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONWARNINGS": "ignore", "PYTHONUNBUFFERED": "1"})
    return p.returncode, (p.stdout + p.stderr).strip(), time.time() - t


def row_counts() -> dict:
    with get_engine().connect() as conn:
        out = {}
        for t in COUNT_TABLES:
            exists = conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{t}"}).scalar_one()
            out[t] = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() if exists else None
        return out


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())["weekly"]
    log_dir = ROOT / cfg["log_dir"]
    log_dir.mkdir(exist_ok=True)
    start = datetime.now()
    log_path = log_dir / f"weekly_{start:%Y-%m-%d_%H%M%S}.log"
    body, failed = [], None

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        failed = ("database connection", f"{type(e).__name__}: {e}".splitlines()[0])

    for name, module in ([] if failed else STEPS):
        print(f"[{datetime.now():%H:%M:%S}] {name} ...", flush=True)
        code, output, secs = run_step(module)
        body += [f"===== {name} ({module}): exit {code}, {secs / 60:.1f} min", output, ""]
        if code != 0:
            last = [l for l in output.splitlines() if l.strip()]
            failed = (name, last[-1] if last else f"exit code {code}")
            break

    _, warnings = checklist(cfg["manual_max_age_days"])
    counts = {} if failed and failed[0] == "database connection" else row_counts()
    end = datetime.now()
    status = (f"STATUS: FAILED at step '{failed[0]}': {failed[1]}" if failed else "STATUS: OK")
    header = [
        status,
        f"start {start:%Y-%m-%d %H:%M:%S}, end {end:%Y-%m-%d %H:%M:%S} ({(end - start).total_seconds() / 60:.1f} min)",
        "",
        "Manual layers:" + (" all within limit" if not warnings else ""),
        *[f"  WARNING: {w}" for w in warnings],
        "",
        "Row counts:",
        *[f"  {t:<20} {('-' if n is None else f'{n:,}'):>12}" for t, n in counts.items()],
        "",
    ]
    log_path.write_text("\n".join(header + body), encoding="utf-8")
    print("\n".join(header))
    print(f"Log: {log_path.relative_to(ROOT).as_posix()}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
