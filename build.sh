#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m PyInstaller --noconfirm --clean --onefile \
  --name grid-vision \
  --collect-all DrissionPage \
  --collect-all PIL \
  recaptcha_yolo.py
printf 'Built %s/dist/grid-vision\n' "$PWD"