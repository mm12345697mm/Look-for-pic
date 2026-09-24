#!/usr/bin/env python3
"""Look-for-pic Web — Flask server with Gemini vision / OCR + metadata identify API."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import zipfile
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent
DEMO_PATH = ROOT / "data" / "demo-package.json"

DMM_PICS = "https://pics.dmm.co.jp/digital/video"
CDN_MEDIA_HOSTS = {"pics.dmm.co.jp", "pics.dmm.com"}
PREFIX_ONE_LABELS = {
    "nhdtc", "nhdtb", "nhdta", "nhdts", "nhdt",
    # SOD-style digital CIDs need leading "1" (curl-verified: without → now_printing)
    "sdmf", "stars", "sdde", "sdmm", "sdam", "start", "fsdss",
    # HAWA / DANDY(A) likewise need leading "1"
    "hawa", "dandy", "dandya",
}
PREFERRED_LABELS = {
    "MIDA", "SSNI", "SNIS", "STARS", "PRED", "MIDV", "MIDE",
    "JUFE", "SSIS", "SONE", "IPX", "IPZZ", "STARS", "ABF", "FSDSS",
}
AV_CODE_RE = re.compile(r"([A-Za-z]{2,10})[-－‐‑‒–—―ー−_\s／/]*(\d{2,5})", re.I)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 12
DDG_TIMEOUT = 7
VISUAL_COMPARE_BUDGET = 40
VISUAL_COMPARE_MAX = 8
SAME_SERIES_TITLE_SIM = 0.72
SAME_SERIES_SCORE_GAP = 0.08
COVER_DOWNLOAD_TIMEOUT = 3
TEXT_TIMEOUT = 20
VISION_TIMEOUT = 45
# Multi-image identify must return before gunicorn's worker wall (240s).
# 150s leaves room for one in-flight vision/catalog call, then partial slots.
MULTI_IDENTIFY_BUDGET_S = 150.0
# Do not start another slot when less than this remains — flush the response.
MULTI_SLOT_RESERVE_S = 4.0
# Do not start a shortened vision call. Below this, the frame is retryable.
MULTI_VISION_START_S = 15.0
# Related carousels for a large batch share this wall, not 8s × N.
MULTI_RELATED_BUDGET_S = 20.0
STREAM_KEEPALIVE_S = 5.0
# Soft deadline for online code→title attempts in identify_code
IDENTIFY_ONLINE_BUDGET = 20
GEMINI_MODELS = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
)
# Prefer only first two for text meta (faster; avoid long model cascades)
GEMINI_TEXT_MODELS = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
)
VISION_PROMPT = """你是 AV／JAV 封面與列表截圖辨識助手。先判斷畫面是哪一種，再讀字。回傳 JSON（不要 markdown、不要程式碼圍欄、不要多餘說明）：
{"code":"APGH-012","title":null,"actress":"...","studio":"...","shot":"cover","texts":["舌技が神","先生が2人っきりの","APGH-012"],"confidence":0.0,"notes":""}

shot 只能是 cover、listing、ui：
- cover：一整張封面或封套，畫面底下沒有網站標題列。
- listing：縮圖加上方或下方的標題列、多列列表、或格子。例如縮圖下「APGH-012 Yuuki Hiiragi」。
- ui：播放器、LIVE、時長、無碼影片這類介面，不是封面。

規則：
1. 只有 shot=cover 時，texts 才列出這張封面上的每一段字（番號、標題各行、女優、片商、角落小字、短宣傳句）。不要只留最大的那句。
2. shot 是 listing 或 ui 時，不要把整頁字倒進去。texts 只留焦點那一張縮圖的番號與主標題，不要鄰近列、聊天室、或其他卡片的字。
3. 番號優先於裝飾字。APGH-012、ApGH-012、APGH 012 寫進 code，並出現在 texts。寫成「英數-數字」。
4. 短直排或宣傳句（舌技、舌技が神）不是目錄片名。title 只填真正的作品標題那一行；沒有就 null。短標語可以留在 texts。
5. 時長（2:25:56）、無碼影片、有碼、中文字幕、LIVE、網站名不是 title。
6. 看不清楚填 null。不要翻譯、不要發明、不要補全看不到的字。confidence 為 0.0～1.0。actress 優先日文名。studio 沒有就 null。
7. 只輸出一行合法 JSON。
"""

VISUAL_MATCH_PROMPT = """你是 AV／JAV 視覺核對助手。Image A 是使用者上傳的「原始截圖／劇照／封面裁切」；Image B 是候選作品的封面或劇照。

任務：判斷 Image B 是否與 Image A 為「同一作品」的同一畫面／同一裝扮瞬間。同系列、同女優、標題相似都不足夠。
只回傳 JSON（不要 markdown、不要程式碼圍欄）：
{"same_work":true,"confidence":0.0,"reason":"簡短中文或日文理由","match_person":true,"match_face":true,"match_accessories":true,"match_clothes":true,"match_pose":true}

規則：
1. 必須以使用者原圖為準，逐項核對：人物（臉／身材特徵）、衣服顏色與款式、表情、飾品、姿勢／拍攝角度。
2. 衣服顏色或款式不同（例如白背心 vs 淺藍色上衣、正面封面 vs 背面劇照裝扮不同）→ match_clothes=false，same_work=false，confidence≤0.30。
3. 使用者圖是背面／側背／劇照裁切時：不可只因同系列封面「看起來像」就 same_work=true；必須與劇照裝扮／姿勢對得上。
4. 同系列換集／換女優／僅場景相似 → same_work=false，confidence≤0.30。
5. confidence：幾乎同一裁切 ≥0.85；僅同系列相似 ≤0.35。
6. 只輸出一行合法 JSON。
"""

VISUAL_RANK_BATCH_PROMPT = """你是 AV／JAV 視覺核對助手。第一張圖是使用者「原始上傳圖」（可能是劇照背面／側拍／封面裁切）；後面依序是候選 Cover0、Cover1、…（可能是封面或劇照）。

必須以使用者原圖為準交叉比對，不可只靠同系列／同女優／標題相似。優先核對：衣服顏色與款式、姿勢／角度、臉／表情、飾品；再考慮場景。
只回傳 JSON（不要 markdown）：
{"best_index":0,"rankings":[{"index":0,"same_work":true,"confidence":0.0,"match_person":true,"match_face":true,"match_accessories":true,"match_clothes":true,"match_pose":true,"reason":"..."}]}

規則：
1. rankings 必須涵蓋每一個 Cover index（0..N-1）。
2. 最多只能有 1 個 same_work=true；其餘 false。衣服顏色／款式對不上 → 不得 same_work=true。
3. 使用者圖是背面／劇照時：正面封面若衣服不同，即使同女優同系列也 same_work=false。
4. best_index 為最接近者；若無人衣服＋人物都對得上，全部 same_work=false 且 confidence≤0.35。
5. 同系列不同集／不同女優 → 絕對不要標 same_work。
6. confidence 要拉開差距（不要全部 1.0）。
"""


app = Flask(__name__, static_folder=None)

# Private site gate:
# - SITE_PASSWORD: shared guests must enter this on /login
# - OWNER_DEVICE_TOKEN: your phone opens /d/<token> once → long-lived cookie, no password after
# Set both (and SECRET_KEY) in Railway variables.
import secrets as _secrets
import hmac as _hmac
from flask import session as _session, redirect as _redirect, request as _request, make_response as _make_response

app.secret_key = (os.environ.get("SECRET_KEY") or os.environ.get("SITE_PASSWORD") or _secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 days for guest login
_OWNER_COOKIE = "lfp_owner"
_OWNER_COOKIE_MAX_AGE = 60 * 60 * 24 * 400  # ~13 months


def _site_password() -> str:
    return (os.environ.get("SITE_PASSWORD") or "").strip()


def _owner_device_token() -> str:
    return (os.environ.get("OWNER_DEVICE_TOKEN") or "").strip()


def _owner_cookie_value(token: str) -> str:
    # Do not store the raw unlock token in the cookie; store an HMAC mark.
    sk = str(app.secret_key)
    return _hmac.new(sk.encode("utf-8"), token.encode("utf-8"), "sha256").hexdigest()


def _is_owner_device() -> bool:
    tok = _owner_device_token()
    if not tok:
        return False
    got = (_request.cookies.get(_OWNER_COOKIE) or "").strip()
    if not got:
        return False
    return _hmac.compare_digest(got, _owner_cookie_value(tok))


def _presented_owner_token() -> str:
    header = (_request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (_request.headers.get("X-Owner-Token") or "").strip()


def _bearer_owner_token_ok() -> bool:
    expect = _owner_device_token()
    supplied = _presented_owner_token()
    if not expect or not supplied:
        return False
    return _hmac.compare_digest(supplied, expect)


def _can_read_identify_sessions() -> bool:
    """Owner cookie, owner token header, or a logged-in site session.

    These records map private screenshots to catalog titles. A request that
    merely reached an open local port is not enough once a token or password
    is configured.
    """
    if _is_owner_device() or _bearer_owner_token_ok():
        return True
    if _session.get("site_ok") is True and _site_password():
        return True
    if not _site_password() and not _owner_device_token():
        if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT")) and not os.environ.get("ALLOW_PUBLIC"):
            return False
        return True
    return False


def _is_authed() -> bool:
    if _is_owner_device() or _bearer_owner_token_ok():
        return True
    pw = _site_password()
    if not pw:
        # Fail closed on hosted environments unless explicitly public
        if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT")) and not os.environ.get("ALLOW_PUBLIC"):
            return False
        return True
    return _session.get("site_ok") is True


def _set_owner_cookie(resp):
    tok = _owner_device_token()
    if not tok:
        return resp
    resp.set_cookie(
        _OWNER_COOKIE,
        _owner_cookie_value(tok),
        max_age=_OWNER_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.before_request
def _require_site_password():
    if _request.endpoint in {"login", "logout", "healthz", "owner_unlock", "owner_unlock_query"}:
        return None
    path = (_request.path or "/")
    if path in {"/login", "/logout", "/api/health", "/healthz"}:
        return None
    if path.startswith("/d/"):
        return None
    if path.startswith("/icons/") or path in {"/manifest.webmanifest", "/favicon.ico"}:
        return None
    # One-shot query unlock: /?d=TOKEN
    q = (_request.args.get("d") or "").strip()
    if q and _owner_device_token() and _hmac.compare_digest(q, _owner_device_token()):
        resp = _redirect("/")
        return _set_owner_cookie(resp)
    if _is_authed():
        return None
    if path.startswith("/api/"):
        return jsonify({"ok": False, "message": "需要登入才能使用（私人站）"}), 401
    nxt = path if path != "/login" else "/"
    return _redirect("/login?next=" + nxt)


@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "private": bool(_site_password()),
        "owner_device": _is_owner_device(),
    })


IDENTIFY_SESSION_MAX = 80
_IDENTIFY_SESSIONS_PATH: Path | None = None
_IDENTIFY_SESSIONS_LOCK = threading.Lock()


def _identify_sessions_resolve_path() -> Path:
    """Prefer data/identify-sessions.json; fall back to /tmp if data/ is not writable."""
    global _IDENTIFY_SESSIONS_PATH
    if _IDENTIFY_SESSIONS_PATH is not None:
        return _IDENTIFY_SESSIONS_PATH
    preferred = ROOT / "data" / "identify-sessions.json"
    fallback = Path("/tmp/lfp-identify-sessions.json")
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        probe = preferred.parent / ".identify-sessions-writetest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        _IDENTIFY_SESSIONS_PATH = preferred
    except Exception:
        _IDENTIFY_SESSIONS_PATH = fallback
    return _IDENTIFY_SESSIONS_PATH


def _identify_sessions_read(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception:
        return []
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return []
    rows = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def _identify_sessions_write(path: Path, sessions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"sessions": sessions[:IDENTIFY_SESSION_MAX]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def identify_session_put(record: dict) -> dict | None:
    """Persist one identify session. Fail-soft: identify still succeeds if this cannot write."""
    if not isinstance(record, dict) or not record.get("id"):
        return None
    path = _identify_sessions_resolve_path()
    with _IDENTIFY_SESSIONS_LOCK:
        try:
            with open(path, "a+", encoding="utf-8") as lockf:
                try:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                sessions = _identify_sessions_read(path)
                sessions = [row for row in sessions if row.get("id") != record["id"]]
                sessions.insert(0, record)
                _identify_sessions_write(path, sessions[:IDENTIFY_SESSION_MAX])
            return record
        except Exception:
            return None


def identify_session_list(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), IDENTIFY_SESSION_MAX))
    path = _identify_sessions_resolve_path()
    with _IDENTIFY_SESSIONS_LOCK:
        rows = _identify_sessions_read(path)
    return [_identify_session_public(row, full=False) for row in rows[:limit]]


def identify_session_get(session_id: str) -> dict | None:
    sid = (session_id or "").strip()
    if not sid:
        return None
    path = _identify_sessions_resolve_path()
    with _IDENTIFY_SESSIONS_LOCK:
        for row in _identify_sessions_read(path):
            if row.get("id") == sid:
                return _identify_session_public(row, full=True)
    return None


def _identify_session_public(record: dict, *, full: bool) -> dict:
    frames_in = [f for f in (record.get("frames") or []) if isinstance(f, dict)]
    if full:
        frames = frames_in
    else:
        frames = [
            {
                "index": f.get("index"),
                "final_code": f.get("final_code"),
                "final_title": f.get("final_title"),
                "title_only": bool(f.get("title_only")),
                "unidentified": bool(f.get("unidentified")),
                "drop_reason": f.get("drop_reason"),
                "visual_lock": bool(f.get("visual_lock")),
            }
            for f in frames_in
        ]
    return {
        "id": record.get("id"),
        "ts": record.get("ts"),
        "created_at": record.get("created_at"),
        "image_count": record.get("image_count") or 0,
        "result_count": record.get("result_count") or 0,
        "banner": record.get("banner") or "",
        "message": record.get("message") or "",
        "summary": record.get("summary") or "",
        "frames": frames,
    }


def _new_session_id() -> str:
    return "ses_" + time.strftime("%Y%m%d%H%M%S") + "_" + _secrets.token_hex(4)


def _trace_title_only(slot: dict | None) -> bool:
    if not isinstance(slot, dict) or slot.get("unidentified"):
        return False
    if slot.get("needs_code"):
        return True
    code = str(slot.get("code") or "").strip()
    if not code or code == "TITLE-SEARCH" or not parse_code_parts(code):
        return bool(str(slot.get("title") or "").strip())
    return False


def _frame_trace(row: dict, slot: dict | None, *, slot_pos: int | None, drop_reason: str | None) -> dict:
    image = row.get("image_bytes")
    digest = image_content_hash(image) if image else None
    parsed = row.get("parsed_code") or row.get("code")
    parsed_s = ""
    if parsed and parse_code_parts(str(parsed)):
        parsed_s = format_display_code(str(parsed))
    final_code = ""
    if isinstance(slot, dict):
        raw_code = str(slot.get("code") or "").strip()
        if raw_code == "TITLE-SEARCH":
            final_code = "TITLE-SEARCH"
        elif raw_code and parse_code_parts(raw_code):
            final_code = format_display_code(raw_code)
        else:
            final_code = raw_code
    return {
        "index": row.get("index"),
        "filename": row.get("filename") or None,
        "fingerprint": digest,
        "preview_ref": ("sha256:" + digest) if digest else None,
        "vision_title": row.get("vision_title") or None,
        "vision_code": row.get("vision_code") or None,
        "ocr_title": row.get("ocr_title") or None,
        "parsed_code": parsed_s or None,
        "drop_reason": drop_reason,
        "final_slot": slot_pos,
        "final_code": final_code or None,
        "final_title": (str(slot.get("title")).strip() if isinstance(slot, dict) and slot.get("title") else None),
        "title_only": _trace_title_only(slot),
        "unidentified": bool(isinstance(slot, dict) and slot.get("unidentified")),
        "visual_lock": bool(isinstance(slot, dict) and slot.get("visual_lock")),
        "needs_code": bool(isinstance(slot, dict) and slot.get("needs_code")),
    }


def _build_identify_session(payload: dict, frames: list[dict]) -> dict:
    now = time.time()
    image_count = int(payload.get("image_count") or len(frames) or 0)
    result_count = int(payload.get("result_count") or len(payload.get("results") or []) or (1 if payload.get("ok") else 0))
    banner = str(payload.get("related_note") or payload.get("message") or "").strip()
    summary = f"{image_count} 張上傳 → {result_count} 部結果"
    return {
        "id": _new_session_id(),
        "ts": now,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "image_count": image_count,
        "result_count": result_count,
        "banner": banner,
        "message": str(payload.get("message") or "").strip(),
        "summary": summary,
        "frames": frames,
    }


def _attach_saved_session(payload: dict, frames: list[dict]) -> dict:
    if not isinstance(payload, dict):
        return payload
    try:
        record = _build_identify_session(payload, frames)
        saved = identify_session_put(record)
        if not saved:
            return payload
        payload["session_id"] = saved["id"]
        payload["identify_session"] = _identify_session_public(saved, full=True)
    except Exception:
        pass
    return payload


def _frames_from_multi(vision_rows: list[dict], results: list[dict]) -> list[dict]:
    """One trace row per upload, in upload order, after merge."""
    placed: dict[int, tuple[int, dict, str | None]] = {}
    for pos, slot in enumerate(results or [], start=1):
        if not isinstance(slot, dict):
            continue
        host = slot.get("from_image_index")
        indexes = []
        if host is not None:
            indexes.append(host)
        for extra in slot.get("merged_image_indexes") or []:
            if extra not in indexes:
                indexes.append(extra)
        for idx in indexes:
            try:
                key = int(idx)
            except (TypeError, ValueError):
                continue
            reason = None
            if host is not None and key != int(host) and key in {
                int(n) for n in (slot.get("merged_image_indexes") or []) if isinstance(n, int) or str(n).isdigit()
            }:
                reason = "merged_same_work"
            placed[key] = (pos, slot, reason)
    frames = []
    for row in vision_rows or []:
        try:
            key = int(row.get("index"))
        except (TypeError, ValueError):
            key = None
        hit = placed.get(key) if key is not None else None
        if hit is None:
            frames.append(_frame_trace(row, None, slot_pos=None, drop_reason="no_slot"))
        else:
            pos, slot, reason = hit
            frames.append(_frame_trace(row, slot, slot_pos=pos, drop_reason=reason))
    return frames


def _frames_from_single(payload: dict, images: list[tuple[bytes, str | None]]) -> list[dict]:
    blob = images[0][0] if images else None
    name = images[0][1] if images else None
    row = {
        "index": 1,
        "filename": name,
        "image_bytes": blob,
        "vision_title": payload.get("read_title") or None,
        "vision_code": payload.get("read_code") or None,
        "ocr_title": payload.get("read_ocr_title") or None,
        "parsed_code": payload.get("code"),
    }
    return [_frame_trace(row, payload, slot_pos=1 if payload.get("ok") else None, drop_reason=None)]


def _ensure_identify_session(payload: dict, images: list[tuple[bytes, str | None]]) -> dict:
    if not isinstance(payload, dict) or payload.get("session_id"):
        return payload
    image_count = len(images or [])
    payload.setdefault("image_count", image_count or (1 if payload.get("ok") else 0))
    if not payload.get("result_count"):
        payload["result_count"] = len(payload.get("results") or []) or (1 if payload.get("ok") else 0)
    return _attach_saved_session(payload, _frames_from_single(payload, images or []))


@app.get("/api/owner/identify-sessions")
def owner_identify_sessions():
    if not _can_read_identify_sessions():
        return jsonify({"ok": False, "message": "需要主人登入才能讀取辨識紀錄"}), 401
    try:
        limit = int(request.args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    return jsonify({"ok": True, "sessions": identify_session_list(limit)})


@app.get("/api/owner/identify-sessions/<session_id>")
def owner_identify_session_detail(session_id: str):
    if not _can_read_identify_sessions():
        return jsonify({"ok": False, "message": "需要主人登入才能讀取辨識紀錄"}), 401
    row = identify_session_get(session_id)
    if not row:
        return jsonify({"ok": False, "message": "找不到這筆辨識紀錄"}), 404
    return jsonify({"ok": True, "session": row})


@app.route("/d/<token>")
def owner_unlock(token: str):
    """Bookmark this URL on your phone once → later visits skip the share password."""
    expect = _owner_device_token()
    if not expect or not _hmac.compare_digest((token or "").strip(), expect):
        return _redirect("/login")
    resp = _redirect("/")
    _session["site_ok"] = True
    _session.permanent = True
    return _set_owner_cookie(resp)


@app.route("/login", methods=["GET", "POST"])
def login():
    # If this browser is already the owner device, skip the form
    if _is_owner_device():
        return _redirect("/")
    pw = _site_password()
    err = ""
    if _request.method == "POST":
        got = (_request.form.get("password") or "").strip()
        if pw and got == pw:
            _session["site_ok"] = True
            _session.permanent = True
            nxt = (_request.args.get("next") or _request.form.get("next") or "/").strip() or "/"
            if not nxt.startswith("/"):
                nxt = "/"
            return _redirect(nxt)
        err = "密碼錯誤"
    elif not pw:
        err = "主機尚未設定 SITE_PASSWORD（私人站無法開放）"
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="robots" content="noindex,nofollow"/>
<title>Look-for-pic 私人登入</title>
<style>
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b0b12;color:#f2f2f7;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.card{{width:100%;max-width:360px;background:#161622;border:1px solid #2a2a3a;border-radius:16px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.35)}}
h1{{font-size:1.15rem;margin:0 0 8px}}
p{{margin:0 0 16px;color:#a0a0b8;font-size:.92rem;line-height:1.45}}
input{{width:100%;box-sizing:border-box;padding:14px 12px;border-radius:12px;border:1px solid #3a3a50;background:#0f0f18;color:#fff;font-size:1rem;margin-bottom:12px}}
button{{width:100%;padding:14px;border:0;border-radius:12px;background:#7c5cff;color:#fff;font-weight:600;font-size:1rem}}
.err{{color:#ff8e8e;margin:0 0 12px;font-size:.9rem}}
</style>
</head>
<body>
<form class="card" method="post" action="/login">
<h1>私人站登入</h1>
<p>訪客請輸入分享密碼。你的手機可用專屬解鎖連結，之後免密。</p>
{"<p class=err>"+err+"</p>" if err else ""}
<input type="password" name="password" placeholder="分享密碼" autocomplete="current-password" required autofocus/>
<button type="submit">進入</button>
</form>
</body>
</html>"""
    resp = _make_response(html)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/logout")
def logout():
    _session.clear()
    resp = _redirect("/login")
    resp.set_cookie(_OWNER_COOKIE, "", max_age=0, path="/")
    return resp



_demo_cache: dict | None = None


def load_demo() -> dict:
    global _demo_cache
    if _demo_cache is not None:
        return _demo_cache
    if DEMO_PATH.is_file():
        with open(DEMO_PATH, encoding="utf-8") as f:
            _demo_cache = json.load(f)
    else:
        _demo_cache = {}
    return _demo_cache



def get_gemini_api_key() -> str:
    """Env first; else box-secrets card.GEMINI_API_KEY. Never log the key."""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    try:
        secrets_path = Path("/home/box/agent-data/box-secrets.json")
        if secrets_path.is_file():
            with open(secrets_path, encoding="utf-8") as f:
                data = json.load(f)
            card = data.get("card") if isinstance(data, dict) else None
            if isinstance(card, dict):
                key = str(card.get("GEMINI_API_KEY") or "").strip()
                if key:
                    return key
    except Exception:
        pass
    return ""


def is_usable_title(title: str | None) -> bool:
    """Title usable for search: len>=4 and mostly JP/CJK."""
    if not title:
        return False
    t = str(title).strip()
    if len(t) < 4:
        return False
    cjk = 0
    other = 0
    for c in t:
        o = ord(c)
        if c.isspace() or c in "　・…‥「」『』【】（）()[]【】!?！？ー−-—_./·":
            continue
        if (
            0x3040 <= o <= 0x30FF  # hiragana/katakana
            or 0x4E00 <= o <= 0x9FFF  # CJK
            or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF
            or 0xFF66 <= o <= 0xFF9D  # halfwidth kana
        ):
            cjk += 1
        else:
            other += 1
    if cjk < 2:
        return False
    total = cjk + other
    if total == 0:
        return False
    return (cjk / total) >= 0.4



def normalize_ocr_title(title: str | None) -> str:
    """Fix common OCR confusables so catalog title search can hit (〇≠○ etc.)."""
    t = (title or "").strip()
    if not t:
        return ""
    repl = {
        "〇": "○",  # U+3007 ideographic number zero → white circle
        "◯": "○",
        "●": "○",
        "◎": "○",
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "…": "",
        "⋯": "",
        "･･･": "",
        "...": "",
    }
    for a, b in repl.items():
        t = t.replace(a, b)
    # Vision often mixes Simplified 的 into JP titles (息子的 → 息子の)
    if "的" in t and re.search(r"[\u3040-\u30ff]", t):
        t = t.replace("的", "の")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _longest_common_substr_len(a: str, b: str) -> int:
    """Length of longest contiguous shared substring (O(n*m), titles are short)."""
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    best = 0
    # Bound work: titles rarely > 80 chars after normalize
    a = a[:96]
    b = b[:96]
    for i in range(len(a)):
        if best >= len(a) - i:
            break
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > best:
                best = k
    return best


def _title_cover_ratio(query: str | None, catalog: str | None) -> float:
    """How much of the longer string is the shorter one, when one contains the other.

    1.0 is an exact title. A shared series line plus a long extra slogan is lower
    than the same line plus only a short name.
    """
    q = re.sub(r"\s+", "", normalize_ocr_title(query) or (query or ""))
    c = re.sub(r"\s+", "", normalize_ocr_title(catalog) or (catalog or ""))
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        return len(q) / len(c)
    if c in q:
        return len(c) / len(q)
    return 0.0


def title_similarity(a: str | None, b: str | None) -> float:
    a = normalize_ocr_title(a) or (a or "").strip()
    b = normalize_ocr_title(b) or (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # A long query that is almost the whole catalog title is the same work.
        # A short hook (夜行バス) inside a different title is not.
        cover = _title_cover_ratio(a, b)
        short_len = min(
            len(re.sub(r"\s+", "", a)),
            len(re.sub(r"\s+", "", b)),
        )
        if short_len >= 12 and cover >= 0.8:
            return 0.92
        if short_len >= 12:
            return 0.5 + 0.4 * cover
        return 0.25 + 0.35 * cover
    # Long shared prefix (vision OCR often drifts only on the tail)
    n = 0
    lim = min(len(a), len(b))
    while n < lim and a[n] == b[n]:
        n += 1
    prefix = 0.0
    if n >= 8:
        prefix = 0.55 + 0.4 * (n / max(len(a), len(b), 1))
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    jacc = inter / union
    # Contiguous series template (e.g. NHDTC 声我慢SEX…中出し) beats char Jaccard
    lcs = _longest_common_substr_len(a, b)
    lcs_score = 0.0
    if lcs >= 10:
        lcs_score = 0.42 + 0.5 * (lcs / max(len(a), len(b), 1))
    elif lcs >= 7:
        lcs_score = 0.28 + 0.35 * (lcs / max(len(a), len(b), 1))
    return max(jacc, prefix, lcs_score)


def normalize_code(raw: str) -> str:
    s = (raw or "").strip().upper()
    for ch in "‐‑‒–—―ー−":
        s = s.replace(ch, "-")
    s = re.sub(r"\s+", "", s)
    s = s.replace("_", "-").replace("／", "-").replace("/", "-")
    s = s.replace("－", "-")
    return s


def parse_code_parts(code: str) -> tuple[str, str] | None:
    n = normalize_code(code)
    m = re.match(r"^([A-Z]{2,10})-?(\d{1,5})$", n)
    if not m:
        return None
    return m.group(1), m.group(2)


def format_display_code(code: str) -> str:
    parts = parse_code_parts(code)
    if not parts:
        return normalize_code(code)
    label, number = parts
    # Keep leading zeros in the numeric part (e.g. 029 → 029, not 29)
    return f"{label}-{number}"


def is_mida616(code: str | None) -> bool:
    """Demo-package sentinel (MIDA-616 / mida00616)."""
    if not code:
        return False
    parts = parse_code_parts(str(code))
    if not parts:
        return False
    try:
        return parts[0] == "MIDA" and int(parts[1]) == 616
    except ValueError:
        return False


def codes_numeric_equal(a: str | None, b: str | None) -> bool:
    """True if both parse as the same label + integer (NHDTC-99 == NHDTC-099)."""
    if not a or not b:
        return False
    pa, pb = parse_code_parts(str(a)), parse_code_parts(str(b))
    if not pa or not pb:
        return format_display_code(str(a)) == format_display_code(str(b))
    try:
        return pa[0] == pb[0] and int(pa[1] or 0) == int(pb[1] or 0)
    except ValueError:
        return pa[0] == pb[0] and pa[1] == pb[1]


def prefer_display_code(query: str, catalog: str | None = None) -> str:
    """Keep leading zeros: when numeric-equal, prefer the longer digit form (008 > 8)."""
    q = format_display_code(query)
    if not catalog:
        return q
    c = format_display_code(str(catalog))
    if not codes_numeric_equal(q, c):
        return q
    pq, pc = parse_code_parts(q), parse_code_parts(c)
    if not pq:
        return c
    if not pc:
        return q
    if len(pc[1]) > len(pq[1]):
        return c
    return q


def code_lookup_slugs(code: str, *, limit: int = 12) -> list[str]:
    """
    Catalog/URL slug variants for a 品番.
    Display form (with its zeros) is first; also try stripped zeros and common pads.
    Example: DOSD-008 → DOSD-008, DOSD-8, DOSD-00008, …
    """
    parts = parse_code_parts(code)
    display = format_display_code(code) if parts else normalize_code(code)
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    add(display)
    add(display.lower())
    add(display.replace("-", ""))
    add(display.replace("-", "").lower())
    if parts:
        lab, num = parts
        stripped = num.lstrip("0") or "0"
        # Catalog pages often omit leading zeros (DOSD-008 → DOSD-8)
        if stripped != num:
            add(f"{lab}-{stripped}")
            add(f"{lab}{stripped}")
            add(f"{lab}-{stripped}".lower())
            add(f"{lab}{stripped}".lower())
        for width in (3, 4, 5):
            padded = stripped.zfill(width) if len(stripped) <= width else stripped
            add(f"{lab}-{padded}")
            add(f"{lab}{padded}")
            add(f"{lab}-{padded}".lower())
            if len(out) >= limit:
                break
    return out[:limit]


def code_stripped_form(code: str) -> str | None:
    """DOSD-008 → DOSD-8; None if already unpadded."""
    parts = parse_code_parts(code)
    if not parts:
        return None
    lab, num = parts
    stripped = num.lstrip("0") or "0"
    if stripped == num:
        return None
    return f"{lab}-{stripped}"


def code_to_cid(code: str) -> str | None:
    parts = parse_code_parts(code)
    if not parts:
        return None
    label, number = parts
    label_l = label.lower()
    num = number.zfill(5)
    if label_l in PREFIX_ONE_LABELS:
        return f"1{label_l}{num}"
    return f"{label_l}{num}"


def cover_url(cid: str) -> str:
    return f"{DMM_PICS}/{cid}/{cid}pl.jpg"


def still_urls(cid: str, count: int = 10) -> list[str]:
    return [f"{DMM_PICS}/{cid}/{cid}jp-{i}.jpg" for i in range(1, count + 1)]


COVER_PROBE_TIMEOUT = 4.0
TITLE_CODE_MATCH_MIN = 0.45


def is_now_printing_url(url: str | None) -> bool:
    """True if URL is DMM's placeholder / missing-cover image."""
    s = (url or "").lower()
    return "now_printing" in s or "/noimage/" in s


def usable_cover_url(url: object) -> bool:
    """True if cover is an http(s) URL and not DMM's now_printing placeholder."""
    s = str(url or "").strip()
    if not (s.lower().startswith("http://") or s.lower().startswith("https://")):
        return False
    if is_now_printing_url(s):
        return False
    return True


def probe_cover_url(url: str, timeout: float = COVER_PROBE_TIMEOUT) -> tuple[bool, str | None]:
    """
    HEAD/GET a cover URL following redirects.
    Returns (ok, final_url). Rejects 404 and now_printing.
    """
    u = (url or "").strip()
    if not u.startswith("http"):
        return False, None
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.dmm.co.jp/",
        "Accept": "image/*,*/*;q=0.8",
    }
    try:
        r = requests.head(
            u,
            timeout=(2, timeout),
            headers=headers,
            allow_redirects=True,
            verify=False,
        )
        final = str(r.url or u)
        if is_now_printing_url(final):
            return False, final
        if r.status_code in (403, 405) or (r.status_code >= 400 and r.status_code != 404):
            r = requests.get(
                u,
                timeout=(2, timeout),
                headers=headers,
                allow_redirects=True,
                verify=False,
                stream=True,
            )
            final = str(r.url or u)
            try:
                next(r.iter_content(256), b"")
            except Exception:
                pass
            try:
                r.close()
            except Exception:
                pass
        if r.status_code >= 400:
            return False, final
        if is_now_printing_url(final):
            return False, final
        return True, final
    except Exception:
        return False, None


def cover_cid_candidates(code: str) -> list[str]:
    """Ordered CID guesses for a product code (deduped)."""
    parts = parse_code_parts(code)
    if not parts:
        return []
    label, number = parts
    label_l = label.lower()
    stripped = number.lstrip("0") or "0"
    pads: list[str] = []
    seen_p: set[str] = set()
    # Include original digits (008), common DMM pads, and stripped (8 / 08)
    for raw in (number, stripped):
        if raw not in seen_p:
            seen_p.add(raw)
            pads.append(raw)
        for w in (5, 4, 3, 2):
            p = raw.zfill(w) if len(raw) <= w else raw
            if p not in seen_p:
                seen_p.add(p)
                pads.append(p)
    out: list[str] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        if c and c not in seen:
            seen.add(c)
            out.append(c)

    primary = code_to_cid(code)
    if primary:
        add(primary)
    for p in pads:
        add(f"1{label_l}{p}")
        add(f"{label_l}{p}")
    return out


def resolve_cover_cid(code: str) -> tuple[str | None, str | None]:
    """
    Try CID candidates until a real (non-now_printing) DMM cover is found.
    Returns (cid, cover_url) or (None, None).
    """
    for cid in cover_cid_candidates(code):
        url = cover_url(cid)
        ok, final = probe_cover_url(url)
        if ok and not is_now_printing_url(final):
            return cid, url
    return None, None


def sanitize_cover_fields(
    code: str | None = None,
    cid: str | None = None,
    cover: str | None = None,
) -> tuple[str | None, str | None, list[str]]:
    """Ensure cover is a working CDN URL (never now_printing). Returns (cid, cover, stills)."""
    cover_s = (str(cover).strip() if cover else "") or None
    cid_s = (str(cid).strip() if cid else "") or None

    if cover_s and is_now_printing_url(cover_s):
        cover_s = None

    if cover_s:
        ok, final = probe_cover_url(cover_s)
        if not ok or is_now_printing_url(final):
            cover_s = None

    if not cover_s and code and parse_code_parts(str(code)):
        rcid, rcover = resolve_cover_cid(str(code))
        if rcid and rcover:
            return rcid, rcover, still_urls(rcid, 10)

    if not cover_s and cid_s:
        url = cover_url(cid_s)
        ok, final = probe_cover_url(url)
        if ok and not is_now_printing_url(final):
            return cid_s, url, still_urls(cid_s, 10)
        if code and parse_code_parts(str(code)):
            rcid, rcover = resolve_cover_cid(str(code))
            if rcid and rcover:
                return rcid, rcover, still_urls(rcid, 10)
        return None, None, []

    if cover_s and cid_s:
        return cid_s, cover_s, still_urls(cid_s, 10)
    if cover_s:
        return cid_s, cover_s, []
    return cid_s, None, []


def fetch_catalog_title_for_code(code: str) -> dict | None:
    """Lightweight code→official title for cross-check.

    Avbase is tried first; its own fallback is the javbus work page. Missing
    helper names must not raise — a thrown lookup used to reject a printed 品番.
    """
    display = format_display_code(code)
    if not display:
        return None
    for fetcher in (fetch_avbase_by_code, _fetch_javbus_by_code):
        try:
            meta = fetcher(display)
        except Exception:
            meta = None
        if isinstance(meta, dict) and (meta.get("title") or "").strip():
            return meta
    return None


def verify_code_matches_title(
    code: str,
    reference_title: str | None,
    *,
    min_sim: float = TITLE_CODE_MATCH_MIN,
) -> dict:
    """
    Cross-check a candidate 番號 against an on-screen / user title.
    Rejects low title similarity OR missing/now_printing cover.
    """
    display = format_display_code(code)
    ref = (reference_title or "").strip()
    cid, cover = resolve_cover_cid(display)
    cover_ok = bool(cid and cover)
    catalog = fetch_catalog_title_for_code(display)
    catalog_title = (catalog.get("title") if catalog else None) or None
    sim = title_similarity(ref, catalog_title) if (ref and catalog_title) else 0.0
    title_ok = True
    if is_usable_title(ref):
        if catalog_title:
            title_ok = sim >= min_sim
        else:
            # Have a readable title but no catalog title for this code → do not trust
            title_ok = False
            sim = 0.0
    ok = bool(title_ok and cover_ok)
    reason_bits: list[str] = []
    if not cover_ok:
        reason_bits.append("封面無效／now_printing")
    if is_usable_title(ref) and not title_ok:
        reason_bits.append(f"片名不符(sim={sim:.2f})")
    return {
        "ok": ok,
        "code": display,
        "similarity": sim,
        "catalog_title": catalog_title,
        "reference_title": ref or None,
        "cid": cid,
        "cover": cover,
        "cover_ok": cover_ok,
        "title_ok": title_ok,
        "source": (catalog or {}).get("source") if catalog else None,
        "reason": "；".join(reason_bits) if reason_bits else "ok",
    }


def extract_codes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in AV_CODE_RE.finditer(text or ""):
        code = normalize_code(f"{m.group(1)}-{m.group(2)}")
        if not parse_code_parts(code):
            continue
        if code in seen:
            continue
        seen.add(code)
        found.append(code)
    return found


def pick_best_code(codes: list[str]) -> str | None:
    if not codes:
        return None
    if len(codes) == 1:
        return codes[0]

    def score(c: str) -> int:
        parts = parse_code_parts(c)
        if not parts:
            return 0
        label, number = parts
        s = len(label)
        if len(number) >= 3:
            s += 2
        if label.upper() in PREFERRED_LABELS:
            s += 5
        return s

    return max(codes, key=score)


def run_tesseract(image_path: str, lang: str = "jpn+eng") -> str:
    try:
        r = subprocess.run(
            ["tesseract", image_path, "stdout", "-l", lang, "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return (r.stdout or "") + ("\n" + r.stderr if r.returncode and r.stderr else "")
    except Exception as e:
        return f"[tesseract error: {e}]"


def preprocess_image(src: Path, dest: Path) -> None:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if img.mode != "L":
        img = img.convert("L")
    img = ImageOps.autocontrast(img)
    img = ImageEnhance.Contrast(img).enhance(1.6)
    # Upscale small images for OCR
    w, h = img.size
    if max(w, h) < 1200:
        scale = 1200 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    img.save(dest, format="PNG")


def ocr_image_bytes(image_bytes: bytes) -> str:
    texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lfpic-ocr-") as td:
        td_path = Path(td)
        raw_path = td_path / "upload.bin"
        raw_path.write_bytes(image_bytes)
        # JPEG q95 keeps caption bars that a PNG rewrite drops (座席の隙間).
        try:
            img = Image.open(raw_path)
            img = ImageOps.exif_transpose(img).convert("RGB")
            png_path = td_path / "orig.png"
            img.save(png_path, format="PNG")
            jpg_path = td_path / "orig.jpg"
            img.save(jpg_path, format="JPEG", quality=95)
        except Exception:
            png_path = raw_path
            jpg_path = raw_path
            img = None

        t1 = run_tesseract(str(jpg_path))
        texts.append(t1)
        texts.append(run_tesseract(str(png_path)))

        try:
            prep = td_path / "prep.png"
            preprocess_image(png_path if png_path != raw_path else jpg_path, prep)
            texts.append(run_tesseract(str(prep)))
        except Exception:
            pass

        # Caption bars sit on a dark strip. One bottom band and one lower-middle
        # band catch overlay titles without dumping a grid of neighbor lines.
        if img is not None:
            w, h = img.size
            bands = (
                (int(h * 0.78), h),
                (int(h * 0.55), int(h * 0.88)),
            )
            for i, (y0, y1) in enumerate(bands):
                if y1 - y0 < 12:
                    continue
                try:
                    band = img.crop((0, y0, w, y1))
                    band = band.resize(
                        (max(8, band.width * 2), max(8, band.height * 2)),
                        Image.Resampling.LANCZOS,
                    )
                    band = ImageOps.autocontrast(band)
                    band_path = td_path / f"band{i}.jpg"
                    band.save(band_path, format="JPEG", quality=95)
                    texts.append(run_tesseract(str(band_path)))
                except Exception:
                    continue
            # A small 品番 in the bottom margin is missed by the full frame.
            # One high-contrast strip is enough; jacket match drops a misread.
            if not _trusted_ocr_codes("\n".join(texts)):
                try:
                    strip = img.crop((0, int(h * 0.58), w, h))
                    strip = ImageOps.autocontrast(strip)
                    strip = ImageEnhance.Contrast(strip).enhance(1.8)
                    strip = strip.resize(
                        (max(8, strip.width * 3), max(8, strip.height * 3)),
                        Image.Resampling.LANCZOS,
                    )
                    strip_path = td_path / "bottom.png"
                    strip.save(strip_path, format="PNG")
                    texts.append(run_tesseract(str(strip_path), lang="eng"))
                except Exception:
                    pass

    joined = "\n".join(t for t in texts if t and not str(t).startswith("[tesseract"))
    # Scene-text reader is optional. Its lines are not the title; they only
    # feed particle repairs (先生2人 → 先生が2人) when tesseract missed them.
    rapid = _rapidocr_lines(image_bytes)
    # Scene-text lines stay in the read. A vertical title is often one
    # phrase per line there, which a prefix search can still match.
    repairs = _ocr_title_repairs(joined + "\n" + rapid)
    if rapid:
        joined = joined + "\n" + rapid
    if repairs:
        joined = joined + "\n" + "\n".join(repairs)
    # Prefer the text that yields more AV codes, but keep every line so a
    # later phrase query can still see 座席の隙間 beside a longer noisy line.
    best = ""
    best_n = -1
    for t in texts:
        n = len(extract_codes(t))
        if n > best_n or (n == best_n and len(t) > len(best)):
            best = t
            best_n = n
    if best_n > 0 and best and best not in joined:
        joined = best + "\n" + joined
    return joined


_RAPID_OCR = None


def _rapidocr_lines(image_bytes: bytes) -> str:
    """Optional second reader. Missing package → empty. Never raises."""
    global _RAPID_OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""
    try:
        if _RAPID_OCR is None:
            _RAPID_OCR = RapidOCR()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            path = tmp.name
        try:
            result, _elapse = _RAPID_OCR(path)
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        lines = []
        for row in result or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                lines.append(str(row[1] or "").strip())
        return "\n".join(x for x in lines if x)
    except Exception:
        return ""


def _title_is_search_ready(text: str | None) -> bool:
    """A readable title long enough to search as itself, without slicing."""
    t = re.sub(r"\s+", "", str(text or ""))
    return _ocr_line_is_clean(t) and len(t) >= 12


def _ocr_line_is_clean(text: str | None) -> bool:
    """True when a line is mostly Japanese, not an OCR garbage mix."""
    t = re.sub(r"\s+", "", str(text or ""))
    if re.search(r"[A-Za-z@#]", t):
        return False
    cjk = _cjk_count(t)
    if cjk < 4:
        return False
    return cjk >= len(t) * 0.7


def _focused_cjk_queries(blob: str | None) -> list[str]:
    """A few readable phrases from one frame, not every line on a page."""
    raw = str(blob or "")
    if not raw or raw.startswith("[tesseract"):
        return []
    found = re.findall(r"[\u3040-\u30ff\u4e00-\u9fff0-9]{4,18}", raw)
    uniq = list(dict.fromkeys(found))

    def _rank(token: str) -> tuple:
        kana = len(re.findall(r"[\u3040-\u30ff]", token))
        return (-(1 if kana else 0), -len(token))

    uniq.sort(key=_rank)
    return uniq[:4]


def _ocr_line_prefixes(blob: str | None) -> list[str]:
    """Short prefixes of OCR lines, for a title the catalog still recognizes.

    A full noisy line often misses. Its 4–6 character head can still be
    the shared series title. Longer lines first. This is not a product-code list.
    """
    raw = str(blob or "")
    if not raw or raw.startswith("[tesseract"):
        return []
    lines: list[str] = []
    seen_line: set[str] = set()
    for piece in re.split(r"[\r\n]+", raw):
        # A short source line is one phrase. A long tesseract smear's
        # head is not a catalog query, so it stays out of this list.
        # A line that is mostly symbols or latin is the same kind of smear.
        source = piece.strip()
        if not source or len(source) > 18:
            continue
        nospace = re.sub(r"\s+", "", source)
        cjk_n = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", nospace))
        if not nospace or cjk_n < 4 or cjk_n / len(nospace) < 0.75:
            continue
        compact = re.sub(r"[^\u3040-\u30ff\u4e00-\u9fff]", "", source)
        if len(compact) < 4 or compact in seen_line:
            continue
        if not re.search(r"[\u4e00-\u9fff]", compact):
            continue
        seen_line.add(compact)
        lines.append(compact)
    # Phrase-length lines first. A smear that survived the length cap is
    # usually longer than the scene-text line it was copied from.
    lines.sort(key=lambda s: (0 if 4 <= len(s) <= 14 else 1, abs(len(s) - 8), -len(s)))
    out: list[str] = []
    for line in lines[:6]:
        for n in (4, 6, 8):
            if len(line) < n:
                continue
            pref = line[:n]
            if pref not in out:
                out.append(pref)
        if len(line) <= 10 and line not in out:
            out.append(line)
        if len(out) >= 8:
            break
    return out[:8]


_DASH_OCR_CODE_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{2,10})[-－‐‑‒–—―ー−](\d{2,5})(?!\d)"
)
_SPACE_OCR_CODE_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{3,10})[ ](\d{3,5})(?!\d)"
)


def _trusted_ocr_codes(text: str | None) -> list[str]:
    """品番 actually printed as a code, not a latin-digit accident.

    APGH-012 and APGH 012 count. "rake 12", "yr 33", and "shat 676" do not:
    those are OCR noise, and they must not become the frame's 番號.
    """
    raw = str(text or "")
    found: list[str] = []
    seen: set[str] = set()
    for rx in (_DASH_OCR_CODE_RE, _SPACE_OCR_CODE_RE):
        for m in rx.finditer(raw):
            code = normalize_code(f"{m.group(1)}-{m.group(2)}")
            disp = format_display_code(code) if parse_code_parts(code) else ""
            if not disp or disp in seen:
                continue
            seen.add(disp)
            found.append(disp)
    return found


def _sole_trusted_ocr_code(text: str | None) -> tuple[str | None, list[str]]:
    found = _trusted_ocr_codes(text)
    if len(found) == 1:
        return found[0], found
    return None, found


_GLUED_OCR_CODE_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{2,10})[-－‐‑‒–—―ー−](\d{3,8})"
)

# Characters these covers' fonts swap under OCR. Not a product-code list.
_OCR_CONFUSION = {
    "U": "LVJI",
    "L": "UVIJ",
    "J": "ILT",
    "I": "JL1",
    "O": "Q0D",
    "Q": "O0D",
    "D": "O0",
    "V": "UY",
    "S": "5",
    "B": "83",
    "Z": "2",
    "G": "6C",
    "C": "G",
    "T": "J",
    "H": "N",
    "N": "H",
    "0": "8O",
    "1": "47",
    "4": "1",
    "5": "6",
    "6": "580",
    "8": "603B",
    "2": "7",
    "3": "8",
    "7": "1",
    "9": "0",
}


def _ocr_code_candidates(text: str | None) -> list[str]:
    """Printed-looking 品番, including a code glued to the next number.

    ABCD-100240 is the code plus a runtime, not a longer 品番. A misread
    that is only "rake 12" never enters this list.
    """
    raw = str(text or "")
    found: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        disp = format_display_code(code) if parse_code_parts(code) else ""
        if not disp or disp in seen:
            return
        seen.add(disp)
        found.append(disp)

    for code in _trusted_ocr_codes(raw):
        add(code)
    for m in _GLUED_OCR_CODE_RE.finditer(raw):
        label, digits = m.group(1), m.group(2)
        if len(digits) <= 5:
            continue
        for n in (3, 4, 5):
            if len(digits) >= n:
                add(f"{label}-{digits[:n]}")
    return found


def _confusion_variants(code: str, limit: int = 24) -> list[str]:
    """One-character and one-letter-plus-one-digit OCR neighbors."""
    parts = parse_code_parts(code)
    if not parts:
        return []
    label, number = parts
    chars = list(label.upper() + number)
    nlab = len(label)
    out: list[str] = []
    seen: set[str] = set()

    def push(nxt: list[str]) -> None:
        if len(out) >= limit:
            return
        disp = format_display_code("".join(nxt[:nlab]) + "-" + "".join(nxt[nlab:]))
        if not disp or disp in seen or disp == format_display_code(code):
            return
        if not parse_code_parts(disp):
            return
        seen.add(disp)
        out.append(disp)

    singles: list[list[str]] = []
    for i, ch in enumerate(chars):
        for rep in _OCR_CONFUSION.get(ch, ""):
            if i < nlab and not rep.isalpha():
                continue
            if i >= nlab and not rep.isdigit():
                continue
            nxt = chars[:]
            nxt[i] = rep
            singles.append(nxt)
    # A misread often changes one letter and one digit together. Those
    # neighbors go first so the jacket probe is not spent on the rest.
    letter_idx = list(range(nlab))
    digit_idx = list(range(nlab, len(chars)))
    priority: list[list[str]] = []
    rest: list[list[str]] = []
    for i in letter_idx:
        letter_alts = [a for a in _OCR_CONFUSION.get(chars[i], "") if a.isalpha()]
        for j in digit_idx:
            digit_alts = [b for b in _OCR_CONFUSION.get(chars[j], "") if b.isdigit()]
            if letter_alts and digit_alts:
                nxt = chars[:]
                nxt[i] = letter_alts[0]
                nxt[j] = digit_alts[0]
                priority.append(nxt)
            for a in letter_alts:
                for b in digit_alts:
                    nxt = chars[:]
                    nxt[i] = a
                    nxt[j] = b
                    rest.append(nxt)
    digit_singles = [nxt for nxt in singles if any(nxt[i] != chars[i] for i in range(nlab, len(chars)))]
    letter_singles = [nxt for nxt in singles if nxt not in digit_singles]
    # One-character misreads first (6/8, O/D). Two-character neighbors after,
    # so the jacket probe is not spent before the single-character fix.
    for nxt in digit_singles + letter_singles + priority + rest:
        push(nxt)
        if len(out) >= limit:
            break
    return out


def _pick_code_by_jacket(
    codes: list[str] | None,
    image_bytes: bytes | None,
    *,
    limit: int = 8,
) -> tuple[str | None, float | None, bool]:
    """Highest jacket score among codes. None when nothing was compared or none locks."""
    scored: list[tuple[float, str]] = []
    compared = False
    for code in list(codes or [])[:limit]:
        try:
            _cid, url = resolve_cover_cid(code)
        except Exception:
            url = None
        if not url:
            continue
        score = _jacket_score_against_url(image_bytes, url)
        if score is None:
            continue
        compared = True
        if float(score) >= 0.85:
            return code, float(score), True
        scored.append((float(score), code))
    if not scored:
        return None, None, compared
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_s, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_s >= 0.70 and (len(scored) == 1 or best_s - second >= 0.12):
        return best, best_s, True
    return None, best_s, True


def _resolve_printed_code(
    text: str | None,
    image_bytes: bytes | None,
) -> tuple[str | None, float | None, bool, bool]:
    """Pick the 品番 whose jacket is this picture.

    Returns (code, score, had_candidates, compared). had_candidates is False
    when the text had nothing that looked like a printed code, so the caller
    must not throw away a code that came from somewhere else. compared is
    False when no cover could be scored.
    """
    cands = _ocr_code_candidates(text)
    if not cands or not image_bytes:
        return None, None, bool(cands), False
    picked, score, compared = _pick_code_by_jacket(cands, image_bytes, limit=6)
    if picked:
        return picked, score, True, True
    # A misread often has no catalog row. Try nearby characters before
    # keeping that token, and before giving up on the printed code.
    variants: list[str] = []
    for code in cands[:2]:
        for variant in _confusion_variants(code):
            if variant not in cands and variant not in variants:
                variants.append(variant)
            if len(variants) >= 24:
                break
    picked, score, var_compared = _pick_code_by_jacket(variants, image_bytes, limit=20)
    if picked:
        return picked, score, True, True
    if not compared and not var_compared:
        if len(cands) == 1:
            return cands[0], None, True, False
        return None, None, True, False
    return None, None, True, True


def _ocr_title_repairs(text: str) -> list[str]:
    """Particle and fragment repairs. These are search phrases, not codes.

    Tesseract/scene OCR drops が between 先生 and 2人, and splits 隙間手コキ.
    The repaired phrase is searched; a series of hits still has to lock to
    the uploaded picture before a 品番 is kept.
    """
    raw = str(text or "")
    if not raw or raw.startswith("[tesseract"):
        return []
    compact = re.sub(r"[\s　]+", "", raw)
    out: list[str] = []

    def add(q: str) -> None:
        q = (q or "").strip()
        if len(q) < 4 or q in out:
            return
        out.append(q)

    if "先生2人" in compact or "先生が2人" in compact or re.search(r"先生\s*2\s*人", raw):
        add("先生が2人")
    if "隙間" in compact and "手" in compact:
        add("座席の隙間")
        add("隙間手コキ")
    if "隠れ" in compact and "巨乳" in compact:
        add("隠れ巨乳な彼女")
    if ("地味" in compact) and ("眼鏡" in compact or "メガネ" in compact) and ("隠し切れ" in compact):
        add("地味な眼鏡では隠し切れない")
    return out


def ocr_image(file_storage) -> str:
    return ocr_image_bytes(file_storage.read())


def detect_image_mime(image_bytes: bytes, filename: str | None = None) -> str:
    name = (filename or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def maybe_downscale_for_vision(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Keep Gemini payload reasonable; return (bytes, mime)."""
    try:
        from io import BytesIO

        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        max_side = max(w, h)
        if max_side <= 2048 and len(image_bytes) <= 4_000_000:
            return image_bytes, mime_type
        scale = min(1.0, 2048 / max_side)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        buf = BytesIO()
        out_mime = "image/jpeg"
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), out_mime
    except Exception:
        return image_bytes, mime_type


def strip_json_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def parse_vision_json(text: str) -> dict[str, Any]:
    s = strip_json_fences(text)
    # Try direct parse, then first {...} block
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError("vision response is not JSON")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("vision JSON root must be object")

    def clean_str(v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            t = v.strip()
            if not t or t.lower() in ("null", "none", "n/a", "不明", "なし"):
                return None
            return t
        return str(v).strip() or None

    code = clean_str(data.get("code"))
    if code:
        # Normalize common fullwidth / spacing
        codes = extract_codes(code)
        code = format_display_code(codes[0]) if codes else format_display_code(code)

    def _text_items(value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        elif isinstance(value, str):
            raw_items = re.split(r"[\r\n]+", value)
        else:
            raw_items = []
        out: list[str] = []
        seen: set[str] = set()
        for raw in raw_items:
            item = clean_str(raw)
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    texts = _text_items(data.get("texts"))
    for extra in (
        clean_str(data.get("title")),
        clean_str(data.get("actress")),
        clean_str(data.get("studio")),
        clean_str(data.get("notes")),
        code,
    ):
        if extra and extra not in texts:
            texts.append(extra)
    # A corner stamp may be only in texts, while title was filled with a slogan.
    if not code:
        sole, _many = _sole_product_code("\n".join(texts))
        if sole:
            code = sole

    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        confidence = None

    shot = (clean_str(data.get("shot")) or "").lower()
    if shot not in {"cover", "listing", "ui"}:
        shot = ""

    return {
        "code": code,
        "title": clean_str(data.get("title")),
        "actress": clean_str(data.get("actress")),
        "studio": clean_str(data.get("studio")),
        "shot": shot,
        "texts": texts[:24],
        "confidence": confidence,
        "notes": clean_str(data.get("notes")) or "",
    }


def gemini_extract_text(resp_json: dict) -> str:
    cands = resp_json.get("candidates") or []
    if not cands:
        feedback = resp_json.get("promptFeedback") or resp_json.get("error") or resp_json
        raise RuntimeError(f"Gemini 無 candidates：{feedback}")
    parts = (((cands[0] or {}).get("content") or {}).get("parts")) or []
    texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
    text = "\n".join(t for t in texts if t).strip()
    if not text:
        raise RuntimeError("Gemini 回傳空文字")
    return text


def call_gemini_vision(
    image_bytes: bytes,
    mime_type: str,
    api_key: str,
    *,
    timeout: float | None = None,
    max_models: int | None = None,
) -> dict[str, Any]:
    image_bytes, mime_type = maybe_downscale_for_vision(image_bytes, mime_type)
    # A batch deadline must not shrink this call. A short read can invent a
    # volume; the multi pipeline skips the frame instead.
    limit = float(VISION_TIMEOUT if timeout is None else timeout)
    if limit < 2.0:
        raise RuntimeError("vision budget too small")
    models = GEMINI_MODELS if not max_models else GEMINI_MODELS[: max(1, int(max_models))]
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload_base = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": VISION_PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
        },
    }
    last_err: Exception | None = None
    for model in models:
        # Never log api_key; keep it only in the request URL query.
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            r = requests.post(url, json=payload_base, timeout=limit)
            if r.status_code >= 400:
                body = (r.text or "")[:300]
                # Continue on unavailable / rate-limit / overload / not-found
                if r.status_code in (400, 404, 429, 503) or "not found" in body.lower() or "overloaded" in body.lower():
                    last_err = RuntimeError(f"model {model} HTTP {r.status_code}: {body}")
                    continue
                last_err = RuntimeError(f"Gemini HTTP {r.status_code}: {body}")
                continue
            text = gemini_extract_text(r.json())
            parsed = parse_vision_json(text)
            parsed["_model"] = model
            return parsed
        except requests.Timeout as e:
            last_err = RuntimeError(f"model {model} 逾時（~{limit:.0f}s）")
            continue
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(str(last_err) if last_err else "Gemini 模型皆不可用")


def call_gemini_text(prompt: str, api_key: str, *, max_tokens: int = 1024) -> str:
    """Text-only Gemini generateContent with short timeout + 2-model fallback. Never logs the key."""
    payload_base = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_tokens,
        },
    }
    last_err: Exception | None = None
    for model in GEMINI_TEXT_MODELS:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            r = requests.post(url, json=payload_base, timeout=TEXT_TIMEOUT)
            if r.status_code >= 400:
                body = (r.text or "")[:300]
                if r.status_code in (400, 404, 429, 503) or "not found" in body.lower() or "overloaded" in body.lower():
                    last_err = RuntimeError(f"model {model} HTTP {r.status_code}: {body}")
                    continue
                last_err = RuntimeError(f"Gemini HTTP {r.status_code}: {body}")
                continue
            return gemini_extract_text(r.json())
        except requests.Timeout:
            last_err = RuntimeError(f"model {model} 逾時（~{TEXT_TIMEOUT}s）")
            continue
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(str(last_err) if last_err else "Gemini 模型皆不可用")


def _clean_meta_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        parts = [_clean_meta_str(x) for x in v]
        parts = [p for p in parts if p]
        return "、".join(parts) if parts else None
    if isinstance(v, str):
        t = v.strip()
        if not t or t.lower() in ("null", "none", "n/a", "不明", "なし", "unknown"):
            return None
        return t
    return str(v).strip() or None



# --- Recovered helpers from pre-corruption bytecode (pyc 11:47) ---
_avbase_build_id = None  # type: ignore

def _install_recovered_helpers() -> None:
    import marshal
    import sys
    import types
    from pathlib import Path as _P

    # Marshal blob is CPython 3.13 bytecode; wrong minor version SIGSEGVs under gunicorn.
    # Skip (don't crash) on other Pythons so unit tests can import cache/cover helpers.
    if sys.version_info[:2] != (3, 13):
        return

    blob_path = _P(__file__).resolve().parent / "_recovered_helpers.marshal"
    if not blob_path.is_file():
        raise FileNotFoundError(
            f"missing {blob_path.name}; commit _recovered_helpers.marshal with the app"
        )
    blob = marshal.loads(blob_path.read_bytes())
    g = globals()
    for _name, _co in blob.items():
        g[_name] = types.FunctionType(_co, g, _name)


_install_recovered_helpers()

# FunctionType(marshal) drops __defaults__/__kwdefaults__; restore common ones.
def _restore_helper_defaults() -> None:
    import types as _types
    g = globals()
    # Known signatures from original source
    fixes = {
        "download_cover_bytes": ((3.0,), None),  # timeout=COVER_DOWNLOAD_TIMEOUT approx
        "fetch_avbase_title_results": ((None,), None),  # actress=None — overridden below anyway
        "fetch_jav321_title_results": ((None,), None),
        "fetch_javlibrary_title_results": ((None,), None),
        "gemini_compare_user_to_cover": (None, {"timeout": 10.0}),
        "work_payload": (None, {"why": None}),
        "related_from_demo": (None, None),
    }
    for name, (defaults, kwdefaults) in fixes.items():
        fn = g.get(name)
        if not isinstance(fn, _types.FunctionType):
            continue
        if defaults is not None:
            # Use module COVER_DOWNLOAD_TIMEOUT when present
            if name == "download_cover_bytes":
                fn.__defaults__ = (float(g.get("COVER_DOWNLOAD_TIMEOUT", 3)),)
            else:
                fn.__defaults__ = defaults
        if kwdefaults is not None:
            fn.__kwdefaults__ = dict(kwdefaults)

_restore_helper_defaults()

# Tests import this module on Python 3.12, where the 3.13 marshal blob is skipped.
# Production still uses the recovered header helper when that blob loaded.
if not callable(globals().get("_avbase_headers")):
    def _avbase_headers() -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept-Language": "ja,en;q=0.8,zh-TW;q=0.6",
            "Accept": "text/html,application/xhtml+xml",
        }


_CDN_UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1"
)


def _cdn_header_variants() -> list[dict[str, str]]:
    accept = "image/jpeg,image/webp,image/avif,image/*,*/*;q=0.8"
    return [
        {
            "User-Agent": UA,
            "Referer": "https://www.dmm.co.jp/",
            "Accept": accept,
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
        },
        {
            "User-Agent": UA,
            "Referer": "https://www.dmm.co.jp/digital/videoa/-/detail/",
            "Accept": accept,
            "Accept-Language": "ja,en;q=0.6",
        },
        {
            "User-Agent": _CDN_UA_IOS,
            "Referer": "https://www.dmm.co.jp/",
            "Accept": "image/*",
        },
    ]


def _looks_like_jpeg(blob: bytes | None) -> bool:
    return bool(blob) and len(blob) >= 800 and blob[:2] == b"\xff\xd8"


def dmm_cover_variant_urls(url: str) -> list[str]:
    """Same-work DMM jacket variants only (pl ↔ ps, digital ↔ mono/movie).

    Never invent jp/js sample stills as a cover. Stills pass through unchanged.
    """
    primary = (url or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        s = (u or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    add(primary)
    if not primary:
        return out

    def _swap(u: str, src: str, dst: str) -> str:
        m = re.search(rf"{src}\.jpg(\?.*)?$", u, flags=re.I)
        if not m:
            return ""
        return re.sub(rf"{src}\.jpg(?=\?|$)", f"{dst}.jpg", u, count=1, flags=re.I)

    add(_swap(primary, "pl", "ps"))
    add(_swap(primary, "ps", "pl"))

    pics = "https://pics.dmm.co.jp"
    m = re.match(
        r"^https?://pics\.dmm\.(?:co\.jp|com)/digital/video/([^/?#]+)/[^/?#]+?(pl|ps)\.jpg(\?.*)?$",
        primary,
        flags=re.I,
    )
    if m:
        cid, q = m.group(1), m.group(3) or ""
        add(f"{pics}/mono/movie/adult/{cid}/{cid}pl.jpg{q}")
        add(f"{pics}/mono/movie/adult/{cid}/{cid}ps.jpg{q}")
    m = re.match(
        r"^https?://pics\.dmm\.(?:co\.jp|com)/mono/movie/adult/([^/?#]+)/[^/?#]+?(pl|ps)\.jpg(\?.*)?$",
        primary,
        flags=re.I,
    )
    if m:
        cid, q = m.group(1), m.group(3) or ""
        add(f"{DMM_PICS}/{cid}/{cid}pl.jpg{q}")
        add(f"{DMM_PICS}/{cid}/{cid}ps.jpg{q}")
    # Same digital jackets on pics.dmm.com (Railway sometimes prefers one host)
    for u in list(out):
        if "pics.dmm.co.jp/digital/" in u:
            add(u.replace("pics.dmm.co.jp", "pics.dmm.com", 1))
        elif "pics.dmm.com/digital/" in u:
            add(u.replace("pics.dmm.com", "pics.dmm.co.jp", 1))
    return out


_COVER_BYTES_LOCK = threading.Lock()
_COVER_BYTES_CACHE: dict[str, tuple[float, bytes | None]] = {}
_COVER_BYTES_MAX = 96


def _cover_cache_get(url: str) -> tuple[bool, bytes | None]:
    now = time.monotonic()
    with _COVER_BYTES_LOCK:
        hit = _COVER_BYTES_CACHE.get(url)
        if not hit:
            return False, None
        ts, blob = hit
        ttl = 600.0 if blob else 45.0
        if now - ts > ttl:
            _COVER_BYTES_CACHE.pop(url, None)
            return False, None
        return True, blob


def _cover_cache_put(url: str, blob: bytes | None) -> None:
    now = time.monotonic()
    with _COVER_BYTES_LOCK:
        if url not in _COVER_BYTES_CACHE and len(_COVER_BYTES_CACHE) >= _COVER_BYTES_MAX:
            oldest = min(_COVER_BYTES_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _COVER_BYTES_CACHE.pop(oldest, None)
        _COVER_BYTES_CACHE[url] = (now, blob)


def download_cover_bytes(url: str, timeout: float | None = None) -> bytes | None:
    """Fetch candidate cover/still bytes. Prefer DMM CDN; reject placeholders.

    Successful bytes and hard misses are cached in-process so a 14-image
    series does not download the same jacket once per slot.
    """
    u = (url or "").strip()
    if not u.startswith("http"):
        return None
    found, blob = _cover_cache_get(u)
    if found:
        return blob
    blob, cacheable = _download_cover_bytes_uncached(u, timeout)
    if cacheable:
        _cover_cache_put(u, blob)
    return blob


def _download_cover_bytes_uncached(url: str, timeout: float | None = None) -> tuple[bytes | None, bool]:
    """Return (bytes, cacheable). Timeouts are not cached; hard misses are."""
    if timeout is None:
        timeout = float(COVER_DOWNLOAD_TIMEOUT)
    u = (url or "").strip()
    if not u.startswith("http"):
        return None, False
    if is_now_printing_url(u):
        return None, True
    connect_t = 2.5
    read_t = max(1.5, float(timeout))
    headers_list = _cdn_header_variants()
    cid_m = re.search(r"/digital/video/([^/?#]+)/", u, flags=re.I)
    if cid_m:
        headers_list = list(headers_list) + [
            {
                "User-Agent": UA,
                "Referer": (
                    "https://www.dmm.co.jp/digital/videoa/-/detail/=/cid="
                    + cid_m.group(1)
                    + "/"
                ),
                "Accept": "image/jpeg,image/webp,image/*,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9",
            }
        ]
    saw_timeout = False
    for headers in headers_list:
        try:
            r = requests.get(
                u,
                timeout=(connect_t, read_t),
                headers=headers,
                verify=False,
                allow_redirects=True,
            )
            if r.status_code >= 400 or not r.content or len(r.content) < 800:
                if r.status_code in (403, 429):
                    continue
                return None, True
            if is_now_printing_url(str(r.url or u)):
                return None, True
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" in ctype:
                continue
            if not _looks_like_jpeg(r.content):
                continue
            return r.content, True
        except requests.Timeout:
            saw_timeout = True
            continue
        except Exception:
            continue
    return None, not saw_timeout


def fetch_cdn_file_bytes(url: str, timeout: float | None = None) -> bytes | None:
    """Same-origin proxy fetch: requested URL, then jacket pl/ps / mono fallbacks."""
    urls = dmm_cover_variant_urls(url)
    if not urls:
        return None
    primary_t = 8.0 if timeout is None else float(timeout)
    blob = download_cover_bytes(urls[0], timeout=primary_t)
    if blob:
        return blob
    # One longer retry on the exact URL (Railway → DMM can stall on pl.jpg)
    blob = download_cover_bytes(urls[0], timeout=max(10.0, primary_t))
    if blob:
        return blob
    for alt in urls[1:]:
        blob = download_cover_bytes(alt, timeout=min(8.0, max(5.0, primary_t)))
        if blob:
            return blob
    return None



def enrich_title_candidate(c: dict, why: str = "片名候選") -> dict:
    """Normalize a title-search hit into a gallery-ready work dict (CDN cover/stills)."""
    code_raw = str(c.get("code") or "").strip()
    code = format_display_code(code_raw) if code_raw and parse_code_parts(code_raw) else ""
    cid_in = str(c.get("cid") or "") or None
    cover_in = str(c.get("cover") or c.get("cover_url") or "").strip() or None
    cid, cover, stills_default = sanitize_cover_fields(
        code=code or None, cid=cid_in, cover=cover_in
    )
    stills = c.get("stills")
    if not isinstance(stills, list) or not stills:
        stills = stills_default
    else:
        stills = [str(u) for u in stills if u and not is_now_printing_url(str(u))]
        if not stills:
            stills = stills_default
    title_zh = (
        str(c.get("title_zh") or c.get("titleZh") or "").strip() or None
    )
    out = {
        "code": code or None,
        "title": (str(c.get("title") or "").strip() or None),
        "title_zh": title_zh,
        "actress": (str(c.get("actress") or "").strip() or None),
        "studio": (str(c.get("studio") or "").strip() or None),
        "cid": cid or None,
        "cover": cover or None,
        "stills": stills,
        "why": why,
        "line": "candidate",
        "score": (
            c.get("visual_score")
            if c.get("visual_score") is not None
            else c.get("score")
        ),
        "title_score": c.get("title_score", c.get("score")),
        "source": c.get("source"),
    }
    if c.get("visual") is not None:
        out["visual"] = c.get("visual")
    if c.get("visual_score") is not None:
        out["visual_score"] = c.get("visual_score")
        vs = c.get("visual") or {}
        if vs.get("same_work"):
            out["why"] = f"{why}／視覺相符"
        elif c.get("visual_score"):
            out["why"] = f"{why}／視覺排序"
    return out


def parse_visual_match_json(text: str) -> dict[str, Any]:
    s = strip_json_fences(text)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return {
                "same_work": False,
                "confidence": 0.0,
                "reason": "parse_fail",
                "match_person": False,
                "match_clothes": False,
                "match_pose": False,
            }
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {
                "same_work": False,
                "confidence": 0.0,
                "reason": "parse_fail",
                "match_person": False,
                "match_clothes": False,
                "match_pose": False,
            }
    if not isinstance(data, dict):
        data = {}

    def as_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y")
        return False

    try:
        conf = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = data.get("reason")
    if not isinstance(reason, str):
        reason = ""
    parsed = {
        "same_work": as_bool(data.get("same_work")),
        "confidence": conf,
        "reason": reason.strip()[:160],
        "match_person": as_bool(data.get("match_person")),
        "match_face": as_bool(data.get("match_face")) if "match_face" in data else None,
        "match_accessories": as_bool(data.get("match_accessories")) if "match_accessories" in data else None,
        "match_clothes": as_bool(data.get("match_clothes")),
        "match_pose": as_bool(data.get("match_pose")),
    }
    return enforce_visual_same_work(parsed)


def enforce_visual_same_work(vm: dict) -> dict:
    """Require person+clothes and real identity cues vs the *original user image*.

    Cover/stills similarity or same-series vibe is not enough. same_work stays
    true only when the user shot matches identity + outfit, with face/accessories
    /pose support when those flags are present. Explicit clothes or person miss
    always rejects (series siblings must not win).
    """
    if not isinstance(vm, dict):
        return {
            "same_work": False,
            "confidence": 0.0,
            "reason": "invalid",
            "match_person": False,
            "match_clothes": False,
            "match_pose": False,
        }
    person = bool(vm.get("match_person"))
    clothes = bool(vm.get("match_clothes"))
    pose = bool(vm.get("match_pose"))
    face = vm.get("match_face")
    accessories = vm.get("match_accessories")
    # Detail: prefer face/accessories; pose alone only if face/accessories omitted.
    detail_ok = False
    if face is True or accessories is True:
        detail_ok = True
    elif face is None and accessories is None:
        detail_ok = pose or (person and clothes)
    else:
        # Explicit false on face and accessories → need strong pose+clothes+person
        detail_ok = bool(pose and person and clothes)

    try:
        conf = float(vm.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    # Hard rejects for series-sibling traps
    if not clothes:
        vm["same_work"] = False
        vm["confidence"] = min(conf, 0.30)
        return vm
    if not person:
        vm["same_work"] = False
        vm["confidence"] = min(conf, 0.28)
        return vm
    if face is False and accessories is False and not pose:
        vm["same_work"] = False
        vm["confidence"] = min(conf, 0.32)
        return vm
    if not detail_ok:
        vm["same_work"] = False
        conf = min(conf, 0.38)
        vm["confidence"] = conf
        return vm

    # person+clothes+detail ok — still require model same_work OR very high confidence
    if vm.get("same_work") and conf < 0.50:
        # Low confidence "same_work" from series vibe → demote
        vm["same_work"] = False
        vm["confidence"] = min(conf, 0.42)
        return vm
    if not vm.get("same_work") and conf >= 0.88 and person and clothes and (face is True or accessories is True):
        # Near-duplicate crop vs cover/still: allow when flags strongly agree
        vm["same_work"] = True
    return vm


def visual_match_score(vm: dict) -> float:
    """Rank score from visual compare JSON. Face/accessories outweigh series similarity."""
    vm = enforce_visual_same_work(dict(vm) if isinstance(vm, dict) else {})
    conf = float(vm.get("confidence") or 0.0)
    bonus = 0.0
    if vm.get("match_person"):
        bonus += 0.10
    else:
        bonus -= 0.18
        conf = min(conf, 0.32)
    if vm.get("match_face"):
        bonus += 0.16
    elif "match_face" in vm and vm.get("match_face") is False:
        bonus -= 0.14
        conf = min(conf, 0.32)
    if vm.get("match_accessories"):
        bonus += 0.12
    elif "match_accessories" in vm and vm.get("match_accessories") is False:
        bonus -= 0.08
    if vm.get("match_clothes"):
        bonus += 0.10
    else:
        bonus -= 0.12
        conf = min(conf, 0.36)
    if vm.get("match_pose"):
        bonus += 0.06
    else:
        bonus -= 0.04
    if vm.get("same_work"):
        if vm.get("match_face") is False or not vm.get("match_person") or not vm.get("match_clothes"):
            bonus -= 0.25
        else:
            bonus += 0.14
    return max(0.0, min(1.0, conf * 0.65 + bonus))


def _jpeg_thumb_b64(image_bytes: bytes, max_side: int = 1024, quality: int = 75) -> tuple[str, str]:
    """Return (b64, mime) thumbnail for Gemini multi-image calls."""
    from io import BytesIO

    try:
        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        m = max(w, h)
        if m > max_side:
            scale = max_side / m
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        return base64.b64encode(image_bytes).decode("ascii"), "image/jpeg"


def gemini_rank_covers_batch(
    user_bytes: bytes,
    cover_bytes_list: list[bytes],
    api_key: str,
    *,
    timeout: float = 10.0,
    labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One Gemini call: user crop + N covers → per-cover visual JSON list (aligned). Never raises."""
    if not cover_bytes_list:
        return []
    u_b64, u_mime = _jpeg_thumb_b64(user_bytes, max_side=900, quality=70)
    parts: list[dict] = [
        {"text": "UserCrop（使用者圖）："},
        {"inline_data": {"mime_type": u_mime, "data": u_b64}},
    ]
    for i, cb in enumerate(cover_bytes_list):
        c_b64, c_mime = _jpeg_thumb_b64(cb, max_side=640, quality=65)
        label = ""
        if labels and i < len(labels) and labels[i]:
            label = f" code={labels[i]}"
        parts.append({"text": f"Cover{i}{label}："})
        parts.append({"inline_data": {"mime_type": c_mime, "data": c_b64}})
    parts.append({"text": VISUAL_RANK_BATCH_PROMPT})
    n_covers = len(cover_bytes_list)
    out_tokens = 1024 if n_covers <= 4 else 1536
    payload_base = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.05, "maxOutputTokens": out_tokens},
    }
    last_err: Exception | None = None
    text = ""
    for model in GEMINI_TEXT_MODELS:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            r = requests.post(url, json=payload_base, timeout=timeout)
            if r.status_code >= 400:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                continue
            text = gemini_extract_text(r.json())
            break
        except Exception as e:
            last_err = e
            continue
    if not text:
        return [
            {
                "same_work": False,
                "confidence": 0.0,
                "reason": f"batch_fail:{type(last_err).__name__ if last_err else '?'}",
                "match_person": False,
                "match_clothes": False,
                "match_pose": False,
            }
            for _ in cover_bytes_list
        ]

    raw = strip_json_fences(text)
    data: Any = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                rankings_salvage: list[dict] = []
                for rm in re.finditer(
                    r'\{\s*"index"\s*:\s*(\d+)[^}]*\}',
                    m.group(0),
                ):
                    try:
                        rankings_salvage.append(json.loads(rm.group(0)))
                    except json.JSONDecodeError:
                        try:
                            rankings_salvage.append(
                                {
                                    "index": int(rm.group(1)),
                                    "same_work": False,
                                    "confidence": 0.0,
                                    "reason": "salvage_partial",
                                }
                            )
                        except Exception:
                            pass
                bi = re.search(r'"best_index"\s*:\s*(\d+)', m.group(0))
                data = {
                    "best_index": int(bi.group(1)) if bi else None,
                    "rankings": rankings_salvage,
                }
        else:
            data = {}
    if not isinstance(data, dict):
        data = {}
    rankings = data.get("rankings")
    best_index = data.get("best_index")
    out: list[dict[str, Any]] = []
    by_idx: dict[int, dict] = {}
    if isinstance(rankings, list):
        for row in rankings:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("index"))
            except (TypeError, ValueError):
                continue
            by_idx[idx] = parse_visual_match_json(json.dumps(row, ensure_ascii=False))
    for i in range(len(cover_bytes_list)):
        vm = by_idx.get(i) or {
            "same_work": False,
            "confidence": 0.0,
            "reason": "missing",
            "match_person": False,
            "match_clothes": False,
            "match_pose": False,
        }
        try:
            if best_index is not None and int(best_index) == i:
                # Prefer best_index for ranking only — never invent same_work without
                # person/clothes/(face|accessories|pose) agreement (same-series trap).
                vm["confidence"] = max(float(vm.get("confidence") or 0), 0.55)
        except (TypeError, ValueError):
            pass
        vm = enforce_visual_same_work(vm)
        out.append(vm)
    same_idxs = [i for i, vm in enumerate(out) if vm.get("same_work")]
    if len(same_idxs) > 1:
        best = max(same_idxs, key=lambda i: float(out[i].get("confidence") or 0))
        for i in same_idxs:
            if i != best:
                out[i]["same_work"] = False
                out[i]["confidence"] = min(float(out[i].get("confidence") or 0), 0.4)
    return out





def _cid_from_avbase_product(product: dict | None, work_id: str | None = None) -> str | None:
    """Best-effort DMM cid from an avbase product dict."""
    if not isinstance(product, dict):
        product = {}
    pid = str(product.get("product_id") or product.get("cid") or "").strip()
    if pid:
        # strip common prefixes like h_1059
        m = re.match(r"^(?:h_\d+)?([a-z]+\d+)$", pid, re.I)
        if m:
            return m.group(1).lower()
        return pid.lower()
    if work_id and parse_code_parts(str(work_id)):
        return code_to_cid(format_display_code(str(work_id)))
    return None



def fetch_avbase_by_code(code: str) -> dict | None:
    """Resolve a 品番 via avbase.net work page / works?q=code (title + actress + studio)."""
    from urllib.parse import quote

    display = format_display_code(code) if parse_code_parts(code) else (code or "").strip().upper()
    query_display = display
    if not display or not parse_code_parts(display):
        return None
    headers = _avbase_headers()
    work = None
    # Lookup slugs: keep display zeros, also try stripped (DOSD-008 → DOSD-8) and pads
    _slugs = code_lookup_slugs(display)
    # Prefer exact work page (stable even when search ranking is odd)
    for slug in _slugs:
        try:
            url = f"https://www.avbase.net/works/{quote(slug)}"
            r = requests.get(url, headers=headers, timeout=10, verify=False)
            if r.status_code >= 400 or not r.text:
                continue
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                continue
            data = json.loads(m.group(1))
            cand = ((data.get("props") or {}).get("pageProps") or {}).get("work")
            if isinstance(cand, dict) and str(cand.get("work_id") or "").strip():
                wid = format_display_code(str(cand.get("work_id")))
                # Same 品番 if letters match and numeric values equal (099 == 99)
                if codes_numeric_equal(wid, query_display):
                    display = prefer_display_code(query_display, wid)
                    work = cand
                    break
        except Exception:
            continue
    if work is None:
        try:
            javbus_row = _fetch_javbus_by_code(display)
        except Exception:
            javbus_row = None
        if isinstance(javbus_row, dict) and (javbus_row.get("title") or javbus_row.get("code")):
            return javbus_row
        try:
            rows = fetch_avbase_title_results(display, actress=None)
        except Exception:
            rows = []
        for row in rows:
            row_code = str(row.get("code") or "")
            if codes_numeric_equal(row_code, query_display):
                display = prefer_display_code(query_display, row_code)
                # Normalize to identify_code meta shape
                return {
                    "code": display,
                    "title": row.get("title"),
                    "actress": row.get("actress"),
                    "studio": row.get("studio"),
                    "cid": row.get("cid") or code_to_cid(display),
                    "related": [],
                    "source": "avbase",
                    "cover": row.get("cover"),
                }
        return None

    title = str(work.get("title") or "").strip() or None
    products = work.get("products") or []
    p0 = products[0] if products and isinstance(products[0], dict) else {}
    cid = _cid_from_avbase_product(p0, display) if p0 else None
    if not cid:
        cid = code_to_cid(display)
    studio = None
    actress_name = None
    if p0:
        maker = p0.get("maker") or {}
        if isinstance(maker, dict):
            studio = maker.get("name")
    actors = (
        work.get("actors")
        or work.get("casts")
        or work.get("performers")
        or (p0.get("actors") if p0 else None)
        or []
    )
    if isinstance(actors, list) and actors:
        a0 = actors[0]
        if isinstance(a0, dict):
            actress_name = (
                a0.get("name")
                or a0.get("actor_name")
                or ((a0.get("actor") or {}) if isinstance(a0.get("actor"), dict) else {}).get("name")
            )
        elif isinstance(a0, str):
            actress_name = a0
    # Title often ends with actress name after a space (…巨乳美女6 広瀬美結)
    if not actress_name and title:
        parts = str(title).strip().split()
        if len(parts) >= 2 and 2 <= len(parts[-1]) <= 20:
            # Avoid trailing digits-only tokens
            if any(ch.isalpha() or ("\u3040" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff") for ch in parts[-1]):
                if not re.search(r"\d", parts[-1]):
                    actress_name = parts[-1]
    cover = None
    if p0:
        cover = p0.get("image_url") or p0.get("thumbnail_url")
    if not cover and cid:
        cover = cover_url(cid)
    return {
        "code": display,
        "title": title,
        "actress": actress_name,
        "studio": studio,
        "cid": cid,
        "related": [],
        "source": "avbase",
        "cover": cover,
    }


def fetch_avbase_title_results(title: str, actress: str | None = None) -> list[dict]:
    """
    Title → code via avbase.net (works from this box; ~0.3–0.7s).
    Uses __NEXT_DATA__ JSON; returns candidate dicts with DMM covers.
    """
    import time as _time
    from urllib.parse import quote

    title = (title or "").strip()
    if not title:
        return []
    headers = _avbase_headers()
    out: list[dict] = []
    seen: set[str] = set()
    t0 = _time.monotonic()
    avbase_failed = False
    try:
        url = f"https://www.avbase.net/works?q={quote(title)}"
        r = requests.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code >= 400 or not r.text:
            avbase_failed = True
        else:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                avbase_failed = True
            else:
                data = json.loads(m.group(1))
                works = ((data.get("props") or {}).get("pageProps") or {}).get("works") or []
                # Rebind so the loop below sees the parsed list. A 200 with
                # zero works is a real empty answer and must not hit javbus.
                r = r  # noqa: keep response in scope for nothing
                _avbase_works = works
    except Exception:
        avbase_failed = True
        _avbase_works = []
    else:
        if avbase_failed:
            _avbase_works = []
    if not avbase_failed:
        try:
            for w in _avbase_works:
                if not isinstance(w, dict):
                    continue
                code_raw = str(w.get("work_id") or "").strip()
                if not code_raw or not parse_code_parts(code_raw):
                    continue
                code = format_display_code(code_raw)
                if code in seen:
                    continue
                rtitle = str(w.get("title") or "").strip()
                products = w.get("products") or []
                p0 = products[0] if products and isinstance(products[0], dict) else {}
                actors = w.get("actors") or (p0.get("actors") if isinstance(p0, dict) else None) or []
                actor_names: list[str] = []
                if isinstance(actors, list):
                    for a0 in actors:
                        if isinstance(a0, dict) and a0.get("name"):
                            actor_names.append(str(a0.get("name")))
                        elif isinstance(a0, str) and a0.strip():
                            actor_names.append(a0.strip())
                actor_blob = " ".join(actor_names)

                # Actress-name queries: keep works starring that person even when
                # the title text does not contain her name (old filter dropped them).
                q_compact = re.sub(r"[\s　・·．.]+", "", title)
                act_q = re.sub(r"[\s　・·．.]+", "", (actress or "").strip())
                is_actress_query = bool(
                    (actress and (actress in title or act_q == q_compact))
                    or (
                        len(q_compact) >= 3
                        and not parse_code_parts(title)
                        and re.fullmatch(r"[\u3040-\u30ff\u4e00-\u9fff]{2,12}", q_compact or "")
                        and not re.search(r"[をにでがはもとからまでへの「」]", title or "")
                    )
                )
                name_in_actors = False
                if is_actress_query and q_compact:
                    for an in actor_names:
                        an_c = re.sub(r"[\s　・·．.]+", "", an)
                        if q_compact in an_c or an_c in q_compact or (actress and actress in an):
                            name_in_actors = True
                            break
                    if not name_in_actors and q_compact and q_compact in re.sub(r"\s+", "", rtitle):
                        name_in_actors = True

                score = title_similarity(title, rtitle)
                if title and title[: min(8, len(title))] and title[:8] in rtitle:
                    score = max(score, 0.85)
                # Code-style queries (e.g. DRPT-120) must match work_id, not JP title text
                q_code = format_display_code(title) if parse_code_parts(title) else ""
                if q_code and q_code == code:
                    score = max(score, 1.0)
                if is_actress_query and name_in_actors:
                    score = max(score, 0.72)
                if score < 0.25 and not (is_actress_query and name_in_actors):
                    continue
                cid = _cid_from_avbase_product(p0, code)
                cover = None
                studio = None
                actress_name = actress
                if p0:
                    cover = p0.get("image_url") or p0.get("thumbnail_url")
                    maker = p0.get("maker") or {}
                    if isinstance(maker, dict):
                        studio = maker.get("name")
                if not actress_name and actor_names:
                    actress_name = actor_names[0]
                if is_actress_query and name_in_actors and not actress_name:
                    actress_name = title
                if not cid:
                    cid = code_to_cid(code)
                if not cover and cid:
                    cover = cover_url(cid)
                # Prefer digital CDN pl cover when possible
                if cover and "mono/movie" in str(cover) and cid:
                    dig = cover_url(cid)
                    if dig:
                        cover = dig
                seen.add(code)
                out.append(
                    {
                        "code": code,
                        "title": rtitle or title,
                        "actress": actress_name,
                        "studio": studio,
                        "cid": cid,
                        "cover": cover,
                        "href": f"https://www.avbase.net/works/{w.get('id')}" if w.get("id") else None,
                        "source": "avbase",
                        "score": float(score),
                    }
                )
                if len(out) >= 24:
                    break
        except Exception:
            avbase_failed = True
            out = []
    if out:
        out.sort(key=lambda x: x.get("score") or 0, reverse=True)
        return out
    if avbase_failed:
        return _fetch_javbus_title_results(title, actress)
    return []


_JAVBUS_SEARCH_MEMO: dict[str, list[dict]] = {}


def _javbus_headers() -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Cookie": "dv=1",
        "Referer": "https://www.javbus.com/",
        "Accept-Language": "ja,en;q=0.8",
    }


def _javbus_is_actress_query(title: str, actress: str | None) -> bool:
    q_compact = re.sub(r"[\s　・·．.]+", "", title or "")
    act_q = re.sub(r"[\s　・·．.]+", "", (actress or "").strip())
    if actress and (actress in (title or "") or act_q == q_compact):
        return True
    # A personal name (柊ゆうき), not a title fragment that happens to use the same script.
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{1,5}[\u3040-\u309f]{1,8}", q_compact or ""))


def _fetch_javbus_title_results(title: str, actress: str | None = None) -> list[dict]:
    """Title / actress search via javbus when avbase did not answer.

    Used only after an avbase HTTP failure. A successful avbase response,
    including zero matching works, never calls this.
    """
    from urllib.parse import quote

    title = (title or "").strip()
    if not title:
        return []
    memo_key = re.sub(r"\s+", "", title) + "\n" + re.sub(r"\s+", "", actress or "")
    cached = _JAVBUS_SEARCH_MEMO.get(memo_key)
    if cached is not None:
        return [dict(row) for row in cached]
    out: list[dict] = []
    try:
        url = "https://www.javbus.com/search/" + quote(title)
        r = requests.get(url, headers=_javbus_headers(), timeout=12, verify=False)
        if r.status_code >= 400 or not r.text:
            _JAVBUS_SEARCH_MEMO[memo_key] = []
            return []
        actress_q = _javbus_is_actress_query(title, actress)
        seen: set[str] = set()
        for box in re.finditer(
            r'<a class="movie-box"\s+href="https://www\.javbus\.com/([A-Za-z0-9]+-\d+)"[^>]*>(.*?)</a>',
            r.text,
            re.S,
        ):
            code = format_display_code(box.group(1))
            if not parse_code_parts(code) or code in seen:
                continue
            blob = box.group(2)
            title_m = re.search(r'<img[^>]*\stitle="([^"]+)"', blob)
            date_m = re.search(r"<date>\s*([A-Za-z0-9]+-\d+)\s*</date>", blob)
            if date_m and parse_code_parts(date_m.group(1)):
                code = format_display_code(date_m.group(1))
            rtitle = (title_m.group(1).strip() if title_m else "") or title
            score = title_similarity(title, rtitle)
            compact_q = re.sub(r"\s+", "", title)
            if len(compact_q) >= 12 and title[:8] in rtitle:
                score = max(score, 0.85)
            q_code = format_display_code(title) if parse_code_parts(title) else ""
            if q_code and codes_numeric_equal(q_code, code):
                score = max(score, 1.0)
            actress_name = actress
            if actress_q:
                score = max(score, 0.72)
                if not actress_name:
                    actress_name = title
            elif score < 0.25:
                continue
            cid = code_to_cid(code)
            seen.add(code)
            out.append(
                {
                    "code": code,
                    "title": rtitle,
                    "actress": actress_name,
                    "studio": None,
                    "cid": cid,
                    "cover": cover_url(cid) if cid else None,
                    "href": f"https://www.javbus.com/{code}",
                    "source": "javbus",
                    "score": float(score),
                    "title_fit": _title_cover_ratio(title, rtitle),
                }
            )
            if len(out) >= 24:
                break
    except Exception:
        out = []
    out.sort(key=lambda x: (-(x.get("score") or 0), -(x.get("title_fit") or 0)))
    _JAVBUS_SEARCH_MEMO[memo_key] = [dict(row) for row in out]
    return out


def _fetch_javbus_by_code(code: str) -> dict | None:
    """One work page: catalog title and billed actress. No invented Chinese."""
    display = format_display_code(code) if parse_code_parts(code or "") else ""
    if not display:
        return None
    memo_key = "code\n" + display
    cached = _JAVBUS_SEARCH_MEMO.get(memo_key)
    if cached is not None:
        return dict(cached[0]) if cached else None
    try:
        r = requests.get(
            f"https://www.javbus.com/{display}",
            headers=_javbus_headers(),
            timeout=12,
            verify=False,
        )
        if r.status_code >= 400 or not r.text or "avatar-box" not in r.text and "<h3>" not in r.text:
            _JAVBUS_SEARCH_MEMO[memo_key] = []
            return None
        h3 = ""
        hm = re.search(r"<h3>(.*?)</h3>", r.text, re.S)
        if hm:
            h3 = re.sub(r"<[^>]+>", " ", hm.group(1))
            h3 = re.sub(r"\s+", " ", h3).strip()
        actress_name = None
        am = re.search(r'class="avatar-box"[^>]*>.*?title="([^"]+)"', r.text, re.S)
        if am:
            actress_name = am.group(1).strip() or None
        title = h3
        if title.upper().startswith(display):
            title = title[len(display) :].strip()
        if actress_name and title.endswith(actress_name):
            title = title[: -len(actress_name)].strip()
        if not is_usable_title(title):
            _JAVBUS_SEARCH_MEMO[memo_key] = []
            return None
        cid = code_to_cid(display)
        row = {
            "code": display,
            "title": title,
            "actress": actress_name,
            "studio": None,
            "cid": cid,
            "related": [],
            "source": "javbus",
            "cover": cover_url(cid) if cid else None,
        }
        _JAVBUS_SEARCH_MEMO[memo_key] = [dict(row)]
        return row
    except Exception:
        _JAVBUS_SEARCH_MEMO[memo_key] = []
        return None


def fetch_jav321_title_results(title: str, actress: str | None = None) -> list[dict]:
    """jav321 only helps when the query embeds a 品番."""
    codes = extract_codes(title or "")
    out: list[dict] = []
    for c in codes[:3]:
        disp = format_display_code(c)
        if not parse_code_parts(disp):
            continue
        try:
            meta = fetch_jav321(disp) if "fetch_jav321" in globals() else None
        except Exception:
            meta = None
        cid = code_to_cid(disp)
        item = {
            "code": disp,
            "title": (meta or {}).get("title") or title,
            "actress": (meta or {}).get("actress") or actress,
            "studio": (meta or {}).get("studio"),
            "cid": cid,
            "cover": cover_url(cid) if cid else None,
            "source": "jav321",
            "score": 0.5,
        }
        out.append(item)
    return out


def fetch_javlibrary_title_results(title: str, actress: str | None = None) -> list[dict]:
    """Best-effort; often 403 from this box."""
    try:
        return fetch_javlibrary(title) if False else []  # disabled path placeholder
    except Exception:
        return []


# Note: leave marshal fetch_javlibrary(code) for identify_code path


def same_series_collision_indices(candidates: list[dict]) -> list[int]:
    """
    Indices of candidates that collide on near-identical titles / title scores.
    When 2+ codes share a series title, visual compare must cover all of them
    (not only the first 2–3 by title score).
    """
    coded = list(candidates or [])
    if len(coded) < 2:
        return list(range(len(coded)))
    scores = [float(c.get("score") or 0.0) for c in coded]
    top = max(scores) if scores else 0.0
    titles = [str(c.get("title") or "").strip() for c in coded]
    # Prefer the most common non-empty title as series anchor
    anchor = ""
    for t in titles:
        if t:
            anchor = t
            break
    # If top-score cluster is large, treat that whole cluster as collision
    cluster = [
        i
        for i, sc in enumerate(scores)
        if sc >= max(0.35, top - SAME_SERIES_SCORE_GAP)
    ]
    if len(cluster) >= 2:
        # Expand with near-identical titles vs any cluster member
        selected = set(cluster)
        for i, t in enumerate(titles):
            if i in selected or not t:
                continue
            for j in list(selected):
                tj = titles[j]
                if tj and title_similarity(t, tj) >= SAME_SERIES_TITLE_SIM:
                    selected.add(i)
                    break
                if anchor and title_similarity(t, anchor) >= SAME_SERIES_TITLE_SIM:
                    selected.add(i)
                    break
        return sorted(selected)
    # Fallback: titles similar to first
    if anchor:
        idxs = [
            i
            for i, t in enumerate(titles)
            if t and title_similarity(t, anchor) >= SAME_SERIES_TITLE_SIM
        ]
        if len(idxs) >= 2:
            return idxs
    return list(range(min(len(coded), VISUAL_COMPARE_MAX)))


def _visual_is_lock(vm: dict | None) -> bool:
    """Lock-quality verdict: same work and the same clothes as the user image."""
    if not isinstance(vm, dict):
        return False
    return bool(vm.get("same_work") and vm.get("match_clothes"))


def _still_explicit_reject(vm: dict | None) -> bool:
    """A still that disagrees on person or clothes. Missing flags are not a reject."""
    if not isinstance(vm, dict):
        return False
    if vm.get("match_clothes") is False or vm.get("match_person") is False:
        return True
    return False


def _collect_still_urls(item: dict, *, limit: int = 3) -> list[str]:
    """Up to `limit` still URLs. Prefer ones already on the candidate, then CDN."""
    urls: list[str] = []
    seen: set[str] = set()
    raw = item.get("stills") if isinstance(item.get("stills"), list) else []
    for u in raw:
        s = str(u or "").strip()
        if not s or is_now_printing_url(s) or s in seen:
            continue
        seen.add(s)
        urls.append(s)
        if len(urls) >= limit:
            return urls
    cid = str(item.get("cid") or "").strip()
    if not cid:
        code = format_display_code(str(item.get("code") or ""))
        cid = code_to_cid(code) if code and parse_code_parts(code) else ""
    if cid and len(urls) < limit:
        try:
            for u in still_urls(str(cid), limit):
                if u in seen or is_now_printing_url(u):
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= limit:
                    break
        except Exception:
            pass
    return urls[:limit]


def _reconcile_cover_with_stills(
    prev_sc: float,
    prev_item: dict,
    still_pairs: list[tuple[float, dict]],
) -> tuple[float, dict, str]:
    """Merge a cover verdict with still comparisons against the user image.

    One disagreeing still must not revoke a cover lock: stills from the same
    work often use another outfit. Revoke only after at least two stills
    explicitly reject person or clothes and none of them lock. Any still that
    agrees on same_work + clothes becomes the lock.
    """
    prev_vm = prev_item.get("visual") or {}
    cover_lock = _visual_is_lock(prev_vm)
    locks = [
        (sc, it)
        for sc, it in still_pairs
        if _visual_is_lock((it or {}).get("visual") or {})
    ]
    if locks:
        locks.sort(
            key=lambda pair: float(((pair[1].get("visual") or {}).get("confidence")) or 0),
            reverse=True,
        )
        sc, it = locks[0]
        return max(float(sc), float(prev_sc) + 0.01), it, "still_lock"
    rejects = [
        it
        for _sc, it in still_pairs
        if _still_explicit_reject((it or {}).get("visual") or {})
    ]
    if cover_lock and len(still_pairs) >= 2 and len(rejects) >= 2:
        revoked = dict(prev_item)
        rvm = dict(prev_vm)
        rvm["same_work"] = False
        if any((it.get("visual") or {}).get("match_clothes") is False for it in rejects):
            rvm["match_clothes"] = False
        if any((it.get("visual") or {}).get("match_person") is False for it in rejects):
            rvm["match_person"] = False
        rvm["confidence"] = min(float(rvm.get("confidence") or 0), 0.34)
        reasons = [
            str((it.get("visual") or {}).get("reason") or "").strip()
            for it in rejects
            if str((it.get("visual") or {}).get("reason") or "").strip()
        ]
        reason_bit = reasons[0] if reasons else "stills mismatch"
        rvm["reason"] = (str(rvm.get("reason") or "") + f"｜劇照核對否決：{reason_bit}")[:240]
        rvm = enforce_visual_same_work(rvm)
        revoked["visual"] = {
            "same_work": rvm.get("same_work"),
            "confidence": rvm.get("confidence"),
            "reason": rvm.get("reason"),
            "match_person": rvm.get("match_person"),
            "match_face": rvm.get("match_face"),
            "match_accessories": rvm.get("match_accessories"),
            "match_clothes": rvm.get("match_clothes"),
            "match_pose": rvm.get("match_pose"),
        }
        revoked["visual_score"] = visual_match_score(rvm)
        ts = float(revoked.get("title_score") or revoked.get("score") or 0)
        revoked["score"] = round(float(revoked["visual_score"]) * 0.92 + ts * 0.08, 4)
        return float(revoked["score"]), revoked, "revoked"
    if cover_lock:
        return float(prev_sc), prev_item, "cover_lock"
    if still_pairs:
        best_sc, best_it = max(still_pairs, key=lambda pair: float(pair[0]))
        if float(best_sc) > float(prev_sc) + 0.05:
            return float(best_sc), best_it, "rank"
    return float(prev_sc), prev_item, "rank"


def _format_visual_rank_note(meta: dict, best_code: str, best_vm: dict | None) -> str:
    """User-facing compare note. Sort-only states why a lock was not possible."""
    ncmp = int(meta.get("compared") or 0)
    still_bit = "＋劇照" if meta.get("note_stills") else ""
    vm = best_vm or {}
    if _visual_is_lock(vm):
        lock_bit = "視覺鎖定"
        revoke_bit = ""
    elif meta.get("stills_revoked"):
        lock_bit = "僅排序未鎖定（多張劇照與封面衣服／人物不一致，無法視覺鎖定）"
        revoke_bit = "；已否決封面誤判"
    elif meta.get("note_stills"):
        lock_bit = "僅排序未鎖定（封面與劇照皆未同時符合同一作品與衣服）"
        revoke_bit = ""
    else:
        lock_bit = "僅排序未鎖定（沒有足以視覺鎖定的封面或劇照）"
        revoke_bit = ""
    return (
        f"已對照使用者原圖比對 {ncmp} 張封面{still_bit}"
        f"（主選 {best_code}；{lock_bit}；依人物／衣服／表情／飾品／姿勢{revoke_bit}）"
    )


def rank_candidates_by_visual(
    user_image_bytes: bytes,
    candidates: list[dict],
    *,
    api_key: str | None = None,
    max_n: int = VISUAL_COMPARE_MAX,
    budget_s: float = VISUAL_COMPARE_BUDGET,
) -> tuple[list[dict], dict]:
    """
    Rank candidates by Gemini visual match vs user crop.
    For same-series title collisions, compare all near-tie codes (up to max_n),
    set main = best visual match, and rewrite candidate scores from visual_score.
    Never raises.
    """
    import time

    meta: dict[str, Any] = {
        "visual_ranked": False,
        "compared": 0,
        "skipped": 0,
        "note": "",
        "mode": "",
        "compared_codes": [],
    }
    key = (api_key or get_gemini_api_key() or "").strip()
    if not key or not user_image_bytes:
        meta["note"] = "no_key_or_image"
        return list(candidates or []), meta

    coded = [
        dict(c)
        for c in (candidates or [])
        if c.get("code") and parse_code_parts(str(c["code"]))
    ]
    if len(coded) < 1:
        return list(candidates or []), meta

    # Preserve original title scores
    for c in coded:
        if c.get("title_score") is None and c.get("score") is not None:
            c["title_score"] = float(c.get("score") or 0)

    if len(coded) == 1:
        # Single candidate: still verify cover (+stills path below) against user original
        collision_idxs = [0]
        want_sorted = [0]
        n_cap = 1
        pick_idxs = [0]
        top = [coded[0]]
        rest = []
    else:
        collision_idxs = same_series_collision_indices(coded)
        # Always include index 0; expand to full collision set, capped by max_n
        want = sorted(set(collision_idxs) | {0})
        # Prefer higher title_score within the collision set when truncating
        want_sorted = sorted(
            want,
            key=lambda i: float(coded[i].get("title_score") or coded[i].get("score") or 0),
            reverse=True,
        )
        # Same-series collisions: compare as many covers as budget allows (up to 8)
        n_cap = max(2, min(int(max_n), 8, len(want_sorted), len(coded)))
        pick_idxs = want_sorted[:n_cap]
        # Keep original relative order for stable Cover0.. labels among picks
        pick_idxs = sorted(pick_idxs)
        top = [coded[i] for i in pick_idxs]
        pick_set = set(pick_idxs)
        rest = [dict(c) for i, c in enumerate(coded) if i not in pick_set]
    t0 = time.monotonic()

    pairs: list[tuple[dict, bytes]] = []
    for c in top:
        item = dict(c)
        disp_c = format_display_code(str(item["code"]))
        rcid, rcover, _ = sanitize_cover_fields(
            code=disp_c,
            cid=str(item.get("cid") or "") or None,
            cover=str(item.get("cover") or "") or None,
        )
        if rcid:
            item["cid"] = rcid
        if rcover:
            item["cover"] = rcover
        else:
            item["cover"] = None
        cover = str(item.get("cover") or "").strip()
        blob = download_cover_bytes(cover) if cover else None
        if not blob:
            alt = str(c.get("cover") or c.get("cover_url") or "").strip()
            if alt and alt != cover:
                blob = download_cover_bytes(alt)
                if blob:
                    item["cover"] = alt
        if not blob:
            meta["skipped"] += 1
            continue
        pairs.append((item, blob))

    if len(pairs) < 1:
        meta["note"] = "need_cover"
        return list(candidates or []), meta

    def _attach(item: dict, vm: dict) -> tuple[float, dict]:
        item = dict(item)
        vm = enforce_visual_same_work(dict(vm) if isinstance(vm, dict) else {})
        item["visual"] = {
            "same_work": vm.get("same_work"),
            "confidence": vm.get("confidence"),
            "reason": vm.get("reason"),
            "match_person": vm.get("match_person"),
            "match_face": vm.get("match_face"),
            "match_accessories": vm.get("match_accessories"),
            "match_clothes": vm.get("match_clothes"),
            "match_pose": vm.get("match_pose"),
        }
        vs = visual_match_score(vm)
        item["visual_score"] = vs
        title_sc = float(item.get("title_score") or item.get("score") or 0.0)
        item["title_score"] = title_sc
        # Display / sort score must reflect visual discrimination (not flat 0.85)
        item["score"] = round(vs * 0.92 + title_sc * 0.08, 4)
        return (float(item["score"]), item)

    def _run_batch(chunk: list[tuple[dict, bytes]], timeout: float) -> list[dict] | None:
        if len(chunk) < 2:
            return None
        vms_local = gemini_rank_covers_batch(
            user_image_bytes,
            [b for _, b in chunk],
            key,
            timeout=timeout,
            labels=[str(it.get("code") or "") for it, _b in chunk],
        )
        if (
            not vms_local
            or len(vms_local) != len(chunk)
            or all(str(vm.get("reason") or "").startswith("batch_fail") for vm in vms_local)
            or all(str(vm.get("reason") or "") in ("missing", "salvage_partial", "parse_fail") for vm in vms_local)
        ):
            return None
        # Treat all-missing as fail
        if all(str(vm.get("reason") or "") == "missing" for vm in vms_local):
            return None
        return vms_local

    ranked_pairs: list[tuple[float, dict]] = []
    _single_verify_done = False

    if len(pairs) == 1:
        # Single cover: verify vs user original, then stills cross-check below
        meta["mode"] = "single_verify"
        (item0, blob0) = pairs[0]
        vms0 = gemini_rank_covers_batch(
            user_image_bytes,
            [blob0],
            key,
            timeout=max(8.0, min(18.0, budget_s - 0.5)),
            labels=[str(item0.get("code") or "")],
        )
        if not vms0:
            meta["note"] = "single_verify_fail"
            return list(candidates or []), meta
        ranked_pairs = [_attach(item0, vms0[0])]
        meta["compared"] = 1
        meta["compared_codes"] = [str(item0.get("code") or "")]
        _single_verify_done = True

    remain = budget_s - (time.monotonic() - t0)

    # Chunk size 4 keeps JSON short enough for Flash; tournament covers 5–8 codes
    CHUNK = 4
    scored: dict[str, tuple[float, dict]] = {}

    if _single_verify_done:
        pass  # ranked_pairs already set; skip multi-cover batch
    elif len(pairs) <= CHUNK:
        meta["mode"] = "batch"
        meta["compared_codes"] = [str(it.get("code") or "") for it, _b in pairs]
        timeout = max(10.0, min(22.0, remain - 0.8))
        vms = _run_batch(pairs, timeout)
        if vms:
            meta["compared"] = len(pairs)
            for (item, _b), vm in zip(pairs, vms):
                ranked_pairs.append(_attach(item, vm))
        else:
            remain = budget_s - (time.monotonic() - t0)
            if remain >= 7.0 and len(pairs) >= 2:
                meta["mode"] = "batch_top2_retry"
                chunk = pairs[:2]
                vms = _run_batch(chunk, max(7.0, min(16.0, remain - 0.5)))
                if vms:
                    meta["compared"] = 2
                    meta["compared_codes"] = [str(it.get("code") or "") for it, _b in chunk]
                    for (item, _b), vm in zip(chunk, vms):
                        ranked_pairs.append(_attach(item, vm))
                    for item, _b in pairs[2:]:
                        item = dict(item)
                        ts = float(item.get("title_score") or item.get("score") or 0)
                        item["title_score"] = ts
                        item["visual_score"] = 0.05
                        item["score"] = round(ts * 0.05, 4)
                        ranked_pairs.append((float(item["score"]), item))
            if not ranked_pairs:
                meta["mode"] = "batch_failed"
                meta["note"] = "visual_timeout"
                return list(candidates or []), meta
    else:
        # Tournament: score every cover in chunks of 4, then final face-off of top
        meta["mode"] = "batch_tournament"
        chunk_winners: list[tuple[dict, bytes]] = []
        for start in range(0, len(pairs), CHUNK):
            remain = budget_s - (time.monotonic() - t0)
            if remain < 6.0:
                break
            chunk = pairs[start : start + CHUNK]
            if len(chunk) == 1 and chunk_winners:
                # Pair leftover with previous winner
                chunk = [chunk_winners[-1], chunk[0]]
            if len(chunk) < 2:
                continue
            timeout = max(8.0, min(18.0, remain - 0.6))
            vms = _run_batch(chunk, timeout)
            if not vms:
                continue
            meta["compared"] += len(chunk)
            local: list[tuple[float, dict, bytes]] = []
            for (item, blob), vm in zip(chunk, vms):
                sc, attached = _attach(item, vm)
                code_k = format_display_code(str(attached.get("code") or ""))
                prev = scored.get(code_k)
                if prev is None or sc > prev[0]:
                    scored[code_k] = (sc, attached)
                local.append((sc, attached, blob))
            local.sort(key=lambda x: x[0], reverse=True)
            # keep top 2 from chunk for final
            for sc, attached, blob in local[:2]:
                chunk_winners.append((attached, blob))
        if scored:
            # Final face-off among unique top codes (up to 4)
            uniq: list[tuple[dict, bytes]] = []
            seen_c: set[str] = set()
            # Prefer highest scored so far
            for code_k, (sc, attached) in sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True):
                if code_k in seen_c:
                    continue
                # find blob
                blob = None
                for it, b in pairs:
                    if format_display_code(str(it.get("code") or "")) == code_k:
                        blob = b
                        break
                if blob is None:
                    continue
                uniq.append((attached, blob))
                seen_c.add(code_k)
                if len(uniq) >= 4:
                    break
            remain = budget_s - (time.monotonic() - t0)
            if len(uniq) >= 2 and remain >= 7.0:
                vms = _run_batch(uniq, max(7.0, min(16.0, remain - 0.5)))
                if vms:
                    meta["compared"] += len(uniq)
                    meta["mode"] = "batch_tournament_final"
                    for (item, _b), vm in zip(uniq, vms):
                        sc, attached = _attach(item, vm)
                        code_k = format_display_code(str(attached.get("code") or ""))
                        scored[code_k] = (sc, attached)
            ranked_pairs = sorted(scored.values(), key=lambda x: x[0], reverse=True)
            # Include any pairs not scored (failed chunk) as demoted
            for item, _b in pairs:
                code_k = format_display_code(str(item.get("code") or ""))
                if code_k not in scored:
                    item = dict(item)
                    ts = float(item.get("title_score") or item.get("score") or 0)
                    item["title_score"] = ts
                    item["visual_score"] = 0.04
                    item["score"] = 0.04
                    ranked_pairs.append((0.04, item))
        if not ranked_pairs:
            meta["mode"] = "batch_failed"
            meta["note"] = "visual_timeout"
            return list(candidates or []), meta

    ranked_pairs.sort(key=lambda x: x[0], reverse=True)

    # Always cross-check top candidates against stills vs the original user image.
    # Cover-only same_work can be a series-sibling false positive, but one still
    # from the same work often wears a different outfit. Download several stills
    # and revoke a cover lock only when at least two of them reject person or
    # clothes and none agree. A still that locks (same_work + clothes) wins.
    # This pass is not gated on leftover cover budget — a second image search
    # must re-check stills even when the cover compare already ran long.
    if ranked_pairs:
        n_still_codes = min(4, len(ranked_pairs)) if len(ranked_pairs) >= 2 else 1
        flat_pairs: list[tuple[dict, bytes]] = []
        for _sc, it in ranked_pairs[:n_still_codes]:
            urls = _collect_still_urls(it, limit=3)
            got = 0
            for u in urls:
                if got >= 3:
                    break
                try:
                    blob = download_cover_bytes(u)
                except Exception:
                    blob = None
                if not blob:
                    continue
                flat_pairs.append((dict(it), blob))
                got += 1
        if flat_pairs:
            still_timeout = 12.0
            vms_all: list[dict | None] = []
            for start in range(0, len(flat_pairs), 4):
                chunk = flat_pairs[start : start + 4]
                vms = None
                if len(chunk) >= 2:
                    vms = _run_batch(chunk, still_timeout)
                else:
                    only_item, only_blob = chunk[0]
                    vms = gemini_rank_covers_batch(
                        user_image_bytes,
                        [only_blob],
                        key,
                        timeout=still_timeout,
                        labels=[str(only_item.get("code") or "")],
                    )
                if not vms or len(vms) != len(chunk):
                    vms_all.extend([None] * len(chunk))
                else:
                    vms_all.extend(vms)
            scored_stills = [vm for vm in vms_all if isinstance(vm, dict)]
            if scored_stills:
                meta["mode"] = (meta.get("mode") or "batch") + "+stills"
                meta["note_stills"] = True
                meta["compared"] += len(scored_stills)
                by_code: dict[str, tuple[float, dict]] = {
                    format_display_code(str(it.get("code") or "")): (sc, it)
                    for sc, it in ranked_pairs
                }
                grouped: dict[str, list[tuple[float, dict]]] = {}
                for (item, _b), vm in zip(flat_pairs, vms_all):
                    if not isinstance(vm, dict):
                        continue
                    sc, attached = _attach(item, vm)
                    code_k = format_display_code(str(attached.get("code") or ""))
                    grouped.setdefault(code_k, []).append((sc, attached))
                for code_k, still_list in grouped.items():
                    prev = by_code.get(code_k)
                    if prev is None:
                        by_code[code_k] = still_list[0]
                        continue
                    prev_sc, prev_it = prev
                    new_sc, new_it, outcome = _reconcile_cover_with_stills(
                        prev_sc, prev_it, still_list
                    )
                    by_code[code_k] = (new_sc, new_it)
                    if outcome == "revoked":
                        meta["stills_revoked"] = True
                ranked_pairs = list(by_code.values())

    def _lock_sort_key(pair: tuple[float, dict]) -> tuple:
        sc, it = pair
        vm = it.get("visual") or {}
        # A lock outranks a higher title/code score that never matched clothes.
        return (1 if _visual_is_lock(vm) else 0, float(sc))

    ranked_pairs.sort(key=_lock_sort_key, reverse=True)

    # Append non-compared codes after visually ranked ones (still list them),
    # but demote flat title scores so UI ordering matches visual ranking.
    demoted_rest: list[dict] = []
    for item in rest:
        item = dict(item)
        ts = float(item.get("title_score") or item.get("score") or 0.0)
        item["title_score"] = ts
        if item.get("visual_score") is None:
            item["visual_score"] = 0.02
            item["score"] = 0.02
        demoted_rest.append(item)
    ranked_list = [it for _, it in ranked_pairs] + demoted_rest
    meta["visual_ranked"] = meta["compared"] > 0
    if meta["visual_ranked"]:
        best_code = str(ranked_list[0].get("code") or "")
        best_vm = (ranked_list[0].get("visual") or {}) if ranked_list else {}
        meta["note"] = _format_visual_rank_note(meta, best_code, best_vm)
        meta["visual_lock"] = _visual_is_lock(best_vm)
    return ranked_list, meta



def filter_title_candidates(candidates: list[dict], min_score: float = 0.25) -> list[dict]:
    """Dedupe by code; keep hits with score >= min_score or javlibrary source."""
    ranked: list[dict] = []
    seen: set[str] = set()
    for c in candidates or []:
        code_raw = str(c.get("code") or "").strip()
        if not code_raw or not parse_code_parts(code_raw):
            continue
        code = format_display_code(code_raw)
        score = float(c.get("score") or 0)
        src = str(c.get("source") or "")
        if score < min_score and src not in ("javlibrary", "avbase", "jav321"):
            continue
        if code in seen:
            continue
        seen.add(code)
        item = dict(c)
        item["code"] = code
        if not item.get("cover") or is_now_printing_url(str(item.get("cover") or "")):
            rcid, rcover, _ = sanitize_cover_fields(code=code, cid=item.get("cid"), cover=item.get("cover"))
            item["cid"] = rcid
            item["cover"] = rcover
        ranked.append(item)

    def rank(c: dict) -> tuple:
        parts = parse_code_parts(c.get("code") or "")
        label = (parts[0] if parts else "").upper()
        pref = 1 if label in PREFERRED_LABELS else 0
        src = 1 if c.get("source") in ("javlibrary", "avbase", "jav321") else 0
        fit = float(c.get("title_fit") or 0)
        return (c.get("score") or 0, fit, src, pref)

    ranked.sort(key=rank, reverse=True)
    return ranked


def _strip_glued_actress(title: str, actress: str | None) -> str:
    """Drop an OCR cast name glued onto the ends of a title.

    「舌技が神柊ゆうき」 must still search as 「舌技が神」. A name that sits in
    the middle of the phrase is left alone so we do not punch a hole in it.
    """
    raw = normalize_ocr_title(title) or (title or "").strip()
    act = re.sub(r"\s+", "", (actress or "").strip())
    if not raw or len(act) < 2:
        return raw
    compact = re.sub(r"\s+", "", raw)
    if act not in compact or compact == act:
        return raw
    names = [act]
    spaced = re.sub(r"\s+", "", (actress or "").strip())
    if spaced and spaced not in names:
        names.append(spaced)
    shown = (actress or "").strip()
    if shown and shown not in names:
        names.append(shown)
    stripped = raw
    for name in names:
        for pat in (
            rf"^\s*{re.escape(name)}\s*",
            rf"\s*{re.escape(name)}\s*$",
        ):
            stripped = re.sub(pat, " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" ・·／/|　")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped != raw and is_usable_title(stripped):
        return stripped
    return raw


def _catalog_title_queries(title: str, actress: str | None = None) -> list[str]:
    """Title strings to send to the catalog.

    The raw phrase is always included, even when prefix variants are missing
    or throw. A cast name glued on by OCR is searched without that name first.
    """
    original = normalize_ocr_title(title) or (title or "").strip()
    stripped = _strip_glued_actress(original, actress)
    queries: list[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", (q or "").strip())
        if len(q) < 4 or q in queries:
            return
        queries.append(q)

    if stripped and stripped != original:
        add(stripped)
    add(original)
    try:
        variant_fn = globals().get("title_query_variants")
        if callable(variant_fn):
            for q in variant_fn(stripped or original) or []:
                add(str(q))
    except Exception:
        pass
    return queries[:8]


def _boost_title_score(item: dict, *phrases: str) -> dict:
    """Raise a catalog row when any searched phrase is actually in its title."""
    item = dict(item)
    score = float(item.get("score") or 0)
    catalog = str(item.get("title") or "")
    fit = float(item.get("title_fit") or 0)
    for phrase in phrases:
        phrase = (phrase or "").strip()
        if not phrase or not catalog:
            continue
        compact = re.sub(r"\s+", "", phrase)
        score = max(score, title_similarity(phrase, catalog))
        fit = max(fit, _title_cover_ratio(phrase, catalog))
        # Only a long phrase may count as the title itself. A 4-char hook
        # that happens to occur inside another work stays a weak clue.
        if len(compact) >= 12 and (phrase in catalog or catalog in phrase):
            cover = _title_cover_ratio(phrase, catalog)
            score = max(score, 0.92 if cover >= 0.8 else 0.5 + 0.4 * cover)
        elif len(compact) >= 12 and phrase[:8] in catalog:
            score = max(score, 0.85)
    item["score"] = score
    item["title_fit"] = fit
    return item


def _trailing_billed_name(title: str | None) -> str:
    """Short name after the series line (先生が… 柊ゆうき). Not a slogan."""
    raw = re.sub(r"\s+", " ", (normalize_ocr_title(title) or title or "")).strip()
    parts = [p for p in raw.split(" ") if p]
    if len(parts) < 2:
        return ""
    tail = parts[-1]
    head = "".join(parts[:-1])
    if len(head) < 8 or not (2 <= len(tail) <= 8):
        return ""
    if not re.fullmatch(r"[\u3040-\u30ff\u4e00-\u9fff・]+", tail):
        return ""
    return tail


def _core_title(title: str | None) -> str:
    raw = re.sub(r"\s+", " ", (normalize_ocr_title(title) or title or "")).strip()
    name = _trailing_billed_name(raw)
    if name:
        raw = raw[: raw.rfind(name)].strip()
    return re.sub(r"\s+", "", raw)


def _select_title_volume(
    cands: list[dict] | None,
    query: str,
    actress: str | None = None,
) -> tuple[dict | None, bool]:
    """Pick one catalog row for a typed title.

    Returns (row, ambiguous). Ambiguous is true when several volumes share the
    core title and neither an exact line nor a billed name distinguishes them.
    Catalog order is not a tie-break.
    """
    rows = [c for c in (cands or []) if isinstance(c, dict) and c.get("code")]
    if not rows:
        return None, False
    q_compact = re.sub(r"\s+", "", normalize_ocr_title(query) or query or "")
    exact = [
        c
        for c in rows
        if q_compact
        and re.sub(r"\s+", "", normalize_ocr_title(c.get("title")) or str(c.get("title") or ""))
        == q_compact
    ]
    if len(exact) == 1:
        return exact[0], False
    pool = exact if len(exact) > 1 else rows
    q_core = _core_title(query)
    tied = []
    for c in pool:
        core = _core_title(c.get("title") or "")
        if q_core and core and (core == q_core or q_compact == core):
            tied.append(c)
    if len(tied) <= 1:
        return (tied[0] if tied else rows[0]), False
    name = re.sub(r"\s+", "", (actress or "").strip())
    if len(name) >= 2:
        matched = []
        for c in tied:
            blob = re.sub(
                r"\s+",
                "",
                str(c.get("title") or "") + str(c.get("actress") or ""),
            )
            if name in blob:
                matched.append(c)
        if len(matched) == 1:
            return matched[0], False
    return None, True


def _title_search_result(
    cands: list[dict],
    query: str,
    actress: str | None = None,
    *,
    title_zh: str | None = None,
    pack,
) -> dict | None:
    """One volume, or an unresolved series when the title alone cannot choose."""
    best, ambiguous = _select_title_volume(cands, query, actress)
    if ambiguous:
        return {
            "code": None,
            "title": query,
            "actress": (actress or "").strip() or None,
            "series_unresolved": True,
            "candidates": list(cands or []),
            "score": None,
            "source": "title",
        }
    if not best:
        return None
    out = {
        "code": best.get("code"),
        "title": best.get("title") or query,
        "title_zh": best.get("title_zh") or title_zh,
        "actress": best.get("actress") or actress,
        "studio": best.get("studio"),
        "cover": best.get("cover"),
        "cid": best.get("cid"),
        "source": best.get("source"),
        "score": best.get("score"),
        "title_fit": best.get("title_fit"),
    }
    return pack(out, cands)


def search_by_title(title: str, actress: str | None = None) -> dict | None:
    """
    Resolve a title to work code(s) and optional cover.
    Tries: demo → (Chinese) Gemini map → AVBase → jav321(code) → JAVLibrary → DuckDuckGo.
    Returns best match dict; when 2+ distinct codes match, includes them in `candidates`.
    """
    title = normalize_ocr_title(title) or (title or "").strip()
    if not is_usable_title(title):
        return None

    def _pack(best: dict, extras: list[dict] | None = None) -> dict:
        """Attach candidates list (best first) and ensure CDN cover on coded hits."""
        out = dict(best)
        code = out.get("code")
        if code and parse_code_parts(str(code)):
            out["code"] = format_display_code(str(code))
            cid, cover, _st = sanitize_cover_fields(
                code=out["code"],
                cid=str(out.get("cid") or "") or None,
                cover=str(out.get("cover") or "") or None,
            )
            out["cid"] = cid
            out["cover"] = cover
        cands: list[dict] = []
        seen: set[str] = set()
        for item in [out] + list(extras or []):
            c_code = item.get("code")
            if not c_code or not parse_code_parts(str(c_code)):
                continue
            disp = format_display_code(str(c_code))
            if disp in seen:
                continue
            seen.add(disp)
            packed = dict(item)
            packed["code"] = disp
            cid, cover, _st = sanitize_cover_fields(
                code=disp,
                cid=str(packed.get("cid") or "") or None,
                cover=str(packed.get("cover") or "") or None,
            )
            packed["cid"] = cid
            packed["cover"] = cover
            cands.append(packed)
        out["candidates"] = cands
        return out

    # 0) Local demo: fast, no network — collect all strong title matches
    demo = load_demo()
    works = demo.get("works") or {}
    demo_hits: list[dict] = []
    for _k, raw in works.items():
        if not isinstance(raw, dict) or not raw.get("code"):
            continue
        dt = str(raw.get("title") or "")
        sc = title_similarity(title, dt)
        if sc < 0.45:
            continue
        code = format_display_code(str(raw["code"]))
        if not parse_code_parts(code):
            continue
        cid = str(raw.get("cid") or code_to_cid(code) or "")
        demo_hits.append(
            {
                "code": code,
                "title": raw.get("title") or title,
                "actress": raw.get("actress") or actress,
                "studio": raw.get("studio"),
                "cover": raw.get("cover")
                or raw.get("cover_url")
                or (cover_url(cid) if cid else None),
                "cid": cid or None,
                "source": "demo",
                "score": sc,
            }
        )
    demo_hits.sort(key=lambda x: x.get("score") or 0, reverse=True)
    # Exact / strong single demo match: return immediately (with siblings if any)
    if demo_hits and (demo_hits[0].get("score") or 0) >= 0.55:
        # Keep other demo hits that are clearly different codes (score >= 0.45)
        return _pack(demo_hits[0], demo_hits)

    original_title = title
    # OCR cast is only a hint for stripping a glued name. It is not a filter:
    # a wrong actress must not hide a title the catalog can search.
    _actress_hint = actress
    actress = None
    _score_phrases = tuple(
        dict.fromkeys(
            p
            for p in (
                _strip_glued_actress(original_title, _actress_hint),
                original_title,
            )
            if p
        )
    )

    # 0a) AVBase on original title (+ shorter prefixes) BEFORE Gemini rewrite.
    # One bad variant must not wipe hits already collected.
    early_av: list[dict] = []
    seen_codes: set[str] = set()
    for q in _catalog_title_queries(original_title, _actress_hint):
        try:
            batch = fetch_avbase_title_results(q, actress=None)
        except Exception:
            continue
        for item in batch:
            code = item.get("code")
            if not code or code in seen_codes:
                continue
            item = _boost_title_score(item, *_score_phrases)
            seen_codes.add(code)
            early_av.append(item)
        strong = [c for c in early_av if (c.get("score") or 0) >= 0.55]
        if strong:
            break
    early_av = filter_title_candidates(early_av, min_score=0.30)
    if early_av and (early_av[0].get("score") or 0) >= 0.45:
        return _title_search_result(early_av, original_title, _actress_hint, pack=_pack)

    # 0a2) Distinctive short n-grams when full OCR title still misses (censored glyphs / truncation)
    if not early_av or (early_av and (early_av[0].get("score") or 0) < 0.45):
        try:
            short_qs: list[str] = []
            # The full string can miss (length / a censored glyph). The head of
            # that same string is still the title; a bare theme word is not.
            raw = normalize_ocr_title(original_title) or original_title
            raw_compact = re.sub(r"\s+", "", raw)
            for n in (40, 32, 24, 18):
                if len(raw_compact) > n + 6:
                    short_qs.append(raw_compact[:n])
            for q in _title_related_keyword_queries(original_title):
                if q and 4 <= len(q) <= 16 and q not in short_qs:
                    short_qs.append(q)
            # Prefer mid-title chunks that OCR usually gets right (bus/seat etc.)
            for n in (6, 8, 10, 12):
                if len(raw) >= n + 4:
                    mid = raw[len(raw) // 4 : len(raw) // 4 + n]
                    if mid and mid not in short_qs:
                        short_qs.append(mid)
            seen_short: set[str] = {str(c.get("code") or "") for c in early_av if c.get("code")}
            for q in short_qs[:8]:
                for item in fetch_avbase_title_results(q, actress=actress)[:8]:
                    code = item.get("code")
                    if not code or code in seen_short:
                        continue
                    item = _boost_title_score(item, *_score_phrases, q)
                    seen_short.add(code)
                    early_av.append(item)
                strong = [c for c in early_av if (c.get("score") or 0) >= 0.55]
                if strong:
                    break
            early_av = filter_title_candidates(early_av, min_score=0.30)
            if early_av and (early_av[0].get("score") or 0) >= 0.45:
                return _title_search_result(early_av, original_title, _actress_hint, pack=_pack)
        except Exception:
            pass

    # 0b) Chinese title → Gemini map to JP title + code
    gemini_hit: dict | None = None
    query_title_zh: str | None = None
    try:
        chinese_heavy = bool(is_chinese_heavy_title(title))
    except Exception:
        # Helper lives in the 3.13 marshal blob. A missing name must not abort
        # a Japanese title that AVBase already had a chance to resolve.
        chinese_heavy = False
    if chinese_heavy:
        query_title_zh = _clean_title_zh(title) or title.strip()
        try:
            gemini_hit = gemini_map_chinese_title(title)
        except Exception:
            gemini_hit = None
        # Do not early-return a single Gemini code when web may yield alternatives;
        # seed candidates and continue so multi-code listing works.
        if gemini_hit and gemini_hit.get("title_ja"):
            title = str(gemini_hit["title_ja"]).strip() or title
        if gemini_hit and gemini_hit.get("title_zh"):
            query_title_zh = _clean_title_zh(str(gemini_hit["title_zh"])) or query_title_zh

    candidates: list[dict] = []

    # Seed from Gemini code if present
    if gemini_hit and gemini_hit.get("code") and parse_code_parts(str(gemini_hit["code"])):
        code = format_display_code(str(gemini_hit["code"]))
        cid = code_to_cid(code)
        candidates.append(
            {
                "code": code,
                "title": gemini_hit.get("title_ja") or gemini_hit.get("title") or title,
                "title_zh": query_title_zh or gemini_hit.get("title_zh"),
                "actress": gemini_hit.get("actress") or actress,
                "studio": gemini_hit.get("studio"),
                "cover": cover_url(cid) if cid else None,
                "cid": cid,
                "source": "gemini",
                "score": 0.55,
            }
        )

    # a) AVBase title search (fast & works from this box; JAVLibrary=403 / DDG empty)
    try:
        queries: list[str] = []
        for base_q in (original_title, title):
            if base_q:
                queries.extend(_catalog_title_queries(base_q, _actress_hint))
        for q in dict.fromkeys(queries):
            try:
                batch = fetch_avbase_title_results(q, actress=None)
            except Exception:
                continue
            for item in batch:
                if any(c.get("code") == item.get("code") for c in candidates):
                    continue
                item = _boost_title_score(item, *_score_phrases)
                candidates.append(item)
            if any((c.get("score") or 0) >= 0.55 and c.get("source") == "avbase" for c in candidates):
                break
    except Exception:
        pass

    # a2) jav321 — only helps when query embeds a 品番 (site has no JP title search)
    try:
        for item in fetch_jav321_title_results(title, actress=actress):
            if not any(c.get("code") == item.get("code") for c in candidates):
                candidates.append(item)
    except Exception:
        pass

    strong_av = [
        c for c in candidates
        if c.get("source") in ("avbase", "jav321") and (c.get("score") or 0) >= 0.4
    ]

    # b) JAVLibrary / DuckDuckGo — skip when avbase already resolved (fail-fast budget)
    if not strong_av:
        try:
            candidates.extend(fetch_javlibrary_title_results(title, actress=actress))
        except Exception:
            pass

        try:
            ddg = fetch_duckduckgo_title_results(title)
            seen = {c["code"] for c in candidates if c.get("code")}
            for item in ddg:
                if item["code"] in seen:
                    for i, c in enumerate(candidates):
                        if c.get("code") == item["code"]:
                            if item.get("cover") and not c.get("cover"):
                                c["cover"] = item["cover"]
                            if (item.get("score") or 0) > (c.get("score") or 0):
                                merged = {**c}
                                for k, v in item.items():
                                    if v:
                                        merged[k] = v
                                candidates[i] = merged
                    continue
                seen.add(item["code"])
                candidates.append(item)
        except Exception:
            pass

    good = filter_title_candidates(candidates, min_score=0.25)

    # If nothing passed the filter but we have raw candidates, keep top by rank
    if not good and candidates:
        def rank_raw(c: dict) -> tuple:
            parts = parse_code_parts(c.get("code") or "")
            label = (parts[0] if parts else "").upper()
            pref = 1 if label in PREFERRED_LABELS else 0
            src = 1 if c.get("source") in ("javlibrary", "avbase", "jav321") else 0
            return (c.get("score") or 0, src, pref)

        coded = [c for c in candidates if c.get("code") and parse_code_parts(str(c["code"]))]
        if coded:
            best_raw = max(coded, key=rank_raw)
            if (best_raw.get("score") or 0) >= 0.15 or best_raw.get("source") in ("javlibrary", "avbase", "jav321"):
                good = filter_title_candidates([best_raw], min_score=0.0)

    if not good:
        # Title-only partial from Gemini JP title (no code)
        if gemini_hit and (gemini_hit.get("title") or gemini_hit.get("title_ja")):
            return {
                "code": None,
                "title": gemini_hit.get("title_ja") or gemini_hit.get("title"),
                "title_zh": query_title_zh or gemini_hit.get("title_zh"),
                "actress": gemini_hit.get("actress") or actress,
                "studio": gemini_hit.get("studio"),
                "cover": None,
                "source": "gemini",
                "score": 0.5,
                "candidates": [],
            }
        return None

    best = good[0]
    # Require some similarity unless only one javlibrary / preferred hit
    if (best.get("score") or 0) < 0.25 and len(good) > 1:
        if best.get("source") not in ("javlibrary", "avbase", "jav321"):
            # drop weak head; try next
            good = [c for c in good if (c.get("score") or 0) >= 0.25 or c.get("source") in ("javlibrary", "avbase", "jav321")]
            if not good:
                return None
            best = good[0]
            good = [best]

    return _title_search_result(
        good, original_title, _actress_hint, title_zh=query_title_zh, pack=_pack
    )



def lookup_demo(code: str) -> dict | None:
    """Lookup a work in local demo-package.json by display code."""
    display = format_display_code(str(code or ""))
    if not display or not parse_code_parts(display):
        return None
    demo = load_demo()
    works = demo.get("works") or {}
    # Direct key
    if display in works and isinstance(works[display], dict):
        return dict(works[display])
    # Scan by code field
    for _k, raw in works.items():
        if not isinstance(raw, dict):
            continue
        rc = format_display_code(str(raw.get("code") or ""))
        if rc == display:
            return dict(raw)
    # Also accept cid-ish keys
    cid = code_to_cid(display)
    if cid and cid in works and isinstance(works[cid], dict):
        return dict(works[cid])
    return None

_TITLE_CUT_TAIL = re.compile(r"(?:…+|‥+|\.{3,})\s*$")


def _title_is_cut(title: str | None) -> bool:
    """True when a title ends in an ellipsis, not when … sits inside a full title."""
    return bool(_TITLE_CUT_TAIL.search((title or "").strip()))


def _title_stem(title: str) -> str:
    return _TITLE_CUT_TAIL.sub("", (title or "").strip()).strip()


def _titles_are_same_phrase(a: str | None, b: str | None) -> bool:
    """One title, not two scenes that only share a keyword such as 夜行バス.

    Equal, or one stem is a real prefix of the other (OCR cut or a longer
    read of the same line). A trailing … is ignored; an internal … is not.
    """
    left = _title_stem(re.sub(r"\s+", " ", (a or "").strip()))
    right = _title_stem(re.sub(r"\s+", " ", (b or "").strip()))
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 8 and longer.startswith(shorter)


def _vision_only_adds_cast_or_edition(catalog: str, vision: str) -> bool:
    """True when vision is the catalog line plus a cast name and/or BOD/VOL.

    「正式題名 三比菜々美 (BOD)」 is the same work, not a longer title.
    A longer read of the sentence itself (more plot text) is not this case.
    """
    cat = _title_stem(re.sub(r"\s+", " ", (catalog or "").strip()))
    body = _title_stem(re.sub(r"\s+", " ", _strip_edition_markers(vision or "")).strip())
    if not cat or not body:
        return False
    cat_c = re.sub(r"\s+", "", cat)
    body_c = re.sub(r"\s+", "", body)
    if body_c == cat_c:
        return True
    if not body_c.startswith(cat_c):
        return False
    extra = body_c[len(cat_c) :]
    if not extra or len(extra) > 16:
        return False
    if re.search(r"[をにでがはもとからまでへの「」！？。…]", extra):
        return False
    parts = [p for p in re.split(r"[、,，・·／/|]+", extra) if p]
    if not parts or len(parts) > 3:
        return False
    for part in parts:
        if not (2 <= len(part) <= 12):
            return False
        if not re.fullmatch(r"[\u3040-\u30ff\u4e00-\u9fff\u3005A-Za-z]+", part):
            return False
    return True


def choose_display_title(catalog: str | None, vision: str | None) -> str | None:
    """Pick the on-card Japanese title. Never invent Chinese.

    The resolved work's catalog title stays when the vision/OCR string is a
    different phrase (夜行バスで激ヤバ…小悪魔女子は must not replace
    就寝中の夜行バスで指マン…). Vision is used only when there is no catalog
    title, or when it is the same phrase: a trailing cut keeps the longer
    catalog line, and a longer read of that same line may extend it.
    A cast name or edition tag glued on the end — 三比菜々美 (BOD), VOL.2 —
    does not replace the catalog title. An official title that only contains
    an internal … is kept whole.
    """
    cat = re.sub(r"\s+", " ", (catalog or "").strip())
    vis = re.sub(r"\s+", " ", (vision or "").strip())
    if not vis:
        return cat or None
    if not cat:
        return vis
    vis_body = re.sub(r"\s+", " ", _strip_edition_markers(vis)).strip() or vis
    same = _titles_are_same_phrase(cat, vis) or _titles_are_same_phrase(cat, vis_body)
    if not same:
        return cat
    if _vision_only_adds_cast_or_edition(cat, vis):
        return cat
    use = vis
    if vis_body != vis and _titles_are_same_phrase(cat, vis_body) and not _title_is_cut(vis_body):
        use = vis_body
    cat_cut = _title_is_cut(cat)
    vis_cut = _title_is_cut(use)
    if vis_cut and not cat_cut:
        return cat
    if cat_cut and not vis_cut and len(use) > len(_title_stem(cat)):
        return use
    if len(use) > len(cat) + 3 and not vis_cut:
        return use
    return cat


def apply_vision_meta(payload: dict, vision_meta: dict | None) -> dict:
    """Fill missing actress/studio from vision. Keep the catalog title.

    A vision string replaces the Japanese title only when it is the same
    phrase. title_zh belongs to the catalog title and is cleared when the
    Japanese title actually changes to a different phrase. Never invent Chinese.
    """
    if not vision_meta:
        return payload
    vt = vision_meta.get("title")
    if vt:
        before = str(payload.get("title") or "")
        chosen = choose_display_title(before, vt)
        payload["title"] = chosen
        if before and chosen and not _titles_are_same_phrase(before, str(chosen)):
            payload["title_zh"] = None
    if vision_meta.get("actress") and not payload.get("actress"):
        payload["actress"] = vision_meta["actress"]
    if vision_meta.get("studio") and not payload.get("studio"):
        payload["studio"] = vision_meta["studio"]
    return payload



# --- Offline identify cache (server-side, persists successful lookups) ---
OFFLINE_CACHE_MAX = 500
# Soft wall-clock for filling missing title_zh on cache hits (main + related).
# Staged across visits: skip items that already have title_zh, fill more next open.
OFFLINE_CACHE_TITLE_ZH_BUDGET = 8.0
# Incremental related top-up on cache hits (skip buckets already at cap).
OFFLINE_CACHE_RELATED_BUDGET = 8.0
# History / related-by-title API: Chinese fill for related slides this request.
HISTORY_RELATED_TITLE_ZH_BUDGET = 8.0
# Related maxima (caps, not quotas — never pad with junk).
RELATED_THEME_CAP = 5
RELATED_KEYWORD_CAP = 5
# Interactive「關鍵字再搜」only. Carousel keyword bucket stays RELATED_KEYWORD_CAP.
RELATED_KEYWORD_RESEARCH_CAP = 10
RELATED_ACTRESS_CAP = 3
# Floor for the keyword bucket. Title search may consume the shared deadline;
# 關鍵字相關 still gets this slice, the same way 同女優 keeps its own budget.
RELATED_KEYWORD_BUDGET = 5.0
STILLS_TARGET = 10
_OFFLINE_CACHE_MEM_LOCK = threading.Lock()
_OFFLINE_CACHE_PATH: Path | None = None


def _offline_cache_resolve_path() -> Path:
    """Prefer data/offline-cache.json; fall back to /tmp if data/ is not writable."""
    global _OFFLINE_CACHE_PATH
    if _OFFLINE_CACHE_PATH is not None:
        return _OFFLINE_CACHE_PATH
    preferred = ROOT / "data" / "offline-cache.json"
    fallback = Path("/tmp/lfp-offline-cache.json")
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        probe = preferred.parent / ".offline-cache-writetest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        _OFFLINE_CACHE_PATH = preferred
    except Exception:
        _OFFLINE_CACHE_PATH = fallback
    return _OFFLINE_CACHE_PATH


def _offline_cache_entry_id(code: str) -> str | None:
    """Canonical entry id: LABEL|int so NHDTC-99 and NHDTC-099 share one slot."""
    parts = parse_code_parts(code)
    if not parts:
        return None
    lab, num = parts
    try:
        return f"{lab}|{int(num)}"
    except ValueError:
        return f"{lab}|{num}"


def _offline_cache_index_keys(code: str | None = None, image_hash: str | None = None) -> list[str]:
    keys: list[str] = []
    if code:
        display = format_display_code(str(code))
        if display:
            keys.append(f"code:{display}")
        parts = parse_code_parts(display or str(code))
        if parts:
            lab, num = parts
            try:
                keys.append(f"code_num:{lab}-{int(num)}")
            except ValueError:
                keys.append(f"code_num:{lab}-{num}")
            # Also index common zero-padded display forms
            for width in (2, 3, 4, 5):
                padded = f"{lab}-{num.zfill(width)}"
                k = f"code:{padded}"
                if k not in keys:
                    keys.append(k)
    if image_hash:
        h = str(image_hash).strip().lower()
        if h:
            keys.append(f"img:{h}")
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _slim_related_for_cache(items: list | None) -> list[dict]:
    slim: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        code = it.get("code")
        slim.append(
            {
                "code": format_display_code(str(code)) if code and parse_code_parts(str(code)) else code,
                "title": it.get("title"),
                "title_zh": it.get("title_zh"),
                "actress": it.get("actress"),
                "cover": it.get("cover"),
                "cid": it.get("cid"),
                "line": it.get("line"),
                "why": it.get("why"),
                "keyword_hits": it.get("keyword_hits"),
                "matched_keywords": list(it.get("matched_keywords") or it.get("hit_keywords") or []),
                "stills": list(it.get("stills") or [])[:10] if isinstance(it.get("stills"), list) else [],
            }
        )
        if len(slim) >= 13:
            break
    return slim


def _related_cache_item_key(it: dict | None) -> str | None:
    if not isinstance(it, dict) or not it.get("code"):
        return None
    return _offline_cache_entry_id(str(it.get("code"))) or str(it.get("code"))


def _why_is_actress_bucket(why: str) -> bool:
    text = why or ""
    return "同女優" in text or "同演員" in text


def _why_is_theme_bucket(why: str) -> bool:
    """Real title/series labels. A passing mention of 主題 (主題線) is not one."""
    text = why or ""
    return "片名" in text or "同系列" in text or "主題相近" in text or text.startswith("主題")


def _related_line_of(x: dict | None) -> str:
    """Bucket for display order: 片名 > 關鍵字 > 同女優.

    Explicit `line` wins. A 同女優 note that only says 主題 in passing
    (主題線に近い) stays in the actress bucket so it cannot sit between
    title and keyword. Dedup keeps the earlier bucket when a work qualifies
    for more than one.
    """
    if not isinstance(x, dict):
        return "theme"
    ln = str(x.get("line") or "")
    if ln == "title":
        ln = "theme"
    why = str(x.get("why") or "")
    if ln == "keyword" or (ln not in {"theme", "keyword", "actress"} and "關鍵字" in why):
        return "keyword"
    if ln == "theme":
        if _why_is_actress_bucket(why) and not _why_is_theme_bucket(why):
            return "actress"
        return "theme"
    if ln == "actress" or _why_is_actress_bucket(why):
        return "actress"
    if _why_is_theme_bucket(why) or "主題" in why or "片名" in why:
        return "theme"
    if "演員" in why or "女優" in why:
        return "actress"
    return "theme"


def _carousel_related_items(payload: dict | None) -> list[dict]:
    """Related cards the gallery carousel actually shows.

    Prefer related_by_title. Fall back to `related` only for real
    theme/keyword/actress rows — title-search candidates are vertical works,
    not carousel slides, so they must not make a「僅顯示主作品」note look false.
    """
    if not isinstance(payload, dict):
        return []
    rel = payload.get("related_by_title")
    use_curated = isinstance(rel, list) and bool(rel)
    source = rel if use_curated else (payload.get("related") or [])
    out: list[dict] = []
    for x in source or []:
        if not isinstance(x, dict) or not x.get("code"):
            continue
        ln = str(x.get("line") or "")
        why = str(x.get("why") or "")
        if ln in {"candidate", "multi", "main"}:
            continue
        if "候選" in why or "candidate" in why.lower():
            continue
        if use_curated:
            out.append(x)
            continue
        if ln in {"theme", "keyword", "actress", "title"} or any(
            s in why for s in ("關鍵字", "同女優", "同演員", "主題相近", "片名相近")
        ):
            out.append(x)
    return out


def _finalize_related_note(payload: dict | None) -> dict | None:
    """Drop a same-actress / main-only note that the carousel contradicts.

    「線上目錄未取得同女優相關；僅顯示主作品 CDN。」 is honest only when the
    actress bucket is empty and no other related cards are showing. Visual-lock
    and title-search banners in other clauses stay.
    """
    if not isinstance(payload, dict):
        return payload
    note = str(payload.get("related_note") or "").strip()
    if not note:
        return payload
    items = _carousel_related_items(payload)
    has_actress = any(_related_line_of(x) == "actress" for x in items)
    has_any = bool(items)
    kept: list[str] = []
    for part in re.split(r"[；;]", note):
        p = part.strip()
        if not p:
            continue
        miss = "未取得同女優" in p
        main_only = "僅顯示主作品" in p
        online_miss = "無法取得線上相關" in p
        if has_actress and (miss or main_only or online_miss):
            continue
        if has_any and (main_only or online_miss):
            continue
        if p not in kept:
            kept.append(p)
    payload["related_note"] = "；".join(kept) or None
    return payload


def _related_bucket_counts(items) -> tuple[int, int, int]:
    t = k = a = 0
    for x in items or []:
        if not isinstance(x, dict):
            continue
        ln = _related_line_of(x)
        if ln == "theme":
            t += 1
        elif ln == "keyword":
            k += 1
        elif ln == "actress":
            a += 1
    return t, k, a


def _cap_related_buckets(items) -> list[dict]:
    """Keep existing order within each bucket; enforce 5+5+3 maxima; no padding."""
    theme: list[dict] = []
    keyword: list[dict] = []
    actress: list[dict] = []
    seen: set[str] = set()
    for x in items or []:
        if not isinstance(x, dict) or not x.get("code"):
            continue
        rc = (
            format_display_code(str(x.get("code")))
            if parse_code_parts(str(x.get("code")))
            else ""
        )
        if not rc or rc in seen:
            continue
        seen.add(rc)
        item = dict(x)
        item["code"] = rc
        item["line"] = _related_line_of(item)
        ln = item["line"]
        if ln == "theme" and len(theme) < RELATED_THEME_CAP:
            theme.append(item)
        elif ln == "keyword" and len(keyword) < RELATED_KEYWORD_CAP:
            keyword.append(item)
        elif ln == "actress" and len(actress) < RELATED_ACTRESS_CAP:
            actress.append(item)
    keyword.sort(key=_keyword_related_sort_key, reverse=True)
    return theme + keyword[:RELATED_KEYWORD_CAP] + actress


def _keyword_related_sort_key(item: dict | None) -> tuple:
    """Order inside the keyword bucket: distinctive theme, then hit count.

    A ママ-only row must not outrank 家庭教師 / 肉欲教育 / 10秒挿入 just because
    the catalog score was high. More hits still win among the same tier.
    Relation phrases sit ahead of bare kinship when theme hits are tied at zero.
    """
    if not isinstance(item, dict):
        return (0, 0, 0)
    hits = int(item.get("keyword_hits") or 0)
    matched = item.get("matched_keywords") or item.get("hit_keywords") or []
    if not isinstance(matched, list):
        matched = []
    theme = 0
    compound = 0
    for raw in matched:
        tok = str(raw or "").strip()
        if not tok:
            continue
        if _is_auto_theme_keyword(tok):
            theme += 1
        if _is_relation_phrase(tok):
            compound += 1
    return (1 if theme else 0, hits, 1 if compound else 0)


def _merge_unique_urls(primary, secondary, *, cap: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in list(primary or []) + list(secondary or []):
        s = str(u or "").strip()
        if not s or is_now_printing_url(s) or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _merge_related_for_cache(prev_items, new_items) -> list:
    """Union by code: keep existing good fields, append new codes, then cap 5+5+3."""
    by_key: dict = {}
    order: list[str] = []
    for it in list(prev_items or []) + list(new_items or []):
        if not isinstance(it, dict):
            continue
        k = _related_cache_item_key(it)
        if not k:
            continue
        if k not in by_key:
            by_key[k] = dict(it)
            order.append(k)
            continue
        merged = dict(by_key[k])
        incoming = dict(it)
        for field in (
            "title",
            "title_zh",
            "actress",
            "cover",
            "cid",
            "line",
            "why",
            "keyword_hits",
            "matched_keywords",
        ):
            if not merged.get(field) and incoming.get(field):
                merged[field] = incoming[field]
        if incoming.get("stills"):
            merged["stills"] = _merge_unique_urls(merged.get("stills"), incoming.get("stills"))
        by_key[k] = merged
    return _cap_related_buckets([by_key[k] for k in order])


def _item_needs_title_zh(item: dict | None) -> bool:
    """True when a coded work is missing title_zh. Never invent Chinese."""
    if not isinstance(item, dict):
        return False
    code = item.get("code")
    if not code or str(code) in ("TITLE-SEARCH", "片名搜尋") or not parse_code_parts(str(code)):
        return False
    return not str(item.get("title_zh") or "").strip()


def _payload_needs_title_zh(payload: dict) -> bool:
    """True when main or any coded related slide is missing title_zh."""
    if not isinstance(payload, dict):
        return False
    if _item_needs_title_zh(payload):
        return True
    for key in ("related_by_title", "related"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if _item_needs_title_zh(item):
                return True
    return False


def _related_needs_backfill(payload: dict) -> bool:
    """True when a related bucket is under its cap and we have a way to fill it."""
    rel = payload.get("related_by_title") or payload.get("related") or []
    if not isinstance(rel, list):
        rel = []
    t, k, a = _related_bucket_counts(rel)
    title = str(payload.get("title") or "")
    actress = str(payload.get("actress") or "").strip()
    if t < RELATED_THEME_CAP and is_usable_title(title):
        return True
    if k < RELATED_KEYWORD_CAP and is_usable_title(title):
        return True
    if a < RELATED_ACTRESS_CAP and actress:
        return True
    return False


def _payload_stills_need_fill(payload: dict) -> bool:
    """True when main or a related item with cid is under STILLS_TARGET."""
    if not isinstance(payload, dict):
        return False
    stills = payload.get("stills") if isinstance(payload.get("stills"), list) else []
    cid = str(payload.get("cid") or "").strip()
    if cid and len(_merge_unique_urls(stills, [], cap=20)) < STILLS_TARGET:
        return True
    for item in payload.get("related_by_title") or payload.get("related") or []:
        if not isinstance(item, dict):
            continue
        icid = str(item.get("cid") or "").strip()
        if not icid:
            continue
        istills = item.get("stills") if isinstance(item.get("stills"), list) else []
        if len(_merge_unique_urls(istills, [], cap=20)) < STILLS_TARGET:
            return True
    return False


def _payload_needs_enrichment(payload: dict) -> bool:
    """Fingerprint of remaining gaps — skip network only when nothing is left to fill."""
    return (
        _payload_needs_title_zh(payload)
        or _related_needs_backfill(payload)
        or _payload_stills_need_fill(payload)
    )


def _backfill_item_stills(item: dict) -> None:
    if not isinstance(item, dict):
        return
    existing = item.get("stills") if isinstance(item.get("stills"), list) else []
    kept = _merge_unique_urls(existing, [], cap=20)
    if len(kept) >= STILLS_TARGET:
        item["stills"] = kept
        return
    cid = str(item.get("cid") or "").strip()
    extra = still_urls(cid, STILLS_TARGET) if cid else []
    item["stills"] = _merge_unique_urls(kept, extra, cap=20)


def _backfill_main_stills(payload: dict) -> None:
    """Keep existing stills; only add unique CDN URLs if under STILLS_TARGET."""
    _backfill_item_stills(payload)


def _payload_enrichment_fingerprint(payload: dict) -> tuple:
    rel = payload.get("related_by_title") if isinstance(payload.get("related_by_title"), list) else []
    keys = tuple(
        _related_cache_item_key(x) or ""
        for x in rel
        if isinstance(x, dict)
    )
    zh = tuple(str((x or {}).get("title_zh") or "") for x in rel if isinstance(x, dict))
    return (
        str(payload.get("title_zh") or ""),
        str(payload.get("title") or ""),
        str(payload.get("actress") or ""),
        tuple(str(k) for k in (payload.get("theme_keywords") or [])),
        tuple(str(u) for u in (payload.get("stills") or [])),
        keys,
        zh,
    )


def _backfill_catalog_identity(payload: dict) -> dict:
    """Fill a missing actress (and title) from the code catalog.

    A code-only cache hit used to skip the catalog, so 同女優 never ran and
    the payload kept whatever actress the old entry had — often nothing.
    """
    if not isinstance(payload, dict):
        return payload
    code = str(payload.get("code") or "").strip()
    if not code or str(code) == "TITLE-SEARCH" or not parse_code_parts(code):
        return payload
    need_actress = not str(payload.get("actress") or "").strip()
    need_title = not is_usable_title(str(payload.get("title") or ""))
    if not need_actress and not need_title:
        return payload
    try:
        meta = fetch_avbase_by_code(format_display_code(code))
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        return payload
    if need_title and str(meta.get("title") or "").strip():
        payload["title"] = str(meta.get("title")).strip()
    if need_actress and str(meta.get("actress") or "").strip():
        payload["actress"] = str(meta.get("actress")).strip()
    if not str(payload.get("studio") or "").strip() and meta.get("studio"):
        payload["studio"] = meta.get("studio")
    return payload


def enrich_offline_cache_hit(payload: dict, *, image_hash: str | None = None) -> dict:
    """Incremental backfill for a cache/history hit, then merge-put.

    Keep existing cover/stills/related. Only fill gaps: missing title_zh,
    related buckets under 5/5/3, extra still URLs if under target.
    Skip network for buckets already at cap.

    cache_backfilled / chinese_titles_attached mean "attempted this request"
    only — they must not freeze an incomplete payload on later re-query.
    """
    if not isinstance(payload, dict):
        return payload
    before_identity = _payload_enrichment_fingerprint(payload)
    try:
        _backfill_catalog_identity(payload)
    except Exception:
        pass
    _recompute_theme_keywords(payload)
    if not _payload_needs_enrichment(payload):
        try:
            if _payload_enrichment_fingerprint(payload) != before_identity:
                offline_cache_put(payload, image_hash=image_hash)
        except Exception:
            pass
        _finalize_related_note(payload)
        return payload
    payload.setdefault(
        "related_by_title",
        payload.get("related_by_title") or payload.get("related") or [],
    )
    payload.setdefault("related", payload.get("related") or [])
    before = _payload_enrichment_fingerprint(payload)
    try:
        _backfill_item_stills(payload)
        for item in payload.get("related_by_title") or []:
            _backfill_item_stills(item)
    except Exception:
        pass
    try:
        if _related_needs_backfill(payload):
            rel = [x for x in (payload.get("related_by_title") or []) if isinstance(x, dict)]
            t, k, a = _related_bucket_counts(rel)
            filled = find_related_by_title(
                payload.get("title"),
                exclude_code=str(payload.get("code") or "") if payload.get("code") else None,
                max_n=RELATED_THEME_CAP,
                actress=payload.get("actress"),
                budget_sec=OFFLINE_CACHE_RELATED_BUDGET,
                seed=rel,
                fill_theme=t < RELATED_THEME_CAP,
                fill_keyword=k < RELATED_KEYWORD_CAP,
                fill_actress=a < RELATED_ACTRESS_CAP,
            )
            payload["related_by_title"] = _merge_related_for_cache(rel, filled)
        else:
            payload["related_by_title"] = _cap_related_buckets(
                payload.get("related_by_title") or []
            )
    except Exception:
        pass
    try:
        if _payload_needs_title_zh(payload):
            attach_chinese_titles(
                payload,
                related_network=True,
                related_budget_sec=OFFLINE_CACHE_TITLE_ZH_BUDGET,
            )
    except Exception:
        pass
    # Attempt markers for this request only — next open still checks gaps.
    payload["cache_backfilled"] = True
    payload["chinese_titles_attached"] = True
    _recompute_theme_keywords(payload)
    _finalize_related_note(payload)
    try:
        if _payload_enrichment_fingerprint(payload) != before:
            offline_cache_put(payload, image_hash=image_hash)
    except Exception:
        pass
    return payload


def _offline_cache_payload_ok(payload: dict) -> bool:
    """Cache only gallery-ready hits: valid 品番 + a real (non-now_printing) cover.

    Title-only / Gemini-only failures must not be stored, or re-query would
    stick on 「離線快取」 with no photos.
    """
    if not isinstance(payload, dict) or not payload.get("ok"):
        return False
    code = payload.get("code")
    if not code or str(code) == "TITLE-SEARCH" or not parse_code_parts(str(code)):
        return False
    return usable_cover_url(payload.get("cover"))


def _offline_cache_drop_entry(data: dict, entry_id: str | None) -> None:
    """Remove an entry and every index key that points at it."""
    if not entry_id:
        return
    entries = data.get("entries") or {}
    entries.pop(entry_id, None)
    by_key = data.get("by_key") or {}
    dead = [k for k, v in by_key.items() if v == entry_id]
    for k in dead:
        by_key.pop(k, None)


def _offline_cache_build_value(payload: dict) -> dict:
    code = payload.get("code")
    display = format_display_code(str(code)) if code and parse_code_parts(str(code)) else code
    stills = payload.get("stills") if isinstance(payload.get("stills"), list) else []
    return {
        "ok": True,
        "code": display,
        "title": payload.get("title"),
        "title_zh": payload.get("title_zh"),
        "actress": payload.get("actress"),
        "studio": payload.get("studio"),
        "cid": payload.get("cid"),
        "cover": payload.get("cover") if usable_cover_url(payload.get("cover")) else None,
        "stills": list(stills)[:20],
        "related_by_title": _slim_related_for_cache(
            payload.get("related_by_title") or payload.get("related")
        ),
        "theme_keywords": list((_recompute_theme_keywords(payload).get("theme_keywords")) or []),
        "keyword_queries": list(payload.get("keyword_queries") or []),
        "message": payload.get("message") or "",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _offline_cache_read_unlocked(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "by_key": {}, "entries": {}}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return {"version": 1, "by_key": {}, "entries": {}}
        data.setdefault("version", 1)
        data.setdefault("by_key", {})
        data.setdefault("entries", {})
        if not isinstance(data["by_key"], dict):
            data["by_key"] = {}
        if not isinstance(data["entries"], dict):
            data["entries"] = {}
        return data
    except Exception:
        return {"version": 1, "by_key": {}, "entries": {}}


def _offline_cache_write_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _offline_cache_prune(data: dict, max_n: int = OFFLINE_CACHE_MAX) -> None:
    entries = data.get("entries") or {}
    if len(entries) <= max_n:
        return
    ranked = sorted(
        entries.items(),
        key=lambda kv: float((kv[1] or {}).get("touched_at") or 0),
    )
    drop_ids = {eid for eid, _ in ranked[: max(0, len(entries) - max_n)]}
    for eid in drop_ids:
        entries.pop(eid, None)
    by_key = data.get("by_key") or {}
    dead = [k for k, v in by_key.items() if v in drop_ids]
    for k in dead:
        by_key.pop(k, None)


def offline_cache_get(
    *,
    code: str | None = None,
    image_hash: str | None = None,
) -> dict | None:
    """Return a gallery-ready payload from offline cache, or None."""
    keys = _offline_cache_index_keys(code=code, image_hash=image_hash)
    if not keys:
        return None
    path = _offline_cache_resolve_path()
    with _OFFLINE_CACHE_MEM_LOCK:
        try:
            with open(path, "a+", encoding="utf-8") as lockf:
                try:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                data = _offline_cache_read_unlocked(path)
                by_key = data.get("by_key") or {}
                entries = data.get("entries") or {}
                dirty = False
                entry_id = None
                for k in keys:
                    entry_id = by_key.get(k)
                    if entry_id and entry_id in entries:
                        break
                    if entry_id and entry_id not in entries:
                        by_key.pop(k, None)
                        dirty = True
                    entry_id = None
                if not entry_id:
                    if dirty:
                        try:
                            _offline_cache_write_unlocked(path, data)
                        except Exception:
                            pass
                    return None
                entry = entries.get(entry_id)
                check = dict(entry) if isinstance(entry, dict) else {}
                check.setdefault("ok", True)
                if not isinstance(entry, dict) or not _offline_cache_payload_ok(check):
                    # Incomplete / now_printing / title-only: treat as miss and purge
                    _offline_cache_drop_entry(data, entry_id)
                    try:
                        _offline_cache_write_unlocked(path, data)
                    except Exception:
                        pass
                    return None
                # touch for LRU
                entry["touched_at"] = time.time()
                entries[entry_id] = entry
                try:
                    _offline_cache_write_unlocked(path, data)
                except Exception:
                    pass
                out = dict(entry)
                out.pop("touched_at", None)
                out["ok"] = True
                out["from_offline_cache"] = True
                msg = str(out.get("message") or "").strip()
                if "離線快取" not in msg:
                    out["message"] = "離線快取" + (f"：{msg}" if msg else "")
                else:
                    out["message"] = msg or "離線快取"
                out.setdefault("related", [])
                out.setdefault("stills", out.get("stills") or [])
                out.setdefault("related_by_title", out.get("related_by_title") or [])
                _recompute_theme_keywords(out)
                return out
        except Exception:
            return None


def offline_cache_put(payload: dict, *, image_hash: str | None = None) -> None:
    """Persist a successful identify result for later offline hits."""
    if not _offline_cache_payload_ok(payload):
        return
    code = str(payload.get("code") or "")
    entry_id = _offline_cache_entry_id(code)
    if not entry_id:
        return
    value = _offline_cache_build_value(payload)
    value["touched_at"] = time.time()
    index_keys = _offline_cache_index_keys(code=code, image_hash=image_hash)
    # Prefer catalog display form already in value
    if value.get("code"):
        for k in _offline_cache_index_keys(code=str(value["code"])):
            if k not in index_keys:
                index_keys.append(k)
    path = _offline_cache_resolve_path()
    with _OFFLINE_CACHE_MEM_LOCK:
        try:
            with open(path, "a+", encoding="utf-8") as lockf:
                try:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                data = _offline_cache_read_unlocked(path)
                entries = data.setdefault("entries", {})
                by_key = data.setdefault("by_key", {})
                # merge: keep richer title/cover if new is thinner; never wipe stills/cover
                prev = entries.get(entry_id)
                if isinstance(prev, dict):
                    for field in (
                        "title",
                        "title_zh",
                        "actress",
                        "studio",
                        "cid",
                        "theme_keywords",
                        "keyword_queries",
                    ):
                        if field in ("theme_keywords", "keyword_queries"):
                            # Recomputed from the current title. Do not restore a
                            # stale chip list (lone 家庭教師, VOL/OL junk).
                            continue
                        if not value.get(field) and prev.get(field):
                            value[field] = prev[field]
                    if not usable_cover_url(value.get("cover")) and usable_cover_url(prev.get("cover")):
                        value["cover"] = prev.get("cover")
                    value["stills"] = _merge_unique_urls(prev.get("stills"), value.get("stills"), cap=20)
                    value["related_by_title"] = _merge_related_for_cache(
                        prev.get("related_by_title"),
                        value.get("related_by_title"),
                    )
                if not usable_cover_url(value.get("cover")):
                    # Do not persist title-only / now_printing after merge
                    return
                entries[entry_id] = value
                for k in index_keys:
                    by_key[k] = entry_id
                _offline_cache_prune(data, OFFLINE_CACHE_MAX)
                _offline_cache_write_unlocked(path, data)
        except Exception:
            return


def image_content_hash(image_bytes: bytes | None) -> str | None:
    if not image_bytes:
        return None
    try:
        return hashlib.sha256(image_bytes).hexdigest()
    except Exception:
        return None


def _fetch_titled_meta_for_code(fetcher, display: str) -> dict | None:
    """Call a code→meta fetcher with display first, then stripped-zero slug."""
    if not callable(fetcher):
        return None
    slugs = [display]
    alt = code_stripped_form(display)
    if alt:
        slugs.append(alt)
    last = None
    for slug in slugs:
        try:
            m = fetcher(slug)
        except Exception:
            m = None
        if m:
            last = m
            if (m.get("title") or "").strip():
                return m
    return last


def identify_code(
    code: str,
    ocr_preview: str | None = None,
    vision_meta: dict | None = None,
) -> dict:
    display = format_display_code(code)
    parts = parse_code_parts(display)
    if not parts:
        return apply_vision_meta(
            {
                "ok": False,
                "code": display,
                "title": None,
                "actress": None,
                "studio": None,
                "cid": None,
                "cover": None,
                "stills": [],
                "related": [],
                "ocr_text_preview": ocr_preview,
                "message": "番號格式無效，請用如 MIDA-616",
            },
            vision_meta,
        )

    # Offline cache hit — skip slow network (manual user_code benefits too)
    try:
        cached = offline_cache_get(code=display)
    except Exception:
        cached = None
    if cached and cached.get("ok"):
        cached = dict(cached)
        cached["ocr_text_preview"] = ocr_preview
        cached.setdefault("related", [])
        cached.setdefault("related_note", None)
        cached.setdefault("related_by_title", cached.get("related_by_title") or [])
        # Still fill missing title_zh (main + related); live path used to skip this
        cached = enrich_offline_cache_hit(cached)
        _finalize_related_note(cached)
        return apply_vision_meta(cached, vision_meta)

    cid = code_to_cid(display)
    related: list[dict] = []
    related_note = None
    title = None
    actress = None
    studio = None
    message = None

    # Demo MIDA-616 path: full package
    if is_mida616(display):
        demo_raw = lookup_demo(display) or {
            "code": "MIDA-616",
            "title": "彼女の妹のノーブラ誘惑に負け巨乳ナマ乳沼に溺れたサイテーなボク",
            "actress": "福田ゆあ",
            "studio": "MOODYZ DIVA",
            "cid": "mida00616",
        }
        main = work_payload(demo_raw, why="主作品（示範包）")
        related = related_from_demo()
        out = {
            "ok": True,
            "code": main["code"],
            "title": main["title"],
            "actress": main["actress"],
            "studio": main["studio"],
            "cid": main["cid"],
            "cover": main["cover"],
            "stills": main["stills"],
            "related": related,
            "ocr_text_preview": ocr_preview,
            "message": "示範包：主作品＋主題＋女優",
            "related_note": None,
        }
        # Vision title preferred over demo/OCR when present
        _recompute_theme_keywords(out)
        return apply_vision_meta(out, vision_meta)

    # Other demo hits
    demo_raw = lookup_demo(display)
    if demo_raw:
        main = work_payload(demo_raw)
        out = {
            "ok": True,
            "code": main["code"],
            "title": main["title"],
            "actress": main["actress"],
            "studio": main["studio"],
            "cid": main["cid"],
            "cover": main["cover"],
            "stills": main["stills"],
            "related": [],
            "ocr_text_preview": ocr_preview,
            "message": "示範包內單一部作品",
            "related_note": "相關推薦目前僅 MIDA-616 路徑會自動帶入主題＋女優。",
        }
        _recompute_theme_keywords(out)
        return apply_vision_meta(out, vision_meta)

    # Online lookup: demo → JAVLibrary → JavBus → DDG(fast) → Gemini text → CDN-only.
    # Soft deadline: stop online title attempts after IDENTIFY_ONLINE_BUDGET seconds.
    import time as _time

    meta = None
    timed_out = False
    t_online = _time.monotonic()

    def _has_title(m: dict | None) -> bool:
        return bool(m and (m.get("title") or "").strip())

    def _budget_left() -> float:
        return IDENTIFY_ONLINE_BUDGET - (_time.monotonic() - t_online)

    def _merge_title(src_meta: dict | None, label: str) -> None:
        nonlocal meta
        if not src_meta:
            return
        if _has_title(src_meta):
            if not meta:
                meta = src_meta
            else:
                meta = {
                    **meta,
                    "title": src_meta.get("title"),
                    "actress": meta.get("actress") or src_meta.get("actress"),
                    "studio": meta.get("studio") or src_meta.get("studio"),
                    "source": f"{meta.get('source') or 'web'}+{label}",
                }
                if src_meta.get("cid") and not meta.get("cid"):
                    meta["cid"] = src_meta["cid"]
                if src_meta.get("related") and not meta.get("related"):
                    meta["related"] = src_meta["related"]
        elif src_meta and not meta:
            meta = src_meta

    # AVBase first: exact 品番 page, then search (JAVLibrary/DDG helpers are flaky here).
    try:
        meta = fetch_avbase_by_code(display)
    except Exception:
        meta = None

    if not _has_title(meta) and _budget_left() > 0.5:
        try:
            meta = _fetch_titled_meta_for_code(fetch_javlibrary, display)
        except Exception:
            meta = meta
    if not _has_title(meta) and _budget_left() > 0.5:
        try:
            jb = _fetch_titled_meta_for_code(fetch_javbus, display)
        except Exception:
            jb = None
        _merge_title(jb, (jb or {}).get("source") or "javbus")

    if not _has_title(meta) and _budget_left() > 1.0:
        try:
            ddg = _fetch_titled_meta_for_code(fetch_duckduckgo, display)
        except Exception:
            ddg = None
        _merge_title(ddg, "duckduckgo")

    if not _has_title(meta) and _budget_left() > 2.0:
        try:
            gmeta = gemini_lookup_code_meta(display)
        except Exception:
            gmeta = None
        if _has_title(gmeta):
            if not meta:
                meta = {
                    "code": display,
                    "title": gmeta.get("title"),
                    "actress": gmeta.get("actress"),
                    "studio": gmeta.get("studio"),
                    "cid": cid,
                    "related": [],
                    "source": "gemini",
                }
            else:
                meta = {
                    **meta,
                    "title": gmeta.get("title"),
                    "actress": meta.get("actress") or gmeta.get("actress"),
                    "studio": meta.get("studio") or gmeta.get("studio"),
                    "source": f"{meta.get('source') or 'web'}+gemini",
                }
    elif not _has_title(meta) and _budget_left() <= 2.0:
        timed_out = True

    if meta:
        title = meta.get("title")
        actress = meta.get("actress")
        studio = meta.get("studio")
        if meta.get("cid"):
            cid = meta["cid"]
        related = meta.get("related") or []
        if not related:
            related_note = "線上目錄未取得同女優相關；僅顯示主作品 CDN。"
        message = f"來源：{meta.get('source') or 'web'}"
        if not (title and str(title).strip()):
            related_note = "已取得 CDN 封面，但無法解析標題（AVBase／JAVLibrary／JavBus／搜尋／Gemini 皆無結果）。"
            if timed_out:
                message = "僅 CDN（標題查詢逾時）— 已嘗試線上來源"
            else:
                message = "僅 CDN（無標題）— 已嘗試 AVBase、JAVLibrary、JavBus、DuckDuckGo、Gemini"
    else:
        title = None
        related_note = "已取得 CDN 封面，但無法解析標題（AVBase／JAVLibrary／JavBus／搜尋／Gemini 皆無結果）。"
        if timed_out:
            message = "僅 CDN（標題查詢逾時）— 已嘗試線上來源"
        else:
            message = "僅 CDN（無標題）— 已嘗試 AVBase、JAVLibrary、JavBus、DuckDuckGo、Gemini"

    # Prefer the catalog title. A different vision phrase must not replace it
    # or keep that work's Chinese line beside the other sentence.
    catalog_title = title
    if vision_meta:
        if vision_meta.get("title"):
            title = choose_display_title(title, vision_meta.get("title"))
            if message:
                message = f"看圖辨識＋{message}"
            else:
                message = "看圖辨識"
        if vision_meta.get("actress") and not actress:
            actress = vision_meta["actress"]
        if vision_meta.get("studio") and not studio:
            studio = vision_meta["studio"]
        # If no online title but vision has one, clear the CDN-only note tone
        if vision_meta.get("title") and (not meta or not _has_title(meta)):
            message = "看圖辨識（標題）＋ DMM CDN"
            related_note = related_note or "無法取得線上相關；僅顯示主作品 CDN。"

    cid, cover, stills = sanitize_cover_fields(
        code=display,
        cid=str(cid) if cid else None,
        cover=None,
    )

    title_zh = None
    try:
        # Prefer Chinese from online meta if present
        if meta and isinstance(meta, dict):
            title_zh = meta.get("title_zh")
        if (
            catalog_title
            and title
            and not _titles_are_same_phrase(str(catalog_title), str(title))
        ):
            title_zh = None
        title_zh = resolve_chinese_title(
            display,
            title_ja=title,
            existing_zh=title_zh,
        )
    except Exception:
        title_zh = None

    out = {
        "ok": True,
        "code": display,
        "title": title,
        "title_zh": title_zh,
        "actress": actress,
        "studio": studio,
        "cid": cid,
        "cover": cover,
        "stills": stills,
        "related": related,
        "ocr_text_preview": ocr_preview,
        "message": message,
        "related_note": related_note,
    }
    try:
        _backfill_catalog_identity(out)
    except Exception:
        pass
    _recompute_theme_keywords(out)
    _finalize_related_note(out)
    try:
        offline_cache_put(out)
    except Exception:
        pass
    return out


def empty_identify(
    *,
    ok: bool = False,
    message: str,
    ocr_preview: str | None = None,
    vision_used: bool = False,
    code: str | None = None,
    title: str | None = None,
    actress: str | None = None,
    studio: str | None = None,
    cover: str | None = None,
    stills: list | None = None,
    search_mode: str = "manual",
) -> dict:
    return {
        "ok": ok,
        "code": code,
        "title": title,
        "actress": actress,
        "studio": studio,
        "cid": None,
        "cover": cover,
        "stills": stills if stills is not None else [],
        "related": [],
        "ocr_text_preview": ocr_preview,
        "vision_used": vision_used,
        "search_mode": search_mode,
        "message": message,
    }


def title_only_payload(
    *,
    title: str,
    actress: str | None = None,
    studio: str | None = None,
    cover: str | None = None,
    ocr_preview: str | None = None,
    vision_used: bool = True,
    message: str | None = None,
) -> dict:
    """Partial ok response when we have a title but could not resolve a code.

    Never invent a cover URL — empty/missing cover means the UI shows a placeholder
    instead of a broken「載入失敗」image.
    """
    cover_s = (str(cover).strip() if cover else "") or None
    return {
        "ok": True,
        "code": "TITLE-SEARCH",
        "title": title,
        "actress": actress,
        "studio": studio,
        "cid": None,
        "cover": cover_s,
        "stills": [cover_s] if cover_s else [],
        "related": [],
        "candidates": [],
        "ocr_text_preview": ocr_preview,
        "vision_used": vision_used,
        "search_mode": "title",
        "message": message
        or (
            f"以片名搜尋：「{title}」。未解析出番號；可手動輸入番號以補齊封面／劇照。"
        ),
        "related_note": "片名搜尋未解析番號；可手動輸入番號以豐富結果。",
        "needs_code": True,
    }



def _candidate_titles_collide(cands: list | None) -> bool:
    """True when two catalog titles are the same series template."""
    titles: list[str] = []
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        t = re.sub(r"\s+", "", str(c.get("title") or ""))
        if len(t) >= 8:
            titles.append(t)
    if len(titles) < 2:
        return False
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            if titles[i][:10] and titles[i][:10] == titles[j][:10]:
                return True
            try:
                if title_similarity(titles[i], titles[j]) >= 0.72:
                    return True
            except Exception:
                continue
    return False


def _norm_gray(im: Image.Image, w: int, h: int) -> list[float]:
    small = im.convert("L").resize((w, h), Image.Resampling.BILINEAR)
    pix = list(small.getdata())
    n = float(len(pix) or 1)
    mean = sum(pix) / n
    var = sum((p - mean) ** 2 for p in pix) / n
    std = var ** 0.5 or 1.0
    return [(p - mean) / std for p in pix]


def _gray_corr(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / float(len(a))


def _figure_crop(img: Image.Image) -> Image.Image:
    """Person region: face, body, clothes, expression. Title bars stay out."""
    w, h = img.size
    x0, y0 = int(w * 0.16), int(h * 0.18)
    x1, y1 = int(w * 0.84), int(h * 0.90)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img
    return img.crop((x0, y0, x1, y1))


def _jacket_front_views(cover: Image.Image, user: Image.Image) -> list[Image.Image]:
    """Whole slide, plus the front panel when the catalog image is a wide package.

    A full jacket is back + spine + front. The upload is often only the front.
    Comparing the uncropped slide to that crop scores the spine and the back
    and rejects the right volume.
    """
    cw, ch = cover.size
    if cw < 16 or ch < 16:
        return [cover]
    views = [cover]
    cover_aspect = cw / float(ch)
    user_aspect = user.size[0] / float(max(user.size[1], 1))
    if cover_aspect > max(1.35, user_aspect * 1.45):
        # Package art is usually back | spine | front, with the front on the right.
        views.append(cover.crop((int(cw * 0.62), 0, cw, ch)))
        views.append(cover.crop((cw // 2, 0, cw, ch)))
        views.append(cover.crop((0, 0, max(16, int(cw * 0.40)), ch)))
    return views


_BATCH = threading.local()


def _enter_batch_ctx(deadline: float) -> None:
    _BATCH.active = True
    _BATCH.deadline = deadline
    _BATCH.jacket_incomplete = False


def _leave_batch_ctx() -> None:
    for name in ("active", "deadline", "jacket_incomplete"):
        if hasattr(_BATCH, name):
            delattr(_BATCH, name)


def _batch_deadline() -> float | None:
    if not getattr(_BATCH, "active", False):
        return None
    return getattr(_BATCH, "deadline", None)


def _seconds_left(deadline: float | None) -> float:
    if deadline is None:
        return 1e9
    return float(deadline) - time.monotonic()


def _aligned_jacket_scores(user: Image.Image, cover: Image.Image) -> tuple[float, float]:
    """Frame score, then the person/clothes score at that same window.

    The catalog front is scaled and slid until its framing matches the upload.
    The second score is only the figure crop (face, body, clothes, expression),
    so a shared series layout does not count as the same outfit.

    The upload is normalized once. Each window still uses the same 36×36
    correlation as before, so same_work / match_clothes thresholds stay put.
    """
    uw, uh = user.size
    if uw < 8 or uh < 8:
        return 0.0, 0.0
    user_fig = _figure_crop(user)
    user_fig_norm = _norm_gray(user_fig, 28, 28)
    win_h = 72
    win_w = max(12, int(round(uw * (win_h / float(uh)))))
    ua = _norm_gray(user.resize((win_w, win_h), Image.Resampling.BILINEAR), 36, 36)
    best_frame = 0.0
    best_figure = 0.0
    for src in _jacket_front_views(cover, user):
        sw, sh = src.size
        if sw < 8 or sh < 8:
            continue
        for zoom in (1.0, 1.55):
            src_h = max(win_h, int(round(win_h * zoom)))
            src_w = max(win_w, int(round(sw * (src_h / float(sh)))))
            src_r = src.resize((src_w, src_h), Image.Resampling.BILINEAR)
            step_x = max(6, (src_w - win_w) // 5 or 1)
            step_y = max(6, (src_h - win_h) // 3 or 1)
            for y in range(0, max(1, src_h - win_h + 1), step_y):
                for x in range(0, max(1, src_w - win_w + 1), step_x):
                    patch = src_r.crop((x, y, x + win_w, y + win_h))
                    frame = _gray_corr(ua, _norm_gray(patch, 36, 36))
                    if frame <= best_frame:
                        continue
                    best_frame = frame
                    best_figure = _gray_corr(
                        user_fig_norm,
                        _norm_gray(_figure_crop(patch), 28, 28),
                    )
                    if best_frame >= 0.93 and best_figure >= 0.72:
                        return best_frame, best_figure
    return best_frame, best_figure


def _jacket_similarity(user: Image.Image, cover: Image.Image) -> float:
    """How well the upload sits on this jacket after front crop-align."""
    frame, _figure = _aligned_jacket_scores(user, cover)
    return frame


def _fetch_jacket_blob(cand: dict, deadline: float | None) -> tuple[dict, bytes | None, bool]:
    """Download one candidate jacket at the full cover timeout.

    The third value is True when the deadline stopped us before this cover
    was fetched. That is not a 404: the lock must not keep the other volumes.
    """
    need = float(COVER_DOWNLOAD_TIMEOUT) + 0.4
    if _seconds_left(deadline) < need:
        return cand, None, True
    try:
        disp = format_display_code(str(cand.get("code")))
        cid = str(cand.get("cid") or "") or (code_to_cid(disp) or "")
        url = str(cand.get("cover") or "") or (cover_url(cid) if cid else "")
        blob = download_cover_bytes(url, timeout=COVER_DOWNLOAD_TIMEOUT) if url else None
        if not blob and cid:
            if _seconds_left(deadline) < need:
                return cand, None, True
            alt = cover_url(cid)
            if alt and alt != url:
                blob = download_cover_bytes(alt, timeout=COVER_DOWNLOAD_TIMEOUT)
        return cand, blob, False
    except Exception:
        return cand, None, False


def _jacket_lock_winner(user_image_bytes: bytes | None, candidates: list | None) -> dict | None:
    """Pick a candidate whose jacket matches the upload. No API key required.

    A series template (six APGH volumes, one title) must not keep whichever
    row the catalog listed first. Lock only on a clear margin.
    """
    if getattr(_BATCH, "active", False):
        _BATCH.jacket_incomplete = False
    if not user_image_bytes:
        return None
    coded = [c for c in (candidates or []) if isinstance(c, dict) and c.get("code") and parse_code_parts(str(c.get("code")))]
    if len(coded) < 2:
        return None
    try:
        user = Image.open(io.BytesIO(user_image_bytes))
        user = ImageOps.exif_transpose(user).convert("RGB")
    except Exception:
        return None
    if min(user.size) < 64:
        return None
    deadline = _batch_deadline()
    pool = coded[:12]
    fetched: list[tuple[dict, bytes | None, bool]]
    if len(pool) >= 3:
        workers = min(4, len(pool))
        with ThreadPoolExecutor(max_workers=workers) as pool_ex:
            fetched = list(pool_ex.map(lambda cand: _fetch_jacket_blob(cand, deadline), pool))
    else:
        fetched = [_fetch_jacket_blob(cand, deadline) for cand in pool]
    scored: list[tuple[float, float, dict]] = []
    aborted = False
    incomplete = False
    for cand, blob, was_aborted in fetched:
        if was_aborted:
            # A cover we never fetched is not a miss. Do not lock on the rest.
            aborted = True
            break
        if not blob:
            continue
        if _seconds_left(deadline) < 0.35:
            # A partial compare must not lock: a later volume could still win.
            incomplete = True
            break
        try:
            cover = Image.open(io.BytesIO(blob))
            cover = ImageOps.exif_transpose(cover).convert("RGB")
        except Exception:
            continue
        frame, figure = _aligned_jacket_scores(user, cover)
        scored.append((frame, figure, cand))
    if aborted or incomplete:
        if getattr(_BATCH, "active", False):
            _BATCH.jacket_incomplete = True
        return None
    if not scored:
        return None
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    best_s, best_fig, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    # same_work is the aligned front. match_clothes is the person region
    # (face, body, clothes, expression). A series template can share the
    # first and still fail the second. Text rank must not fill that gap.
    same_work = best_s >= 0.70 and (best_s - second) >= 0.12
    match_clothes = best_fig >= 0.58
    if same_work and match_clothes:
        winner = dict(best)
        winner["jacket_score"] = best_s
        winner["jacket_figure"] = best_fig
        winner["visual"] = {
            "same_work": True,
            "match_person": True,
            "match_face": best_fig >= 0.70,
            "match_clothes": True,
            "match_accessories": best_fig >= 0.64,
            "match_pose": best_s >= 0.75,
            "confidence": best_s,
        }
        return winner
    return None


def _ocr_code_survives_title_mismatch(verify_meta: dict | None, image_bytes: bytes | None) -> tuple[bool, float | None]:
    """Keep a printed 品番 when its jacket is this picture, despite a bad OCR title."""
    if not isinstance(verify_meta, dict) or verify_meta.get("ok"):
        return False, None
    if not image_bytes or not verify_meta.get("cover_ok"):
        return False, None
    score = _jacket_score_against_url(image_bytes, verify_meta.get("cover"))
    if score is not None and score >= 0.70:
        return True, score
    return False, score


def _jacket_score_against_url(image_bytes: bytes | None, url: str | None) -> float | None:
    """Jacket correlation, or None when the two pictures cannot be compared."""
    if not image_bytes or not url:
        return None
    try:
        blob = download_cover_bytes(str(url))
        if not blob:
            return None
        user = Image.open(io.BytesIO(image_bytes))
        user = ImageOps.exif_transpose(user).convert("RGB")
        cover = Image.open(io.BytesIO(blob))
        cover = ImageOps.exif_transpose(cover).convert("RGB")
        return float(_jacket_similarity(user, cover))
    except Exception:
        return None


def _upload_matches_cached_cover(image_bytes: bytes | None, cached: dict | None) -> bool:
    """False only when the cached jacket was compared and is a different picture.

    An earlier identify can be stored under this file's hash. Without a
    vision key that row would be replayed forever. A low jacket score means
    it is not this cover, so the title search runs again. A missing cover,
    a failed download, or bytes that are not an image are not a mismatch.
    """
    if not image_bytes or not isinstance(cached, dict):
        return False
    url = str(cached.get("cover") or "")
    if not url:
        return True
    score = _jacket_score_against_url(image_bytes, url)
    if score is None:
        return True
    return score >= 0.40


def _cluster_shares_title(rows: list | None, *, min_rows: int = 3, min_prefix: int = 8) -> bool:
    """True when several catalog rows are one series template, not unrelated hits."""
    titles: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"\s+", "", str(row.get("title") or ""))
        if len(title) >= min_prefix:
            titles.append(title)
    if len(titles) < min_rows:
        return False
    pref = titles[0]
    for title in titles[1:]:
        n = 0
        lim = min(len(pref), len(title))
        while n < lim and pref[n] == title[n]:
            n += 1
        pref = pref[:n]
        if len(pref) < min_prefix:
            return False
    return True


def _resolve_unnumbered_cover(queries: list[str] | None, image_bytes: bytes | None) -> dict | None:
    """No printed 品番: search title cues, then lock the jacket to one volume.

    A shared series title must not keep whichever row the catalog listed
    first. A short unique phrase does not override a jacket lock. When the
    jacket does not lock, a longer phrase that is actually in a catalog
    title is kept.
    """
    series_pool: list[dict] = []
    specific: list[tuple[float, int, dict]] = []
    seen_series: set[str] = set()
    seen_q: set[str] = set()
    for raw_q in queries or []:
        q = re.sub(r"\s+", " ", str(raw_q or "")).strip()
        key = re.sub(r"\s+", "", q)
        if len(key) < 4 or key in seen_q or not is_usable_title(q):
            continue
        seen_q.add(key)
        if len(seen_q) > 8:
            break
        try:
            found = search_by_title(q)
        except Exception:
            found = None
        if not isinstance(found, dict) or not _hit_has_catalog_code(found):
            continue
        rows = [c for c in (found.get("candidates") or []) if isinstance(c, dict) and c.get("code")]
        if not rows:
            rows = [found]
        if _cluster_shares_title(rows):
            for row in rows:
                code = format_display_code(str(row.get("code") or ""))
                if not code or code in seen_series:
                    continue
                seen_series.add(code)
                series_pool.append(row)
            continue
        best = rows[0]
        title = re.sub(r"\s+", "", str(best.get("title") or ""))
        if len(key) >= 4 and (key in title or title_similarity(key, title) >= 0.55):
            specific.append((title_similarity(key, str(best.get("title") or "")), len(key), found))
    pool = list(series_pool)
    seen_pool = set(seen_series)
    for _score, _ln, hit in specific:
        for row in [hit] + list(hit.get("candidates") or []):
            if not isinstance(row, dict) or not row.get("code"):
                continue
            code = format_display_code(str(row.get("code") or ""))
            if not code or code in seen_pool:
                continue
            seen_pool.add(code)
            pool.append(row)
    if image_bytes and len(pool) >= 2:
        try:
            winner = _jacket_lock_winner(image_bytes, pool[:12])
        except Exception:
            winner = None
        if isinstance(winner, dict) and winner.get("code"):
            code = format_display_code(str(winner.get("code")))
            rest = [
                c
                for c in pool
                if format_display_code(str(c.get("code") or "")) != code
            ]
            hit = dict(winner)
            hit["candidates"] = [winner] + rest
            hit["visual_confident"] = True
            hit["series_unresolved"] = False
            hit["visual_meta"] = {
                "visual_ranked": True,
                "visual_lock": True,
                "mode": "jacket",
                "compared": min(len(pool), 12),
                "note": "封面與原圖鎖定",
            }
            return hit
        # The jacket compare was cut off. A short phrase must not pick the volume.
        if getattr(_BATCH, "jacket_incomplete", False):
            return None
    if specific:
        specific.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return specific[0][2]
    return None


def _hit_visually_locked(hit: dict | None) -> bool:
    if not isinstance(hit, dict):
        return False
    meta = hit.get("visual_meta") or {}
    if meta.get("visual_lock") or hit.get("visual_confident"):
        return True
    vm = ((hit.get("candidates") or [{}])[0] or {}).get("visual") if hit.get("candidates") else None
    return bool(isinstance(vm, dict) and vm.get("same_work") and vm.get("match_clothes"))


def _mark_lock_incomplete(hit: dict, coded: list) -> dict:
    """A multi-candidate compare was cut off. Do not keep or invent a volume."""
    out = dict(hit)
    out["lock_incomplete"] = True
    out["visual_confident"] = False
    out["visual_lock"] = False
    out["series_unresolved"] = len(coded) >= 2
    out["visual_meta"] = {
        "visual_ranked": False,
        "visual_lock": False,
        "compared": 0,
        "note": "同系列還沒比完，不鎖定番號",
        "mode": "incomplete",
    }
    return out


def apply_visual_rank_to_hit(
    hit: dict,
    user_image_bytes: bytes | None,
    *,
    api_key: str | None = None,
    visual_budget: float | None = None,
    deadline: float | None = None,
) -> dict:
    """Reorder/verify hit.candidates vs *original user image* (1+ coded candidates).

    Locks 番號/片名 onto the visual winner only when same_work + clothes/person
    gates pass (visual_confident). Otherwise still reorders by visual score but
    marks visual_confident=False so UI/message can show 未鎖定.

    visual_budget is ignored. A short budget must not run a partial rank:
    the batch marks that slot retryable instead.
    """
    if not hit or not user_image_bytes:
        return hit
    cands = list(hit.get("candidates") or [])
    if not cands and hit.get("code"):
        cands = [hit]
    coded = [c for c in cands if c.get("code") and parse_code_parts(str(c["code"]))]
    if len(coded) < 1:
        return hit
    jacket = None
    try:
        jacket = _jacket_lock_winner(user_image_bytes, coded)
    except Exception:
        jacket = None
    if isinstance(jacket, dict) and jacket.get("code"):
        code = format_display_code(str(jacket.get("code")))
        rest = [
            c
            for c in coded
            if format_display_code(str(c.get("code") or "")) != code
        ]
        winner = dict(jacket)
        prior = jacket.get("visual") if isinstance(jacket.get("visual"), dict) else {}
        winner["visual"] = {
            "same_work": bool(prior.get("same_work")),
            "match_person": bool(prior.get("match_person")),
            "match_face": bool(prior.get("match_face")),
            "match_clothes": bool(prior.get("match_clothes")),
            "match_accessories": bool(prior.get("match_accessories")),
            "match_pose": bool(prior.get("match_pose")),
            "confidence": float(jacket.get("jacket_score") or prior.get("confidence") or 0.9),
        }
        if not (winner["visual"]["same_work"] and winner["visual"]["match_clothes"]):
            jacket = None
            winner = None
    if isinstance(jacket, dict) and jacket.get("code") and isinstance(winner, dict):
        winner["visual_score"] = float(jacket.get("jacket_score") or 0.9)
        out = dict(hit)
        out["candidates"] = [winner] + rest
        out["code"] = winner.get("code")
        out["title"] = winner.get("title") or out.get("title")
        out["actress"] = winner.get("actress") or out.get("actress")
        out["studio"] = winner.get("studio") or out.get("studio")
        out["cover"] = winner.get("cover") or out.get("cover")
        out["cid"] = winner.get("cid") or out.get("cid")
        out["source"] = winner.get("source") or out.get("source")
        out["visual_best_code"] = code
        out["visual_confident"] = True
        out["series_unresolved"] = False
        out["visual_meta"] = {
            "visual_ranked": True,
            "visual_lock": True,
            "compared": len(coded),
            "note": "封面與原圖鎖定",
            "mode": "jacket",
        }
        return out
    if deadline is None:
        deadline = _batch_deadline()
    jacket_cut = bool(getattr(_BATCH, "jacket_incomplete", False))
    remain = _seconds_left(deadline)
    # A short tournament plus the unlocked promotion can copy the wrong volume.
    # Run the full compare, or mark the slot retryable.
    if jacket_cut or remain < float(VISUAL_COMPARE_BUDGET):
        if len(coded) >= 2:
            return _mark_lock_incomplete(hit, coded)
        return hit
    ranked, meta = rank_candidates_by_visual(
        user_image_bytes,
        coded,
        api_key=api_key,
        budget_s=VISUAL_COMPARE_BUDGET,
    )
    if not meta.get("visual_ranked"):
        hit = dict(hit)
        hit["visual_meta"] = meta
        # Several volumes, one title, and no picture lock: do not keep
        # whichever row the catalog listed first.
        if len(coded) >= 2 and _candidate_titles_collide(coded):
            hit["series_unresolved"] = True
            hit["visual_confident"] = False
        return hit
    best = ranked[0]
    out = dict(hit)
    # Candidates already sorted by visual score; expose that ordering as main
    out["candidates"] = ranked
    vm0 = best.get("visual") or {}
    locked = bool(vm0.get("same_work") and vm0.get("match_clothes"))
    # Always expose visual ranking; only *lock* identity fields when gates pass
    out["visual_meta"] = meta
    out["visual_best_code"] = format_display_code(str(best.get("code") or ""))
    out["score"] = (
        best.get("visual_score")
        if best.get("visual_score") is not None
        else best.get("score") or out.get("score")
    )
    if locked:
        out["code"] = best.get("code") or out.get("code")
        out["title"] = best.get("title") or out.get("title")
        out["actress"] = best.get("actress") or out.get("actress")
        out["studio"] = best.get("studio") or out.get("studio")
        out["cover"] = best.get("cover") or out.get("cover")
        out["cid"] = best.get("cid") or out.get("cid")
        out["source"] = best.get("source") or out.get("source")
    elif len(ranked) >= 2:
        # Multi-candidate: still promote visual best for display order, but keep
        # title-search code if visual did not confirm same_work.
        out["code"] = best.get("code") or out.get("code")
        out["title"] = best.get("title") or out.get("title")
        out["actress"] = best.get("actress") or out.get("actress")
        out["studio"] = best.get("studio") or out.get("studio")
        out["cover"] = best.get("cover") or out.get("cover")
        out["cid"] = best.get("cid") or out.get("cid")
        out["source"] = best.get("source") or out.get("source")
    top_vs = float(best.get("visual_score") or 0)
    second = float(ranked[1].get("visual_score") or 0) if len(ranked) > 1 else 0.0
    if locked and top_vs >= 0.55 and (len(ranked) < 2 or (top_vs - second) >= 0.18):
        out["visual_confident"] = True
    else:
        out["visual_confident"] = False
    if _candidate_titles_collide(out.get("candidates") or coded) and not _hit_visually_locked(out):
        out["series_unresolved"] = True
    return out


def merge_title_candidates(
    result: dict,
    hit: dict | None,
    query_title: str = "",
) -> dict:
    """Attach all distinct title-search codes as candidates / related gallery cards."""
    if not hit:
        return result
    raw_cands = hit.get("candidates") or []
    if not raw_cands and hit.get("code") and parse_code_parts(str(hit["code"])):
        raw_cands = [hit]
    enriched = [enrich_title_candidate(c, why="片名候選") for c in raw_cands]
    # Drop entries without a real code
    enriched = [e for e in enriched if e.get("code") and parse_code_parts(str(e["code"]))]
    # Prefer visual ranking order already on hit.candidates; else sort by score
    vmeta = (hit or {}).get("visual_meta") or {}
    if vmeta.get("visual_ranked"):
        # Keep order from hit (visually ranked); scores already rewritten
        pass
    else:
        enriched.sort(
            key=lambda e: float(e.get("visual_score") or e.get("score") or 0),
            reverse=True,
        )
    # Main must follow visual best when ranked, else hit/result code
    preferred = (
        hit.get("visual_best_code")
        or hit.get("code")
        or result.get("code")
        or ""
    )
    main_code = format_display_code(str(preferred)) if preferred else ""
    if main_code:
        # Rotate so main is first in candidates (related = rest)
        head = [e for e in enriched if format_display_code(str(e["code"])) == main_code]
        tail = [e for e in enriched if format_display_code(str(e["code"])) != main_code]
        enriched = head + tail if head else enriched
        # Align result main fields with visual winner
        if head:
            m0 = head[0]
            result["code"] = m0.get("code") or result.get("code")
            if m0.get("title"):
                result["title"] = m0.get("title")
            if m0.get("actress"):
                result["actress"] = m0.get("actress")
            if m0.get("studio"):
                result["studio"] = m0.get("studio")
            if m0.get("cover"):
                result["cover"] = m0.get("cover")
            if m0.get("cid"):
                result["cid"] = m0.get("cid")
            if m0.get("score") is not None:
                result["score"] = m0.get("score")
            if m0.get("stills"):
                result["stills"] = m0.get("stills")
    result["candidates"] = enriched
    if len(enriched) < 2:
        return result

    if not main_code:
        main_code = format_display_code(str(result.get("code") or hit.get("code") or ""))
    extras = [
        e for e in enriched if format_display_code(str(e["code"])) != main_code
    ]
    if not extras:
        return result

    prev = list(result.get("related") or [])
    # Candidate cards first; keep any theme/actress related after
    result["related"] = extras + prev
    q = (query_title or result.get("title") or hit.get("title") or "").strip()
    n = len(enriched)
    banner = f"片名「{q}」找到 {n} 個不同番號，已全部列出（請點選正確的）"
    vmeta = (hit or {}).get("visual_meta") or {}
    if vmeta.get("visual_ranked"):
        banner = banner + "；" + (vmeta.get("note") or "同系列可能混淆，已依人物／衣服／姿勢排序")
    prev_msg = (result.get("message") or "").strip()
    result["message"] = banner if not prev_msg else f"{banner} {prev_msg}"
    result["related_note"] = banner
    result["search_mode"] = "title"
    if vmeta:
        result["visual_meta"] = vmeta
    return result


def multi_candidate_payload(
    *,
    query_title: str,
    hit: dict,
    ocr_preview: str | None = None,
    vision_used: bool = False,
    extra_msg: str | None = None,
) -> dict:
    """Build an identify response listing every coded title candidate as gallery cards."""
    cands = hit.get("candidates") or [hit]
    enriched = [enrich_title_candidate(c, why="片名候選") for c in cands]
    enriched = [e for e in enriched if e.get("code") and parse_code_parts(str(e["code"]))]
    if not enriched:
        return title_only_payload(
            title=query_title,
            actress=hit.get("actress"),
            studio=hit.get("studio"),
            cover=None,
            ocr_preview=ocr_preview,
            vision_used=vision_used,
            message=(extra_msg + " " if extra_msg else "")
            + f"以片名搜尋：「{query_title}」。未解析出番號；可手動輸入番號以補齊封面／劇照。",
        )
    vmeta = (hit or {}).get("visual_meta") or {}
    preferred = (
        hit.get("visual_best_code")
        or hit.get("code")
        or (enriched[0].get("code") if enriched else "")
        or ""
    )
    main_code = format_display_code(str(preferred)) if preferred else ""
    if main_code:
        head = [e for e in enriched if format_display_code(str(e["code"])) == main_code]
        tail = [e for e in enriched if format_display_code(str(e["code"])) != main_code]
        enriched = head + tail if head else enriched
    main = enriched[0]
    extras = enriched[1:]
    n = len(enriched)
    q = query_title or main.get("title") or ""
    banner = f"片名「{q}」找到 {n} 個不同番號，已全部列出（請點選正確的）"
    if vmeta.get("visual_ranked"):
        banner = banner + "；" + (vmeta.get("note") or "同系列可能混淆，已依人物／衣服／姿勢排序")
    msg = banner
    if extra_msg:
        msg = f"{banner} {extra_msg}"
    out = {
        "ok": True,
        "code": main.get("code"),
        "title": main.get("title") or query_title,
        "actress": main.get("actress"),
        "studio": main.get("studio"),
        "cid": main.get("cid"),
        "cover": main.get("cover"),
        "stills": main.get("stills") or [],
        "related": extras,
        "candidates": enriched,
        "ocr_text_preview": ocr_preview,
        "vision_used": vision_used,
        "search_mode": "title",
        "message": msg,
        "related_note": banner,
        "score": main.get("score"),
    }
    if vmeta:
        out["visual_meta"] = vmeta
    return out


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "look-for-pic-web",
            "port": 8787,
            "gemini_configured": bool(get_gemini_api_key()),
        }
    )





def http_get(url: str, *, timeout: float = 10.0, headers: dict | None = None) -> str | None:
    """Lightweight GET returning text, or None on failure (for catalog scrapes)."""
    try:
        h = {"User-Agent": UA, "Accept-Language": "zh-TW,zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.5"}
        if headers:
            h.update(headers)
        r = requests.get(url, headers=h, timeout=(3, timeout), verify=False, allow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return None
        if "Just a moment" in r.text[:800]:
            return None
        return r.text
    except Exception:
        return None


def _looks_chinese_title(s: str | None) -> bool:
    """True when string is likely a Chinese title (not kanji-only Japanese).

    Prefer is_chinese_heavy_title; also accept Simplified-Chinese markers /
    common CN phrasing. Pure JP kanji compounds (羞恥電車) alone are NOT enough.
    """
    t = (s or "").strip()
    if len(t) < 2:
        return False
    try:
        if is_chinese_heavy_title(t):
            return True
    except Exception:
        pass
    kana = len(re.findall(r"[\u3040-\u30ff]", t))
    if kana >= 1:
        return False  # any kana → treat as Japanese primary title
    # Simplified-only characters strongly suggest ZH
    simplified_markers = "耻汉与齐车杀产发经总后东来对吗么这说开关时实际"
    if any(ch in t for ch in simplified_markers):
        han = len(re.findall(r"[\u4e00-\u9fff]", t))
        return han >= 2
    # CN function words / grammar particles uncommon in JP titles
    cn_hints = ("的", "了", "是", "被", "和", "与", "在", "她", "他", "我", "不", "人妻")
    if any(h in t for h in cn_hints):
        han = len(re.findall(r"[\u4e00-\u9fff]", t))
        return han >= 3
    return False


def _clean_title_zh(raw: str | None, *, title_ja: str | None = None, code: str | None = None) -> str | None:
    t = re.sub(r"\s+", " ", (raw or "").strip())
    if not t:
        return None
    # Strip site name suffixes only (avoid eating 品番 hyphens like NHDTC-099)
    t = re.sub(r"\s*[\|／/]\s*.{0,40}$", "", t).strip()
    t = re.sub(
        r"\s+[\-–—]\s*(MissAV|JAVLibrary|JavBus|AVBase|FANZA|DMM).*$",
        "",
        t,
        flags=re.I,
    ).strip()
    if code:
        variants = set()
        raw_code = str(code).strip()
        variants.add(raw_code)
        if parse_code_parts(raw_code):
            disp = format_display_code(raw_code)
            variants.add(disp)
            parts = parse_code_parts(raw_code)
            if parts:
                letter, num = parts[0], parts[1]
                variants.add(f"{letter}-{num}")
                variants.add(f"{letter}-{num.lstrip('0') or '0'}")
                variants.add(f"{letter}{num}")
                variants.add(f"{letter}{num.zfill(3)}")
                variants.add(f"{letter}{num.zfill(5)}")
        for v in sorted(variants, key=len, reverse=True):
            if not v:
                continue
            t = re.sub(re.escape(v), " ", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip(" -\u3000")
    if title_ja and t == title_ja.strip():
        return None
    if not _looks_chinese_title(t):
        return None
    if len(t) < 2 or len(t) > 80:
        return None
    return t


def fetch_missav_chinese_title(code: str) -> str | None:
    """Best-effort Chinese title from MissAV public HTML (no magnets)."""
    if not code or not parse_code_parts(str(code)):
        return None
    disp = format_display_code(str(code))
    slugs = [disp.lower()]
    alt = code_stripped_form(disp)
    if alt:
        slugs.append(alt.lower())
    host_bases = (
        "https://missav.ai",
        "https://missav.ws",
        "https://missav.live",
    )
    for slug in slugs:
        for host in host_bases:
            url = f"{host}/{slug}"
            html = http_get(url, timeout=8.0)
            if not html:
                continue
            # og:title / h1 often: "CODE 中文标题" or "中文标题 - CODE"
            for pat in (
                r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)',
                r'content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']',
                r"<h1[^>]*>(.*?)</h1>",
                r"<title[^>]*>([^<]+)</title>",
            ):
                m = re.search(pat, html, flags=re.I | re.S)
                if not m:
                    continue
                raw = re.sub(r"<[^>]+>", "", m.group(1))
                zh = _clean_title_zh(raw, code=disp)
                if zh:
                    return zh
    return None


def fetch_javlibrary_chinese_title(code: str) -> str | None:
    """Best-effort Chinese title from JAVLibrary CN search/detail."""
    from urllib.parse import quote

    if not code or not parse_code_parts(str(code)):
        return None
    disp = format_display_code(str(code))
    keywords = [disp]
    alt = code_stripped_form(disp)
    if alt:
        keywords.append(alt)
    for kw in keywords:
        url = f"https://www.javlibrary.com/cn/vl_searchbyid.php?keyword={quote(kw)}"
        html = http_get(url, timeout=8.0, headers={"Referer": "https://www.javlibrary.com/cn/"})
        if not html:
            continue
        # Direct detail redirect page
        tm = re.search(r'id="video_title".*?<a[^>]*>([^<]+)</a>', html, flags=re.I | re.S)
        if tm:
            zh = _clean_title_zh(tm.group(1), code=disp)
            if zh:
                return zh
        # Search result cards: title="CODE 中文..."
        for m in re.finditer(
            r'class="video"[^>]*>.*?title="([^"]+)"', html, flags=re.I | re.S
        ):
            raw = m.group(1)
            blob = re.sub(r"[\s\-]", "", raw).upper()
            if (
                disp.replace("-", "").upper() not in blob
                and disp.upper() not in raw.upper()
                and (not alt or alt.replace("-", "").upper() not in blob)
            ):
                # still accept if starts with code-ish
                if not re.match(re.escape(disp.split("-")[0]), raw, flags=re.I):
                    continue
            zh = _clean_title_zh(raw, code=disp)
            if zh:
                return zh
    return None


def resolve_chinese_title(
    code: str | None,
    *,
    title_ja: str | None = None,
    existing_zh: str | None = None,
    user_title: str | None = None,
) -> str | None:
    """Resolve Traditional/Simplified Chinese title from public catalogs.

    Sources (public HTML only): existing payload → user Chinese query →
    MissAV → JAVLibrary CN. No pirate/magnet links. Returns None if unavailable.
    """
    zh = _clean_title_zh(existing_zh, title_ja=title_ja, code=code)
    if zh:
        return zh
    if user_title and _looks_chinese_title(user_title):
        zh = _clean_title_zh(user_title, title_ja=title_ja, code=code)
        if zh:
            return zh
    # If the "Japanese" title is already Chinese-heavy, treat as zh and skip scrape
    if title_ja and _looks_chinese_title(title_ja) and not re.search(r"[\u3040-\u30ff]", title_ja):
        return None  # caller already showing Chinese as main title
    if not code or not parse_code_parts(str(code)):
        return None
    for fetcher in (fetch_missav_chinese_title, fetch_javlibrary_chinese_title):
        try:
            zh = fetcher(str(code))
        except Exception:
            zh = None
        if zh:
            return zh
    return None


def attach_chinese_titles(
    payload: dict,
    *,
    related_network: bool = True,
    related_budget_sec: float = 3.0,
) -> dict:
    """Fill title_zh on main work (and optionally related) when missing.

    related_network=True: also try title_zh for related slides, but with a hard
    wall-clock budget so MissAV/JAVLibrary stalls cannot wipe the whole identify.
    """
    import time as _time

    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    if code and (str(code) == "TITLE-SEARCH" or not parse_code_parts(str(code))):
        code = None
    if not payload.get("title_zh"):
        try:
            zh = resolve_chinese_title(
                str(code) if code else None,
                title_ja=payload.get("title"),
                existing_zh=payload.get("title_zh"),
                user_title=payload.get("user_title") or payload.get("query_title"),
            )
        except Exception:
            zh = None
        if zh:
            payload["title_zh"] = zh
    t_rel0 = _time.monotonic()
    for key in ("related_by_title", "related"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            # Normalize passthrough
            if item.get("title_zh"):
                item["title_zh"] = _clean_title_zh(
                    str(item.get("title_zh")),
                    title_ja=item.get("title"),
                    code=item.get("code"),
                ) or item.get("title_zh")
                continue
            if not related_network:
                continue
            if related_budget_sec and (_time.monotonic() - t_rel0) > float(related_budget_sec):
                break
            icode = item.get("code")
            if not icode or not parse_code_parts(str(icode)):
                continue
            try:
                zh = resolve_chinese_title(
                    str(icode),
                    title_ja=item.get("title"),
                    existing_zh=item.get("title_zh"),
                )
            except Exception:
                zh = None
            if zh:
                item["title_zh"] = zh
    return payload


def _title_related_keyword_queries(title: str) -> list[str]:
    """Build short keyword queries that still reflect the work title."""
    return _title_sibling_phrases(title)[:8]


# Relationship / pronoun fluff and bare action tokens.
# They may sit in the lexicon (彼女, 息子, ママ) but must not outrank theme nouns,
# and must not be primary keyword-search drivers when a stronger term exists.
# Bare 誘惑 is not a theme noun; it is kept only inside a compound (ノーブラ誘惑).
# 彼女 / 妹 stay weak for automatic search priority, but a title pattern like
# 彼女の妹 still exposes 彼女, 妹, and 彼女の妹 as selectable chips.
# 息子 / ママ are the same class: selectable kinship-role chips, weak for auto.
# Hiragana まま is not a chip (it means "still" / "as is"). Pronouns (ボク / 私)
# stay chips only inside kinshipのkinship, not as bare leftovers.
# 義妹 stays a short theme noun (see _SHORT_THEME_NOUNS).
_WEAK_THEME_TOKENS = frozenset(
    {
        "中出し",
        "顔射",
        "NTR",
        "SEX",
        "CA",
        "VR",
        "油",
        "彼女",
        "彼氏",
        "お姉さん",
        "人妻",
        "息子",
        "ママ",
        "妹",
        "姉",
        "兄",
        "弟",
        "私",
        "僕",
        "ボク",
        "俺",
        "君",
        "あなた",
        "誘惑",
        "負け",
        "負ける",
        "負けた",
        "負けちゃう",
        "拘束",
        "監禁",
        "調教",
        "開発",
        "開發",
        "下着",
        "会社",
    }
)

# Productive title suffixes. Noun + suffix is one theme when the noun is glued
# on (ノーブラ誘惑, 巨乳沼, 肉欲教育, 羞恥教育, 搾精旅行). Bare 誘惑 / 沼 / 教育
# / 旅行 are not chips. The noun window is the same 2–8 kanji/katakana run
# (性教育 is one kanji short of that window, so it is not minted from a single 性).
_COMPOUND_SUFFIXES: tuple[str, ...] = ("誘惑", "沼", "教育", "旅行")

# Kinship / pronoun nouns that form selectable XのY chips (彼女の妹).
# One-character members are chips only as part of such a phrase, not as leftovers.
# 息子 / ママ are also lexicon chips (see below) so a bare occurrence still shows.
_RELATION_NOUNS: tuple[str, ...] = (
    "お姉さん",
    "あなた",
    "彼女",
    "彼氏",
    "義妹",
    "義母",
    "義父",
    "義姉",
    "義兄",
    "義弟",
    "義娘",
    "従姉",
    "従妹",
    "叔母",
    "叔父",
    "息子",
    "ママ",
    "ボク",
    "妹",
    "姉",
    "兄",
    "弟",
    "私",
    "僕",
    "俺",
    "君",
)
# Pronouns are not the left side of kinshipのoccupation (ボクの巨乳 stays unsplit).
_PRONOUN_NOUNS = frozenset({"あなた", "ボク", "私", "僕", "俺", "君"})
# Occupations / roles on the right of 息子の家庭教師. Not body or clothing
# nouns, so 妹のノーブラ and ボクの巨乳 do not become phrases.
_ROLE_THEME_NOUNS = frozenset(
    {
        "家庭教師",
        "女教師",
        "ナース",
        "女医",
        "秘書",
    }
)
# Edition / format marks. Not theme chips, ever — not even "if the title
# prints them prominently". OL is not in this set: it stays an occupation
# keyword. VR is not in this set either: it is a product theme.
# Junk even without a number (VOL, EP, BOD, BD, DVD, Blu-ray);
# VOL.2 / 第2巻 / 第十二話 are the numbered forms. (BOD) / （Blu-ray） are the
# parenthetical disc-edition tags catalog sites append after the actress.
_EDITION_LATIN = frozenset(
    {
        "vol",
        "volume",
        "ep",
        "episode",
        "bod",
        "bd",
        "dvd",
        "uhd",
        "bluray",
        "4k",
    }
)
_EDITION_MARKER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"vol(?:ume)?|ep(?:isode)?|bluray|blu[\s\-‐－]?ray|uhd|bod|dvd|(?:4|４)k|bd"
    r")(?![A-Za-z])"
    r"(?:\s*[\.．]?\s*[0-9０-９]+)?"
    r"|第\s*[0-9０-９一二三四五六七八九十百千〇零]+\s*[巻話章集回]"
    r"|ブルーレイ(?:ディスク)?"
)
_PAREN_GROUP_RE = re.compile(r"[\(（\[【［]([^\)）\]】］]{0,40})[\)）\]】］]")
_RELATION_NOUN_SET = frozenset(_RELATION_NOUNS)
_KINSHIP_ROLE_SET = frozenset(n for n in _RELATION_NOUNS if n not in _PRONOUN_NOUNS)


_THEME_KEYWORD_LEXICON = (
    # User examples + common plot tokens (JP / ZH variants)
    "満員",
    "滿員",
    "電車",
    "媚薬",
    "媚藥",
    "オイル",
    "油",
    "乳首",
    "乳頭",
    "巨乳",
    "美乳",
    "爆乳",
    "OL",
    "女教師",
    "家庭教師",
    "人妻",
    "痴漢",
    "癡漢",
    "開発",
    "開發",
    "調教",
    "マッサージ",
    "エステ",
    "温泉",
    "寝取",
    "義妹",
    "義母",
    "義父",
    "義姉",
    "義兄",
    "義弟",
    "義娘",
    "従姉",
    "従妹",
    "叔母",
    "叔父",
    "彼女",
    "息子",
    "ママ",
    "お姉さん",
    "ナース",
    "女医",
    "秘書",
    "CA",
    "ノーブラ",
    "中出し",
    "交尾",
    "童貞",
    "絶倫",
    "搾精",
    "顔射",
    "拘束",
    "監禁",
    "痴女",
    "逆レ",
    "毎朝",
    "毎晩",
    "通勤",
    "会社",
    "オフィス",
    "下着",
    "パンスト",
    "夜行バス",
    "声我慢",
    "逆NTR",
    "羞恥",
    "指マン",
    "美尻",
    # Short high-signal look tokens (2–4 chars). Keep out of _WEAK_THEME_TOKENS
    # so leftover {4,6} scraps are not the only "distinctive" keywords.
    "眼鏡っ娘",
    "メガネっ娘",
    "眼鏡",
    "メガネ",
    "地味",
    "美人",
    "辦公室",
    "办公室",
)

# Short setting / identity nouns (valid even at 2 chars). Not 地位 — 地味.
_SHORT_THEME_NOUNS = frozenset(
    {
        "眼鏡",
        "メガネ",
        "地味",
        "美人",
        "電車",
        "OL",
        "オフィス",
        "辦公室",
        "办公室",
        "満員",
        "滿員",
        "温泉",
        "秘書",
        "女医",
        "痴女",
        "義妹",
        "義母",
        "義父",
        "義姉",
        "義兄",
        "義弟",
        "義娘",
        "従姉",
        "従妹",
        "叔母",
        "叔父",
        "交尾",
        "童貞",
        "絶倫",
        "搾精",
        "毎晩",
        "巨乳",
        "美乳",
        "爆乳",
        "通勤",
        "ナース",
    }
)

# Search/hit aliases so 眼鏡 titles match メガネ / 眼鏡っ娘 catalog rows.
_THEME_KEYWORD_ALIASES: dict[str, tuple[str, ...]] = {
    "眼鏡": ("眼鏡", "メガネ", "眼鏡っ娘", "メガネっ娘"),
    "メガネ": ("眼鏡", "メガネ", "眼鏡っ娘", "メガネっ娘"),
    "眼鏡っ娘": ("眼鏡", "メガネ", "眼鏡っ娘", "メガネっ娘"),
    "メガネっ娘": ("眼鏡", "メガネ", "眼鏡っ娘", "メガネっ娘"),
    "オフィス": ("オフィス", "辦公室", "办公室"),
    "辦公室": ("オフィス", "辦公室", "办公室"),
    "办公室": ("オフィス", "辦公室", "办公室"),
}


def _keyword_index(text: str, kw: str) -> int:
    """Index of kw in text, or -1.

    ASCII lexicon tokens (OL, NTR, SEX) must be a whole Latin/digit token.
    OL matches 美人OL and 巨乳OL, not the middle of VOL / GOLD / COOL.
    CJK keywords stay ordinary substrings (逆NTR, 家庭教師).
    """
    if not text or not kw:
        return -1
    if re.fullmatch(r"[A-Za-z0-9]+", kw):
        m = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])",
            text,
            flags=re.IGNORECASE,
        )
        return m.start() if m else -1
    # Mixed tokens such as 逆NTR stay case-insensitive substrings. casefold
    # does not change length for these, so the index still points into text.
    folded = text.casefold()
    key = kw.casefold()
    if len(folded) == len(text) and len(key) == len(kw):
        return folded.find(key)
    return text.find(kw)


def _fold_fullwidth_latin(text: str) -> str:
    """Map Ａ-Ｚ / ａ-ｚ to ASCII. Length-preserving. Digits stay as written."""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _is_edition_marker_token(tok: str) -> bool:
    """True for disc/edition junk: BOD, (already-unwrapped) Blu-ray, VOL.2, 第2巻.

    OL and VR are not edition marks. A theme word that merely sits beside a
    format tag is not itself junk.
    """
    raw = (tok or "").strip()
    if not raw:
        return False
    folded = _fold_fullwidth_latin(raw).strip()
    if re.fullmatch(r"(?i)blu[\s\-‐－]?ray", folded):
        return True
    if re.fullmatch(r"第\s*[0-9０-９一二三四五六七八九十百千〇零]+\s*[巻話章集回]", raw):
        return True
    if folded in {"ブルーレイ", "ブルーレイディスク"}:
        return True
    compact = re.sub(r"[\s\.．·・‐－\-]+", "", folded).casefold()
    if compact in _EDITION_LATIN or compact in {"ブルーレイ", "ブルーレイディスク"}:
        return True
    if re.fullmatch(r"(?:vol(?:ume)?|ep(?:isode)?|bod|bd|dvd|uhd|bluray|4k)[0-9]+", compact):
        return True
    return False


def _is_edition_marker_span(text: str, start: int, end: int) -> bool:
    """True when this Latin span is VOL / BOD / BD / DVD, numbered or not.

    OL is never an edition mark. Numbered forms (VOL.2) and parenthetical
    (BOD) / (Blu-ray) are removed up front by _strip_edition_markers; this
    guards the Latin pass if a bare token remains.
    """
    return _is_edition_marker_token((text or "")[start:end])


def _strip_edition_markers(text: str) -> str:
    """Drop episode/volume/disc junk before keyword extraction.

    Removes VOL / Vol / VOL.2 / EP.2, 第N巻-style counters, and format tags
    BOD / BD / DVD / Blu-ray / ブルーレイ / 4K / UHD. A parenthetical that is
    only a format tag — (BOD), （Blu-ray）, 【BD】 — is removed whole. A
    parenthetical that mixes a format tag with real words keeps those words.
    Does not touch occupation OL or theme VR.
    """
    if not text:
        return ""
    folded = _fold_fullwidth_latin(text)

    def _paren(m: re.Match) -> str:
        inner = m.group(1) or ""
        cleaned = _EDITION_MARKER_RE.sub(" ", inner)
        compact_clean = re.sub(r"[\s・,，/|．\.]+", "", cleaned)
        compact_inner = re.sub(r"[\s・,，/|．\.]+", "", inner)
        if not compact_clean:
            return " "
        if compact_clean != compact_inner:
            return " " + cleaned + " "
        return m.group(0)

    out = _PAREN_GROUP_RE.sub(_paren, folded)
    out = _EDITION_MARKER_RE.sub(" ", out)
    out = re.sub(r"[\(（\[【［]\s*[\)）\]】］]", " ", out)
    return out


def _title_sibling_phrases(title: str) -> list[str]:
    """Distinctive title phrases for 片名相近 / same-series catalog search."""
    raw = normalize_ocr_title(title) or (title or "").strip()
    raw = _strip_edition_markers(raw)
    t = re.sub(r"\s+", "", raw)
    if len(t) < 6:
        return []
    out: list[str] = []

    def _add(q: str) -> None:
        q = (q or "").strip()
        if len(q) < 4 or len(q) > 18:
            return
        if q not in out:
            out.append(q)

    # Full 「…」 / "…" hooks — series siblings share the quoted template
    for m in re.finditer(r"[「『\"]([^」』\"]{6,40})[」』\"]", raw):
        hook = re.sub(r"\s+", "", m.group(1))
        _add(hook[:18])
        _add(hook[:12])
        head = hook.split("。")[0]
        if len(head) >= 6:
            _add(head[:18])
            _add(head[:12])

    # Contentful chunks between particles/punctuation (series templates often live here)
    parts = re.split(r"[をにでがはもとからまでへの、。！？\!\?／/\|・]+", t)
    for p in parts:
        if 4 <= len(p) <= 16:
            _add(p)
        if len(p) > 16:
            _add(p[:12])
            _add(p[:8])

    # Lexicon compounds + small context windows
    for kw in sorted(_THEME_KEYWORD_LEXICON, key=len, reverse=True):
        i = _keyword_index(t, kw)
        if i < 0:
            continue
        _add(kw)
        _add(t[max(0, i - 2) : min(len(t), i + len(kw) + 4)])
        # Adjacent lexicon pair → compound (満員+電車, 媚薬+オイル)
    for a, b in (
        ("満員", "電車"),
        ("滿員", "電車"),
        ("媚薬", "オイル"),
        ("媚藥", "オイル"),
        ("乳首", "開発"),
        ("乳首", "イキ"),
        ("巨乳", "OL"),
        ("地味", "眼鏡"),
        ("地味", "メガネ"),
        ("美人", "OL"),
        ("眼鏡", "OL"),
        ("声我慢", "SEX"),
        ("逆", "NTR"),
        ("夜行", "バス"),
    ):
        ia, ib = _keyword_index(t, a), _keyword_index(t, b)
        if ia >= 0 and ib >= 0:
            if 0 <= ia < ib <= ia + 12:
                _add(t[ia : ib + len(b)])
            _add(a + b)

    # Prefix + mid windows (OCR / truncated titles)
    for n in (12, 10, 8, 6):
        if len(t) >= n + 2:
            _add(t[:n])
    if len(t) >= 16:
        _add(t[4:14])
        mid = len(t) // 3
        _add(t[mid : mid + 10])

    # Always keep short series compounds (満員電車, 声我慢SEX…) even if longer variants dominate
    must: list[str] = []
    for m in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9]{1,6}(?:電車|バス))", t):
        comp = m.group(1)
        if 3 <= len(comp) <= 10 and comp not in must:
            must.append(comp)
        # Non-overlapping finditer can skip shorter cores (羞恥電車 inside 字尻羞恥電車)
        if comp.endswith("電車") and len(comp) > 4:
            core = comp[-4:]  # e.g. 羞恥電車
            if core not in must:
                must.append(core)
        if comp.endswith("バス") and len(comp) > 4:
            core = comp[-4:]
            if core not in must:
                must.append(core)
    for a, b in (
        ("満員", "電車"),
        ("滿員", "電車"),
        ("媚薬", "オイル"),
        ("媚藥", "オイル"),
        ("声我慢", "SEX"),
        ("夜行", "バス"),
        ("逆", "NTR"),
        ("巨乳", "OL"),
        ("地味", "眼鏡"),
        ("地味", "メガネ"),
        ("美人", "OL"),
        ("乳首", "開発"),
    ):
        ia, ib = _keyword_index(t, a), _keyword_index(t, b)
        if ia >= 0 and ib >= 0:
            comp = a + b
            if 4 <= len(comp) <= 10 and comp in t and comp not in must:
                must.append(comp)
            if 0 <= ia < ib <= ia + 10:
                span = t[ia : ib + len(b)]
                if 4 <= len(span) <= 12 and span not in must:
                    must.append(span)

    # Prefer longer / earlier phrases first
    def _rank(q: str) -> tuple:
        pos = t.find(q)
        return (-len(q), pos if pos >= 0 else 10_000)

    out.sort(key=_rank)
    merged: list[str] = []
    for q in must + out:
        if q not in merged:
            merged.append(q)
    return merged[:16]


_DIGIT_FOLD = str.maketrans("０１２３４５６７８９", "0123456789")
# Distinctive time + action hooks (10秒で挿入 / 10秒挿入 / 3分で絶頂).
# The chip is the compact form; で / に are surface spellings of the same hook.
# Not edition junk: VOL.2 / 第2巻 never match (no 秒/分/時間 + action).
_TIME_ACTION_VERBS: tuple[str, ...] = (
    "フェラチオ",
    "セックス",
    "中出し",
    "手コキ",
    "クンニ",
    "挿入",
    "絶頂",
    "射精",
    "顔射",
    "イカせ",
    "フェラ",
    "接吻",
    "ハメ",
    "キス",
    "イク",
)
_TIME_ACTION_VERB_RE = "|".join(re.escape(v) for v in _TIME_ACTION_VERBS)
_TIME_ACTION_FIND_RE = re.compile(
    rf"([0-9０-９]{{1,3}})\s*(時間|秒|分)\s*(?:で|に)?\s*({_TIME_ACTION_VERB_RE})"
)
_TIME_ACTION_SURFACE_RE = re.compile(
    rf"^([0-9]+)(時間|秒|分)(?:で|に)?({_TIME_ACTION_VERB_RE})$"
)


def _fold_digits(text: str) -> str:
    return (text or "").translate(_DIGIT_FOLD)


def _canonical_time_action(num: str, unit: str, action: str) -> str:
    return f"{_fold_digits(num)}{unit}{action}"


def _time_action_match_forms(tok: str) -> tuple[str, ...]:
    """Compact, で, and に spellings of one time+action chip. Empty if tok is not one."""
    raw = re.sub(r"\s+", "", _fold_digits((tok or "").strip()))
    m = _TIME_ACTION_SURFACE_RE.fullmatch(raw)
    if not m:
        return ()
    n, unit, act = m.group(1), m.group(2), m.group(3)
    return (f"{n}{unit}{act}", f"{n}{unit}で{act}", f"{n}{unit}に{act}")


def _is_time_action_keyword(tok: str) -> bool:
    """True for the compact chip (10秒挿入), not the で/に surface alone."""
    forms = _time_action_match_forms(tok)
    return bool(forms) and forms[0] == re.sub(r"\s+", "", _fold_digits((tok or "").strip()))


def _extract_time_action_phrases(title: str) -> list[str]:
    """In-title time+action hooks as compact chips (10秒で挿入 → 10秒挿入)."""
    text = _fold_digits(title or "")
    out: list[str] = []
    seen: set[str] = set()
    for m in _TIME_ACTION_FIND_RE.finditer(text):
        start = m.start(1)
        if start > 0 and text[start - 1].isdigit():
            continue
        canon = _canonical_time_action(m.group(1), m.group(2), m.group(3))
        if canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return out


# Trip-length settings (一泊二日, 二泊三日, 2泊3日). One numeral on each side
# so 十一泊十二日 is not sliced into 一泊二. Not edition junk.
_STAY_NUM = r"[0-9０-９一二三四五六七八九十]"
_STAY_FIND_RE = re.compile(rf"(?<!{_STAY_NUM})({_STAY_NUM}泊{_STAY_NUM}日)")

# Glued act compounds (連続中出し). The bare act may stay a weak chip;
# the prefixed form is the theme. A kanji immediately before 連続 blocks
# a false cut such as 非連続中出し.
_ACT_PREFIXES: tuple[str, ...] = ("連続",)
_ACT_CORES: tuple[str, ...] = (
    "セックス",
    "中出し",
    "フェラ",
    "挿入",
    "射精",
    "顔射",
    "交尾",
)
_PREFIXED_ACT_RE = re.compile(
    r"(?<![\u4e00-\u9fff])("
    + "|".join(re.escape(p) for p in _ACT_PREFIXES)
    + r")("
    + "|".join(re.escape(a) for a in sorted(_ACT_CORES, key=len, reverse=True))
    + r")"
)


def _extract_stay_phrases(title: str) -> list[str]:
    """In-title N泊M日 settings (一泊二日). Surface form, not a translation."""
    text = re.sub(r"\s+", "", title or "")
    out: list[str] = []
    seen: set[str] = set()
    for m in _STAY_FIND_RE.finditer(text):
        phrase = m.group(1)
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    return out


def _is_stay_keyword(tok: str) -> bool:
    t = re.sub(r"\s+", "", tok or "")
    return _STAY_FIND_RE.fullmatch(t) is not None


def _extract_prefixed_act_phrases(title: str) -> list[str]:
    """In-title prefix+act compounds (連続中出し). Not a Chinese rewrite."""
    text = re.sub(r"\s+", "", title or "")
    out: list[str] = []
    seen: set[str] = set()
    for m in _PREFIXED_ACT_RE.finditer(text):
        phrase = m.group(1) + m.group(2)
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    return out


def _is_prefixed_act_keyword(tok: str) -> bool:
    t = re.sub(r"\s+", "", tok or "")
    return _PREFIXED_ACT_RE.fullmatch(t) is not None


def _time_phrase_in_text(phrase: str, text: str) -> bool:
    """Substring hit of any spelling. A longer number's tail (110秒) is not 10秒."""
    folded = _fold_digits(text or "")
    forms = _time_action_match_forms(phrase)
    if not forms:
        key = re.sub(r"\s+", "", _fold_digits(phrase or ""))
        forms = (key,) if key else ()
    for key in forms:
        if not key:
            continue
        for hit in re.finditer(re.escape(key), folded):
            if hit.start() > 0 and folded[hit.start() - 1].isdigit():
                continue
            return True
    return False


def _keyword_surface_pos(title: str, tok: str) -> int:
    """Earliest index of tok, or of 10秒で挿入 when the chip is 10秒挿入."""
    if not title or not tok:
        return 10_000
    folded = _fold_digits(title)
    best = folded.find(_fold_digits(tok))
    for al in _time_action_match_forms(tok):
        i = folded.find(al)
        if i >= 0 and (best < 0 or i < best):
            best = i
    return best if best >= 0 else 10_000


def _theme_keyword_aliases(tok: str) -> tuple[str, ...]:
    t = (tok or "").strip()
    if not t:
        return ()
    timed = _time_action_match_forms(t)
    if timed:
        return timed
    return _THEME_KEYWORD_ALIASES.get(t) or _THEME_KEYWORD_ALIASES.get(t.casefold()) or (t,)


def _is_weak_theme_token(tok: str) -> bool:
    t = (tok or "").strip()
    if not t:
        return True
    if t in _SHORT_THEME_NOUNS or t.upper() in _SHORT_THEME_NOUNS:
        return False
    return t in _WEAK_THEME_TOKENS or t.upper() in _WEAK_THEME_TOKENS


def _is_relation_noun(tok: str) -> bool:
    return (tok or "").strip() in _RELATION_NOUN_SET


def _is_relation_phrase(tok: str) -> bool:
    """Selectable XのY that must not lead automatic search.

    彼女の妹 — both sides are relationship nouns.
    息子の家庭教師 — kinship noun + occupation. Bare 息子 stays weak.
    """
    t = (tok or "").strip()
    if t.count("の") != 1:
        return False
    left, right = t.split("の", 1)
    if _is_relation_noun(left) and _is_relation_noun(right):
        return True
    return left in _KINSHIP_ROLE_SET and right in _ROLE_THEME_NOUNS


def _contains_edition_marker(tok: str) -> bool:
    """True when the token is, or still contains, disc/edition junk.

    Catches a glued pair such as 交尾BOD as well as a bare BOD / VOL.2.
    """
    raw = (tok or "").strip()
    if not raw:
        return False
    if _is_edition_marker_token(raw):
        return True
    return _EDITION_MARKER_RE.search(_fold_fullwidth_latin(raw)) is not None


def _keyword_token_ok(tok: str) -> bool:
    """Chip/query token length. 妹 is allowed; other 1-char scraps are not.

    Edition/format tokens (BOD, Blu-ray, VOL, 第2巻) are never chips, even
    when a cached list or a re-search payload still contains them, and even
    when a pair query glued one onto a real theme word.
    """
    t = (tok or "").strip()
    if not t or len(t) > 24:
        return False
    if _contains_edition_marker(t):
        return False
    if len(t) >= 2:
        return True
    return _is_relation_noun(t)


def _is_auto_theme_keyword(tok: str) -> bool:
    """Terms that lead automatic related search (not relationship chips)."""
    t = (tok or "").strip()
    if not t:
        return False
    if _is_time_action_keyword(t):
        return True
    if _is_relation_phrase(t) or _is_weak_theme_token(t):
        return False
    return True


def _extract_relation_compounds(title: str) -> list[tuple[str, str, str]]:
    """In-title relationship phrases as (phrase, left, right).

    彼女の妹 yields the full chip plus both parts.
    息子の家庭教師 does too: kinship on the left, occupation on the right.
    妹のノーブラ does not (clothing is not an occupation). ボクの巨乳 does not
    (pronouns are not a kinship-role left side, and 巨乳 is not an occupation).
    """
    t = re.sub(r"\s+", "", title or "")
    if "の" not in t:
        return []
    rel_nouns = sorted(_RELATION_NOUN_SET, key=len, reverse=True)
    kin_nouns = sorted(_KINSHIP_ROLE_SET, key=len, reverse=True)
    roles = sorted(_ROLE_THEME_NOUNS, key=len, reverse=True)
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _before(end: int, nouns: list[str]) -> str:
        for noun in nouns:
            if end >= len(noun) and t.endswith(noun, 0, end):
                return noun
        return ""

    def _after(start: int, nouns: list[str]) -> str:
        for noun in nouns:
            if t.startswith(noun, start):
                return noun
        return ""

    def _emit(left: str, right: str) -> None:
        phrase = f"{left}の{right}"
        if phrase in seen:
            return
        seen.add(phrase)
        out.append((phrase, left, right))

    start = 0
    while True:
        i = t.find("の", start)
        if i < 0:
            break
        start = i + 1
        left_rel = _before(i, rel_nouns)
        if left_rel:
            right_rel = _after(i + 1, rel_nouns)
            if right_rel:
                _emit(left_rel, right_rel)
                continue
        left_kin = _before(i, kin_nouns)
        if left_kin:
            right_role = _after(i + 1, roles)
            if right_role:
                _emit(left_kin, right_role)
    return out


def _compound_noun_heads() -> list[str]:
    """Non-weak lexicon nouns that may head noun+誘惑 / noun+沼, longest first."""
    seen: set[str] = set()
    heads: list[str] = []
    for raw in list(_THEME_KEYWORD_LEXICON) + list(_SHORT_THEME_NOUNS):
        noun = (raw or "").strip()
        if len(noun) < 2 or noun in seen or _is_weak_theme_token(noun):
            continue
        seen.add(noun)
        heads.append(noun)
    heads.sort(key=len, reverse=True)
    return heads


def _extract_title_compounds(title: str) -> list[tuple[str, str]]:
    """In-title compounds as (compound, noun_half).

    Prefer a known theme noun glued to 誘惑/沼/教育 (ノーブラ誘惑, 巨乳沼,
    羞恥教育). Otherwise keep a short kanji/katakana noun glued to the same
    suffix (ナマ乳沼, 肉欲教育) without minting that noun as its own chip.
    Weak heads (彼女) do not form a compound. A particle between the noun
    and the suffix (ノーブラの誘惑, 肉欲の教育) does not either. Bare 教育
    is not a chip.
    """
    t = re.sub(r"\s+", "", title or "")
    if len(t) < 3:
        return []
    heads = _compound_noun_heads()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(noun: str, suf: str) -> None:
        noun = (noun or "").strip()
        if len(noun) < 2 or _is_weak_theme_token(noun) or noun in _COMPOUND_SUFFIXES:
            return
        compound = noun + suf
        if compound in seen or compound not in t:
            return
        if not (3 <= len(compound) <= 18):
            return
        seen.add(compound)
        out.append((compound, noun))

    for suf in _COMPOUND_SUFFIXES:
        start = 0
        while True:
            i = t.find(suf, start)
            if i < 0:
                break
            start = i + max(len(suf), 1)
            if i <= 0:
                continue
            head = t[:i]
            noun = ""
            for h in heads:
                if head.endswith(h):
                    noun = h
                    break
            if noun:
                _push(noun, suf)
                continue
            m = re.search(r"([\u30a0-\u30ff\u4e00-\u9fff]{2,8})$", head)
            if not m:
                continue
            span = m.group(1)
            generic = ""
            for h in heads:
                idx = span.rfind(h)
                if idx >= 0 and idx + len(h) < len(span):
                    rest = span[idx + len(h) :]
                    if 2 <= len(rest) <= 6 and not _is_weak_theme_token(rest):
                        generic = rest
                        break
            if not generic:
                generic = span[-6:] if len(span) > 6 else span
            _push(generic, suf)
    return out


def _chip_contains_part(compound: str, part: str) -> bool:
    """True when `part` is a shorter chip covered by `compound`.

    の-phrases match a whole side only (息子の家庭教師 covers 家庭教師 and 息子;
    義妹 does not cover 妹). Glued suffix compounds match a prefix
    (ノーブラ誘惑 covers ノーブラ, 巨乳沼 covers 巨乳).
    """
    compound = (compound or "").strip()
    part = (part or "").strip()
    if not compound or not part or compound == part or len(part) >= len(compound):
        return False
    if compound.count("の") == 1:
        left, right = compound.split("の", 1)
        return part == left or part == right
    if compound.startswith(part):
        return True
    # 連続中出し covers the act 中出し; it does not start with that act.
    return _is_prefixed_act_keyword(compound) and compound.endswith(part)


def _subsumed_keyword_parts(keywords: list[str] | None) -> set[str]:
    """Chips that are a shorter piece of some other chip in the same list."""
    items = [str(k or "").strip() for k in (keywords or []) if str(k or "").strip()]
    parts: set[str] = set()
    for comp in items:
        for other in items:
            if _chip_contains_part(comp, other):
                parts.add(other)
    return parts


def _compound_covers_auto_theme(compound: str, keywords: list[str] | None) -> bool:
    """True when a phrase covers a real theme noun, not only weak kinship.

    息子の家庭教師 covers 家庭教師, so automatic search must lead with the
    phrase. 彼女の妹 covers only 彼女 / 妹, so it stays behind 巨乳.
    """
    for part in keywords or []:
        if _chip_contains_part(compound, part) and _is_auto_theme_keyword(part):
            return True
    return False


def _order_compounds_before_parts(found: list[str]) -> list[str]:
    """Move a longer chip to just before its own shorter chips.

    Leaves a compound where it is when it already sits ahead of those parts,
    so 巨乳 stays in front of 彼女の妹. 息子の家庭教師 moves ahead of 家庭教師.
    """
    items = [str(k or "").strip() for k in found if str(k or "").strip()]
    compounds = [tok for tok in items if any(_chip_contains_part(tok, other) for other in items)]
    compounds.sort(key=len, reverse=True)
    for comp in compounds:
        parts = [other for other in items if _chip_contains_part(comp, other)]
        if not parts or comp not in items:
            continue
        if items.index(comp) < min(items.index(part) for part in parts):
            continue
        items.remove(comp)
        at = min(items.index(part) for part in parts)
        items.insert(at, comp)
    return items


def _split_auto_keyword_queries(
    keywords: list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Leading auto queries: covering compounds, other theme nouns, weak phrases.

    Parts of a longer chip (家庭教師 under 息子の家庭教師, 彼女 under 彼女の妹)
    are omitted here. Single-term fallback asks for them only after the phrase.
    """
    items = [str(k or "").strip() for k in (keywords or []) if str(k or "").strip()]
    parts = _subsumed_keyword_parts(items)
    early: list[str] = []
    late: list[str] = []
    distinctive: list[str] = []
    for tok in items:
        if _is_relation_phrase(tok) and _compound_covers_auto_theme(tok, items):
            early.append(tok)
        elif _is_relation_phrase(tok):
            late.append(tok)
        elif tok in parts or _is_weak_theme_token(tok):
            continue
        else:
            distinctive.append(tok)
    return early, distinctive, late


def _redundant_substring_only(matched: list[str], keywords: list[str] | None) -> bool:
    """True when the row only hits shorter pieces of a longer available chip.

    家庭教師 + 息子 without 息子の家庭教師 must not fill the strict bucket
    ahead of the phrase. 肉欲教育 / 10秒挿入 are not pieces of that phrase.
    """
    parts = _subsumed_keyword_parts(keywords)
    if not parts or not matched:
        return False
    if any(any(_chip_contains_part(tok, part) for part in parts) for tok in matched):
        return False
    if any(tok not in parts and _is_auto_theme_keyword(tok) for tok in matched):
        return False
    return any(tok in parts for tok in matched)


def _rank_theme_keywords(
    found: list[str],
    title: str,
    compounds: list[tuple[str, str]],
) -> list[str]:
    """Compounds of known theme nouns, then other strong nouns, then weak fluff.

    The noun half of a kept compound stays (ノーブラ under ノーブラ誘惑) but
    ranks after strong nouns that are not already covered by that compound,
    so 巨乳 is not pushed behind a duplicate of the same head. A phrase that
    contains another chip then moves to just before that chip (息子の家庭教師
    before 家庭教師 / 息子) without passing unrelated theme nouns.
    """
    compact = re.sub(r"\s+", "", title or "")
    compound_head = {comp: noun for comp, noun in compounds}
    heads = {noun for noun in compound_head.values() if noun}

    def _tier(tok: str) -> int:
        # 一泊二日 / 連続中出し are productive theme chips, beside lexicon nouns.
        # Do this before the compound-head tier: 中出し is weak, and that must
        # not demote the prefixed act that contains it.
        if _is_stay_keyword(tok) or _is_prefixed_act_keyword(tok):
            return 1
        head = compound_head.get(tok)
        if head and not _is_weak_theme_token(tok) and not _is_relation_phrase(tok):
            known = (not _is_weak_theme_token(head)) and (
                head in _THEME_KEYWORD_LEXICON or head in _SHORT_THEME_NOUNS
            )
            return 0 if known else 3
        # Relationship phrase stays selectable but behind theme nouns.
        if _is_relation_phrase(tok):
            return 4
        # 10秒挿入-style hooks are productive theme chips, beside lexicon nouns.
        if _is_time_action_keyword(tok):
            return 1
        if _is_weak_theme_token(tok):
            return 5
        if tok in heads and (tok in _THEME_KEYWORD_LEXICON or tok in _SHORT_THEME_NOUNS):
            return 2
        return 1

    def _key(tok: str) -> tuple:
        pos = _keyword_surface_pos(compact, tok)
        return (_tier(tok), pos, -len(tok))

    return _order_compounds_before_parts(sorted(found, key=_key))


def _keyword_min_hits(keywords: list[str] | None) -> int:
    """≥3 extracted keywords → need ≥2 hits; 1–2 keywords may match alone."""
    n = len([k for k in (keywords or []) if k])
    if n >= 3:
        return 2
    return 1 if n else 0


def _selected_keyword_min_hits(keywords: list[str] | None) -> int:
    """Interactive re-search over the chips the user turned on.

    ≥2 selected → require multiple hits among that set (AND / multi-hit, same
    idea as the ≥3-keyword bucket rule). Exactly 1 selected → that keyword may
    fill the row. Never pad with non-matches.
    """
    n = len([k for k in (keywords or []) if str(k or "").strip()])
    if n >= 2:
        return 2
    return 1 if n else 0


def _normalize_keyword_list(raw, *, limit: int = 10) -> list[str]:
    """Stable unique keyword tokens for API payloads and re-search chips."""
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, str):
        raw = re.split(r"[\s,，、・/|]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    for item in raw:
        tok = str(item or "").strip()
        if not _keyword_token_ok(tok):
            continue
        key = tok.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _extract_title_theme_keywords(title: str, actress: str | None = None) -> list[str]:
    """Discrete theme keywords from a title (満員/電車/媚薬/巨乳/眼鏡/地味 …).

    Prefer lexicon + Latin tokens and in-title compounds (ノーブラ誘惑, 巨乳沼,
    肉欲教育). Keep the distinctive noun half of a lexicon compound (ノーブラ),
    and keep high-signal 2-char look tokens (眼鏡/地味/美人).

    Relationship pattern 彼女の妹 adds three selectable chips: 彼女, 妹, and
    彼女の妹. Kinship + occupation (息子の家庭教師) does the same for the phrase
    and both parts. Specific kinship in the lexicon (叔母, 義母, 義姉, …) is a
    theme chip when the title says it; generic 彼女 / 妹 stay selectable but
    weak. Weak halves stay selectable, but a longer chip is ordered
    before its own shorter chips. Unrelated theme nouns (巨乳) stay ahead of
    彼女の妹. Bare 誘惑 / 教育 / 旅行 are
    not chips unless glued to a noun (搾精旅行). A time+action hook
    (10秒で挿入, 3分で絶頂) becomes one compact chip (10秒挿入). A stay
    (一泊二日) and a prefixed act (連続中出し) are chips in catalog spelling,
    never a Chinese rewrite. Edition / format junk (VOL, Vol.2, EP.2, 第2巻,
    BOD, (BOD), BD, DVD, Blu-ray, ブルーレイ) is stripped before matching, so
    it cannot become a chip, a leftover scrap, or an automatic query. OL stays
    in the lexicon: it is an occupation chip when the title actually contains
    that token, and it does not match inside VOL. Leftover {4,6} scraps run
    only when nothing distinctive was found (JUFE-271 は隠し切れな must not pad).
    """
    raw = (title or "").strip()
    if not raw:
        return []
    t = _strip_edition_markers(raw)
    if actress:
        for piece in re.split(r"[\s　・/|]+", str(actress)):
            piece = piece.strip()
            if len(piece) >= 2:
                t = t.replace(piece, " ")
    t_norm = t
    found: list[str] = []
    seen: set[str] = set()
    alias_seen: set[str] = set()
    compounds = _extract_title_compounds(t)
    relations = _extract_relation_compounds(t)

    def _add(tok: str) -> None:
        tok = (tok or "").strip()
        if not _keyword_token_ok(tok):
            return
        key = tok.casefold()
        if key in seen:
            return
        for al in _theme_keyword_aliases(tok):
            if al.casefold() in alias_seen:
                return
        seen.add(key)
        alias_seen.add(key)
        for al in _theme_keyword_aliases(tok):
            alias_seen.add(al.casefold())
        found.append(tok)

    for comp, _noun in compounds:
        _add(comp)
    for phrase, left, right in relations:
        _add(phrase)
        _add(left)
        _add(right)
    for canon in _extract_time_action_phrases(t):
        _add(canon)
    for phrase in _extract_stay_phrases(t):
        _add(phrase)
    for phrase in _extract_prefixed_act_phrases(t):
        _add(phrase)

    for kw in sorted(_THEME_KEYWORD_LEXICON, key=len, reverse=True):
        if _keyword_index(t_norm, kw) < 0:
            continue
        _add(kw)
        if re.fullmatch(r"[A-Za-z0-9]+", kw):
            t_norm = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])",
                " ",
                t_norm,
                flags=re.IGNORECASE,
            )
        else:
            t_norm = re.sub(re.escape(kw), " ", t_norm, flags=re.IGNORECASE)

    for m in re.finditer(r"[A-Za-z]{2,6}", t):
        if _is_edition_marker_span(t, m.start(), m.end()):
            continue
        _add(m.group(0).upper())

    # Known 2-char theme nouns left in the title after longer lexicon hits.
    # Same Latin boundary as the lexicon pass (OL must not fall out of VOL).
    compact = re.sub(r"\s+", "", t_norm)
    for noun in sorted(_SHORT_THEME_NOUNS, key=len, reverse=True):
        if noun and _keyword_index(compact, noun) >= 0:
            _add(noun)

    distinctive_lex = [k for k in found if not _is_weak_theme_token(k)]
    # Particle-bounded compounds only when nothing distinctive was found.
    # Fixed 4–6 windows sliced プライベート補習 into ライベート補 / 人っきりのプ.
    if not distinctive_lex:
        for chunk in _bounded_title_compounds(t_norm):
            if len(found) >= 10:
                break
            _add(chunk)

    return _rank_theme_keywords(found, t, compounds)[:10]


_TITLE_COMPOUND_STOP = {
    "全部",
    "すべて",
    "こと",
    "ため",
    "為",
    "本当",
    "自分",
    "ここ",
    "そこ",
    "これ",
    "それ",
    "もの",
    "とき",
    "時",
}


# Clause glue that is not a theme particle. Kept out of the single-character
# class so だけ does not become だ + け. も / いし sit between content words
# (金も無い, 無いし同僚) and must not glue two clauses into one chip.
_CLAUSE_JOIN_RE = re.compile(
    r"(?<=[\u4e00-\u9fff\u30a0-\u30ff0-9])"
    r"(?:ないし|ながら|けれど|けど|のに|ので|だけ|しか|つつ|ても|でも|いし|も)"
    r"(?=[\u4e00-\u9fff\u30a0-\u30ff])"
)


def _bounded_title_compounds(text: str) -> list[str]:
    """Chunks split on particles, not a sliding window.

    先生が2人っきりのプライベート補習で全部面倒みてあげる →
    先生 / 2人っきり / プライベート補習 / 面倒みてあげる.
    A token that is only the tail of a longer katakana word is dropped.
    Pure hiragana and edition junk are not chips. A clause joiner
    (だけ / ないし / も) splits the chunk so two clauses are not one chip.
    """
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return []
    parts = re.split(r"[をにでがはもとからまでへの、。！？！\?／/\|・…]+", t)
    parts = [piece for part in parts for piece in _CLAUSE_JOIN_RE.split(part)]
    raw_parts: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 全部面倒みてあげる → also the act without the generic prefix.
        if part.startswith("全部") and len(part) > 4:
            raw_parts.append(part[2:])
        raw_parts.append(part)
    katakana_words = re.findall(r"[\u30a0-\u30ffー]{4,}", t)
    out: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        if part in _TITLE_COMPOUND_STOP:
            continue
        if _contains_edition_marker(part) or _is_edition_marker_token(part):
            continue
        if re.fullmatch(r"[\u3040-\u309f]+", part):
            continue
        # 彼女のいない… splits on の into a tail that starts mid-word (いない貧乏…).
        if re.match(r"[\u3040-\u309f]", part):
            continue
        if _CLAUSE_JOIN_RE.search(part):
            continue
        # (仮名)アキさん is an alias label, not a theme. 自分以上 is a
        # comparison ("more than me"), not a compound.
        if "仮名" in part:
            continue
        if re.fullmatch(r"(自分|それ|これ|あれ|彼女|彼氏)以上", part):
            continue
        if not re.search(r"[\u4e00-\u9fff\u30a0-\u30ff0-9]", part):
            continue
        if part in _COMPOUND_SUFFIXES or any(part.startswith(suf) for suf in _COMPOUND_SUFFIXES):
            continue
        if len(part) < 4 and part != "先生":
            continue
        if len(part) > 16:
            continue
        if any(part != word and part in word and re.fullmatch(r"[\u30a0-\u30ffー]+", part) for word in katakana_words):
            continue
        if part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out


def _alias_in_title(alias: str, text: str) -> bool:
    """Substring hit. One-character relation nouns need a particle/edge boundary.

    妹 matches 彼女の妹 and a title that starts with 妹, not the tail of 義妹.
    ASCII aliases use the same whole-token boundary as extraction (OL ≠ VOL).
    Time+action chips match 10秒で挿入 / 10秒に挿入 / １０秒挿入, not the tail of 110秒.
    """
    al = (alias or "").strip()
    if not al or not text:
        return False
    if _time_action_match_forms(al):
        return _time_phrase_in_text(al, text)
    folded = text.casefold()
    key = al.casefold()
    if len(al) == 1 and _is_relation_noun(al):
        return (
            re.search(
                rf"(?:^|[のをにはがともへやで、。！？・\s]){re.escape(key)}",
                folded,
            )
            is not None
        )
    return _keyword_index(text, al) >= 0


def _matched_theme_keywords(candidate_title: str, keywords: list[str] | None) -> list[str]:
    """Source keywords that actually hit this related title (aliases count).

    Returns the source tokens, not a guessed extra list. Empty when nothing hits.
    """
    text = candidate_title or ""
    if not text or not keywords:
        return []
    hit: list[str] = []
    for kw in keywords:
        tok = str(kw or "").strip()
        if not tok or _is_edition_marker_token(tok):
            continue
        if any(_alias_in_title(al, text) for al in _theme_keyword_aliases(tok)):
            hit.append(tok)
    return hit


def _keyword_overlap(candidate_title: str, keywords: list[str]) -> tuple[int, float, list[str], int]:
    """Hit count, rank score, matched tokens, and distinctive-theme hit count.

    More matched keywords rank first. 彼女の妹 adds a strong bonus; a lone
    彼女 or 妹 still scores, but less than the full phrase or a theme hit.
    """
    matched = _matched_theme_keywords(candidate_title, keywords)
    hits = len(matched)
    theme_hits = sum(1 for k in matched if _is_auto_theme_keyword(k))
    compound_hits = sum(1 for k in matched if _is_relation_phrase(k))
    part_hits = sum(
        1 for k in matched if _is_relation_noun(k) and not _is_relation_phrase(k)
    )
    score = (
        float(hits) * 10.0
        + float(theme_hits) * 3.0
        + float(compound_hits) * 5.0
        + float(part_hits) * 1.0
    )
    return hits, score, matched, theme_hits


def _keyword_hit_count(candidate_title: str, keywords: list[str]) -> int:
    return len(_matched_theme_keywords(candidate_title, keywords))


def _high_sim_is_same_series(ref: str, cand: str, sim: float) -> bool:
    """True when similarity is a near-duplicate volume, not a short fragment.

    title_similarity returns 0.92 when either string contains the other, so
    肉欲教育ママ scores like a second volume of a long series title. Those
    fragments belong in the keyword bucket. A real sibling is long and covers
    a large share of the other title.
    """
    if sim < 0.72:
        return False
    ref_n = len(re.sub(r"\s+", "", ref or ""))
    cand_n = len(re.sub(r"\s+", "", cand or ""))
    shorter = min(ref_n, cand_n)
    longer = max(ref_n, cand_n, 1)
    if shorter < 18:
        return False
    if shorter / float(longer) < 0.55:
        return False
    return True


def _shared_run_is_series(ref_compact: str, cand_compact: str, lcs: int) -> bool:
    """A long shared run is a series template only when it is most of the shorter title.

    Twelve characters of 今日も息子の家庭教師 inside an unrelated 家庭教師 title
    must not steal that work out of the keyword bucket.
    """
    if lcs < 12:
        return False
    shorter = min(len(ref_compact or ""), len(cand_compact or ""))
    if shorter <= 0:
        return False
    return lcs >= 12 and (lcs / float(shorter)) >= 0.55


def _is_title_theme_match(
    ref_title: str,
    cand_title: str,
    *,
    keywords: list[str] | None = None,
    phrases: list[str] | None = None,
) -> tuple[bool, float]:
    """Whether cand is 片名相近 / same-series by title (not merely weak keyword)."""
    ref = normalize_ocr_title(ref_title) or (ref_title or "").strip()
    cand = normalize_ocr_title(cand_title) or (cand_title or "").strip()
    if not ref or not cand:
        return False, 0.0
    sim = title_similarity(ref, cand)
    kws = keywords if keywords is not None else _extract_title_theme_keywords(ref)
    hits = _keyword_hit_count(cand, kws)
    phrases = phrases if phrases is not None else _title_sibling_phrases(ref)
    ref_compact = re.sub(r"\s+", "", ref)
    cand_compact = re.sub(r"\s+", "", cand)
    shared = [p for p in phrases if len(p) >= 4 and p in cand_compact]
    best_shared = max((len(p) for p in shared), default=0)
    lcs = _longest_common_substr_len(ref_compact, cand_compact)
    # Opening series hook (満員電車… / 彼氏チ○ポ…) — first ~5 chars of title
    opening = [p for p in shared if 0 <= ref_compact.find(p) <= 5]

    # Long contiguous series template (NHDTC 声我慢SEX…中出し)
    if lcs >= 12 and _shared_run_is_series(ref_compact, cand_compact, lcs):
        return True, max(sim, 0.62)
    # Very high overall similarity (near-duplicate / same series rename).
    # Containment alone is 0.92 for a short fragment such as 肉欲教育ママ inside
    # a long series title. That fragment is a keyword hit, not another volume.
    if _high_sim_is_same_series(ref, cand, sim):
        return True, sim
    # Opening compound + rich keyword overlap (DRPT ↔ ATID 満員電車…)
    if opening and hits >= 3 and sim >= 0.22:
        return True, max(sim, 0.50)
    # Long shared phrase that is itself an opening hook
    if best_shared >= 10 and opening and hits >= 2 and sim >= 0.20:
        return True, max(sim, 0.52)
    # Mid/long template without opening only when overlap is very strong
    if best_shared >= 12 and hits >= 3 and sim >= 0.28:
        return True, max(sim, 0.48)
    if best_shared >= 4 and hits >= 5 and sim >= 0.30 and opening:
        return True, max(sim, 0.46)
    # Shared *電車 / *バス family — generic rails need rich overlap; niche rails (羞恥電車) OK with ≥2
    rail = [p for p in shared if len(p) >= 4 and (p.endswith("電車") or p.endswith("バス"))]
    if rail:
        generic = {"満員電車", "夜行バス", "電車", "バス"}
        niche = [p for p in rail if p not in generic]
        if niche and hits >= 2 and sim >= 0.12:
            return True, max(sim, 0.44)
        # Generic crowded-train / night-bus alone: only with strong keyword overlap
        if any(p in generic for p in rail) and hits >= 4 and sim >= 0.24:
            return True, max(sim, 0.45)
    return False, sim


def _keyword_search_queries(
    title: str,
    keywords: list[str],
    *,
    selected_only: bool = False,
) -> list[str]:
    """Catalog queries for a keyword bucket.

    selected_only: interactive re-search — query the chosen tokens and their
    compounds/aliases, not unrelated sibling phrases from the full title.
    Cap 10 (關鍵字再搜).

    Default bucket: a phrase that covers a theme noun leads (息子の家庭教師
    before 家庭教師). Other theme nouns follow (10秒挿入, 肉欲教育, 巨乳).
    A weak phrase such as 彼女の妹 is queried after those nouns and before its
    halves, which are not in this leading list. Weak kinship tokens are queried
    only when the title has no stronger term, or when there are just 1–2
    keywords total. When a ≥3-keyword pass finds nothing or only one usable
    work, `_find_related_by_keywords` asks `_keyword_fallback_singles`, with
    substring singles after the compound.
    """
    title = title or ""
    queries: list[str] = []

    def _add_q(q: str) -> None:
        q = (q or "").strip()
        if _keyword_token_ok(q) and q not in queries:
            queries.append(q)

    # Selectable chips keep every half. Automatic queries lead with a phrase
    # that covers a theme noun, then other theme nouns, then a weak phrase
    # such as 彼女の妹. Shorter pieces of those phrases are fallback-only.
    early, distinctive, late = _split_auto_keyword_queries(keywords)
    if selected_only:
        ordered = sorted(
            list(keywords),
            key=lambda k: (_keyword_surface_pos(title, k), -len(k)),
        )
    else:
        # Pairs follow title order. Singles stay in chip rank (compound first).
        ordered = sorted(
            distinctive,
            key=lambda k: (_keyword_surface_pos(title, k), -len(k)),
        )

    def _add_pairs() -> None:
        for i in range(len(ordered) - 1):
            a, b = ordered[i], ordered[i + 1]
            if not a or not b or a.casefold() == b.casefold():
                continue
            # ノーブラ is already inside ノーブラ誘惑; don't invent a doubled query.
            if a in b or b in a:
                continue
            _add_q(a + b)
        for a, b in (
            ("満員", "電車"),
            ("滿員", "電車"),
            ("媚薬", "オイル"),
            ("媚藥", "オイル"),
            ("乳首", "開発"),
            ("乳首", "イキ"),
            ("巨乳", "OL"),
            ("美乳", "OL"),
            ("地味", "眼鏡"),
            ("地味", "メガネ"),
            ("眼鏡", "OL"),
            ("メガネ", "OL"),
            ("美人", "OL"),
            ("声我慢", "SEX"),
        ):
            if any(k.casefold() == a.casefold() for k in keywords) and any(
                k.casefold() == b.casefold() for k in keywords
            ):
                _add_q(a + b)
        # Glasses + plain look: catalog often uses メガネ even when the title has 眼鏡
        if any(not _is_weak_theme_token(k) and k in _THEME_KEYWORD_ALIASES for k in keywords):
            if any(k == "地味" for k in keywords):
                _add_q("地味眼鏡")
                _add_q("地味メガネ")

    def _add_singles(singles: list[str], *, include_weak: bool) -> None:
        for kw in singles:
            if not include_weak and _is_weak_theme_token(kw):
                continue
            _add_q(kw)
            for alias in _theme_keyword_aliases(kw):
                if alias != kw and (include_weak or not _is_weak_theme_token(alias)):
                    _add_q(alias)

    if selected_only:
        # Explicit re-search queries every selected token (the user asked for it).
        _add_pairs()
        _add_singles(list(keywords), include_weak=True)
        return queries[:10]

    # Covering phrase first (息子の家庭教師), then theme nouns, then 彼女の妹.
    if early:
        _add_singles(early, include_weak=True)
    if distinctive:
        _add_singles(distinctive, include_weak=False)
    if distinctive:
        _add_pairs()
    if late:
        _add_singles(late, include_weak=True)
    if distinctive:
        for p in _title_sibling_phrases(title):
            if len(queries) >= 8:
                break
            if not p or p in _WEAK_THEME_TOKENS or len(p) < 4:
                continue
            if any(p.startswith(k) for k in distinctive):
                _add_q(p)
    elif not early and not late:
        # No theme noun and no phrase: series scraps, then the weak tokens.
        for p in _title_sibling_phrases(title)[:6]:
            if p not in _WEAK_THEME_TOKENS and len(p) >= 4:
                _add_q(p)
        _add_singles(list(keywords), include_weak=True)
    elif not early and not distinctive:
        # Phrase only (彼女の妹): the phrase is already queued; halves follow
        # when nothing stronger exists.
        _add_singles(
            [k for k in keywords if k not in late and k not in early],
            include_weak=True,
        )
    # 1–2 keywords may still query a weak token, but only after stronger ones.
    if distinctive and len(keywords) <= 2:
        _add_singles(
            [k for k in keywords if _is_weak_theme_token(k)],
            include_weak=True,
        )
    return queries[:8]


def _keyword_fallback_singles(keywords: list[str] | None) -> list[str]:
    """One-token queries for when multi-hit / compound search is empty or thin.

    Order: the longer chip, then theme nouns it does not cover, then the
    shorter pieces (家庭教師 after 息子の家庭教師), then weak kinship
    (息子 / ママ). Aliases follow their chip (10秒挿入 also searches 10秒で挿入).
    """
    items = [str(k or "").strip() for k in (keywords or []) if str(k or "").strip()]
    parts = _subsumed_keyword_parts(items)
    compounds = [tok for tok in items if any(_chip_contains_part(tok, part) for part in parts)]
    auto_rest = [
        tok
        for tok in items
        if tok not in compounds and tok not in parts and _is_auto_theme_keyword(tok)
    ]
    subsumed = [tok for tok in items if tok in parts]
    rest = [tok for tok in items if tok not in compounds and tok not in auto_rest and tok not in subsumed]
    ordered = compounds + auto_rest + subsumed + rest
    queries: list[str] = []

    def _add(q: str) -> None:
        q = (q or "").strip()
        if _keyword_token_ok(q) and q not in queries:
            queries.append(q)

    for tok in ordered:
        _add(tok)
        for alias in _theme_keyword_aliases(tok):
            if alias != tok:
                _add(alias)
    return queries[:12]


def _auto_keyword_needs_single_fallback(
    *,
    explicit: bool,
    min_hits: int,
    strict_n: int,
    max_n: int,
) -> bool:
    """True when the multi-hit pass found nothing usable, or only one work.

    Two or more multi-hits are enough — do not pad the cap with singles.
    Zero or one is too few for a ≥3-keyword title: individual chips may fill
    the remaining slots, up to max_n. Interactive re-search never falls back.
    """
    if explicit or min_hits <= 1 or max_n <= 0:
        return False
    if strict_n >= 2 or strict_n >= max_n:
        return False
    return True


def _keyword_list_has_edition_marker(raw) -> bool:
    """True when a stored chip list still contains BOD / VOL / Blu-ray junk."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,，、・/|]+", raw)
    if not isinstance(raw, (list, tuple)):
        return False
    return any(_contains_edition_marker(str(item or "")) for item in raw)


def _stamp_theme_keywords(payload: dict) -> dict:
    """Attach theme_keywords + keyword_queries on a work so the UI does not re-guess.

    Keeps a non-empty list already on the payload. Fills from the title otherwise.
    A stored list that still contains an edition/format tag (BOD, VOL, Blu-ray)
    is stale: re-extract from the title instead of dropping the tag and keeping
    the thin remainder (BOD + 中出し → 中出し only).
    """
    if not isinstance(payload, dict):
        return payload
    title = str(payload.get("title") or "")
    actress = str(payload.get("actress") or "").strip() or None
    existing = payload.get("theme_keywords")
    stale = _keyword_list_has_edition_marker(existing) or _keyword_list_has_edition_marker(
        payload.get("keyword_queries")
    )
    if isinstance(existing, list) and existing and not stale:
        kws = _normalize_keyword_list(existing)
    else:
        kws = _extract_title_theme_keywords(title, actress=actress)
    existing_q = payload.get("keyword_queries")
    if isinstance(existing_q, list) and existing_q and not stale:
        queries = _normalize_keyword_list(existing_q, limit=8)
    else:
        queries = _keyword_search_queries(title, kws, selected_only=False)
    payload["theme_keywords"] = kws
    payload["keyword_queries"] = queries
    return payload


def _recompute_theme_keywords(payload: dict) -> dict:
    """Replace chips from the current extractor whenever a title is present.

    Cached pre-#15 lists (a lone 家庭教師, or VOL/OL junk) and later BOD /
    Blu-ray lists must not stick on read or re-identify. An empty title falls
    back to _stamp_theme_keywords.
    """
    if not isinstance(payload, dict):
        return payload
    title = str(payload.get("title") or "").strip()
    if not title:
        return _stamp_theme_keywords(payload)
    actress = str(payload.get("actress") or "").strip() or None
    kws = _extract_title_theme_keywords(title, actress=actress)
    payload["theme_keywords"] = kws
    payload["keyword_queries"] = _keyword_search_queries(title, kws, selected_only=False)
    return payload


def _stamp_listed_work_keywords(payload: dict) -> dict:
    """Recompute chips on the main work and every listed candidate or result.

    Title/image multi-candidate payloads used to stamp only the top-level work.
    The other vertical card (DANDY-893 next to DANDYA-001) then had no
    theme_keywords, so the UI could not render selectable chips on it.
    """
    if not isinstance(payload, dict):
        return payload
    _recompute_theme_keywords(payload)
    for key in ("candidates", "results"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            if not str(item.get("title") or "").strip():
                continue
            _recompute_theme_keywords(item)
    return payload


def _preserve_related_bucket(raw: dict) -> dict:
    """Re-enrich a related row without dropping its bucket or hit keywords.

    enrich_title_candidate always sets line=candidate. Doing that to a
    results[].related_by_title row turned 關鍵字 into 片名候選 and the
    carousel cap then dropped the keyword bucket.
    """
    why = str(raw.get("why") or "片名相近")
    item = enrich_title_candidate(raw, why=why)
    for field in ("line", "keyword_hits", "matched_keywords", "hit_keywords"):
        val = raw.get(field)
        if val not in (None, "", []):
            item[field] = val
    item["line"] = _related_line_of(item)
    if item["line"] == "keyword":
        matched = item.get("matched_keywords") or item.get("hit_keywords")
        if not isinstance(matched, list):
            matched = []
        item["matched_keywords"] = _normalize_keyword_list(matched)
        item["hit_keywords"] = item["matched_keywords"]
    return item


def _find_related_by_keywords(
    title: str,
    *,
    exclude_code: str | None = None,
    actress: str | None = None,
    max_n: int = 5,
    budget_sec: float = 6.0,
    already: set[str] | None = None,
    keywords: list[str] | None = None,
    min_hits: int | None = None,
) -> list[dict]:
    """Up to max_n works matching title theme keywords; more hits rank higher.

    Default (keywords is None): extract from the title.
    If the title yields ≥3 keywords, prefer ≥2 hits and search a covering
    phrase first (息子の家庭教師 before 家庭教師). Rows that only hit the
    shorter pieces stay out of that strict pass. When the pass finds nothing
    or only one usable work, fall back to individual chips: the phrase, then
    uncovered theme nouns, then the shorter pieces, then 息子 / ママ. Caps
    stay maxima: do not pad once two or more multi-hits exist, and never
    invent a non-matching row.

    If the title yields only 1–2 keywords, those may define the bucket.

    Explicit keywords (interactive re-search): match only that set.
    ≥2 selected → require ≥2 hits among them (multi-hit / AND-style).
    Exactly 1 selected → that keyword may fill the row (up to max_n, no junk pad).
    Re-search does not fall back to unselected singles.
    """
    import time as _time

    if max_n <= 0:
        return []
    explicit = keywords is not None
    if explicit:
        keywords = _normalize_keyword_list(keywords)
    else:
        keywords = _extract_title_theme_keywords(title, actress=actress)
    if len(keywords) < 1:
        return []
    if explicit:
        # User-picked tokens all count, including relationship chips (彼女 / 妹).
        if min_hits is None:
            min_hits = _selected_keyword_min_hits(keywords)
    elif min_hits is None:
        min_hits = _keyword_min_hits(keywords)
    min_hits = int(min_hits or 0)
    t0 = _time.monotonic()
    budget = float(budget_sec) if budget_sec and budget_sec > 0 else 6.0
    exclude = ""
    if exclude_code and parse_code_parts(str(exclude_code)):
        exclude = format_display_code(str(exclude_code))
    seen: set[str] = set(already or ())
    if exclude:
        seen.add(exclude)

    queries = _keyword_search_queries(title, keywords, selected_only=explicit)
    # Leave a slice for single-keyword fallback. Distinctive singles are already
    # first in `queries`; this reserve is for weak chips the leading list omits
    # (息子 / ママ) when the multi-hit pass comes back empty.
    primary_end = t0 + budget
    if not explicit and min_hits > 1 and budget > 1.5:
        primary_end = t0 + (budget * 0.65)

    strict: dict[str, tuple[float, dict]] = {}
    loose: dict[str, tuple[float, dict]] = {}
    fetched: set[str] = set()

    def _remember(bucket: dict[str, tuple[float, dict]], code: str, sc: float, row: dict) -> None:
        prev = bucket.get(code)
        if prev is None or sc > prev[0]:
            bucket[code] = (sc, row)

    def _ingest(rows: list[dict]) -> None:
        for c in rows[:12]:
            code_raw = str(c.get("code") or "").strip()
            if not code_raw or not parse_code_parts(code_raw):
                continue
            code = format_display_code(code_raw)
            if code in seen:
                continue
            hits, base, matched, theme_hits = _keyword_overlap(
                str(c.get("title") or ""), keywords
            )
            if hits < 1:
                continue
            sc = base + float(c.get("score") or 0)
            weak_only = theme_hits < 1 and any(
                _is_auto_theme_keyword(k) for k in keywords
            )
            # Shorter pieces of a chip already in the set (家庭教師 under
            # 息子の家庭教師) must not occupy the strict bucket. They fill
            # only when the compound / multi-hit pass is empty or has one row.
            redundant = (not explicit) and _redundant_substring_only(matched, keywords)
            # Keep single-keyword rows aside. They fill the bucket only when
            # the multi-hit pass is empty or too thin.
            if not explicit and min_hits > 1:
                _remember(loose, code, sc, c)
            if hits < min_hits or redundant:
                continue
            if not explicit and weak_only:
                continue
            _remember(strict, code, sc, c)

    def _fetch_query(q: str) -> list[dict]:
        if not q or q in fetched:
            return []
        fetched.add(q)
        if _time.monotonic() - t0 > budget:
            return []
        rows: list[dict] = []
        for fetch in (
            fetch_avbase_title_results,
            fetch_jav321_title_results,
            fetch_javlibrary_title_results,
        ):
            if _time.monotonic() - t0 > budget:
                break
            try:
                rows.extend(fetch(q, actress=None)[:10])
            except Exception:
                pass
            if rows:
                break
        return rows

    for q in queries:
        if _time.monotonic() >= min(primary_end, t0 + budget):
            break
        _ingest(_fetch_query(q))

    if _auto_keyword_needs_single_fallback(
        explicit=explicit,
        min_hits=min_hits,
        strict_n=len(strict),
        max_n=max_n,
    ):
        for q in _keyword_fallback_singles(keywords):
            if _time.monotonic() - t0 > budget:
                break
            if q in fetched:
                continue
            _ingest(_fetch_query(q))

    use_singles = _auto_keyword_needs_single_fallback(
        explicit=explicit,
        min_hits=min_hits,
        strict_n=len(strict),
        max_n=max_n,
    )

    def _loose_rank(pair: tuple[float, dict]) -> tuple:
        sc, c = pair
        hits, _base, matched, theme_hits = _keyword_overlap(
            str(c.get("title") or ""), keywords
        )
        compound = 1 if any(_is_relation_phrase(k) for k in matched) else 0
        return (1 if theme_hits else 0, compound, hits, sc)

    def _strict_rank(pair: tuple[float, dict]) -> tuple:
        sc, c = pair
        _hits, _base, matched, _theme = _keyword_overlap(
            str(c.get("title") or ""), keywords
        )
        cover = 1 if any(_compound_covers_auto_theme(tok, keywords) for tok in matched) else 0
        return (cover, sc)

    if use_singles:
        ordered_rows: list[tuple[float, dict]] = sorted(
            strict.values(), key=_strict_rank, reverse=True
        )
        used = {
            format_display_code(str(c.get("code") or ""))
            for _sc, c in ordered_rows
            if c.get("code")
        }
        rest = [pair for code, pair in loose.items() if code not in used]
        rest.sort(key=_loose_rank, reverse=True)
        ordered_rows.extend(rest)
        emit_floor = 1
        allow_weak = True
    else:
        ordered_rows = sorted(strict.values(), key=_strict_rank, reverse=True)
        emit_floor = min_hits
        allow_weak = explicit

    out: list[dict] = []
    for _sc, c in ordered_rows:
        hits, _base, matched, theme_hits = _keyword_overlap(
            str(c.get("title") or ""), keywords
        )
        if hits < emit_floor:
            continue
        # Theme terms lead the automatic bucket. Relationship-only overlap
        # still scores lower, but does not pad ahead of ノーブラ / 巨乳 when
        # multi-hit rows already exist. Single-keyword fallback may keep
        # 息子 / ママ so the bucket is not empty.
        if not allow_weak and theme_hits < 1 and any(
            _is_auto_theme_keyword(k) for k in keywords
        ):
            continue
        why = f"關鍵字×{hits}"
        item = enrich_title_candidate(c, why=why)
        item["line"] = "keyword"
        item["why"] = why
        item["keyword_hits"] = hits
        item["matched_keywords"] = matched
        item["hit_keywords"] = matched
        out.append(item)
        seen.add(format_display_code(str(c.get("code") or "")))
        if len(out) >= max_n:
            break
    return out[:max_n]


def _actress_name_matches(query: str, candidate: str) -> bool:
    """True when the catalog billing contains the queried actress name.

    A shorter fragment of the query (or a different person in the same row)
    does not count. Used so the 同女優 bucket is never padded with junk.
    """
    name = (query or "").strip()
    act = (candidate or "").strip()
    if len(name) < 2 or not act:
        return False
    compact = re.sub(r"[\s　・·．.]+", "", name)
    parts = [act] + re.split(r"[\s　・·．./|、,]+", act)
    for piece in parts:
        pc = re.sub(r"[\s　・·．.]+", "", piece or "")
        if not pc:
            continue
        if compact == pc or compact in pc:
            return True
    return False


def _find_related_by_actress(
    actress: str,
    *,
    exclude_code: str | None = None,
    max_n: int = 3,
    budget_sec: float = 6.0,
    already: set[str] | None = None,
) -> list[dict]:
    """Same-actress bucket: up to max_n other works (cap 3). No junk pad."""
    import time as _time

    name = (actress or "").strip()
    if not name or max_n <= 0:
        return []
    t0 = _time.monotonic()
    budget = float(budget_sec) if budget_sec and budget_sec > 0 else 6.0
    exclude = ""
    if exclude_code and parse_code_parts(str(exclude_code)):
        exclude = format_display_code(str(exclude_code))
    seen: set[str] = set(already or ())
    if exclude:
        seen.add(exclude)
    out: list[dict] = []

    queries = [name]
    compact = re.sub(r"[\s　・·．.]+", "", name)
    if compact and compact not in queries:
        queries.append(compact)

    ranked: list[tuple[float, dict]] = []
    for q in queries:
        if _time.monotonic() - t0 > budget:
            break
        try:
            rows = fetch_avbase_title_results(q, actress=name)[:12]
        except Exception:
            rows = []
        for c in rows:
            code_raw = str(c.get("code") or "").strip()
            if not code_raw or not parse_code_parts(code_raw):
                continue
            code = format_display_code(code_raw)
            if code in seen:
                continue
            act = str(c.get("actress") or "")
            if not _actress_name_matches(name, act):
                continue
            sc = float(c.get("score") or 0) * 0.5 + 1.0
            ranked.append((sc, c))
            seen.add(code)

    ranked.sort(key=lambda x: x[0], reverse=True)
    # Cap only — never pad; return however many real same-actress hits we found (≤ max_n)
    target = max(0, min(int(max_n), 3))
    for _sc, c in ranked[:target]:
        item = enrich_title_candidate(c, why="同演員")
        item["line"] = "actress"
        item["why"] = "同演員"
        out.append(item)
    return out[:target]


def find_related_by_title(
    title: str | None,
    exclude_code: str | None = None,
    max_n: int = 5,
    actress: str | None = None,
    budget_sec: float = 8.0,
    *,
    seed: list | None = None,
    fill_theme: bool | None = None,
    fill_keyword: bool | None = None,
    fill_actress: bool | None = None,
) -> list[dict]:
    """Related works in three independent buckets (caps, not quotas):

    1. Same title / series / name-similarity — up to 5
    2. Kanji keyword matches from JP title — up to 5 (separate, not a top-up)
    3. Same actress — up to 3

    Order: title → keyword → actress. Deduplicate by code. Never pad with junk;
    empty/short buckets are fine. Soft deadline for identify.

    seed: existing related to keep (incremental backfill). fill_* default to
    True only when that bucket is under its cap after seeding.
    """
    import time as _time

    title = normalize_ocr_title(title) or (title or "").strip()
    title_cap = max(0, min(int(max_n) if max_n else RELATED_THEME_CAP, RELATED_THEME_CAP))
    keyword_cap = RELATED_KEYWORD_CAP
    actress_cap = RELATED_ACTRESS_CAP
    if title_cap <= 0 or not is_usable_title(title):
        # Keep any seeded related; only actress-fill if that bucket is short
        seeded = _cap_related_buckets(seed or [])
        _t, _k, a_n = _related_bucket_counts(seeded)
        want_act = fill_actress if fill_actress is not None else True
        if want_act and (actress or "").strip() and a_n < actress_cap:
            try:
                extra = _find_related_by_actress(
                    actress,
                    exclude_code=exclude_code,
                    max_n=actress_cap - a_n,
                    budget_sec=min(5.0, float(budget_sec) if budget_sec else 5.0),
                )
                seeded = _cap_related_buckets(list(seeded) + list(extra or []))
            except Exception:
                pass
        return seeded
    t0 = _time.monotonic()
    budget = float(budget_sec) if budget_sec and budget_sec > 0 else 8.0

    def _left() -> float:
        return budget - (_time.monotonic() - t0)

    # Reserve time so keyword + actress buckets can still run after title search
    _later_reserve = 5.0 if (actress or "").strip() else 3.5

    def _left_title() -> float:
        return _left() - _later_reserve

    exclude = ""
    if exclude_code and parse_code_parts(str(exclude_code)):
        exclude = format_display_code(str(exclude_code))
    seen: set[str] = {exclude} if exclude else set()
    out: list[dict] = []
    for raw in seed or []:
        if not isinstance(raw, dict):
            continue
        code_raw = str(raw.get("code") or "").strip()
        if not code_raw or not parse_code_parts(code_raw):
            continue
        code = format_display_code(code_raw)
        if code in seen:
            continue
        seen.add(code)
        item = dict(raw)
        item["code"] = code
        item["line"] = _related_line_of(item)
        out.append(item)
    theme_n0, keyword_n0, actress_n0 = _related_bucket_counts(out)
    if fill_theme is None:
        fill_theme = theme_n0 < title_cap
    if fill_keyword is None:
        fill_keyword = keyword_n0 < keyword_cap
    if fill_actress is None:
        fill_actress = actress_n0 < actress_cap
    keywords = _extract_title_theme_keywords(title, actress=actress)
    phrases = _title_sibling_phrases(title)

    def _push(raw: dict, why: str = "片名相近", line: str = "theme") -> None:
        nonlocal out
        code_raw = str(raw.get("code") or "").strip()
        if not code_raw or not parse_code_parts(code_raw):
            return
        code = format_display_code(code_raw)
        if code in seen:
            return
        seen.add(code)
        item = enrich_title_candidate(raw, why=why)
        item["line"] = line
        item["why"] = why
        if line == "keyword":
            matched = raw.get("matched_keywords") or raw.get("hit_keywords")
            if not isinstance(matched, list) or not matched:
                matched = _matched_theme_keywords(
                    str(raw.get("title") or item.get("title") or ""),
                    keywords,
                )
            item["matched_keywords"] = _normalize_keyword_list(matched)
            item["hit_keywords"] = item["matched_keywords"]
            hits = raw.get("keyword_hits")
            item["keyword_hits"] = int(hits) if hits else len(item["matched_keywords"])
        out.append(item)

    # --- 1) Title / same-series (cap 5, no pad) ---
    hit = None
    if fill_theme and _left_title() > 1.0:
        try:
            hit = search_by_title(title, actress=actress)
        except Exception:
            hit = None
    if hit:
        cands = list(hit.get("candidates") or [])
        if hit.get("code") and parse_code_parts(str(hit["code"])):
            head = {
                "code": hit.get("code"),
                "title": hit.get("title"),
                "actress": hit.get("actress"),
                "studio": hit.get("studio"),
                "cover": hit.get("cover"),
                "cid": hit.get("cid"),
                "score": hit.get("score"),
                "source": hit.get("source"),
                "title_zh": hit.get("title_zh"),
            }
            if not any(
                format_display_code(str(c.get("code") or ""))
                == format_display_code(str(head["code"]))
                for c in cands
                if c.get("code")
            ):
                cands = [head] + cands
        ranked = []
        for c in cands:
            code = format_display_code(str(c.get("code") or "")) if c.get("code") else ""
            if not code or code in seen:
                continue
            ok, sc = _is_title_theme_match(
                title, str(c.get("title") or ""), keywords=keywords, phrases=phrases
            )
            hit_score = float(c.get("score") or 0)
            # Accept same-series theme match OR high catalog title score
            if not ok and hit_score < 0.72:
                continue
            if not ok:
                sc = max(float(sc), hit_score * 0.9)
            ranked.append((sc, c))
        ranked.sort(key=lambda x: x[0], reverse=True)
        for _sc, c in ranked:
            _push(c, why="片名相近", line="theme")
            if sum(1 for x in out if str(x.get("line")) == "theme") >= title_cap:
                break

    # Sibling phrase catalog search (series templates / mid-title hooks)
    theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
    if fill_theme and theme_n < title_cap and _left_title() > 1.2:
        queries: list[str] = []
        try:
            for q in title_query_variants(title)[1:6]:
                if q and q not in queries and len(re.sub(r"\s+", "", q)) >= 6:
                    queries.append(q)
        except Exception:
            pass
        for q in phrases:
            if q and q not in queries:
                queries.append(q)
        ranked2: list[tuple[float, dict]] = []
        for q in queries[:10]:
            if theme_n >= title_cap or _left_title() < 0.8:
                break
            try:
                more = fetch_avbase_title_results(q, actress=None)[:12]
            except Exception:
                more = []
            for c in more:
                code = format_display_code(str(c.get("code") or "")) if c.get("code") else ""
                if not code or code in seen:
                    continue
                ok, sc = _is_title_theme_match(
                    title, str(c.get("title") or ""), keywords=keywords, phrases=phrases
                )
                if not ok:
                    continue
                ct = re.sub(r"\s+", "", str(c.get("title") or ""))
                hits = _keyword_hit_count(str(c.get("title") or ""), keywords)
                sim = title_similarity(title, str(c.get("title") or ""))
                sc = float(sc) + float(hits) * 0.03 + float(sim) * 0.15
                if q and re.sub(r"\s+", "", q) in ct:
                    sc += 0.08
                ranked2.append((sc, c))
        ranked2.sort(key=lambda x: x[0], reverse=True)
        if ranked2:
            top = ranked2[0][0]
            ranked2 = [x for x in ranked2 if x[0] >= max(0.48, top - 0.22)]
        for _sc, c in ranked2:
            _push(c, why="片名相近", line="theme")
            theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
            if theme_n >= title_cap:
                break

    # Demo theme package ONLY for the MIDA-616 offline demo path
    theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
    if fill_theme and theme_n < title_cap and exclude and is_mida616(exclude):
        try:
            for r in related_from_demo():
                why = str(r.get("why") or "")
                ln = str(r.get("line") or "")
                # Actress-bucket rows stay actress even if the note mentions 主題.
                if ln == "actress" or _why_is_actress_bucket(why):
                    continue
                _push(r, why=why or "主題相近", line="theme")
                theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
                if theme_n >= title_cap:
                    break
        except Exception:
            pass

    # --- 2) Keywords — independent bucket (cap 5), always try when keywords exist ---
    # Dedicated floor: a long 片名 search used to exhaust `_left()` and skip this
    # bucket, so code/title lookups showed 片名/同演員 with no 關鍵字相關.
    keyword_items_added = 0
    keyword_have = sum(1 for x in out if str(x.get("line")) == "keyword")
    if fill_keyword and keywords and keyword_have < keyword_cap:
        try:
            kw_budget = max(RELATED_KEYWORD_BUDGET, min(6.0, max(0.0, _left())))
            for r in _find_related_by_keywords(
                title,
                exclude_code=exclude or None,
                actress=actress,
                max_n=keyword_cap - keyword_have,
                budget_sec=kw_budget,
                already=seen,
            ):
                # Keep this bucket independent. A fragment such as 肉欲教育ママ used
                # to be relabeled 片名相近 because containment similarity is ~0.92,
                # which emptied 關鍵字 on the DANDY tutor-title path (hint became
                # 片名／同演員 only). Codes already listed as 片名 stay there via seen.
                _push(r, why=str(r.get("why") or "名稱關鍵字"), line="keyword")
                keyword_items_added += 1
                if keyword_items_added >= keyword_cap:
                    break
        except Exception:
            pass

    theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
    keyword_n = sum(1 for x in out if str(x.get("line")) == "keyword")

    # --- 3) Actress — independent bucket (cap 3), always try when actress known ---
    # Dedicated budget: title/keyword overruns must not skip 同女優.
    if fill_actress and (actress or "").strip():
        try:
            for r in _find_related_by_actress(
                actress,
                exclude_code=exclude or None,
                max_n=actress_cap,
                budget_sec=4.0,
                already=seen,
            ):
                _push(r, why=str(r.get("why") or "同演員"), line="actress")
                if sum(1 for x in out if str(x.get("line")) == "actress") >= actress_cap:
                    break
        except Exception:
            pass

    # Demo actress siblings for MIDA when online actress search is empty
    if (
        fill_actress
        and actress
        and not any(str(x.get("line")) == "actress" for x in out)
        and exclude
        and is_mida616(exclude)
    ):
        try:
            for r in related_from_demo():
                why = str(r.get("why") or "")
                if "女優" not in why:
                    continue
                _push(r, why=why or "同演員", line="actress")
                if sum(1 for x in out if str(x.get("line")) == "actress") >= actress_cap:
                    break
        except Exception:
            pass

    # Stable display order: theme → keyword → actress (caps already applied).
    # Resolve line again so a 同女優 note cannot remain inside the title block.
    normalized: list[dict] = []
    for x in out:
        if not isinstance(x, dict):
            continue
        item = dict(x)
        item["line"] = _related_line_of(item)
        if item["line"] == "keyword":
            matched = item.get("matched_keywords") or item.get("hit_keywords")
            if not isinstance(matched, list) or not matched:
                matched = _matched_theme_keywords(str(item.get("title") or ""), keywords)
            item["matched_keywords"] = _normalize_keyword_list(matched)
            item["hit_keywords"] = item["matched_keywords"]
        normalized.append(item)
    theme_items = [x for x in normalized if x.get("line") == "theme"][:title_cap]
    keyword_items = [x for x in normalized if x.get("line") == "keyword"][:keyword_cap]
    actress_items = [x for x in normalized if x.get("line") == "actress"][:actress_cap]
    other_items = [
        x
        for x in normalized
        if x.get("line") not in {"theme", "keyword", "actress"}
    ]
    # Within keyword tier: distinctive theme hits, then more keyword hits.
    keyword_items.sort(key=_keyword_related_sort_key, reverse=True)
    return theme_items + keyword_items + actress_items + other_items



def attach_related_by_title(
    result: dict,
    *,
    budget_sec: float = 14.0,
    per_item: bool = True,
) -> dict:
    """Mutate identify payload to include related_by_title.

    Independent buckets (caps, not quotas — no junk pad):
      title/series ≤5 → keyword ≤5 → actress ≤3
    Order preserved; dedupe by code. Total related ≤ ~13.

    per_item=False (multi): only fill top-level related_by_title once, skip results[].
    """
    if not result or not result.get("ok"):
        result = result or {}
        result.setdefault("related_by_title", [])
        return result
    title = result.get("title")
    code = result.get("code")
    if code and (str(code) == "TITLE-SEARCH" or not parse_code_parts(str(code))):
        code = None
    existing = result.get("related_by_title")
    had_existing = isinstance(existing, list) and bool(existing)
    if not had_existing:
        try:
            result["related_by_title"] = find_related_by_title(
                title,
                exclude_code=str(code) if code else None,
                max_n=RELATED_THEME_CAP,
                actress=result.get("actress"),
                budget_sec=max(budget_sec, 16.0),
            )
        except Exception:
            result["related_by_title"] = []
    else:
        # Keep existing related as-is (no re-probe / no wipe); cap 5+5+3
        result["related_by_title"] = _cap_related_buckets(existing)
        # Incremental top-up: only buckets still under cap (skip network when full)
        try:
            rel = list(result.get("related_by_title") or [])
            t, k, a = _related_bucket_counts(rel)
            need_theme = t < RELATED_THEME_CAP and is_usable_title(str(title or ""))
            need_kw = k < RELATED_KEYWORD_CAP and is_usable_title(str(title or ""))
            need_act = a < RELATED_ACTRESS_CAP and bool((result.get("actress") or "").strip())
            if need_theme or need_kw or need_act:
                filled = find_related_by_title(
                    title,
                    exclude_code=str(code) if code else None,
                    max_n=RELATED_THEME_CAP,
                    actress=result.get("actress"),
                    budget_sec=max(budget_sec, 16.0),
                    seed=rel,
                    fill_theme=need_theme,
                    fill_keyword=need_kw,
                    fill_actress=need_act,
                )
                result["related_by_title"] = _merge_related_for_cache(rel, filled)
        except Exception:
            pass

    # Attach Chinese titles on related + main when missing (per-request budget)
    try:
        attach_chinese_titles(
            result,
            related_network=True,
            related_budget_sec=OFFLINE_CACHE_TITLE_ZH_BUDGET,
        )
    except Exception:
        pass

    _stamp_listed_work_keywords(result)
    _finalize_related_note(result)
    if not per_item:
        return result
    # Also attach on each results[] entry if present (single-image path)
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        if item.get("related_by_title"):
            fixed = []
            for r in item.get("related_by_title") or []:
                if not isinstance(r, dict):
                    continue
                fixed.append(_preserve_related_bucket(r))
            item["related_by_title"] = fixed[:13]
            try:
                attach_chinese_titles(
                    item,
                    related_network=True,
                    related_budget_sec=OFFLINE_CACHE_TITLE_ZH_BUDGET,
                )
            except Exception:
                pass
            _stamp_listed_work_keywords(item)
            continue
        it_title = item.get("title") or title
        it_code = item.get("code")
        if it_code and (str(it_code) == "TITLE-SEARCH" or not parse_code_parts(str(it_code))):
            it_code = None
        try:
            item["related_by_title"] = find_related_by_title(
                it_title,
                exclude_code=str(it_code) if it_code else None,
                max_n=5,
                actress=item.get("actress"),
                budget_sec=min(budget_sec, 10.0),
            )
            attach_chinese_titles(
                item,
                related_network=True,
                related_budget_sec=OFFLINE_CACHE_TITLE_ZH_BUDGET,
            )
        except Exception:
            item["related_by_title"] = []
        _stamp_listed_work_keywords(item)
    _finalize_related_note(result)
    return result



def collect_images_from_request() -> list[tuple[bytes, str | None]]:
    """Accept multiple uploads via `images` and/or repeated `image` fields.

    Dedupes by content hash so clients that append both field names do not
    double-count the same files (progress used to show 6 when 3 were selected).
    """
    out: list[tuple[bytes, str | None]] = []
    seen: set[str] = set()
    for key in ("images", "image"):
        try:
            files = request.files.getlist(key)
        except Exception:
            files = []
        for f in files:
            if not f or not getattr(f, "filename", None):
                continue
            try:
                data = f.read() or b""
            except Exception:
                data = b""
            if not data:
                continue
            digest = f"{len(data)}:{hash(data)}"
            if digest in seen:
                continue
            seen.add(digest)
            out.append((data, f.filename))
    return out


def build_multi_fail_stub(job: dict, *, why: str = "多圖未找到資料") -> dict:
    """Lightweight gallery card for a unique vision code/title that failed resolve."""
    row = job.get("row") or {}
    code_raw = (job.get("code") or row.get("code") or "").strip()
    title = (job.get("title") or row.get("title") or "").strip() or None
    actress = (row.get("actress") or None)
    studio = (row.get("studio") or None)
    code = None
    cid = None
    cover = None
    stills: list[str] = []
    if code_raw and parse_code_parts(code_raw):
        code = format_display_code(code_raw)
        try:
            cid, cover = resolve_cover_cid(code)
            if cid:
                stills = still_urls(cid, 10)
        except Exception:
            cid, cover, stills = None, None, []
        # Fallback to primary CID even if probe failed (client may still load)
        if not cid:
            try:
                cid = code_to_cid(code)
                if cid:
                    cover = cover_url(cid)
                    stills = still_urls(cid, 10)
            except Exception:
                pass
    return {
        "ok": True,
        "code": code or (code_raw or "TITLE-SEARCH"),
        "title": title,
        "actress": actress,
        "studio": studio,
        "cid": cid,
        "cover": cover,
        "stills": stills,
        "related": [],
        "related_by_title": [],
        "candidates": [],
        "why": why,
        "line": "multi",
        "stub": True,
        "message": why,
        "vision_used": bool(row.get("vision_used")),
        "search_mode": "code" if code else "title",
        "from_image_index": row.get("index"),
    }


def _append_unique_note(payload: dict, note: str) -> None:
    text = (note or "").strip()
    if not text or not isinstance(payload, dict):
        return
    for key in ("message", "related_note"):
        prev = str(payload.get(key) or "").strip()
        if text in prev:
            continue
        payload[key] = (prev + "；" + text).strip("；") if prev else text


def _collect_visual_pool(*groups) -> list[dict]:
    """Coded works for one image's visual compare, first occurrence wins."""
    pool: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            cand = _payload_as_visual_candidate(raw)
            if not cand:
                continue
            code = str(cand.get("code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            pool.append(cand)
    return pool


def _mark_visual_mismatch(payload: dict, note: str) -> dict:
    payload["visual_lock"] = False
    payload["visual_mismatch"] = True
    payload["visual_note"] = note
    _append_unique_note(payload, note)
    return payload


def verify_work_against_image(
    result: dict,
    image_bytes: bytes | None,
    *,
    api_key: str | None = None,
    vision_title: str | None = None,
    budget_s: float | None = None,
) -> dict:
    """Visually re-check one uploaded image against its own catalog candidates.

    Same lock rule as single-image identify: person + clothes (cover, then
    stills). A locking candidate replaces the slot. A hit that does not lock
    is not kept as a verified match — another candidate is preferred, otherwise
    the card stays with an honest 未核對圖片 note. Keywords are restamped from
    the title that remains, so slots do not share each other's chips.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    out = dict(result)
    key = (api_key or get_gemini_api_key() or "").strip()
    if getattr(_BATCH, "active", False):
        remain = _seconds_left(_batch_deadline())
        jacket_cut = bool(getattr(_BATCH, "jacket_incomplete", False))
        already_locked = bool(out.get("visual_lock")) or _hit_visually_locked(out)
        # A finished jacket lock stays. Do not spend a second pass undoing it.
        if already_locked and (jacket_cut or remain < float(VISUAL_COMPARE_BUDGET)):
            return out
        if jacket_cut or remain < float(VISUAL_COMPARE_BUDGET):
            pool = _collect_visual_pool([out], out.get("candidates"))
            # One printed code is not a volume choice. Two or more is.
            if len(pool) >= 2 and not already_locked:
                return _mark_lock_incomplete(out, pool)
            out["visual_lock"] = False
            return out
    if not image_bytes or not key:
        out["visual_lock"] = False
        return out
    # budget_s must not shrink the compare. Callers that are out of time
    # already returned a retryable slot above.
    pool = _collect_visual_pool([out], out.get("candidates"))
    if not pool:
        _mark_visual_mismatch(out, "未核對圖片（沒有可比較的封面或劇照）")
        _recompute_theme_keywords(out)
        return out
    ranked, meta = rank_candidates_by_visual(
        image_bytes,
        pool,
        api_key=key,
        budget_s=VISUAL_COMPARE_BUDGET,
    )
    locked = bool((meta or {}).get("visual_ranked") and (meta or {}).get("visual_lock"))
    if not locked:
        query = str(vision_title or "").strip()
        extras: list[dict] = []
        if is_usable_title(query):
            try:
                # Do not pass the OCR actress: a wrong name must not hide the title.
                hit = search_by_title(query)
            except Exception:
                hit = None
            if isinstance(hit, dict):
                extras.append(hit)
                extras.extend(hit.get("candidates") or [])
        wider = _collect_visual_pool(pool, extras)
        if len(wider) > len(pool):
            ranked, meta = rank_candidates_by_visual(
                image_bytes,
                wider,
                api_key=key,
                budget_s=VISUAL_COMPARE_BUDGET,
            )
            locked = bool((meta or {}).get("visual_ranked") and (meta or {}).get("visual_lock"))
    if not (meta or {}).get("visual_ranked"):
        _mark_visual_mismatch(out, "未核對圖片（無法比對封面或劇照）")
        _recompute_theme_keywords(out)
        return out
    prev = format_display_code(str(out.get("code") or ""))
    out = _apply_visual_winner(out, ranked, meta, promote_candidates=False)
    new = format_display_code(str(out.get("code") or ""))
    if prev and new and prev != new:
        winner = ranked[0] if ranked else {}
        # Do not keep the rejected work's Chinese title, stills, or related row.
        out["title_zh"] = (winner.get("title_zh") if isinstance(winner, dict) else None) or None
        out["stills"] = list((winner.get("stills") if isinstance(winner, dict) else None) or [])
        out["related_by_title"] = []
        out["related"] = []
    out["visual_lock"] = bool(locked)
    if locked:
        out["visual_mismatch"] = False
        out["visual_note"] = ""
    else:
        _mark_visual_mismatch(out, "未核對圖片（人物／衣服／姿勢與這張上傳圖不符）")
    _recompute_theme_keywords(out)
    return out


def _catalog_code_of(payload: dict | None) -> str:
    """Display code, or empty when the slot is title-only / unidentified."""
    code = str((payload or {}).get("code") or "").strip()
    if not code or code == "TITLE-SEARCH" or not parse_code_parts(code):
        return ""
    return format_display_code(code)


_SITE_CHROME_TITLES = {
    "無碼影片",
    "無碼",
    "有碼",
    "中文字幕",
    "字幕",
    "高清",
    "馬賽克",
    "破解",
    "流出",
    "live",
    "missav",
    "javdb",
}


def _is_site_chrome_title(title: str | None) -> bool:
    """Listing badges and durations are not catalog titles."""
    t = re.sub(r"[\s　]+", "", str(title or "").strip())
    if not t:
        return False
    if t.casefold() in _SITE_CHROME_TITLES:
        return True
    return bool(re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", t))


def _cjk_count(text: str | None) -> int:
    n = 0
    for c in text or "":
        o = ord(c)
        if (
            0x3040 <= o <= 0x30FF
            or 0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or c == "々"
        ):
            n += 1
    return n


def _is_decorative_overlay(title: str | None) -> bool:
    """Short jacket slogan, not the catalog title line.

    「舌技が神」 is four characters of cover art. It must not veto a 品番
    printed under the thumbnail, and title search on that slogan cannot
    recover a work whose catalog title is a different sentence.
    """
    if _is_site_chrome_title(title):
        return True
    t = normalize_ocr_title(title) or str(title or "").strip()
    t = re.sub(r"\s+", "", t)
    if not is_usable_title(t):
        return False
    return _cjk_count(t) <= 6


def _sole_product_code(text: str | None) -> tuple[str | None, list[str]]:
    """One 品番, or every distinct code when the shot is a multi-title listing.

    A grid that shows APGH-025 and APGH-012 must not collapse to whichever
    code the scorer sees first. A single caption under one thumb is safe to use.
    """
    found: list[str] = []
    seen: set[str] = set()
    for raw in extract_codes(text or ""):
        disp = format_display_code(raw)
        if not parse_code_parts(disp) or disp in seen:
            continue
        seen.add(disp)
        found.append(disp)
    if len(found) == 1:
        return found[0], found
    return None, found


def _split_title_and_code(title: str | None) -> tuple[str | None, str | None]:
    """Separate a readable 品番 from leftover title text. Drop site chrome."""
    raw = str(title or "").strip()
    if not raw:
        return None, None
    sole, _many = _sole_product_code(raw)
    cleaned = AV_CODE_RE.sub(" ", raw)
    cleaned = re.sub(r"[\s　]+", " ", cleaned).strip(" -/|・")
    if not cleaned or _is_site_chrome_title(cleaned) or not is_usable_title(cleaned):
        cleaned = None
    return sole, cleaned


def _actress_query_name(name: str | None) -> str | None:
    """Japanese cast name for catalog search. Romaji listing chrome is not a query."""
    shown = str(name or "").strip()
    compact = re.sub(r"[\s　]+", "", shown)
    if len(compact) < 2 or not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", compact):
        return None
    return shown


def _compose_read_blob(*parts: Any) -> str:
    """Every readable string, in order, without duplicate lines."""
    lines: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if isinstance(part, list):
            chunks = part
        else:
            chunks = re.split(r"[\r\n]+", str(part or ""))
        for chunk in chunks:
            s = str(chunk or "").strip()
            if not s or s in seen or s.startswith("[tesseract"):
                continue
            seen.add(s)
            lines.append(s)
    return "\n".join(lines)


# A grid prints 品番 with a hyphen (APGH-012). OCR noise such as "rake 12"
# or "shat 676" must not count, or one jacket is treated as a listing and
# its real title line is never searched.
_DASH_CODE_RE = re.compile(
    r"([A-Za-z]{2,10})[-－‐‑‒–—―ー−](\d{2,5})",
    re.I,
)


def _grid_product_codes(text: str | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _DASH_CODE_RE.finditer(text or ""):
        code = normalize_code(f"{m.group(1)}-{m.group(2)}")
        disp = format_display_code(code) if parse_code_parts(code) else ""
        if not disp or disp in seen:
            continue
        seen.add(disp)
        found.append(disp)
    return found


def _listing_chrome_in_text(blob: str | None) -> bool:
    """Site row, grid, or player chrome — not text printed on one jacket.

    A missav caption is a 品番 plus a latin name under the thumb
    (APGH-012 Yuuki Hiiragi), often with a duration or 無碼影片. Two or more
    hyphenated 品番 means a grid. LIVE is a player overlay. None of these
    are a full cover. Whitespace letter-digit pairs from a noisy OCR pass
    are not a second 品番.
    """
    text = str(blob or "")
    if re.search(
        r"(無碼影片|無碼|有碼|中文字幕|missav|javdb|javlibrary|dmm\.co\.jp|\bLIVE\b|\d{1,2}:\d{2}(?::\d{2})?)",
        text,
        re.I,
    ):
        return True
    # Caption row: hyphenated 品番 plus a latin name (APGH-012 Yuuki Hiiragi).
    # "rake 12 Haka" from a noisy jacket is not that row.
    if re.search(
        r"[A-Za-z]{2,10}[-－‐‑‒–—―ー−]\d{3,5}\s+[A-Za-z][A-Za-z.'’\- ]{1,40}",
        text,
    ):
        return True
    return len(_grid_product_codes(text)) >= 2


def _cover_full_text_allowed(vision: dict | None, blob: str | None) -> bool:
    """Full-read search only for one complete jacket and no listing row under it.

    Both must hold: vision shot is cover, and the read has no site chrome.
    Listing, grid, and UI captures keep the focused code + primary title even
    if the model also dumped neighboring lines into texts. Do not drop this gate.
    """
    shot = str((vision or {}).get("shot") or "").strip().lower()
    if shot != "cover":
        return False
    if _listing_chrome_in_text(blob):
        return False
    return True


def _full_text_search_queries(blob: str | None, *, actress: str | None = None) -> list[str]:
    """Distinctive lines from the whole read, slogan last is omitted when real lines exist.

    Manual lookup pastes every OCR string, not only the biggest phrase. A short
    cover slogan such as 舌技が神 is not a catalog query when the art also
    printed a longer title fragment, a maker mark line, or a corner 品番.
    """
    raw = (blob or "").strip()
    if not raw or raw.startswith("[tesseract"):
        return []
    pieces = [p.strip() for p in re.split(r"[\r\n]+", raw) if p.strip()]
    pieces.extend(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff々ー]{4,40}", raw))
    actress_c = re.sub(r"[\s　]+", "", actress or "")
    ranked: list[str] = []
    seen: set[str] = set()

    def _consider(piece: str) -> None:
        piece = normalize_ocr_title(piece) or str(piece or "").strip()
        piece = re.sub(r"\s+", " ", piece).strip()
        _sole, cleaned = _split_title_and_code(piece)
        piece = cleaned or ""
        if not piece or piece in seen or _is_site_chrome_title(piece) or not is_usable_title(piece):
            return
        if _is_decorative_overlay(piece):
            return
        compact = re.sub(r"[\s　]+", "", piece)
        if actress_c and compact == actress_c:
            return
        seen.add(piece)
        ranked.append(piece)

    for piece in pieces:
        _consider(piece)
    ranked.sort(key=lambda s: _cjk_count(s), reverse=True)
    queries: list[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", (q or "").strip())
        if len(q) < 4 or q in queries or _is_decorative_overlay(q) or not is_usable_title(q):
            return
        queries.append(q)

    for line in ranked:
        add(line)
        compact = re.sub(r"\s+", "", line)
        if _cjk_count(compact) >= 10:
            for n in (8, 6):
                if len(compact) < n:
                    continue
                add(compact[:n])
                mid = max(0, (len(compact) - n) // 2)
                add(compact[mid : mid + n])
        if len(queries) >= 6:
            break
    return queries[:6]


def _ordered_title_queries(primary: str | None, extras: list[str] | None) -> list[str]:
    """Search real title fragments before a short decorative slogan."""
    ordered: list[str] = []
    decorative: list[str] = []

    def push(q: str | None) -> None:
        q = re.sub(r"\s+", " ", str(q or "").strip())
        if not q or q in ordered or q in decorative:
            return
        if not is_usable_title(q):
            return
        if _is_decorative_overlay(q):
            decorative.append(q)
        else:
            ordered.append(q)

    for q in extras or []:
        push(q)
    push(primary)
    if ordered:
        return (ordered + decorative)[:8]
    return decorative[:4]


def _title_from_ocr_text(text: str | None) -> str | None:
    """Best usable title line in an OCR dump. Ignores tesseract stubs and site chrome."""
    raw = (text or "").strip()
    if not raw or raw.startswith("[tesseract"):
        return None
    pieces = re.split(r"[\r\n]+", raw)
    pieces.extend(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff々ー]{4,40}", raw))
    best = ""
    for piece in pieces:
        piece = normalize_ocr_title(piece) or str(piece).strip()
        piece = re.sub(r"\s+", " ", piece).strip()
        _sole, cleaned = _split_title_and_code(piece)
        piece = cleaned or ""
        if not piece or _is_site_chrome_title(piece) or not is_usable_title(piece):
            continue
        if len(piece) > len(best):
            best = piece
    return best or None


def _user_frame_preview(image_bytes: bytes | None) -> str | None:
    """Small data-URL of the upload so an unresolved slot still shows that frame."""
    if not image_bytes:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((240, 240))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


def _hit_has_catalog_code(hit: dict | None) -> bool:
    if not isinstance(hit, dict):
        return False
    if _catalog_code_of(hit):
        return True
    for cand in hit.get("candidates") or []:
        if isinstance(cand, dict) and _catalog_code_of(cand):
            return True
    return False


def _unidentified_slot(row: dict | None) -> dict:
    """Gallery card for a frame with no code and no title. Never a silent skip."""
    row = row or {}
    return {
        "ok": True,
        "code": "TITLE-SEARCH",
        "title": "（這張尚未辨識）",
        "actress": None,
        "studio": None,
        "cid": None,
        "cover": None,
        "stills": [],
        "related": [],
        "related_by_title": [],
        "candidates": [],
        "why": "未辨識",
        "line": "multi",
        "stub": False,
        "unidentified": True,
        "needs_code": True,
        "message": "這張圖未讀到番號或片名，已保留。可手動輸入番號。",
        "vision_used": bool(row.get("vision_used")),
        "search_mode": "title",
        "from_image_index": row.get("index"),
        "user_preview": _user_frame_preview(row.get("image_bytes")),
    }


def _unresolved_title_slot(job: dict, *, why: str) -> dict:
    """Title was searched and the catalog still returned no code. Card stays."""
    row = job.get("row") or {}
    title = (job.get("title") or row.get("title") or "").strip()
    payload = title_only_payload(
        title=title or "（片名未解析）",
        actress=row.get("actress"),
        studio=row.get("studio"),
        vision_used=bool(row.get("vision_used")),
        message=why,
    )
    payload["from_image_index"] = row.get("index")
    payload["needs_code"] = True
    payload["unidentified"] = False
    payload["line"] = "multi"
    payload["why"] = "片名未解析番號"
    payload["user_preview"] = _user_frame_preview(row.get("image_bytes"))
    return payload


def _adopt_title_catalog_hit(
    hit: dict,
    *,
    vision_meta: dict | None,
    image_bytes: bytes | None,
    api_key: str | None,
    image_index,
) -> dict | None:
    """Turn a coded title-search hit into a gallery work, same fields as 作品名稱 search."""
    if image_bytes and _hit_has_catalog_code(hit):
        try:
            hit = apply_visual_rank_to_hit(hit, image_bytes, api_key=api_key)
        except Exception:
            pass
    if isinstance(hit, dict) and hit.get("lock_incomplete") and not _hit_visually_locked(hit):
        return _time_budget_slot({"index": image_index, "image_bytes": image_bytes})
    if isinstance(hit, dict) and hit.get("series_unresolved") and not _hit_visually_locked(hit):
        return None
    if not _hit_has_catalog_code(hit):
        return None
    code = _catalog_code_of(hit)
    one = None
    try:
        one = identify_code(code, vision_meta=vision_meta)
    except Exception:
        one = None
    if not isinstance(one, dict) or not one.get("ok") or not _catalog_code_of(one):
        enriched = enrich_title_candidate(hit if _catalog_code_of(hit) else (hit.get("candidates") or [{}])[0], why="片名搜尋")
        one = {
            "ok": True,
            "code": enriched.get("code") or code,
            "title": enriched.get("title") or hit.get("title"),
            "actress": enriched.get("actress") or hit.get("actress"),
            "studio": enriched.get("studio") or hit.get("studio"),
            "cid": enriched.get("cid") or hit.get("cid"),
            "cover": enriched.get("cover") or hit.get("cover"),
            "stills": list(enriched.get("stills") or hit.get("stills") or []),
            "related": [],
            "related_by_title": [],
            "candidates": list(hit.get("candidates") or []),
            "message": "以片名搜尋解析番號",
        }
    else:
        one = dict(one)
        one["candidates"] = list(hit.get("candidates") or one.get("candidates") or [])
        if not one.get("cover") and hit.get("cover"):
            one["cover"] = hit.get("cover")
        if not one.get("cid") and hit.get("cid"):
            one["cid"] = hit.get("cid")
        if not one.get("stills") and hit.get("stills"):
            one["stills"] = list(hit.get("stills") or [])
    if vision_meta:
        one = apply_vision_meta(one, vision_meta)
    # Catalog cast wins. Do not let a wrong OCR name replace it.
    if hit.get("actress") and vision_meta and vision_meta.get("actress"):
        catalog_act = str(hit.get("actress") or "").strip()
        ocr_act = str(vision_meta.get("actress") or "").strip()
        if catalog_act and ocr_act and catalog_act != ocr_act:
            one["actress"] = catalog_act
    one["ok"] = True
    one["search_mode"] = "title"
    one["stub"] = False
    one["unidentified"] = False
    one["needs_code"] = False
    one["from_image_index"] = image_index
    one["why"] = one.get("why") or "片名搜尋"
    return one


def _candidates_for_read_codes(codes: list[str]) -> list[dict]:
    """Catalog rows for 品番 that were printed on the frame. No invented titles."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in codes[:8]:
        disp = format_display_code(str(raw))
        if not parse_code_parts(disp) or disp in seen:
            continue
        seen.add(disp)
        try:
            rows = fetch_avbase_title_results(disp, actress=None) or []
        except Exception:
            rows = []
        hit = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and codes_numeric_equal(str(row.get("code") or ""), disp)
            ),
            None,
        )
        if isinstance(hit, dict) and _catalog_code_of(hit):
            out.append(hit)
    return out


def _candidates_for_actress(actress: str | None, *, prefer: list[str] | None = None) -> list[dict]:
    """Works billed to this actress. A romaji caption is not a catalog query."""
    name = _actress_query_name(actress)
    if not name:
        return []
    try:
        rows = fetch_avbase_title_results(name, actress=name) or []
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _catalog_code_of(row)
        if not code or code in seen:
            continue
        if not _actress_name_matches(name, str(row.get("actress") or "")):
            continue
        seen.add(code)
        out.append(row)
    pref = {
        format_display_code(str(c))
        for c in (prefer or [])
        if parse_code_parts(str(c))
    }
    out.sort(key=lambda c: 0 if _catalog_code_of(c) in pref else 1)
    return out


def _visual_lock_winner(
    image_bytes: bytes | None,
    candidates: list[dict],
    api_key: str | None,
) -> dict | None:
    """Same lock bar as other identify modes: same work and the same clothes."""
    coded = [c for c in candidates or [] if isinstance(c, dict) and _catalog_code_of(c)]
    if not image_bytes or not coded or not (api_key or "").strip():
        return None
    if getattr(_BATCH, "active", False):
        remain = _seconds_left(_batch_deadline())
        if getattr(_BATCH, "jacket_incomplete", False) or remain < float(VISUAL_COMPARE_BUDGET):
            return None
    try:
        ranked, meta = rank_candidates_by_visual(image_bytes, coded[:8], api_key=api_key)
    except Exception:
        return None
    if not (meta or {}).get("visual_lock") or not ranked:
        return None
    winner = ranked[0]
    if not isinstance(winner, dict) or not _visual_is_lock(winner.get("visual")):
        return None
    if not _catalog_code_of(winner):
        return None
    return winner


def _slot_from_locked_candidate(
    winner: dict,
    *,
    vision_meta: dict | None,
    image_index,
    why: str,
    search_mode: str,
) -> dict:
    code = _catalog_code_of(winner)
    one = {
        "ok": True,
        "code": code,
        "title": winner.get("title") or "",
        "actress": winner.get("actress"),
        "studio": winner.get("studio"),
        "cid": winner.get("cid"),
        "cover": winner.get("cover"),
        "stills": list(winner.get("stills") or []),
        "related": [],
        "related_by_title": [],
        "candidates": [dict(winner)],
        "visual_lock": True,
        "visual_mismatch": False,
        "needs_code": False,
        "unidentified": False,
        "stub": False,
        "search_mode": search_mode,
        "why": why,
        "from_image_index": image_index,
        "message": why,
        "source": winner.get("source") or search_mode,
        "visual_meta": {"visual_ranked": True, "visual_lock": True, "note": why},
    }
    if vision_meta:
        one = apply_vision_meta(one, vision_meta)
    one["code"] = code
    one["visual_lock"] = True
    one["needs_code"] = False
    return one


def _recover_locked_work(
    *,
    image_bytes: bytes | None,
    api_key: str | None,
    actress: str | None,
    codes: list[str] | None,
    vision_meta: dict | None,
    image_index,
) -> dict | None:
    """When the printed phrase is not the catalog title, lock a real work.

    A listing crop may show several 品番. One thumb with only a slogan and an
    actress name uses that filmography. Either way the image has to lock
    (same work and same clothes) before a code replaces the title-only card.
    """
    key = (api_key or "").strip()
    if not image_bytes or not key:
        return None
    code_list = [format_display_code(str(c)) for c in (codes or []) if parse_code_parts(str(c))]
    if len(code_list) >= 2:
        winner = _visual_lock_winner(image_bytes, _candidates_for_read_codes(code_list), key)
        if winner:
            return _slot_from_locked_candidate(
                winner,
                vision_meta=vision_meta,
                image_index=image_index,
                why="列表上的番號已對上原圖",
                search_mode="code",
            )
    winner = _visual_lock_winner(
        image_bytes,
        _candidates_for_actress(actress, prefer=code_list),
        key,
    )
    if not winner:
        return None
    return _slot_from_locked_candidate(
        winner,
        vision_meta=vision_meta,
        image_index=image_index,
        why="片名對不上目錄，已依女優作品與原圖鎖定",
        search_mode="actress",
    )


def _escalate_frame_title(
    title: str,
    *,
    actress: str | None,
    image_bytes: bytes | None,
    api_key: str | None,
    vision_meta: dict | None,
    image_index,
) -> dict | None:
    """Search a frame's title the way 作品名稱 search does, then keep the code.

    The OCR actress is not a filter. Glued cast names are stripped, and a
    failure of prefix-variants does not skip the raw phrase.
    """
    raw = (title or "").strip()
    if not is_usable_title(raw) and not is_usable_title(_strip_glued_actress(raw, actress)):
        return None
    queries = _catalog_title_queries(raw, actress)
    if raw and raw not in queries:
        queries = [raw] + queries
    hit = None
    seen_q: set[str] = set()
    for q in queries:
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        try:
            # Actress stays out of the retrieval call. Stripping already happened.
            found = search_by_title(q)
        except Exception:
            found = None
        if _hit_has_catalog_code(found):
            hit = found
            break
    if not isinstance(hit, dict):
        return None
    return _adopt_title_catalog_hit(
        hit,
        vision_meta=vision_meta,
        image_bytes=image_bytes,
        api_key=api_key,
        image_index=image_index,
    )


def _lock_unknown_onto_sibling(
    slot: dict,
    image_bytes: bytes | None,
    siblings: list[dict],
    api_key: str | None,
) -> dict:
    """If a textless frame visually locks to another upload's code, adopt that work.

    No lock → the slot stays 未辨識. A lock is not a silent drop; the caller
    may merge only when both frames locked the same code.
    """
    if not image_bytes or not slot.get("unidentified") or not (api_key or "").strip():
        return slot
    if getattr(_BATCH, "active", False) and _seconds_left(_batch_deadline()) < float(VISUAL_COMPARE_BUDGET):
        return slot
    pool: list[dict] = []
    seen: set[str] = set()
    for sib in siblings:
        cand = _payload_as_visual_candidate(sib)
        if not cand:
            continue
        code = _catalog_code_of(cand)
        if not code or code in seen:
            continue
        seen.add(code)
        pool.append(cand)
    if not pool:
        return slot
    try:
        ranked, meta = rank_candidates_by_visual(image_bytes, pool, api_key=api_key)
    except Exception:
        return slot
    if not (meta or {}).get("visual_lock") or not ranked:
        return slot
    winner = ranked[0]
    if not _catalog_code_of(winner):
        return slot
    adopted = dict(slot)
    for field in ("code", "title", "title_zh", "actress", "studio", "cover", "cid"):
        if winner.get(field):
            adopted[field] = winner.get(field)
    if winner.get("stills"):
        adopted["stills"] = list(winner.get("stills") or [])
    adopted["visual_lock"] = True
    adopted["visual_mismatch"] = False
    adopted["visual_note"] = ""
    adopted["unidentified"] = False
    adopted["needs_code"] = False
    adopted["stub"] = False
    adopted["ok"] = True
    adopted["search_mode"] = "code"
    adopted["why"] = "與其他上傳圖為同一作品"
    _recompute_theme_keywords(adopted)
    return adopted


def _frame_title_supports_code(catalog_title: str | None, frame_title: str | None) -> bool:
    """False when the words on this frame belong to a different work than the code.

    A short fragment of the catalog line still supports it. A different phrase
    (school-swimsuit 媚薬合宿 vs a JUFE title) does not. A short cover slogan
    such as 舌技が神 is not a catalog title, so it does not veto a 品番 that
    was actually read off the frame.
    """
    frame = str(frame_title or "").strip()
    if not is_usable_title(frame) or _is_decorative_overlay(frame):
        return True
    catalog = str(catalog_title or "").strip()
    if not is_usable_title(catalog):
        return False
    if _titles_are_same_phrase(catalog, frame):
        return True
    return title_similarity(frame, catalog) >= TITLE_CODE_MATCH_MIN


def _frame_titles_conflict(a: dict, b: dict) -> bool:
    """True when both uploads printed different works, so they must not merge."""
    left = str(a.get("_frame_title") or "").strip()
    right = str(b.get("_frame_title") or "").strip()
    if not is_usable_title(left) or not is_usable_title(right):
        return False
    if _titles_are_same_phrase(left, right):
        return False
    return title_similarity(left, right) < TITLE_CODE_MATCH_MIN


def _merge_locked_same_work(results: list[dict]) -> tuple[list[dict], int]:
    """Collapse two slots only when both visually locked the same code.

    A shared actress, a fuzzy title, or a frame that never locked stays.
    Two frames whose own titles name different works stay even if a misread
    code and a visual lock would otherwise fold them together.
    """
    kept: list[dict] = []
    merged = 0
    for row in results:
        if not isinstance(row, dict):
            continue
        code = _catalog_code_of(row)
        if not code or not row.get("visual_lock"):
            kept.append(row)
            continue
        host = next(
            (
                prev
                for prev in kept
                if prev.get("visual_lock")
                and _catalog_code_of(prev) == code
                and not _frame_titles_conflict(prev, row)
            ),
            None,
        )
        if host is None:
            kept.append(row)
            continue
        merged += 1
        idxs: list = []
        for src in (host, row):
            if src.get("from_image_index") is not None:
                idxs.append(src.get("from_image_index"))
            for n in src.get("merged_image_indexes") or []:
                idxs.append(n)
        host["merged_image_indexes"] = sorted({i for i in idxs if i is not None})
    return kept, merged


def _pin_manual_query(rows: list[dict], user_code: str, user_title: str) -> bool:
    """Attach a typed 番號 or 片名 to the open frame. Do not add another slot.

    Prefer the frame whose only title is a short slogan (舌技が神), then a
    frame with no 品番. A frame that already has a different real code is
    left alone.
    """
    disp = format_display_code(user_code) if user_code and parse_code_parts(user_code) else ""
    title_q = (user_title or "").strip()
    if disp:
        if any(codes_numeric_equal(str(r.get("code") or ""), disp) for r in rows):
            return True

        def _open_rank(row: dict) -> int:
            if _is_decorative_overlay(row.get("title")):
                return 0
            existing = str(row.get("code") or "")
            if not (existing and parse_code_parts(existing)):
                return 1
            return 9

        opens = [r for r in rows if _open_rank(r) < 9]
        opens.sort(key=_open_rank)
        if not opens:
            return False
        target = opens[0]
        target["code"] = disp
        if title_q and is_usable_title(title_q) and (
            not is_usable_title(target.get("title")) or _is_decorative_overlay(target.get("title"))
        ):
            target["title"] = title_q
        return True
    if title_q and is_usable_title(title_q):
        if any(str(r.get("title") or "").strip().casefold() == title_q.casefold() for r in rows):
            return True
        for row in rows:
            existing = str(row.get("code") or "")
            if existing and parse_code_parts(existing):
                continue
            if not is_usable_title(row.get("title")) or _is_decorative_overlay(row.get("title")):
                row["title"] = title_q
                return True
    return False


def _client_slot(slot: dict) -> dict:
    """Progress payload for one slot. No image bytes."""
    keep = (
        "ok",
        "code",
        "title",
        "actress",
        "studio",
        "cid",
        "cover",
        "why",
        "line",
        "stub",
        "unidentified",
        "needs_code",
        "from_image_index",
        "message",
        "timed_out",
        "visual_lock",
        "visual_note",
        "user_preview",
    )
    out = {k: slot.get(k) for k in keep if k in slot and slot.get(k) is not None}
    stills = slot.get("stills")
    if isinstance(stills, list):
        out["stills"] = [str(u) for u in stills[:8] if u]
    return out


def _time_budget_slot(row: dict | None) -> dict:
    """One card for a frame the batch did not finish. Not a 查詢不到."""
    row = row or {}
    return {
        "ok": True,
        "code": "TITLE-SEARCH",
        "title": "（這張尚未查完）",
        "actress": None,
        "studio": None,
        "cid": None,
        "cover": None,
        "stills": [],
        "related": [],
        "related_by_title": [],
        "candidates": [],
        "why": "查詢逾時",
        "line": "multi",
        "stub": False,
        "unidentified": False,
        "timed_out": True,
        "needs_code": True,
        "message": "這張時間不夠，還沒鎖定。請再上傳這張重查一次。不是查詢不到。",
        "vision_used": bool(row.get("vision_used")),
        "search_mode": "title",
        "from_image_index": row.get("index"),
        "user_preview": _user_frame_preview(row.get("image_bytes")),
    }


def _job_from_vision_row(row: dict) -> dict:
    if row.get("budget_skipped"):
        return {"kind": "timeout", "code": "", "title": "", "row": row}
    code = (row.get("code") or "").strip()
    title = (row.get("title") or "").strip()
    if code and parse_code_parts(code):
        return {"kind": "code", "code": format_display_code(code), "title": title, "row": row}
    if is_usable_title(title):
        return {"kind": "title", "code": "", "title": title, "row": row}
    return {"kind": "unknown", "code": "", "title": "", "row": row}


def run_multi_identify_pipeline(
    images: list[tuple[bytes, str | None]],
    *,
    user_code: str = "",
    user_title: str = "",
    on_progress=None,
    deadline: float | None = None,
) -> tuple[dict, int]:
    """Vision each image → one gallery slot per upload.

    Same-work merge happens only after both frames visually lock the same code.
    A frame that failed OCR or only has a title is never dropped.
    A large batch stops at MULTI_IDENTIFY_BUDGET_S and returns the slots it
    finished, plus an honest 尚未查完 card for the rest.
    """
    if not images:
        return run_identify_pipeline(
            image_bytes=None,
            filename=None,
            user_code=user_code,
            user_title=user_title,
            on_progress=on_progress,
        )
    if len(images) == 1:
        result, status = run_identify_pipeline(
            image_bytes=images[0][0],
            filename=images[0][1],
            user_code=user_code,
            user_title=user_title,
            on_progress=on_progress,
        )
        return attach_related_by_title(result, budget_sec=14.0), status

    n = len(images)
    if deadline is None:
        deadline = time.monotonic() + float(MULTI_IDENTIFY_BUDGET_S)
    else:
        deadline = float(deadline)
    _enter_batch_ctx(deadline)
    try:
        api_key = get_gemini_api_key()
        _progress(on_progress, "receive", "active", f"正在接收 {n} 張圖片…", 0.02)
        _progress(on_progress, "receive", "done", f"已接收 {n} 張圖片", 1 / 7)

        vision_rows: list[dict] = []
        for i, (img_bytes, fname) in enumerate(images):
            idx = i + 1
            if _seconds_left(deadline) < MULTI_VISION_START_S:
                vision_rows.append(
                    {
                        "index": idx,
                        "filename": fname,
                        "code": None,
                        "title": None,
                        "actress": None,
                        "studio": None,
                        "vision_used": False,
                        "image_bytes": img_bytes,
                        "budget_skipped": True,
                    }
                )
                _progress(
                    on_progress,
                    "vision",
                    "active",
                    f"第 {idx}/{n} 張：時間不夠，請再上傳這張重查",
                    0.05 + 0.35 * (idx / max(n, 1)),
                )
                continue
            _progress(
                on_progress,
                "vision",
                "active",
                f"辨識第 {idx}/{n} 張…",
                0.05 + 0.35 * (i / max(n, 1)),
            )
            row: dict = {
                "index": idx,
                "filename": fname,
                "code": None,
                "title": None,
                "actress": None,
                "studio": None,
                "vision_used": False,
                "image_bytes": img_bytes,
            }
            ocr_text = ""
            mime = detect_image_mime(img_bytes, fname)
            if api_key:
                try:
                    vm = call_gemini_vision(img_bytes, mime, api_key)
                    row["vision_used"] = True
                    if vm.get("code"):
                        row["code"] = str(vm.get("code"))
                    if vm.get("title"):
                        row["title"] = str(vm.get("title"))
                    if vm.get("actress"):
                        row["actress"] = str(vm.get("actress"))
                    if vm.get("studio"):
                        row["studio"] = str(vm.get("studio"))
                    if isinstance(vm.get("texts"), list):
                        row["vision_texts"] = [str(t).strip() for t in vm["texts"] if str(t or "").strip()]
                    if vm.get("shot"):
                        row["shot"] = str(vm.get("shot"))
                except Exception as e:
                    row["vision_error"] = str(e)[:120]
                    try:
                        ocr_text = ocr_image_bytes(img_bytes)
                        sole, many = _sole_trusted_ocr_code(ocr_text)
                        if sole:
                            row["code"] = sole
                        elif many:
                            row["ocr_codes"] = many
                    except Exception:
                        pass
            else:
                try:
                    ocr_text = ocr_image_bytes(img_bytes)
                    sole, many = _sole_trusted_ocr_code(ocr_text)
                    if sole:
                        row["code"] = sole
                    elif many:
                        row["ocr_codes"] = many
                except Exception:
                    pass
            if row.get("vision_used"):
                row["vision_code"] = str(row["code"]) if row.get("code") else None
                row["vision_title"] = str(row["title"]) if row.get("title") else None
            # A code glued into the title line, or a badge mistaken for a title.
            embedded, cleaned_title = _split_title_and_code(row.get("title"))
            _title_codes_sole, title_codes = _sole_product_code(str(row.get("title") or ""))
            if cleaned_title != row.get("title"):
                row["title"] = cleaned_title
            if _is_site_chrome_title(row.get("title")):
                row["title"] = None
            if embedded and not (row.get("code") and parse_code_parts(str(row.get("code") or ""))):
                row["code"] = embedded
            elif len(title_codes) >= 2 and not (row.get("code") and parse_code_parts(str(row.get("code") or ""))):
                row["ocr_codes"] = title_codes
            # Vision can return a short cover slogan and still miss the 品番 under the thumb.
            # OCR runs whenever the code is missing, even if that slogan is a "usable" title.
            # The frame is still kept if both miss.
            if not (row.get("code") and parse_code_parts(str(row.get("code") or ""))):
                # The no-key path already OCR'd this frame. A second pass must not
                # replace that read: tesseract varies, and a later pass can drop a
                # short badge or title line the first pass actually saw.
                if not ocr_text:
                    try:
                        ocr_text = ocr_image_bytes(img_bytes)
                    except Exception:
                        ocr_text = ""
                sole, many = _sole_trusted_ocr_code(ocr_text or "")
                if sole:
                    row["code"] = sole
                    row["ocr_code"] = sole
                elif many:
                    row["ocr_codes"] = many
                if not is_usable_title(row.get("title")) or _is_site_chrome_title(row.get("title")):
                    ocr_title = _title_from_ocr_text(ocr_text)
                    if ocr_title:
                        row["ocr_title"] = ocr_title
                        row["title"] = ocr_title
            elif ocr_text and not is_usable_title(row.get("title")):
                # A weak OCR 品番 (yr 33 → YR-33) must not hide the caption line.
                ocr_title = _title_from_ocr_text(ocr_text)
                if ocr_title:
                    row["ocr_title"] = ocr_title
                    row["title"] = ocr_title
            read_blob = _compose_read_blob(
                row.get("vision_texts"),
                row.get("vision_title"),
                row.get("title"),
                row.get("actress"),
                ocr_text,
            )
            if not (row.get("code") and parse_code_parts(str(row.get("code") or ""))):
                sole_blob, many_blob = _sole_trusted_ocr_code(read_blob)
                if sole_blob:
                    row["code"] = sole_blob
                    row["ocr_code"] = sole_blob
                elif many_blob and not row.get("ocr_codes"):
                    row["ocr_codes"] = many_blob
            # Full-text search only for one complete cover and no listing row under
            # the thumb. A grid, multi-row screenshot, or player overlay keeps the
            # focused 品番 and primary title. Do not drop this gate.
            if _cover_full_text_allowed(row, read_blob):
                row["text_queries"] = _full_text_search_queries(read_blob, actress=row.get("actress"))
            else:
                row["text_queries"] = []
            if (
                row["text_queries"]
                and not (row.get("code") and parse_code_parts(str(row.get("code") or "")))
                and (not is_usable_title(row.get("title")) or _is_decorative_overlay(row.get("title")))
            ):
                row["ocr_title"] = row.get("ocr_title") or row["text_queries"][0]
                row["title"] = row["text_queries"][0]
            if _listing_chrome_in_text(read_blob):
                repairs = _ocr_title_repairs(str(row.get("title") or ""))
            else:
                repairs = _ocr_title_repairs(read_blob)
            if repairs:
                current = re.sub(r"\s+", "", str(row.get("title") or ""))
                merged_q = list(dict.fromkeys(list(repairs) + list(row.get("text_queries") or [])))
                if current and current not in merged_q and _ocr_line_is_clean(current):
                    merged_q.append(current)
                row["text_queries"] = merged_q[:6]
                # A clean caption that already contains the repair stays (JUFE full line).
                # A noisy line or a different fragment (特別な補習) yields to the repair.
                if not _ocr_line_is_clean(current) or repairs[0] not in current:
                    row["ocr_title"] = repairs[0]
                    row["title"] = repairs[0]
            # Prefixes are for a noisy read. A clean vision/OCR title is already
            # the catalog query; slicing it searches a different work's words.
            if not _listing_chrome_in_text(read_blob) and not _title_is_search_ready(
                str(row.get("title") or "")
            ):
                prefixes = _ocr_line_prefixes(read_blob)
                merged_pref = list(row.get("text_queries") or [])
                for pref in prefixes:
                    if pref not in merged_pref:
                        merged_pref.append(pref)
                row["text_queries"] = merged_pref[:8]
            # This cover may already have succeeded on its own. A multi pass that
            # reads nothing must reuse that image's cached 番號 and 作品名稱,
            # instead of leaving the frame to be merged into another upload.
            if not (row.get("code") and parse_code_parts(str(row.get("code") or ""))) and not is_usable_title(
                row.get("title")
            ):
                cached_img = None
                try:
                    cached_img = offline_cache_get(image_hash=image_content_hash(img_bytes))
                except Exception:
                    cached_img = None
                if (
                    isinstance(cached_img, dict)
                    and _catalog_code_of(cached_img)
                    and _upload_matches_cached_cover(img_bytes, cached_img)
                ):
                    row["code"] = _catalog_code_of(cached_img)
                    if is_usable_title(cached_img.get("title")):
                        row["title"] = str(cached_img.get("title")).strip()
                    row["image_cache"] = cached_img
            vision_rows.append(row)
            detail = f"第 {idx}/{n} 張"
            if row.get("code"):
                detail += f"：{format_display_code(str(row['code']))}"
            elif row.get("title"):
                detail += "：已讀到片名"
            else:
                detail += "：未讀到，仍保留"
            _progress(
                on_progress,
                "vision",
                "done" if idx == n else "active",
                detail,
                0.05 + 0.35 * (idx / n),
            )

        _progress(on_progress, "vision", "done", f"已看完 {n} 張", 0.42)
        _progress(on_progress, "parse", "active", "彙整番號／片名…", 0.45)

        # One upload → one job. Duplicate strings are not dropped here.
        jobs: list[dict] = [_job_from_vision_row(row) for row in vision_rows]

        # A typed code/title pins onto the open frame (slogan or no 品番).
        # It must not become a sixth card or stamp the first image.
        pinned_manual = _pin_manual_query(vision_rows, user_code, user_title)
        if pinned_manual:
            jobs = [_job_from_vision_row(row) for row in vision_rows]
        elif user_code and parse_code_parts(user_code):
            disp = format_display_code(user_code)
            if not any(j.get("kind") == "code" and codes_numeric_equal(str(j.get("code") or ""), disp) for j in jobs):
                jobs.append({"kind": "code", "code": disp, "title": user_title, "row": None})
        if not pinned_manual and user_title and is_usable_title(user_title):
            key = user_title.casefold()
            if not any((j.get("title") or "").casefold() == key for j in jobs):
                jobs.append({"kind": "title", "code": "", "title": user_title, "row": None})

        _progress(
            on_progress,
            "parse",
            "done",
            f"待查 {len(jobs)} 張",
            0.5,
        )

        _progress(on_progress, "verify", "active", "逐張對照原圖的人物／衣服／姿勢…", 0.52)
        _progress(on_progress, "verify", "done", "開始逐張搜尋", 0.54)
        results: list[dict] = []

        def _note_slot(slot: dict) -> None:
            if not on_progress or not isinstance(slot, dict):
                return
            try:
                on_progress(
                    {
                        "step": "search",
                        "status": "done",
                        "detail": str(slot.get("message") or slot.get("why") or "一張完成"),
                        "progress": min(0.9, 0.55 + 0.25 * (len(results) / max(len(jobs), 1))),
                        "slot": _client_slot(slot),
                    }
                )
            except Exception:
                pass

        for ji, job in enumerate(jobs):
            if getattr(_BATCH, "active", False):
                _BATCH.jacket_incomplete = False
            if job.get("kind") == "timeout" or _seconds_left(deadline) < MULTI_SLOT_RESERVE_S:
                for rest in jobs[ji:]:
                    rest_row = rest.get("row") if isinstance(rest.get("row"), dict) else None
                    one = _time_budget_slot(rest_row)
                    one["line"] = "main" if not results else "multi"
                    one["ok"] = True
                    results.append(one)
                    _note_slot(one)
                _progress(
                    on_progress,
                    "search",
                    "active",
                    f"時間上限，其餘 {len(jobs) - ji} 張請再上傳重查",
                    0.8,
                )
                break
            _progress(
                on_progress,
                "search",
                "active",
                f"搜尋第 {ji + 1}/{len(jobs)} 張…",
                0.55 + 0.25 * (ji / max(len(jobs), 1)),
            )
            row = job.get("row") if isinstance(job.get("row"), dict) else None
            vm = None
            if row:
                vm = {
                    "title": row.get("title"),
                    "actress": row.get("actress"),
                    "studio": row.get("studio"),
                    "code": row.get("code"),
                }
            slot_image = row.get("image_bytes") if row else None
            image_index = row.get("index") if row else None
            one: dict | None = None
            try:
                cached_frame = row.get("image_cache") if row else None
                if (
                    job["kind"] == "code"
                    and isinstance(cached_frame, dict)
                    and _catalog_code_of(cached_frame) == job.get("code")
                ):
                    one = dict(cached_frame)
                    one["ok"] = True
                    one["from_offline_cache"] = True
                    one.setdefault("search_mode", "code")
                elif job["kind"] == "code":
                    one, _st = run_identify_pipeline(
                        image_bytes=None,
                        filename=None,
                        user_code=job["code"],
                        user_title="",
                        on_progress=None,
                        skip_related=True,
                    )
                elif job["kind"] == "title":
                    # No 品番: search the title cues, then let the jacket pick the volume.
                    # A shared series template is not accepted until the picture locks.
                    queries = list((row or {}).get("text_queries") or [])
                    if job.get("title") and job["title"] not in queries:
                        queries.insert(0, job["title"])
                    resolved = None
                    noisy_title = not _title_is_search_ready(str(job.get("title") or ""))
                    if slot_image and noisy_title and _seconds_left(deadline) > 8.0:
                        try:
                            resolved = _resolve_unnumbered_cover(queries, slot_image)
                        except Exception:
                            resolved = None
                    if isinstance(resolved, dict) and _catalog_code_of(resolved):
                        one, _st = run_identify_pipeline(
                            image_bytes=None,
                            filename=None,
                            user_code=_catalog_code_of(resolved),
                            user_title="",
                            on_progress=None,
                            skip_related=True,
                        )
                        if isinstance(one, dict):
                            one["visual_meta"] = resolved.get("visual_meta")
                            if (resolved.get("visual_meta") or {}).get("visual_lock"):
                                one["visual_lock"] = True
                            one["resolve_queries"] = queries[:8]
                    else:
                        one, _st = run_identify_pipeline(
                            image_bytes=None,
                            filename=None,
                            user_code="",
                            user_title=job["title"],
                            on_progress=None,
                            skip_related=True,
                        )
                    if (
                        not (isinstance(resolved, dict) and _catalog_code_of(resolved))
                        and slot_image
                        and isinstance(one, dict)
                        and (one.get("candidates") or one.get("code"))
                    ):
                        packed = {
                            "code": one.get("code"),
                            "title": one.get("title"),
                            "actress": one.get("actress"),
                            "studio": one.get("studio"),
                            "cover": one.get("cover"),
                            "cid": one.get("cid"),
                            "source": one.get("source"),
                            "candidates": list(one.get("candidates") or []),
                        }
                        if not packed["candidates"] and packed.get("code"):
                            packed["candidates"] = [dict(packed)]
                        try:
                            packed = apply_visual_rank_to_hit(packed, slot_image, api_key=api_key)
                        except Exception:
                            packed = packed
                        if _hit_visually_locked(packed):
                            locked_code = _catalog_code_of(packed)
                            if locked_code and locked_code != _catalog_code_of(one):
                                try:
                                    refreshed, _rst = run_identify_pipeline(
                                        image_bytes=None,
                                        filename=None,
                                        user_code=locked_code,
                                        user_title="",
                                        on_progress=None,
                                        skip_related=True,
                                    )
                                except Exception:
                                    refreshed = None
                                if isinstance(refreshed, dict) and refreshed.get("ok") and _catalog_code_of(refreshed):
                                    one = refreshed
                            else:
                                one["code"] = packed.get("code") or one.get("code")
                                if packed.get("title"):
                                    one["title"] = packed.get("title")
                                one["visual_lock"] = True
                        elif packed.get("lock_incomplete"):
                            one = _time_budget_slot(row)
                        elif packed.get("series_unresolved"):
                            one = {}
                    if vm and isinstance(one, dict) and one.get("ok") and not one.get("timed_out"):
                        one = apply_vision_meta(one, vm)
            except Exception as e:
                one = empty_identify(message=f"查詢失敗：{e}")

            if not isinstance(one, dict):
                one = {}
            frame_title = (job.get("title") or (vm or {}).get("title") or "").strip()
            # A code stamped on this frame is not enough when the printed title
            # belongs to another work. The school-swimsuit cover must not collapse
            # into a JUFE code that a different upload actually is.
            title_rejects_code = bool(
                job["kind"] == "code"
                and _catalog_code_of(one)
                and is_usable_title(frame_title)
                and not _frame_title_supports_code(one.get("title"), frame_title)
            )
            if one.get("timed_out"):
                # Retry card. Do not escalate into a guessed volume.
                pass
            elif job["kind"] == "unknown":
                one = _unidentified_slot(row)
            elif not _catalog_code_of(one) or title_rejects_code:
                escalated = None
                title_q = frame_title
                if is_usable_title(title_q) or is_usable_title(
                    _strip_glued_actress(title_q, (vm or {}).get("actress"))
                ):
                    try:
                        escalated = _escalate_frame_title(
                            title_q,
                            actress=(vm or {}).get("actress") if vm else None,
                            image_bytes=slot_image,
                            api_key=api_key,
                            vision_meta=vm,
                            image_index=image_index,
                        )
                    except Exception:
                        escalated = None
                if escalated and escalated.get("timed_out"):
                    one = escalated
                elif escalated and _catalog_code_of(escalated):
                    one = escalated
                elif title_rejects_code:
                    pass
                else:
                    if not _catalog_code_of(one):
                        for extra_q in (row or {}).get("text_queries") or []:
                            if not extra_q or extra_q == frame_title:
                                continue
                            try:
                                escalated = _escalate_frame_title(
                                    extra_q,
                                    actress=(vm or {}).get("actress") if vm else None,
                                    image_bytes=slot_image,
                                    api_key=api_key,
                                    vision_meta=vm,
                                    image_index=image_index,
                                )
                            except Exception:
                                escalated = None
                            if escalated and escalated.get("timed_out"):
                                one = escalated
                                break
                            if escalated and _catalog_code_of(escalated):
                                one = escalated
                                break
                    recovered = None
                    if not one.get("timed_out") and not _catalog_code_of(one) and slot_image:
                        try:
                            recovered = _recover_locked_work(
                                image_bytes=slot_image,
                                api_key=api_key,
                                actress=(vm or {}).get("actress") if vm else None,
                                codes=(row or {}).get("ocr_codes") if row else None,
                                vision_meta=vm,
                                image_index=image_index,
                            )
                        except Exception:
                            recovered = None
                    if recovered and recovered.get("timed_out"):
                        one = recovered
                    elif recovered and _catalog_code_of(recovered):
                        one = recovered
                    elif one.get("timed_out"):
                        pass
                    elif job["kind"] == "title":
                        one = _unresolved_title_slot(
                            job,
                            why="已用片名搜尋，目錄沒有返回番號。可手動輸入番號。",
                        )
                    elif job["kind"] == "code":
                        one = build_multi_fail_stub(job, why="番號已查，目錄沒有完整資料")
                        one["from_image_index"] = image_index
                        one["needs_code"] = not bool(_catalog_code_of(one))
                    else:
                        one = _unidentified_slot(row)

            if one.get("ok") and _catalog_code_of(one) and not one.get("timed_out"):
                try:
                    one = verify_work_against_image(
                        one,
                        slot_image,
                        api_key=api_key,
                        vision_title=(vm or {}).get("title") if isinstance(vm, dict) else None,
                    )
                except Exception:
                    pass
                if one.get("lock_incomplete") and not _hit_visually_locked(one):
                    one = _time_budget_slot(row)
                elif (
                    getattr(_BATCH, "jacket_incomplete", False)
                    and not _hit_visually_locked(one)
                ):
                    one = _time_budget_slot(row)
                else:
                    disp = _catalog_code_of(one)
                    if disp:
                        one["code"] = disp
            one.setdefault("related_by_title", [])
            one["from_image_index"] = image_index if image_index is not None else one.get("from_image_index")
            one["_frame_title"] = frame_title
            one["why"] = one.get("why") or "多圖辨識"
            one["line"] = "main" if not results else "multi"
            one["ok"] = True
            results.append(one)
            _note_slot(one)

        # Textless frames may still be a still of a work another frame already found.
        relocked: list[dict] = []
        for slot in results:
            if slot.get("unidentified"):
                idx = slot.get("from_image_index")
                src = next((r for r in vision_rows if r.get("index") == idx), None)
                img = (src or {}).get("image_bytes")
                others = [s for s in results if s is not slot and _catalog_code_of(s)]
                slot = _lock_unknown_onto_sibling(slot, img, others, api_key)
            relocked.append(slot)
        results = relocked
        results, n_merged = _merge_locked_same_work(results)
        for slot in results:
            if isinstance(slot, dict):
                slot.pop("_frame_title", None)

        if not results:
            _progress(on_progress, "search", "error", "查詢後無有效作品", 0.8)
            _progress(on_progress, "done", "error", "失敗", 1.0)
            return (
                empty_identify(
                    message=f"已看 {n} 張圖，查詢後無有效作品",
                    vision_used=any(r.get("vision_used") for r in vision_rows),
                ),
                200,
            )

        n_unknown = sum(1 for r in results if r.get("unidentified"))
        n_title_open = sum(
            1
            for r in results
            if r.get("needs_code")
            and not r.get("unidentified")
            and not r.get("timed_out")
            and not _catalog_code_of(r)
        )
        n_budget = sum(1 for r in results if r.get("timed_out"))
        n_code_stub = sum(1 for r in results if r.get("stub") and _catalog_code_of(r))
        ok_count = sum(1 for r in results if _catalog_code_of(r) and not r.get("stub"))

        msg = f"多圖辨識：{n} 張 → {len(results)} 部"
        note = f"多圖辨識共 {len(results)} 部"
        if n_title_open:
            bit = f"其中 {n_title_open} 部已用片名搜尋，目錄沒有返回番號"
            msg += f"（{bit}）"
            note += f"；{bit}"
        if n_code_stub:
            bit = f"其中 {n_code_stub} 部番號已查，目錄沒有完整資料（卡片已保留）"
            note += f"；{bit}"
        if n_unknown:
            bit = f"其中 {n_unknown} 部未辨識（已保留該張）"
            note += f"；{bit}"
        if n_budget:
            bit = f"其中 {n_budget} 張時間不夠未鎖定（請再上傳那幾張重查，不是查詢不到）"
            msg += f"（{bit}）"
            note += f"；{bit}"
        if n_merged:
            bit = f"{n_merged} 張與其他張為同一作品（番號與原圖都對上）已合併"
            msg += f"（{bit}）"
            note += f"；{bit}"

        _progress(on_progress, "search", "done", f"列出 {len(results)} 部（成功 {ok_count}）", 0.82)
        main = results[0]
        # Other multi hits as related gallery cards (related_by_title filled once below)
        extras = []
        for r in results[1:]:
            extras.append(
                {
                    "code": r.get("code"),
                    "title": r.get("title"),
                    "actress": r.get("actress"),
                    "studio": r.get("studio"),
                    "cid": r.get("cid"),
                    "cover": r.get("cover"),
                    "stills": r.get("stills") or [],
                    "why": r.get("why") or "多圖辨識",
                    "line": "multi",
                    "related_by_title": [],
                    "stub": bool(r.get("stub")),
                    "unidentified": bool(r.get("unidentified")),
                    "needs_code": bool(r.get("needs_code")),
                    "from_image_index": r.get("from_image_index"),
                }
            )
        prev_related = list(main.get("related") or [])
        payload = {
            "ok": True,
            "multi": True,
            "image_count": n,
            "result_count": len(results),
            "code": main.get("code"),
            "title": main.get("title"),
            "actress": main.get("actress"),
            "studio": main.get("studio"),
            "cid": main.get("cid"),
            "cover": main.get("cover"),
            "stills": main.get("stills") or [],
            "related": extras + prev_related,
            "related_by_title": [],
            "results": results,
            "candidates": main.get("candidates") or [],
            "vision_used": any(r.get("vision_used") for r in vision_rows),
            "search_mode": "code",
            "message": msg,
            "related_note": note,
            "partial": bool(n_budget),
            "dropped": [],
            "ocr_text_preview": None,
        }
        _progress(
            on_progress,
            "cover",
            "done" if payload.get("cover") else "skipped",
            "封面就緒" if payload.get("cover") else "部分無封面",
            0.92,
        )
        # Related for EVERY main hit (each screenshot row gets its own carousel siblings).
        _progress(on_progress, "done", "active", "為每部作品補齊相關…", 0.94)
        total_rel = 0
        def _slot_needs_related(row: dict) -> bool:
            if not isinstance(row, dict) or not row.get("ok") or row.get("stub") or row.get("unidentified"):
                return False
            if _catalog_code_of(row):
                return True
            title = str(row.get("title") or "")
            return bool(is_usable_title(title) and not title.startswith("（"))

        n_ok = sum(1 for r in results if _slot_needs_related(r))
        # One shared related budget. The old floor of 8s per slot made 14 images
        # spend more than a minute after identify had already finished.
        related_left = min(float(MULTI_RELATED_BUDGET_S), max(0.0, _seconds_left(deadline) - 1.5))
        if n_ok <= 1:
            per_budget = min(12.0, related_left)
        else:
            per_budget = min(8.0, related_left / max(n_ok, 1))
        for i, row in enumerate(results):
            if not _slot_needs_related(row) or per_budget < 1.0 or _seconds_left(deadline) < 1.5:
                if isinstance(row, dict):
                    row.setdefault("related_by_title", [])
                continue
            try:
                _progress(
                    on_progress,
                    "done",
                    "active",
                    f"相關作品 {i + 1}/{len(results)}…",
                    0.94 + 0.05 * ((i + 1) / max(len(results), 1)),
                )
                slot_budget = min(per_budget, max(0.0, _seconds_left(deadline) - 0.4))
                filled = attach_related_by_title(row, budget_sec=slot_budget, per_item=False)
                rel = list(filled.get("related_by_title") or [])
                row["related_by_title"] = rel
                total_rel += len(rel)
            except Exception:
                row.setdefault("related_by_title", [])
        # Top-level related mirrors first work (compat); gallery uses each results[].related_by_title
        if results and isinstance(results[0], dict):
            payload["related_by_title"] = list(results[0].get("related_by_title") or [])
            payload["theme_keywords"] = list(results[0].get("theme_keywords") or [])
            payload["keyword_queries"] = list(results[0].get("keyword_queries") or [])
        else:
            payload["related_by_title"] = []
        payload["results"] = results
        done_detail = f"完成，列出 {len(results)} 部"
        if total_rel:
            done_detail += f"；相關共 {total_rel}"
        _progress(on_progress, "done", "done", done_detail, 1.0)
        _attach_saved_session(payload, _frames_from_multi(vision_rows, results))
        return payload, 200
    finally:
        _leave_batch_ctx()



IDENTIFY_STEPS = (
    ("receive", "接收圖片／文字"),
    ("vision", "看圖辨識（Gemini）"),
    ("parse", "讀取番號／片名"),
    ("verify", "核對片名與番號"),
    ("search", "搜尋作品資料"),
    ("cover", "抓取封面與劇照"),
    ("done", "完成，進入畫廊"),
)


def _progress(cb, step: str, status: str, detail: str = "", progress: float | None = None) -> None:
    """Safe progress callback. status: pending|active|done|skipped|error."""
    if not cb:
        return
    if progress is None:
        ids = [s[0] for s in IDENTIFY_STEPS]
        try:
            idx = ids.index(step)
            if status == "done":
                progress = (idx + 1) / len(ids)
            elif status == "active":
                progress = idx / len(ids)
            elif status == "skipped":
                progress = (idx + 1) / len(ids)
            else:
                progress = idx / len(ids)
        except ValueError:
            progress = 0.0
    try:
        cb({"step": step, "status": status, "detail": detail or "", "progress": round(float(progress), 3)})
    except Exception:
        pass


def _payload_as_visual_candidate(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    code = str(payload.get("code") or "").strip()
    if not code or not parse_code_parts(code):
        return None
    stills = payload.get("stills") if isinstance(payload.get("stills"), list) else []
    return {
        "code": format_display_code(code),
        "title": payload.get("title"),
        "actress": payload.get("actress"),
        "studio": payload.get("studio"),
        "cid": payload.get("cid"),
        "cover": payload.get("cover"),
        "stills": list(stills),
        "score": payload.get("score") or payload.get("title_score") or 0.4,
        "source": payload.get("source"),
    }


def _apply_visual_winner(
    payload: dict,
    ranked: list[dict],
    meta: dict,
    *,
    promote_candidates: bool = False,
) -> dict:
    """Copy the visual winner onto an identify payload and keep the compare note."""
    out = dict(payload)
    out["visual_meta"] = meta
    if not ranked:
        return out
    best = ranked[0]
    vm = best.get("visual") or {}
    locked = _visual_is_lock(vm)
    try:
        conf = float(best.get("visual_score") or vm.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    out["visual_confident"] = bool(locked and vm.get("match_person") and conf >= 0.55)
    for field in ("code", "title", "actress", "studio", "cover", "cid"):
        if best.get(field):
            out[field] = best.get(field)
    if best.get("stills"):
        out["stills"] = best.get("stills")
    if promote_candidates and len(ranked) >= 2:
        out["candidates"] = ranked
    win = format_display_code(str(out.get("code") or ""))
    if win and isinstance(out.get("related_by_title"), list):
        out["related_by_title"] = [
            item
            for item in out["related_by_title"]
            if not (
                isinstance(item, dict)
                and format_display_code(str(item.get("code") or "")) == win
            )
        ]
    note = str(meta.get("note") or "").strip()
    if note:
        prev = str(out.get("message") or "")
        if note not in prev:
            out["message"] = (prev + " " + note).strip() if prev else note
        rn = str(out.get("related_note") or "")
        if note not in rn:
            out["related_note"] = (rn + "；" + note).strip("；") if rn else note
    return out


def reverify_cached_image_hit(
    cached: dict,
    user_image_bytes: bytes | None,
    api_key: str | None = None,
) -> dict | None:
    """Re-run cover + stills visual match for a same-image cache hit.

    A locked result (or a multi-candidate sort) is returned. A single cached
    code that does not lock returns None so identify can search siblings
    instead of replaying 僅排序未鎖定.
    """
    if not user_image_bytes or not isinstance(cached, dict) or not cached.get("ok"):
        return None
    pool: list[dict] = []
    main = _payload_as_visual_candidate(cached)
    if main:
        pool.append(main)
    for key in ("candidates", "related_by_title", "related"):
        for raw in cached.get(key) or []:
            if not isinstance(raw, dict):
                continue
            cand = _payload_as_visual_candidate(raw)
            if cand:
                pool.append(cand)
    seen: set[str] = set()
    cands: list[dict] = []
    for c in pool:
        code = format_display_code(str(c.get("code") or ""))
        if not code or code in seen:
            continue
        seen.add(code)
        cands.append(c)
    if not cands:
        return None
    ranked, meta = rank_candidates_by_visual(
        user_image_bytes, cands, api_key=api_key
    )
    if not meta.get("visual_ranked"):
        return None
    best = ranked[0] if ranked else None
    locked = _visual_is_lock((best or {}).get("visual") or {})
    if not locked and len(cands) < 2:
        return None
    out = _apply_visual_winner(cached, ranked, meta, promote_candidates=False)
    out["from_offline_cache"] = True
    out["image_reverified"] = True
    _recompute_theme_keywords(out)
    _finalize_related_note(out)
    return out


def _ensure_image_visual_rank(
    result: dict,
    image_bytes: bytes | None,
    api_key: str | None,
    extra_candidates: list | None = None,
) -> dict:
    """Compare cover + stills when this image identify has not ranked yet."""
    if not image_bytes or not isinstance(result, dict) or not result.get("ok"):
        return result
    if (result.get("visual_meta") or {}).get("visual_ranked"):
        return result
    pool: list[dict] = []
    main = _payload_as_visual_candidate(result)
    if main:
        pool.append(main)
    for raw in list(extra_candidates or []) + list(result.get("candidates") or []):
        if not isinstance(raw, dict):
            continue
        cand = _payload_as_visual_candidate(raw)
        if cand:
            pool.append(cand)
    seen: set[str] = set()
    cands: list[dict] = []
    for c in pool:
        code = format_display_code(str(c.get("code") or ""))
        if not code or code in seen:
            continue
        seen.add(code)
        cands.append(c)
    if not cands:
        return result
    ranked, meta = rank_candidates_by_visual(image_bytes, cands, api_key=api_key)
    if not meta.get("visual_ranked"):
        result = dict(result)
        result["visual_meta"] = meta
        return result
    return _apply_visual_winner(result, ranked, meta, promote_candidates=True)


def _complete_identify_result(
    result: dict,
    *,
    image_bytes: bytes | None = None,
    api_key: str | None = None,
    extra_candidates: list | None = None,
    skip_related: bool = False,
    image_hash: str | None = None,
) -> dict:
    """Shared finish for code, title, and image identify.

    Visual lock (when an image is present and not yet ranked), then the same
    related buckets and title-keyword stamp. The actress note is reconciled
    after the carousel exists so it cannot claim the bucket is empty.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    if image_bytes:
        try:
            result = _ensure_image_visual_rank(
                result, image_bytes, api_key, extra_candidates
            )
        except Exception:
            pass
    if result.get("from_offline_cache"):
        try:
            result = enrich_offline_cache_hit(result, image_hash=image_hash)
        except Exception:
            pass
    elif not skip_related:
        try:
            result = attach_related_by_title(result, budget_sec=14.0)
        except Exception:
            result.setdefault("related_by_title", [])
    else:
        result.setdefault("related_by_title", [])
    _stamp_listed_work_keywords(result)
    _finalize_related_note(result)
    try:
        offline_cache_put(result, image_hash=image_hash)
    except Exception:
        pass
    return result


def run_identify_pipeline(
    *,
    image_bytes: bytes | None = None,
    filename: str | None = None,
    user_code: str = "",
    user_title: str = "",
    user_actress: str = "",
    on_progress=None,
    skip_related: bool = False,
) -> tuple[dict, int]:
    """
    Shared identify logic for JSON and SSE endpoints.
    Returns (payload_dict, http_status).
    skip_related=True: caller (e.g. multi) will attach related_by_title once later.
    """
    ocr_preview = None
    ocr_text = ""
    vision_used = False
    vision_meta: dict | None = None
    extra_msg: str | None = None
    ambiguous_codes: list[str] = []
    text_queries: list[str] = []
    code = (user_code or "").strip()
    user_title = (user_title or "").strip()
    search_mode = "manual" if code else ("title" if user_title else "code")
    api_key = get_gemini_api_key()

    # Step 1: receive
    recv_bits = []
    if image_bytes:
        recv_bits.append("圖片")
    if code:
        recv_bits.append(f"番號 {format_display_code(code)}")
    if user_title:
        recv_bits.append("片名文字")
    _progress(on_progress, "receive", "active", "正在接收輸入…" if recv_bits else "等待輸入…", 0.02)
    if not image_bytes and not code and not user_title:
        _progress(on_progress, "receive", "error", "請提供 image、code 或 title", 0.0)
        return (
            empty_identify(
                message="請提供 image、code 或 title",
                vision_used=False,
                search_mode="manual",
            ),
            400,
        )
    _progress(
        on_progress,
        "receive",
        "done",
        "已接收：" + "、".join(recv_bits),
        1 / 6,
    )

    img_hash = image_content_hash(image_bytes) if image_bytes else None

    def _finish(payload: dict, extras: list | None = None) -> dict:
        return _complete_identify_result(
            payload,
            image_bytes=image_bytes,
            api_key=api_key,
            extra_candidates=extras,
            skip_related=skip_related,
            image_hash=img_hash,
        )
    # Same screenshot may reuse catalog fields, but must re-check cover + stills
    # against this upload. A previous 僅排序未鎖定 must not be replayed as-is.
    if img_hash and not code and not user_title:
        try:
            cached_img = offline_cache_get(image_hash=img_hash)
        except Exception:
            cached_img = None
        if cached_img and cached_img.get("ok"):
            cached_img = dict(cached_img)
            cached_img["vision_used"] = False
            cached_img["search_mode"] = "code"
            cached_img.setdefault("related_by_title", cached_img.get("related_by_title") or [])
            cached_img = enrich_offline_cache_hit(cached_img, image_hash=img_hash)
            refreshed = None
            try:
                refreshed = reverify_cached_image_hit(cached_img, image_bytes, api_key)
            except Exception:
                refreshed = None
            if refreshed:
                _progress(on_progress, "vision", "done", "同圖重核封面與劇照", 2 / 6)
                _progress(on_progress, "parse", "done", f"番號：{refreshed.get('code') or '—'}", 3 / 6)
                _progress(on_progress, "search", "done", "已對照原圖重核", 4 / 6)
                _progress(
                    on_progress,
                    "cover",
                    "done",
                    "封面與劇照已重核",
                    5 / 6,
                )
                _progress(on_progress, "done", "done", "完成（同圖視覺重核）", 1.0)
                return refreshed, 200
            if not (api_key or "").strip() and _upload_matches_cached_cover(image_bytes, cached_img):
                # No vision key: keep the cache only when the jacket still matches.
                _progress(on_progress, "vision", "skipped", "離線快取（同圖）", 2 / 6)
                _progress(on_progress, "parse", "done", f"番號：{cached_img.get('code') or '—'}", 3 / 6)
                _progress(on_progress, "search", "done", "離線快取", 4 / 6)
                _progress(on_progress, "cover", "done" if cached_img.get("cover") else "skipped", "封面（快取）", 5 / 6)
                _progress(on_progress, "done", "done", "完成（離線快取）", 1.0)
                return cached_img, 200
            # Key present but this cached row did not lock: search again.

    # Manual code: try offline cache before vision/network (fast path)
    if code and parse_code_parts(code):
        try:
            cached_code = offline_cache_get(code=code)
        except Exception:
            cached_code = None
        if cached_code and cached_code.get("ok") and not image_bytes:
            disp_c = format_display_code(code)
            _progress(on_progress, "vision", "skipped", "無圖片，略過看圖辨識", 2 / 6)
            _progress(on_progress, "parse", "done", f"番號：{disp_c}", 3 / 6)
            _progress(on_progress, "search", "done", "離線快取", 4 / 6)
            if cached_code.get("cover"):
                _progress(on_progress, "cover", "done", "封面（快取）", 5 / 6)
            else:
                _progress(on_progress, "cover", "skipped", "無封面", 5 / 6)
            cached_code = dict(cached_code)
            cached_code["vision_used"] = False
            cached_code["search_mode"] = "manual" if user_code else "code"
            cached_code.setdefault("related_by_title", cached_code.get("related_by_title") or [])
            cached_code = enrich_offline_cache_hit(cached_code)
            _progress(on_progress, "done", "done", "完成（離線快取）", 1.0)
            return cached_code, 200

    # Step 2: vision (or skip)
    if image_bytes is not None:
        mime = detect_image_mime(image_bytes, filename)
        if api_key:
            _progress(on_progress, "vision", "active", "Gemini 看圖辨識中…", 1 / 6)
            try:
                vision_meta = call_gemini_vision(image_bytes, mime, api_key)
                vision_used = True
                if isinstance(vision_meta, dict) and vision_meta.get("title"):
                    sole_in_title, cleaned_title = _split_title_and_code(vision_meta.get("title"))
                    _ignored, title_codes = _sole_product_code(str(vision_meta.get("title") or ""))
                    vision_meta = dict(vision_meta)
                    vision_meta["title"] = cleaned_title
                    if sole_in_title and not vision_meta.get("code"):
                        vision_meta["code"] = sole_in_title
                    elif len(title_codes) >= 2:
                        ambiguous_codes = title_codes
                vcode = (vision_meta or {}).get("code")
                if vcode and not code:
                    code = str(vcode)
                    search_mode = "code"
                detail = "看圖完成"
                if vision_meta.get("code"):
                    detail += f"：番號 {vision_meta.get('code')}"
                elif vision_meta.get("title"):
                    detail += "：已讀到片名"
                _progress(on_progress, "vision", "done", detail, 2 / 6)
            except Exception as e:
                extra_msg = f"看圖辨識失敗，改用 OCR：{e}"
                vision_meta = None
                vision_used = False
                _progress(on_progress, "vision", "error", str(extra_msg)[:120], 2 / 6)
                try:
                    ocr_text = ocr_image_bytes(image_bytes)
                    ocr_preview = (ocr_text or "")[:500]
                    if not code:
                        sole, many = _sole_trusted_ocr_code(ocr_text)
                        if sole:
                            code = sole
                            search_mode = "code"
                        elif many:
                            ambiguous_codes = many
                except Exception as ocr_e:
                    _progress(on_progress, "parse", "error", f"OCR 亦失敗：{ocr_e}", 0.4)
                    return (
                        empty_identify(
                            message=f"{extra_msg}；OCR 亦失敗：{ocr_e}",
                            ocr_preview=ocr_preview,
                            vision_used=False,
                            search_mode="manual",
                        ),
                        500,
                    )
        else:
            extra_msg = "未設定 GEMINI_API_KEY，看圖辨識不可用；改用 OCR＋查詢。"
            _progress(on_progress, "vision", "skipped", "未設定 Gemini，略過看圖", 2 / 6)
            try:
                ocr_text = ocr_image_bytes(image_bytes)
                ocr_preview = (ocr_text or "")[:500]
                if not code:
                    sole, many = _sole_trusted_ocr_code(ocr_text)
                    if sole:
                        code = sole
                        search_mode = "code"
                    elif many:
                        ambiguous_codes = many
            except Exception as e:
                _progress(on_progress, "parse", "error", f"OCR 失敗：{e}", 0.4)
                return (
                    empty_identify(
                        message=f"{extra_msg} OCR 失敗：{e}",
                        ocr_preview=ocr_preview,
                        vision_used=False,
                        search_mode="manual",
                    ),
                    500,
                )

        # Vision succeeded but no code → OCR for code
        if vision_used and not code:
            try:
                ocr_text = ocr_image_bytes(image_bytes)
                ocr_preview = (ocr_text or "")[:500]
                sole, many = _sole_trusted_ocr_code(ocr_text)
                if sole:
                    code = sole
                    search_mode = "code"
                    extra_msg = (extra_msg + " " if extra_msg else "") + "看圖未讀出番號，已用 OCR 補番號。"
                elif many:
                    ambiguous_codes = many
            except Exception:
                pass
    else:
        _progress(on_progress, "vision", "skipped", "無圖片，略過看圖辨識", 2 / 6)

    # A still with almost no print: vision often returns nothing, while OCR
    # still has a short distinctive phrase the catalog can search.
    if image_bytes is not None and not is_usable_title((vision_meta or {}).get("title")):
        ocr_title = _title_from_ocr_text(ocr_preview)
        if ocr_title:
            vision_meta = dict(vision_meta or {})
            vision_meta["title"] = ocr_title
            extra_msg = (extra_msg + " " if extra_msg else "") + "看圖未讀到片名，已用 OCR 補片名。"

    # A corner 品番 is still a focused read on any shot. Dumping every other
    # line into search is separate, and only allowed for one complete cover
    # with no listing row under the thumb. Do not drop this gate.
    if image_bytes is not None:
        read_blob = _compose_read_blob(
            (vision_meta or {}).get("texts") if vision_meta else None,
            (vision_meta or {}).get("title") if vision_meta else None,
            (vision_meta or {}).get("actress") if vision_meta else None,
            (vision_meta or {}).get("studio") if vision_meta else None,
            ocr_text or ocr_preview,
        )
        if not code:
            sole_blob, many_blob = _sole_trusted_ocr_code(read_blob)
            if sole_blob:
                code = sole_blob
                search_mode = "code"
                extra_msg = (extra_msg + " " if extra_msg else "") + "已從整段文字讀出番號。"
            elif many_blob:
                ambiguous_codes = many_blob
        if _cover_full_text_allowed(vision_meta, read_blob):
            text_queries = _full_text_search_queries(
                read_blob,
                actress=(vision_meta or {}).get("actress") if vision_meta else None,
            )
            current_title = str((vision_meta or {}).get("title") or "") if vision_meta else ""
            if (
                not code
                and text_queries
                and (not is_usable_title(current_title) or _is_decorative_overlay(current_title))
            ):
                vision_meta = dict(vision_meta or {})
                vision_meta["title"] = text_queries[0]
                extra_msg = (extra_msg + " " if extra_msg else "") + "已改搜封面上其餘文字。"
        else:
            text_queries = []

    # A dashed OCR token is not the 品番 until its jacket is this picture.
    # Several tokens, or a token glued to the next number, are scored the
    # same way. A low score falls through to title cues.
    code_visually_confirmed = False
    if image_bytes is not None and not user_code and "read_blob" in locals():
        printed, printed_score, had_codes, codes_compared = _resolve_printed_code(
            read_blob, image_bytes
        )
        if printed and printed_score is not None:
            code = printed
            search_mode = "code"
            code_visually_confirmed = True
            extra_msg = (
                (extra_msg + " " if extra_msg else "")
                + f"封面與原圖鎖定番號 {format_display_code(printed)}。"
            )
        elif had_codes and (codes_compared or printed):
            # An OCR token with no jacket score is not a 品番. Title cues
            # take over instead of keeping that unread confirmation.
            if code:
                extra_msg = (
                    (extra_msg + " " if extra_msg else "")
                    + f"讀到的番號與封面不一致，改以片名搜尋。"
                )
            code = ""
            search_mode = "title"

    # Step 3: parse code/title
    _progress(on_progress, "parse", "active", "整理番號／片名…", 2 / 6)

    early_payload: dict | None = None
    early_status = 200
    title_search_hit: dict | None = None
    title_search_query: str = ""
    verify_meta: dict | None = None

    # Cross-check vision/OCR 番號 vs 片名 before trusting the code.
    # Manual user_code skips title mismatch rejection (still resolves cover later).
    ref_title_for_verify = ""
    if user_title and is_usable_title(user_title):
        ref_title_for_verify = user_title.strip()
    elif vision_meta and is_usable_title(vision_meta.get("title")):
        ref_title_for_verify = str(vision_meta.get("title") or "").strip()

    # 「舌技が神」 is cover art, not the catalog line. A 品番 read from the
    # listing caption must not be discarded because that slogan disagrees.
    if (
        code
        and ref_title_for_verify
        and not user_code
        and _is_decorative_overlay(ref_title_for_verify)
    ):
        _progress(on_progress, "verify", "skipped", "短標語不是目錄片名，保留已讀番號", 3 / 7)
        ref_title_for_verify = ""

    if code and ref_title_for_verify and not user_code:
        _progress(
            on_progress,
            "verify",
            "active",
            f"核對 {format_display_code(code)} 與片名…",
            3 / 7,
        )
        try:
            verify_meta = verify_code_matches_title(code, ref_title_for_verify)
        except Exception as ve:
            verify_meta = {
                "ok": False,
                "code": format_display_code(code),
                "reason": f"核對失敗：{ve}",
                "cover_ok": False,
                "title_ok": False,
                "similarity": 0.0,
            }
            if image_bytes:
                try:
                    cid, cover = resolve_cover_cid(format_display_code(code))
                except Exception:
                    cid, cover = None, None
                if cover:
                    verify_meta["cover_ok"] = True
                    verify_meta["cover"] = cover
                    verify_meta["cid"] = cid
        if not verify_meta.get("ok") and not code_visually_confirmed:
            # A noisy OCR title must not throw away a 品番 whose jacket is
            # this picture. A low score still drops the code: that read is
            # the hallucinated PREFIX-NNN case, and title cues take over.
            keep_code, jacket_score = _ocr_code_survives_title_mismatch(verify_meta, image_bytes)
            if keep_code:
                verify_meta = dict(verify_meta)
                verify_meta["ok"] = True
                verify_meta["jacket_score"] = jacket_score
                verify_meta["reason"] = "封面與原圖一致"
                if verify_meta.get("cid") and vision_meta is not None:
                    vision_meta = dict(vision_meta)
                    vision_meta["_verified_cid"] = verify_meta.get("cid")
                    vision_meta["_verified_cover"] = verify_meta.get("cover")
                _progress(
                    on_progress,
                    "verify",
                    "done",
                    f"{format_display_code(code)} 封面與原圖一致（{float(jacket_score or 0):.2f}）",
                    3 / 7,
                )
            else:
                rejected = format_display_code(code)
                reason = verify_meta.get("reason") or "不符"
                cat = verify_meta.get("catalog_title") or ""
                detail = f"拒絕 {rejected}：{reason}"
                if cat:
                    detail += f"（目錄：{str(cat)[:36]}）"
                _progress(on_progress, "verify", "error", detail[:120], 3 / 7)
                extra_msg = (
                    (extra_msg + " " if extra_msg else "")
                    + f"番號 {rejected} 與片名不符或封面無效（{reason}），改以片名搜尋。"
                )
                code = ""
                search_mode = "title"
        else:
            _progress(
                on_progress,
                "verify",
                "done",
                f"{format_display_code(code)} 片名相符"
                + (f"（sim={float(verify_meta.get('similarity') or 0):.2f}）"),
                3 / 7,
            )
            # Prefer resolved cover cid from verify
            if verify_meta.get("cid") and vision_meta is not None:
                vision_meta = dict(vision_meta)
                vision_meta["_verified_cid"] = verify_meta.get("cid")
                vision_meta["_verified_cover"] = verify_meta.get("cover")
    elif code and user_code:
        _progress(on_progress, "verify", "skipped", "手動番號，略過片名核對", 3 / 7)
    elif code:
        # Code without usable title — still probe cover; reject empty cover codes when title exists later
        _progress(on_progress, "verify", "skipped", "無可核對片名", 3 / 7)
    else:
        _progress(on_progress, "verify", "skipped", "尚無番號可核對", 3 / 7)

    # Image path: title search when no code
    if image_bytes is not None and not code and vision_meta and is_usable_title(vision_meta.get("title")):
        vtitle = str(vision_meta.get("title") or "").strip()
        vactress = vision_meta.get("actress")
        vstudio = vision_meta.get("studio")
        _progress(on_progress, "parse", "done", f"已讀到片名：{vtitle[:40]}", 3 / 6)
        _progress(on_progress, "search", "active", "正在用片名搜尋…", 3 / 6)
        phrase_extras = list(text_queries or [])
        phrase_blob = "\n".join(
            str(part or "")
            for part in (
                ocr_preview,
                vtitle,
                read_blob,
            )
        )
        # Full-text phrases only when this is not a listing/player read.
        # A grid or UI capture keeps the focused title. Do not drop this gate.
        if not _listing_chrome_in_text(phrase_blob):
            for extra_q in _ocr_title_repairs(phrase_blob):
                if extra_q not in phrase_extras:
                    phrase_extras.append(extra_q)
            if not _title_is_search_ready(vtitle):
                for extra_q in _ocr_line_prefixes(phrase_blob):
                    if extra_q not in phrase_extras:
                        phrase_extras.append(extra_q)
            for extra_q in _focused_cjk_queries(phrase_blob):
                if extra_q not in phrase_extras:
                    phrase_extras.append(extra_q)
        hit = None
        cover_queries = _ordered_title_queries(vtitle, phrase_extras)
        if image_bytes and not _title_is_search_ready(vtitle):
            try:
                hit = _resolve_unnumbered_cover(cover_queries, image_bytes)
            except Exception:
                hit = None
            if isinstance(hit, dict) and hit.get("code"):
                vtitle = str(hit.get("title") or vtitle)
        if not (isinstance(hit, dict) and hit.get("code")):
            hit = None
        seen_q: set[str] = set()
        if hit is None:
            for q in cover_queries:
                if not q or q in seen_q:
                    continue
                seen_q.add(q)
                if len(seen_q) > 8:
                    break
                try:
                    found = search_by_title(q, actress=vactress)
                except Exception as se:
                    extra_msg = (extra_msg + " " if extra_msg else "") + f"片名搜尋失敗：{se}"
                    found = None
                if not _hit_has_catalog_code(found):
                    continue
                if image_bytes:
                    try:
                        found = apply_visual_rank_to_hit(found, image_bytes, api_key=api_key)
                    except Exception:
                        pass
                if isinstance(found, dict) and found.get("series_unresolved") and not _hit_visually_locked(found):
                    continue
                hit = found
                vtitle = q
                break

        if hit and hit.get("code") and parse_code_parts(str(hit["code"])):
            # Visual rank when multiple same-series candidates.
            # A jacket lock already chosen above stays; ranking again can
            # replace it with whichever row the catalog listed first.
            n_pre = len([c for c in (hit.get("candidates") or []) if c.get("code")]) or (1 if hit.get("code") else 0)
            already_locked = bool((hit.get("visual_meta") or {}).get("visual_lock"))
            if n_pre >= 1 and image_bytes and not already_locked:
                _progress(on_progress, "cover", "active", "對照原圖核對人物／衣服／表情／飾品／姿勢…", 4 / 6)
                hit = apply_visual_rank_to_hit(hit, image_bytes, api_key=api_key)
            code = str(hit["code"])
            search_mode = "title"
            title_search_hit = hit
            title_search_query = vtitle
            if hit.get("title") and not vision_meta.get("title"):
                vision_meta["title"] = hit["title"]
            n_cands = len([c for c in (hit.get("candidates") or []) if c.get("code")])
            if n_cands >= 2:
                msg_bit = (
                    f"以片名搜尋找到 {n_cands} 個不同番號"
                    f"（主選 {format_display_code(code)}，來源：{hit.get('source') or 'web'}）。"
                )
                vmeta = hit.get("visual_meta") or {}
                if vmeta.get("visual_ranked"):
                    msg_bit += " " + (vmeta.get("note") or "已依人物／衣服／姿勢排序")
            else:
                msg_bit = f"以片名搜尋解析番號 {format_display_code(code)}（來源：{hit.get('source') or 'web'}）。"
            extra_msg = (extra_msg + " " if extra_msg else "") + msg_bit
            _progress(on_progress, "search", "done", msg_bit[:100], 4 / 6)
        else:
            # Prefer listing coded candidates over TITLE-SEARCH without cover
            cands = (hit or {}).get("candidates") or []
            coded = [c for c in cands if c.get("code") and parse_code_parts(str(c["code"]))]
            if len(coded) >= 1:
                hit2 = hit or {"candidates": coded, "title": vtitle}
                if len(coded) >= 1 and image_bytes:
                    _progress(on_progress, "cover", "active", "對照原圖核對人物／衣服／表情／飾品／姿勢…", 4 / 6)
                    hit2 = apply_visual_rank_to_hit(hit2, image_bytes, api_key=api_key)
                    vmeta = hit2.get("visual_meta") or {}
                    if vmeta.get("visual_ranked"):
                        extra_msg = (
                            (extra_msg + " " if extra_msg else "")
                            + (vmeta.get("note") or "已對照原圖依人物／衣服／姿勢核對")
                        )
                payload = _finish(
                    multi_candidate_payload(
                        query_title=vtitle,
                        hit=hit2,
                        ocr_preview=ocr_preview,
                        vision_used=vision_used,
                        extra_msg=extra_msg,
                    )
                )
                _progress(on_progress, "search", "done", f"片名候選 {len(coded)} 筆", 4 / 6)
                _progress(on_progress, "cover", "done", "已依番號帶入 CDN 封面", 5 / 6)
                _progress(on_progress, "done", "done", "完成", 1.0)
                return payload, 200
            recovered = _recover_locked_work(
                image_bytes=image_bytes,
                api_key=api_key,
                actress=vactress,
                codes=ambiguous_codes,
                vision_meta=vision_meta,
                image_index=1,
            )
            if recovered and _catalog_code_of(recovered):
                _progress(on_progress, "search", "done", str(recovered.get("message") or "已對上原圖")[:100], 4 / 6)
                payload = _finish(recovered)
                _progress(on_progress, "cover", "done" if payload.get("cover") else "skipped", "封面就緒" if payload.get("cover") else "無封面", 5 / 6)
                _progress(on_progress, "done", "done", "完成", 1.0)
                return payload, 200
            early_payload = _finish(
                title_only_payload(
                    title=vtitle,
                    actress=vactress,
                    studio=vstudio,
                    cover=None,  # no fake / broken cover
                    ocr_preview=ocr_preview,
                    vision_used=vision_used,
                    message=(
                        (extra_msg + " " if extra_msg else "")
                        + f"以片名搜尋：「{vtitle}」。未解析出番號；可手動輸入番號以補齊封面／劇照。"
                    ),
                )
            )
            _progress(on_progress, "search", "done", "片名搜尋未解析番號（部分結果）", 4 / 6)
            _progress(on_progress, "cover", "skipped", "無番號可抓封面", 5 / 6)
            _progress(on_progress, "done", "done", "完成（僅片名）", 1.0)
            return early_payload, 200

    if image_bytes is not None and not code and not early_payload:
        vtitle = (vision_meta or {}).get("title") if vision_meta else None
        recovered = _recover_locked_work(
            image_bytes=image_bytes,
            api_key=api_key,
            actress=(vision_meta or {}).get("actress") if vision_meta else None,
            codes=ambiguous_codes,
            vision_meta=vision_meta,
            image_index=1,
        )
        if recovered and _catalog_code_of(recovered):
            _progress(on_progress, "search", "done", str(recovered.get("message") or "已對上原圖")[:100], 4 / 6)
            payload = _finish(recovered)
            _progress(on_progress, "done", "done", "完成", 1.0)
            return payload, 200
        if is_usable_title(vtitle):
            early_payload = _finish(
                title_only_payload(
                    title=str(vtitle).strip(),
                    actress=(vision_meta or {}).get("actress"),
                    studio=(vision_meta or {}).get("studio"),
                    ocr_preview=ocr_preview,
                    vision_used=vision_used,
                    message=(extra_msg + " " if extra_msg else "")
                    + f"以片名搜尋：「{str(vtitle).strip()}」。未解析出番號。",
                )
            )
            _progress(on_progress, "parse", "done", "僅有片名", 3 / 6)
            _progress(on_progress, "search", "skipped", "無法解析番號", 4 / 6)
            _progress(on_progress, "cover", "skipped", "無番號可抓封面", 5 / 6)
            _progress(on_progress, "done", "done", "完成（僅片名）", 1.0)
            return early_payload, 200
        _progress(on_progress, "parse", "error", "未在圖片中找到番號或片名", 0.45)
        _progress(on_progress, "search", "skipped", "略過", 0.5)
        _progress(on_progress, "cover", "skipped", "略過", 0.5)
        _progress(on_progress, "done", "error", "辨識失敗", 1.0)
        return (
            empty_identify(
                message=(extra_msg + " " if extra_msg else "") + "未在圖片中找到番號或片名",
                ocr_preview=ocr_preview,
                vision_used=vision_used,
                search_mode="manual",
            ),
            200,
        )

    # Title-only input (no image/code)
    if not code and user_title:
        search_mode = "title"
        _progress(on_progress, "parse", "done", f"使用片名：{user_title[:40]}", 3 / 6)
        _progress(on_progress, "search", "active", "正在用片名搜尋…", 3 / 6)
        hit = None
        try:
            hit = search_by_title(user_title, actress=(user_actress or "").strip() or None)
        except Exception as se:
            extra_msg = (extra_msg + " " if extra_msg else "") + f"片名搜尋失敗：{se}"

        if hit and hit.get("series_unresolved") and not image_bytes:
            _progress(on_progress, "search", "done", "同系列多部，片名無法分卷", 4 / 6)
            _progress(on_progress, "cover", "skipped", "不指定番號", 5 / 6)
            _progress(on_progress, "done", "done", "完成（系列未分卷）", 1.0)
            return (
                title_only_payload(
                    title=user_title,
                    actress=(user_actress or "").strip() or None,
                    message=(
                        (extra_msg + " " if extra_msg else "")
                        + f"以片名「{user_title}」對上同系列多部，沒有女優或圖片可分卷，不指定番號。"
                    ),
                ),
                200,
            )

        if hit and hit.get("code") and parse_code_parts(str(hit["code"])):
            n_pre = len([c for c in (hit.get("candidates") or []) if c.get("code")]) or (1 if hit.get("code") else 0)
            if n_pre >= 1 and image_bytes:
                _progress(on_progress, "cover", "active", "對照原圖核對人物／衣服／表情／飾品／姿勢…", 4 / 6)
                hit = apply_visual_rank_to_hit(hit, image_bytes, api_key=api_key)
            code = str(hit["code"])
            title_search_hit = hit
            title_search_query = user_title
            vision_meta = {
                "title": hit.get("title") or user_title,
                "actress": hit.get("actress"),
                "studio": hit.get("studio"),
            }
            n_cands = len([c for c in (hit.get("candidates") or []) if c.get("code")])
            if n_cands >= 2:
                msg_bit = (
                    f"以片名「{user_title}」找到 {n_cands} 個不同番號"
                    f"（主選 {format_display_code(code)}，來源：{hit.get('source') or 'web'}）。"
                )
                vmeta = hit.get("visual_meta") or {}
                if vmeta.get("visual_ranked"):
                    msg_bit += " " + (vmeta.get("note") or "已依人物／衣服／姿勢排序")
            else:
                msg_bit = (
                    f"以片名「{user_title}」解析番號 {format_display_code(code)}"
                    f"（來源：{hit.get('source') or 'web'}）。"
                )
            extra_msg = (extra_msg + " " if extra_msg else "") + msg_bit
            _progress(on_progress, "search", "done", msg_bit[:100], 4 / 6)
        elif hit and (hit.get("title") or user_title):
            cands = hit.get("candidates") or []
            coded = [c for c in cands if c.get("code") and parse_code_parts(str(c["code"]))]
            if coded:
                hit2 = hit
                if len(coded) >= 1 and image_bytes:
                    _progress(on_progress, "cover", "active", "對照原圖核對人物／衣服／表情／飾品／姿勢…", 4 / 6)
                    hit2 = apply_visual_rank_to_hit(hit, image_bytes, api_key=api_key)
                    vmeta = hit2.get("visual_meta") or {}
                    if vmeta.get("visual_ranked"):
                        extra_msg = (
                            (extra_msg + " " if extra_msg else "")
                            + (vmeta.get("note") or "已對照原圖依人物／衣服／姿勢核對")
                        )
                payload = _finish(
                    multi_candidate_payload(
                        query_title=user_title,
                        hit=hit2,
                        ocr_preview=ocr_preview,
                        vision_used=False,
                        extra_msg=extra_msg,
                    )
                )
                _progress(on_progress, "search", "done", f"片名候選 {len(coded)} 筆", 4 / 6)
                _progress(on_progress, "cover", "done", "已依番號帶入 CDN 封面", 5 / 6)
                _progress(on_progress, "done", "done", "完成", 1.0)
                return payload, 200
            payload = _finish(
                title_only_payload(
                    title=str(hit.get("title") or user_title).strip(),
                    actress=hit.get("actress"),
                    studio=hit.get("studio"),
                    cover=None,
                    ocr_preview=ocr_preview,
                    vision_used=False,
                    message=(
                        (extra_msg + " " if extra_msg else "")
                        + f"以片名搜尋：「{user_title}」。未解析出番號；可手動輸入番號以補齊封面／劇照。"
                    ),
                )
            )
            _progress(on_progress, "search", "done", "片名搜尋未解析番號", 4 / 6)
            _progress(on_progress, "cover", "skipped", "無封面", 5 / 6)
            _progress(on_progress, "done", "done", "完成（僅片名）", 1.0)
            return payload, 200
        else:
            _progress(on_progress, "search", "error", f"找不到片名：「{user_title}」", 0.7)
            _progress(on_progress, "cover", "skipped", "略過", 0.7)
            _progress(on_progress, "done", "error", "搜尋失敗", 1.0)
            return (
                empty_identify(
                    message=(extra_msg + " " if extra_msg else "") + f"找不到片名：「{user_title}」",
                    ocr_preview=ocr_preview,
                    vision_used=False,
                    search_mode="title",
                    title=user_title,
                ),
                200,
            )

    if not code:
        _progress(on_progress, "parse", "error", "請提供 image、code 或 title", 0.4)
        return (
            empty_identify(
                message="請提供 image、code 或 title",
                ocr_preview=ocr_preview,
                vision_used=vision_used,
                search_mode="manual",
            ),
            400,
        )

    # Have a code
    disp = format_display_code(code)
    _progress(on_progress, "parse", "done", f"番號：{disp}", 3 / 6)

    # Step 4: search metadata
    _progress(on_progress, "search", "active", f"搜尋作品資料（{disp}）…", 3 / 6)
    result = identify_code(code, ocr_preview=ocr_preview, vision_meta=vision_meta)
    result["vision_used"] = vision_used
    result["search_mode"] = search_mode if search_mode in ("code", "title", "manual") else (
        "manual" if user_code else ("title" if user_title else "code")
    )
    if user_code and not vision_used and not user_title:
        result["search_mode"] = "manual"
    if user_title and not user_code:
        result["search_mode"] = "title"
    if extra_msg:
        prev = result.get("message") or ""
        combined = (extra_msg + (" " + prev if prev else "")).strip()
        if len(combined) > 280:
            combined = combined[:277] + "…"
        result["message"] = combined
    result.setdefault("vision_used", vision_used)

    src = result.get("message") or ""
    if result.get("title"):
        _progress(on_progress, "search", "done", f"標題：{str(result['title'])[:48]}", 4 / 6)
    else:
        _progress(on_progress, "search", "done", (src[:80] or "已查詢（可能無標題）"), 4 / 6)

    # Step 5: cover/stills (URLs already built in identify_code)
    _progress(on_progress, "cover", "active", "CDN 封面載入中…", 4 / 6)
    if result.get("cover"):
        n_stills = len(result.get("stills") or [])
        _progress(on_progress, "cover", "done", f"封面就緒" + (f"＋劇照 {n_stills} 張" if n_stills else ""), 5 / 6)
    else:
        _progress(on_progress, "cover", "skipped", "無封面 URL", 5 / 6)

    # Attach other title-search codes as gallery cards when applicable
    if title_search_hit:
        result = merge_title_candidates(
            result, title_search_hit, query_title=title_search_query
        )

    # Step 6: visual lock (image), related buckets, keyword stamp, honest note.
    ok = bool(result.get("ok"))
    status = 200 if ok else 400
    if ok:
        extras = (title_search_hit or {}).get("candidates") if title_search_hit else None
        result = _finish(result, extras)
        n_extra = len(result.get("candidates") or [])
        detail = "完成，進入畫廊"
        if n_extra >= 2:
            detail = f"完成，列出 {n_extra} 個番號候選"
        if result.get("from_offline_cache"):
            detail = "完成（離線快取）"
        elif not skip_related:
            n_rel = len(result.get("related_by_title") or [])
            if n_rel:
                detail = f"{detail}；片名相關 {n_rel}"
        _progress(on_progress, "done", "done", detail, 1.0)
    else:
        result.setdefault("related_by_title", [])
        _progress(on_progress, "done", "error", result.get("message") or "失敗", 1.0)
    return result, status


@app.post("/api/identify")
def identify():
    user_code = (request.form.get("code") or "").strip()
    user_title = (request.form.get("title") or "").strip()
    images = collect_images_from_request()

    if len(images) > 1:
        result, status = run_multi_identify_pipeline(
            images,
            user_code=user_code,
            user_title=user_title,
        )
    elif len(images) == 1:
        result, status = run_identify_pipeline(
            image_bytes=images[0][0],
            filename=images[0][1],
            user_code=user_code,
            user_title=user_title,
        )
        # related_by_title already attached inside pipeline — do not run twice
    else:
        result, status = run_identify_pipeline(
            image_bytes=None,
            filename=None,
            user_code=user_code,
            user_title=user_title,
        )
        # related_by_title already attached inside pipeline — do not run twice
    result = _ensure_identify_session(result, images)
    return jsonify(result), status


def allowed_media_url(url: str | None) -> bool:
    """True only for DMM CDN image URLs (cover/stills download)."""
    u = (url or "").strip()
    if not u.startswith("http://") and not u.startswith("https://"):
        return False
    if is_now_printing_url(u):
        return False
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return False
    return host in CDN_MEDIA_HOSTS


def _safe_zip_stem(code: str) -> str:
    raw = format_display_code(code or "") or "work"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw)).strip("._") or "work"
    return stem[:40]


def build_work_zip_bytes(
    code: str,
    cover: str | None,
    stills: list | None,
    *,
    fetch_bytes=None,
) -> tuple[bytes, int, str]:
    """Zip one work's cover + stills. Returns (zip_bytes, file_count, filename)."""
    fetcher = fetch_bytes or download_cover_bytes
    stem = _safe_zip_stem(code)
    names_urls: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, url: str | None) -> None:
        u = (url or "").strip()
        if not u or u in seen or not allowed_media_url(u):
            return
        seen.add(u)
        names_urls.append((name, u))

    if cover:
        add("cover", cover)
    for i, u in enumerate(stills or []):
        add(f"still-{i + 1:02d}", str(u or ""))
        if len(names_urls) >= 16:
            break

    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, url in names_urls:
            try:
                blob = fetcher(url, timeout=8.0)
            except TypeError:
                blob = fetcher(url)
            if not blob:
                continue
            zf.writestr(f"{stem}/{name}.jpg", blob)
            n += 1
    return buf.getvalue(), n, f"{stem}.zip"


@app.post("/api/work-zip")
def work_zip():
    """Download cover + stills for a single work as a zip (not the whole session)."""
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    cover = str(body.get("cover") or "").strip()
    stills = body.get("stills") if isinstance(body.get("stills"), list) else []
    data, n, fname = build_work_zip_bytes(code, cover, stills)
    if n <= 0:
        return jsonify({"ok": False, "message": "沒有可下載的圖片"}), 404
    return Response(
        data,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/cdn-file")
def cdn_file():
    """Same-origin JPEG proxy so the client can prefetch cover/stills as Blobs."""
    url = (request.args.get("url") or "").strip()
    if not allowed_media_url(url):
        return jsonify({"ok": False, "message": "不支援的圖片網址"}), 400
    blob = fetch_cdn_file_bytes(url, timeout=8.0)
    if not blob:
        return jsonify({"ok": False, "message": "下載失敗"}), 404
    fname = (url.rsplit("/", 1)[-1] or "image.jpg").split("?")[0]
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", fname)[:80] or "image.jpg"
    return Response(
        blob,
        mimetype="image/jpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/related-by-title", methods=["GET", "POST"])
def related_by_title_api():
    """Fetch related works: title≤5 + keyword≤5 + actress≤3 (caps; history detail).

    POST may include seed related so we skip filled buckets and fill missing
    title_zh on existing slides within HISTORY_RELATED_TITLE_ZH_BUDGET.
    """
    body = request.get_json(silent=True) if request.method == "POST" else None
    body = body if isinstance(body, dict) else {}
    title = (body.get("title") or request.args.get("title") or "").strip()
    code = (
        body.get("code")
        or body.get("exclude")
        or request.args.get("code")
        or request.args.get("exclude")
        or ""
    ).strip()
    actress = (body.get("actress") or request.args.get("actress") or "").strip() or None
    seed = body.get("seed") or body.get("related") or body.get("related_by_title") or []
    if not isinstance(seed, list):
        seed = []
    wrap: dict = {
        "ok": True,
        "code": code or None,
        "title": title,
        "related_by_title": _cap_related_buckets(seed),
    }
    try:
        rel = wrap["related_by_title"]
        t, k, a = _related_bucket_counts(rel)
        need_related = (
            (t < RELATED_THEME_CAP and is_usable_title(title))
            or (k < RELATED_KEYWORD_CAP and is_usable_title(title))
            or (a < RELATED_ACTRESS_CAP and bool(actress))
        )
        # Skip the 16s related search when buckets are already at cap so this
        # pass can spend its wall-clock on missing title_zh instead.
        if need_related:
            try:
                items = find_related_by_title(
                    title,
                    exclude_code=code or None,
                    max_n=5,
                    actress=actress,
                    budget_sec=16.0,
                    seed=rel,
                    fill_theme=t < RELATED_THEME_CAP,
                    fill_keyword=k < RELATED_KEYWORD_CAP,
                    fill_actress=a < RELATED_ACTRESS_CAP,
                )
                wrap["related_by_title"] = _merge_related_for_cache(rel, items)
            except Exception:
                pass
        for item in wrap.get("related_by_title") or []:
            try:
                _backfill_item_stills(item)
            except Exception:
                pass
        try:
            attach_chinese_titles(
                wrap,
                related_network=True,
                related_budget_sec=HISTORY_RELATED_TITLE_ZH_BUDGET,
            )
        except Exception:
            pass
        _stamp_theme_keywords(wrap)
    except Exception as e:
        return jsonify({"ok": False, "related_by_title": [], "message": str(e)}), 500
    return jsonify({
        "ok": True,
        "related_by_title": wrap.get("related_by_title") or [],
        "title": title,
        "title_zh": wrap.get("title_zh"),
        "exclude": code,
        "theme_keywords": wrap.get("theme_keywords") or [],
        "keyword_queries": wrap.get("keyword_queries") or [],
    })


@app.route("/api/related-by-keywords", methods=["POST"])
def related_by_keywords_api():
    """Re-search the keyword bucket using only the chips the user selected.

    ≥2 keywords → keep works that hit multiple selected keywords (cap 10).
    1 keyword → that keyword may fill the row (cap 10). No junk pad.
    Does not change the main related carousel (keyword bucket stays at 5).
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    title = str(body.get("title") or "").strip()
    code = str(body.get("code") or body.get("exclude") or "").strip()
    actress = str(body.get("actress") or "").strip() or None
    keywords = _normalize_keyword_list(body.get("keywords"))
    min_hits = _selected_keyword_min_hits(keywords)
    stamped = _stamp_theme_keywords({"title": title, "actress": actress})
    if not keywords:
        return jsonify({
            "ok": True,
            "keywords": [],
            "min_hits": 0,
            "related": [],
            "theme_keywords": stamped.get("theme_keywords") or [],
            "keyword_queries": stamped.get("keyword_queries") or [],
        })
    try:
        items = _find_related_by_keywords(
            title,
            exclude_code=code or None,
            actress=actress,
            max_n=RELATED_KEYWORD_RESEARCH_CAP,
            budget_sec=6.0,
            keywords=keywords,
            min_hits=min_hits,
        )
    except Exception as e:
        return jsonify({"ok": False, "related": [], "keywords": keywords, "message": str(e)}), 500
    wrap: dict = {"ok": True, "title": title, "related_by_title": items}
    try:
        attach_chinese_titles(
            wrap,
            related_network=True,
            related_budget_sec=3.0,
        )
    except Exception:
        pass
    related = list(wrap.get("related_by_title") or [])[:RELATED_KEYWORD_RESEARCH_CAP]
    return jsonify({
        "ok": True,
        "keywords": keywords,
        "min_hits": min_hits,
        "related": related,
        "theme_keywords": stamped.get("theme_keywords") or [],
        "keyword_queries": _keyword_search_queries(title, keywords, selected_only=True),
    })


@app.post("/api/identify/stream")
def identify_stream():
    """SSE progress stream then final result event. Accepts multiple images."""
    user_code = (request.form.get("code") or "").strip()
    user_title = (request.form.get("title") or "").strip()
    images = collect_images_from_request()
    n_images = len(images)

    def generate():
        import queue
        import threading

        q: queue.Queue = queue.Queue()

        def on_progress(evt: dict) -> None:
            q.put(("progress", evt))

        def worker() -> None:
            try:
                if n_images > 1:
                    result, status = run_multi_identify_pipeline(
                        images,
                        user_code=user_code,
                        user_title=user_title,
                        on_progress=on_progress,
                    )
                elif n_images == 1:
                    result, status = run_identify_pipeline(
                        image_bytes=images[0][0],
                        filename=images[0][1],
                        user_code=user_code,
                        user_title=user_title,
                        on_progress=on_progress,
                    )
                    # related already attached in pipeline
                else:
                    result, status = run_identify_pipeline(
                        image_bytes=None,
                        filename=None,
                        user_code=user_code,
                        user_title=user_title,
                        on_progress=on_progress,
                    )
                    # related already attached in pipeline
                result = _ensure_identify_session(result, images)
                q.put(("result", {"type": "result", "data": result, "status": status}))
            except Exception as e:
                q.put(
                    (
                        "result",
                        {
                            "type": "result",
                            "data": empty_identify(message=f"伺服器錯誤：{e}"),
                            "status": 500,
                        },
                    )
                )
            finally:
                q.put(("end", None))

        threading.Thread(target=worker, daemon=True).start()

        if n_images > 1:
            steps = [
                {"id": "receive", "label": f"接收圖片（{n_images} 張）"},
                {"id": "vision", "label": "逐張看圖辨識"},
                {"id": "parse", "label": "彙整番號／片名"},
                {"id": "verify", "label": "核對片名與番號"},
                {"id": "search", "label": "搜尋作品資料"},
                {"id": "cover", "label": "抓取封面與劇照"},
                {"id": "done", "label": "完成，進入畫廊"},
            ]
        else:
            steps = [{"id": s, "label": l} for s, l in IDENTIFY_STEPS]
        yield f"data: {json.dumps({'type': 'steps', 'steps': steps}, ensure_ascii=False)}\n\n"

        while True:
            try:
                kind, payload = q.get(timeout=float(STREAM_KEEPALIVE_S))
            except queue.Empty:
                # Comment frames keep the SSE connection alive while a slot
                # is still comparing jackets. They are not a result.
                yield ": keepalive\n\n"
                continue
            if kind == "end":
                break
            if kind == "progress":
                body = {"type": "progress", **payload}
                yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
            elif kind == "result":
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Static files from web root
@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/<path:path>")
def static_files(path: str):
    # Do not shadow /api/*
    if path.startswith("api/"):
        return jsonify({"ok": False, "message": "not found"}), 404
    target = ROOT / path
    if target.is_file():
        return send_from_directory(ROOT, path)
    return jsonify({"ok": False, "message": "not found"}), 404



# warm demo cache for gunicorn workers
try:
    load_demo()
except Exception:
    pass

if __name__ == "__main__":
    # Load demo once at startup
    load_demo()
    import os
    port = int(os.environ.get("PORT") or "8787")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
