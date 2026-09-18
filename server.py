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
CDN_MEDIA_HOSTS = {"pics.dmm.co.jp"}
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
VISION_PROMPT = """你是 AV／JAV 列表截圖辨識助手。圖片可能是 JAVDB 等網站的整頁截圖：封面圖下方或旁邊有番號與日文片名。

請只讀取畫面中「主作品／焦點那一筆」的資訊，回傳 JSON（不要 markdown、不要程式碼圍欄、不要多餘說明）：
{"code":"MIDA-616","title":"日本語タイトル","actress":"...","studio":"...","confidence":0.0,"notes":""}

規則：
1. 務必嘗試讀出番號（品番，如 MIDA-616）以及封面附近／下方的日文片名那一行。若是封面局部裁切、無明顯番號，也請盡力讀出畫面上可見的日文標題片段。
2. 片名請用畫面上的原文（多半是日文），不要翻譯、不要發明、不要補全看不到的字。
3. 看不清楚的欄位請填 null；confidence 為 0.0～1.0。
4. actress／studio 若畫面沒有就 null。
5. 只輸出一行合法 JSON。
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


def _is_authed() -> bool:
    if _is_owner_device():
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


def title_similarity(a: str | None, b: str | None) -> float:
    a = normalize_ocr_title(a) or (a or "").strip()
    b = normalize_ocr_title(b) or (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
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
    return "now_printing" in (url or "").lower()


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
    """Lightweight code→official title for cross-check (jav321 / javlibrary / ddg)."""
    display = format_display_code(code)
    slugs = [display]
    alt = code_stripped_form(display)
    if alt:
        slugs.append(alt)
    for slug in slugs:
        for fetcher in (fetch_javbus, fetch_javlibrary, fetch_duckduckgo):
            try:
                meta = fetcher(slug)
            except Exception:
                meta = None
            if meta and (meta.get("title") or "").strip():
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
        # Ensure readable image extension for tesseract
        try:
            img = Image.open(raw_path)
            img = ImageOps.exif_transpose(img)
            png_path = td_path / "orig.png"
            img.save(png_path, format="PNG")
        except Exception:
            png_path = raw_path

        t1 = run_tesseract(str(png_path))
        texts.append(t1)

        try:
            prep = td_path / "prep.png"
            preprocess_image(png_path, prep)
            t2 = run_tesseract(str(prep))
            texts.append(t2)
        except Exception:
            pass

    # Prefer the text that yields more AV codes
    best = ""
    best_n = -1
    for t in texts:
        n = len(extract_codes(t))
        if n > best_n or (n == best_n and len(t) > len(best)):
            best = t
            best_n = n
    return best


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

    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        confidence = None

    return {
        "code": code,
        "title": clean_str(data.get("title")),
        "actress": clean_str(data.get("actress")),
        "studio": clean_str(data.get("studio")),
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


def call_gemini_vision(image_bytes: bytes, mime_type: str, api_key: str) -> dict[str, Any]:
    image_bytes, mime_type = maybe_downscale_for_vision(image_bytes, mime_type)
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
    for model in GEMINI_MODELS:
        # Never log api_key; keep it only in the request URL query.
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            r = requests.post(url, json=payload_base, timeout=VISION_TIMEOUT)
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
            last_err = RuntimeError(f"model {model} 逾時（~{VISION_TIMEOUT}s）")
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


def download_cover_bytes(url: str, timeout: float | None = None) -> bytes | None:
    """Fetch candidate cover image bytes (short timeout). Prefer DMM CDN."""
    if timeout is None:
        timeout = float(COVER_DOWNLOAD_TIMEOUT)
    u = (url or "").strip()
    if not u.startswith("http"):
        return None
    try:
        r = requests.get(
            u,
            timeout=(2, timeout),
            headers={"User-Agent": UA, "Referer": "https://www.dmm.co.jp/"},
            verify=False,
        )
        if r.status_code >= 400 or not r.content or len(r.content) < 800:
            return None
        if is_now_printing_url(str(r.url or u)):
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" in ctype:
            return None
        return r.content
    except Exception:
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
    try:
        url = f"https://www.avbase.net/works?q={quote(title)}"
        r = requests.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code >= 400 or not r.text:
            return []
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        works = ((data.get("props") or {}).get("pageProps") or {}).get("works") or []
        for w in works:
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
        return out
    out.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return out


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

    # Always cross-check top candidates against stills vs the *original user image*.
    # User shots are often still crops (rear/side); cover-only same_work false-positives
    # on series siblings must be revoked when stills disagree on clothes/person.
    remain = budget_s - (time.monotonic() - t0)
    if ranked_pairs and remain >= 6.5:
        still_pairs: list[tuple[dict, bytes]] = []
        n_still = min(4, len(ranked_pairs)) if len(ranked_pairs) >= 2 else 1
        for _sc, it in ranked_pairs[:n_still]:
            cid = str(it.get("cid") or "") or None
            if not cid:
                code = format_display_code(str(it.get("code") or ""))
                cid = code_to_cid(code) if code else None
            urls = []
            if cid:
                try:
                    urls = still_urls(cid, 3)
                except Exception:
                    urls = []
            blob = None
            for u in urls:
                blob = download_cover_bytes(u)
                if blob:
                    break
            if blob:
                still_pairs.append((dict(it), blob))
        if len(still_pairs) >= 1:
            meta["mode"] = (meta.get("mode") or "batch") + "+stills"
            timeout = max(7.0, min(16.0, remain - 0.5))
            # Batch path needs 2+; for a single top candidate, duplicate-call via batch of 1
            # by pairing with a tiny second download if needed — else pairwise attach.
            vms = None
            if len(still_pairs) >= 2:
                vms = _run_batch(still_pairs, timeout)
            elif len(still_pairs) == 1:
                # Single still vs user: reuse batch API with one cover list via private path
                only_item, only_blob = still_pairs[0]
                vms_one = gemini_rank_covers_batch(
                    user_image_bytes,
                    [only_blob],
                    key,
                    timeout=timeout,
                    labels=[str(only_item.get("code") or "")],
                )
                vms = vms_one if vms_one else None
            if vms and len(vms) == len(still_pairs):
                meta["compared"] += len(still_pairs)
                meta["note_stills"] = True
                by_code: dict[str, tuple[float, dict]] = {
                    format_display_code(str(it.get("code") or "")): (sc, it)
                    for sc, it in ranked_pairs
                }
                for (item, _b), vm in zip(still_pairs, vms):
                    sc, attached = _attach(item, vm)
                    code_k = format_display_code(str(attached.get("code") or ""))
                    prev = by_code.get(code_k)
                    still_vm = attached.get("visual") or {}
                    still_ok = bool(
                        still_vm.get("same_work")
                        and still_vm.get("match_person")
                        and still_vm.get("match_clothes")
                    )
                    if prev is None:
                        by_code[code_k] = (sc, attached)
                        continue
                    prev_sc, prev_it = prev
                    prev_vm = prev_it.get("visual") or {}
                    prev_ok = bool(
                        prev_vm.get("same_work")
                        and prev_vm.get("match_person")
                        and prev_vm.get("match_clothes")
                    )
                    # Stills confirm → prefer stills
                    if still_ok:
                        by_code[code_k] = (max(sc, prev_sc + 0.01), attached)
                        continue
                    # Cover claimed same_work but stills reject clothes/person → revoke
                    if prev_ok and not still_ok:
                        revoked = dict(prev_it)
                        rvm = dict(prev_vm)
                        rvm["same_work"] = False
                        if still_vm.get("match_clothes") is False:
                            rvm["match_clothes"] = False
                        if still_vm.get("match_person") is False:
                            rvm["match_person"] = False
                        if still_vm.get("match_face") is False:
                            rvm["match_face"] = False
                        if still_vm.get("match_pose") is False:
                            rvm["match_pose"] = False
                        rvm["confidence"] = min(float(rvm.get("confidence") or 0), 0.34)
                        reason_bit = str(still_vm.get("reason") or "stills mismatch")
                        rvm["reason"] = (
                            str(rvm.get("reason") or "")
                            + f"｜劇照核對否決：{reason_bit}"
                        )[:240]
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
                        revoked["score"] = round(
                            float(revoked["visual_score"]) * 0.92 + ts * 0.08, 4
                        )
                        by_code[code_k] = (float(revoked["score"]), revoked)
                        meta["stills_revoked"] = True
                        continue
                    # Otherwise take higher score
                    if sc > prev_sc + 0.05:
                        by_code[code_k] = (sc, attached)
                ranked_pairs = sorted(by_code.values(), key=lambda x: x[0], reverse=True)

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
        ncmp = meta["compared"]
        still_bit = "＋劇照" if meta.get("note_stills") else ""
        revoke_bit = "；已否決封面誤判" if meta.get("stills_revoked") else ""
        best_vm = (ranked_list[0].get("visual") or {}) if ranked_list else {}
        lock_bit = (
            "視覺鎖定"
            if best_vm.get("same_work") and best_vm.get("match_clothes")
            else "僅排序未鎖定"
        )
        meta["note"] = (
            f"已對照使用者原圖比對 {ncmp} 張封面{still_bit}"
            f"（主選 {best_code}；{lock_bit}；依人物／衣服／表情／飾品／姿勢{revoke_bit}）"
        )
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
        return (c.get("score") or 0, src, pref)

    ranked.sort(key=rank, reverse=True)
    return ranked


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

    # 0a) AVBase on original title (+ shorter prefixes) BEFORE Gemini rewrite
    early_av: list[dict] = []
    try:
        seen_codes: set[str] = set()
        for q in title_query_variants(original_title):
            for item in fetch_avbase_title_results(q, actress=actress):
                code = item.get("code")
                if not code or code in seen_codes:
                    continue
                # Re-score against the full original title
                item = dict(item)
                item["score"] = max(
                    float(item.get("score") or 0),
                    title_similarity(original_title, str(item.get("title") or "")),
                )
                if original_title and original_title[:8] in str(item.get("title") or ""):
                    item["score"] = max(float(item["score"]), 0.85)
                seen_codes.add(code)
                early_av.append(item)
            # Stop early once we have a strong hit
            strong = [c for c in early_av if (c.get("score") or 0) >= 0.55]
            if strong:
                break
    except Exception:
        early_av = []
    early_av = filter_title_candidates(early_av, min_score=0.30)
    if early_av and (early_av[0].get("score") or 0) >= 0.45:
        best = early_av[0]
        out = {
            "code": best.get("code"),
            "title": best.get("title") or original_title,
            "actress": best.get("actress") or actress,
            "studio": best.get("studio"),
            "cover": best.get("cover"),
            "cid": best.get("cid"),
            "source": best.get("source"),
            "score": best.get("score"),
        }
        return _pack(out, early_av)

    # 0a2) Distinctive short n-grams when full OCR title still misses (censored glyphs / truncation)
    if not early_av or (early_av and (early_av[0].get("score") or 0) < 0.45):
        try:
            short_qs: list[str] = []
            for q in _title_related_keyword_queries(original_title):
                if q and 4 <= len(q) <= 16 and q not in short_qs:
                    short_qs.append(q)
            # Prefer mid-title chunks that OCR usually gets right (bus/seat etc.)
            raw = normalize_ocr_title(original_title) or original_title
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
                    item = dict(item)
                    item["score"] = max(
                        float(item.get("score") or 0),
                        title_similarity(original_title, str(item.get("title") or "")),
                    )
                    # Boost if distinctive query appears in catalog title
                    ct = str(item.get("title") or "")
                    if q and q in ct:
                        item["score"] = max(float(item["score"]), 0.72)
                    seen_short.add(code)
                    early_av.append(item)
                strong = [c for c in early_av if (c.get("score") or 0) >= 0.55]
                if strong:
                    break
            early_av = filter_title_candidates(early_av, min_score=0.30)
            if early_av and (early_av[0].get("score") or 0) >= 0.45:
                best = early_av[0]
                out = {
                    "code": best.get("code"),
                    "title": best.get("title") or original_title,
                    "actress": best.get("actress") or actress,
                    "studio": best.get("studio"),
                    "cover": best.get("cover"),
                    "cid": best.get("cid"),
                    "source": best.get("source"),
                    "score": best.get("score"),
                }
                return _pack(out, early_av)
        except Exception:
            pass

    # 0b) Chinese title → Gemini map to JP title + code
    gemini_hit: dict | None = None
    query_title_zh: str | None = None
    if is_chinese_heavy_title(title):
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
                queries.extend(title_query_variants(base_q))
        for q in dict.fromkeys(queries):
            for item in fetch_avbase_title_results(q, actress=actress):
                if any(c.get("code") == item.get("code") for c in candidates):
                    continue
                item = dict(item)
                # score vs full original title
                item["score"] = max(
                    float(item.get("score") or 0),
                    title_similarity(original_title, str(item.get("title") or "")),
                )
                if original_title and original_title[:8] in str(item.get("title") or ""):
                    item["score"] = max(float(item["score"]), 0.85)
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

    out = {
        "code": best.get("code"),
        "title": best.get("title") or title,
        "title_zh": best.get("title_zh") or query_title_zh,
        "actress": best.get("actress") or actress,
        "studio": best.get("studio"),
        "cover": best.get("cover"),
        "cid": best.get("cid"),
        "source": best.get("source"),
        "score": best.get("score"),
    }
    return _pack(out, good)



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

def apply_vision_meta(payload: dict, vision_meta: dict | None) -> dict:
    """Prefer vision title (and fill missing actress/studio) over OCR/lookup."""
    if not vision_meta:
        return payload
    vt = vision_meta.get("title")
    if vt:
        payload["title"] = vt
    if vision_meta.get("actress") and not payload.get("actress"):
        payload["actress"] = vision_meta["actress"]
    if vision_meta.get("studio") and not payload.get("studio"):
        payload["studio"] = vision_meta["studio"]
    return payload



# --- Offline identify cache (server-side, persists successful lookups) ---
OFFLINE_CACHE_MAX = 500
# Soft wall-clock for filling missing title_zh on cache hits (main + related).
OFFLINE_CACHE_TITLE_ZH_BUDGET = 3.0
# Incremental related top-up on cache hits (skip buckets already at cap).
OFFLINE_CACHE_RELATED_BUDGET = 8.0
# Related maxima (caps, not quotas — never pad with junk).
RELATED_THEME_CAP = 5
RELATED_KEYWORD_CAP = 5
RELATED_ACTRESS_CAP = 3
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
            }
        )
        if len(slim) >= 13:
            break
    return slim


def _related_cache_item_key(it: dict | None) -> str | None:
    if not isinstance(it, dict) or not it.get("code"):
        return None
    return _offline_cache_entry_id(str(it.get("code"))) or str(it.get("code"))


def _related_line_of(x: dict | None) -> str:
    if not isinstance(x, dict):
        return "theme"
    ln = str(x.get("line") or "")
    why = str(x.get("why") or "")
    if ln in {"theme", "title"} or "片名" in why or "主題" in why:
        return "theme"
    if ln == "keyword" or "關鍵字" in why:
        return "keyword"
    if ln == "actress" or "演員" in why or "女優" in why:
        return "actress"
    return ln or "theme"


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
    keyword.sort(key=lambda x: int(x.get("keyword_hits") or 0), reverse=True)
    return theme + keyword[:RELATED_KEYWORD_CAP] + actress


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
        for field in ("title", "title_zh", "actress", "cover", "cid", "line", "why"):
            if not merged.get(field) and incoming.get(field):
                merged[field] = incoming[field]
        if incoming.get("stills"):
            merged["stills"] = _merge_unique_urls(merged.get("stills"), incoming.get("stills"))
        by_key[k] = merged
    return _cap_related_buckets([by_key[k] for k in order])


def _payload_needs_title_zh(payload: dict) -> bool:
    """True when main or any coded related slide is missing title_zh."""
    if not isinstance(payload, dict):
        return False
    if not str(payload.get("title_zh") or "").strip():
        return True
    for key in ("related_by_title", "related"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code or not parse_code_parts(str(code)):
                continue
            if not str(item.get("title_zh") or "").strip():
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


def _backfill_main_stills(payload: dict) -> None:
    """Keep existing stills; only add unique CDN URLs if under STILLS_TARGET.

    Does not download — still_urls are deterministic CDN paths from cid.
    """
    existing = payload.get("stills") if isinstance(payload.get("stills"), list) else []
    kept = _merge_unique_urls(existing, [], cap=20)
    if len(kept) >= STILLS_TARGET:
        payload["stills"] = kept
        return
    cid = str(payload.get("cid") or "").strip()
    extra = still_urls(cid, STILLS_TARGET) if cid else []
    payload["stills"] = _merge_unique_urls(kept, extra, cap=20)


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
        tuple(str(u) for u in (payload.get("stills") or [])),
        keys,
        zh,
    )


def enrich_offline_cache_hit(payload: dict, *, image_hash: str | None = None) -> dict:
    """Incremental backfill for a cache/history hit, then merge-put.

    Keep existing cover/stills/related. Only fill gaps: missing title_zh,
    related buckets under 5/5/3, extra still URLs if under target.
    Skip network for buckets already at cap.
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("cache_backfilled") or payload.get("chinese_titles_attached"):
        return payload
    payload.setdefault(
        "related_by_title",
        payload.get("related_by_title") or payload.get("related") or [],
    )
    payload.setdefault("related", payload.get("related") or [])
    before = _payload_enrichment_fingerprint(payload)
    try:
        _backfill_main_stills(payload)
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
    payload["cache_backfilled"] = True
    payload["chinese_titles_attached"] = True
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
                    for field in ("title", "title_zh", "actress", "studio", "cid"):
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

    # Prefer vision title; fill missing fields from vision
    if vision_meta:
        if vision_meta.get("title"):
            title = vision_meta["title"]
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



def apply_visual_rank_to_hit(
    hit: dict,
    user_image_bytes: bytes | None,
    *,
    api_key: str | None = None,
) -> dict:
    """Reorder/verify hit.candidates vs *original user image* (1+ coded candidates).

    Locks 番號/片名 onto the visual winner only when same_work + clothes/person
    gates pass (visual_confident). Otherwise still reorders by visual score but
    marks visual_confident=False so UI/message can show 未鎖定.
    """
    if not hit or not user_image_bytes:
        return hit
    cands = list(hit.get("candidates") or [])
    if not cands and hit.get("code"):
        cands = [hit]
    coded = [c for c in cands if c.get("code") and parse_code_parts(str(c["code"]))]
    if len(coded) < 1:
        return hit
    ranked, meta = rank_candidates_by_visual(
        user_image_bytes, coded, api_key=api_key
    )
    if not meta.get("visual_ranked"):
        hit = dict(hit)
        hit["visual_meta"] = meta
        return hit
    best = ranked[0]
    out = dict(hit)
    # Candidates already sorted by visual score; expose that ordering as main
    out["candidates"] = ranked
    vm0 = best.get("visual") or {}
    locked = bool(
        vm0.get("same_work")
        and vm0.get("match_person")
        and vm0.get("match_clothes")
    )
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


_WEAK_THEME_TOKENS = frozenset(
    {
        "中出し",
        "顔射",
        "NTR",
        "SEX",
        "OL",
        "CA",
        "VR",
        "油",
        "彼女",
        "お姉さん",
        "人妻",
        "拘束",
        "監禁",
        "調教",
        "開発",
        "開發",
        "下着",
        "会社",
        "オフィス",
    }
)


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
    "彼女",
    "お姉さん",
    "ナース",
    "女医",
    "秘書",
    "CA",
    "ノーブラ",
    "中出し",
    "顔射",
    "拘束",
    "監禁",
    "痴女",
    "逆レ",
    "毎朝",
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
)


def _title_sibling_phrases(title: str) -> list[str]:
    """Distinctive title phrases for 片名相近 / same-series catalog search."""
    raw = normalize_ocr_title(title) or (title or "").strip()
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
        i = t.find(kw)
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
        ("声我慢", "SEX"),
        ("逆", "NTR"),
        ("夜行", "バス"),
    ):
        if a in t and b in t:
            ia, ib = t.find(a), t.find(b)
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
        ("乳首", "開発"),
    ):
        if a in t and b in t:
            comp = a + b
            if 4 <= len(comp) <= 10 and comp in t and comp not in must:
                must.append(comp)
            ia, ib = t.find(a), t.find(b)
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


def _extract_title_theme_keywords(title: str, actress: str | None = None) -> list[str]:
    """Discrete theme keywords from a title (満員/電車/媚薬/巨乳/OL …).

    Prefer lexicon + Latin tokens; avoid junk 2–3 char scraps that pad unrelated hits.
    """
    raw = (title or "").strip()
    if not raw:
        return []
    t = raw
    if actress:
        for piece in re.split(r"[\s　・/|]+", str(actress)):
            piece = piece.strip()
            if len(piece) >= 2:
                t = t.replace(piece, " ")
    t_norm = t
    found: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        tok = (tok or "").strip()
        if len(tok) < 2:
            return
        key = tok.casefold()
        if key in seen:
            return
        seen.add(key)
        found.append(tok)

    for kw in sorted(_THEME_KEYWORD_LEXICON, key=len, reverse=True):
        if kw.casefold() in t_norm.casefold():
            _add(kw)
            t_norm = re.sub(re.escape(kw), " ", t_norm, flags=re.IGNORECASE)

    for m in re.finditer(r"[A-Za-z]{2,6}", t):
        _add(m.group(0).upper())

    # Only keep longer leftover compounds (4+), not 2–3 char noise
    for m in re.finditer(r"[\u4e00-\u9fff\u3040-\u30ff]{4,6}", t_norm):
        chunk = m.group(0)
        if re.fullmatch(r"[\u3040-\u309f]+", chunk):
            continue
        if len(found) >= 10:
            break
        _add(chunk)

    return found[:10]


def _keyword_hit_count(candidate_title: str, keywords: list[str]) -> int:
    text = (candidate_title or "").casefold()
    if not text or not keywords:
        return 0
    n = 0
    for kw in keywords:
        if kw and kw.casefold() in text:
            n += 1
    return n


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
    if lcs >= 12:
        return True, max(sim, 0.62)
    # Very high overall similarity (near-duplicate / same series rename)
    if sim >= 0.72:
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


def _find_related_by_keywords(
    title: str,
    *,
    exclude_code: str | None = None,
    actress: str | None = None,
    max_n: int = 5,
    budget_sec: float = 6.0,
    already: set[str] | None = None,
) -> list[dict]:
    """Up to max_n works matching title theme keywords; more hits rank higher.

    Tight: compound queries first; require ≥2 keyword hits; no single-hit junk pad.
    """
    import time as _time

    if max_n <= 0:
        return []
    keywords = _extract_title_theme_keywords(title, actress=actress)
    if len(keywords) < 1:
        return []
    distinctive = [k for k in keywords if k.upper() not in _WEAK_THEME_TOKENS and k not in _WEAK_THEME_TOKENS]
    t0 = _time.monotonic()
    budget = float(budget_sec) if budget_sec and budget_sec > 0 else 6.0
    exclude = ""
    if exclude_code and parse_code_parts(str(exclude_code)):
        exclude = format_display_code(str(exclude_code))
    seen: set[str] = set(already or ())
    if exclude:
        seen.add(exclude)

    queries: list[str] = []

    def _add_q(q: str) -> None:
        q = (q or "").strip()
        if len(q) >= 2 and q not in queries:
            queries.append(q)

    # Prefer sibling phrases / compounds over bare weak tokens
    for p in _title_sibling_phrases(title)[:6]:
        if p not in _WEAK_THEME_TOKENS and len(p) >= 4:
            _add_q(p)

    ordered = sorted(
        distinctive or keywords,
        key=lambda k: (title.find(k) if k and k in title else 10_000, -len(k)),
    )
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if a and b and a.casefold() != b.casefold():
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
        ("声我慢", "SEX"),
    ):
        if any(k.casefold() == a.casefold() for k in keywords) and any(
            k.casefold() == b.casefold() for k in keywords
        ):
            _add_q(a + b)
    # Strong singles last — skip ultra-common alone
    for kw in ordered[:4]:
        if kw not in _WEAK_THEME_TOKENS and kw.upper() not in _WEAK_THEME_TOKENS:
            _add_q(kw)
    queries = queries[:8]

    ranked: dict[str, tuple[float, dict]] = {}
    for q in queries:
        if _time.monotonic() - t0 > budget:
            break
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
        for c in rows[:12]:
            code_raw = str(c.get("code") or "").strip()
            if not code_raw or not parse_code_parts(code_raw):
                continue
            code = format_display_code(code_raw)
            if code in seen:
                continue
            hits = _keyword_hit_count(str(c.get("title") or ""), keywords)
            if hits < 2:
                continue
            d_hits = _keyword_hit_count(str(c.get("title") or ""), distinctive or keywords)
            sc = float(hits) * 10.0 + float(d_hits) * 3.0 + float(c.get("score") or 0)
            prev = ranked.get(code)
            if prev is None or sc > prev[0]:
                ranked[code] = (sc, c)

    ordered_rows = sorted(ranked.values(), key=lambda x: x[0], reverse=True)
    out: list[dict] = []
    for sc, c in ordered_rows:
        hits = _keyword_hit_count(str(c.get("title") or ""), keywords)
        if hits < 2:
            continue
        # Drop weak multi-hits that only share common tokens
        d_hits = _keyword_hit_count(str(c.get("title") or ""), distinctive or keywords)
        if d_hits < 1 and hits < 3:
            continue
        why = f"關鍵字×{hits}"
        item = enrich_title_candidate(c, why=why)
        item["line"] = "keyword"
        item["why"] = why
        item["keyword_hits"] = hits
        out.append(item)
        seen.add(format_display_code(str(c.get("code") or "")))
        if len(out) >= max_n:
            break
    return out[:max_n]


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
            act_compact = re.sub(r"[\s　・·．.]+", "", act)
            act_hit = 1.0 if (name in act or (compact and compact in act_compact)) else 0.35
            sc = float(c.get("score") or 0) * 0.5 + act_hit
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
                if "女優" in why and "主題" not in why:
                    continue
                _push(r, why=why or "主題相近", line="theme")
                theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
                if theme_n >= title_cap:
                    break
        except Exception:
            pass

    theme_n = sum(1 for x in out if str(x.get("line")) == "theme")

    # --- 2) Keywords — independent bucket (cap 5), always try when keywords exist ---
    keyword_items_added = 0
    if fill_keyword and keywords and _left() >= 1.2:
        try:
            for r in _find_related_by_keywords(
                title,
                exclude_code=exclude or None,
                actress=actress,
                max_n=keyword_cap,
                budget_sec=min(6.0, max(2.0, _left() * 0.45)),
                already=seen,
            ):
                # Strong title-series matches found via keyword search → promote to theme
                ok, _sc = _is_title_theme_match(
                    title, str(r.get("title") or ""), keywords=keywords, phrases=phrases
                )
                if ok and theme_n < title_cap:
                    _push(r, why="片名相近", line="theme")
                    theme_n += 1
                    continue
                _push(r, why=str(r.get("why") or "名稱關鍵字"), line="keyword")
                keyword_items_added += 1
                if keyword_items_added >= keyword_cap:
                    break
        except Exception:
            pass

    theme_n = sum(1 for x in out if str(x.get("line")) == "theme")
    keyword_n = sum(1 for x in out if str(x.get("line")) == "keyword")

    # --- 3) Actress — independent bucket (cap 3), always try when actress known ---
    if fill_actress and (actress or "").strip() and _left() >= 0.8:
        try:
            for r in _find_related_by_actress(
                actress,
                exclude_code=exclude or None,
                max_n=actress_cap,
                budget_sec=min(4.0, max(1.5, _left())),
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

    # Stable display order: theme → keyword → actress (caps already applied)
    theme_items = [x for x in out if str(x.get("line")) == "theme"][:title_cap]
    keyword_items = [x for x in out if str(x.get("line")) == "keyword"][:keyword_cap]
    actress_items = [x for x in out if str(x.get("line")) == "actress"][:actress_cap]
    other_items = [
        x
        for x in out
        if str(x.get("line")) not in {"theme", "keyword", "actress"}
    ]
    # Within keyword tier: more hits first
    keyword_items.sort(key=lambda x: int(x.get("keyword_hits") or 0), reverse=True)
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

    # Attach Chinese titles on related + main when missing
    try:
        attach_chinese_titles(result)
    except Exception:
        pass

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
                fixed.append(enrich_title_candidate(r, why=str(r.get("why") or "片名相近")))
            item["related_by_title"] = fixed[:13]
            try:
                attach_chinese_titles(item)
            except Exception:
                pass
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
            attach_chinese_titles(item)
        except Exception:
            item["related_by_title"] = []
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


def run_multi_identify_pipeline(
    images: list[tuple[bytes, str | None]],
    *,
    user_code: str = "",
    user_title: str = "",
    on_progress=None,
) -> tuple[dict, int]:
    """Vision each image → resolve works → dedupe by code. Single image delegates."""
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
    api_key = get_gemini_api_key()
    _progress(on_progress, "receive", "active", f"正在接收 {n} 張圖片…", 0.02)
    _progress(on_progress, "receive", "done", f"已接收 {n} 張圖片", 1 / 7)

    vision_rows: list[dict] = []
    for i, (img_bytes, fname) in enumerate(images):
        idx = i + 1
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
            except Exception as e:
                row["vision_error"] = str(e)[:120]
                try:
                    ocr_text = ocr_image_bytes(img_bytes)
                    best = pick_best_code(extract_codes(ocr_text))
                    if best:
                        row["code"] = best
                except Exception:
                    pass
        else:
            try:
                ocr_text = ocr_image_bytes(img_bytes)
                best = pick_best_code(extract_codes(ocr_text))
                if best:
                    row["code"] = best
            except Exception:
                pass
        # Manual overrides apply to first image only as seed
        if i == 0 and user_code and not row.get("code"):
            row["code"] = user_code
        if i == 0 and user_title and not row.get("title"):
            row["title"] = user_title
        vision_rows.append(row)
        detail = f"第 {idx}/{n} 張"
        if row.get("code"):
            detail += f"：{format_display_code(str(row['code']))}"
        elif row.get("title"):
            detail += "：已讀到片名"
        else:
            detail += "：未讀到"
        _progress(
            on_progress,
            "vision",
            "done" if idx == n else "active",
            detail,
            0.05 + 0.35 * (idx / n),
        )

    _progress(on_progress, "vision", "done", f"已看完 {n} 張", 0.42)
    _progress(on_progress, "parse", "active", "彙整番號／片名…", 0.45)

    # Build unique resolve jobs (prefer code; else title); track drops
    jobs: list[dict] = []
    dropped: list[dict] = []  # {reason, code, title, index}
    seen_codes: set[str] = set()
    seen_titles: set[str] = set()
    for row in vision_rows:
        code = (row.get("code") or "").strip()
        title = (row.get("title") or "").strip()
        idx = row.get("index")
        if code and parse_code_parts(code):
            disp = format_display_code(code)
            if disp in seen_codes:
                dropped.append(
                    {"reason": "duplicate", "code": disp, "title": title, "index": idx}
                )
                continue
            seen_codes.add(disp)
            jobs.append({"kind": "code", "code": disp, "title": title, "row": row})
        elif is_usable_title(title):
            key = title.casefold()
            if key in seen_titles:
                dropped.append(
                    {"reason": "duplicate", "code": "", "title": title, "index": idx}
                )
                continue
            seen_titles.add(key)
            jobs.append({"kind": "title", "code": "", "title": title, "row": row})
        else:
            dropped.append(
                {
                    "reason": "no_signal",
                    "code": code or "",
                    "title": title or "",
                    "index": idx,
                }
            )

    if user_code and parse_code_parts(user_code):
        disp = format_display_code(user_code)
        if disp not in seen_codes:
            seen_codes.add(disp)
            jobs.insert(0, {"kind": "code", "code": disp, "title": user_title, "row": None})
    if user_title and is_usable_title(user_title):
        key = user_title.casefold()
        if key not in seen_titles and not any(j.get("title", "").casefold() == key for j in jobs):
            jobs.append({"kind": "title", "code": "", "title": user_title, "row": None})

    n_dup = sum(1 for d in dropped if d["reason"] == "duplicate")
    n_nosig = sum(1 for d in dropped if d["reason"] == "no_signal")
    _progress(
        on_progress,
        "parse",
        "done",
        (
            f"待查 {len(jobs)} 部（已去重"
            + (f"，略過 {n_dup}" if n_dup else "")
            + (f"，無番號 {n_nosig}" if n_nosig else "")
            + "）"
        )
        if jobs
        else "無可查詢項目",
        0.5,
    )

    if not jobs:
        _progress(on_progress, "search", "error", "多圖皆未找到番號或片名", 0.7)
        _progress(on_progress, "done", "error", "辨識失敗", 1.0)
        drop_bits = []
        if n_dup:
            drop_bits.append(f"{n_dup} 張去重")
        if n_nosig:
            drop_bits.append(f"{n_nosig} 張未讀到")
        extra = f"（{'／'.join(drop_bits)}）" if drop_bits else ""
        return (
            empty_identify(
                message=f"已看 {n} 張圖，皆未找到番號或片名{extra}",
                vision_used=any(r.get("vision_used") for r in vision_rows),
                search_mode="code",
            ),
            200,
        )

    _progress(on_progress, "verify", "skipped", "多圖路徑：逐部查詢", 0.52)
    results: list[dict] = []
    failed_jobs: list[dict] = []
    for ji, job in enumerate(jobs):
        _progress(
            on_progress,
            "search",
            "active",
            f"搜尋第 {ji + 1}/{len(jobs)} 部…",
            0.55 + 0.25 * (ji / max(len(jobs), 1)),
        )
        row = job.get("row")
        vm = None
        if row:
            vm = {
                "title": row.get("title"),
                "actress": row.get("actress"),
                "studio": row.get("studio"),
                "code": row.get("code"),
            }
        try:
            if job["kind"] == "code":
                one, _st = run_identify_pipeline(
                    image_bytes=None,
                    filename=None,
                    user_code=job["code"],
                    user_title="",
                    on_progress=None,
                    skip_related=True,
                )
                # Prefer vision title when present
                if vm and vm.get("title") and one.get("ok"):
                    one = apply_vision_meta(one, vm)
            else:
                # Already vision'd above — resolve by title only (no second vision)
                one, _st = run_identify_pipeline(
                    image_bytes=None,
                    filename=None,
                    user_code="",
                    user_title=job["title"],
                    on_progress=None,
                    skip_related=True,
                )
                if vm and one.get("ok"):
                    one = apply_vision_meta(one, vm)
        except Exception as e:
            one = empty_identify(message=f"查詢失敗：{e}")
        if not one.get("ok"):
            failed_jobs.append(job)
            stub = build_multi_fail_stub(job, why="多圖未找到資料")
            stub["line"] = "main" if not results else "multi"
            results.append(stub)
            dropped.append(
                {
                    "reason": "resolve_fail",
                    "code": job.get("code") or "",
                    "title": job.get("title") or "",
                    "index": (row or {}).get("index"),
                }
            )
            continue
        # Dedupe by code against collected results
        code = one.get("code")
        if code and parse_code_parts(str(code)):
            disp = format_display_code(str(code))
            if any(
                format_display_code(str(r.get("code") or "")) == disp
                for r in results
                if r.get("code") and parse_code_parts(str(r.get("code") or ""))
            ):
                dropped.append(
                    {
                        "reason": "duplicate",
                        "code": disp,
                        "title": one.get("title") or "",
                        "index": (row or {}).get("index"),
                    }
                )
                continue
            one["code"] = disp
        # Do NOT attach_related_by_title per hit — once on final payload (capped budget)
        one.setdefault("related_by_title", [])
        one["from_image_index"] = (row or {}).get("index")
        one["why"] = one.get("why") or "多圖辨識"
        one["line"] = "main" if not results else "multi"
        results.append(one)

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

    n_fail = sum(1 for d in dropped if d["reason"] == "resolve_fail")
    n_dup = sum(1 for d in dropped if d["reason"] == "duplicate")
    n_nosig = sum(1 for d in dropped if d["reason"] == "no_signal")
    n_drop_notice = n_fail + n_dup + n_nosig
    ok_count = sum(1 for r in results if not r.get("stub"))
    fail_codes = [
        format_display_code(d["code"]) if d.get("code") and parse_code_parts(d["code"]) else (d.get("title") or "?")
        for d in dropped
        if d["reason"] == "resolve_fail"
    ]

    msg = f"多圖辨識：{n} 張 → {len(results)} 部"
    if n_drop_notice:
        bits = []
        if n_fail:
            bits.append(f"{n_fail} 張查詢失敗")
        if n_dup:
            bits.append(f"{n_dup} 張去重")
        if n_nosig:
            bits.append(f"{n_nosig} 張未讀到")
        msg += f"（{'／'.join(bits)}）"
        if fail_codes:
            msg += "：" + "、".join(fail_codes[:4])
            if len(fail_codes) > 4:
                msg += "…"
    elif n != len(results):
        msg += "（已去重）"

    note = f"多圖辨識共 {len(results)} 部"
    if n_fail:
        note += f"；其中 {n_fail} 部僅顯示番號／試封面（資料未找到）"
    if n_dup or n_nosig:
        note += f"；略過 {n_dup + n_nosig} 張（去重／未讀到）"

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
        "dropped": dropped,
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
    n_ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and not r.get("stub"))
    # Split budget across works; keep a floor so later rows still get actress+keyword.
    per_budget = 12.0 if n_ok <= 1 else max(8.0, min(12.0, 36.0 / max(n_ok, 1)))
    for i, row in enumerate(results):
        if not isinstance(row, dict) or not row.get("ok") or row.get("stub"):
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
            filled = attach_related_by_title(row, budget_sec=per_budget, per_item=False)
            rel = list(filled.get("related_by_title") or [])
            row["related_by_title"] = rel
            total_rel += len(rel)
        except Exception:
            row.setdefault("related_by_title", [])
    # Top-level related mirrors first work (compat); gallery uses each results[].related_by_title
    if results and isinstance(results[0], dict):
        payload["related_by_title"] = list(results[0].get("related_by_title") or [])
    else:
        payload["related_by_title"] = []
    payload["results"] = results
    done_detail = f"完成，列出 {len(results)} 部"
    if total_rel:
        done_detail += f"；相關共 {total_rel}"
    _progress(on_progress, "done", "done", done_detail, 1.0)
    return payload, 200



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


def run_identify_pipeline(
    *,
    image_bytes: bytes | None = None,
    filename: str | None = None,
    user_code: str = "",
    user_title: str = "",
    on_progress=None,
    skip_related: bool = False,
) -> tuple[dict, int]:
    """
    Shared identify logic for JSON and SSE endpoints.
    Returns (payload_dict, http_status).
    skip_related=True: caller (e.g. multi) will attach related_by_title once later.
    """
    ocr_preview = None
    vision_used = False
    vision_meta: dict | None = None
    extra_msg: str | None = None
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
    # Same screenshot → reuse offline cache (skip vision/network)
    if img_hash and not code and not user_title:
        try:
            cached_img = offline_cache_get(image_hash=img_hash)
        except Exception:
            cached_img = None
        if cached_img and cached_img.get("ok"):
            _progress(on_progress, "vision", "skipped", "離線快取（同圖）", 2 / 6)
            _progress(on_progress, "parse", "done", f"番號：{cached_img.get('code') or '—'}", 3 / 6)
            _progress(on_progress, "search", "done", "離線快取", 4 / 6)
            if cached_img.get("cover"):
                _progress(on_progress, "cover", "done", "封面（快取）", 5 / 6)
            else:
                _progress(on_progress, "cover", "skipped", "無封面", 5 / 6)
            cached_img = dict(cached_img)
            cached_img["vision_used"] = False
            cached_img["search_mode"] = "code"
            cached_img.setdefault("related_by_title", cached_img.get("related_by_title") or [])
            cached_img = enrich_offline_cache_hit(cached_img, image_hash=img_hash)
            _progress(on_progress, "done", "done", "完成（離線快取）", 1.0)
            return cached_img, 200

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
                vcode = vision_meta.get("code")
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
                        best = pick_best_code(extract_codes(ocr_text))
                        if best:
                            code = best
                            search_mode = "code"
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
                    best = pick_best_code(extract_codes(ocr_text))
                    if best:
                        code = best
                        search_mode = "code"
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
                best = pick_best_code(extract_codes(ocr_text))
                if best:
                    code = best
                    search_mode = "code"
                    extra_msg = (extra_msg + " " if extra_msg else "") + "看圖未讀出番號，已用 OCR 補番號。"
            except Exception:
                pass
    else:
        _progress(on_progress, "vision", "skipped", "無圖片，略過看圖辨識", 2 / 6)

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
        if not verify_meta.get("ok"):
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
        hit = None
        try:
            hit = search_by_title(vtitle, actress=vactress)
        except Exception as se:
            extra_msg = (extra_msg + " " if extra_msg else "") + f"片名搜尋失敗：{se}"

        if hit and hit.get("code") and parse_code_parts(str(hit["code"])):
            # Visual rank when multiple same-series candidates
            n_pre = len([c for c in (hit.get("candidates") or []) if c.get("code")]) or (1 if hit.get("code") else 0)
            if n_pre >= 1 and image_bytes:
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
                payload = multi_candidate_payload(
                    query_title=vtitle,
                    hit=hit2,
                    ocr_preview=ocr_preview,
                    vision_used=vision_used,
                    extra_msg=extra_msg,
                )
                _progress(on_progress, "search", "done", f"片名候選 {len(coded)} 筆", 4 / 6)
                _progress(on_progress, "cover", "done", "已依番號帶入 CDN 封面", 5 / 6)
                _progress(on_progress, "done", "done", "完成", 1.0)
                return payload, 200
            early_payload = title_only_payload(
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
            _progress(on_progress, "search", "done", "片名搜尋未解析番號（部分結果）", 4 / 6)
            _progress(on_progress, "cover", "skipped", "無番號可抓封面", 5 / 6)
            _progress(on_progress, "done", "done", "完成（僅片名）", 1.0)
            return early_payload, 200

    if image_bytes is not None and not code and not early_payload:
        vtitle = (vision_meta or {}).get("title") if vision_meta else None
        if is_usable_title(vtitle):
            early_payload = title_only_payload(
                title=str(vtitle).strip(),
                actress=(vision_meta or {}).get("actress"),
                studio=(vision_meta or {}).get("studio"),
                ocr_preview=ocr_preview,
                vision_used=vision_used,
                message=(extra_msg + " " if extra_msg else "")
                + f"以片名搜尋：「{str(vtitle).strip()}」。未解析出番號。",
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
            hit = search_by_title(user_title)
        except Exception as se:
            extra_msg = (extra_msg + " " if extra_msg else "") + f"片名搜尋失敗：{se}"

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
                payload = multi_candidate_payload(
                    query_title=user_title,
                    hit=hit2,
                    ocr_preview=ocr_preview,
                    vision_used=False,
                    extra_msg=extra_msg,
                )
                _progress(on_progress, "search", "done", f"片名候選 {len(coded)} 筆", 4 / 6)
                _progress(on_progress, "cover", "done", "已依番號帶入 CDN 封面", 5 / 6)
                _progress(on_progress, "done", "done", "完成", 1.0)
                return payload, 200
            payload = title_only_payload(
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

    # Step 6: done
    ok = bool(result.get("ok"))
    status = 200 if ok else 400
    if ok:
        n_extra = len(result.get("candidates") or [])
        detail = "完成，進入畫廊"
        if n_extra >= 2:
            detail = f"完成，列出 {n_extra} 個番號候選"
        # related_by_title — skip full re-search when served from offline cache;
        # incremental backfill fills title_zh + remaining 5/5/3 related slots.
        if result.get("from_offline_cache"):
            result.setdefault("related_by_title", result.get("related_by_title") or [])
            if not result.get("cache_backfilled") and not result.get("chinese_titles_attached"):
                result = enrich_offline_cache_hit(result, image_hash=img_hash)
            detail = "完成（離線快取）"
        elif not skip_related:
            try:
                result = attach_related_by_title(result, budget_sec=14.0)
                n_rel = len(result.get("related_by_title") or [])
                if n_rel:
                    detail = f"{detail}；片名相關 {n_rel}"
            except Exception:
                result.setdefault("related_by_title", [])
        else:
            result.setdefault("related_by_title", [])
        try:
            offline_cache_put(result, image_hash=img_hash)
        except Exception:
            pass
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
    """Same-origin attachment for one allowed CDN image (sequential-download fallback)."""
    url = (request.args.get("url") or "").strip()
    if not allowed_media_url(url):
        return jsonify({"ok": False, "message": "不支援的圖片網址"}), 400
    blob = download_cover_bytes(url, timeout=8.0)
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


@app.get("/api/related-by-title")
def related_by_title_api():
    """Fetch related works: title≤5 + keyword≤5 + actress≤3 (caps; history detail)."""
    title = (request.args.get("title") or "").strip()
    code = (request.args.get("code") or request.args.get("exclude") or "").strip()
    actress = (request.args.get("actress") or "").strip() or None
    try:
        items = find_related_by_title(title, exclude_code=code or None, max_n=5, actress=actress, budget_sec=16.0)
        # Light Chinese title attach for API consumers
        wrap = {"ok": True, "code": code or None, "title": title, "related_by_title": items}
        try:
            attach_chinese_titles(wrap)
            items = wrap.get("related_by_title") or items
        except Exception:
            pass
    except Exception as e:
        return jsonify({"ok": False, "related_by_title": [], "message": str(e)}), 500
    return jsonify({
        "ok": True,
        "related_by_title": items,
        "title": title,
        "title_zh": wrap.get("title_zh"),
        "exclude": code,
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
            kind, payload = q.get()
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
