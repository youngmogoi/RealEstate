"""Copy your verdicts from the review workbook into config/land_checks.yaml.

Run with `make import-checks` (newest exports/scores_*.xlsx), or
`python -m ops.import_checks exports/scores_2026-11-10.xlsx`. Then `make route` moves the parcels.
Reads my_verdict (ok / no; anything else is ignored) and my_note on every sheet. A PIN already in
land_checks.yaml with a different verdict or note has its line updated in place (dated today); new
PINs are added at the bottom under a dated heading. Comments and earlier entries are kept.
"""

import argparse
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

from scoring.route import load_verdicts

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "config" / "land_checks.yaml"


def read_verdicts(path: Path) -> dict[str, dict]:
    """{pin: {verdict, note}} from every sheet's my_verdict / my_note (pins read as text)."""
    out = {}
    for name, df in pd.read_excel(path, sheet_name=None, dtype={"pin": str}).items():
        if "my_verdict" not in df or "pin" not in df:
            continue
        for pin, v, note in zip(df["pin"], df["my_verdict"], df["my_note"]):
            v = str(v).strip().lower() if isinstance(v, str) else None
            if v in ("ok", "no") and isinstance(pin, str):
                out[pin.strip().zfill(8) if pin.strip().isdigit() else pin.strip()] = {
                    "verdict": v, "note": note.strip() if isinstance(note, str) and note.strip() else None,
                    "sheet": name}
    return out


def entry(pin: str, v: dict, checked: date) -> str:
    note = json.dumps(v.get("note") or "", ensure_ascii=False)           # a valid YAML double-quoted string
    return f'"{pin}": {{verdict: "{v["verdict"]}", note: {note}, checked: {checked:%Y-%m-%d}}}'


def merge(text_: str, existing: dict[str, dict], new: dict[str, dict], today: date, source: str) -> tuple[str, dict]:
    """land_checks.yaml with `new` merged in, and counts {added, updated, unchanged}."""
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    added = []
    for pin, v in new.items():
        old = existing.get(pin)
        if old is None:
            added.append(entry(pin, v, today))
            counts["added"] += 1
        elif old.get("verdict") == v["verdict"] and (v.get("note") is None or old.get("note") == v["note"]):
            counts["unchanged"] += 1
        else:
            keep = v | {"note": v.get("note") or old.get("note")}
            text_, n = re.subn(rf'^"{re.escape(pin)}":.*$', entry(pin, keep, today), text_, count=1, flags=re.M)
            counts["updated" if n else "added"] += 1
            if not n:
                added.append(entry(pin, keep, today))
    if added:
        text_ = text_.rstrip("\n") + f"\n\n# Imported {today:%Y-%m-%d} from {source}\n" + "\n".join(added) + "\n"
    return text_, counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook", nargs="?", help="default: the newest exports/scores_*.xlsx")
    args = ap.parse_args()
    path = Path(args.workbook) if args.workbook else max((ROOT / "exports").glob("scores_*.xlsx"),
                                                          key=lambda p: p.stat().st_mtime, default=None)
    if path is None or not path.exists():
        raise SystemExit("no review workbook: run `make export` first")
    new = read_verdicts(path)
    text_, counts = merge(CHECKS.read_text(encoding="utf-8"), load_verdicts(CHECKS), new, date.today(), path.name)
    CHECKS.write_text(text_, encoding="utf-8")
    load_verdicts(CHECKS)                                     # fails loudly if the file no longer reads
    print(f"{path.name}: {len(new)} verdicts -> land_checks.yaml ({counts['added']} added, {counts['updated']} "
          f"updated, {counts['unchanged']} unchanged). Run `make route` to apply them.")


if __name__ == "__main__":
    main()
