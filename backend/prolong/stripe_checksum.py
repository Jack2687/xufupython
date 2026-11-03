"""Stripe checkout js_checksum / rv_timestamp 现算。

算法（从 stripe.js / checkout-app-init 还原，已用支付 HAR 校验）：
1. JSON.stringify(obj)（无空格）
2. 空格填充使 len % 3 == 0（缺几补几；整除时补 3）
3. 逐字节 XOR 5
4. btoa → encodeURIComponent
5. 可打印 ASCII Caesar +11： (code-32+11)%95+32
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests

STRIPE_JS_URL = "https://js.stripe.com/v3/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def _xor5(s: str) -> str:
    return "".join(chr(5 ^ ord(c)) for c in s)


def _dp_l(plain: str) -> str:
    n = 3 - (len(plain) % 3)
    padded = plain + (" " * n)
    b64 = base64.b64encode(_xor5(padded).encode("latin1")).decode("ascii")
    return urllib.parse.quote(b64, safe="")


def _bp(s: str, shift: int = 11) -> str:
    return "".join(chr((ord(c) - 32 + shift) % 95 + 32) for c in s)


def encode_stripe_blob(obj: dict[str, Any]) -> str:
    plain = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    return _bp(_dp_l(plain), 11)


def compute_js_checksum(payment_method_id: str) -> str:
    """js_checksum = encode({id: pm_xxx})，每次随 pm 变化。"""
    pm = (payment_method_id or "").strip()
    if not pm.startswith("pm_"):
        raise ValueError(f"payment_method_id 无效: {payment_method_id!r}")
    return encode_stripe_blob({"id": pm})


def compute_rv_timestamp(*, rv_ts: str, rv: str, sv: str) -> str:
    """rv_timestamp = encode({rvTs, rv, sv})，随 stripe.js 构建变化。"""
    if not rv_ts or not rv or not sv:
        raise ValueError("rv_ts/rv/sv 不能为空")
    return encode_stripe_blob({"rvTs": rv_ts, "rv": rv, "sv": sv})


@dataclass(frozen=True)
class StripeBuildConstants:
    version: str  # short, e.g. af5d23bda9
    rv: str       # long build hash
    sv: str       # secondary hash
    rv_ts: str    # build timestamp string baked into JS


_BUILD_CACHE: StripeBuildConstants | None = None


def parse_stripe_build_constants(js_source: str) -> StripeBuildConstants:
    """从 stripe.js 源码提取 version / rv / sv / rvTs。"""
    text = js_source or ""

    m = re.search(
        r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{10})"\s*,\s*o\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{40})"',
        text,
    )
    if not m:
        m = re.search(
            r'/\*!?\s*STRIPE_JS_BUILD_SALT[^/]*/\s*"([a-f0-9]{10})"\s*,\s*o\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^/]*/\s*"([a-f0-9]{40})"',
            text,
        )
    if not m:
        shorts = re.findall(r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{10})"', text)
        longs = re.findall(r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{40})"', text)
        if not shorts or not longs:
            raise RuntimeError("无法从 stripe.js 解析 BUILD_SALT 版本哈希")
        version, rv = shorts[0], longs[0]
    else:
        version, rv = m.group(1), m.group(2)

    m_ts = re.search(r'rvTs["\']?\s*:\s*["\']([^"\']+)["\']', text)
    if not m_ts:
        m_ts = re.search(r'rvTs:"([^"]+)"', text)
    m_date = re.search(r'"(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"', text)
    rv_ts = (m_ts.group(1) if m_ts else None) or (m_date.group(1) if m_date else None)
    if not rv_ts:
        m_date2 = re.search(r'r="(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"', text)
        rv_ts = m_date2.group(1) if m_date2 else None
    if not rv_ts:
        raise RuntimeError("无法从 stripe.js 解析 rvTs")

    m_sv = re.search(
        r'sv:\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{64})"',
        text,
    )
    if not m_sv:
        m_sv = re.search(
            rf'{re.escape(rv)}"\s*,\s*a\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{{64}})"',
            text,
        )
    if not m_sv:
        m_sv = re.search(
            r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{64})"',
            text,
        )
    sv = m_sv.group(1) if m_sv else None
    if not sv:
        raise RuntimeError("无法从 stripe.js 解析 sv")

    return StripeBuildConstants(version=version, rv=rv, sv=sv, rv_ts=rv_ts)


def fetch_stripe_build_constants(
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
    use_cache: bool = True,
) -> StripeBuildConstants:
    global _BUILD_CACHE
    if use_cache and _BUILD_CACHE is not None:
        return _BUILD_CACHE

    http = session or requests.Session()
    resp = http.get(
        STRIPE_JS_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"下载 stripe.js 失败 [{resp.status_code}]")

    consts = parse_stripe_build_constants(resp.text)

    if use_cache:
        _BUILD_CACHE = consts
    return consts


def build_runtime_checksums(
    payment_method_id: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """返回 confirm 所需 version / js_checksum / rv_timestamp。"""
    consts = fetch_stripe_build_constants(session=session)
    return {
        "version": consts.version,
        "js_checksum": compute_js_checksum(payment_method_id),
        "rv_timestamp": compute_rv_timestamp(rv_ts=consts.rv_ts, rv=consts.rv, sv=consts.sv),
        "rv": consts.rv,
        "sv": consts.sv,
        "rv_ts": consts.rv_ts,
    }
