"""卡串 / 长链解析、随机姓名、金额提取（cslive_go_direct_pay 流程专属副本）。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

HAR_EMAIL = "heb879127@gmail.com"

HAR_ADDRESS = {
    "country": "US",
    "state": "NC",
    "city": "Cary",
    "line1": "302 Colonades Way",
    "postal_code": "27518",
}

_CS_RE = re.compile(r"^cs_(live|test)_[A-Za-z0-9]+$")
_CS_SEARCH_RE = re.compile(r"cs_(live|test)_[A-Za-z0-9]+")

_FIRST = (
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael",
    "Linda", "William", "Elizabeth", "David", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Ada", "Grace",
)
_LAST = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Wilson",
    "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
)


@dataclass(frozen=True)
class CardData:
    number: str
    exp_month: str
    exp_year: str
    cvc: str


def parse_card(raw: str) -> CardData:
    text = (raw or "").strip()
    parts = text.split("----")
    if len(parts) != 3:
        raise ValueError("卡串格式必须为: 卡号----MMYY----CVC")

    number, mmyy, cvc = (p.strip() for p in parts)
    if not number.isdigit() or len(number) < 12:
        raise ValueError("卡号无效")
    if not mmyy.isdigit() or len(mmyy) != 4:
        raise ValueError("有效期必须是 4 位 MMYY")
    month = mmyy[:2]
    year = mmyy[2:]
    if not (1 <= int(month) <= 12):
        raise ValueError(f"月份非法: {month}")
    if not cvc.isdigit() or not (3 <= len(cvc) <= 4):
        raise ValueError("CVC 无效")
    return CardData(number=number, exp_month=month, exp_year=year, cvc=cvc)


def parse_checkout_session(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("长链 / cs 不能为空")
    if _CS_RE.match(text):
        return text

    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        m = _CS_SEARCH_RE.search(parsed.path or "")
        if m:
            return m.group(0)
        m = _CS_SEARCH_RE.search(text)
        if m:
            return m.group(0)
        raise ValueError("URL 中未找到 cs_live_ / cs_test_ session id")

    m = _CS_SEARCH_RE.search(text)
    if m and _CS_RE.match(m.group(0)):
        return m.group(0)
    raise ValueError("无法解析长链或 cs session id")


def random_cardholder_name() -> str:
    return f"{random.choice(_FIRST)} {random.choice(_LAST)}"


def extract_amount_due(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    summary = payload.get("total_summary") or {}
    if summary.get("due") is not None:
        try:
            return int(summary["due"])
        except (TypeError, ValueError):
            pass
    invoice = payload.get("invoice") or {}
    if invoice.get("amount_due") is not None:
        try:
            return int(invoice["amount_due"])
        except (TypeError, ValueError):
            pass
    return None


def mask_pan(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 8:
        return "****"
    return f"{digits[:4]}****{digits[-4:]}"
