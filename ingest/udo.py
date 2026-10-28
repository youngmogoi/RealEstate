"""Zoning ordinances (UDOs): download, manifest, and section/table extraction.

Run with `make udo`. Sources are under `udo` in config/ingest.yaml (official city/town sites
only); each has a folder under data/raw/udo/. A download goes to <folder>/<date>/ and gets a row
in data/raw/udo/manifest.csv (source URL, download date, amendment date read from the document,
sha256). A download identical to the previous snapshot is not kept. Sites that block scripts
are listed for downloading by hand: any PDF saved directly in <folder>/ (or <folder>/manual/)
is registered on the next run. A file identical to another jurisdiction's is skipped.

Extraction writes extract/sections.jsonl (one line per numbered section: number, title, first
page, text), extract/tables/*.csv, and extract/tables.csv: one row per table with a `clean` flag
and the reasons a table may not have extracted cleanly. For a dated snapshot, extract/ sits in
the snapshot folder; for a hand-downloaded file, in <folder>/extract/<file name>/.

Charlotte's tables come from its UDO web pages (same official site, clean HTML tables); every
number in them is checked against the text of the matching PDF article.
"""

import csv
import hashlib
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pymupdf
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_COLS = ["jurisdiction", "title", "file", "source_url", "downloaded", "amended", "amended_evidence",
                 "sha256", "bytes", "pages"]
# "10,000" is one number; "1,2,3,4" (footnote markers) is four
NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
VALUE_CELL = re.compile(r"^[\d,.\s%:/–-]*(?:MF\d?|SF|ft|feet|acres?)?[\d,.\s%:/–-]*$", re.I)


# ---------- download and manifest ----------

def fetch(url: str, timeout: int = 300) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_manifest(path: Path, rows: list[dict]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, MANIFEST_COLS)
        if new:
            w.writeheader()
        w.writerows(rows)


def folder(src: dict) -> str:
    return src.get("folder", src["jurisdiction"])


def parse_date(s: str) -> date | None:
    s = re.sub(r"(?<=[-/.])\s+", "", s.strip())  # "1- 9-2025" (Municode pads with a space)
    for fmt in ("%B %d, %Y", "%m.%d.%Y", "%m.%d.%y", "%m/%d/%Y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def amendment_date(pdf: Path, pattern: str, latest: bool = False, pages: int = 6,
                   not_after: date | None = None) -> tuple[str, str]:
    """(date as printed, the matching text) from the first pages of a PDF; blanks if not found.

    latest=True scans every page and keeps the newest date (for ordinances that print each
    section's revision date in its page header or history note instead of one date on the cover).
    Dates after `not_after` (the download date) are typos in the source and are skipped.
    """
    with pymupdf.open(pdf) as d:
        n = d.page_count if latest else min(pages, d.page_count)
        text = "\n".join(d[i].get_text() for i in range(n))
    if latest:
        found = [(parse_date(m.group(1)), m) for m in re.finditer(pattern, text)]
        found = [(d, m) for d, m in found if d]
        future = sorted({" ".join(m.group(0).split()) for d, m in found if not_after and d > not_after})
        found = [(d, m) for d, m in found if not (not_after and d > not_after)]
        if found:
            d, m = max(found, key=lambda x: x[0])
            evidence = f"newest of {len({x for x, _ in found})} dates: {' '.join(m.group(0).split())}"
            if future:
                evidence += f" (ignored future-dated, a typo in the source: {'; '.join(future)})"
            return m.group(1), evidence
        return "", ""
    m = re.search(pattern, text)
    return (m.group(1), " ".join(m.group(0).split())) if m else ("", "")


def charlotte_current(versions_url: str) -> tuple[str, str]:
    """(PDF URL, amendment date) of the version the site marks current."""
    html, _ = fetch(versions_url)
    text = html.decode("utf-8", "replace")
    m = re.search(r"Amended (\w+ \d{1,2}, \d{4}).{0,4000}?(https://admin\.charlotteudo\.org/assets/[0-9a-f-]+\.pdf)",
                  text, re.S)
    if not m or "Current Version" not in text:
        raise RuntimeError(f"could not find the current version on {versions_url}")
    return m.group(2), m.group(1)


def charlotte_articles(articles_url: str) -> list[str]:
    html, _ = fetch(articles_url)
    base = re.match(r"https?://[^/]+", articles_url).group(0)
    links = sorted(set(re.findall(r'href="(/articles/part-[^"#]+/article-\d+-[^"#]+)"', html.decode("utf-8"))),
                   key=lambda u: int(re.search(r"/article-(\d+)-", u).group(1)))
    return [base + u for u in links]


def snapshot(src: dict, out_dir: Path, today: date, manifest: list[dict]) -> list[dict]:
    """Download one source into a dated folder. Returns new manifest rows ([] if unchanged)."""
    j = src["jurisdiction"]
    snap = out_dir / folder(src) / today.isoformat()
    if any(r["jurisdiction"] == j and r["file"].startswith(f"{folder(src)}/{today}/") for r in manifest):
        print(f"  {j}: already downloaded today")
        return []
    files: list[tuple[Path, str]] = []
    snap.mkdir(parents=True, exist_ok=True)
    if src["kind"] == "charlotte":
        url, _ = charlotte_current(src["versions_url"])
        data, _ = fetch(url)
        (snap / "udo.pdf").write_bytes(data)
        files.append((snap / "udo.pdf", url))
        (snap / "articles").mkdir(exist_ok=True)
        for u in charlotte_articles(src["articles_url"]):
            n = re.search(r"/article-(\d+)-", u).group(1)
            p = snap / "articles" / f"article-{int(n):02d}.html"
            p.write_bytes(fetch(u)[0])
            files.append((p, u))
    else:
        data, ctype = fetch(src["url"])
        if not data.startswith(b"%PDF"):
            raise RuntimeError(f"{j}: {src['url']} returned {ctype}, not a PDF")
        p = snap / "ordinance.pdf"
        p.write_bytes(data)
        files.append((p, src["url"]))

    pdf = files[0][0]
    digest = sha256(pdf)
    prev = [r for r in manifest if r["jurisdiction"] == j and r["file"].endswith(pdf.name)]
    if prev and prev[-1]["sha256"] == digest:
        shutil.rmtree(snap)
        print(f"  {j}: unchanged since {prev[-1]['downloaded']}")
        return []
    rows = [manifest_row(src, p, url, today, out_dir) for p, url in files]
    for r in rows[1:]:  # article pages: the version the site marked current, same as the PDF
        r["amended"] = rows[0]["amended"]
        r["amended_evidence"] = article_evidence(rows[0], src)
    return rows


def article_evidence(pdf_row: dict, src: dict) -> str:
    return (f"current version on {src['versions_url']} when downloaded; same snapshot as {pdf_row['file']} "
            f"({pdf_row['amended_evidence']}); table numbers checked against that PDF")


def manifest_row(src: dict, path: Path, url: str, downloaded: date, out_dir: Path) -> dict:
    row = {"jurisdiction": src["jurisdiction"], "title": src["title"], "file": path.relative_to(out_dir).as_posix(),
           "source_url": url, "downloaded": downloaded.isoformat(), "amended": "", "amended_evidence": "",
           "sha256": sha256(path), "bytes": path.stat().st_size, "pages": ""}
    if path.suffix.lower() == ".pdf":
        row["amended"], row["amended_evidence"] = amendment_date(path, src["amended"], src.get("amended_latest", False),
                                                                 not_after=downloaded)
        with pymupdf.open(path) as d:
            row["pages"] = d.page_count
    return row


def print_url(pdf: Path) -> str:
    """The page URL a browser prints in the footer of a saved web page, if any."""
    with pymupdf.open(pdf) as d:
        m = re.search(r"https://\S+", d[0].get_text()) if d.page_count else None
    return m.group(0) if m else ""


def local_pdfs(src: dict, out_dir: Path) -> list[Path]:
    base = out_dir / folder(src)
    return sorted(base.glob("*.pdf")) + sorted((base / "manual").glob("*.pdf"))


def register_local(src: dict, out_dir: Path, manifest: list[dict]) -> list[dict]:
    """Manifest rows for hand-downloaded PDFs in the source's folder not yet recorded."""
    owner = {r["sha256"]: r for r in manifest}
    rows = []
    for p in local_pdfs(src, out_dir):
        digest = sha256(p)
        if digest in owner:
            r = owner[digest]
            if r["jurisdiction"] != src["jurisdiction"]:
                print(f"  {src['jurisdiction']}: skipped {p.name}: same file as {r['jurisdiction']} {r['file']}")
            continue
        url = print_url(p) or src.get("page_url", "")
        row = manifest_row(src, p, f"{url} (downloaded by hand)", datetime.fromtimestamp(p.stat().st_mtime).date(),
                           out_dir)
        rows.append(row)
        owner[digest] = row
    return rows


def extract_dir(pdf: Path) -> Path:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pdf.parent.name):
        return pdf.parent / "extract"
    return pdf.parent / "extract" / pdf.stem


# ---------- extraction ----------

def page_lines(page) -> list[str]:
    return [line.strip() for line in page.get_text().splitlines() if line.strip()]


def running_lines(pages: list[list[str]], edge: int = 5, share: float = 0.2) -> set[str]:
    """Header/footer lines: among the first/last `edge` lines of more than `share` of pages."""
    c = Counter(line for ls in pages for line in set(ls[:edge] + ls[-edge:]))
    return {line for line, n in c.items() if n > share * len(pages)}


def pdf_sections(pdf: Path, heading: str) -> list[dict]:
    """Split a PDF's text at numbered section headings. Running headers/footers are dropped.

    A heading whose title group matched nothing takes the next line as its title (ordinances
    that set the number and the title on separate lines).
    """
    pat = re.compile(heading)
    with pymupdf.open(pdf) as d:
        pages = [page_lines(d[i]) for i in range(d.page_count)]
    skip = running_lines(pages)
    lines = [(i, line) for i, ls in enumerate(pages, start=1) for line in ls if line not in skip]

    def heading(line: str):
        m = pat.match(line)
        return m if m and not re.search(r"\.{4,}", line) else None  # dot leaders = table of contents

    # Section numbers don't repeat in an ordinance; earlier copies are table-of-contents entries,
    # so only the last heading with a given number starts its section.
    remaining = Counter(m.group(1) for _, line in lines if (m := heading(line)))
    sections, cur = [], {"section": "", "title": "(front matter)", "page": 1, "lines": []}
    title_next = False
    for i, line in lines:
        if title_next:
            cur["title"], title_next = line, False
            continue
        m = heading(line)
        if m:
            remaining[m.group(1)] -= 1
        if m and remaining[m.group(1)] == 0:
            sections.append(cur)
            cur = {"section": m.group(1), "title": (m.group(2) or "").strip(), "page": i, "lines": []}
            title_next = not m.group(2)
        else:
            cur["lines"].append(line)
    sections.append(cur)
    return [{"section": s["section"], "title": s["title"], "page": s["page"], "text": "\n".join(s["lines"])}
            for s in sections]


def table_problems(rows: list[list]) -> list[str]:
    """Reasons a PDF table may not have extracted cleanly (empty list = looks clean)."""
    cells = [c for r in rows for c in r]
    if not rows or len(rows) < 2:
        return ["fewer than 2 rows"]
    out = []
    widths = {len(r) for r in rows}
    if len(widths) > 1:
        out.append("ragged rows")
    none_share = sum(c is None for c in cells) / len(cells)
    if none_share > 0.3:
        out.append(f"{none_share:.0%} merged/missing cells")
    text = [c for c in cells if isinstance(c, str)]
    if any(re.search(r"\d\n[.,]", c) for c in text):
        out.append("numbers split across lines (e.g. '13.5' read as '135\\n.')")
    if any(re.search(r"[A-Za-z]{25,}", c) for c in text):
        out.append("words run together")
    if max(widths) == 1:
        out.append("single column")
    return out


def pdf_tables(pdf: Path, sections: list[dict], out: Path) -> list[dict]:
    index = []
    starts = [(s["page"], s["section"]) for s in sections if s["section"]]
    with pymupdf.open(pdf) as d:
        for i in range(d.page_count):
            for k, t in enumerate(d[i].find_tables().tables, start=1):
                rows = t.extract()
                name = f"p{i + 1:04d}_{k}.csv"
                with (out / name).open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows([["" if c is None else c for c in r] for r in rows])
                first = " ".join(str(c) for c in (rows[0] if rows else []) if c)
                caption = re.search(r"Table\s+[\w.-]+[^\n]{0,80}", first)
                section = max((s for s in starts if s[0] <= i + 1), default=(0, ""))[1]
                problems = table_problems(rows)
                index.append({"file": f"tables/{name}", "page": i + 1, "section": section,
                              "caption": caption.group(0) if caption else "", "rows": len(rows),
                              "cols": max((len(r) for r in rows), default=0), "clean": not problems,
                              "problems": "; ".join(problems)})
    return index


def next_data_html(raw: str) -> str:
    """The HTML a Next.js page streams in self.__next_f.push([1, "..."]) chunks."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', raw)
    return "".join(json.loads(c) for c in chunks)


def html_grid(table) -> list[list[str]]:
    """An HTML table as a rectangular grid: a spanning cell's text fills every slot it covers."""
    grid: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(table.find_all("tr")):
        c = 0
        for cell in tr.find_all(["td", "th"]):
            while (r, c) in grid:
                c += 1
            text = cell.get_text(" ", strip=True)
            span_r, span_c = int(cell.get("rowspan") or 1), int(cell.get("colspan") or 1)
            for dr in range(span_r):
                for dc in range(span_c):
                    grid[(r + dr, c + dc)] = text
            c += span_c
    if not grid:
        return []
    n_rows, n_cols = max(r for r, _ in grid) + 1, max(c for _, c in grid) + 1
    return [[grid.get((r, c)) for c in range(n_cols)] for r in range(n_rows)]


def html_tables(article: Path, pdf_text: str, out: Path) -> list[dict]:
    """Tables from a Charlotte UDO article page, each number checked against the PDF article text."""
    soup = BeautifulSoup(next_data_html(article.read_text(encoding="utf-8")), "html.parser")
    pdf_nums = set(NUM.findall(pdf_text))
    index = []
    for k, t in enumerate(soup.find_all("table"), start=1):
        rows = html_grid(t)
        caption = rows[0][0] if rows and rows[0][0] and rows[0][0].startswith("Table") else ""
        if not caption:
            prev = t.find_previous(string=re.compile(r"Table \d+-\d+"))
            caption = prev.strip() if prev else ""
        heading = t.find_previous(string=re.compile(r"^\s*\d{1,2}\.\d{1,2}\s+[A-Z]"))
        body = [r for r in rows if set(r) != {caption}]
        problems = []
        if len(body) < 2:
            problems.append("fewer than 2 rows")
        if any(c is None for r in rows for c in r):
            problems.append("holes in the grid (cells missing)")
        # value cells (short, mostly numeric) must match the PDF text of the same article
        values = {n for r in body for c in r[1:] if c and len(c) < 30 and VALUE_CELL.match(c) for n in NUM.findall(c)}
        missing = sorted(values - pdf_nums, key=lambda n: float(n.replace(",", "")))
        if missing:
            problems.append("numbers not in the PDF text: " + ", ".join(missing[:10]))
        rows = [["" if c is None else c for c in r] for r in rows]
        widths = {len(r) for r in rows}
        name = f"{article.stem}_t{k:02d}.csv"
        with (out / name).open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        index.append({"file": f"tables/{name}", "page": "", "section": (heading or "").strip()[:60],
                      "caption": re.sub(r"\s+", " ", caption)[:120], "rows": len(rows),
                      "cols": max(widths, default=0), "clean": not problems, "problems": "; ".join(problems)})
    return index


def charlotte_article_text(pdf: Path) -> dict[int, str]:
    """PDF text per article, keyed by article number (from the 'Art. N.' running header)."""
    out: dict[int, list[str]] = {}
    with pymupdf.open(pdf) as d:
        for i in range(d.page_count):
            t = d[i].get_text()
            m = re.search(r"Art\. (\d+)\.", t)
            if m:
                out.setdefault(int(m.group(1)), []).append(t)
    return {k: "\n".join(v) for k, v in out.items()}


def extract(src: dict, pdf: Path) -> dict:
    """Write sections and tables for one ordinance PDF; return counts."""
    out = extract_dir(pdf)
    if out.exists():
        shutil.rmtree(out)
    (out / "tables").mkdir(parents=True)
    sections = pdf_sections(pdf, src["heading"])
    with (out / "sections.jsonl").open("w", encoding="utf-8") as f:
        for s in sections:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    if src["kind"] == "charlotte":
        texts = charlotte_article_text(pdf)
        index = []
        for a in sorted((pdf.parent / "articles").glob("article-*.html")):
            index += html_tables(a, texts.get(int(a.stem.split("-")[1]), ""), out / "tables")
    else:
        index = pdf_tables(pdf, sections, out / "tables")
    with (out / "tables.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["file", "page", "section", "caption", "rows", "cols", "clean", "problems"])
        w.writeheader()
        w.writerows(index)
    return {"sections": sum(1 for s in sections if s["section"]), "tables": len(index),
            "flagged": sum(not t["clean"] for t in index)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text(encoding="utf-8"))["udo"]
    out_dir = ROOT / cfg["out_dir"]
    manifest_path = out_dir / "manifest.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    blocked = []
    for src in cfg["sources"]:
        j, base = src["jurisdiction"], folder(src)
        manifest = read_manifest(manifest_path)
        rows, why = [], ""
        if src["kind"] == "manual":
            why = "site blocks scripts"
        else:
            try:
                rows = snapshot(src, out_dir, today, manifest)
            except Exception as e:  # a blocked or moved site: hand it to the user, keep going
                why = f"{type(e).__name__}: {e}"
                failed = out_dir / base / today.isoformat()
                if failed.exists() and not any(r["file"].startswith(f"{base}/{today}") for r in manifest):
                    shutil.rmtree(failed)
        rows += register_local(src, out_dir, manifest + rows)
        append_manifest(manifest_path, rows)
        manifest = read_manifest(manifest_path)
        pdfs = [r for r in manifest if r["jurisdiction"] == j and r["file"].endswith(".pdf")
                and (out_dir / r["file"]).exists()]
        snaps = [r for r in pdfs if re.match(rf"{re.escape(base)}/\d{{4}}-", r["file"])]
        current = snaps[-1:] + [r for r in pdfs if r not in snaps]
        # a blocked download is done once the same file was saved by hand
        wanted = urllib.parse.unquote(src.get("url", "").rsplit("/", 1)[-1])
        saved = any(Path(r["file"]).name == wanted for r in current)
        if why and not saved and not (src["kind"] == "manual" and current):
            blocked.append((src, why, len(current)))
        new = {r["file"] for r in rows}
        for r in current:
            pdf = out_dir / r["file"]
            label = f"{j} {pdf.name}"
            if r["file"] in new or not (extract_dir(pdf) / "tables.csv").exists():
                counts = extract(src, pdf)
                print(f"  {label}: amended {r['amended'] or 'NOT FOUND'}, {r['pages']} pages; "
                      f"{counts['sections']} sections, {counts['tables']} tables ({counts['flagged']} flagged)")
            else:
                print(f"  {label}: up to date ({r['downloaded']}, amended {r['amended']})")
    if blocked:
        print("\nDownload by hand (save the PDF into the folder shown, then rerun `make udo`):")
        for src, why, n in blocked:
            have = f"; {n} file(s) already there" if n else ""
            print(f"  {src['jurisdiction']:<13} {src.get('url') or src.get('page_url') or src.get('versions_url')}\n"
                  f"  {'':<13} -> {cfg['out_dir']}/{folder(src)}/   ({why}{have})")

if __name__ == "__main__":
    main()
