# -*- coding: utf-8 -*-
"""
Business policies and environment configuration for YOLO reCAPTCHA solver.
"""
import os

YOLO_MAX_ROUNDS_DEFAULT = 24

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:
        return default

def resolve_yolo_max_rounds(default: int = YOLO_MAX_ROUNDS_DEFAULT) -> int:
    """Resolve YOLO_RECAPTCHA_MAX_ROUNDS — the only read point."""
    if not default or default <= 0:
        default = YOLO_MAX_ROUNDS_DEFAULT
    return max(1, _env_int("YOLO_RECAPTCHA_MAX_ROUNDS", default))

def match_min_conf() -> float:
    """判定「这一格里有考题物体」的单框阈值，与排名无关。"""
    return max(0.3, _env_float("YOLO_MATCH_CONF_3X3", 0.38))

def rescue_min_conf() -> float:
    """多标签救回的专用阈值：比 top-1 阈值低，专治「漏选」。"""
    return max(0.15, _env_float("YOLO_RESCUE_CONF_3X3", 0.25))

def max_clicks() -> int:
    """reCAPTCHA 正解通常 2~5 格；点满一屏必错，还白送风控一次行为样本。"""
    return max(1, _env_int("YOLO_MAX_CLICKS_3X3", 6))

def probe_conf() -> float:
    """服务端推理下限（降低以允许次优候选）。"""
    return min(max(0.05, _env_float("YOLO_3X3_PROBE_CONF", 0.15)), rescue_min_conf())

def dynamic_refresh_timeout() -> float:
    return _env_float("YOLO_DYNAMIC_REFRESH_TIMEOUT", 12.0)

def blind_round_limit() -> int:
    return max(2, _env_int("YOLO_BLIND_ROUND_LIMIT", 3))

def skip_4x4_limit() -> int:
    return max(1, _env_int("YOLO_4X4_SKIP_LIMIT", 5))

def click_delay() -> float:
    return _env_float("YOLO_RECAPTCHA_CLICK_DELAY", 0.15)
