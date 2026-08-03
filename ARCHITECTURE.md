# Architecture

Module-by-module detail, the data shapes, and the decisions that look like dead
weight until someone removes one. [`README.md`](README.md) has the overview; this
is the file to read before changing anything.

---

## 1. Flow

```
PDF
 │
 ├─ triage.py         per-page char density → text_layer | scanned | hybrid
 │
 ├─ deterministic.py  ALWAYS runs, both branches. Regex over collapsed page text:
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
 ├─ validate.py       merge into ONE record; spec-driven checks; the subtotal
 │                    guard; confidence (worst source wins); blocking → review
 │
 └─ emit.py           pipeline record → typed contract JSON
                      "52,180" → 52180; "10.7%" → 10.7; "Dec-26" → "2026-12"
```

Deterministic extraction runs on **every** document regardless of branch. A
scanned document still has a filename, so `document_id`, `document_date` and the
version signal are recoverable even when the page content is unreadable. Partial
evidence beats none, and a record that says "I got the date but not the rent roll"
is far more useful than one that says nothing.

Entry points: `src/pipeline.py` (extraction → `out/`) and `src/emit.py`
(extraction + typed contract → `out/om_records.json`). `src/validate.py` also runs
standalone as an older top-level driver; `pipeline.py` supersedes it and adds the
OCR branch, but validate.py's *check functions* are what pipeline.py imports.

---

## 2. Modules

### `triage.py`

```python
triage_pdf(path) -> {file, status: "ok"|"read_error", classification, pages,
                     text_page_fraction, page_detail[{page, chars, has_text}]}
```

Per page: `len(page.extract_text().strip()) >= triage.min_chars_text_page` (40).
Document verdict from the fraction of text pages: `>= 0.9` → `text_layer`,
`<= 0.1` → `scanned`, else `hybrid`. Any pdfplumber exception →
`status: "read_error"`, which routes to review.

Loads its spec slice at **import** time, so a YAML typo fails before any PDF is
opened rather than three passes later.

Also defines `collect(args)` — a directory expands to `sorted(glob("**/*.pdf"))`,
a `.pdf` path yields itself. Every module reuses this exact CLI shape.

### `deterministic.py`

Two text readers, and the distinction between them is load-bearing:

```python
extract_text(path)         # pdfplumber default — COLLAPSED whitespace. For regex.
extract_text_layout(path)  # extract_text(layout=True). For the table.
```

Collapsed text is stable and cheap and right for label matching. It is wrong for a
table, because collapsing runs of spaces destroys column identity — see the README
section on column alignment.

Note this has a consequence for `labeled_fields`: those regexes run against the
**collapsed** text, so a pattern requiring two or more spaces between label and
value will never fire.

Field constructors — every extractor returns this shape, never a bare value:

```python
_field(value, source, found=True) -> {"value":…, "source":…, "found":True}
_null_field()                     -> {"value":None, "source":None, "found":False}
```

| function | behaviour |
|---|---|
| `parcel_id(text)` | Finds all parcel numbers, dedupes preserving order. 0 → null field. **>1 distinct → `{value:None, found:False, flags:["multiple_parcel_ids:a,b"]}` — it never picks.** A property spanning several parcels is real, and in that case no single value is "the" parcel. This is the fail-closed pattern the rest of the pipeline copies. |
| `version_signal(filename, text)` | Loops `(("filename", filename), ("body", text))` outer, `spec.version_signals` inner. Filename wins outright — body prose in almost any OM contains "draft" or "preliminary" in a disclaimer, and that must not relabel a document whose filename says Final. Within a source, list order is precedence. |
| `document_date(filename)` | Each spec pattern declares its own capture `order` (`ymd`/`mdy`/`dmy`/`month_year`); parsing keys on that, **never on list position**, so inserting a pattern mid-list is safe. Two-digit years pivot to 20xx. A `ValueError` from `date()` means "keep looking", because `13.40.2019` is not a date. **`month_year` returns `YYYY-MM` and is not padded to a day** — the day is not in the source, and a consumer seeing `2019-09-01` has no way to know it was invented. |
| `labeled_fields(text)` | One single-capture regex per spec row. Adding a property fact is a YAML edit. |
| `contact_candidates(text)` | Regexes over the spec patterns, deduped. Evidence only. |

### `llm_pass.py` — the only generative step

**Region slicing** is the model's entire field of view.

- Splits on newline, drops blank lines. **Deliberately does not `.strip()` the
  lines** — in layout mode the leading and trailing spaces carry the column
  alignment, and a trailing run is how an empty last cell shows itself.
- Finds every line matching `region.start_pattern`. With `multi_region: true`,
  slices *all* blocks and concatenates — that is how a rent roll continued on page
  two under a repeated heading yields all its rows.
- **Overlap guard:** `if start <= consumed_until: continue`. Body prose naming the
  table ("the rent roll on the following page") matches the heading pattern;
  without this guard it opens a duplicate region and emits every row twice.
- `_slice_one()` walks forward up to `max_lines` (80). A line matching
  `row_pattern` is a data row and resets the counter; once ≥1 row has been seen,
  `stop_after_non_row_lines` (**3**) consecutive non-row lines ends the block.
  The threshold is greater than 1 for two reasons that both occur in real
  documents: a long tenant name wraps onto a line with no figures, and designed
  OMs interleave graphic chrome between table rows.
- Blocks containing zero data rows are dropped.
- Only blocks under the configured heading are ever considered, so a look-alike
  table under a different heading is never seen at all — a far more reliable
  defence than asking the model to tell them apart.

`row_pattern` is `\b\d{1,3},\d{3}\b` — a square-footage figure with a thousands
separator. **Deliberately permissive**: it also matches subtotal rows. Slicing is
permissive; the prompt and the validator are what discriminate. A pattern narrow
enough to exclude subtotals would also drop any real tenant whose row is missing a
column.

**Schema built from the spec.** `build_schema()` walks `spec.llm.fields`, maps each
field's `type` to its pattern (`money_pattern`, `sf_pattern`, `percent_pattern`,
`term_pattern`), and sets `additionalProperties: False` at both levels so the model
cannot invent fields. Adding a field to YAML changes prompt *and* schema together.

**Model call:**

```python
POST http://localhost:11434/api/chat
{ model, messages: [system, user], stream: false,
  format: "json", think: false, options: {temperature: 0, num_ctx: 8192} }
```

`think` is only set if the key is present in the spec, so omitting it leaves the
model's own default alone. `_strip_think(raw)` removes any `<think>…</think>` block
before `json.loads`.

**Fail-closed reason codes**, all returning `{status:"review", reason, tenants:[]}`:
`no_rent_roll_region`, `model_unreachable:<err>`, `bad_model_envelope:<err>`,
`model_output_not_json:<err>` (carries `raw[:400]`), `schema_invalid:<msg>`,
`read_error`.

**`mock_parse()` / `LLM_MOCK=1`** — a deterministic anchored parser using the
comma'd square footage as the column anchor. A **test double only**. It is never
consulted when a real model call fails; that would mask the failure.

`tenants_from_text(text, source, file_name)` is the shared driver both branches
call. One body means the text and OCR branches cannot drift on slicing rules or
validation strictness — only on where their text came from, which is exactly the
difference recorded on the output.

### `ocr_pass.py`

**Two-stage by design:** OCR to structured Markdown first, then hand that Markdown
to the same LLM pass. A one-shot vision model does layout analysis and value
extraction in a single forward pass, and when it drifts you cannot tell which half
failed. Splitting the stages makes an OCR error and a parsing error distinguishable
in the review queue — the difference between a fixable pipeline and a mysterious
one.

Backends tried in spec order, **imported lazily**. `ImportError` → not installed,
move on. Any other exception → recorded per-backend as `name:failed(Type:msg)` so
the review queue shows what each backend actually said. All exhausted →
`ocr_backend_unavailable:<tried>`.

`process(path, force=False)` re-triages and **skips `text_layer` documents** —
re-OCRing extractable text only adds transcription error to a source that was
already exact.

### `validate.py`

| function | checks |
|---|---|
| `check_document_fields` | Per `validation.document_checks`: field present at all; `required` + not found → `required_field_empty`; `pattern` fullmatch → `pattern_mismatch`. |
| `check_flags` | Any `flags:[…]` on any field node. Code = text before the first `:`. Blocking iff listed in `validation.blocking_flags`. |
| `check_rows` | If the LLM pass didn't return ok → a single blocking `rows_unavailable` carrying its reason. Else per-row `validation.row_checks`, **the subtotal guard**, and a duplicate guard keyed on `(tenant_name, suite, rentable_sf)` — one tenant in several suites is several legitimate rows and must never be collapsed. |
| `check_count` | `tenant_count == len(tenants)`. |

**The subtotal guard rejects the record rather than dropping the row.** Silently
deleting it would hide a real parsing failure, and this failure is too expensive to
hide — see the README.

`record_sources()` collects every `source` from found field nodes plus the rows'
source. `confidence()` ranks them against `validation.source_rank`
(`["ocr","mock","llm","body_regex","filename"]`, weakest first; unknown treated as
weakest) and maps the **minimum** through `confidence_by_source`. Any blocking
issue forces confidence to `"none"`.

**No repair.** Validation rejects; it never rewrites a value to make it pass.

### `pipeline.py`

`run_one(path)` = triage → deterministic → branch → validate's check functions →
one record:

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

`main()` writes `out/validated.json`, `out/review_queue.json`, and
`out/pipeline_report.json` (counts by confidence, by route, review reasons).

### `emit.py`

The typed boundary. Conversions live here, once, rather than smeared through the
passes — upstream every value is kept **as printed** so it can be compared against
the page.

- `to_number` — returns `None` for anything that does not cleanly parse.
  Deliberate: a value the extractor could not read must not arrive downstream as
  `0`, which would become a real in-place rent of zero dollars and quietly drag
  every average that touches it.
- `to_percent` — `"10.7%"` → `10.7`, **not** `0.111`. The source printed a percent,
  so the number carries percent units. Silent rescaling is how a rent roll ends up
  claiming a tenant occupies 0.111% of a building.
- `to_iso_term` — returns `(iso, precision)` and never more precision than the
  source had. `"Dec-26"` → `("2026-12", "month")`. `"MTM"` →
  `(None, "month_to_month")`, which is a *different state* from `(None, "absent")`:
  an MTM lease has no expiration, a blank cell has an unknown one, and anyone
  modeling rollover needs to tell those apart. Two-digit years pivot at 70,
  because rent rolls carry both expired and future leases.
- **grain** — every child carries the parent's `document_id`, set from the parent
  rather than trusted from the row, so the two cannot disagree.
  `check_invariants()` enforces that and the count rule in code, because a
  jsonschema cannot compare two fields.

**`emit()` refuses any record the pipeline routed to review, before schema
validation.** That gate is not redundant: the contract schema is structural, so a
rent roll containing a `TOTAL OFFICE LEASED` row passes it perfectly — correct
types, correct required fields — and ships a phantom lease. A fail-closed pipeline
with an advisory last mile is not fail-closed.

---

## 3. Adding a field

Nothing below requires a Python change.

**A property fact:** add a row to `deterministic.labeled_fields` (name + a
single-capture regex against the collapsed text), then a property to
`spec/om_record_schema.json`, then a line in `emit.to_contract()` if it needs type
conversion.

**A rent roll column:** add a row to `llm.fields` — that alone changes the prompt
and the model-output schema together. Then add a `validation.row_checks` rule for
its shape, a property to `tenant_row` in the record schema, and its conversion in
`emit.to_contract()`.

**A new validation rule:** a row in `validation.document_checks` or
`validation.row_checks`. Set `blocking: false` for an advisory note that appears in
`issues` and `notes` without stopping the record.

**A new document type entirely:** the four keys in `llm.region` plus `llm.fields`
are the retargeting surface. See the README.

---

## 4. Do not simplify these

Every one of them was paid for.

| | why |
|---|---|
| `llm.think: false` | Reasoning mode against a JSON grammar corrupts the last value of the last row, reproducibly, at temperature 0. |
| `region.layout_text: true` **and** not stripping region lines | Together they are the only thing that says which column an isolated figure belongs to. Turning either off returns blank-cell rows to guesswork. |
| `stop_after_non_row_lines: 3` | At 1, a wrapped tenant name or a line of page chrome ends the table and every row below it is dropped — which reads downstream as a small building, not as a bug. |
| the multi-region overlap guard | Without it, prose naming the table duplicates every row. |
| `row_pattern` being permissive | A pattern narrow enough to exclude subtotals also drops real tenants with a missing column. |
| `validation.non_tenant_row_patterns` | The only thing standing between a subtotal row and a phantom lease. |
| rejecting rather than dropping a non-tenant row | Dropping hides the parsing failure that produced it. |
| two separate confidence fields | Evidence quality and reading quality are different axes and call for different review. |
| `to_number()` returning `None`, not `0` | A zero rent looks like data. |
| `to_iso_term` keeping `month` precision | Padding to a day fabricates a fact the source never carried. |
| `emit()` honouring the review verdict | Otherwise the review queue is advisory. |
| the spec loads at module import | A YAML typo fails before any PDF is opened. |

---

## 5. Consolidating into one script

The modules collapse cleanly into a single file if you want that. What to watch:

- **Drop the `sys.path.insert` and cross-imports** — in one file they are just
  function calls. But keep the *sharing*: the whole point is that the text and OCR
  branches run identical slicing and validation.
- **Keep the spec external.** The config-driven constraint is what makes this
  maintainable. If you truly want one artifact, embed the YAML as a module-level
  triple-quoted string and `yaml.safe_load` it — do not inline the patterns into
  the Python.
- **Definition order:** `_field`/`_null_field` → text readers → deterministic
  extractors → `slice_region` / `build_schema` / `call_ollama` /
  `tenants_from_text` → OCR backends → validation checks → `run_one` → contract
  translation → `main`.
- **Keep the spec loads at module top** so a typo still fails at import.
- **Keep `mock_parse` out of the fallback path** even when it is in the same file.
  The temptation is much higher once it is three functions away.
