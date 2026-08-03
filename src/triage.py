"""triage.py — classify a PDF as text_layer / scanned / hybrid.

Why this exists: the pipeline branches here. Text-layer documents go straight to
regex + text parsing; scanned documents need an OCR pass first. Classification is
per-page character density, rolled up to a document verdict.

This runs before anything expensive. Deciding the branch costs one pdfplumber pass
and no rasterization at all, which matters when the alternative is spending 40
seconds per document on OCR you did not need.

Usage:
    python src/triage.py <file-or-dir> [more files...]
Output: JSON to stdout, one object per file.
"""
import json
import sys
from pathlib import Path

import pdfplumber
import yaml

SPEC = yaml.safe_load(
    (Path(__file__).parent.parent / "spec" / "om_extraction_spec.yaml").read_text()
)["triage"]


def triage_pdf(path: Path) -> dict:
    pages = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                nchars = len(text.strip())
                pages.append({
                    "page": i,
                    "chars": nchars,
                    "has_text": nchars >= SPEC["min_chars_text_page"],
                })
    except Exception as e:  # unreadable file -> fail closed, route to review
        return {"file": str(path), "status": "read_error", "error": str(e)}

    n = len(pages)
    text_frac = (sum(p["has_text"] for p in pages) / n) if n else 0.0
    if text_frac >= SPEC["text_layer_doc_threshold"]:
        cls = "text_layer"
    elif text_frac <= SPEC["scanned_doc_threshold"]:
        cls = "scanned"
    else:
        cls = "hybrid"

    return {
        "file": str(path),
        "status": "ok",
        "classification": cls,
        "pages": n,
        "text_page_fraction": round(text_frac, 3),
        "page_detail": pages,
    }


def collect(args):
    """A directory expands to its PDFs, sorted. A .pdf path yields itself.

    Every module in this package exposes this same shape, so any pass can be run
    standalone over a file or a folder with identical semantics.
    """
    for a in args:
        p = Path(a)
        if p.is_dir():
            yield from sorted(p.glob("**/*.pdf"))
        elif p.suffix.lower() == ".pdf":
            yield p


if __name__ == "__main__":
    results = [triage_pdf(p) for p in collect(sys.argv[1:])]
    print(json.dumps(results, indent=2))
