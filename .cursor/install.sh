#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Look-for-pic Web Flask app.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 1. System OCR engine: Tesseract + Japanese / Traditional-Chinese language data.
#    server.py shells out to `tesseract` for the OCR identify path.
if ! command -v tesseract >/dev/null 2>&1 \
  || ! tesseract --list-langs 2>/dev/null | grep -qx jpn \
  || ! tesseract --list-langs 2>/dev/null | grep -qx chi_tra; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-jpn tesseract-ocr-chi-tra
fi

# 2. uv — portable Python toolchain/venv manager (no sudo, no PPA).
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 3. CPython 3.13 is REQUIRED: _recovered_helpers.marshal contains 3.13 bytecode
#    that server.py loads at import time. Any other minor version skips those
#    helpers (e.g. is_mida616), which breaks the identify pipeline at runtime.
uv python install 3.13

# 4. Project virtualenv + dependencies (recreated for a deterministic result).
uv venv --clear --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

echo "Look-for-pic Web environment ready (python $(.venv/bin/python --version 2>&1))."
