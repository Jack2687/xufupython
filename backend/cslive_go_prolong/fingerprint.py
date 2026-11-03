"""Stripe 设备指纹：m.stripe.com/6 注册 guid/muid/sid（cslive_go_direct_pay 流程专属副本）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import uuid
import urllib.parse
from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

_PLUGINS_STR = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)
_CANVAS_FPS = [
    "0100100101111111101111101111111001110010110111110111111",
    "0100100101111111101111101111111001110010110111110111110",
    "0100100101111111101111101111111001110010110111110111101",
]
_AUDIO_FPS = [
    "d331ca493eb692cfcd19ae5db713ad4b",
    "a7c5f72e1b3d4e8f9c0d2a6b7e8f1c3d",
    "e4b8d6f2a0c3d5e7f9b1c3d5e7f9a0b2",
]


def local_fingerprint() -> tuple[str, str, str]:
    def _id() -> str:
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]

    return _id(), _id(), _id()


def _encode_m6(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(urllib.parse.quote(raw, safe="").encode()).decode()


def _b64url_seg(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def register_fingerprint(http: requests.Session, timeout: int = 10) -> tuple[str, str, str]:
    """向 m.stripe.com/6 注册设备指纹，失败则回退本地随机。"""
    guid, muid, sid = local_fingerprint()
    fp_id = uuid.uuid4().hex
    screens = [(1920, 1080, 1), (1536, 864, 1.25), (2560, 1440, 1), (1440, 900, 1)]
    sw, sh, dpr = random.choice(screens)
    vh = sh - random.randint(40, 70)
    cpu = random.choice([4, 8, 12, 16])
    canvas_fp = random.choice(_CANVAS_FPS)
    audio_fp = random.choice(_AUDIO_FPS)

    def _build_full(v2: int, inc_ids: bool) -> dict[str, Any]:
        s1, s2, s3, s4, s5 = (_b64url_seg() for _ in range(5))
        ts_now = int(time.time() * 1000)
        return {
            "v2": v2,
            "id": fp_id,
            "t": round(random.uniform(3, 120), 1),
            "tag": "$npm_package_version",
            "src": "js",
            "a": {
                "a": {"v": "true", "t": 0},
                "b": {"v": "false", "t": 0},
                "c": {"v": "zh-CN", "t": 0},
                "d": {"v": "Win32", "t": 0},
                "e": {"v": _PLUGINS_STR, "t": round(random.uniform(0, 0.5), 1)},
                "f": {"v": f"{sw}w_{vh}h_24d_{dpr}r", "t": 0},
                "g": {"v": str(cpu), "t": 0},
                "h": {"v": "false", "t": 0},
                "i": {"v": "sessionStorage-enabled, localStorage-enabled", "t": round(random.uniform(0.5, 2), 1)},
                "j": {"v": canvas_fp, "t": round(random.uniform(5, 120), 1)},
                "k": {"v": "", "t": 0},
                "l": {"v": USER_AGENT, "t": 0},
                "m": {"v": "", "t": 0},
                "n": {"v": "false", "t": round(random.uniform(3, 50), 1)},
                "o": {"v": audio_fp, "t": round(random.uniform(20, 30), 1)},
            },
            "b": {
                "a": f"https://{s1}.{s2}.{s3}/",
                "b": f"https://{s1}.{s3}/{s4}/{s5}/{_b64url_seg()}",
                "c": _b64url_seg(),
                "d": muid if inc_ids else "NA",
                "e": sid if inc_ids else "NA",
                "f": False,
                "g": True,
                "h": True,
                "i": ["location"],
                "j": [],
                "n": round(random.uniform(800, 2000), 1),
                "u": "pay.openai.com",
                "v": "pay.openai.com",
                "w": f"{ts_now}:{hashlib.sha256(os.urandom(32)).hexdigest()}",
            },
            "h": os.urandom(10).hex(),
        }

    def _build_mouse(source: str) -> dict[str, Any]:
        return {
            "muid": muid,
            "sid": sid,
            "url": (
                f"https://{_b64url_seg()}.{_b64url_seg()}/"
                f"{_b64url_seg()}/{_b64url_seg()}/{_b64url_seg()}"
            ),
            "source": source,
            "data": [random.randint(1, 8) for _ in range(10)],
        }

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://m.stripe.network",
        "Referer": "https://m.stripe.network/",
    }
    url = "https://m.stripe.com/6"

    try:
        r1 = http.post(url, data=_encode_m6(_build_full(1, False)), headers=headers, timeout=timeout)
        if r1.status_code == 200:
            j = r1.json()
            muid = j.get("muid", muid)
            guid = j.get("guid", guid)
            sid = j.get("sid", sid)
    except Exception:  # noqa: BLE001
        pass

    try:
        r2 = http.post(url, data=_encode_m6(_build_full(2, True)), headers=headers, timeout=timeout)
        if r2.status_code == 200:
            j = r2.json()
            guid = j.get("guid", guid)
            muid = j.get("muid", muid)
            sid = j.get("sid", sid)
    except Exception:  # noqa: BLE001
        pass

    for source in ("mouse-timings-10-v2", "mouse-timings-10"):
        try:
            http.post(url, data=_encode_m6(_build_mouse(source)), headers=headers, timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

    return guid, muid, sid


def merge_runtime(user: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并用户 runtime；js/rv 指纹默认由 stripe_checksum 现算，此处只保留可选覆盖与 captcha。"""
    out: dict[str, Any] = {
        "passive_captcha_token": "",
        "passive_captcha_ekey": "",
    }
    if user:
        for k, v in user.items():
            if v is None:
                continue
            out[k] = v
    return out
