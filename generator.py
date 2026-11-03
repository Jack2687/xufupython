"""Pro 20x（菲律宾）支付长链生成逻辑。

HTTP 默认走系统 curl --socks5-hostname（与你手动验证代理的方式一致），
避免 requests/PySocks 偶发拿到错误出口节点。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode, urlparse
from backend import payment_trace as trace

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
STRIPE_INIT_TMPL = "https://api.stripe.com/v1/payment_pages/{cs}/init"
IPINFO_URL = "https://ipinfo.io/json"
PK_FALLBACK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)

DEFAULT_PROXY = {
    "host": "us.1024proxy.io",
    "port": "3000",
    "username": "ywv816441-region-PH",
    "password": "rqtjsety",
    "type": "socks5",
}

COUNTRY_CURRENCY = {
    "US": "USD",
    "PH": "PHP",
    "JP": "JPY",
    "GB": "GBP",
    "EG": "EGP",
    "ID": "IDR",
    "IN": "INR",
    "BR": "BRL",
    "AU": "AUD",
    "CA": "CAD",
    "NZ": "NZD",
    "SG": "SGD",
    "HK": "HKD",
    "KR": "KRW",
    "TW": "TWD",
    "TH": "THB",
    "VN": "VND",
    "MY": "MYR",
    "MX": "MXN",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "AT": "EUR",
    "PT": "EUR",
    "IE": "EUR",
    "FI": "EUR",
    "GR": "EUR",
}


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get((country or "").strip().upper(), "USD")


def align_billing_to_exit(country: str, currency: str, exit_country: str) -> tuple[str, str]:
    """账单国家必须和请求出口国一致，否则 ChatGPT 会 400。"""
    user_country = (country or "").strip().upper()
    user_currency = (currency or "").strip().upper()
    exit_cc = (exit_country or "").strip().upper()
    if not exit_cc:
        return user_country or "PH", user_currency or "PHP"
    return exit_cc, currency_for_country(exit_cc)

# 关键：Web UA 对 Pro 强制 custom（伪长链会转圈）；
# ChatGPT iOS UA 会返回真正 hosted_checkout_session + pay.openai.com。
IOS_USER_AGENT = "ChatGPT/1.2026.196 (iOS 18.5; iPhone17,2; build 2682191628)"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

LogFn = Callable[[str], None] | None


def _find_curl() -> str:
    for name in ("curl.exe", "curl"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("未找到 curl.exe，请确认系统已安装 curl")


def build_proxy_auth(username: str = "", password: str = "") -> str:
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user and not pwd:
        return ""
    return f"{user}:{pwd}"


def parse_proxy_url(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("代理地址不能为空")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError(f"不支持的代理协议: {scheme}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("代理主机不能为空")
    port = parsed.port or (443 if scheme == "https" else 80)
    return {
        "scheme": scheme,
        "host": host,
        "port": str(port),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "url": text,
    }


def normalize_proxy(
    *,
    proxy_host: str = "",
    proxy_port: str | int = "",
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_type: str = "socks5",
    proxy_url: str = "",
) -> dict[str, str]:
    url = (proxy_url or "").strip()
    if url:
        parsed = parse_proxy_url(url)
        kind = "socks5" if parsed["scheme"] in {"socks5", "socks5h"} else "http"
        return {
            "type": kind,
            "host": parsed["host"],
            "port": parsed["port"],
            "username": parsed["username"],
            "password": parsed["password"],
            "url": parsed["url"],
        }
    kind = (proxy_type or "socks5").strip().lower()
    if kind in {"http", "https"}:
        kind = "http"
    else:
        kind = "socks5"
    host = (proxy_host or "").strip()
    port = str(proxy_port or "").strip()
    user = (proxy_username or "").strip()
    pwd = (proxy_password or "").strip()
    if kind == "http":
        auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if (user or pwd) else ""
        built = f"http://{auth}{host}:{port}"
    else:
        built = f"socks5://{host}:{port}"
    return {
        "type": kind,
        "host": host,
        "port": port,
        "username": user,
        "password": pwd,
        "url": built,
    }


def build_curl_proxy_args(proxy: dict[str, str]) -> list[str]:
    from backend.egress import curl_preproxy_args
    host = (proxy.get("host") or "").strip()
    port = str(proxy.get("port") or "").strip()
    if not host or not port:
        raise ValueError("代理主机和端口不能为空")
    if (proxy.get("type") or "socks5") == "http":
        return [*curl_preproxy_args('http'),"-x", proxy.get("url") or f"http://{host}:{port}"]
    curl_preproxy_args('socks5')
    args = ["--socks5-hostname", f"{host}:{port}"]
    auth = build_proxy_auth(proxy.get("username") or "", proxy.get("password") or "")
    if auth:
        args.extend(["-U", auth])
    return args


@trace.curl_call
def curl_request(
    method: str,
    url: str,
    *,
    proxy_host: str = "",
    proxy_port: str | int = "",
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_type: str = "socks5",
    proxy_url: str = "",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, str]:
    """用 curl 走 SOCKS5 或 HTTP 代理发请求。"""
    curl = _find_curl()
    proxy = normalize_proxy(
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_type=proxy_type,
        proxy_url=proxy_url,
    )

    cmd = [
        curl,
        "-sS",
        "-X",
        method.upper(),
        *build_curl_proxy_args(proxy),
        "--max-time",
        str(timeout),
        "-w",
        "\n__HTTP_STATUS__:%{http_code}",
    ]

    for k, v in (headers or {}).items():
        cmd.extend(["-H", f"{k}: {v}"])

    tmp_path = None
    try:
        if json_body is not None:
            cmd.extend(["-H", "Content-Type: application/json"])
            tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".json")
            json.dump(json_body, tmp, ensure_ascii=False)
            tmp.close()
            tmp_path = tmp.name
            cmd.extend(["--data-binary", f"@{tmp_path}"])
        elif form_body is not None:
            cmd.extend(["-H", "Content-Type: application/x-www-form-urlencoded"])
            cmd.extend(["--data", urlencode(form_body)])

        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",timeout=timeout+5)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"curl 失败 (code={proc.returncode}): {err[:400]}")

        out = proc.stdout or ""
        marker = "\n__HTTP_STATUS__:"
        if marker not in out:
            raise RuntimeError(f"curl 输出异常: {out[:300]}")
        body, status_s = out.rsplit(marker, 1)
        return int(status_s.strip()), body
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@trace.direct_probe
def check_ip_direct() -> dict[str, Any]:
    """不走代理，看本机出口（仅用于对照）。"""
    curl = _find_curl()
    proc = subprocess.run(
        [curl, "-sS", "--max-time", "20", IPINFO_URL],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "")[:200]}
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {"error": "本机 ipinfo 解析失败", "raw": (proc.stdout or "")[:200]}
    return {
        "ip": data.get("ip"),
        "country": data.get("country"),
        "city": data.get("city"),
        "org": data.get("org"),
    }


def check_ip_via_proxy(
    *,
    proxy_host: str,
    proxy_port: str | int,
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_type: str = "socks5",
    proxy_url: str = "",
    retries: int = 8,
    require_country: str | None = "PH",
    on_log: LogFn = None,
) -> dict[str, Any]:
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    last: dict[str, Any] = {}
    for i in range(1, max(1, retries) + 1):
        status, body = curl_request(
            "GET",
            IPINFO_URL,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            proxy_type=proxy_type,
            proxy_url=proxy_url,
            headers={"Accept": "application/json"},
            timeout=40,
        )
        if status >= 400:
            raise RuntimeError(f"ipinfo HTTP {status}: {body[:200]}")
        data = json.loads(body)
        last = {
            "ip": data.get("ip"),
            "country": data.get("country"),
            "region": data.get("region"),
            "city": data.get("city"),
            "org": data.get("org"),
            "raw": data,
            "attempt": i,
        }
        cc = str(last.get("country") or "").upper()
        log(
            f"[代理出口 #{i}] IP={last.get('ip')} country={cc} "
            f"city={last.get('city')} org={last.get('org')}"
        )
        if not require_country or cc == require_country.upper():
            return last
        log(f"出口不是 {require_country}，重试换节点…")
    return last


def normalize_access_token(raw: str) -> str:
    """支持直接粘贴 accessToken，或整段 /api/auth/session JSON。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("accessToken 不能为空")

    if text.lower().startswith("bearer "):
        text = text[7:].strip()

    # 整段 session JSON（用户最常见贴法）
    if text.startswith("{") and "accessToken" in text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("粘贴的像 session JSON，但解析失败，请重新复制完整 JSON") from exc
        token = str(data.get("accessToken") or "").strip()
        if not token:
            raise ValueError("JSON 里没有 accessToken 字段")
        text = token

    # 去掉误带的引号
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()

    if not text.startswith("eyJ"):
        raise ValueError(
            "识别到的 token 不像 JWT（应以 eyJ 开头）。"
            "请只贴 accessToken，或粘贴完整 /api/auth/session JSON。"
        )
    return text


def build_checkout_headers(
    access_token: str,
    *,
    client: str = "ios",
    account_id: str = "",
) -> dict[str, str]:
    """client=ios → 真 hosted；client=web → Pro 常被强制 custom。"""
    token = normalize_access_token(access_token)
    mode = (client or "ios").strip().lower()
    if mode == "ios":
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": IOS_USER_AGENT,
            "OAI-Language": "zh-Hans",
            "OpenAI-Client-Type": "native",
            "OpenAI-Client-Name": "chatgpt-ios",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "en-US",
        "User-Agent": WEB_USER_AGENT,
    }


def normalize_plan(plan: str) -> str:
    kind = (plan or "pro").strip().lower()
    if kind in {"team", "chatgptteamplan", "chatgpt_team", "teamplan"}:
        return "team"
    if kind in {"team_premium", "team_prolite", "team_advanced", "premium", "advanced"}:
        return "team_premium"
    if kind in {"pro5x", "prolite", "chatgptprolite", "pro_5x", "pro-5x"}:
        return "pro5x"
    return "pro"


def build_checkout_payload(
    *,
    plan: str = "pro",
    country: str = "PH",
    currency: str = "PHP",
    checkout_ui_mode: str = "hosted",
    entry_point: str | None = None,
    workspace_name: str = "work",
    seat_quantity: int = 2,
    premium_seat_quantity: int = 1,
    price_interval: str = "month",
) -> dict[str, Any]:
    """Pro 20x / Pro 5x / Team 普通席 / Team 高级席 的 checkout 请求体。Team 不带 promo。"""
    payload: dict[str, Any] = {
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": checkout_ui_mode,
        "cancel_url": "https://chatgpt.com/#pricing",
    }
    kind = normalize_plan(plan)
    if kind == "team":
        seats = max(2, int(seat_quantity or 2))
        payload["plan_name"] = "chatgptteamplan"
        payload["team_plan_data"] = {
            "workspace_name": (workspace_name or "work").strip() or "work",
            "price_interval": (price_interval or "month").strip() or "month",
            "seat_quantity": seats,
        }
    elif kind == "team_premium":
        standard = max(2, int(seat_quantity or 2))
        premium = max(1, int(premium_seat_quantity or 1))
        payload["plan_name"] = "chatgptteamplan"
        payload["team_plan_data"] = {
            "workspace_name": (workspace_name or "work").strip() or "work",
            "price_interval": (price_interval or "month").strip() or "month",
            "seat_quantities": [
                {"seat_type": "default", "quantity": standard},
                {"seat_type": "prolite", "quantity": premium},
            ],
        }
    elif kind == "pro5x":
        payload["plan_name"] = "chatgptprolite"
    else:
        payload["plan_name"] = "chatgptpro"
    if entry_point:
        payload["entry_point"] = entry_point
    return payload


def create_pro_checkout(
    access_token: str,
    *,
    proxy_host: str,
    proxy_port: str | int,
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_type: str = "socks5",
    proxy_url: str = "",
    country: str = "PH",
    currency: str = "PHP",
    entry_point: str | None = "pricing_modal",
    checkout_ui_mode: str = "hosted",
    require_cs_live: bool = True,
    client: str = "ios",
    account_id: str = "",
    plan: str = "pro",
    workspace_name: str = "work",
    seat_quantity: int = 2,
    premium_seat_quantity: int = 1,
    price_interval: str = "month",
) -> dict[str, Any]:
    payload = build_checkout_payload(
        plan=plan,
        country=country,
        currency=currency,
        checkout_ui_mode=checkout_ui_mode,
        entry_point=entry_point,
        workspace_name=workspace_name,
        seat_quantity=seat_quantity,
        premium_seat_quantity=premium_seat_quantity,
        price_interval=price_interval,
    )

    status, body = curl_request(
        "POST",
        CHECKOUT_URL,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_type=proxy_type,
        proxy_url=proxy_url,
        headers=build_checkout_headers(
            access_token, client=client, account_id=account_id
        ),
        json_body=payload,
        timeout=60,
    )
    try:
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"checkout 返回非 JSON：HTTP {status} {body[:300]}") from exc

    if status >= 400:
        detail = data.get("detail") or data.get("error") or data
        raise RuntimeError(f"checkout 失败 HTTP {status}: {detail}")

    cs = data.get("checkout_session_id") or ""
    if not cs:
        raise RuntimeError(f"未返回 checkout_session_id：{data}")
    if require_cs_live and not str(cs).startswith("cs_"):
        raise RuntimeError(
            "未拿到 cs_live_ 会话。"
            f"tag={data.get('tag')!r}, ui={data.get('checkout_ui_mode')!r}, "
            f"id={cs!r}。请确认出口是菲律宾，且 token 有效。"
        )
    return data


def build_short_checkout_url(checkout: dict[str, Any], country: str = "PH") -> str:
    cs = str(checkout.get("checkout_session_id") or "").strip()
    if not cs:
        raise ValueError("缺少 checkout_session_id")
    entity = str(checkout.get("processor_entity") or "").strip()
    if not entity:
        entity = "openai_ie" if str(country).upper() in {
            "DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "LU",
            "FI", "GR", "CY", "EE", "LV", "LT", "MT", "SK", "SI", "GB",
        } else "openai_llc"
    return f"https://chatgpt.com/checkout/{entity}/{cs}"


def stripe_init_long_url(
    checkout_session_id: str,
    publishable_key: str | None = None,
    *,
    proxy_host: str,
    proxy_port: str | int,
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_type: str = "socks5",
    proxy_url: str = "",
) -> dict[str, Any]:
    cs = checkout_session_id.strip()
    pk = (publishable_key or PK_FALLBACK).strip()
    status, body = curl_request(
        "POST",
        STRIPE_INIT_TMPL.format(cs=quote(cs, safe="")),
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_type=proxy_type,
        proxy_url=proxy_url,
        headers={
            "Accept": "application/json",
            "Origin": "https://pay.openai.com",
            "Referer": "https://pay.openai.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        },
        form_body={
            "key": pk,
            "eid": "NA",
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Manila",
            "redirect_type": "url",
        },
        timeout=60,
    )
    try:
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"stripe init 非 JSON：HTTP {status} {body[:300]}") from exc

    if status >= 400:
        err = data.get("error") or data
        raise RuntimeError(f"stripe init 失败 HTTP {status}: {err}")

    stripe_hosted = data.get("stripe_hosted_url") or data.get("url") or ""
    if not stripe_hosted:
        hosted = f"https://pay.openai.com/c/pay/{cs}"
    else:
        hosted = str(stripe_hosted).replace(
            "https://checkout.stripe.com/",
            "https://pay.openai.com/",
        )

    product = None
    try:
        product = data["invoice"]["lines"]["data"][0]["description"]
    except Exception:  # noqa: BLE001
        try:
            product = data["invoice"]["lines"]["data"][0]["price"]["product"]["name"]
        except Exception:  # noqa: BLE001
            product = None

    return {
        "hostedUrl": hosted,
        "checkoutSessionId": cs,
        "stripeSessionId": cs,
        "product": product,
        "customerEmail": data.get("customer_email"),
        "currency": data.get("currency"),
        "uiMode": data.get("ui_mode"),
        "successUrl": data.get("success_url"),
        "returnUrl": data.get("return_url"),
        "init": data,
    }


@trace.phase('checkout')
def generate_pro_long_link(
    access_token: str,
    *,
    proxy_host: str = DEFAULT_PROXY["host"],
    proxy_port: str | int = DEFAULT_PROXY["port"],
    proxy_username: str = DEFAULT_PROXY["username"],
    proxy_password: str = DEFAULT_PROXY["password"],
    proxy_type: str = DEFAULT_PROXY["type"],
    proxy_url: str = "",
    country: str = "PH",
    currency: str = "PHP",
    require_ph: bool = True,
    link_mode: str = "long",
    plan: str = "pro",
    workspace_name: str = "work",
    seat_quantity: int = 2,
    premium_seat_quantity: int = 1,
    price_interval: str = "month",
    on_log: LogFn = None,
) -> dict[str, Any]:
    """生成菲律宾 Pro 20x / Pro 5x / Team 支付链接。

    link_mode:
      - long（默认，可支付真长链）：用 ChatGPT iOS UA 调 checkout，拿 hosted + pay.openai.com
      - short：Web custom 短链 chatgpt.com/checkout/.../oaics_...
    """
    proxy = normalize_proxy(
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_type=proxy_type,
        proxy_url=proxy_url,
    )
    mode = (link_mode or "long").strip().lower()
    if mode not in {"short", "long"}:
        raise ValueError("link_mode 只能是 short 或 long")

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    account_id = ""
    raw_in = (access_token or "").strip()
    if raw_in.startswith("{"):
        try:
            account_id = str((json.loads(raw_in).get("account") or {}).get("id") or "")
        except Exception:  # noqa: BLE001
            account_id = ""

    token = normalize_access_token(access_token)
    plan_kind = normalize_plan(plan)
    premium_seats = max(1, int(premium_seat_quantity or 1))
    standard_seats = max(2, int(seat_quantity or 2))
    plan_labels = {
        "team": "Team 普通席",
        "team_premium": "Team 高级席",
        "pro5x": "Pro 5x",
        "pro": "Pro 20x",
    }
    plan_label = plan_labels.get(plan_kind, "Pro 20x")
    if raw_in.startswith("{"):
        log(f"已从 session JSON 提取 accessToken（长度 {len(token)}，前缀 {token[:12]}…）")
    else:
        log(f"已识别 accessToken（长度 {len(token)}，前缀 {token[:12]}…）")
    if plan_kind == "team":
        log(f"套餐=Team 普通席  工作区={workspace_name or 'work'}  席位={standard_seats}  周期={price_interval or 'month'}")
    elif plan_kind == "team_premium":
        log(
            f"套餐=Team 高级席  工作区={workspace_name or 'work'}  "
            f"普通席={standard_seats}  高级席={premium_seats}  周期={price_interval or 'month'}"
        )
    elif plan_kind == "pro5x":
        log("套餐=Pro 5x（chatgptprolite）")
    else:
        log("套餐=Pro 20x")
    result_seats = (
        premium_seats if plan_kind == "team_premium" else standard_seats if plan_kind == "team" else 1
    )
    result_ws = workspace_name if plan_kind in {"team", "team_premium"} else ""

    if proxy["type"] == "http":
        log(f"代理通道: curl -x {proxy['url']}")
    else:
        log(f"代理通道: curl --socks5-hostname {proxy['host']}:{proxy['port']} -U \"{proxy['username'] or ''}:***\"")
    proxy_kw = {
        "proxy_host": proxy["host"],
        "proxy_port": proxy["port"],
        "proxy_username": proxy["username"],
        "proxy_password": proxy["password"],
        "proxy_type": proxy["type"],
        "proxy_url": proxy["url"] if proxy["type"] == "http" else "",
    }
    log("对照：检测本机直连 IP（不走代理）…")
    direct = check_ip_direct()
    if "error" in direct:
        log(f"本机直连检测失败: {direct['error']}")
    else:
        log(
            f"[本机直连] IP={direct.get('ip')} country={direct.get('country')} "
            f"city={direct.get('city')} org={direct.get('org')}"
        )

    log("检测代理出口 IP（DNS 也走代理，可多次重试换节点）…")
    ipinfo = check_ip_via_proxy(
        **proxy_kw,
        retries=10 if require_ph else 1,
        require_country="PH" if require_ph else None,
        on_log=on_log,
    )

    if require_ph and str(ipinfo.get("country") or "").upper() != "PH":
        raise RuntimeError(
            f"代理出口仍不是 PH（最后一次: {ipinfo.get('country')} / {ipinfo.get('ip')}）。"
            "这不是本机 IP；是代理池把你分到了其他国家节点。"
            "请在 1024proxy 后台确认 region-PH，或稍后重试。"
        )

    if (
        proxy["type"] != "http"
        and direct.get("ip")
        and ipinfo.get("ip")
        and direct.get("ip") == ipinfo.get("ip")
    ):
        raise RuntimeError(
            "代理出口 IP 与本机直连 IP 相同，说明代理未生效。请检查主机/端口/账号密码。"
        )
    if proxy["type"] == "http" and direct.get("ip") and ipinfo.get("ip") and direct.get("ip") == ipinfo.get("ip"):
        log("本机直连与 HTTP 代理出口相同，多半系统已走 7890，继续生成。")

    exit_cc = str(ipinfo.get("country") or "").upper()
    aligned_country, aligned_currency = align_billing_to_exit(country, currency, exit_cc)
    if aligned_country != (country or "").strip().upper() or aligned_currency != (currency or "").strip().upper():
        log(f"账单国家按出口对齐: {country}/{currency} → {aligned_country}/{aligned_currency}")
    country, currency = aligned_country, aligned_currency

    common = {
        "ipinfo": {k: ipinfo.get(k) for k in ("ip", "country", "region", "city", "org")},
        "directIp": direct,
        "billingCountry": country,
        "billingCurrency": currency,
    }

    # —— 短链（Web custom）——
    if mode == "short":
        short_entry = (
            "team_workspace_purchase_modal"
            if plan_kind in {"team", "team_premium"}
            else "all_plans_pricing_modal"
        )
        log(f"请求 ChatGPT checkout（短链：Web UA + custom + {short_entry}）…")
        checkout = create_pro_checkout(
            token,
            **proxy_kw,
            country=country,
            currency=currency,
            entry_point=short_entry,
            checkout_ui_mode="custom",
            require_cs_live=False,
            client="web",
            account_id=account_id,
            plan=plan_kind,
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            premium_seat_quantity=premium_seats,
            price_interval=price_interval,
        )
        cs = checkout["checkout_session_id"]
        short_url = build_short_checkout_url(checkout, country)
        log(f"拿到 session: {cs}")
        log(f"tag={checkout.get('tag')!r} ui={checkout.get('checkout_ui_mode')!r}")
        log("[OK] 短链：需在已登录该账号的浏览器中打开，建议仍走 PH 出口")
        log(short_url)
        return {
            "ok": True,
            "linkMode": "short",
            "plan": plan_kind,
            "planLabel": plan_label,
            "seatQuantity": result_seats,
            "workspaceName": result_ws,
            "payable": True,
            "checkoutSessionId": cs,
            "stripeSessionId": cs,
            "hostedUrl": short_url,
            "shortUrl": short_url,
            "uiMode": checkout.get("checkout_ui_mode"),
            "checkoutTag": checkout.get("tag"),
            "checkoutUiMode": checkout.get("checkout_ui_mode"),
            "processorEntity": checkout.get("processor_entity"),
            "steps": [
                {"stage": "validate", "status": "done"},
                {"stage": "checkout", "status": "done"},
            ],
            **common,
        }

    # —— 真长链（ChatGPT iOS UA → hosted_checkout_session）——
    log("请求 ChatGPT checkout（真长链：iOS UA + hosted）…")
    checkout = create_pro_checkout(
        token,
        **proxy_kw,
        country=country,
        currency=currency,
        entry_point=None,
        checkout_ui_mode="hosted",
        require_cs_live=True,
        client="ios",
        account_id=account_id,
        plan=plan_kind,
        workspace_name=workspace_name,
        seat_quantity=seat_quantity,
        premium_seat_quantity=premium_seats,
        price_interval=price_interval,
    )
    cs = checkout["checkout_session_id"]
    direct_url = checkout.get("url") or ""
    log(f"拿到 session: {cs}")
    log(f"tag={checkout.get('tag')!r} ui={checkout.get('checkout_ui_mode')!r}")

    if (
        checkout.get("tag") == "hosted_checkout_session"
        and checkout.get("checkout_ui_mode") == "hosted"
        and isinstance(direct_url, str)
        and "pay.openai.com" in direct_url
    ):
        log("[OK] 接口直接返回真正 hosted 长链（与别人可付链同型）")
        log(direct_url)
        return {
            "ok": True,
            "linkMode": "long",
            "plan": plan_kind,
            "planLabel": plan_label,
            "seatQuantity": result_seats,
            "workspaceName": result_ws,
            "payable": True,
            "checkoutSessionId": cs,
            "stripeSessionId": cs,
            "hostedUrl": direct_url,
            "uiMode": "hosted",
            "checkoutTag": checkout.get("tag"),
            "checkoutUiMode": checkout.get("checkout_ui_mode"),
            "processorEntity": checkout.get("processor_entity"),
            "client": "ios",
            "steps": [
                {"stage": "validate", "status": "done"},
                {"stage": "checkout", "status": "done"},
            ],
            **common,
        }

    log("接口未直接给 url，回退 Stripe payment_pages/init…")
    stripe = stripe_init_long_url(
        cs,
        checkout.get("publishable_key"),
        **proxy_kw,
    )
    ui_mode = str(stripe.get("uiMode") or checkout.get("checkout_ui_mode") or "")
    true_hosted = ui_mode == "hosted" and bool(stripe.get("successUrl"))
    log(f"产品: {stripe.get('product')}")
    log(f"Stripe ui_mode={ui_mode!r} success_url={stripe.get('successUrl')!r}")
    if true_hosted:
        log("[OK] 长链为真正 hosted，可正常支付跳转")
    else:
        log(
            "[FAIL] 仍不是真正 hosted。请确认代理是 PH，并重试；"
            "或改用短链模式。"
        )

    return {
        "ok": True,
        "linkMode": "long",
        "plan": plan_kind,
        "planLabel": plan_label,
        "seatQuantity": result_seats,
        "workspaceName": result_ws,
        "payable": true_hosted,
        "checkoutSessionId": cs,
        "stripeSessionId": cs,
        "hostedUrl": stripe["hostedUrl"],
        "product": stripe.get("product"),
        "customerEmail": stripe.get("customerEmail"),
        "uiMode": ui_mode,
        "successUrl": stripe.get("successUrl"),
        "returnUrl": stripe.get("returnUrl"),
        "checkoutTag": checkout.get("tag"),
        "checkoutUiMode": checkout.get("checkout_ui_mode"),
        "client": "ios",
        "steps": [
            {"stage": "validate", "status": "done"},
            {"stage": "checkout", "status": "done"},
            {"stage": "stripe_init", "status": "done"},
        ],
        **common,
    }


# 兼容旧 app.py 导入名
def build_proxy_url(
    host: str,
    port: str | int,
    username: str = "",
    password: str = "",
    proxy_type: str = "socks5",
) -> str:
    del proxy_type
    auth = build_proxy_auth(username, password)
    if auth:
        user, pwd = auth.split(":", 1)
        return f"socks5h://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
    return f"socks5h://{host}:{port}"


def make_session(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("已改用 curl 通道，请使用 check_ip_via_proxy / generate_pro_long_link")


def check_ip(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("请使用 check_ip_via_proxy")


if __name__ == "__main__":
    import sys

    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        print("用法: python generator.py <accessToken>")
        raise SystemExit(2)

    result = generate_pro_long_link(token, on_log=print)
    print(json.dumps({k: v for k, v in result.items() if k not in ("steps",)}, ensure_ascii=False, indent=2))
    print(result["hostedUrl"])
