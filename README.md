# om-extract

Extract structured rent roll data from commercial real estate Offering Memorandums.
Runs entirely on your own machine — no API keys, no uploads, no per-page cost.

An OM is forty pages of photography and prose wrapped around a handful of numbers
somebody actually needs. This pulls out the numbers, tells you how much to trust
them, and refuses to guess when it cannot read something.

```
Offering Memorandum - 2026-04-06 - Final.pdf   text_layer  text  ok      conf=medium  rows=4
Offering Memorandum - 2026-08-19 - Final.pdf   text_layer  text  ok      conf=medium  rows=10
Offering Memorandum - 2024-09-30 - SCAN.pdf    scanned     ocr   ok      conf=low     rows=3
Offering Memorandum - 2026-06-15 - Draft.pdf   text_layer  text  review  conf=none    rows=2

3 validated, 1 to review, 17 rows -> out/
```

---

## What it pulls out

**Per document** — address, county, year built, floors, rentable square footage,
tax parcel number, document date and revision state, listing contacts.

**Per tenant** — name, suite, rentable SF, percent of building, lease expiration,
in-place base rent, market rent. One row per tenant-per-document, typed and
normalized:

```json
{
  "document_id": "OM-2026-04-06",
  "property_address": "900 Kingsbridge Road",
  "total_rentable_sf": 486300,
  "tenant_count": 4,
  "tenants": [
    {
      "document_id": "OM-2026-04-06",
      "tenant_name": "Fairmont Ridge Bank",
      "rentable_sf": 52180,
      "pct_of_building": 10.7,
      "lease_expiration": "2026-12",
      "lease_expiration_precision": "month",
      "in_place_base_rent": 25.40,
      "market_rent": 31.0
    }
  ],
  "extraction_confidence": "High",
  "row_confidence": "Medium"
}
```

---

## The design idea

**Most of this is deterministic, not generative.** A regex on `Parcel ID Number`
is 100% reliable where a 14B model is about 95%. So the language model gets
exactly one job — parse the rent roll table — because that is the only place where
layout and label wording genuinely drift beyond what regex can follow.

The model never sees the whole document. It sees a sliced region, usually six to
twenty lines. Everything else is pattern matching.

```
PDF
 ├─ triage         per-page char density → text_layer | scanned | hybrid
 ├─ deterministic  regex: parcel, dates, version, property facts, contacts
 ├─ BRANCH
 │    text_layer     → llm_pass                    source "llm"
 │    scanned|hybrid → ocr_pass → llm_pass         source "ocr"
 ├─ validate       spec-driven checks; anything blocking → review queue
 └─ emit           typed contract JSON
```

Four rules hold everywhere:

**Fail closed.** Anything ambiguous, unparseable, or unverifiable goes to
`out/review_queue.json` with a named reason. Never a guess, never a partial row,
never a silent repair of model output.

**Blank is not unreadable.** Every extracted field carries
`{value, source, found}`. A document that prints no parcel number is a different
situation from one whose parcel number could not be read, and a consumer needs to
be able to tell them apart.

**Config-driven.** All patterns, prompts, thresholds, field definitions and
validation rules live in [`spec/om_extraction_spec.yaml`](spec/om_extraction_spec.yaml).
The `llm.fields` block drives both the prompt and the JSON schema that validates
the model's output, so the two cannot drift. Adding a field is a YAML edit.

**Worst source wins.** A record's confidence is capped by its weakest evidence,
not raised by its best. Rows recovered by OCR make the whole record `low`, however
clean the regex fields around them were.

---

## Two confidence numbers, on purpose

`extraction_confidence` scores the **document evidence** — was a parcel number or
a listing contact block actually found? `row_confidence` scores **how the rows
were read** — regex, model over a text layer, or OCR then model.

They genuinely differ. A scanned OM with a crisp printed parcel number is `High`
on evidence and `Low` on reading. Collapsing them into one number makes that
document indistinguishable from one where the evidence itself is thin, and those
two call for completely different review.

---

## The subtotal problem

This is the failure mode that motivated most of the validation layer.

A rent roll's total rows look exactly like tenant rows. Nothing structural
separates them — same columns, same shapes, often a full set of figures:

```
Whitlock and Pryce, LLP     35,404    7.3%    Dec-21   27.30    34.00   -20%
Tenants <15K RSF            96,470    19.8%
Leased Storage              5,120     1.1%
TOTAL OFFICE LEASED         431,700   88.8%   Jan-25   26.15    32.40   -19%
BOMA Adjustment             4,260     0.9%
Available                   50,340    10.4%
```

Emit `TOTAL OFFICE LEASED` as a tenant and you have a 431,700 SF phantom lease at
a blended rent that looks entirely plausible to anyone reading the output.

So it is defended twice. The prompt lists the labels to skip, and
`validation.non_tenant_row_patterns` rejects the whole record if one gets through.
It **rejects rather than drops the row** — silently deleting it would hide a real
parsing failure, and this failure is too expensive to hide.

---

## Column alignment is a lie

The other lesson worth stealing. Three consecutive rows of one table, exactly as
`pdfplumber` returns them:

```
Fairmont Ridge Bank         52,180    10.7%   Dec-26   25.40    31.00   -18%
Delacroix Media Publishing LLC 48,915 10.1%   Sep-24   21.85    31.00   -30%
Sandoval Hospitality Services, LLC 41,260 8.5% Apr-21  23.75    31.00   -23%
```

Same table. The rows with short tenant names stay neatly column-aligned; the rows
with long names push their figures out of position and collapse the gaps. An
extractor keying on horizontal position gets the first row right and the next two
wrong. The prompt tells the model to read by column *order* relative to the
square-footage anchor instead.

Which is also why the region is read with `pdfplumber extract_text(layout=True)`
and the sliced lines are deliberately **not** stripped. Collapsed text turns a row
with an empty cell into

```
Acme Holdings LLC 12,400 2.5% 28.75
```

where nothing says whether `28.75` is the in-place rent or the market rent. Layout
mode keeps the gap, so an empty cell is visible as an empty cell. Use collapsed
text for regex, layout text for tables.

---

## Install

```bash
git clone https://github.com/<you>/om-extract.git
cd om-extract
./setup.sh                  # core; scanned documents fail closed
./setup.sh --with-ocr       # also installs docling (~3 GB) for scanned documents
```

`setup.sh` checks Python, builds the venv, installs pinned dependencies, verifies
Ollama is running, pulls the model named in the spec, regenerates the fixtures and
runs the gates. It is safe to re-run.

Manually:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ollama && ollama serve &     # or the signed installer from ollama.com
ollama pull qwen3:14b                     # 9.3 GB
.venv/bin/python fixtures/generate_fixtures.py
```

Requires Python 3.11+, about 10 GB of disk for the model, and outbound access to
pypi.org and ollama.com **at install time only**. At run time nothing leaves the
machine.

---

## Use

```bash
# extraction only -> out/validated.json, out/review_queue.json, out/pipeline_report.json
.venv/bin/python src/pipeline.py /path/to/your/oms

# extraction + typed contract JSON -> out/om_records.json, out/emit_errors.json
.venv/bin/python src/emit.py /path/to/your/oms

# any single pass, standalone, over a file or a folder
.venv/bin/python src/triage.py        /path/to/oms
.venv/bin/python src/deterministic.py /path/to/oms
.venv/bin/python src/llm_pass.py      /path/to/oms
```

Start with `triage.py` on a sample of your own documents. It tells you how many
are scanned, which is what decides whether you need the OCR install at all.
Executed, digitally-produced OMs usually carry a text layer.

### Retargeting it

The spec is the product. To point this at a different table — an operating
statement, a sales comp set, a lease abstract — edit
`spec/om_extraction_spec.yaml`:

- `llm.region.start_pattern` — the heading that opens the table
- `llm.region.row_pattern` — a stable-shaped token every data row carries
- `llm.fields` — the columns you want (drives prompt *and* schema)
- `validation.row_checks` — the shapes those values must satisfy
- `validation.non_tenant_row_patterns` — labels that are not data rows

No Python changes. That constraint is what keeps the thing maintainable.

---

## Gates

```bash
.venv/bin/python eval/run_eval.py                    # 116/116 deterministic checks
LLM_MOCK=1 .venv/bin/python eval/run_eval_llm.py     # smoke: harness + schema + fail-closed
.venv/bin/python eval/run_eval_llm.py                # real model, gate ≥98%
.venv/bin/python eval/run_eval_ocr.py                # OCR branch, gate ≥98%
```

`fixtures/generate_fixtures.py` builds fifteen synthetic OMs, each encoding one
trap taken from real brokerage documents:

| Trap | |
|---|---|
| parcel number repeated on every page | must dedupe |
| no parcel number at all | contact block is the only evidence |
| one tenant, two suites | two rows, never merged |
| image-only pages, ruled table | routes to the OCR branch |
| two distinct parcel numbers | flag for review, never pick one |
| `3.9.2026` filename | m.d.yyyy date format |
| `Sept 2019` filename | month precision only — never padded to a day |
| label drift | Occupant / Rentable SF / Expires / Annual Base Rent |
| `$`-prefixed money | Sq. Ft. and Lease End labels |
| Escalation + Security Deposit columns | trailing distractors |
| TI Allowance + Rent/Month columns | distractors bracketing the real ones |
| eight subtotal and summary rows | one carrying a full set of figures |
| blank expiration, blank market rent | null, not guessed |
| tenant names wrapping to a second line | joined, not emitted as their own row |
| ten tenants split across two pages | continued under a repeated heading |
| a Prior Year Lease Schedule | must never be read |

All fixture data is invented. No real document is included in this repository, and
none should be — see [`SECURITY.md`](SECURITY.md) before running this against
anything confidential.

Scoring notes worth copying if you build something similar:

- **Field accuracy denominates over all ground-truth rows × fields**, so the
  fields of a *missing* row count as wrong. Returning one perfect row and dropping
  the rest cannot score well.
- **Normalization is symmetric on both sides.** Casefold and whitespace-collapse
  for text, `$` and `,` stripped for numbers. That is fair comparison, not repair —
  `llm_pass.py` never edits what the model returns, it only accepts or rejects it.
- **Rows key on `(tenant_name, rentable_sf)`, not name alone.** A tenant in three
  suites is three rows, and those rows *are* the lease structure. A scorer keyed on
  the name would merge them and then report the survivors as missing — a scorer
  contradicting the contract it scores.
- **Subtotals emitted as tenants are counted, never averaged.** A count of 1 fails
  the gate outright. Some errors do not deserve to be diluted across a corpus.

---

## Notes from building it

**`think: false` is load-bearing.** With `format: "json"` the JSON grammar
constrains the sampler while a reasoning model still wants to emit think tokens.
On qwen3:14b that conflict reproducibly appended a stray `" /"` to the last value
of the *last* row, at temperature 0. Debug the inference mode before the prompt
when an artifact is deterministic and position-specific.

**Negative prompt rules can backfire.** Adding *"never append a trailing slash"*
made accuracy worse and spread the artifact to a document that had been clean.
Naming a forbidden token puts it in context and raises its probability. Rules
describing what the layout **is** work; rules naming a mistake invite it.

**Check the fixture before blaming the model.** The OCR branch scored 0%, then
60%, for two reasons that were both bugs in the *fixture*: space-padded columns
that defeated table detection, and a table running past the page edge whose
clipped glyphs transcribed as `4,7!`. Give rasterized tables ruling lines — layout
models key on them.

**Don't tune a test double to pass.** `LLM_MOCK=1` swaps in a deterministic
anchored parser so slicing and validation can be regression-tested with no model
running. It cannot read a distractor column and does not know what a subtotal is,
so mock mode gates on what it *can* prove — schema validity and fail-closed
behaviour — and prints accuracy as information only. It is never consulted when a
real model call fails; that would mask the failure.

---

## Layout

```
spec/om_extraction_spec.yaml   all pattern knowledge — the single source of truth
spec/om_record_schema.json     the output contract
src/triage.py                  text_layer / scanned / hybrid
src/deterministic.py           the zero-LLM pass
src/llm_pass.py                region slicing, prompt, schema validation
src/ocr_pass.py                OCR → the same llm_pass
src/validate.py                spec-driven checks, confidence, review routing
src/pipeline.py                entry point
src/emit.py                    typed contract JSON
fixtures/generate_fixtures.py  the synthetic corpus and its ground truth
eval/                          the gates
```

[`ARCHITECTURE.md`](ARCHITECTURE.md) has the module-by-module detail, the data
shapes, and the decisions that look like dead weight until you remove one.

---

## Limitations

- The labelled-fields regexes assume a single-column property details block. Real
  OMs often set it as two columns, which the collapsed text extractor interleaves
  onto one line. Documented rather than papered over.
- OCR runs on CPU at roughly 40 s/document. There is no GPU path.
- The rent roll must sit under a heading matching `region.start_pattern`. A table
  with no heading at all is not found, and that is a deliberate trade — the same
  narrowness is what keeps a "Prior Year Lease Schedule" out of your results.
- Nothing here calls a public records API, a broker platform, or any external
  service. Everything comes out of the PDF you gave it.

## License

MIT. See [LICENSE](LICENSE).
