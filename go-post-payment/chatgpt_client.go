// Extracted from xupay/internal/cslive/chatgpt_client.go; see SOURCE.json.
package main

import (
	http "net/http"
	url "net/url"
	strings "strings"
)

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
