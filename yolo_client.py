# -*- coding: utf-8 -*-
"""
HTTP client for captcha-api (YOLO single-tile /predict).

Standalone client for the configured YOLO HTTP API.

服务端 /predict 同时返回：
  target / confidence      —— top-1，老字段，保持兼容
  predictions[]            —— 按置信度降序、每类只留最高分的全部候选

多标签的意义全在 predictions 上：一格里 bus 占了 70%、角落露出个车头
(car 0.38) 时，只看 target=="bus" 就判不匹配，那格必漏。

Env:
  CAPTCHA_API_URL          e.g. http://127.0.0.1:8000  (required)
  CAPTCHA_API_KEY          x-api-key
  CAPTCHA_API_TIMEOUT_MS   default 30000
  CAPTCHA_API_MIN_CONF     default 0.5（只剩 tile_is_target 这条老路径在用）
"""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def enabled() -> bool:
    return bool((os.environ.get("CAPTCHA_API_URL") or "").strip())


def _base_url() -> str:
    return (os.environ.get("CAPTCHA_API_URL") or "").strip().rstrip("/")


def _api_key() -> str:
    return (os.environ.get("CAPTCHA_API_KEY") or "").strip()


def _timeout_sec() -> float:
    try:
        ms = float(os.environ.get("CAPTCHA_API_TIMEOUT_MS") or "30000")
    except Exception:
        ms = 30000.0
    return max(0.5, min(60.0, ms / 1000.0))


def min_confidence() -> float:
    try:
        return float(os.environ.get("CAPTCHA_API_MIN_CONF") or "0.5")
    except Exception:
        return 0.5


def normalize_label(name: str) -> str:
    s = str(name or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = " ".join(s.split())
    aliases = {
        "car": "cars",
        "cars": "cars",
        "bus": "buses",
        "buses": "buses",
        "bicycle": "bicycles",
        "bicycles": "bicycles",
        "bike": "bicycles",
        "motorcycle": "motorcycles",
        "motorcycles": "motorcycles",
        "motorbike": "motorcycles",
        "truck": "trucks",
        "trucks": "trucks",
        "hydrant": "hydrants",
        "hydrants": "hydrants",
        "fire hydrant": "hydrants",
        "fire hydrants": "hydrants",
        "crosswalk": "crosswalks",
        "crosswalks": "crosswalks",
        "traffic light": "traffic lights",
        "traffic lights": "traffic lights",
        "boat": "boats",
        "boats": "boats",
        "bridge": "bridges",
        "bridges": "bridges",
        "chimney": "chimneys",
        "chimneys": "chimneys",
        "stair": "stairs",
        "stairs": "stairs",
        "palm tree": "palm trees",
        "palm trees": "palm trees",
        "parking meter": "parking meters",
        "parking meters": "parking meters",
        "tractor": "tractors",
        "tractors": "tractors",
        "mountain": "mountains",
        "mountains": "mountains",
        "hill": "mountains",
        "hills": "mountains",
        "mountains or hills": "mountains",
    }
    if s in aliases:
        return aliases[s]
    if s.endswith("s") and s[:-1] in aliases:
        return aliases[s[:-1]]
    return s


def labels_match(got: str, want: str) -> bool:
    g, w = normalize_label(got), normalize_label(want)
    if not g or not w:
        return False
    if g == w:
        return True
    return g in w or w in g


def predict_tile(image_path: str, conf: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """POST /predict. Returns dict or None on transport error.

    conf 是「服务端推理下限」，不是判定阈值。传低一点(0.3)只是让次优候选
    进到 predictions 里备用，最终该不该点仍由调用方决定。
    老服务端不认识这个 query 参数，忽略即可，不影响返回。
    """
    if not enabled():
        return None
    path = (image_path or "").strip()
    if not path or not os.path.isfile(path):
        return None

    url = f"{_base_url()}/predict"
    if conf is not None:
        try:
            url += "?" + urlencode({"conf": f"{max(0.01, min(0.95, float(conf))):.2f}"})
        except Exception:
            pass
    boundary = f"----yolo{uuid.uuid4().hex}"
    filename = os.path.basename(path) or "tile.png"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as f:
            raw_img = f.read()
    except Exception as e:
        try:
            from logutil import log as _ulog

            _ulog(f"read failed: {e}", level="WARN", tag="yolo-api")
        except Exception:
            print(f"[yolo-api] read failed: {e}", flush=True)
        return None

    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            raw_img,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    key = _api_key()
    if key:
        headers["x-api-key"] = key

    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=_timeout_sec()) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return json.loads(text) if text else {}
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        try:
            from logutil import log as _ulog

            _ulog(f"HTTP {e.code}: {detail or e}", level="WARN", tag="yolo-api")
        except Exception:
            print(f"[yolo-api] HTTP {e.code}: {detail or e}", flush=True)
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, Exception) as e:
        try:
            from logutil import log as _ulog

            _ulog(f"predict failed: {e}", level="WARN", tag="yolo-api")
        except Exception:
            print(f"[yolo-api] predict failed: {e}", flush=True)
        return None


def predict_tile_multi(
    image_path: str, conf: Optional[float] = None, top_k: int = 5
) -> Optional[List[Dict[str, Any]]]:
    """返回去重排序的预测列表，可选传 conf 调低服务端阈值。

    服务端已按置信度降序、每类只留最高分，这里只取前 top_k 项。
    返回 [{"label": "car", "confidence": 0.85}, {"label": "bus", 0.12}, ...]
    或 None（API 错误 / status != success）。
    """
    res = predict_tile(image_path, conf=conf)
    if res is None:
        return None
    if str(res.get("status") or "").lower() != "success":
        return None
    preds = res.get("predictions", [])
    if not preds:
        # 兼容旧服务端：只有 target/confidence，包成单项数组
        target = res.get("target")
        confidence = res.get("confidence")
        if target and confidence is not None:
            return [{"label": str(target), "confidence": float(confidence)}]
        return None
    return preds[:top_k] if isinstance(preds, list) else None


def tile_contains_any(
    image_path: str, targets: Sequence[str], min_conf: Optional[float] = None
) -> Tuple[bool, Optional[str], float]:
    """检查格子是否包含任一目标。返回 (匹配, 匹配到的标签, 置信度)。

    这是多标签的核心：一格里 bus 占 70%、角落露出车头(car 0.38)，
    旧路径只看 top-1=="bus" 判不匹配必漏；这里遍历 predictions 能救回来。
    """
    preds = predict_tile_multi(image_path, conf=0.3, top_k=5)
    if not preds:
        return False, None, 0.0
    threshold = min_conf if min_conf is not None else min_confidence()
    for pred in preds:
        label = str(pred.get("label") or "")
        conf_val = float(pred.get("confidence") or 0.0)
        if conf_val < threshold:
            break  # 服务端已排序，后面更低，直接退出
        for want in targets:
            if labels_match(label, want):
                return True, label, conf_val
    return False, None, 0.0


def tile_is_target(image_path: str, target_object: str) -> Optional[bool]:
    """True match / False no / None API error. 单标签接口，保持向后兼容。"""
    res = predict_tile(image_path)
    if res is None:
        return None
    if str(res.get("status") or "").lower() != "success":
        return False
    try:
        conf = float(res.get("confidence") or 0.0)
    except Exception:
        conf = 0.0
    if conf < min_confidence():
        return False
    return labels_match(str(res.get("target") or ""), target_object)
