# -*- coding: utf-8 -*-
"""
Shared console logging for the standalone solver.

Goals:
  - Clear step banners for the renew pipeline
  - Quiet default (less tile spam); WOIDEN_LOG_VERBOSE=1 for debug
  - Light emoji markers so runs are scannable in terminal

Env:
  WOIDEN_LOG_VERBOSE=1   per-tile YOLO / DOM probe details
  WOIDEN_LOG_EMOJI=0     disable emoji (plain ASCII)
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def verbose() -> bool:
    return _flag("WOIDEN_LOG_VERBOSE", False)


def _emoji_on() -> bool:
    return _flag("WOIDEN_LOG_EMOJI", True)


# level -> emoji (optional)
_LEVEL_EMOJI = {
    "INFO": "ℹ️ ",
    "WARN": "⚠️ ",
    "ERROR": "❌ ",
    "OK": "✅ ",
    "STEP": "▶️ ",
    "DONE": "🏁 ",
    "L1": "🔁 ",
    "L2": "🔄 ",
    "L3": "🔐 ",
    "YOLO": "🧩 ",
    "TG": "✈️ ",
    "MATH": "🧮 ",
    "TS": "🛡️ ",
    "QUOTA": "🚫 ",
}

_STEP_EMOJI = {
    "START": "🚀",
    "LOGIN": "🔑",
    "TURNSTILE": "🛡️",
    "RENEW": "📝",
    "TG_CODE": "✈️",
    "CODE_PAGE": "📄",
    "MATH": "🧮",
    "RECAPTCHA": "🧩",
    "SUBMIT": "📨",
    "RESULT": "🏁",
    "L1": "🔁",
    "L2": "🔄",
    "L3": "🔐",
    "SESSION": "👤",
    "BROWSER": "🌐",
}

# rate-limit identical debug lines
_last_same: Dict[str, float] = {}
_run_t0: Optional[float] = None
_run_notes: list = []


def _e(key: str) -> str:
    if not _emoji_on():
        return ""
    return _LEVEL_EMOJI.get(key, "") or _STEP_EMOJI.get(key, "")


def log(msg: str, level: str = "INFO", tag: str = "") -> None:
    """Standard line. tag e.g. yolo / audio / api (optional)."""
    level = (level or "INFO").upper()
    prefix = f"[{level}]"
    if tag:
        prefix = f"[{tag}]{prefix}"
    em = _e(level) if level in _LEVEL_EMOJI else ""
    print(f"{prefix} {em}{msg}", flush=True)


def vlog(msg: str, level: str = "INFO", tag: str = "yolo") -> None:
    """Verbose-only (WOIDEN_LOG_VERBOSE=1)."""
    if not verbose():
        return
    log(msg, level=level, tag=tag)


def log_once(key: str, msg: str, level: str = "INFO", every: float = 2.0, tag: str = "") -> None:
    """Suppress identical key within `every` seconds (cuts UI-ready spam)."""
    now = time.time()
    prev = _last_same.get(key, 0.0)
    if now - prev < every:
        return
    _last_same[key] = now
    log(msg, level=level, tag=tag)


def step_begin(idx: int, total: int, name: str, detail: str = "") -> None:
    """Big step banner — main pipeline readability."""
    name_u = (name or "STEP").upper()
    em = _STEP_EMOJI.get(name_u, "▶️") if _emoji_on() else ">"
    bar = "─" * 12
    extra = f"  {detail}" if detail else ""
    print(f"{bar} {em} [{idx}/{total}] {name_u}{extra} {bar}", flush=True)


def step_end(name: str, ok: bool, detail: str = "", seconds: Optional[float] = None) -> None:
    name_u = (name or "STEP").upper()
    if _emoji_on():
        mark = "✅" if ok else "❌"
    else:
        mark = "OK" if ok else "FAIL"
    bits = [f"<<< {name_u} {mark}"]
    if detail:
        bits.append(str(detail)[:200])
    if seconds is not None:
        bits.append(f"{seconds:.1f}s")
    print("  ".join(bits), flush=True)


def phase(label: str, msg: str, level: str = "INFO") -> None:
    """Sub-phase inside a step: [L1] [L2] [SESSION] ..."""
    lab = (label or "").upper()
    em = _STEP_EMOJI.get(lab, "") if _emoji_on() else ""
    if em:
        em = em + " "
    log(f"[{lab}] {em}{msg}", level=level)


def run_start(title: str = "reCAPTCHA YOLO") -> None:
    global _run_t0, _run_notes
    _run_t0 = time.time()
    _run_notes = []
    em = _STEP_EMOJI.get("START", "") if _emoji_on() else ""
    line = f"{'=' * 8} {em} {title} START {'=' * 8}".strip()
    print(line, flush=True)


def run_note(note: str) -> None:
    if note:
        _run_notes.append(str(note)[:180])


def run_end(ok: bool, reason: str = "") -> None:
    global _run_t0
    elapsed = (time.time() - _run_t0) if _run_t0 else 0.0
    em = _STEP_EMOJI.get("RESULT", "") if _emoji_on() else ""
    status = "SUCCESS" if ok else "FAIL"
    mark = "✅" if (ok and _emoji_on()) else ("❌" if _emoji_on() else status)
    print(f"{'=' * 8} {em} {title_safe(status)} {mark}  {elapsed:.1f}s {'=' * 8}", flush=True)
    if reason:
        log(f"result: {reason}", level="OK" if ok else "ERROR")
    if _run_notes:
        log("notes: " + " | ".join(_run_notes[-6:]), level="INFO")
    _run_t0 = None


def title_safe(s: str) -> str:
    return str(s or "").strip() or "DONE"


def yolo_round_summary(
    rnd: int,
    max_rounds: int,
    target: str,
    clicked: Any,
    empty_streak: int = 0,
    api_fail: int = 0,
    extra: str = "",
) -> None:
    """One line per YOLO round (default view)."""
    try:
        cl = sorted(clicked) if clicked else []
    except Exception:
        cl = list(clicked) if clicked else []
    bits = [
        f"r{rnd}/{max_rounds}",
        f"target={target!r}",
        f"click={cl}",
    ]
    if empty_streak:
        bits.append(f"empty={empty_streak}")
    if api_fail:
        bits.append(f"api_fail={api_fail}")
    if extra:
        bits.append(extra)
    em = _e("YOLO")
    log(f"{em}{' '.join(bits)}".strip(), level="INFO", tag="yolo")


def yolo_tile(idx: int, msg: str, match: bool = False) -> None:
    """Per-tile detail — verbose only."""
    vlog(f"格 {idx}: {msg} → {'点' if match else '放过'}", tag="yolo")
