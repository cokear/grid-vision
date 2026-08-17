# -*- coding: utf-8 -*-
"""Standalone reCAPTCHA image-challenge solver backed by a YOLO HTTP API."""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from yolo_client import normalize_label, labels_match, predict_tile
import yolo_client as yolo_api
from recaptcha_dom import (
    challenge_ui_ready,
    click_checkbox,
    crop_tile,
    find_frame,
    force_reopen_recaptcha,
    get_table_tiles,
    grid_fingerprint,
    is_recaptcha_solved,
    log,
    need_force_checkbox_reopen,
    probe_recaptcha_state,
    recaptcha_expired_or_need_checkbox,
    safe_click,
    screenshot_element,
    wait_dynamic_tiles_refreshed,
    wait_bframe_ready,
    wait_grid_ready_for_shot,
    read_target,
    _looks_dynamic,
)
from result import SolveStatus
from artifacts import save_hard_tiles_for_training
from policy import (
    resolve_yolo_max_rounds,
    match_min_conf,
    rescue_min_conf,
    max_clicks,
    probe_conf,
    dynamic_refresh_timeout,
    blind_round_limit,
    skip_4x4_limit,
    click_delay
)

try:
    from logutil import vlog, yolo_round_summary, yolo_tile, phase as log_phase
except Exception:
    def vlog(msg, level="INFO", tag="yolo"):
        pass

    def yolo_round_summary(rnd, max_rounds, target, clicked, empty_streak=0, api_fail=0, extra=""):
        log(
            f"[经理] r{rnd}/{max_rounds} target={target!r} "
            f"click={sorted(clicked) if clicked else []} empty={empty_streak} api_fail={api_fail} {extra}"
        )

    def yolo_tile(idx, msg, match=False):
        log(f"[经理] 格 {idx}: {msg} → {'点' if match else '放过'}")

    def log_phase(label, msg, level="INFO"):
        log(f"[{label}] {msg}", level=level)



def _norm_target_key(target: str) -> str:
    return yolo_api.normalize_label(target or "")


def _count_tiles(page) -> int:
    """Return current challenge tile count (9=3x3, 16=4x4), or 0 if unknown."""
    try:
        _bf, _tb, tiles = get_table_tiles(page)
        n = len(tiles or [])
        if n in (9, 16):
            return n
        if n >= 16:
            return 16
        if n >= 9:
            return 9
        return n
    except Exception:
        return 0


def _is_4x4(page, num_tiles: int = 0) -> bool:
    n = int(num_tiles or 0) or _count_tiles(page)
    return n >= 16


def _skip_4x4_challenge(page, target: str = "", reason: str = "") -> bool:
    """Avoid YOLO on 4x4: Skip if available, else Reload. Stay on same page.

    YOLO is weak on 4x4, so swap it for a 3x3 challenge.
    Does NOT reload the host page / browser.
    """
    tkey = _norm_target_key(target)
    why = reason or "YOLO weak on 4x4"
    log(f"[经理] 跳过 4x4（{why}） target={tkey!r} — Skip/内部 Reload，不硬解")
    
    # 增加拟人化随机停顿，防止秒点Skip触发风控
    import random
    time.sleep(random.uniform(1.5, 3.5))
    
    try:
        # On static 4x4 the verify button often shows Skip when nothing selected
        _click_skip(page)
    except Exception:
        pass
    time.sleep(1.0)
    if is_recaptcha_solved(page):
        return True
    # Always Reload after Skip to force a fresh (hopefully 3x3) board.
    _click_reload(page)
    time.sleep(2.2)
    return True


def _visible_recaptcha_error_hits(page) -> list:
    """Return list of visible error hits: [{sel, t, vis}, ...]. Empty if none.

    IMPORTANT: bframe always contains hidden template strings like
      "Please try again."
      "Please select all matching images."
    in the DOM even on a healthy challenge. Only trust VISIBLE error nodes.
    """
    bframe = find_frame(page, "bframe", timeout=2.5)
    if not bframe:
        return []
    try:
        info = bframe.run_js(
            """
            function isVisible(el) {
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st) return false;
              if (st.display === 'none' || st.visibility === 'hidden') return false;
              if (parseFloat(st.opacity || '1') < 0.05) return false;
              const r = el.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) return false;
              return true;
            }
            const sels = [
              '.rc-imageselect-error-select-more',
              '.rc-imageselect-error-dynamic-more',
              '.rc-imageselect-incorrect-response',
            ];
            const hits = [];
            for (const sel of sels) {
              const el = document.querySelector(sel);
              if (!el) continue;
              const t = ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
              const vis = isVisible(el);
              if (vis && t) hits.push({sel, t: t.slice(0, 80), vis: true});
            }
            return {hits, any: hits.length > 0};
            """
        )
        if isinstance(info, dict) and info.get("any"):
            hits = info.get("hits") or []
            if hits:
                log(f"visible reCAPTCHA error: {hits!r}", "WARN")
            return list(hits) if isinstance(hits, list) else []
    except Exception as e:
        log(f"try_again visibility probe failed: {e}", "WARN")

    # Fallback: element API + explicit style/rect (still no body dump)
    hits = []
    for sel in (
        ".rc-imageselect-error-select-more",
        ".rc-imageselect-error-dynamic-more",
        ".rc-imageselect-incorrect-response",
    ):
        try:
            err = bframe.ele(sel, timeout=0.3)
        except Exception:
            err = None
        if not err:
            continue
        try:
            st = (err.attr("style") or "").lower()
            if "display: none" in st or "display:none" in st:
                continue
            if "visibility: hidden" in st or "visibility:hidden" in st:
                continue
            t = (err.text or "").strip()
            if t:
                log(f"visible reCAPTCHA error via ele {sel}: {t[:60]!r}", "WARN")
                hits.append({"sel": sel, "t": t[:80], "vis": True})
        except Exception:
            continue
    return hits


def _classify_recaptcha_error(page) -> str:
    """Classify visible reCAPTCHA error banner.

    Returns:
      'select_more'  — Please select all matching images (same board, incomplete)
      'dynamic_more' — Please also check the new images (same challenge, more tiles)
      'incorrect'    — Please try again (wrong / board may swap)
      ''             — no visible error
    """
    hits = _visible_recaptcha_error_hits(page)
    if not hits:
        return ""
    # Priority: select-more / dynamic-more over generic incorrect
    # (Google may show both; incomplete-select is more specific)
    kinds = []
    for h in hits:
        sel = str((h or {}).get("sel") or "").lower()
        t = str((h or {}).get("t") or "").lower()
        if "select-more" in sel or "select all matching" in t:
            kinds.append("select_more")
        elif "dynamic-more" in sel or "new image" in t or "check the new" in t:
            kinds.append("dynamic_more")
        elif "incorrect" in sel or "try again" in t:
            kinds.append("incorrect")
    for prefer in ("select_more", "dynamic_more", "incorrect"):
        if prefer in kinds:
            return prefer
    return "incorrect"


def _instruction_shows_try_again(page) -> bool:
    """Only TRUE when a reCAPTCHA error banner is actually visible."""
    return bool(_classify_recaptcha_error(page))


def _click_skip(page) -> bool:
    """静态 4x4 无目标时点 Skip（verify 按钮文案可能是 Skip）。"""
    bframe = find_frame(page, "bframe", timeout=4)
    if not bframe:
        return False
    try:
        btn = bframe.ele("#recaptcha-verify-button", timeout=3)
        if not btn:
            return False
        label = ((btn.text or "") + " " + (btn.attr("value") or "")).strip().lower()
        # 无图可选时按钮常为 Skip
        safe_click(btn)
        log(f"Skip/Verify clicked (label={label!r})")
        return True
    except Exception as e:
        log(f"Skip click failed: {e}", "WARN")
        return False


def _click_reload(page) -> None:
    bframe = find_frame(page, "bframe", timeout=4)
    if not bframe:
        return
    try:
        btn = bframe.ele("#recaptcha-reload-button", timeout=3)
        if btn:
            safe_click(btn)
            log("Reload clicked")
    except Exception as e:
        log(f"Reload failed: {e}", "WARN")


def _snapshot_grid_fp(page) -> Optional[bytes]:
    """Quick whole-table fingerprint for detecting post-fail / post-reload image change."""
    try:
        _bf, table, tiles = get_table_tiles(page)
        if not table or not tiles:
            return None
        tmp = os.path.join(
            os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
            f"yolo_fp_{os.getpid()}.png",
        )
        if not screenshot_element(table, tmp, retries=1):
            return None
        return grid_fingerprint(tmp)
    except Exception:
        return None


def _wait_fresh_grid_after_fail(page, before_fp=None, timeout: float = 8.0) -> bool:
    """After Verify fail / Reload: wait until a NEW stable 3x3/4x4 grid is paintable.

    Google often auto-swaps images on 'Please try again' WITHOUT us clicking Reload.
    If we also Reload too early, we skip a whole board (user saw: jump to next
    challenge without solving the auto-refreshed one). Prefer wait-for-change first.
    """
    timeout = max(3.0, float(timeout))
    deadline = time.time() + timeout
    tmp = os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
        f"yolo_fresh_{os.getpid()}.png",
    )
    saw_change = before_fp is None
    last_fp = None
    stable = 0
    while time.time() < deadline:
        if is_recaptcha_solved(page):
            return True
        if recaptcha_expired_or_need_checkbox(page):
            log("fresh-grid wait: challenge expired", "WARN")
            return False
        _bf, table, tiles = get_table_tiles(page)
        n = len(tiles or [])
        if not table or n < 4:
            time.sleep(0.35)
            continue
        if not screenshot_element(table, tmp, retries=1):
            time.sleep(0.35)
            continue
        fp = grid_fingerprint(tmp)
        if before_fp is not None and fp is not None and fp != before_fp:
            saw_change = True
        if not saw_change:
            time.sleep(0.35)
            continue
        # require one stable frame after change (paint finished)
        if last_fp is not None and fp == last_fp:
            stable += 1
            if stable >= 1:
                log(f"fresh grid ready tiles={n} (post-fail/reload settle)")
                return True
        else:
            stable = 0
        last_fp = fp
        time.sleep(0.4)
    log("fresh-grid wait timeout — continue with whatever is on screen", "WARN")
    return False


def _recover_after_static_fail(page, before_fp=None) -> Tuple[str, object, bool]:
    """Handle static/dynamic Verify fail / Please try again.

    Real Google behavior after incomplete Verify:
      - Often ALREADY swapped to a new board
      - Red "Please try again" may STILL be visible on the new board
      - Correct action: SOLVE the new board (click tiles), NOT Reload again

    Order:
      1) Snapshot fp
      2) Wait briefly for auto-new-images (do NOT Reload yet)
      3) Only if images never change → Reload once
      4) Re-read target; leave residual red banner alone (solve path ignores it)

    Returns (target, bframe, dynamic).
    """
    fp0 = before_fp if before_fp is not None else _snapshot_grid_fp(page)
    log("[经理] 失败恢复：先等 Google 自动换图（不立刻 Reload）…")
    # shorter wait — user sees new board quickly; long wait feels like "no click"
    auto_ok = _wait_fresh_grid_after_fail(page, before_fp=fp0, timeout=3.5)
    if not auto_ok:
        log("[经理] 自动换图未确认 → 点一次内部 Reload")
        fp1 = _snapshot_grid_fp(page) or fp0
        _click_reload(page)
        time.sleep(1.0)
        _wait_fresh_grid_after_fail(page, before_fp=fp1, timeout=6.0)
    else:
        log("[经理] 已是新图 — 直接解题（忽略残留 Please try again 红字）")

    time.sleep(0.35)
    bframe = find_frame(page, "bframe", timeout=3)
    target = read_target(page, bframe=bframe) if bframe else read_target(page)
    dynamic = _looks_dynamic(bframe) if bframe else False
    if target:
        log(f"[经理] 失败恢复后题面 target={target!r} dynamic={dynamic}")
    return target or "", bframe, dynamic


def _click_verify(page) -> None:
    bframe = find_frame(page, "bframe", timeout=4)
    if not bframe:
        return
    try:
        btn = bframe.ele("#recaptcha-verify-button", timeout=3)
        if btn:
            safe_click(btn)
            log("Verify clicked")
    except Exception as e:
        log(f"Verify failed: {e}", "WARN")


def _shot_one_tile(tile_ele, path: str) -> bool:
    """截单格小图（优先元素截图，失败再由调用方 crop）。"""
    try:
        if screenshot_element(tile_ele, path, retries=2):
            return True
    except Exception:
        pass
    return False


def _safe_name(s: str, max_len: int = 40) -> str:
    t = re.sub(r"[^\w.\-]+", "_", str(s or "").strip()) or "x"
    return t[:max_len]


# solve_tiles_once api_fail sentinel: the grid was never observed this round
# (all-black screenshot / unreadable PNG). Distinct from "N tiles failed the API"
# because retrying the API is pointless — the screenshot pipeline itself is dead.
OBS_UNRELIABLE = -1


def solve_tiles_once(
    page, screenshot_dir: str, target: str, tag: str
) -> Tuple[Set[int], Optional[bytes], int, List[Dict[str, Any]]]:
    """Capture, classify, and click one observable 3x3 challenge board."""
    if recaptcha_expired_or_need_checkbox(page):
        return set(), None, 0, []

    os.makedirs(screenshot_dir, exist_ok=True)
    full_path = os.path.join(screenshot_dir, f"yolo_grid_{_safe_name(tag)}.png")
    ready_diag: Dict[str, Any] = {}
    ready = wait_grid_ready_for_shot(
        page, full_path, num_tiles=9, timeout=8.0, diag=ready_diag
    )
    _frame, table, tiles = get_table_tiles(page)
    if not table or not tiles:
        log("challenge grid is unavailable", "WARN")
        return set(), None, 0, []

    tile_count = len(tiles)
    if tile_count >= 16:
        fingerprint = None
        if screenshot_element(table, full_path):
            fingerprint = grid_fingerprint(full_path)
        return set(), fingerprint, 0, []

    overview_ok = screenshot_element(table, full_path)
    before_fp = grid_fingerprint(full_path) if overview_ok else None
    if not ready:
        unreliable = bool(ready_diag.get("broken")) or not overview_ok
        log("challenge grid is not ready for image recognition", "WARN")
        return set(), before_fp, OBS_UNRELIABLE if unreliable else 0, []

    tile_paths: List[Optional[str]] = []
    for index, tile in enumerate(tiles):
        tile_path = os.path.join(
            screenshot_dir, f"yolo_tile_{_safe_name(tag)}_{index}.png"
        )
        if not _shot_one_tile(tile, tile_path):
            tile_path = crop_tile(full_path, index, tile_count, pad_ratio=0.02)
        tile_paths.append(tile_path)

    wanted = yolo_api.normalize_label(target)

    def classify(index: int, tile_path: Optional[str]) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "i": index,
            "path": tile_path,
            "pred": "none",
            "conf": 0.0,
            "want_conf": 0.0,
            "want": wanted,
            "match": False,
            "api_fail": False,
            "msg": "",
        }
        if not tile_path:
            record.update(api_fail=True, msg="tile screenshot unavailable")
            return record
        response = predict_tile(tile_path, conf=probe_conf())
        if not response:
            record.update(api_fail=True, msg="YOLO API did not respond")
            return record
        if str(response.get("status") or "").lower() != "success":
            record["msg"] = str(response.get("message") or "no prediction")
            return record

        top_label = normalize_label(str(response.get("target") or ""))
        try:
            top_conf = float(response.get("confidence") or 0.0)
        except (TypeError, ValueError):
            top_conf = 0.0
        want_conf = top_conf if labels_match(top_label, wanted) else 0.0
        want_label = top_label if want_conf else ""
        for prediction in response.get("predictions") or []:
            try:
                label = normalize_label(str(prediction.get("label") or ""))
                confidence = float(prediction.get("confidence") or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue
            if labels_match(label, wanted) and confidence > want_conf:
                want_label, want_conf = label, confidence

        matched = (
            labels_match(top_label, wanted) and top_conf >= match_min_conf()
        ) or want_conf >= rescue_min_conf()
        record.update(
            pred=want_label if matched and want_label else top_label,
            conf=want_conf if matched and want_conf else top_conf,
            want_conf=want_conf,
            match=matched,
            msg=f"top={top_label!r} confidence={top_conf:.2f}",
        )
        return record

    import concurrent.futures

    records: List[Optional[Dict[str, Any]]] = [None] * tile_count
    with concurrent.futures.ThreadPoolExecutor(max_workers=tile_count) as executor:
        futures = {
            executor.submit(classify, index, path): index
            for index, path in enumerate(tile_paths)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                records[index] = future.result()
            except Exception as exc:
                records[index] = {
                    "i": index,
                    "path": tile_paths[index],
                    "match": False,
                    "api_fail": True,
                    "msg": str(exc),
                }

    tile_records = [record for record in records if record is not None]
    api_fail_count = sum(bool(record.get("api_fail")) for record in tile_records)
    candidates = [record for record in tile_records if record.get("match")]
    candidates.sort(key=lambda record: float(record.get("conf") or 0.0), reverse=True)
    candidates = candidates[: max_clicks()]
    random.shuffle(candidates)

    clicked: Set[int] = set()
    for record in candidates:
        index = int(record["i"])
        yolo_tile(index, str(record.get("msg") or ""), match=True)
        try:
            if "selected" in str(tiles[index].attr("class") or ""):
                continue
            safe_click(tiles[index])
            clicked.add(index)
            time.sleep(click_delay() + random.uniform(0.5, 1.5))
        except Exception as exc:
            log(f"tile {index} click failed: {exc}", "WARN")

    return clicked, before_fp, api_fail_count, tile_records


def _handle_recaptcha_yolo(
    page, screenshot_dir: str = "output/screenshots", max_rounds: Optional[int] = None
) -> Any:
    """
    脚本 = 经理；YOLO API = 只看小图回标签。

    求解阶段规则：
      - 不刷新网页 / 不 page.get / 不重开浏览器
      - 只在当前 reCAPTCHA 弹层里死磕
      - 允许点 reCAPTCHA 内部 Reload（换一组图，不是刷新页面）
      - 禁止因隐藏文案 "Please try again." 提前放弃

    流程：1 审题 → 2 截每格小图 → 3 发给 YOLO → 4 按回信点击 → 验证
    """
    if not yolo_api.enabled():
        log("CAPTCHA_API_URL empty — 经理无法发快递", "ERROR")
        return SolveStatus.FAILED 
    if is_recaptcha_solved(page):
        return True

    # CRITICAL: empty bframe / expired checkbox must force reopen first.
    # Screenshot case: red "Verification challenge expired. Check the checkbox again."
    if challenge_ui_ready(page, timeout=1.0) and not recaptcha_expired_or_need_checkbox(page):
        log("[经理] 挑战 UI 已就绪，跳过 checkbox")
    else:
        log("[经理] 挑战未开/已过期 → force_reopen_recaptcha")
        try:
            force_reopen_recaptcha(page, max_clicks=4)
        except Exception as e:
            log(f"initial force_reopen failed: {e}", "WARN")
            if not click_checkbox(page, force=True):
                log("checkbox not clicked", "WARN")
            time.sleep(1.5)
        if is_recaptcha_solved(page):
            log("[经理] checkbox 后直接 solved（无图题）")
            return True
        if not challenge_ui_ready(page, timeout=2.0):
            log("[经理] 仍无题面 → 再 force_reopen 一次", "WARN")
            try:
                force_reopen_recaptcha(page, max_clicks=2)
            except Exception:
                click_checkbox(page, force=True)
                time.sleep(1.8)

    # --- 1. 审题：进 bframe，扒题目文字（不是 YOLO 的事）---
    bframe = wait_bframe_ready(page, timeout=18.0)
    if not bframe or not challenge_ui_ready(page, timeout=2.0):
        log("[经理] bframe 仍无题面 → 最后 force_reopen", "WARN")
        try:
            force_reopen_recaptcha(page, max_clicks=3)
        except Exception:
            click_checkbox(page, force=True)
            time.sleep(2.0)
        bframe = wait_bframe_ready(page, timeout=12.0)

    if not bframe:
        log("[经理] 审题失败：挑战 bframe 未出现/未切入（checkbox 可能未点上）", "ERROR")
        return SolveStatus.FAILED 
    target = read_target(page, bframe=bframe)
    if not target:
        # Empty instruction after open: reload challenge once inside widget, then re-read
        log("[经理] 题面文字空 — reCAPTCHA 内部 Reload 后再读一次", "WARN")
        _click_reload(page)
        time.sleep(2.0)
        bframe = wait_bframe_ready(page, timeout=10.0) or bframe
        target = read_target(page, bframe=bframe)
    if not target:
        log("[经理] 审题失败：bframe 内未读到考题文字（题没出来）", "ERROR")
        return SolveStatus.FAILED 
    log(f"[经理] 审题完成 target={target!r}")

    dynamic = _looks_dynamic(bframe)
    log(f"[经理] 题型={'DYNAMIC 消消乐' if dynamic else 'STATIC 一次点选'}")

    # 唯一轮次配置解析点在 resolve_yolo_max_rounds()；
    # max_rounds 只作为"环境变量未设置时"的默认值，绝不覆盖环境变量。
    max_rounds = resolve_yolo_max_rounds(int(max_rounds or 0))
    refresh_timeout = dynamic_refresh_timeout()
    # 可见失败条：只换 reCAPTCHA 内部题，绝不因 streak 放弃（死磕）
    # 仅作日志计数；真正结束只看 max_rounds / solved
    try_again_streak = 0
    empty_click_streak = 0
    expired_reopen_count = 0
    # 连续"盘面不可观测"轮数（截图全黑 / 读图失败）。达到 blind_limit 就认定
    # 截图管道坏了，强制重开 checkbox + 重新定位 bframe，而不是继续空转。
    blind_streak = 0
    blind_limit = blind_round_limit()
    # 动态题（消消乐）专用：本道题（自上次换题/Reload 以来）是否点过匹配格。
    # 用来区分两种"无匹配"：从头没点过(→Reload) vs 点过后已清空(→Verify 交卷)。
    clicked_any_this_challenge = False
    # 4x4 Skip 不消耗 max_rounds：脚本按设计根本不扫 4x4（YOLO 弱），只 Skip/换题，
    # 那不是一次解题尝试。以前它照样吃掉一轮，配 6 轮实际可能只剩 4 次真尝试。
    # 代价是连续刷 4x4 会多转几圈 —— 用独立上限兜住，连续跳这么多次就放弃本次挑战。
    skip4x4_streak = 0
    skip4x4_limit = skip_4x4_limit()
    # 物理迭代硬兜底：只防死循环，正常路径永远撞不到它。
    # 每个真轮次前面最多垫 skip4x4_limit 次 4x4 跳过（再多就被 streak 上限掐掉）。
    iters = 0
    iter_budget = max_rounds * (1 + skip4x4_limit) + 8
    log_phase(
        "RECAPTCHA",
        f"start target={target!r} type={'dynamic' if dynamic else 'static'} "
        f"max_rounds={max_rounds} (4x4 skip 不计轮次, limit={skip4x4_limit})",
    )

    # while 而非 for：4x4 Skip 要能"退还"本轮（rnd -= 1），for 的迭代数是死的。
    rnd = 0
    while rnd < max_rounds:
        iters += 1
        if iters > iter_budget:
            log(
                f"[经理] 物理迭代兜底触发 (iters={iters} > {iter_budget}) — 退出死磕",
                "ERROR",
            )
            break
        rnd += 1
        if is_recaptcha_solved(page):
            log(f"[经理] solved at round {rnd}")
            return SolveStatus.SUCCESS
        # Dead / expired widget: stop shots, force checkbox reopen.
        # Uses multi-signal probe (anchor text + dead-widget heuristic).
        force_reopen = False
        try:
            force_reopen = need_force_checkbox_reopen(page, empty_streak=empty_click_streak)
        except Exception as e:
            log(f"reopen probe failed: {e}", "WARN")
            force_reopen = False

        if force_reopen:
            expired_reopen_count += 1
            st = {}
            try:
                st = probe_recaptcha_state(page)
            except Exception:
                pass
            log(
                f"[经理] reCAPTCHA 需重开 checkbox (#{expired_reopen_count}) "
                f"state={{tiles={st.get('tile_count')}, checked={st.get('checked')}, "
                f"token={st.get('token_len')}, expired_text={st.get('expired_text')}, "
                f"reason={st.get('expired_reason')!r}}} — force_reopen_recaptcha",
                "WARN",
            )
            if expired_reopen_count > 10:
                log("[经理] 重开次数过多", "ERROR")
                return SolveStatus.FAILED # Hard multi-click reopen (in-frame + host iframe + CDP)
            opened = False
            try:
                opened = bool(force_reopen_recaptcha(page, max_clicks=4))
            except Exception as e:
                log(f"force_reopen_recaptcha failed: {e}", "WARN")
                try:
                    click_checkbox(page, force=True)
                    time.sleep(2.0)
                except Exception:
                    pass
            if is_recaptcha_solved(page):
                return SolveStatus.SUCCESS
            if not opened and not challenge_ui_ready(page, timeout=2.0):
                log("[经理] force_reopen 后仍无题 — 再 force 一轮", "WARN")
                try:
                    force_reopen_recaptcha(page, max_clicks=2)
                except Exception:
                    click_checkbox(page, force=True)
                    time.sleep(2.0)
            bframe = wait_bframe_ready(page, timeout=14.0)
            if bframe:
                t_new = read_target(page, bframe=bframe)
                if t_new:
                    target = t_new
                    log(f"[经理] 重开后新题 target={target!r}")
                dynamic = _looks_dynamic(bframe)
            else:
                log("[经理] 强制重点后仍无 bframe UI", "WARN")
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        # NOTE: residual "Please try again" banner often stays visible ON a fresh board.
        # Do NOT treat visible red text as "must reload again" — that caused:
        #   screenshot new board → no tile clicks → Reload (user observation).
        # Only enter recover when we JUST failed a Verify this loop (below).
        # If banner is up but grid is ready, SOLVE it.

        # No challenge UI at all
        num_tiles_guess = _count_tiles(page)
        ui_ready = challenge_ui_ready(page, timeout=0.9)
        if not ui_ready and num_tiles_guess < 4:
            empty_click_streak += 1
            time.sleep(0.6)
            if recaptcha_expired_or_need_checkbox(page):
                continue
            if empty_click_streak <= 1:
                log(f"[经理] 暂无题面 empty_streak={empty_click_streak} — 短等")
                time.sleep(0.8)
                continue
            log(
                f"[经理] 连续无题面 (streak={empty_click_streak}) — force_reopen_recaptcha",
                "WARN",
            )
            try:
                force_reopen_recaptcha(page, max_clicks=3)
            except Exception:
                click_checkbox(page, force=True)
                time.sleep(1.8)
            if challenge_ui_ready(page, timeout=2.5):
                bframe = wait_bframe_ready(page, timeout=10.0)
                t_new = read_target(page, bframe=bframe) if bframe else ""
                if t_new:
                    target = t_new
                    log(f"[经理] 无题面重开后 target={target!r}")
                dynamic = _looks_dynamic(bframe) if bframe else dynamic
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        # 4x4：不硬解 YOLO，内部 Skip/Reload 换 3x3。本轮不算一次解题尝试 → 退还 rnd。
        if _is_4x4(page, num_tiles_guess):
            skip4x4_streak += 1
            log(
                f"[经理] 4x4 ({num_tiles_guess}格) target={target!r} "
                f"— Skip/内部换题，不硬解、不点主 checkbox "
                f"(不计入轮次 streak={skip4x4_streak}/{skip4x4_limit})"
            )
            rnd -= 1
            if skip4x4_streak >= skip4x4_limit:
                log(
                    f"[经理] 连续 4x4 跳过 {skip4x4_streak} 次仍没换到 3x3 — 放弃本次挑战",
                    "ERROR",
                )
                return bool(is_recaptcha_solved(page))
            _skip_4x4_challenge(page, target=target, reason=f"{num_tiles_guess} tiles")
            if is_recaptcha_solved(page):
                return True
            time.sleep(1.0)
            if recaptcha_expired_or_need_checkbox(page):
                continue
            t2 = read_target(page)
            if t2:
                target = t2
            bframe = find_frame(page, "bframe", timeout=3) or bframe
            dynamic = _looks_dynamic(bframe) if bframe else dynamic
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        # If red banner still showing but grid ready: log and SOLVE (click tiles)
        if _instruction_shows_try_again(page):
            log(
                "[经理] 题面有残留 Please try again 红字 — 仍直接解题点击（不刷新）",
                "WARN",
            )

        # Site interstitial ("Unlock more content / View a short ad") can sit over
        # the recaptcha iframe and poison YOLO tile screenshots — scrub first.
        vlog(f"---- 第 {rnd}/{max_rounds} 轮：截图→发快递→点击 ----")
        # tag 用于截图/训练样本文件名，必须唯一。4x4 退还轮次后同一个 rnd 会被复用，
        # 所以只要物理迭代数和轮次数对不上（说明中间跳过过 4x4），就带上 iters 后缀。
        tag = f"r{rnd}" if iters == rnd else f"r{rnd}i{iters}"
        clicked, before_fp, api_fail_count, tile_records = solve_tiles_once(
            page, screenshot_dir, target, tag
        )

        def _dump_hard(reason: str) -> None:
            try:
                save_hard_tiles_for_training(
                    screenshot_dir=screenshot_dir,
                    tag=tag,
                    target=target,
                    tile_records=tile_records or [],
                    clicked=clicked or set(),
                    reason=reason,
                )
            except Exception as e:
                log(f"[train] dump failed: {e}", "WARN")

        yolo_round_summary(
            rnd,
            max_rounds,
            target,
            clicked,
            empty_streak=empty_click_streak,
            api_fail=api_fail_count,
            extra=(
                "blind"
                if api_fail_count == OBS_UNRELIABLE
                else ("4x4-skip" if _is_4x4(page, _count_tiles(page) or 0) else "")
            ),
        )
        if not clicked and before_fp is None and recaptcha_expired_or_need_checkbox(page):
            log("[经理] 截图轮发现真正过期 — 重开 checkbox", "WARN")
            continue
        try:
            n_after = _count_tiles(page)
            if n_after:
                num_tiles_guess = n_after
        except Exception:
            pass
        if _is_4x4(page, num_tiles_guess):
            skip4x4_streak += 1
            log(
                f"[经理] 本轮实际为 4x4 — 丢弃 YOLO 结果，Skip/内部换题 "
                f"(不计入轮次 streak={skip4x4_streak}/{skip4x4_limit})",
                "WARN",
            )
            rnd -= 1
            if skip4x4_streak >= skip4x4_limit:
                log(
                    f"[经理] 连续 4x4 跳过 {skip4x4_streak} 次仍没换到 3x3 — 放弃本次挑战",
                    "ERROR",
                )
                return bool(is_recaptcha_solved(page))
            _skip_4x4_challenge(page, target=target, reason="post-scan 4x4")
            if is_recaptcha_solved(page):
                return True
            t2 = read_target(page)
            if t2:
                target = t2
            bframe = find_frame(page, "bframe", timeout=3) or bframe
            dynamic = _looks_dynamic(bframe) if bframe else dynamic
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        # 走到这里盘面确认不是 4x4 → 连续跳过链断了，重置 streak。
        skip4x4_streak = 0

        # 盘面根本没看到（截图全黑 / 读图失败）：本轮"无匹配"没有任何语义。
        # 绝不能计入 empty_streak、绝不能 Verify 交卷（会拿残缺答案撞
        # "Please try again."），也不该 Reload 白扔一道题。
        # 连续多轮不可观测 = 截图管道坏了，重开 checkbox / 重新定位 bframe。
        if api_fail_count == OBS_UNRELIABLE:
            blind_streak += 1
            log(
                f"[经理] 盘面不可观测 (blind_streak={blind_streak}/{blind_limit}) — "
                f"不计入 empty_streak，不 Verify / 不 Reload",
                "WARN",
            )
            if blind_streak >= blind_limit:
                blind_streak = 0
                log(
                    "[经理] 连续不可观测 — 截图管道疑似损坏，强制重开 checkbox 并重新定位 bframe",
                    "ERROR",
                )
                try:
                    force_reopen_recaptcha(page, max_clicks=3)
                except Exception as e:
                    log(f"[经理] 不可观测恢复：重开失败 {e}", "WARN")
                time.sleep(1.5)
                if is_recaptcha_solved(page):
                    return True
                bframe = find_frame(page, "bframe", timeout=5) or bframe
                t2 = read_target(page)
                if t2:
                    target = t2
                dynamic = _looks_dynamic(bframe) if bframe else dynamic
                # 重开后是新题：清空本题状态，避免拿旧题的 clicked 去交卷
                empty_click_streak = 0
                clicked_any_this_challenge = False
                continue
            time.sleep(1.0)
            bframe = find_frame(page, "bframe", timeout=3) or bframe
            continue
        blind_streak = 0

        # API 超时/失败：本轮观测不可信，禁止当成"干净空盘"去 Verify/Reload
        if not clicked and api_fail_count > 0:
            log(
                f"[经理] 本轮 YOLO API 失败 {api_fail_count} 格 — "
                f"不计入 empty_streak，短等后重扫（不 Verify / 不 Reload）",
                "WARN",
            )
            time.sleep(1.2)
            continue

        if not dynamic:
            if not clicked:
                # 全空点：整盘没认出目标 → 难例小图
                _dump_hard("static_no_match")
                log("[经理] 静态 3x3：本轮无匹配 → 换题并等新图（先自动/再 Reload）")
                t_new, bframe, dynamic = _recover_after_static_fail(
                    page, before_fp=before_fp
                )
                if t_new:
                    target = t_new
                empty_click_streak = 0
                clicked_any_this_challenge = False
                # After recover: immediately solve next board (do not end loop empty)
                continue
            time.sleep(0.35)
            log("[经理] 静态 3x3：点击验证")
            fp_before_verify = before_fp or _snapshot_grid_fp(page)
            _click_verify(page)
            time.sleep(2.2)
            ok = is_recaptcha_solved(page)
            log(f"[经理] 静态验证结果 -> {ok}")
            if ok:
                return True
            err_kind = _classify_recaptcha_error(page)
            try_again_streak += 1 if err_kind else 0
            log(
                f"[经理] 静态验证未通过 kind={err_kind or 'unknown'!r} "
                f"(try_again_vis={bool(err_kind)})",
                "WARN",
            )
            # Verify 失败 = 漏选/选错 → 落盘难例
            if err_kind == "select_more":
                _dump_hard("static_select_more")
                # YOLO 漏点且残盘再扫也匹配不到 → 立刻内部 Reload，不重扫
                log(
                    "[经理] 静态 select-more → 内部 Reload 换题，不重扫残盘",
                    "WARN",
                )
                fp1 = fp_before_verify or _snapshot_grid_fp(page)
                _click_reload(page)
                time.sleep(1.0)
                _wait_fresh_grid_after_fail(page, before_fp=fp1, timeout=6.0)
                bframe = find_frame(page, "bframe", timeout=3)
                t_new = read_target(page, bframe=bframe) if bframe else read_target(page)
                if t_new:
                    target = t_new
                dynamic = _looks_dynamic(bframe) if bframe else dynamic
            else:
                _dump_hard("static_try_again" if err_kind else "static_verify_fail")
                t_new, bframe, dynamic = _recover_after_static_fail(
                    page, before_fp=fp_before_verify
                )
                if t_new:
                    target = t_new
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        # ----- 动态 3x3：点匹配格 → 等刷新 → 再扫；无匹配不要立刻 Verify -----
        if not clicked:
            empty_click_streak += 1
            # CRITICAL: empty match ≠ solved. Never Verify on first empty round
            # (user screenshot: Please try again after incomplete select).
            if empty_click_streak == 1:
                log(
                    f"[经理] 动态：本轮无匹配 (empty_streak=1) — 再扫一轮，禁止立刻 Verify",
                    "WARN",
                )
                time.sleep(0.8)
                t2 = read_target(page)
                if t2:
                    target = t2
                bframe = find_frame(page, "bframe", timeout=3) or bframe
                dynamic = _looks_dynamic(bframe) if bframe else True
                continue

            log(
                f"[经理] 动态：连续无匹配 (empty_streak={empty_click_streak})"
            )
            # 关键区分：
            #  - 本题点过匹配格 → 消消乐已清空 = 做完了 → Verify 交卷
            #    （空交卷会 Please try again，但"点过后再无匹配"正是完工信号）
            #  - 本题从头没点过 → 空交卷必被拒 → Reload 换新题
            if clicked_any_this_challenge:
                log("[经理] 动态：点过匹配格且已无剩余 → Verify 交卷")
                fp_before_verify = before_fp or _snapshot_grid_fp(page)
                _click_verify(page)
                time.sleep(2.2)
                if is_recaptcha_solved(page):
                    return SolveStatus.SUCCESS
                # Verify 被拒：按错误类型分流
                #  - select_more: YOLO 漏点且再扫也匹配不到 → 立刻内部 Reload 换题
                #  - dynamic_more: 追加了新图 → 同题继续扫（不清 clicked 也可，但盘面已变）
                #  - incorrect / 其他: 等自动换图，必要时 Reload
                err_kind = _classify_recaptcha_error(page)
                log(
                    f"[经理] 动态 Verify 未通过 kind={err_kind or 'unknown'!r} "
                    f"(try_again_vis={bool(err_kind)})",
                    "WARN",
                )
                if err_kind == "select_more":
                    _dump_hard("dynamic_select_more")
                    log(
                        "[经理] select-more（漏点且 YOLO 已匹配不到）→ 内部 Reload 换题，不重扫残盘",
                        "WARN",
                    )
                    fp1 = fp_before_verify or _snapshot_grid_fp(page)
                    _click_reload(page)
                    time.sleep(1.0)
                    _wait_fresh_grid_after_fail(page, before_fp=fp1, timeout=6.0)
                    bframe = find_frame(page, "bframe", timeout=3)
                    t_new = read_target(page, bframe=bframe) if bframe else read_target(page)
                    if t_new:
                        target = t_new
                    dynamic = _looks_dynamic(bframe) if bframe else dynamic
                    empty_click_streak = 0
                    clicked_any_this_challenge = False
                    continue

                # incorrect / dynamic_more / unknown：等 Google 换图，必要时 Reload
                _dump_hard("dynamic_try_again" if err_kind else "dynamic_verify_fail")
                t_new, bframe, dynamic = _recover_after_static_fail(
                    page, before_fp=fp_before_verify
                )
                if t_new:
                    target = t_new
                empty_click_streak = 0
                clicked_any_this_challenge = False
                continue

            _dump_hard("dynamic_no_match")
            log("[经理] 动态：从头无匹配 → 换题/Reload，不空 Verify")
            fp_before = before_fp or _snapshot_grid_fp(page)
            # Prefer reload over empty Verify (empty Verify burns tries)
            t_new, bframe, dynamic = _recover_after_static_fail(page, before_fp=fp_before)
            if t_new:
                target = t_new
            empty_click_streak = 0
            clicked_any_this_challenge = False
            continue

        empty_click_streak = 0
        clicked_any_this_challenge = True
        # 动态：必须等新图刷完再下一轮 crop（blank 时继续等）
        log(f"[经理] 动态：等待格子刷新完成再截图（timeout={refresh_timeout}s）…")
        ok_refresh = wait_dynamic_tiles_refreshed(
            page,
            before_fp,
            num_tiles=num_tiles_guess if num_tiles_guess in (9, 16) else 9,
            timeout=refresh_timeout,
        )
        if not ok_refresh:
            log(
                "[经理] 刷新未确认且可能仍有 blank — 再等 2s 后继续下一轮",
                "WARN",
            )
            time.sleep(2.0)
        else:
            time.sleep(random.uniform(0.25, 0.45))

        if is_recaptcha_solved(page):
            return True
        t2 = read_target(page)
        if t2:
            target = t2
        if try_again_streak > 0:
            try_again_streak = max(0, try_again_streak - 1)

    log(f"[经理] 死磕轮次用尽 ({max_rounds}) — 仍未 solved（未刷新页面）")
    if is_recaptcha_solved(page):
        return SolveStatus.SUCCESS
    # 最后一次：若动态且已无匹配倾向，再 Verify 一次
    try:
        log("[经理] 末轮兜底 Verify 一次")
        _click_verify(page)
        time.sleep(2.5)
    except Exception:
        pass
    return bool(is_recaptcha_solved(page))


def handle_recaptcha_yolo(
    page, screenshot_dir: str = "output/screenshots", max_rounds: Optional[int] = None
) -> SolveStatus:
    """Run the solver and expose one stable result type to callers."""
    result = _handle_recaptcha_yolo(page, screenshot_dir, max_rounds)
    if isinstance(result, SolveStatus):
        return result
    return SolveStatus.SUCCESS if result else SolveStatus.FAILED
