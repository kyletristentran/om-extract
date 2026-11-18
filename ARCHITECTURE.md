# Architecture

This document describes each module, the data shapes and the design decisions that should be kept. [`README.md`](README.md) gives the overview. Read this file before changing the code.

## 1. Flow

```
PDF
 │
 ├─ triage.py         per-page char density → text_layer | scanned | hybrid
 │
 ├─ deterministic.py  Runs on both branches. Regex over collapsed page text:
 │                      parcel number (deduped; >1 distinct → flag, never pick)
 │                      version signal (filename beats body, precedence-ordered)
 │                      document_date + document_id from the filename
 │                      labelled property facts
 │                      listing-contact candidates
 │
 ├─ BRANCH on the triage classification
 │    text_layer     → llm_pass.extract_tenants()      source "llm"
 │    scanned|hybrid → ocr_pass.process() → Markdown
 │                       → llm_pass.tenants_from_text()  source "ocr"
 │
 │      both branches converge in llm_pass.tenants_from_text():
 │        slice_region() → build_prompt() → local model (format=json, think=false)
 │        → jsonschema built from spec.llm.fields → tenants[]
 │
 ├─ validate.py       merge into one record; spec-driven checks; the subtotal
 │                    guard; confidence (worst source wins); blocking → review
 │
 └─ emit.py           pipeline record → typed contract JSON
                      "52,180" → 52180; "10.7%" → 10.7; "Dec-26" → "2026-12"
```

Deterministic extraction runs on every document, whichever branch it takes. A scanned document still has a filename, so `document_id`, `document_date` and the version signal can be recovered even when the page content is unreadable. A record that reports the date but not the rent roll is more useful than no record.

The entry points are `src/pipeline.py`, which runs extraction and writes to `out/`, and `src/emit.py`, which runs extraction and writes the typed contract to `out/om_records.json`. `src/validate.py` can also run standalone as an older top-level driver. `pipeline.py` supersedes it and adds the OCR branch, but it imports its check functions from `validate.py`.

## 2. Modules

### `triage.py`

```python
triage_pdf(path) -> {file, status: "ok"|"read_error", classification, pages,
                     text_page_fraction, page_detail[{page, chars, has_text}]}
```

A page counts as a text page when `len(page.extract_text().strip()) >= triage.min_chars_text_page` (40). The document verdict comes from the fraction of text pages: `>= 0.9` is `text_layer`, `<= 0.1` is `scanned`, and anything between is `hybrid`. Any pdfplumber exception sets `status: "read_error"`, which routes the document to review.

The module loads its slice of the spec at import time, so a YAML typo fails before any PDF is opened.

It also defines `collect(args)`. A directory expands to `sorted(glob("**/*.pdf"))` and a `.pdf` path yields itself. Every module uses the same CLI shape.

### `deterministic.py`

The module has two text readers, and the difference between them matters:

```python
extract_text(path)         # pdfplumber default, collapsed whitespace. For regex.
extract_text_layout(path)  # extract_text(layout=True). For the table.
```

Collapsed text is stable and cheap, and it suits label matching. It does not suit a table, because collapsing runs of spaces removes the information that identifies a column. The README section on column alignment gives an example.

One consequence affects `labeled_fields`. Those regexes run against the collapsed text, so a pattern that requires two or more spaces between label and value never matches.

Every extractor returns a field in the following shape and never a bare value:

```python
_field(value, source, found=True) -> {"value":…, "source":…, "found":True}
_null_field()                     -> {"value":None, "source":None, "found":False}
```

| Function | Behavior |
|---|---|
| `parcel_id(text)` | Finds all parcel numbers and dedupes them in order. With none, it returns a null field. With more than one distinct number, it returns `{value:None, found:False, flags:["multiple_parcel_ids:a,b"]}` and does not choose between them. A property can span several parcels, and in that case no single value is correct. The rest of the pipeline follows this fail-closed pattern. |
| `version_signal(filename, text)` | Loops over `(("filename", filename), ("body", text))` and, within each, over `spec.version_signals`. The filename takes priority. Body prose in almost any OM contains "draft" or "preliminary" in a disclaimer, and that should not relabel a document whose filename says Final. Within a source, list order sets precedence. |
| `document_date(filename)` | Each spec pattern declares its own capture `order` (`ymd`, `mdy`, `dmy` or `month_year`). Parsing depends on that declaration and not on list position, so a pattern can be inserted in the middle of the list safely. Two-digit years pivot to 20xx. A `ValueError` from `date()` means the search continues, because `13.40.2019` is not a date. `month_year` returns `YYYY-MM` and is not padded to a day. The day is not in the source, and a consumer reading `2019-09-01` could not know it was invented. |
| `labeled_fields(text)` | One single-capture regex per spec row. Adding a property fact is a YAML edit. |
| `contact_candidates(text)` | Regexes over the spec patterns, deduped. Used as evidence only. |

### `llm_pass.py`

This module contains the only generative step.

Region slicing determines everything the model sees.

- The text is split on newlines and blank lines are dropped. The lines are not stripped. In layout mode the leading and trailing spaces carry the column alignment, and a trailing run of spaces is how an empty last cell appears.
- The slicer finds every line that matches `region.start_pattern`. With `multi_region: true` it slices all blocks and concatenates them. This is how a rent roll that continues on page two under a repeated heading yields all its rows.
- An overlap guard, `if start <= consumed_until: continue`, prevents duplicates. Body prose that names the table ("the rent roll on the following page") matches the heading pattern. Without the guard it would open a second region and emit every row twice.
- `_slice_one()` walks forward up to `max_lines` (80). A line that matches `row_pattern` is a data row and resets the counter. Once at least one row has been seen, `stop_after_non_row_lines` (3) consecutive non-row lines end the block. The threshold is above 1 for two reasons that both occur in real documents: a long tenant name wraps onto a line with no figures, and designed OMs place graphic elements between table rows.
- Blocks with no data rows are dropped.
- Only blocks under the configured heading are considered, so a similar table under a different heading is never passed to the model. This is more reliable than asking the model to tell the two apart.

`row_pattern` is `\b\d{1,3},\d{3}\b`, a square-footage figure with a thousands separator. The pattern is permissive on purpose and also matches subtotal rows. Slicing is permissive, and the prompt and the validator do the discriminating. A pattern narrow enough to exclude subtotals would also drop any real tenant whose row is missing a column.

`build_schema()` builds the output schema from the spec. It walks `spec.llm.fields`, maps each field's `type` to its pattern (`money_pattern`, `sf_pattern`, `percent_pattern`, `term_pattern`) and sets `additionalProperties: False` at both levels so the model cannot add fields. Adding a field to the YAML changes the prompt and the schema together.

The model call:

```python
POST http://localhost:11434/api/chat
{ model, messages: [system, user], stream: false,
  format: "json", think: false, options: {temperature: 0, num_ctx: 8192} }
```

`think` is set only if the key is present in the spec, so omitting it leaves the model's default in place. `_strip_think(raw)` removes any `<think>…</think>` block before `json.loads`.

The fail-closed reason codes all return `{status:"review", reason, tenants:[]}`: `no_rent_roll_region`, `model_unreachable:<err>`, `bad_model_envelope:<err>`, `model_output_not_json:<err>` (which carries `raw[:400]`), `schema_invalid:<msg>` and `read_error`.

`mock_parse()`, enabled with `LLM_MOCK=1`, is a deterministic anchored parser that uses the comma-separated square footage as the column anchor. It is a test double. It is never called when a real model call fails, because that would hide the failure.

`tenants_from_text(text, source, file_name)` is the shared driver that both branches call. Because there is one function body, the text and OCR branches cannot diverge on slicing rules or validation strictness. They differ only in where their text came from, and that difference is recorded on the output.

### `ocr_pass.py`

OCR runs in two stages. The first converts the page to structured Markdown, and the second passes that Markdown to the same LLM pass. A single-pass vision model performs layout analysis and value extraction together, and when its output is wrong there is no way to tell which part failed. Separating the stages makes an OCR error distinguishable from a parsing error in the review queue.

Backends are tried in spec order and imported lazily. An `ImportError` means the backend is not installed and the next one is tried. Any other exception is recorded for that backend as `name:failed(Type:msg)`, so the review queue shows what each backend reported. If all are exhausted the result is `ocr_backend_unavailable:<tried>`.

`process(path, force=False)` runs triage again and skips `text_layer` documents. Running OCR over extractable text only adds transcription error to a source that was already exact.

### `validate.py`

| Function | Checks |
|---|---|
| `check_document_fields` | For each entry in `validation.document_checks`: the field is present; a `required` field that was not found raises `required_field_empty`; a failed `pattern` fullmatch raises `pattern_mismatch`. |
| `check_flags` | Any `flags:[…]` on any field node. The code is the text before the first `:`. A flag is blocking if it is listed in `validation.blocking_flags`. |
| `check_rows` | If the LLM pass did not return ok, a single blocking `rows_unavailable` carries its reason. Otherwise it applies the per-row `validation.row_checks`, the subtotal guard, and a duplicate guard keyed on `(tenant_name, suite, rentable_sf)`. One tenant in several suites is several legitimate rows and is never collapsed. |
| `check_count` | `tenant_count == len(tenants)`. |

The subtotal guard rejects the record instead of dropping the row. Dropping it would hide the parsing failure that produced it. The README describes the case.

`record_sources()` collects every `source` from found field nodes, plus the source of the rows. `confidence()` ranks them against `validation.source_rank` (`["ocr","mock","llm","body_regex","filename"]`, weakest first, with unknown sources treated as weakest) and maps the minimum through `confidence_by_source`. Any blocking issue forces the confidence to `"none"`.

Validation does not repair values. It rejects a value that fails and never rewrites one to make it pass.

### `pipeline.py`

`run_one(path)` runs triage, the deterministic pass, the branch and the check functions from `validate.py`, and returns one record:

```json
{ "file": "...", "status": "ok"|"review", "classification": "text_layer",
  "route": "text"|"ocr", "ocr_backend": null,
  "extraction_confidence": "high"|"medium"|"low"|"none",
  "sources": ["body_regex","filename","llm"],
  "document_id": {...}, "document_date": {...}, "parcel_id": {...},
  "property_address": {...}, "tenant_count": 4,
  "tenants": [ {tenant_name, suite, rentable_sf, pct_of_building,
                lease_expiration, in_place_base_rent, market_rent}, ... ],
  "issues": [ {code, detail, blocking}, ... ] }
```

`main()` writes `out/validated.json`, `out/review_queue.json` and `out/pipeline_report.json` (counts by confidence, counts by route and review reasons).

### `emit.py`

This module is the typed boundary. All conversions happen here. Upstream, every value is kept as printed so that it can be compared against the page.

- `to_number` returns `None` for anything that does not parse cleanly. A value the extractor could not read should not arrive downstream as `0`, because it would be treated as a real in-place rent of zero dollars and would lower every average that includes it.
- `to_percent` converts `"10.7%"` to `10.7` and not to `0.107`. The source printed a percent, so the number keeps percent units. Rescaling it without notice could produce a rent roll that reports a tenant occupying 0.107% of a building.
- `to_iso_term` returns `(iso, precision)` and never reports more precision than the source had. `"Dec-26"` becomes `("2026-12", "month")`. `"MTM"` becomes `(None, "month_to_month")`, which is a different state from `(None, "absent")`. A month-to-month lease has no expiration and a blank cell has an unknown one, and a rollover model needs to tell them apart. Two-digit years pivot at 70, because rent rolls carry both expired and future leases.
- Grain: every child row carries the parent's `document_id`. The value is set from the parent and not taken from the row, so the two cannot disagree. `check_invariants()` enforces this and the count rule in code, because a JSON Schema cannot compare two fields.

`emit()` refuses any record that the pipeline routed to review, and it does so before schema validation. This check is needed because the contract schema is structural. A rent roll that contains a `TOTAL OFFICE LEASED` row has the correct types and required fields, passes the schema and would ship a lease that does not exist. If the last step treated the review verdict as advisory, the pipeline would no longer fail closed.

## 3. Adding a field

None of the following requires a change to the pipeline logic.

- **A property fact.** Add a row to `deterministic.labeled_fields` (a name and a single-capture regex against the collapsed text), then a property to `spec/om_record_schema.json`, then a line in `emit.to_contract()` if it needs type conversion.
- **A rent roll column.** Add a row to `llm.fields`, which changes the prompt and the model-output schema together. Then add a `validation.row_checks` rule for its shape, a property to `tenant_row` in the record schema, and its conversion in `emit.to_contract()`.
- **A validation rule.** Add a row to `validation.document_checks` or `validation.row_checks`. Set `blocking: false` for an advisory note that appears in `issues` and `notes` without stopping the record.
- **A new document type.** The four keys in `llm.region` and `llm.fields` are what need to change. See the README.

## 4. Decisions to keep

Each setting below fixes a failure observed during development.

| Setting | Reason |
|---|---|
| `llm.think: false` | Reasoning mode against a JSON grammar corrupts the last value of the last row, reproducibly, at temperature 0. |
| `region.layout_text: true` and unstripped region lines | Together they are the only information that shows which column an isolated figure belongs to. Turning either off makes rows with blank cells ambiguous again. |
| `stop_after_non_row_lines: 3` | At 1, a wrapped tenant name or a line of page furniture ends the table and every row below it is dropped. Downstream this looks like a small building and not like a bug. |
| The multi-region overlap guard | Without it, prose that names the table duplicates every row. |
| A permissive `row_pattern` | A pattern narrow enough to exclude subtotals also drops real tenants with a missing column. |
| `validation.non_tenant_row_patterns` | It is the last check that stops a subtotal row from being emitted as a lease. |
| Rejecting, not dropping, a non-tenant row | Dropping hides the parsing failure that produced it. |
| Two separate confidence fields | Evidence quality and reading quality are different measures and call for different review. |
| `to_number()` returning `None`, not `0` | A zero rent looks like data. |
| `to_iso_term` keeping `month` precision | Padding to a day adds a fact the source did not contain. |
| `emit()` honoring the review verdict | Otherwise the review queue is advisory. |
| Loading the spec at module import | A YAML typo fails before any PDF is opened. |

## 5. Consolidating into one script

The modules can be combined into a single file. Keep the following in mind:

- The `sys.path.insert` and cross-imports can be dropped, because in one file they become function calls. Keep the shared driver, so that the text and OCR branches still run identical slicing and validation.
- Keep the spec external. The configuration is what makes the project maintainable. If a single artifact is required, embed the YAML as a module-level triple-quoted string and load it with `yaml.safe_load`. Do not inline the patterns into the Python.
- Definition order: `_field`/`_null_field`, then the text readers, the deterministic extractors, `slice_region` / `build_schema` / `call_ollama` / `tenants_from_text`, the OCR backends, the validation checks, `run_one`, the contract translation and `main`.
- Keep the spec loads at the top of the module so that a typo still fails at import.
- Keep `mock_parse` out of the fallback path even when it is in the same file.
