// Extracted from xupay/internal/cslive/protocol_browser.go; see SOURCE.json.
package main

import (
	fmt "fmt"
)

const protocol_UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

// BrowserProfile 是整笔交易共用的浏览器画像，与程序所在主机无关。
// 已发布的 ID 对应字段不可更改；调整画像时必须使用新版本 ID，以保证恢复一致。
type protocol_BrowserProfile struct {
	ID, UserAgent, Platform, NavigatorPlatform, PlatformVersion, Architecture, Bitness string
	ScreenWidth, ScreenHeight, AvailWidth, AvailHeight, ColorDepth                     int
	HardwareConcurrency                                                                int
	DevicePixelRatio                                                                   float64
}

// DefaultBrowserProfile 返回固定的 Windows 基线，供组件初始化与对照测试使用。
// 新交易的默认随机选择通过 ResolveBrowserProfile 完成。
func protocol_DefaultBrowserProfile() protocol_BrowserProfile {
	return protocol_BrowserProfile{
		ID: "chrome131-windows-v1", UserAgent: protocol_UserAgent,
		Platform: "Windows", NavigatorPlatform: "Win32", PlatformVersion: "15.0.0",
		Architecture: "x86", Bitness: "64", ScreenWidth: 1920, ScreenHeight: 1080,
		AvailWidth: 1920, AvailHeight: 1040, ColorDepth: 24,
		HardwareConcurrency: 8, DevicePixelRatio: 1,
	}
}

// Chrome 131 的真实品牌顺序：GREASE 值在最后，第二个品牌不带版本。
// 传输层默认头与业务请求头都从这里取，避免同一画像出现两组 sec-ch-ua。
const (
	protocol_secCHUA           = `"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"`
	protocol_secCHUAFullList   = `"Google Chrome";v="131.0.0.0", "Chromium";v="131.0.0.0", "Not_A Brand";v="24.0.0.0"`
	protocol_chromeFullVersion = `"131.0.0.0"`
)

func (p protocol_BrowserProfile) Headers() protocol_Fields {
	return protocol_O("User-Agent", p.UserAgent, "sec-ch-ua", protocol_secCHUA, "sec-ch-ua-mobile", "?0", "sec-ch-ua-platform", fmt.Sprintf("%q", p.Platform))
}

func (p protocol_BrowserProfile) ExtendedHints() protocol_Fields {
	return protocol_O("sec-ch-ua-arch", fmt.Sprintf("%q", p.Architecture), "sec-ch-ua-bitness", fmt.Sprintf("%q", p.Bitness), "sec-ch-ua-model", `""`, "sec-ch-ua-full-version", protocol_chromeFullVersion, "sec-ch-ua-platform-version", fmt.Sprintf("%q", p.PlatformVersion), "sec-ch-ua-full-version-list", protocol_secCHUAFullList)
}
