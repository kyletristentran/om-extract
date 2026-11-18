"""run_eval.py: score triage + deterministic extraction against ground truth.

Phase-1 scope only: classification, document_id/date, parcel ID (including the
multiple-parcel flag case), version signal, property address, RSF, and whether a
listing contact block was found.

This gate is the cheap one and it runs first. If it fails, the problem is the
environment or the fixtures, not the model. Stop and fix it before looking at
anything downstream.

    python eval/run_eval.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import process  # noqa: E402
from triage import triage_pdf      # noqa: E402

GT = json.loads((ROOT / "fixtures" / "ground_truth.json").read_text())
PDFS = ROOT / "fixtures" / "pdfs"

rows, passed, total = [], 0, 0


def check(name, field, expected, actual):
    global passed, total
    ok = expected == actual
    total += 1
    passed += ok
    rows.append((name, field, "PASS" if ok else f"FAIL exp={expected!r} got={actual!r}"))


for fname, gt in sorted(GT.items()):
    path = PDFS / fname
    t = triage_pdf(path)
    d = process(path)

    check(fname, "classification", gt["classification"], t.get("classification"))
    check(fname, "document_id", gt["document_id"], d["document_id"]["value"])
    check(fname, "document_date", gt["document_date"], d["document_date"]["value"])
    check(fname, "document_version", gt["document_version"], d["document_version"]["value"])

    # A scanned document has no text layer, so every body-regex field on it is
    # genuinely unreadable by this pass, including the parcel number, which is
    # printed on the page but only as pixels. Ground truth still carries the real
    # value because the OCR branch has to recover it; asserting it HERE would be
    # testing the OCR branch through the wrong door, and the honest expectation
    # for the deterministic pass on a scan is "nothing, and it said so".
    #
    # Note what still passes on the scan: classification, document_id,
    # document_date and the version signal, because those come from the filename.
    # Partial evidence beats none.
    if not gt.get("ocr_required"):
        parcel = d["parcel_id"]
        if gt["parcel_id"] == "FLAG_MULTIPLE":
            # The interesting case: the pass must REFUSE to pick, and say why.
            ok_flag = (not parcel["found"]) and any(
                f.startswith("multiple_parcel_ids") for f in parcel.get("flags", []))
            check(fname, "parcel_multi_flag", True, ok_flag)
        else:
            check(fname, "parcel_id", gt["parcel_id"], parcel["value"])
        check(fname, "property_address", gt["property_address"],
              d["property_address"]["value"])
        check(fname, "total_rentable_sf", gt["total_rentable_sf"],
              d["total_rentable_sf"]["value"])
        check(fname, "contacts_present", gt["contacts_present"],
              d["contact_candidates"]["found"])

w = max(len(r[0]) for r in rows)
for name, field, res in rows:
    print(f"{name:<{w}}  {field:<20} {res}")
print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
