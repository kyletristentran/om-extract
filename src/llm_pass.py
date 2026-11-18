"""llm_pass.py: the only generative step (rent roll table -> tenants[] JSON).

Everything else in this pipeline is regex. This module exists because rent roll
layout and column wording drift in ways regex cannot follow: tenant names are
multi-word and contain commas, column headers are split across physical lines, and
the horizontal alignment that would tell you which column a figure belongs to
breaks down on exactly the rows where the tenant name is longest.

Discipline:
  - The model sees ONLY the sliced rent roll region, never the whole document.
    A 40-page OM is mostly photography and prose; none of it belongs in context.
  - Output is validated against a jsonschema built from the spec, so adding a field
    to spec/om_extraction_spec.yaml changes the prompt and the schema together,
    with no code edit.
  - Fail closed. Anything unparseable, unvalidatable, or unreachable returns
    status "review" with a reason. Never a guess, never a partial row.
  - No post-hoc repair of model output. If the model returns a bad value the
    schema rejects the record and it goes to review. That failure is the signal.
    A silent fixup would hide the failure mode we most need to see.

LLM_MOCK=1 swaps the model call for a deterministic anchored parser (source
"mock"), so the eval can regression-test slicing + validation + scoring with no
model running.

Usage:
    python src/llm_pass.py <file-or-dir> [...]
    LLM_MOCK=1 python src/llm_pass.py fixtures/pdfs
Output: JSON to stdout; also writes out/llm_results.json
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from jsonschema import ValidationError, validate

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deterministic import (collect, extract_text,  # noqa: E402  (shared, not forked)
                           extract_text_layout)

SPEC = yaml.safe_load((ROOT / "spec" / "om_extraction_spec.yaml").read_text())["llm"]
FIELDS = SPEC["fields"]
FIELD_NAMES = [f["name"] for f in FIELDS]

# type -> the spec key holding that type's value pattern
_TYPE_PATTERNS = {
    "money": "money_pattern",
    "sf": "sf_pattern",
    "percent": "percent_pattern",
    "term": "term_pattern",
}


def _review(reason, **extra):
    """Fail closed: no tenants, an explicit reason, nothing invented."""
    return {"status": "review", "reason": reason, "tenants": [], **extra}


# --------------------------------------------------------------------------
# region slicing: the model's entire field of view
# --------------------------------------------------------------------------
def _slice_one(lines: list[str], start: int, cfg: dict, row_re) -> tuple[list[str], int]:
    """Slice one rent roll block starting after `lines[start]`.

    Returns (block_lines, rows_seen). The block ends after
    `stop_after_non_row_lines` consecutive non-row lines, once at least one row
    has been seen, so trailing body prose does not reach the model.

    That threshold is greater than 1 on purpose, for two reasons that both show up
    in real documents: a tenant name too long for its column wraps onto a line
    carrying no figures at all, and a designed OM interleaves graphic chrome
    (pull quotes, section labels, stat callouts) between table rows. At a
    threshold of 1, either one ends the table and every row below it is silently
    dropped, which reads downstream as a small building rather than as a bug.
    """
    out, rows_seen, non_row_run = [], 0, 0
    for ln in lines[start + 1: start + 1 + cfg["max_lines"]]:
        if row_re.search(ln):
            rows_seen += 1
            non_row_run = 0
        elif rows_seen:
            non_row_run += 1
            if non_row_run >= cfg["stop_after_non_row_lines"]:
                break
        out.append(ln)
    return out, rows_seen


def slice_region(text: str) -> list[str]:
    """Return the rent roll lines (header + data rows), or [] if absent.

    A document may carry the table in more than one block: a long rent roll
    continues onto the next page under a repeated heading. With
    `region.multi_region` set, every block is sliced and concatenated, so a split
    table yields all its rows instead of only those before the page break. Blocks
    containing no data row are dropped, which is what keeps a heading with no
    table under it from contributing noise.

    Only blocks under the configured heading are considered. A look-alike table
    elsewhere in the document, such as a "Historical Rent Roll" or a comparable-property
    summary, is never seen at all, which is a far more reliable defence than
    asking the model to tell them apart.
    """
    cfg = SPEC["region"]
    # Deliberately NOT stripped. In layout mode the leading and trailing spaces
    # carry the column alignment, and a trailing run of spaces is how an empty
    # last cell shows itself. In collapsed mode there is no padding, so this is a
    # no-op there.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    starts = [i for i, ln in enumerate(lines)
              if re.search(cfg["start_pattern"], ln, re.IGNORECASE)]
    if not starts:
        return []
    if not cfg.get("multi_region"):
        starts = starts[:1]

    row_re = re.compile(cfg["row_pattern"])
    out, consumed_until = [], -1
    for start in starts:
        # Skip a heading match that falls INSIDE a block already taken. Body prose
        # naming the table ("the rent roll on the following page") matches the
        # heading pattern, and without this guard it opens a second region
        # overlapping the first and emits the same rows twice. Overlapping regions
        # are the same table, so the first one wins.
        if start <= consumed_until:
            continue
        block, rows_seen = _slice_one(lines, start, cfg, row_re)
        if rows_seen:
            out.extend(block)
            consumed_until = start + len(block)
    return out


# --------------------------------------------------------------------------
# schema: built from the spec so the YAML is the single source of truth
# --------------------------------------------------------------------------
def build_schema() -> dict:
    props, required = {}, []
    for f in FIELDS:
        node = {"type": ["string", "null"]}
        key = _TYPE_PATTERNS.get(f["type"])
        if key:
            node["pattern"] = SPEC[key]
        elif f.get("pattern"):
            node["pattern"] = f["pattern"]
        props[f["name"]] = node
        if f.get("required"):
            required.append(f["name"])
    return {
        "type": "object",
        "required": ["tenants"],
        "properties": {
            "tenants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,   # the model cannot invent fields
                },
            }
        },
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------
# model call
# --------------------------------------------------------------------------
def build_prompt(region: list[str]) -> tuple[str, str]:
    field_lines = "\n".join(
        f"  - {f['name']} ({f['type']}): {f.get('description', '')}" for f in FIELDS
    )
    system = f"{SPEC['system_prompt']}\n\nFields, in order:\n{field_lines}"
    user = "Rent roll table:\n\n" + "\n".join(region)
    return system, user


def call_ollama(system: str, user: str) -> str:
    payload = {
        "model": SPEC["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "format": "json",
        "options": SPEC.get("options", {}),
    }
    # Reasoning mode is a spec setting because it is not free: with format=json the
    # JSON grammar constrains the sampler while a thinking model still wants to
    # emit reasoning tokens. On qwen3:14b that conflict leaked a trailing " /" into
    # the last value of the last row, reproducibly, at temperature 0. Omit the key
    # entirely to leave the model's own default alone.
    if "think" in SPEC:
        payload["think"] = SPEC["think"]
    req = urllib.request.Request(
        SPEC["endpoint"],
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=SPEC["timeout_s"]) as resp:
        return json.loads(resp.read())["message"]["content"]


def _strip_think(raw: str) -> str:
    """Some reasoning models emit <think>...</think> ahead of the JSON."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


# --------------------------------------------------------------------------
# mock: a test double, not a fallback for the real path
# --------------------------------------------------------------------------
_MOCK_SF = re.compile(r"\b(\d{1,3},\d{3})\b")
_MOCK_MONEY = re.compile(r"^\$?(\d[\d,]*(?:\.\d{1,2})?)$")
_MOCK_PCT = re.compile(r"^-?\d+(?:\.\d{1,2})?%$")
_MOCK_TERM = re.compile(r"^(?:[A-Za-z]{3}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|MTM)$", re.I)


def mock_parse(region: list[str]) -> dict:
    """Anchored parse used only under LLM_MOCK=1.

    Uses the comma'd square-footage figure as the column anchor: name before it,
    then percent / term / rents in order after it. Exists so slicing, validation
    and scoring can be regression-tested with no model running.

    It is NEVER consulted when a real model call fails. Falling back to it would
    mask the failure, and the whole point of the review queue is that failures
    stay visible.
    """
    rows = []
    for raw in region:
        ln = raw.strip()
        m = _MOCK_SF.search(ln)
        if not m:
            # A tenant name too long for its column continues on the next line: no
            # figures anywhere, no label punctuation. Join it to the row above, the
            # same thing the prompt asks the model to do. Guarded tightly, because a wrap
            # fragment is short, carries no digits, has no colon, and does not end
            # a sentence. Without the sentence test this happily swallowed body
            # prose into the tenant name of the row above.
            # `not ln.isupper()` rejects running headers and section titles, which
            # a designed OM sets in caps. A real wrap fragment is ordinary
            # title-case text ("Building C", "and Trust"); page chrome is
            # "MARKET OVERVIEW". Crude on purpose: the double is
            # allowed to be crude, it is not allowed to be a second extractor.
            if (rows and ln and ":" not in ln and not re.search(r"\d", ln)
                    and len(ln) <= 40 and not ln.rstrip().endswith(".")
                    and not ln.isupper()):
                rows[-1]["tenant_name"] = f"{rows[-1]['tenant_name']} {ln}".strip()
            continue

        # Strip whitespace only. A trailing period is part of the name
        # ("Blackwater Software, Inc."), and removing it silently rewrites a
        # value the extractor is supposed to copy exactly as printed.
        name = ln[:m.start()].strip()
        rest = ln[m.end():].split()

        pct = term = in_place = market = None
        for tok in rest:
            t = tok.strip()
            if pct is None and _MOCK_PCT.match(t):
                pct = t
            elif term is None and _MOCK_TERM.match(t):
                term = t.title() if "-" in t and not t[0].isdigit() else t
            elif _MOCK_MONEY.match(t):
                if in_place is None:
                    in_place = _MOCK_MONEY.match(t).group(1)
                elif market is None:
                    market = _MOCK_MONEY.match(t).group(1)

        if not name:
            continue
        rows.append({"tenant_name": name, "suite": None, "rentable_sf": m.group(1),
                     "pct_of_building": pct, "lease_expiration": term,
                     "in_place_base_rent": in_place, "market_rent": market})
    return {"tenants": rows}


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def tenants_from_text(text: str, source: str, file_name: str) -> dict:
    """Slice -> model (or mock) -> schema-validate. The shared driver.

    Both entry points go through here: the text-layer path (extract_tenants,
    source "llm") and the OCR path (ocr_pass, source "ocr"). Keeping one body
    means the two branches can never drift on slicing rules or validation
    strictness, only on where their text came from, which is exactly the
    difference we want recorded on the output.
    """
    mock = os.environ.get("LLM_MOCK") == "1"
    if mock:
        source = "mock"

    region = slice_region(text)
    if not region:
        # No text-layer table. For a scanned document this is correct and expected.
        # The OCR branch is what recovers it. Either way: never guess.
        return {"file": file_name, **_review("no_rent_roll_region", source=source)}

    if mock:
        parsed = mock_parse(region)
    else:
        try:
            raw = call_ollama(*build_prompt(region))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"file": file_name, **_review(f"model_unreachable:{e}", source=source)}
        except (KeyError, json.JSONDecodeError) as e:
            return {"file": file_name, **_review(f"bad_model_envelope:{e}", source=source)}
        try:
            parsed = json.loads(_strip_think(raw))
        except json.JSONDecodeError as e:
            return {"file": file_name, **_review(f"model_output_not_json:{e}",
                                                 source=source, raw=raw[:400])}

    try:
        validate(parsed, build_schema())
    except ValidationError as e:
        return {"file": file_name,
                **_review(f"schema_invalid:{e.message[:160]}", source=source)}

    rows = parsed["tenants"]
    return {
        "file": file_name,
        "status": "ok",
        "source": source,
        "tenant_count": len(rows),
        "tenants": rows,
        "region_lines": len(region),
    }


def extract_tenants(path: Path) -> dict:
    """Text-layer entry point: read the PDF's own text and parse its rent roll."""
    reader = extract_text_layout if SPEC["region"].get("layout_text") else extract_text
    text = reader(path)
    if text is None:
        source = "mock" if os.environ.get("LLM_MOCK") == "1" else "llm"
        return {"file": path.name, **_review("read_error", source=source)}
    return tenants_from_text(text, "llm", path.name)


if __name__ == "__main__":
    results = [extract_tenants(p) for p in collect(sys.argv[1:])]
    out = ROOT / "out" / "llm_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
