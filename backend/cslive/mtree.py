"""Stripe MTree 设备指纹注册，对应 Go stripe_mtree.go + stripe_client.go (InitializeIdentity)。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
import time
import urllib.parse
from typing import Any

import requests

from .browser import BrowserProfile
from .constants import (
    MTREE_ORIGIN, MTREE_TAG_TEMPLATE, MTREE_URL_SALT,
    STRIPE_PLUGINS_WITH_MIME,
)


def _encode_uri_component(value: str) -> str:
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"
    out = []
    for b in value.encode("utf-8"):
        ch = chr(b)
        if ch in unreserved:
            out.append(ch)
        else:
            out.append(f"%{b:02X}")
    return "".join(out)


def _mtree_encode(payload: Any) -> str:
    """JSON → encodeURIComponent → base64，对应 Go stripe_mtreeEncode。"""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(_encode_uri_component(raw).encode("ascii")).decode("ascii")


def _random_nonce(length: int = 20) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def _sha256_with_salt(value: str) -> str:
    return hashlib.sha256((value + MTREE_URL_SALT).encode("utf-8")).hexdigest()


def _normalize_ms(ms: float) -> float:
    if ms < 0:
        return 0.0
    if ms >= 100:
        places = 1
    elif ms >= 10:
        places = 2
    elif ms >= 1:
        places = 3
    else:
        places = 4
    return round(ms, places)


def _format_pixel_ratio(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


def _profile_languages() -> list[str]:
    return ["en-US", "en"]


def _mtree_feature_values(profile: BrowserProfile) -> dict[str, str]:
    return {
        "b": "true",
        "c": ",".join(_profile_languages()),
        "d": profile.navigator_platform,
        "e": STRIPE_PLUGINS_WITH_MIME,
        "f": (f"{profile.avail_width}w_{profile.avail_height}h_"
              f"{profile.color_depth}d_{_format_pixel_ratio(profile.device_pixel_ratio)}r"),
        "g": str(profile.hardware_concurrency),
        "h": "false",
        "i": "sessionStorage-unavailable, localStorage-unavailable",
        "l": profile.user_agent,
    }


MTREE_FEATURE_KEYS = ["b", "c", "d", "e", "f", "g", "h", "i", "l"]


class MtreeTimings:
    def __init__(self):
        self._start = time.time()
        self._last = self._start

    def lap(self) -> float:
        now = time.time()
        elapsed = (now - self._last) * 1000
        self._last = now
        return _normalize_ms(elapsed)

    def total(self) -> float:
        return _normalize_ms((time.time() - self._start) * 1000)


def _build_device_feature(profile: BrowserProfile, timings: MtreeTimings) -> dict:
    values = _mtree_feature_values(profile)
    async_keys = {"l"}
    fields = {}
    for key in MTREE_FEATURE_KEYS:
        elapsed = timings.lap()
        feature = {"v": values[key], "t": elapsed}
        if key in async_keys:
            feature["at"] = elapsed
        fields[key] = feature
    return fields


def _build_device_context(muid: str = "", sid: str = "") -> dict:
    return {
        "a": "",
        "b": "",
        "d": muid,
        "e": sid,
        "f": False,
        "g": True,
        "h": True,
        "i": ["location"],
        "j": [],
        "n": 0,
        "u": "",
    }


def _mtree_headers(profile: BrowserProfile, user_agent: str) -> dict[str, str]:
    h = profile.headers()
    h.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": MTREE_ORIGIN,
        "Referer": MTREE_ORIGIN + "/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    })
    return h


def register_stripe_identity(
    http: requests.Session,
    profile: BrowserProfile,
    stripe_js_id: str,
    existing: dict[str, str] | None = None,
    timeout: int = 10,
) -> dict[str, str]:
    """向 m.stripe.com/6 注册设备指纹，对应 Go InitializeIdentity。"""
    identity: dict[str, str] = dict(existing or {})
    if identity.get("guid") and identity.get("muid") and identity.get("sid"):
        return identity

    muid = identity.get("muid", "")
    sid = identity.get("sid", "")
    url = "https://m.stripe.com/6"
    headers = _mtree_headers(profile, profile.user_agent)

    def _build_payload(include_ids: bool) -> str:
        timings = MtreeTimings()
        payload = {
            "v2": "1",
            "id": stripe_js_id,
            "t": timings.total(),
            "tag": MTREE_TAG_TEMPLATE,
            "src": "js",
            "a": _build_device_feature(profile, timings),
            "b": _build_device_context(muid if include_ids else "", sid if include_ids else ""),
            "h": _random_nonce(20),
        }
        return _mtree_encode(payload)

    # First registration (no ids)
    try:
        r1 = http.post(url, data=_build_payload(False), headers=headers, timeout=timeout)
        if r1.status_code == 200:
            j = r1.json()
            muid = j.get("muid", muid)
            identity["guid"] = j.get("guid", identity.get("guid", ""))
            identity["sid"] = j.get("sid", sid)
            sid = identity["sid"]
    except Exception:
        pass

    # Second registration (with ids)
    try:
        r2 = http.post(url, data=_build_payload(True), headers=headers, timeout=timeout)
        if r2.status_code == 200:
            j = r2.json()
            identity["guid"] = j.get("guid", identity.get("guid", ""))
            muid = j.get("muid", muid)
            sid = j.get("sid", sid)
            # cookies
            for name, val in r2.cookies.items():
                if name == "__stripe_mid":
                    identity["mid"] = val
                elif name == "__stripe_sid":
                    identity["sid_cookie"] = val
    except Exception:
        pass

    identity["muid"] = muid or identity.get("mid", "")
    identity["sid"] = sid or identity.get("sid_cookie", "")
    identity["guid"] = identity.get("guid") or identity.get("muid", "")
    identity["mid"] = identity.get("mid") or identity.get("muid", "")
    identity["sid_cookie"] = identity.get("sid_cookie") or identity.get("sid", "")

    if not identity.get("guid") or not identity.get("muid") or not identity.get("sid"):
        raise RuntimeError("stripe_identity_missing")
    return identity
