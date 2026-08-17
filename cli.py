# -*- coding: utf-8 -*-
"""Backward-compatible entry point; use recaptcha_yolo.py for new callers."""
from recaptcha_yolo import main


if __name__ == "__main__":
    raise SystemExit(main())