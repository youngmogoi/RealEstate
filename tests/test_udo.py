"""Tests for ingest/udo.py helpers, on date and path formats seen in the 2026-10 ordinances."""

from datetime import date
from pathlib import Path

import pytest

from ingest.udo import extract_dir, parse_date


@pytest.mark.parametrize("s, d", [
    ("March 23, 2026", date(2026, 3, 23)),      # Charlotte cover
    ("02.25.2025", date(2025, 2, 25)),          # Davidson page header
    ("8/18/2026", date(2026, 8, 18)),           # Huntersville history note
    ("9-8-25", date(2025, 9, 8)),               # Matthews "passed 9-8-25"
    ("1- 9-2025", date(2025, 1, 9)),       # Mint Hill (Municode pads the day)
    ("not a date", None),
])
def test_parse_date(s, d):
    assert parse_date(s) == d


def test_extract_dir():
    assert extract_dir(Path("udo/CHARLOTTEUDO/2026-10-28/udo.pdf")) == Path("udo/CHARLOTTEUDO/2026-10-28/extract")
    assert extract_dir(Path("udo/MatthewsUDO/Matthewsudo.pdf")) == Path("udo/MatthewsUDO/extract/Matthewsudo")
