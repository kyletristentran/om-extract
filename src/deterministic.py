"""deterministic.py: the zero-LLM extraction pass.

Extracts everything reliably regex-able from an Offering Memorandum:
  - Assessor's / tax parcel number (deduped across pages; >1 distinct -> flag)
  - Document version signal from filename AND body (precedence-ordered)
  - Document date -> document_id (OM-YYYY-MM[-DD]) from the filename
  - Labelled property facts (address, county, year built, RSF, floors)
  - Listing-contact name candidates from body text (evidence only)

Every extracted field carries {value, source, found}. Blank is not the same as
unreadable, and a consumer needs to be able to tell them apart: a document that
genuinely prints no parcel number is a different situation from one whose parcel
number could not be read.

Usage:
    python src/deterministic.py <file-or-dir> [...]
Output: JSON to stdout; also writes out/deterministic_results.json
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

import pdfplumber
import yaml

ROOT = Path(__file__).parent.parent
SPEC = yaml.safe_load(
    (ROOT / "spec" / "om_extraction_spec.yaml").read_text()
)["deterministic"]


def _field(value, source, found=True):
    return {"value": value, "source": source, "found": found}


def _null_field():
    return {"value": None, "source": None, "found": False}


# --------------------------------------------------------------------------
# text readers: the two are not interchangeable
# --------------------------------------------------------------------------
def extract_text(path: Path) -> str | None:
    """Collapsed text. Stable and cheap. Right for regex, wrong for tables."""
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return None


def extract_text_layout(path: Path) -> str | None:
    """Text with horizontal positions preserved (pdfplumber layout mode).

    Collapsing runs of spaces destroys column identity. A rent roll row with an
    empty lease-expiration cell comes out of the default extractor as

        Acme Holdings LLC 12,400 2.5% 28.75

    and nothing in that string says whether 28.75 is the in-place rent or the
    market rent. Layout mode keeps the gap, so the empty cell is visible as an
    empty cell.

    Used only for the rent roll region. Everything else reads the collapsed text,
    which is more stable for label matching.
    """
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages)
    except Exception:
        return None


# --------------------------------------------------------------------------
# extractors
# --------------------------------------------------------------------------
def parcel_id(text: str) -> dict:
    """Never picks.

    Dedupes parcel numbers across pages. On more than one distinct value it
    returns found=False plus a flag, because a property spanning several parcels
    is a real and common situation, and in that situation no single value is
    "the" parcel ID. Choosing one would be inventing a fact.

    This is the fail-closed pattern the rest of the pipeline copies.
    """
    pat = re.compile(SPEC["parcel_id"]["pattern"], re.IGNORECASE)
    ids = list(dict.fromkeys(m.group(1).strip() for m in pat.finditer(text)))
    if not ids:
        return {**_null_field(), "flags": []}
    if len(ids) > 1:
        return {"value": None, "source": "body_regex", "found": False,
                "flags": [f"multiple_parcel_ids:{','.join(ids)}"]}
    return {**_field(ids[0], "body_regex"), "flags": []}


def version_signal(filename: str, text: str) -> dict:
    """Filename wins outright over body; within each source, list order is precedence.

    The asymmetry is deliberate. Body prose in almost any OM contains the word
    "draft" or "preliminary" somewhere in a disclaimer, and a body match must not
    be allowed to relabel a document whose filename says Final.
    """
    for source, hay in (("filename", filename), ("body", text)):
        for sig in SPEC["version_signals"]:
            if re.search(sig["pattern"], hay, re.IGNORECASE):
                return _field(sig["label"], source)
    return _null_field()


# Which capture group is year/month/day for each declared order. A pattern entry
# names its order explicitly; parsing never depends on list position, so a new
# pattern can be inserted anywhere in the spec without silently rewiring the rest.
_DATE_ORDERS = {"ymd": (0, 1, 2), "mdy": (2, 0, 1), "dmy": (2, 1, 0)}
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def document_date(filename: str) -> dict:
    """Parse the document date out of the filename.

    A month-and-year filename ("Offering Memorandum Sept 2019") yields a
    YYYY-MM date and a YYYY-MM id. It does NOT get padded to the 1st or the last
    of the month: the day is not in the source, and a downstream consumer that
    sees 2019-09-01 has no way to know it was invented here.
    """
    id_format = SPEC["document_date"].get("document_id_format", "OM-{iso_date}")

    for entry in SPEC["document_date"]["filename_patterns"]:
        order = entry.get("order", "ymd")
        m = re.search(entry["pattern"], filename)
        if not m:
            continue

        if order == "month_year":
            mon = _MONTHS.get(m.group(1).lower()[:4]) or _MONTHS.get(m.group(1).lower()[:3])
            if not mon:
                continue
            iso = f"{int(m.group(2)):04d}-{mon:02d}"
        else:
            idx = _DATE_ORDERS.get(order)
            if idx is None or len(m.groups()) < 3:
                continue          # unknown order key: skip the entry, do not crash
            g = m.groups()
            y, mo, dy = int(g[idx[0]]), int(g[idx[1]]), int(g[idx[2]])
            if y < 100:
                y += 2000
            try:
                iso = date(y, mo, dy).isoformat()
            except ValueError:
                continue          # 13.40.2019 is not a date; keep looking

        return {"date": _field(iso, "filename"),
                "document_id": _field(id_format.format(iso_date=iso), "filename")}

    return {"date": _null_field(), "document_id": _null_field()}


def labeled_fields(text: str) -> dict:
    """Single-capture regexes over the Property Details / Site Description block.

    Adding a property fact is a row in spec.deterministic.labeled_fields; there
    is nothing to change here.
    """
    out = {}
    for rule in SPEC.get("labeled_fields", []):
        m = re.search(rule["pattern"], text)
        value = m.group(1).strip() if m else None
        out[rule["name"]] = _field(value, "body_regex") if value else _null_field()
    return out


def contact_candidates(text: str) -> dict:
    names = []
    for raw in SPEC["contact_block"]["patterns"]:
        names += re.findall(raw, text, re.IGNORECASE)
    names = list(dict.fromkeys(n.strip() for n in names))
    return _field(names, "body_regex") if names else _null_field()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def process(path: Path) -> dict:
    text = extract_text(path)
    if text is None:
        return {"file": path.name, "status": "read_error"}
    ddate = document_date(path.name)
    return {
        "file": path.name,
        "status": "ok",
        "document_id": ddate["document_id"],
        "document_date": ddate["date"],
        "document_version": version_signal(path.name, text),
        "parcel_id": parcel_id(text),
        **labeled_fields(text),
        "contact_candidates": contact_candidates(text),
    }


def collect(args):
    for a in args:
        p = Path(a)
        if p.is_dir():
            yield from sorted(p.glob("**/*.pdf"))
        elif p.suffix.lower() == ".pdf":
            yield p


if __name__ == "__main__":
    results = [process(p) for p in collect(sys.argv[1:])]
    out = ROOT / "out" / "deterministic_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
