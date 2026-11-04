// Standalone post-payment worker. Only refresh, subscription reads, merchant
// return and cancellation are reachable; no checkout or payment code is linked.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const executorVersion = "go_cslive_v1"
const model_ProgressStageCancellation = "renewal"
const model_ProgressRunning = "running"

var observability_providerID = regexp.MustCompile(`(?:oaics_|cs_live_|cs_test_|pi_|seti_|pm_|ctoken_|bps_)[A-Za-z0-9_]+`)

type model_Checkpoint struct {
	Phase           string       `json:"phase"`
	Payment         string       `json:"payment"`
	Plan            string       `json:"plan"`
	AccountID       string       `json:"account_id"`
	Email           string       `json:"email"`
	CheckoutID      string       `json:"checkout_id"`
	Entity          string       `json:"entity"`
	AccessToken     string       `json:"access_token"`
	SessionToken    string       `json:"session_token"`
	Cookies         string       `json:"cookies"`
	DeviceID        string       `json:"device_id"`
	ReturnURL       string       `json:"return_url"`
	Metadata        protocol_Map `json:"metadata"`
	Activation      string       `json:"activation"`
	Renewal         string       `json:"renewal"`
	CancelRequested bool         `json:"cancel_requested"`
	CancelAttempts  int          `json:"cancel_attempts"`
}

type credentials struct {
	AccessToken  string            `json:"access_token"`
	SessionToken string            `json:"session_token"`
	AccountID    string            `json:"account_id"`
	Email        string            `json:"email"`
	Cookies      map[string]string `json:"cookies"`
}
type cookieSnapshot struct {
	Name    string   `json:"name"`
	Value   string   `json:"value"`
	Domain  string   `json:"domain"`
	Path    string   `json:"path"`
	Expires *float64 `json:"expires"`
}
type state struct {
	Executor        string      `json:"executor"`
	Phase           string      `json:"phase"`
	Plan            string      `json:"plan"`
	AccountID       string      `json:"account_id"`
	CheckoutID      string      `json:"checkout_id"`
	Entity          string      `json:"entity"`
	ReturnURL       string      `json:"return_url"`
	Activation      string      `json:"activation"`
	Renewal         string      `json:"renewal"`
	CancelRequested bool        `json:"cancel_requested"`
	Credentials     credentials `json:"credentials"`
	RequestContext  struct {
		Cookies []cookieSnapshot `json:"cookies"`
	} `json:"request_context"`
	Context *model_Checkpoint `json:"go_context,omitempty"`
}
type input struct {
	Operation      string    `json:"operation"`
	State          state     `json:"state"`
	Proxy          string    `json:"proxy"`
	ProxyExpiresAt time.Time `json:"proxy_expires_at"`
	// 付款后 session 刷新的可配置重试次数与间隔（秒）；0 表示用默认。
	RefreshRetries  int     `json:"refresh_retries"`
	RefreshInterval float64 `json:"refresh_interval_seconds"`
}
type wire struct {
	in  *bufio.Scanner
	out *json.Encoder
}

func (w *wire) emit(value any) error { return w.out.Encode(value) }
func (w *wire) checkpoint(cp *model_Checkpoint, phase string) error {
	if err := w.emit(map[string]any{"type": "checkpoint", "phase": phase, "checkpoint": cp}); err != nil {
		return protocol_NewError("checkpoint_failed")
	}
	if !w.in.Scan() {
		return protocol_NewError("checkpoint_failed")
	}
	var ack struct {
		OK bool `json:"ok"`
	}
	if json.Unmarshal(w.in.Bytes(), &ack) != nil || !ack.OK {
		return protocol_NewError("checkpoint_failed")
	}
	return nil
}

type observability_Observer struct{ sink func(string, protocol_Map) }

func (o *observability_Observer) Emit(event string, fields protocol_Map) {
	if o != nil && o.sink != nil {
		o.sink(event, fields)
	}
}
func enhancedRequestKind(string, string) string { return "other" }

type progress struct{ observer *observability_Observer }

func (p *progress) emit(stage, status, code string) {
	p.observer.Emit("progress", protocol_Map{"phase": stage, "outcome": status, "error_code": code})
}

type gptpay_transaction struct {
	cp       *model_Checkpoint
	http     protocol_HTTPTransport
	platform *chatgpt_Platform
	progress *progress
	saveFn   func(*model_Checkpoint, string) error
}

func (t *gptpay_transaction) save(phase string) error {
	if phase == "cancellation_requested" {
		// A crash/recovery cannot reset the two-write limit.
		if t.cp.CancelAttempts >= 2 {
			return protocol_NewError("cancellation_pending")
		}
		t.cp.CancelAttempts++
	}
	if phase != "" {
		t.cp.Phase = phase
	}
	return t.saveFn(t.cp, phase)
}

func validCookie(name, value string) bool {
	return len(name) > 0 && len(name) <= 200 && len(value) <= 32768 && !strings.ContainsAny(name+value, "\r\n\x00;")
}
func sessionFromCookies(raw string) string {
	req := &http.Request{Header: http.Header{"Cookie": []string{raw}}}
	values := map[string]string{}
	for _, c := range req.Cookies() {
		values[c.Name] = c.Value
	}
	for _, base := range []string{"__Secure-next-auth.session-token", "__Secure-platform.session-token"} {
		if values[base] != "" {
			return values[base]
		}
		parts := []string{}
		for i := 0; i < 100; i++ {
			v := values[base+"."+strconv.Itoa(i)]
			if v == "" {
				break
			}
			parts = append(parts, v)
		}
		if len(parts) > 0 {
			return strings.Join(parts, "")
		}
	}
	return ""
}

func initialCheckpoint(in input) (*model_Checkpoint, error) {
	s := in.State
	if s.Executor != executorVersion || s.AccountID == "" || s.Credentials.AccountID != s.AccountID || s.Credentials.AccessToken == "" {
		return nil, protocol_NewError("checkpoint_mismatch")
	}
	if in.Operation != "finish" && in.Operation != "refresh" {
		return nil, protocol_NewError("invalid_operation")
	}
	cp := s.Context
	if cp != nil {
		if cp.AccountID != s.AccountID || cp.CheckoutID != s.CheckoutID || cp.Plan != s.Plan || cp.CancelRequested != s.CancelRequested || cp.CancelAttempts < 0 || cp.CancelAttempts > 2 {
			return nil, protocol_NewError("checkpoint_mismatch")
		}
	} else {
		cp = &model_Checkpoint{Phase: s.Phase, Payment: "charged", Plan: s.Plan, AccountID: s.AccountID, Email: s.Credentials.Email,
			CheckoutID: s.CheckoutID, Entity: s.Entity, ReturnURL: s.ReturnURL, AccessToken: s.Credentials.AccessToken,
			SessionToken: s.Credentials.SessionToken, Activation: s.Activation, Renewal: s.Renewal, CancelRequested: s.CancelRequested, Metadata: protocol_Map{}}
		values := map[string]string{}
		for name, value := range s.Credentials.Cookies {
			if validCookie(name, value) {
				values[name] = value
			}
		}
		for _, c := range s.RequestContext.Cookies {
			if strings.TrimPrefix(c.Domain, ".") != "chatgpt.com" || c.Path != "" && c.Path != "/" || c.Expires != nil && *c.Expires <= float64(time.Now().Unix()) {
				continue
			}
			if validCookie(c.Name, c.Value) {
				values[c.Name] = c.Value
			}
		}
		if len(values) > 0 {
			// Preserve actual cookies; avoid reintroducing stale unchunked auth beside chunks.
			for _, base := range []string{"__Secure-next-auth.session-token", "__Secure-platform.session-token"} {
				if values[base+".0"] != "" {
					delete(values, base)
				}
			}
			names := []string{}
			for name := range values {
				names = append(names, name)
			}
			sort.Strings(names)
			parts := []string{}
			for _, name := range names {
				parts = append(parts, name+"="+values[name])
			}
			cp.Cookies = strings.Join(parts, "; ")
			cp.DeviceID = values["oai-did"]
		}
	}
	if token := sessionFromCookies(cp.Cookies); token != "" {
		cp.SessionToken = token
	}
	if cp.Metadata == nil {
		cp.Metadata = protocol_Map{}
	}
	if in.Operation == "finish" {
		if cp.Payment != "charged" || cp.CheckoutID == "" || model_findPlan(cp.Plan).id == "" {
			return nil, protocol_NewError("payment_evidence_missing")
		}
	}
	return cp, nil
}

func requestAllowed(cp *model_Checkpoint, method, target string) bool {
	u, e := url.Parse(target)
	if e != nil || u.Scheme != "https" || u.Host != "chatgpt.com" || u.User != nil || u.Fragment != "" {
		return false
	}
	switch u.Path {
	case "/api/auth/session":
		return method == "GET" && u.RawQuery == "refresh=true&reason=token_expired"
	case "/backend-api/subscriptions":
		return method == "GET" && len(u.Query()) == 1 && len(u.Query()["account_id"]) == 1 && u.Query().Get("account_id") == cp.AccountID
	case "/backend-api/subscriptions/cancel":
		return method == "POST" && u.RawQuery == "" && cp.CancelRequested && cp.Activation == "active" && cp.Phase == "cancellation_requested" && cp.CancelAttempts > 0 && cp.CancelAttempts <= 2
	case "/checkout/verify":
		_, e := chatgpt_ValidateReturnURL(target, cp)
		return method == "GET" && e == nil
	}
	return false
}
func requestKind(cp *model_Checkpoint, target string) string {
	u, _ := url.Parse(target)
	if u == nil {
		return ""
	}
	switch u.Path {
	case "/api/auth/session":
		return "session_read"
	case "/checkout/verify":
		return "merchant_return"
	case "/backend-api/subscriptions/cancel":
		return "renewal_update"
	case "/backend-api/subscriptions":
		if strings.HasPrefix(cp.Phase, "cancellation") {
			return "renewal_read"
		}
		return "subscription_read"
	}
	return ""
}

func run(in input, w *wire) error {
	cp, err := initialCheckpoint(in)
	if err != nil {
		return err
	}
	if in.Operation == "finish" && cp.Phase == "completed" {
		return w.emit(map[string]any{"type": "result", "ok": true, "checkpoint": cp})
	}
	if in.Proxy == "" || !in.ProxyExpiresAt.After(time.Now()) {
		return protocol_NewError("proxy_session_expired")
	}
	deadline := time.Now().Add(240 * time.Second)
	if in.ProxyExpiresAt.Before(deadline) {
		deadline = in.ProxyExpiresAt
	}
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()
	transport, err := transport_NewTransport(ctx, in.Proxy, 45, deadline)
	if err != nil {
		return err
	}
	defer transport.Close()
	lastRequestKind := ""
	observer := &observability_Observer{sink: func(event string, fields protocol_Map) {
		if target, ok := fields["path"].(string); ok {
			fields["request_kind"] = requestKind(cp, target)
			if u, e := url.Parse(target); e == nil {
				fields["path"] = u.Path
			}
		}
		if kind, ok := fields["request_kind"].(string); ok && kind != "" {
			lastRequestKind = kind
		} else if event == "http_retry_wait" {
			fields["request_kind"] = lastRequestKind
		}
		_ = w.emit(map[string]any{"type": "event", "event": event, "fields": fields})
	}}
	transport.BindObserver(observer, "chatgpt")
	transport.beforeRequest = func(_ context.Context, method, target string) error {
		if !time.Now().Before(in.ProxyExpiresAt) {
			return protocol_NewError("proxy_session_expired")
		}
		if in.Operation == "refresh" && target != "https://chatgpt.com/api/auth/session?refresh=true&reason=token_expired" {
			return protocol_NewError("endpoint_denied")
		}
		if !requestAllowed(cp, method, target) {
			return protocol_NewError("endpoint_denied")
		}
		observer.Emit("request_started", protocol_Map{"request_kind": requestKind(cp, target)})
		return nil
	}
	platform := chatgpt_NewPlatform(transport, cp, fingerprint_Clock{})
	if in.RefreshRetries > 0 {
		platform.RefreshRetries = in.RefreshRetries
	}
	if in.RefreshInterval > 0 {
		platform.RefreshInterval = in.RefreshInterval
	}
	tx := &gptpay_transaction{cp: cp, http: transport, platform: platform, progress: &progress{observer: observer}, saveFn: w.checkpoint}
	if in.Operation == "refresh" {
		err = platform.Refresh()
	} else {
		err = tx.finishPayment()
	}
	// Final state is returned even on failure so rotated cookies and evidence survive.
	if e := w.emit(map[string]any{"type": "result", "ok": err == nil, "error_code": protocol_FlowErrorCode(err), "checkpoint": cp}); e != nil {
		return e
	}
	return nil
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 65536), 1048576)
	w := &wire{in: scanner, out: json.NewEncoder(os.Stdout)}
	var err error
	func() {
		defer func() {
			if recover() != nil {
				err = errors.New("internal_error")
			}
		}()
		if !scanner.Scan() {
			err = protocol_NewError("input_invalid")
			return
		}
		var in input
		if json.Unmarshal(scanner.Bytes(), &in) != nil {
			err = protocol_NewError("input_invalid")
			return
		}
		err = run(in, w)
	}()
	if err != nil {
		_ = w.emit(map[string]any{"type": "result", "ok": false, "error_code": protocol_FlowErrorCode(err)})
	}
}
