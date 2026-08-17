# -*- coding: utf-8 -*-
"""Attach the YOLO solver to an existing Chromium debugging endpoint."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence, Tuple

from result import SolveStatus

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
EXIT_DEPENDENCY, EXIT_CONNECTION, EXIT_INTERNAL = 3, 4, 5


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def parse_cdp(value: str) -> Tuple[str, int]:
    raw = str(value or "").strip()
    if "://" in raw:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        host, port = parsed.hostname, parsed.port
    else:
        host, separator, port_text = raw.rpartition(":")
        port = int(port_text) if separator and port_text.isdigit() else None
    if not host or port is None or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("CDP address must be HOST:PORT")
    return host, port


def address_text(address: Tuple[str, int]) -> str:
    return f"{address[0]}:{address[1]}"


def attach(address: Tuple[str, int]):
    from DrissionPage import ChromiumOptions, ChromiumPage

    options = ChromiumOptions(read_file=False).set_address(address_text(address))
    return ChromiumPage(addr_or_opts=options)


def select_tab(browser, tab_id: Optional[str], title: Optional[str], url: Optional[str]):
    if tab_id:
        return browser.get_tab(tab_id)
    if not title and not url:
        return browser.latest_tab
    title_term, url_term = (title or "").casefold(), (url or "").casefold()
    for tab in browser.get_tabs():
        if title_term and title_term not in str(tab.title).casefold():
            continue
        if url_term and url_term not in str(tab.url).casefold():
            continue
        return tab
    raise LookupError("no tab matches the requested selector")


def tab_info(tab) -> Dict[str, str]:
    return {
        "id": str(getattr(tab, "tab_id", "") or ""),
        "title": str(getattr(tab, "title", "") or ""),
        "url": str(getattr(tab, "url", "") or ""),
    }


def dependency_probe() -> Dict[str, Any]:
    missing = [
        name for name in ("DrissionPage", "requests", "PIL")
        if importlib.util.find_spec(name) is None
    ]
    return {"ok": not missing, "missing": missing}


def api_probe(timeout: float) -> Dict[str, Any]:
    base = os.environ.get("CAPTCHA_API_URL", "").strip().rstrip("/")
    if not base:
        return {"ok": False, "error": "CAPTCHA_API_URL is empty"}
    candidates = [base] if base.endswith("/predict") else [f"{base}/health", base]
    error = "unreachable"
    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return {"ok": response.status < 500, "url": url, "status": response.status}
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return {"ok": True, "url": url, "status": exc.code}
            error = f"HTTP {exc.code}"
        except Exception as exc:
            error = str(exc)
    return {"ok": False, "url": base, "error": error}


def run_solve(args: argparse.Namespace) -> int:
    started = time.monotonic()
    try:
        browser = attach(args.cdp)
        tab = select_tab(browser, args.tab_id, args.title, args.url)
        from solver import handle_recaptcha_yolo

        status = handle_recaptcha_yolo(tab, args.artifacts, args.max_rounds)
        emit({
            "ok": status is SolveStatus.SUCCESS,
            "command": "solve",
            "status": status.value,
            "cdp": address_text(args.cdp),
            "tab": tab_info(tab),
            "artifacts": os.path.abspath(args.artifacts),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        return EXIT_OK if status is SolveStatus.SUCCESS else EXIT_FAILED
    except ModuleNotFoundError as exc:
        emit({"ok": False, "command": "solve", "status": "dependency_error", "error": str(exc)})
        return EXIT_DEPENDENCY
    except LookupError as exc:
        emit({"ok": False, "command": "solve", "status": "tab_not_found", "error": str(exc)})
        return EXIT_USAGE
    except (ConnectionError, OSError) as exc:
        emit({"ok": False, "command": "solve", "status": "connection_error", "error": str(exc)})
        return EXIT_CONNECTION
    except Exception as exc:
        emit({"ok": False, "command": "solve", "status": "internal_error", "error": str(exc)})
        return EXIT_INTERNAL
    # Never quit the attached browser: this process does not own it.


def run_doctor(args: argparse.Namespace) -> int:
    dependencies, api = dependency_probe(), api_probe(args.timeout)
    cdp: Dict[str, Any] = {"ok": None, "address": address_text(args.cdp)}
    if args.check_cdp:
        try:
            browser = attach(args.cdp)
            cdp.update(ok=True, tabs=len(browser.tab_ids))
        except Exception as exc:
            cdp.update(ok=False, error=str(exc))
    ok = dependencies["ok"] and api["ok"] and cdp["ok"] is not False
    emit({"ok": ok, "command": "doctor", "dependencies": dependencies, "api": api, "cdp": cdp})
    if not dependencies["ok"]:
        return EXIT_DEPENDENCY
    if cdp["ok"] is False:
        return EXIT_CONNECTION
    return EXIT_OK if api["ok"] else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Solve reCAPTCHA on an existing Chromium tab")
    commands = parser.add_subparsers(dest="command", required=True)
    solve = commands.add_parser("solve")
    solve.add_argument("--cdp", type=parse_cdp, default=parse_cdp("127.0.0.1:9222"))
    selector = solve.add_mutually_exclusive_group()
    selector.add_argument("--tab-id")
    selector.add_argument("--title", help="case-insensitive title substring")
    selector.add_argument("--url", help="case-insensitive URL substring")
    solve.add_argument("--max-rounds", type=int, default=24)
    solve.add_argument("--artifacts", default="output/recaptcha-yolo")
    solve.set_defaults(handler=run_solve)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--cdp", type=parse_cdp, default=parse_cdp("127.0.0.1:9222"))
    doctor.add_argument("--check-cdp", action="store_true")
    doctor.add_argument("--timeout", type=float, default=3.0)
    doctor.set_defaults(handler=run_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())