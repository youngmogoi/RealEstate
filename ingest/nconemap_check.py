"""Check whether NC OneMap has newer Mecklenburg parcels than our 2026-01-07 snapshot.

Run by `make weekly`. NC OneMap is kept as history only (the county files are the primary
source), so this only reports; it never downloads. Exit code 0 either way.
"""

import json
import urllib.parse
import urllib.request

from sqlalchemy import text

from ingest.db import get_engine

URL = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1/query"


def remote_summary() -> dict:
    q = urllib.parse.urlencode({
        "where": "CNTYNAME='Mecklenburg'", "f": "json",
        "outStatistics": json.dumps([
            {"statisticType": "count", "onStatisticField": "PARNO", "outStatisticFieldName": "n"},
            {"statisticType": "max", "onStatisticField": "SALEDATE", "outStatisticFieldName": "max_sale"},
        ])})
    with urllib.request.urlopen(f"{URL}?{q}", timeout=120) as r:
        a = json.load(r)["features"][0]["attributes"]
    return {"rows": int(a["n"]), "max_sale_ms": a["max_sale"]}


def main() -> None:
    remote = remote_summary()
    with get_engine().connect() as conn:
        local = conn.execute(text(
            "SELECT max(snapshot_date), count(*) FILTER (WHERE snapshot_date = (SELECT max(snapshot_date) "
            "FROM nconemap_parcels_snapshots)) FROM nconemap_parcels_snapshots")).one()
    same = abs(remote["rows"] - local[1]) <= 10
    print(f"NC OneMap Mecklenburg: {remote['rows']:,} rows on the service vs {local[1]:,} in our {local[0]} snapshot: "
          + ("no newer file." if same else "NEWER DATA AVAILABLE - download the county zip from nconemap.gov "
                                         "into data/raw/ and run `make ingest-nconemap`."))


if __name__ == "__main__":
    main()
