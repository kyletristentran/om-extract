"""run_eval_ocr.py — score the OCR branch against the scanned fixtures.

Same three metrics as the text-path eval, restricted to documents flagged
`ocr_required` in ground truth. Those are the documents the text path is required
to REFUSE; this is where their rows actually have to be recovered.

Same >=98% gate. An OCR branch that scores well below the text branch is not
"good enough for scans" — it is a branch producing numbers nobody should trust,
and the honest move is to leave those documents in the review queue.

If no OCR backend is installed the harness says so and exits 0 without claiming a
pass. Running without OCR is a supported configuration; silently reporting a green
gate for a code path that never executed is not.

    python eval/run_eval_ocr.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_pass import FIELD_NAMES  # noqa: E402
from ocr_pass import process as ocr_process  # noqa: E402
from validate import NON_TENANT  # noqa: E402

GT = json.loads((ROOT / "fixtures" / "ground_truth.json").read_text())
PDFS = ROOT / "fixtures" / "pdfs"
NUMERIC_FIELDS = {"rentable_sf", "in_place_base_rent", "market_rent"}
SCORED = [f for f in FIELD_NAMES if f != "suite"]

TARGETS = {k: v for k, v in GT.items() if v.get("ocr_required")}


def norm(field, v):
    if v is None:
        return None
    s = str(v).strip()
    if field in NUMERIC_FIELDS:
        return s.replace("$", "").replace(",", "").replace(" ", "")
    return re.sub(r"\s+", " ", s).casefold()


if not TARGETS:
    print("no fixtures flagged ocr_required — nothing to score")
    sys.exit(0)

rows_expected = rows_found = 0
fields_total = fields_correct = 0
docs_valid = 0
non_tenant_total = 0
detail, failures, unavailable = [], [], []

for fname, gt in sorted(TARGETS.items()):
    gt_rows = gt.get("tenants", [])
    res = ocr_process(PDFS / fname)

    reason = res.get("reason", "")
    if res.get("status") != "ok" and reason.startswith("ocr_backend_unavailable"):
        unavailable.append((fname, reason))
        continue

    rows_expected += len(gt_rows)
    fields_total += len(gt_rows) * len(SCORED)

    if res.get("status") != "ok":
        detail.append((fname, f"0/{len(gt_rows)} rows", f"REVIEW {reason[:50]}"))
        failures.append((fname, reason))
        continue

    docs_valid += 1
    got_by_key = {(norm("tenant_name", r.get("tenant_name")),
                   norm("rentable_sf", r.get("rentable_sf"))): r
                  for r in res["tenants"]}

    matched = 0
    for g in gt_rows:
        key = (norm("tenant_name", g["tenant_name"]),
               norm("rentable_sf", g["rentable_sf"]))
        got = got_by_key.get(key)
        if got is None:
            failures.append((fname, f"missing row {g['tenant_name']} / {g['rentable_sf']}"))
            continue
        matched += 1
        for f in SCORED:
            if norm(f, got.get(f)) == norm(f, g.get(f)):
                fields_correct += 1
            else:
                failures.append((fname, f"{g['tenant_name'][:28]}.{f}: "
                                        f"exp={g.get(f)!r} got={got.get(f)!r}"))
    rows_found += matched

    bad = [r.get("tenant_name") for r in res["tenants"]
           if any(p.search(r.get("tenant_name") or "") for p in NON_TENANT)]
    non_tenant_total += len(bad)

    detail.append((fname, f"{matched}/{len(gt_rows)} rows  "
                          f"backend={res.get('ocr_backend')}",
                   "PASS" if matched == len(gt_rows) else "CHECK"))

if unavailable:
    print("OCR backend not installed — the branch was not exercised.\n")
    for fname, reason in unavailable:
        print(f"  {fname}: {reason[:110]}")
    print("\nInstall one with:  pip install -r requirements-ocr.txt")
    print("Scanned documents currently route to out/review_queue.json, which is a")
    print("supported configuration. NOT REPORTING A PASS for an unrun code path.")
    sys.exit(0)


def pct(a, b):
    return (100.0 * a / b) if b else 0.0


recall = pct(rows_found, rows_expected)
accuracy = pct(fields_correct, fields_total)

w = max(len(d[0]) for d in detail)
for name, note, res in detail:
    print(f"{name:<{w}}  {note:<38} {res}")

if failures:
    print(f"\n--- {len(failures)} failure(s) ---")
    for fname, why in failures[:25]:
        print(f"  {fname[:44]}: {why}")

print(f"\nrow recall      {recall:6.1f}%   ({rows_found}/{rows_expected} rows)")
print(f"field accuracy  {accuracy:6.1f}%   ({fields_correct}/{fields_total} fields)")
print(f"documents valid {docs_valid}/{len(TARGETS)}")
print(f"subtotals emitted as tenants: {non_tenant_total}")

gate = recall >= 98.0 and accuracy >= 98.0 and non_tenant_total == 0
print(f"\nGATE (>=98% recall AND accuracy, 0 subtotals): {'PASS' if gate else 'FAIL'}")
sys.exit(0 if gate else 1)
