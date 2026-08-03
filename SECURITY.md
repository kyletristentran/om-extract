# Handling real documents

Everything in this repository is synthetic. Before you point it at anything real,
two things are worth being deliberate about.

## Do not commit Offering Memorandums

An OM is confidential brokerage material. It is circulated under a confidentiality
agreement, it usually says so on page one, and it is not yours to redistribute —
including the "low res" marketing copies that circulate freely. `.gitignore`
excludes `private/` and `*.om.pdf` as a speed bump, but the real defence is not
putting them in the working tree.

Keep your own test documents outside the repo, or in `private/`, and point the
pipeline at them by path:

```bash
.venv/bin/python src/pipeline.py ~/Documents/oms
```

If you want a shareable regression corpus, extend
`fixtures/generate_fixtures.py` instead. Encoding a trap you hit in a real
document, as synthetic data, is the whole design of that file.

## What "local" does and does not buy you

**It buys:** no credentials, no vendor account, no per-page cost, no third party
able to read a document you were trusted with, and no data leaving the machine at
run time. That was the design premise, and it is why the language model is Ollama
on `localhost` rather than a hosted API. Substituting a cloud LLM or OCR service
"just to compare" throws away the entire property.

**It does not buy:** freedom from your organization's software, data-handling, and
endpoint policy. Processing confidential documents on a work machine is still
processing confidential data — locally, which is a better posture, but the
question your policy asks is about the data, not the network. Worth being able to
answer before someone asks.

Two practical notes for a managed machine:

- Ollama runs an HTTP server on `127.0.0.1:11434`. It never listens on an external
  interface, but some endpoint agents flag any listening process.
- Installation needs outbound access to `pypi.org` and `ollama.com`. Nothing needs
  network access after that. If the model pull is blocked, Ollama's store is
  content-addressed and moves by plain file copy — `rsync ~/.ollama/models/` from
  a machine that could pull it. Blobs are named by SHA-256, so merging two stores
  is safe.

## Reporting a vulnerability

Open an issue for anything that is not itself sensitive. For something you would
rather not post publicly, open an issue asking for a contact address and leave the
details out.
