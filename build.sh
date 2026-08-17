#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m PyInstaller --noconfirm --clean --onefile \
  --name recaptcha-yolo \
  --collect-all DrissionPage \
  --collect-all PIL \
  recaptcha_yolo.py
printf 'Built %s/dist/recaptcha-yolo\n' "$PWD"