"""浏览器画像定义，对应 Go 的 protocol_BrowserProfile。"""
from __future__ import annotations
import random
from dataclasses import dataclass
from .constants import SEC_CH_UA, SEC_CH_UA_FULL_LIST, CHROME_FULL_VERSION


@dataclass
class BrowserProfile:
    id: str
    user_agent: str
    platform: str
    navigator_platform: str
    platform_version: str
    architecture: str
    bitness: str
    screen_width: int
    screen_height: int
    avail_width: int
    avail_height: int
    color_depth: int
    hardware_concurrency: int
    device_pixel_ratio: float

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{self.platform}"',
        }

    def extended_hints(self) -> dict[str, str]:
        return {
            "sec-ch-ua-arch": f'"{self.architecture}"',
            "sec-ch-ua-bitness": f'"{self.bitness}"',
            "sec-ch-ua-model": '""',
            "sec-ch-ua-full-version": CHROME_FULL_VERSION,
            "sec-ch-ua-platform-version": f'"{self.platform_version}"',
            "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_LIST,
        }


_WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_MACOS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_PROFILES: dict[str, BrowserProfile] = {
    "chrome131-windows-v1": BrowserProfile(
        id="chrome131-windows-v1", user_agent=_WINDOWS_UA,
        platform="Windows", navigator_platform="Win32", platform_version="15.0.0",
        architecture="x86", bitness="64",
        screen_width=1920, screen_height=1080, avail_width=1920, avail_height=1040,
        color_depth=24, hardware_concurrency=8, device_pixel_ratio=1.0,
    ),
    "chrome131-macos-v1": BrowserProfile(
        id="chrome131-macos-v1", user_agent=_MACOS_UA,
        platform="macOS", navigator_platform="MacIntel", platform_version="14.0.0",
        architecture="x86", bitness="64",
        screen_width=1440, screen_height=900, avail_width=1440, avail_height=875,
        color_depth=24, hardware_concurrency=8, device_pixel_ratio=2.0,
    ),
}


def default_browser_profile() -> BrowserProfile:
    return _PROFILES["chrome131-windows-v1"]


def resolve_browser_profile(selection: str) -> BrowserProfile:
    if not selection or selection == "random":
        selection = random.choice(["windows", "macos"])
    key = f"chrome131-{selection}-v1"
    if key not in _PROFILES:
        raise ValueError(f"Unknown browser profile: {selection}")
    return _PROFILES[key]
