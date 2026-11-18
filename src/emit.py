"""emit.py: turn a validated pipeline record into the typed output contract.

This is the last mile, and it is a separate module on purpose. Everything upstream
produces the pipeline's own shape, which is built for extraction and review: every
value is kept AS PRINTED so it can be compared against the page. The output
contract is built for consumption: numbers are numbers, dates are ISO.

The two disagree in ways that are easy to get wrong, so the translation lives here
explicitly rather than being smeared through the passes:

  money    "26.06" / "$1,975" -> 26.06 / 1975. Plain numbers, null if absent.
  sf       "54,330" -> 54330
  percent  "11.1%" -> 11.1 (not 0.111; the source printed a percent, so the
           number carries percent units; rescaling silently is how a rent roll
           ends up claiming a tenant occupies 0.111% of a building)
  term     "Dec-26" -> "2026-12" with precision "month". The day is not in the
           source and is not invented.
  grain    one document object with a nested tenants[], every child carrying its
           parent's document_id. Enforced in code: a schema cannot compare fields.

Usage:
    python src/emit.py <file-or-dir> [...]
Output: out/om_records.json (contract records) + out/emit_errors.json
"""
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import collect  # noqa: E402
from pipeline import run_one  # noqa: E402

SCHEMA = json.loads((ROOT / "spec" / "om_record_schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

VERSION_ENUM = {"Final", "Revised", "Updated", "Draft", "Preliminary"}

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# Two-digit year pivot. Rent rolls carry both expired and future leases, so the
# window has to straddle "now" rather than assume the future. 70 puts 1970-1999
# in the past century and 2000-2069 in this one, which covers every lease term a
# commercial rent roll realistically prints.
_YY_PIVOT = 70


# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------
def to_number(v):
    """'26.06' -> 26.06, '$1,975,000' -> 1975000, '' / None / junk -> None.

    Returns None for anything that does not cleanly parse. That is deliberate: a
    value the extractor could not read must not arrive downstream as 0, which
    would silently become a real in-place rent of zero dollars, a number that
    looks like data and quietly drags every average that touches it.
    """
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s or not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return None
    return float(s) if "." in s else int(s)


def to_int(v):
    n = to_number(v)
    return int(n) if isinstance(n, (int, float)) and float(n).is_integer() else None


def to_percent(v):
    """'11.1%' -> 11.1. Units are preserved, not rescaled to a fraction."""
    if v is None:
        return None
    s = str(v).strip().rstrip("%").replace(" ", "")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", s):
        return None
    return float(s)


def to_iso_term(v) -> tuple[str | None, str]:
    """Lease expiration as printed -> (ISO string or None, precision).

    Returns the precision the SOURCE supported, never more:
      'Dec-26'      -> ('2026-12',    'month')   the day is not in the document
      '12/31/2026'  -> ('2026-12-31', 'day')
      '2026-12-31'  -> ('2026-12-31', 'day')
      'MTM'         -> (None,         'month_to_month')
      None          -> (None,         'absent')

    'month_to_month' and 'absent' are different states and are kept apart. An MTM
    lease has no expiration; a blank cell has an unknown one. Anyone modeling
    rollover needs to be able to tell those apart, and a single null cannot.
    """
    if v is None:
        return None, "absent"
    s = str(v).strip()
    if not s:
        return None, "absent"
    if re.fullmatch(r"(?i)M-?T-?M", s):
        return None, "month_to_month"

    m = re.fullmatch(r"(?i)([A-Za-z]{3})-(\d{2})", s)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            return None, "absent"
        yy = int(m.group(2))
        year = 1900 + yy if yy >= _YY_PIVOT else 2000 + yy
        return f"{year:04d}-{mon:02d}", "month"

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr = 1900 + yr if yr >= _YY_PIVOT else 2000 + yr
        try:
            from datetime import date
            return date(yr, mo, dy).isoformat(), "day"
        except ValueError:
            return None, "absent"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s, "day"

    return None, "absent"


# --------------------------------------------------------------------------
# evidence confidence
# --------------------------------------------------------------------------
def evidence_confidence(record: dict) -> str:
    """Confidence in the document evidence, which is a different question from how
    cleanly the rows were read.

      High   a parcel ID or a listing contact block was found in the body
      Medium a filename version signal only
      Low    inferred

    A scanned document whose parcel number came through OCR still scores High
    here, and carries its reading risk in row_confidence instead. Collapsing the
    two axes would make that document indistinguishable from one where the
    evidence itself is weak, and those call for completely different review.
    """
    parcel = record.get("parcel_id", {}) or {}
    if parcel.get("found") and parcel.get("value"):
        return "High"
    contacts = record.get("contact_candidates", {}) or {}
    if contacts.get("found") and contacts.get("value"):
        return "High"
    version = record.get("document_version", {}) or {}
    if version.get("found") and version.get("value"):
        return "Medium"
    return "Low"


def build_notes(record: dict) -> str | None:
    """Audit trail: what a reviewer needs to see without opening the PDF."""
    notes = []
    for issue in record.get("issues", []):
        notes.append(f"{'BLOCKING' if issue['blocking'] else 'note'}: {issue['detail']}")
    if record.get("route") == "ocr" and record.get("tenants"):
        notes.append(f"rows recovered by OCR ({record.get('ocr_backend')}); "
                     f"values are transcribed, not read from a text layer")
    return " | ".join(notes) if notes else None


# --------------------------------------------------------------------------
# the translation
# --------------------------------------------------------------------------
def _val(record, key):
    return (record.get(key) or {}).get("value")


def to_contract(record: dict) -> dict:
    document_id = _val(record, "document_id")
    version = _val(record, "document_version")
    contacts = _val(record, "contact_candidates")

    rows = []
    for r in record.get("tenants", []):
        iso_term, precision = to_iso_term(r.get("lease_expiration"))
        rows.append({
            # Hard rule: every child carries the parent's document_id. Set here
            # from the parent rather than trusted from the row, so the two cannot
            # disagree in the first place.
            "document_id": document_id,
            "tenant_name": r.get("tenant_name"),
            "suite": r.get("suite"),
            "rentable_sf": to_int(r.get("rentable_sf")),
            "pct_of_building": to_percent(r.get("pct_of_building")),
            "lease_expiration": iso_term,
            "lease_expiration_precision": precision,
            "in_place_base_rent": to_number(r.get("in_place_base_rent")),
            "market_rent": to_number(r.get("market_rent")),
        })

    return {
        "document_id": document_id,
        "document_date": _val(record, "document_date"),
        "document_version": version if version in VERSION_ENUM else None,
        "property_name": _val(record, "property_name"),
        "property_address": _val(record, "property_address"),
        "county": _val(record, "county"),
        "year_built": _val(record, "year_built"),
        "floors": to_int(_val(record, "floors")),
        "total_rentable_sf": to_int(_val(record, "total_rentable_sf")),
        "parcel_id": _val(record, "parcel_id"),
        "tenant_count": len(rows),
        "tenants": rows,
        "contact_names": "; ".join(contacts) if isinstance(contacts, list) else contacts,
        "extraction_confidence": evidence_confidence(record),
        "row_confidence": {"high": "High", "medium": "Medium",
                           "low": "Low", "none": "Low"}.get(
                               record.get("extraction_confidence"), "Low"),
        "source_file": record.get("file"),
        "classification": record.get("classification"),
        "route": record.get("route"),
        "notes": build_notes(record),
    }


def check_invariants(rec: dict) -> list[str]:
    """The two hard rules a jsonschema structurally cannot express."""
    errs = []
    if rec["tenant_count"] != len(rec["tenants"]):
        errs.append(f"tenant_count {rec['tenant_count']} != {len(rec['tenants'])} rows")
    bad = [t["document_id"] for t in rec["tenants"]
           if t["document_id"] != rec["document_id"]]
    if bad:
        errs.append(f"child document_id mismatch: {sorted(set(bad))} "
                    f"!= {rec['document_id']}")
    return errs


def emit(path: Path) -> tuple[dict | None, dict | None]:
    """Returns (contract_record, error). Exactly one of the two is None."""
    record = run_one(path)

    # A record the pipeline routed to review does not get emitted, full stop.
    #
    # This gate is not redundant with the schema below, and leaving it out was a
    # real bug: the contract schema is structural, so a rent roll containing a
    # "TOTAL OFFICE LEASED" row passes it with correct types and correct
    # required fields, and ships a 439,489 SF phantom lease at a plausible
    # blended rent. The pipeline had already caught it and said so. Emitting
    # anyway would mean the review queue is advisory, and a fail-closed pipeline
    # with an advisory last mile is not fail-closed.
    blocking = [i for i in record.get("issues", []) if i.get("blocking")]
    if blocking:
        return None, {"file": record.get("file"),
                      "errors": [f"{i['code']}: {i['detail']}" for i in blocking],
                      "pipeline_status": record.get("status")}

    rec = to_contract(record)
    errs = [e.message for e in VALIDATOR.iter_errors(rec)] + check_invariants(rec)
    if errs:
        # Fail closed at the boundary too. A record that does not satisfy the
        # contract is not emitted in a partial form; it is reported with every
        # reason, so the fix goes into the spec rather than into a patch here.
        return None, {"file": record.get("file"), "errors": errs,
                      "pipeline_status": record.get("status")}
    return rec, None


def main(argv):
    ok, bad = [], []
    for p in collect(argv):
        rec, err = emit(p)
        (bad if err else ok).append(err or rec)

    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    (outdir / "om_records.json").write_text(json.dumps(ok, indent=2))
    (outdir / "emit_errors.json").write_text(json.dumps(bad, indent=2))

    w = max((len(r["document_id"] or "") for r in ok), default=14)
    for r in ok:
        sf = sum(t["rentable_sf"] or 0 for t in r["tenants"])
        print(f"{r['document_id']:<{w}}  {r['tenant_count']:>3} tenants  "
              f"{sf:>9,} SF  evidence={r['extraction_confidence']:<6} "
              f"rows={r['row_confidence']}")
    for b in bad:
        print(f"{b['file']}: REJECTED: {'; '.join(b['errors'][:3])}")

    print(f"\n{len(ok)} contract records -> out/om_records.json"
          + (f", {len(bad)} rejected -> out/emit_errors.json" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
