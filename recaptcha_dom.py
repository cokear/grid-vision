# -*- coding: utf-8 -*-
"""
DrissionPage helpers for the standalone YOLO reCAPTCHA solver.
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore


def log(msg: str, level: str = "INFO") -> None:
    try:
        from logutil import log as _ulog, vlog, log_once, verbose

        # High-frequency probes: collapse unless verbose
        m = str(msg or "")
        if not verbose() and (
            m.startswith("challenge UI ready")
            or m.startswith("bframe ready:")
            or "instruction raw:" in m
            or m.startswith("grid ready for YOLO")
            or m.startswith("paint still")
            or m.startswith("waiting grid fingerprint")
            or m.startswith("dynamic tiles refreshed")
        ):
            if m.startswith("challenge UI ready") or m.startswith("bframe ready:"):
                log_once("ui-ready", m[:120], level=level, every=3.0, tag="yolo")
            elif "instruction raw:" in m:
                vlog(m[:160], level=level, tag="yolo")
            else:
                vlog(m, level=level, tag="yolo")
            return
        _ulog(msg, level=level, tag="yolo")
    except Exception:
        print(f"[yolo][{level}] {msg}", flush=True)


def _iframe_meta(iframe) -> Tuple[str, str]:
    try:
        src = (iframe.attr("src") or "") + ""
    except Exception:
        src = ""
    try:
        title = (iframe.attr("title") or "") + ""
    except Exception:
        title = ""
    return src, title


def _is_bframe_iframe(src: str, title: str) -> bool:
    blob = (src + " " + title).lower()
    if "bframe" in blob:
        return True
    if "recaptcha challenge" in title.lower():
        return True
    # some builds only have api2/bframe in path
    if "recaptcha" in blob and "bframe" in src.lower():
        return True
    return False


def _is_anchor_iframe(src: str, title: str) -> bool:
    blob = (src + " " + title).lower()
    if "bframe" in blob:
        return False
    return "anchor" in blob or ("recaptcha" in blob and "api2/anchor" in src.lower())


def _enter_iframe(page, iframe):
    """
    Enter iframe document. Returning the raw <iframe> element is NOT enough —
    .ele() would still search wrong document. Prefer get_frame.
    """
    errors = []
    for how in ("get_frame", "to_frame", "raw"):
        try:
            if how == "get_frame":
                fr = page.get_frame(iframe)
            elif how == "to_frame" and hasattr(iframe, "get_frame"):
                fr = iframe.get_frame()
            else:
                fr = iframe
            if fr is None:
                continue
            # smoke: can we run js in this context?
            try:
                fr.run_js("return 1")
            except Exception as e:
                errors.append(f"{how}:js:{e}")
                # still try ele later
            return fr
        except Exception as e:
            errors.append(f"{how}:{e}")
    if errors:
        log(f"enter iframe failed: {errors[:3]}", "WARN")
    return None


def find_frame(page, kind: str = "bframe", timeout: float = 6.0):
    """kind: anchor | bframe. Returns ChromiumFrame inside the iframe."""
    deadline = time.time() + max(1.0, float(timeout))
    want_bframe = kind == "bframe"
    while time.time() < deadline:
        try:
            iframes = page.eles("tag:iframe") or []
        except Exception:
            iframes = []
        # also try nested (rare)
        for iframe in iframes:
            src, title = _iframe_meta(iframe)
            if want_bframe:
                if not _is_bframe_iframe(src, title):
                    continue
            else:
                if not _is_anchor_iframe(src, title):
                    continue
            fr = _enter_iframe(page, iframe)
            if fr is not None:
                return fr
        time.sleep(0.3)
    return None


def _frame_has_challenge_ui(bframe) -> Tuple[bool, str]:
    """Return (ok, detail) if bframe document looks like image challenge."""
    if not bframe:
        return False, "no frame"
    # JS probe is more reliable than ele when class matching is picky
    try:
        info = bframe.run_js(
            """
            const out = {
              instr: !!document.querySelector('.rc-imageselect-instructions'),
              table: !!document.querySelector('table.rc-imageselect-table, table'),
              td: document.querySelectorAll('td').length,
              img: document.querySelectorAll('img').length,
              strong: '',
              bodyLen: (document.body && (document.body.innerText||'').length) || 0,
              title: document.title || ''
            };
            const st = document.querySelector('.rc-imageselect-instructions strong, strong');
            if (st) out.strong = (st.innerText||st.textContent||'').trim().slice(0, 40);
            const it = document.querySelector('.rc-imageselect-instructions');
            if (it) out.instrText = (it.innerText||'').trim().slice(0, 80);
            return out;
            """
        )
        if isinstance(info, dict):
            if info.get("instr") or info.get("table") or int(info.get("td") or 0) >= 9:
                return True, str(info)
            return False, str(info)
        if info:
            return True, str(info)[:120]
    except Exception as e:
        js_err = str(e)[:80]
    else:
        js_err = ""

    # ele fallbacks
    for sel, name in (
        (".rc-imageselect-instructions", "instr"),
        ("tag:table@class:rc-imageselect-table", "table"),
        ("css:table.rc-imageselect-table", "table_css"),
        (".rc-imageselect-tile", "tile"),
        ("#rc-imageselect", "root"),
    ):
        try:
            if bframe.ele(sel, timeout=0.4):
                return True, f"ele:{name}"
        except Exception:
            continue
    return False, js_err or "empty"


def wait_bframe_ready(page, timeout: float = 18.0):
    """
    After checkbox: wait until challenge iframe is ENTERED and UI is queryable.
    Logs diagnostic when stuck on 'bframe found but no UI'.
    """
    deadline = time.time() + max(3.0, float(timeout))
    last_note = ""
    last_detail = ""
    while time.time() < deadline:
        bframe = find_frame(page, "bframe", timeout=1.5)
        if not bframe:
            note = "waiting bframe iframe"
            if note != last_note:
                log(note)
                last_note = note
            time.sleep(0.4)
            continue

        ok, detail = _frame_has_challenge_ui(bframe)
        if ok:
            log(f"bframe ready: {detail[:160]}")
            return bframe

        note = "bframe found, waiting instructions/table"
        if note != last_note or detail != last_detail:
            log(f"{note} | probe={detail[:160]}")
            last_note = note
            last_detail = detail
        time.sleep(0.4)

    # final probe for logs
    bframe = find_frame(page, "bframe", timeout=2.0)
    if bframe:
        ok, detail = _frame_has_challenge_ui(bframe)
        log(f"wait_bframe_ready timeout | last_probe={detail[:200]}", "WARN")
        # still return frame so read_target can try harder / log raw
        return bframe
    log("wait_bframe_ready timeout | no bframe at all", "WARN")
    return None


def is_recaptcha_solved(page) -> bool:
    try:
        tok = page.run_js(
            """
            const el = document.querySelector('#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
            if (!el) return '';
            return (el.value || el.innerText || '').trim();
            """
        )
        if tok and len(str(tok)) > 20:
            return True
    except Exception:
        pass
    try:
        anchor = find_frame(page, "anchor", timeout=1.5)
        if anchor:
            checked = anchor.run_js(
                """
                const c = document.querySelector('.recaptcha-checkbox-checked, [aria-checked="true"]');
                return !!c;
                """
            )
            if checked:
                return True
    except Exception:
        pass
    return False


def _text_says_challenge_expired(text: str) -> bool:
    """Strict match only — do NOT match normal bframe templates like
    'Please also check the new images' / hidden 'Please try again'.
    """
    low = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not low:
        return False
    # Google product strings (with/without punctuation spacing)
    if "verification challenge expired" in low:
        return True
    if "check the checkbox again" in low:
        return True
    if "challenge expired" in low and "checkbox" in low:
        return True
    # compact form from screenshots: "expired.check the checkbox"
    if "expired" in low and "checkbox again" in low:
        return True
    return False


def probe_recaptcha_state(page) -> dict:
    """Multi-signal recaptcha state. Used for expire / reopen decisions.

    Signals (any reliable expire phrase OR dead-widget combo):
      - anchor body / error / aria text
      - host body (sometimes mirrors)
      - checked?
      - image challenge UI ready?
      - tile count
      - g-recaptcha-response token length
    """
    state = {
        "ui_ready": False,
        "tile_count": 0,
        "checked": False,
        "token_len": 0,
        "expired_text": False,
        "expired_reason": "",
        "anchor_body": "",
        "host_hit": False,
        "has_widget": False,
    }
    try:
        state["ui_ready"] = bool(challenge_ui_ready(page, timeout=0.4))
    except Exception:
        state["ui_ready"] = False
    try:
        _b, _t, tiles = get_table_tiles(page)
        state["tile_count"] = len(tiles or [])
    except Exception:
        state["tile_count"] = 0
    try:
        state["token_len"] = int(
            page.run_js(
                """
                const el = document.querySelector(
                  '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
                );
                return el && el.value ? el.value.length : 0;
                """
            )
            or 0
        )
    except Exception:
        state["token_len"] = 0
    try:
        state["has_widget"] = bool(
            page.run_js(
                """
                return !!(
                  document.querySelector("iframe[src*='recaptcha']")
                  || document.querySelector('.g-recaptcha')
                  || document.querySelector("iframe[title*='reCAPTCHA']")
                );
                """
            )
        )
    except Exception:
        pass

    # Host text (usually misses iframe, but cheap)
    try:
        host = page.run_js(
            """
            return ((document.body && (document.body.innerText || document.body.textContent)) || '')
              .replace(/\\s+/g, ' ').slice(0, 2000);
            """
        ) or ""
        if _text_says_challenge_expired(str(host)):
            state["expired_text"] = True
            state["expired_reason"] = "host"
            state["host_hit"] = True
    except Exception:
        pass

    # Anchor iframe — primary place for the red banner
    try:
        anchor = find_frame(page, "anchor", timeout=2.0)
        if anchor:
            info = anchor.run_js(
                """
                const body = (document.body && (document.body.innerText || document.body.textContent) || '');
                const bits = [];
                document.querySelectorAll(
                  '.rc-anchor-error-msg, .rc-anchor-error-msg-container, '
                  + '.rc-anchor-error-message, #recaptcha-accessible-status, '
                  + '.rc-anchor-aria-status, .rc-anchor-content, #rc-anchor-container'
                ).forEach(el => {
                  const t = ((el.innerText || el.textContent || '') + '').trim();
                  if (t) bits.push(t);
                });
                // also any red-ish text nodes near anchor
                document.querySelectorAll('div,span,label').forEach(el => {
                  try {
                    const st = getComputedStyle(el);
                    const t = ((el.innerText || el.textContent || '') + '').trim();
                    if (!t || t.length > 120) return;
                    const c = (st.color || '').toLowerCase();
                    // red-ish
                    const m = c.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                    if (m) {
                      const r=+m[1], g=+m[2], b=+m[3];
                      if (r > 150 && r > g + 40 && r > b + 40) bits.push(t);
                    }
                  } catch (e) {}
                });
                const checked = !!document.querySelector(
                  '.recaptcha-checkbox-checked, #recaptcha-anchor[aria-checked="true"]'
                );
                const aria = document.querySelector('#recaptcha-anchor');
                const ariaChecked = aria ? (aria.getAttribute('aria-checked') || '') : '';
                return {
                  body: body.replace(/\\s+/g, ' ').slice(0, 500),
                  bits: bits.slice(0, 12).map(x => x.replace(/\\s+/g, ' ').slice(0, 120)),
                  checked: checked,
                  ariaChecked: ariaChecked,
                };
                """
            )
            if isinstance(info, dict):
                state["checked"] = bool(info.get("checked"))
                body = str(info.get("body") or "")
                state["anchor_body"] = body[:200]
                blob = body + " " + " ".join(str(x) for x in (info.get("bits") or []))
                if _text_says_challenge_expired(blob):
                    state["expired_text"] = True
                    state["expired_reason"] = state["expired_reason"] or "anchor"
    except Exception as e:
        log(f"probe anchor failed: {e}", "WARN")

    return state


def recaptcha_expired_or_need_checkbox(page) -> bool:
    """True when challenge is dead and checkbox must be re-clicked.

    Screenshot state we must catch every time:
      red: "Verification challenge expired. Check the checkbox again."
      unchecked square, no image grid, no token.

    Decision:
      1) image grid ready → NOT expired
      2) expire phrase in anchor/host → expired
      3) dead widget (has widget, unchecked, no tiles, no token) → need checkbox
         (do NOT require empty_streak or text — text scrape is flaky cross-origin)
    """
    st = probe_recaptcha_state(page)
    if st.get("ui_ready") or int(st.get("tile_count") or 0) >= 9:
        return False
    if st.get("expired_text"):
        log(
            f"expired HIT reason={st.get('expired_reason')} "
            f"checked={st.get('checked')} tiles={st.get('tile_count')} "
            f"anchor={st.get('anchor_body')!r}",
            "WARN",
        )
        return True
    # Dead widget: the exact screenshot state even if text scrape missed
    if (
        st.get("has_widget")
        and not st.get("checked")
        and int(st.get("token_len") or 0) < 20
        and int(st.get("tile_count") or 0) < 4
        and not st.get("ui_ready")
    ):
        log(
            "expired/dead-widget HIT "
            f"checked=0 tiles={st.get('tile_count')} token={st.get('token_len')} "
            f"anchor={st.get('anchor_body')!r}",
            "WARN",
        )
        return True
    return False


def need_force_checkbox_reopen(page, empty_streak: int = 0) -> bool:
    """Broader reopen gate for the main loop.

    Use when:
      - true expired text / dead widget, OR
      - empty_streak>=1 with no tiles/unchecked (don't wait for streak=2)
    """
    if recaptcha_expired_or_need_checkbox(page):
        return True
    st = probe_recaptcha_state(page)
    if st.get("ui_ready") or int(st.get("tile_count") or 0) >= 9:
        return False
    if (
        int(empty_streak or 0) >= 1
        and st.get("has_widget")
        and not st.get("checked")
        and int(st.get("token_len") or 0) < 20
        and int(st.get("tile_count") or 0) < 4
    ):
        log(
            f"force-reopen: dead widget empty_streak={empty_streak} "
            f"checked=0 tiles=0 token=0",
            "WARN",
        )
        return True
    return False


def force_reopen_recaptcha(page, max_clicks: int = 4) -> bool:
    """Hard reopen path for expired checkbox state.

    Returns True if challenge UI is open or recaptcha already solved.
    Tries multiple click methods per attempt (in-frame + host iframe + CDP).
    """
    if is_recaptcha_solved(page):
        return True
    if challenge_ui_ready(page, timeout=0.6):
        return True

    for n in range(1, max(1, int(max_clicks)) + 1):
        st = probe_recaptcha_state(page)
        log(
            f"force_reopen #{n}/{max_clicks} "
            f"checked={st.get('checked')} tiles={st.get('tile_count')} "
            f"token={st.get('token_len')} expired_text={st.get('expired_text')}"
        )
        # Always force click — ignore "already checked" lies after expire
        try:
            click_checkbox(page, force=True)
        except Exception as e:
            log(f"force_reopen click_checkbox failed: {e}", "WARN")
        time.sleep(1.0)
        # Always also hit host iframe coords (more reliable when frame API flaky)
        try:
            _click_anchor_iframe_on_host(page)
        except Exception as e:
            log(f"force_reopen host iframe click failed: {e}", "WARN")
        time.sleep(1.4)

        if is_recaptcha_solved(page):
            log("force_reopen: solved by checkbox only")
            return True
        if challenge_ui_ready(page, timeout=1.5):
            log("force_reopen: challenge UI open")
            return True
        # tile count as secondary
        try:
            _b, _t, tiles = get_table_tiles(page)
            if tiles and len(tiles) >= 9:
                log(f"force_reopen: tiles={len(tiles)}")
                return True
        except Exception:
            pass

    log("force_reopen FAILED — still no challenge UI", "ERROR")
    return bool(is_recaptcha_solved(page) or challenge_ui_ready(page, timeout=0.5))


def challenge_ui_ready(page, timeout: float = 1.5) -> bool:
    """True only when bframe has real challenge UI (instructions/table/tiles).

    An empty bframe iframe often exists in the parent DOM before the user
    ever clicks the checkbox — that must NOT count as 'challenge open'.
    """
    bframe = find_frame(page, "bframe", timeout=timeout)
    if not bframe:
        return False
    ok, detail = _frame_has_challenge_ui(bframe)
    if ok:
        log(f"challenge UI ready: {str(detail)[:120]}")
    return bool(ok)


def _click_anchor_iframe_on_host(page) -> bool:
    """Click the host-page anchor iframe (left side where checkbox is).

    Works even when get_frame into anchor is flaky. Prefer left-center of
    iframe rect (checkbox sits on the left of the anchor widget).
    """
    try:
        rect = page.run_js(
            """
            const iframe = document.querySelector(
              "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha'][src*='anchor'], "
              + "iframe[title*='reCAPTCHA'], iframe[title*='recaptcha']"
            );
            if (!iframe) return null;
            const r = iframe.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) return null;
            return {
              x: r.left + Math.min(28, r.width * 0.18),
              y: r.top + r.height * 0.5,
              w: r.width,
              h: r.height,
            };
            """
        )
    except Exception as e:
        log(f"host iframe rect failed: {e}", "WARN")
        return False
    if not isinstance(rect, dict):
        return False
    x, y = float(rect.get("x") or 0), float(rect.get("y") or 0)
    log(f"host-click anchor iframe at ({x:.0f},{y:.0f}) size={rect.get('w')}x{rect.get('h')}")
    # Try several click styles
    try:
        page.run_js(
            """
            const x = arguments[0], y = arguments[1];
            const el = document.elementFromPoint(x, y) || document.querySelector(
              "iframe[src*='recaptcha'][src*='anchor'], iframe[title*='reCAPTCHA']"
            );
            if (!el) return 'no-el';
            el.focus && el.focus();
            ['pointerdown','mousedown','mouseup','click'].forEach(t => {
              el.dispatchEvent(new MouseEvent(t, {
                bubbles: true, cancelable: true, view: window,
                clientX: x, clientY: y,
              }));
            });
            // also click iframe element itself
            if (el.tagName === 'IFRAME') el.click();
            return 'ok';
            """,
            x,
            y,
        )
    except Exception as e:
        log(f"host elementFromPoint click failed: {e}", "WARN")
    # DrissionPage actions click by coordinate if available
    try:
        if hasattr(page, "actions"):
            page.actions.move_to(x, y).click()
            log("host actions.move_to click")
            time.sleep(1.2)
            return True
    except Exception:
        pass
    try:
        # Some DP builds: page.run_cdp Input.dispatchMouseEvent
        for typ in ("mousePressed", "mouseReleased"):
            page.run_cdp(
                "Input.dispatchMouseEvent",
                type=typ,
                x=x,
                y=y,
                button="left",
                clickCount=1,
            )
        log("host CDP mouse click")
        time.sleep(1.2)
        return True
    except Exception as e:
        log(f"host CDP click failed: {e}", "WARN")
    time.sleep(0.8)
    return True


def click_checkbox(page, force: bool = False) -> bool:
    """Click the reCAPTCHA anchor checkbox to open the image challenge.

    force=True: always click (expired reopen). Never skip for 'already checked'.
    On force, try BOTH in-frame click AND host iframe coordinate click.
    """
    try:
        force_expired = force
        if not force_expired:
            try:
                force_expired = recaptcha_expired_or_need_checkbox(page)
            except Exception:
                force_expired = False

        # Only skip when challenge image UI is open AND not forcing
        if not force_expired:
            try:
                if challenge_ui_ready(page, timeout=0.4):
                    log("challenge already open — skip checkbox click")
                    return True
            except Exception:
                pass
        else:
            log("force checkbox click (expired/dead widget)")

        clicked = False
        anchor = find_frame(page, "anchor", timeout=5)
        if anchor and not force_expired:
            try:
                already = anchor.run_js(
                    """
                    return !!document.querySelector(
                      '.recaptcha-checkbox-checked, #recaptcha-anchor[aria-checked="true"]'
                    );
                    """
                )
                if already:
                    log("checkbox already checked")
                    return True
            except Exception:
                pass

        if anchor:
            # If force-expired, try uncheck-looking state by always clicking the box
            box = None
            for sel in (
                "#recaptcha-anchor",
                ".recaptcha-checkbox-border",
                ".recaptcha-checkbox",
                ".rc-anchor-checkbox",
                ".rc-anchor-checkbox-label",
            ):
                try:
                    box = anchor.ele(sel, timeout=1.2)
                except Exception:
                    box = None
                if box:
                    break
            if box:
                try:
                    box.click()
                    clicked = True
                except Exception:
                    try:
                        box.click(by_js=True)
                        clicked = True
                    except Exception:
                        pass
            if not clicked:
                try:
                    ok = anchor.run_js(
                        """
                        const b = document.querySelector(
                          '#recaptcha-anchor, .recaptcha-checkbox-border, '
                          + '.recaptcha-checkbox, .rc-anchor-checkbox-label'
                        );
                        if (!b) return false;
                        b.click();
                        return true;
                        """
                    )
                    clicked = bool(ok)
                except Exception as e:
                    log(f"anchor JS click failed: {e}", "WARN")
            if clicked:
                log("checkbox clicked (in-frame)")
                time.sleep(0.8 if force_expired else 1.5)
                # force path: also do host click — don't return early
                if not force_expired:
                    return True

        # Host-page iframe click (most reliable for expired dead state)
        log("checkbox: host iframe / widget click")
        host_ok = False
        try:
            host_ok = bool(_click_anchor_iframe_on_host(page))
        except Exception as e:
            log(f"host iframe click failed: {e}", "WARN")

        if not host_ok:
            try:
                page.run_js(
                    """
                    const iframe = document.querySelector(
                      "iframe[src*='recaptcha/api2/anchor'], iframe[title*='reCAPTCHA']"
                    );
                    if (iframe) { iframe.click(); return 'iframe'; }
                    const w = document.querySelector('.g-recaptcha, .rc-anchor');
                    if (w) { w.click(); return 'widget'; }
                    return 'none';
                    """
                )
                log("checkbox clicked via host widget fallback")
                host_ok = True
            except Exception as e:
                log(f"checkbox host fallback failed: {e}", "WARN")

        time.sleep(1.2)
        return bool(clicked or host_ok)
    except Exception as e:
        log(f"checkbox click failed: {e}", "WARN")
        return False


def safe_click(ele) -> bool:
    if not ele:
        return False
    try:
        ele.click()
        return True
    except Exception:
        try:
            ele.click(by_js=True)
            return True
        except Exception:
            return False


def screenshot_element(ele, path: str, retries: int = 2) -> bool:
    if not ele or not path:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    for _ in range(max(1, retries)):
        try:
            if hasattr(ele, "get_screenshot"):
                ele.get_screenshot(path=path)
            else:
                ele.screenshot(path)
            if os.path.isfile(path) and os.path.getsize(path) > 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


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
        log(f"crop {tile_idx} failed: {e}", "WARN")
        return None


def get_table_tiles(page) -> Tuple[object, object, List]:
    """Returns (bframe, table, tiles_list) or (None, None, [])."""
    bframe = find_frame(page, "bframe", timeout=8)
    if not bframe:
        return None, None, []
    table = None
    for sel in (
        "tag:table@class:rc-imageselect-table",
        "css:table.rc-imageselect-table",
        "css:.rc-imageselect-table",
        "tag:table",
    ):
        try:
            table = bframe.ele(sel, timeout=2)
            if table:
                break
        except Exception:
            table = None
    if not table:
        try:
            # JS: find table with many tds
            has = bframe.run_js(
                "return !!document.querySelector('table.rc-imageselect-table, table');"
            )
            if has:
                table = bframe.ele("tag:table", timeout=1)
        except Exception as e:
            log(f"table js: {e}", "WARN")
    try:
        tiles = table.eles("tag:td") if table else []
        if tiles and len(tiles) < 4:
            # try image tiles
            tiles = table.eles("css:.rc-imageselect-tile") or tiles
        return bframe, table, list(tiles or [])
    except Exception as e:
        log(f"table: {e}", "WARN")
        return bframe, None, []


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


def _blank_diag(info: dict) -> str:
    """Compact one-line reason for a blank/foggy verdict (black screen vs read fail)."""
    if not isinstance(info, dict):
        return ""
    why = str(info.get("why") or "")
    if why:
        return f" why={why}"
    kinds = info.get("kinds") or {}
    parts = [f"{k}x{v}" for k, v in sorted(kinds.items()) if v]
    stats = info.get("stats") or ""
    out = ""
    if parts:
        out += " kind=" + ",".join(parts)
    if stats:
        out += f" {stats}"
    return out


def _tile_blank_or_foggy(
    png_path: str, num_tiles: int = 9, diag: Optional[dict] = None
) -> Tuple[list, bool]:
    """Return (blank_indices, any_foggy). Local pixel stats only — no cloud.

    diag: optional dict filled in-place with why/kinds/stats so callers can tell
    a genuinely black grid apart from a screenshot/decode failure.
    """
    blanks = []
    foggy = False
    if Image is None:
        if diag is not None:
            diag["why"] = "PIL-missing"
        return list(range(max(1, num_tiles))), True
    if not png_path or not os.path.isfile(png_path):
        if diag is not None:
            diag["why"] = "shot-missing"
        return list(range(max(1, num_tiles))), True
    try:
        if os.path.getsize(png_path) <= 0:
            if diag is not None:
                diag["why"] = "shot-empty(0B)"
            return list(range(max(1, num_tiles))), True
    except Exception:
        pass
    kinds: dict = {}
    samples: list = []
    try:
        img = Image.open(png_path).convert("RGB")
        w, h = img.size
        side = 4 if num_tiles >= 16 else 3
        if num_tiles == 16:
            side = 4
        elif num_tiles == 9:
            side = 3
        else:
            side = max(2, int(round(math.sqrt(num_tiles))))
        left, top = int(w * 0.02), int(h * 0.02)
        right, bottom = int(w * 0.98), int(h * 0.98)
        grid = img.crop((left, top, right, bottom))
        gw, gh = grid.size
        cw, ch = gw / float(side), gh / float(side)
        for idx in range(side * side):
            row, col = divmod(idx, side)
            x0, y0 = int(col * cw), int(row * ch)
            x1, y1 = int((col + 1) * cw), int((row + 1) * ch)
            tile = grid.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
            tw, th = tile.size
            ix, iy = max(2, int(tw * 0.12)), max(2, int(th * 0.14))
            core = tile.crop((ix, iy, tw - max(1, ix // 2), th - max(1, iy // 2)))
            sample = core.resize((28, 28))
            pixels = list(sample.getdata())
            if not pixels:
                blanks.append(idx)
                kinds["nopix"] = kinds.get("nopix", 0) + 1
                continue
            grays = [(r + g + b) / 3.0 for r, g, b in pixels]
            mean = sum(grays) / len(grays)
            var = sum((g - mean) ** 2 for g in grays) / len(grays)
            uniq = len({(r // 16, g // 16, b // 16) for r, g, b in pixels})
            nw = sum(1 for r, g, b in pixels if r >= 240 and g >= 240 and b >= 240) / float(
                len(pixels)
            )
            if len(samples) < 3:
                samples.append(f"t{idx}(m={mean:.0f},v={var:.0f},u={uniq})")
            # blank / loading
            if nw >= 0.88 or (nw >= 0.75 and var < 120 and uniq <= 8):
                blanks.append(idx)
                kinds["white"] = kinds.get("white", 0) + 1
                continue
            if mean >= 248 and var < 12 and uniq <= 4:
                blanks.append(idx)
                kinds["white"] = kinds.get("white", 0) + 1
                continue
            if mean <= 55 and var < 12 and uniq <= 4:
                blanks.append(idx)
                kinds["black"] = kinds.get("black", 0) + 1
                continue
            # foggy half-paint
            if mean >= 225 and var < 100 and uniq <= 16:
                foggy = True
            elif mean >= 210 and nw >= 0.25 and var < 140 and uniq <= 18:
                foggy = True
            elif var < 25 and uniq <= 8 and 90 <= mean <= 245:
                foggy = True
        if diag is not None:
            diag["kinds"] = kinds
            diag["stats"] = " ".join(samples)
        return blanks, foggy
    except Exception as e:
        if diag is not None:
            diag["why"] = f"decode-fail({type(e).__name__})"
        return list(range(max(1, num_tiles))), True


def wait_grid_ready_for_shot(
    page,
    png_path: str,
    num_tiles: int = 9,
    timeout: float = 6.0,
    diag: Optional[dict] = None,
) -> bool:
    """Wait until grid can be cropped for YOLO (no blank/fog). First round / after open.

    diag: optional dict filled in-place on failure with the last verdict, so the
    caller can tell "still painting" (white/fog) from a broken screenshot pipeline
    (all-black / shot-missing / decode-fail) that no amount of waiting will fix.
    """
    deadline = time.time() + max(2.0, float(timeout))
    last: dict = {}
    shot_fail = 0
    while time.time() < deadline:
        _b, table, tiles = get_table_tiles(page)
        n = len(tiles) if tiles else num_tiles
        if not table:
            last = {"why": "no-table"}
            time.sleep(0.35)
            continue
        if not screenshot_element(table, png_path, retries=1):
            shot_fail += 1
            last = {"why": "shot-fail"}
            time.sleep(0.35)
            continue
        d: dict = {}
        blanks, fog = _tile_blank_or_foggy(png_path, n, diag=d)
        if not blanks and not fog:
            log("grid ready for YOLO crop")
            return True
        last = dict(d)
        last["blanks"] = len(blanks)
        last["all_blank"] = bool(blanks) and len(blanks) >= n
        log(f"grid not ready blanks={blanks} fog={int(fog)}{_blank_diag(d)}")
        time.sleep(0.45)
    if diag is not None:
        diag.update(last)
        diag["shot_fail"] = shot_fail
        # Broken pipeline: every tile black, or we never got a readable PNG.
        kinds = last.get("kinds") or {}
        diag["broken"] = bool(
            last.get("why")
            or (last.get("all_blank") and kinds.get("black"))
        )
    log(f"wait_grid_ready timeout{_blank_diag(last)}", "WARN")
    return False


def wait_dynamic_tiles_refreshed(
    page,
    before_fp,
    num_tiles: int = 9,
    timeout: float = 12.0,
) -> bool:
    """
    After clicking dynamic tiles: wait until
      1) whole-grid screenshot fingerprint changes
      2) no blank loading tiles (keep waiting while blanks exist)
      3) paint not foggy
      4) two consecutive stable fingerprints
    MUST call this before the next YOLO crop round.
    Default timeout 12s; on timeout still try one extra settle if blanks remain.
    """
    timeout = max(10.0, float(timeout))
    deadline = time.time() + timeout
    tmp = os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
        f"yolo_dyn_wait_{os.getpid()}.png",
    )
    last_fp = None
    stable_hits = 0
    saw_change = before_fp is None
    last_blanks = []

    time.sleep(0.55)  # let selection animation start

    while time.time() < deadline:
        _b, table, tiles = get_table_tiles(page)
        n = len(tiles) if tiles else num_tiles
        if not table:
            time.sleep(0.3)
            continue
        if not screenshot_element(table, tmp, retries=1):
            time.sleep(0.3)
            continue

        blanks, fog = _tile_blank_or_foggy(tmp, n)
        last_blanks = blanks
        # deep black dead challenge
        if len(blanks) >= max(6, n - 1):
            try:
                if Image is not None:
                    img = Image.open(tmp).convert("RGB").resize((32, 32))
                    px = list(img.getdata())
                    mean = sum((r + g + b) / 3.0 for r, g, b in px) / max(1, len(px))
                    if mean <= 90:
                        log("challenge looks dead (dark mask) during refresh wait", "WARN")
                        return False
            except Exception:
                pass

        fp = grid_fingerprint(tmp)
        if before_fp is not None and fp is not None and fp != before_fp:
            saw_change = True

        if not saw_change:
            log("waiting grid fingerprint change after click...")
            time.sleep(0.4)
            continue

        # 有 blank 就继续等，不要急着全格 YOLO
        if blanks:
            log(f"paint still loading blanks={blanks} fog={int(fog)} — keep waiting")
            last_fp = None
            stable_hits = 0
            time.sleep(0.5)
            continue

        if fog:
            log("paint still foggy — keep waiting")
            last_fp = None
            stable_hits = 0
            time.sleep(0.45)
            continue

        if last_fp is not None and fp == last_fp:
            stable_hits += 1
            if stable_hits >= 1:
                log("dynamic tiles refreshed + paint stable")
                return True
        else:
            if last_fp is not None:
                log("paint still changing, wait another stable frame")
            stable_hits = 0
        last_fp = fp
        time.sleep(0.45)

    # 超时：若仍有 blank，再强制多等一轮，避免立刻全格 YOLO
    if last_blanks:
        log(
            f"refresh timeout ({timeout}s) still blanks={last_blanks} — extra settle 2s",
            "WARN",
        )
        time.sleep(2.0)
        _b, table, tiles = get_table_tiles(page)
        n = len(tiles) if tiles else num_tiles
        if table and screenshot_element(table, tmp, retries=1):
            blanks, fog = _tile_blank_or_foggy(tmp, n)
            if not blanks and not fog:
                log("extra settle: grid clear, allow next YOLO")
                return True
            log(f"extra settle still not clear blanks={blanks} fog={int(fog)}", "WARN")
            return False

    log(f"dynamic refresh wait timeout ({timeout}s) — next shot may be stale", "WARN")
    return False


# 长词优先匹配（避免「桥」误伤其它词）
_CN_TO_EN = {
    "消防栓": "hydrant",
    "灭火栓": "hydrant",
    "人行横道": "crosswalk",
    "斑马线": "crosswalk",
    "停车计时器": "parking meter",
    "交通灯": "traffic light",
    "红绿灯": "traffic light",
    "信号灯": "traffic light",
    "棕榈树": "palm tree",
    "摩托车": "motorcycle",
    "自行车": "bicycle",
    "单车": "bicycle",
    "公交车": "bus",
    "巴士": "bus",
    "小汽车": "car",
    "轿车": "car",
    "汽车": "car",
    "卡车": "truck",
    "货车": "truck",
    "拖拉机": "tractor",
    "楼梯": "stair",
    "台阶": "stair",
    "烟囱": "chimney",
    "船": "boat",
    "桥": "bridge",  # 你截图：请选择包含「桥」的所有图片
}

# 中文题面（reCAPTCHA 中文界面）
_CN_SENTENCE_PATS = [
    r"请选择包含\s*(.+?)\s*的所有图片",
    r"请选择包含\s*(.+?)\s*的图片",
    r"选择包含\s*(.+?)\s*的所有图片",
    r"点击包含\s*(.+?)\s*的",
]

_EN_PATS = [
    r"select all (?:images|squares|tiles|pictures)?\s*(?:with|containing)\s+(?:a |an )?([a-z][a-z\s\-]{0,30})",
    r"(?:images|squares|tiles|pictures) with (?:a |an )?([a-z][a-z\s\-]{0,30})",
    r"with (?:a |an )?([a-z][a-z\s\-]{0,20})(?:\.|,|$|\n)",
]


def _extract_instruction_text(bframe) -> str:
    """
    从 bframe 内抓考题文字。

    你提供的 DOM（中文 reCAPTCHA）结构：
      .rc-imageselect-instructions
        .rc-imageselect-desc
          请选择包含 <strong>桥</strong> 的所有图片。
    strong 里是目标词，整段 desc 是完整题面。
    """
    if not bframe:
        return ""
    texts = []
    strong_only = ""

    # 0) 优先 strong（大号加粗目标词，中文「桥」就在这里）
    for sel in (
        ".rc-imageselect-desc strong",
        ".rc-imageselect-instructions strong",
        "#rc-imageselect strong",
        "css:.rc-imageselect-desc strong",
        "tag:strong",
    ):
        try:
            st = bframe.ele(sel, timeout=0.8)
            if st:
                t = (st.text or "").strip()
                if t and len(t) <= 20:
                    strong_only = t
                    texts.append(t)
                    break
        except Exception:
            continue

    # 1) 整段说明
    selectors = [
        ".rc-imageselect-desc",
        ".rc-imageselect-desc-wrapper",
        ".rc-imageselect-instructions",
        ".rc-imageselect-desc-text",
        "#rc-imageselect",
    ]
    for sel in selectors:
        try:
            ele = bframe.ele(sel, timeout=0.6)
            if not ele:
                continue
            t = (ele.text or "").strip()
            if t:
                texts.append(t)
        except Exception:
            continue

    # 2) JS：一次取出 full + strong（最稳）
    try:
        js_obj = bframe.run_js(
            """
            const desc = document.querySelector('.rc-imageselect-desc, .rc-imageselect-instructions, .rc-imageselect-desc-wrapper');
            const st = document.querySelector('.rc-imageselect-desc strong, .rc-imageselect-instructions strong, strong');
            return {
              full: desc ? (desc.innerText || desc.textContent || '').trim() : '',
              strong: st ? (st.innerText || st.textContent || '').trim() : '',
              body: (document.body && document.body.innerText || '').trim().slice(0, 200)
            };
            """
        )
        if isinstance(js_obj, dict):
            if js_obj.get("strong"):
                strong_only = str(js_obj["strong"]).strip() or strong_only
                texts.append(strong_only)
            if js_obj.get("full"):
                texts.append(str(js_obj["full"]).strip())
            if js_obj.get("body") and not texts:
                texts.append(str(js_obj["body"]).strip())
        elif js_obj and str(js_obj).strip():
            texts.append(str(js_obj).strip())
    except Exception as e:
        log(f"instruction js failed: {e}", "WARN")

    texts = [t for t in texts if t]
    if not texts and not strong_only:
        return ""
    # 拼成「strong + 最长全文」，方便后面中英文解析
    full = max(texts, key=len) if texts else ""
    if strong_only and strong_only not in full:
        return f"{strong_only}\n{full}".strip()
    return full or strong_only


def read_target(page, bframe=None) -> str:
    """
    Read 'what to find' from challenge DOM.
    Requires being inside bframe; waits if caller did not pass a ready frame.
    """
    if bframe is None:
        bframe = wait_bframe_ready(page, timeout=15.0)
    if not bframe:
        log("read_target: no bframe (iframe not entered / not open)", "ERROR")
        return ""

    # small settle: text sometimes paints after table shell
    text = ""
    for attempt in range(1, 6):
        text = _extract_instruction_text(bframe)
        if text:
            break
        time.sleep(0.45)
        # re-acquire frame (some sites recreate iframe)
        bframe = find_frame(page, "bframe", timeout=2.0) or bframe
        log(f"instruction text empty, retry {attempt}/5")

    if not text:
        log("read_target: bframe ok but instruction text empty", "ERROR")
        return ""

    log(f"instruction raw: {text[:160]!r}")

    # --- 中文：请选择包含「桥」的所有图片 / strong 单独一词 ---
    for pat in _CN_SENTENCE_PATS:
        m = re.search(pat, text)
        if m:
            word = m.group(1).strip()
            # 去掉可能夹带的空白/换行
            word = re.sub(r"\s+", "", word)
            for cn, en in sorted(_CN_TO_EN.items(), key=lambda x: -len(x[0])):
                if cn in word or word == cn:
                    log(f"target CN sentence {cn!r} -> {en}")
                    return en
            # strong 可能就是「桥」本身
            if word in _CN_TO_EN:
                en = _CN_TO_EN[word]
                log(f"target CN word {word!r} -> {en}")
                return en

    # 长词优先扫全文（「桥」在 strong 里单独出现也能命中）
    for cn, en in sorted(_CN_TO_EN.items(), key=lambda x: -len(x[0])):
        if cn in text:
            log(f"target CN {cn!r} -> {en}")
            return en

    low = text.lower()
    for pat in _EN_PATS:
        m = re.search(pat, low, re.I)
        if m:
            phrase = m.group(1).strip(" .,;:\n\t")
            # 解决 DOM 文本合并导致的无空格连词问题，例如 "motorcyclesIf"
            phrase = re.split(r"(?i)(if\b|click\b|there\b|please\b|\n)", phrase)[0].strip()
            if phrase:
                log(f"target EN -> {phrase!r}")
                return phrase

    known = [
        "traffic lights",
        "traffic light",
        "crosswalks",
        "crosswalk",
        "motorcycles",
        "motorcycle",
        "bicycles",
        "bicycle",
        "hydrants",
        "hydrant",
        "bridges",
        "bridge",
        "buses",
        "bus",
        "cars",
        "car",
        "trucks",
        "truck",
        "boats",
        "boat",
        "stairs",
        "stair",
        "chimneys",
        "chimney",
        "palm trees",
        "palm tree",
        "parking meters",
        "parking meter",
        "tractors",
        "tractor",
    ]
    for k in sorted(known, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low):
            log(f"target token -> {k}")
            return k
    log(f"unparsed instruction: {text[:100]!r}", "WARN")
    return ""


def _looks_dynamic(bframe) -> bool:
    try:
        html = (bframe.html or "").lower()
        if "none left" in html or "click verify once there are none left" in html:
            return True
    except Exception:
        pass
    return False
