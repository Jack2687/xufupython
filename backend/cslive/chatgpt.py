"""ChatGPT 平台客户端，对应 Go chatgpt_client.go + chatgpt_subscriptions.go + chatgpt_parsing.go。"""
from __future__ import annotations

import json
import random
import re
import string
import urllib.parse
from typing import Any

import requests

from .browser import BrowserProfile
from .constants import CHATGPT_BASE, checkout_plan, hosted_plan_type, is_team_plan, matches_subscription_type


def _observation() -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "v1.r.p." + "".join(random.choices(alphabet, k=10))


def _telemetry() -> str:
    val = [
        1,
        round(random.randint(3800, 16000) / 10, 1),
        random.randint(3, 12),
        random.randint(80, 420),
        random.randint(18, 90),
        random.randint(1, 6),
        0,
        random.randint(420, 1500),
    ]
    return json.dumps(val, separators=(",", ":"))


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _first_truthy(*args: Any) -> Any:
    for a in args:
        if a:
            return a
    return None


def _as_map(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    return {}


def _renewal_state(data: dict) -> str:
    states: set[str] = set()
    items = [data]
    subs = data.get("subscriptions") or data.get("items")
    if isinstance(subs, list):
        items.extend(subs)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("will_renew") is False or item.get("cancel_at_period_end") is True or item.get("cancels_at") or item.get("cancel_at"):
            states.add("off")
        if item.get("will_renew") is True or item.get("cancel_at_period_end") is False:
            states.add("on")
    if len(states) == 1:
        return next(iter(states))
    return "unknown"


def _subscription_active(data: dict, plan: str) -> bool:
    plan_type = _as_str(data.get("plan_type")).lower()
    if not matches_subscription_type(plan, plan_type):
        return False
    return bool(_first_truthy(data.get("active_until"), data.get("current_period_end"), data.get("expires_at"), data.get("billing_period_end"))) or data.get("will_renew") is True


def _is_subscription_not_found(path: str, response: requests.Response) -> bool:
    if path != "/backend-api/subscriptions":
        return False
    if response.status_code != 404:
        return False
    if len(response.content) > 4096:
        return False
    ct = response.headers.get("Content-Type", "")
    if ct and "application/json" not in ct:
        return False
    try:
        body = response.json()
        return isinstance(body, dict) and body.get("detail") == "No subscription found for account"
    except Exception:
        return False


class ChatGPTPlatform:
    def __init__(self, http: requests.Session, cp: dict, profile: BrowserProfile, clock_offset_hours: int = 0):
        self.http = http
        self.cp = cp
        self.profile = profile
        self.clock_offset_hours = clock_offset_hours
        if not cp.get("cookies"):
            st = cp.get("session_token", "")
            cp["cookies"] = (
                f"__Secure-platform.session-token={st}; "
                f"__Secure-next-auth.session-token={st}; "
                "__Secure-platform.callback-url=https%3A%2F%2Fchatgpt.com%2F; "
                "__Secure-next-auth.callback-url=https%3A%2F%2Fchatgpt.com%2F"
            )
        if "session_id" not in cp.get("metadata", {}):
            import uuid
            cp.setdefault("metadata", {})["session_id"] = str(uuid.uuid4())

    def _headers(self, path: str, referer: str | None = None, mode: str = "payment") -> dict[str, str]:
        cp = self.cp
        referer = referer or (CHATGPT_BASE + "/")
        h: dict[str, str] = {
            "Authorization": f"Bearer {cp.get('access_token', '')}",
            "Cookie": cp.get("cookies", ""),
            "User-Agent": self.profile.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": CHATGPT_BASE,
            "Referer": referer,
            "oai-device-id": cp.get("device_id", ""),
            "oai-language": "en-US",
            "oai-session-id": cp.get("metadata", {}).get("session_id", ""),
        }
        for k, v in [("client_version", "oai-client-version"), ("build", "oai-client-build-number"), ("attestation", "oai-web-deployment-attestation")]:
            if cp.get("metadata", {}).get(k):
                h[v] = cp["metadata"][k]
        target = path
        if path.endswith("/taxes") or path.endswith("/snapshot"):
            target = path.replace("/backend-api", "/api/v1")
        if mode == "renewal":
            target = path.replace("/backend-api", "/api/v1")
            h.update({"Accept": "application/json", "chatgpt-account-id": cp.get("account_id", ""), "x-openai-is-client-observation": _observation()})
        else:
            h.update({"x-oai-is-client-observation": _observation(), "x-oai-is-pending-updates": '{"v":3,"updates":[]}'})
            if path != "/backend-api/subscriptions":
                h.update(self.profile.headers())
            if not (path.endswith("/confirm") or path.endswith("/approve") or path == "/backend-api/payments/checkout"):
                h.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin", "priority": "u=1, i"})
                if path != "/backend-api/subscriptions":
                    h.update(self.profile.extended_hints())
            if "/payments/checkout" in path and not (path.endswith("/taxes") or path.endswith("/update")) or cp.get("plan", "") != "plus":
                if "/payments/checkout" in path:
                    h.update({"chatgpt-account-id": cp.get("account_id", ""), "platform-account-id": cp.get("account_id", "")})
        if "/payments/" in path or "/subscriptions" in path:
            h.update({"x-openai-target-path": target, "x-openai-target-route": target})
        h.update(self.profile.headers())
        return h

    def _absorb(self, response: requests.Response):
        # Merge response cookies into cp.cookies
        cp = self.cp
        jar: dict[str, str] = {}
        for part in (cp.get("cookies") or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()
        for name, value in response.cookies.items():
            jar[name] = value
            if name == "oai-did":
                cp["device_id"] = value
        cp["cookies"] = "; ".join(f"{k}={v}" for k, v in jar.items())

    def call(self, method: str, path: str, body: Any = None, headers: dict | None = None, mode: str = "payment") -> dict:
        h = headers or self._headers(path, mode=mode)
        url = CHATGPT_BASE + path
        kwargs: dict = {"headers": h, "timeout": 60}
        if method == "GET":
            resp = self.http.get(url, **kwargs)
        else:
            kwargs["json"] = body
            resp = self.http.request(method, url, **kwargs)
        self._absorb(resp)
        if _is_subscription_not_found(path.split("?")[0], resp):
            raise RuntimeError("subscription_not_found")
        if resp.status_code >= 400:
            raise RuntimeError(f"http_{resp.status_code}")
        return resp.json()

    def challenge_headers(self, path: str, token: tuple[str, str], referer: str, split: bool) -> dict[str, str]:
        h = self._headers(path, referer=referer)
        sentinel_token, telemetry = token
        if not split:
            telemetry = _telemetry()
        h.update({"openai-sentinel-token": sentinel_token, "oai-telemetry": telemetry})
        if split:
            try:
                payload = json.loads(sentinel_token)
                if payload.get("p"):
                    h["OpenAI-Sentinel-Proof-Token"] = payload["p"]
                if payload.get("t"):
                    h["OpenAI-Sentinel-Turnstile-Token"] = payload["t"]
            except Exception:
                raise RuntimeError("invalid_sentinel_token")
        return h

    def refresh(self):
        cp = self.cp
        h = self._headers("/api/auth/session", mode="renewal")
        h.pop("Authorization", None)
        h.update({"x-openai-target-path": "/api/auth/session", "x-openai-target-route": "/api/auth/session"})
        resp = self.http.get(CHATGPT_BASE + "/api/auth/session?refresh=true&reason=token_expired", headers=h, timeout=60)
        self._absorb(resp)
        if resp.status_code >= 400:
            raise RuntimeError(f"http_{resp.status_code}")
        data = resp.json()
        token = data.get("accessToken", "")
        if not token or data.get("error"):
            raise RuntimeError("session_expired")
        account = _as_str(_as_map(data.get("account")).get("id"))
        if account and cp.get("account_id") and account != cp["account_id"]:
            raise RuntimeError("account_mismatch")
        cp["access_token"] = token
        if not cp.get("account_id"):
            cp["account_id"] = account
        if not cp.get("email"):
            cp["email"] = _as_str(_as_map(data.get("user")).get("email"))

    def homepage(self):
        cp = self.cp
        h = dict(self._headers(""))
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        resp = self.http.get(CHATGPT_BASE + "/", headers=h, timeout=60)
        self._absorb(resp)
        if resp.status_code != 200:
            raise RuntimeError("homepage_unavailable")
        html = resp.text
        for key, pattern in [
            ("client_version", r'data-build="([^"]+)"'),
            ("build", r'data-seq="([^"]+)"'),
            ("attestation", r'"webDeploymentAttestation":"([^"]+)"'),
            ("session_id", r'"sessionId":"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                cp.setdefault("metadata", {})[key] = m.group(1)
        if not cp.get("device_id"):
            raise RuntimeError("device_id_missing")

    def account(self) -> dict:
        import zoneinfo
        cp = self.cp
        tz = zoneinfo.ZoneInfo(cp.get("timezone", "UTC"))
        from datetime import datetime
        local = datetime.now(tz)
        offset_min = -int(local.utcoffset().total_seconds() // 60)
        data = self.call("GET", f"/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min={offset_min}")
        for value in _as_map(data.get("accounts")).values():
            entry = _as_map(value)
            account = _as_map(entry.get("account"))
            aid = _as_str(_first_truthy(account.get("id"), account.get("account_id")))
            if aid == cp.get("account_id"):
                kind = _as_str(account.get("account_type")).lower()
                if kind in ("team", "business", "enterprise"):
                    raise RuntimeError("personal_account_required")
                return {"account": account, "entitlement": _as_map(entry.get("entitlement"))}
        raise RuntimeError("account_not_found")

    def checkout(self, plan_name: str, token: tuple[str, str], ui_mode: str = "redirect") -> dict:
        cp = self.cp
        if not ui_mode:
            ui_mode = "redirect"
        path = "/backend-api/payments/checkout"
        h = self.challenge_headers(path, token, CHATGPT_BASE + "/#pricing", True)
        body: dict = {
            "plan_name": plan_name,
            "billing_details": {"country": cp.get("country", ""), "currency": cp.get("currency", "")},
            "checkout_ui_mode": ui_mode,
            "cancel_url": CHATGPT_BASE + "/#pricing",
        }
        if ui_mode != "hosted":
            body["entry_point"] = "all_plans_pricing_modal"
        if is_team_plan(cp.get("plan", "")):
            cfg = cp.get("team_seat_config", {})
            total = cfg.get("default", 0) + cfg.get("prolite", 0)
            body["team_plan_data"] = {
                "workspace_name": "Team",
                "quantity": total,
                "seat_quantity": total,
                "price_interval": cfg.get("interval", "month"),
                "seat_quantities": cfg.get("quantities", []),
            }
        return self.call("POST", path, body=body, headers=h)

    def taxes(self, billing: dict) -> dict:
        cp = self.cp
        return self.call("POST", "/backend-api/payments/checkout/taxes", body={
            "checkout_session_id": cp.get("checkout_id", ""),
            "checkout_email": _first_truthy(billing.get("email"), cp.get("email")) or "",
            "billing_country": billing.get("country", ""),
            "billing_name": billing.get("name", ""),
            "currency": cp.get("currency", "").lower(),
            "processor_entity": cp.get("entity", "openai_llc"),
            "billing_address": _billing_address(billing),
        })

    def subscriptions(self, renewal: bool = False) -> dict:
        cp = self.cp
        mode = "renewal" if renewal else "payment"
        path = f"/backend-api/subscriptions?account_id={urllib.parse.quote(cp.get('account_id', ''))}"
        return self.call("GET", path, mode=mode)

    def snapshot(self, billing: dict):
        try:
            self.call("POST", "/backend-api/payments/checkout/snapshot", body={
                "snapshot": {"billing_address": {"name": billing.get("name", ""), "address": _billing_address(billing)}}
            })
        except Exception:
            pass

    def approve(self, checkout_id: str, entity: str, token: tuple[str, str], landing: str) -> dict:
        path = "/backend-api/payments/checkout/approve"
        h = self.challenge_headers(path, token, landing, True)
        return self.call("POST", path, body={"checkout_session_id": checkout_id, "processor_entity": entity}, headers=h)

    def get_publishable_key(self) -> str:
        cp = self.cp
        if cp.get("publishable_key", "").startswith("pk_"):
            return cp["publishable_key"]
        data = self.call("GET", f"/backend-api/payments/stripe_client_bootstrap?account_id={urllib.parse.quote(cp.get('account_id', ''))}")
        key = _as_str(_first_truthy(data.get("publishable_key"), data.get("publishableKey")))
        if not key.startswith("pk_"):
            raise RuntimeError("publishable_key_missing")
        cp["publishable_key"] = key
        return key

    def ensure_device_id(self):
        cp = self.cp
        if cp.get("device_id"):
            return
        for part in (cp.get("cookies") or "").split(";"):
            part = part.strip()
            if part.startswith("oai-did=") and len(part) > 8:
                cp["device_id"] = part[8:]
                return
        import uuid
        cp["device_id"] = str(uuid.uuid4())
        if cp["device_id"] not in (cp.get("cookies") or ""):
            sep = "; " if cp.get("cookies") else ""
            cp["cookies"] = (cp.get("cookies") or "") + sep + "oai-did=" + cp["device_id"]


def _billing_address(billing: dict) -> dict:
    return {
        "line1": billing.get("line1", ""),
        "city": billing.get("city", ""),
        "state": billing.get("state", ""),
        "postal_code": billing.get("postal_code", ""),
        "country": billing.get("country", ""),
    }


def renewal_state(data: dict) -> str:
    return _renewal_state(data)


def subscription_active(data: dict, plan: str) -> bool:
    return _subscription_active(data, plan)
