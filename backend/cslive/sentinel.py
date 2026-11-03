"""Sentinel PoW token 生成，对应 Go sentinel_generator.go + challenge_client.go。"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from .browser import BrowserProfile
from .constants import (
    CHATGPT_BASE, SENTINEL_FRAME_VERSION, SENTINEL_SDK_URL,
    SENTINEL_MAX_NONCE, SENTINEL_TIMEZONE_LABELS,
)


def _now_ms() -> float:
    return time.time() * 1000


def _local_time(zone: str):
    import zoneinfo
    tz = zoneinfo.ZoneInfo(zone)
    return datetime.now(tz)


def _sentinel_chrome_date(zone: str, t: datetime) -> str:
    labels = SENTINEL_TIMEZONE_LABELS.get(zone)
    if labels:
        label = labels[0]
        is_dst = bool(t.dst() and t.dst().total_seconds() != 0)
        if zone == "Europe/Dublin":
            is_dst = (t.utcoffset().total_seconds() if t.utcoffset() else 0) > 0
        if is_dst and labels[1]:
            label = labels[1]
    else:
        label = t.strftime("%Z")
        if len(label) <= 3:
            parts = zone.split("/")
            label = parts[-1].replace("_", " ") or "Coordinated Universal Time"
    offset_sec = int(t.utcoffset().total_seconds()) if t.utcoffset() else 0
    sign = "+" if offset_sec >= 0 else "-"
    minutes = abs(offset_sec) // 60
    return t.strftime(f"%a %b %d %Y %H:%M:%S") + f" GMT{sign}{minutes//60:02d}{minutes%60:02d} ({label})"


def _round_decimal(value: float, places: int) -> float:
    if math.isnan(value) or math.isinf(value):
        return value
    factor = 10 ** places
    return round(value * factor) / factor


def _python_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    # mimic Python's repr for floats
    s = repr(value)
    if "." not in s and "e" not in s:
        s += ".0"
    return s


def _sentinel_dump_json(value: Any, ascii_only: bool = False) -> str:
    """JSON 序列化，精确复刻 Go sentinel_dumpJSON 的行为。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _python_float(value)
    if isinstance(value, str):
        return _sentinel_quote_json(value, ascii_only)
    if isinstance(value, list):
        parts = [_sentinel_dump_json(v, ascii_only) for v in value]
        return "[" + ",".join(parts) + "]"
    if isinstance(value, dict):
        parts = [_sentinel_quote_json(k, ascii_only) + ":" + _sentinel_dump_json(v, ascii_only) for k, v in value.items()]
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _sentinel_quote_json(s: str, ascii_only: bool = False) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif ord(ch) < 32 or (ascii_only and ord(ch) > 127):
            code = ord(ch)
            if code <= 0xffff:
                out.append(f"\\u{code:04x}")
            else:
                # surrogate pair
                code -= 0x10000
                hi = 0xd800 + (code >> 10)
                lo = 0xdc00 + (code & 0x3ff)
                out.append(f"\\u{hi:04x}\\u{lo:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _fnv1a(s: str) -> str:
    h = 0x811c9dc5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xffffffff
    h ^= h >> 16
    h = (h * 0x85ebca6b) & 0xffffffff
    h ^= h >> 13
    h = (h * 0xc2b2ae35) & 0xffffffff
    h ^= h >> 16
    return f"{h:08x}"


def _encode_sentinel_json(value: Any) -> str:
    raw = _sentinel_dump_json(value)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


class SentinelGenerator:
    def __init__(self, device_id: str, profile: BrowserProfile, locale: str, timezone: str, clock_offset_hours: int = 0):
        self.device_id = device_id.strip()
        self.profile = profile
        self.locale = locale or "en-US"
        self.timezone = timezone or "UTC"
        self.clock_offset_hours = clock_offset_hours
        alphabet = string.ascii_lowercase + string.digits
        self.react_key = "__reactContainer$" + "".join(random.choices(alphabet, k=13))
        self.time_origin = self._now_ms() - (1200 + random.random() * 3600)
        self._start = time.time()

    def _now_ms(self) -> float:
        return (time.time() + self.clock_offset_hours * 3600) * 1000

    def _per_second(self) -> float:
        return time.time() - self._start

    def _config(self, nonce: int, elapsed_ms: float) -> list:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(self.timezone)
        local = datetime.now(tz)
        if self.clock_offset_hours != 0:
            from datetime import timedelta
            local = local + timedelta(hours=self.clock_offset_hours)
        primary = self.locale.replace("_", "-")
        base = primary.split("-")[0]
        languages = primary if primary.lower() == base.lower() else f"{primary},{base}"
        p = self.profile
        return [
            p.screen_width + p.screen_height,
            _sentinel_chrome_date(self.timezone, local),
            4294967296,
            nonce,
            p.user_agent,
            SENTINEL_SDK_URL,
            None,
            primary,
            languages,
            random.randint(2, 8),
            "scheduling-[object Scheduling]",
            self.react_key,
            "onscrollend",
            elapsed_ms,
            self.device_id,
            "",
            8,
            _round_decimal(self.time_origin, 1),
            0, 0, 0, 0, 0, 0, 0,
        ]

    def requirements_token(self) -> str:
        config = self._config(1, random.random() * 4900 + 100)
        encoded = _encode_sentinel_json(config)
        return "gAAAAAC" + encoded + "~S"

    def pow_token(self, seed: str, difficulty: str, deadline: float) -> str:
        start = self._per_second()
        difficulty = difficulty or "0"
        prefix = min(len(difficulty), 8)
        for nonce in range(SENTINEL_MAX_NONCE):
            if nonce % 64 == 0 and time.time() > deadline:
                raise RuntimeError("deadline_exceeded")
            config = self._config(nonce, (self._per_second() - start) * 1000)
            encoded = _encode_sentinel_json(config)
            digest = _fnv1a(seed + encoded)
            if digest.startswith(difficulty) or digest[:prefix] <= difficulty:
                if time.time() > deadline:
                    raise RuntimeError("deadline_exceeded")
                return "gAAAAAB" + encoded + "~S"
        raise RuntimeError("pow_exhausted")


def get_sentinel_token(
    http: requests.Session,
    flow: str,
    access_token: str,
    cookies: str,
    device_id: str,
    profile: BrowserProfile,
    timezone: str,
    deadline: float,
    clock_offset_hours: int = 0,
) -> tuple[str, str]:
    """完整 sentinel 流程：requirements → POST req → PoW → 返回 [2]string 令牌对。"""
    generator = SentinelGenerator(device_id, profile, "en-US", timezone, clock_offset_hours)
    requirements = generator.requirements_token()

    headers = {**profile.headers(),
        "Authorization": f"Bearer {access_token}",
        "Cookie": cookies,
        "oai-device-id": device_id,
        "Accept": "*/*",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": CHATGPT_BASE,
        "Referer": f"{CHATGPT_BASE}/backend-api/sentinel/frame.html?sv={SENTINEL_FRAME_VERSION}",
    }
    body = json.dumps({"p": requirements, "id": device_id, "flow": flow}, separators=(",", ":"))
    resp = http.post(f"{CHATGPT_BASE}/backend-api/sentinel/req", data=body.encode(), headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"sentinel_req HTTP {resp.status_code}")
    data = resp.json()

    challenge = data.get("token") or data.get("c")
    if not challenge:
        raise RuntimeError("sentinel_challenge_missing")

    pow_data = data.get("proofofwork") or data.get("proofOfWork") or data.get("proof_of_work") or {}
    turnstile = data.get("turnstile") or data.get("Turnstile") or {}

    proof = requirements
    if pow_data.get("required"):
        if not pow_data.get("seed"):
            raise RuntimeError("sentinel_pow_missing")
        proof = generator.pow_token(
            str(pow_data["seed"]),
            str(pow_data.get("difficulty") or "0"),
            deadline,
        )

    t = ""
    if turnstile.get("required"):
        if not turnstile.get("dx"):
            raise RuntimeError("sentinel_dx_missing")
        raise RuntimeError("sentinel_turnstile_required")  # no browser available

    if time.time() > deadline:
        raise RuntimeError("deadline_exceeded")

    payload = json.dumps({"p": proof, "t": t, "c": challenge, "id": device_id, "flow": flow}, separators=(",", ":"))

    # Telemetry
    import secrets as _sec
    telemetry_val = [1, round(random.randint(3800, 16000) / 10, 1), random.randint(3, 12), random.randint(80, 420), random.randint(18, 90), random.randint(1, 6), 0, random.randint(420, 1500)]
    timing = json.dumps(telemetry_val, separators=(",", ":"))

    return payload, timing
