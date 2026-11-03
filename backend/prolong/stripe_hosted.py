"""Stripe Hosted Checkout（pay.openai.com）协议封装。"""

from __future__ import annotations

import uuid
from typing import Any

import requests
from .. import payment_trace as trace
from ..payment_transport import payment_session

from .card_parse import HAR_ADDRESS, HAR_EMAIL, extract_amount_due, mask_pan
from .fingerprint import USER_AGENT, local_fingerprint, merge_runtime, register_fingerprint

STRIPE_API = "https://api.stripe.com"
PK_FALLBACK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2020-08-27;custom_checkout_beta=v1"
DEFAULT_RUNTIME_VERSION = "af5d23bda9"

RUNTIME_REQUIRED_KEYS = ("version", "js_checksum", "rv_timestamp")


def new_fingerprint() -> tuple[str, str, str]:
    return local_fingerprint()


def validate_confirm_runtime(runtime: dict[str, Any]) -> None:
    missing = [k for k in RUNTIME_REQUIRED_KEYS if not str((runtime or {}).get(k) or "").strip()]
    if missing:
        raise ValueError("runtime 缺少 confirm 必填键: " + ", ".join(missing))


def build_payment_method_form(
    *,
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvc: str,
    name: str,
    email: str,
    pk: str,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
    cs: str,
    checkout_config_id: str,
    runtime_version: str,
    address: dict[str, str] | None = None,
) -> dict[str, str]:
    addr = address or HAR_ADDRESS
    ver = runtime_version or DEFAULT_RUNTIME_VERSION
    return {
        "type": "card",
        "card[number]": card_number,
        "card[exp_month]": exp_month,
        "card[exp_year]": exp_year,
        "card[cvc]": cvc,
        "billing_details[name]": name,
        "billing_details[email]": email,
        "billing_details[address][country]": addr["country"],
        "billing_details[address][state]": addr["state"],
        "billing_details[address][city]": addr["city"],
        "billing_details[address][line1]": addr["line1"],
        "billing_details[address][postal_code]": addr["postal_code"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
        "payment_user_agent": f"stripe.js/{ver}; stripe-js-v3/{ver}; checkout",
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
    }


def build_confirm_form(
    *,
    cs: str,
    pm_id: str,
    expected_amount: int,
    init_checksum: str,
    pk: str,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
    checkout_config_id: str,
    runtime: dict[str, Any],
) -> dict[str, str]:
    runtime = merge_runtime(runtime)
    validate_confirm_runtime(runtime)
    ver = str(runtime["version"]).strip()
    form = {
        "eid": "NA",
        "payment_method": pm_id,
        "expected_amount": str(int(expected_amount)),
        "expected_payment_method_type": "card",
        "_stripe_version": STRIPE_VERSION,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "version": ver,
        "init_checksum": init_checksum,
        "js_checksum": str(runtime["js_checksum"]).strip(),
        "rv_timestamp": str(runtime["rv_timestamp"]).strip(),
        "link_brand": "link",
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
    }
    captcha = str(runtime.get("passive_captcha_token") or "").strip()
    if captcha:
        form["passive_captcha_token"] = captcha
        form["passive_captcha_ekey"] = str(runtime.get("passive_captcha_ekey") or "")
    return form


class StripeHostedClient:
    def __init__(
        self,
        pk: str,
        proxy: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.pk = pk
        self.timeout = timeout
        self.session = payment_session(proxy)
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://pay.openai.com",
                "Referer": "https://pay.openai.com/",
            }
        )

    def register_device_fingerprint(self) -> tuple[str, str, str]:
        return register_fingerprint(self.session)

    def _post(self, path: str, data: dict[str, str], tag: str) -> dict[str, Any]:
        url = f"{STRIPE_API}{path}"
        kind = {'init': 'checkout_initialize', 'tax_region': 'billing_update',
                'payment_methods': 'payment_method_create', 'confirm': 'payment_confirm'}.get(tag, '')
        with trace.request(kind) as diagnostic:
            resp = self.session.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            diagnostic.response(resp.status_code, resp.headers, resp.text)
            if resp.status_code != 200:
                raise RuntimeError(f"{tag} 失败 [{resp.status_code}]")
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{tag} 返回非 JSON") from exc

    def init(self, cs: str) -> dict[str, Any]:
        data = {
            "key": self.pk,
            "eid": "NA",
            "browser_locale": "zh-CN",
            "browser_timezone": "Asia/Shanghai",
            "redirect_type": "url",
        }
        payload = self._post(f"/v1/payment_pages/{cs}/init", data, "init")
        if not payload.get("init_checksum"):
            raise RuntimeError("init 未返回 init_checksum")
        status = str(payload.get("status") or "")
        if status != "open":
            raise RuntimeError(f"checkout 状态不是 open（当前 {status!r}），请更换长链")
        return payload

    def update_tax_region(self, cs: str, address: dict[str, str] | None = None) -> dict[str, Any]:
        addr = address or HAR_ADDRESS
        data = {
            "eid": "NA",
            "tax_region[country]": addr["country"],
            "tax_region[state]": addr["state"],
            "tax_region[city]": addr["city"],
            "tax_region[line1]": addr["line1"],
            "tax_region[postal_code]": addr["postal_code"],
            "key": self.pk,
        }
        return self._post(f"/v1/payment_pages/{cs}", data, "tax_region")

    def create_payment_method(self, form: dict[str, str]) -> dict[str, Any]:
        payload = self._post("/v1/payment_methods", form, "payment_methods")
        if not str(payload.get("id") or "").startswith("pm_"):
            raise RuntimeError(f"payment_methods 未返回 pm_id: {str(payload)[:300]}")
        return payload

    def confirm(self, cs: str, form: dict[str, str]) -> dict[str, Any]:
        return self._post(f"/v1/payment_pages/{cs}/confirm", form, "confirm")


def resolve_email(init_resp: dict[str, Any]) -> str:
    email = str(init_resp.get("customer_email") or "").strip()
    return email or HAR_EMAIL


def resolve_checkout_config_id(init_resp: dict[str, Any]) -> str:
    for key in ("config_id", "checkout_config_id"):
        val = str(init_resp.get(key) or "").strip()
        if val:
            return val
    return str(uuid.uuid4())


def resolve_expected_amount(tax_resp: dict[str, Any], init_resp: dict[str, Any] | None = None) -> int:
    amount = extract_amount_due(tax_resp)
    if amount is None and init_resp is not None:
        amount = extract_amount_due(init_resp)
    if amount is None or amount <= 0:
        raise RuntimeError("无法从 tax/init 响应得到有效 expected_amount")
    return amount
