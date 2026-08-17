# reCAPTCHA YOLO CLI

This directory contains a standalone command-line solver. It attaches to an
already running Chromium instance through its remote debugging endpoint. It
does not launch, navigate, refresh, close, or otherwise own that browser.

Use it only on pages and automation environments you are authorized to test.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Set the YOLO service URL. The configured endpoint may be either the API base
URL or the `/predict` URL accepted by `yolo_client.py`.

```bash
export CAPTCHA_API_URL=http://127.0.0.1:8000
```

## Run

Start Chromium separately with remote debugging enabled, then attach by CDP.
With no selector, the most recently active tab is used.

```bash
python3 recaptcha_yolo.py doctor --check-cdp --cdp 127.0.0.1:9222
python3 recaptcha_yolo.py solve --cdp 127.0.0.1:9222
python3 recaptcha_yolo.py solve --cdp 127.0.0.1:9222 --title Login
python3 recaptcha_yolo.py solve --cdp 127.0.0.1:9222 --url example.com/account
python3 recaptcha_yolo.py solve --cdp 127.0.0.1:9222 --tab-id TAB_ID
```

The final stdout line is compact JSON. Exit codes are stable: `0` success,
`1` solve/API failure, `2` invalid selection, `3` missing dependency, `4` CDP
connection failure, and `5` unexpected internal failure. Solver diagnostics may
also be written before the final JSON line.

## Build One File

```bash
python3 -m pip install -r requirements-build.txt
chmod +x build.sh
./build.sh
./dist/recaptcha-yolo --help
```

The executable still requires network access to the configured YOLO service
and access to the Chromium CDP address. Build on the target operating system;
PyInstaller output is platform-specific.

## Test

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py tests/*.py
```