"""cs-live 协议常量，直接对应 Go 源码里的常量定义。"""
from __future__ import annotations

CHATGPT_BASE = "https://chatgpt.com"
STRIPE_BASE = "https://api.stripe.com/v1"

# Stripe API 版本
STRIPE_CUSTOM_API_VERSION = "2025-03-31.basil"
STRIPE_CUSTOM_CHECKOUT_API_VERSION = (
    "2025-03-31.basil; custom_checkout_beta=v1; "
    "checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
STRIPE_HOSTED_VERSION = "2026-03-25.dahlia"
STRIPE_CUSTOM_JS_URL = "https://js.stripe.com/basil/stripe.js"

# Sentinel
SENTINEL_FRAME_VERSION = "20260810913b"
SENTINEL_SDK_URL = f"https://chatgpt.com/sentinel/{SENTINEL_FRAME_VERSION}/sdk.js"
SENTINEL_MAX_NONCE = 500_000

# MTree
MTREE_URL_SALT = "7766e861-8279-424d-87a1-07a6022fd8cd"
MTREE_ORIGIN = "https://m.stripe.network"
MTREE_TAG_TEMPLATE = "$npm_package_version"

# sec-ch-ua (Chrome 131)
SEC_CH_UA = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
SEC_CH_UA_FULL_LIST = '"Google Chrome";v="131.0.0.0", "Chromium";v="131.0.0.0", "Not_A Brand";v="24.0.0.0"'
CHROME_FULL_VERSION = '"131.0.0.0"'

# 套餐定义：(id, checkout_plan_name, hosted_plan_type, is_team, subscription_types_set)
PLAN_DEFINITIONS = [
    ("go",     "chatgptgoplan",    "go",       False, {"go", "go_plan", "chatgptgo", "chatgptgoplan"}),
    ("plus",   "chatgptplusplan",  "basic",    False, {"basic", "plus", "basic_plan", "chatgptplusplan"}),
    ("pro5x",  "chatgptprolite",   "standard", False, {"standard", "prolite", "standard_plan", "pro5", "pro5x", "chatgptprolite"}),
    ("pro20x", "chatgptpro",       "premium",  False, {"premium", "pro", "premium_plan", "pro20", "pro20x", "chatgptpro"}),
    ("promax", "chatgptpromax",    "promax",   False, {"promax", "promax_plan", "pro_max", "pro_max_plan", "chatgptpromax"}),
    ("team",   "chatgptteamplan",  "team",     True,  {"team", "team_plan", "chatgptteam", "chatgptteamplan", "default"}),
    ("team5x", "chatgptteamplan",  "team",     True,  {"team5x", "team_5x", "prolite", "chatgptprolite", "chatgptteamplan", "team"}),
]

# 地区：country_code -> (currency, timezone)
REGION_DEFINITIONS: dict[str, tuple[str, str]] = {
    "CL": ("CLP", "America/Santiago"),
    "PH": ("PHP", "Asia/Manila"),
    "US": ("USD", "America/New_York"),
    "JP": ("JPY", "Asia/Tokyo"),
    "EG": ("EGP", "Africa/Cairo"),
    "NR": ("AUD", "Pacific/Nauru"),
    "SG": ("SGD", "Asia/Singapore"),
    "TR": ("TRY", "Europe/Istanbul"),
    "GB": ("GBP", "Europe/London"),
}

# Sentinel 时区标签 (standard_label, daylight_label)
SENTINEL_TIMEZONE_LABELS: dict[str, tuple[str, str]] = {
    "Asia/Manila":         ("Philippine Standard Time",        "Philippine Standard Time"),
    "America/New_York":    ("Eastern Standard Time",           "Eastern Daylight Time"),
    "Asia/Singapore":      ("Singapore Standard Time",         "Singapore Standard Time"),
    "Asia/Tokyo":          ("Japan Standard Time",             "Japan Standard Time"),
    "Asia/Seoul":          ("Korean Standard Time",            "Korean Standard Time"),
    "Europe/London":       ("Greenwich Mean Time",             "British Summer Time"),
    "Europe/Berlin":       ("Central European Standard Time",  "Central European Summer Time"),
    "Europe/Paris":        ("Central European Standard Time",  "Central European Summer Time"),
    "Europe/Amsterdam":    ("Central European Standard Time",  "Central European Summer Time"),
    "Europe/Dublin":       ("Greenwich Mean Time",             "Irish Standard Time"),
    "Asia/Kolkata":        ("India Standard Time",             "India Standard Time"),
    "Australia/Sydney":    ("Australian Eastern Standard Time","Australian Eastern Daylight Time"),
    "America/Toronto":     ("Eastern Standard Time",           "Eastern Daylight Time"),
    "Asia/Hong_Kong":      ("Hong Kong Standard Time",         "Hong Kong Standard Time"),
    "Asia/Taipei":         ("Taipei Standard Time",            "Taipei Standard Time"),
    "Asia/Kuala_Lumpur":   ("Malaysia Time",                   "Malaysia Time"),
    "Asia/Bangkok":        ("Indochina Time",                  "Indochina Time"),
    "Asia/Ho_Chi_Minh":    ("Indochina Time",                  "Indochina Time"),
    "Asia/Jakarta":        ("Western Indonesia Time",          "Western Indonesia Time"),
    "America/Sao_Paulo":   ("Brasilia Standard Time",          "Brasilia Summer Time"),
    "America/Mexico_City": ("Central Standard Time",           "Central Daylight Time"),
    "UTC":                 ("Coordinated Universal Time",      "Coordinated Universal Time"),
}

STRIPE_PLUGINS_WITH_MIME = ", ".join([
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
])


def checkout_plan(plan_id: str) -> str:
    for p in PLAN_DEFINITIONS:
        if p[0] == plan_id.lower():
            return p[1]
    return ""


def hosted_plan_type(plan_id: str) -> str:
    for p in PLAN_DEFINITIONS:
        if p[0] == plan_id.lower():
            return p[2]
    return ""


def is_team_plan(plan_id: str) -> bool:
    for p in PLAN_DEFINITIONS:
        if p[0] == plan_id.lower():
            return p[3]
    return False


def matches_subscription_type(plan_id: str, value: str) -> bool:
    for p in PLAN_DEFINITIONS:
        if p[0] == plan_id.lower():
            return value in p[4]
    return False


def lookup_region(country: str) -> tuple[str, str] | None:
    return REGION_DEFINITIONS.get(country.upper())
