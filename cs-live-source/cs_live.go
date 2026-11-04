// cs_live.go：明文单文件源码，不包含启动器、源码压缩包或原仓库导入。
//
// 依赖 Go 1.25+，面向 Linux amd64 / macOS arm64，CGO_ENABLED=0。
// 将 cs_live.go 与 go.mod、go.sum 放入独立目录，执行：
//   go mod download
//   go run cs_live.go --help
// 提链（只创建订单、不付款）：
//   go run cs_live.go create-checkout --country PH --plan plus --session data/session.json
// 支付（新建另一笔订单并付款，不复用上次提链订单）：
//   go run cs_live.go pay --country PH --plan plus --session data/session.json --billing data/billing.json --renewal keep
// 手动人机交接（浏览器继续验证原付款，不重提卡片/付款）：
//   go run cs_live.go payment-action --recover data/payment-EXAMPLE.enc --tls-cert localhost.pem --tls-key localhost-key.pem
// 核验原交易：go run cs_live.go pay --recover data/payment-EXAMPLE.enc
// pay 同时传 --tls-cert / --tls-key 时，遇到待验证付款自动生成本机 HTTPS 入口。
// 证书需被浏览器信任并包含 127.0.0.1；不传证书仅为 HTTP 调试入口。
// 验证链接只在本机可用，10 分钟有效；浏览器不沿用命令行代理。
// 浏览器完成通知不是扣款证据，最终 payment 来自原交易查询。
//
// 新操作默认请求 hosted；响应仍按 Custom/Hosted 模式分流。
// custom + cs_live 使用 Stripe payment_pages，不使用 oaics 确认接口。
// 可传 --expected-amount 限制最终含税金额（最小币种单位）。
// 默认隐藏输入 Session 与卡片，不使用命令行参数传卡片。
// billing.json 只有 name、line1、city、state、postal_code、country 六个字段。
// data/.checkpoint-key 与恢复文件需一起保留；转发源码时不要附带 data/。
//
// 所有原项目顶层符号带原包名前缀（model_、protocol_、gptpay_ 等）避免冲突。
// Go 集成可在同包调用 gptpay_NewPaymentClient、model_DefaultConfig 等符号；
// 手动验证入口为 paymentaction_Serve / paymentaction_ServeTLS。
// 文件内按原模块分段，保留依赖的支付编排、持久化、防重和人机交接实现。
// 本次未运行测试、真实接口或付款。
//go:build linux || darwin

package main

import (
	bufio "bufio"
	bytes "bytes"
	flate "compress/flate"
	gzip "compress/gzip"
	zlib "compress/zlib"
	context "context"
	aes "crypto/aes"
	cipher "crypto/cipher"
	hmac "crypto/hmac"
	rand "crypto/rand"
	sha256 "crypto/sha256"
	tls "crypto/tls"
	x509 "crypto/x509"
	base64 "encoding/base64"
	binary "encoding/binary"
	hex "encoding/hex"
	json "encoding/json"
	xml "encoding/xml"
	errors "errors"
	flag "flag"
	fmt "fmt"
	brotli "github.com/andybalholm/brotli"
	fhttp "github.com/bogdanfinn/fhttp"
	fhttpcookiejar "github.com/bogdanfinn/fhttp/cookiejar"
	fhttptrace "github.com/bogdanfinn/fhttp/httptrace"
	tls_client "github.com/bogdanfinn/tls-client"
	profiles "github.com/bogdanfinn/tls-client/profiles"
	regexp2 "github.com/dlclark/regexp2"
	uuid "github.com/google/uuid"
	zstd "github.com/klauspost/compress/zstd"
	scrypt "golang.org/x/crypto/scrypt"
	html "golang.org/x/net/html"
	proxy "golang.org/x/net/proxy"
	publicsuffix "golang.org/x/net/publicsuffix"
	unix "golang.org/x/sys/unix"
	term "golang.org/x/term"
	cases "golang.org/x/text/cases"
	language "golang.org/x/text/language"
	dep_html "html"
	io "io"
	math "math"
	big "math/big"
	dep_math_rand_v2 "math/rand/v2"
	mime "mime"
	net "net"
	http "net/http"
	url "net/url"
	os "os"
	signal "os/signal"
	filepath "path/filepath"
	reflect "reflect"
	regexp "regexp"
	runtime "runtime"
	sort "sort"
	strconv "strconv"
	strings "strings"
	sync "sync"
	time "time"
	tzdata "time/tzdata"
	unicode "unicode"
	utf16 "unicode/utf16"
	utf8 "unicode/utf8"
)

// ---- internal/challenge/client.go ----

// Sentinel 只生成绑定本次 flow 的证明，不跨交易缓存令牌。
type challenge_Sentinel struct {
	Profile  protocol_BrowserProfile
	http     protocol_HTTPTransport
	cp       *model_Checkpoint
	clock    fingerprint_Clock
	observer *observability_Observer
}

func challenge_NewSentinel(http protocol_HTTPTransport, cp *model_Checkpoint, clock fingerprint_Clock, observer *observability_Observer) *challenge_Sentinel {
	return &challenge_Sentinel{Profile: protocol_DefaultBrowserProfile(), http: http, cp: cp, clock: clock, observer: observer}
}

func (s *challenge_Sentinel) Token(flow string) (token [2]string, err error) {
	started := time.Now()
	defer func() {
		outcome := "succeeded"
		if err != nil {
			outcome = "failed"
		}
		s.observer.Emit("sentinel", protocol_Map{"flow": flow, "executor": "local_vm", "outcome": outcome, "error_code": protocol_FlowErrorCode(err), "duration_ms": time.Since(started).Seconds() * 1000})
	}()
	token, err = s.local(flow)
	var ve *sentinel_Error
	if errors.As(err, &ve) {
		err = protocol_NewError(ve.Code)
	}
	return
}

func (s *challenge_Sentinel) local(flow string) ([2]string, error) {
	cp := s.cp
	profile := s.Profile
	deadline := s.http.Deadline()
	check := func() error {
		if !time.Now().Before(deadline) {
			return protocol_NewError("deadline_exceeded")
		}
		return nil
	}
	if e := check(); e != nil {
		return [2]string{}, e
	}
	generator := sentinel_NewGenerator(cp.DeviceID, profile, "en-US", cp.Timezone, s.clock)
	requirements, e := generator.RequirementsToken()
	if e != nil {
		return [2]string{}, e
	}
	headers := profile.Headers()
	headers.Update(protocol_O("Authorization", "Bearer "+cp.AccessToken, "Cookie", cp.Cookies, "oai-device-id", cp.DeviceID, "Accept", "*/*", "Content-Type", "text/plain;charset=UTF-8", "Origin", chatgpt_PlatformBase, "Referer", chatgpt_PlatformBase+"/backend-api/sentinel/frame.html?sv="+sentinel_FrameVersion))
	body, e := protocol_CompactJSON(protocol_O("p", requirements, "id", cp.DeviceID, "flow", flow))
	if e != nil {
		return [2]string{}, e
	}
	response, e := s.http.Request("POST", chatgpt_PlatformBase+"/backend-api/sentinel/req", protocol_RequestOptions{Headers: headers, Text: &body})
	if e != nil {
		return [2]string{}, e
	}
	data, e := protocol_JSONResponse(response, false)
	if e != nil {
		return [2]string{}, e
	}
	if e = check(); e != nil {
		return [2]string{}, e
	}
	challenge := protocol_FirstTruthy(data["token"], data["c"])
	var pow protocol_Map
	for _, k := range []string{"proofofwork", "proofOfWork", "proof_of_work"} {
		if m, ok := data[k].(protocol_Map); ok {
			pow = m
			break
		}
	}
	turnstile := protocol_AsMap(protocol_FirstTruthy(data["turnstile"], data["Turnstile"]))
	proof := requirements
	t := ""
	if !protocol_Truthy(challenge) {
		return [2]string{}, protocol_NewError("sentinel_challenge_missing")
	}
	if pow["required"] == true {
		if !protocol_Truthy(pow["seed"]) {
			return [2]string{}, protocol_NewError("sentinel_pow_missing")
		}
		proof, e = generator.PowToken(protocol_AsString(pow["seed"]), protocol_AsString(protocol_FirstTruthy(pow["difficulty"], "0")), deadline)
		if e != nil {
			return [2]string{}, e
		}
	}
	required := turnstile["required"] == true
	if required {
		if !protocol_Truthy(turnstile["dx"]) {
			return [2]string{}, protocol_NewError("sentinel_dx_missing")
		}
		t, e = sentinel_SolveTurnstileDX(protocol_AsString(turnstile["dx"]), requirements, sentinel_Options{Deadline: deadline, Profile: profile, Cookies: cp.Cookies, Clock: s.clock, Bootstrap: protocol_Map{"session": protocol_Map{"account": protocol_Map{"id": cp.AccountID}}, "account": protocol_Map{"id": cp.AccountID}}})
		if e != nil {
			return [2]string{}, e
		}
	}
	payload := protocol_O("p", proof, "t", t, "c", challenge, "id", cp.DeviceID, "flow", flow)
	if e = s.Validate(protocol_AsMap(payload), flow, required); e != nil {
		return [2]string{}, e
	}
	encoded, e := protocol_CompactJSON(payload)
	if e != nil {
		return [2]string{}, e
	}
	timing := chatgpt_Telemetry()
	if e = check(); e != nil {
		return [2]string{}, e
	}
	return [2]string{encoded, timing}, nil
}

func (s *challenge_Sentinel) Validate(payload protocol_Map, flow string, requiredT bool) error {
	for _, k := range []string{"p", "c", "id", "flow"} {
		if !protocol_Truthy(payload[k]) {
			return protocol_NewError("sentinel_incomplete")
		}
	}
	if payload["flow"] != flow || payload["id"] != s.cp.DeviceID {
		return protocol_NewError("sentinel_context_mismatch")
	}
	t := protocol_AsString(payload["t"])
	if requiredT && t == "" {
		return protocol_NewError("sentinel_turnstile_missing")
	}
	decoded, _ := base64.StdEncoding.DecodeString(t + strings.Repeat("=", (4-len(t)%4)%4))
	if strings.HasPrefix(string(decoded), "0: ") || strings.Contains(string(decoded), "SyntaxError") {
		return protocol_NewError("sentinel_turnstile_error")
	}
	return nil
}

// ---- internal/chatgpt/client.go ----

const chatgpt_PlatformBase = "https://chatgpt.com"

type chatgpt_Platform struct {
	// PromoCode 仅存在于本次提链调用，不写支付快照。
	PromoCode string
	Profile   protocol_BrowserProfile
	HTTP      protocol_HTTPTransport
	CP        *model_Checkpoint
	Clock     fingerprint_Clock
}

type chatgpt_PlatformHeaderOptions struct{ Referer, Mode string }

type chatgpt_PlatformCallOptions struct {
	Body    any
	Headers protocol_Fields
	Mode    string
}

func chatgpt_NewPlatform(transport protocol_HTTPTransport, cp *model_Checkpoint, clock fingerprint_Clock) *chatgpt_Platform {
	if cp.Metadata == nil {
		cp.Metadata = protocol_Map{}
	}
	if cp.Cookies == "" {
		cp.Cookies = "__Secure-platform.session-token=" + cp.SessionToken + "; __Secure-next-auth.session-token=" + cp.SessionToken + "; __Secure-platform.callback-url=https%3A%2F%2Fchatgpt.com%2F; __Secure-next-auth.callback-url=https%3A%2F%2Fchatgpt.com%2F"
	}
	if _, exists := cp.Metadata["session_id"]; !exists {
		cp.Metadata["session_id"] = protocol_RandomUUID()
	}
	return &chatgpt_Platform{HTTP: transport, CP: cp, Clock: clock, Profile: protocol_DefaultBrowserProfile()}
}

func chatgpt_Observation() string {
	return "v1.r.p." + protocol_RandomAlphabet(10, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
}

func chatgpt_telemetryInt(low, high int) int {
	n, err := rand.Int(rand.Reader, big.NewInt(int64(high-low+1)))
	if err != nil {
		panic("random source unavailable")
	}
	return low + int(n.Int64())
}

func chatgpt_Telemetry() string {
	value := []any{1, float64(chatgpt_telemetryInt(3800, 16000)) / 10, chatgpt_telemetryInt(3, 12), chatgpt_telemetryInt(80, 420), chatgpt_telemetryInt(18, 90), chatgpt_telemetryInt(1, 6), 0, chatgpt_telemetryInt(420, 1500)}
	result, _ := protocol_CompactJSON(value)
	return result
}

func (p *chatgpt_Platform) Absorb(response *protocol_HTTPResponse) {
	if response == nil {
		return
	}
	request := &http.Request{Header: http.Header{"Cookie": []string{p.CP.Cookies}}}
	jar := protocol_Fields{}
	for _, cookie := range request.Cookies() {
		jar.Set(cookie.Name, cookie.Value)
	}
	parsed := (&http.Response{Header: response.Headers}).Cookies()
	for _, cookie := range parsed {
		jar.Set(cookie.Name, cookie.Value)
	}
	var parts []string
	for _, entry := range jar {
		parts = append(parts, entry.Key+"="+protocol_AsString(entry.Value))
	}
	p.CP.Cookies = strings.Join(parts, "; ")
	if value := jar.Get("oai-did"); value != nil {
		p.CP.DeviceID = protocol_AsString(value)
	}
}

// EnsureDeviceID 供现成 Hosted 长链使用：不访问 ChatGPT 首页，只从已有 Cookie 或新 UUID 补齐设备标识。
func (p *chatgpt_Platform) EnsureDeviceID() {
	if p.CP.DeviceID == "" {
		req := &http.Request{Header: http.Header{"Cookie": []string{p.CP.Cookies}}}
		for _, cookie := range req.Cookies() {
			if cookie.Name == "oai-did" && cookie.Value != "" {
				p.CP.DeviceID = cookie.Value
				break
			}
		}
	}
	if p.CP.DeviceID == "" {
		p.CP.DeviceID = protocol_RandomUUID()
	}
	if p.CP.DeviceID != "" && !strings.Contains(p.CP.Cookies, "oai-did=") {
		if p.CP.Cookies != "" {
			p.CP.Cookies += "; "
		}
		p.CP.Cookies += "oai-did=" + p.CP.DeviceID
	}
}

func (p *chatgpt_Platform) Headers(path string, options ...chatgpt_PlatformHeaderOptions) protocol_Fields {
	opts := chatgpt_PlatformHeaderOptions{Referer: chatgpt_PlatformBase + "/", Mode: "payment"}
	if len(options) > 0 {
		if options[0].Referer != "" {
			opts.Referer = options[0].Referer
		}
		if options[0].Mode != "" {
			opts.Mode = options[0].Mode
		}
	}
	cp := p.CP
	h := protocol_O("Authorization", "Bearer "+cp.AccessToken, "Cookie", cp.Cookies, "User-Agent", p.Profile.UserAgent, "Accept", "*/*", "Accept-Language", "en-US,en;q=0.9", "Content-Type", "application/json", "Origin", chatgpt_PlatformBase, "Referer", opts.Referer, "oai-device-id", cp.DeviceID, "oai-language", "en-US", "oai-session-id", cp.Metadata["session_id"])
	for _, pair := range [][2]string{{"client_version", "oai-client-version"}, {"build", "oai-client-build-number"}, {"attestation", "oai-web-deployment-attestation"}} {
		if protocol_Truthy(cp.Metadata[pair[0]]) {
			h.Set(pair[1], cp.Metadata[pair[0]])
		}
	}
	target := path
	if strings.HasSuffix(path, "/taxes") || strings.HasSuffix(path, "/snapshot") {
		target = strings.ReplaceAll(path, "/backend-api", "/api/v1")
	}
	if opts.Mode == "renewal" {
		target = strings.ReplaceAll(path, "/backend-api", "/api/v1")
		h.Update(protocol_O("Accept", "application/json", "chatgpt-account-id", cp.AccountID, "x-openai-is-client-observation", chatgpt_Observation()))
	} else {
		h.Update(protocol_O("x-oai-is-client-observation", chatgpt_Observation(), "x-oai-is-pending-updates", `{"v":3,"updates":[]}`))
		if path != "/backend-api/subscriptions" {
			h.Update(p.Profile.Headers())
		}
		if !strings.HasSuffix(path, "/confirm") && !strings.HasSuffix(path, "/approve") && path != "/backend-api/payments/checkout" {
			h.Update(protocol_O("sec-fetch-dest", "empty", "sec-fetch-mode", "cors", "sec-fetch-site", "same-origin", "priority", "u=1, i"))
			if path != "/backend-api/subscriptions" {
				h.Update(p.Profile.ExtendedHints())
			}
		}
		if strings.Contains(path, "/payments/checkout") && (!(strings.HasSuffix(path, "/taxes") || strings.HasSuffix(path, "/update")) || cp.Plan != "plus") {
			h.Update(protocol_O("chatgpt-account-id", cp.AccountID, "platform-account-id", cp.AccountID))
		}
	}
	if strings.Contains(path, "/payments/") || strings.Contains(path, "/subscriptions") {
		h.Update(protocol_O("x-openai-target-path", target, "x-openai-target-route", target))
	}
	// 刷新、订阅查询和续费路径也必须覆盖传输层默认平台提示。
	h.Update(p.Profile.Headers())
	return h
}

func (p *chatgpt_Platform) Call(method, path string, options ...chatgpt_PlatformCallOptions) (protocol_Map, error) {
	opts := chatgpt_PlatformCallOptions{}
	if len(options) > 0 {
		opts = options[0]
	}
	route, err := url.Parse(path)
	if err != nil {
		return nil, &protocol_ProtocolError{Code: "invalid_request_url"}
	}
	headers := opts.Headers
	if len(headers) == 0 {
		headers = p.Headers(route.Path, chatgpt_PlatformHeaderOptions{Mode: opts.Mode})
	}
	response, err := p.HTTP.Request(method, chatgpt_PlatformBase+path, protocol_RequestOptions{Headers: headers, Body: opts.Body})
	if err != nil {
		return nil, err
	}
	p.Absorb(response)
	if chatgpt_isSubscriptionNotFound(method, route.EscapedPath(), response) {
		return nil, protocol_NewError("subscription_not_found")
	}
	return protocol_JSONResponse(response, false)
}

func (p *chatgpt_Platform) Refresh() error {
	// 付款成功的瞬间，OpenAI 会轮换账号会话，/api/auth/session?refresh=true 可能短暂
	// 返回不含 accessToken 的 200；稍后重试即可拿到带新套餐的令牌。故对“空令牌”这类
	// 可恢复情况做有限次重试+等待；session_token 缺失或账号不符属不可恢复，立即返回。
	var last error
	for attempt := 0; attempt < 4; attempt++ {
		if attempt > 0 {
			if e := p.HTTP.Pause(3); e != nil {
				return e
			}
		}
		e := p.refreshOnce()
		if e == nil {
			return nil
		}
		last = e
		if protocol_FlowErrorCode(e) != "session_expired" {
			return e
		}
	}
	return last
}

func (p *chatgpt_Platform) refreshOnce() error {
	cp := p.CP
	if cp.SessionToken == "" {
		return &protocol_ProtocolError{Code: "session_token_required"}
	}
	h := p.Headers("/api/auth/session", chatgpt_PlatformHeaderOptions{Mode: "renewal"})
	h.Delete("Authorization")
	h.Update(protocol_O("x-openai-target-path", "/api/auth/session", "x-openai-target-route", "/api/auth/session"))
	data, err := p.Call("GET", "/api/auth/session?refresh=true&reason=token_expired", chatgpt_PlatformCallOptions{Headers: h})
	if err != nil {
		return err
	}
	token := protocol_AsString(data["accessToken"])
	if token == "" || protocol_Truthy(data["error"]) {
		return &protocol_ProtocolError{Code: "session_expired"}
	}
	account := protocol_AsString(protocol_AsMap(data["account"])["id"])
	if account != "" && cp.AccountID != "" && account != cp.AccountID {
		return &protocol_ProtocolError{Code: "account_mismatch"}
	}
	cp.AccessToken = token
	if cp.AccountID == "" {
		cp.AccountID = account
	}
	if cp.Email == "" {
		cp.Email = protocol_AsString(protocol_AsMap(data["user"])["email"])
	}
	return nil
}

func (p *chatgpt_Platform) Homepage() error {
	h := p.Headers("")
	h.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	response, err := p.HTTP.Request("GET", chatgpt_PlatformBase+"/", protocol_RequestOptions{Headers: h})
	if err != nil {
		return err
	}
	p.Absorb(response)
	if response.StatusCode != 200 {
		return &protocol_ProtocolError{Code: "homepage_unavailable"}
	}
	html := string(response.Content)
	for _, pair := range [][2]string{{"client_version", `data-build="([^"]+)"`}, {"build", `data-seq="([^"]+)"`}, {"attestation", `"webDeploymentAttestation":"([^"]+)"`}, {"session_id", `"sessionId":"([^"]+)"`}} {
		if match := regexp.MustCompile(pair[1]).FindStringSubmatch(html); len(match) > 1 {
			p.CP.Metadata[pair[0]] = match[1]
		}
	}
	scripts := []any{}
	seen := map[string]bool{}
	base, _ := url.Parse(chatgpt_PlatformBase + "/")
	for _, match := range regexp.MustCompile(`(?:src|href)=["']([^"']+\.js(?:\?[^"']*)?)["']`).FindAllStringSubmatch(html, -1) {
		relative, err := url.Parse(match[1])
		if err != nil {
			continue
		}
		script := base.ResolveReference(relative).String()
		if !seen[script] {
			seen[script] = true
			scripts = append(scripts, script)
			if len(scripts) == 24 {
				break
			}
		}
	}
	p.CP.Metadata["scripts"] = scripts
	if p.CP.DeviceID == "" {
		return &protocol_ProtocolError{Code: "device_id_missing"}
	}
	return nil
}

func (p *chatgpt_Platform) Account() (protocol_Map, error) {
	offset, err := p.Clock.TimezoneOffsetMin(p.CP.Timezone)
	if err != nil {
		return nil, err
	}
	data, err := p.Call("GET", "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min="+strconv.Itoa(offset))
	if err != nil {
		return nil, err
	}
	for _, value := range protocol_AsMap(data["accounts"]) {
		entry := protocol_AsMap(value)
		account := protocol_AsMap(entry["account"])
		if protocol_AsString(protocol_FirstTruthy(account["id"], account["account_id"])) == p.CP.AccountID {
			kind := strings.ToLower(protocol_AsString(account["account_type"]))
			if kind == "team" || kind == "business" || kind == "enterprise" {
				return nil, &protocol_ProtocolError{Code: "personal_account_required"}
			}
			return protocol_Map{"account": account, "entitlement": protocol_AsMap(entry["entitlement"])}, nil
		}
	}
	return nil, &protocol_ProtocolError{Code: "account_not_found"}
}

func (p *chatgpt_Platform) Checkout(planName string, token [2]string, uiMode string) (protocol_Map, error) {
	if uiMode == "" {
		uiMode = "redirect"
	}
	path := "/backend-api/payments/checkout"
	// iOS 原生客户端提链：OpenAI 对 native 客户端返回“真 hosted”结账（http 直连付款可付），
	// 而 web 客户端会被强制为 custom(dahlia) 型长链，付款必须走浏览器 hCaptcha/3DS。
	// 这里复用 cs_live 现成的过-Cloudflare 传输（tls_client 指纹），仅把客户端标识改成
	// ChatGPT iOS App，并按参考实现省略 Sentinel token（原生端不要求）。
	h := p.iosCheckoutHeaders()
	body := protocol_O("plan_name", planName, "billing_details", protocol_O("country", p.CP.Country, "currency", p.CP.Currency), "checkout_ui_mode", uiMode, "cancel_url", chatgpt_PlatformBase+"/#pricing")
	if uiMode != "hosted" {
		body.Set("entry_point", "all_plans_pricing_modal")
	}
	if model_IsTeamPlan(p.CP.Plan) {
		cfg := p.CP.TeamSeatConfig()
		body.Set("team_plan_data", protocol_O("workspace_name", "Team", "quantity", cfg.Total(), "seat_quantity", cfg.Total(), "price_interval", cfg.Interval, "seat_quantities", cfg.Quantities()))
		if p.PromoCode != "" {
			body.Set("promo_code", p.PromoCode)
		}
	}
	return p.Call("POST", path, chatgpt_PlatformCallOptions{Headers: h, Body: body})
}

// iosCheckoutHeaders 复刻参考实现(generator.py build_checkout_headers client=ios)的
// ChatGPT iOS 原生客户端请求头。OpenAI 据此返回“真 hosted”结账而非 custom(dahlia)。
// 仍复用 cs_live 的传输层(tls_client 过 Cloudflare)与本单设备身份/Cookie。
func (p *chatgpt_Platform) iosCheckoutHeaders() protocol_Fields {
	cp := p.CP
	h := protocol_O(
		"Authorization", "Bearer "+cp.AccessToken,
		"Accept", "application/json",
		"User-Agent", chatgpt_iOSUserAgent,
		"OAI-Language", "zh-Hans",
		"OpenAI-Client-Type", "native",
		"OpenAI-Client-Name", "chatgpt-ios",
		"Content-Type", "application/json",
		"oai-device-id", cp.DeviceID,
	)
	if cp.Cookies != "" {
		h.Set("Cookie", cp.Cookies)
	}
	if cp.AccountID != "" {
		h.Set("ChatGPT-Account-ID", cp.AccountID)
		h.Set("chatgpt-account-id", cp.AccountID)
		h.Set("platform-account-id", cp.AccountID)
	}
	h.Set("x-openai-target-path", "/backend-api/payments/checkout")
	h.Set("x-openai-target-route", "/backend-api/payments/checkout")
	return h
}

const chatgpt_iOSUserAgent = "ChatGPT/1.2026.196 (iOS 18.5; iPhone17,2; build 2682191628)"

func (p *chatgpt_Platform) ChallengeHeaders(path string, token [2]string, referer string, split bool) (protocol_Fields, error) {
	h := p.Headers(path, chatgpt_PlatformHeaderOptions{Referer: referer})
	telemetry := token[1]
	if !split {
		telemetry = chatgpt_Telemetry()
	}
	h.Update(protocol_O("openai-sentinel-token", token[0], "oai-telemetry", telemetry))
	if split {
		var payload protocol_Map
		if err := json.Unmarshal([]byte(token[0]), &payload); err != nil {
			return nil, &protocol_ProtocolError{Code: "invalid_sentinel_token"}
		}
		for _, pair := range [][2]string{{"p", "OpenAI-Sentinel-Proof-Token"}, {"t", "OpenAI-Sentinel-Turnstile-Token"}} {
			if protocol_Truthy(payload[pair[0]]) {
				h.Set(pair[1], payload[pair[0]])
			}
		}
	}
	return h, nil
}

func (p *chatgpt_Platform) Taxes(billing model_Billing) (protocol_Map, error) {
	if p.CP.Entity != "openai_llc" {
		return nil, &protocol_ProtocolError{Code: "unsupported_processor_entity"}
	}
	return p.Call("POST", "/backend-api/payments/checkout/taxes", chatgpt_PlatformCallOptions{Body: protocol_O("checkout_session_id", p.CP.CheckoutID, "checkout_email", protocol_FirstTruthy(billing.Email, p.CP.Email), "billing_country", billing.Country, "billing_name", billing.Name, "currency", strings.ToLower(p.CP.Currency), "processor_entity", p.CP.Entity, "billing_address", billing.Address())})
}

// ---- internal/chatgpt/parsing.go ----

func chatgpt_ValidateReturnURL(raw string, cp *model_Checkpoint) (string, error) {
	parsed, err := url.Parse(raw)
	invalid := err != nil
	if !invalid {
		invalid = parsed.Scheme != "https" || parsed.Host != "chatgpt.com" || parsed.User != nil || parsed.Path != "/checkout/verify" || parsed.Fragment != "" || strings.Contains(raw, "\\")
	}
	for _, r := range raw {
		if unicode.IsSpace(r) || r < 32 {
			invalid = true
		}
	}
	if !invalid {
		query, queryErr := url.ParseQuery(parsed.RawQuery)
		invalid = queryErr != nil || !chatgpt_oneQuery(query, "stripe_session_id", cp.CheckoutID)
		for _, field := range [][2]string{{"account_id", cp.AccountID}, {"processor_entity", cp.Entity}} {
			if _, exists := query[field[0]]; exists && !chatgpt_oneQuery(query, field[0], field[1]) {
				invalid = true
			}
		}
	}
	if invalid {
		return "", &protocol_ProtocolError{Code: "invalid_return_url"}
	}
	return raw, nil
}

func chatgpt_oneQuery(query url.Values, key, expected string) bool {
	values := query[key]
	return len(values) == 1 && values[0] == expected
}

func chatgpt_RenewalState(data protocol_Map) string {
	states := map[string]bool{}
	items := []any{data}
	if values, ok := protocol_FirstTruthy(data["subscriptions"], data["items"]).([]any); ok {
		items = append(items, values...)
	}
	for _, value := range items {
		item := protocol_AsMap(value)
		if item == nil {
			continue
		}
		if item["will_renew"] == false || item["cancel_at_period_end"] == true || protocol_Truthy(item["cancels_at"]) || protocol_Truthy(item["cancel_at"]) {
			states["off"] = true
		}
		if item["will_renew"] == true || item["cancel_at_period_end"] == false {
			states["on"] = true
		}
	}
	if len(states) == 1 {
		for state := range states {
			return state
		}
	}
	return "unknown"
}

func chatgpt_SubscriptionActive(data protocol_Map, plan string) bool {
	if !model_MatchesSubscriptionType(plan, strings.ToLower(protocol_AsString(data["plan_type"]))) {
		return false
	}
	return protocol_Truthy(protocol_FirstTruthy(data["active_until"], data["current_period_end"], data["expires_at"], data["billing_period_end"])) || data["will_renew"] == true
}

// ---- internal/chatgpt/subscriptions.go ----

const chatgpt_subscriptionsPath = "/backend-api/subscriptions"
const chatgpt_subscriptionNotFoundDetail = "No subscription found for account"
const chatgpt_subscriptionErrorBodyLimit = 4096

func (p *chatgpt_Platform) Subscriptions(renewal bool) (protocol_Map, error) {
	mode := "payment"
	if renewal {
		mode = "renewal"
	}
	return p.Call("GET", chatgpt_subscriptionsPath+"?account_id="+url.QueryEscape(p.CP.AccountID), chatgpt_PlatformCallOptions{Mode: mode})
}

// 该响应仅说明订阅查询尚无结果，不代表扣款失败，也不授权再次付款。
// 精确限制请求、状态和业务正文，避免把路由错误或通用404解释为尚未到账。
func chatgpt_isSubscriptionNotFound(method, path string, response *protocol_HTTPResponse) bool {
	if method != http.MethodGet || path != chatgpt_subscriptionsPath || response == nil || response.StatusCode != http.StatusNotFound || len(response.Content) > chatgpt_subscriptionErrorBodyLimit {
		return false
	}
	if contentType := response.Headers.Get("Content-Type"); contentType != "" {
		mediaType, _, err := mime.ParseMediaType(contentType)
		if err != nil || mediaType != "application/json" && !(strings.HasPrefix(mediaType, "application/") && strings.HasSuffix(mediaType, "+json")) {
			return false
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(response.Content))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return false
	}
	detail := ""
	seen := map[string]bool{}
	for decoder.More() {
		token, err := decoder.Token()
		key, ok := token.(string)
		if err != nil || !ok || seen[key] {
			return false
		}
		seen[key] = true
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return false
		}
		if key == "detail" && json.Unmarshal(value, &detail) != nil {
			return false
		}
	}
	end, err := decoder.Token()
	if err != nil || end != json.Delim('}') || decoder.Decode(new(any)) != io.EOF {
		return false
	}
	return detail == chatgpt_subscriptionNotFoundDetail
}

func (p *chatgpt_Platform) PaymentMethods() (protocol_Map, error) {
	return p.Call("GET", "/backend-api/payments/payment_methods?account_id="+url.QueryEscape(p.CP.AccountID))
}

func (p *chatgpt_Platform) PricingConfig(country string) (protocol_Map, error) {
	path := "/backend-api/checkout_pricing_config/configs/" + url.PathEscape(country)
	headers := p.Headers(path)
	if p.CP.AccessToken == "" {
		// 公开目录不发送空 Bearer 或合成 Session Cookie。
		public := protocol_Fields{}
		for _, field := range headers {
			if !strings.EqualFold(field.Key, "Authorization") && !strings.EqualFold(field.Key, "Cookie") {
				public = append(public, field)
			}
		}
		headers = public
	}
	return p.Call("GET", path, chatgpt_PlatformCallOptions{Headers: headers})
}

func (p *chatgpt_Platform) RenewSubscription() (protocol_Map, error) {
	return p.Call("POST", chatgpt_subscriptionsPath+"/renew", chatgpt_PlatformCallOptions{Body: protocol_O("account_id", p.CP.AccountID)})
}

func (p *chatgpt_Platform) CustomerPortal() (string, error) {
	data, err := p.Call("GET", "/backend-api/payments/customer_portal?account_id="+url.QueryEscape(p.CP.AccountID))
	if err != nil {
		return "", err
	}
	raw := protocol_AsString(data["url"])
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Host != "pay.openai.com" || parsed.User != nil || !strings.HasPrefix(parsed.Path, "/p/session") {
		return "", protocol_NewError("customer_portal_unavailable")
	}
	return raw, nil
}

func (p *chatgpt_Platform) UpdateSubscription(planName, paymentMethodID, setupIntentID string) (protocol_Map, error) {
	body := protocol_O("account_id", p.CP.AccountID, "updated_plan", planName)
	if paymentMethodID != "" {
		body.Set("payment_method_id", paymentMethodID)
	}
	if setupIntentID != "" {
		body.Set("setup_intent_id", setupIntentID)
	}
	return p.Call("POST", chatgpt_subscriptionsPath+"/update", chatgpt_PlatformCallOptions{Body: body})
}

func (p *chatgpt_Platform) CancelPendingUpdate(paymentIntentID string) (protocol_Map, error) {
	return p.Call("POST", chatgpt_subscriptionsPath+"/update/cancel_pending", chatgpt_PlatformCallOptions{Body: protocol_O("account_id", p.CP.AccountID, "expected_payment_intent_id", paymentIntentID)})
}

func (p *chatgpt_Platform) AttachPaymentMethod(paymentMethodID string) (protocol_Map, error) {
	return p.Call("POST", "/backend-api/payments/payment_methods", chatgpt_PlatformCallOptions{Body: protocol_O("account_id", p.CP.AccountID, "payment_method_id", paymentMethodID, "set_as_default", true)})
}

// ---- internal/checkpointstore/create_unix.go ----

func checkpointstore_createEmpty(path string) error {
	fd, err := unix.Open(path, unix.O_CREAT|unix.O_EXCL|unix.O_WRONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return err
	}
	return unix.Close(fd)
}

// ---- internal/checkpointstore/errors.go ----

var checkpointstore_ErrLocked = errors.New("checkpoint_locked")
var checkpointstore_ErrExists = errors.New("checkpoint_exists")
var checkpointstore_ErrInvalid = errors.New("input_or_checkpoint_invalid")

// ---- internal/checkpointstore/fernet.go ----

// Fernet 格式与原 Python cryptography 实现一致，恢复不设置消息有效期。
func checkpointstore_encryptFernet(key, plain []byte) (string, error) {
	iv := make([]byte, aes.BlockSize)
	if _, err := rand.Read(iv); err != nil {
		return "", err
	}
	return checkpointstore_encryptFernetParts(key, plain, uint64(time.Now().Unix()), iv)
}
func checkpointstore_encryptFernetParts(key, plain []byte, timestamp uint64, iv []byte) (string, error) {
	if len(key) != 32 || len(iv) != aes.BlockSize {
		return "", errors.New("invalid encryption context")
	}
	block, err := aes.NewCipher(key[16:])
	if err != nil {
		return "", err
	}
	padding := aes.BlockSize - len(plain)%aes.BlockSize
	padded := make([]byte, len(plain)+padding)
	copy(padded, plain)
	for i := len(plain); i < len(padded); i++ {
		padded[i] = byte(padding)
	}
	token := make([]byte, 25+len(padded))
	token[0] = 0x80
	binary.BigEndian.PutUint64(token[1:9], timestamp)
	copy(token[9:25], iv)
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(token[25:], padded)
	signature := hmac.New(sha256.New, key[:16])
	signature.Write(token)
	token = append(token, signature.Sum(nil)...)
	return base64.URLEncoding.EncodeToString(token), nil
}
func checkpointstore_decryptFernet(key []byte, text string) ([]byte, error) {
	invalid := errors.New("unable to decrypt or validate checkpoint")
	token, err := base64.URLEncoding.DecodeString(text)
	if err != nil || len(key) != 32 || len(token) < 73 || token[0] != 0x80 {
		return nil, invalid
	}
	signed := token[:len(token)-sha256.Size]
	signature := hmac.New(sha256.New, key[:16])
	signature.Write(signed)
	if !hmac.Equal(signature.Sum(nil), token[len(token)-sha256.Size:]) {
		return nil, invalid
	}
	encrypted := signed[25:]
	if len(encrypted) == 0 || len(encrypted)%aes.BlockSize != 0 {
		return nil, invalid
	}
	block, err := aes.NewCipher(key[16:])
	if err != nil {
		return nil, invalid
	}
	plain := make([]byte, len(encrypted))
	cipher.NewCBCDecrypter(block, signed[9:25]).CryptBlocks(plain, encrypted)
	padding := int(plain[len(plain)-1])
	if padding < 1 || padding > aes.BlockSize || padding > len(plain) {
		return nil, invalid
	}
	for _, value := range plain[len(plain)-padding:] {
		if int(value) != padding {
			return nil, invalid
		}
	}
	return plain[:len(plain)-padding], nil
}

// ---- internal/checkpointstore/files.go ----

func checkpointstore_openLocked(path string) (*os.File, error) {
	fd, err := unix.Open(path, unix.O_CREAT|unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	stat, err := file.Stat()
	if err != nil || !stat.Mode().IsRegular() {
		file.Close()
		if err != nil {
			return nil, err
		}
		return nil, checkpointstore_ErrInvalid
	}
	if err := unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
		file.Close()
		if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
			return nil, checkpointstore_ErrLocked
		}
		return nil, err
	}
	return file, nil
}
func checkpointstore_readRegular(path string, limit int64) ([]byte, error) {
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	stat, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if !stat.Mode().IsRegular() {
		return nil, checkpointstore_ErrInvalid
	}
	raw, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > limit {
		return nil, checkpointstore_ErrInvalid
	}
	return raw, nil
}

// 每次保存先写并同步临时文件，发布后再同步目录；失败时不更新内存快照。
func checkpointstore_atomicWrite(path, prefix string, raw []byte, syncFile func(*os.File) error) error {
	dir := filepath.Dir(path)
	file, err := os.CreateTemp(dir, prefix)
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if err = file.Chmod(0600); err == nil {
		_, err = file.Write(raw)
	}
	if err == nil {
		err = syncFile(file)
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(name, path); err != nil {
		return err
	}
	directory, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer directory.Close()
	return syncFile(directory)
}

// ---- internal/checkpointstore/key.go ----

func checkpointstore_LocalKey(path string, create bool) (string, error) {
	return checkpointstore_localKey(path, create, func(f *os.File) error { return f.Sync() })
}
func checkpointstore_localKey(path string, create bool, syncFile func(*os.File) error) (string, error) {
	if create {
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return "", err
		}
	}
	lock, err := checkpointstore_openLocked(path + ".lock")
	if err != nil {
		return "", err
	}
	defer lock.Close()
	raw, err := checkpointstore_readRegular(path, 127)
	if err == nil {
		if !regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`).Match(raw) {
			return "", checkpointstore_ErrInvalid
		}
		return string(raw), nil
	}
	if !errors.Is(err, os.ErrNotExist) || !create {
		return "", checkpointstore_ErrInvalid
	}
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	key := base64.RawURLEncoding.EncodeToString(bytes)
	if err := checkpointstore_atomicWrite(path, ".key-", []byte(key), syncFile); err != nil {
		return "", err
	}
	return key, nil
}

// ---- internal/checkpointstore/store.go ----
// Package checkpointstore 提供同步加密持久化与进程间独占锁。

type checkpointstore_Store struct {
	Path       string
	Checkpoint *model_Checkpoint
	lock       *os.File
	salt, key  []byte
	mu         sync.Mutex
	syncFile   func(*os.File) error
}
type checkpointstore_envelope struct {
	Version    int    `json:"version"`
	Salt       string `json:"salt"`
	Ciphertext string `json:"ciphertext"`
}

func checkpointstore_Open(path, password string, recoverExisting bool) (store *checkpointstore_Store, err error) {
	if utf8.RuneCountInString(password) < 12 {
		return nil, checkpointstore_ErrInvalid
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if err = os.MkdirAll(filepath.Dir(absolute), 0700); err != nil {
		return nil, err
	}
	store = &checkpointstore_Store{Path: absolute, syncFile: func(f *os.File) error { return f.Sync() }}
	store.lock, err = checkpointstore_openLocked(absolute + ".lock")
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			store.Close()
			store = nil
		}
	}()
	var encoded checkpointstore_envelope
	if recoverExisting {
		raw, readErr := checkpointstore_readRegular(absolute, 4*1024*1024)
		if readErr != nil {
			return store, checkpointstore_ErrInvalid
		}
		var object map[string]json.RawMessage
		if json.Unmarshal(raw, &object) != nil || len(object) != 3 || object["version"] == nil || object["salt"] == nil || object["ciphertext"] == nil {
			return store, checkpointstore_ErrInvalid
		}
		if err = json.Unmarshal(raw, &encoded); err != nil || encoded.Version != 1 {
			return store, checkpointstore_ErrInvalid
		}
		// Python validate=True 拒绝空白；普通解码保留其允许非零填充位的行为。
		if !regexp.MustCompile(`^[A-Za-z0-9+/]{22}==$`).MatchString(encoded.Salt) {
			return store, checkpointstore_ErrInvalid
		}
		store.salt, err = base64.StdEncoding.DecodeString(encoded.Salt)
		if err != nil || len(store.salt) != 16 {
			return store, checkpointstore_ErrInvalid
		}
	} else {
		if openErr := checkpointstore_createEmpty(absolute); openErr != nil {
			if errors.Is(openErr, os.ErrExist) {
				return store, checkpointstore_ErrExists
			}
			return store, openErr
		}
		store.salt = make([]byte, 16)
		if _, err = rand.Read(store.salt); err != nil {
			return store, err
		}
	}
	store.key, err = scrypt.Key([]byte(password), store.salt, 32768, 8, 1, 32)
	if err != nil {
		return store, err
	}
	if recoverExisting {
		plain, decryptErr := checkpointstore_decryptFernet(store.key, encoded.Ciphertext)
		if decryptErr != nil {
			return store, checkpointstore_ErrInvalid
		}
		var cp model_Checkpoint
		decoder := json.NewDecoder(bytes.NewReader(plain))
		decoder.UseNumber()
		decoder.DisallowUnknownFields()
		if decoder.Decode(&cp) != nil || cp.ValidateForRecovery() != nil {
			return store, checkpointstore_ErrInvalid
		}
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			return store, checkpointstore_ErrInvalid
		}
		store.Checkpoint = &cp
	}
	return store, nil
}

func (s *checkpointstore_Store) Save(checkpoint model_Checkpoint) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lock == nil {
		return checkpointstore_ErrInvalid
	}
	copy, err := checkpoint.Clone()
	if err != nil {
		return checkpointstore_ErrInvalid
	}
	plain, err := json.Marshal(copy)
	if err != nil {
		return checkpointstore_ErrInvalid
	}
	ciphertext, err := checkpointstore_encryptFernet(s.key, plain)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(checkpointstore_envelope{1, base64.StdEncoding.EncodeToString(s.salt), ciphertext})
	if err != nil {
		return err
	}
	if err = checkpointstore_atomicWrite(s.Path, ".checkpoint-", encoded, s.syncFile); err != nil {
		return err
	}
	s.Checkpoint = &copy
	return nil
}
func (s *checkpointstore_Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lock == nil {
		return nil
	}
	err := s.lock.Close()
	s.lock = nil
	return err
}

// ---- internal/cli/arguments.go ----

type cli_arguments struct {
	Country, Plan, Session, Currency, Timezone, Proxy, CheckoutType string
	BrowserProfile                                                  string
	TLSCert, TLSKey                                                 string
	WithPrice, Debug                                                bool
	Offset                                                          int
	Timeout, Deadline                                               float64
	Recover, Billing, Renewal, Checkpoint                           string
	Expected                                                        int64
	Seats, Seats5x                                                  int
	Interval                                                        string
	CancelRenewalOnly                                               bool
	set                                                             map[string]bool
}

func cli_parseArguments(argv []string, payment bool, stderr io.Writer) (cli_arguments, error) {
	a := cli_arguments{set: map[string]bool{}}
	name := "create-checkout"
	if payment {
		name = "pay"
	}
	f := flag.NewFlagSet(name, flag.ContinueOnError)
	f.SetOutput(stderr)
	f.StringVar(&a.Country, "country", "", "结账国家，例如 PH、US、JP、EG、NR")
	f.StringVar(&a.Plan, "plan", "", "plus、pro5x、pro20x、promax、go、team 或 team5x")
	f.StringVar(&a.Session, "session", "", "Session JSON 文件；默认交互粘贴，非终端从标准输入读取")
	f.StringVar(&a.Currency, "currency", "", "其他地区须指定币种")
	f.StringVar(&a.Timezone, "timezone", "", "其他地区须指定 IANA 时区")
	f.StringVar(&a.Proxy, "proxy", "", "新操作代理地址；恢复沿用快照")
	f.IntVar(&a.Seats, "seats", 0, "普通 Team 席位数，seat_type=default")
	f.IntVar(&a.Seats5x, "seats-5x", 0, "Team 5X 席位数，seat_type=prolite")
	f.StringVar(&a.Interval, "interval", "", "Team 计费周期：month 或 year，默认 month")
	f.StringVar(&a.BrowserProfile, "browser-profile", "", "Chrome 131 画像：windows、macos 或 random；默认 random，每笔交易选择一次")
	f.IntVar(&a.Offset, "fingerprint-clock-offset-hours", 0, "指纹时钟偏移：0 或 -24；不改变日志和超时")
	f.Float64Var(&a.Timeout, "timeout", 45, "单请求超时秒数")
	f.Float64Var(&a.Deadline, "deadline", 240, "整次操作超时秒数")
	f.BoolVar(&a.Debug, "debug", false, "输出 HTTP、Sentinel 等安全明细")
	f.StringVar(&a.CheckoutType, "checkout-type", "", "请求偏好：oaics 或 hosted；最终按响应 checkout_ui_mode 分流")
	if payment {
		f.StringVar(&a.TLSCert, "tls-cert", "", "遇到待验证付款时启动 HTTPS 入口的证书")
		f.StringVar(&a.TLSKey, "tls-key", "", "HTTPS 入口证书私钥")
		f.StringVar(&a.Recover, "recover", "", "核验原加密 Checkpoint，不创建或重放付款")
		f.StringVar(&a.Billing, "billing", "data/billing.json", "不含 email 的账单地址 JSON 文件")
		f.StringVar(&a.Renewal, "renewal", "keep", "keep 保留自动续费，cancel 在付款成功后取消")
		f.BoolVar(&a.CancelRenewalOnly, "cancel-renewal", false, "仅关闭当前账号自动续费，不创建 Checkout 或付款")
		f.StringVar(&a.Checkpoint, "checkpoint", "", "加密恢复文件路径，必须尚不存在")
		f.Int64Var(&a.Expected, "expected-amount", 0, "可选：必须匹配的含税金额（最小币种单位）")
	} else {
		f.BoolVar(&a.WithPrice, "with-price", false, "读取本次 Checkout 价格快照，不计算税费或付款")
	}
	if err := f.Parse(argv); err != nil {
		return a, err
	}
	f.Visit(func(v *flag.Flag) { a.set[v.Name] = true })
	fail := func(text string) (cli_arguments, error) { fmt.Fprintln(stderr, text); return a, cli_ErrInput }
	if f.NArg() != 0 {
		return fail("unexpected positional arguments")
	}
	if a.Plan != "" && model_CheckoutPlan(a.Plan) == "" {
		return fail("--plan must be plus, pro5x, pro20x, promax, go, team or team5x")
	}
	if _, err := model_NormalizeTeamSeatConfig(a.Plan, a.Seats, a.Seats5x, a.Interval); err != nil {
		if (a.Seats != 0 || a.Seats5x != 0) && !model_IsTeamPlan(a.Plan) {
			return fail("--seats and --seats-5x only apply to team or team5x")
		}
		if a.Interval != "" && a.Interval != "month" && a.Interval != "year" {
			return fail("--interval must be month or year")
		}
		return fail("team seats must total at least 2")
	}
	if a.Offset != 0 && a.Offset != -24 {
		return fail("--fingerprint-clock-offset-hours must be 0 or -24")
	}
	if err := protocol_ValidateBrowserProfileSelection(a.BrowserProfile); err != nil {
		return fail("--browser-profile must be windows, macos or random")
	}
	if !cli_positiveBudget(a.Timeout) || !cli_positiveBudget(a.Deadline) {
		return fail("--timeout and --deadline must be finite positive numbers")
	}
	if payment {
		if (a.TLSCert == "") != (a.TLSKey == "") {
			return fail("--tls-cert and --tls-key must be supplied together")
		}
		if a.CancelRenewalOnly && a.TLSCert != "" {
			return fail("TLS handoff does not apply to --cancel-renewal")
		}
		if a.Recover != "" && a.CancelRenewalOnly {
			return fail("--recover cannot be combined with --cancel-renewal")
		}
		if a.Recover != "" {
			for _, name := range []string{"country", "plan", "session", "billing", "expected-amount", "renewal", "checkpoint", "fingerprint-clock-offset-hours", "browser-profile", "seats", "seats-5x", "interval", "checkout-type", "cancel-renewal"} {
				if a.set[name] {
					return fail("--recover cannot be combined with new-payment arguments")
				}
			}
			if a.Proxy != "" || a.Currency != "" || a.Timezone != "" {
				return fail("--recover cannot be combined with new-payment arguments")
			}
		} else if a.CancelRenewalOnly {
			for _, name := range []string{"plan", "billing", "expected-amount", "renewal", "checkpoint", "seats", "seats-5x", "interval", "checkout-type"} {
				if a.set[name] {
					return fail("--cancel-renewal cannot be combined with payment arguments")
				}
			}
			if a.Country == "" {
				return fail("--cancel-renewal requires --country")
			}
		} else {
			if a.Country == "" || a.Plan == "" {
				return fail("New payments require --country --plan")
			}
			if a.Billing == "" {
				return fail("--billing must not be empty")
			}
		}
		if a.Renewal != "keep" && a.Renewal != "cancel" {
			return fail("--renewal must be keep or cancel")
		}
		if a.set["expected-amount"] && a.Expected < 0 {
			return fail("--expected-amount must be >= 0")
		}
	} else {
		if a.Country == "" || a.Plan == "" {
			return fail("--country and --plan are required")
		}
	}
	if a.CheckoutType != "" && a.CheckoutType != "oaics" && a.CheckoutType != "hosted" {
		return fail("--checkout-type must be oaics or hosted")
	}
	return a, nil
}
func cli_positiveBudget(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) && v > 0 }
func (a cli_arguments) config() model_Config {
	return model_Config{Proxy: a.Proxy, Currency: a.Currency, Timezone: a.Timezone, Timeout: a.Timeout, Deadline: a.Deadline, BrowserProfile: a.BrowserProfile}
}

// ---- internal/cli/catalog_prices.go ----

// CatalogPrices 与创建账单命令分离，参数中不接受卡片、付款或账单配置。
func (r *cli_Runner) CatalogPrices(ctx context.Context, argv []string) (code int) {
	cfg := model_DefaultConfig()
	var options gptpay_CatalogPricesOptions
	var sessionFile string
	var debug bool
	flags := flag.NewFlagSet("catalog-prices", flag.ContinueOnError)
	flags.SetOutput(r.Err)
	flags.StringVar(&options.ConfigKey, "config-key", "", "价格配置键，例如 PHP、US；不自动推测地区映射")
	flags.StringVar(&options.ExpectedCountry, "expect-country", "", "校验响应国家，例如 PH")
	flags.StringVar(&options.ExpectedCurrency, "expect-currency", "", "校验响应币种，例如 PHP")
	flags.StringVar(&sessionFile, "session", "", "可选 Session JSON 文件；省略时匿名查询")
	flags.StringVar(&cfg.Proxy, "proxy", "", "查询代理地址")
	flags.StringVar(&cfg.BrowserProfile, "browser-profile", "", "windows、macos 或 random")
	flags.Float64Var(&cfg.Timeout, "timeout", cfg.Timeout, "单请求超时秒数")
	flags.Float64Var(&cfg.Deadline, "deadline", cfg.Deadline, "整次查询超时秒数")
	flags.BoolVar(&debug, "debug", false, "输出脱敏查询日志")
	if err := flags.Parse(argv); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if flags.NArg() != 0 || !protocol_Matches(`[A-Z]{2,3}`, strings.ToUpper(strings.TrimSpace(options.ConfigKey))) ||
		options.ExpectedCountry != "" && !protocol_Matches(`[A-Z]{2}`, strings.ToUpper(strings.TrimSpace(options.ExpectedCountry))) ||
		options.ExpectedCurrency != "" && !protocol_Matches(`[A-Z]{3}`, strings.ToUpper(strings.TrimSpace(options.ExpectedCurrency))) ||
		!cli_positiveBudget(cfg.Timeout) || !cli_positiveBudget(cfg.Deadline) || protocol_ValidateProxy(cfg.Proxy) != nil || protocol_ValidateBrowserProfileSelection(cfg.BrowserProfile) != nil {
		r.message("查询参数无效，请使用 --help 查看参数。", "ERROR")
		return 2
	}
	var session model_Session
	var err error
	if sessionFile != "" {
		session, err = r.SessionInput(ctx, sessionFile)
	}
	if err != nil {
		if errors.Is(err, context.Canceled) || ctx.Err() != nil {
			return 130
		}
		r.message("session_error: "+cli_sessionErrorMessage(err), "ERROR")
		return 2
	}
	output := protocol_Map{}
	code = 1
	defer func() {
		if recover() != nil {
			output = protocol_Map{"error_code": "protocol_error"}
			code = 1
		}
		if r.output(output) != nil {
			r.message("写入查询结果失败。", "ERROR")
			code = 1
		}
	}()
	result, err := r.CatalogClientFactory(cfg, gptpay_ClientOptions{EventSink: r.observer(debug)}).CatalogPrices(ctx, session, options)
	if err != nil {
		output["error_code"] = observability_SafeErrorCode(err)
		return 1
	}
	output = protocol_Map{"data": result}
	return 0
}

// ---- internal/cli/create_checkout.go ----

func (r *cli_Runner) CreateCheckout(ctx context.Context, argv []string) (code int) {
	a, err := cli_parseArguments(argv, false, r.Err)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	cli_offsetNotice(r, a.Offset)
	session, err := r.SessionInput(ctx, a.Session)
	if err != nil {
		if errors.Is(err, context.Canceled) || ctx.Err() != nil {
			r.message("Session input cancelled.", "WARNING")
			return 130
		}
		r.message("session_error: "+cli_sessionErrorMessage(err), "ERROR")
		return 2
	}
	started := time.Now()
	output := protocol_Map{}
	code = 1
	defer func() {
		if recover() != nil {
			output = protocol_Map{"error_code": "network_error"}
			code = 1
		}
		output["elapsed_ms"] = math.RoundToEven(time.Since(started).Seconds() * 1000)
		if err := r.output(output); err != nil {
			r.message("无法写入命令结果，请检查标准输出目标。", "ERROR")
			code = 1
		}
	}()
	result, err := r.ClientFactory(a.config(), gptpay_ClientOptions{EventSink: r.observer(a.Debug)}).CreateCheckout(ctx, session, gptpay_CreateCheckoutOptions{Country: a.Country, Plan: a.Plan, Seats: a.Seats, Seats5x: a.Seats5x, Interval: a.Interval, CheckoutType: a.CheckoutType, WithPrice: a.WithPrice, FingerprintClockOffsetHours: a.Offset})
	if err != nil {
		output["error_code"] = observability_SafeErrorCode(err)
		return 1
	}
	output = protocol_Map{"checkout_id": result.CheckoutID, "checkout_url": result.CheckoutURL, "processor_entity": result.ProcessorEntity, "error_code": result.ErrorCode, "price": result.Price}
	if result.Flow != "" {
		output["flow"] = result.Flow
	}
	if result.ErrorCode == "" {
		return 0
	}
	return 1
}

// ---- internal/cli/input.go ----

var cli_ErrInput = errors.New("input_or_checkpoint_invalid")

type cli_safeInputError struct{ message string }

func (e *cli_safeInputError) Error() string      { return e.message }
func cli_sessionInputError(message string) error { return &cli_safeInputError{message} }
func cli_parseSessionInput(raw string) (model_Session, error) {
	session, err := model_ParseSession(raw)
	if err != nil {
		return session, cli_sessionInputError(err.Error())
	}
	return session, nil
}
func cli_sessionErrorMessage(err error) string {
	var safe *cli_safeInputError
	if errors.As(err, &safe) {
		return safe.message
	}
	return "Unable to read or validate Session JSON input"
}

func cli_readUTF8(reader io.Reader, maxChars int, bom bool) (string, error) {
	raw, err := io.ReadAll(io.LimitReader(reader, int64((maxChars+1)*4+3)))
	if err != nil {
		return "", cli_ErrInput
	}
	if bom {
		raw = bytes.TrimPrefix(raw, []byte{0xef, 0xbb, 0xbf})
	}
	if !utf8.Valid(raw) || utf8.RuneCount(raw) > maxChars {
		return "", cli_ErrInput
	}
	return string(raw), nil
}
func cli_ReadSessionFile(path string) (model_Session, error) {
	file, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return model_Session{}, cli_sessionInputError("Session JSON file not found. Check --session FILE.")
		}
		return model_Session{}, cli_sessionInputError("Unable to read Session JSON file")
	}
	defer file.Close()
	raw, err := cli_readUTF8(file, model_SessionJSONMaxChars, true)
	if err != nil {
		return model_Session{}, cli_sessionInputError("Session JSON file must use UTF-8 encoding and fit the input limit")
	}
	return cli_parseSessionInput(raw)
}
func cli_ReadSessionStream(reader io.Reader) (model_Session, error) {
	raw, err := cli_readUTF8(reader, model_SessionJSONMaxChars, false)
	if err != nil {
		return model_Session{}, cli_sessionInputError("Session JSON input must use UTF-8 encoding and fit the input limit")
	}
	return cli_parseSessionInput(raw)
}

func cli_ParseCard(raw string) (model_Card, error) {
	match := regexp.MustCompile(`^([0-9 ]+)---- *([0-9]{2})/([0-9]{2}) *---- *([0-9]{3,4})$`).FindStringSubmatch(strings.TrimSpace(raw))
	if len(match) != 5 {
		return model_Card{}, cli_ErrInput
	}
	card := model_Card{Number: strings.ReplaceAll(match[1], " ", ""), CVC: match[4], ExpMonth: match[2], ExpYear: "20" + match[3]}
	if card.Validate() != nil {
		return model_Card{}, cli_ErrInput
	}
	return card, nil
}
func cli_ReadBilling(path string) (model_Billing, error) {
	file, err := os.Open(path)
	if err != nil {
		return model_Billing{}, cli_ErrInput
	}
	defer file.Close()
	raw, err := cli_readUTF8(file, 16384, true)
	if err != nil {
		return model_Billing{}, cli_ErrInput
	}
	var data map[string]string
	if json.Unmarshal([]byte(raw), &data) != nil || len(data) != 6 {
		return model_Billing{}, cli_ErrInput
	}
	for _, key := range []string{"name", "line1", "city", "state", "postal_code", "country"} {
		if _, ok := data[key]; !ok {
			return model_Billing{}, cli_ErrInput
		}
	}
	// 账单邮箱沿用本次账号，文件不能单独覆盖。
	return cli_billingFromFields(data)
}
func cli_billingFromFields(data map[string]string) (model_Billing, error) {
	if len(data) != 6 {
		return model_Billing{}, cli_ErrInput
	}
	billing := model_Billing{Name: data["name"], Line1: data["line1"], City: data["city"], State: data["state"], PostalCode: data["postal_code"], Country: data["country"]}
	if billing.Validate() != nil {
		return model_Billing{}, cli_ErrInput
	}
	return billing, nil
}
func cli_DisplayAmount(amount int64, currency string) string {
	switch currency {
	case "USD", "PHP", "EGP", "AUD":
		return fmt.Sprintf("%d.%02d %s（%d 最小币种单位）", amount/100, amount%100, currency, amount)
	case "JPY":
		return fmt.Sprintf("%d JPY", amount)
	default:
		return fmt.Sprintf("%d %s（最小币种单位）", amount, currency)
	}
}

func cli_jsonClosed(text string) bool {
	depth := 0
	quoted, escaped := false, false
	for _, char := range text {
		if quoted {
			if escaped {
				escaped = false
			} else if char == '\\' {
				escaped = true
			} else if char == '"' {
				quoted = false
			}
		} else if char == '"' {
			quoted = true
		} else if char == '{' || char == '[' {
			depth++
		} else if char == '}' || char == ']' {
			depth--
			if depth <= 0 {
				return true
			}
		}
	}
	return false
}

// ---- internal/cli/pay.go ----

func (r *cli_Runner) Pay(ctx context.Context, argv []string) (code int) {
	a, err := cli_parseArguments(argv, true, r.Err)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if a.TLSCert != "" {
		if _, e := tls.LoadX509KeyPair(a.TLSCert, a.TLSKey); e != nil {
			r.message("HTTPS 证书或私钥无效，尚未开始付款。", "ERROR")
			return 2
		}
	}
	started := time.Now()
	var store *checkpointstore_Store
	storeReady := false
	var result model_Result
	output := protocol_Map{}
	code = 1
	defer func() {
		if recover() != nil {
			output = protocol_Map{"error_code": "network_error"}
			code = 1
		}
		if storeReady {
			result = cli_restoreChargedResult(result, store.Checkpoint)
		}
		charged := result.Payment == model_PaymentCharged
		if charged {
			output = cli_chargedResultOutput(result, output)
			code = 0
		} else if ctx.Err() != nil {
			output = protocol_Map{"error_code": "cancelled"}
			code = 130
		}
		if store != nil {
			if storeReady {
				output["checkpoint_file"] = store.Path
			}
			if storeReady && store.Checkpoint != nil {
				if _, ok := output["payment"]; !ok {
					output["payment"] = store.Checkpoint.Payment
				}
				if _, ok := output["phase"]; !ok {
					output["phase"] = store.Checkpoint.Phase
				}
			}
			store.Close()
		}
		if charged {
			r.reportChargedWarning(result, output)
		} else {
			output["elapsed_ms"] = math.RoundToEven(time.Since(started).Seconds() * 1000)
		}
		if code != 0 && !charged {
			switch output["payment"] {
			case "unknown", "pending", "authorized", "action_required", model_PaymentUnknown, model_PaymentPending, model_PaymentAuthorized, model_PaymentActionRequired:
				r.message("请保留 Checkpoint，使用 --recover 核验原交易；不要重新发起支付。", "WARNING")
			}
		}
		if reason := result.FailureReason(); reason != "" {
			r.message("付款失败："+reason+" provider_code="+result.ProviderCode+" decline_code="+result.DeclineCode, "WARNING")
		}
		if err := r.output(output); err != nil {
			r.message("无法写入命令结果，请检查标准输出目标。", "ERROR")
			code = 1
		}
	}()
	fail := func(err error) int {
		if errors.Is(err, context.Canceled) || ctx.Err() != nil {
			output = protocol_Map{"error_code": "cancelled"}
			return 130
		}
		name := "input_or_checkpoint_invalid"
		if errors.Is(err, checkpointstore_ErrExists) {
			name = "checkpoint_exists"
		} else if errors.Is(err, checkpointstore_ErrLocked) {
			name = "checkpoint_locked"
		}
		output = protocol_Map{"error_code": name}
		if storeReady && store.Checkpoint != nil && store.Checkpoint.Payment == "charged" {
			output["error_code"] = observability_SafeErrorCode(err)
		} else {
			r.message("输入或恢复文件校验失败："+name+"；检查参数、卡片格式、账单和本地密钥，不要覆盖原交易快照或密钥。", "ERROR")
		}
		return 1
	}
	if a.Recover != "" {
		password, e := r.LocalKey(r.keyPath(), false)
		if e != nil {
			return fail(e)
		}
		store, e = r.OpenStore(a.Recover, password, true)
		if e != nil {
			return fail(e)
		}
		storeReady = true
		result, err = r.ClientFactory(a.config(), gptpay_ClientOptions{Checkpoint: store.Save, EventSink: r.observer(a.Debug)}).Recover(ctx, *store.Checkpoint)
	} else if a.CancelRenewalOnly {
		cli_offsetNotice(r, a.Offset)
		session, e := r.SessionInput(ctx, a.Session)
		if e != nil {
			return fail(e)
		}
		result, err = r.ClientFactory(a.config(), gptpay_ClientOptions{EventSink: r.observer(a.Debug)}).CancelRenewal(ctx, session, a.Country)
	} else {
		cli_offsetNotice(r, a.Offset)
		billing, e := r.BillingInput(a.Billing)
		if e != nil {
			return fail(e)
		}
		session, e := r.SessionInput(ctx, a.Session)
		if e != nil {
			return fail(e)
		}
		card, e := r.CardInput(ctx)
		if e != nil {
			return fail(e)
		}
		password, e := r.LocalKey(r.keyPath(), true)
		if e != nil {
			return fail(e)
		}
		path := a.Checkpoint
		if path == "" {
			path = filepath.Join(r.DataDir, "payment-"+strings.ReplaceAll(uuid.NewString(), "-", "")+".enc")
		}
		store, e = r.OpenStore(path, password, false)
		if e != nil {
			return fail(e)
		}
		storeReady = true
		r.message("恢复文件："+store.Path, "INFO")
		r.message("恢复命令：go run cs_live.go pay --recover "+cli_shellQuote(store.Path), "INFO")
		approve := func(amount int64, currency string) (bool, error) {
			// 金额摘要是继续本次付款的必要输出；日志目标失败时不默认为已展示。
			if err := r.message(fmt.Sprintf("套餐=%s 国家=%s 最终含税金额=%s 卡尾号=%s 自动续费=%s", a.Plan, strings.ToUpper(a.Country), cli_DisplayAmount(amount, currency), card.Number[len(card.Number)-4:], a.Renewal), "INFO"); err != nil {
				return false, cli_ErrInput
			}
			return true, nil
		}
		cancel := a.Renewal == "cancel"
		var expected *int64
		if a.set["expected-amount"] {
			expected = &a.Expected
		}
		result, err = r.ClientFactory(a.config(), gptpay_ClientOptions{Checkpoint: store.Save, EventSink: r.observer(a.Debug)}).Pay(ctx, session, card, gptpay_PayOptions{Country: a.Country, Plan: a.Plan, CheckoutType: a.CheckoutType, Seats: a.Seats, Seats5x: a.Seats5x, Interval: a.Interval, Billing: billing, ExpectedAmount: expected, CancelRenewal: &cancel, ApprovePayment: approve, FingerprintClockOffsetHours: a.Offset})
	}
	if err == nil && result.Payment == model_PaymentActionRequired && a.TLSCert != "" && storeReady {
		result, err = r.continuePaymentAction(ctx, a, store, result)
	}
	if result.Payment == model_PaymentCharged {
		output = result.Public()
		if err != nil {
			output["warning_code"] = observability_SafeErrorCode(err)
		}
		return 0
	}
	if err != nil {
		return fail(err)
	}
	output = result.Public()
	if a.CancelRenewalOnly && result.ErrorCode == "" && result.Renewal == "off" {
		return 0
	}
	if result.ErrorCode == "payment_cancelled" {
		return 130
	}
	return 1
}
func cli_shellQuote(value string) string {
	if !strings.ContainsAny(value, " \t\n\r'\"\\$`;&|<>()*?[]{}!") {
		return value
	}
	return cli_quoteTerminalArgument(value)
}

// ---- internal/cli/pay_action.go ----

// 沿用已锁定的原交易文件，避免子命令重开文件死锁或重复输出结果。
func (r *cli_Runner) continuePaymentAction(ctx context.Context, a cli_arguments, store *checkpointstore_Store, prior model_Result) (model_Result, error) {
	cp := store.Checkpoint
	if cp == nil || cp.Payment != "action_required" || !protocol_Matches(`pi_[A-Za-z0-9]+`, cp.PaymentIntentID) || !protocol_Matches(`pk_[A-Za-z0-9_]+`, cp.PublishableKey) || !protocol_Matches(cp.PaymentIntentID+`_secret_[A-Za-z0-9]+`, cp.ClientSecret) {
		return prior, nil
	}
	serve := r.ActionServer
	if serve == nil {
		serve = func(c context.Context, creds paymentaction_Credentials, ready func(string) error, cert, key string, report func(string, string)) error {
			return paymentaction_ServeTLS(c, creds, ready, cert, key, report)
		}
	}
	actionCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	err := serve(actionCtx, paymentaction_Credentials{PublishableKey: cp.PublishableKey, ClientSecret: cp.ClientSecret}, func(link string) error {
		return r.message("在本机浏览器打开验证入口（10 分钟有效，请勿分享）："+link, "INFO")
	}, a.TLSCert, a.TLSKey, func(outcome, code string) {
		r.message("浏览器验证结果（仅诊断）：outcome="+outcome+" code="+code, "INFO")
	})
	if err != nil {
		r.message("验证入口已关闭，保留原交易状态；请使用恢复文件继续核验。", "WARNING")
		return prior, nil
	}
	// 浏览器返回仅表示交互结束，最终资金结果由服务器核验。
	return r.ClientFactory(a.config(), gptpay_ClientOptions{Checkpoint: store.Save, EventSink: r.observer(a.Debug)}).Recover(ctx, *cp)
}

// ---- internal/cli/payment_action.go ----

// PaymentAction 显式打开交接入口，默认 pay/recover 行为保持不变。
func (r *cli_Runner) PaymentAction(ctx context.Context, args []string) int {
	return r.paymentAction(ctx, args, func(ctx context.Context, c paymentaction_Credentials, ready func(string) error) error {
		return paymentaction_Serve(ctx, c, ready, func(outcome, code string) {
			r.message("浏览器验证结果（仅诊断）：outcome="+outcome+" code="+code, "INFO")
		})
	})
}

func (r *cli_Runner) paymentAction(ctx context.Context, args []string, serve func(context.Context, paymentaction_Credentials, func(string) error) error) int {
	flags := flag.NewFlagSet("payment-action", flag.ContinueOnError)
	flags.SetOutput(r.Err)
	checkpoint := flags.String("recover", "", "原付款恢复文件（必填）")
	certFile := flags.String("tls-cert", "", "本机 HTTPS 证书，须包含 127.0.0.1")
	keyFile := flags.String("tls-key", "", "本机 HTTPS 证书私钥")
	if err := flags.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return 0
		}
		return 2
	}
	if *checkpoint == "" || flags.NArg() != 0 || (*certFile == "") != (*keyFile == "") {
		r.message("请使用 --recover 指定原付款恢复文件。", "ERROR")
		return 2
	}
	key, err := r.LocalKey(r.keyPath(), false)
	if err != nil {
		r.message("读取本地密钥失败。", "ERROR")
		return 1
	}
	store, err := r.OpenStore(*checkpoint, key, true)
	if err != nil {
		r.message("读取或锁定恢复文件失败。", "ERROR")
		return 1
	}
	defer store.Close()
	cp := store.Checkpoint
	if cp == nil || cp.Payment != "action_required" || !protocol_Matches(`pi_[A-Za-z0-9]+`, cp.PaymentIntentID) || !protocol_Matches(`pk_[A-Za-z0-9_]+`, cp.PublishableKey) || !protocol_Matches(cp.PaymentIntentID+`_secret_[A-Za-z0-9]+`, cp.ClientSecret) {
		r.message("恢复文件不具备待验证交易所需的信息，请先核验原交易。", "ERROR")
		return 1
	}
	if *certFile != "" {
		serve = func(ctx context.Context, c paymentaction_Credentials, ready func(string) error) error {
			return paymentaction_ServeTLS(ctx, c, ready, *certFile, *keyFile, func(outcome, code string) {
				r.message("浏览器验证结果（仅诊断）：outcome="+outcome+" code="+code, "INFO")
			})
		}
	} else {
		r.message("当前为本机 HTTP 调试入口；真实验证请配置 --tls-cert 和 --tls-key。", "WARNING")
	}
	actionCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	err = serve(actionCtx, paymentaction_Credentials{PublishableKey: cp.PublishableKey, ClientSecret: cp.ClientSecret}, func(link string) error {
		return r.message("在本机浏览器打开此临时入口（10 分钟有效，请勿分享）："+link, "INFO")
	})
	if err != nil {
		r.message("验证入口已关闭，未据此变更付款结果。请保留恢复文件。", "WARNING")
		return 1
	}
	// 浏览器通知不携带支付状态，唯一可信证据来自服务器核验原交易。
	result, err := r.ClientFactory(model_DefaultConfig(), gptpay_ClientOptions{Checkpoint: store.Save, EventSink: r.observer(false)}).Recover(ctx, *cp)
	if err != nil {
		r.message("原交易核验失败，请保留恢复文件继续核验。", "ERROR")
		return 1
	}
	output := result.Public()
	output["checkpoint_file"] = store.Path
	if reason := result.FailureReason(); reason != "" {
		r.message("付款失败："+reason+" provider_code="+result.ProviderCode+" decline_code="+result.DeclineCode, "WARNING")
	}
	if r.output(output) != nil {
		return 1
	}
	if result.Payment != model_PaymentCharged {
		return 1
	}
	return 0
}

// ---- internal/cli/payment_result.go ----

// 已保存的明确扣款结果不会因后续异常丢失；不从其它状态推断扣款。
func cli_restoreChargedResult(result model_Result, checkpoint *model_Checkpoint) model_Result {
	if result.Payment == model_PaymentCharged || checkpoint == nil || checkpoint.Payment != "charged" {
		return result
	}
	cp := *checkpoint
	result = model_Result{Payment: model_PaymentCharged, Activation: cp.Activation, Renewal: cp.Renewal, CheckoutID: cp.CheckoutID, PaymentIntentID: cp.PaymentIntentID, Amount: cp.Amount, Currency: cp.Currency, Checkpoint: cp}
	if elapsed, ok := protocol_Integer(cp.Metadata["payment_elapsed_ms"]); ok && elapsed >= 0 {
		result.PaymentElapsedMS = &elapsed
	}
	return result
}

func cli_chargedResultOutput(result model_Result, previous protocol_Map) protocol_Map {
	warning := result.WarningCode
	if warning == "" {
		warning = result.ErrorCode
	}
	if warning == "" {
		warning = protocol_AsString(previous["warning_code"])
	}
	if warning == "" {
		warning = protocol_AsString(previous["error_code"])
	}
	output := result.Public()
	output["error_code"] = ""
	if warning != "" {
		output["warning_code"] = observability_SafeErrorCode(warning)
	}
	// 只有协议记录的原交易扣款耗时有效；恢复耗时不可替代缺失记录。
	output["elapsed_ms"] = nil
	if result.PaymentElapsedMS != nil && *result.PaymentElapsedMS >= 0 {
		output["elapsed_ms"] = *result.PaymentElapsedMS
	}
	return output
}

func (r *cli_Runner) reportChargedWarning(result model_Result, output protocol_Map) {
	issues := []string{}
	if result.Activation != "active" {
		issues = append(issues, "订阅待核验")
	}
	if result.Checkpoint.CancelRequested && result.Renewal != "off" {
		issues = append(issues, "续费处理未完成")
	}
	if warning := protocol_AsString(output["warning_code"]); warning != "" {
		issues = append(issues, "后续处理有警告（"+observability_SafeErrorCode(warning)+"）")
	}
	if len(issues) > 0 {
		r.message("已扣款，支付成功；"+strings.Join(issues, "，")+"。请保留 Checkpoint，使用 --recover 继续核验原交易；不要重新发起支付。", "WARNING")
	}
}

// ---- internal/cli/runner.go ----
// Package cli 将终端交互与协议库分离，所有自动测试都可注入离线客户端。

type cli_Client interface {
	CreateCheckout(context.Context, model_Session, gptpay_CreateCheckoutOptions) (model_CheckoutResult, error)
	Pay(context.Context, model_Session, model_Card, gptpay_PayOptions) (model_Result, error)
	Recover(context.Context, model_Checkpoint) (model_Result, error)
	CancelRenewal(context.Context, model_Session, string) (model_Result, error)
}
type cli_CatalogClient interface {
	CatalogPrices(context.Context, model_Session, gptpay_CatalogPricesOptions) (model_CatalogPrices, error)
}

type cli_Runner struct {
	ActionServer         func(context.Context, paymentaction_Credentials, func(string) error, string, string, func(string, string)) error
	CatalogClientFactory func(model_Config, gptpay_ClientOptions) cli_CatalogClient
	In                   io.Reader
	Out, Err             io.Writer
	DataDir              string
	ClientFactory        func(model_Config, gptpay_ClientOptions) cli_Client
	SessionInput         func(context.Context, string) (model_Session, error)
	CardInput            func(context.Context) (model_Card, error)
	BillingInput         func(string) (model_Billing, error)
	LocalKey             func(string, bool) (string, error)
	OpenStore            func(string, string, bool) (*checkpointstore_Store, error)
}

func cli_NewRunner() *cli_Runner {
	r := &cli_Runner{In: os.Stdin, Out: os.Stdout, Err: os.Stderr, DataDir: "data", ClientFactory: func(c model_Config, o gptpay_ClientOptions) cli_Client { return gptpay_NewPaymentClient(c, o) }, CardInput: cli_ReadHiddenCard, BillingInput: cli_ReadBilling, LocalKey: checkpointstore_LocalKey, OpenStore: checkpointstore_Open}
	r.CatalogClientFactory = func(c model_Config, o gptpay_ClientOptions) cli_CatalogClient { return gptpay_NewPaymentClient(c, o) }
	r.SessionInput = r.readSession
	return r
}
func (r *cli_Runner) readSession(ctx context.Context, path string) (model_Session, error) {
	if path != "" {
		return cli_ReadSessionFile(path)
	}
	if file, ok := r.In.(*os.File); ok && term.IsTerminal(int(file.Fd())) {
		terminal, err := cli_terminalOutput(file)
		if err != nil {
			return model_Session{}, cli_ErrInput
		}
		defer terminal.Close()
		return cli_PasteSession(ctx, file, terminal)
	}
	return cli_ReadSessionStream(r.In)
}
func cli_formatMessage(text, level string) string {
	return observability_FormatTimestamp(time.Now()) + " " + level + " " + text
}
func (r *cli_Runner) message(text, level string) error {
	_, err := fmt.Fprintln(r.Err, cli_formatMessage(text, level))
	return err
}
func (r *cli_Runner) output(value any) error {
	encoder := json.NewEncoder(r.Out)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}
func (r *cli_Runner) observer(debug bool) model_EventSink {
	return func(event model_Event) {
		if debug || observability_IsKeyEvent(event) {
			fmt.Fprintln(r.Err, observability_FormatEvent(event))
		}
	}
}
func (r *cli_Runner) keyPath() string { return filepath.Join(r.DataDir, ".checkpoint-key") }
func cli_offsetNotice(r *cli_Runner, offset int) {
	if offset != 0 {
		r.message("指纹时钟偏移：-24 小时；日志与超时使用真实时间。", "INFO")
	}
}

// ---- internal/cli/terminal.go ----

func cli_PasteSession(ctx context.Context, source *os.File, terminal io.Writer) (model_Session, error) {
	if !term.IsTerminal(int(source.Fd())) {
		return model_Session{}, cli_ErrInput
	}
	restore, err := cli_hiddenSettings(source, true)
	if err != nil {
		return model_Session{}, cli_ErrInput
	}
	defer restore()
	fmt.Fprintln(terminal, "\x1b[?2004h"+cli_formatMessage("粘贴 Session JSON（内容不回显，完整后自动继续；Ctrl+C 取消）：", "INFO"))
	defer fmt.Fprint(terminal, "\x1b[?2004l")
	const beginning = "\x1b[200~"
	const ending = "\x1b[201~"
	raw := []byte{}
	buffer := make([]byte, 4096)
	for {
		n, err := cli_terminalRead(ctx, source, buffer)
		if err != nil {
			return model_Session{}, err
		}
		if n == 0 {
			return model_Session{}, errors.New("Session JSON input ended before completion")
		}
		raw = append(raw, buffer[:n]...)
		if len(raw) > (model_SessionJSONMaxChars+len(beginning)+len(ending))*4 {
			return model_Session{}, errors.New("Session JSON exceeds the input limit")
		}
		validLength := 0
		for validLength < len(raw) {
			r, size := utf8.DecodeRune(raw[validLength:])
			if r == utf8.RuneError && size == 1 {
				if !utf8.FullRune(raw[validLength:]) {
					break
				}
				return model_Session{}, errors.New("Session JSON input must use UTF-8 encoding")
			}
			validLength += size
		}
		if validLength != len(raw) {
			continue
		}
		if utf8.RuneCount(raw) > model_SessionJSONMaxChars+len(beginning)+len(ending) {
			return model_Session{}, errors.New("Session JSON exceeds the input limit")
		}
		content := strings.TrimLeft(string(raw), " \t\r\n\v\f")
		if content == "" || strings.HasPrefix(beginning, content) {
			continue
		}
		if strings.HasPrefix(content, beginning) {
			index := strings.Index(content, ending)
			if index < 0 {
				continue
			}
			content = content[len(beginning):index] + content[index+len(ending):]
		} else if strings.Contains(content, "\x04") {
			content = strings.ReplaceAll(content, "\x04", "")
		} else if !strings.HasPrefix(content, "{") {
			return model_Session{}, errors.New("Paste a JSON object without Markdown code fences")
		} else if !cli_jsonClosed(content) {
			continue
		}
		session, err := cli_parseSessionInput(content)
		if err != nil {
			return model_Session{}, err
		}
		fmt.Fprintln(terminal, cli_formatMessage("Session JSON 校验通过。", "INFO"))
		return session, nil
	}
}

func cli_ReadHiddenCard(ctx context.Context) (model_Card, error) {
	terminal, err := cli_openCardTerminal()
	if err != nil {
		return model_Card{}, cli_ErrInput
	}
	defer terminal.Close()
	return cli_readHiddenCardTerminal(ctx, terminal)
}
func cli_readHiddenCardTerminal(ctx context.Context, terminal *os.File) (model_Card, error) {
	if !term.IsTerminal(int(terminal.Fd())) {
		return model_Card{}, cli_ErrInput
	}
	output, err := cli_terminalOutput(terminal)
	if err != nil {
		return model_Card{}, cli_ErrInput
	}
	defer output.Close()
	restore, err := cli_hiddenSettings(terminal, false)
	if err != nil {
		return model_Card{}, cli_ErrInput
	}
	defer restore()
	fmt.Fprint(output, cli_formatMessage("粘贴卡片信息（卡号----MM/YY----CVV，输入不回显，按回车继续）：", "INFO"))
	raw := []byte{}
	buffer := make([]byte, 512)
	for {
		n, err := cli_terminalRead(ctx, terminal, buffer)
		if err != nil {
			return model_Card{}, err
		}
		if n == 0 {
			return model_Card{}, cli_ErrInput
		}
		raw = append(raw, buffer[:n]...)
		if len(raw) > 4096 {
			return model_Card{}, cli_ErrInput
		}
		if strings.ContainsAny(string(raw), "\r\n") {
			fmt.Fprintln(output)
			return cli_ParseCard(strings.TrimRight(string(raw), "\r\n"))
		}
	}
}

// ---- internal/cli/terminal_darwin.go ----

// 两个平台 ioctl 编号不同；画像仍由协议配置选择，不随运行系统改变。
func cli_getTerminalSettings(fd int) (*unix.Termios, error) {
	var request uint = 0x5401
	if runtime.GOOS == "darwin" {
		request = 0x40487413
	}
	return unix.IoctlGetTermios(fd, request)
}
func cli_setTerminalSettings(fd int, value *unix.Termios) error {
	var request uint = 0x5402
	if runtime.GOOS == "darwin" {
		request = 0x80487414
	}
	return unix.IoctlSetTermios(fd, request, value)
}
func cli_flushTerminal(fd int) {
	if runtime.GOOS == "darwin" {
		_ = unix.IoctlSetPointerInt(fd, 0x80047410, 1)
		return
	}
	_ = unix.IoctlSetInt(fd, 0x540b, 0)
}

// ---- internal/cli/terminal_unix.go ----

func cli_terminalRead(ctx context.Context, file *os.File, buffer []byte) (int, error) {
	for {
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		poll := []unix.PollFd{{Fd: int32(file.Fd()), Events: unix.POLLIN}}
		n, err := unix.Poll(poll, 100)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return 0, err
		}
		if n > 0 {
			return file.Read(buffer)
		}
	}
}

func cli_hiddenSettings(file *os.File, noncanonical bool) (func(), error) {
	fd := int(file.Fd())
	old, err := cli_getTerminalSettings(fd)
	if err != nil {
		return nil, err
	}
	next := *old
	next.Lflag &^= unix.ECHO | unix.ECHONL
	if noncanonical {
		next.Lflag &^= unix.ICANON
		next.Cc[unix.VMIN] = 1
		next.Cc[unix.VTIME] = 0
	}
	if err = cli_setTerminalSettings(fd, &next); err != nil {
		return nil, err
	}
	return func() { cli_flushTerminal(fd); _ = cli_setTerminalSettings(fd, old) }, nil
}

func cli_openCardTerminal() (*os.File, error) { return os.OpenFile("/dev/tty", os.O_RDWR, 0) }
func cli_terminalOutput(source *os.File) (*os.File, error) {
	return os.OpenFile(source.Name(), os.O_WRONLY, 0)
}
func cli_quoteTerminalArgument(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

// ---- internal/fingerprint/clock.go ----
// Package fingerprint 提供与真实计时隔离的协议指纹时钟。

// Clock 只偏移协议指纹的墙上时钟，不参与截止时间或真实耗时。
type fingerprint_Clock struct {
	OffsetHours int
	Now         func() time.Time
}

func fingerprint_NewClock(offset int) (fingerprint_Clock, error) {
	if offset != 0 && offset != -24 {
		return fingerprint_Clock{}, fmt.Errorf("fingerprint_clock_offset_hours must be 0 or -24")
	}
	return fingerprint_Clock{OffsetHours: offset}, nil
}

func (c fingerprint_Clock) NowMS() float64 {
	now := time.Now
	if c.Now != nil {
		now = c.Now
	}
	t := now()
	return (float64(t.Unix()) + float64(t.Nanosecond())/1e9 + float64(c.OffsetHours)*3600) * 1000
}

func (c fingerprint_Clock) LocalTime(zone string) (time.Time, error) {
	if zone == "" {
		zone = "UTC"
	}
	loc, err := time.LoadLocation(zone)
	if err != nil {
		return time.Time{}, err
	}
	seconds := c.NowMS() / 1000
	return time.Unix(int64(seconds), int64((seconds-float64(int64(seconds)))*1e9)).In(loc), nil
}

func (c fingerprint_Clock) TimezoneOffsetMin(zone string) (int, error) {
	t, err := c.LocalTime(zone)
	if err != nil {
		return 0, err
	}
	_, seconds := t.Zone()
	return -(seconds / 60), nil
}

// ---- internal/observability/action.go ----

// SafeActionType 只允许类型标签，未知值不携带验证链接或客户端密钥。
func observability_SafeActionType(value string) string {
	switch value {
	case "intent_confirmation_challenge", "stripe_js", "three_d_secure_2_fingerprint", "three_d_secure_2_challenge", "three_d_secure_2_source", "use_stripe_sdk", "redirect_to_url", "three_d_secure_redirect", "stripe_3ds2_fingerprint", "stripe_3ds2_challenge", "verify_with_microdeposits", "display_bank_transfer_instructions":
		return value
	default:
		return "redacted"
	}
}

// SafePaymentStatus 不输出未知的自由文本，保留排查所需的有限状态枚举。
func observability_SafePaymentStatus(value string) string {
	switch value {
	case "open", "complete", "expired", "paid", "unpaid", "no_payment_required", "succeeded", "processing", "pending", "requires_payment_method", "requires_confirmation", "requires_action", "requires_capture", "requires_approval", "canceled", "cancelled", "declined", "failed", "blocked":
		return value
	default:
		return "redacted"
	}
}
func observability_SafeDiagnosticSource(value string) string {
	switch value {
	case "response", "payment_intent", "submission_payment_intent", "error_payment_intent", "submission_attempt":
		return value
	default:
		return "redacted"
	}
}

// ---- internal/observability/diagnostic_secrets.go ----

// RequestDiagnosticSecrets 仅在失败观测时提取已知请求值，用于遮盖服务端回显。
// 有界遍历不调用用户提供的 Stringer，不保留正文，也不执行 SDK 对象中的方法。
func observability_RequestDiagnosticSecrets(rawURL string, options protocol_RequestOptions) []string {
	values := []string{}
	seen := map[string]bool{}
	add := func(value string) {
		if len(value) >= 1 && len(value) <= observability_diagnosticBodyLimit && len(values) < 512 && !seen[value] {
			values, seen[value] = append(values, value), true
		}
	}
	if u, err := url.Parse(rawURL); err == nil {
		for _, value := range observability_providerID.FindAllString(u.Path, -1) {
			add(value)
		}
		if secret, found := strings.CutPrefix(u.Path, "/p/session/"); found {
			add(secret)
		}
		for _, entries := range u.Query() {
			for _, value := range entries {
				add(value)
			}
		}
		if u.User != nil {
			add(u.User.Username())
			password, _ := u.User.Password()
			add(password)
		}
	}
	for _, field := range options.Headers {
		value, ok := field.Value.(string)
		if !ok {
			continue
		}
		add(value)
		if strings.EqualFold(field.Key, "Authorization") || strings.EqualFold(field.Key, "Proxy-Authorization") {
			kind, token, _ := strings.Cut(value, " ")
			add(strings.TrimSpace(token))
			if strings.EqualFold(kind, "Basic") {
				if decoded, err := base64.StdEncoding.DecodeString(token); err == nil {
					user, password, _ := strings.Cut(string(decoded), ":")
					add(user)
					add(password)
				}
			}
		}
		if strings.EqualFold(field.Key, "Cookie") {
			for _, part := range strings.Split(value, ";") {
				_, cookie, _ := strings.Cut(part, "=")
				add(strings.TrimSpace(cookie))
			}
		}
	}
	remaining := 2048
	var walk func(reflect.Value, int)
	walk = func(value reflect.Value, depth int) {
		remaining--
		if remaining < 0 || depth > 16 || !value.IsValid() {
			return
		}
		if value.Type() == reflect.TypeOf(protocol_Field{}) {
			walk(value.FieldByName("Value"), depth+1)
			return
		}
		switch value.Kind() {
		case reflect.Interface, reflect.Pointer:
			if !value.IsNil() {
				walk(value.Elem(), depth+1)
			}
		case reflect.String:
			add(value.String())
		case reflect.Map:
			iterator := value.MapRange()
			for iterator.Next() && remaining > 0 {
				walk(iterator.Value(), depth+1)
			}
		case reflect.Slice, reflect.Array:
			for i := 0; i < value.Len() && remaining > 0; i++ {
				walk(value.Index(i), depth+1)
			}
		case reflect.Struct:
			for i := 0; i < value.NumField() && remaining > 0; i++ {
				if value.Type().Field(i).IsExported() {
					walk(value.Field(i), depth+1)
				}
			}
		}
	}
	walk(reflect.ValueOf(options.Form), 0)
	walk(reflect.ValueOf(options.Body), 0)
	if options.Text != nil {
		add(*options.Text)
		if len(*options.Text) <= observability_diagnosticBodyLimit {
			var decoded any
			if json.Unmarshal([]byte(*options.Text), &decoded) == nil {
				walk(reflect.ValueOf(decoded), 0)
			}
		}
	}
	return values
}

// ---- internal/observability/format.go ----

const observability_slowOperationMS = 5000

// FormatTimestamp 使用运行机器的时区；不使用协议的国家、时区或指纹偏移。
func observability_FormatTimestamp(at time.Time) string {
	return at.In(time.Local).Format("2006-01-02T15:04:05.000-07:00")
}

func observability_IsKeyEvent(e model_Event) bool {
	if protocol_AsString(e["warningCode"]) != "" {
		return true
	}
	event := protocol_AsString(e["event"])
	if protocol_StringSet("transaction", "phase", "price", "payment_confirm", "payment_error", "payment_diagnostic", "checkout_route", "radar_session", "radar_attachment", "account")[event] || event == "checkpoint" && e["outcome"] == "failed" {
		return true
	}
	if event != "http_request" && event != "sentinel" {
		return false
	}
	if e["outcome"] == "retry" || e["outcome"] == "failed" {
		return true
	}
	duration, ok := e["durationMs"].(float64)
	if !ok {
		value, _ := protocol_Integer(e["durationMs"])
		duration = float64(value)
	}
	return duration >= observability_slowOperationMS
}

func observability_FormatEvent(e model_Event) string {
	level := "DEBUG"
	if observability_IsKeyEvent(e) {
		level = "INFO"
		if e["outcome"] == "failed" || protocol_AsString(e["warningCode"]) != "" {
			level = "WARNING"
		}
	}
	parts := []string{}
	for _, k := range strings.Fields("event traceId operation phase service method path checkoutPrefix paymentRoute flow planType renewal activeUntil executor browserProfile attempt status outcome paymentState intentStatus diagnosticSource rawStatus rawState rawPaymentStatus nextActionType sdkActionType sdkHasStripeJS sdkHasThreeDS2Source sdkHasServerTransactionID sdkHasDirectoryServerName errorCode warningCode providerCode declineCode requestId responseContentType responseErrorCode responseErrorType responseMessage responseDetail responseTitle responseSummary responseTruncated cfRay responseRequestId paymentElapsedMs durationMs elapsedMs remainingDeadlineMs") {
		v, ok := e[k]
		if ok && v != nil && protocol_AsString(v) != "" {
			value := protocol_AsString(v)
			switch k {
			case "sdkHasStripeJS", "sdkHasThreeDS2Source", "sdkHasServerTransactionID", "sdkHasDirectoryServerName":
				b, ok := v.(bool)
				if !ok {
					continue
				}
				value = strconv.FormatBool(b)
			case "diagnosticSource":
				value = observability_SafeDiagnosticSource(value)
			case "rawStatus", "rawState", "rawPaymentStatus":
				value = observability_SafePaymentStatus(value)
			case "nextActionType", "sdkActionType":
				value = observability_SafeActionType(value)
			case "warningCode":
				value = observability_SafeErrorCode(v)
			case "paymentElapsedMs":
				elapsed, ok := observability_paymentElapsedMilliseconds(v)
				if !ok {
					continue
				}
				value = protocol_AsString(elapsed)
			case "responseMessage", "responseDetail", "responseTitle", "responseSummary":
				value = strconv.Quote(observability_SafeResponseText(value, nil))
			case "responseErrorCode", "responseErrorType":
				value = observability_safeResponseCode(value, nil)
			case "responseContentType":
				value = observability_safeContentType(value)
			case "responseTruncated":
				truncated, ok := v.(bool)
				if !ok {
					continue
				}
				value = strconv.FormatBool(truncated)
			case "cfRay":
				if !observability_diagnosticRay.MatchString(value) {
					continue
				}
			case "responseRequestId":
				if !observability_diagnosticRequestID.MatchString(value) {
					continue
				}
			case "checkoutPrefix":
				if !protocol_StringSet("oaics_", "cs_live_")[value] {
					continue
				}
			case "paymentRoute":
				if !protocol_StringSet("chatgpt", "stripe")[value] {
					continue
				}
			case "browserProfile":
				if !protocol_StringSet("chrome131-windows-v1", "chrome131-macos-v1")[value] {
					continue
				}
			}
			if value != "" {
				parts = append(parts, k+"="+value)
			}
		}
	}
	// 只转换显示，保留 EventSink 收到的 UTC 时间戳和原始事件对象。
	stamp := protocol_AsString(e["timestamp"])
	if parsed, err := time.Parse(time.RFC3339Nano, stamp); err == nil {
		stamp = observability_FormatTimestamp(parsed)
	}
	return fmt.Sprintf("%s %-7s protocol %s", stamp, level, strings.Join(parts, " "))
}

// ---- internal/observability/http_diagnostics.go ----

// HTTPFailureDiagnostics 仅允许少量错误字段和关联标识进入 EventSink；不返回原始正文。
func observability_HTTPFailureDiagnostics(response *protocol_HTTPResponse, secrets []string) protocol_Map {
	result := protocol_Map{}
	if response == nil || response.StatusCode < 300 {
		return result
	}
	secrets = append([]string(nil), secrets...)
	for _, value := range response.Cookies {
		if value != "" {
			secrets = append(secrets, value)
		}
	}
	media := observability_safeContentType(response.Headers.Get("Content-Type"))
	if media != "" {
		result["response_content_type"] = media
	}
	if value := response.Headers.Get("CF-Ray"); observability_diagnosticRay.MatchString(value) {
		result["cf_ray"] = value
	}
	for _, header := range []string{"Request-Id", "X-Request-Id"} {
		if value := response.Headers.Get(header); observability_diagnosticRequestID.MatchString(value) {
			result["response_request_id"] = value
			break
		}
	}
	content := response.Content
	truncated := len(content) > observability_diagnosticBodyLimit
	if truncated {
		content = content[:observability_diagnosticBodyLimit]
		result["response_truncated"] = true
	}
	put := func(key string, value any) {
		text, ok := value.(string)
		if !ok || text == "" {
			return
		}
		if key == "response_error_code" || key == "response_error_type" {
			result[key] = observability_safeResponseCode(text, secrets)
		} else {
			result[key] = observability_SafeResponseText(text, secrets)
		}
	}
	var decoded map[string]any
	if !truncated && json.Unmarshal(content, &decoded) == nil && decoded != nil {
		node := decoded
		if nested, ok := decoded["error"].(map[string]any); ok {
			node = nested
		} else if nested, ok := decoded["detail"].(map[string]any); ok {
			node = nested
		}
		put("response_error_code", node["code"])
		put("response_error_type", node["type"])
		if param, ok := node["param"].(string); ok && param != "" {
			result["provider_param"] = protocol_SafeProviderParam(param)
		}
		put("response_message", node["message"])
		put("response_detail", node["detail"])
		if value, ok := decoded["error"].(string); ok {
			put("response_message", value)
		}
		if items, ok := decoded["detail"].([]any); ok && len(items) > 0 {
			if first, ok := items[0].(map[string]any); ok {
				put("response_message", first["msg"])
				put("response_error_type", first["type"])
				if loc, ok := first["loc"].([]any); ok {
					parts := make([]string, 0, len(loc))
					for _, item := range loc {
						if text, ok := item.(string); ok && text != "" {
							parts = append(parts, text)
						}
					}
					put("response_detail", strings.Join(parts, "."))
				}
			}
		}
		return result
	}
	if media == "text/html" || strings.HasPrefix(strings.ToLower(strings.TrimSpace(string(content))), "<!doctype html") || strings.HasPrefix(strings.TrimSpace(string(content)), "<html") {
		title, summary := observability_htmlErrorText(string(content))
		put("response_title", title)
		put("response_summary", summary)
		if truncated && result["response_summary"] == nil {
			result["response_summary"] = "Response body omitted: size limit"
		}
		return result
	}
	if truncated {
		result["response_summary"] = "Response body omitted: size limit"
		return result
	}
	if media == "application/xml" || media == "text/xml" {
		var failure struct {
			XMLName xml.Name `xml:"Error"`
			Code    string   `xml:"Code"`
			Message string   `xml:"Message"`
		}
		if xml.Unmarshal(content, &failure) == nil {
			put("response_error_code", failure.Code)
			put("response_message", failure.Message)
		}
		return result
	}
	if media == "text/plain" {
		put("response_summary", string(content))
	}
	return result
}

var observability_htmlDiagnosticBlockedTags = protocol_StringSet("script", "style", "form", "textarea", "select", "template", "noscript", "svg", "math", "pre", "code")

func observability_htmlErrorText(source string) (string, string) {
	tokens := html.NewTokenizer(strings.NewReader(source))
	title, summary := []string{}, []string{}
	blocked, inTitle := "", false
	blockedDepth := 0
	for {
		kind := tokens.Next()
		if kind == html.ErrorToken {
			break
		}
		token := tokens.Token()
		if kind == html.StartTagToken {
			if blocked != "" {
				if token.Data == blocked {
					blockedDepth++
				}
			} else if observability_htmlDiagnosticBlockedTags[token.Data] {
				blocked, blockedDepth = token.Data, 1
			}
			if token.Data == "title" {
				inTitle = true
			}
		}
		if kind == html.EndTagToken {
			if token.Data == blocked {
				blockedDepth--
				if blockedDepth == 0 {
					blocked = ""
				}
			}
			if token.Data == "title" {
				inTitle = false
			}
		}
		if kind != html.TextToken || blocked != "" {
			continue
		}
		if inTitle {
			title = append(title, token.Data)
		} else {
			summary = append(summary, token.Data)
		}
	}
	return strings.Join(title, " "), strings.Join(summary, " ")
}

// ---- internal/observability/observer.go ----

type observability_Observer struct {
	Operation, TraceID    string
	sink                  model_EventSink
	started, phaseStarted time.Time
	phase                 string
}

func observability_NewObserver(operation string, sink model_EventSink, traceID, phase string) *observability_Observer {
	if !protocol_Matches(`[0-9a-f]{32}`, traceID) {
		traceID = strings.ReplaceAll(protocol_RandomUUID(), "-", "")
	}
	now := time.Now()
	return &observability_Observer{Operation: observability_safeString(operation), TraceID: traceID, sink: sink, started: now, phaseStarted: now, phase: phase}
}

var observability_eventFields = map[string]string{"checkout_prefix": "checkoutPrefix", "payment_route": "paymentRoute", "sdk_has_stripe_js": "sdkHasStripeJS", "sdk_has_three_d_secure_2_source": "sdkHasThreeDS2Source", "sdk_has_server_transaction_id": "sdkHasServerTransactionID", "sdk_has_directory_server_name": "sdkHasDirectoryServerName", "diagnostic_source": "diagnosticSource", "raw_status": "rawStatus", "raw_state": "rawState", "raw_payment_status": "rawPaymentStatus", "next_action_type": "nextActionType", "sdk_action_type": "sdkActionType", "network_stage": "networkStage", "connection_ready_ms": "connectionReadyMs", "request_write_ms": "requestWriteMs", "first_byte_ms": "firstByteMs", "response_wait_ms": "responseWaitMs", "network_stage_elapsed_ms": "networkStageElapsedMs", "plan_type": "planType", "renewal": "renewal", "active_until": "activeUntil", "attempt": "attempt", "retry_wait_ms": "retryWaitMs", "retry_wait_elapsed_ms": "retryWaitElapsedMs", "duration_ms": "durationMs", "payment_elapsed_ms": "paymentElapsedMs", "error_code": "errorCode", "warning_code": "warningCode", "provider_param": "providerParam", "provider_code": "providerCode", "decline_code": "declineCode", "request_id": "requestId", "executor": "executor", "flow": "flow", "intent_status": "intentStatus", "method": "method", "outcome": "outcome", "path": "path", "payment_state": "paymentState", "phase": "phase", "remaining_deadline_ms": "remainingDeadlineMs", "service": "service", "status": "status", "browser_profile": "browserProfile", "response_content_type": "responseContentType", "response_error_code": "responseErrorCode", "response_error_type": "responseErrorType", "response_message": "responseMessage", "response_detail": "responseDetail", "response_title": "responseTitle", "response_summary": "responseSummary", "response_truncated": "responseTruncated", "cf_ray": "cfRay", "response_request_id": "responseRequestId"}

var observability_safePattern = regexp.MustCompile(`[^A-Za-z0-9_.:/{}-]+`)

var observability_providerID = regexp.MustCompile(`(?:oaics_|cs_live_|cs_test_|pi_|seti_|pm_|ctoken_|bps_)[A-Za-z0-9_]+`)

func observability_safeString(s string) string {
	s = observability_safePattern.ReplaceAllString(s, "_")
	if len(s) > 160 {
		s = s[:160]
	}
	return s
}

func observability_NormalizeHTTPPath(raw string) string {
	u, e := url.Parse(raw)
	if e != nil {
		return "/"
	}
	p := u.Path
	if p == "" {
		p = "/"
	}
	// Customer Portal 的路径本身携带访问凭据，仅移除 query 仍会泄漏。
	if strings.HasPrefix(p, "/p/session/") {
		return "/p/session/{id}"
	}
	return observability_providerID.ReplaceAllString(p, "{id}")
}

var observability_knownErrors = protocol_StringSet("radar_session_missing", "billing_automatic_tax_unrecognized_customer_location", "parameter_unknown", "catalog_prices_invalid", "catalog_prices_region_mismatch", "protocol_error", "network_timeout", "account_mismatch", "account_not_found", "action_required", "activation_pending", "subscription_not_found", "approval_blocked", "authorized", "cancellation_pending", "charged", "checkout_amount_changed", "checkout_currency_mismatch", "checkout_id_invalid", "checkout_total_missing", "checkout_price_missing", "checkout_price_failed", "checkout_context_mismatch", "checkpoint_failed", "confirmation_token_missing", "deadline_exceeded", "device_id_missing", "existing_subscription", "homepage_unavailable", "hosted_page_unavailable", "hosted_url_invalid", "hosted_url_missing", "https_required", "init_checksum_missing", "invalid_json_response", "invalid_return_url", "network_error", "not_attempted", "not_charged", "payment_intent_secret_missing", "payment_intent_state_unavailable", "payment_confirmation_already_submitted", "payment_method_missing", "payment_page_id_missing", "payment_not_submitted", "payment_pending", "pending", "personal_account_required", "processor_entity_invalid", "publishable_key_missing", "recovery_failed", "renewal_state_unknown", "sentinel_challenge_missing", "sentinel_context_mismatch", "sentinel_dx_missing", "sentinel_incomplete", "sentinel_pow_missing", "sentinel_turnstile_error", "sentinel_turnstile_missing", "session_expired", "session_token_required", "stripe_build_missing", "stripe_identity_missing", "unexpected_redirect", "unknown", "unsupported_processor_entity", "checkout_ui_mode_invalid", "payment_approval_failed", "payment_cancelled", "input_or_checkpoint_invalid", "cancelled")

var observability_providerCodes = protocol_StringSet("billing_automatic_tax_unrecognized_customer_location", "parameter_unknown", "payment_method_failed", "payment_method_declined", "card_declined", "expired_card", "incorrect_cvc", "incorrect_number", "incorrect_zip", "invalid_cvc", "invalid_expiry_month", "invalid_expiry_year", "invalid_number", "processing_error", "payment_intent_unexpected_state", "payment_intent_authentication_failure", "authentication_required")

var observability_declineCodes = protocol_StringSet("payment_method_declined", "payment_method_failed", "card_declined", "do_not_honor", "generic_decline", "insufficient_funds", "duplicate_transaction", "transaction_not_allowed", "card_not_supported", "currency_not_supported", "expired_card", "incorrect_cvc", "incorrect_number", "incorrect_zip", "invalid_account", "invalid_amount", "issuer_not_available", "try_again_later", "processing_error", "reenter_transaction", "restricted_card", "fraudulent", "lost_card", "stolen_card", "merchant_blacklist", "authentication_required", "approve_with_id", "call_issuer", "card_velocity_exceeded", "no_action_taken", "not_permitted", "pickup_card", "revocation_of_authorization", "revocation_of_all_authorizations", "security_violation", "service_not_allowed", "stop_payment_order", "testmode_decline", "withdrawal_count_limit_exceeded")

func observability_SafeErrorCode(value any) string {
	var code string
	switch x := value.(type) {
	case string:
		code = x
	case error:
		var pe *protocol_ProtocolError
		if errors.As(x, &pe) {
			code = pe.Code
		} else {
			return "network_error"
		}
	default:
		return "redacted_error"
	}
	if observability_knownErrors[code] || protocol_Matches(`http_[1-5][0-9]{2}`, code) || protocol_Matches(`protocol_error_[A-Za-z][A-Za-z0-9]{0,63}`, code) {
		return code
	}
	return "redacted_error"
}

func (o *observability_Observer) Emit(event string, fields protocol_Map) {
	if o == nil {
		return
	}
	defer func() { _ = recover() }()
	now := time.Now()
	payload := model_Event{"timestamp": now.UTC().Format("2006-01-02T15:04:05.000+00:00"), "event": observability_safeString(event), "traceId": o.TraceID, "operation": o.Operation, "elapsedMs": math.RoundToEven(now.Sub(o.started).Seconds() * 1000)}
	for k, target := range observability_eventFields {
		v, ok := fields[k]
		if !ok || v == nil {
			continue
		}
		if s, ok := v.(string); ok && s == "" {
			continue
		}
		switch k {
		case "checkout_prefix":
			if value, ok := v.(string); ok && protocol_StringSet("oaics_", "cs_live_")[value] {
				payload[target] = value
			}
		case "payment_route":
			if value, ok := v.(string); ok && protocol_StringSet("chatgpt", "stripe")[value] {
				payload[target] = value
			}
		case "response_truncated":
			if value, ok := v.(bool); ok {
				payload[target] = value
			}
		case "browser_profile":
			if value, ok := v.(string); ok && protocol_StringSet("chrome131-windows-v1", "chrome131-macos-v1")[value] {
				payload[target] = value
			}
		case "plan_type":
			if value, ok := v.(string); ok && (strings.ToLower(value) == "free" || model_IsSubscriptionType(strings.ToLower(value))) {
				payload[target] = strings.ToLower(value)
			}
		case "renewal":
			if value, ok := v.(string); ok && protocol_StringSet("on", "off", "unknown")[value] {
				payload[target] = value
			}
		case "active_until":
			if value, ok := v.(string); ok {
				if ts, err := time.Parse(time.RFC3339, value); err == nil {
					payload[target] = ts.UTC().Format("2006-01-02T15:04:05Z")
				}
			}
		case "flow":
			if value, ok := v.(string); ok && protocol_StringSet("checkout", "subscription_update", "existing_subscription")[value] {
				payload[target] = value
			} else {
				payload[target] = observability_safeString(protocol_AsString(v))
			}
		case "response_content_type":
			if value, ok := v.(string); ok {
				if media := observability_safeContentType(value); media != "" {
					payload[target] = media
				}
			}
		case "response_error_code", "response_error_type":
			if value, ok := v.(string); ok {
				payload[target] = observability_safeResponseCode(value, nil)
			}
		case "response_message", "response_detail", "response_title", "response_summary":
			if value, ok := v.(string); ok {
				payload[target] = observability_SafeResponseText(value, nil)
			}
		case "cf_ray":
			if value, ok := v.(string); ok && observability_diagnosticRay.MatchString(value) {
				payload[target] = value
			}
		case "response_request_id":
			if value, ok := v.(string); ok && observability_diagnosticRequestID.MatchString(value) {
				payload[target] = value
			}
		case "request_id":
			s, ok := v.(string)
			if ok && protocol_Matches(`req_[A-Za-z0-9]{1,80}`, s) {
				payload[target] = s
			} else {
				payload[target] = "redacted"
			}
		case "sdk_has_stripe_js", "sdk_has_three_d_secure_2_source", "sdk_has_server_transaction_id", "sdk_has_directory_server_name":
			if value, ok := v.(bool); ok {
				payload[target] = value
			}
		case "diagnostic_source":
			payload[target] = observability_SafeDiagnosticSource(protocol_AsString(v))
		case "raw_status", "raw_state", "raw_payment_status":
			payload[target] = observability_SafePaymentStatus(protocol_AsString(v))
		case "next_action_type", "sdk_action_type":
			payload[target] = observability_SafeActionType(protocol_AsString(v))
		case "network_stage":
			if value, ok := v.(string); ok && (value == "connection" || value == "request_write" || value == "response_headers" || value == "response_body") {
				payload[target] = value
			}
		case "provider_param":
			payload[target] = protocol_SafeProviderParam(protocol_AsString(v))
		case "provider_code", "decline_code":
			s, ok := v.(string)
			allowed := observability_providerCodes
			if k == "decline_code" {
				allowed = observability_declineCodes
			}
			if ok && allowed[s] {
				payload[target] = s
			} else {
				payload[target] = "redacted_error"
			}
		case "path":
			payload[target] = observability_NormalizeHTTPPath(protocol_AsString(v))
		case "error_code", "warning_code":
			payload[target] = observability_SafeErrorCode(v)
		case "payment_elapsed_ms":
			if elapsed, ok := observability_paymentElapsedMilliseconds(v); ok {
				payload[target] = elapsed
			}
		case "attempt", "status":
			if n, ok := protocol_Integer(v); ok {
				payload[target] = n
			}
		case "connection_ready_ms", "request_write_ms", "first_byte_ms", "response_wait_ms", "network_stage_elapsed_ms", "duration_ms", "remaining_deadline_ms", "retry_wait_ms", "retry_wait_elapsed_ms":
			var f float64
			switch x := v.(type) {
			case float64:
				f = x
			case int:
				f = float64(x)
			case int64:
				f = float64(x)
			}
			payload[target] = math.Max(0, math.RoundToEven(f))
		default:
			payload[target] = observability_safeString(protocol_AsString(v))
		}
	}
	if o.sink != nil {
		o.sink(payload)
	}
}

func (o *observability_Observer) Checkpoint(phase string, changed, persisted bool) {
	if changed {
		if o.phase != "" {
			o.Emit("phase", protocol_Map{"phase": o.phase, "outcome": "completed", "duration_ms": time.Since(o.phaseStarted).Seconds() * 1000})
		}
		o.phase = phase
		o.phaseStarted = time.Now()
	}
	outcome := "skipped"
	if persisted {
		outcome = "saved"
	}
	o.Emit("checkpoint", protocol_Map{"phase": phase, "outcome": outcome})
}

func (o *observability_Observer) Complete(phase, payment, code string, warnings ...string) {
	warning := ""
	if len(warnings) > 0 {
		warning = warnings[0]
	}
	// 已确认的扣款不因激活、续费或后续落盘失败改成支付失败。
	if payment == "charged" {
		if warning == "" {
			warning = code
		}
		code = ""
	}
	outcome := "succeeded"
	phaseOutcome := "completed"
	if code != "" {
		outcome = "failed"
		phaseOutcome = "failed"
	} else if warning != "" {
		phaseOutcome = "pending"
	}
	if o.phase != "" {
		o.Emit("phase", protocol_Map{"phase": o.phase, "outcome": phaseOutcome, "warning_code": warning, "duration_ms": time.Since(o.phaseStarted).Seconds() * 1000})
		o.phase = ""
	}
	o.Emit("transaction", protocol_Map{"phase": phase, "outcome": outcome, "payment_state": payment, "error_code": code, "warning_code": warning})
}

func observability_paymentElapsedMilliseconds(value any) (float64, bool) {
	var elapsed float64
	switch v := value.(type) {
	case float64:
		elapsed = v
	case int:
		elapsed = float64(v)
	case int64:
		elapsed = float64(v)
	default:
		return 0, false
	}
	if math.IsNaN(elapsed) || math.IsInf(elapsed, 0) {
		return 0, false
	}
	return math.Max(0, math.RoundToEven(elapsed)), true
}

// SafePaymentCode 只公开已有白名单错误码，不传递响应原文。
func observability_SafePaymentCode(code string, decline bool) string {
	allowed := observability_providerCodes
	if decline {
		allowed = observability_declineCodes
	}
	if allowed[code] {
		return code
	}
	return ""
}

// ---- internal/observability/response_redaction.go ----

const observability_diagnosticBodyLimit = 32 * 1024
const observability_diagnosticTextLimit = 240

var observability_diagnosticANSI = regexp.MustCompile("\x1b(?:\\[[0-?]*[ -/]*[@-~]|\\][^\x07]*(?:\x07|\x1b\\\\))")
var observability_diagnosticURL = regexp.MustCompile(`(?i)\b(?:https?|socks[45]h?|socks4a)://[^\s<>"']+`)
var observability_diagnosticEmail = regexp.MustCompile(`(?i)\b[A-Z0-9.!#$%&'*+/=?^_` + "`" + `{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b`)
var observability_diagnosticAuth = regexp.MustCompile(`(?i)\b(?:bearer|basic)\s+[^\s,;<>"']+`)
var observability_diagnosticJWT = regexp.MustCompile(`[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`)
var observability_diagnosticCredential = regexp.MustCompile(`(?i)\b(?:sk|pk|rk|sess|session|token|access_token|refresh_token|cs_live|ctoken|pi|seti|pm|cus)_[A-Za-z0-9_=-]+`)
var observability_diagnosticSensitiveLabel = regexp.MustCompile(`(?i)\b(?:authorization|set-cookie|cookie|session|token|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|api[_ -]?key|password|passwd|secret|proxy|card[_ -]?number|cvc|cvv|email|phone|billing|address|name|ip(?: address)?)\b\s*(?::|=|\bis\b)\s*.*`)
var observability_diagnosticQuoted = regexp.MustCompile(`"[^"]*"|'[^']*'|` + "`[^`]*`")
var observability_diagnosticOpaque = regexp.MustCompile(`[A-Za-z0-9_+/=-]{20,}`)
var observability_diagnosticIP = regexp.MustCompile(`\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b|\b(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.%]*`)
var observability_diagnosticNumbers = regexp.MustCompile(`\b(?:[0-9][ -]*){3,}\b`)
var observability_diagnosticCode = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`)
var observability_diagnosticConventionalCode = regexp.MustCompile(`^[a-z]+(?:[_.-][a-z]+)+$`)
var observability_diagnosticHex = regexp.MustCompile(`^[a-fA-F0-9]{16,64}$`)
var observability_diagnosticRay = regexp.MustCompile(`^[a-fA-F0-9]{16,32}-[A-Z]{3}$`)
var observability_diagnosticRequestID = regexp.MustCompile(`^(?:req_[A-Za-z0-9]{1,80}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})$`)

// SafeResponseText 是诊断文本唯一的输出边界；它不输出响应字段、属性或原始异常。
// 请求中已知的值优先遮盖，随后清除常见凭据、个人信息和终端控制序列。
func observability_SafeResponseText(value string, secrets []string) string {
	if len(value) > observability_diagnosticBodyLimit {
		return "[response text omitted: size limit]"
	}
	value = dep_html.UnescapeString(value)
	value = observability_diagnosticANSI.ReplaceAllString(value, " ")
	value = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.In(r, unicode.Cf) {
			if unicode.IsSpace(r) {
				return ' '
			}
			return -1
		}
		return r
	}, value)
	value = observability_redactKnownValues(value, secrets)
	value = strings.Join(strings.Fields(value), " ")
	for _, pattern := range []*regexp.Regexp{observability_diagnosticURL, observability_diagnosticEmail, observability_diagnosticAuth, observability_diagnosticJWT, observability_diagnosticCredential, observability_diagnosticSensitiveLabel, observability_diagnosticQuoted, observability_diagnosticIP, observability_diagnosticOpaque, observability_diagnosticNumbers} {
		value = pattern.ReplaceAllString(value, "[redacted]")
	}
	if utf8.RuneCountInString(value) > observability_diagnosticTextLimit {
		value = string([]rune(value)[:observability_diagnosticTextLimit]) + "…"
	}
	return value
}

// 所有已知值在原文上定位后一次性遮盖，避免后续短值再次改写占位符。
// 很短的 Cookie、开关和地区值必须作为独立词/值出现，不能破坏普通英文单词。
func observability_redactKnownValues(value string, secrets []string) string {
	mask := make([]bool, len(value))
	word := func(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' }
	for _, secret := range secrets {
		if secret == "" || len(secret) > observability_diagnosticBodyLimit {
			continue
		}
		for _, candidate := range []string{secret, url.QueryEscape(secret)} {
			for offset := 0; offset < len(value); {
				found := strings.Index(value[offset:], candidate)
				if found < 0 {
					break
				}
				start := offset + found
				end := start + len(candidate)
				offset = end
				if utf8.RuneCountInString(secret) < 8 {
					first, _ := utf8.DecodeRuneInString(candidate)
					last, _ := utf8.DecodeLastRuneInString(candidate)
					before, _ := utf8.DecodeLastRuneInString(value[:start])
					after, _ := utf8.DecodeRuneInString(value[end:])
					if start > 0 && word(first) && word(before) || end < len(value) && word(last) && word(after) {
						continue
					}
				}
				for index := start; index < end; index++ {
					mask[index] = true
				}
			}
		}
	}
	var result strings.Builder
	for i := 0; i < len(value); {
		if !mask[i] {
			result.WriteByte(value[i])
			i++
			continue
		}
		result.WriteString("[redacted]")
		for i < len(value) && mask[i] {
			i++
		}
	}
	return result.String()
}

func observability_safeResponseCode(value string, secrets []string) string {
	if !observability_diagnosticCode.MatchString(value) || observability_diagnosticCredential.MatchString(value) || observability_diagnosticJWT.MatchString(value) || observability_diagnosticHex.MatchString(value) {
		return "redacted"
	}
	if len(value) >= 20 && !(observability_diagnosticConventionalCode.MatchString(value) || strings.ToUpper(value) == value && observability_diagnosticConventionalCode.MatchString(strings.ToLower(value))) {
		return "redacted"
	}
	if observability_redactKnownValues(value, secrets) != value {
		return "redacted"
	}
	return value
}

func observability_safeContentType(value string) string {
	if len(value) > 256 {
		return ""
	}
	media, _, err := mime.ParseMediaType(value)
	if err != nil || !protocol_Matches(`[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+`, media) {
		return ""
	}
	return strings.ToLower(media)
}

// ---- internal/paymentaction/server.go ----
// Package paymentaction 提供本机官方 SDK 验证交接，不负责创建或确认新付款。

type paymentaction_Credentials struct {
	PublishableKey string `json:"publishableKey"`
	ClientSecret   string `json:"clientSecret"`
}

// Serve 仅在本机随机端口开放限时入口；done 只是浏览器通知，不是扣款证据。
func paymentaction_Serve(ctx context.Context, credentials paymentaction_Credentials, ready func(string) error, report ...func(string, string)) error {
	return paymentaction_serve(ctx, credentials, ready, nil, report...)
}

// ServeTLS 使用调用方证书，不修改系统信任库，不输出私钥错误详情。
func paymentaction_ServeTLS(ctx context.Context, credentials paymentaction_Credentials, ready func(string) error, certFile, keyFile string, report ...func(string, string)) error {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return errors.New("action_tls_certificate_invalid")
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil || leaf.VerifyHostname("127.0.0.1") != nil || time.Now().Before(leaf.NotBefore) || time.Now().After(leaf.NotAfter) {
		return errors.New("action_tls_certificate_invalid")
	}
	return paymentaction_serve(ctx, credentials, ready, &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, report...)
}

func paymentaction_serve(ctx context.Context, credentials paymentaction_Credentials, ready func(string) error, tlsConfig *tls.Config, report ...func(string, string)) error {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return errors.New("action_listener_failed")
	}
	defer listener.Close()
	token := make([]byte, 32)
	if _, err = rand.Read(token); err != nil {
		return errors.New("action_token_failed")
	}
	base := "/" + hex.EncodeToString(token)
	host := listener.Addr().String()
	scheme := "http"
	if tlsConfig != nil {
		listener = tls.NewListener(listener, tlsConfig)
		scheme = "https"
	}
	done := make(chan struct{})
	handler := paymentaction_newHandler(host, base, credentials, done, report...)
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192}
	defer server.Close()
	stopped := make(chan error, 1)
	go func() { stopped <- server.Serve(listener) }()
	if err = ready(scheme + "://" + host + base + "/"); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-done:
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			return errors.New("action_shutdown_failed")
		}
		return nil
	case <-stopped:
		return errors.New("action_server_stopped")
	}
}

func paymentaction_newHandler(host, base string, credentials paymentaction_Credentials, done chan struct{}, report ...func(string, string)) http.Handler {
	var once sync.Once
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self' https://js.stripe.com https://*.js.stripe.com; frame-src https://js.stripe.com https://*.stripe.com; connect-src 'self' https://*.stripe.com; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
		// Host 与 Origin 双校验，阻止 DNS rebinding、跨站读取和跨站完成通知。
		if r.Host != host {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		originBase := "http://" + host
		if r.TLS != nil {
			originBase = "https://" + host
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != originBase {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if site := r.Header.Get("Sec-Fetch-Site"); site != "" && site != "same-origin" && site != "none" {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case base + "/":
			if r.Method != "GET" {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(paymentaction_page))
		case base + "/app.js":
			if r.Method != "GET" {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
			_, _ = w.Write([]byte(paymentaction_script))
		case base + "/context", base + "/complete":
			if r.Method != "POST" {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			if r.Header.Get("Origin") != originBase || r.Header.Get("X-Action-Request") != "1" {
				w.WriteHeader(http.StatusForbidden)
				return
			}
			if strings.HasSuffix(r.URL.Path, "/context") {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(credentials)
			} else {
				var result struct {
					Outcome    string `json:"outcome"`
					Code       string `json:"code"`
					DurationMS int64  `json:"durationMs"`
				}
				decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
				if err := decoder.Decode(&result); err != nil && err != io.EOF {
					w.WriteHeader(http.StatusBadRequest)
					return
				}
				outcome, code := paymentaction_safeResult(result.Outcome, result.Code)
				w.WriteHeader(http.StatusNoContent)
				once.Do(func() {
					for _, sink := range report {
						if sink != nil {
							sink(outcome, code)
							if result.DurationMS >= 0 && result.DurationMS <= 600000 {
								sink("browser_duration_ms", strconv.FormatInt(result.DurationMS, 10))
							}
						}
					}
					close(done)
				})
			}
		default:
			http.NotFound(w, r)
		}
	})
}

const paymentaction_page = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>原付款验证</title><style>body{font:16px system-ui;background:#f4f7fc;color:#18233b;margin:0;padding:48px 20px}main{max-width:540px;margin:auto;background:white;padding:32px;border-radius:16px}button{padding:12px 24px;background:#2563eb;color:white;border:0;border-radius:8px;cursor:pointer}button:disabled{opacity:.5}p{line-height:1.7}</style><main><h1>完成原付款验证</h1><p>此页面仅处理原交易的后续验证，不创建新订单。验证可能需要交互，并可能使原付款完成。</p><button id="start">继续验证原付款</button><p id="status" role="status">保持终端运行；未准备好时可关闭本页并在终端按 Ctrl+C。</p></main><script src="https://js.stripe.com/v3/"></script><script src="app.js"></script></html>`
const paymentaction_script = `"use strict";
const button=document.getElementById("start"), status=document.getElementById("status");
button.addEventListener("click",async()=>{
 button.disabled=true;status.textContent="正在处理官方验证，请勿重复提交……";
 let stage="context";
 const started=performance.now(); let cspBlocked=false;
 const onCSP=()=>{cspBlocked=true;};
 document.addEventListener("securitypolicyviolation",onCSP);
 try {
  const response=await fetch("context",{method:"POST",headers:{"X-Action-Request":"1"},cache:"no-store",credentials:"omit"});
  if(!response.ok)throw new Error("context_failed");
  const data=await response.json();
  stage="sdk_load";
  if(typeof Stripe!=="function")throw new Error("sdk_unavailable");
  stage="preflight";
  const stripe=Stripe(data.publishableKey);
  const current=await stripe.retrievePaymentIntent(data.clientSecret);
  if(current.error)throw new Error("preflight_failed");
  const expected=data.clientSecret.split("_secret_")[0];
  if(!current.paymentIntent || current.paymentIntent.id!==expected)throw new Error("context_mismatch");
  if(current.paymentIntent.status!=="requires_action")throw new Error("action_not_required");
  stage="sdk_action";
  const result=await stripe.handleNextAction({clientSecret:data.clientSecret});
  const allowed=new Set(["card_declined","payment_intent_unexpected_state","payment_intent_authentication_failure","payment_intent_invalid_parameter","invalid_client_secret","resource_missing","api_connection_error"]);
  const code=result.error?(allowed.has(result.error.code)?result.error.code:"sdk_error"):"";
  status.textContent=result.error?"验证未完成（"+code+"）。正在通知终端核验原交易，请查看终端结果。":"验证步骤已结束，正在通知终端核验原交易。";
  stage="notify";
  const completed=await fetch("complete",{method:"POST",headers:{"X-Action-Request":"1"},credentials:"omit",body:JSON.stringify({outcome:result.error?"sdk_error":"sdk_returned",code,durationMs:Math.round(performance.now()-started)})});
  if(!completed.ok)throw new Error("notify_failed");
 }catch(error){
 const safeCodes=new Set(["preflight_failed","context_mismatch","action_not_required"]);
 const failureCode=cspBlocked?"csp_blocked":safeCodes.has(error.message)?error.message:stage==="sdk_load"?"sdk_unavailable":"unknown";
 if(stage!=="notify"){try{await fetch("complete",{method:"POST",headers:{"X-Action-Request":"1"},credentials:"omit",body:JSON.stringify({outcome:stage==="sdk_action"?"sdk_exception":"page_error",code:failureCode,durationMs:Math.round(performance.now()-started)})});}catch(_){}}
 const labels={context:"读取本机验证上下文失败",sdk_load:"官方 SDK 未加载",preflight:"原交易状态核验未通过，未启动验证",sdk_action:"官方 SDK 验证未完成",notify:"通知终端失败"};status.textContent=labels[stage]+"。请检查网络和终端，不要重新付款；可关闭本页后核验原交易。";}finally{document.removeEventListener("securitypolicyviolation",onCSP);}
});`

// 浏览器内容不可信：只允许有限枚举进入终端，不传消息、网址和 SDK 载荷。
func paymentaction_safeResult(outcome, code string) (string, string) {
	switch outcome {
	case "sdk_returned", "sdk_error", "sdk_exception", "page_error":
	default:
		outcome = "unknown"
	}
	switch code {
	case "card_declined", "csp_blocked", "sdk_unavailable", "preflight_failed", "context_mismatch", "action_not_required", "payment_intent_unexpected_state", "payment_intent_authentication_failure", "payment_intent_invalid_parameter", "invalid_client_secret", "resource_missing", "api_connection_error":
	default:
		code = "unknown"
	}
	return outcome, code
}

// ---- internal/sentinel/codec.go ----

func sentinel_latin1Bytes(s string) ([]byte, error) {
	b := make([]byte, 0, utf8.RuneCountInString(s))
	for _, r := range s {
		if r > 255 {
			return nil, fmt.Errorf("latin-1 codec cannot encode character")
		}
		b = append(b, byte(r))
	}
	return b, nil
}
func sentinel_latin1String(data []byte) string {
	r := make([]rune, len(data))
	for i, b := range data {
		r[i] = rune(b)
	}
	return string(r)
}
func sentinel_b64EncodeLatin1(s string) (string, error) {
	b, err := sentinel_latin1Bytes(s)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(b), nil
}
func sentinel_b64DecodeLatin1(s string) (string, error) {
	// Python b64decode(validate=False) 忽略非字母表字符及多余 padding。
	var out []byte
	quartet := make([]byte, 0, 4)
	pads := 0
	for _, r := range s {
		if r > 127 {
			return "", fmt.Errorf("ascii codec cannot encode character")
		}
		if r == '=' {
			if len(quartet) == 3 {
				quartet = append(quartet, '=')
				decoded, err := base64.StdEncoding.DecodeString(string(quartet))
				if err != nil {
					return "", err
				}
				return sentinel_latin1String(append(out, decoded...)), nil
			}
			if len(quartet) == 2 {
				pads++
				if pads == 2 {
					quartet = append(quartet, '=', '=')
					decoded, err := base64.StdEncoding.DecodeString(string(quartet))
					if err != nil {
						return "", err
					}
					return sentinel_latin1String(append(out, decoded...)), nil
				}
			}
			continue
		}
		if !((r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '+' || r == '/') {
			continue
		}
		pads = 0
		quartet = append(quartet, byte(r))
		if len(quartet) == 4 {
			decoded, err := base64.StdEncoding.DecodeString(string(quartet))
			if err != nil {
				return "", err
			}
			out = append(out, decoded...)
			quartet = quartet[:0]
		}
	}
	if len(quartet) != 0 {
		return "", fmt.Errorf("Incorrect padding")
	}
	return sentinel_latin1String(out), nil
}
func sentinel_xorLatin1(data, key string) string {
	if key == "" {
		return data
	}
	r, k := []rune(data), []rune(key)
	for i := range r {
		r[i] ^= k[i%len(k)]
	}
	return string(r)
}

// ---- internal/sentinel/error.go ----
// Package sentinel 实现固定浏览器画像的证明生成器与受限寄存器 VM。

// Error 对外只暴露稳定错误码；Cause 仅用于离线诊断，不进入安全日志。
type sentinel_Error struct {
	Code  string
	Cause error
}

func (e *sentinel_Error) Error() string { return e.Code }
func (e *sentinel_Error) Unwrap() error { return e.Cause }

func sentinel_checkDeadline(deadline time.Time) error {
	if !time.Now().Before(deadline) {
		return &sentinel_Error{Code: "deadline_exceeded"}
	}
	return nil
}

func sentinel_resourceError(reason string) error {
	return &sentinel_Error{Code: "sentinel_turnstile_error", Cause: fmt.Errorf("%s", reason)}
}

// ---- internal/sentinel/generator.go ----

const sentinel_FrameVersion = "20260810913b"
const sentinel_SDKStubURL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
const sentinel_SDKURL = "https://chatgpt.com/sentinel/" + sentinel_FrameVersion + "/sdk.js"
const sentinel_maxNonce = 500000

var sentinel_timezoneLabels = map[string][2]string{
	"Asia/Manila":         {"Philippine Standard Time", "Philippine Standard Time"},
	"America/New_York":    {"Eastern Standard Time", "Eastern Daylight Time"},
	"Asia/Singapore":      {"Singapore Standard Time", "Singapore Standard Time"},
	"Asia/Tokyo":          {"Japan Standard Time", "Japan Standard Time"},
	"Asia/Seoul":          {"Korean Standard Time", "Korean Standard Time"},
	"Europe/London":       {"Greenwich Mean Time", "British Summer Time"},
	"Europe/Berlin":       {"Central European Standard Time", "Central European Summer Time"},
	"Europe/Paris":        {"Central European Standard Time", "Central European Summer Time"},
	"Europe/Amsterdam":    {"Central European Standard Time", "Central European Summer Time"},
	"Europe/Dublin":       {"Greenwich Mean Time", "Irish Standard Time"},
	"Asia/Kolkata":        {"India Standard Time", "India Standard Time"},
	"Australia/Sydney":    {"Australian Eastern Standard Time", "Australian Eastern Daylight Time"},
	"America/Toronto":     {"Eastern Standard Time", "Eastern Daylight Time"},
	"Asia/Hong_Kong":      {"Hong Kong Standard Time", "Hong Kong Standard Time"},
	"Asia/Taipei":         {"Taipei Standard Time", "Taipei Standard Time"},
	"Asia/Kuala_Lumpur":   {"Malaysia Time", "Malaysia Time"},
	"Asia/Bangkok":        {"Indochina Time", "Indochina Time"},
	"Asia/Ho_Chi_Minh":    {"Indochina Time", "Indochina Time"},
	"Asia/Jakarta":        {"Western Indonesia Time", "Western Indonesia Time"},
	"America/Sao_Paulo":   {"Brasilia Standard Time", "Brasilia Summer Time"},
	"America/Mexico_City": {"Central Standard Time", "Central Daylight Time"},
	"UTC":                 {"Coordinated Universal Time", "Coordinated Universal Time"},
}

func sentinel_chromeDate(zone string, t time.Time) string {
	label := ""
	if labels, ok := sentinel_timezoneLabels[zone]; ok {
		label = labels[0]
		daylight := t.IsDST()
		if zone == "Europe/Dublin" {
			// IANA 的负 DST 与 macOS 的夏季 DST 标记不同；协议使用 Chrome 的冬夏标签。
			_, offset := t.Zone()
			daylight = offset > 0
		}
		if daylight && labels[1] != "" {
			label = labels[1]
		}
	} else {
		label, _ = t.Zone()
		if len(label) <= 3 {
			parts := strings.Split(zone, "/")
			label = strings.ReplaceAll(parts[len(parts)-1], "_", " ")
			if label == "" {
				label = "Coordinated Universal Time"
			}
		}
	}
	_, offset := t.Zone()
	minutes := int(math.Floor(float64(offset) / 60))
	sign := "+"
	if minutes < 0 {
		sign = "-"
		minutes = -minutes
	}
	return fmt.Sprintf("%s GMT%s%02d%02d (%s)", t.Format("Mon Jan 02 2006 15:04:05"), sign, minutes/60, minutes%60, label)
}

// Generator 的随机值和 timeOrigin 在当前挑战中隔离，不缓存跨 flow 令牌。
type sentinel_Generator struct {
	deviceID, locale, timezone string
	profile                    protocol_BrowserProfile
	clock                      fingerprint_Clock
	reactKey                   string
	timeOrigin                 float64
	randomFloat                func() float64
	randomInt                  func(int, int) int
	perSecond                  func() float64
}

func sentinel_NewGenerator(deviceID string, profile protocol_BrowserProfile, locale, timezone string, clock fingerprint_Clock) *sentinel_Generator {
	if locale == "" {
		locale = "en-US"
	}
	if timezone == "" {
		timezone = "UTC"
	}
	alphabet := "abcdefghijklmnopqrstuvwxyz0123456789"
	key := make([]byte, 13)
	for i := range key {
		key[i] = alphabet[dep_math_rand_v2.IntN(len(alphabet))]
	}
	start := time.Now()
	return &sentinel_Generator{deviceID: strings.TrimSpace(deviceID), profile: profile, locale: locale,
		timezone: timezone, clock: clock, reactKey: "__reactContainer$" + string(key),
		timeOrigin:  clock.NowMS() - (1200 + dep_math_rand_v2.Float64()*3600),
		randomFloat: dep_math_rand_v2.Float64, randomInt: func(a, b int) int { return a + dep_math_rand_v2.IntN(b-a+1) },
		perSecond: func() float64 { return time.Since(start).Seconds() }}
}

func (g *sentinel_Generator) config(nonce int, elapsedMS float64) (*sentinel_array, error) {
	local, err := g.clock.LocalTime(g.timezone)
	if err != nil {
		return nil, err
	}
	primary := strings.ReplaceAll(g.locale, "_", "-")
	base := strings.SplitN(primary, "-", 2)[0]
	languages := primary
	if !strings.EqualFold(primary, base) {
		languages += "," + base
	}
	// 只有已明确的屏幕汇总和 UA 来自画像；未确认含义的数值槽位沿用协议基线。
	return sentinel_arr(int64(g.profile.ScreenWidth+g.profile.ScreenHeight), sentinel_chromeDate(g.timezone, local), int64(4294967296), int64(nonce),
		g.profile.UserAgent, sentinel_SDKURL, nil, primary, languages, int64(g.randomInt(2, 8)),
		"scheduling-[object Scheduling]", g.reactKey, "onscrollend", elapsedMS, g.deviceID,
		"", int64(8), sentinel_roundDecimal(g.timeOrigin, 1), int64(0), int64(0), int64(0),
		int64(0), int64(0), int64(0), int64(0)), nil
}

func (g *sentinel_Generator) RequirementsToken() (string, error) {
	config, err := g.config(1, g.randomFloat()*4900+100)
	if err != nil {
		return "", err
	}
	encoded, err := sentinel_encodeSentinelJSON(config)
	if err != nil {
		return "", err
	}
	return "gAAAAAC" + encoded + "~S", nil
}

func (g *sentinel_Generator) PowToken(seed, difficulty string, deadline time.Time) (string, error) {
	start := g.perSecond()
	if difficulty == "" {
		difficulty = "0"
	}
	prefix := min(len(difficulty), 8)
	for nonce := 0; nonce < sentinel_maxNonce; nonce++ {
		if nonce%64 == 0 {
			if err := sentinel_checkDeadline(deadline); err != nil {
				return "", err
			}
		}
		config, err := g.config(nonce, (g.perSecond()-start)*1000)
		if err != nil {
			return "", err
		}
		encoded, err := sentinel_encodeSentinelJSON(config)
		if err != nil {
			return "", err
		}
		digest := sentinel_hashFNV1a(seed + encoded)
		if strings.HasPrefix(digest, difficulty) || digest[:prefix] <= difficulty {
			if err := sentinel_checkDeadline(deadline); err != nil {
				return "", err
			}
			return "gAAAAAB" + encoded + "~S", nil
		}
	}
	return "", fmt.Errorf("pow_exhausted")
}

func sentinel_hashFNV1a(s string) string {
	h := uint32(2166136261)
	for _, b := range []byte(s) {
		h ^= uint32(b)
		h *= 16777619
	}
	h ^= h >> 16
	h *= 2246822507
	h ^= h >> 13
	h *= 3266489909
	h ^= h >> 16
	return fmt.Sprintf("%08x", h)
}

func sentinel_encodeSentinelJSON(value any) (string, error) {
	raw, err := sentinel_dumpJSON(value, false, false)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString([]byte(raw)), nil
}

// 用 float64 的精确有理数做十进制舍入，避免先乘十引入二次舍入。
func sentinel_roundDecimal(value float64, places int) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return value
	}
	scale := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(places)), nil)
	rat := new(big.Rat).SetFloat64(value)
	rat.Mul(rat, new(big.Rat).SetInt(scale))
	negative := rat.Sign() < 0
	num := new(big.Int).Abs(rat.Num())
	quotient, remainder := new(big.Int), new(big.Int)
	quotient.QuoRem(num, rat.Denom(), remainder)
	cmp := new(big.Int).Lsh(remainder, 1).Cmp(rat.Denom())
	if cmp > 0 || (cmp == 0 && quotient.Bit(0) == 1) {
		quotient.Add(quotient, big.NewInt(1))
	}
	if negative {
		quotient.Neg(quotient)
	}
	result, _ := new(big.Rat).SetFrac(quotient, scale).Float64()
	if result == 0 && math.Signbit(value) {
		return math.Copysign(0, -1)
	}
	return result
}

// ---- internal/sentinel/json.go ----

func sentinel_quoteJSON(s string, ascii bool) (string, error) {
	if !utf8.ValidString(s) {
		return "", fmt.Errorf("invalid UTF-8 string")
	}
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString("\\\"")
		case '\\':
			b.WriteString("\\\\")
		case '\b':
			b.WriteString("\\b")
		case '\f':
			b.WriteString("\\f")
		case '\n':
			b.WriteString("\\n")
		case '\r':
			b.WriteString("\\r")
		case '\t':
			b.WriteString("\\t")
		default:
			if r < 32 || (ascii && r > 127) {
				if r <= 0xffff {
					fmt.Fprintf(&b, "\\u%04x", r)
				} else {
					hi, lo := utf16.EncodeRune(r)
					fmt.Fprintf(&b, "\\u%04x\\u%04x", hi, lo)
				}
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String(), nil
}

func sentinel_dumpJSON(value any, ascii, allowNaN bool) (string, error) {
	return sentinel_dumpDepth(value, ascii, allowNaN, map[any]bool{}, 0)
}
func sentinel_dumpDepth(value any, ascii, allowNaN bool, seen map[any]bool, depth int) (string, error) {
	if depth > 64 {
		return "", fmt.Errorf("maximum recursion depth exceeded while encoding a JSON object")
	}
	switch v := value.(type) {
	case nil:
		return "null", nil
	case bool:
		if v {
			return "true", nil
		}
		return "false", nil
	case string:
		return sentinel_quoteJSON(v, ascii)
	case int:
		return strconv.Itoa(v), nil
	case int64:
		return strconv.FormatInt(v, 10), nil
	case *big.Int:
		return v.String(), nil
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) {
			if !allowNaN {
				return "", fmt.Errorf("Out of range float values are not JSON compliant")
			}
			if math.IsNaN(v) {
				return "NaN", nil
			}
			if math.IsInf(v, 1) {
				return "Infinity", nil
			}
			return "-Infinity", nil
		}
		return sentinel_pythonFloat(v), nil
	case *sentinel_array:
		if seen[v] {
			return "", fmt.Errorf("Circular reference detected")
		}
		seen[v] = true
		defer delete(seen, v)
		values := make([]string, len(v.values))
		for i, child := range v.values {
			s, err := sentinel_dumpDepth(child, ascii, allowNaN, seen, depth+1)
			if err != nil {
				return "", err
			}
			values[i] = s
		}
		return "[" + strings.Join(values, ",") + "]", nil
	case *sentinel_object:
		if seen[v] {
			return "", fmt.Errorf("Circular reference detected")
		}
		seen[v] = true
		defer delete(seen, v)
		values := make([]string, len(v.entries))
		for i, item := range v.entries {
			var key string
			if s, ok := item.key.(string); ok {
				key = s
			} else {
				var err error
				key, err = sentinel_dumpDepth(item.key, ascii, allowNaN, seen, depth+1)
				if err != nil {
					return "", err
				}
			}
			key, err := sentinel_quoteJSON(key, ascii)
			if err != nil {
				return "", err
			}
			s, err := sentinel_dumpDepth(item.value, ascii, allowNaN, seen, depth+1)
			if err != nil {
				return "", err
			}
			values[i] = key + ":" + s
		}
		return "{" + strings.Join(values, ",") + "}", nil
	}
	return "", fmt.Errorf("Object of type %s is not JSON serializable", sentinel_typeName(value))
}

// 小型 JSON 读取器保留对象插入顺序、任意精度整数与 Python 常量解析。
type sentinel_jsonReader struct {
	s     string
	i     int
	depth int
}

func sentinel_parseJSON(s string) (any, error) {
	p := sentinel_jsonReader{s: s}
	v, err := p.value()
	if err != nil {
		return nil, err
	}
	p.space()
	if p.i != len(s) {
		return nil, p.fail("Extra data")
	}
	return v, nil
}
func (p *sentinel_jsonReader) space() {
	for p.i < len(p.s) && strings.ContainsRune(" \r\n\t", rune(p.s[p.i])) {
		p.i++
	}
}
func (p *sentinel_jsonReader) fail(message string) error {
	line := 1 + strings.Count(p.s[:p.i], "\n")
	col := p.i - strings.LastIndex(p.s[:p.i], "\n")
	return fmt.Errorf("%s: line %d column %d (char %d)", message, line, col, p.i)
}
func (p *sentinel_jsonReader) value() (any, error) {
	p.space()
	if p.depth > 65 {
		return nil, sentinel_resourceError("turnstile_data_depth_limit")
	}
	if p.i >= len(p.s) {
		return nil, p.fail("Expecting value")
	}
	for _, c := range []struct {
		text  string
		value any
	}{{"null", nil}, {"true", true}, {"false", false}, {"NaN", math.NaN()}, {"Infinity", math.Inf(1)}, {"-Infinity", math.Inf(-1)}} {
		if strings.HasPrefix(p.s[p.i:], c.text) {
			p.i += len(c.text)
			return c.value, nil
		}
	}
	switch p.s[p.i] {
	case '"':
		return p.stringValue()
	case '[':
		p.i++
		p.depth++
		defer func() { p.depth-- }()
		a := sentinel_arr()
		p.space()
		if p.i < len(p.s) && p.s[p.i] == ']' {
			p.i++
			return a, nil
		}
		for {
			v, err := p.value()
			if err != nil {
				return nil, err
			}
			a.values = append(a.values, v)
			p.space()
			if p.i >= len(p.s) {
				return nil, p.fail("Expecting ',' delimiter")
			}
			c := p.s[p.i]
			p.i++
			if c == ']' {
				return a, nil
			}
			if c != ',' {
				return nil, p.fail("Expecting ',' delimiter")
			}
		}
	case '{':
		p.i++
		p.depth++
		defer func() { p.depth-- }()
		o := sentinel_obj()
		p.space()
		if p.i < len(p.s) && p.s[p.i] == '}' {
			p.i++
			return o, nil
		}
		for {
			p.space()
			if p.i >= len(p.s) || p.s[p.i] != '"' {
				return nil, p.fail("Expecting property name enclosed in double quotes")
			}
			k, err := p.stringValue()
			if err != nil {
				return nil, err
			}
			p.space()
			if p.i >= len(p.s) || p.s[p.i] != ':' {
				return nil, p.fail("Expecting ':' delimiter")
			}
			p.i++
			v, err := p.value()
			if err != nil {
				return nil, err
			}
			_ = o.set(k, v)
			p.space()
			if p.i >= len(p.s) {
				return nil, p.fail("Expecting ',' delimiter")
			}
			c := p.s[p.i]
			p.i++
			if c == '}' {
				return o, nil
			}
			if c != ',' {
				return nil, p.fail("Expecting ',' delimiter")
			}
		}
	default:
		start := p.i
		if p.s[p.i] == '-' {
			p.i++
		}
		if p.i >= len(p.s) || p.s[p.i] < '0' || p.s[p.i] > '9' {
			p.i = start
			return nil, p.fail("Expecting value")
		}
		if p.s[p.i] == '0' {
			p.i++
		} else {
			for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
				p.i++
			}
		}
		isFloat := false
		if p.i+1 < len(p.s) && p.s[p.i] == '.' && p.s[p.i+1] >= '0' && p.s[p.i+1] <= '9' {
			isFloat = true
			p.i++
			for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
				p.i++
			}
		}
		if p.i < len(p.s) && (p.s[p.i] == 'e' || p.s[p.i] == 'E') {
			j := p.i + 1
			if j < len(p.s) && (p.s[j] == '+' || p.s[j] == '-') {
				j++
			}
			if j < len(p.s) && p.s[j] >= '0' && p.s[j] <= '9' {
				isFloat = true
				p.i = j + 1
				for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
					p.i++
				}
			}
		}
		text := p.s[start:p.i]
		if isFloat {
			f, err := strconv.ParseFloat(text, 64)
			if err != nil && !math.IsInf(f, 0) {
				return nil, err
			}
			return f, nil
		}
		if len(strings.TrimPrefix(text, "-")) > 4300 {
			return nil, fmt.Errorf("Exceeds the limit (4300 digits) for integer string conversion")
		}
		n, ok := new(big.Int).SetString(text, 10)
		if !ok {
			return nil, p.fail("Expecting value")
		}
		return sentinel_compactInt(n), nil
	}
}
func (p *sentinel_jsonReader) stringValue() (string, error) {
	p.i++
	var b strings.Builder
	for p.i < len(p.s) {
		c := p.s[p.i]
		p.i++
		if c == '"' {
			return b.String(), nil
		}
		if c < 32 {
			return "", p.fail("Invalid control character at")
		}
		if c != '\\' {
			b.WriteByte(c)
			continue
		}
		if p.i >= len(p.s) {
			return "", p.fail("Unterminated string starting at")
		}
		e := p.s[p.i]
		p.i++
		switch e {
		case '"', '\\', '/':
			b.WriteByte(e)
		case 'b':
			b.WriteByte('\b')
		case 'f':
			b.WriteByte('\f')
		case 'n':
			b.WriteByte('\n')
		case 'r':
			b.WriteByte('\r')
		case 't':
			b.WriteByte('\t')
		case 'u':
			if p.i+4 > len(p.s) {
				return "", p.fail("Invalid \\uXXXX escape")
			}
			n, err := strconv.ParseUint(p.s[p.i:p.i+4], 16, 16)
			if err != nil {
				return "", p.fail("Invalid \\uXXXX escape")
			}
			p.i += 4
			r := rune(n)
			if utf16.IsSurrogate(r) {
				if r >= 0xd800 && r <= 0xdbff && p.i+6 <= len(p.s) && p.s[p.i:p.i+2] == "\\u" {
					lo, err := strconv.ParseUint(p.s[p.i+2:p.i+6], 16, 16)
					if err == nil && lo >= 0xdc00 && lo <= 0xdfff {
						p.i += 6
						r = utf16.DecodeRune(r, rune(lo))
					}
				}
				if utf16.IsSurrogate(r) {
					return "", fmt.Errorf("utf-8 codec cannot encode surrogate")
				}
			}
			b.WriteRune(r)
		default:
			return "", p.fail("Invalid \\escape")
		}
	}
	return "", p.fail("Unterminated string starting at")
}

// ---- internal/sentinel/methods.go ----

func sentinel_integerIndex(value any) (int, error) {
	i, ok := sentinel_asInt(value)
	if !ok || !i.IsInt64() {
		return 0, fmt.Errorf("'%s' object cannot be interpreted as an integer", sentinel_typeName(value))
	}
	n := i.Int64()
	return int(n), nil
}

func (m *sentinel_machine) lookup(value, key any) (any, error) {
	if value == nil {
		return nil, nil
	}
	if o, ok := value.(*sentinel_object); ok {
		if _, err := sentinel_keyString(key); err != nil {
			return nil, err
		}
		return o.get(key), nil
	}
	if name, ok := key.(string); ok {
		switch v := value.(type) {
		case *sentinel_array:
			if method := m.arrayMethod(v, name); method != nil {
				return method, nil
			}
			return nil, nil
		case string:
			if method := sentinel_stringMethod(v, name); method != nil {
				return sentinel_fn(func(args ...any) (any, error) {
					if err := sentinel_checkStringAllocation(v, name, args, m.limits.valueBytes); err != nil {
						return nil, err
					}
					return method.function(args...)
				}), nil
			}
			return nil, nil
		case []byte:
			if name == "decode" {
				return sentinel_native(0, 2, func(a []any) (any, error) {
					encoding := "utf-8"
					if len(a) > 0 {
						encoding = sentinel_pyStr(a[0])
					}
					switch encoding {
					case "utf-8", "utf8":
						if !utf8.Valid(v) {
							return nil, fmt.Errorf("utf-8 codec cannot decode bytes")
						}
						return string(v), nil
					case "latin-1", "latin1", "iso-8859-1":
						return sentinel_latin1String(v), nil
					case "ascii":
						for _, b := range v {
							if b > 127 {
								return nil, fmt.Errorf("ascii codec cannot decode bytes")
							}
						}
						return string(v), nil
					}
					return nil, fmt.Errorf("unknown encoding: %s", encoding)
				}), nil
			}
		}
		return nil, nil
	}
	n, err := sentinel_integerIndex(key)
	if err != nil {
		return nil, nil
	}
	switch v := value.(type) {
	case *sentinel_array:
		if n < 0 {
			n += len(v.values)
		}
		if n >= 0 && n < len(v.values) {
			return v.values[n], nil
		}
	case string:
		r := []rune(v)
		if n < 0 {
			n += len(r)
		}
		if n >= 0 && n < len(r) {
			return string(r[n]), nil
		}
	case []byte:
		if n < 0 {
			n += len(v)
		}
		if n >= 0 && n < len(v) {
			return int64(v[n]), nil
		}
	}
	return nil, nil
}

func (m *sentinel_machine) arrayMethod(list *sentinel_array, name string) *sentinel_callable {
	guard := func(minArgs, maxArgs int, action func([]any) (any, error)) *sentinel_callable {
		return sentinel_native(minArgs, maxArgs, func(a []any) (any, error) {
			v, err := action(a)
			if err != nil {
				return nil, err
			}
			if err = m.mutate(list); err != nil {
				return nil, err
			}
			return v, nil
		})
	}
	switch name {
	case "append":
		return guard(1, 1, func(a []any) (any, error) { list.values = append(list.values, a[0]); return nil, nil })
	case "clear":
		return guard(0, 0, func([]any) (any, error) { list.values = nil; return nil, nil })
	case "extend":
		return guard(1, 1, func(a []any) (any, error) {
			values, err := sentinel_listCopy(a[0])
			if err != nil {
				return nil, err
			}
			list.values = append(list.values, values.values...)
			return nil, nil
		})
	case "insert":
		return guard(2, 2, func(a []any) (any, error) {
			n, err := sentinel_integerIndex(a[0])
			if err != nil {
				return nil, err
			}
			if n < 0 {
				n += len(list.values)
			}
			n = max(0, min(n, len(list.values)))
			list.values = append(list.values, nil)
			copy(list.values[n+1:], list.values[n:])
			list.values[n] = a[1]
			return nil, nil
		})
	case "pop":
		return guard(0, 1, func(a []any) (any, error) {
			n := -1
			var err error
			if len(a) > 0 {
				n, err = sentinel_integerIndex(a[0])
				if err != nil {
					return nil, err
				}
			}
			if len(list.values) == 0 {
				return nil, fmt.Errorf("pop from empty list")
			}
			if n < 0 {
				n += len(list.values)
			}
			if n < 0 || n >= len(list.values) {
				return nil, fmt.Errorf("pop index out of range")
			}
			value := list.values[n]
			list.values = append(list.values[:n], list.values[n+1:]...)
			return value, nil
		})
	case "remove":
		return guard(1, 1, func(a []any) (any, error) {
			for i, value := range list.values {
				if sentinel_equal(value, a[0]) {
					list.values = append(list.values[:i], list.values[i+1:]...)
					return nil, nil
				}
			}
			return nil, fmt.Errorf("list.remove(x): x not in list")
		})
	case "reverse":
		return guard(0, 0, func([]any) (any, error) {
			for i, j := 0, len(list.values)-1; i < j; i, j = i+1, j-1 {
				list.values[i], list.values[j] = list.values[j], list.values[i]
			}
			return nil, nil
		})
	case "sort":
		return guard(0, 0, func([]any) (any, error) {
			var sortErr error
			sort.SliceStable(list.values, func(i, j int) bool {
				if sortErr != nil {
					return false
				}
				if err := sentinel_checkDeadline(m.options.Deadline); err != nil {
					sortErr = err
					return false
				}
				v, err := sentinel_less(list.values[i], list.values[j])
				if err != nil {
					sortErr = err
				}
				return v
			})
			return nil, sortErr
		})
	case "copy":
		return sentinel_native(0, 0, func([]any) (any, error) { return sentinel_listCopy(list) })
	case "count":
		return sentinel_native(1, 1, func(a []any) (any, error) {
			n := int64(0)
			for _, v := range list.values {
				if sentinel_equal(v, a[0]) {
					n++
				}
			}
			return n, nil
		})
	case "index":
		return sentinel_native(1, 3, func(a []any) (any, error) {
			start, end := 0, len(list.values)
			if len(a) > 1 {
				var err error
				start, err = sentinel_integerIndex(a[1])
				if err != nil {
					return nil, err
				}
				if start < 0 {
					start += len(list.values)
				}
				start = max(0, start)
			}
			if len(a) > 2 {
				var err error
				end, err = sentinel_integerIndex(a[2])
				if err != nil {
					return nil, err
				}
				if end < 0 {
					end += len(list.values)
				}
				end = min(end, len(list.values))
			}
			for i := start; i < end; i++ {
				if sentinel_equal(list.values[i], a[0]) {
					return int64(i), nil
				}
			}
			return nil, fmt.Errorf("%s is not in list", sentinel_pyRepr(a[0], map[any]bool{}))
		})
	}
	return nil
}

// ---- internal/sentinel/numeric.go ----

func sentinel_pythonFloat(value float64) string {
	if math.IsNaN(value) {
		return "nan"
	}
	if math.IsInf(value, 1) {
		return "inf"
	}
	if math.IsInf(value, -1) {
		return "-inf"
	}
	e := strconv.FormatFloat(value, 'e', -1, 64)
	parts := strings.Split(e, "e")
	exp, _ := strconv.Atoi(parts[1])
	if exp < -4 || exp >= 16 {
		return e
	}
	s := strconv.FormatFloat(value, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

func sentinel_asFloat(value any) (float64, error) {
	switch v := value.(type) {
	case bool:
		if v {
			return 1, nil
		}
		return 0, nil
	case int:
		return float64(v), nil
	case int64:
		return float64(v), nil
	case *big.Int:
		f, _ := new(big.Float).SetInt(v).Float64()
		if math.IsInf(f, 0) {
			return 0, fmt.Errorf("int too large to convert to float")
		}
		return f, nil
	case float64:
		return v, nil
	case string:
		s := strings.TrimSpace(strings.ReplaceAll(v, "_", ""))
		lower := strings.ToLower(s)
		if lower == "nan" || lower == "+nan" || lower == "-nan" {
			return math.NaN(), nil
		}
		if lower == "infinity" || lower == "+infinity" {
			return math.Inf(1), nil
		}
		if lower == "-infinity" {
			return math.Inf(-1), nil
		}
		f, err := strconv.ParseFloat(s, 64)
		if err == nil || math.IsInf(f, 0) {
			return f, nil
		}
		return 0, fmt.Errorf("could not convert string to float: %s", sentinel_quotePython(v))
	}
	return 0, fmt.Errorf("float() argument must be a string or a real number, not '%s'", sentinel_typeName(value))
}

func sentinel_asInt(value any) (*big.Int, bool) {
	switch v := value.(type) {
	case int:
		return big.NewInt(int64(v)), true
	case int64:
		return big.NewInt(v), true
	case *big.Int:
		return new(big.Int).Set(v), true
	case bool:
		if v {
			return big.NewInt(1), true
		}
		return big.NewInt(0), true
	}
	return nil, false
}
func sentinel_compactInt(value *big.Int) any {
	if value.IsInt64() {
		return value.Int64()
	}
	return value
}
func sentinel_numericRat(value any) (*big.Rat, bool) {
	if i, ok := sentinel_asInt(value); ok {
		return new(big.Rat).SetInt(i), true
	}
	if f, ok := value.(float64); ok {
		r := new(big.Rat).SetFloat64(f)
		return r, r != nil
	}
	return nil, false
}

func sentinel_equal(a, b any) bool { return sentinel_equalDepth(a, b, 0) }
func sentinel_equalDepth(a, b any, depth int) bool {
	if depth > 64 {
		return false
	}
	if x, ok := sentinel_numericRat(a); ok {
		if y, ok := sentinel_numericRat(b); ok {
			return x.Cmp(y) == 0
		}
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case *sentinel_array:
		y, ok := b.(*sentinel_array)
		if !ok {
			return false
		}
		if x == y {
			return true
		}
		if len(x.values) != len(y.values) {
			return false
		}
		for i := range x.values {
			if !sentinel_equalDepth(x.values[i], y.values[i], depth+1) {
				return false
			}
		}
		return true
	case *sentinel_object:
		y, ok := b.(*sentinel_object)
		if !ok {
			return false
		}
		if x == y {
			return true
		}
		if len(x.entries) != len(y.entries) {
			return false
		}
		for _, item := range x.entries {
			if !y.has(item.key) || !sentinel_equalDepth(item.value, y.get(item.key), depth+1) {
				return false
			}
		}
		return true
	case *sentinel_callable:
		y, ok := b.(*sentinel_callable)
		return ok && x == y
	case float64:
		y, ok := b.(float64)
		return ok && x == y
	}
	return false
}

func sentinel_less(a, b any) (bool, error) { return sentinel_lessDepth(a, b, 0) }
func sentinel_lessDepth(a, b any, depth int) (bool, error) {
	if depth > 64 {
		return false, fmt.Errorf("maximum recursion depth exceeded in comparison")
	}
	if x, ok := sentinel_numericRat(a); ok {
		if y, ok := sentinel_numericRat(b); ok {
			return x.Cmp(y) < 0, nil
		}
	}
	if sentinel_isNumber(a) && sentinel_isNumber(b) {
		if x, ok := a.(float64); ok {
			if math.IsNaN(x) {
				return false, nil
			}
			if math.IsInf(x, 0) {
				if y, ok := b.(float64); ok {
					return x < y, nil
				}
				return math.IsInf(x, -1), nil
			}
		}
		if y, ok := b.(float64); ok {
			if math.IsNaN(y) {
				return false, nil
			}
			if math.IsInf(y, 0) {
				return math.IsInf(y, 1), nil
			}
		}
	}
	if x, ok := a.(string); ok {
		if y, ok := b.(string); ok {
			return x < y, nil
		}
	}
	if x, ok := a.(*sentinel_array); ok {
		if y, ok := b.(*sentinel_array); ok {
			for i := 0; i < min(len(x.values), len(y.values)); i++ {
				if !sentinel_equal(x.values[i], y.values[i]) {
					return sentinel_lessDepth(x.values[i], y.values[i], depth+1)
				}
			}
			return len(x.values) < len(y.values), nil
		}
	}
	return false, fmt.Errorf("'<' not supported between instances of '%s' and '%s'", sentinel_typeName(a), sentinel_typeName(b))
}

func sentinel_subtract(a, b any) (any, error) {
	if x, ok := sentinel_asInt(a); ok {
		if y, ok := sentinel_asInt(b); ok {
			return sentinel_compactInt(new(big.Int).Sub(x, y)), nil
		}
	}
	if sentinel_isNumber(a) && sentinel_isNumber(b) {
		x, err := sentinel_asFloat(a)
		if err != nil {
			return nil, err
		}
		y, err := sentinel_asFloat(b)
		if err != nil {
			return nil, err
		}
		return x - y, nil
	}
	return nil, fmt.Errorf("unsupported operand type(s) for -: '%s' and '%s'", sentinel_typeName(a), sentinel_typeName(b))
}

func sentinel_isNumber(value any) bool {
	switch value.(type) {
	case int, int64, *big.Int, bool, float64:
		return true
	}
	return false
}

// ---- internal/sentinel/regex.go ----

func (m *sentinel_machine) findScript(pattern string) (any, error) {
	if len(pattern) > m.limits.regexBytes {
		return nil, sentinel_resourceError("turnstile_regex_limit")
	}
	// 引擎允许可变长 lookbehind，但 Python 基线禁止；先保留原约束。
	if err := sentinel_validatePythonLookbehind(pattern); err != nil {
		return nil, sentinel_resourceError("turnstile_regex_invalid")
	}
	pattern = sentinel_normalizePythonRegex(pattern)
	re, err := regexp2.Compile(pattern, regexp2.None)
	if err != nil {
		return nil, sentinel_resourceError("turnstile_regex_invalid")
	}
	document := m.window.get("document").(*sentinel_object)
	scripts, _ := document.get("scripts").(*sentinel_array)
	if scripts == nil {
		return nil, nil
	}
	for _, item := range scripts.values {
		if err = sentinel_checkDeadline(m.options.Deadline); err != nil {
			return nil, err
		}
		re.MatchTimeout = time.Until(m.options.Deadline)
		src := ""
		if script, ok := item.(*sentinel_object); ok {
			src = sentinel_strOrEmpty(script.get("src"))
		}
		matched, err := re.FindStringMatch(src)
		if err != nil {
			if e := sentinel_checkDeadline(m.options.Deadline); e != nil {
				return nil, e
			}
			return nil, sentinel_resourceError("turnstile_regex_timeout")
		}
		if err = sentinel_checkDeadline(m.options.Deadline); err != nil {
			return nil, err
		}
		if matched != nil {
			return matched.String(), nil
		}
	}
	return nil, nil
}

func sentinel_normalizePythonRegex(pattern string) string {
	var b strings.Builder
	characterClass := false
	for i := 0; i < len(pattern); i++ {
		if pattern[i] == '\\' && i+1 < len(pattern) {
			b.WriteByte('\\')
			i++
			if pattern[i] == 'Z' && !characterClass {
				b.WriteByte('z')
			} else {
				b.WriteByte(pattern[i])
			}
			continue
		}
		if pattern[i] == '[' {
			characterClass = true
		}
		if pattern[i] == ']' {
			characterClass = false
		}
		if !characterClass && strings.HasPrefix(pattern[i:], "(?P<") {
			b.WriteString("(?<")
			i += 3
			continue
		}
		if !characterClass && strings.HasPrefix(pattern[i:], "(?P=") {
			if end := strings.IndexByte(pattern[i+4:], ')'); end >= 0 {
				b.WriteString(`\k<`)
				b.WriteString(pattern[i+4 : i+4+end])
				b.WriteByte('>')
				i += 4 + end
				continue
			}
		}
		b.WriteByte(pattern[i])
	}
	return b.String()
}

type sentinel_regexWidth struct{ min, max int }
type sentinel_widthParser struct {
	text         []rune
	pos, capture int
	groups       map[string]sentinel_regexWidth
}

const sentinel_unboundedWidth = 1000000000

func sentinel_validatePythonLookbehind(pattern string) error {
	p := sentinel_widthParser{text: []rune(pattern), groups: map[string]sentinel_regexWidth{}}
	_, err := p.sequence()
	return err
}
func (p *sentinel_widthParser) sequence() (sentinel_regexWidth, error) {
	branch := sentinel_regexWidth{}
	var alternatives []sentinel_regexWidth
	for p.pos < len(p.text) && p.text[p.pos] != ')' {
		if p.text[p.pos] == '|' {
			alternatives = append(alternatives, branch)
			branch = sentinel_regexWidth{}
			p.pos++
			continue
		}
		atom, err := p.atom()
		if err != nil {
			return sentinel_regexWidth{}, err
		}
		if p.pos < len(p.text) {
			switch p.text[p.pos] {
			case '*':
				atom = sentinel_regexWidth{0, sentinel_unboundedWidth}
				p.pos++
			case '+':
				atom.max = sentinel_unboundedWidth
				p.pos++
			case '?':
				atom.min = 0
				p.pos++
			case '{':
				start := p.pos
				p.pos++
				from := p.pos
				for p.pos < len(p.text) && p.text[p.pos] >= '0' && p.text[p.pos] <= '9' {
					p.pos++
				}
				if from == p.pos {
					p.pos = start
					break
				}
				n, _ := strconv.Atoi(string(p.text[from:p.pos]))
				maxN := n
				if p.pos < len(p.text) && p.text[p.pos] == ',' {
					p.pos++
					from = p.pos
					for p.pos < len(p.text) && p.text[p.pos] >= '0' && p.text[p.pos] <= '9' {
						p.pos++
					}
					maxN = sentinel_unboundedWidth
					if from != p.pos {
						maxN, _ = strconv.Atoi(string(p.text[from:p.pos]))
					}
				}
				if p.pos < len(p.text) && p.text[p.pos] == '}' {
					p.pos++
					atom.min = min(sentinel_unboundedWidth, atom.min*n)
					atom.max = min(sentinel_unboundedWidth, atom.max*maxN)
				} else {
					p.pos = start
				}
			}
			if p.pos < len(p.text) && (p.text[p.pos] == '?' || p.text[p.pos] == '+') {
				p.pos++
			}
		}
		branch.min = min(sentinel_unboundedWidth, branch.min+atom.min)
		branch.max = min(sentinel_unboundedWidth, branch.max+atom.max)
	}
	alternatives = append(alternatives, branch)
	result := alternatives[0]
	for _, b := range alternatives[1:] {
		result.min = min(result.min, b.min)
		result.max = max(result.max, b.max)
	}
	return result, nil
}
func (p *sentinel_widthParser) atom() (sentinel_regexWidth, error) {
	r := p.text[p.pos]
	p.pos++
	switch r {
	case '^', '$':
		return sentinel_regexWidth{}, nil
	case '[':
		if p.pos < len(p.text) && p.text[p.pos] == '^' {
			p.pos++
		}
		if p.pos < len(p.text) && p.text[p.pos] == ']' {
			p.pos++
		}
		for p.pos < len(p.text) {
			c := p.text[p.pos]
			p.pos++
			if c == '\\' && p.pos < len(p.text) {
				p.pos++
				continue
			}
			if c == ']' {
				break
			}
		}
		return sentinel_regexWidth{1, 1}, nil
	case '\\':
		if p.pos >= len(p.text) {
			return sentinel_regexWidth{}, nil
		}
		c := p.text[p.pos]
		p.pos++
		if strings.ContainsRune("AbBZz", c) {
			return sentinel_regexWidth{}, nil
		}
		if c >= '1' && c <= '9' {
			key := string(c)
			for p.pos < len(p.text) && p.text[p.pos] >= '0' && p.text[p.pos] <= '9' {
				key += string(p.text[p.pos])
				p.pos++
			}
			if width, ok := p.groups[key]; ok {
				return width, nil
			}
			return sentinel_regexWidth{0, sentinel_unboundedWidth}, nil
		}
		extra := 0
		switch c {
		case 'x':
			extra = 2
		case 'u':
			extra = 4
		case 'U':
			extra = 8
		}
		p.pos = min(len(p.text), p.pos+extra)
		return sentinel_regexWidth{1, 1}, nil
	case '(':
		capture := true
		lookaround, lookbehind := false, false
		name := ""
		if p.pos < len(p.text) && p.text[p.pos] == '?' {
			capture = false
			p.pos++
			if p.pos >= len(p.text) {
				return sentinel_regexWidth{}, nil
			}
			switch p.text[p.pos] {
			case ':', '>':
				p.pos++
			case '=', '!':
				lookaround = true
				p.pos++
			case '<':
				if p.pos+1 < len(p.text) && (p.text[p.pos+1] == '=' || p.text[p.pos+1] == '!') {
					lookaround = true
					lookbehind = true
					p.pos += 2
				}
			case '#':
				for p.pos < len(p.text) && p.text[p.pos] != ')' {
					p.pos++
				}
				if p.pos < len(p.text) {
					p.pos++
				}
				return sentinel_regexWidth{}, nil
			case 'P':
				p.pos++
				if p.pos < len(p.text) && p.text[p.pos] == '=' {
					p.pos++
					start := p.pos
					for p.pos < len(p.text) && p.text[p.pos] != ')' {
						p.pos++
					}
					name = string(p.text[start:p.pos])
					if p.pos < len(p.text) {
						p.pos++
					}
					if width, ok := p.groups[name]; ok {
						return width, nil
					}
					return sentinel_regexWidth{0, sentinel_unboundedWidth}, nil
				}
				if p.pos < len(p.text) && p.text[p.pos] == '<' {
					capture = true
					p.pos++
					start := p.pos
					for p.pos < len(p.text) && p.text[p.pos] != '>' {
						p.pos++
					}
					name = string(p.text[start:p.pos])
					if p.pos < len(p.text) {
						p.pos++
					}
				}
			default:
				for p.pos < len(p.text) && strings.ContainsRune("aiLmsux-", p.text[p.pos]) {
					p.pos++
				}
				if p.pos < len(p.text) && p.text[p.pos] == ')' {
					p.pos++
					return sentinel_regexWidth{}, nil
				}
				if p.pos < len(p.text) && p.text[p.pos] == ':' {
					p.pos++
				}
			}
		}
		id := 0
		if capture {
			p.capture++
			id = p.capture
		}
		width, err := p.sequence()
		if err != nil {
			return sentinel_regexWidth{}, err
		}
		if p.pos < len(p.text) && p.text[p.pos] == ')' {
			p.pos++
		}
		if lookbehind && width.min != width.max {
			return sentinel_regexWidth{}, fmt.Errorf("look-behind requires fixed-width pattern")
		}
		if capture {
			p.groups[strconv.Itoa(id)] = width
			if name != "" {
				p.groups[name] = width
			}
		}
		if lookaround {
			return sentinel_regexWidth{}, nil
		}
		return width, nil
	}
	return sentinel_regexWidth{1, 1}, nil
}

// ---- internal/sentinel/string_methods.go ----

func sentinel_upper(s string) string { return cases.Upper(language.Und).String(s) }

func sentinel_checkStringAllocation(value, name string, args []any, limit int) error {
	if name == "replace" && len(args) >= 2 {
		old, ok := args[0].(string)
		if !ok {
			return nil
		}
		replacement, ok := args[1].(string)
		if !ok {
			return nil
		}
		count := strings.Count(value, old)
		if len(args) > 2 {
			n, err := sentinel_integerIndex(args[2])
			if err != nil {
				return nil
			}
			if n >= 0 {
				count = min(count, n)
			}
		}
		growth := len(replacement) - len(old)
		if growth > 0 && count > (limit-len(value))/growth {
			return sentinel_resourceError("turnstile_value_limit")
		}
	}
	if name == "join" && len(args) == 1 {
		list, err := sentinel_listCopy(args[0])
		if err != nil {
			return nil
		}
		total := 0
		for i, item := range list.values {
			s, ok := item.(string)
			if !ok {
				return nil
			}
			total += len(s)
			if i > 0 {
				total += len(value)
			}
			if total > limit {
				return sentinel_resourceError("turnstile_value_limit")
			}
		}
	}
	return nil
}

func sentinel_stringArgument(value any) (string, error) {
	s, ok := value.(string)
	if !ok {
		return "", fmt.Errorf("must be str, not %s", sentinel_typeName(value))
	}
	return s, nil
}

// 有 maxsplit 时保留剩余片段中的原始空白，不能先 Fields 再 Join。
func sentinel_splitWhitespace(value string, count int, reverse bool) []string {
	r := []rune(value)
	if reverse {
		var parts []string
		end := len(r)
		for end > 0 && unicode.IsSpace(r[end-1]) {
			end--
		}
		for end > 0 {
			if count == 0 {
				parts = append(parts, string(r[:end]))
				break
			}
			start := end
			for start > 0 && !unicode.IsSpace(r[start-1]) {
				start--
			}
			parts = append(parts, string(r[start:end]))
			count--
			end = start
			for end > 0 && unicode.IsSpace(r[end-1]) {
				end--
			}
		}
		for i, j := 0, len(parts)-1; i < j; i, j = i+1, j-1 {
			parts[i], parts[j] = parts[j], parts[i]
		}
		return parts
	}
	var parts []string
	start := 0
	for start < len(r) && unicode.IsSpace(r[start]) {
		start++
	}
	for start < len(r) {
		if count == 0 {
			parts = append(parts, string(r[start:]))
			break
		}
		end := start
		for end < len(r) && !unicode.IsSpace(r[end]) {
			end++
		}
		parts = append(parts, string(r[start:end]))
		count--
		start = end
		for start < len(r) && unicode.IsSpace(r[start]) {
			start++
		}
	}
	return parts
}
func sentinel_stringMethod(value, name string) *sentinel_callable {
	switch name {
	case "upper":
		return sentinel_native(0, 0, func([]any) (any, error) { return sentinel_upper(value), nil })
	case "lower":
		return sentinel_native(0, 0, func([]any) (any, error) { return cases.Lower(language.Und).String(value), nil })
	case "casefold":
		return sentinel_native(0, 0, func([]any) (any, error) { return cases.Fold().String(value), nil })
	case "capitalize":
		return sentinel_native(0, 0, func([]any) (any, error) {
			r := []rune(strings.ToLower(value))
			if len(r) > 0 {
				r[0] = unicode.ToUpper(r[0])
			}
			return string(r), nil
		})
	case "strip", "lstrip", "rstrip":
		return sentinel_native(0, 1, func(a []any) (any, error) {
			cut := unicode.IsSpace
			if len(a) > 0 && a[0] != nil {
				s, err := sentinel_stringArgument(a[0])
				if err != nil {
					return nil, err
				}
				cut = func(r rune) bool { return strings.ContainsRune(s, r) }
			}
			switch name {
			case "lstrip":
				return strings.TrimLeftFunc(value, cut), nil
			case "rstrip":
				return strings.TrimRightFunc(value, cut), nil
			}
			return strings.TrimFunc(value, cut), nil
		})
	case "replace":
		return sentinel_native(2, 3, func(a []any) (any, error) {
			old, err := sentinel_stringArgument(a[0])
			if err != nil {
				return nil, err
			}
			replacement, err := sentinel_stringArgument(a[1])
			if err != nil {
				return nil, err
			}
			count := -1
			if len(a) == 3 {
				count, err = sentinel_integerIndex(a[2])
				if err != nil {
					return nil, err
				}
			}
			return strings.Replace(value, old, replacement, count), nil
		})
	case "split", "rsplit":
		return sentinel_native(0, 2, func(a []any) (any, error) {
			count := -1
			if len(a) > 1 {
				var err error
				count, err = sentinel_integerIndex(a[1])
				if err != nil {
					return nil, err
				}
			}
			var values []string
			if len(a) == 0 || a[0] == nil {
				values = sentinel_splitWhitespace(value, count, name == "rsplit")
			} else {
				separator, err := sentinel_stringArgument(a[0])
				if err != nil {
					return nil, err
				}
				if separator == "" {
					return nil, fmt.Errorf("empty separator")
				}
				if name == "rsplit" && count >= 0 {
					all := strings.Split(value, separator)
					if len(all) > count+1 {
						values = append([]string{strings.Join(all[:len(all)-count], separator)}, all[len(all)-count:]...)
					} else {
						values = all
					}
				} else {
					n := count + 1
					if count < 0 {
						n = -1
					}
					values = strings.SplitN(value, separator, n)
				}
			}
			result := sentinel_arr()
			for _, s := range values {
				result.values = append(result.values, s)
			}
			return result, nil
		})
	case "join":
		return sentinel_native(1, 1, func(a []any) (any, error) {
			list, err := sentinel_listCopy(a[0])
			if err != nil {
				return nil, err
			}
			parts := make([]string, len(list.values))
			for i, v := range list.values {
				s, ok := v.(string)
				if !ok {
					return nil, fmt.Errorf("sequence item %d: expected str instance, %s found", i, sentinel_typeName(v))
				}
				parts[i] = s
			}
			return strings.Join(parts, value), nil
		})
	case "startswith", "endswith", "find", "rfind", "index", "rindex", "count":
		return sentinel_native(1, 3, func(a []any) (any, error) {
			sub, err := sentinel_stringArgument(a[0])
			if err != nil {
				return nil, err
			}
			r := []rune(value)
			start, end := 0, len(r)
			if len(a) > 1 {
				start, err = sentinel_integerIndex(a[1])
				if err != nil {
					return nil, err
				}
				if start < 0 {
					start += len(r)
				}
				start = max(0, start)
			}
			if len(a) > 2 {
				end, err = sentinel_integerIndex(a[2])
				if err != nil {
					return nil, err
				}
				if end < 0 {
					end += len(r)
				}
				end = max(0, min(end, len(r)))
			}
			segment := ""
			valid := start <= end && start <= len(r)
			if valid {
				segment = string(r[start:end])
			}
			switch name {
			case "startswith":
				return valid && strings.HasPrefix(segment, sub), nil
			case "endswith":
				return valid && strings.HasSuffix(segment, sub), nil
			case "count":
				if !valid {
					return int64(0), nil
				}
				return int64(strings.Count(segment, sub)), nil
			}
			index := -1
			if valid {
				if name == "rfind" || name == "rindex" {
					index = strings.LastIndex(segment, sub)
				} else {
					index = strings.Index(segment, sub)
				}
			}
			if index < 0 {
				if name == "index" || name == "rindex" {
					return nil, fmt.Errorf("substring not found")
				}
				return int64(-1), nil
			}
			return int64(start + utf8.RuneCountInString(segment[:index])), nil
		})
	case "encode":
		return sentinel_native(0, 2, func(a []any) (any, error) {
			encoding := "utf-8"
			if len(a) > 0 {
				encoding = sentinel_pyStr(a[0])
			}
			switch encoding {
			case "utf-8", "utf8":
				return []byte(value), nil
			case "latin-1", "latin1", "iso-8859-1":
				return sentinel_latin1Bytes(value)
			case "ascii":
				for _, r := range value {
					if r > 127 {
						return nil, fmt.Errorf("ascii codec cannot encode character")
					}
				}
				return []byte(value), nil
			}
			return nil, fmt.Errorf("unknown encoding: %s", encoding)
		})
	case "removeprefix", "removesuffix":
		return sentinel_native(1, 1, func(a []any) (any, error) {
			s, err := sentinel_stringArgument(a[0])
			if err != nil {
				return nil, err
			}
			if name == "removeprefix" {
				return strings.TrimPrefix(value, s), nil
			}
			return strings.TrimSuffix(value, s), nil
		})
	case "isascii", "isalpha", "isalnum", "isdigit", "isdecimal", "isspace", "islower", "isupper":
		return sentinel_native(0, 0, func([]any) (any, error) {
			if name == "isascii" {
				for _, r := range value {
					if r > 127 {
						return false, nil
					}
				}
				return true, nil
			}
			if value == "" {
				return false, nil
			}
			hasCase := false
			for _, r := range value {
				switch name {
				case "isalpha":
					if !unicode.IsLetter(r) {
						return false, nil
					}
				case "isalnum":
					if !unicode.IsLetter(r) && !unicode.IsNumber(r) {
						return false, nil
					}
				case "isdigit", "isdecimal":
					if !unicode.IsDigit(r) {
						return false, nil
					}
				case "isspace":
					if !unicode.IsSpace(r) {
						return false, nil
					}
				case "islower":
					if unicode.IsUpper(r) || unicode.IsTitle(r) {
						return false, nil
					}
					hasCase = hasCase || unicode.IsLower(r)
				case "isupper":
					if unicode.IsLower(r) {
						return false, nil
					}
					hasCase = hasCase || unicode.IsUpper(r)
				}
			}
			if name == "islower" || name == "isupper" {
				return hasCase, nil
			}
			return true, nil
		})
	}
	return nil
}

// ---- internal/sentinel/value.go ----

// 显式引用容器保留 copy、DOM、闭包和寄存器自引用的原有语义。
type sentinel_array struct{ values []any }

func sentinel_arr(values ...any) *sentinel_array { return &sentinel_array{values: values} }

type sentinel_entry struct{ key, value any }
type sentinel_object struct {
	entries []sentinel_entry
	index   map[string]int
}

func sentinel_obj(values ...any) *sentinel_object {
	o := &sentinel_object{index: map[string]int{}}
	for i := 0; i+1 < len(values); i += 2 {
		_ = o.set(values[i], values[i+1])
	}
	return o
}
func (o *sentinel_object) set(key, value any) error {
	k, err := sentinel_keyString(key)
	if err != nil {
		return err
	}
	if i, ok := o.index[k]; ok {
		o.entries[i].value = value
	} else {
		o.index[k] = len(o.entries)
		o.entries = append(o.entries, sentinel_entry{key, value})
	}
	return nil
}
func (o *sentinel_object) get(key any) any {
	k, err := sentinel_keyString(key)
	if err != nil {
		return nil
	}
	if i, ok := o.index[k]; ok {
		return o.entries[i].value
	}
	return nil
}
func (o *sentinel_object) has(key any) bool {
	k, err := sentinel_keyString(key)
	if err != nil {
		return false
	}
	_, ok := o.index[k]
	return ok
}
func (o *sentinel_object) remove(key any) {
	k, err := sentinel_keyString(key)
	if err != nil {
		return
	}
	i, ok := o.index[k]
	if !ok {
		return
	}
	delete(o.index, k)
	o.entries = append(o.entries[:i], o.entries[i+1:]...)
	for j := i; j < len(o.entries); j++ {
		key, _ := sentinel_keyString(o.entries[j].key)
		o.index[key] = j
	}
}

type sentinel_callable struct{ function func(...any) (any, error) }

func sentinel_fn(f func(...any) (any, error)) *sentinel_callable {
	return &sentinel_callable{function: f}
}

func sentinel_keyString(key any) (string, error) {
	switch v := key.(type) {
	case nil:
		return "null", nil
	case string:
		return "s:" + v, nil
	case bool:
		if v {
			return "n:1", nil
		}
		return "n:0", nil
	case int:
		return "n:" + strconv.Itoa(v), nil
	case int64:
		return "n:" + strconv.FormatInt(v, 10), nil
	case *big.Int:
		return "n:" + v.String(), nil
	case float64:
		if r := new(big.Rat).SetFloat64(v); r != nil {
			return "n:" + r.RatString(), nil
		}
		return "n:" + sentinel_pythonFloat(v), nil
	default:
		return "", fmt.Errorf("unhashable type: '%s'", sentinel_typeName(key))
	}
}

func sentinel_typeName(value any) string {
	switch value.(type) {
	case nil:
		return "NoneType"
	case bool:
		return "bool"
	case string:
		return "str"
	case int, int64, *big.Int:
		return "int"
	case float64:
		return "float"
	case *sentinel_array:
		return "list"
	case *sentinel_object:
		return "dict"
	case *sentinel_callable:
		return "function"
	case []byte:
		return "bytes"
	}
	return "object"
}

func sentinel_truth(value any) bool {
	switch v := value.(type) {
	case nil:
		return false
	case bool:
		return v
	case string:
		return v != ""
	case int:
		return v != 0
	case int64:
		return v != 0
	case *big.Int:
		return v.Sign() != 0
	case float64:
		return v != 0
	case *sentinel_array:
		return len(v.values) != 0
	case *sentinel_object:
		return len(v.entries) != 0
	case []byte:
		return len(v) != 0
	}
	return true
}

func sentinel_strOrEmpty(value any) string {
	if !sentinel_truth(value) {
		return ""
	}
	return sentinel_pyStr(value)
}
func sentinel_pyStr(value any) string {
	if s, ok := value.(string); ok {
		return s
	}
	return sentinel_pyRepr(value, map[any]bool{})
}
func sentinel_pyRepr(value any, seen map[any]bool) string {
	switch v := value.(type) {
	case nil:
		return "None"
	case bool:
		if v {
			return "True"
		}
		return "False"
	case string:
		return sentinel_quotePython(v)
	case int:
		return strconv.Itoa(v)
	case int64:
		return strconv.FormatInt(v, 10)
	case *big.Int:
		return v.String()
	case float64:
		return sentinel_pythonFloat(v)
	case *sentinel_array:
		if seen[v] {
			return "[...]"
		}
		seen[v] = true
		defer delete(seen, v)
		values := make([]string, len(v.values))
		for i, child := range v.values {
			values[i] = sentinel_pyRepr(child, seen)
		}
		return "[" + strings.Join(values, ", ") + "]"
	case *sentinel_object:
		if seen[v] {
			return "{...}"
		}
		seen[v] = true
		defer delete(seen, v)
		values := make([]string, len(v.entries))
		for i, item := range v.entries {
			values[i] = sentinel_pyRepr(item.key, seen) + ": " + sentinel_pyRepr(item.value, seen)
		}
		return "{" + strings.Join(values, ", ") + "}"
	case []byte:
		return "b" + sentinel_quotePython(sentinel_latin1String(v))
	case *sentinel_callable:
		return "<function>"
	}
	return fmt.Sprint(value)
}

func sentinel_quotePython(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString("\\\\")
		case '\n':
			b.WriteString("\\n")
		case '\r':
			b.WriteString("\\r")
		case '\t':
			b.WriteString("\\t")
		default:
			if r == quote {
				b.WriteRune('\\')
				b.WriteRune(r)
			} else if unicode.IsPrint(r) || r == ' ' {
				b.WriteRune(r)
			} else if r < 256 {
				fmt.Fprintf(&b, "\\x%02x", r)
			} else if r <= 0xffff {
				fmt.Fprintf(&b, "\\u%04x", r)
			} else {
				fmt.Fprintf(&b, "\\U%08x", r)
			}
		}
	}
	b.WriteRune(quote)
	return b.String()
}

func sentinel_fromNative(value any) any {
	switch v := value.(type) {
	case []any:
		a := sentinel_arr()
		for _, child := range v {
			a.values = append(a.values, sentinel_fromNative(child))
		}
		return a
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		// 账号 bootstrap 的构造顺序与 Python 客户端固定顺序一致。
		if _, ok := v["session"]; ok {
			for i, k := range keys {
				if k == "session" {
					keys = append([]string{"session"}, append(keys[:i], keys[i+1:]...)...)
					break
				}
			}
		}
		o := sentinel_obj()
		for _, key := range keys {
			_ = o.set(key, sentinel_fromNative(v[key]))
		}
		return o
	case int:
		return int64(v)
	}
	return value
}

// ---- internal/sentinel/vm.go ----

const (
	sentinel_maxInstructions   = 20000
	sentinel_maxExecutionDepth = 32
	sentinel_maxDataDepth      = 64
	sentinel_maxValueBytes     = 256 * 1024
	sentinel_maxContainerItems = 100000
	sentinel_maxRegisterBytes  = 4 * 1024 * 1024
	sentinel_maxDXBytes        = 1024 * 1024
	sentinel_maxRegexBytes     = 4 * 1024
)

type sentinel_Options struct {
	Deadline  time.Time
	Profile   protocol_BrowserProfile
	Cookies   string
	Clock     fingerprint_Clock
	Bootstrap map[string]any
}

type sentinel_limits struct{ instructions, executionDepth, dataDepth, valueBytes, containerItems, registerBytes, dxBytes, regexBytes int }

func sentinel_defaultLimits() sentinel_limits {
	return sentinel_limits{sentinel_maxInstructions, sentinel_maxExecutionDepth, sentinel_maxDataDepth, sentinel_maxValueBytes, sentinel_maxContainerItems, sentinel_maxRegisterBytes, sentinel_maxDXBytes, sentinel_maxRegexBytes}
}

type sentinel_size struct{ bytes, items int }

func (a sentinel_size) add(b sentinel_size) sentinel_size {
	return sentinel_size{a.bytes + b.bytes, a.items + b.items}
}
func (a sentinel_size) sub(b sentinel_size) sentinel_size {
	return sentinel_size{a.bytes - b.bytes, a.items - b.items}
}

type sentinel_machine struct {
	options          sentinel_Options
	limits           sentinel_limits
	regs             *sentinel_object
	registerSizes    map[string]sentinel_size
	registerTotal    sentinel_size
	mutationSizes    map[any]sentinel_size
	mutationTotal    sentinel_size
	window           *sentinel_object
	success          *string
	remaining, depth int
}

func sentinel_SolveTurnstileDX(dx, requirements string, options sentinel_Options) (string, error) {
	return sentinel_solveWithLimits(dx, requirements, options, sentinel_defaultLimits())
}
func sentinel_solveWithLimits(dx, requirements string, options sentinel_Options, limits sentinel_limits) (token string, err error) {
	// 解析/方法调用出现普通错误时对外只返回稳定码，保留本地错误链。
	defer func() {
		if recovered := recover(); recovered != nil {
			err = &sentinel_Error{Code: "sentinel_turnstile_error", Cause: fmt.Errorf("vm execution failed")}
			token = ""
		}
		if err != nil {
			var protocol *sentinel_Error
			if !errors.As(err, &protocol) {
				err = &sentinel_Error{Code: "sentinel_turnstile_error", Cause: err}
			}
		}
	}()
	if err = sentinel_checkDeadline(options.Deadline); err != nil {
		return "", err
	}
	if utf8.RuneCountInString(dx) > limits.dxBytes {
		return "", sentinel_resourceError("turnstile_dx_limit")
	}
	decoded, err := sentinel_b64DecodeLatin1(dx)
	if err != nil {
		return "", err
	}
	decoded = sentinel_xorLatin1(decoded, requirements)
	if utf8.RuneCountInString(decoded) > limits.dxBytes {
		return "", sentinel_resourceError("turnstile_dx_limit")
	}
	if err = sentinel_checkDeadline(options.Deadline); err != nil {
		return "", err
	}
	program, err := sentinel_parseJSON(decoded)
	if err != nil {
		return "", err
	}
	m := &sentinel_machine{options: options, limits: limits, regs: sentinel_obj(), registerSizes: map[string]sentinel_size{}, mutationSizes: map[any]sentinel_size{}, remaining: limits.instructions}
	if _, err = m.measure(program); err != nil {
		return "", err
	}
	m.window = m.buildWindow()
	if err = m.initialize(requirements, program); err != nil {
		return "", err
	}
	if err = m.run(); err != nil {
		return "", err
	}
	if m.success == nil || *m.success == "" {
		return "", sentinel_resourceError("turnstile_result_missing")
	}
	if err = sentinel_checkDeadline(options.Deadline); err != nil {
		return "", err
	}
	return *m.success, nil
}

func (m *sentinel_machine) measure(value any) (sentinel_size, error) {
	seen := map[any]bool{}
	nodes, items := 0, 0
	var walk func(any, int) (int, error)
	walk = func(value any, depth int) (int, error) {
		nodes++
		if nodes%128 == 1 {
			if err := sentinel_checkDeadline(m.options.Deadline); err != nil {
				return 0, err
			}
		}
		if depth > m.limits.dataDepth {
			return 0, sentinel_resourceError("turnstile_data_depth_limit")
		}
		switch v := value.(type) {
		case string:
			if !utf8.ValidString(v) {
				return 0, fmt.Errorf("invalid UTF-8 string")
			}
			if len(v) > m.limits.valueBytes {
				return 0, sentinel_resourceError("turnstile_value_limit")
			}
			return len(v), nil
		case []byte:
			if len(v) > m.limits.valueBytes {
				return 0, sentinel_resourceError("turnstile_value_limit")
			}
			return len(v), nil
		case *sentinel_array:
			if seen[v] {
				return 0, nil
			}
			seen[v] = true
			items += len(v.values)
			if items > m.limits.containerItems {
				return 0, sentinel_resourceError("turnstile_container_limit")
			}
			total := len(v.values)
			for _, child := range v.values {
				n, err := walk(child, depth+1)
				if err != nil {
					return 0, err
				}
				total += n
				if total > m.limits.registerBytes {
					return 0, sentinel_resourceError("turnstile_register_limit")
				}
			}
			return total, nil
		case *sentinel_object:
			if seen[v] {
				return 0, nil
			}
			seen[v] = true
			items += len(v.entries)
			if items > m.limits.containerItems {
				return 0, sentinel_resourceError("turnstile_container_limit")
			}
			total := len(v.entries)
			for _, child := range v.entries {
				for _, part := range []any{child.key, child.value} {
					n, err := walk(part, depth+1)
					if err != nil {
						return 0, err
					}
					total += n
				}
				if total > m.limits.registerBytes {
					return 0, sentinel_resourceError("turnstile_register_limit")
				}
			}
			return total, nil
		}
		return 1, nil
	}
	bytes, err := walk(value, 0)
	if err != nil {
		return sentinel_size{}, err
	}
	if err = sentinel_checkDeadline(m.options.Deadline); err != nil {
		return sentinel_size{}, err
	}
	return sentinel_size{bytes, items}, nil
}

func (m *sentinel_machine) checkSize(s sentinel_size) error {
	if s.bytes > m.limits.registerBytes {
		return sentinel_resourceError("turnstile_register_limit")
	}
	if s.items > m.limits.containerItems {
		return sentinel_resourceError("turnstile_container_limit")
	}
	return nil
}
func (m *sentinel_machine) set(key, value any) error {
	s, err := m.measure(value)
	if err != nil {
		return err
	}
	k, err := sentinel_keyString(key)
	if err != nil {
		return err
	}
	updated := m.registerTotal.sub(m.registerSizes[k]).add(s)
	if err = m.checkSize(updated.add(m.mutationTotal)); err != nil {
		return err
	}
	if err = m.regs.set(key, value); err != nil {
		return err
	}
	m.registerSizes[k] = s
	m.registerTotal = updated
	return nil
}
func (m *sentinel_machine) get(key any) any { return m.regs.get(key) }
func (m *sentinel_machine) refresh(key any) error {
	if m.regs.has(key) {
		return m.set(key, m.get(key))
	}
	return nil
}
func (m *sentinel_machine) mutate(value any) error {
	s, err := m.measure(value)
	if err != nil {
		return err
	}
	switch value.(type) {
	case *sentinel_object, *sentinel_array:
	default:
		return nil
	}
	updated := m.mutationTotal.sub(m.mutationSizes[value]).add(s)
	if err = m.checkSize(m.registerTotal.add(updated)); err != nil {
		return err
	}
	m.mutationSizes[value] = s
	m.mutationTotal = updated
	return nil
}

func (m *sentinel_machine) initialize(requirements string, program any) error {
	var initErr error
	handle := func(opcode, minArgs, maxArgs int, handler func([]any) (any, error)) {
		if initErr != nil {
			return
		}
		initErr = m.set(int64(opcode), sentinel_fn(func(args ...any) (any, error) {
			if len(args) < minArgs || (maxArgs >= 0 && len(args) > maxArgs) {
				return nil, fmt.Errorf("invalid argument count")
			}
			return handler(args)
		}))
	}
	noResult := func(err error) (any, error) { return nil, err }
	noop := func([]any) (any, error) { return nil, nil }
	handle(0, 1, 1, noop)
	handle(1, 2, 2, func(a []any) (any, error) {
		return noResult(m.set(a[0], sentinel_xorLatin1(sentinel_strOrEmpty(m.get(a[0])), sentinel_strOrEmpty(m.get(a[1])))))
	})
	handle(2, 2, 2, func(a []any) (any, error) { return noResult(m.set(a[0], a[1])) })
	encodeResult := func(value any) (string, error) {
		raw := sentinel_pyStr(value)
		if _, err := m.measure(raw); err != nil {
			return "", err
		}
		encoded, err := sentinel_b64EncodeLatin1(raw)
		if err != nil {
			return "", err
		}
		if _, err = m.measure(encoded); err != nil {
			return "", err
		}
		return encoded, nil
	}
	handle(3, 1, 1, func(a []any) (any, error) {
		s, err := encodeResult(a[0])
		if err != nil {
			return nil, err
		}
		m.success = &s
		return nil, nil
	})
	handle(4, 1, 1, func(a []any) (any, error) {
		s, err := encodeResult(a[0])
		if err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("%s", s)
	})
	handle(5, 2, 2, func(a []any) (any, error) {
		cur, add := m.get(a[0]), m.get(a[1])
		if list, ok := cur.(*sentinel_array); ok {
			if len(list.values) >= m.limits.containerItems {
				return nil, sentinel_resourceError("turnstile_container_limit")
			}
			list.values = append(list.values, add)
			return noResult(m.refresh(a[0]))
		}
		text := ""
		if add != nil {
			text = sentinel_pyStr(add)
		}
		return noResult(m.set(a[0], sentinel_strOrEmpty(cur)+text))
	})
	handle(6, 3, 3, func(a []any) (any, error) {
		v, err := m.lookup(m.get(a[1]), m.get(a[2]))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(7, 1, -1, func(a []any) (any, error) {
		f, ok := m.get(a[0]).(*sentinel_callable)
		if !ok {
			return nil, fmt.Errorf("call non-function %s", sentinel_pyStr(a[0]))
		}
		return f.function(m.dereference(a[1:])...)
	})
	copyRegister := func(a []any) (any, error) { return noResult(m.set(a[0], m.get(a[1]))) }
	handle(8, 2, 2, copyRegister)
	if initErr == nil {
		initErr = m.set(int64(10), m.window)
	}
	handle(11, 2, 2, func(a []any) (any, error) {
		v, err := m.findScript(sentinel_strOrEmpty(m.get(a[1])))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(12, 1, 1, func(a []any) (any, error) { return noResult(m.set(a[0], m.regs)) })
	handle(13, 2, -1, func(a []any) (any, error) {
		if f, ok := m.get(a[1]).(*sentinel_callable); ok {
			_, err := f.function(a[2:]...)
			if err != nil {
				if sentinel_isProtocol(err) {
					return nil, err
				}
				return noResult(m.set(a[0], err.Error()))
			}
		}
		return nil, nil
	})
	handle(14, 2, 2, func(a []any) (any, error) {
		v, err := sentinel_parseJSON(sentinel_strOrEmpty(m.get(a[1])))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(15, 2, 2, func(a []any) (any, error) {
		v, err := sentinel_dumpJSON(m.get(a[1]), true, true)
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(17, 2, -1, func(a []any) (any, error) {
		var v any
		var err error
		if f, ok := m.get(a[1]).(*sentinel_callable); ok {
			v, err = f.function(m.dereference(a[2:])...)
		}
		if err != nil {
			if sentinel_isProtocol(err) {
				return nil, err
			}
			v = err.Error()
		}
		return noResult(m.set(a[0], v))
	})
	handle(18, 1, 1, func(a []any) (any, error) {
		v, err := sentinel_b64DecodeLatin1(sentinel_strOrEmpty(m.get(a[0])))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(19, 1, 1, func(a []any) (any, error) {
		v, err := sentinel_b64EncodeLatin1(sentinel_strOrEmpty(m.get(a[0])))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(20, 3, -1, func(a []any) (any, error) {
		if sentinel_equal(m.get(a[0]), m.get(a[1])) {
			if f, ok := m.get(a[2]).(*sentinel_callable); ok {
				return f.function(a[3:]...)
			}
		}
		return nil, nil
	})
	handle(21, 4, -1, func(a []any) (any, error) {
		diff, err := sentinel_subtract(m.get(a[0]), m.get(a[1]))
		if err != nil {
			return nil, nil
		}
		var absolute any
		if n, ok := sentinel_asInt(diff); ok {
			absolute = sentinel_compactInt(n.Abs(n))
		} else if n, ok := diff.(float64); ok {
			absolute = math.Abs(n)
		} else {
			return nil, nil
		}
		r, err := sentinel_less(m.get(a[2]), absolute)
		if err != nil {
			return nil, nil
		}
		if r {
			if f, ok := m.get(a[3]).(*sentinel_callable); ok {
				v, err := f.function(a[4:]...)
				if err != nil && !sentinel_isProtocol(err) {
					return nil, nil
				}
				return v, err
			}
		}
		return nil, nil
	})
	handle(22, 2, 2, func(a []any) (any, error) {
		prev := m.copyQueue()
		body, err := sentinel_listCopy(a[1])
		if err != nil {
			return nil, err
		}
		if err = m.set(int64(9), body); err != nil {
			return nil, err
		}
		err = m.run()
		if err != nil && !sentinel_isProtocol(err) {
			err = m.set(a[0], err.Error())
		}
		restore := m.set(int64(9), prev)
		if restore != nil {
			return nil, restore
		}
		return nil, err
	})
	handle(23, 2, -1, func(a []any) (any, error) {
		if m.get(a[0]) != nil {
			if f, ok := m.get(a[1]).(*sentinel_callable); ok {
				return f.function(a[2:]...)
			}
		}
		return nil, nil
	})
	handle(24, 3, 3, func(a []any) (any, error) {
		v, err := m.lookup(m.get(a[1]), m.get(a[2]))
		if err != nil {
			return nil, err
		}
		f, ok := v.(*sentinel_callable)
		if !ok {
			return nil, fmt.Errorf("bind fail key=%s", sentinel_pyStr(m.get(a[2])))
		}
		return noResult(m.set(a[0], sentinel_fn(f.function)))
	})
	for _, opcode := range []int{25, 26} {
		handle(opcode, 0, -1, noop)
	}
	handle(27, 2, 2, func(a []any) (any, error) {
		cur, value := m.get(a[0]), m.get(a[1])
		if list, ok := cur.(*sentinel_array); ok {
			for i, item := range list.values {
				if sentinel_equal(item, value) {
					list.values = append(list.values[:i], list.values[i+1:]...)
					break
				}
			}
			return noResult(m.refresh(a[0]))
		}
		v, err := sentinel_subtract(cur, value)
		if err != nil {
			v = nil
		}
		return noResult(m.set(a[0], v))
	})
	handle(28, 0, -1, noop)
	handle(29, 3, 3, func(a []any) (any, error) {
		v, err := sentinel_less(m.get(a[1]), m.get(a[2]))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], v))
	})
	handle(30, 4, 4, func(a []any) (any, error) {
		target, ret := a[0], a[1]
		params := sentinel_arr()
		var body any = a[2]
		if r, ok := a[3].(*sentinel_array); ok {
			var ok bool
			params, ok = a[2].(*sentinel_array)
			if !ok {
				return nil, fmt.Errorf("parameter list is not iterable")
			}
			body = r
		} else if !sentinel_truth(body) {
			body = sentinel_arr()
		}
		return noResult(m.set(target, sentinel_fn(func(args ...any) (result any, err error) {
			prev := m.copyQueue()
			for i, param := range params.values {
				if i < len(args) {
					if err = m.set(param, args[i]); err != nil {
						return nil, err
					}
				}
			}
			queue, err := sentinel_listCopy(body)
			if err != nil {
				return nil, err
			}
			if err = m.set(int64(9), queue); err != nil {
				return nil, err
			}
			defer func() {
				if restore := m.set(int64(9), prev); restore != nil {
					err = restore
				}
			}()
			if err = m.run(); err != nil {
				return nil, err
			}
			return m.get(ret), nil
		})))
	})
	handle(33, 3, 3, func(a []any) (any, error) {
		x, err := sentinel_asFloat(m.get(a[1]))
		if err != nil {
			return nil, err
		}
		y, err := sentinel_asFloat(m.get(a[2]))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], x*y))
	})
	handle(34, 2, 2, copyRegister)
	handle(35, 3, 3, func(a []any) (any, error) {
		denom := m.get(a[2])
		if !sentinel_truth(denom) {
			denom = int64(0)
		}
		d, err := sentinel_asFloat(denom)
		if err != nil {
			return nil, err
		}
		if d == 0 {
			return noResult(m.set(a[0], int64(0)))
		}
		n, err := sentinel_asFloat(m.get(a[1]))
		if err != nil {
			return nil, err
		}
		return noResult(m.set(a[0], n/d))
	})
	if initErr != nil {
		return initErr
	}
	if err := m.set(int64(16), requirements); err != nil {
		return err
	}
	return m.set(int64(9), program)
}

func sentinel_isProtocol(err error) bool {
	var protocol *sentinel_Error
	return errors.As(err, &protocol)
}
func (m *sentinel_machine) dereference(args []any) []any {
	values := make([]any, len(args))
	for i, a := range args {
		values[i] = m.get(a)
	}
	return values
}
func sentinel_listCopy(value any) (*sentinel_array, error) {
	switch v := value.(type) {
	case *sentinel_array:
		return sentinel_arr(append([]any(nil), v.values...)...), nil
	case *sentinel_object:
		a := sentinel_arr()
		for _, e := range v.entries {
			a.values = append(a.values, e.key)
		}
		return a, nil
	case string:
		a := sentinel_arr()
		for _, r := range v {
			a.values = append(a.values, string(r))
		}
		return a, nil
	}
	return nil, fmt.Errorf("'%s' object is not iterable", sentinel_typeName(value))
}
func (m *sentinel_machine) copyQueue() *sentinel_array {
	v := m.get(int64(9))
	if !sentinel_truth(v) {
		return sentinel_arr()
	}
	a, _ := sentinel_listCopy(v)
	if a == nil {
		return sentinel_arr()
	}
	return a
}
func (m *sentinel_machine) run() error {
	if m.depth >= m.limits.executionDepth {
		return sentinel_resourceError("turnstile_execution_depth_limit")
	}
	m.depth++
	defer func() { m.depth-- }()
	for {
		if err := sentinel_checkDeadline(m.options.Deadline); err != nil {
			return err
		}
		queue, ok := m.get(int64(9)).(*sentinel_array)
		if !ok || len(queue.values) == 0 {
			return nil
		}
		instr := queue.values[0]
		queue.values = queue.values[1:]
		a, ok := instr.(*sentinel_array)
		if !ok || len(a.values) == 0 {
			continue
		}
		opcode := a.values[0]
		handler := m.get(opcode)
		f, ok := handler.(*sentinel_callable)
		if !ok {
			return fmt.Errorf("opcode %s is %s", sentinel_pyStr(opcode), sentinel_typeName(handler))
		}
		m.remaining--
		if m.remaining < 0 {
			return sentinel_resourceError("turnstile_instruction_limit")
		}
		if _, err := f.function(a.values[1:]...); err != nil {
			return err
		}
		if err := sentinel_checkDeadline(m.options.Deadline); err != nil {
			return err
		}
		if m.success != nil {
			return nil
		}
	}
}

// ---- internal/sentinel/window.go ----

func sentinel_native(minArgs, maxArgs int, f func([]any) (any, error)) *sentinel_callable {
	return sentinel_fn(func(args ...any) (any, error) {
		if len(args) < minArgs || (maxArgs >= 0 && len(args) > maxArgs) {
			return nil, fmt.Errorf("invalid argument count")
		}
		return f(args)
	})
}
func sentinel_constant(value any) *sentinel_callable {
	return sentinel_native(0, -1, func([]any) (any, error) { return value, nil })
}

func (m *sentinel_machine) element(tag any) *sentinel_object {
	name := sentinel_strOrEmpty(tag)
	if name == "" {
		name = "DIV"
	}
	name = sentinel_upper(name)
	el := sentinel_obj("nodeName", name, "tagName", name, "style", sentinel_obj(), "attributes", sentinel_obj(), "children", sentinel_arr(), "parentNode", nil, "ownerDocument", nil)
	attributes := el.get("attributes").(*sentinel_object)
	children := el.get("children").(*sentinel_array)
	_ = el.set("setAttribute", sentinel_native(2, 2, func(a []any) (any, error) {
		if err := attributes.set(sentinel_pyStr(a[0]), sentinel_pyStr(a[1])); err != nil {
			return nil, err
		}
		return nil, m.mutate(attributes)
	}))
	_ = el.set("getAttribute", sentinel_native(1, 1, func(a []any) (any, error) { return attributes.get(a[0]), nil }))
	_ = el.set("appendChild", sentinel_native(1, 1, func(a []any) (any, error) {
		children.values = append(children.values, a[0])
		if child, ok := a[0].(*sentinel_object); ok {
			_ = child.set("parentNode", el)
		}
		if err := m.mutate(children); err != nil {
			return nil, err
		}
		return a[0], nil
	}))
	_ = el.set("removeChild", sentinel_native(1, 1, func(a []any) (any, error) {
		items := children.values[:0]
		for _, child := range children.values {
			if !sentinel_identical(child, a[0]) {
				items = append(items, child)
			}
		}
		children.values = items
		if err := m.mutate(children); err != nil {
			return nil, err
		}
		return a[0], nil
	}))
	_ = el.set("remove", sentinel_native(0, 0, func([]any) (any, error) {
		if parent, ok := el.get("parentNode").(*sentinel_object); ok {
			if kids, ok := parent.get("children").(*sentinel_array); ok {
				items := kids.values[:0]
				for _, child := range kids.values {
					if !sentinel_identical(child, el) {
						items = append(items, child)
					}
				}
				kids.values = items
				return nil, m.mutate(kids)
			}
		}
		return nil, nil
	}))
	_ = el.set("click", sentinel_native(0, 0, func([]any) (any, error) { return nil, nil }))
	_ = el.set("addEventListener", sentinel_constant(nil))
	_ = el.set("removeEventListener", sentinel_constant(nil))
	_ = el.set("getBoundingClientRect", sentinel_native(0, 0, func([]any) (any, error) {
		return sentinel_obj("x", int64(0), "y", int64(0), "width", int64(0), "height", int64(0), "top", int64(0), "left", int64(0), "right", int64(0), "bottom", int64(0)), nil
	}))
	return el
}

func sentinel_identical(a, b any) bool {
	switch x := a.(type) {
	case *sentinel_object:
		y, ok := b.(*sentinel_object)
		return ok && x == y
	case *sentinel_array:
		y, ok := b.(*sentinel_array)
		return ok && x == y
	case *sentinel_callable:
		y, ok := b.(*sentinel_callable)
		return ok && x == y
	case nil:
		return b == nil
	}
	return sentinel_equal(a, b)
}

func (m *sentinel_machine) buildWindow() *sentinel_object {
	document := sentinel_obj()
	body, html := m.element("body"), m.element("html")
	_ = document.set("scripts", sentinel_arr(sentinel_obj("src", sentinel_SDKStubURL), sentinel_obj("src", sentinel_SDKURL)))
	_ = document.set("body", body)
	_ = document.set("documentElement", html)
	_ = document.set("cookie", m.options.Cookies)
	_ = document.set("createElement", sentinel_native(1, 1, func(a []any) (any, error) { return m.element(a[0]), nil }))
	_ = document.set("getElementById", sentinel_constant(nil))
	_ = document.set("querySelector", sentinel_constant(nil))
	_ = document.set("querySelectorAll", sentinel_native(0, -1, func([]any) (any, error) { return sentinel_arr(), nil }))
	_ = body.set("ownerDocument", document)
	_ = html.set("ownerDocument", document)
	storage := sentinel_obj()
	storageAPI := sentinel_obj(
		"getItem", sentinel_native(1, 1, func(a []any) (any, error) { return storage.get(sentinel_pyStr(a[0])), nil }),
		"setItem", sentinel_native(2, 2, func(a []any) (any, error) {
			_ = storage.set(sentinel_pyStr(a[0]), sentinel_pyStr(a[1]))
			return nil, m.mutate(storage)
		}),
		"removeItem", sentinel_native(1, 1, func(a []any) (any, error) { storage.remove(sentinel_pyStr(a[0])); return nil, m.mutate(storage) }),
	)
	reflectSet := func(a []any) (any, error) {
		target, ok := a[0].(*sentinel_object)
		if !ok {
			return nil, fmt.Errorf("'%s' object has no attribute '%s'", sentinel_typeName(a[0]), sentinel_pyStr(a[1]))
		}
		if err := target.set(a[1], a[2]); err != nil {
			return nil, err
		}
		if err := m.mutate(target); err != nil {
			return nil, err
		}
		return true, nil
	}
	profile := m.options.Profile
	var pixelRatio any = profile.DevicePixelRatio
	if profile.DevicePixelRatio == math.Trunc(profile.DevicePixelRatio) {
		pixelRatio = int64(profile.DevicePixelRatio)
	}
	bootstrap := sentinel_fromNative(m.options.Bootstrap)
	if bootstrap == nil {
		bootstrap = sentinel_obj()
	}
	window := sentinel_obj(
		"document", document,
		"navigator", sentinel_obj("userAgent", profile.UserAgent, "language", "en-US", "languages", sentinel_arr("en-US", "en"), "hardwareConcurrency", int64(profile.HardwareConcurrency), "platform", profile.NavigatorPlatform, "vendor", "Google Inc.", "webdriver", false),
		"screen", sentinel_obj("width", int64(profile.ScreenWidth), "height", int64(profile.ScreenHeight), "availWidth", int64(profile.AvailWidth), "availHeight", int64(profile.AvailHeight), "colorDepth", int64(profile.ColorDepth), "pixelDepth", int64(profile.ColorDepth)),
		"history", sentinel_obj("length", int64(2), "scrollRestoration", "auto"),
		"location", sentinel_obj("href", "https://chatgpt.com/", "search", "", "pathname", "/", "origin", "https://chatgpt.com", "host", "chatgpt.com", "protocol", "https:"),
		"localStorage", storageAPI,
		"performance", sentinel_obj("now", sentinel_native(0, 0, func([]any) (any, error) { return m.options.Clock.NowMS(), nil }), "memory", sentinel_obj("jsHeapSizeLimit", int64(4294705152), "totalJSHeapSize", int64(10000000), "usedJSHeapSize", int64(5000000))),
		"Reflect", sentinel_obj("set", sentinel_native(3, -1, reflectSet), "get", sentinel_native(2, -1, func(a []any) (any, error) {
			if target, ok := a[0].(*sentinel_object); ok {
				return target.get(a[1]), nil
			}
			return m.lookup(a[0], sentinel_pyStr(a[1]))
		})),
		"Object", sentinel_obj("create", sentinel_native(0, -1, func([]any) (any, error) { return sentinel_obj(), nil }), "keys", sentinel_native(1, 1, func(a []any) (any, error) {
			keys := sentinel_arr()
			if o, ok := a[0].(*sentinel_object); ok {
				for _, e := range o.entries {
					keys.values = append(keys.values, e.key)
				}
			}
			return keys, nil
		}), "assign", sentinel_native(1, -1, func(a []any) (any, error) { return a[0], nil }), "set", sentinel_native(3, 3, func(a []any) (any, error) { _, err := reflectSet(a); return nil, err })),
		"Math", m.mathObject(),
		"JSON", sentinel_obj("parse", sentinel_native(1, 1, func(a []any) (any, error) {
			s, ok := a[0].(string)
			if !ok {
				return nil, fmt.Errorf("the JSON object must be str, bytes or bytearray, not %s", sentinel_typeName(a[0]))
			}
			return sentinel_parseJSON(s)
		}), "stringify", sentinel_native(1, 1, func(a []any) (any, error) { return sentinel_dumpJSON(a[0], true, true) })),
		"innerWidth", int64(profile.ScreenWidth), "innerHeight", int64(profile.ScreenHeight), "devicePixelRatio", pixelRatio, "chrome", sentinel_obj("runtime", sentinel_obj()),
		"__reactRouterContext", sentinel_obj("state", sentinel_obj("loaderData", sentinel_obj("root", sentinel_obj("clientBootstrap", bootstrap)))),
		"atob", sentinel_native(1, 1, func(a []any) (any, error) {
			s, ok := a[0].(string)
			if !ok {
				return nil, fmt.Errorf("encoding expects str")
			}
			return sentinel_b64DecodeLatin1(s)
		}),
		"btoa", sentinel_native(1, 1, func(a []any) (any, error) {
			s, ok := a[0].(string)
			if !ok {
				return nil, fmt.Errorf("encoding expects str")
			}
			return sentinel_b64EncodeLatin1(s)
		}),
	)
	_ = window.set("window", window)
	return window
}

func (m *sentinel_machine) mathObject() *sentinel_object {
	abs := sentinel_native(1, 1, func(a []any) (any, error) {
		if i, ok := sentinel_asInt(a[0]); ok {
			return sentinel_compactInt(new(big.Int).Abs(i)), nil
		}
		if f, ok := a[0].(float64); ok {
			return math.Abs(f), nil
		}
		return nil, fmt.Errorf("bad operand type for abs(): '%s'", sentinel_typeName(a[0]))
	})
	floor := sentinel_native(1, 1, func(a []any) (any, error) {
		if i, ok := sentinel_asInt(a[0]); ok {
			return sentinel_compactInt(i), nil
		}
		f, ok := a[0].(float64)
		if !ok {
			return nil, fmt.Errorf("unsupported floor operand")
		}
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return nil, fmt.Errorf("cannot convert float to integer")
		}
		i, _ := new(big.Float).SetFloat64(math.Floor(f)).Int(nil)
		return sentinel_compactInt(i), nil
	})
	round := sentinel_native(1, 2, func(a []any) (any, error) {
		if len(a) == 2 {
			places, err := sentinel_integerIndex(a[1])
			if err != nil {
				return nil, err
			}
			f, err := sentinel_asFloat(a[0])
			if err != nil {
				return nil, err
			}
			if places >= 0 && places <= 308 {
				return sentinel_roundDecimal(f, places), nil
			}
			return nil, fmt.Errorf("unsupported rounding precision")
		}
		if i, ok := sentinel_asInt(a[0]); ok {
			return sentinel_compactInt(i), nil
		}
		f, err := sentinel_asFloat(a[0])
		if err != nil {
			return nil, err
		}
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return nil, fmt.Errorf("cannot convert float to integer")
		}
		i, _ := new(big.Float).SetFloat64(math.RoundToEven(f)).Int(nil)
		return sentinel_compactInt(i), nil
	})
	minMax := func(maximum bool) *sentinel_callable {
		return sentinel_native(1, -1, func(a []any) (any, error) {
			values := a
			if len(a) == 1 {
				v, err := sentinel_listCopy(a[0])
				if err != nil {
					return nil, err
				}
				values = v.values
			}
			if len(values) == 0 {
				return nil, fmt.Errorf("arg is an empty sequence")
			}
			best := values[0]
			for _, value := range values[1:] {
				var take bool
				var err error
				if maximum {
					take, err = sentinel_less(best, value)
				} else {
					take, err = sentinel_less(value, best)
				}
				if err != nil {
					return nil, err
				}
				if take {
					best = value
				}
			}
			return best, nil
		})
	}
	return sentinel_obj("abs", abs, "floor", floor, "round", round, "random", sentinel_native(0, 0, func([]any) (any, error) { return dep_math_rand_v2.Float64(), nil }), "max", minMax(true), "min", minMax(false))
}

// ---- internal/stripe/build.go ----

// API 发布版本与 Stripe.js 的部署修订不同，保持当前 Basil 协议契约。
const stripe_CustomAPIVersion = "2025-03-31.basil"
const stripe_CustomJSURL = "https://js.stripe.com/basil/stripe.js"

// Custom Checkout Session 由 ChatGPT 带 beta 创建。payment_pages confirm
// 若不声明这些 beta，Stripe 会 HTTP 200 但会话保持 open/unpaid。
const stripe_CustomCheckoutAPIVersion = "2025-03-31.basil; custom_checkout_beta=v1; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"

var stripe_stripeJSBuildMarker = regexp.MustCompile(`/\*!\s*STRIPE_JS_BUILD_SALT\s+([a-f0-9]{10})\s*\*/`)
var stripe_stripeJSVersionLiteral = regexp.MustCompile(`^\s*(?:"([a-f0-9]{10})"|'([a-f0-9]{10})')`)

// ParseStripeJSVersion 只提取 payment_user_agent 使用的短构建标识。
// 注释中的 salt 与相邻版本字符串独立；分别检查一致性，不要求两者相等。
// 同名注释也修饰 40/64 位常量，不能把这些值或 Hosted 校验值混作版本。
func stripe_ParseStripeJSVersion(source string) (string, error) {
	markers := stripe_stripeJSBuildMarker.FindAllStringSubmatchIndex(source, -1)
	version := ""
	salt := ""
	for _, marker := range markers {
		value := source[marker[2]:marker[3]]
		if salt != "" && salt != value {
			return "", protocol_NewError("stripe_build_missing")
		}
		salt = value
		literal := stripe_stripeJSVersionLiteral.FindStringSubmatch(source[marker[1]:])
		if literal != nil {
			candidate := protocol_AsString(protocol_FirstTruthy(literal[1], literal[2]))
			if version != "" && candidate != version {
				return "", protocol_NewError("stripe_build_missing")
			}
			version = candidate
		}
	}
	if !stripe_validStripeJSVersion(version) {
		return "", protocol_NewError("stripe_build_missing")
	}
	return version, nil
}

func stripe_validStripeJSVersion(version string) bool {
	return version != "0000000000" && protocol_Matches(`[a-f0-9]{10}`, version)
}

func (s *stripe_Stripe) initializeBuild() error {
	response, err := s.HTTP.Request("GET", stripe_CustomJSURL, protocol_RequestOptions{Headers: s.Profile.Headers()})
	if err != nil {
		return err
	}
	if response == nil || response.StatusCode != 200 {
		return protocol_NewError("stripe_build_missing")
	}
	source := string(response.Content)
	version, err := stripe_ParseStripeJSVersion(source)
	if err != nil {
		return err
	}
	s.CP.StripeVersion = stripe_CustomAPIVersion
	s.JSVersion = version
	// Custom cs_live confirm 需要同一份 Basil 脚本里的 rv/sv；解析失败不阻断 oaics_。
	if build, buildErr := stripe_ParseStripeBuildConstants(source); buildErr == nil {
		s.Build = build
	}
	return nil
}

// ---- internal/stripe/client.go ----

const stripe_StripeBase = "https://api.stripe.com/v1"

var stripe_stripePlugins = []string{"PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer", "Microsoft Edge PDF Viewer", "WebKit built-in PDF"}

type stripe_Stripe struct {
	Profile                                                                         protocol_BrowserProfile
	HTTP                                                                            protocol_HTTPTransport
	CP                                                                              *model_Checkpoint
	Clock                                                                           fingerprint_Clock
	Started                                                                         time.Time
	ClientID, ElementsID, ConfigID, JSVersion, HostedURL, RadarSessionID, hostedEid string
	Build                                                                           map[string]string
	hostedSnapshot                                                                  []protocol_Map
	extractedHostedInit                                                             protocol_Map
	portal                                                                          stripe_portalSession
	confirmLock                                                                     sync.Mutex
	confirmSubmitted                                                                bool
	observer                                                                        *observability_Observer
}

type stripe_StripeCallOptions struct {
	Form       any
	Hosted     bool
	Headers    protocol_Fields
	AllowError bool
}

func stripe_NewStripe(transport protocol_HTTPTransport, cp *model_Checkpoint, clock fingerprint_Clock) *stripe_Stripe {
	if cp.StripeIdentity == nil {
		cp.StripeIdentity = map[string]string{}
	}
	return &stripe_Stripe{Profile: protocol_DefaultBrowserProfile(), HTTP: transport, CP: cp, Clock: clock, Started: time.Now(), ClientID: protocol_RandomUUID(), ElementsID: "elements_session_" + protocol_RandomAlphabet(11, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), ConfigID: protocol_RandomUUID(), Build: map[string]string{}}
}

func (s *stripe_Stripe) BindObserver(observer *observability_Observer) { s.observer = observer }

func (s *stripe_Stripe) Headers(hostedOption ...bool) protocol_Fields {
	hosted := len(hostedOption) > 0 && hostedOption[0]
	origin := "https://js.stripe.com"
	referer := origin + "/"
	if hosted {
		parsed, _ := url.Parse(s.HostedURL)
		if parsed != nil {
			origin = "https://" + parsed.Host
		}
		referer = strings.Split(s.HostedURL, "#")[0]
	}
	h := s.Profile.Headers()
	h.Update(protocol_O("Accept", "application/json", "Content-Type", "application/x-www-form-urlencoded", "Origin", origin, "Referer", referer))
	identity := s.CP.StripeIdentity
	if !hosted || origin == "https://checkout.stripe.com" {
		parts := []string{}
		for _, pair := range [][2]string{{"__stripe_mid", protocol_AsString(protocol_FirstTruthy(identity["mid"], identity["muid"]))}, {"__stripe_sid", protocol_AsString(protocol_FirstTruthy(identity["sid_cookie"], identity["sid"]))}} {
			if pair[1] != "" {
				parts = append(parts, pair[0]+"="+pair[1])
			}
		}
		if len(parts) > 0 {
			h.Set("Cookie", strings.Join(parts, "; "))
		}
	}
	if hosted {
		site := "cross-site"
		if origin == "https://checkout.stripe.com" {
			site = "same-site"
		}
		h.Update(protocol_O("sec-fetch-dest", "empty", "sec-fetch-mode", "cors", "sec-fetch-site", site))
	}
	return h
}

func (s *stripe_Stripe) Call(method, path string, options ...stripe_StripeCallOptions) (protocol_Map, error) {
	opts := stripe_StripeCallOptions{}
	if len(options) > 0 {
		opts = options[0]
	}
	h := opts.Headers
	if len(h) == 0 {
		h = s.Headers(opts.Hosted)
	}
	response, err := s.HTTP.Request(method, stripe_StripeBase+path, protocol_RequestOptions{Headers: h, Form: opts.Form})
	if err != nil {
		return nil, err
	}
	data, err := protocol_JSONResponse(response, opts.AllowError)
	if err != nil {
		return nil, err
	}
	providerError := protocol_AsMap(protocol_FirstTruthy(data["error"], data["last_payment_error"]))
	if len(providerError) > 0 && s.observer != nil {
		// 诊断失败不得改变真实付款结果或重置防重保护。
		func() {
			defer func() { _ = recover() }()
			s.observer.Emit("payment_error", protocol_Map{"service": "stripe", "method": method, "path": stripe_StripeBase + path, "outcome": "failed", "provider_code": providerError["code"], "decline_code": providerError["decline_code"], "request_id": response.Headers.Get("Request-Id")})
		}()
	}
	return data, nil
}

func (s *stripe_Stripe) Initialize() error {
	if err := s.initializeBuild(); err != nil {
		return err
	}
	return s.InitializeIdentity()
}

// InitializeIdentity 只建立 guid/muid/sid。Hosted 现成长链不加载 Custom basil 脚本。
//
// 身份缺失时连续发两次设备注册，两次均携带本实现的 MTree 特征与上下文。
// 已有完整身份则复用；第一次失败不阻断身份建立，身份只从第二次响应读取。
func (s *stripe_Stripe) InitializeIdentity() error {
	identity := s.CP.StripeIdentity
	if identity["guid"] != "" && identity["muid"] != "" && identity["sid"] != "" {
		return nil
	}
	_, _ = s.postStripeJSInit()
	response, err := s.postStripeJSInit()
	if err != nil {
		return err
	}
	data, err := protocol_JSONResponse(response, false)
	if err != nil {
		return err
	}
	cookies := response.Cookies
	identity = map[string]string{}
	for _, key := range []string{"guid", "muid", "sid"} {
		identity[key] = protocol_AsString(protocol_FirstTruthy(data[key], ""))
	}
	identity["mid"] = protocol_AsString(protocol_FirstTruthy(cookies["__stripe_mid"], identity["muid"]))
	identity["sid_cookie"] = protocol_AsString(protocol_FirstTruthy(cookies["__stripe_sid"], identity["sid"]))
	identity["muid"] = protocol_AsString(protocol_FirstTruthy(identity["muid"], identity["mid"]))
	identity["sid"] = protocol_AsString(protocol_FirstTruthy(identity["sid"], identity["sid_cookie"]))
	identity["guid"] = protocol_AsString(protocol_FirstTruthy(identity["guid"], cookies["m"], identity["muid"]))
	if identity["guid"] == "" || identity["muid"] == "" || identity["sid"] == "" {
		return &protocol_ProtocolError{Code: "stripe_identity_missing"}
	}
	s.CP.StripeIdentity = identity
	return nil
}

// stripeJSInitID 是 Stripe.js 的 stripe_js_id，浏览器发送的是原始 UUIDv4 文本。
// 它与 client_attribution_metadata.client_session_id 同源，不能改成去连字符的 32 位 hex，
// 否则同一会话在 m/6 注册与业务请求里会呈现两个不同的设备标识。
func (s *stripe_Stripe) stripeJSInitID() string {
	return s.ClientID
}

// deviceRegistration 构造 m/6 的请求体。
// 结构与编码、以及请求头都对齐真实浏览器抓包（见 mtree.go 的说明）：
// 顶层 a 承载特征对象、顶层 b 承载上下文对象，整个载荷 JSON → encodeURIComponent → base64。
// tag 使用官方脚本里的字面量模板（真实抓包中四个请求都发该字符串）。
// id 仍沿用本笔页面会话标识；官方使用的 md5(特征值拼接) 语义尚未启用，见文档说明。
func (s *stripe_Stripe) deviceRegistration(timings *stripe_mtreeTimings) (string, error) {
	// 总耗时必须在特征采集完成后读取，避免遗漏采集过程。
	features := s.deviceFeature(timings)
	payload := protocol_O(
		"v2", "1",
		"id", s.stripeJSInitID(),
		"t", timings.total(),
		"tag", stripe_mtreeTagTemplate,
		"src", "js",
		"a", features,
		"b", s.deviceContext(),
		"h", stripe_randomNonce(20),
	)
	return stripe_mtreeEncode(payload)
}

// postStripeJSInit 发送一次 m/6 设备注册。
// 载荷里的 tag 是官方脚本未替换的构建模板字面量，不随调用点变化。
func (s *stripe_Stripe) postStripeJSInit() (*protocol_HTTPResponse, error) {
	body, err := s.deviceRegistration(stripe_newMtreeTimings(nil))
	if err != nil {
		return nil, err
	}
	return s.HTTP.Request("POST", "https://m.stripe.com/6", protocol_RequestOptions{Headers: s.mtreeHeaders(), Text: &body})
}

func (s *stripe_Stripe) IDs() protocol_Fields {
	return protocol_O("guid", s.CP.StripeIdentity["guid"], "muid", s.CP.StripeIdentity["muid"], "sid", s.CP.StripeIdentity["sid"])
}

// TimeOnPage 返回页面停留时间（毫秒），以 Hosted 页面加载完成的时刻为起点。
// 真实用户在支付页停留通常为 8~30 秒；不足 8 秒时按 8 秒上报，避免所有交易呈现同一个固定值。
func (s *stripe_Stripe) TimeOnPage() string {
	elapsed := time.Since(s.Started).Milliseconds()
	if elapsed < 8000 {
		elapsed = 8000
	}
	return strconv.FormatInt(elapsed, 10)
}

func stripe_cardFields(card model_Card) protocol_Fields {
	return protocol_O("number", card.Number, "cvc", card.CVC, "exp_month", card.ExpMonth, "exp_year", card.ExpYear)
}

func (s *stripe_Stripe) Token(card model_Card, billing model_Billing, checkout protocol_Map) (string, error) {
	if !stripe_validStripeJSVersion(s.JSVersion) {
		return "", protocol_NewError("stripe_build_missing")
	}
	m := protocol_O("client_session_id", s.ClientID, "merchant_integration_source", "elements", "merchant_integration_subtype", "payment-element", "merchant_integration_version", "2021", "payment_intent_creation_flow", "deferred", "payment_method_selection_flow", "merchant_specified", "elements_session_id", s.ElementsID, "elements_session_config_id", s.ConfigID, "merchant_integration_additional_elements", []string{"expressCheckout", "payment", "address"})
	details := protocol_O("name", billing.Name, "phone", "", "address", billing.Address())
	pm := protocol_O("type", "card", "card", stripe_cardFields(card), "allow_redisplay", "limited", "billing_details", details, "payment_user_agent", "stripe.js/"+s.JSVersion+"; stripe-js-v3/"+s.JSVersion+"; payment-element; deferred-intent", "referrer", "https://chatgpt.com", "time_on_page", s.TimeOnPage())
	pm.Update(s.IDs())
	if s.observer != nil {
		outcome := "missing"
		if len(s.radarOptions()) > 0 {
			outcome = "attached"
		}
		s.observer.Emit("radar_attachment", protocol_Map{"outcome": outcome})
	}
	pm.Set("client_attribution_metadata", m)
	if opts := s.radarOptions(); opts != nil {
		pm.Set("radar_options", opts)
	}
	form := protocol_O("payment_method_data", pm, "client_attribution_metadata", m, "setup_future_usage", "off_session", "set_as_default_payment_method", "false", "key", s.CP.PublishableKey, "_stripe_version", s.CP.StripeVersion)
	context := protocol_Fields{}
	if protocol_Truthy(checkout["customer_session_client_secret"]) {
		context = protocol_O("currency", strings.ToLower(s.CP.Currency), "mode", "subscription", "payment_method_types", []string{"card", "link"})
	}
	for _, key := range []string{"stripe_customer_id", "customer_id", "customer"} {
		if strings.HasPrefix(protocol_AsString(checkout[key]), "cus_") {
			context.Set("customer", checkout[key])
			break
		}
	}
	if len(context) > 0 {
		form.Set("client_context", context)
	}
	data, err := s.Call("POST", "/confirmation_tokens", stripe_StripeCallOptions{Form: form})
	if err != nil {
		return "", err
	}
	id := protocol_AsString(data["id"])
	if !strings.HasPrefix(id, "ctoken_") {
		return "", &protocol_ProtocolError{Code: "confirmation_token_missing"}
	}
	return id, nil
}

// ConfirmForm 的 client_session_id 与 m/6 的 stripe_js_id 同源。
// 两者是同一个页面会话标识：浏览器不会在设备注册与付款确认之间换掉它，
// 之前这里每次调用都新建随机 UUID，会让同一笔交易出现两个设备会话标识。
func (s *stripe_Stripe) ConfirmForm() protocol_Fields {
	cp := s.CP
	return protocol_O("confirmation_token", cp.ConfirmationToken, "client_secret", cp.ClientSecret, "key", cp.PublishableKey, "return_url", cp.ReturnURL, "_stripe_version", cp.StripeVersion, "client_attribution_metadata", protocol_O("client_session_id", s.stripeJSInitID(), "merchant_integration_source", "l1"))
}

func (s *stripe_Stripe) beginPaymentSubmit() error {
	// 请求结果未知仍算已提交；幂等键不授权重新尝试付款。
	s.confirmLock.Lock()
	defer s.confirmLock.Unlock()
	if s.confirmSubmitted {
		return &protocol_ProtocolError{Code: "payment_confirmation_already_submitted"}
	}
	s.confirmSubmitted = true
	return nil
}

func (s *stripe_Stripe) Confirm() (protocol_Map, error) {
	if err := s.beginPaymentSubmit(); err != nil {
		return nil, err
	}
	h := s.Headers()
	h.Set("Idempotency-Key", "pi-confirm-"+s.CP.PaymentIntentID)
	return s.Call("POST", "/payment_intents/"+s.CP.PaymentIntentID+"/confirm", stripe_StripeCallOptions{Form: s.CP.ConfirmBody, Headers: h, AllowError: true})
}

func (s *stripe_Stripe) ConfirmCheckoutSession() (protocol_Map, error) {
	if err := s.beginPaymentSubmit(); err != nil {
		return nil, err
	}
	h := s.Headers()
	h.Set("Idempotency-Key", "checkout-confirm-"+s.CP.CheckoutID)
	return s.Call("POST", "/payment_pages/"+s.CP.CheckoutID+"/confirm", stripe_StripeCallOptions{Form: s.CP.ConfirmBody, Headers: h, AllowError: true})
}

func (s *stripe_Stripe) Retrieve() (protocol_Map, error) {
	cp := s.CP
	if cp.PaymentIntentID != "" && cp.ClientSecret != "" {
		return s.Call("GET", "/payment_intents/"+cp.PaymentIntentID+"?client_secret="+url.QueryEscape(cp.ClientSecret)+"&key="+url.QueryEscape(cp.PublishableKey)+"&_stripe_version="+url.QueryEscape(cp.StripeVersion), stripe_StripeCallOptions{AllowError: true})
	}
	if strings.HasPrefix(cp.CheckoutID, "cs_live_") && protocol_AsString(cp.Metadata["checkout_ui_mode"]) == "custom" {
		return s.CustomPage()
	}
	return s.Page()
}

func (s *stripe_Stripe) Page() (protocol_Map, error) {
	return s.Call("GET", s.hostedPagePath(), stripe_StripeCallOptions{Hosted: true, AllowError: true})
}

func (s *stripe_Stripe) CustomPage() (protocol_Map, error) {
	return s.Call("GET", "/payment_pages/"+s.CP.CheckoutID+"?key="+url.QueryEscape(s.CP.PublishableKey)+"&_stripe_version="+url.QueryEscape(stripe_CustomCheckoutAPIVersion)+"&expand%5B%5D=payment_intent", stripe_StripeCallOptions{AllowError: true})
}

// ---- internal/stripe/encoding.go ----

// EncodeStripeBlob 保留 Python 的 ASCII JSON、1..3 字节填充及后续逐字节变换。
func stripe_EncodeStripeBlob(value any) (string, error) {
	plain, err := protocol_CompactJSON(value)
	if err != nil {
		return "", err
	}
	padded := []byte(plain + strings.Repeat(" ", 3-len(plain)%3))
	for i := range padded {
		padded[i] ^= 5
	}
	encoded := []byte(url.QueryEscape(base64.StdEncoding.EncodeToString(padded)))
	for i, c := range encoded {
		encoded[i] = (c-32+11)%95 + 32
	}
	return string(encoded), nil
}

func stripe_ComputeIDChecksum(id string) (string, error) {
	value := strings.TrimSpace(id)
	if value == "" {
		return "", &protocol_ProtocolError{Code: "checkout_id_invalid"}
	}
	return stripe_EncodeStripeBlob(protocol_O("id", value))
}

func stripe_ComputeJSChecksum(paymentMethodID string) (string, error) {
	pm := strings.TrimSpace(paymentMethodID)
	if !strings.HasPrefix(pm, "pm_") {
		return "", &protocol_ProtocolError{Code: "invalid_payment_method"}
	}
	return stripe_ComputeIDChecksum(pm)
}

func stripe_ComputeRVTimestamp(rvTS, rv, sv string) (string, error) {
	if rvTS == "" || rv == "" || sv == "" {
		return "", &protocol_ProtocolError{Code: "stripe_build_missing"}
	}
	return stripe_EncodeStripeBlob(protocol_O("rvTs", rvTS, "rv", rv, "sv", sv))
}

func stripe_ParseStripeBuildConstants(source string) (map[string]string, error) {
	find := func(pattern string) string {
		match := regexp.MustCompile(pattern).FindStringSubmatch(source)
		if len(match) > 1 {
			return match[1]
		}
		return ""
	}
	pair := regexp.MustCompile(`STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{10})\"\s*,\s*o\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{40})\"`).FindStringSubmatch(source)
	version, rv := "", ""
	if len(pair) > 2 {
		version, rv = pair[1], pair[2]
	} else {
		version = find(`STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{10})\"`)
		rv = find(`STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{40})\"`)
	}
	if version == "" || rv == "" {
		return nil, &protocol_ProtocolError{Code: "stripe_build_missing"}
	}
	rvTS := find(`rvTs["']?\s*:\s*["']([^"']+)["']`)
	if rvTS == "" {
		rvTS = find(`"(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"`)
	}
	if rvTS == "" {
		rvTS = find(`r="(\d{4}-\d{2}-\d{2} 00:00:00 -0000)"`)
	}
	sv := find(`sv:\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{64})\"`)
	if sv == "" {
		sv = find(regexp.QuoteMeta(rv) + `"\s*,\s*a\s*=\s*/\*!?\s*STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{64})\"`)
	}
	if sv == "" {
		sv = find(`STRIPE_JS_BUILD_SALT[^\"]*\"([a-f0-9]{64})\"`)
	}
	if rvTS == "" || sv == "" {
		return nil, &protocol_ProtocolError{Code: "stripe_build_missing"}
	}
	return map[string]string{"version": version, "rv": rv, "sv": sv, "rv_ts": rvTS}, nil
}

// ---- internal/stripe/hosted.go ----

// LoadHostedPublishableKey 现成长链付款不再访问 ChatGPT bootstrap。
// 优先解码 hosted fragment；合成测试链没有有效 fragment 时再读页面字段。
func (s *stripe_Stripe) LoadHostedPublishableKey(raw string) error {
	if strings.HasPrefix(s.CP.PublishableKey, "pk_") {
		return nil
	}
	if err := s.ValidateHostedURL(raw); err != nil {
		return err
	}
	s.HostedURL = raw
	if key := stripe_ExtractHostedPublishableKeyFromURL(raw); protocol_Matches(`pk_[A-Za-z0-9_]+`, key) {
		s.CP.PublishableKey = key
		return nil
	}
	response, err := s.HTTP.Request("GET", strings.Split(raw, "#")[0], protocol_RequestOptions{Headers: s.Profile.Headers()})
	if err != nil {
		return err
	}
	if response.StatusCode != 200 {
		return &protocol_ProtocolError{Code: "hosted_page_unavailable"}
	}
	key := stripe_ExtractHostedPublishableKey(string(response.Content))
	if !protocol_Matches(`pk_[A-Za-z0-9_]+`, key) {
		return protocol_NewError("publishable_key_missing")
	}
	s.CP.PublishableKey = key
	return nil
}

func (s *stripe_Stripe) HostedCheckoutURL(raw string) (string, error) {
	if raw != "" {
		if err := s.ValidateHostedURL(raw); err != nil {
			return "", err
		}
		return raw, nil
	}
	_, err := s.initializeHosted("")
	if err != nil {
		return "", err
	}
	return s.HostedURL, nil
}

// ExtractHostedURL 只从创建响应或 Stripe init 读取完整长链。
// 路径-only URL 仅作 init 的 Origin/Referer 上下文，不能拼 fragment，也不能当作结果返回。
func (s *stripe_Stripe) ExtractHostedURL(checkout protocol_Map) (string, error) {
	if raw := stripe_FindHostedURL(checkout, s.CP.CheckoutID); raw != "" {
		s.HostedURL = raw
		return raw, nil
	}
	if !strings.HasPrefix(s.CP.CheckoutID, "cs_live_") {
		return "", protocol_NewError("checkout_id_invalid")
	}
	if !strings.HasPrefix(s.CP.PublishableKey, "pk_") {
		return "", protocol_NewError("publishable_key_missing")
	}
	s.HostedURL = "https://checkout.stripe.com/c/pay/" + s.CP.CheckoutID
	if err := s.ValidateHostedURL(s.HostedURL, false); err != nil {
		return "", err
	}
	init, err := s.Call("POST", "/payment_pages/"+s.CP.CheckoutID+"/init", stripe_StripeCallOptions{Hosted: true, Form: s.hostedInitForm()})
	if err != nil {
		return "", err
	}
	s.rememberHostedEid(init)
	raw := stripe_FindHostedURL(init, s.CP.CheckoutID)
	if raw == "" {
		return "", protocol_NewError("hosted_url_missing")
	}
	s.HostedURL = raw
	s.extractedHostedInit = init
	return raw, nil
}

// ExtractedHostedSnapshot 复用本次提链的 init 响应，不跨交易缓存价格。
func (s *stripe_Stripe) ExtractedHostedSnapshot() protocol_Map {
	return s.extractedHostedInit
}

func (s *stripe_Stripe) initializeHosted(raw string) (protocol_Map, error) {
	s.HostedURL = raw
	if raw == "" {
		s.HostedURL = "https://checkout.stripe.com/c/pay/" + s.CP.CheckoutID
	}
	if err := s.ValidateHostedURL(s.HostedURL, raw != ""); err != nil {
		return nil, err
	}
	init, err := s.Call("POST", "/payment_pages/"+s.CP.CheckoutID+"/init", stripe_StripeCallOptions{Hosted: true, Form: s.hostedInitForm()})
	if err != nil {
		return nil, err
	}
	s.rememberHostedEid(init)
	if raw == "" {
		s.HostedURL = stripe_FindHostedURL(init, s.CP.CheckoutID)
		if s.HostedURL == "" {
			return nil, &protocol_ProtocolError{Code: "hosted_url_missing"}
		}
		if err := s.ValidateHostedURL(s.HostedURL); err != nil {
			return nil, err
		}
	}
	s.hostedSnapshot = []protocol_Map{init}
	return init, nil
}

func (s *stripe_Stripe) HostedPriceSnapshots(raw string) ([]protocol_Map, error) {
	if s.hostedSnapshot == nil {
		if _, err := s.initializeHosted(raw); err != nil {
			return nil, err
		}
	}
	return s.hostedSnapshot, nil
}

func (s *stripe_Stripe) CustomCheckoutForm(init protocol_Map, paymentMethod string) (protocol_Fields, error) {
	if !stripe_validStripeJSVersion(s.JSVersion) {
		return nil, protocol_NewError("stripe_build_missing")
	}
	cp := s.CP
	if !strings.HasPrefix(cp.CheckoutID, "cs_live_") {
		return nil, protocol_NewError("checkout_id_invalid")
	}
	if paymentMethod == "" && cp.ConfirmationToken == "" {
		return nil, protocol_NewError("confirmation_token_missing")
	}
	if cp.ReturnURL == "" {
		return nil, protocol_NewError("invalid_return_url")
	}
	checksum := protocol_AsString(init["init_checksum"])
	if checksum == "" {
		return nil, protocol_NewError("init_checksum_missing")
	}
	var expected any
	if due := stripe_HostedAmount(init); due != nil {
		expected = *due
	} else if cp.Amount != nil {
		expected = *cp.Amount
	}
	selectionFlow := "merchant_specified"
	if init["automatic_payment_method_types"] == true {
		selectionFlow = "automatic"
	}
	attribution := protocol_O("client_session_id", s.ClientID, "checkout_session_id", cp.CheckoutID, "merchant_integration_source", "checkout", "merchant_integration_version", "custom", "merchant_integration_subtype", "payment-element", "payment_intent_creation_flow", "deferred", "payment_method_selection_flow", selectionFlow, "elements_session_id", s.ElementsID)
	if configID := protocol_AsString(init["config_id"]); configID != "" {
		attribution.Set("checkout_config_id", configID)
	}
	if s.ConfigID != "" {
		attribution.Set("elements_session_config_id", s.ConfigID)
	}
	sessionClient := protocol_O("elements_init_source", "custom_checkout", "referrer_host", "chatgpt.com", "stripe_js_id", s.ClientID, "locale", "en-US", "is_aggregation_expected", "false", "session_id", s.ElementsID)
	if betas := stripe_customClientBetas(init); len(betas) > 0 {
		sessionClient.Set("client_betas", betas)
	}
	form := protocol_O("expected_payment_method_type", "card", "expected_amount", expected, "key", cp.PublishableKey, "_stripe_version", stripe_checkoutPageVersion(init, cp.StripeVersion), "return_url", cp.ReturnURL, "init_checksum", checksum, "version", s.JSVersion, "elements_session_client", sessionClient, "elements_options_client", protocol_O("saved_payment_method", protocol_O("enable_save", "auto", "enable_redisplay", "auto")), "client_attribution_metadata", attribution)
	if stripe_consentRequired(init) {
		form.Set("consent", protocol_O("terms_of_service", "accepted"))
	}
	form.Update(s.IDs())
	// Payment Page confirm 不接受顶层 radar_options；会话信息已由
	// Token 的 payment_method_data 携带，不能重复放入此接口。
	checksumID := protocol_AsString(init["id"])
	if paymentMethod != "" {
		form.Set("payment_method", paymentMethod)
		checksumID = paymentMethod
	} else {
		form.Set("confirmation_token", cp.ConfirmationToken)
		if !strings.HasPrefix(checksumID, "ppage_") {
			return nil, protocol_NewError("payment_page_id_missing")
		}
	}
	jsChecksum, err := stripe_ComputeIDChecksum(checksumID)
	if err != nil {
		return nil, err
	}
	rvTS, err := stripe_ComputeRVTimestamp(s.Build["rv_ts"], s.Build["rv"], s.Build["sv"])
	if err != nil {
		return nil, err
	}
	form.Set("js_checksum", jsChecksum)
	form.Set("rv_timestamp", rvTS)
	return form, nil
}

func (s *stripe_Stripe) CustomPriceSnapshot(checkout protocol_Map) (protocol_Map, error) {
	key, ok := protocol_FirstTruthy(checkout["publishable_key"], s.CP.PublishableKey).(string)
	if !ok || !strings.HasPrefix(key, "pk_") {
		return nil, &protocol_ProtocolError{Code: "publishable_key_missing"}
	}
	// 分流只看创建响应的 checkout_session_id / checkout_ui_mode。
	// payment_pages/init 的 session_id 经常是 Elements 会话，不是 cs_live_；
	// create 的 client_secret 也不是这条 Custom 路径的入参。
	return s.Call("POST", "/payment_pages/"+s.CP.CheckoutID+"/init", stripe_StripeCallOptions{Form: protocol_O("key", key, "_stripe_version", s.CP.StripeVersion, "browser_locale", "en-US", "browser_timezone", s.CP.Timezone, "elements_session_client", protocol_O("stripe_js_id", s.ClientID, "elements_init_source", "custom_checkout", "referrer_host", "chatgpt.com", "locale", "en-US"), "elements_options_client", protocol_O("saved_payment_method", protocol_O("enable_save", "auto", "enable_redisplay", "auto")))})
}

func (s *stripe_Stripe) BindCheckoutElements(_ protocol_Map) error {
	query := url.Values{}
	query.Set("key", s.CP.PublishableKey)
	query.Set("_stripe_version", s.CP.StripeVersion)
	// Basil 只接受 payment_intent / setup_intent / deferred_intent。
	// Checkout Session secret 不是 PI/SI：Stripe.js 此时 type=deferred_intent，
	// 且不把 cs_…_secret_ 放进 client_secret，改为带 deferred_intent 明细。
	query.Set("type", "deferred_intent")
	query.Set("checkout_session_id", s.CP.CheckoutID)
	query.Set("elements_init_source", "custom_checkout")
	query.Set("referrer_host", "chatgpt.com")
	query.Set("locale", "en-US")
	query.Set("deferred_intent[mode]", "subscription")
	query.Set("deferred_intent[setup_future_usage]", "off_session")
	query.Set("deferred_intent[payment_method_types][0]", "card")
	query.Set("deferred_intent[payment_method_types][1]", "link")
	if currency := strings.ToLower(s.CP.Currency); currency != "" {
		query.Set("currency", currency)
		query.Set("deferred_intent[currency]", currency)
	}
	if s.CP.Amount != nil {
		query.Set("deferred_intent[amount]", strconv.FormatInt(*s.CP.Amount, 10))
	}
	data, err := s.Call("GET", "/elements/sessions?"+query.Encode(), stripe_StripeCallOptions{})
	if err != nil {
		return err
	}
	id := protocol_AsString(protocol_FirstTruthy(data["elements_session_id"], protocol_AsMap(data["elements_session"])["id"], data["id"]))
	if strings.HasPrefix(id, "elements_session_") {
		s.ElementsID = id
	}
	if configID := protocol_AsString(protocol_FirstTruthy(data["config_id"], protocol_AsMap(data["elements_session"])["config_id"])); configID != "" {
		s.ConfigID = configID
	}
	return nil
}

func (s *stripe_Stripe) UpdateCustomCheckout(billing model_Billing, init protocol_Map) (protocol_Map, error) {
	if !strings.HasPrefix(s.CP.CheckoutID, "cs_live_") {
		return nil, protocol_NewError("checkout_id_invalid")
	}
	// Custom 税区更新与 Hosted 相同：只发 eid/tax_region。guid/muid/sid 会触发
	// parameter_unknown（Did you mean eid?）。响应 id/session_id 可能是 Elements
	// 会话，不拿来否定创建响应里已经确认的 Checkout ID。
	form := protocol_O("eid", protocol_FirstTruthy(init["eid"], "NA"), "tax_region", billing.Address(), "key", s.CP.PublishableKey, "_stripe_version", stripe_checkoutPageVersion(init, s.CP.StripeVersion))
	return s.Call("POST", "/payment_pages/"+s.CP.CheckoutID, stripe_StripeCallOptions{Form: form})
}

func (s *stripe_Stripe) HostedPrepare(raw string, billing model_Billing) (protocol_Map, error) {
	init, err := s.initializeHosted(raw)
	if err != nil {
		return nil, err
	}
	response, err := s.HTTP.Request("GET", strings.Split(s.HostedURL, "#")[0], protocol_RequestOptions{Headers: s.Profile.Headers()})
	if err != nil {
		return nil, err
	}
	if response.StatusCode != 200 {
		return nil, &protocol_ProtocolError{Code: "hosted_page_unavailable"}
	}
	s.Started = time.Now()
	if err = s.loadHostedScripts(string(response.Content)); err != nil {
		return nil, err
	}
	s.hostedClientFingerprintWarmup()
	tax, err := s.Call("POST", "/payment_pages/"+s.CP.CheckoutID, stripe_StripeCallOptions{Hosted: true, Form: s.hostedTaxForm(protocol_AsString(init["eid"]), billing)})
	if err != nil {
		return nil, err
	}
	s.rememberHostedEid(tax)
	page, err := s.Page()
	if err != nil {
		return nil, err
	}
	s.hostedSnapshot = []protocol_Map{init, tax, page}
	checksum := protocol_AsString(init["init_checksum"])
	var scan func(any)
	scan = func(node any) {
		switch value := node.(type) {
		case protocol_Map:
			for _, key := range protocol_SortedKeys(value) {
				v := value[key]
				if text, ok := v.(string); strings.Contains(strings.ToLower(key), "checksum") && ok && text != "" {
					if len(text) > len(checksum) {
						checksum = text
					}
				} else {
					scan(v)
				}
			}
		case []any:
			for _, v := range value {
				scan(v)
			}
		}
	}
	scan(init)
	taxChecksum := protocol_AsString(tax["init_checksum"])
	if len(taxChecksum) > len(checksum) && len(taxChecksum) > 40 {
		checksum = taxChecksum
	}
	if checksum == "" {
		return nil, &protocol_ProtocolError{Code: "init_checksum_missing"}
	}
	var total any
	for _, data := range []protocol_Map{tax, init, page} {
		if value := stripe_HostedAmount(data); value != nil {
			total = *value
			break
		}
	}
	group := stripe_firstLineItemGroup(tax, init, page)
	return protocol_Map{"amount": total, "currency": protocol_FirstTruthy(tax["currency"], init["currency"], page["currency"], s.CP.Currency), "checksum": checksum, "eid": protocol_FirstTruthy(tax["eid"], init["eid"], "NA"), "config_id": protocol_AsString(protocol_FirstTruthy(tax["config_id"], init["config_id"], page["config_id"], "")), "line_item_group": group}, nil
}

func stripe_firstLineItemGroup(nodes ...protocol_Map) protocol_Map {
	for _, node := range nodes {
		if group := protocol_AsMap(node["line_item_group"]); len(group) > 0 {
			return group
		}
	}
	return nil
}

func (s *stripe_Stripe) ValidateHostedURL(raw string, completeOption ...bool) error {
	complete := true
	if len(completeOption) > 0 {
		complete = completeOption[0]
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || (parsed.Host != "checkout.stripe.com" && parsed.Host != "pay.openai.com") || parsed.Path != "/c/pay/"+s.CP.CheckoutID || (complete && (len(parsed.Fragment) < 250 || parsed.RawQuery != "")) {
		return &protocol_ProtocolError{Code: "hosted_url_invalid"}
	}
	return nil
}

func (s *stripe_Stripe) HostedForm(card model_Card, billing model_Billing, info protocol_Map) (protocol_Fields, error) {
	cp := s.CP
	attribution := protocol_O("client_session_id", s.ClientID, "checkout_session_id", cp.CheckoutID, "merchant_integration_source", "checkout", "merchant_integration_version", "hosted_checkout", "payment_method_selection_flow", "automatic", "elements_session_id", s.ElementsID)
	if protocol_Truthy(info["config_id"]) {
		attribution.Set("checkout_config_id", info["config_id"])
	}
	form := protocol_O("type", "card", "card", stripe_cardFields(card), "billing_details", protocol_O("name", billing.Name, "email", protocol_FirstTruthy(billing.Email, cp.Email), "address", billing.Address()), "payment_user_agent", "stripe.js/"+s.Build["version"]+"; stripe-js-v3/"+s.Build["version"]+"; checkout", "referrer", strings.Split(s.HostedURL, "#")[0], "time_on_page", s.TimeOnPage(), "key", cp.PublishableKey, "_stripe_version", stripe_HostedVersion)
	form.Update(s.IDs())
	form.Set("client_attribution_metadata", attribution)
	s.lookupCardMetadata(card)
	data, err := s.Call("POST", "/payment_methods", stripe_StripeCallOptions{Hosted: true, Form: form})
	if err != nil {
		return nil, err
	}
	pm := protocol_AsString(data["id"])
	if !strings.HasPrefix(pm, "pm_") {
		return nil, &protocol_ProtocolError{Code: "payment_method_missing"}
	}
	jsChecksum, err := stripe_ComputeJSChecksum(pm)
	if err != nil {
		return nil, err
	}
	rvTS, err := stripe_ComputeRVTimestamp(s.Build["rv_ts"], s.Build["rv"], s.Build["sv"])
	if err != nil {
		return nil, err
	}
	var expected any
	if cp.Amount != nil {
		expected = *cp.Amount
	}
	// 缺少 passive_captcha_token：真实浏览器在这里会带上 Stripe 通过 hCaptcha 被动模式
	// 取得的 JWT（真实抓包字段名就是 passive_captcha_token）。协议客户端无法执行
	// hCaptcha 的信号采集，因此拿不到该 token，服务端会据此升级为交互式人机验证，
	// 由 payment-action 的浏览器交接处理。详见 docs/payment-testing.md 的人机验证一节。
	result := protocol_O("eid", protocol_FirstTruthy(info["eid"], s.hostedEid, "NA"), "payment_method", pm, "expected_amount", expected, "expected_payment_method_type", "card", "link_brand", "link", "key", cp.PublishableKey, "_stripe_version", stripe_HostedVersion, "js_checksum", jsChecksum, "rv_timestamp", rvTS)
	// 确认与创建付款方式沿用同一组设备会话标识，不在阶段切换时重新生成。
	result.Update(s.IDs())
	result.Set("client_attribution_metadata", attribution)
	if s.HostedURL != "" {
		result.Set("referrer", s.HostedURL)
	}
	if details := stripe_lastDisplayedLineItemGroup(stripe_firstNonEmptyMap(info, s.hostedSnapshot...)); details != nil {
		result.Set("last_displayed_line_item_group_details", details)
	}
	result.Update(protocol_O("return_url", strings.Split(s.HostedURL, "#")[0], "consent", protocol_O("terms_of_service", "accepted")))
	return result, nil
}

func stripe_firstNonEmptyMap(info protocol_Map, extra ...protocol_Map) protocol_Map {
	out := protocol_Map{}
	for k, v := range info {
		out[k] = v
	}
	if len(protocol_AsMap(out["line_item_group"])) > 0 {
		return out
	}
	for i := len(extra) - 1; i >= 0; i-- {
		if group := protocol_AsMap(extra[i]["line_item_group"]); len(group) > 0 {
			out["line_item_group"] = group
			return out
		}
	}
	return out
}

func stripe_checkoutPageVersion(init protocol_Map, fallback string) string {
	base := strings.TrimSpace(fallback)
	if base == "" {
		base = stripe_CustomAPIVersion
	}
	betas := strings.TrimSpace(protocol_AsString(init["beta_versions"]))
	if betas == "" {
		return stripe_CustomCheckoutAPIVersion
	}
	if strings.Contains(base, betas) {
		return base
	}
	return base + "; " + betas
}

func stripe_consentRequired(init protocol_Map) bool {
	consent := protocol_AsMap(init["consent_collection"])
	return protocol_AsString(consent["terms_of_service"]) == "required"
}

func stripe_MergeCustomPaymentPage(dst, src protocol_Map) {
	if dst == nil || src == nil {
		return
	}
	for _, key := range []string{"init_checksum", "eid", "config_id", "beta_versions", "automatic_payment_method_types", "consent_collection", "total_summary", "recurring_details", "invoice", "line_item_group"} {
		if src[key] != nil && src[key] != "" {
			dst[key] = src[key]
		}
	}
	if id := protocol_AsString(src["id"]); strings.HasPrefix(id, "ppage_") {
		dst["id"] = id
	}
}

func stripe_customClientBetas(init protocol_Map) []string {
	raw := protocol_AsString(init["beta_versions"])
	if raw == "" {
		raw = stripe_CustomCheckoutAPIVersion
	}
	var betas []string
	if strings.Contains(raw, "custom_checkout_beta=v1") {
		betas = append(betas, "custom_checkout_beta_6")
	}
	if strings.Contains(raw, "checkout_server_update_beta=v1") {
		betas = append(betas, "custom_checkout_server_updates_1")
	}
	if strings.Contains(raw, "checkout_manual_approval_preview=v1") {
		betas = append(betas, "custom_checkout_manual_approval_1")
	}
	return betas
}

// ---- internal/stripe/hosted_fingerprint.go ----

var stripe_hostedScriptURL = regexp.MustCompile(`https://js\.stripe\.com/v3/fingerprinted/js/(?:checkout-app(?:-init)?|stripe)-[A-Za-z0-9]+\.js[^"'\s]*`)
var stripe_hostedInitializedScriptURL = regexp.MustCompile(`https://js\.stripe\.com/v3/stripe\.js\?stripeCheckoutInitialized=true[^"'\s]*`)
var stripe_hostedOuterURL = regexp.MustCompile(`["']([^"']*m-outer-[A-Za-z0-9_-]+\.html[^"']*)["']`)

func (s *stripe_Stripe) hostedInitForm() protocol_Fields {
	form := protocol_O("browser_locale", "en-US", "browser_timezone", s.CP.Timezone, "key", s.CP.PublishableKey, "eid", "NA", "redirect_type", "url")
	if s.HostedURL != "" {
		// Stripe.js init 把当前 hosted 地址（含 fragment）放进 referrer；HTTP Referer 仍去 fragment。
		form.Set("referrer", s.HostedURL)
	}
	return form
}

func (s *stripe_Stripe) hostedTaxForm(eid string, billing model_Billing) protocol_Fields {
	// 实抓 tax 更新只有 eid/tax_region/key，不带 _stripe_version 和 mids。
	return protocol_O("eid", protocol_FirstTruthy(eid, "NA"), "tax_region", billing.Address(), "key", s.CP.PublishableKey)
}

func (s *stripe_Stripe) hostedPagePath() string {
	query := url.Values{}
	query.Set("eid", protocol_AsString(protocol_FirstTruthy(s.hostedEid, "NA")))
	query.Set("key", s.CP.PublishableKey)
	return "/payment_pages/" + s.CP.CheckoutID + "?" + query.Encode()
}

func (s *stripe_Stripe) rememberHostedEid(data protocol_Map) {
	if eid := protocol_AsString(data["eid"]); eid != "" {
		s.hostedEid = eid
	}
}

func (s *stripe_Stripe) loadHostedScripts(html string) error {
	var buildSource string
	scripts := append(stripe_hostedScriptURL.FindAllString(html, -1), stripe_hostedInitializedScriptURL.FindAllString(html, -1)...)
	for _, raw := range scripts {
		response, err := s.HTTP.Request("GET", raw, protocol_RequestOptions{Headers: s.Profile.Headers()})
		if err != nil {
			return err
		}
		if response.StatusCode != 200 {
			return &protocol_ProtocolError{Code: "stripe_build_missing"}
		}
		if strings.Contains(raw, "/stripe-") || strings.Contains(raw, "stripeCheckoutInitialized=true") {
			buildSource = string(response.Content)
		}
		if err := s.loadHostedOuter(response.Content); err != nil {
			return err
		}
	}
	if buildSource == "" {
		response, err := s.HTTP.Request("GET", "https://js.stripe.com/v3", protocol_RequestOptions{Headers: s.Profile.Headers()})
		if err != nil {
			return err
		}
		if response.StatusCode != 200 {
			return &protocol_ProtocolError{Code: "stripe_build_missing"}
		}
		buildSource = string(response.Content)
		if err := s.loadHostedOuter(response.Content); err != nil {
			return err
		}
	}
	build, err := stripe_ParseStripeBuildConstants(buildSource)
	if err != nil {
		return err
	}
	s.Build = build
	return nil
}

// hostedClientFingerprintWarmup 在 Hosted 页面脚本加载后建立设备身份，
// 并补发付款元素挂载遥测。身份建立失败不构成付款证据，也不中止准备。
//
// InitializeIdentity 在身份缺失时发送两次设备注册，已有完整身份时直接复用。
// 页面预热不额外补发 m/6 注册。
func (s *stripe_Stripe) hostedClientFingerprintWarmup() {
	_ = s.InitializeIdentity()
	payload, err := protocol_CompactJSON(protocol_O("type", "measurement", "event_name", "paymentElement.mount", "metrics", protocol_O("duration", s.TimeOnPage()), "client_session_id", s.ClientID, "publish_key", s.CP.PublishableKey))
	if err != nil {
		return
	}
	h := s.Profile.Headers()
	h.Update(protocol_O("Accept", "*/*", "Content-Type", "text/plain", "Origin", "https://js.stripe.com", "Referer", "https://js.stripe.com/"))
	_, _ = s.HTTP.Request("POST", "https://r.stripe.com/b", protocol_RequestOptions{Headers: h, Text: &payload})
}

func (s *stripe_Stripe) loadHostedOuter(source []byte) error {
	outer := stripe_hostedOuterURL.FindSubmatch(source)
	if len(outer) < 2 {
		return nil
	}
	base, _ := url.Parse("https://js.stripe.com/v3/")
	relative, err := url.Parse(string(outer[1]))
	if err != nil {
		return nil
	}
	outerURL := base.ResolveReference(relative)
	if outerURL.Host != "js.stripe.com" {
		return nil
	}
	_, err = s.HTTP.Request("GET", outerURL.String(), protocol_RequestOptions{Headers: s.Profile.Headers()})
	return err
}

func (s *stripe_Stripe) lookupCardMetadata(card model_Card) {
	if len(card.Number) < 6 || !protocol_Matches(`[0-9]{6}`, card.Number[:6]) {
		return
	}
	query := url.Values{}
	query.Set("bin_prefix", card.Number[:6])
	query.Set("key", s.CP.PublishableKey)
	_, _ = s.HTTP.Request("GET", "https://api.stripe.com/edge-internal/card-metadata?"+query.Encode(), protocol_RequestOptions{Headers: s.Headers(true)})
}

func stripe_lastDisplayedLineItemGroup(info protocol_Map) protocol_Fields {
	group := protocol_AsMap(info["line_item_group"])
	if len(group) == 0 {
		return nil
	}
	var inclusive, exclusive, discount int64
	for _, item := range stripe_asObjectList(group["tax_amounts"]) {
		amount, _ := protocol_Integer(item["amount"])
		if item["inclusive"] == true {
			inclusive += amount
		} else {
			exclusive += amount
		}
	}
	for _, item := range stripe_asObjectList(group["discount_amounts"]) {
		amount, _ := protocol_Integer(item["amount"])
		discount += amount
	}
	shipping, _ := protocol_Integer(protocol_AsMap(group["shipping_rate"])["amount"])
	subtotal, _ := protocol_Integer(protocol_FirstTruthy(group["subtotal"], int64(0)))
	return protocol_O("subtotal", subtotal, "total_exclusive_tax", exclusive, "total_inclusive_tax", inclusive, "total_discount_amount", discount, "shipping_rate_amount", shipping)
}

func stripe_asObjectList(raw any) []protocol_Map {
	switch value := raw.(type) {
	case []any:
		out := make([]protocol_Map, 0, len(value))
		for _, item := range value {
			if m := protocol_AsMap(item); len(m) > 0 {
				out = append(out, m)
			}
		}
		return out
	case []protocol_Map:
		return value
	default:
		return nil
	}
}

// ---- internal/stripe/hosted_parsing.go ----

var stripe_hostedPublishableKeyField = regexp.MustCompile(`"(?:apiKey|publishableKey|publishable_key)"\s*:\s*"(pk_(?:live|test)_[A-Za-z0-9]+)"`)

// HostedVersion 是 checkout.stripe.com 控制器的 apiVersion。
// 实抓 Hosted 页面为 Dahlia，不再使用 Basil + checkout_server_update_beta。
const stripe_HostedVersion = "2026-03-25.dahlia"

// ExtractHostedPublishableKeyFromURL 解码 Hosted fragment：URL unescape → Base64 → XOR 5 → JSON.apiKey。
// 这是 checkout 页面取 pk 的方式，不能抓 HTML 里 JS 包自带的其它 pk。
func stripe_ExtractHostedPublishableKeyFromURL(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	plain, err := stripe_decodeHostedCheckoutHash(parsed.Fragment)
	if err != nil {
		return ""
	}
	if key := protocol_AsString(protocol_FirstTruthy(plain["apiKey"], plain["publishableKey"], plain["publishable_key"])); protocol_Matches(`pk_[A-Za-z0-9_]+`, key) {
		return key
	}
	return ""
}

func stripe_decodeHostedCheckoutHash(fragment string) (protocol_Map, error) {
	raw := strings.TrimPrefix(fragment, "#")
	if raw == "" {
		return nil, protocol_NewError("hosted_url_invalid")
	}
	if unescaped, err := url.QueryUnescape(raw); err == nil {
		raw = unescaped
	}
	if pad := len(raw) % 4; pad != 0 {
		raw += strings.Repeat("=", 4-pad)
	}
	data, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		return nil, protocol_NewError("hosted_url_invalid")
	}
	for i := range data {
		data[i] ^= 5
	}
	var m protocol_Map
	if err := json.Unmarshal(stripe_bytesTrimSpace(data), &m); err != nil {
		if key := stripe_ExtractHostedPublishableKey(string(data)); key != "" {
			return protocol_Map{"apiKey": key}, nil
		}
		return nil, protocol_NewError("hosted_url_invalid")
	}
	return m, nil
}

func stripe_bytesTrimSpace(p []byte) []byte {
	return []byte(strings.TrimSpace(string(p)))
}

// ExtractHostedPublishableKey 只接受带字段名的 pk，避免误用 checkout JS 包里的其它密钥。
func stripe_ExtractHostedPublishableKey(html string) string {
	if m := stripe_hostedPublishableKeyField.FindStringSubmatch(html); len(m) > 1 {
		return m[1]
	}
	return ""
}

func stripe_HostedAmount(data protocol_Map) *int64 {
	for _, path := range [][]string{{"total_summary", "due"}, {"total_summary", "total"}, {"amount_due"}, {"invoice", "amount_due"}, {"total_summary", "amount_due"}, {"amount_total"}} {
		var value any = data
		for _, key := range path {
			value = protocol_AsMap(value)[key]
		}
		if number, ok := protocol_Integer(value); ok && number >= 0 {
			return &number
		}
	}
	return nil
}

func stripe_FindHostedURL(data any, checkoutID string) string {
	if value, ok := data.(string); ok {
		parsed, err := url.Parse(value)
		if err == nil && parsed.Scheme == "https" && parsed.User == nil && (parsed.Host == "pay.openai.com" || parsed.Host == "checkout.stripe.com") && parsed.Path == "/c/pay/"+checkoutID && len(parsed.Fragment) >= 250 && parsed.RawQuery == "" {
			return value
		}
	}
	switch value := data.(type) {
	case map[string]any:
		for _, key := range protocol_SortedKeys(value) {
			if found := stripe_FindHostedURL(value[key], checkoutID); found != "" {
				return found
			}
		}
	case []any:
		for _, item := range value {
			if found := stripe_FindHostedURL(item, checkoutID); found != "" {
				return found
			}
		}
	case protocol_Fields:
		for _, item := range value {
			if found := stripe_FindHostedURL(item.Value, checkoutID); found != "" {
				return found
			}
		}
	}
	return ""
}

// ---- internal/stripe/mtree.go ----

// mtreeURLSalt 取自官方脚本 https://m.stripe.network/out-4.5.45.js 的 URL_SALT 常量，
// 用于对标题做 sha256WithSalt。该值随脚本版本变化，更新脚本版本时需同步核对。
const stripe_mtreeURLSalt = "7766e861-8279-424d-87a1-07a6022fd8cd"

// mtreeOrigin 是 MTree 客户端的部署来源。MTree 脚本本身托管在 m.stripe.network，
// 因此它发出的 m/6 请求 Origin/Referer 都是该源，而不是 js.stripe.com；
// 服务端用 access-control-allow-origin: https://m.stripe.network 校验。
const stripe_mtreeOrigin = "https://m.stripe.network"

// mtreeTagTemplate 是 m/6 载荷里 tag 字段的字面量取值。
// 官方脚本未替换该构建模板变量，真实抓包中四个请求都发这个字符串。
const stripe_mtreeTagTemplate = "$npm_package_version"

// MTreeTagTemplate 暴露 m/6 载荷 tag 字段的字面量取值，供测试对照。
func stripe_MTreeTagTemplate() string { return stripe_mtreeTagTemplate }

// MTreeOrigin 暴露 MTree 客户端来源，供测试对照请求头。
func stripe_MTreeOrigin() string { return stripe_mtreeOrigin }

// mtreeTimings 记录各特征的计算耗时，按官方字段逐一测量，不使用固定常数。
type stripe_mtreeTimings struct {
	start time.Time
	last  time.Time
	now   func() time.Time
}

func stripe_newMtreeTimings(now func() time.Time) *stripe_mtreeTimings {
	if now == nil {
		now = time.Now
	}
	start := now()
	return &stripe_mtreeTimings{start: start, last: start, now: now}
}

func (m *stripe_mtreeTimings) lap() float64 {
	current := m.now()
	elapsed := float64(current.Sub(m.last).Microseconds()) / 1000
	m.last = current
	return stripe_normalizeMilliseconds(elapsed)
}

func (m *stripe_mtreeTimings) total() float64 {
	return stripe_normalizeMilliseconds(float64(m.now().Sub(m.start).Microseconds()) / 1000)
}

// normalizeMilliseconds 输出毫秒耗时，精度对齐官方实测取值（0、0.1、4.6、4.9）。
//
// 官方是 `+(now()-start).toPrecision(5)`。不能直接照搬：toPrecision 对亚毫秒值会输出
// 科学计数法（如 1.2345e-05），转成十进制会退化成 0，使真实耗时全部显示为 0。
// 这里按量级选择定点小数位，保证微秒级耗时仍可见，同时贴近 5 位有效数字：
//
//	>= 100ms  -> 1 位小数   (123.5)
//	>= 10ms   -> 2 位小数   (12.35)
//	>= 1ms    -> 3 位小数   (1.235)
//	<  1ms    -> 4 位小数   (0.0770)
func stripe_normalizeMilliseconds(milliseconds float64) float64 {
	if milliseconds < 0 {
		return 0
	}
	places := 4
	switch {
	case milliseconds >= 100:
		places = 1
	case milliseconds >= 10:
		places = 2
	case milliseconds >= 1:
		places = 3
	}
	rounded, err := strconv.ParseFloat(strconv.FormatFloat(milliseconds, 'f', places, 64), 64)
	if err != nil {
		return 0
	}
	return rounded
}

// MTreeFeatureKeys 按官方脚本的短键顺序列出已实现的特征键。
// 供对照测试构造期望载荷，避免测试与实现各写一份键表。
func stripe_MTreeFeatureKeys() []string {
	return []string{"b", "c", "d", "e", "f", "g", "h", "i", "l"}
}

// MTreeFeatureValues 返回每个已实现特征键对应的期望值。
// 值全部取自传入画像的既有字段，与 deviceFeature 使用同一份定义。
func stripe_MTreeFeatureValues(profile protocol_BrowserProfile) map[string]string {
	return map[string]string{
		"b": "true",
		"c": strings.Join(stripe_profileLanguages(), ","),
		"d": profile.NavigatorPlatform,
		"e": stripe_stripePluginsWithMime,
		"f": strconv.Itoa(profile.AvailWidth) + "w_" + strconv.Itoa(profile.AvailHeight) + "h_" +
			strconv.Itoa(profile.ColorDepth) + "d_" + stripe_formatPixelRatio(profile.DevicePixelRatio) + "r",
		"g": strconv.Itoa(profile.HardwareConcurrency),
		"h": "false",
		"i": "sessionStorage-unavailable, localStorage-unavailable",
		"l": profile.UserAgent,
	}
}

// deviceFeature 组装顶层 a 特征表。只填写本笔画像确实持有的设备事实与可验证的固定语义，
// 不虚构主机硬件，也不猜测官方脚本中语义未确认的字段。
func (s *stripe_Stripe) deviceFeature(timings *stripe_mtreeTimings) protocol_Fields {
	values := stripe_MTreeFeatureValues(s.Profile)
	// l 为异步项，官方对异步特征额外上报 at 活跃耗时。
	async := map[string]bool{"l": true}
	var fields protocol_Fields
	for _, key := range stripe_MTreeFeatureKeys() {
		elapsed := timings.lap()
		feature := protocol_O("v", values[key], "t", elapsed)
		if async[key] {
			feature.Set("at", elapsed)
		}
		fields.Set(key, feature)
	}
	return fields
}

// profileLanguages 是画像语言列表；当前画像固定 en-US。
func stripe_profileLanguages() []string { return []string{"en-US", "en"} }

// formatPixelRatio 让 1.0 输出为 1，与浏览器把 devicePixelRatio 拼接进字符串的结果一致。
func stripe_formatPixelRatio(value float64) string {
	return strconv.FormatFloat(value, 'f', -1, 64)
}

// 说明：真实抓包中 m/6 载荷有"两种形态"，这是第一种（设备注册）：
// 顶层 `a` 承载特征对象（首次注册为 null），顶层 `b` 承载上下文对象。
// 第二种是行为遥测 `{muid,sid,url,source,data}`（如 mouse-timings），
// 纯协议客户端没有真实交互事件，不伪造该数据。
//
// deviceContext 组装顶层 b 上下文对象，字段取值与真实抓包一致。
// 首次注册时 muid/sid 还没有值，上下文的 d/e 留空；服务端会在响应里回填它们。
func (s *stripe_Stripe) deviceContext() protocol_Fields {
	context := protocol_O("a", "")
	// b.b 是当前页面 URL 的 hashed 值。协议客户端没有页面 URL 上下文，留空不伪造。
	context.Set("b", "")
	// b.c 在真实抓包中是页面脚本注入的固定串（形如 Slack webhook token），来源不明，不伪造。
	// b.d/b.e 是已建立的设备标识。真实抓包里首次注册也带这两个字段（取值来自已有的
	// __stripe_mid/__stripe_sid cookie）；协议客户端首次注册时没有值，按空串发送而不是省略。
	context.Set("d", s.CP.StripeIdentity["muid"])
	context.Set("e", s.CP.StripeIdentity["sid"])
	context.Set("f", false)
	context.Set("g", true)
	context.Set("h", true)
	context.Set("i", []string{"location"})
	context.Set("j", []string{})
	// b.n 在真实抓包中是重复出现的同一数值（本次会话 256.8），疑似页面级耗时聚合值，
	// 语义未确认，按 0 发送而不编造。
	context.Set("n", 0)
	// b.u 是当前页面的域名；纯协议流程没有页面，留空。
	context.Set("u", "")
	// b.w 是 `时间戳:sha256(随机值+时间戳+1)`，由页面脚本注入，协议客户端没有该值。
	return context
}

// stripePluginsWithMime 对应浏览器 plugins 数组暴露的名称与 mime 类型序列。
// 取值来自 Chromium 内置 PDF 查看器的固定组合，与本文档画像的插件名列表一致。
var stripe_stripePluginsWithMime = strings.Join([]string{
	"PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
	"Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
	"Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
	"Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
	"WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf",
}, ", ")

// randomNonce 对应官方 getRandomValue，生成固定长度的随机标识。
func stripe_randomNonce(length int) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
	var b strings.Builder
	for i := 0; i < length; i++ {
		b.WriteByte(alphabet[dep_math_rand_v2.IntN(len(alphabet))])
	}
	return b.String()
}

// sha256WithSalt 对应官方 sha256WithSalt：sha256(值 + URL_SALT)，输出小写 hex。
func stripe_sha256WithSalt(value string) string {
	sum := sha256.Sum256([]byte(value + stripe_mtreeURLSalt))
	return hex.EncodeToString(sum[:])
}

// mtreeEncode 对应官方的 safeBtoa(encodeURIComponent(JSON.stringify(payload)))，即
// JSON → encodeURIComponent → base64。浏览器请求体在 DevTools 的“请求载荷”里显示为 base64 文本。
func stripe_mtreeEncode(payload any) (string, error) {
	plain, err := protocol_CompactJSON(payload)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString([]byte(stripe_encodeURIComponent(plain))), nil
}

// encodeURIComponent 实现 ECMAScript 的 encodeURIComponent 语义：
// 不转义 A-Za-z0-9 与 - _ . ! ~ * ' ( )，其余字节按 UTF-8 逐字节写成 %XX。
//
// 不能用 url.QueryEscape 代替：那是表单/查询串编码，会把空格写成 "+"，
// 而官方用 decodeURIComponent 反解，"+" 不会还原成空格，特征值会被解成
// "PDF+Viewer,..." 这种与浏览器不一致的内容。实测浏览器载荷里空格是 %20、加号是 %2B。
func stripe_encodeURIComponent(value string) string {
	const unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"
	var b strings.Builder
	for i := 0; i < len(value); i++ {
		c := value[i]
		if strings.IndexByte(unreserved, c) >= 0 {
			b.WriteByte(c)
			continue
		}
		const hexDigits = "0123456789ABCDEF"
		b.WriteByte('%')
		b.WriteByte(hexDigits[c>>4])
		b.WriteByte(hexDigits[c&0x0f])
	}
	return b.String()
}

// mtreeHeaders 是官方客户端发送 m/6 时使用的请求头集合，取值来自真实浏览器抓包。
//
// 关键点：MTree 脚本本身托管在 m.stripe.network，因此它发出的请求
// Origin/Referer 是 https://m.stripe.network 而不是 js.stripe.com。
// 服务端用 access-control-allow-origin: https://m.stripe.network 校验，写错会跨域失败。
// sec-fetch-site 对应 cross-site（结算页 -> m.stripe.network）。
//
// 正文是标准 base64，含 "+" 字符；必须避免被当成表单编码（"+" 会被解成空格），
// 真实浏览器显式声明 text/plain;charset=UTF-8，且服务端据此放行 Content-Type。
func (s *stripe_Stripe) mtreeHeaders() protocol_Fields {
	h := s.Headers()
	h.Set("Origin", stripe_mtreeOrigin)
	h.Set("Referer", stripe_mtreeOrigin+"/")
	h.Update(protocol_O(
		"Accept", "*/*",
		"Content-Type", "text/plain;charset=UTF-8",
		"sec-fetch-dest", "empty",
		"sec-fetch-mode", "cors",
		"sec-fetch-site", "cross-site",
	))
	return h
}

// ---- internal/stripe/portal.go ----

const stripe_portalHost = "pay.openai.com"

var stripe_preloadedJSON = regexp.MustCompile(`(?s)<script[^>]*id="preloaded_json"[^>]*>(.*?)</script>`)

type stripe_portalSession struct {
	url, key, id, version, publishableKey, secret string
}

func (s *stripe_Stripe) LoadPortal(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Host != stripe_portalHost || parsed.User != nil || !strings.HasPrefix(parsed.Path, "/p/session") {
		return protocol_NewError("customer_portal_unavailable")
	}
	h := s.Profile.Headers()
	h.Update(protocol_O("Accept", "text/html,application/xhtml+xml", "Upgrade-Insecure-Requests", "1"))
	response, err := s.HTTP.Request("GET", raw, protocol_RequestOptions{Headers: h})
	if err != nil {
		return err
	}
	if response.StatusCode != 200 {
		return protocol_NewError("customer_portal_unavailable")
	}
	boot, err := stripe_parsePortalBootstrap(string(response.Content))
	if err != nil {
		return err
	}
	s.portal = stripe_portalSession{
		url:            raw,
		key:            protocol_AsString(boot["session_api_key"]),
		id:             protocol_AsString(boot["portal_session_id"]),
		version:        protocol_AsString(boot["current_version"]),
		publishableKey: protocol_AsString(boot["publishable_key"]),
		secret:         parsed.Query().Get("secret"),
	}
	if s.portal.secret == "" {
		parts := strings.Split(strings.Trim(parsed.Path, "/"), "/")
		if len(parts) >= 3 && parts[0] == "p" && parts[1] == "session" {
			s.portal.secret = parts[2]
		}
	}
	if !strings.HasPrefix(s.portal.key, "ek_") || !strings.HasPrefix(s.portal.id, "bps_") || !strings.HasPrefix(s.portal.publishableKey, "pk_") || s.portal.version == "" {
		return protocol_NewError("customer_portal_unavailable")
	}
	s.CP.PublishableKey = s.portal.publishableKey
	s.HostedURL = raw
	return nil
}

func (s *stripe_Stripe) CreatePortalSetupIntent() (string, string, error) {
	if s.portal.id == "" {
		return "", "", protocol_NewError("customer_portal_unavailable")
	}
	data, err := s.Call("POST", "/billing_portal/sessions/"+s.portal.id+"/exp/setup_intents", stripe_StripeCallOptions{Headers: s.portalHeaders(), Form: protocol_O()})
	if err != nil {
		return "", "", err
	}
	id := protocol_AsString(data["id"])
	secret := protocol_AsString(data["client_secret"])
	if !strings.HasPrefix(id, "seti_") || !strings.Contains(secret, "_secret_") {
		return "", "", protocol_NewError("setup_intent_missing")
	}
	return id, secret, nil
}

func (s *stripe_Stripe) BindPortalElements(secret string) error {
	if s.portal.id == "" || !strings.Contains(secret, "_secret_") || !strings.HasPrefix(s.CP.PublishableKey, "pk_") {
		return protocol_NewError("setup_intent_missing")
	}
	query := url.Values{}
	query.Set("client_secret", secret)
	query.Set("key", s.CP.PublishableKey)
	query.Set("_stripe_version", s.portal.version)
	query.Set("type", "setup_intent")
	query.Set("elements_init_source", "payment_element")
	query.Set("referrer_host", stripe_portalHost)
	query.Set("locale", "en-US")
	data, err := s.Call("GET", "/elements/sessions?"+query.Encode(), stripe_StripeCallOptions{})
	if err != nil {
		return err
	}
	id := stripe_findElementsSessionID(data)
	if !strings.HasPrefix(id, "elements_session_") {
		return protocol_NewError("stripe_build_missing")
	}
	s.ElementsID = id
	if configID := protocol_AsString(protocol_FirstTruthy(data["config_id"], protocol_AsMap(data["elements_session"])["config_id"])); configID != "" {
		s.ConfigID = configID
	}
	return nil
}

func (s *stripe_Stripe) PortalConfirmationToken(card model_Card, billing model_Billing) (string, error) {
	if !stripe_validStripeJSVersion(s.JSVersion) {
		return "", protocol_NewError("stripe_build_missing")
	}
	m := s.portalAttribution()
	details := protocol_O("name", billing.Name, "phone", "", "address", billing.Address())
	pm := protocol_O("type", "card", "card", stripe_cardFields(card), "allow_redisplay", "limited", "billing_details", details, "payment_user_agent", s.portalUserAgent()+"; deferred-intent", "referrer", s.portalReferrer(), "time_on_page", s.TimeOnPage())
	pm.Update(s.IDs())
	pm.Set("client_attribution_metadata", m)
	form := protocol_O("payment_method_data", pm, "client_attribution_metadata", m, "setup_future_usage", "off_session", "set_as_default_payment_method", "true", "key", s.CP.PublishableKey, "_stripe_version", s.portal.version, "client_context", protocol_O("mode", "setup", "currency", strings.ToLower(s.CP.Currency), "payment_method_types", []string{"card"}))
	data, err := s.Call("POST", "/confirmation_tokens", stripe_StripeCallOptions{Form: form})
	if err != nil {
		return "", err
	}
	id := protocol_AsString(data["id"])
	if !strings.HasPrefix(id, "ctoken_") {
		return "", protocol_NewError("confirmation_token_missing")
	}
	return id, nil
}

func (s *stripe_Stripe) PortalAPIVersion() string {
	return s.portal.version
}

func (s *stripe_Stripe) ConfirmPortalSetupIntent(id, secret, paymentMethodID, confirmationToken string) (string, error) {
	if !strings.HasPrefix(id, "seti_") || !strings.Contains(secret, "_secret_") {
		return "", protocol_NewError("setup_intent_missing")
	}
	if confirmationToken == "" && !strings.HasPrefix(paymentMethodID, "pm_") {
		return "", protocol_NewError("payment_method_missing")
	}
	form := protocol_O("client_secret", secret, "return_url", s.portalReturnURL(), "key", s.CP.PublishableKey, "_stripe_version", s.portal.version, "client_attribution_metadata", s.portalAttribution())
	if confirmationToken != "" {
		form.Set("confirmation_token", confirmationToken)
	} else {
		form.Set("payment_method", paymentMethodID)
		form.Set("mandate_data", protocol_O("customer_acceptance", protocol_O("type", "online", "online", protocol_O("infer_from_client", "true"))))
	}
	data, err := s.Call("POST", "/setup_intents/"+id+"/confirm", stripe_StripeCallOptions{Form: form, AllowError: true})
	if err != nil {
		return "", err
	}
	pm := stripe_setupIntentPaymentMethod(data)
	if stripe_setupIntentSucceeded(data, pm) {
		if pm == "" {
			pm = paymentMethodID
		}
		return pm, nil
	}
	if confirmationToken == "" && paymentMethodID != "" {
		portalForm := protocol_O("payment_method", paymentMethodID, "return_url", s.portalReturnURL())
		data, err = s.Call("POST", "/billing_portal/sessions/"+s.portal.id+"/exp/setup_intents/"+id+"/confirm", stripe_StripeCallOptions{Headers: s.portalHeaders(), Form: portalForm, AllowError: true})
		if err != nil {
			return "", err
		}
		pm = stripe_setupIntentPaymentMethod(data)
		if stripe_setupIntentSucceeded(data, pm) {
			if pm == "" {
				pm = paymentMethodID
			}
			return pm, nil
		}
	}
	if strings.ToLower(protocol_AsString(data["status"])) == "requires_action" {
		return pm, protocol_NewError("payment_action_required")
	}
	return "", protocol_NewError("setup_intent_failed")
}

func stripe_setupIntentPaymentMethod(data protocol_Map) string {
	return protocol_AsString(protocol_FirstTruthy(data["payment_method"], protocol_AsMap(data["payment_method"])["id"]))
}

func (s *stripe_Stripe) SetPortalDefaultPaymentMethod(paymentMethodID string) error {
	if s.portal.id == "" || !strings.HasPrefix(paymentMethodID, "pm_") {
		return protocol_NewError("payment_method_missing")
	}
	_, err := s.Call("POST", "/billing_portal/sessions/"+s.portal.id+"/exp/set_customer_default_payment_instrument", stripe_StripeCallOptions{Headers: s.portalHeaders(), Form: protocol_O("payment_method", paymentMethodID), AllowError: true})
	return err
}

func (s *stripe_Stripe) portalHeaders() protocol_Fields {
	h := s.Headers(true)
	h.Update(protocol_O("Accept", "application/json", "Content-Type", "application/x-www-form-urlencoded", "Authorization", "Bearer "+s.portal.key, "Stripe-Version", s.portal.version))
	return h
}

func (s *stripe_Stripe) portalReturnURL() string {
	u, err := url.Parse(s.portal.url)
	if err != nil {
		return "https://" + stripe_portalHost + "/p/session/add-payment-method/return"
	}
	q := url.Values{}
	if s.portal.secret != "" {
		q.Set("secret", s.portal.secret)
	}
	u.Path = "/p/session/add-payment-method/return"
	u.RawQuery = q.Encode()
	u.Fragment = ""
	return u.String()
}

func (s *stripe_Stripe) portalReferrer() string {
	return strings.Split(s.portal.url, "#")[0]
}

func (s *stripe_Stripe) portalUserAgent() string {
	return "stripe.js/" + s.JSVersion + "; stripe-js-v3/" + s.JSVersion + "; payment-element"
}

func (s *stripe_Stripe) portalAttribution() protocol_Fields {
	return protocol_O("client_session_id", s.ClientID, "merchant_integration_source", "elements", "merchant_integration_subtype", "payment-element", "merchant_integration_version", "2021", "payment_method_selection_flow", "automatic", "elements_session_id", s.ElementsID, "elements_session_config_id", s.ConfigID)
}

func stripe_findElementsSessionID(v any) string {
	switch x := v.(type) {
	case string:
		if strings.HasPrefix(x, "elements_session_") {
			return x
		}
	case protocol_Map:
		for _, key := range []string{"elements_session_id", "session_id", "id"} {
			if id := stripe_findElementsSessionID(x[key]); id != "" {
				return id
			}
		}
		if id := stripe_findElementsSessionID(x["elements_session"]); id != "" {
			return id
		}
		for _, val := range x {
			if id := stripe_findElementsSessionID(val); id != "" {
				return id
			}
		}
	case []any:
		for _, item := range x {
			if id := stripe_findElementsSessionID(item); id != "" {
				return id
			}
		}
	}
	return ""
}

func stripe_parsePortalBootstrap(raw string) (protocol_Map, error) {
	match := stripe_preloadedJSON.FindStringSubmatch(raw)
	if match == nil {
		return nil, protocol_NewError("customer_portal_unavailable")
	}
	js := strings.TrimSpace(dep_html.UnescapeString(match[1]))
	var boot protocol_Map
	if err := json.Unmarshal([]byte(js), &boot); err != nil {
		return nil, protocol_NewError("customer_portal_unavailable")
	}
	return boot, nil
}

func stripe_setupIntentSucceeded(data protocol_Map, paymentMethodID string) bool {
	if protocol_AsMap(data["error"])["message"] != nil || protocol_AsMap(data["error"])["code"] != nil {
		return false
	}
	status := strings.ToLower(protocol_AsString(data["status"]))
	if status != "succeeded" && status != "processing" {
		return false
	}
	pm := protocol_AsString(protocol_FirstTruthy(data["payment_method"], protocol_AsMap(data["payment_method"])["id"]))
	return pm == "" || pm == paymentMethodID
}

// ---- internal/stripe/radar.go ----

func (s *stripe_Stripe) CreateRadarSession() error {
	if strings.HasPrefix(s.RadarSessionID, "rse_") {
		return nil
	}
	if !stripe_validStripeJSVersion(s.JSVersion) {
		return protocol_NewError("stripe_build_missing")
	}
	identity := s.CP.StripeIdentity
	if identity["guid"] == "" || identity["muid"] == "" || identity["sid"] == "" {
		return protocol_NewError("stripe_identity_missing")
	}
	form := protocol_O(
		"guid", identity["guid"],
		"muid", identity["muid"],
		"sid", identity["sid"],
		"referrer", "https://chatgpt.com",
		"payment_user_agent", "stripe.js/"+s.JSVersion+"; stripe-js-v3/"+s.JSVersion+"; payment-element; deferred-intent",
		"key", s.CP.PublishableKey,
		"_stripe_version", s.CP.StripeVersion,
	)
	data, err := s.Call("POST", "/radar/session", stripe_StripeCallOptions{Form: form, AllowError: true})
	if err != nil {
		return err
	}
	id := protocol_AsString(data["id"])
	if !strings.HasPrefix(id, "rse_") {
		if code := protocol_AsString(protocol_AsMap(data["error"])["code"]); code != "" {
			return protocol_NewError(code)
		}
		return protocol_NewError("radar_session_missing")
	}
	s.RadarSessionID = id
	return nil
}

func (s *stripe_Stripe) radarOptions() protocol_Fields {
	if !strings.HasPrefix(s.RadarSessionID, "rse_") {
		return nil
	}
	return protocol_O("session", s.RadarSessionID)
}

// ---- internal/stripe/status.go ----

// Classify 优先采用 PaymentIntent 的直接资金证据，避免页面状态掩盖已扣款或待操作状态。
func stripe_Classify(value any) model_PaymentState {
	data, ok := value.(map[string]any)
	if !ok {
		return model_PaymentUnknown
	}
	rootError := protocol_AsMap(data["error"])
	containers := []protocol_Map{data}
	for _, key := range []string{"error", "submission_attempt"} {
		if node := protocol_AsMap(data[key]); node != nil {
			containers = append(containers, node)
		}
	}
	for _, node := range containers {
		if pi := protocol_AsMap(node["payment_intent"]); pi != nil {
			if state := stripe_classifyIntent(pi, rootError); state != model_PaymentUnknown {
				return state
			}
		}
	}
	if strings.HasPrefix(protocol_AsString(data["id"]), "pi_") || data["object"] == "payment_intent" {
		if state := stripe_classifyIntent(data, rootError); state != model_PaymentUnknown {
			return state
		}
	}
	paymentStatus := stripe_normalizedStatus(data["payment_status"])
	if paymentStatus == "paid" {
		return model_PaymentCharged
	}
	submission := protocol_AsMap(data["submission_attempt"])
	topStates := []string{stripe_normalizedStatus(data["status"]), stripe_normalizedStatus(data["state"])}
	submissionStates := []string{stripe_normalizedStatus(submission["state"]), stripe_normalizedStatus(submission["status"])}
	states := append(append(append([]string{}, topStates...), submissionStates...), paymentStatus)
	if stripe_statusAny(states, "canceled", "cancelled", "declined", "failed", "expired") {
		return model_PaymentDeclined
	}
	if stripe_statusAny(states, "requires_approval") || stripe_statusAny(submissionStates, "requires_action") {
		return model_PaymentUnknown
	}
	if stripe_statusAny(topStates, "complete") && paymentStatus == "unpaid" {
		return model_PaymentDeclined
	}
	if stripe_statusAny(states, "open", "processing", "pending", "requires_confirmation") || paymentStatus == "unpaid" {
		return model_PaymentPending
	}
	if stripe_statusAny(topStates, "requires_action") {
		return model_PaymentActionRequired
	}
	errorData := protocol_AsMap(protocol_FirstTruthy(data["last_payment_error"], data["error"]))
	_, hasStatus := data["status"]
	_, hasState := data["state"]
	if (hasStatus || hasState) && stripe_statusAny(states, "requires_payment_method", "") && (protocol_Truthy(errorData["decline_code"]) || errorData["code"] == "card_declined") {
		return model_PaymentDeclined
	}
	return model_PaymentUnknown
}

func stripe_classifyIntent(pi, fallback protocol_Map) model_PaymentState {
	status := stripe_normalizedStatus(pi["status"])
	switch status {
	case "succeeded":
		return model_PaymentCharged
	case "requires_capture":
		return model_PaymentAuthorized
	case "requires_action":
		return model_PaymentActionRequired
	case "processing", "requires_confirmation":
		return model_PaymentPending
	case "canceled":
		return model_PaymentDeclined
	}
	// 验证失败已退回付款方式选择，不再是等待挑战；仅采用 PI 自身的明确错误。
	// 不根据 HTTP 错误或历史错误覆盖 processing、requires_capture、succeeded。
	if status == "requires_payment_method" && protocol_AsMap(pi["last_payment_error"])["code"] == "payment_intent_authentication_failure" {
		return model_PaymentDeclined
	}
	errorData := protocol_AsMap(protocol_FirstTruthy(pi["last_payment_error"], fallback))
	_, hasStatus := pi["status"]
	if hasStatus && (status == "requires_payment_method" || status == "") && (protocol_Truthy(errorData["decline_code"]) || errorData["code"] == "card_declined") {
		return model_PaymentDeclined
	}
	return model_PaymentUnknown
}

func stripe_normalizedStatus(v any) string {
	return strings.ToLower(strings.TrimSpace(protocol_AsString(protocol_FirstTruthy(v, ""))))
}

func stripe_statusAny(values []string, targets ...string) bool {
	for _, value := range values {
		for _, target := range targets {
			if value == target {
				return true
			}
		}
	}
	return false
}

// ---- internal/transport/compression.go ----

// fhttp v0.6.9 在 HTTP/1.1 readLoop 内识别 deflate 时会等待自己的 EOF 通知，
// 形成死锁。关闭它的自动解压后，在正文读取完毕的调用方解码，保留 curl 的原响应头。
func transport_decodeTransportBody(content []byte, encoding string) ([]byte, error) {
	kinds := strings.Split(encoding, ",")
	for i := len(kinds) - 1; i >= 0; i-- {
		var reader io.Reader
		var closeReader func() error
		raw := bytes.NewReader(content)
		switch strings.ToLower(strings.TrimSpace(kinds[i])) {
		case "", "identity":
			continue
		case "gzip", "x-gzip":
			r, err := gzip.NewReader(raw)
			if err != nil {
				return nil, err
			}
			reader = r
			closeReader = r.Close
		case "deflate":
			r, err := zlib.NewReader(raw)
			if err == nil {
				reader = r
				closeReader = r.Close
			} else {
				r = flate.NewReader(bytes.NewReader(content))
				reader = r
				closeReader = r.Close
			}
		case "br":
			reader = brotli.NewReader(raw)
		case "zstd":
			r, err := zstd.NewReader(raw, zstd.WithDecoderConcurrency(1))
			if err != nil {
				return nil, err
			}
			reader = r
			closeReader = func() error { r.Close(); return nil }
		default:
			return nil, errors.New("unsupported content encoding")
		}
		decoded, err := io.ReadAll(reader)
		if closeReader != nil {
			closeErr := closeReader()
			if err == nil {
				err = closeErr
			}
		}
		if err != nil {
			return nil, err
		}
		content = decoded
	}
	return content, nil
}

// ---- internal/transport/preconnect.go ----

// preparedConnection 只预建隧道，不发送 HTTP；连接仅交给指定请求一次。
type transport_preparedConnection struct {
	ready   chan struct{}
	cancel  context.CancelFunc
	conn    net.Conn
	address string
	started time.Time
	base    proxy.ContextDialer
	once    sync.Once
}

// Preconnect 为本笔交易的下一条指定 POST 提前完成 TCP/代理握手。
// 不支持并发调用 Transport 方法；后台只使用独立拨号器，不读写交易会话。
func (t *transport_Transport) Preconnect(raw string) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || t.prepared != nil {
		return
	}
	port := u.Port()
	if port == "" {
		port = "443"
	}
	ctx, cancel := context.WithTimeout(t.ctx, t.timeout)
	p := &transport_preparedConnection{ready: make(chan struct{}), cancel: cancel, address: net.JoinHostPort(u.Hostname(), port), started: time.Now(), base: t.client.GetDialer()}
	t.prepared, t.preparedURL = p, raw
	go func() {
		defer close(p.ready)
		p.conn, _ = p.base.DialContext(ctx, "tcp", p.address)
	}()
}

func (p *transport_preparedConnection) Close() {
	p.cancel()
	<-p.ready
	p.once.Do(func() {
		if p.conn != nil {
			_ = p.conn.Close()
		}
	})
}

func (p *transport_preparedConnection) Dial(network, address string) (net.Conn, error) {
	return p.DialContext(context.Background(), network, address)
}

func (p *transport_preparedConnection) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	if address != p.address {
		return p.base.DialContext(ctx, network, address)
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-p.ready:
	}
	var conn net.Conn
	p.once.Do(func() {
		if time.Since(p.started) <= 15*time.Second && ctx.Err() == nil {
			conn = p.conn
		} else if p.conn != nil {
			_ = p.conn.Close()
		}
	})
	if conn != nil {
		return conn, nil
	}
	// 预建失败时尚未发送业务请求，使用原拨号器正常建立连接。
	return p.base.DialContext(ctx, network, address)
}

func (t *transport_Transport) preparedClient(raw string) (tls_client.HttpClient, error) {
	if t.prepared == nil || raw != t.preparedURL {
		return t.newClient()
	}
	p := t.prepared
	options := append([]tls_client.HttpClientOption{}, t.options...)
	options = append(options, tls_client.WithProxyDialerFactory(func(string, time.Duration, *net.TCPAddr, fhttp.Header, tls_client.Logger) (proxy.ContextDialer, error) {
		return p, nil
	}))
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), options...)
}

// ---- internal/transport/proxy.go ----

var transport_errProxy = errors.New("proxy connection failed")

type transport_transportProxyDialer struct {
	url     *url.URL
	dialer  net.Dialer
	timeout time.Duration
	// 仅本地证书测试设置；生产使用系统根证书。
	tlsConfig *tls.Config
}

func transport_parseTransportProxy(raw string) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return nil, transport_errProxy
	}
	port := ""
	switch u.Scheme {
	case "http":
		port = "80"
	case "https":
		port = "443"
	case "socks4", "socks4a", "socks5", "socks5h":
		port = "1080"
	default:
		return nil, transport_errProxy
	}
	if u.Port() == "" {
		u.Host = net.JoinHostPort(u.Hostname(), port)
	} else if n, err := strconv.Atoi(u.Port()); err != nil || n < 1 || n > 65535 {
		return nil, transport_errProxy
	}
	return u, nil
}

func transport_transportProxyFactory(raw string, timeout time.Duration, local *net.TCPAddr, _ fhttp.Header, _ tls_client.Logger) (proxy.ContextDialer, error) {
	u, err := transport_parseTransportProxy(raw)
	if err != nil {
		return nil, err
	}
	return &transport_transportProxyDialer{url: u, timeout: timeout, dialer: net.Dialer{Timeout: timeout, LocalAddr: local}}, nil
}

func (d *transport_transportProxyDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	if network != "tcp" && network != "tcp4" && network != "tcp6" {
		return nil, transport_errProxy
	}
	budget := d.timeout
	if budget <= 0 {
		budget = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	c, err := d.dialer.DialContext(ctx, "tcp", d.url.Host)
	if err != nil {
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			_ = c.Close()
		}
	}()
	// CONNECT 和 SOCKS 握手也属于请求预算，取消后不能遗留后台阻塞连接。
	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		if err := c.SetDeadline(deadline); err != nil {
			return nil, err
		}
	}
	var tunnel net.Conn = c
	switch d.url.Scheme {
	case "http", "https":
		if d.url.Scheme == "https" {
			conf := &tls.Config{MinVersion: tls.VersionTLS12, ServerName: d.url.Hostname(), NextProtos: []string{"http/1.1"}}
			if d.tlsConfig != nil {
				conf = d.tlsConfig.Clone()
				conf.ServerName = d.url.Hostname()
				conf.NextProtos = []string{"http/1.1"}
			}
			t := tls.Client(c, conf)
			if err := t.HandshakeContext(ctx); err != nil {
				return nil, err
			}
			tunnel = t
		}
		tunnel, err = d.connect(tunnel, address)
	case "socks4", "socks4a":
		err = d.socks4(ctx, c, address)
	case "socks5", "socks5h":
		err = d.socks5(ctx, c, address)
	default:
		err = transport_errProxy
	}
	if err != nil {
		return nil, err
	}
	if !stop() || ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err := c.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	success = true
	return tunnel, nil
}

func (d *transport_transportProxyDialer) connect(c net.Conn, address string) (net.Conn, error) {
	r := &http.Request{Method: "CONNECT", URL: &url.URL{Host: address}, Host: address, Header: make(http.Header)}
	if d.url.User != nil {
		password, _ := d.url.User.Password()
		token := base64.StdEncoding.EncodeToString([]byte(d.url.User.Username() + ":" + password))
		r.Header.Set("Proxy-Authorization", "Basic "+token)
	}
	if err := r.Write(c); err != nil {
		return nil, err
	}
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, r)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, transport_errProxy
	}
	// ReadResponse 可能预读 CONNECT 后的数据；保留缓冲以免丢掉 TLS 字节。
	return &transport_proxyBufferedConn{Conn: c, reader: br}, nil
}

type transport_proxyBufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *transport_proxyBufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

func transport_proxyTarget(ctx context.Context, address string, local, ipv4 bool) (string, int, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return "", 0, transport_errProxy
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return "", 0, transport_errProxy
	}
	if local && net.ParseIP(host) == nil {
		network := "ip"
		if ipv4 {
			network = "ip4"
		}
		ips, err := net.DefaultResolver.LookupIP(ctx, network, host)
		if err != nil {
			return "", 0, err
		}
		if len(ips) == 0 {
			return "", 0, transport_errProxy
		}
		host = ips[0].String()
	}
	return host, n, nil
}

func (d *transport_transportProxyDialer) socks4(ctx context.Context, c net.Conn, address string) error {
	host, port, err := transport_proxyTarget(ctx, address, d.url.Scheme == "socks4", true)
	if err != nil {
		return err
	}
	user := ""
	if d.url.User != nil {
		user = d.url.User.Username()
	}
	if strings.ContainsRune(user, 0) || strings.ContainsRune(host, 0) {
		return transport_errProxy
	}
	packet := []byte{4, 1, byte(port >> 8), byte(port), 0, 0, 0, 1}
	ip := net.ParseIP(host)
	remote := ip == nil
	if !remote {
		v4 := ip.To4()
		if v4 == nil {
			return transport_errProxy
		}
		copy(packet[4:8], v4)
	} else if d.url.Scheme != "socks4a" {
		return transport_errProxy
	}
	packet = append(packet, []byte(user)...)
	packet = append(packet, 0)
	if remote {
		packet = append(packet, []byte(host)...)
		packet = append(packet, 0)
	}
	if err := transport_writeProxy(c, packet); err != nil {
		return err
	}
	r := make([]byte, 8)
	if _, err := io.ReadFull(c, r); err != nil {
		return err
	}
	if r[0] != 0 || r[1] != 90 {
		return transport_errProxy
	}
	return nil
}

func (d *transport_transportProxyDialer) socks5(ctx context.Context, c net.Conn, address string) error {
	host, port, err := transport_proxyTarget(ctx, address, d.url.Scheme == "socks5", false)
	if err != nil {
		return err
	}
	user, password := "", ""
	if d.url.User != nil {
		user = d.url.User.Username()
		password, _ = d.url.User.Password()
	}
	methods := []byte{5, 1, 0}
	if d.url.User != nil {
		methods = []byte{5, 2, 0, 2}
	}
	if len(user) > 255 || len(password) > 255 || len(host) > 255 {
		return transport_errProxy
	}
	if err := transport_writeProxy(c, methods); err != nil {
		return err
	}
	answer := make([]byte, 2)
	if _, err := io.ReadFull(c, answer); err != nil {
		return err
	}
	if answer[0] != 5 {
		return transport_errProxy
	}
	switch answer[1] {
	case 0:
	case 2:
		if d.url.User == nil {
			return transport_errProxy
		}
		packet := append([]byte{1, byte(len(user))}, []byte(user)...)
		packet = append(packet, byte(len(password)))
		packet = append(packet, []byte(password)...)
		if err := transport_writeProxy(c, packet); err != nil {
			return err
		}
		if _, err := io.ReadFull(c, answer); err != nil {
			return err
		}
		if answer[0] != 1 || answer[1] != 0 {
			return transport_errProxy
		}
	default:
		return transport_errProxy
	}
	packet := []byte{5, 1, 0}
	ip := net.ParseIP(host)
	if ip == nil {
		packet = append(packet, 3, byte(len(host)))
		packet = append(packet, []byte(host)...)
	} else if v4 := ip.To4(); v4 != nil {
		packet = append(packet, 1)
		packet = append(packet, v4...)
	} else {
		packet = append(packet, 4)
		packet = append(packet, ip.To16()...)
	}
	packet = binary.BigEndian.AppendUint16(packet, uint16(port))
	if err := transport_writeProxy(c, packet); err != nil {
		return err
	}
	head := make([]byte, 4)
	if _, err := io.ReadFull(c, head); err != nil {
		return err
	}
	if head[0] != 5 || head[1] != 0 || head[2] != 0 {
		return transport_errProxy
	}
	n := 0
	switch head[3] {
	case 1:
		n = 4
	case 4:
		n = 16
	case 3:
		var b [1]byte
		if _, err := io.ReadFull(c, b[:]); err != nil {
			return err
		}
		n = int(b[0])
	default:
		return transport_errProxy
	}
	_, err = io.CopyN(io.Discard, c, int64(n+2))
	return err
}

func transport_writeProxy(c net.Conn, p []byte) error {
	for len(p) > 0 {
		n, err := c.Write(p)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrUnexpectedEOF
		}
		p = p[n:]
	}
	return nil
}

func transport_transportProxyOption(raw string) tls_client.HttpClientOption {
	return tls_client.WithProxyDialerFactory(func(_ string, timeout time.Duration, local *net.TCPAddr, h fhttp.Header, l tls_client.Logger) (proxy.ContextDialer, error) {
		return transport_transportProxyFactory(raw, timeout, local, h, l)
	})
}

// ---- internal/transport/timing.go ----

// 仅采集阶段时间，不保留目标地址、请求头或原始错误。
// 自定义 TLS 拨号器不保证触发握手钩子，因此连接准备包含代理建链和 TLS，
// 不将缺失的底层测量伪装为零耗时。
type transport_requestTiming struct {
	mu                            sync.Mutex
	started                       time.Time
	connected, written, firstByte time.Time
}

func (t *transport_requestTiming) trace() *fhttptrace.ClientTrace {
	return &fhttptrace.ClientTrace{
		GotConn: func(fhttptrace.GotConnInfo) {
			t.mu.Lock()
			defer t.mu.Unlock()
			if t.connected.IsZero() {
				t.connected = time.Now()
			}
		},
		WroteRequest: func(info fhttptrace.WroteRequestInfo) {
			if info.Err == nil {
				t.mu.Lock()
				defer t.mu.Unlock()
				if t.written.IsZero() {
					t.written = time.Now()
				}
			}
		},
		GotFirstResponseByte: func() {
			t.mu.Lock()
			defer t.mu.Unlock()
			if t.firstByte.IsZero() {
				t.firstByte = time.Now()
			}
		},
	}
}
func (t *transport_requestTiming) fields(finished time.Time) protocol_Map {
	t.mu.Lock()
	defer t.mu.Unlock()
	fields := protocol_Map{}
	add := func(key string, a, b time.Time) {
		if !a.IsZero() && !b.IsZero() && !b.Before(a) {
			fields[key] = float64(b.Sub(a)) / float64(time.Millisecond)
		}
	}
	add("connection_ready_ms", t.started, t.connected)
	add("request_write_ms", t.connected, t.written)
	add("first_byte_ms", t.started, t.firstByte)
	add("response_wait_ms", t.written, t.firstByte)
	// 以最后成功观测的边界描述等待，不猜测 DNS、TCP 或 TLS 子阶段。
	stage, since := "connection", t.started
	if !t.connected.IsZero() {
		stage, since = "request_write", t.connected
	}
	if !t.written.IsZero() {
		stage, since = "response_headers", t.written
	}
	if !t.firstByte.IsZero() {
		stage, since = "response_body", t.firstByte
	}
	fields["network_stage"] = stage
	add("network_stage_elapsed_ms", since, finished)
	return fields
}

// ---- internal/transport/transport.go ----

// curlChrome131Headers 保留 curl_cffi 0.16.3 的默认头布局，将默认平台统一为 Windows。
// 业务请求按本笔交易画像覆盖平台和 UA，与程序运行的操作系统无关。
// sec-ch-ua 直接取画像常量，保证传输层默认头与浏览器画像不会出现两组品牌串。
func transport_curlChrome131Headers() protocol_Fields {
	profile := protocol_DefaultBrowserProfile()
	return protocol_O(
		"sec-ch-ua", profile.Headers().Get("sec-ch-ua"),
		"sec-ch-ua-mobile", "?0", "sec-ch-ua-platform", fmt.Sprintf("%q", profile.Platform),
		"upgrade-insecure-requests", "1",
		"user-agent", profile.UserAgent,
		"accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
		"sec-fetch-site", "none", "sec-fetch-mode", "navigate", "sec-fetch-user", "?1", "sec-fetch-dest", "document",
		"accept-encoding", "gzip, deflate, br, zstd", "accept-language", "en-US,en;q=0.9", "priority", "u=0, i",
	)
}

// Transport 仅供单笔交易串行使用；GET 复用会话，写请求独享网络连接。
type transport_Transport struct {
	ctx          context.Context
	cancel       context.CancelFunc
	timeout      time.Duration
	expires      time.Time
	jar          fhttp.CookieJar
	client       tls_client.HttpClient
	options      []tls_client.HttpClientOption
	observer     *observability_Observer
	service      string
	proxySecrets []string
	prepared     *transport_preparedConnection
	preparedURL  string
}

func transport_NewTransport(ctx context.Context, proxyURL string, timeout float64, expires time.Time) (*transport_Transport, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	ctx, cancel := context.WithCancel(ctx)
	j, err := fhttpcookiejar.New(&fhttpcookiejar.Options{PublicSuffixList: publicsuffix.List})
	if err != nil {
		cancel()
		return nil, &protocol_ProtocolError{Code: "network_error"}
	}
	t := &transport_Transport{ctx: ctx, cancel: cancel, timeout: time.Duration(timeout * float64(time.Second)), expires: expires, jar: j}
	t.options = []tls_client.HttpClientOption{
		tls_client.WithClientProfile(profiles.Chrome_131), tls_client.WithRandomTLSExtensionOrder(),
		tls_client.WithDisableHttp3(), tls_client.WithNotFollowRedirects(),
		tls_client.WithTransportOptions(&tls_client.TransportOptions{DisableCompression: true}),
		tls_client.WithTimeoutMilliseconds(max(1, int(math.Ceil(timeout*1000)))),
	}
	if proxyURL != "" {
		if _, err := transport_parseTransportProxy(proxyURL); err != nil {
			cancel()
			return nil, &protocol_ProtocolError{Code: "network_error"}
		}
		t.options = append(t.options, transport_transportProxyOption(proxyURL))
		if proxy, err := url.Parse(proxyURL); err == nil && proxy.User != nil {
			password, _ := proxy.User.Password()
			t.proxySecrets = []string{proxy.User.Username(), password}
		}
	}
	t.client, err = t.newClient()
	if err != nil {
		cancel()
		return nil, &protocol_ProtocolError{Code: "network_error"}
	}
	return t, nil
}

func (t *transport_Transport) newClient() (tls_client.HttpClient, error) {
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), t.options...)
}

func (t *transport_Transport) Deadline() time.Time { return t.expires }

func (t *transport_Transport) BindObserver(observer *observability_Observer, service string) {
	t.observer, t.service = observer, service
}

func (t *transport_Transport) Close() {
	t.cancel()
	if t.prepared != nil {
		t.prepared.Close()
	}
	t.client.CloseIdleConnections()
}

func (t *transport_Transport) Pause(seconds float64) error {
	d := time.Duration(seconds * float64(time.Second))
	if !time.Now().Add(d).Before(t.expires) {
		return &protocol_ProtocolError{Code: "deadline_exceeded"}
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-t.ctx.Done():
		return &protocol_ProtocolError{Code: "deadline_exceeded"}
	case <-timer.C:
		return nil
	}
}

func (t *transport_Transport) Request(method, rawURL string, opts protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" {
		return nil, &protocol_ProtocolError{Code: "https_required"}
	}
	attempts := 1
	if method == "GET" {
		attempts = 3
	}
	for attempt := 1; attempt <= attempts; attempt++ {
		remaining := time.Until(t.expires)
		if remaining <= 0 || t.ctx.Err() != nil {
			return nil, &protocol_ProtocolError{Code: "deadline_exceeded"}
		}
		budget := min(t.timeout, remaining)
		ctx, cancel := context.WithTimeout(t.ctx, budget)
		// 响应可能清除或轮换 Cookie，失败诊断必须遮盖实际发送的旧值。
		var sentCookies []string
		if t.observer != nil && t.jar != nil {
			for _, cookie := range t.jar.Cookies(u) {
				sentCookies = append(sentCookies, cookie.Value)
			}
		}
		started := time.Now()
		timing := &transport_requestTiming{started: started}
		ctx = fhttptrace.WithClientTrace(ctx, timing.trace())
		response, requestErr := t.requestOnce(ctx, method, rawURL, opts)
		cancel()
		finished := time.Now()
		retrying := attempt < attempts && (requestErr != nil || transport_retryHTTPStatus(response.StatusCode))
		t.observe(method, rawURL, attempt, started, finished, response, requestErr, retrying, opts, sentCookies, timing.fields(finished))
		if !retrying {
			if requestErr != nil {
				return nil, &protocol_ProtocolError{Code: "network_error"}
			}
			return response, nil
		}
		wait := 0.5 * math.Pow(2, float64(attempt-1))
		waitStarted := time.Now()
		waitErr := t.Pause(wait)
		if t.observer != nil {
			outcome := "completed"
			if waitErr != nil {
				outcome = "failed"
			}
			t.observer.Emit("http_retry_wait", protocol_Map{"service": t.service, "attempt": attempt, "outcome": outcome, "retry_wait_ms": wait * 1000, "retry_wait_elapsed_ms": float64(time.Since(waitStarted)) / float64(time.Millisecond), "error_code": protocol_FlowErrorCode(waitErr)})
		}
		if waitErr != nil {
			return nil, waitErr
		}
	}
	return nil, &protocol_ProtocolError{Code: "network_error"}
}

func transport_retryHTTPStatus(status int) bool {
	switch status {
	case 408, 429, 500, 502, 503, 504:
		return true
	}
	return false
}

func (t *transport_Transport) requestOnce(ctx context.Context, method, rawURL string, opts protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	payload, contentType, err := transport_transportPayload(opts)
	if err != nil {
		return nil, err
	}
	var body io.Reader
	if method != "GET" || payload != nil {
		body = io.NopCloser(strings.NewReader(string(payload)))
	}
	req, err := fhttp.NewRequestWithContext(ctx, method, rawURL, body)
	if err != nil {
		return nil, err
	}
	// 即便正文为空也保留非 nil 的一次性 reader，阻断 HTTP/2 的无正文重放分支。
	// 绝不设置 GetBody；幂等键仍完整发送，不用删键规避传输层的重试。
	req.GetBody = nil
	if body != nil {
		req.ContentLength = int64(len(payload))
	}
	req.Header = transport_transportHeaders(opts.Headers, contentType)
	if method == "POST" && payload == nil && req.Header["content-type"] == nil {
		req.Header["content-type"] = []string{"application/x-www-form-urlencoded"}
		req.Header[fhttp.HeaderOrderKey] = append(req.Header[fhttp.HeaderOrderKey], "content-type")
	}
	if u, err := url.Parse(rawURL); err == nil {
		if cookies := t.jar.Cookies(u); len(cookies) > 0 {
			var parts []string
			for _, c := range cookies {
				parts = append(parts, c.Name+"="+c.Value)
			}
			req.Header["cookie"] = append([]string{strings.Join(parts, "; ")}, req.Header["cookie"]...)
		}
	}
	client := t.client
	if method != "GET" {
		client, err = t.preparedClient(rawURL)
		if err != nil {
			return nil, err
		}
		defer client.CloseIdleConnections()
	}
	resp, err := client.Do(req)
	if err != nil {
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		return nil, err
	}
	defer resp.Body.Close()
	if u, err := url.Parse(rawURL); err == nil {
		t.jar.SetCookies(u, resp.Cookies())
	}
	content, err := io.ReadAll(resp.Body)
	if err == nil {
		content, err = transport_decodeTransportBody(content, resp.Header.Get("Content-Encoding"))
	}
	if err != nil {
		return nil, err
	}
	cookies := make(map[string]string)
	for _, c := range resp.Cookies() {
		cookies[c.Name] = c.Value
	}
	headers := make(http.Header, len(resp.Header))
	for key, values := range resp.Header {
		headers[key] = append([]string(nil), values...)
	}
	return &protocol_HTTPResponse{StatusCode: resp.StatusCode, Headers: headers, Content: content, Cookies: cookies}, nil
}

func transport_transportPayload(opts protocol_RequestOptions) ([]byte, string, error) {
	if opts.Form != nil {
		s, err := protocol_FormEncode(opts.Form)
		return []byte(s), "application/octet-stream", err
	}
	if opts.Body != nil {
		s, err := protocol_CompactJSON(opts.Body)
		return []byte(s), "application/json", err
	}
	if opts.Text != nil {
		return []byte(*opts.Text), "application/octet-stream", nil
	}
	return nil, "", nil
}

func transport_transportHeaders(overrides protocol_Fields, contentType string) fhttp.Header {
	defaults := transport_curlChrome131Headers()
	out := make(fhttp.Header)
	order := make([]string, 0, len(defaults)+len(overrides)+2)
	seen := make(map[string]bool)
	for _, group := range []protocol_Fields{defaults, overrides} {
		for _, f := range group {
			key := strings.ToLower(f.Key)
			if !seen[key] {
				order = append(order, key)
				seen[key] = true
			}
			if f.Value == nil {
				delete(out, key)
				continue
			}
			out[key] = []string{fmt.Sprint(f.Value)}
		}
	}
	if contentType != "" && len(out["content-type"]) == 0 {
		out["content-type"] = []string{contentType}
		order = append(order, "content-type")
	}
	// libcurl 的自动 Cookie 和 Content-Length 排在业务头之后。
	order = append(order, "cookie", "content-length")
	out[fhttp.HeaderOrderKey] = order
	return out
}

func (t *transport_Transport) observe(method, rawURL string, attempt int, started, finished time.Time, response *protocol_HTTPResponse, requestErr error, retrying bool, opts protocol_RequestOptions, sentCookies []string, timing ...protocol_Map) {
	if t.observer == nil {
		return
	}
	outcome := "succeeded"
	if retrying {
		outcome = "retry"
	} else if requestErr != nil || response.StatusCode >= 300 {
		outcome = "failed"
	}
	fields := protocol_Map{"service": t.service, "method": method, "path": rawURL, "attempt": attempt, "outcome": outcome, "duration_ms": max(0, float64(finished.Sub(started))/float64(time.Millisecond)), "remaining_deadline_ms": max(0, float64(t.expires.Sub(finished))/float64(time.Millisecond))}
	for _, metrics := range timing {
		for key, value := range metrics {
			fields[key] = value
		}
	}
	if requestErr != nil {
		fields["error_code"] = transport_transportErrorCode(requestErr)
	}
	if response != nil {
		fields["status"] = response.StatusCode
		if response.StatusCode >= 300 {
			fields["error_code"] = fmt.Sprintf("http_%d", response.StatusCode)
			secrets := append(observability_RequestDiagnosticSecrets(rawURL, opts), t.proxySecrets...)
			secrets = append(secrets, sentCookies...)
			for key, value := range observability_HTTPFailureDiagnostics(response, secrets) {
				fields[key] = value
			}
		}
		u, _ := url.Parse(rawURL)
		if t.service == "stripe" && u.Hostname() == "api.stripe.com" {
			if id := response.Headers.Get("Request-Id"); id != "" {
				fields["request_id"] = id
			}
		}
	}
	t.observer.Emit("http_request", fields)
}

func transport_transportErrorCode(err error) string {
	if err == nil {
		return "network_error"
	}
	if n, ok := err.(net.Error); ok && n.Timeout() {
		return "network_timeout"
	}
	return "network_error"
}

// ---- pkg/gptpay/capabilities.go ----

// Capabilities 返回可用于表单与业务校验的 SDK 能力目录，不访问上游。
func gptpay_Capabilities() model_CapabilityCatalog { return model_Capabilities() }

// ---- pkg/gptpay/catalog_prices.go ----

type gptpay_CatalogPricesOptions struct {
	// ConfigKey 是页面实际使用的国家或币种键，例如 US、PHP；不自动猜测或回退。
	ConfigKey string
	// 可选断言用于识别上游地区回退；响应不匹配时整次查询失败。
	ExpectedCountry, ExpectedCurrency string
}

// CatalogPrices 仅请求价格配置 GET，不刷新 Session、不创建 Checkout、不初始化付款。
// 空 Session 使用公开查询；传入凭据时要求完整 Session，不自动刷新或降级。
func (c *gptpay_PaymentClient) CatalogPrices(ctx context.Context, session model_Session, options gptpay_CatalogPricesOptions) (result model_CatalogPrices, err error) {
	options.ConfigKey = strings.ToUpper(strings.TrimSpace(options.ConfigKey))
	options.ExpectedCountry = strings.ToUpper(strings.TrimSpace(options.ExpectedCountry))
	options.ExpectedCurrency = strings.ToUpper(strings.TrimSpace(options.ExpectedCurrency))
	if !protocol_Matches(`[A-Z]{2,3}`, options.ConfigKey) ||
		options.ExpectedCountry != "" && !protocol_Matches(`[A-Z]{2}`, options.ExpectedCountry) ||
		options.ExpectedCurrency != "" && !protocol_Matches(`[A-Z]{3}`, options.ExpectedCurrency) {
		return result, protocol_NewError("input_or_checkpoint_invalid")
	}
	if err = c.validateConfig(); err != nil {
		return result, err
	}
	if (strings.TrimSpace(session.AccessToken) == "") != (strings.TrimSpace(session.SessionToken) == "") {
		return result, protocol_NewError("session_token_required")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(c.Config.Deadline*float64(time.Second)))
	defer cancel()
	if err = ctx.Err(); err != nil {
		return result, err
	}
	deadline, _ := ctx.Deadline()
	profile, err := protocol_ResolveBrowserProfile(c.Config.BrowserProfile)
	if err != nil {
		return result, err
	}
	obs := observability_NewObserver("catalog_prices", c.EventSink, "", "")
	obs.Emit("catalog_prices", protocol_Map{"outcome": "started"})
	defer func() {
		outcome := "completed"
		if err != nil {
			outcome = "failed"
		}
		obs.Emit("catalog_prices", protocol_Map{"outcome": outcome, "error_code": protocol_FlowErrorCode(err)})
	}()
	factory := c.transportFactory
	if factory == nil {
		factory = func(ctx context.Context, proxy string, timeout float64, deadline time.Time) (protocol_HTTPTransport, error) {
			return transport_NewTransport(ctx, proxy, timeout, deadline)
		}
	}
	http, err := factory(ctx, c.Config.Proxy, c.Config.Timeout, deadline)
	if err != nil {
		return result, err
	}
	defer http.Close()
	if h, ok := http.(interface {
		BindObserver(*observability_Observer, string)
	}); ok {
		h.BindObserver(obs, "chatgpt")
	}
	cp := model_DefaultCheckpoint()
	cp.AccessToken, cp.SessionToken, cp.Cookies = session.AccessToken, session.SessionToken, session.Cookies
	clock, _ := fingerprint_NewClock(0)
	platform := chatgpt_NewPlatform(http, &cp, clock)
	platform.Profile = profile
	data, err := platform.PricingConfig(options.ConfigKey)
	if err != nil {
		return result, err
	}
	result, err = gptpay_parseCatalogPrices(data, options)
	if err == nil {
		result.FetchedAt = time.Now().UTC()
	}
	return result, err
}

func gptpay_parseCatalogPrices(data protocol_Map, options gptpay_CatalogPricesOptions) (model_CatalogPrices, error) {
	fail := func() (model_CatalogPrices, error) {
		return model_CatalogPrices{}, protocol_NewError("catalog_prices_invalid")
	}
	country, ok := data["country_code"].(string)
	if !ok || !protocol_Matches(`[A-Z]{2}`, country) {
		return fail()
	}
	config, ok := data["currency_config"].(map[string]any)
	if !ok {
		return fail()
	}
	currency, ok := config["symbol_code"].(string)
	if !ok || !protocol_Matches(`[A-Z]{3}`, currency) {
		return fail()
	}
	exp, ok := protocol_Integer(config["minor_unit_exponent"])
	if !ok || exp < 0 || exp > 4 {
		return fail()
	}
	if options.ExpectedCountry != "" && country != options.ExpectedCountry || options.ExpectedCurrency != "" && currency != options.ExpectedCurrency {
		return model_CatalogPrices{}, protocol_NewError("catalog_prices_region_mismatch")
	}
	result := model_CatalogPrices{ConfigKey: options.ConfigKey, Country: country, Currency: currency, MinorUnitExponent: int(exp), DisplayOnly: true, Items: []model_CatalogPrice{}}
	for _, plan := range model_Capabilities().Plans {
		key := model_PricingConfigPlanKey(plan.ID)
		raw, exists := config[key]
		if !exists {
			continue
		}
		node, ok := raw.(map[string]any)
		if !ok {
			return fail()
		}
		// 只解析 SDK 支持的周期；未开放的年付折算价可能小于币种最小单位。
		for _, interval := range plan.Intervals {
			raw, exists := node[interval]
			if !exists {
				continue
			}
			price, ok := raw.(map[string]any)
			if !ok {
				return fail()
			}
			amount, ok := gptpay_catalogAmountMinor(price["amount"], int(exp))
			if !ok {
				return fail()
			}
			tax, ok := price["tax"].(string)
			if !ok || tax != "inclusive" && tax != "exclusive" {
				return fail()
			}
			basis := "month"
			if interval == "year" {
				basis = "unverified"
			}
			result.Items = append(result.Items, model_CatalogPrice{Plan: plan.ID, PriceKey: key, Interval: interval, AmountMinor: amount, Tax: tax, AmountBasis: basis, PerSeat: plan.Seats != nil})
		}
	}
	if len(result.Items) == 0 {
		return fail()
	}
	return result, nil
}

// 保留 JSON 十进制精度，拒绝隐式舍入、负数、超大指数与 int64 溢出。
func gptpay_catalogAmountMinor(value any, exponent int) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok || !protocol_Matches(`[0-9]{1,19}(\.[0-9]{1,8})?`, string(number)) {
		return 0, false
	}
	amount, ok := new(big.Rat).SetString(string(number))
	if !ok {
		return 0, false
	}
	scale := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(exponent)), nil)
	amount.Mul(amount, new(big.Rat).SetInt(scale))
	if !amount.IsInt() || !amount.Num().IsInt64() {
		return 0, false
	}
	return amount.Num().Int64(), true
}

// ---- pkg/gptpay/checkout_payment.go ----

// 付款接口按订单归属选择；Stripe 内部仍使用与上游会话匹配的表单。
func (t *gptpay_transaction) runCheckoutPayment(card model_Card, billing model_Billing, checkout, info protocol_Map) error {
	switch {
	case strings.HasPrefix(t.cp.CheckoutID, "oaics_"):
		return t.runCustomFlow(card, billing, checkout)
	case strings.HasPrefix(t.cp.CheckoutID, "cs_live_"):
		if t.cp.Metadata["checkout_ui_mode"] == "redirect" {
			return t.runHostedFlow(card, billing, info)
		}
		return t.runCustomStripeCheckout(card, billing, checkout)
	default:
		return protocol_NewError("checkout_id_invalid")
	}
}

// ---- pkg/gptpay/checkout_promo.go ----

func gptpay_checkoutPromoCode(plan, value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	if !model_IsTeamPlan(strings.ToLower(plan)) || !utf8.ValidString(value) || len(value) > 256 {
		return "", protocol_NewError("invalid_promo_code")
	}
	for _, char := range value {
		if unicode.IsControl(char) {
			return "", protocol_NewError("invalid_promo_code")
		}
	}
	return value, nil
}

// ---- pkg/gptpay/checkout_public_url.go ----

// 仅替换已校验完整长链的域名，保留路径及 fragment 的原始编码。
func gptpay_publicCheckoutURL(raw, id string) string {
	if !strings.HasPrefix(id, "cs_live_") || stripe_FindHostedURL(raw, id) == "" {
		return raw
	}
	return strings.Replace(raw, "https://checkout.stripe.com/", "https://pay.openai.com/", 1)
}

// ---- pkg/gptpay/client.go ----

type gptpay_ClientOptions struct {
	Checkpoint   model_CheckpointSink
	EventSink    model_EventSink
	ProgressSink model_ProgressSink
}

type gptpay_PaymentClient struct {
	Config           model_Config
	Checkpoint       model_CheckpointSink
	EventSink        model_EventSink
	ProgressSink     model_ProgressSink
	transportFactory gptpay_transportFactory
	sentinelFactory  func(protocol_HTTPTransport, *model_Checkpoint, fingerprint_Clock) gptpay_tokenSource
}

func gptpay_NewPaymentClient(config model_Config, options ...gptpay_ClientOptions) *gptpay_PaymentClient {
	c := &gptpay_PaymentClient{Config: config}
	if len(options) > 0 {
		c.Checkpoint = options[0].Checkpoint
		c.EventSink = options[0].EventSink
		c.ProgressSink = options[0].ProgressSink
	}
	return c
}

type gptpay_CreateCheckoutOptions struct {
	// PromoCode 仅用于 Team 提链，留空时不发送优惠码。
	PromoCode                             string
	Country, Plan, CheckoutType, Interval string
	Seats, Seats5x                        int
	WithPrice                             bool
	FingerprintClockOffsetHours           int
}

type gptpay_QuoteOptions struct {
	Country, Plan, Interval string
	Seats, Seats5x          int
	Billing                 model_Billing
}

type gptpay_PayOptions struct {
	Country, Plan, CheckoutType, Interval string
	Seats, Seats5x                        int
	Billing                               model_Billing
	ExpectedAmount                        *int64
	CancelRenewal                         *bool
	ApprovePayment                        model_PaymentApproval
	FingerprintClockOffsetHours           int
}

func (c *gptpay_PaymentClient) validateConfig() error {
	if e := protocol_ValidateBrowserProfileSelection(c.Config.BrowserProfile); e != nil {
		return e
	}
	if e := protocol_ValidateProxy(c.Config.Proxy); e != nil {
		return e
	}
	for _, n := range []float64{c.Config.Timeout, c.Config.Deadline} {
		if math.IsNaN(n) || math.IsInf(n, 0) || n <= 0 {
			return fmt.Errorf("invalid timeout or deadline")
		}
	}
	return nil
}

func (c *gptpay_PaymentClient) newCheckpoint(s model_Session, country, plan string, cancel bool, seats, seats5x int, interval string) (model_Checkpoint, error) {
	cp := model_DefaultCheckpoint()
	if e := c.validateConfig(); e != nil {
		return cp, e
	}
	country = strings.ToUpper(country)
	plan = strings.ToLower(plan)
	if model_CheckoutPlan(plan) == "" {
		return cp, fmt.Errorf("unknown plan")
	}
	if !protocol_Matches(`[A-Z]{2}`, country) {
		return cp, fmt.Errorf("invalid country")
	}
	region, ok := model_LookupRegion(country)
	if !ok && (c.Config.Currency == "" || c.Config.Timezone == "") {
		return cp, fmt.Errorf("unknown region: configure currency and timezone")
	}
	if c.Config.Currency != "" {
		region.Currency = strings.ToUpper(c.Config.Currency)
	}
	if c.Config.Timezone != "" {
		region.Timezone = c.Config.Timezone
	}
	if _, e := time.LoadLocation(region.Timezone); e != nil || region.Timezone == "" || region.Timezone == "Local" {
		return cp, fmt.Errorf("invalid timezone")
	}
	if !protocol_Matches(`[A-Z]{3}`, region.Currency) {
		return cp, fmt.Errorf("invalid currency")
	}
	if s.AccessToken == "" || s.SessionToken == "" {
		return cp, fmt.Errorf("Session requires accessToken and sessionToken")
	}
	cfg, err := model_NormalizeTeamSeatConfig(plan, seats, seats5x, interval)
	if err != nil {
		return cp, err
	}
	cp.Plan = plan
	cp.Country = country
	cp.Currency = region.Currency
	cp.Timezone = region.Timezone
	if cfg.Total() > 0 {
		cp.Metadata["team_default_seats"] = int64(cfg.Default)
		cp.Metadata["team_prolite_seats"] = int64(cfg.ProLite)
		cp.Metadata["team_price_interval"] = cfg.Interval
	}
	cp.AccountID = s.AccountID
	cp.Email = s.Email
	cp.AccessToken = s.AccessToken
	cp.SessionToken = s.SessionToken
	cp.Cookies = s.Cookies
	cp.Proxy = c.Config.Proxy
	cp.CancelRequested = cancel
	profile, e := protocol_ResolveBrowserProfile(c.Config.BrowserProfile)
	if e != nil {
		return cp, e
	}
	cp.Metadata["browser_profile"] = profile.ID
	return cp, nil
}

func (c *gptpay_PaymentClient) CreateCheckout(ctx context.Context, s model_Session, o gptpay_CreateCheckoutOptions) (model_CheckoutResult, error) {
	promo, err := gptpay_checkoutPromoCode(o.Plan, o.PromoCode)
	if err != nil {
		return model_CheckoutResult{}, err
	}
	if o.CheckoutType != "" && o.CheckoutType != "oaics" && o.CheckoutType != "hosted" {
		return model_CheckoutResult{}, fmt.Errorf("unknown checkout type")
	}
	clock, e := fingerprint_NewClock(o.FingerprintClockOffsetHours)
	if e != nil {
		return model_CheckoutResult{}, e
	}
	cp, e := c.newCheckpoint(s, o.Country, o.Plan, false, o.Seats, o.Seats5x, o.Interval)
	if e != nil {
		return model_CheckoutResult{}, e
	}
	t, e := gptpay_newTransaction(ctx, c, cp, "create_checkout", clock)
	if e != nil {
		return model_CheckoutResult{}, e
	}
	t.platform.PromoCode = promo
	return t.createCheckout(o.CheckoutType, o.WithPrice), nil
}

func (c *gptpay_PaymentClient) CancelRenewal(ctx context.Context, s model_Session, country string) (model_Result, error) {
	if country == "" {
		return model_Result{}, fmt.Errorf("country is required")
	}
	clock, e := fingerprint_NewClock(0)
	if e != nil {
		return model_Result{}, e
	}
	cp, e := c.newCheckpoint(s, country, "plus", true, 0, 0, "")
	if e != nil {
		return model_Result{}, e
	}
	t, e := gptpay_newTransaction(ctx, c, cp, "pay", clock)
	if e != nil {
		return model_Result{}, e
	}
	return t.cancelRenewalOnly(), nil
}

func (c *gptpay_PaymentClient) Pay(ctx context.Context, s model_Session, card model_Card, o gptpay_PayOptions) (model_Result, error) {
	if c.Checkpoint == nil {
		return model_Result{}, fmt.Errorf("payment requires a synchronous durable checkpoint sink")
	}
	if e := card.Validate(); e != nil {
		return model_Result{}, e
	}
	if e := o.Billing.Validate(); e != nil {
		return model_Result{}, e
	}
	if o.ExpectedAmount != nil && *o.ExpectedAmount < 0 {
		return model_Result{}, fmt.Errorf("invalid expected amount")
	}
	if o.CheckoutType != "" && o.CheckoutType != "oaics" && o.CheckoutType != "hosted" {
		return model_Result{}, fmt.Errorf("invalid checkout type")
	}
	clock, e := fingerprint_NewClock(o.FingerprintClockOffsetHours)
	if e != nil {
		return model_Result{}, e
	}
	cancel := true
	if o.CancelRenewal != nil {
		cancel = *o.CancelRenewal
	}
	cp, e := c.newCheckpoint(s, o.Country, o.Plan, cancel, o.Seats, o.Seats5x, o.Interval)
	if e != nil {
		return model_Result{}, e
	}
	cp.Metadata["fingerprint_clock_offset_hours"] = clock.OffsetHours
	t, e := gptpay_newTransaction(ctx, c, cp, "pay", clock)
	if e != nil {
		return model_Result{}, e
	}
	return t.execute(o.Billing, &card, o.ExpectedAmount, o.ApprovePayment, o.CheckoutType), nil
}

func (c *gptpay_PaymentClient) Quote(ctx context.Context, s model_Session, o gptpay_QuoteOptions) (model_Result, error) {
	if e := o.Billing.Validate(); e != nil {
		return model_Result{}, e
	}
	cp, e := c.newCheckpoint(s, o.Country, o.Plan, false, o.Seats, o.Seats5x, o.Interval)
	if e != nil {
		return model_Result{}, e
	}
	clock, _ := fingerprint_NewClock(0)
	t, e := gptpay_newTransaction(ctx, c, cp, "quote", clock)
	if e != nil {
		return model_Result{}, e
	}
	return t.execute(o.Billing, nil, nil, nil, ""), nil
}

func (c *gptpay_PaymentClient) Recover(ctx context.Context, cp model_Checkpoint) (model_Result, error) {
	if c.Config.BrowserProfile != "" {
		return model_Result{}, fmt.Errorf("recovery uses the checkpoint browser profile; remove the profile override")
	}
	if e := cp.ValidateForRecovery(); e != nil {
		return model_Result{}, e
	}
	if e := c.validateConfig(); e != nil {
		return model_Result{}, e
	}
	if c.Checkpoint == nil {
		return model_Result{}, fmt.Errorf("recovery requires a synchronous durable checkpoint sink")
	}
	clone, e := cp.Clone()
	if e != nil {
		return model_Result{}, e
	}
	offset, _ := protocol_Integer(clone.Metadata["fingerprint_clock_offset_hours"])
	clock, e := fingerprint_NewClock(int(offset))
	if e != nil {
		return model_Result{}, e
	}
	t, e := gptpay_newTransaction(ctx, c, clone, "recover", clock)
	if e != nil {
		return model_Result{}, e
	}
	return t.recover(), nil
}

// ---- pkg/gptpay/custom_submission.go ----

// 仅识别已经确认失败的税务错误，不让未知上游文本进入日志或覆盖已扣款结果。
func gptpay_customSubmissionError(data protocol_Map) error {
	if stripe_Classify(data) != model_PaymentDeclined {
		return nil
	}
	submission := protocol_AsMap(data["submission_attempt"])
	failure := protocol_AsMap(submission["error"])
	if failure["code"] == "billing_automatic_tax_unrecognized_customer_location" {
		return protocol_NewError("billing_automatic_tax_unrecognized_customer_location")
	}
	return nil
}

// ---- pkg/gptpay/dependencies.go ----

type gptpay_transportFactory func(context.Context, string, float64, time.Time) (protocol_HTTPTransport, error)

type gptpay_tokenSource interface {
	Token(flow string) ([2]string, error)
}

// ---- pkg/gptpay/flow_checkout.go ----

func (t *gptpay_transaction) prepareCheckout(mode string, stripeRequired bool) (protocol_Map, error) {
	t.progress.emit(model_ProgressStageSession, model_ProgressRunning, "")
	if e := t.save("initializing"); e != nil {
		return nil, e
	}
	if e := t.platform.Homepage(); e != nil {
		return nil, e
	}
	if e := t.platform.Refresh(); e != nil && (protocol_FlowErrorCode(e) != "session_expired" || t.cp.AccessToken == "") {
		return nil, e
	}
	account, e := t.platform.Account()
	if protocol_FlowErrorCode(e) == "http_401" {
		if e = t.platform.Refresh(); e == nil {
			account, e = t.platform.Account()
		}
	}
	if e != nil {
		return nil, e
	}
	cp := t.cp
	ent := protocol_AsMap(account["entitlement"])
	baseline := protocol_Map{"plan_type": protocol_AsString(protocol_FirstTruthy(protocol_AsMap(account["account"])["plan_type"], ent["plan_type"], "")), "subscription_plan": protocol_AsString(ent["subscription_plan"]), "has_active_subscription": ent["has_active_subscription"], "cancels_at": protocol_AsString(ent["cancels_at"])}
	_, until, renewal, e := t.loadCurrentSubscription(baseline)
	if e != nil {
		return nil, e
	}
	cp.Metadata["subscription_baseline"] = baseline
	flow := gptpay_accountPaymentFlow(cp.Plan, baseline)
	t.emitAccountStatus(baseline, until, renewal, flow)
	if flow == "subscription_update" {
		cp.Metadata["payment_flow"] = "subscription_update"
		cp.Metadata["updated_plan"] = model_CheckoutPlan(cp.Plan)
		if e = t.save("account_checked"); e != nil {
			return nil, e
		}
		t.progress.emit(model_ProgressStageSession, model_ProgressSucceeded, "")
		t.progress.emit(model_ProgressStageCheckout, model_ProgressRunning, "")
		return protocol_Map{"payment_flow": "subscription_update"}, nil
	}
	if e = t.save("account_checked"); e != nil {
		return nil, e
	}
	if flow == "existing_subscription" {
		return nil, protocol_NewError("existing_subscription")
	}
	t.progress.emit(model_ProgressStageSession, model_ProgressSucceeded, "")
	t.progress.emit(model_ProgressStageCheckout, model_ProgressRunning, "")
	if stripeRequired {
		if e = t.initializeStripe(); e != nil {
			return nil, e
		}
		if e = t.save("stripe_ready"); e != nil {
			return nil, e
		}
	}
	if !stripeRequired && mode == "hosted" {
		// 只提前建立独立隧道；仍在 Sentinel 完成后发送唯一一次创建请求。
		if h, ok := t.http.(interface{ Preconnect(string) }); ok {
			h.Preconnect(chatgpt_PlatformBase + "/backend-api/payments/checkout")
		}
	}
	token, e := t.sentinel.Token("chatgpt_checkout")
	if e != nil {
		return nil, e
	}
	checkout, e := t.platform.Checkout(model_CheckoutPlan(cp.Plan), token, mode)
	if e != nil {
		return nil, e
	}
	cp.CheckoutID = protocol_AsString(protocol_FirstTruthy(checkout["checkout_session_id"], checkout["stripe_checkout_session_id"], ""))
	if !protocol_Matches(`(?:oaics_|cs_live_)[A-Za-z0-9]+`, cp.CheckoutID) {
		return nil, protocol_NewError("checkout_id_invalid")
	}
	prefix, route := "oaics_", "chatgpt"
	if strings.HasPrefix(cp.CheckoutID, "cs_live_") {
		prefix, route = "cs_live_", "stripe"
	}
	t.observer.Emit("checkout_route", protocol_Map{"checkout_prefix": prefix, "payment_route": route, "outcome": "selected"})
	cp.Entity = protocol_AsString(protocol_FirstTruthy(checkout["processor_entity"], "openai_llc"))
	if !protocol_Matches(`[a-z0-9_]{2,64}`, cp.Entity) {
		return nil, protocol_NewError("processor_entity_invalid")
	}
	actual, ok := checkout["checkout_ui_mode"].(string)
	if !ok || actual != "custom" && actual != "redirect" && actual != "hosted" {
		return nil, protocol_NewError("checkout_ui_mode_invalid")
	}
	if actual == "hosted" {
		actual = "redirect"
	}
	cp.Metadata["checkout_ui_mode"] = actual
	if secret, ok := checkout["client_secret"].(string); ok && strings.HasPrefix(secret, cp.CheckoutID+"_secret_") && len(secret) > len(cp.CheckoutID+"_secret_") {
		cp.Metadata["checkout_client_secret"] = secret
	}
	if raw := protocol_AsString(checkout["confirm_return_url"]); raw != "" {
		if u, err := chatgpt_ValidateReturnURL(raw, cp); err == nil {
			cp.ReturnURL = u
		}
	}
	if v := checkout["publishable_key"]; v != nil {
		key, ok := v.(string)
		if !ok || !protocol_Matches(`pk_[A-Za-z0-9_]+`, key) {
			return nil, protocol_NewError("publishable_key_missing")
		}
		cp.PublishableKey = key
	}
	if e = t.save("checkout_created"); e != nil {
		return nil, e
	}
	return checkout, nil
}

func (t *gptpay_transaction) initializeStripe() error {
	// Hosted 链接补全可能发生在创建之后，不能覆盖 Checkout 返回的公钥。
	if t.cp.PublishableKey == "" {
		bootstrap, e := t.platform.Call("GET", "/backend-api/payments/stripe_client_bootstrap?account_id="+url.QueryEscape(t.cp.AccountID))
		if e != nil {
			return e
		}
		t.cp.PublishableKey = protocol_AsString(protocol_FirstTruthy(bootstrap["publishable_key"], bootstrap["publishableKey"]))
		if !strings.HasPrefix(t.cp.PublishableKey, "pk_") {
			return protocol_NewError("publishable_key_missing")
		}
	}
	return t.stripe.Initialize()
}

func (t *gptpay_transaction) checkoutURL(checkout protocol_Map, stripeReady bool) (string, error) {
	if t.cp.Metadata["checkout_ui_mode"] == "custom" {
		return chatgpt_PlatformBase + "/checkout/" + t.cp.Entity + "/" + t.cp.CheckoutID, nil
	}
	if raw := stripe_FindHostedURL(checkout, t.cp.CheckoutID); raw != "" {
		return raw, nil
	}
	if strings.HasPrefix(t.cp.CheckoutID, "cs_live_") {
		if e := t.ensurePublishableKey(checkout); e != nil {
			return "", e
		}
		return t.stripe.ExtractHostedURL(checkout)
	}
	if raw := stripe_FindHostedURL(checkout, t.cp.CheckoutID); raw != "" || stripeReady {
		return t.stripe.HostedCheckoutURL(raw)
	}
	if e := t.initializeStripe(); e != nil {
		return "", e
	}
	return t.stripe.HostedCheckoutURL("")
}

// standaloneHostedURL 尊重调用方的长链要求；上游可能返回 custom 模式的 cs_live 单。
// 只读取同一结算单的完整链接，不拼接 fragment，也不再次创建结算单。
func (t *gptpay_transaction) standaloneHostedURL(checkout protocol_Map) (string, error) {
	if !strings.HasPrefix(t.cp.CheckoutID, "cs_live_") {
		return "", protocol_NewError("checkout_id_invalid")
	}
	if raw := stripe_FindHostedURL(checkout, t.cp.CheckoutID); raw != "" {
		return raw, nil
	}
	if err := t.ensurePublishableKey(checkout); err != nil {
		return "", err
	}
	return t.stripe.ExtractHostedURL(checkout)
}

func (t *gptpay_transaction) ensurePublishableKey(checkout protocol_Map) error {
	if strings.HasPrefix(t.cp.PublishableKey, "pk_") {
		return nil
	}
	if key, ok := checkout["publishable_key"].(string); ok && protocol_Matches(`pk_[A-Za-z0-9_]+`, key) {
		t.cp.PublishableKey = key
		return nil
	}
	bootstrap, e := t.platform.Call("GET", "/backend-api/payments/stripe_client_bootstrap?account_id="+url.QueryEscape(t.cp.AccountID))
	if e != nil {
		return e
	}
	t.cp.PublishableKey = protocol_AsString(protocol_FirstTruthy(bootstrap["publishable_key"], bootstrap["publishableKey"]))
	if !strings.HasPrefix(t.cp.PublishableKey, "pk_") {
		return protocol_NewError("publishable_key_missing")
	}
	return nil
}

func gptpay_minor(v any) *int64 {
	if m, ok := v.(protocol_Map); ok {
		v = m["minorUnitsAmount"]
	}
	n, ok := protocol_Integer(v)
	if !ok || n < 0 {
		return nil
	}
	return &n
}

func gptpay_firstMinor(v ...any) *int64 {
	for _, x := range v {
		if n := gptpay_minor(x); n != nil {
			return n
		}
	}
	return nil
}

func gptpay_extractPrice(data protocol_Map, currency string) (*model_CheckoutPrice, error) {
	nodes := []protocol_Map{data}
	for _, k := range []string{"checkout_session", "checkout_snapshot", "checkout_state"} {
		for _, n := range append([]protocol_Map{}, nodes...) {
			if child, ok := n[k].(protocol_Map); ok {
				nodes = append(nodes, child)
			}
		}
	}
	for _, n := range nodes {
		for _, v := range []any{n["currency"], protocol_AsMap(n["billing_details"])["currency"]} {
			if v != nil {
				c, ok := v.(string)
				if !ok || strings.ToUpper(c) != currency {
					return nil, protocol_NewError("checkout_currency_mismatch")
				}
			}
		}
	}
	for i := len(nodes) - 1; i >= 0; i-- {
		n := nodes[i]
		totals, details, summary, invoice := protocol_AsMap(n["total"]), protocol_AsMap(n["total_details"]), protocol_AsMap(n["total_summary"]), protocol_AsMap(n["invoice"])
		subtotal := gptpay_firstMinor(n["amount_subtotal"], totals["subtotal"])
		total := gptpay_firstMinor(n["amount_total"], totals["total"], summary["due"], summary["total"], n["amount_due"], invoice["amount_due"], summary["amount_due"])
		if subtotal != nil || total != nil {
			return &model_CheckoutPrice{Currency: currency, SubtotalMinor: subtotal, TotalMinor: total, TaxMinor: gptpay_firstMinor(details["amount_tax"], totals["tax"]), DiscountMinor: gptpay_firstMinor(details["amount_discount"], totals["discount"])}, nil
		}
	}
	return nil, nil
}

func (t *gptpay_transaction) readCheckoutPrice(checkout protocol_Map, custom bool, url string) (price *model_CheckoutPrice, err error) {
	started := time.Now()
	t.observer.Emit("price", protocol_Map{"outcome": "started", "phase": t.cp.Phase})
	defer func() {
		outcome := "completed"
		if err != nil {
			outcome = "failed"
		}
		t.observer.Emit("price", protocol_Map{"outcome": outcome, "phase": t.cp.Phase, "error_code": protocol_FlowErrorCode(err), "duration_ms": time.Since(started).Seconds() * 1000})
	}()
	price, err = gptpay_extractPrice(checkout, t.cp.Currency)
	if err != nil || price != nil {
		return
	}
	if !custom {
		// 提链已读取过 init 时先使用同一响应，缺少金额才走完整页面查询。
		price, err = gptpay_extractPrice(t.stripe.ExtractedHostedSnapshot(), t.cp.Currency)
		if err != nil || price != nil {
			return
		}
	}
	var snapshots []protocol_Map
	if custom && strings.HasPrefix(t.cp.CheckoutID, "cs_live_") {
		var m protocol_Map
		m, err = t.stripe.CustomPriceSnapshot(checkout)
		snapshots = []protocol_Map{m}
	} else if custom {
		return nil, protocol_NewError("checkout_price_missing")
	} else {
		if err = t.ensurePublishableKey(checkout); err != nil {
			return nil, err
		}
		snapshots, err = t.stripe.HostedPriceSnapshots(url)
	}
	if err != nil {
		return nil, err
	}
	for _, m := range snapshots {
		price, err = gptpay_extractPrice(m, t.cp.Currency)
		if err != nil || price != nil {
			return
		}
	}
	return nil, protocol_NewError("checkout_price_missing")
}

func (t *gptpay_transaction) loadCurrentSubscription(baseline protocol_Map) (protocol_Map, string, string, error) {
	subs, e := t.platform.Subscriptions(false)
	if protocol_FlowErrorCode(e) == "subscription_not_found" {
		return protocol_Map{}, "", "unknown", nil
	}
	if e != nil {
		return nil, "", "", e
	}
	if pt := strings.TrimSpace(protocol_AsString(subs["plan_type"])); pt != "" {
		baseline["plan_type"] = pt
	}
	if sp := strings.TrimSpace(protocol_AsString(protocol_FirstTruthy(subs["subscription_plan"], subs["plan"]))); sp != "" {
		baseline["subscription_plan"] = sp
	}
	until := protocol_AsString(protocol_FirstTruthy(subs["active_until"], subs["current_period_end"], subs["expires_at"], subs["billing_period_end"]))
	pt := strings.ToLower(protocol_AsString(baseline["plan_type"]))
	if pt != "" && pt != "free" && (protocol_Truthy(until) || subs["will_renew"] == true) {
		baseline["has_active_subscription"] = true
	}
	return subs, until, chatgpt_RenewalState(subs), nil
}

func gptpay_accountPaymentFlow(plan string, baseline protocol_Map) string {
	if gptpay_plusToProliteUpgrade(plan, baseline) {
		return "subscription_update"
	}
	if baseline["has_active_subscription"] == true {
		return "existing_subscription"
	}
	return "checkout"
}

func (t *gptpay_transaction) emitAccountStatus(baseline protocol_Map, until, renewal, flow string) {
	planType := strings.ToLower(strings.TrimSpace(protocol_AsString(baseline["plan_type"])))
	if planType == "" {
		planType = "free"
	}
	if renewal == "" {
		renewal = "unknown"
	}
	fields := protocol_Map{"plan_type": planType, "renewal": renewal, "flow": flow, "outcome": "checked"}
	if ts := gptpay_rfc3339UTC(until); ts != "" {
		fields["active_until"] = ts
	}
	t.observer.Emit("account", fields)
}

func gptpay_rfc3339UTC(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339} {
		if ts, err := time.Parse(layout, raw); err == nil {
			return ts.UTC().Format("2006-01-02T15:04:05Z")
		}
	}
	return ""
}

// ---- pkg/gptpay/flow_custom.go ----

func (t *gptpay_transaction) runCustomFlow(card model_Card, billing model_Billing, checkout protocol_Map) error {
	if strings.HasPrefix(t.cp.CheckoutID, "cs_live_") {
		return t.runCustomStripeCheckout(card, billing, checkout)
	}
	token, e := t.stripe.Token(card, billing, checkout)
	if e != nil {
		return e
	}
	t.cp.ConfirmationToken = token
	return t.runCustomInternalConfirm()
}

func (t *gptpay_transaction) runCustomInternalConfirm() error {
	cp := t.cp
	proof, e := t.sentinel.Token("checkout_session_approval")
	if e != nil {
		return e
	}
	path := "/backend-api/payments/checkout/confirm"
	headers, e := t.platform.ChallengeHeaders(path, proof, chatgpt_PlatformBase+"/checkout/"+cp.Entity+"/"+cp.CheckoutID, false)
	if e != nil {
		return e
	}
	if e = t.boundary("openai_confirm_checkout"); e != nil {
		return e
	}
	data, e := t.platform.Call("POST", path, chatgpt_PlatformCallOptions{Body: protocol_O("checkout_session_id", cp.CheckoutID, "confirm_token", cp.ConfirmationToken, "selected_payment_method_type", "card"), Headers: headers})
	if e != nil {
		return e
	}
	status := strings.ToLower(protocol_AsString(protocol_FirstTruthy(data["status"], data["result"], data["state"])))
	if status == "blocked" {
		t.setPaymentState(model_PaymentDeclined)
		if e = t.save("payment_result"); e != nil {
			return e
		}
		return protocol_NewError("approval_blocked")
	}
	cp.ClientSecret = protocol_AsString(protocol_FirstTruthy(
		data["client_secret"],
		protocol_AsMap(data["payment_intent"])["client_secret"],
		protocol_AsMap(data["stripe_payment_intent"])["client_secret"],
		protocol_AsMap(data["checkout_session"])["client_secret"],
	))
	if !protocol_Matches(`pi_[A-Za-z0-9]+_secret_[A-Za-z0-9]+`, cp.ClientSecret) {
		return protocol_NewError("payment_intent_secret_missing")
	}
	cp.PaymentIntentID = strings.Split(cp.ClientSecret, "_secret_")[0]
	if raw := protocol_AsString(protocol_FirstTruthy(data["confirm_return_url"], cp.ReturnURL)); raw != "" {
		cp.ReturnURL = raw
	}
	if e = t.save("openai_confirmed"); e != nil {
		return e
	}
	if _, e = chatgpt_ValidateReturnURL(cp.ReturnURL, cp); e != nil {
		return e
	}
	ready, e := t.needsClientConfirmation()
	if e != nil {
		return e
	}
	if !ready {
		return t.save("payment_result")
	}
	cp.ConfirmBody = t.stripe.ConfirmForm()
	if e = t.boundary("stripe_confirm"); e != nil {
		return e
	}
	data, e = t.stripe.Confirm()
	if e != nil {
		return e
	}
	t.observePayment(data)
	return t.save("payment_result")
}

func (t *gptpay_transaction) runCustomStripeCheckout(card model_Card, billing model_Billing, checkout protocol_Map) error {
	cp := t.cp
	init, e := t.stripe.CustomPriceSnapshot(checkout)
	if e != nil {
		return e
	}
	if protocol_AsString(init["init_checksum"]) == "" {
		init, e = t.stripe.CustomPage()
		if e != nil {
			return e
		}
	}
	if e = t.stripe.BindCheckoutElements(checkout); e != nil {
		return e
	}
	updated, e := t.stripe.UpdateCustomCheckout(billing, init)
	if e != nil {
		return e
	}
	stripe_MergeCustomPaymentPage(init, updated)
	if e = t.stripe.CreateRadarSession(); e != nil {
		t.observer.Emit("radar_session", protocol_Map{"phase": t.cp.Phase, "outcome": "failed", "error_code": protocol_FlowErrorCode(e)})
	} else {
		t.observer.Emit("radar_session", protocol_Map{"phase": t.cp.Phase, "outcome": "succeeded"})
	}
	token, e := t.stripe.Token(card, billing, checkout)
	if e != nil {
		return e
	}
	cp.ConfirmationToken = token
	if cp.ReturnURL == "" {
		cp.ReturnURL = chatgpt_PlatformBase + "/checkout/verify?stripe_session_id=" + url.QueryEscape(cp.CheckoutID) + "&processor_entity=" + url.QueryEscape(cp.Entity) + "&plan_type=" + model_HostedPlanType(cp.Plan)
	}
	if _, e = chatgpt_ValidateReturnURL(cp.ReturnURL, cp); e != nil {
		return e
	}
	form, e := t.stripe.CustomCheckoutForm(init, "")
	if e != nil {
		return e
	}
	cp.ConfirmBody = form
	if e = t.boundary("openai_confirm_checkout"); e != nil {
		return e
	}
	data, e := t.stripe.ConfirmCheckoutSession()
	if e != nil {
		return e
	}
	t.observePaymentDiagnostics(data)
	errObj := protocol_AsMap(data["error"])
	t.observer.Emit("payment_confirm", protocol_Map{"phase": cp.Phase, "outcome": "submitted", "intent_status": protocol_AsString(protocol_FirstTruthy(data["payment_status"], data["status"])), "checkout_status": protocol_AsString(data["status"]), "payment_status": protocol_AsString(data["payment_status"]), "page_id_kind": gptpay_paymentPageIDKind(data), "has_payment_intent": gptpay_hasPaymentIntent(data), "provider_code": protocol_AsString(errObj["code"]), "provider_param": protocol_AsString(errObj["param"])})
	if code := protocol_AsString(errObj["code"]); code != "" {
		page, pageErr := t.stripe.CustomPage()
		if pageErr == nil {
			data = page
			t.observePayment(data)
		}
		if e = t.save("payment_result"); e != nil {
			return e
		}
		return protocol_NewError(code)
	}
	data, e = t.pollCustomPayment(data)
	if e != nil {
		return e
	}
	submission := protocol_AsMap(data["submission_attempt"])
	if protocol_FirstTruthy(submission["state"], submission["status"]) == "requires_approval" || protocol_FirstTruthy(submission["state"], submission["status"]) == "requires_action" {
		if e = t.snapshotHostedBilling(billing); e != nil {
			return e
		}
	}
	data, e = t.approveCustomCheckout(data)
	if e != nil {
		return e
	}
	// 先保存资金证据，再报告提交失败原因，不能把税务错误隐藏成通用未扣款。
	t.observePayment(data)
	if e = t.save("payment_result"); e != nil {
		return e
	}
	return gptpay_customSubmissionError(data)
}

func (t *gptpay_transaction) pollCustomPayment(data protocol_Map) (protocol_Map, error) {
	var e error
	for i := 0; i < 8; i++ {
		state := stripe_Classify(data)
		submission := protocol_AsMap(data["submission_attempt"])
		if state == model_PaymentCharged || state == model_PaymentActionRequired || state == model_PaymentDeclined || protocol_FirstTruthy(submission["state"], submission["status"]) == "requires_approval" {
			break
		}
		if e = t.stripeHTTP.Pause(1.2); e != nil {
			return nil, e
		}
		data, e = t.stripe.CustomPage()
		if e != nil {
			return nil, e
		}
	}
	return data, nil
}

func (t *gptpay_transaction) approveCustomCheckout(data protocol_Map) (protocol_Map, error) {
	cp := t.cp
	submission := protocol_AsMap(data["submission_attempt"])
	state := protocol_FirstTruthy(submission["state"], submission["status"])
	if state != "requires_approval" && state != "requires_action" {
		return data, nil
	}
	path := "/backend-api/payments/checkout/approve"
	token, e := t.sentinel.Token("checkout_session_approval")
	if e != nil {
		return nil, e
	}
	headers, e := t.platform.ChallengeHeaders(path, token, cp.ReturnURL, true)
	if e != nil {
		return nil, e
	}
	// 批准会推进原付款；挑战准备完成后再次同步持久化，校验调用方当前执行权。
	// 沿用原阶段，避免把待批准快照变为另一笔交易或不可恢复的阶段。
	if e = t.save(""); e != nil {
		return nil, e
	}
	reply, e := t.platform.Call("POST", path, chatgpt_PlatformCallOptions{Body: protocol_O("checkout_session_id", cp.CheckoutID, "processor_entity", cp.Entity), Headers: headers})
	if e != nil {
		return nil, e
	}
	if protocol_FirstTruthy(reply["result"], reply["status"], reply["state"]) == "blocked" {
		return nil, protocol_NewError("approval_blocked")
	}
	data, e = t.stripe.CustomPage()
	if e != nil {
		return nil, e
	}
	return t.pollCustomPaymentAfterApprove(data)
}

func (t *gptpay_transaction) needsClientConfirmation() (bool, error) {
	data, e := t.stripe.Retrieve()
	if e != nil {
		return false, e
	}
	if protocol_Truthy(data["error"]) || data["id"] != t.cp.PaymentIntentID {
		return false, protocol_NewError("payment_intent_state_unavailable")
	}
	status, ok := data["status"].(string)
	if !ok || !protocol_StringSet("requires_payment_method", "requires_confirmation", "requires_action", "processing", "requires_capture", "canceled", "succeeded")[status] {
		return false, protocol_NewError("payment_intent_state_unavailable")
	}
	t.observePayment(data)
	last, hasLast := data["last_payment_error"]
	pm, hasPM := data["payment_method"]
	// 同名 requires_payment_method 也可能来自既往拒绝，必须核对字段存在且明确为 null。
	initial := status == "requires_payment_method" && hasLast && last == nil && hasPM && pm == nil
	ready := (status == "requires_confirmation" || initial) && data["confirmation_method"] == "automatic" && last == nil && data["latest_charge"] == nil && data["next_action"] == nil
	outcome := "skipped"
	if ready {
		outcome = "ready"
	}
	t.observer.Emit("payment_confirm", protocol_Map{"phase": t.cp.Phase, "outcome": outcome, "intent_status": status, "payment_state": t.cp.Payment})
	return ready, nil
}

func gptpay_paymentPageIDKind(data protocol_Map) string {
	id := protocol_AsString(data["id"])
	switch {
	case strings.HasPrefix(id, "ppage_"):
		return "ppage"
	case strings.HasPrefix(id, "cs_"):
		return "cs"
	case id == "":
		return "missing"
	default:
		return "other"
	}
}

func gptpay_hasPaymentIntent(data protocol_Map) bool {
	switch raw := data["payment_intent"].(type) {
	case string:
		return strings.HasPrefix(raw, "pi_") && !strings.Contains(raw, "_secret_")
	default:
		id := protocol_AsString(protocol_AsMap(raw)["id"])
		return strings.HasPrefix(id, "pi_")
	}
}

func (t *gptpay_transaction) pollCustomPaymentAfterApprove(data protocol_Map) (protocol_Map, error) {
	var e error
	for i := 0; i < 10; i++ {
		state := stripe_Classify(data)
		if state == model_PaymentCharged || state == model_PaymentActionRequired || state == model_PaymentDeclined {
			return data, nil
		}
		if e = t.stripeHTTP.Pause(1.5); e != nil {
			return nil, e
		}
		data, e = t.stripe.CustomPage()
		if e != nil {
			return nil, e
		}
	}
	return data, nil
}

// ---- pkg/gptpay/flow_hosted.go ----

func (t *gptpay_transaction) runHostedFlow(card model_Card, billing model_Billing, info protocol_Map) error {
	cp := t.cp
	cp.StripeVersion = stripe_HostedVersion
	cp.Metadata["hosted_url"] = t.stripe.HostedURL
	form, e := t.stripe.HostedForm(card, billing, info)
	if e != nil {
		return e
	}
	cp.ConfirmBody = form
	if e = t.boundary("hosted_confirm"); e != nil {
		return e
	}
	data, e := t.stripe.Call("POST", "/payment_pages/"+cp.CheckoutID+"/confirm", stripe_StripeCallOptions{Form: form, Hosted: true, AllowError: true})
	if e != nil {
		return e
	}
	data, e = t.pollHostedPayment(data)
	if e != nil {
		return e
	}
	t.observePayment(data)
	// 保存扣款快照前记录回跳地址，以便恢复辅助流程。
	landing, callback, state := t.selectHostedLanding(data)
	chargedSaved := cp.Payment == "charged"
	if chargedSaved {
		// 明确扣款后先保存可恢复的资金结果，再发辅助请求。
		if e = t.save("payment_result"); e != nil {
			return e
		}
	}
	if e = t.snapshotHostedBilling(billing); e != nil {
		return e
	}
	data, e = t.approveHostedPayment(data, landing, callback, state)
	if e != nil {
		return e
	}
	if chargedSaved {
		return nil
	}
	return t.save("payment_result")
}

func (t *gptpay_transaction) absorbHostedIntent(data protocol_Map) {
	raw := data["payment_intent"]
	if id := protocol_AsString(raw); strings.HasPrefix(id, "pi_") && !strings.Contains(id, "_secret_") {
		t.cp.PaymentIntentID = id
		return
	}
	pi := protocol_AsMap(raw)
	if id := protocol_AsString(pi["id"]); strings.HasPrefix(id, "pi_") {
		t.cp.PaymentIntentID = id
	}
	if secret := protocol_AsString(pi["client_secret"]); strings.Contains(secret, "_secret_") {
		t.cp.ClientSecret = secret
	}
}

func (t *gptpay_transaction) pollHostedPayment(data protocol_Map) (protocol_Map, error) {
	var e error
	for i := 0; i < 3; i++ {
		state := stripe_Classify(data)
		if state == model_PaymentCharged || state == model_PaymentActionRequired || state == model_PaymentDeclined {
			break
		}
		if e = t.stripeHTTP.Pause(1.2); e != nil {
			return nil, e
		}
		data, e = t.stripe.Page()
		if e != nil {
			return nil, e
		}
	}
	return data, nil
}

// 账单快照仅用于辅助同步；协议错误不能抹掉已经取得的资金状态。
func (t *gptpay_transaction) snapshotHostedBilling(billing model_Billing) error {
	_, e := t.platform.Call("POST", "/backend-api/payments/checkout/snapshot", chatgpt_PlatformCallOptions{Body: protocol_O("snapshot", protocol_O("billing_address", protocol_O("name", billing.Name, "address", billing.Address())))})
	if e != nil {
		var pe *protocol_ProtocolError
		if !errors.As(e, &pe) {
			return e
		}
	}
	return nil
}

func (t *gptpay_transaction) selectHostedLanding(data protocol_Map) (string, bool, any) {
	cp := t.cp
	submission := protocol_AsMap(data["submission_attempt"])
	state := protocol_FirstTruthy(submission["state"], submission["status"])
	cp.ReturnURL = chatgpt_PlatformBase + "/checkout/verify?stripe_session_id=" + url.QueryEscape(cp.CheckoutID) + "&processor_entity=" + url.QueryEscape(cp.Entity) + "&plan_type=" + model_HostedPlanType(cp.Plan)
	landing := cp.ReturnURL
	callback := false
	completion := protocol_AsMap(data["after_completion"])
	redirect := protocol_AsMap(completion["redirect"])
	for _, v := range []any{data["success_url"], data["return_url"], data["url"], redirect["url"], completion["url"]} {
		candidate := protocol_AsString(v)
		u, e := url.Parse(candidate)
		if e != nil {
			continue
		}
		q := u.Query()["stripe_session_id"]
		if u.Scheme == "https" && u.Host == "chatgpt.com" && u.User == nil && (len(q) == 1 && q[0] == cp.CheckoutID || u.Path == "/checkout/"+cp.Entity+"/"+cp.CheckoutID) {
			landing = candidate
			callback = true
			break
		}
	}
	return landing, callback, state
}

func (t *gptpay_transaction) approveHostedPayment(data protocol_Map, landing string, callback bool, state any) (protocol_Map, error) {
	cp := t.cp
	if !protocol_StringSet("charged", "action_required", "not_charged")[cp.Payment] && (callback || state == "requires_approval" || state == "requires_action") {
		path := "/backend-api/payments/checkout/approve"
		token, e := t.sentinel.Token("checkout_session_approval")
		if e != nil {
			return nil, e
		}
		headers, e := t.platform.ChallengeHeaders(path, token, landing, true)
		if e != nil {
			return nil, e
		}
		if e = t.save("hosted_approve"); e != nil {
			return nil, e
		}
		reply, e := t.platform.Call("POST", path, chatgpt_PlatformCallOptions{Body: protocol_O("checkout_session_id", cp.CheckoutID, "processor_entity", cp.Entity), Headers: headers})
		if e != nil {
			return nil, e
		}
		if protocol_FirstTruthy(reply["result"], reply["status"], reply["state"]) == "blocked" {
			return nil, protocol_NewError("approval_blocked")
		}
		data, e = t.stripe.Page()
		if e != nil {
			return nil, e
		}
		t.observePayment(data)
	}
	return data, nil
}

// ---- pkg/gptpay/flow_recovery.go ----

func (t *gptpay_transaction) finishPayment() error {
	if e := t.confirmActivation(); e != nil {
		return e
	}
	if !t.cp.CancelRequested {
		return t.save("completed")
	}
	return t.disableRenewal()
}

func (t *gptpay_transaction) confirmActivation() error {
	cp := t.cp
	if e := t.save("activation"); e != nil {
		return e
	}
	if cp.ReturnURL != "" {
		u, e := chatgpt_ValidateReturnURL(cp.ReturnURL, cp)
		if e == nil {
			if _, e = t.http.Request("GET", u, protocol_RequestOptions{Headers: t.platform.Headers("")}); e != nil {
				return e
			}
		} else if protocol_FlowErrorCode(e) != "invalid_return_url" {
			return e
		}
	}
	for i := 0; i < 5; i++ {
		if i > 0 {
			if e := t.http.Pause(2); e != nil {
				return e
			}
		}
		data, e := t.platform.Subscriptions(false)
		if protocol_FlowErrorCode(e) == "http_401" {
			if e = t.platform.Refresh(); e != nil {
				return e
			}
			if e = t.save(""); e != nil {
				return e
			}
			data, e = t.platform.Subscriptions(false)
		}
		// 已扣款后的特定“尚无订阅”404属于到账延迟，继续有限轮询。
		if protocol_FlowErrorCode(e) == "subscription_not_found" && cp.Payment == "charged" {
			continue
		}
		if e != nil {
			return e
		}
		if chatgpt_SubscriptionActive(data, cp.Plan) {
			cp.Activation = "active"
			cp.Renewal = chatgpt_RenewalState(data)
			break
		}
	}
	if e := t.save(""); e != nil {
		return e
	}
	if cp.Activation != "active" {
		return protocol_NewError("activation_pending")
	}
	return nil
}

func (t *gptpay_transaction) disableRenewal() error {
	cp := t.cp
	t.progress.emit(model_ProgressStageCancellation, model_ProgressRunning, "")
	if e := t.platform.Refresh(); e != nil {
		return e
	}
	if e := t.save("cancellation"); e != nil {
		return e
	}
	data, e := t.platform.Subscriptions(true)
	if e != nil {
		return e
	}
	cp.Renewal = chatgpt_RenewalState(data)
	if cp.Renewal == "unknown" {
		return protocol_NewError("renewal_state_unknown")
	}
	if cp.Renewal == "off" {
		return t.save("completed")
	}
	for i := 0; i < 2; i++ {
		if e = t.save("cancellation_requested"); e != nil {
			return e
		}
		_, e = t.platform.Call("POST", "/backend-api/subscriptions/cancel", chatgpt_PlatformCallOptions{Body: protocol_O("account_id", cp.AccountID), Mode: "renewal"})
		if e == nil {
			break
		}
		original := e
		data, e = t.platform.Subscriptions(true)
		if e != nil {
			return e
		}
		cp.Renewal = chatgpt_RenewalState(data)
		if cp.Renewal == "off" {
			return t.save("completed")
		}
		if i > 0 || cp.Renewal != "on" || !protocol_StringSet("network_error", "http_408", "http_429", "http_500", "http_502", "http_503", "http_504")[protocol_FlowErrorCode(original)] {
			return original
		}
	}
	for i := 0; i < 5; i++ {
		if i > 0 {
			if e = t.http.Pause(2); e != nil {
				return e
			}
		}
		data, e = t.platform.Subscriptions(true)
		if e != nil {
			return e
		}
		cp.Renewal = chatgpt_RenewalState(data)
		if cp.Renewal == "off" {
			return t.save("completed")
		}
	}
	return protocol_NewError("cancellation_pending")
}

func (t *gptpay_transaction) recoverPayment() error {
	cp := t.cp
	if cp.Payment == "charged" && cp.ChargedPrice() == nil && cp.PaymentIntentID != "" && cp.ClientSecret != "" {
		// 已知扣款仍可补齐原单实收，失败只影响收尾，不重放付款或降低资金状态。
		data, err := t.stripe.Retrieve()
		if err != nil {
			return err
		}
		if data["id"] != cp.PaymentIntentID {
			return protocol_NewError("payment_intent_state_unavailable")
		}
		t.recordChargedPrice(data)
		if cp.ChargedPrice() != nil {
			if err = t.save(""); err != nil {
				return err
			}
		}
	}
	if protocol_AsString(cp.Metadata["payment_flow"]) == "subscription_update" {
		return t.recoverSubscriptionUpdate()
	}
	if cp.Payment == "charged" {
		t.progress.emit(model_ProgressStagePayment, model_ProgressSucceeded, "")
	} else {
		// 恢复中的 payment 仅表示核验原交易，不会重新提交付款。
		t.progress.emit(model_ProgressStagePayment, model_ProgressRunning, "")
	}
	if model_IsPreSubmitPhase(cp.Phase) {
		return protocol_NewError("payment_not_submitted")
	}
	custom := cp.Metadata["checkout_ui_mode"] == "custom"
	if !custom {
		t.stripe.HostedURL = protocol_AsString(cp.Metadata["hosted_url"])
	}
	if cp.Payment != "charged" {
		if cp.PaymentIntentID == "" && custom && !strings.HasPrefix(cp.CheckoutID, "cs_live_") {
			data, e := t.platform.Call("GET", "/backend-api/payments/checkout/"+cp.CheckoutID)
			if e != nil {
				return e
			}
			secret := protocol_AsString(protocol_FirstTruthy(data["client_secret"], protocol_AsMap(data["checkout_session"])["client_secret"]))
			if !protocol_Matches(`pi_[A-Za-z0-9]+_secret_[A-Za-z0-9]+`, secret) {
				return protocol_NewError("payment_pending")
			}
			cp.ClientSecret = secret
			cp.PaymentIntentID = strings.Split(secret, "_secret_")[0]
		}
		data, e := t.stripe.Retrieve()
		if e != nil {
			return e
		}
		if cp.PaymentIntentID != "" && cp.ClientSecret != "" && data["id"] != cp.PaymentIntentID {
			return protocol_NewError("payment_intent_state_unavailable")
		}
		t.observePayment(data)
		// 仅恢复 Custom cs_live 原单的待批准步骤，不重发付款确认。
		// 使用最新查询状态；审批方法会在写请求前再次持久化检查点。
		submission := protocol_AsMap(data["submission_attempt"])
		if custom && strings.HasPrefix(cp.CheckoutID, "cs_live_") && cp.Payment == "unknown" && protocol_FirstTruthy(submission["state"], submission["status"]) == "requires_approval" {
			data, e = t.approveCustomCheckout(data)
			if e != nil {
				return e
			}
			t.observePayment(data)
		}
		if e = t.save("payment_result"); e != nil {
			return e
		}
	}
	if cp.Payment == "charged" {
		return t.finishPayment()
	}
	// 已确认未扣款和需验证不是处理中；仅修正错误分类，不重发交易。
	switch model_PaymentState(cp.Payment) {
	case model_PaymentDeclined:
		return protocol_NewError("not_charged")
	case model_PaymentActionRequired:
		return protocol_NewError("action_required")
	case model_PaymentAuthorized:
		return protocol_NewError("authorized")
	}
	return protocol_NewError("payment_pending")
}

func (t *gptpay_transaction) cancelRenewalOnly() (result model_Result) {
	defer t.close()
	defer func() {
		if recover() != nil {
			result = t.complete(protocol_NewError("protocol_error"))
		}
	}()
	return t.complete(t.runCancelRenewalOnly())
}

func (t *gptpay_transaction) runCancelRenewalOnly() error {
	t.progress.emit(model_ProgressStageSession, model_ProgressRunning, "")
	if e := t.save("initializing"); e != nil {
		return e
	}
	if t.cp.AccountID == "" || t.cp.Email == "" {
		if e := t.platform.Refresh(); e != nil {
			return e
		}
	}
	if e := t.platform.Homepage(); e != nil {
		return e
	}
	account, e := t.platform.Account()
	if protocol_FlowErrorCode(e) == "http_401" {
		if e = t.platform.Refresh(); e == nil {
			account, e = t.platform.Account()
		}
	}
	if e != nil {
		return e
	}
	ent := protocol_AsMap(account["entitlement"])
	if ent["has_active_subscription"] != true {
		return protocol_NewError("subscription_not_found")
	}
	t.cp.Activation = "active"
	t.progress.emit(model_ProgressStageSession, model_ProgressSucceeded, "")
	t.progress.emit(model_ProgressStageCancellation, model_ProgressRunning, "")
	return t.disableRenewal()
}

// ---- pkg/gptpay/flow_upgrade.go ----

func gptpay_plusToProliteUpgrade(plan string, baseline protocol_Map) bool {
	if plan != "pro5x" || baseline["has_active_subscription"] != true {
		return false
	}
	return model_MatchesSubscriptionType("plus", protocol_AsString(baseline["plan_type"])) || model_MatchesSubscriptionType("plus", protocol_AsString(baseline["subscription_plan"]))
}

func (t *gptpay_transaction) runSubscriptionUpdate(card *model_Card, billing model_Billing, expected *int64, approval model_PaymentApproval) error {
	if card == nil {
		if e := t.requireSavedPaymentMethod(); e != nil {
			return e
		}
	}
	price, e := t.readUpgradePrice()
	if e != nil {
		return e
	}
	if price == nil || price.TotalMinor == nil {
		return protocol_NewError("checkout_price_missing")
	}
	if e = t.pricing(*price.TotalMinor, price.Currency, expected, model_PriceSourceSubscriptionProration); e != nil {
		return e
	}
	if e = t.save("upgrade_priced"); e != nil {
		return e
	}
	t.progress.emit(model_ProgressStageCheckout, model_ProgressSucceeded, "")
	if card == nil {
		return nil
	}
	t.progress.emit(model_ProgressStagePayment, model_ProgressRunning, "")
	if approval != nil {
		if e = t.ctx.Err(); e != nil {
			return e
		}
		ok, e := gptpay_callApproval(approval, *t.cp.Amount, t.cp.Currency)
		if e != nil {
			return protocol_NewError("payment_approval_failed")
		}
		if !ok {
			return protocol_NewError("payment_cancelled")
		}
		if e = t.ctx.Err(); e != nil {
			return e
		}
	}
	// 报价或金额审批拒绝不得改变订阅续费状态。
	if e = t.ensurePlusRenewal(); e != nil {
		return e
	}
	pmID, e := t.bindUpgradePaymentMethod(*card, billing)
	if e != nil {
		return e
	}
	if e = t.boundary("subscription_update"); e != nil {
		return e
	}
	data, e := t.platform.UpdateSubscription(protocol_AsString(t.cp.Metadata["updated_plan"]), pmID, "")
	if e != nil {
		if protocol_FlowErrorCode(e) == "http_400" {
			t.setPaymentState(model_PaymentDeclined)
			if se := t.save("payment_result"); se != nil {
				return se
			}
			return protocol_NewError("not_charged")
		}
		return e
	}
	state, pi := gptpay_interpretSubscriptionUpdate(data)
	var received protocol_Map
	var intentErr error
	if pi != "" {
		t.cp.PaymentIntentID = pi
	}
	if secret := gptpay_upgradeClientSecret(data); secret != "" {
		t.cp.ClientSecret = secret
	}
	if t.cp.PaymentIntentID != "" && t.cp.ClientSecret != "" {
		if ver := t.stripe.PortalAPIVersion(); ver != "" {
			t.cp.StripeVersion = ver
		}
		if retrieved, err := t.stripe.Retrieve(); err != nil {
			intentErr = err
		} else if retrieved["id"] != t.cp.PaymentIntentID || protocol_Truthy(retrieved["error"]) {
			intentErr = protocol_NewError("payment_intent_state_unavailable")
		} else {
			received = retrieved
			if classified := stripe_Classify(retrieved); classified != model_PaymentUnknown {
				state = classified
			}
			errData := protocol_AsMap(protocol_FirstTruthy(retrieved["last_payment_error"], retrieved["error"]))
			t.observer.Emit("payment_error", protocol_Map{"service": "stripe", "path": "/v1/payment_intents", "provider_code": protocol_FirstTruthy(retrieved["status"], errData["code"]), "decline_code": protocol_FirstTruthy(errData["decline_code"], errData["code"], data["payment_method_failure_reason"])})
		}
	} else if state == model_PaymentDeclined {
		errData := protocol_AsMap(protocol_FirstTruthy(data["last_payment_error"], data["error"]))
		t.observer.Emit("payment_error", protocol_Map{"service": "chatgpt", "path": "/backend-api/subscriptions/update", "provider_code": protocol_FirstTruthy(data["status"], errData["code"]), "decline_code": protocol_FirstTruthy(gptpay_upgradeFailureCode(data), errData["decline_code"], errData["code"])})
	}
	if state == "" {
		state = model_PaymentPending
	}
	t.setPaymentState(state)
	t.recordChargedPrice(received)
	if e = t.save("payment_result"); e != nil {
		return e
	}
	if intentErr != nil {
		return intentErr
	}
	if state == model_PaymentDeclined && pi != "" {
		if _, e = t.platform.CancelPendingUpdate(pi); e != nil && protocol_FlowErrorCode(e) != "http_404" {
			t.observer.Emit("phase", protocol_Map{"phase": t.cp.Phase, "outcome": "failed", "error_code": protocol_FlowErrorCode(e)})
		}
	}
	if t.cp.Payment == "charged" {
		return t.finishPayment()
	}
	return protocol_NewError(string(state))
}

func (t *gptpay_transaction) ensurePlusRenewal() error {
	data, e := t.platform.Subscriptions(false)
	if e != nil {
		return e
	}
	if chatgpt_RenewalState(data) != "off" {
		return nil
	}
	if e = t.save(""); e != nil {
		return e
	}
	_, e = t.platform.RenewSubscription()
	return e
}

func (t *gptpay_transaction) requireSavedPaymentMethod() error {
	_, e := t.bindUpgradePaymentMethod(model_Card{}, model_Billing{})
	return e
}

func (t *gptpay_transaction) bindUpgradePaymentMethod(card model_Card, billing model_Billing) (string, error) {
	data, e := t.platform.PaymentMethods()
	if e != nil {
		return "", e
	}
	methods, _ := data["payment_methods"].([]any)
	defaultID := protocol_AsString(data["default_payment_method_id"])
	last4 := ""
	if n := card.Number; len(n) >= 4 {
		last4 = n[len(n)-4:]
	}
	if last4 != "" {
		// 传入完整卡时重新走 portal 挂载，让 Stripe.js 指纹和 Payment Element 会话落在新 pm_ 上。
		pmID, asDefault, e := t.attachUpgradeCard(card, billing)
		if e != nil {
			return "", e
		}
		t.cp.Metadata["payment_method_id"] = pmID
		if asDefault {
			return "", nil
		}
		return pmID, nil
	}
	if defaultID == "" || len(methods) == 0 {
		return "", protocol_NewError("payment_method_missing")
	}
	t.cp.Metadata["payment_method_id"] = defaultID
	return "", nil
}

func (t *gptpay_transaction) attachUpgradeCard(card model_Card, billing model_Billing) (string, bool, error) {
	raw, e := t.platform.CustomerPortal()
	if e != nil {
		return "", false, e
	}
	if e = t.stripe.LoadPortal(raw); e != nil {
		return "", false, e
	}
	if e = t.initializeStripe(); e != nil {
		return "", false, e
	}
	seti, secret, e := t.stripe.CreatePortalSetupIntent()
	if e != nil {
		return "", false, e
	}
	t.cp.Metadata["setup_intent_id"] = seti
	if e = t.stripe.BindPortalElements(secret); e != nil {
		return "", false, e
	}
	token, e := t.stripe.PortalConfirmationToken(card, billing)
	if e != nil {
		return "", false, e
	}
	pmID, e := t.stripe.ConfirmPortalSetupIntent(seti, secret, "", token)
	if e != nil {
		return "", false, e
	}
	if e = t.stripe.SetPortalDefaultPaymentMethod(pmID); e != nil {
		t.observer.Emit("phase", protocol_Map{"phase": t.cp.Phase, "outcome": "failed", "error_code": protocol_FlowErrorCode(e)})
		return pmID, false, nil
	}
	// Stripe 默认不等于 ChatGPT 已保存卡；不登记的话 update 仍可能扣旧 pm_。
	if _, e = t.platform.AttachPaymentMethod(pmID); e != nil {
		t.observer.Emit("phase", protocol_Map{"phase": t.cp.Phase, "outcome": "failed", "error_code": protocol_FlowErrorCode(e)})
		return pmID, false, nil
	}
	return pmID, true, nil
}

func (t *gptpay_transaction) readUpgradePrice() (*model_CheckoutPrice, error) {
	subs, e := t.platform.Subscriptions(false)
	if e != nil {
		return nil, e
	}
	cfg, e := t.platform.PricingConfig(t.cp.Country)
	if e != nil {
		return nil, e
	}
	return gptpay_estimateUpgradePrice(cfg, subs, t.cp.Currency, time.Now())
}

func gptpay_interpretSubscriptionUpdate(data protocol_Map) (model_PaymentState, string) {
	pi := protocol_AsString(protocol_FirstTruthy(data["pending_update_payment_intent_id"], data["payment_intent_id"]))
	status := strings.ToLower(protocol_AsString(data["status"]))
	reason := gptpay_upgradeFailureCode(data)
	decline := reason
	if len(protocol_AsMap(data["next_action"])) > 0 || len(protocol_AsMap(protocol_AsMap(data["payment_intent"])["next_action"])) > 0 {
		return model_PaymentActionRequired, pi
	}
	switch {
	case status == "requires_action" || status == "action_required" || decline == "authentication_required" || strings.Contains(reason, "authenticat"):
		return model_PaymentActionRequired, pi
	case status == "payment_method_failed" || strings.Contains(reason, "declined") || decline != "":
		return model_PaymentDeclined, pi
	case protocol_StringSet("success", "ok", "completed", "complete", "updated", "paid", "charged", "active", "succeeded")[status]:
		return model_PaymentCharged, pi
	default:
		return "", pi
	}
}

func gptpay_upgradeFailureCode(data protocol_Map) string {
	nested := []protocol_Map{protocol_AsMap(data["payment_method_failure_reason"]), protocol_AsMap(data["last_payment_error"]), protocol_AsMap(data["error"]), protocol_AsMap(data["payment_intent"])}
	values := []any{data["payment_method_failure_reason"], data["decline_code"], data["failure_code"]}
	for _, node := range nested {
		values = append(values, node["decline_code"], node["code"], node["reason"], node["failure_code"])
	}
	for _, v := range values {
		if _, isMap := v.(protocol_Map); isMap {
			continue
		}
		s := strings.ToLower(strings.TrimSpace(protocol_AsString(v)))
		if s == "" || strings.HasPrefix(s, "map[") {
			continue
		}
		return s
	}
	return ""
}

func gptpay_upgradeClientSecret(data protocol_Map) string {
	for _, v := range []any{data["client_secret"], data["pending_update_client_secret"], protocol_AsMap(data["payment_intent"])["client_secret"], protocol_AsMap(data["pending_update"])["client_secret"], protocol_AsMap(data["stripe_payment_intent"])["client_secret"]} {
		s := protocol_AsString(v)
		if strings.Contains(s, "_secret_") {
			return s
		}
	}
	return ""
}

func gptpay_estimateUpgradePrice(cfg, subs protocol_Map, currency string, now time.Time) (*model_CheckoutPrice, error) {
	currency = strings.ToUpper(currency)
	node := protocol_AsMap(cfg["currency_config"])
	if code := strings.ToUpper(protocol_AsString(protocol_FirstTruthy(cfg["symbol_code"], node["symbol_code"]))); code != "" && code != currency {
		return nil, protocol_NewError("checkout_currency_mismatch")
	}
	exp := 2
	if n, ok := protocol_Integer(protocol_FirstTruthy(cfg["minor_unit_exponent"], node["minor_unit_exponent"])); ok {
		if n < 0 || n > 4 {
			return nil, protocol_NewError("checkout_price_missing")
		}
		exp = int(n)
	}
	prolite, ok := gptpay_monthAmountMinor(node["prolite"], exp)
	if !ok {
		return nil, protocol_NewError("checkout_price_missing")
	}
	plus, _ := gptpay_monthAmountMinor(node["plus"], exp)
	start := gptpay_parseTime(subs["active_start"])
	until := gptpay_parseTime(protocol_FirstTruthy(subs["active_until"], subs["current_period_end"], subs["expires_at"], subs["billing_period_end"]))
	credit := gptpay_prorateCredit(plus, start, until, now)
	due := prolite - credit
	if due < 0 {
		due = 0
	}
	return &model_CheckoutPrice{Currency: currency, SubtotalMinor: &prolite, TotalMinor: &due, DiscountMinor: &credit}, nil
}

func gptpay_monthAmountMinor(plan any, exp int) (int64, bool) {
	month := protocol_AsMap(protocol_AsMap(plan)["month"])
	override := protocol_AsMap(month["psp_override"])
	return gptpay_toMinor(protocol_FirstTruthy(override["amount"], month["amount"]), exp)
}

func gptpay_toMinor(v any, exp int) (int64, bool) {
	f, ok := gptpay_jsonFloat(v)
	if !ok || f < 0 || math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, false
	}
	n := math.Round(f * math.Pow10(exp))
	// MaxInt64 转成 float64 会向上舍入为 2^63，等于此边界也会溢出。
	if n >= float64(math.MaxInt64) {
		return 0, false
	}
	return int64(n), true
}

func gptpay_jsonFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case int32:
		return float64(x), true
	}
	if n, ok := protocol_Integer(v); ok {
		return float64(n), true
	}
	return 0, false
}

func gptpay_parseTime(v any) time.Time {
	s := protocol_AsString(v)
	if s == "" {
		return time.Time{}
	}
	for _, layout := range []string{time.RFC3339, time.RFC3339Nano} {
		if ts, err := time.Parse(layout, s); err == nil {
			return ts
		}
	}
	return time.Time{}
}

func gptpay_prorateCredit(plusMinor int64, start, until, now time.Time) int64 {
	if plusMinor <= 0 || until.IsZero() {
		return 0
	}
	if now.After(until) || now.Equal(until) {
		return 0
	}
	if start.IsZero() || !until.After(start) {
		return 0
	}
	remain := until.Sub(now)
	period := until.Sub(start)
	if remain >= period {
		return plusMinor
	}
	return int64(math.Round(float64(plusMinor) * float64(remain) / float64(period)))
}

func (t *gptpay_transaction) recoverSubscriptionUpdate() error {
	cp := t.cp
	if cp.Payment == "charged" {
		t.progress.emit(model_ProgressStagePayment, model_ProgressSucceeded, "")
		return t.finishPayment()
	}
	t.progress.emit(model_ProgressStagePayment, model_ProgressRunning, "")
	if model_IsPreSubmitPhase(cp.Phase) {
		return protocol_NewError("payment_not_submitted")
	}
	if cp.PaymentIntentID != "" && cp.ClientSecret != "" {
		data, err := t.stripe.Retrieve()
		if err != nil {
			return err
		}
		if data["id"] != cp.PaymentIntentID || protocol_Truthy(data["error"]) {
			return protocol_NewError("payment_intent_state_unavailable")
		}
		t.observePayment(data)
		if err = t.save("payment_result"); err != nil {
			return err
		}
		if cp.Payment == "charged" {
			return t.finishPayment()
		}
		if cp.Payment == "not_charged" {
			return protocol_NewError("not_charged")
		}
		return protocol_NewError("payment_pending")
	}
	if e := t.platform.Homepage(); e != nil {
		return e
	}
	_, e := t.platform.Subscriptions(false)
	if protocol_FlowErrorCode(e) == "http_401" {
		if e = t.platform.Refresh(); e != nil {
			return e
		}
		if e = t.save(""); e != nil {
			return e
		}
		_, e = t.platform.Subscriptions(false)
	}
	if e != nil {
		return e
	}
	// 账户套餐状态不证明原单已实扣，缺少原单证据时保留未确认资金。
	if e = t.save("payment_result"); e != nil {
		return e
	}
	if cp.Payment == "not_charged" {
		return protocol_NewError("not_charged")
	}
	return protocol_NewError("payment_pending")
}

// ---- pkg/gptpay/payment_diagnostics.go ----

// 只提取状态和代码，Observer 再次白名单过滤；不发送 next_action 的完整内容。
func (t *gptpay_transaction) observePaymentDiagnostics(data protocol_Map) {
	nodes := []protocol_Map{data, protocol_AsMap(data["payment_intent"]), protocol_AsMap(protocol_AsMap(data["submission_attempt"])["payment_intent"]), protocol_AsMap(protocol_AsMap(data["error"])["payment_intent"]), protocol_AsMap(data["submission_attempt"])}
	sources := []string{"response", "payment_intent", "submission_payment_intent", "error_payment_intent", "submission_attempt"}
	classified := string(stripe_Classify(data))
	t.providerCode, t.declineCode = "", ""
	for i, node := range nodes {
		action := protocol_AsMap(node["next_action"])
		failure := protocol_AsMap(node["last_payment_error"])
		if len(node) == 0 {
			continue
		}
		fields := protocol_Map{"outcome": "observed", "diagnostic_source": sources[i], "payment_state": classified}
		for _, key := range []string{"status", "state", "payment_status"} {
			if value, ok := node[key]; ok {
				target := map[string]string{"status": "raw_status", "state": "raw_state", "payment_status": "raw_payment_status"}[key]
				fields[target] = value
			}
		}
		if typ := action["type"]; typ != nil {
			fields["next_action_type"] = typ
		}
		sdk := protocol_AsMap(action["use_stripe_sdk"])
		if typ := sdk["type"]; typ != nil {
			fields["sdk_action_type"] = typ
		}
		// 只报告已知字段是否存在，不读取验证码、跳转地址或指纹载荷。
		if len(sdk) > 0 {
			for _, key := range []string{"stripe_js", "three_d_secure_2_source", "server_transaction_id", "directory_server_name"} {
				value, exists := sdk[key]
				fields["sdk_has_"+key] = exists && value != nil
			}
		}
		if code := failure["code"]; code != nil {
			if classified == "not_charged" {
				t.providerCode = observability_SafePaymentCode(protocol_AsString(code), false)
				t.declineCode = observability_SafePaymentCode(protocol_AsString(failure["decline_code"]), true)
			}
			fields["provider_code"] = code
		}
		if code := failure["decline_code"]; code != nil {
			fields["decline_code"] = code
		}
		t.observer.Emit("payment_diagnostic", fields)
	}
}

// ---- pkg/gptpay/payment_state.go ----

// setPaymentState 冻结首次取得明确扣款证据时的耗时；之后的到账核验不改写资金结果。
func (t *gptpay_transaction) setPaymentState(state model_PaymentState) {
	if t.cp.Payment == "charged" {
		return
	}
	t.cp.Payment = string(state)
	if state != model_PaymentCharged {
		return
	}
	fields := protocol_Map{"phase": t.cp.Phase, "outcome": "succeeded", "payment_state": "charged"}
	// 恢复时只能确认当前状态，无法据此还原原交易实际扣款时刻。
	if t.observer.Operation == "pay" {
		elapsed := max(int64(0), time.Since(t.started).Milliseconds())
		t.cp.Metadata["payment_elapsed_ms"] = elapsed
		fields["payment_elapsed_ms"] = elapsed
	}
	t.observer.Emit("payment_confirm", fields)
	t.progress.emit(model_ProgressStagePayment, model_ProgressSucceeded, "")
}

func (t *gptpay_transaction) observePayment(data protocol_Map) {
	t.observePaymentDiagnostics(data)
	t.absorbHostedIntent(data)
	t.setPaymentState(stripe_Classify(data))
	t.recordChargedPrice(data)
}

// 实收只取原 PaymentIntent 的直接证据，估价与页面 unpaid 均不能替代资金事实。
func (t *gptpay_transaction) recordChargedPrice(data protocol_Map) {
	if t.cp.Payment != "charged" || t.cp.ChargedPrice() != nil || t.cp.PaymentIntentID == "" {
		return
	}
	intents := []protocol_Map{data, protocol_AsMap(data["payment_intent"]), protocol_AsMap(protocol_AsMap(data["submission_attempt"])["payment_intent"]), protocol_AsMap(protocol_AsMap(data["error"])["payment_intent"])}
	for _, intent := range intents {
		if intent["id"] != t.cp.PaymentIntentID || intent["status"] != "succeeded" {
			continue
		}
		amount, valid := protocol_Integer(intent["amount_received"])
		currency := strings.ToUpper(protocol_AsString(intent["currency"]))
		if valid && amount >= 0 && currency == t.cp.Currency {
			t.cp.Metadata["charged_price"] = model_EncodePriceMetadata(amount, currency, model_PriceSourcePaymentIntentReceived)
			return
		}
	}
}

// ---- pkg/gptpay/progress.go ----

// 每笔交易维护自己的进度；不解析日志，也不向接收器暴露可变的交易上下文。
type gptpay_progressTracker struct {
	sink      model_ProgressSink
	traceID   string
	operation string
	started   time.Time
	current   model_ProgressStage
	states    map[model_ProgressStage]model_ProgressStatus
	cp        *model_Checkpoint
}

func (p *gptpay_progressTracker) emit(stage model_ProgressStage, status model_ProgressStatus, code string) {
	if p == nil {
		return
	}
	// 一次业务阶段只报告一次同种状态；已确认的成功不被后续异常覆盖。
	if previous := p.states[stage]; previous == status || previous == model_ProgressSucceeded {
		return
	}
	p.current = stage
	p.states[stage] = status
	if p.sink == nil {
		return
	}
	if code != "" {
		code = observability_SafeErrorCode(code)
	}
	value := model_Progress{
		TraceID: p.traceID, Operation: p.operation, Stage: stage, Status: status,
		Payment: model_PaymentState(p.cp.Payment), Code: code,
		ElapsedMS: max(int64(0), time.Since(p.started).Milliseconds()),
	}
	// 通知只用于展示，接收方异常不得中断付款或阻止检查点落盘。
	func() {
		defer func() { _ = recover() }()
		p.sink(value)
	}()
}

func (p *gptpay_progressTracker) finish(code string) {
	if p == nil {
		return
	}
	if p.cp.Payment == "charged" {
		p.emit(model_ProgressStagePayment, model_ProgressSucceeded, "")
		if p.cp.CancelRequested {
			status := model_ProgressPending
			if p.cp.Renewal == "off" {
				status = model_ProgressSucceeded
				code = ""
			} else if p.states[model_ProgressStageCancellation] == model_ProgressRunning && p.cp.Renewal == "on" && code != "cancellation_pending" {
				status = model_ProgressFailed
			}
			p.emit(model_ProgressStageCancellation, status, code)
		}
		return
	}
	if p.current == "" || p.states[p.current] != model_ProgressRunning {
		return
	}
	status := model_ProgressFailed
	if p.current == model_ProgressStagePayment {
		switch model_PaymentState(p.cp.Payment) {
		case model_PaymentUnknown, model_PaymentPending, model_PaymentAuthorized, model_PaymentActionRequired:
			status = model_ProgressPending
		}
	}
	p.emit(p.current, status, code)
}

// ---- pkg/gptpay/subscription.go ----

// Subscription 查询 Session 所属个人账号的套餐和有效期，仅访问 GET 接口。
// 刷新凭据只保留在本次调用内，不创建 Checkout、不修改续费、不调用支付回调。
func (c *gptpay_PaymentClient) Subscription(ctx context.Context, session model_Session) (model_SubscriptionStatus, error) {
	var result model_SubscriptionStatus
	if err := c.validateConfig(); err != nil {
		return result, err
	}
	if strings.TrimSpace(session.AccessToken) == "" || strings.TrimSpace(session.SessionToken) == "" {
		return result, protocol_NewError("session_token_required")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(c.Config.Deadline*float64(time.Second)))
	defer cancel()
	if err := ctx.Err(); err != nil {
		return result, err
	}
	deadline, _ := ctx.Deadline()
	factory := c.transportFactory
	if factory == nil {
		factory = func(ctx context.Context, proxy string, timeout float64, deadline time.Time) (protocol_HTTPTransport, error) {
			return transport_NewTransport(ctx, proxy, timeout, deadline)
		}
	}
	http, err := factory(ctx, c.Config.Proxy, c.Config.Timeout, deadline)
	if err != nil {
		return result, err
	}
	defer http.Close()
	profile, err := protocol_ResolveBrowserProfile(c.Config.BrowserProfile)
	if err != nil {
		return result, err
	}
	cp := model_DefaultCheckpoint()
	cp.AccessToken, cp.SessionToken, cp.Cookies = session.AccessToken, session.SessionToken, session.Cookies
	cp.AccountID, cp.Email, cp.Timezone = session.AccountID, session.Email, "UTC"
	clock, _ := fingerprint_NewClock(0)
	platform := chatgpt_NewPlatform(http, &cp, clock)
	platform.Profile = profile
	if err = platform.Homepage(); err != nil {
		return result, err
	}
	// 刷新接口可能返回 RefreshAccessTokenError，但已有访问令牌仍可读取账号。
	// 仅刷新失效时沿用原令牌，由后续账号接口验证；网络或账号不匹配错误仍终止。
	if err = platform.Refresh(); err != nil && protocol_FlowErrorCode(err) != "session_expired" {
		return result, err
	}
	account, err := platform.Account()
	if err != nil {
		return result, err
	}
	entitlement := protocol_AsMap(account["entitlement"])
	baseline := protocol_Map{
		"plan_type":               protocol_FirstTruthy(protocol_AsMap(account["account"])["plan_type"], entitlement["plan_type"]),
		"subscription_plan":       entitlement["subscription_plan"],
		"has_active_subscription": entitlement["has_active_subscription"],
	}
	subs, err := platform.Subscriptions(false)
	if protocol_FlowErrorCode(err) == "subscription_not_found" {
		subs, err = protocol_Map{}, nil
	}
	if err != nil {
		return result, err
	}
	result, err = gptpay_subscriptionStatus(baseline, subs)
	if err == nil {
		result.FetchedAt = time.Now().UTC()
	}
	return result, err
}

func gptpay_subscriptionStatus(baseline, subs protocol_Map) (model_SubscriptionStatus, error) {
	result := model_SubscriptionStatus{
		// 订阅记录可保留已失效套餐，当前套餐以账号接口为准。
		PlanType:         strings.TrimSpace(protocol_AsString(protocol_FirstTruthy(baseline["plan_type"], subs["plan_type"]))),
		SubscriptionPlan: strings.TrimSpace(protocol_AsString(protocol_FirstTruthy(subs["subscription_plan"], subs["plan"], baseline["subscription_plan"]))),
		Renewal:          chatgpt_RenewalState(subs),
	}
	if result.PlanType == "" {
		return model_SubscriptionStatus{}, protocol_NewError("subscription_status_invalid")
	}
	if active, ok := baseline["has_active_subscription"].(bool); ok {
		result.HasActiveSubscription = &active
	}
	if active, ok := subs["has_active_subscription"].(bool); ok {
		result.HasActiveSubscription = &active
	}
	// 只接受带时区的明确时间；未知值不能解释为永久有效或免费账号。
	if raw := protocol_FirstTruthy(subs["active_until"], subs["current_period_end"], subs["expires_at"], subs["billing_period_end"]); raw != nil {
		text, ok := raw.(string)
		if !ok {
			return model_SubscriptionStatus{}, protocol_NewError("subscription_status_invalid")
		}
		if strings.TrimSpace(text) != "" {
			until, err := time.Parse(time.RFC3339Nano, text)
			if err != nil {
				return model_SubscriptionStatus{}, protocol_NewError("subscription_status_invalid")
			}
			result.ActiveUntil = until.UTC().Format(time.RFC3339)
		}
	}
	return result, nil
}

// ---- pkg/gptpay/transaction.go ----

type gptpay_transaction struct {
	providerCode, declineCode string
	client                    *gptpay_PaymentClient
	cp                        *model_Checkpoint
	http, stripeHTTP          protocol_HTTPTransport
	platform                  *chatgpt_Platform
	stripe                    *stripe_Stripe
	sentinel                  gptpay_tokenSource
	observer                  *observability_Observer
	ctx                       context.Context
	cancel                    context.CancelFunc
	started                   time.Time
	progress                  *gptpay_progressTracker
}

func gptpay_newTransaction(ctx context.Context, c *gptpay_PaymentClient, cp model_Checkpoint, operation string, clock fingerprint_Clock) (*gptpay_transaction, error) {
	started := time.Now()
	profile, err := cp.BrowserProfile()
	if err != nil {
		return nil, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	expires := time.Now().Add(time.Duration(c.Config.Deadline * float64(time.Second)))
	ctx, cancel := context.WithDeadline(ctx, expires)
	if d, ok := ctx.Deadline(); ok {
		expires = d
	}
	phase := ""
	if operation == "recover" {
		phase = cp.Phase
	}
	obs := observability_NewObserver(operation, c.EventSink, protocol_AsString(cp.Metadata["trace_id"]), phase)
	cp.Metadata["trace_id"] = obs.TraceID
	factory := c.transportFactory
	if factory == nil {
		factory = func(ctx context.Context, p string, t float64, d time.Time) (protocol_HTTPTransport, error) {
			return transport_NewTransport(ctx, p, t, d)
		}
	}
	h, e := factory(ctx, cp.Proxy, c.Config.Timeout, expires)
	if e != nil {
		cancel()
		return nil, e
	}
	sh, e := factory(ctx, cp.Proxy, c.Config.Timeout, expires)
	if e != nil {
		h.Close()
		cancel()
		return nil, e
	}
	t := &gptpay_transaction{client: c, cp: &cp, http: h, stripeHTTP: sh, observer: obs, ctx: ctx, cancel: cancel, started: started}
	t.progress = &gptpay_progressTracker{sink: c.ProgressSink, traceID: obs.TraceID, operation: operation, started: started, states: make(map[model_ProgressStage]model_ProgressStatus), cp: &cp}
	t.platform = chatgpt_NewPlatform(h, &cp, clock)
	t.platform.Profile = profile
	t.stripe = stripe_NewStripe(sh, &cp, clock)
	t.stripe.Profile = profile
	// Stripe.js 的 stripe_js_id 与 client_session_id 是同一次页面会话的标识，
	// 浏览器不会在重试或页面重开时换掉它；恢复沿用快照值，缺失时（旧快照）才新建并保存。
	if id := protocol_AsString(cp.Metadata["stripe_client_id"]); id != "" {
		t.stripe.ClientID = id
	} else {
		cp.Metadata["stripe_client_id"] = t.stripe.ClientID
	}
	t.stripe.BindObserver(obs)
	sentinel := challenge_NewSentinel(h, &cp, clock, obs)
	sentinel.Profile = profile
	t.sentinel = sentinel
	if c.sentinelFactory != nil {
		t.sentinel = c.sentinelFactory(h, &cp, clock)
	}
	for _, b := range []struct {
		h       protocol_HTTPTransport
		service string
	}{{h, "chatgpt"}, {sh, "stripe"}} {
		if x, ok := b.h.(interface {
			BindObserver(*observability_Observer, string)
		}); ok {
			x.BindObserver(obs, b.service)
		}
	}
	obs.Emit("transaction", protocol_Map{"phase": cp.Phase, "outcome": "started", "payment_state": cp.Payment, "browser_profile": profile.ID})
	return t, nil
}

func (t *gptpay_transaction) save(phase string) error {
	changed := phase != "" && phase != t.cp.Phase
	if phase != "" {
		t.cp.Phase = phase
	}
	if t.client.Checkpoint != nil {
		snapshot, e := t.cp.Clone()
		if e == nil {
			e = gptpay_callCheckpoint(t.client.Checkpoint, snapshot)
		}
		if e != nil {
			t.observer.Emit("checkpoint", protocol_Map{"phase": t.cp.Phase, "outcome": "failed", "error_code": "checkpoint_failed"})
			return protocol_NewError("checkpoint_failed")
		}
	}
	t.observer.Checkpoint(t.cp.Phase, changed, t.client.Checkpoint != nil)
	return nil
}

func gptpay_callCheckpoint(sink model_CheckpointSink, cp model_Checkpoint) (err error) {
	defer func() {
		if recover() != nil {
			err = protocol_NewError("checkpoint_failed")
		}
	}()
	return sink(cp)
}

// boundary 必须在不可安全重放的请求之前同步落盘；响应丢失后沿用 unknown。
func (t *gptpay_transaction) boundary(phase string) error {
	t.cp.Payment = "unknown"
	return t.save(phase)
}

func (t *gptpay_transaction) complete(err error) model_Result {
	code := protocol_FlowErrorCode(err)
	t.progress.finish(code)
	warning := ""
	if t.cp.Payment == "charged" {
		// 资金结果与到账、续费及持久化等后续处理分开报告。
		warning, code = code, ""
	}
	t.observer.Complete(t.cp.Phase, t.cp.Payment, code, warning)
	cp, e := t.cp.Clone()
	if e != nil {
		cp = *t.cp
	}
	result := model_Result{ProviderCode: t.providerCode, DeclineCode: t.declineCode, Payment: model_PaymentState(cp.Payment), Activation: cp.Activation, Renewal: cp.Renewal, CheckoutID: cp.CheckoutID, PaymentIntentID: cp.PaymentIntentID, Amount: cp.Amount, Currency: cp.Currency, EstimatedPrice: cp.EstimatedPrice(), ChargedPrice: cp.ChargedPrice(), ErrorCode: code, WarningCode: warning, Flow: protocol_AsString(cp.Metadata["payment_flow"]), Checkpoint: cp}
	if elapsed, ok := protocol_Integer(cp.Metadata["payment_elapsed_ms"]); ok && elapsed >= 0 && cp.Payment == "charged" {
		result.PaymentElapsedMS = &elapsed
	}
	return result
}

func (t *gptpay_transaction) close() {
	t.cancel()
	// 关闭是清理边界：一个连接池的失败不能覆盖资金结果，也不能阻止另一个池释放。
	for _, h := range []protocol_HTTPTransport{t.http, t.stripeHTTP} {
		func() {
			defer func() { _ = recover() }()
			if h != nil {
				h.Close()
			}
		}()
	}
}

func (t *gptpay_transaction) createCheckout(kind string, withPrice bool) (result model_CheckoutResult) {
	defer t.close()
	defer func() {
		if recover() != nil {
			result.CheckoutID = t.cp.CheckoutID
			if result.CheckoutID != "" {
				result.ProcessorEntity = t.cp.Entity
			}
			result.ErrorCode = "protocol_error"
			t.progress.finish(result.ErrorCode)
			t.observer.Complete(t.cp.Phase, t.cp.Payment, result.ErrorCode)
		}
	}()
	mode := gptpay_requestedCheckoutUIMode(kind)
	// Hosted 提链及价格读取只需公钥和 init；完整设备上下文留给付款和可恢复快照。
	stripeRequired := withPrice && kind != "hosted" || t.client.Checkpoint != nil
	checkout, err := t.prepareCheckout(mode, stripeRequired)
	if err == nil && protocol_AsString(checkout["payment_flow"]) == "subscription_update" {
		result.Flow = "subscription_update"
		result.ProcessorEntity = t.cp.Entity
		if withPrice {
			result.Price, err = t.readUpgradePrice()
		}
		result.ErrorCode = protocol_FlowErrorCode(err)
		if err == nil {
			t.progress.emit(model_ProgressStageCheckout, model_ProgressSucceeded, "")
		}
		t.progress.finish(result.ErrorCode)
		t.observer.Complete(t.cp.Phase, t.cp.Payment, result.ErrorCode)
		return result
	}
	if err == nil {
		if kind == "hosted" {
			result.CheckoutURL, err = t.standaloneHostedURL(checkout)
		} else {
			result.CheckoutURL, err = t.checkoutURL(checkout, stripeRequired)
		}
		if err == nil && withPrice {
			result.Price, err = t.readCheckoutPrice(checkout, kind != "hosted" && t.cp.Metadata["checkout_ui_mode"] == "custom", result.CheckoutURL)
		}
	}
	result.CheckoutID = t.cp.CheckoutID
	result.CheckoutURL = gptpay_publicCheckoutURL(result.CheckoutURL, result.CheckoutID)
	if result.CheckoutID != "" {
		result.ProcessorEntity = t.cp.Entity
	}
	result.ErrorCode = protocol_FlowErrorCode(err)
	if err == nil {
		t.progress.emit(model_ProgressStageCheckout, model_ProgressSucceeded, "")
	}
	t.progress.finish(result.ErrorCode)
	t.observer.Complete(t.cp.Phase, t.cp.Payment, result.ErrorCode)
	return result
}

func (t *gptpay_transaction) recover() (result model_Result) {
	defer t.close()
	defer func() {
		if recover() != nil {
			result = t.complete(protocol_NewError("recovery_failed"))
		}
	}()
	err := t.recoverPayment()
	var protocolErr *protocol_ProtocolError
	if err != nil && !errors.As(err, &protocolErr) {
		err = protocol_NewError("recovery_failed")
	}
	return t.complete(err)
}

func (t *gptpay_transaction) execute(billing model_Billing, card *model_Card, expected *int64, approval model_PaymentApproval, checkoutType string) (result model_Result) {
	defer t.close()
	defer func() {
		if recover() != nil {
			result = t.complete(protocol_NewError("protocol_error"))
		}
	}()
	err := t.executeFlow(billing, card, expected, approval, checkoutType)
	return t.complete(err)
}

func (t *gptpay_transaction) executeFlow(billing model_Billing, card *model_Card, expected *int64, approval model_PaymentApproval, checkoutType string) error {
	checkout, e := t.prepareCheckout(gptpay_requestedCheckoutUIMode(checkoutType), true)
	if e != nil {
		return e
	}
	if protocol_AsString(t.cp.Metadata["payment_flow"]) == "subscription_update" {
		return t.runSubscriptionUpdate(card, billing, expected, approval)
	}
	var info protocol_Map
	if strings.HasPrefix(t.cp.CheckoutID, "cs_live_") && t.cp.Metadata["checkout_ui_mode"] == "redirect" {
		info, e = t.stripe.HostedPrepare(stripe_FindHostedURL(checkout, t.cp.CheckoutID), billing)
		if e == nil {
			e = t.pricing(info["amount"], info["currency"], expected, model_PriceSourceCheckoutTax)
		}
	} else {
		var tax protocol_Map
		tax, e = t.platform.Taxes(billing)
		if e == nil {
			node := protocol_AsMap(tax["checkout_session"])
			e = t.pricing(node["amount_total"], protocol_FirstTruthy(node["currency"], t.cp.Currency), expected, model_PriceSourceCheckoutTax)
		}
	}
	if e != nil {
		return e
	}
	if card == nil {
		if e = t.save("quoted"); e != nil {
			return e
		}
		t.progress.emit(model_ProgressStageCheckout, model_ProgressSucceeded, "")
		return nil
	}
	t.progress.emit(model_ProgressStageCheckout, model_ProgressSucceeded, "")
	t.progress.emit(model_ProgressStagePayment, model_ProgressRunning, "")
	if approval != nil {
		if e = t.ctx.Err(); e != nil {
			return e
		}
		ok, e := gptpay_callApproval(approval, *t.cp.Amount, t.cp.Currency)
		if e != nil {
			return protocol_NewError("payment_approval_failed")
		}
		if !ok {
			return protocol_NewError("payment_cancelled")
		}
		if e = t.ctx.Err(); e != nil {
			return e
		}
	}
	if t.cp.Metadata["checkout_ui_mode"] == "redirect" {
		e = t.runHostedFlow(*card, billing, info)
	} else {
		e = t.runCustomFlow(*card, billing, checkout)
	}
	if e != nil {
		return e
	}
	if t.cp.Payment == "charged" {
		if e = t.finishPayment(); e != nil {
			return e
		}
	}
	if e = t.save(""); e != nil {
		return e
	}
	if t.cp.Payment != "charged" {
		return protocol_NewError(t.cp.Payment)
	}
	return nil
}

func gptpay_callApproval(approve model_PaymentApproval, amount int64, currency string) (ok bool, err error) {
	defer func() {
		if recover() != nil {
			ok = false
			err = protocol_NewError("payment_approval_failed")
		}
	}()
	return approve(amount, currency)
}

func (t *gptpay_transaction) pricing(total, currency any, expected *int64, source string) error {
	n, ok := protocol_Integer(total)
	if !ok || n < 0 {
		return protocol_NewError("checkout_total_missing")
	}
	if strings.ToUpper(protocol_AsString(currency)) != t.cp.Currency {
		return protocol_NewError("checkout_currency_mismatch")
	}
	if expected != nil && n != *expected {
		return protocol_NewError("checkout_amount_changed")
	}
	t.cp.Amount = &n
	t.cp.Metadata["estimated_price"] = model_EncodePriceMetadata(n, t.cp.Currency, source)
	return t.save("priced")
}

func gptpay_requestedCheckoutUIMode(kind string) string {
	if kind == "oaics" {
		return "custom"
	}
	if kind == "hosted" {
		return "hosted"
	}
	return "custom"
}

// ---- pkg/model/billing.go ----

type model_Billing struct {
	Name       string `json:"name"`
	Email      string `json:"email"`
	Line1      string `json:"line1"`
	City       string `json:"city"`
	State      string `json:"state"`
	PostalCode string `json:"postal_code"`
	Country    string `json:"country"`
}

func (b model_Billing) Address() protocol_Fields {
	return protocol_O("line1", b.Line1, "city", b.City, "state", b.State, "postal_code", b.PostalCode, "country", b.Country)
}

func (b model_Billing) Validate() error {
	if b.Name == "" || b.Line1 == "" || b.City == "" || b.PostalCode == "" || !protocol_Matches(`[A-Z]{2}`, b.Country) {
		return fmt.Errorf("billing name and address are required")
	}
	return nil
}

// ---- pkg/model/capabilities.go ----

// CapabilityCatalog 是 SDK 支持能力的独立快照，不包含网络发现或运营价格。
type model_CapabilityCatalog struct {
	Plans   []model_PlanCapability   `json:"plans"`
	Regions []model_RegionCapability `json:"regions"`
}

type model_PlanCapability struct {
	Label        string                `json:"label"`
	ID           string                `json:"id"`
	CheckoutPlan string                `json:"checkout_plan"`
	HostedType   string                `json:"hosted_type"`
	Intervals    []string              `json:"intervals"`
	Seats        *model_SeatCapability `json:"seats,omitempty"`
}

type model_SeatCapability struct {
	Minimum int      `json:"minimum"`
	Default int      `json:"default"`
	Types   []string `json:"types"`
}

type model_RegionCapability struct {
	Country  string `json:"country"`
	Currency string `json:"currency"`
	Timezone string `json:"timezone"`
}

// Capabilities 每次返回独立切片，调用方的展示变更不会修改协议定义。
func model_Capabilities() model_CapabilityCatalog {
	catalog := model_CapabilityCatalog{Plans: make([]model_PlanCapability, 0, len(model_planDefinitions)), Regions: make([]model_RegionCapability, 0, len(model_regionDefinitions))}
	for _, p := range model_planDefinitions {
		plan := model_PlanCapability{ID: p.id, Label: p.label, CheckoutPlan: p.checkout, HostedType: p.hosted, Intervals: []string{"month"}}
		if p.team {
			plan.Intervals = []string{"month", "year"}
			plan.Seats = &model_SeatCapability{Minimum: model_MinTeamSeats, Default: model_DefaultTeamSeats, Types: []string{"default", "prolite"}}
		}
		catalog.Plans = append(catalog.Plans, plan)
	}
	for country, region := range model_regionDefinitions {
		catalog.Regions = append(catalog.Regions, model_RegionCapability{country, region.Currency, region.Timezone})
	}
	sort.Slice(catalog.Regions, func(i, j int) bool { return catalog.Regions[i].Country < catalog.Regions[j].Country })
	return catalog
}

// ---- pkg/model/card.go ----

type model_Card struct {
	Number   string `json:"number"`
	CVC      string `json:"cvc"`
	ExpMonth string `json:"exp_month"`
	ExpYear  string `json:"exp_year"`
}

func (c model_Card) String() string { return "Card{redacted}" }

func (c model_Card) GoString() string { return c.String() }

func (c model_Card) Validate() error {
	if !protocol_Matches(`[0-9]{12,19}`, c.Number) || !protocol_Matches(`[0-9]{3,4}`, c.CVC) {
		return fmt.Errorf("invalid card number or CVC")
	}
	var m int
	if _, e := fmt.Sscanf(c.ExpMonth, "%d", &m); e != nil || !protocol_Matches(`[0-9]+`, c.ExpMonth) || m < 1 || m > 12 {
		return fmt.Errorf("invalid expiry month")
	}
	if !protocol_Matches(`[0-9]{4}`, c.ExpYear) {
		return fmt.Errorf("expiry year must have four digits")
	}
	return nil
}

// ---- pkg/model/catalog.go ----

type model_planDefinition struct {
	id, label, checkout, hosted string
	team                        bool
	subscriptionTypes           map[string]bool
}

// 套餐能力与结算映射共享一份定义，避免调用方与执行路径各自维护名单。
var model_planDefinitions = []model_planDefinition{
	{"go", "Go", "chatgptgoplan", "go", false, protocol_StringSet("go", "go_plan", "chatgptgo", "chatgptgoplan")},
	{"plus", "Plus", "chatgptplusplan", "basic", false, protocol_StringSet("basic", "plus", "basic_plan", "chatgptplusplan")},
	{"pro5x", "Pro 5X", "chatgptprolite", "standard", false, protocol_StringSet("standard", "prolite", "standard_plan", "pro5", "pro5x", "chatgptprolite")},
	{"pro20x", "Pro 20X", "chatgptpro", "premium", false, protocol_StringSet("premium", "pro", "premium_plan", "pro20", "pro20x", "chatgptpro")},
	{"promax", "Pro Max", "chatgptpromax", "promax", false, protocol_StringSet("promax", "promax_plan", "pro_max", "pro_max_plan", "chatgptpromax")},
	{"team", "Team", "chatgptteamplan", "team", true, protocol_StringSet("team", "team_plan", "chatgptteam", "chatgptteamplan", "default")},
	{"team5x", "Team 5X", "chatgptteamplan", "team", true, protocol_StringSet("team5x", "team_5x", "prolite", "chatgptprolite", "chatgptteamplan", "team")},
}

func model_findPlan(id string) model_planDefinition {
	for _, plan := range model_planDefinitions {
		if plan.id == id {
			return plan
		}
	}
	return model_planDefinition{}
}

type model_Region struct{ Currency, Timezone string }

var model_regionDefinitions = map[string]model_Region{"CL": {"CLP", "America/Santiago"}, "PH": {"PHP", "Asia/Manila"}, "US": {"USD", "America/New_York"}, "JP": {"JPY", "Asia/Tokyo"}, "EG": {"EGP", "Africa/Cairo"}, "NR": {"AUD", "Pacific/Nauru"}}

// CheckoutPlan 返回套餐对应的创建请求值，未知套餐返回空字符串。
func model_CheckoutPlan(id string) string {
	plan := model_findPlan(id)
	return plan.checkout
}

// HostedPlanType 返回套餐对应的 Hosted 验证类型，未知套餐返回空字符串。
func model_HostedPlanType(id string) string {
	plan := model_findPlan(id)
	return plan.hosted
}

// LookupRegion 返回地区值的副本，外部修改不会改变协议定义。
func model_LookupRegion(country string) (model_Region, bool) {
	region, ok := model_regionDefinitions[country]
	return region, ok
}

// MatchesSubscriptionType 只匹配上游响应值，不把响应别名用作 SDK 套餐 ID。
func model_MatchesSubscriptionType(plan, value string) bool {
	definition := model_findPlan(plan)
	return definition.subscriptionTypes[value]
}

// IsSubscriptionType 判断上游订阅类型是否属于已知目录，供脱敏事件校验。
func model_IsSubscriptionType(value string) bool {
	for _, plan := range model_planDefinitions {
		if plan.subscriptionTypes[value] {
			return true
		}
	}
	return false
}

const model_DefaultTeamSeats = 2
const model_MinTeamSeats = 2
const model_DefaultTeamInterval = "month"

func model_IsTeamPlan(plan string) bool {
	return model_findPlan(strings.ToLower(plan)).team
}

type model_TeamSeatConfig struct {
	Default, ProLite int
	Interval         string
}

func (c model_TeamSeatConfig) Total() int { return c.Default + c.ProLite }

func model_NormalizeTeamSeatConfig(plan string, seats, seats5x int, interval string) (model_TeamSeatConfig, error) {
	plan = strings.ToLower(plan)
	interval = strings.ToLower(strings.TrimSpace(interval))
	if interval == "" {
		interval = model_DefaultTeamInterval
	}
	if interval != "month" && interval != "year" {
		return model_TeamSeatConfig{}, fmt.Errorf("invalid team price interval")
	}
	if !model_IsTeamPlan(plan) {
		if seats != 0 || seats5x != 0 {
			return model_TeamSeatConfig{}, fmt.Errorf("seats only apply to team plans")
		}
		return model_TeamSeatConfig{}, nil
	}
	defaultSeats, proLite := seats, seats5x
	if plan == "team5x" && seats5x == 0 && seats != 0 {
		defaultSeats, proLite = 0, seats
	}
	if defaultSeats == 0 && proLite == 0 {
		if plan == "team5x" {
			proLite = model_DefaultTeamSeats
		} else {
			defaultSeats = model_DefaultTeamSeats
		}
	}
	if defaultSeats < 0 || proLite < 0 || defaultSeats+proLite < model_MinTeamSeats {
		return model_TeamSeatConfig{}, fmt.Errorf("team seats must be at least %d", model_MinTeamSeats)
	}
	return model_TeamSeatConfig{Default: defaultSeats, ProLite: proLite, Interval: interval}, nil
}

func (c model_TeamSeatConfig) Quantities() []any {
	items := []any{}
	if c.Default > 0 {
		items = append(items, protocol_O("seat_type", "default", "quantity", c.Default))
	}
	if c.ProLite > 0 {
		items = append(items, protocol_O("seat_type", "prolite", "quantity", c.ProLite))
	}
	return items
}

// TeamSeatConfig 读取已经校验的快照字段，不补席位或周期默认值。
func (c model_Checkpoint) TeamSeatConfig() model_TeamSeatConfig {
	defaultSeats, _ := protocol_Integer(c.Metadata["team_default_seats"])
	proLiteSeats, _ := protocol_Integer(c.Metadata["team_prolite_seats"])
	return model_TeamSeatConfig{Default: int(defaultSeats), ProLite: int(proLiteSeats), Interval: protocol_AsString(c.Metadata["team_price_interval"])}
}

// ---- pkg/model/catalog_prices.go ----

// CatalogPrices 是网页展示价快照，不是 Checkout 报价或付款授权。
// 年付节点保持原金额，未确认计价基准前不计算年付总额。
type model_CatalogPrices struct {
	ConfigKey         string               `json:"config_key"`
	Country           string               `json:"country"`
	Currency          string               `json:"currency"`
	MinorUnitExponent int                  `json:"minor_unit_exponent"`
	FetchedAt         time.Time            `json:"fetched_at"`
	DisplayOnly       bool                 `json:"display_only"`
	Items             []model_CatalogPrice `json:"items"`
}

type model_CatalogPrice struct {
	Plan        string `json:"plan"`
	PriceKey    string `json:"price_key"`
	Interval    string `json:"interval"`
	AmountMinor int64  `json:"amount_minor"`
	Tax         string `json:"tax"`
	// AmountBasis 仅在月付时为 month；年付保持 unverified，避免误乘或少算。
	AmountBasis string `json:"amount_basis"`
	PerSeat     bool   `json:"per_seat"`
}

// PricingConfigPlanKey 将 SDK 套餐 ID 映射到展示价格目录，不接受上游别名作为套餐 ID。
func model_PricingConfigPlanKey(plan string) string {
	switch plan {
	case "go", "plus", "promax":
		return plan
	case "pro5x":
		return "prolite"
	case "pro20x":
		return "pro"
	case "team":
		return "business"
	case "team5x":
		return "business_prolite"
	default:
		return ""
	}
}

// PricingConfigRegionKey 仅维护已核对的地区配置键；币种以响应为准，不复用付款地区默认值。
func model_PricingConfigRegionKey(country string) string {
	switch country {
	case "PH":
		return "PHP"
	case "JP":
		return "JPY"
	case "EG":
		return "EGP"
	case "US", "NR", "CL":
		return country
	default:
		return ""
	}
}

// ---- pkg/model/checkpoint.go ----

var model_preSubmitPhases = protocol_StringSet("prepared", "initializing", "account_checked", "stripe_ready", "checkout_created", "priced", "quoted", "upgrade_priced")

var model_customPhases = protocol_StringSet("openai_confirm_checkout", "openai_confirmed", "stripe_confirm")

var model_subscriptionUpdatePhases = protocol_StringSet("subscription_update")

var model_hostedPhases = protocol_StringSet("hosted_confirm", "hosted_approve")

var model_postPaymentPhases = protocol_StringSet("activation", "cancellation", "cancellation_requested", "completed")

var model_metadataKeys = protocol_StringSet("attestation", "build", "client_version", "checkout_ui_mode", "hosted_url", "scripts", "session_id", "subscription_baseline", "trace_id", "fingerprint_clock_offset_hours", "browser_profile", "payment_elapsed_ms", "checkout_client_secret", "team_default_seats", "team_prolite_seats", "team_price_interval", "payment_flow", "updated_plan", "payment_method_id", "setup_intent_id", "estimated_price", "charged_price", "stripe_client_id")

// ValidateForRecovery 在创建任何网络运行时之前校验完整的 v2 恢复上下文。
func (c model_Checkpoint) ValidateForRecovery() error {
	if c.Version != model_CheckpointVersion {
		return fmt.Errorf("unsupported checkpoint version")
	}
	if c.Amount != nil && *c.Amount < 0 {
		return fmt.Errorf("invalid checkpoint amount")
	}
	if c.Metadata == nil || c.StripeIdentity == nil || c.ConfirmBody == nil {
		return fmt.Errorf("invalid checkpoint mappings")
	}
	for _, validate := range []func() error{c.validateMetadata, c.validateValues, c.validateIdentifiers, c.validateFlowContext} {
		if err := validate(); err != nil {
			return err
		}
	}
	return nil
}

func (c model_Checkpoint) validateValues() error {
	phase, payment := c.Phase, c.Payment
	known := model_preSubmitPhases[phase] || model_customPhases[phase] || model_hostedPhases[phase] || model_subscriptionUpdatePhases[phase] || model_postPaymentPhases[phase] || phase == "payment_result"
	if !known {
		return fmt.Errorf("unknown checkpoint phase")
	}
	valid := false
	switch {
	case model_preSubmitPhases[phase]:
		valid = payment == "not_attempted"
	case model_customPhases[phase] || model_subscriptionUpdatePhases[phase] || phase == "hosted_confirm":
		valid = payment == "unknown"
	case phase == "hosted_approve":
		valid = protocol_StringSet("unknown", "pending", "authorized")[payment]
	case phase == "payment_result":
		valid = protocol_StringSet("unknown", "pending", "action_required", "authorized", "charged", "not_charged")[payment]
	case model_postPaymentPhases[phase]:
		valid = payment == "charged"
	}
	if !valid {
		return fmt.Errorf("checkpoint phase and payment state do not match")
	}
	if model_CheckoutPlan(c.Plan) == "" || !protocol_Matches(`[A-Z]{2}`, c.Country) || !protocol_Matches(`[A-Z]{3}`, c.Currency) {
		return fmt.Errorf("invalid checkpoint plan or region")
	}
	if _, e := time.LoadLocation(c.Timezone); e != nil || c.Timezone == "" || c.Timezone == "Local" {
		return fmt.Errorf("invalid checkpoint timezone")
	}
	if c.AccessToken == "" || c.SessionToken == "" {
		return fmt.Errorf("checkpoint session credentials are required")
	}
	if !protocol_Matches(`[a-z0-9_]{2,64}`, c.Entity) {
		return fmt.Errorf("invalid checkpoint processor entity")
	}
	if !protocol_StringSet("pending", "active")[c.Activation] || !protocol_StringSet("unknown", "on", "off")[c.Renewal] {
		return fmt.Errorf("invalid checkpoint subscription state")
	}
	if e := protocol_ValidateProxy(c.Proxy); e != nil {
		return e
	}
	return nil
}

func (c model_Checkpoint) validateIdentifiers() error {
	mode := protocol_AsString(c.Metadata["checkout_ui_mode"])
	if c.CheckoutID != "" && !protocol_Matches(`(?:oaics_|cs_live_)[A-Za-z0-9]+`, c.CheckoutID) {
		return fmt.Errorf("invalid checkpoint checkout ID")
	}
	if c.PaymentIntentID != "" && !protocol_Matches(`pi_[A-Za-z0-9]+`, c.PaymentIntentID) {
		return fmt.Errorf("invalid checkpoint PaymentIntent ID")
	}
	if c.ClientSecret != "" && (!protocol_Matches(`pi_[A-Za-z0-9]+_secret_[A-Za-z0-9]+`, c.ClientSecret) || strings.Split(c.ClientSecret, "_secret_")[0] != c.PaymentIntentID) {
		return fmt.Errorf("invalid checkpoint PaymentIntent secret")
	}
	if c.PaymentIntentID != "" && c.ClientSecret == "" && mode == "custom" {
		return fmt.Errorf("custom PaymentIntent secret is required")
	}
	if c.ConfirmationToken != "" && !protocol_Matches(`ctoken_[A-Za-z0-9]+`, c.ConfirmationToken) {
		return fmt.Errorf("invalid checkpoint confirmation token")
	}
	if c.PublishableKey != "" && !protocol_Matches(`pk_[A-Za-z0-9_]+`, c.PublishableKey) {
		return fmt.Errorf("invalid checkpoint publishable key")
	}
	return nil
}

// IsPreSubmitPhase 表示尚未进入付款提交边界。
func model_IsPreSubmitPhase(phase string) bool { return model_preSubmitPhases[phase] }

// ---- pkg/model/checkpoint_context.go ----

func (c model_Checkpoint) validateFlowContext() error {
	if protocol_AsString(c.Metadata["payment_flow"]) == "subscription_update" {
		return c.validateSubscriptionUpdateContext()
	}
	phase := c.Phase
	mode := protocol_AsString(c.Metadata["checkout_ui_mode"])
	accountPhase := phase != "prepared" && phase != "initializing"
	stripePhase := accountPhase && phase != "account_checked"
	checkoutPhase := stripePhase && phase != "stripe_ready"
	pricedPhase := checkoutPhase && phase != "checkout_created"
	if accountPhase && (c.AccountID == "" || c.DeviceID == "") {
		return fmt.Errorf("checkpoint account context is incomplete")
	}
	if stripePhase && (c.PublishableKey == "" || c.StripeVersion == "" || c.StripeIdentity["guid"] == "" || c.StripeIdentity["muid"] == "" || c.StripeIdentity["sid"] == "") {
		return fmt.Errorf("checkpoint Stripe context is incomplete")
	}
	if checkoutPhase && (c.CheckoutID == "" || mode == "") {
		return fmt.Errorf("checkpoint checkout context is incomplete")
	}
	if pricedPhase && c.Amount == nil {
		return fmt.Errorf("checkpoint amount is required")
	}
	if model_preSubmitPhases[phase] && (c.ClientSecret != "" || c.PaymentIntentID != "" || c.ConfirmationToken != "" || len(c.ConfirmBody) > 0) {
		return fmt.Errorf("pre-submit checkpoint contains payment artifacts")
	}
	if model_customPhases[phase] && (mode != "custom" || !c.hasCustomPaymentArtifact()) {
		return fmt.Errorf("custom checkpoint context is incomplete")
	}
	if (phase == "openai_confirmed" || phase == "stripe_confirm") && (c.PaymentIntentID == "" || c.ClientSecret == "") {
		return fmt.Errorf("custom PaymentIntent context is incomplete")
	}
	if phase == "stripe_confirm" && len(c.ConfirmBody) == 0 {
		return fmt.Errorf("custom confirmation body is required")
	}
	if model_hostedPhases[phase] && (mode != "redirect" || len(c.ConfirmBody) == 0) {
		return fmt.Errorf("hosted checkpoint context is incomplete")
	}
	if mode == "redirect" && (model_hostedPhases[phase] || phase == "payment_result" || model_postPaymentPhases[phase]) {
		s, ok := c.Metadata["hosted_url"].(string)
		u, e := url.Parse(s)
		if !ok || e != nil || u.Scheme != "https" || u.User != nil || !protocol_StringSet("checkout.stripe.com", "pay.openai.com")[u.Host] || u.EscapedPath() != "/c/pay/"+c.CheckoutID || u.RawQuery != "" || len(u.Fragment) < 250 {
			return fmt.Errorf("invalid hosted checkpoint URL")
		}
	}
	if err := c.validateSubscriptionState(); err != nil {
		return err
	}
	if (phase == "payment_result" || model_postPaymentPhases[phase]) && mode == "custom" {
		if !c.hasCustomPaymentArtifact() {
			return fmt.Errorf("custom payment context is incomplete")
		}
		if strings.HasPrefix(c.CheckoutID, "oaics_") && c.Payment != "not_charged" && (c.PaymentIntentID == "" || c.ClientSecret == "") {
			return fmt.Errorf("custom payment context is incomplete")
		}
	}
	return nil
}

func (c model_Checkpoint) validateSubscriptionUpdateContext() error {
	phase := c.Phase
	if c.CheckoutID != "" || protocol_AsString(c.Metadata["checkout_ui_mode"]) != "" {
		return fmt.Errorf("subscription update must not use checkout")
	}
	accountPhase := phase != "prepared" && phase != "initializing"
	if accountPhase && (c.AccountID == "" || c.DeviceID == "") {
		return fmt.Errorf("checkpoint account context is incomplete")
	}
	priced := protocol_StringSet("priced", "quoted", "upgrade_priced", "subscription_update", "payment_result")[phase] || model_postPaymentPhases[phase]
	if priced && c.Amount == nil {
		return fmt.Errorf("checkpoint amount is required")
	}
	if model_preSubmitPhases[phase] && (c.ClientSecret != "" || c.PaymentIntentID != "" || c.ConfirmationToken != "" || len(c.ConfirmBody) > 0) {
		return fmt.Errorf("pre-submit checkpoint contains payment artifacts")
	}
	return c.validateSubscriptionState()
}

func (c model_Checkpoint) validateSubscriptionState() error {
	phase := c.Phase
	if !model_postPaymentPhases[phase] && (c.Activation != "pending" || c.Renewal != "unknown") {
		return fmt.Errorf("checkpoint phase and subscription state do not match")
	}
	if protocol_StringSet("cancellation", "cancellation_requested", "completed")[phase] && c.Activation != "active" {
		return fmt.Errorf("checkpoint phase requires active subscription")
	}
	if protocol_StringSet("cancellation", "cancellation_requested")[phase] && !c.CancelRequested {
		return fmt.Errorf("cancellation requires cancellation request")
	}
	if phase == "completed" && c.CancelRequested && c.Renewal != "off" {
		return fmt.Errorf("completed cancellation requires renewal off")
	}
	return nil
}

func (c model_Checkpoint) hasCustomPaymentArtifact() bool {
	if strings.HasPrefix(c.ConfirmationToken, "ctoken_") {
		return true
	}
	if c.ConfirmBody == nil {
		return false
	}
	return strings.HasPrefix(protocol_AsString(c.ConfirmBody.Get("payment_method")), "pm_")
}

// ---- pkg/model/checkpoint_metadata.go ----

func (c model_Checkpoint) validateMetadata() error {
	if _, err := c.BrowserProfile(); err != nil {
		return err
	}
	for k := range c.Metadata {
		if !model_metadataKeys[k] {
			return fmt.Errorf("unknown checkpoint metadata field")
		}
	}
	if v, exists := c.Metadata["payment_elapsed_ms"]; exists {
		elapsed, valid := protocol_Integer(v)
		if !valid || elapsed < 0 || c.Payment != "charged" {
			return fmt.Errorf("invalid checkpoint payment elapsed time")
		}
	}
	if v, ok := c.Metadata["fingerprint_clock_offset_hours"]; ok {
		n, valid := protocol_Integer(v)
		if !valid || n != 0 && n != -24 {
			return fmt.Errorf("invalid fingerprint clock offset")
		}
	}
	if err := c.validateTeamSeatMetadata(); err != nil {
		return err
	}
	mode := protocol_AsString(c.Metadata["checkout_ui_mode"])
	if v, ok := c.Metadata["checkout_ui_mode"]; ok {
		if _, ok := v.(string); !ok || mode != "custom" && mode != "redirect" {
			return fmt.Errorf("invalid checkpoint checkout UI mode")
		}
	}
	for _, k := range []string{"attestation", "build", "client_version", "hosted_url", "session_id", "checkout_client_secret"} {
		if v, ok := c.Metadata[k]; ok {
			s, ok := v.(string)
			if !ok || k == "checkout_client_secret" && (c.CheckoutID == "" || !strings.HasPrefix(s, c.CheckoutID+"_secret_") || len(s) == len(c.CheckoutID+"_secret_")) {
				return fmt.Errorf("invalid checkpoint metadata field")
			}
		}
	}
	if v, ok := c.Metadata["stripe_client_id"]; ok {
		// 设备会话标识必须与 m/6 的 stripe_js_id 同源，恢复时不能接受被改写或类型错误的值。
		s, valid := v.(string)
		if !valid || !protocol_Matches(`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, s) {
			return fmt.Errorf("invalid checkpoint stripe client id")
		}
	}
	if v, ok := c.Metadata["trace_id"]; ok && v != nil {
		s, valid := v.(string)
		if !valid || !protocol_Matches(`[0-9a-f]{32}`, s) {
			return fmt.Errorf("invalid checkpoint trace ID")
		}
	}
	if v, ok := c.Metadata["scripts"]; ok && v != nil {
		switch x := v.(type) {
		case []string:
		case []any:
			for _, s := range x {
				if _, ok := s.(string); !ok {
					return fmt.Errorf("invalid checkpoint scripts")
				}
			}
		default:
			return fmt.Errorf("invalid checkpoint scripts")
		}
	}
	if v, ok := c.Metadata["subscription_baseline"]; ok && v != nil {
		m, ok := v.(protocol_Map)
		if !ok || len(m) != 4 {
			return fmt.Errorf("invalid subscription baseline")
		}
		for _, k := range []string{"plan_type", "subscription_plan", "cancels_at"} {
			if _, ok := m[k].(string); !ok {
				return fmt.Errorf("invalid subscription baseline")
			}
		}
		active, exists := m["has_active_subscription"]
		if !exists {
			return fmt.Errorf("invalid subscription baseline")
		}
		if active != nil {
			if _, ok := active.(bool); !ok {
				return fmt.Errorf("invalid subscription baseline")
			}
		}
	}
	for _, key := range []string{"estimated_price", "charged_price"} {
		if v, ok := c.Metadata[key]; ok && v != nil {
			charged := key == "charged_price"
			if charged && c.Payment != string(model_PaymentCharged) {
				return fmt.Errorf("charged price requires confirmed payment")
			}
			if e := model_validatePriceMetadata(v, charged); e != nil {
				return e
			}
		}
	}
	flow := protocol_AsString(c.Metadata["payment_flow"])
	if v, ok := c.Metadata["payment_flow"]; ok {
		if _, ok := v.(string); !ok || flow != "subscription_update" {
			return fmt.Errorf("invalid payment flow")
		}
	}
	plan := protocol_AsString(c.Metadata["updated_plan"])
	if v, ok := c.Metadata["updated_plan"]; ok {
		if _, ok := v.(string); !ok || plan != "chatgptprolite" {
			return fmt.Errorf("invalid updated plan")
		}
	}
	if (flow == "") != (plan == "") {
		return fmt.Errorf("subscription update metadata is incomplete")
	}
	if flow == "subscription_update" && c.Plan != "pro5x" {
		return fmt.Errorf("subscription update requires pro5x")
	}
	if v, ok := c.Metadata["payment_method_id"]; ok {
		s, ok := v.(string)
		if !ok || !protocol_Matches(`pm_[A-Za-z0-9]+`, s) {
			return fmt.Errorf("invalid payment method id")
		}
	}
	if v, ok := c.Metadata["setup_intent_id"]; ok {
		s, ok := v.(string)
		if !ok || !protocol_Matches(`seti_[A-Za-z0-9]+`, s) {
			return fmt.Errorf("invalid setup intent id")
		}
	}
	return nil
}

func (c model_Checkpoint) validateTeamSeatMetadata() error {
	if !model_IsTeamPlan(c.Plan) {
		for _, key := range []string{"team_default_seats", "team_prolite_seats", "team_price_interval"} {
			if _, exists := c.Metadata[key]; exists {
				return fmt.Errorf("team metadata only applies to team plans")
			}
		}
		return nil
	}
	defaultSeats, defaultOK := protocol_Integer(c.Metadata["team_default_seats"])
	proLiteSeats, proLiteOK := protocol_Integer(c.Metadata["team_prolite_seats"])
	maxSeats := int64(^uint(0) >> 1)
	if !defaultOK || !proLiteOK || defaultSeats < 0 || proLiteSeats < 0 || defaultSeats > maxSeats || proLiteSeats > maxSeats-defaultSeats || defaultSeats+proLiteSeats < model_MinTeamSeats {
		return fmt.Errorf("invalid checkpoint team seats")
	}
	if c.Plan == "team5x" && proLiteSeats == 0 {
		return fmt.Errorf("team5x checkpoint requires 5x seats")
	}
	interval, ok := c.Metadata["team_price_interval"].(string)
	if !ok || interval != "month" && interval != "year" {
		return fmt.Errorf("invalid checkpoint team price interval")
	}
	return nil
}

// BrowserProfile 只接受快照明确保存的版本 ID，恢复时不猜测或重新选择画像。
func (c model_Checkpoint) BrowserProfile() (protocol_BrowserProfile, error) {
	v, exists := c.Metadata["browser_profile"]
	if !exists {
		return protocol_BrowserProfile{}, fmt.Errorf("checkpoint browser profile is required")
	}
	id, ok := v.(string)
	if !ok {
		return protocol_BrowserProfile{}, fmt.Errorf("invalid checkpoint browser profile")
	}
	return protocol_LookupBrowserProfile(id)
}

// ---- pkg/model/checkpoint_types.go ----

const model_CheckpointVersion = 2

type model_Checkpoint struct {
	Version           int               `json:"version"`
	Phase             string            `json:"phase"`
	Payment           string            `json:"payment"`
	Plan              string            `json:"plan"`
	Country           string            `json:"country"`
	Currency          string            `json:"currency"`
	Amount            *int64            `json:"amount"`
	AccountID         string            `json:"account_id"`
	Email             string            `json:"email"`
	CheckoutID        string            `json:"checkout_id"`
	Entity            string            `json:"entity"`
	PublishableKey    string            `json:"publishable_key"`
	AccessToken       string            `json:"access_token"`
	SessionToken      string            `json:"session_token"`
	Cookies           string            `json:"cookies"`
	DeviceID          string            `json:"device_id"`
	ClientSecret      string            `json:"client_secret"`
	ConfirmationToken string            `json:"confirmation_token"`
	ReturnURL         string            `json:"return_url"`
	PaymentIntentID   string            `json:"payment_intent_id"`
	StripeVersion     string            `json:"stripe_version"`
	Proxy             string            `json:"proxy"`
	Timezone          string            `json:"timezone"`
	Metadata          protocol_Map      `json:"metadata"`
	StripeIdentity    map[string]string `json:"stripe_identity"`
	ConfirmBody       protocol_Fields   `json:"confirm_body"`
	Activation        string            `json:"activation"`
	Renewal           string            `json:"renewal"`
	CancelRequested   bool              `json:"cancel_requested"`
}

func model_DefaultCheckpoint() model_Checkpoint {
	return model_Checkpoint{Version: model_CheckpointVersion, Phase: "prepared", Payment: "not_attempted", Entity: "openai_llc", Metadata: protocol_Map{}, StripeIdentity: map[string]string{}, ConfirmBody: protocol_Fields{}, Activation: "pending", Renewal: "unknown", CancelRequested: true}
}

func (c model_Checkpoint) String() string { return "Checkpoint{redacted}" }

func (c model_Checkpoint) GoString() string { return c.String() }

func (c *model_Checkpoint) UnmarshalJSON(b []byte) error {
	m, e := protocol_DecodeMap(b)
	if e != nil {
		return fmt.Errorf("invalid checkpoint JSON")
	}
	typ := reflect.TypeOf(model_Checkpoint{})
	allowed := map[string]reflect.Type{}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		name := f.Tag.Get("json")
		if _, exists := m[name]; !exists {
			return fmt.Errorf("missing checkpoint field: %s", name)
		}
		allowed[name] = f.Type
	}
	for k, v := range m {
		t, ok := allowed[k]
		if !ok {
			return fmt.Errorf("unknown checkpoint field")
		}
		if k == "amount" {
			if v != nil {
				n, ok := protocol_Integer(v)
				if !ok || n < 0 {
					return fmt.Errorf("invalid checkpoint amount")
				}
			}
			continue
		}
		if v == nil {
			return fmt.Errorf("invalid checkpoint field type: %s", k)
		}
		switch t.Kind() {
		case reflect.String:
			if _, ok := v.(string); !ok {
				return fmt.Errorf("invalid checkpoint field type: %s", k)
			}
		case reflect.Bool:
			if _, ok := v.(bool); !ok {
				return fmt.Errorf("invalid checkpoint field type: %s", k)
			}
		case reflect.Int:
			if _, ok := protocol_Integer(v); !ok {
				return fmt.Errorf("unsupported checkpoint version")
			}
		case reflect.Map, reflect.Slice:
			object, ok := v.(protocol_Map)
			if !ok {
				return fmt.Errorf("invalid checkpoint field type: %s", k)
			}
			if k == "stripe_identity" {
				for _, value := range object {
					if _, ok := value.(string); !ok {
						return fmt.Errorf("invalid checkpoint identity value")
					}
				}
			}
		}
	}
	type plain model_Checkpoint
	var v plain
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	d.DisallowUnknownFields()
	if e = d.Decode(&v); e != nil {
		return fmt.Errorf("invalid checkpoint field type")
	}
	*c = model_Checkpoint(v)
	return nil
}

func (c model_Checkpoint) Clone() (model_Checkpoint, error) {
	b, e := json.Marshal(c)
	if e != nil {
		return model_Checkpoint{}, fmt.Errorf("invalid checkpoint")
	}
	var out model_Checkpoint
	if e = json.Unmarshal(b, &out); e != nil {
		return model_Checkpoint{}, e
	}
	return out, nil
}

type model_CheckpointSink func(model_Checkpoint) error

// ---- pkg/model/config.go ----

type model_Config struct {
	BrowserProfile string
	Proxy          string
	Timeout        float64
	Deadline       float64
	Currency       string
	Timezone       string
}

func model_DefaultConfig() model_Config { return model_Config{Timeout: 45, Deadline: 240} }

func (c model_Config) String() string {
	return fmt.Sprintf("Config{Timeout:%g Deadline:%g Currency:%s Timezone:%s BrowserProfile:%s Proxy:redacted}", c.Timeout, c.Deadline, c.Currency, c.Timezone, c.BrowserProfile)
}

func (c model_Config) GoString() string { return c.String() }

// ---- pkg/model/event.go ----

type model_Event = map[string]any

type model_EventSink func(model_Event)

// ---- pkg/model/price.go ----

// Price 是定价或扣款阶段独立记录的金额快照。
type model_Price struct {
	AmountMinor int64  `json:"amount_minor"`
	Currency    string `json:"currency"`
	Source      string `json:"source"`
}

const (
	// PriceSourceCheckoutTax 表示金额来自普通 Checkout 流程的含税总价。
	model_PriceSourceCheckoutTax = "checkout_tax"
	// PriceSourceSubscriptionProration 表示金额来自 Plus 升级 Pro 5X 的按比例估算。
	model_PriceSourceSubscriptionProration = "subscription_proration"
	// PriceSourcePaymentIntentReceived 表示金额来自扣款确认时的 PaymentIntent。
	model_PriceSourcePaymentIntentReceived = "payment_intent_received"
)

// estimatedPrice、chargedPrice 各自独立存储于 Metadata，互不覆盖；二者在升级流程中可能不同。
func model_priceFromMetadata(m protocol_Map, charged bool) *model_Price {
	if model_validatePriceMetadata(m, charged) != nil {
		return nil
	}
	amount, _ := protocol_Integer(m["amount_minor"])
	currency := m["currency"].(string)
	source := m["source"].(string)
	return &model_Price{AmountMinor: amount, Currency: currency, Source: source}
}

// EstimatedPrice 返回定价阶段记录的金额；没有有效估价证据时为 nil。
func (c model_Checkpoint) EstimatedPrice() *model_Price {
	m, ok := c.Metadata["estimated_price"].(protocol_Map)
	if !ok {
		return nil
	}
	return model_priceFromMetadata(m, false)
}

// ChargedPrice 返回扣款确认阶段记录的实际金额；扣款确认前为 nil。
func (c model_Checkpoint) ChargedPrice() *model_Price {
	if c.Payment != string(model_PaymentCharged) {
		return nil
	}
	m, ok := c.Metadata["charged_price"].(protocol_Map)
	if !ok {
		return nil
	}
	return model_priceFromMetadata(m, true)
}

// EncodePriceMetadata 构造写入 Checkpoint.Metadata["estimated_price"/"charged_price"] 的结构；
// source 必须是 PriceSource* 常量之一，供 pkg/gptpay 在定价与扣款确认阶段调用。
func model_EncodePriceMetadata(amountMinor int64, currency, source string) protocol_Map {
	return protocol_Map{"amount_minor": amountMinor, "currency": currency, "source": source}
}

func model_validatePriceMetadata(v any, charged bool) error {
	m, ok := v.(protocol_Map)
	if !ok || len(m) != 3 {
		return fmt.Errorf("invalid checkpoint price metadata")
	}
	if n, ok := protocol_Integer(m["amount_minor"]); !ok || n < 0 {
		return fmt.Errorf("invalid checkpoint price amount")
	}
	if currency, ok := m["currency"].(string); !ok || !protocol_Matches(`[A-Z]{3}`, currency) {
		return fmt.Errorf("invalid checkpoint price currency")
	}
	source, ok := m["source"].(string)
	validSource := source == model_PriceSourceCheckoutTax || source == model_PriceSourceSubscriptionProration
	if charged {
		validSource = source == model_PriceSourcePaymentIntentReceived
	}
	if !ok || !validSource {
		return fmt.Errorf("invalid checkpoint price source")
	}
	return nil
}

// ---- pkg/model/progress.go ----

// ProgressStage 是面向集成方的业务阶段，与内部检查点阶段独立。
type model_ProgressStage string

const (
	model_ProgressStageSession      model_ProgressStage = "session"
	model_ProgressStageCheckout     model_ProgressStage = "checkout"
	model_ProgressStagePayment      model_ProgressStage = "payment"
	model_ProgressStageCancellation model_ProgressStage = "cancellation"
)

type model_ProgressStatus string

const (
	model_ProgressRunning   model_ProgressStatus = "running"
	model_ProgressSucceeded model_ProgressStatus = "succeeded"
	model_ProgressFailed    model_ProgressStatus = "failed"
	model_ProgressPending   model_ProgressStatus = "pending"
)

// Progress 只含进度和资金状态，不携带 Session、卡片、链接或恢复凭据。
// Status 与 Code 描述当前 Stage；取消失败不代表 Payment 失败。
type model_Progress struct {
	TraceID   string               `json:"trace_id"`
	Operation string               `json:"operation"`
	Stage     model_ProgressStage  `json:"stage"`
	Status    model_ProgressStatus `json:"status"`
	Payment   model_PaymentState   `json:"payment"`
	Code      string               `json:"code,omitempty"`
	ElapsedMS int64                `json:"elapsed_ms"`
}

// ProgressSink 在交易内同步按序调用，须快速返回；共享接收器须保证并发安全。
// 回调 panic 被隔离。进度通知不能替代 CheckpointSink 的同步持久化。
type model_ProgressSink func(model_Progress)

// ---- pkg/model/result.go ----

type model_PaymentState string

const (
	model_PaymentNotAttempted   model_PaymentState = "not_attempted"
	model_PaymentUnknown        model_PaymentState = "unknown"
	model_PaymentPending        model_PaymentState = "pending"
	model_PaymentActionRequired model_PaymentState = "action_required"
	model_PaymentAuthorized     model_PaymentState = "authorized"
	model_PaymentCharged        model_PaymentState = "charged"
	model_PaymentDeclined       model_PaymentState = "not_charged"
)

type model_CheckoutPrice struct {
	Currency      string `json:"currency"`
	SubtotalMinor *int64 `json:"subtotal_minor"`
	TotalMinor    *int64 `json:"total_minor"`
	TaxMinor      *int64 `json:"tax_minor"`
	DiscountMinor *int64 `json:"discount_minor"`
}

type model_CheckoutResult struct {
	CheckoutID      string               `json:"checkout_id"`
	CheckoutURL     string               `json:"checkout_url"`
	ProcessorEntity string               `json:"processor_entity"`
	ErrorCode       string               `json:"error_code"`
	Price           *model_CheckoutPrice `json:"price"`
	Flow            string               `json:"flow,omitempty"`
}

func (r model_CheckoutResult) String() string {
	return fmt.Sprintf("CheckoutResult{CheckoutID:%s ErrorCode:%s Flow:%s URL:redacted}", r.CheckoutID, r.ErrorCode, r.Flow)
}

func (r model_CheckoutResult) GoString() string { return r.String() }

type model_Result struct {
	ProviderCode, DeclineCode                        string
	Payment                                          model_PaymentState
	Activation, Renewal, CheckoutID, PaymentIntentID string
	Amount                                           *int64
	Currency, ErrorCode                              string
	WarningCode                                      string
	Flow                                             string
	PaymentElapsedMS                                 *int64
	EstimatedPrice, ChargedPrice                     *model_Price
	Checkpoint                                       model_Checkpoint
}

func (r model_Result) Public() protocol_Map {
	result := protocol_Map{"payment": string(r.Payment), "activation": r.Activation, "renewal": r.Renewal, "checkout_id": r.CheckoutID, "payment_intent_id": r.PaymentIntentID, "amount": r.Amount, "currency": r.Currency, "error_code": r.ErrorCode, "phase": r.Checkpoint.Phase}
	if r.Payment == model_PaymentDeclined {
		if r.ProviderCode != "" {
			result["provider_code"] = r.ProviderCode
		}
		if r.DeclineCode != "" {
			result["decline_code"] = r.DeclineCode
		}
		if reason := r.FailureReason(); reason != "" {
			result["failure_reason"] = reason
		}
	}
	if r.WarningCode != "" {
		result["warning_code"] = r.WarningCode
	}
	if r.Flow != "" {
		result["flow"] = r.Flow
	}
	return result
}

// JSON 输出始终使用公开字段；恢复凭据只通过 Checkpoint 显式持久化。
func (r model_Result) MarshalJSON() ([]byte, error) { return json.Marshal(r.Public()) }

func (r model_Result) String() string { s, _ := protocol_CompactJSON(r.Public()); return s }

func (r model_Result) GoString() string { return r.String() }

type model_PaymentApproval func(int64, string) (bool, error)

// FailureReason 使用固定文案，不显示上游自由文本。
func (r model_Result) FailureReason() string {
	if r.Payment != model_PaymentDeclined {
		return ""
	}
	if r.DeclineCode == "card_velocity_exceeded" {
		return "卡片可用余额、信用额度或交易金额限制不足，请向发卡方核实"
	}
	if r.ProviderCode == "card_declined" {
		return "付款被拒绝，请结合 decline_code 向发卡方核实"
	}
	if r.ProviderCode == "payment_intent_authentication_failure" {
		return "付款验证失败"
	}
	return ""
}

// ---- pkg/model/session.go ----

const model_SessionJSONMaxChars = 262144

type model_Session struct {
	AccessToken  string `json:"access_token"`
	SessionToken string `json:"session_token"`
	AccountID    string `json:"account_id"`
	Email        string `json:"email"`
	Cookies      string `json:"cookies"`
}

func (s model_Session) String() string { return "Session{redacted}" }

func (s model_Session) GoString() string { return s.String() }

func model_ParseSession(raw string) (model_Session, error) {
	if !utf8.ValidString(raw) || utf8.RuneCountInString(raw) > model_SessionJSONMaxChars {
		return model_Session{}, fmt.Errorf("invalid Session JSON input")
	}
	m, e := model_decodeSession(raw)
	if e != nil {
		return model_Session{}, e
	}
	text := func(m protocol_Map, k string, required bool) (string, error) {
		v, ok := m[k]
		if !ok {
			v = ""
		}
		s, ok := v.(string)
		if !ok || required && strings.TrimSpace(s) == "" {
			return "", fmt.Errorf("invalid Session field: %s", k)
		}
		return strings.TrimSpace(s), nil
	}
	nested := func(k string) (protocol_Map, error) {
		v, ok := m[k]
		if !ok {
			return protocol_Map{}, nil
		}
		obj, ok := v.(protocol_Map)
		if !ok {
			return nil, fmt.Errorf("Session account and user must be objects")
		}
		return obj, nil
	}
	a, e := nested("account")
	if e != nil {
		return model_Session{}, e
	}
	u, e := nested("user")
	if e != nil {
		return model_Session{}, e
	}
	s := model_Session{}
	for _, p := range []struct {
		m        protocol_Map
		k        string
		dst      *string
		required bool
	}{{m, "accessToken", &s.AccessToken, true}, {m, "sessionToken", &s.SessionToken, true}, {a, "id", &s.AccountID, false}, {u, "email", &s.Email, false}, {m, "cookies", &s.Cookies, false}} {
		*p.dst, e = text(p.m, p.k, p.required)
		if e != nil {
			return model_Session{}, e
		}
	}
	return s, nil
}

// 仅返回位置与固定文案，不携带 JSON 原文或底层解析错误。
func model_decodeSession(raw string) (protocol_Map, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		position := len(raw)
		var syntax *json.SyntaxError
		if errors.As(err, &syntax) {
			position = int(syntax.Offset) - 1
		}
		return nil, model_sessionSyntaxError(raw, position)
	}
	end := int(decoder.InputOffset())
	rest := raw[end:]
	remaining := strings.TrimLeft(rest, " \r\n\t")
	if remaining != "" {
		return nil, model_sessionSyntaxError(raw, end+len(rest)-len(remaining))
	}
	m, ok := value.(protocol_Map)
	if !ok || m == nil {
		return nil, fmt.Errorf("Session JSON must be an object")
	}
	return m, nil
}

func model_sessionSyntaxError(raw string, position int) error {
	if position < 0 {
		position = 0
	}
	if position > len(raw) {
		position = len(raw)
	}
	prefix := raw[:position]
	line := strings.Count(prefix, "\n") + 1
	last := strings.LastIndex(prefix, "\n")
	column := utf8.RuneCountInString(prefix[last+1:]) + 1
	return fmt.Errorf("invalid Session JSON at line %d, column %d", line, column)
}

// ---- pkg/model/subscription.go ----

// SubscriptionStatus 仅包含订阅查询结果，不携带凭据或上游原始响应。
type model_SubscriptionStatus struct {
	PlanType              string    `json:"planType"`
	SubscriptionPlan      string    `json:"subscriptionPlan"`
	HasActiveSubscription *bool     `json:"hasActiveSubscription"`
	ActiveUntil           string    `json:"activeUntil"`
	Renewal               string    `json:"renewal"`
	FetchedAt             time.Time `json:"fetchedAt"`
}

// ---- pkg/protocol/browser.go ----

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

func protocol_LookupBrowserProfile(id string) (protocol_BrowserProfile, error) {
	p := protocol_DefaultBrowserProfile()
	switch id {
	case "chrome131-windows-v1":
	case "chrome131-macos-v1":
		p.ID, p.Platform, p.NavigatorPlatform, p.PlatformVersion = id, "macOS", "MacIntel", "14.0.0"
		p.UserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
		p.ScreenWidth, p.ScreenHeight, p.AvailWidth, p.AvailHeight, p.DevicePixelRatio = 1440, 900, 1440, 875, 2
	default:
		return protocol_BrowserProfile{}, fmt.Errorf("unknown browser profile ID")
	}
	return p, nil
}

func protocol_ValidateBrowserProfileSelection(selection string) error {
	switch selection {
	case "", "windows", "macos", "random":
		return nil
	default:
		return fmt.Errorf("browser profile must be windows, macos or random")
	}
}

// ResolveBrowserProfile 仅在新建交易时调用，random 不得用于恢复或逐请求选择。
func protocol_ResolveBrowserProfile(selection string) (protocol_BrowserProfile, error) {
	return protocol_resolveBrowserProfile(selection, rand.Reader)
}

func protocol_resolveBrowserProfile(selection string, source io.Reader) (protocol_BrowserProfile, error) {
	if err := protocol_ValidateBrowserProfileSelection(selection); err != nil {
		return protocol_BrowserProfile{}, err
	}
	if selection == "" || selection == "random" {
		n, err := rand.Int(source, big.NewInt(2))
		if err != nil {
			return protocol_BrowserProfile{}, fmt.Errorf("browser profile random source unavailable")
		}
		selection = []string{"windows", "macos"}[n.Int64()]
	}
	return protocol_LookupBrowserProfile("chrome131-" + selection + "-v1")
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

// ---- pkg/protocol/encoding.go ----

// CompactJSON 对应 Python ensure_ascii=True，供参与后续编码的协议字段使用。
func protocol_CompactJSON(v any) (string, error) {
	encoded, e := protocol_marshalProtocolJSON(v, 0)
	if e != nil {
		return "", e
	}
	var b strings.Builder
	for _, r := range string(encoded) {
		if r < 128 {
			b.WriteRune(r)
		} else if r <= 0xffff {
			fmt.Fprintf(&b, "\\u%04x", r)
		} else {
			a, c := utf16.EncodeRune(r)
			fmt.Fprintf(&b, "\\u%04x\\u%04x", a, c)
		}
	}
	return b.String(), nil
}

func protocol_DecodeMap(raw []byte) (protocol_Map, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var m protocol_Map
	if err := d.Decode(&m); err != nil || m == nil {
		return nil, protocol_NewError("invalid_json_response")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return nil, protocol_NewError("invalid_json_response")
	}
	return m, nil
}

func protocol_JSONResponse(r *protocol_HTTPResponse, allowError bool) (protocol_Map, error) {
	if r == nil {
		return nil, protocol_NewError("invalid_json_response")
	}
	m, e := protocol_DecodeMap(r.Content)
	if e != nil {
		return nil, e
	}
	if r.StatusCode >= 400 && !allowError {
		return nil, protocol_NewError(fmt.Sprintf("http_%d", r.StatusCode))
	}
	if r.StatusCode >= 300 && r.StatusCode < 400 {
		return nil, protocol_NewError("unexpected_redirect")
	}
	return m, nil
}

func protocol_AsMap(v any) protocol_Map {
	switch x := v.(type) {
	case protocol_Map:
		return x
	case protocol_Fields:
		m := protocol_Map{}
		for _, p := range x {
			m[p.Key] = p.Value
		}
		return m
	}
	return protocol_Map{}
}

func protocol_AsString(v any) string {
	if v == nil {
		return ""
	}
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number:
		s, _ := protocol_protocolNumber(x)
		return s
	case float64:
		return protocol_protocolFloat(x)
	case float32:
		return protocol_protocolFloat(float64(x))
	}
	return fmt.Sprint(v)
}

func protocol_Truthy(v any) bool {
	if v == nil {
		return false
	}
	switch x := v.(type) {
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, e := strconv.ParseFloat(string(x), 64)
		return e == nil && f != 0
	}
	r := reflect.ValueOf(v)
	switch r.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return r.Int() != 0
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return r.Uint() != 0
	case reflect.Float32, reflect.Float64:
		return r.Float() != 0
	case reflect.Slice, reflect.Map, reflect.Array:
		return r.Len() != 0
	case reflect.Pointer, reflect.Interface:
		return !r.IsNil()
	}
	return true
}

func protocol_FirstTruthy(v ...any) any {
	for _, x := range v {
		if protocol_Truthy(x) {
			return x
		}
	}
	return nil
}

func protocol_Integer(v any) (int64, bool) {
	switch x := v.(type) {
	case json.Number:
		n, e := x.Int64()
		return n, e == nil
	case int:
		return int64(x), true
	case int64:
		return x, true
	case int32:
		return int64(x), true
	case int16:
		return int64(x), true
	case int8:
		return int64(x), true
	case uint:
		if uint64(x) <= 1<<63-1 {
			return int64(x), true
		}
	case uint64:
		if x <= 1<<63-1 {
			return int64(x), true
		}
	case uint32:
		return int64(x), true
	case uint16:
		return int64(x), true
	case uint8:
		return int64(x), true
	}
	return 0, false
}

func protocol_SortedKeys(m protocol_Map) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func protocol_RandomUUID() string { return uuid.NewString() }

func protocol_RandomAlphabet(n int, alphabet string) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		x, e := rand.Int(rand.Reader, big.NewInt(int64(len(alphabet))))
		if e != nil {
			panic("random source unavailable")
		}
		b.WriteByte(alphabet[x.Int64()])
	}
	return b.String()
}

// ---- pkg/protocol/errors.go ----

func protocol_FlowErrorCode(err error) string {
	if err == nil {
		return ""
	}
	var pe *protocol_ProtocolError
	if errors.As(err, &pe) {
		return pe.Code
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return "deadline_exceeded"
	}
	return "protocol_error"
}

type protocol_ProtocolError struct{ Code string }

func protocol_NewError(code string) error { return &protocol_ProtocolError{Code: code} }

func (e *protocol_ProtocolError) Error() string { return e.Code }

// ---- pkg/protocol/form.go ----

// FormEncode 对有序字段逐项展开；普通 map 使用稳定键序，避免依赖 Go 的 map 迭代顺序。
// SDK 的循环对象和过深输入在 256 层停止，不让错误输入耗尽调用栈。
func protocol_FormEncode(value any) (string, error) {
	const maxDepth = 256
	var pairs []string
	var walk func(any, string, int) error
	walk = func(v any, key string, depth int) error {
		if depth > maxDepth {
			return fmt.Errorf("form exceeds depth limit")
		}
		if v == nil {
			return nil
		}
		if fields, ok := v.(protocol_Fields); ok {
			for _, f := range fields {
				next := f.Key
				if key != "" {
					next = key + "[" + f.Key + "]"
				}
				if err := walk(f.Value, next, depth+1); err != nil {
					return err
				}
			}
			return nil
		}
		rv := reflect.ValueOf(v)
		switch rv.Kind() {
		case reflect.Pointer, reflect.Interface:
			if rv.IsNil() {
				return nil
			}
			return walk(rv.Elem().Interface(), key, depth+1)
		case reflect.Map:
			if rv.Type().Key().Kind() != reflect.String {
				return fmt.Errorf("form map keys must be strings")
			}
			keys := rv.MapKeys()
			sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
			for _, k := range keys {
				next := k.String()
				if key != "" {
					next = key + "[" + next + "]"
				}
				if err := walk(rv.MapIndex(k).Interface(), next, depth+1); err != nil {
					return err
				}
			}
			return nil
		case reflect.Slice, reflect.Array:
			for i := 0; i < rv.Len(); i++ {
				if err := walk(rv.Index(i).Interface(), fmt.Sprintf("%s[%d]", key, i), depth+1); err != nil {
					return err
				}
			}
			return nil
		}
		var text string
		switch x := v.(type) {
		case json.Number:
			var err error
			text, err = protocol_protocolNumber(x)
			if err != nil {
				return err
			}
		case float64:
			text = protocol_protocolFloat(x)
		case float32:
			text = protocol_protocolFloat(float64(x))
		default:
			switch rv.Kind() {
			case reflect.String:
				text = rv.String()
			case reflect.Bool:
				text = strconv.FormatBool(rv.Bool())
			case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
				text = strconv.FormatInt(rv.Int(), 10)
			case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
				text = strconv.FormatUint(rv.Uint(), 10)
			case reflect.Float32, reflect.Float64:
				text = protocol_protocolFloat(rv.Float())
			default:
				return fmt.Errorf("unsupported form value type")
			}
		}
		pairs = append(pairs, url.QueryEscape(key)+"="+url.QueryEscape(text))
		return nil
	}
	if err := walk(value, "", 0); err != nil {
		return "", err
	}
	return strings.Join(pairs, "&"), nil
}

// ---- pkg/protocol/http.go ----

type protocol_Map = map[string]any

// RequestOptions 保留调用方的字段顺序，并区分 JSON、表单和原始正文。
type protocol_RequestOptions struct {
	Headers protocol_Fields
	Form    any
	Body    any
	Text    *string
}

type protocol_HTTPResponse struct {
	StatusCode int
	Headers    http.Header
	Content    []byte
	Cookies    map[string]string
}

// HTTPTransport 只由单笔交易串行使用；交易之间不共享凭据或连接。
type protocol_HTTPTransport interface {
	Request(method, url string, options protocol_RequestOptions) (*protocol_HTTPResponse, error)
	Pause(seconds float64) error
	Close()
	Deadline() time.Time
}

// ---- pkg/protocol/json_encoding.go ----

// Python 的 float 保留 .0，科学计数的阈值与 Go JSON 不同。
// 这些字符参与 Stripe opaque 编码，不能仅比较解码后的数值。
func protocol_protocolFloat(value float64) string {
	if math.IsNaN(value) {
		return "nan"
	}
	if math.IsInf(value, 1) {
		return "inf"
	}
	if math.IsInf(value, -1) {
		return "-inf"
	}
	magnitude := math.Abs(value)
	if magnitude != 0 && (magnitude < 1e-4 || magnitude >= 1e16) {
		return strconv.FormatFloat(value, 'e', -1, 64)
	}
	s := strconv.FormatFloat(value, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// json.Number 保留输入类型以便金额校验拒绝浮点；重新编码时再采用 Python 数值格式。
func protocol_protocolNumber(value json.Number) (string, error) {
	if value == "" {
		return "", fmt.Errorf("invalid protocol number")
	}
	if _, err := json.Marshal(value); err != nil {
		return "", fmt.Errorf("invalid protocol number")
	}
	text := string(value)
	if strings.ContainsAny(text, ".eE") {
		v, err := value.Float64()
		if err != nil || math.IsInf(v, 0) || math.IsNaN(v) {
			return "", fmt.Errorf("invalid protocol number")
		}
		return protocol_protocolFloat(v), nil
	}
	if text == "-0" {
		return "0", nil
	}
	return text, nil
}

func protocol_marshalProtocolJSON(value any, depth int) ([]byte, error) {
	if depth > 256 {
		return nil, fmt.Errorf("protocol JSON exceeds depth limit")
	}
	switch v := value.(type) {
	case json.Number:
		text, err := protocol_protocolNumber(v)
		return []byte(text), err
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, fmt.Errorf("invalid protocol number")
		}
		return []byte(protocol_protocolFloat(v)), nil
	case float32:
		return protocol_marshalProtocolJSON(float64(v), depth+1)
	case protocol_Fields:
		var result bytes.Buffer
		result.WriteByte('{')
		for i, field := range v {
			if i > 0 {
				result.WriteByte(',')
			}
			key, err := protocol_marshalProtocolJSON(field.Key, depth+1)
			if err != nil {
				return nil, err
			}
			encoded, err := protocol_marshalProtocolJSON(field.Value, depth+1)
			if err != nil {
				return nil, err
			}
			result.Write(key)
			result.WriteByte(':')
			result.Write(encoded)
		}
		result.WriteByte('}')
		return result.Bytes(), nil
	case protocol_Map:
		if v == nil {
			return []byte("null"), nil
		}
		fields := protocol_Fields{}
		for _, key := range protocol_SortedKeys(v) {
			fields = append(fields, protocol_Field{key, v[key]})
		}
		return protocol_marshalProtocolJSON(fields, depth+1)
	}
	reflected := reflect.ValueOf(value)
	if reflected.IsValid() {
		switch reflected.Kind() {
		case reflect.Interface, reflect.Pointer:
			if reflected.IsNil() {
				return []byte("null"), nil
			}
			return protocol_marshalProtocolJSON(reflected.Elem().Interface(), depth+1)
		case reflect.Float32, reflect.Float64:
			return protocol_marshalProtocolJSON(reflected.Float(), depth+1)
		case reflect.Map:
			if reflected.Type().Key().Kind() != reflect.String {
				return nil, fmt.Errorf("protocol JSON requires string object keys")
			}
			if reflected.IsNil() {
				return []byte("null"), nil
			}
			keys := reflected.MapKeys()
			sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
			fields := protocol_Fields{}
			for _, key := range keys {
				fields = append(fields, protocol_Field{key.String(), reflected.MapIndex(key).Interface()})
			}
			return protocol_marshalProtocolJSON(fields, depth+1)
		case reflect.Struct, reflect.Func, reflect.Chan, reflect.UnsafePointer, reflect.Complex64, reflect.Complex128:
			// 协议对象只能包含 JSON 数据；不调用可能重入 Fields 的自定义编码器。
			return nil, fmt.Errorf("unsupported protocol JSON type")
		}
	}
	if reflected.IsValid() && (reflected.Kind() == reflect.Array || reflected.Kind() == reflect.Slice) {
		// []byte 仍保留 encoding/json 的 Base64 契约。
		if _, ok := value.([]byte); !ok {
			if reflected.Kind() == reflect.Slice && reflected.IsNil() {
				return []byte("null"), nil
			}
			var result bytes.Buffer
			result.WriteByte('[')
			for i := 0; i < reflected.Len(); i++ {
				if i > 0 {
					result.WriteByte(',')
				}
				encoded, err := protocol_marshalProtocolJSON(reflected.Index(i).Interface(), depth+1)
				if err != nil {
					return nil, err
				}
				result.Write(encoded)
			}
			result.WriteByte(']')
			return result.Bytes(), nil
		}
	}
	var result bytes.Buffer
	encoder := json.NewEncoder(&result)
	encoder.SetEscapeHTML(false)
	if reflected.IsValid() {
		// 标量别名也按底层 JSON 类型处理，避免自定义 MarshalJSON 绕过深度预算。
		if _, number := value.(json.Number); !number {
			switch reflected.Kind() {
			case reflect.String:
				value = reflected.String()
			case reflect.Bool:
				value = reflected.Bool()
			case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
				value = reflected.Int()
			case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
				value = reflected.Uint()
			}
		}
	}
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(result.Bytes(), []byte{'\n'}), nil
}

// ---- pkg/protocol/ordered_fields.go ----

// Fields 保留请求字段和冻结确认表单的顺序。普通 map 不承诺插入顺序。
type protocol_Field struct {
	Key   string
	Value any
}

type protocol_Fields []protocol_Field

func protocol_O(values ...any) protocol_Fields {
	if len(values)%2 != 0 {
		panic("invalid ordered object")
	}
	f := make(protocol_Fields, 0, len(values)/2)
	for i := 0; i < len(values); i += 2 {
		f.Set(values[i].(string), values[i+1])
	}
	return f
}

func (f *protocol_Fields) Set(key string, value any) {
	for i := range *f {
		if (*f)[i].Key == key {
			(*f)[i].Value = value
			return
		}
	}
	*f = append(*f, protocol_Field{key, value})
}

func (f protocol_Fields) Get(key string) any {
	for _, p := range f {
		if p.Key == key {
			return p.Value
		}
	}
	return nil
}

func (f *protocol_Fields) Delete(key string) {
	for i, p := range *f {
		if p.Key == key {
			*f = append((*f)[:i], (*f)[i+1:]...)
			return
		}
	}
}

func (f *protocol_Fields) Update(other protocol_Fields) {
	for _, p := range other {
		f.Set(p.Key, p.Value)
	}
}

func (f protocol_Fields) Clone() protocol_Fields { return append(protocol_Fields{}, f...) }

func (f protocol_Fields) MarshalJSON() ([]byte, error) { return protocol_marshalProtocolJSON(f, 0) }

func (f *protocol_Fields) UnmarshalJSON(raw []byte) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, e := protocol_DecodeOrdered(d)
	if e != nil {
		return e
	}
	obj, ok := v.(protocol_Fields)
	if !ok {
		return fmt.Errorf("expected JSON object")
	}
	*f = obj
	return nil
}

func protocol_DecodeOrdered(d *json.Decoder) (any, error) {
	t, e := d.Token()
	if e != nil {
		return nil, e
	}
	switch t {
	case json.Delim('{'):
		f := protocol_Fields{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return nil, e
			}
			v, e := protocol_DecodeOrdered(d)
			if e != nil {
				return nil, e
			}
			f.Set(k.(string), v)
		}
		_, e = d.Token()
		return f, e
	case json.Delim('['):
		a := []any{}
		for d.More() {
			v, e := protocol_DecodeOrdered(d)
			if e != nil {
				return nil, e
			}
			a = append(a, v)
		}
		_, e = d.Token()
		return a, e
	}
	return t, nil
}

// ---- pkg/protocol/provider_param.go ----

// SafeProviderParam 仅接受已知请求字段路径，不接受字段值、任意标识符或原始错误描述。
func protocol_SafeProviderParam(value string) string {
	if len(value) == 0 || len(value) > 200 {
		return "redacted"
	}
	parts := strings.Split(strings.ReplaceAll(value, "]", ""), "[")
	if strings.Count(value, "[") != strings.Count(value, "]") {
		return "redacted"
	}
	allowed := protocol_StringSet("expected_payment_method_type", "expected_amount", "key", "_stripe_version", "return_url", "init_checksum", "version", "elements_session_client", "elements_options_client", "client_attribution_metadata", "consent", "terms_of_service", "guid", "muid", "sid", "eid", "radar_options", "radar_session", "session", "payment_method", "confirmation_token", "js_checksum", "rv_timestamp", "referrer", "last_displayed_line_item_group_details", "subtotal", "total_exclusive_tax", "total_inclusive_tax", "total_discount_amount", "shipping_rate_amount", "elements_init_source", "referrer_host", "stripe_js_id", "locale", "is_aggregation_expected", "session_id", "client_betas", "saved_payment_method", "enable_save", "enable_redisplay", "client_session_id", "checkout_session_id", "merchant_integration_source", "merchant_integration_version", "merchant_integration_subtype", "payment_intent_creation_flow", "payment_method_selection_flow", "elements_session_id", "checkout_config_id", "elements_session_config_id", "client_secret", "payment_method_data", "type", "card", "number", "cvc", "exp_month", "exp_year", "billing_details", "name", "email", "address", "line1", "line2", "city", "state", "postal_code", "country", "0", "1")
	for _, part := range parts {
		if !allowed[part] && !protocol_checkoutDiagnosticFields[part] {
			return "redacted"
		}
	}
	// 重新编码确保只接受规范的方括号字段路径。
	canonical := parts[0]
	for _, part := range parts[1:] {
		canonical += "[" + part + "]"
	}
	if canonical != value {
		return "redacted"
	}
	return value
}

// 仅保留结算请求的字段名，不接受字段值或未知参数。
var protocol_checkoutDiagnosticFields = protocol_StringSet("plan_name", "currency", "checkout_ui_mode", "cancel_url", "entry_point", "promo_code", "team_plan_data", "workspace_name", "quantity", "seat_quantity", "price_interval", "seat_quantities")

// ---- pkg/protocol/proxy.go ----

func protocol_ValidateProxy(proxy string) error {
	if proxy == "" {
		return nil
	}
	u, e := url.Parse(proxy)
	if e != nil || u.Hostname() == "" || !protocol_StringSet("http", "https", "socks4", "socks4a", "socks5", "socks5h")[u.Scheme] || u.Path != "" && u.Path != "/" || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("invalid proxy")
	}
	if u.Port() != "" {
		var p int
		if _, e = fmt.Sscanf(u.Port(), "%d", &p); e != nil || p < 1 || p > 65535 {
			return fmt.Errorf("invalid proxy")
		}
	}
	return nil
}

// ---- pkg/protocol/values.go ----

func protocol_Matches(pattern, value string) bool {
	return regexp.MustCompile("^(?:" + pattern + ")$").MatchString(value)
}

func protocol_StringSet(v ...string) map[string]bool {
	m := map[string]bool{}
	for _, s := range v {
		m[s] = true
	}
	return m
}

// ---- 独立命令入口 ----
func standaloneNewOperationArgs(args []string) []string {
	for _, value := range args {
		name, _, _ := strings.Cut(strings.TrimLeft(value, "-"), "=")
		if name == "recover" || name == "cancel-renewal" || name == "checkout-type" {
			return args
		}
	}
	return append([]string{"--checkout-type", "hosted"}, args...)
}
func main() {
	args := os.Args[1:]
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" || args[0] == "help" {
		fmt.Println("独立 cs_live 工具：create-checkout（提链）、pay（付款/恢复）、payment-action（原交易手动验证）。\n用法：go run cs_live.go <子命令> --help\n新操作默认请求 hosted；人机验证入口仅本机有效。")
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	runner := cli_NewRunner()
	code := 2
	switch args[0] {
	case "create-checkout":
		code = runner.CreateCheckout(ctx, standaloneNewOperationArgs(args[1:]))
	case "pay":
		code = runner.Pay(ctx, standaloneNewOperationArgs(args[1:]))
	case "payment-action":
		code = runner.PaymentAction(ctx, args[1:])
	default:
		fmt.Fprintln(os.Stderr, "子命令无效。")
	}
	stop()
	os.Exit(code)
}
