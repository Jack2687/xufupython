// Extracted from xupay/internal/cslive/transport_transport.go; see SOURCE.json.
package main

import (
	context "context"
	fmt "fmt"
	fhttp "github.com/bogdanfinn/fhttp"
	fhttpcookiejar "github.com/bogdanfinn/fhttp/cookiejar"
	fhttptrace "github.com/bogdanfinn/fhttp/httptrace"
	tls_client "github.com/bogdanfinn/tls-client"
	profiles "github.com/bogdanfinn/tls-client/profiles"
	publicsuffix "golang.org/x/net/publicsuffix"
	io "io"
	math "math"
	net "net"
	http "net/http"
	url "net/url"
	strings "strings"
	time "time"
)

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
	enhanced      bool
	beforeRequest func(context.Context, string, string) error
	ctx           context.Context
	cancel        context.CancelFunc
	timeout       time.Duration
	expires       time.Time
	jar           fhttp.CookieJar
	client        tls_client.HttpClient
	options       []tls_client.HttpClientOption
	observer      *observability_Observer
	service       string
	proxySecrets  []string
	prepared      *transport_preparedConnection
	preparedURL   string
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

func (t *transport_Transport) EnableEnhancedDiagnostics() { t.enhanced = true }

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
		if t.beforeRequest != nil {
			if err := t.beforeRequest(ctx, method, rawURL); err != nil {
				cancel()
				return nil, err
			}
		}
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
	if t.enhanced {
		kind := enhancedRequestKind(method, rawURL)
		fields["request_kind"] = kind
		if kind != "other" {
			fields["required_step"] = kind != "telemetry" && kind != "card_metadata"
		}
	}
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

		if u != nil && u.Hostname() == "chatgpt.com" && chatgpt_isSubscriptionNotFound(method, u.EscapedPath(), response) {
			fields["error_code"] = "subscription_not_found"
		}
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
