#!/usr/bin/env bash
# setup.sh: build the environment and prove it works.
#
#   ./setup.sh              core only; scanned documents fail closed
#   ./setup.sh --with-ocr   also installs the OCR backend (~3 GB)
#
# Idempotent: it detects what is already there and re-running is safe. A green
# deterministic gate proves the environment; a green model gate proves the whole
# chain.

set -euo pipefail
cd "$(dirname "$0")"

WITH_OCR=0
[[ "${1:-}" == "--with-ocr" ]] && WITH_OCR=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python -----
say "Python"
PY=""
for c in python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    if [[ "$(printf '%s\n3.11\n' "$v" | sort -V | head -1)" == "3.11" ]]; then
      PY="$c"; break
    fi
  fi
done
[[ -n "$PY" ]] || die "need Python 3.11 or newer"
echo "    $PY ($($PY -V 2>&1))"

# ------------------------------------------------------------------- venv ----
say "Virtual environment"
if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
  echo "    created .venv"
else
  echo "    .venv already exists"
fi
VPY=".venv/bin/python"

# Never install into the ambient interpreter. Doing that once on a machine with a
# scientific stack pulled numpy forward and broke every package compiled against
# the previous major version. The venv is not ceremony.
"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install -r requirements.txt --quiet
echo "    core dependencies installed"

if [[ "$WITH_OCR" == "1" ]]; then
  say "OCR backend (large)"
  "$VPY" -m pip install -r requirements-ocr.txt --quiet
  echo "    OCR backend installed"
else
  warn "OCR not installed. Scanned documents will route to out/review_queue.json."
  warn "Re-run with --with-ocr if you need them. Check first:"
  warn "  .venv/bin/python src/triage.py /path/to/your/oms"
fi

# ----------------------------------------------------------------- ollama ----
MODEL=$("$VPY" - <<'EOF'
import yaml, pathlib
print(yaml.safe_load(pathlib.Path("spec/om_extraction_spec.yaml").read_text())["llm"]["model"])
EOF
)
say "Ollama + model ($MODEL)"
if ! command -v ollama >/dev/null 2>&1; then
  warn "ollama not found. Install it from https://ollama.com (or: brew install ollama)."
  warn "The deterministic gate below will still run; the model gate will not."
else
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    warn "ollama is installed but not responding on :11434. Start it with: ollama serve &"
  else
    if ollama list 2>/dev/null | grep -q "^${MODEL%%:*}"; then
      echo "    $MODEL already pulled"
    else
      echo "    pulling $MODEL (this is several GB)"
      ollama pull "$MODEL"
    fi
  fi
fi

# --------------------------------------------------------------- fixtures ----
say "Fixtures"
"$VPY" fixtures/generate_fixtures.py | tail -3

# ------------------------------------------------------------------ gates ----
say "Gate 1/2: deterministic (proves the environment)"
"$VPY" eval/run_eval.py | tail -2

say "Gate 2/2: mock smoke (proves the harness, no model needed)"
LLM_MOCK=1 "$VPY" eval/run_eval_llm.py | tail -6

say "Done"
cat <<'EOF'
    Next:
      .venv/bin/python eval/run_eval_llm.py            # real model gate (~3 min)
      .venv/bin/python src/pipeline.py fixtures/pdfs   # end to end
      .venv/bin/python src/emit.py fixtures/pdfs       # typed contract JSON

    Then point it at your own documents:
      .venv/bin/python src/triage.py /path/to/your/oms
EOF
