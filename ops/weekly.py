"""Weekly refresh: reload every source, rebuild parcel_features, write changes, log the run.

Run with `make weekly`; scripts/weekly.cmd runs it from Windows Task Scheduler on Sunday nights.
Scriptable sources (street centerlines, city capital projects) are downloaded fresh; the
county layers are reloaded from whatever was last downloaded by hand (`make refresh-manual`
lists them). Every step is idempotent, so a week with no new data records no changes.

The log goes to logs/weekly_<date>_<time>.log. Its first line is the status; on failure it
names the step and the error before anything else.

Disk space is checked before and after the run: low space puts a warning at the top of the log
and shows a Windows notification; very low space skips the run (see `weekly` in config/features.yaml).
"""

import os
import shutil
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


def disk_check(free_gb: float, image_gb: float | None, warn_gb: float, stop_gb: float) -> tuple[str, str]:
    """Return (level, message), level "ok", "warn" or "stop", for the free space on the Docker drive."""
    image = f"; Docker disk image {image_gb:.1f} GB" if image_gb is not None else ""
    fix = "free space (Downloads, Recycle Bin) or compact Docker's disk image"
    if free_gb < stop_gb:
        return "stop", f"only {free_gb:.1f} GB free on the Docker drive (minimum {stop_gb} GB){image}. Run skipped: {fix}."
    if free_gb < warn_gb:
        return "warn", f"{free_gb:.1f} GB free on the Docker drive (warning below {warn_gb} GB){image}. Soon: {fix}."
    return "ok", f"{free_gb:.1f} GB free on the Docker drive{image}"


def measure_disk(cfg: dict) -> tuple[str, str]:
    image = Path(os.path.expandvars(cfg["docker_disk_image"]))
    free_gb = shutil.disk_usage(image.anchor or ROOT.anchor).free / 1e9
    image_gb = image.stat().st_size / 1e9 if image.exists() else None
    return disk_check(free_gb, image_gb, cfg["disk_warn_free_gb"], cfg["disk_stop_free_gb"])


def notify(title: str, message: str) -> None:
    """Show a Windows notification (best effort; the log is the record)."""
    if os.name != "nt":
        return
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text>"
        f"<text>{esc(message)}</text></binding></visual></toast>');"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60)


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

    level, disk_msg = measure_disk(cfg)
    if level == "stop":
        failed = ("disk space", disk_msg)
    else:
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
    counts = {} if failed and failed[0] in ("database connection", "disk space") else row_counts()
    if level != "stop":
        level, disk_msg = measure_disk(cfg)  # after the run, since the run grows Docker's disk image
    end = datetime.now()
    status = (f"STATUS: FAILED at step '{failed[0]}': {failed[1]}" if failed else "STATUS: OK")
    header = [
        status,
        *([f"DISK WARNING: {disk_msg}"] if level == "warn" else []),
        f"start {start:%Y-%m-%d %H:%M:%S}, end {end:%Y-%m-%d %H:%M:%S} ({(end - start).total_seconds() / 60:.1f} min)",
        f"Disk: {disk_msg}",
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
    if failed or level != "ok":
        try:
            notify("NC Land weekly: " + ("FAILED" if failed else "low disk space"),
                   (failed[1] if failed else disk_msg) + f" Log: {log_path.name}")
        except Exception:
            pass
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
