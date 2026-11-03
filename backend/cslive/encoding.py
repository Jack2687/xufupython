"""Stripe 校验和编码，对应 Go stripe_encoding.go / stripe_build.go。"""
from __future__ import annotations
import base64
import json
import re
import urllib.parse
from typing import Any


def _encode_uri_component(value: str) -> str:
    """ECMAScript encodeURIComponent：不转义 A-Za-z0-9 - _ . ! ~ * ' ( )"""
    unreserved = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        "-_.!~*'()"
    )
    out = []
    for c in value.encode("utf-8"):
        ch = chr(c)
        if ch in unreserved:
            out.append(ch)
        else:
            out.append(f"%{c:02X}")
    return "".join(out)


def encode_stripe_blob(obj: Any) -> str:
    """对应 Go stripe_EncodeStripeBlob / Python encode_stripe_blob。
    JSON → 填充到 len%3==0 → XOR 5 → base64 → URL encode → Caesar +11。
    """
    plain = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    padded = plain + " " * (3 - len(plain) % 3)
    xored = bytes(b ^ 5 for b in padded.encode("latin-1"))
    b64 = base64.b64encode(xored).decode("ascii")
    url_encoded = _encode_uri_component(b64)
    result = []
    for ch in url_encoded:
        c = ord(ch)
        result.append(chr((c - 32 + 11) % 95 + 32))
    return "".join(result)


def compute_js_checksum(payment_method_id: str) -> str:
    pm = (payment_method_id or "").strip()
    if not pm.startswith("pm_"):
        raise ValueError(f"invalid payment_method_id: {payment_method_id!r}")
    return encode_stripe_blob({"id": pm})


def compute_id_checksum(id_: str) -> str:
    value = (id_ or "").strip()
    if not value:
        raise ValueError("id cannot be empty")
    return encode_stripe_blob({"id": value})


def compute_rv_timestamp(rv_ts: str, rv: str, sv: str) -> str:
    if not rv_ts or not rv or not sv:
        raise ValueError("rv_ts/rv/sv cannot be empty")
    return encode_stripe_blob({"rvTs": rv_ts, "rv": rv, "sv": sv})


def parse_stripe_build_constants(source: str) -> dict[str, str]:
    """从 stripe.js/basil 源码提取 version/rv/sv/rv_ts。"""
    def find(pattern: str) -> str:
        m = re.search(pattern, source)
        return m.group(1) if m else ""

    # version (10 hex) and rv (40 hex) together
    pair = re.search(
        r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{10})"\s*,\s*o\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{40})"',
        source,
    )
    if pair:
        version, rv = pair.group(1), pair.group(2)
    else:
        version = find(r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{10})"')
        rv = find(r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{40})"')

    if not version or not rv:
        raise RuntimeError("stripe_build_missing: cannot parse BUILD_SALT hashes")

    rv_ts = find(r'rvTs["\']?\s*:\s*["\']([^"\']+)["\']')
    if not rv_ts:
        rv_ts = find(r'"(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"')
    if not rv_ts:
        rv_ts = find(r'r="(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"')

    sv = find(r'sv:\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{64})"')
    if not sv:
        sv = find(re.escape(rv) + r'"\s*,\s*a\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{64})"')
    if not sv:
        sv = find(r'STRIPE_JS_BUILD_SALT[^"]*"([a-f0-9]{64})"')

    if not rv_ts or not sv:
        raise RuntimeError("stripe_build_missing: cannot parse rv_ts or sv")

    return {"version": version, "rv": rv, "sv": sv, "rv_ts": rv_ts}


def parse_stripe_js_version(source: str) -> str:
    """从 stripe.js 提取短版本标识（10 hex），对应 Go stripe_ParseStripeJSVersion。"""
    markers = list(re.finditer(r'/\*!\s*STRIPE_JS_BUILD_SALT\s+([a-f0-9]{10})\s*\*/', source))
    version = ""
    salt = ""
    for marker in markers:
        value = marker.group(1)
        if salt and salt != value:
            raise RuntimeError("stripe_build_missing")
        salt = value
        rest = source[marker.end():]
        lit = re.match(r'\s*(?:"([a-f0-9]{10})"|\'([a-f0-9]{10})\')', rest)
        if lit:
            candidate = lit.group(1) or lit.group(2)
            if version and candidate != version:
                raise RuntimeError("stripe_build_missing")
            version = candidate
    if not version or version == "0000000000" or not re.fullmatch(r'[a-f0-9]{10}', version):
        raise RuntimeError("stripe_build_missing")
    return version
