# Handling real documents

Everything in this repository is synthetic. This note covers two points to settle before running the pipeline on real documents.

## Do not commit offering memorandums

An OM is confidential brokerage material. It is circulated under a confidentiality agreement, it usually says so on page one, and the recipient has no right to redistribute it. That applies equally to the low-resolution marketing copies that circulate widely. `.gitignore` excludes `private/` and `*.om.pdf` as a precaution, but the reliable control is to keep these files out of the working tree.

Keep your own test documents outside the repository, or in `private/`, and pass the pipeline their path:

```bash
.venv/bin/python src/pipeline.py ~/Documents/oms
```

To build a regression corpus that can be shared, extend `fixtures/generate_fixtures.py`. That file exists to encode a trap found in a real document as synthetic data.

## What local processing covers

Running locally means there are no credentials, no vendor account and no per-page cost. No third party can read a document you were trusted with, and no data leaves the machine at run time. This was the design premise, and it is the reason the language model is Ollama on `localhost` and not a hosted API. Substituting a cloud LLM or OCR service for comparison removes that property.

Running locally does not exempt the work from your organization's software, data-handling and endpoint policies. Processing confidential documents on a work machine is still processing confidential data. Local processing is a stronger position, but a data policy asks about the data and not about the network, so confirm what yours allows before you start.

Two practical notes apply to a managed machine:

- Ollama runs an HTTP server on `127.0.0.1:11434`. It does not listen on an external interface, but some endpoint agents flag any listening process.
- Installation needs outbound access to `pypi.org` and `ollama.com`. Nothing needs network access after that. If the model pull is blocked, the model store can be copied as plain files, for example with `rsync ~/.ollama/models/` from a machine that could pull it. Ollama's store is content-addressed and its blobs are named by SHA-256, so merging two stores is safe.

## Reporting a vulnerability

Open an issue for anything that is not sensitive. For a report you would prefer not to post publicly, open an issue that asks for a contact address and leave the details out.
