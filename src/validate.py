"""validate.py — merge the passes, prove the record, route breaks to review.

The last gate before a record is allowed to represent a real document. It takes
the deterministic pass (regex evidence) and the LLM pass (rent roll rows), merges
them into ONE record, and either certifies it or sends it to the review queue with
the specific reasons.

Design rules carried through the whole pipeline:
  - Fail closed. A record with any blocking issue never appears in the clean
    output. It goes to the review queue with every issue named. No partial
    certification, no "mostly fine".
  - Blank is not unreadable. Every field keeps {value, source, found}, so a null
    that was legitimately absent is distinguishable from one that could not be read.
  - Config-driven. Field checks, blocking-ness, and the confidence rubric all live
    in spec/om_extraction_spec.yaml -> validation:. Adding a check is a YAML edit.
  - No repair. Validation rejects; it never rewrites a value to make it pass. A
    value the model got wrong must stay wrong and visible.

Confidence is computed WORST-SOURCE-WINS: a record whose rows came from OCR plus a
model is "low" even if every regex field around it was perfect, because the
weakest evidence in the record is what a reviewer actually has to trust.

Usage:
    python src/validate.py <file-or-dir> [...]
    LLM_MOCK=1 python src/validate.py fixtures/pdfs      # model-free path
Output: JSON to stdout; writes out/validated.json and out/review_queue.json
"""
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import collect, process as deterministic_process  # noqa: E402
from llm_pass import extract_tenants  # noqa: E402

SPEC = yaml.safe_load(
    (ROOT / "spec" / "om_extraction_spec.yaml").read_text()
)["validation"]

# Sources ranked weakest -> strongest.
SOURCE_RANK = SPEC["source_rank"]

# Compiled once. A row whose tenant_name matches any of these is not a tenant.
NON_TENANT = [re.compile(p, re.IGNORECASE) for p in SPEC["non_tenant_row_patterns"]]


def _issue(code, detail, blocking):
    return {"code": code, "detail": detail, "blocking": blocking}


# --------------------------------------------------------------------------
# field-level checks — every rule comes from the spec
# --------------------------------------------------------------------------
def check_document_fields(record: dict) -> list[dict]:
    """Apply spec validation.document_checks to the {value, source, found} fields."""
    issues = []
    for rule in SPEC["document_checks"]:
        field = rule["field"]
        node = record.get(field)
        if node is None:
            issues.append(_issue("field_missing", f"{field} absent from record", True))
            continue

        value, found = node.get("value"), node.get("found")

        if not found or value is None:
            # Absent is only an error when the spec says the field is required.
            if rule.get("required"):
                issues.append(_issue("required_field_empty",
                                     f"{field} is required but not found", True))
            continue

        pattern = rule.get("pattern")
        if pattern and not re.fullmatch(pattern, str(value)):
            issues.append(_issue("pattern_mismatch",
                                 f"{field}={value!r} fails {pattern}",
                                 rule.get("blocking", True)))
    return issues


def check_flags(record: dict) -> list[dict]:
    """Deterministic flags (e.g. multiple_parcel_ids) are blocking by spec list."""
    issues = []
    for field, node in record.items():
        if not isinstance(node, dict):
            continue
        for flag in node.get("flags", []) or []:
            code = flag.split(":", 1)[0]
            issues.append(_issue(code, f"{field}: {flag}",
                                 code in SPEC["blocking_flags"]))
    return issues


def check_rows(rows: list[dict], row_status: str, row_reason: str | None) -> list[dict]:
    """Row-level checks, the non-tenant guard, and the duplicate guard."""
    issues = []

    if row_status != "ok":
        # The LLM pass already failed closed. Carry its reason forward as blocking;
        # a document with no recoverable table is exactly what review is for.
        return [_issue("rows_unavailable", row_reason or "llm pass returned no rows", True)]

    for i, row in enumerate(rows):
        for rule in SPEC["row_checks"]:
            field = rule["field"]
            value = row.get(field)
            if value is None:
                if rule.get("required"):
                    issues.append(_issue("row_field_empty",
                                         f"row[{i}].{field} is null", True))
                continue
            if not re.fullmatch(rule["pattern"], str(value)):
                issues.append(_issue("row_pattern_mismatch",
                                     f"row[{i}].{field}={value!r} fails {rule['pattern']}",
                                     rule.get("blocking", True)))

        # The subtotal guard. Total / summary / space-category rows have the exact
        # shape of a data row and often carry a full set of figures, so nothing
        # structural distinguishes them — only the label does.
        #
        # This REJECTS the record rather than dropping the row. Silently deleting
        # the row would hide a real parsing failure, and the failure it guards
        # against is severe: a "TOTAL LEASED" line emitted as a tenant becomes a
        # several-hundred-thousand-square-foot phantom lease in whatever consumes
        # this output, at a blended rent that looks entirely plausible.
        name = row.get("tenant_name") or ""
        for pat in NON_TENANT:
            if pat.search(name):
                issues.append(_issue("non_tenant_row",
                                     f"row[{i}].tenant_name={name!r} is a subtotal, "
                                     f"summary, or space category, not a tenant "
                                     f"(matched {pat.pattern!r})", True))
                break

    # Duplicate rows mean the model double-emitted. Keyed on the PAIR, because one
    # tenant occupying several suites at different terms is a real and common
    # pattern that must never be collapsed — the multiple rows ARE the lease
    # structure. Keying on tenant_name alone would silently merge them.
    seen = {}
    for i, row in enumerate(rows):
        key = (row.get("tenant_name"), row.get("suite"), row.get("rentable_sf"))
        if key in seen:
            issues.append(_issue("duplicate_row",
                                 f"row[{i}] duplicates row[{seen[key]}] on {key}", True))
        seen[key] = i

    return issues


def check_count(record: dict, rows: list[dict]) -> list[dict]:
    """tenant_count must equal the actual row count."""
    declared = record.get("tenant_count")
    if declared is None:
        return []
    if declared != len(rows):
        return [_issue("tenant_count_mismatch",
                       f"tenant_count={declared} but {len(rows)} rows present", True)]
    return []


# --------------------------------------------------------------------------
# confidence — worst contributing source wins
# --------------------------------------------------------------------------
def record_sources(record: dict, rows_source: str | None) -> list[str]:
    sources = []
    for node in record.values():
        if isinstance(node, dict) and node.get("found") and node.get("source"):
            sources.append(node["source"])
    if rows_source:
        sources.append(rows_source)
    return sorted(set(sources))


def confidence(sources: list[str]) -> str:
    """The weakest source present decides the level; unknown sources rank weakest."""
    if not sources:
        return "none"
    ranks = [SOURCE_RANK.index(s) if s in SOURCE_RANK else 0 for s in sources]
    weakest = SOURCE_RANK[min(ranks)]
    return SPEC["confidence_by_source"][weakest]


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def validate_file(path: Path) -> dict:
    det = deterministic_process(path)
    if det.get("status") != "ok":
        return {"file": path.name, "status": "review",
                "issues": [_issue("read_error",
                                  "deterministic pass could not read file", True)],
                "extraction_confidence": "none", "tenants": []}

    llm = extract_tenants(path)
    rows = llm.get("tenants", [])
    rows_source = llm.get("source") if llm.get("status") == "ok" else None

    record = {k: v for k, v in det.items() if k not in ("file", "status")}
    record["tenant_count"] = len(rows) if llm.get("status") == "ok" else None

    issues = (check_document_fields(record)
              + check_flags(record)
              + check_rows(rows, llm.get("status"), llm.get("reason"))
              + check_count(record, rows))

    blocking = [i for i in issues if i["blocking"]]
    sources = record_sources(record, rows_source)

    return {
        "file": path.name,
        "status": "review" if blocking else "ok",
        "extraction_confidence": confidence(sources) if not blocking else "none",
        "sources": sources,
        **record,
        "tenants": rows,
        "issues": issues,
    }


def main(args):
    results = [validate_file(p) for p in collect(args)]
    clean = [r for r in results if r["status"] == "ok"]
    review = [r for r in results if r["status"] != "ok"]

    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    (outdir / "validated.json").write_text(json.dumps(clean, indent=2))
    (outdir / "review_queue.json").write_text(json.dumps(review, indent=2))

    print(json.dumps(results, indent=2))
    print(f"\n{len(clean)} validated, {len(review)} routed to review "
          f"-> out/review_queue.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
