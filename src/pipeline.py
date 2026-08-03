"""pipeline.py — the single entry point: PDF in, validated record or review item out.

    PDF
     |- triage        -> text_layer / scanned / hybrid
     |- deterministic -> parcel id, version, dates, property facts (always runs)
     |- text_layer    -> llm_pass        (source "llm")
     |  scanned/hybrid-> ocr_pass -> llm (source "ocr")
     |- validate      -> certify, or route to the review queue with reasons

The branch is the whole point of the triage step. Everything downstream of it is
shared code, so a record's `source` and `extraction_confidence` are the only things
that reveal which way it came — which is exactly what a reviewer needs to know.

Deterministic extraction runs on every document regardless of branch: a scanned
document still has a filename, so document_id, document_date and the version
signal are recoverable even when the page content is unreadable. Partial evidence
beats none, and a record that says "I got the date but not the rent roll" is far
more useful than one that says nothing.

Usage:
    python src/pipeline.py <file-or-dir> [...]
    python src/pipeline.py fixtures/pdfs --json      # full records to stdout
Output: out/validated.json, out/review_queue.json, out/pipeline_report.json
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import collect, process as deterministic_process  # noqa: E402
from llm_pass import extract_tenants  # noqa: E402
from ocr_pass import process as ocr_process  # noqa: E402
from triage import triage_pdf  # noqa: E402
from validate import (check_count, check_document_fields, check_flags,  # noqa: E402
                      check_rows, confidence, record_sources)

OCR_CLASSES = yaml.safe_load(
    (ROOT / "spec" / "om_extraction_spec.yaml").read_text())["ocr"]["classifications"]


def run_one(path: Path) -> dict:
    triage = triage_pdf(path)
    if triage.get("status") != "ok":
        return {"file": path.name, "status": "review", "classification": None,
                "route": None, "extraction_confidence": "none", "tenants": [],
                "issues": [{"code": "read_error",
                            "detail": triage.get("error", "unreadable"),
                            "blocking": True}]}

    cls = triage["classification"]
    det = deterministic_process(path)

    # The branch. A hybrid document goes down OCR because the pages that matter
    # are the ones without a text layer; if its rent roll happened to be on a text
    # page, the OCR pass still reads the whole document and finds it.
    if cls in OCR_CLASSES:
        rows_result, route = ocr_process(path), "ocr"
    else:
        rows_result, route = extract_tenants(path), "text"

    rows = rows_result.get("tenants", [])
    rows_ok = rows_result.get("status") == "ok"

    record = {k: v for k, v in det.items() if k not in ("file", "status")}
    record["tenant_count"] = len(rows) if rows_ok else None

    issues = (check_document_fields(record)
              + check_flags(record)
              + check_rows(rows, rows_result.get("status"), rows_result.get("reason"))
              + check_count(record, rows))
    blocking = [i for i in issues if i["blocking"]]

    sources = record_sources(record, rows_result.get("source") if rows_ok else None)

    return {
        "file": path.name,
        "status": "review" if blocking else "ok",
        "classification": cls,
        "route": route,
        "ocr_backend": rows_result.get("ocr_backend"),
        "extraction_confidence": confidence(sources) if not blocking else "none",
        "sources": sources,
        **record,
        "tenants": rows,
        "issues": issues,
    }


def main(argv):
    want_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    results = [run_one(p) for p in collect(args)]

    clean = [r for r in results if r["status"] == "ok"]
    review = [r for r in results if r["status"] != "ok"]

    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    (outdir / "validated.json").write_text(json.dumps(clean, indent=2))
    (outdir / "review_queue.json").write_text(json.dumps(review, indent=2))

    by_conf = {}
    for r in clean:
        by_conf[r["extraction_confidence"]] = by_conf.get(r["extraction_confidence"], 0) + 1
    report = {
        "documents": len(results),
        "validated": len(clean),
        "review": len(review),
        "rows_extracted": sum(len(r["tenants"]) for r in clean),
        "by_confidence": by_conf,
        "by_route": {rt: sum(1 for r in results if r.get("route") == rt)
                     for rt in ("text", "ocr")
                     if any(r.get("route") == rt for r in results)},
        "review_reasons": [{"file": r["file"],
                            "codes": [i["code"] for i in r["issues"] if i["blocking"]]}
                           for r in review],
    }
    (outdir / "pipeline_report.json").write_text(json.dumps(report, indent=2))

    if want_json:
        print(json.dumps(results, indent=2))
    else:
        w = max((len(r["file"]) for r in results), default=10)
        for r in results:
            print(f"{r['file']:<{w}}  {r['classification'] or '-':<10} "
                  f"{r['route'] or '-':<5} {r['status']:<7} "
                  f"conf={r['extraction_confidence']:<7} rows={len(r['tenants'])}")
        print(f"\n{len(clean)} validated, {len(review)} to review, "
              f"{report['rows_extracted']} rows -> out/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
