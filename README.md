# om-extract

om-extract pulls structured rent roll data out of commercial real estate offering memorandums (OMs). It runs entirely on the local machine. It needs no API key, uploads nothing and has no per-page cost.

An OM is typically about forty pages of photography and prose around a small set of figures that an analyst needs. om-extract returns those figures, rates how far each record can be trusted, and sends a document to a review queue when it cannot read something.

```
Offering Memorandum - 2026-04-06 - Final.pdf   text_layer  text  ok      conf=medium  rows=4
Offering Memorandum - 2026-08-19 - Final.pdf   text_layer  text  ok      conf=medium  rows=10
Offering Memorandum - 2024-09-30 - SCAN.pdf    scanned     ocr   ok      conf=low     rows=3
Offering Memorandum - 2026-06-15 - Draft.pdf   text_layer  text  review  conf=none    rows=2

3 validated, 1 to review, 17 rows -> out/
```

## Extracted fields

For each document: address, county, year built, floors, rentable square footage, tax parcel number, document date, revision state and listing contacts.

For each tenant: name, suite, rentable square feet, percent of building, lease expiration, in-place base rent and market rent. The output has one typed, normalized row per tenant per document:

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

## Design

Most of the pipeline is deterministic. A regular expression on `Parcel ID Number` is fully reliable, while a 14B model reads the same field correctly about 95% of the time. The language model is therefore used for one task only, which is parsing the rent roll table. That table is the one place where layout and label wording vary more than a regular expression can follow.

The model never receives the whole document. It receives a sliced region, usually 6 to 20 lines, and everything else is pattern matching.

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

The pipeline follows four rules.

1. **It fails closed.** Anything ambiguous, unparseable or unverifiable goes to `out/review_queue.json` with a named reason. The pipeline does not guess, emit a partial row or repair model output.
2. **A blank field is distinct from an unreadable one.** Every extracted field carries `{value, source, found}`. A document that prints no parcel number is a different case from one whose parcel number could not be read, and a consumer of the output needs to tell the two apart.
3. **Behavior is set in configuration.** All patterns, prompts, thresholds, field definitions and validation rules are in [`spec/om_extraction_spec.yaml`](spec/om_extraction_spec.yaml). The `llm.fields` block generates both the prompt and the JSON schema that validates the model's output, so the two stay consistent. Adding a field is a YAML edit.
4. **The weakest source sets the confidence.** A record's confidence is capped by its weakest evidence. Rows recovered by OCR make the whole record `low`, even when the surrounding regex fields were clean.

## Two confidence fields

`extraction_confidence` rates the document evidence, for example whether a parcel number or a listing contact block was found. `row_confidence` rates how the rows were read: by regex, by the model over a text layer, or by OCR followed by the model.

The two can differ. A scanned OM with a clearly printed parcel number is `High` on evidence and `Low` on reading. A single combined score would make that document look the same as one with thin evidence, and the two need different kinds of review.

## Subtotal rows

Most of the validation layer exists because of this failure.

The total rows of a rent roll have the same structure as tenant rows. They share the same columns and often carry a full set of figures:

```
Whitlock and Pryce, LLP     35,404    7.3%    Dec-21   27.30    34.00   -20%
Tenants <15K RSF            96,470    19.8%
Leased Storage              5,120     1.1%
TOTAL OFFICE LEASED         431,700   88.8%   Jan-25   26.15    32.40   -19%
BOMA Adjustment             4,260     0.9%
Available                   50,340    10.4%
```

If `TOTAL OFFICE LEASED` were emitted as a tenant, the output would contain a 431,700 SF lease that does not exist, at a blended rent that looks plausible to a reader.

The pipeline guards against this in two places. The prompt lists the labels to skip, and `validation.non_tenant_row_patterns` rejects the whole record if one of those rows gets through. The validator rejects the record instead of dropping the row, because dropping it would hide a parsing failure.

## Column alignment

The following three consecutive rows come from one table, as returned by `pdfplumber`:

```
Fairmont Ridge Bank         52,180    10.7%   Dec-26   25.40    31.00   -18%
Delacroix Media Publishing LLC 48,915 10.1%   Sep-24   21.85    31.00   -30%
Sandoval Hospitality Services, LLC 41,260 8.5% Apr-21  23.75    31.00   -23%
```

The row with a short tenant name stays aligned to its columns. The rows with long names push their figures out of position and close the gaps. An extractor that relies on horizontal position reads the first row correctly and the next two incorrectly. For this reason the prompt tells the model to read by column order relative to the square-footage figure.

The same problem explains why the region is read with `pdfplumber extract_text(layout=True)` and why the sliced lines are not stripped. Collapsed text turns a row with an empty cell into

```
Acme Holdings LLC 12,400 2.5% 28.75
```

and nothing in that line shows whether `28.75` is the in-place rent or the market rent. Layout mode preserves the gap, so an empty cell remains visible. The pipeline uses collapsed text for regex matching and layout text for tables.

## Installation

```bash
git clone https://github.com/kyletristentran/om-extract.git
cd om-extract
./setup.sh                  # core; scanned documents fail closed
./setup.sh --with-ocr       # also installs docling (~3 GB) for scanned documents
```

`setup.sh` checks the Python version, builds the virtual environment, installs pinned dependencies, verifies that Ollama is running, pulls the model named in the spec, regenerates the fixtures and runs the evaluation gates. It can be run again safely.

To install manually:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ollama && ollama serve &     # or the signed installer from ollama.com
ollama pull qwen3:14b                     # 9.3 GB
.venv/bin/python fixtures/generate_fixtures.py
```

The project requires Python 3.11 or later, about 10 GB of disk space for the model, and outbound access to pypi.org and ollama.com during installation. At run time nothing leaves the machine.

## Usage

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

Start with `triage.py` on a sample of your own documents. It reports how many are scanned, which determines whether the OCR install is needed. Executed OMs that were produced digitally usually carry a text layer.

### Pointing it at a different table

The spec controls what is extracted. To target a different table, such as an operating statement, a sales comp set or a lease abstract, edit `spec/om_extraction_spec.yaml`:

- `llm.region.start_pattern`: the heading that opens the table
- `llm.region.row_pattern`: a token with a stable shape that every data row carries
- `llm.fields`: the columns to extract (this generates both the prompt and the schema)
- `validation.row_checks`: the shapes those values must satisfy
- `validation.non_tenant_row_patterns`: labels that are not data rows

None of these changes requires editing Python.

## Evaluation gates

```bash
.venv/bin/python eval/run_eval.py                    # 116/116 deterministic checks
LLM_MOCK=1 .venv/bin/python eval/run_eval_llm.py     # smoke: harness + schema + fail-closed
.venv/bin/python eval/run_eval_llm.py                # real model, gate ≥98%
.venv/bin/python eval/run_eval_ocr.py                # OCR branch, gate ≥98%
```

`fixtures/generate_fixtures.py` builds fifteen synthetic OMs. Each one encodes a trap taken from real brokerage documents:

| Trap | Expected handling |
|---|---|
| parcel number repeated on every page | must dedupe |
| no parcel number at all | contact block is the only evidence |
| one tenant, two suites | two rows, never merged |
| image-only pages, ruled table | routes to the OCR branch |
| two distinct parcel numbers | flag for review, never pick one |
| `3.9.2026` filename | m.d.yyyy date format |
| `Sept 2019` filename | month precision only, never padded to a day |
| label drift | Occupant / Rentable SF / Expires / Annual Base Rent |
| `$`-prefixed money | Sq. Ft. and Lease End labels |
| Escalation + Security Deposit columns | trailing distractors |
| TI Allowance + Rent/Month columns | distractors bracketing the real ones |
| eight subtotal and summary rows | one carrying a full set of figures |
| blank expiration, blank market rent | null, not guessed |
| tenant names wrapping to a second line | joined, not emitted as their own row |
| ten tenants split across two pages | continued under a repeated heading |
| a Prior Year Lease Schedule | must never be read |

All fixture data is invented. The repository contains no real document. Read [`SECURITY.md`](SECURITY.md) before running the pipeline on anything confidential.

The scoring follows four conventions:

- Field accuracy is computed over all ground-truth rows and fields, so the fields of a missing row count as wrong. A run that returns one perfect row and drops the rest scores poorly.
- Normalization is applied to both sides of the comparison. Text is casefolded and its whitespace collapsed, and `$` and `,` are stripped from numbers. This makes the comparison fair without repairing anything: `llm_pass.py` never edits what the model returns and only accepts or rejects it.
- Rows are keyed on `(tenant_name, rentable_sf)`. A tenant in three suites has three rows, and those rows describe the lease structure. A scorer keyed on the name alone would merge them and then report the others as missing, which would contradict the output contract.
- Subtotals emitted as tenants are counted, not averaged. A count of 1 fails the gate.

## Notes from development

- **`think: false` is required.** With `format: "json"`, the JSON grammar constrains the sampler while a reasoning model still tries to emit think tokens. On qwen3:14b this reproducibly appended a stray `" /"` to the last value of the last row at temperature 0. When an artifact is deterministic and tied to one position, check the inference mode before changing the prompt.
- **Negative prompt rules made results worse.** Adding the rule "never append a trailing slash" lowered accuracy and spread the artifact to a document that had been clean. Naming a forbidden token places it in context and raises its probability. Rules that describe the layout worked, and rules that named a mistake did not.
- **Two OCR failures were fixture bugs.** The OCR branch scored 0% and then 60% for two reasons, both in the fixture: space-padded columns that defeated table detection, and a table that ran past the page edge, whose clipped glyphs were transcribed as `4,7!`. Rasterized tables need ruling lines, because layout models rely on them.
- **The mock is not tuned to pass.** `LLM_MOCK=1` substitutes a deterministic anchored parser, so slicing and validation can be regression-tested without a running model. The mock cannot read a distractor column and does not recognize a subtotal. Mock mode therefore gates only on schema validity and fail-closed behavior, and it prints accuracy for information. The mock is never used when a real model call fails, because that would hide the failure.

## Repository layout

```
spec/om_extraction_spec.yaml   all pattern knowledge (single source of truth)
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

[`ARCHITECTURE.md`](ARCHITECTURE.md) describes each module, the data shapes and the design decisions that should not be simplified.

## Limitations

- The labeled-field regexes assume a property details block set in a single column. Real OMs often set it in two columns, which the collapsed text extractor interleaves onto one line. This case is not handled.
- OCR runs on CPU at roughly 40 seconds per document. There is no GPU path.
- The rent roll must sit under a heading that matches `region.start_pattern`. A table with no heading is not found. This restriction is intentional, because it also keeps a "Prior Year Lease Schedule" out of the results.
- The pipeline calls no public records API, broker platform or other external service. All output comes from the PDF it was given.

## License

MIT. See [LICENSE](LICENSE).
