"""Stripe 客户端，对应 Go stripe_client.go + stripe_hosted.go + stripe_hosted_fingerprint.go + stripe_encoding.go。"""
from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from typing import Any

import requests

from .browser import BrowserProfile
from .constants import (
    STRIPE_BASE, STRIPE_CUSTOM_API_VERSION, STRIPE_CUSTOM_CHECKOUT_API_VERSION,
    STRIPE_CUSTOM_JS_URL, STRIPE_HOSTED_VERSION,
)
from .encoding import (
    parse_stripe_build_constants, parse_stripe_js_version,
    compute_js_checksum, compute_id_checksum, compute_rv_timestamp,
)
from .mtree import register_stripe_identity


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _as_map(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _first_truthy(*args: Any) -> Any:
    for a in args:
        if a:
            return a
    return None


def _random_uuid() -> str:
    return str(uuid.uuid4())


def _random_elements_id() -> str:
    import random, string
    alpha = string.ascii_letters + string.digits
    return "elements_session_" + "".join(random.choices(alpha, k=11))


class StripeClient:
    def __init__(self, http: requests.Session, cp: dict, profile: BrowserProfile, clock_offset_hours: int = 0):
        self.http = http
        self.cp = cp
        self.profile = profile
        self.clock_offset_hours = clock_offset_hours
        self.client_id = _random_uuid()
        self.elements_id = _random_elements_id()
        self.config_id = _random_uuid()
        self.js_version = ""
        self.hosted_url = ""
        self.hosted_eid = ""
        self.build: dict[str, str] = {}
        self._started = time.time()
        self._hosted_snapshot: list[dict] | None = None
        self._extracted_hosted_init: dict | None = None
        self._confirm_submitted = False
        if not cp.get("stripe_identity"):
            cp["stripe_identity"] = {}

    def time_on_page(self) -> str:
        elapsed = int((time.time() - self._started) * 1000)
        if elapsed < 8000:
            elapsed = 8000
        return str(elapsed)

    def ids(self) -> dict[str, str]:
        identity = self.cp.get("stripe_identity", {})
        return {"guid": identity.get("guid", ""), "muid": identity.get("muid", ""), "sid": identity.get("sid", "")}

    def _stripe_headers(self, hosted: bool = False) -> dict[str, str]:
        identity = self.cp.get("stripe_identity", {})
        origin = "https://js.stripe.com"
        referer = origin + "/"
        if hosted:
            parsed = urllib.parse.urlparse(self.hosted_url or "")
            if parsed.hostname:
                origin = f"https://{parsed.hostname}"
            referer = (self.hosted_url or "").split("#")[0]
        h = dict(self.profile.headers())
        h.update({
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
        })
        if not hosted or origin == "https://checkout.stripe.com":
            parts = []
            mid = identity.get("mid") or identity.get("muid", "")
            sid_cookie = identity.get("sid_cookie") or identity.get("sid", "")
            if mid:
                parts.append(f"__stripe_mid={mid}")
            if sid_cookie:
                parts.append(f"__stripe_sid={sid_cookie}")
            if parts:
                h["Cookie"] = "; ".join(parts)
        if hosted:
            site = "same-site" if origin == "https://checkout.stripe.com" else "cross-site"
            h.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": site})
        return h

    def _call(self, method: str, path: str, form: dict | None = None, hosted: bool = False, allow_error: bool = False, extra_headers: dict | None = None) -> dict:
        url = STRIPE_BASE + path
        h = extra_headers or self._stripe_headers(hosted)
        if method == "GET":
            resp = self.http.get(url, headers=h, timeout=60)
        else:
            resp = self.http.post(url, data=form or {}, headers={**h, "Content-Type": "application/x-www-form-urlencoded"}, timeout=60)
        if not allow_error and resp.status_code != 200:
            raise RuntimeError(f"stripe {method} {path} HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def initialize_build(self):
        resp = self.http.get(STRIPE_CUSTOM_JS_URL, headers=self.profile.headers(), timeout=60)
        if resp.status_code != 200:
            raise RuntimeError("stripe_build_missing")
        source = resp.text
        self.js_version = parse_stripe_js_version(source)
        self.cp["stripe_version"] = STRIPE_CUSTOM_API_VERSION
        try:
            self.build = parse_stripe_build_constants(source)
        except Exception:
            pass

    def initialize_identity(self):
        self.cp["stripe_identity"] = register_stripe_identity(
            self.http, self.profile, self.client_id, self.cp.get("stripe_identity"), timeout=15
        )

    def initialize(self):
        self.initialize_build()
        self.initialize_identity()

    def _hosted_page_path(self) -> str:
        q = urllib.parse.urlencode({"eid": self.hosted_eid or "NA", "key": self.cp.get("publishable_key", "")})
        return f"/payment_pages/{self.cp['checkout_id']}?{q}"

    def page(self) -> dict:
        return self._call("GET", self._hosted_page_path(), hosted=True, allow_error=True)

    def custom_page(self) -> dict:
        pk = self.cp.get("publishable_key", "")
        sv = self.cp.get("stripe_version", STRIPE_CUSTOM_CHECKOUT_API_VERSION)
        path = f"/payment_pages/{self.cp['checkout_id']}?key={urllib.parse.quote(pk)}&_stripe_version={urllib.parse.quote(sv)}&expand%5B%5D=payment_intent"
        return self._call("GET", path, allow_error=True)

    def hosted_prepare(self, raw_url: str, billing: dict) -> dict:
        cp = self.cp
        init, err = self._initialize_hosted(raw_url)
        if err:
            raise RuntimeError(err)
        resp = self.http.get((self.hosted_url or "").split("#")[0], headers=self.profile.headers(), timeout=60)
        if resp.status_code != 200:
            raise RuntimeError("hosted_page_unavailable")
        self._started = time.time()
        self._load_hosted_scripts(resp.text)
        self._hosted_fingerprint_warmup()
        tax = self._call("POST", f"/payment_pages/{cp['checkout_id']}", form={
            "eid": _as_str(_first_truthy(init.get("eid"), "NA")),
            **_tax_region_fields(billing),
            "key": cp.get("publishable_key", ""),
        }, hosted=True)
        if tax.get("eid"):
            self.hosted_eid = tax["eid"]
        page = self.page()
        self._hosted_snapshot = [init, tax, page]
        checksum = _as_str(init.get("init_checksum"))
        def scan(node):
            nonlocal checksum
            if isinstance(node, dict):
                for k, v in node.items():
                    if "checksum" in k.lower() and isinstance(v, str) and v and len(v) > len(checksum):
                        checksum = v
                    else:
                        scan(v)
            elif isinstance(node, list):
                for v in node:
                    scan(v)
        scan(init)
        tax_checksum = _as_str(tax.get("init_checksum"))
        if len(tax_checksum) > len(checksum) and len(tax_checksum) > 40:
            checksum = tax_checksum
        if not checksum:
            raise RuntimeError("init_checksum_missing")
        total = None
        for data in [tax, init, page]:
            v = _hosted_amount(data)
            if v is not None:
                total = v
                break
        group = _first_line_item_group(tax, init, page)
        return {
            "amount": total,
            "currency": _first_truthy(tax.get("currency"), init.get("currency"), page.get("currency"), cp.get("currency")),
            "checksum": checksum,
            "eid": _first_truthy(tax.get("eid"), init.get("eid"), "NA"),
            "config_id": _as_str(_first_truthy(tax.get("config_id"), init.get("config_id"), page.get("config_id"), "")),
            "line_item_group": group,
        }

    def _initialize_hosted(self, raw: str) -> tuple[dict, str | None]:
        if not raw:
            self.hosted_url = f"https://checkout.stripe.com/c/pay/{self.cp['checkout_id']}"
        else:
            self.hosted_url = raw
        if raw:
            err = self._validate_hosted_url(self.hosted_url, complete=True)
        else:
            err = self._validate_hosted_url(self.hosted_url, complete=False)
        if err:
            return {}, err
        init = self._call("POST", f"/payment_pages/{self.cp['checkout_id']}/init", form=self._hosted_init_form(), hosted=True)
        if init.get("eid"):
            self.hosted_eid = init["eid"]
        if not raw:
            found = _find_hosted_url(init, self.cp["checkout_id"])
            if not found:
                return {}, "hosted_url_missing"
            self.hosted_url = found
        self._hosted_snapshot = [init]
        return init, None

    def _validate_hosted_url(self, raw: str, complete: bool = True) -> str | None:
        try:
            p = urllib.parse.urlparse(raw)
        except Exception:
            return "hosted_url_invalid"
        if p.scheme != "https" or p.netloc not in ("checkout.stripe.com", "pay.openai.com"):
            return "hosted_url_invalid"
        if p.path != f"/c/pay/{self.cp.get('checkout_id', '')}":
            return "hosted_url_invalid"
        if complete and (len(p.fragment) < 250 or p.query):
            return "hosted_url_invalid"
        return None

    def _hosted_init_form(self) -> dict:
        form = {
            "browser_locale": "en-US",
            "browser_timezone": self.cp.get("timezone", "UTC"),
            "key": self.cp.get("publishable_key", ""),
            "eid": "NA",
            "redirect_type": "url",
        }
        if self.hosted_url:
            form["referrer"] = self.hosted_url
        return form

    def _load_hosted_scripts(self, html: str):
        script_urls = re.findall(
            r'https://js\.stripe\.com/v3/fingerprinted/js/(?:checkout-app(?:-init)?|stripe)-[A-Za-z0-9]+\.js[^"\'\\s]*',
            html,
        ) + re.findall(
            r'https://js\.stripe\.com/v3/stripe\.js\?stripeCheckoutInitialized=true[^"\'\\s]*',
            html,
        )
        build_source = ""
        for raw_url in script_urls:
            resp = self.http.get(raw_url, headers=self.profile.headers(), timeout=30)
            if resp.status_code != 200:
                raise RuntimeError("stripe_build_missing")
            if "/stripe-" in raw_url or "stripeCheckoutInitialized=true" in raw_url:
                build_source = resp.text
        if not build_source:
            resp = self.http.get("https://js.stripe.com/v3", headers=self.profile.headers(), timeout=30)
            if resp.status_code != 200:
                raise RuntimeError("stripe_build_missing")
            build_source = resp.text
        try:
            self.build = parse_stripe_build_constants(build_source)
            self.js_version = self.build.get("version", "")
        except Exception:
            raise RuntimeError("stripe_build_missing")

    def _hosted_fingerprint_warmup(self):
        try:
            self.initialize_identity()
        except Exception:
            pass
        payload = '{"type":"measurement","event_name":"paymentElement.mount","metrics":{"duration":' + self.time_on_page() + '},"client_session_id":"' + self.client_id + '","publish_key":"' + self.cp.get("publishable_key", "") + '"}'
        h = dict(self.profile.headers())
        h.update({"Accept": "*/*", "Content-Type": "text/plain", "Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/"})
        try:
            self.http.post("https://r.stripe.com/b", data=payload.encode(), headers=h, timeout=10)
        except Exception:
            pass

    def hosted_form(self, card: dict, billing: dict, info: dict) -> dict:
        cp = self.cp
        attribution = {
            "client_session_id": self.client_id,
            "checkout_session_id": cp["checkout_id"],
            "merchant_integration_source": "checkout",
            "merchant_integration_version": "hosted_checkout",
            "payment_method_selection_flow": "automatic",
            "elements_session_id": self.elements_id,
        }
        if info.get("config_id"):
            attribution["checkout_config_id"] = info["config_id"]
        form: dict = {
            "type": "card",
            "card[number]": card["number"],
            "card[cvc]": card["cvc"],
            "card[exp_month]": card["exp_month"],
            "card[exp_year]": card["exp_year"],
            "billing_details[name]": billing.get("name", ""),
            "billing_details[email]": _first_truthy(billing.get("email"), cp.get("email")) or "",
            **{f"billing_details[address][{k}]": v for k, v in _billing_address_fields(billing).items()},
            "payment_user_agent": f"stripe.js/{self.build.get('version', '')}; stripe-js-v3/{self.build.get('version', '')}; checkout",
            "referrer": (self.hosted_url or "").split("#")[0],
            "time_on_page": self.time_on_page(),
            "key": cp.get("publishable_key", ""),
            "_stripe_version": STRIPE_HOSTED_VERSION,
            **self.ids(),
        }
        form["client_attribution_metadata[client_session_id]"] = self.client_id
        form["client_attribution_metadata[checkout_session_id]"] = cp["checkout_id"]
        form["client_attribution_metadata[merchant_integration_source]"] = "checkout"
        form["client_attribution_metadata[merchant_integration_version]"] = "hosted_checkout"
        form["client_attribution_metadata[payment_method_selection_flow]"] = "automatic"
        form["client_attribution_metadata[elements_session_id]"] = self.elements_id
        if info.get("config_id"):
            form["client_attribution_metadata[checkout_config_id]"] = info["config_id"]
        # card metadata lookup
        self._lookup_card_metadata(card)
        # create PM
        pm_resp = self._call("POST", "/payment_methods", form=form, hosted=True)
        pm_id = _as_str(pm_resp.get("id"))
        if not pm_id.startswith("pm_"):
            raise RuntimeError("payment_method_missing")
        js_checksum = compute_js_checksum(pm_id)
        rv_ts = compute_rv_timestamp(self.build.get("rv_ts", ""), self.build.get("rv", ""), self.build.get("sv", ""))
        expected: Any = None
        if cp.get("amount") is not None:
            expected = cp["amount"]
        result: dict = {
            "eid": _first_truthy(info.get("eid"), self.hosted_eid, "NA"),
            "payment_method": pm_id,
            "expected_amount": expected,
            "expected_payment_method_type": "card",
            "link_brand": "link",
            "key": cp.get("publishable_key", ""),
            "_stripe_version": STRIPE_HOSTED_VERSION,
            "js_checksum": js_checksum,
            "rv_timestamp": rv_ts,
            **self.ids(),
        }
        result["client_attribution_metadata[client_session_id]"] = self.client_id
        result["client_attribution_metadata[checkout_session_id]"] = cp["checkout_id"]
        result["client_attribution_metadata[merchant_integration_source]"] = "checkout"
        result["client_attribution_metadata[merchant_integration_version]"] = "hosted_checkout"
        result["client_attribution_metadata[payment_method_selection_flow]"] = "automatic"
        result["client_attribution_metadata[elements_session_id]"] = self.elements_id
        if info.get("config_id"):
            result["client_attribution_metadata[checkout_config_id]"] = info["config_id"]
        if self.hosted_url:
            result["referrer"] = self.hosted_url
        # last_displayed_line_item_group_details
        source = dict(info)
        if not _as_map(source.get("line_item_group")):
            for snap in reversed(self._hosted_snapshot or []):
                if _as_map(snap.get("line_item_group")):
                    source["line_item_group"] = snap["line_item_group"]
                    break
        details = _last_displayed_line_item_group(source)
        if details:
            result["last_displayed_line_item_group_details"] = details
        result["return_url"] = (self.hosted_url or "").split("#")[0]
        result["consent[terms_of_service]"] = "accepted"
        return result

    def confirm_hosted(self, form: dict) -> dict:
        if self._confirm_submitted:
            raise RuntimeError("payment_confirmation_already_submitted")
        self._confirm_submitted = True
        cp = self.cp
        h = self._stripe_headers(hosted=True)
        h["Idempotency-Key"] = f"checkout-confirm-{cp['checkout_id']}"
        return self._call("POST", f"/payment_pages/{cp['checkout_id']}/confirm", form=form, hosted=True, allow_error=True, extra_headers=h)

    def confirm_payment_intent(self, form: dict) -> dict:
        if self._confirm_submitted:
            raise RuntimeError("payment_confirmation_already_submitted")
        self._confirm_submitted = True
        cp = self.cp
        h = self._stripe_headers()
        h["Idempotency-Key"] = f"pi-confirm-{cp.get('payment_intent_id', '')}"
        return self._call("POST", f"/payment_intents/{cp['payment_intent_id']}/confirm", form=form, allow_error=True, extra_headers=h)

    def retrieve(self) -> dict:
        cp = self.cp
        if cp.get("payment_intent_id") and cp.get("client_secret"):
            path = (f"/payment_intents/{cp['payment_intent_id']}"
                    f"?client_secret={urllib.parse.quote(cp['client_secret'])}"
                    f"&key={urllib.parse.quote(cp.get('publishable_key', ''))}"
                    f"&_stripe_version={urllib.parse.quote(cp.get('stripe_version', ''))}")
            return self._call("GET", path, allow_error=True)
        if cp.get("checkout_id", "").startswith("cs_live_") and cp.get("metadata", {}).get("checkout_ui_mode") == "custom":
            return self.custom_page()
        return self.page()

    def _lookup_card_metadata(self, card: dict):
        number = card.get("number", "")
        if len(number) < 6 or not number[:6].isdigit():
            return
        try:
            q = urllib.parse.urlencode({"bin_prefix": number[:6], "key": self.cp.get("publishable_key", "")})
            self.http.get(f"https://api.stripe.com/edge-internal/card-metadata?{q}", headers=self._stripe_headers(True), timeout=10)
        except Exception:
            pass

    def extract_hosted_url(self, checkout: dict) -> str:
        found = _find_hosted_url(checkout, self.cp["checkout_id"])
        if found:
            self.hosted_url = found
            return found
        if not self.cp.get("checkout_id", "").startswith("cs_live_"):
            raise RuntimeError("checkout_id_invalid")
        if not self.cp.get("publishable_key", "").startswith("pk_"):
            raise RuntimeError("publishable_key_missing")
        self.hosted_url = f"https://checkout.stripe.com/c/pay/{self.cp['checkout_id']}"
        err = self._validate_hosted_url(self.hosted_url, complete=False)
        if err:
            raise RuntimeError(err)
        init = self._call("POST", f"/payment_pages/{self.cp['checkout_id']}/init", form=self._hosted_init_form(), hosted=True)
        if init.get("eid"):
            self.hosted_eid = init["eid"]
        raw = _find_hosted_url(init, self.cp["checkout_id"])
        if not raw:
            raise RuntimeError("hosted_url_missing")
        self.hosted_url = raw
        self._extracted_hosted_init = init
        return raw

    def custom_price_snapshot(self, checkout: dict) -> dict:
        key = _as_str(_first_truthy(checkout.get("publishable_key"), self.cp.get("publishable_key")))
        if not key.startswith("pk_"):
            raise RuntimeError("publishable_key_missing")
        return self._call("POST", f"/payment_pages/{self.cp['checkout_id']}/init", form={
            "key": key,
            "_stripe_version": self.cp.get("stripe_version", STRIPE_CUSTOM_API_VERSION),
            "browser_locale": "en-US",
            "browser_timezone": self.cp.get("timezone", "UTC"),
            "elements_session_client[stripe_js_id]": self.client_id,
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[locale]": "en-US",
            "elements_options_client[saved_payment_method][enable_save]": "auto",
            "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        })

    def bind_checkout_elements(self):
        cp = self.cp
        params: dict = {
            "key": cp.get("publishable_key", ""),
            "_stripe_version": cp.get("stripe_version", STRIPE_CUSTOM_API_VERSION),
            "type": "deferred_intent",
            "checkout_session_id": cp["checkout_id"],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "locale": "en-US",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
        }
        if cp.get("currency"):
            cur = cp["currency"].lower()
            params["currency"] = cur
            params["deferred_intent[currency]"] = cur
        if cp.get("amount") is not None:
            params["deferred_intent[amount]"] = str(cp["amount"])
        q = urllib.parse.urlencode(params)
        data = self._call("GET", f"/elements/sessions?{q}")
        id_ = _as_str(_first_truthy(data.get("elements_session_id"), _as_map(data.get("elements_session")).get("id"), data.get("id")))
        if id_.startswith("elements_session_"):
            self.elements_id = id_
        config_id = _as_str(_first_truthy(data.get("config_id"), _as_map(data.get("elements_session")).get("config_id")))
        if config_id:
            self.config_id = config_id

    def token(self, card: dict, billing: dict, checkout: dict) -> str:
        if not self.js_version:
            raise RuntimeError("stripe_build_missing")
        cp = self.cp
        m = {
            "client_session_id": self.client_id,
            "merchant_integration_source": "elements",
            "merchant_integration_subtype": "payment-element",
            "merchant_integration_version": "2021",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": "merchant_specified",
            "elements_session_id": self.elements_id,
            "elements_session_config_id": self.config_id,
            "merchant_integration_additional_elements": ["expressCheckout", "payment", "address"],
        }
        pm_form: dict = {
            "type": "card",
            "card[number]": card["number"],
            "card[cvc]": card["cvc"],
            "card[exp_month]": card["exp_month"],
            "card[exp_year]": card["exp_year"],
            "allow_redisplay": "limited",
            "billing_details[name]": billing.get("name", ""),
            "billing_details[phone]": "",
            **{f"billing_details[address][{k}]": v for k, v in _billing_address_fields(billing).items()},
            "payment_user_agent": f"stripe.js/{self.js_version}; stripe-js-v3/{self.js_version}; payment-element; deferred-intent",
            "referrer": "https://chatgpt.com",
            "time_on_page": self.time_on_page(),
            **self.ids(),
        }
        context: dict = {}
        if checkout.get("customer_session_client_secret"):
            context = {"currency": cp.get("currency", "").lower(), "mode": "subscription", "payment_method_types": ["card", "link"]}
        for key in ("stripe_customer_id", "customer_id", "customer"):
            if _as_str(checkout.get(key)).startswith("cus_"):
                context["customer"] = checkout[key]
                break
        form: dict = {
            "payment_method_data[type]": pm_form["type"],
            **{f"payment_method_data[card][{k.split('[')[1].rstrip(']')}]": v for k, v in pm_form.items() if k.startswith("card[")},
            "payment_method_data[allow_redisplay]": "limited",
            **{f"payment_method_data[billing_details][{k.split('[billing_details][')[1]}]": v for k, v in pm_form.items() if "[billing_details][" in k},
            "payment_method_data[payment_user_agent]": pm_form["payment_user_agent"],
            "payment_method_data[referrer]": pm_form["referrer"],
            "payment_method_data[time_on_page]": pm_form["time_on_page"],
            **{f"payment_method_data[{k}]": v for k, v in self.ids().items()},
            "setup_future_usage": "off_session",
            "set_as_default_payment_method": "false",
            "key": cp.get("publishable_key", ""),
            "_stripe_version": cp.get("stripe_version", STRIPE_CUSTOM_API_VERSION),
        }
        for k, v in m.items():
            if isinstance(v, list):
                for i, item in enumerate(v):
                    form[f"client_attribution_metadata[{k}][{i}]"] = item
            else:
                form[f"client_attribution_metadata[{k}]"] = str(v)
        if context:
            for k, v in context.items():
                if isinstance(v, list):
                    for i, item in enumerate(v):
                        form[f"client_context[{k}][{i}]"] = item
                else:
                    form[f"client_context[{k}]"] = str(v)
        data = self._call("POST", "/confirmation_tokens", form=form)
        token_id = _as_str(data.get("id"))
        if not token_id.startswith("ctoken_"):
            raise RuntimeError("confirmation_token_missing")
        return token_id

    def confirm_form(self) -> dict:
        cp = self.cp
        return {
            "confirmation_token": cp.get("confirmation_token", ""),
            "client_secret": cp.get("client_secret", ""),
            "key": cp.get("publishable_key", ""),
            "return_url": cp.get("return_url", ""),
            "_stripe_version": cp.get("stripe_version", STRIPE_CUSTOM_API_VERSION),
            "client_attribution_metadata[client_session_id]": self.client_id,
            "client_attribution_metadata[merchant_integration_source]": "l1",
        }

    def custom_checkout_form(self, init: dict, payment_method: str = "") -> dict:
        if not self.js_version:
            raise RuntimeError("stripe_build_missing")
        cp = self.cp
        if not cp.get("checkout_id", "").startswith("cs_live_"):
            raise RuntimeError("checkout_id_invalid")
        if not payment_method and not cp.get("confirmation_token"):
            raise RuntimeError("confirmation_token_missing")
        if not cp.get("return_url"):
            raise RuntimeError("invalid_return_url")
        checksum = _as_str(init.get("init_checksum"))
        if not checksum:
            raise RuntimeError("init_checksum_missing")
        expected: Any = None
        due = _hosted_amount(init)
        if due is not None:
            expected = due
        elif cp.get("amount") is not None:
            expected = cp["amount"]
        selection_flow = "automatic" if init.get("automatic_payment_method_types") is True else "merchant_specified"
        attribution: dict = {
            "client_session_id": self.client_id,
            "checkout_session_id": cp["checkout_id"],
            "merchant_integration_source": "checkout",
            "merchant_integration_version": "custom",
            "merchant_integration_subtype": "payment-element",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": selection_flow,
            "elements_session_id": self.elements_id,
        }
        if init.get("config_id"):
            attribution["checkout_config_id"] = init["config_id"]
        if self.config_id:
            attribution["elements_session_config_id"] = self.config_id
        sv = _checkout_page_version(init, cp.get("stripe_version", ""))
        session_client: dict = {
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": self.client_id,
            "locale": "en-US",
            "is_aggregation_expected": "false",
            "session_id": self.elements_id,
        }
        betas = _custom_client_betas(init)
        if betas:
            session_client["client_betas"] = betas
        form: dict = {
            "expected_payment_method_type": "card",
            "expected_amount": str(expected) if expected is not None else "",
            "key": cp.get("publishable_key", ""),
            "_stripe_version": sv,
            "return_url": cp.get("return_url", ""),
            "init_checksum": checksum,
            "version": self.js_version,
            **self.ids(),
        }
        # elements_session_client as nested fields
        for k, v in session_client.items():
            form[f"elements_session_client[{k}]"] = str(v)
        form["elements_options_client[saved_payment_method][enable_save]"] = "auto"
        form["elements_options_client[saved_payment_method][enable_redisplay]"] = "auto"
        for k, v in attribution.items():
            form[f"client_attribution_metadata[{k}]"] = str(v)
        if _consent_required(init):
            form["consent[terms_of_service]"] = "accepted"
        checksum_id = _as_str(init.get("id"))
        if payment_method:
            form["payment_method"] = payment_method
            checksum_id = payment_method
        else:
            form["confirmation_token"] = cp.get("confirmation_token", "")
            if not checksum_id.startswith("ppage_"):
                raise RuntimeError("payment_page_id_missing")
        js_checksum = compute_id_checksum(checksum_id)
        rv_ts = compute_rv_timestamp(self.build.get("rv_ts", ""), self.build.get("rv", ""), self.build.get("sv", ""))
        form["js_checksum"] = js_checksum
        form["rv_timestamp"] = rv_ts
        return form

    def update_custom_checkout(self, billing: dict, init: dict) -> dict:
        cp = self.cp
        eid = _first_truthy(init.get("eid"), "NA")
        sv = _checkout_page_version(init, cp.get("stripe_version", ""))
        return self._call("POST", f"/payment_pages/{cp['checkout_id']}", form={
            "eid": eid,
            **_tax_region_fields(billing),
            "key": cp.get("publishable_key", ""),
            "_stripe_version": sv,
        })

    def create_radar_session(self):
        try:
            data = self._call("POST", "/radar/radar_sessions", form={"key": self.cp.get("publishable_key", "")})
            self.cp.setdefault("metadata", {})["radar_session_id"] = _as_str(data.get("id"))
        except Exception:
            pass


def _billing_address_fields(billing: dict) -> dict:
    return {
        "country": billing.get("country", ""),
        "state": billing.get("state", ""),
        "city": billing.get("city", ""),
        "line1": billing.get("line1", ""),
        "postal_code": billing.get("postal_code", ""),
    }


def _tax_region_fields(billing: dict) -> dict:
    return {
        "tax_region[country]": billing.get("country", ""),
        "tax_region[state]": billing.get("state", ""),
        "tax_region[city]": billing.get("city", ""),
        "tax_region[line1]": billing.get("line1", ""),
        "tax_region[postal_code]": billing.get("postal_code", ""),
    }


def _hosted_amount(data: dict) -> int | None:
    for path in [["total_summary", "due"], ["total_summary", "total"], ["amount_due"], ["invoice", "amount_due"], ["total_summary", "amount_due"], ["amount_total"]]:
        node: Any = data
        for key in path:
            node = _as_map(node).get(key) if isinstance(node, dict) else None
        if isinstance(node, (int, float)) and node >= 0:
            return int(node)
    return None


def _find_hosted_url(data: Any, checkout_id: str) -> str:
    if isinstance(data, str):
        try:
            p = urllib.parse.urlparse(data)
            if (p.scheme == "https" and p.netloc in ("pay.openai.com", "checkout.stripe.com")
                    and p.path == f"/c/pay/{checkout_id}" and len(p.fragment) >= 250 and not p.query):
                return data
        except Exception:
            pass
        return ""
    if isinstance(data, dict):
        for v in data.values():
            found = _find_hosted_url(v, checkout_id)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_hosted_url(item, checkout_id)
            if found:
                return found
    return ""


def _last_displayed_line_item_group(info: dict) -> dict | None:
    group = _as_map(info.get("line_item_group"))
    if not group:
        return None
    inclusive = exclusive = discount = 0
    for item in (group.get("tax_amounts") or []):
        if isinstance(item, dict):
            amount = item.get("amount", 0)
            if item.get("inclusive"):
                inclusive += amount
            else:
                exclusive += amount
    for item in (group.get("discount_amounts") or []):
        if isinstance(item, dict):
            discount += item.get("amount", 0)
    shipping = _as_map(group.get("shipping_rate")).get("amount", 0) or 0
    subtotal = group.get("subtotal", 0) or 0
    return {"subtotal": subtotal, "total_exclusive_tax": exclusive, "total_inclusive_tax": inclusive, "total_discount_amount": discount, "shipping_rate_amount": shipping}


def _first_line_item_group(*nodes: dict) -> dict | None:
    for node in nodes:
        group = _as_map(node.get("line_item_group"))
        if group:
            return group
    return None


def _checkout_page_version(init: dict, fallback: str) -> str:
    from .constants import STRIPE_CUSTOM_API_VERSION, STRIPE_CUSTOM_CHECKOUT_API_VERSION
    base = (fallback or "").strip() or STRIPE_CUSTOM_API_VERSION
    betas = (init.get("beta_versions") or "").strip()
    if not betas:
        return STRIPE_CUSTOM_CHECKOUT_API_VERSION
    if betas in base:
        return base
    return base + "; " + betas


def _consent_required(init: dict) -> bool:
    return _as_str(_as_map(init.get("consent_collection")).get("terms_of_service")) == "required"


def _custom_client_betas(init: dict) -> list[str]:
    raw = _as_str(init.get("beta_versions")) or STRIPE_CUSTOM_CHECKOUT_API_VERSION
    betas = []
    if "custom_checkout_beta=v1" in raw:
        betas.append("custom_checkout_beta_6")
    if "checkout_server_update_beta=v1" in raw:
        betas.append("custom_checkout_server_updates_1")
    if "checkout_manual_approval_preview=v1" in raw:
        betas.append("custom_checkout_manual_approval_1")
    return betas


def stripe_classify(data: Any) -> str:
    if not isinstance(data, dict):
        return "unknown"
    root_error = _as_map(data.get("error"))
    containers = [data]
    for key in ("error", "submission_attempt"):
        node = _as_map(data.get(key))
        if node:
            containers.append(node)
    for node in containers:
        pi = _as_map(node.get("payment_intent"))
        if pi:
            state = _classify_intent(pi, root_error)
            if state != "unknown":
                return state
    id_ = _as_str(data.get("id"))
    if id_.startswith("pi_") or data.get("object") == "payment_intent":
        state = _classify_intent(data, root_error)
        if state != "unknown":
            return state
    payment_status = _norm(data.get("payment_status"))
    if payment_status == "paid":
        return "charged"
    submission = _as_map(data.get("submission_attempt"))
    top_states = [_norm(data.get("status")), _norm(data.get("state"))]
    sub_states = [_norm(submission.get("state")), _norm(submission.get("status"))]
    states = top_states + sub_states + [payment_status]
    if any(s in ("canceled", "cancelled", "declined", "failed", "expired") for s in states):
        return "not_charged"
    if any(s == "requires_approval" for s in states) or any(s == "requires_action" for s in sub_states):
        return "unknown"
    if any(s == "complete" for s in top_states) and payment_status == "unpaid":
        return "not_charged"
    if any(s in ("open", "processing", "pending", "requires_confirmation") for s in states) or payment_status == "unpaid":
        return "pending"
    if any(s == "requires_action" for s in top_states):
        return "action_required"
    error_data = _as_map(_first_truthy(data.get("last_payment_error"), data.get("error")))
    has_status = "status" in data or "state" in data
    if has_status and any(s in ("requires_payment_method", "") for s in states) and (error_data.get("decline_code") or error_data.get("code") == "card_declined"):
        return "not_charged"
    return "unknown"


def _classify_intent(pi: dict, fallback: dict) -> str:
    status = _norm(pi.get("status"))
    if status == "succeeded":
        return "charged"
    if status == "requires_capture":
        return "authorized"
    if status == "requires_action":
        return "action_required"
    if status in ("processing", "requires_confirmation"):
        return "pending"
    if status == "canceled":
        return "not_charged"
    if status == "requires_payment_method":
        err = _as_map(pi.get("last_payment_error"))
        if err.get("code") == "payment_intent_authentication_failure":
            return "not_charged"
    error_data = _as_map(_first_truthy(pi.get("last_payment_error"), fallback))
    has_status = "status" in pi
    if has_status and status in ("requires_payment_method", "") and (error_data.get("decline_code") or error_data.get("code") == "card_declined"):
        return "not_charged"
    return "unknown"


def _norm(v: Any) -> str:
    return _as_str(v).strip().lower()
