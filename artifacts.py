# -*- coding: utf-8 -*-
"""
Image manipulation and dataset harvesting (hard examples).
"""
import os
import json
import math
import shutil
import tempfile
import time
import re
from typing import List, Dict, Any, Set, Optional
from logutil import log
from yolo_client import normalize_label

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

def _safe_name(s: str, max_len: int = 40) -> str:
    t = re.sub(r"[^\w.\-]+", "_", str(s or "").strip()) or "x"
    return t[:max_len]

def crop_tile(src_png: str, tile_idx: int, num_tiles: int, pad_ratio: float = 0.02) -> Optional[str]:
    if Image is None:
        log("Pillow missing — cannot crop tiles", "ERROR")
        return None
    n = int(round(math.sqrt(num_tiles)))
    if n * n != num_tiles or not (0 <= tile_idx < num_tiles):
        return None
    try:
        img = Image.open(src_png).convert("RGB")
        w, h = img.size
        cw, ch = w / n, h / n
        r, c = divmod(tile_idx, n)
        pad_x, pad_y = cw * pad_ratio, ch * pad_ratio
        x0 = max(0, int(c * cw - pad_x))
        y0 = max(0, int(r * ch - pad_y))
        x1 = min(w, int((c + 1) * cw + pad_x))
        y1 = min(h, int((r + 1) * ch + pad_y))
        out = src_png.replace(".png", f"_t{tile_idx}.png")
        img.crop((x0, y0, x1, y1)).save(out)
        return out
    except Exception as e:
        log(f"crop_tile failed: {e}", "WARN")
        return None

def grid_fingerprint(png_path: str):
    """Small RGB fingerprint of grid center (detect post-click image change)."""
    if Image is None or not png_path or not os.path.isfile(png_path):
        return None
    try:
        img = Image.open(png_path).convert("RGB")
        w, h = img.size
        if w < 20 or h < 20:
            return None
        sample = img.crop((int(w * 0.08), int(h * 0.08), int(w * 0.92), int(h * 0.92)))
        return sample.resize((48, 48)).tobytes()
    except Exception:
        return None

def _train_dump_enabled() -> bool:
    """Default ON: keep hard tiles for model training. Set YOLO_SAVE_HARD_TILES=0 to disable."""
    raw = (os.environ.get("YOLO_SAVE_HARD_TILES") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")

def save_hard_tiles_for_training(
    *,
    screenshot_dir: str,
    tag: str,
    target: str,
    tile_records: List[Dict[str, Any]],
    clicked: Set[int],
    reason: str,
    grid_path: Optional[str] = None,
) -> int:
    if not _train_dump_enabled():
        return 0
    if not tile_records:
        return 0

    base = (screenshot_dir or "").strip() or tempfile.gettempdir()
    hard_root = os.path.join(base, "yolo_hard")
    os.makedirs(hard_root, exist_ok=True)

    want = normalize_label(target)
    want_s = _safe_name(want) or "target"
    saved = 0
    lines: List[str] = []
    ts = int(time.time())

    for rec in tile_records:
        i = int(rec.get("i", -1))
        src = rec.get("path")
        if not src or not os.path.isfile(src):
            continue
        pred = str(rec.get("pred") or "none")
        conf = float(rec.get("conf") or 0.0)
        match = bool(rec.get("match"))
        was_clicked = i in clicked

        is_wrong_class = (not match) and pred not in ("", "none", "null") and conf > 0
        is_miss_candidate = (not was_clicked) and (not match)

        if was_clicked and match:
            continue
        if not is_miss_candidate and not is_wrong_class:
            continue

        kind = "wrong" if is_wrong_class else "miss"
        fname = (
            f"{_safe_name(tag)}_i{i}"
            f"__want-{want_s}"
            f"__pred-{_safe_name(pred)}"
            f"__c{conf:.2f}"
            f"__{'clk' if was_clicked else 'noclk'}"
            f"__{_safe_name(reason)}.png"
        )
        dst = os.path.join(hard_root, fname)
        try:
            shutil.copy2(src, dst)
            saved += 1
            lines.append(
                json.dumps(
                    {
                        "tag": tag,
                        "i": i,
                        "target": want,
                        "pred": pred,
                        "conf": conf,
                        "clicked": was_clicked,
                        "match": match,
                        "kind": kind,
                        "reason": reason,
                        "src": src,
                        "dst": dst,
                        "ts": ts,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            log(f"[train] copy tile {i} failed: {e}", "WARN")

    if lines:
        man = os.path.join(hard_root, "manifest.jsonl")
        try:
            with open(man, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            log(f"[train] manifest write failed: {e}", "WARN")

    if saved:
        log(
            f"[train] 难例 {saved} 张 → {hard_root}/ "
            f"(平铺，人工再分) target={want!r} reason={reason}"
        )
    return saved
