"""ocr_pass.py — recover the rent roll from scanned pages.

Two-stage by design: OCR to structured Markdown FIRST, then hand that Markdown to
the same LLM pass the text-layer branch uses. A one-shot vision model ("look at
this page, give me JSON") is worse on dense financial tables — it has to do layout
analysis and value extraction in a single forward pass, and when it drifts you
cannot tell which half failed. Splitting the stages means an OCR error and a
parsing error are distinguishable in the review queue, which is the difference
between a fixable pipeline and a mysterious one.

Nothing about slicing, prompting, or validation is reimplemented here. This module
produces text; `llm_pass.tenants_from_text()` does the rest, tagged source "ocr"
so the confidence rubric in validate.py can rank it below regex and plain LLM.

Backends are tried in spec order and imported lazily, because the primary one
pulls a large model stack that most runs do not need:
  - docling  — primary; structured Markdown that preserves table geometry
  - paddleocr — fallback; plain text, materially weaker on table structure

If no backend is installed the pass fails closed with `ocr_backend_unavailable`.
It never falls back to guessing, and never to the text layer it already knows is
absent. Running without OCR installed is a supported configuration, not a broken
one — scanned documents simply route to review.

Usage:
    python src/ocr_pass.py <file-or-dir> [...]
    pip install -r requirements-ocr.txt
Output: JSON to stdout; also writes out/ocr_results.json
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import collect  # noqa: E402  (shared CLI shape)
from llm_pass import _review, tenants_from_text  # noqa: E402
from triage import triage_pdf  # noqa: E402

SPEC = yaml.safe_load((ROOT / "spec" / "om_extraction_spec.yaml").read_text())["ocr"]


# --------------------------------------------------------------------------
# backends — each returns text, or raises
# --------------------------------------------------------------------------
def _ocr_docling(path: Path) -> str:
    """Docling's converter. Markdown keeps table structure, which is what the
    region slicer needs to find row boundaries at all — a flat stream of OCR'd
    words has no row concept to slice on."""
    from docling.document_converter import DocumentConverter  # lazy: heavy import

    result = DocumentConverter().convert(str(path))
    return result.document.export_to_markdown()


def _ocr_paddle(path: Path) -> str:
    """PaddleOCR fallback. Line-level text only — no table geometry, so the slicer
    is working from reading order alone. Expect lower accuracy on wide tables."""
    from paddleocr import PaddleOCR  # lazy
    from pdf2image import convert_from_path  # lazy

    engine = PaddleOCR(use_angle_cls=True, lang=SPEC.get("lang", "en"), show_log=False)
    lines = []
    for image in convert_from_path(str(path), dpi=SPEC.get("dpi", 300)):
        for block in engine.ocr(image, cls=True) or []:
            for entry in block or []:
                lines.append(entry[1][0])
    return "\n".join(lines)


BACKENDS = {"docling": _ocr_docling, "paddleocr": _ocr_paddle}


def ocr_text(path: Path) -> tuple[str | None, str]:
    """Run the first backend that is installed and succeeds.

    Returns (text, backend_name) or (None, reason). An ImportError means the
    backend is not installed — that is a setup fact, so we move to the next one.
    Any other exception is a real failure on this file and is recorded
    per-backend, so the review queue shows what each backend actually said rather
    than a single opaque "OCR failed".
    """
    tried = []
    for name in SPEC["backends"]:
        fn = BACKENDS.get(name)
        if fn is None:
            tried.append(f"{name}:unknown_backend")
            continue
        try:
            text = fn(path)
        except ImportError as e:
            tried.append(f"{name}:not_installed({e.name or e})")
            continue
        except Exception as e:                      # noqa: BLE001 — record, don't mask
            tried.append(f"{name}:failed({type(e).__name__}:{str(e)[:80]})")
            continue
        if text and text.strip():
            return text, name
        tried.append(f"{name}:empty_output")
    return None, "ocr_backend_unavailable:" + ",".join(tried)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def process(path: Path, force: bool = False) -> dict:
    """OCR a scanned/hybrid document and parse its rent roll.

    Text-layer documents are skipped (status "skipped") rather than OCR'd —
    re-OCRing a page that already has extractable text only adds transcription
    error to a source that was already exact. Pass force=True to override for a
    document whose text layer is present but garbage.
    """
    verdict = triage_pdf(path)
    if verdict.get("status") != "ok":
        return {"file": path.name, **_review("read_error", source="ocr")}

    cls = verdict["classification"]
    if cls not in SPEC["classifications"] and not force:
        return {"file": path.name, "status": "skipped", "classification": cls,
                "reason": "text_layer doc — OCR would add transcription error"}

    text, backend = ocr_text(path)
    if text is None:
        return {"file": path.name, "classification": cls,
                **_review(backend, source="ocr")}

    result = tenants_from_text(text, "ocr", path.name)
    return {**result, "classification": cls, "ocr_backend": backend,
            "ocr_chars": len(text)}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    results = [process(p, force=force) for p in collect(args)]
    out = ROOT / "out" / "ocr_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
