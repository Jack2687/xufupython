"""cs-live 完整支付流程，对应 Go gptpay_transaction.go + flow_*.go。

入口：
  pay(session, card, billing, options)   → 完整订阅办理
  create_checkout(session, options)      → 仅生成提链 URL
"""
from __future__ import annotations

import time
import urllib.parse
import uuid
from typing import Any, Callable

import requests

from .browser import BrowserProfile, resolve_browser_profile
from .chatgpt import (
    ChatGPTPlatform, renewal_state, subscription_active,
    _as_map, _as_str, _first_truthy,
)
from .constants import CHATGPT_BASE, checkout_plan, hosted_plan_type, lookup_region
from .sentinel import get_sentinel_token
from .stripe import StripeClient, stripe_classify, _find_hosted_url, _hosted_amount


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def pay(
    session: dict,
    card: dict,
    billing: dict,
    *,
    country: str,
    plan: str,
    proxy: str | None = None,
    timeout: float = 60,
    deadline: float | None = None,
    cancel_renewal: bool = True,
    expected_amount: int | None = None,
    checkout_type: str = "",       # "" | "oaics" | "hosted"
    profile_selection: str = "",   # "" | "windows" | "macos" | "random"
    clock_offset_hours: int = 0,
    on_event: Callable[[str, dict], None] | None = None,
    on_checkpoint: Callable[[dict], None] | None = None,
    seats: int = 0,
    seats5x: int = 0,
    interval: str = "month",
) -> dict:
    """完整订阅办理。返回 result dict (status, payment, checkout_id, error_code, …)。"""
    if deadline is None:
        deadline = time.time() + timeout * 3
    cp = _build_checkpoint(session, country, plan, cancel_renewal, seats, seats5x, interval, profile_selection, clock_offset_hours)
    http = _make_session(proxy)
    result = _Transaction(
        http=http, cp=cp, operation="pay", deadline=deadline, timeout=timeout,
        on_event=on_event, on_checkpoint=on_checkpoint,
    ).execute(billing=billing, card=card, expected_amount=expected_amount, checkout_type=checkout_type)
    return result


def create_checkout(
    session: dict,
    *,
    country: str,
    plan: str,
    proxy: str | None = None,
    timeout: float = 60,
    deadline: float | None = None,
    checkout_type: str = "",
    with_price: bool = False,
    profile_selection: str = "",
    clock_offset_hours: int = 0,
    on_event: Callable[[str, dict], None] | None = None,
    seats: int = 0,
    seats5x: int = 0,
    interval: str = "month",
    promo_code: str = "",
) -> dict:
    """仅生成提链 URL。返回 {checkout_url, checkout_id, entity, error_code}。"""
    if deadline is None:
        deadline = time.time() + timeout * 3
    cp = _build_checkpoint(session, country, plan, False, seats, seats5x, interval, profile_selection, clock_offset_hours)
    http = _make_session(proxy)
    result = _Transaction(
        http=http, cp=cp, operation="create_checkout", deadline=deadline, timeout=timeout,
        on_event=on_event, on_checkpoint=None,
    ).create_checkout(checkout_type=checkout_type, with_price=with_price, promo_code=promo_code)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(proxy: str | None) -> requests.Session:
    s = requests.Session()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def _build_checkpoint(
    session: dict, country: str, plan: str,
    cancel_renewal: bool, seats: int, seats5x: int, interval: str,
    profile_selection: str, clock_offset_hours: int,
) -> dict:
    country = country.upper()
    plan = plan.lower()
    region = lookup_region(country)
    if not region:
        raise ValueError(f"Unknown country: {country}")
    currency, timezone = region
    profile = resolve_browser_profile(profile_selection)
    return {
        "plan": plan,
        "country": country,
        "currency": currency,
        "timezone": timezone,
        "access_token": session.get("access_token") or session.get("accessToken", ""),
        "session_token": session.get("session_token") or session.get("sessionToken", ""),
        "cookies": session.get("cookies", ""),
        "account_id": session.get("account_id") or session.get("accountId", ""),
        "email": session.get("email", ""),
        "device_id": session.get("device_id") or session.get("deviceId", ""),
        "cancel_requested": cancel_renewal,
        "publishable_key": "",
        "checkout_id": "",
        "entity": "",
        "payment_intent_id": "",
        "client_secret": "",
        "confirmation_token": "",
        "return_url": "",
        "amount": None,
        "payment": "not_attempted",
        "activation": "pending",
        "renewal": "unknown",
        "phase": "",
        "proxy": "",
        "stripe_identity": {},
        "stripe_version": "",
        "metadata": {"browser_profile": profile.id, "clock_offset_hours": clock_offset_hours},
        "_profile": profile,
        "_seats": seats,
        "_seats5x": seats5x,
        "_interval": interval,
        "_clock_offset_hours": clock_offset_hours,
    }


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

class _Transaction:
    def __init__(self, http: requests.Session, cp: dict, operation: str, deadline: float, timeout: float,
                 on_event: Callable | None, on_checkpoint: Callable | None):
        self.http = http
        self.cp = cp
        self.deadline = deadline
        self.timeout = timeout
        self.operation = operation
        self.on_event = on_event
        self.on_checkpoint = on_checkpoint
        self.profile: BrowserProfile = cp["_profile"]
        self.platform = ChatGPTPlatform(http, cp, self.profile, cp.get("_clock_offset_hours", 0))
        self.stripe = StripeClient(http, cp, self.profile, cp.get("_clock_offset_hours", 0))

    def _emit(self, name: str, fields: dict):
        if self.on_event:
            try:
                self.on_event(name, fields)
            except Exception:
                pass

    def _save(self, phase: str = ""):
        if phase:
            self.cp["phase"] = phase
        if self.on_checkpoint:
            try:
                self.on_checkpoint(dict(self.cp))
            except Exception:
                pass

    def _check_deadline(self):
        if time.time() > self.deadline:
            raise RuntimeError("deadline_exceeded")

    def _sentinel_token(self, flow: str) -> tuple[str, str]:
        cp = self.cp
        return get_sentinel_token(
            self.http, flow,
            cp.get("access_token", ""), cp.get("cookies", ""), cp.get("device_id", ""),
            self.profile, cp.get("timezone", "UTC"), self.deadline,
            cp.get("_clock_offset_hours", 0),
        )

    # ------------------------------------------------------------------
    # Main execution paths
    # ------------------------------------------------------------------

    def execute(self, billing: dict, card: dict | None, expected_amount: int | None, checkout_type: str) -> dict:
        error_code = ""
        try:
            self._execute_flow(billing, card, expected_amount, checkout_type)
        except Exception as e:
            error_code = _flow_error(e)
        return self._result(error_code)

    def create_checkout(self, checkout_type: str, with_price: bool, promo_code: str) -> dict:
        error_code = ""
        checkout_url = ""
        try:
            mode = _requested_ui_mode(checkout_type)
            self._save("initializing")
            self.platform.homepage()
            self._refresh_if_needed()
            self._check_account()
            stripe_required = (with_price and checkout_type != "hosted") or self.on_checkpoint is not None
            if stripe_required:
                self._init_stripe()
                self._save("stripe_ready")
            token = self._sentinel_token("chatgpt_checkout")
            if promo_code:
                self.platform.PromoCode = promo_code  # type: ignore
            checkout = self.platform.checkout(checkout_plan(self.cp["plan"]), token, mode)
            self._absorb_checkout(checkout, checkout_type)
            if checkout_type == "hosted":
                checkout_url = self.stripe.extract_hosted_url(checkout)
            else:
                checkout_url = self._checkout_url(checkout, stripe_required)
        except Exception as e:
            error_code = _flow_error(e)
        result = {"checkout_url": checkout_url, "checkout_id": self.cp.get("checkout_id", ""), "entity": self.cp.get("entity", ""), "error_code": error_code}
        if self.cp.get("checkout_id"):
            result["session"] = {
                "access_token": self.cp.get("access_token", ""),
                "session_token": self.cp.get("session_token", ""),
                "cookies": self.cp.get("cookies", ""),
                "account_id": self.cp.get("account_id", ""),
                "email": self.cp.get("email", ""),
            }
        return result

    def _execute_flow(self, billing: dict, card: dict | None, expected_amount: int | None, checkout_type: str):
        checkout = self._prepare_checkout(checkout_type, True)
        ui_mode = self.cp.get("metadata", {}).get("checkout_ui_mode", "custom")
        if ui_mode == "redirect":
            if not self.cp.get("checkout_id", "").startswith("cs_live_"):
                raise RuntimeError("checkout_id_invalid")
            info = self.stripe.hosted_prepare(_find_hosted_url(checkout, self.cp["checkout_id"]), billing)
            if info.get("amount") is not None:
                self._pricing(info["amount"], info.get("currency"), expected_amount, "checkout_tax")
        else:
            tax = self.platform.taxes(billing)
            node = _as_map(tax.get("checkout_session"))
            self._pricing(node.get("amount_total"), _first_truthy(node.get("currency"), self.cp.get("currency")), expected_amount, "checkout_tax")

        if card is None:
            self._save("quoted")
            return

        self._save("priced")
        self._emit("checkout", {"outcome": "succeeded", "phase": self.cp.get("phase")})

        if ui_mode == "redirect":
            self._run_hosted_flow(card, billing, info)
        else:
            self._run_custom_flow(card, billing, checkout)

        if self.cp.get("payment") == "charged":
            self._finish_payment()
        self._save("")
        if self.cp.get("payment") != "charged":
            raise RuntimeError(self.cp.get("payment", "unknown"))

    def _prepare_checkout(self, checkout_type: str, stripe_required: bool) -> dict:
        mode = _requested_ui_mode(checkout_type)
        selected = checkout_type in ("hosted", "oaics")
        self._save("initializing")
        self.platform.homepage()
        self._refresh_if_needed()
        account = self._check_account()
        ent = _as_map(account.get("entitlement"))
        baseline = {
            "plan_type": _as_str(_first_truthy(_as_map(account.get("account")).get("plan_type"), ent.get("plan_type"), "")),
            "subscription_plan": _as_str(ent.get("subscription_plan")),
            "has_active_subscription": ent.get("has_active_subscription"),
            "cancels_at": _as_str(ent.get("cancels_at")),
        }
        subs, until, renew = self._load_current_subscription(baseline)
        flow = _account_payment_flow(self.cp["plan"], baseline)
        if selected and flow == "subscription_update":
            flow = "existing_subscription"
        self._emit("account", {"plan_type": baseline.get("plan_type", "free"), "renewal": renew or "unknown", "flow": flow, "outcome": "checked"})
        if flow == "subscription_update":
            self.cp.setdefault("metadata", {})["payment_flow"] = "subscription_update"
            self._save("account_checked")
            return {"payment_flow": "subscription_update"}
        self._save("account_checked")
        if flow == "existing_subscription":
            raise RuntimeError("existing_subscription")
        if stripe_required:
            self._init_stripe()
            self._save("stripe_ready")
        token = self._sentinel_token("chatgpt_checkout")
        checkout = self.platform.checkout(checkout_plan(self.cp["plan"]), token, mode)
        self._absorb_checkout(checkout, checkout_type)
        self._save("checkout_created")
        return checkout

    def _absorb_checkout(self, checkout: dict, checkout_type: str):
        cp = self.cp
        selected = checkout_type in ("hosted", "oaics")
        mode = _requested_ui_mode(checkout_type)
        checkout_id = _as_str(_first_truthy(checkout.get("checkout_session_id"), checkout.get("stripe_checkout_session_id"), ""))
        import re
        if not re.match(r'(?:oaics_|cs_live_)[A-Za-z0-9]+', checkout_id):
            raise RuntimeError("checkout_id_invalid")
        cp["checkout_id"] = checkout_id
        cp["entity"] = _as_str(_first_truthy(checkout.get("processor_entity"), "openai_llc"))
        if checkout.get("publishable_key"):
            cp["publishable_key"] = _as_str(checkout["publishable_key"])
        # Determine actual UI mode
        actual = mode
        if not selected:
            actual = _as_str(checkout.get("checkout_ui_mode"))
            if actual not in ("custom", "redirect", "hosted"):
                raise RuntimeError("checkout_ui_mode_invalid")
        if actual == "hosted":
            actual = "redirect"
        cp.setdefault("metadata", {})["checkout_ui_mode"] = actual
        raw = _find_hosted_url(checkout, checkout_id)
        if raw and (not selected or checkout_type == "hosted"):
            cp.setdefault("metadata", {})["hosted_url"] = raw
        if isinstance(checkout.get("client_secret"), str):
            secret = checkout["client_secret"]
            if secret.startswith(checkout_id + "_secret_") and len(secret) > len(checkout_id + "_secret_"):
                cp.setdefault("metadata", {})["checkout_client_secret"] = secret
        if checkout.get("confirm_return_url"):
            raw_return = _as_str(checkout["confirm_return_url"])
            cp["return_url"] = raw_return
        if checkout.get("publishable_key"):
            key = _as_str(checkout["publishable_key"])
            if key.startswith("pk_"):
                cp["publishable_key"] = key

    def _init_stripe(self):
        cp = self.cp
        if not cp.get("publishable_key", "").startswith("pk_"):
            cp["publishable_key"] = self.platform.get_publishable_key()
        self.stripe.initialize()

    def _refresh_if_needed(self):
        try:
            self.platform.refresh()
        except RuntimeError as e:
            code = _flow_error(e)
            if code not in ("session_expired",) or not self.cp.get("access_token"):
                raise
        account_data = None
        try:
            account_data = self.platform.account()
        except RuntimeError as e:
            if _flow_error(e) == "http_401":
                self.platform.refresh()
                account_data = self.platform.account()
            else:
                raise
        return account_data

    def _check_account(self) -> dict:
        try:
            return self.platform.account()
        except RuntimeError as e:
            if _flow_error(e) == "http_401":
                self.platform.refresh()
                return self.platform.account()
            raise

    def _load_current_subscription(self, baseline: dict) -> tuple[dict, str, str]:
        try:
            subs = self.platform.subscriptions(False)
        except RuntimeError as e:
            if _flow_error(e) == "subscription_not_found":
                return {}, "", "unknown"
            raise
        if subs.get("plan_type", "").strip():
            baseline["plan_type"] = subs["plan_type"].strip()
        sp = _as_str(_first_truthy(subs.get("subscription_plan"), subs.get("plan"))).strip()
        if sp:
            baseline["subscription_plan"] = sp
        until = _as_str(_first_truthy(subs.get("active_until"), subs.get("current_period_end"), subs.get("expires_at"), subs.get("billing_period_end")))
        pt = baseline.get("plan_type", "").lower()
        if pt and pt != "free" and (until or subs.get("will_renew") is True):
            baseline["has_active_subscription"] = True
        return subs, until, renewal_state(subs)

    def _pricing(self, total: Any, currency: Any, expected: int | None, source: str):
        try:
            n = int(total)
        except (TypeError, ValueError):
            raise RuntimeError("checkout_total_missing")
        if n < 0:
            raise RuntimeError("checkout_total_missing")
        if _as_str(currency).upper() != self.cp.get("currency", ""):
            raise RuntimeError("checkout_currency_mismatch")
        if expected is not None and n != expected:
            raise RuntimeError("checkout_amount_changed")
        self.cp["amount"] = n
        self._save("priced")

    def _checkout_url(self, checkout: dict, stripe_ready: bool) -> str:
        cp = self.cp
        if cp.get("metadata", {}).get("checkout_ui_mode") == "custom":
            return f"{CHATGPT_BASE}/checkout/{cp['entity']}/{cp['checkout_id']}"
        raw = _find_hosted_url(checkout, cp["checkout_id"])
        if raw:
            return raw
        if cp.get("checkout_id", "").startswith("cs_live_"):
            return self.stripe.extract_hosted_url(checkout)
        return self.stripe.extract_hosted_url(checkout)

    # ------------------------------------------------------------------
    # Hosted flow (cs_live_ + redirect)
    # ------------------------------------------------------------------

    def _run_hosted_flow(self, card: dict, billing: dict, info: dict):
        cp = self.cp
        cp["stripe_version"] = "2026-03-25.dahlia"
        cp.setdefault("metadata", {})["hosted_url"] = self.stripe.hosted_url
        form = self.stripe.hosted_form(card, billing, info)
        cp["confirm_body"] = form
        # boundary — set payment unknown before submit
        cp["payment"] = "unknown"
        self._save("hosted_confirm")
        data = self.stripe.confirm_hosted(form)
        # emit diagnostics
        self._emit("payment_confirm", {"phase": cp.get("phase"), "outcome": "submitted",
                                        "checkout_status": _as_str(data.get("status")),
                                        "payment_status": _as_str(data.get("payment_status"))})
        # poll
        data = self._poll_hosted(data)
        self._observe_payment(data)
        landing, callback, state = self._select_hosted_landing(data)
        charged_saved = cp.get("payment") == "charged"
        if charged_saved:
            self._save("payment_result")
        self.platform.snapshot(billing)
        data = self._approve_hosted(data, landing, callback, state)
        if not charged_saved:
            self._save("payment_result")

    def _poll_hosted(self, data: dict) -> dict:
        for _ in range(3):
            state = stripe_classify(data)
            if state in ("charged", "action_required", "not_charged"):
                break
            time.sleep(1.2)
            self._check_deadline()
            data = self.stripe.page()
        return data

    def _select_hosted_landing(self, data: dict) -> tuple[str, bool, Any]:
        cp = self.cp
        submission = _as_map(data.get("submission_attempt"))
        state = _first_truthy(submission.get("state"), submission.get("status"))
        cp["return_url"] = (f"{CHATGPT_BASE}/checkout/verify?stripe_session_id="
                            f"{urllib.parse.quote(cp['checkout_id'])}&processor_entity="
                            f"{urllib.parse.quote(cp['entity'])}&plan_type="
                            f"{hosted_plan_type(cp['plan'])}")
        landing = cp["return_url"]
        callback = False
        completion = _as_map(data.get("after_completion"))
        redirect = _as_map(completion.get("redirect"))
        for v in [data.get("success_url"), data.get("return_url"), data.get("url"), redirect.get("url"), completion.get("url")]:
            candidate = _as_str(v)
            if not candidate:
                continue
            try:
                p = urllib.parse.urlparse(candidate)
                q = urllib.parse.parse_qs(p.query).get("stripe_session_id", [])
                if (p.scheme == "https" and p.netloc == "chatgpt.com" and
                        (len(q) == 1 and q[0] == cp["checkout_id"] or p.path == f"/checkout/{cp['entity']}/{cp['checkout_id']}")):
                    landing = candidate
                    callback = True
                    break
            except Exception:
                pass
        return landing, callback, state

    def _approve_hosted(self, data: dict, landing: str, callback: bool, state: Any) -> dict:
        cp = self.cp
        if cp.get("payment") not in ("charged", "action_required", "not_charged") and (callback or state in ("requires_approval", "requires_action")):
            path = "/backend-api/payments/checkout/approve"
            token = self._sentinel_token("checkout_session_approval")
            headers = self.platform.challenge_headers(path, token, landing, True)
            self._save("hosted_approve")
            reply = self.platform.call("POST", path, body={"checkout_session_id": cp["checkout_id"], "processor_entity": cp["entity"]}, headers=headers)
            if _first_truthy(reply.get("result"), reply.get("status"), reply.get("state")) == "blocked":
                raise RuntimeError("approval_blocked")
            data = self.stripe.page()
            self._observe_payment(data)
        return data

    # ------------------------------------------------------------------
    # Custom flow (oaics_ or cs_live_ + custom mode)
    # ------------------------------------------------------------------

    def _run_custom_flow(self, card: dict, billing: dict, checkout: dict):
        cp = self.cp
        if cp.get("checkout_id", "").startswith("cs_live_"):
            return self._run_custom_stripe_checkout(card, billing, checkout)
        token = self.stripe.token(card, billing, checkout)
        cp["confirmation_token"] = token
        self._run_custom_internal_confirm()

    def _run_custom_internal_confirm(self):
        cp = self.cp
        sentinel = self._sentinel_token("checkout_session_approval")
        path = "/backend-api/payments/checkout/confirm"
        headers = self.platform.challenge_headers(path, sentinel, f"{CHATGPT_BASE}/checkout/{cp['entity']}/{cp['checkout_id']}", False)
        cp["payment"] = "unknown"
        self._save("openai_confirm_checkout")
        data = self.platform.call("POST", path, body={
            "checkout_session_id": cp["checkout_id"],
            "confirm_token": cp["confirmation_token"],
            "selected_payment_method_type": "card",
        }, headers=headers)
        status = _as_str(_first_truthy(data.get("status"), data.get("result"), data.get("state"))).lower()
        if status == "blocked":
            cp["payment"] = "not_charged"
            self._save("payment_result")
            raise RuntimeError("approval_blocked")
        cp["client_secret"] = _as_str(_first_truthy(
            data.get("client_secret"),
            _as_map(data.get("payment_intent")).get("client_secret"),
            _as_map(data.get("stripe_payment_intent")).get("client_secret"),
            _as_map(data.get("checkout_session")).get("client_secret"),
        ))
        import re
        if not re.match(r'pi_[A-Za-z0-9]+_secret_[A-Za-z0-9]+', cp.get("client_secret", "")):
            raise RuntimeError("payment_intent_secret_missing")
        cp["payment_intent_id"] = cp["client_secret"].split("_secret_")[0]
        if data.get("confirm_return_url"):
            cp["return_url"] = _as_str(data["confirm_return_url"])
        self._save("openai_confirmed")
        ready = self._needs_client_confirmation()
        if not ready:
            return self._save("payment_result")
        cp["confirm_body"] = self.stripe.confirm_form()
        cp["payment"] = "unknown"
        self._save("stripe_confirm")
        result = self.stripe.confirm_payment_intent(cp["confirm_body"])
        self._observe_payment(result)
        self._save("payment_result")

    def _run_custom_stripe_checkout(self, card: dict, billing: dict, checkout: dict):
        cp = self.cp
        init = self.stripe.custom_price_snapshot(checkout)
        if not init.get("init_checksum"):
            init = self.stripe.custom_page()
        self.stripe.bind_checkout_elements()
        updated = self.stripe.update_custom_checkout(billing, init)
        _merge_custom_payment_page(init, updated)
        self.stripe.create_radar_session()
        token = self.stripe.token(card, billing, checkout)
        cp["confirmation_token"] = token
        if not cp.get("return_url"):
            cp["return_url"] = (f"{CHATGPT_BASE}/checkout/verify?stripe_session_id="
                                f"{urllib.parse.quote(cp['checkout_id'])}&processor_entity="
                                f"{urllib.parse.quote(cp['entity'])}&plan_type="
                                f"{hosted_plan_type(cp['plan'])}")
        form = self.stripe.custom_checkout_form(init, "")
        cp["confirm_body"] = form
        cp["payment"] = "unknown"
        self._save("openai_confirm_checkout")
        data = self.stripe.confirm_hosted(form)
        self._emit_payment_diagnostics(data)
        err_obj = _as_map(data.get("error"))
        self._emit("payment_confirm", {"phase": cp.get("phase"), "outcome": "submitted",
                                        "checkout_status": _as_str(data.get("status")),
                                        "payment_status": _as_str(data.get("payment_status")),
                                        "provider_code": _as_str(err_obj.get("code"))})
        if err_obj.get("code"):
            try:
                page = self.stripe.custom_page()
                data = page
                self._observe_payment(data)
            except Exception:
                pass
            self._save("payment_result")
            raise RuntimeError(_as_str(err_obj["code"]))
        data = self._poll_custom(data)
        submission = _as_map(data.get("submission_attempt"))
        sa_state = _first_truthy(submission.get("state"), submission.get("status"))
        if sa_state in ("requires_approval", "requires_action"):
            self.platform.snapshot(billing)
        data = self._approve_custom(data)
        self._observe_payment(data)
        self._save("payment_result")
        self._check_custom_submission_error(data)

    def _poll_custom(self, data: dict) -> dict:
        for _ in range(8):
            state = stripe_classify(data)
            submission = _as_map(data.get("submission_attempt"))
            sa_state = _first_truthy(submission.get("state"), submission.get("status"))
            if state in ("charged", "action_required", "not_charged") or sa_state == "requires_approval":
                break
            time.sleep(1.2)
            self._check_deadline()
            data = self.stripe.custom_page()
        return data

    def _approve_custom(self, data: dict) -> dict:
        cp = self.cp
        submission = _as_map(data.get("submission_attempt"))
        state = _first_truthy(submission.get("state"), submission.get("status"))
        if state not in ("requires_approval", "requires_action"):
            return data
        path = "/backend-api/payments/checkout/approve"
        token = self._sentinel_token("checkout_session_approval")
        headers = self.platform.challenge_headers(path, token, cp.get("return_url", ""), True)
        self._save("")
        reply = self.platform.call("POST", path, body={"checkout_session_id": cp["checkout_id"], "processor_entity": cp["entity"]}, headers=headers)
        if _first_truthy(reply.get("result"), reply.get("status"), reply.get("state")) == "blocked":
            raise RuntimeError("approval_blocked")
        data = self.stripe.custom_page()
        return self._poll_custom_after_approve(data)

    def _poll_custom_after_approve(self, data: dict) -> dict:
        for _ in range(10):
            state = stripe_classify(data)
            if state in ("charged", "action_required", "not_charged"):
                return data
            time.sleep(1.5)
            self._check_deadline()
            data = self.stripe.custom_page()
        return data

    def _check_custom_submission_error(self, data: dict):
        err = _as_map(data.get("error"))
        if err:
            raise RuntimeError(_as_str(err.get("code")) or "payment_declined")

    def _needs_client_confirmation(self) -> bool:
        cp = self.cp
        data = self.stripe.retrieve()
        if data.get("error") or data.get("id") != cp.get("payment_intent_id"):
            raise RuntimeError("payment_intent_state_unavailable")
        status = _as_str(data.get("status"))
        self._observe_payment(data)
        last = data.get("last_payment_error")
        pm = data.get("payment_method")
        has_last = "last_payment_error" in data
        has_pm = "payment_method" in data
        initial = (status == "requires_payment_method" and has_last and last is None and has_pm and pm is None)
        ready = ((status == "requires_confirmation" or initial) and
                 data.get("confirmation_method") == "automatic" and
                 last is None and data.get("latest_charge") is None and data.get("next_action") is None)
        return ready

    # ------------------------------------------------------------------
    # Post-payment
    # ------------------------------------------------------------------

    def _finish_payment(self):
        self._confirm_activation()
        if not self.cp.get("cancel_requested"):
            self._save("completed")
            return
        self._disable_renewal()

    def _confirm_activation(self):
        cp = self.cp
        self._save("activation")
        if cp.get("return_url"):
            try:
                self.http.get(cp["return_url"], headers=self.platform._headers(""), timeout=30)
            except Exception:
                pass
        for i in range(5):
            if i > 0:
                time.sleep(2)
                self._check_deadline()
            try:
                subs = self.platform.subscriptions(False)
            except RuntimeError as e:
                if _flow_error(e) == "http_401":
                    self.platform.refresh()
                    self._save("")
                    try:
                        subs = self.platform.subscriptions(False)
                    except RuntimeError as e2:
                        if _flow_error(e2) == "subscription_not_found" and cp.get("payment") == "charged":
                            continue
                        raise
                elif _flow_error(e) == "subscription_not_found" and cp.get("payment") == "charged":
                    continue
                else:
                    raise
            if subscription_active(subs, cp["plan"]):
                cp["activation"] = "active"
                cp["renewal"] = renewal_state(subs)
                break
        self._save("")
        if cp.get("activation") != "active":
            raise RuntimeError("activation_pending")

    def _disable_renewal(self):
        cp = self.cp
        self.platform.refresh()
        self._save("cancellation")
        subs = self.platform.subscriptions(True)
        cp["renewal"] = renewal_state(subs)
        if cp["renewal"] == "unknown":
            raise RuntimeError("renewal_state_unknown")
        if cp["renewal"] == "off":
            return self._save("completed")
        for attempt in range(2):
            self._save("cancellation_requested")
            try:
                self.platform.call("POST", "/backend-api/subscriptions/cancel", body={"account_id": cp["account_id"]}, mode="renewal")
                break
            except RuntimeError as original:
                subs = self.platform.subscriptions(True)
                cp["renewal"] = renewal_state(subs)
                if cp["renewal"] == "off":
                    return self._save("completed")
                retryable = _flow_error(original) in ("network_error", "http_408", "http_429", "http_500", "http_502", "http_503", "http_504")
                if attempt > 0 or cp["renewal"] != "on" or not retryable:
                    raise original
        for i in range(5):
            if i > 0:
                time.sleep(2)
                self._check_deadline()
            subs = self.platform.subscriptions(True)
            cp["renewal"] = renewal_state(subs)
            if cp["renewal"] == "off":
                return self._save("completed")
        raise RuntimeError("cancellation_pending")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _observe_payment(self, data: dict):
        self._emit_payment_diagnostics(data)
        self._absorb_hosted_intent(data)
        state = stripe_classify(data)
        if state not in ("unknown",):
            self._set_payment_state(state, data)

    def _absorb_hosted_intent(self, data: dict):
        cp = self.cp
        raw = data.get("payment_intent")
        if isinstance(raw, str) and raw.startswith("pi_") and "_secret_" not in raw:
            cp["payment_intent_id"] = raw
            return
        pi = _as_map(raw)
        if _as_str(pi.get("id")).startswith("pi_"):
            cp["payment_intent_id"] = _as_str(pi["id"])
        if "_secret_" in _as_str(pi.get("client_secret")):
            cp["client_secret"] = _as_str(pi["client_secret"])

    def _set_payment_state(self, state: str, data: dict):
        cp = self.cp
        if cp.get("payment") == "charged":
            return
        cp["payment"] = state
        if state == "charged":
            self._emit("payment_confirm", {"phase": cp.get("phase"), "outcome": "succeeded", "payment_state": "charged"})

    def _emit_payment_diagnostics(self, data: dict):
        err_obj = _as_map(data.get("error"))
        self._emit("payment_diagnostic", {
            "status": _as_str(data.get("status")),
            "payment_status": _as_str(data.get("payment_status")),
            "provider_code": _as_str(err_obj.get("code")),
            "decline_code": _as_str(err_obj.get("decline_code")),
        })

    def _result(self, error_code: str) -> dict:
        cp = self.cp
        warning = ""
        if cp.get("payment") == "charged":
            warning, error_code = error_code, ""
        return {
            "payment": cp.get("payment", "unknown"),
            "activation": cp.get("activation", "pending"),
            "renewal": cp.get("renewal", "unknown"),
            "checkout_id": cp.get("checkout_id", ""),
            "payment_intent_id": cp.get("payment_intent_id", ""),
            "amount": cp.get("amount"),
            "currency": cp.get("currency", ""),
            "error_code": error_code,
            "warning_code": warning,
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _requested_ui_mode(kind: str) -> str:
    if kind == "oaics":
        return "custom"
    if kind == "hosted":
        return "hosted"
    return "custom"


def _account_payment_flow(plan: str, baseline: dict) -> str:
    if baseline.get("has_active_subscription") is True:
        return "existing_subscription"
    return "checkout"


def _flow_error(e: Exception) -> str:
    msg = str(e)
    if msg.startswith("http_"):
        return msg
    return msg or "unknown_error"


def _merge_custom_payment_page(dst: dict, src: dict):
    if not dst or not src:
        return
    for key in ("init_checksum", "eid", "config_id", "beta_versions", "automatic_payment_method_types",
                "consent_collection", "total_summary", "recurring_details", "invoice", "line_item_group"):
        if src.get(key) is not None and src.get(key) != "":
            dst[key] = src[key]
    if _as_str(src.get("id")).startswith("ppage_"):
        dst["id"] = src["id"]
