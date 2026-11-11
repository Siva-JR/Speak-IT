#!/usr/bin/env bash
# One-shot setup: venv + deps + models + test data. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python pywhispercpp || echo "WARN: pywhispercpp failed to build, whisper_cpp candidates will fail"
.venv/bin/python scripts/download_models.py
.venv/bin/python scripts/prepare_data.py
echo "ready. run: .venv/bin/python run.py --list ; .venv/bin/python run.py --limit 3 --repeats 1  (smoke) ; .venv/bin/python run.py"
