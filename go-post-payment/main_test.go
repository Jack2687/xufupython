package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

type step struct {
	path    string
	status  int
	body    string
	failure string
}
type fixtureTransport struct {
	t       *testing.T
	steps   []step
	paths   []string
	headers []protocol_Fields
	pauses  []float64
}

func (f *fixtureTransport) Request(method, target string, opts protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	f.t.Helper()
	if len(f.steps) == 0 {
		f.t.Fatal("unexpected request")
	}
	s := f.steps[0]
	f.steps = f.steps[1:]
	f.paths = append(f.paths, method+" "+s.path)
	f.headers = append(f.headers, opts.Headers)
	if !strings.Contains(target, s.path) {
		f.t.Fatalf("wrong endpoint: want %s", s.path)
	}
	if strings.Contains(s.path, "/api/auth/session") && opts.Headers.Get("Authorization") != nil {
		f.t.Fatal("refresh sent Authorization")
	}
	if s.failure != "" {
		return nil, protocol_NewError(s.failure)
	}
	headers := http.Header{"Content-Type": []string{"application/json"}}
	if strings.Contains(s.path, "/api/auth/session") && s.status == 200 {
		headers.Add("Set-Cookie", "__Secure-next-auth.session-token=rotated; Path=/; Secure")
	}
	return &protocol_HTTPResponse{StatusCode: s.status, Headers: headers, Content: []byte(s.body)}, nil
}
func (f *fixtureTransport) Pause(s float64) error { f.pauses = append(f.pauses, s); return nil }
func (*fixtureTransport) Close()                  {}
func (*fixtureTransport) Deadline() time.Time     { return time.Now().Add(time.Hour) }

const active = `{"plan_type":"standard","will_renew":true}`
const off = `{"plan_type":"standard","will_renew":false,"active_until":"2099-01-01"}`
const refreshed = `{"accessToken":"new-access","account":{"id":"fixture-account"}}`

func fixture(t *testing.T, keep bool, steps ...step) (*gptpay_transaction, *fixtureTransport) {
	cp := &model_Checkpoint{Phase: "activation", Payment: "charged", Plan: "pro5x", AccountID: "fixture-account", CheckoutID: "cs_fixture", Entity: "openai_llc", AccessToken: "old-access", SessionToken: "session-fixture", Activation: "pending", Renewal: "unknown", CancelRequested: !keep, Metadata: protocol_Map{}}
	steps = append([]step{{"/api/auth/session", 200, refreshed, ""}}, steps...)
	tr := &fixtureTransport{t: t, steps: steps}
	p := chatgpt_NewPlatform(tr, cp, fingerprint_Clock{})
	return &gptpay_transaction{cp: cp, http: tr, platform: p, progress: &progress{}, saveFn: func(*model_Checkpoint, string) error { return nil }}, tr
}
func TestFinish401RefreshKeep(t *testing.T) {
	tx, tr := fixture(t, true, step{"subscriptions", 401, `{}`, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""})
	if err := tx.finishPayment(); err != nil {
		t.Fatal(err)
	}
	if len(tr.steps) != 0 || tx.cp.Phase != "completed" || tx.cp.AccessToken != "new-access" || !strings.Contains(tx.cp.Cookies, "rotated") {
		t.Fatal("lost refreshed state")
	}
}
func TestInitialRefreshSavedBeforeReturnAndSubscription(t *testing.T) {
	tx, tr := fixture(t, true,
		step{"/checkout/verify", 200, `<html>returned</html>`, ""},
		step{"subscriptions", 200, active, ""},
	)
	tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
	savedBeforeReturn := false
	tx.saveFn = func(cp *model_Checkpoint, phase string) error {
		if len(tr.paths) == 1 && phase == "" {
			savedBeforeReturn = cp.AccessToken == "new-access" && strings.Contains(cp.Cookies, "rotated")
		}
		return nil
	}
	if err := tx.finishPayment(); err != nil {
		t.Fatal(err)
	}
	if !savedBeforeReturn || len(tr.steps) != 0 || tx.cp.Phase != "completed" {
		t.Fatal("initial refresh was not saved before return")
	}
	for _, i := range []int{1, 2} {
		if tr.headers[i].Get("Authorization") != "Bearer new-access" || !strings.Contains(protocol_AsString(tr.headers[i].Get("Cookie")), "rotated") {
			t.Fatal("return or subscription query used pre-refresh credentials")
		}
	}
}

func TestInitialRefreshUsesConfiguredRetries(t *testing.T) {
	for _, tc := range []struct {
		name                string
		attempts            int
		interval            float64
		configured, success bool
	}{
		{"default_success", 4, 3, false, true},
		{"default_exhausted", 4, 3, false, false},
		{"production_success", 4, 5, true, true},
		{"production_exhausted", 4, 5, true, false},
		{"custom_success", 2, 1, true, true},
		{"custom_exhausted", 2, 1, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tx, tr := fixture(t, true)
			tr.steps = nil
			tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
			if tc.configured {
				tx.platform.RefreshRetries = tc.attempts
				tx.platform.RefreshInterval = tc.interval
			}
			for i := 0; i < tc.attempts; i++ {
				body := `{}`
				if tc.success && i == tc.attempts-1 {
					body = refreshed
				}
				tr.steps = append(tr.steps, step{"/api/auth/session", 200, body, ""})
			}
			want := "session_expired"
			if tc.success {
				want = ""
				tr.steps = append(tr.steps, step{"/checkout/verify", 200, "", ""}, step{"subscriptions", 200, active, ""})
			}
			if got := protocol_FlowErrorCode(tx.finishPayment()); got != want {
				t.Fatalf("got %s want %s", got, want)
			}
			if len(tr.steps) != 0 || len(tr.pauses) != tc.attempts-1 || tx.cp.Payment != "charged" || tx.cp.CancelAttempts != 0 {
				t.Fatal("initial refresh retry bound or payment state changed")
			}
			for _, pause := range tr.pauses {
				if pause != tc.interval {
					t.Fatalf("interval = %v, want %v", pause, tc.interval)
				}
			}
			if !tc.success && (tx.cp.Activation != "pending" || tx.cp.AccessToken != "old-access") {
				t.Fatal("failed refresh advanced activation")
			}
		})
	}
}

func TestInitialRefreshFailureBlocksReturn(t *testing.T) {
	for _, tc := range []struct {
		name, body, want string
		status           int
		saveFailure      bool
	}{
		{"denied", `<html>denied</html>`, "invalid_json_response", 403, false},
		{"mismatch", `{"accessToken":"other","account":{"id":"other-account"}}`, "account_mismatch", 200, false},
		{"save_failed", refreshed, "checkpoint_failed", 200, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tx, tr := fixture(t, false)
			tr.steps = []step{{"/api/auth/session", tc.status, tc.body, ""}}
			tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
			if tc.saveFailure {
				tx.saveFn = func(_ *model_Checkpoint, phase string) error {
					if phase == "" {
						return protocol_NewError("checkpoint_failed")
					}
					return nil
				}
			}
			if got := protocol_FlowErrorCode(tx.finishPayment()); got != tc.want {
				t.Fatalf("got %s want %s", got, tc.want)
			}
			if len(tr.paths) != 1 || len(tr.pauses) != 0 || tx.cp.Payment != "charged" || tx.cp.CancelAttempts != 0 {
				t.Fatal("initial refresh failure advanced to return or cancellation")
			}
		})
	}
}
func TestFinish401RefreshOnLaterActivationRound(t *testing.T) {
	tx, tr := fixture(t, true,
		step{"subscriptions", 401, `{}`, ""},
		step{"/api/auth/session", 200, refreshed, ""},
		step{"subscriptions", 404, `{"detail":"No subscription found for account"}`, ""},
		step{"subscriptions", 401, `{}`, ""},
		step{"/api/auth/session", 200, refreshed, ""},
		step{"subscriptions", 200, active, ""},
	)
	if err := tx.finishPayment(); err != nil {
		t.Fatal(err)
	}
	if len(tr.steps) != 0 || len(tr.pauses) != 1 || tr.pauses[0] != 2 || tx.cp.Phase != "completed" {
		t.Fatal("later activation round did not refresh and complete")
	}
}
func TestCancellationRefreshesAfterActivationRefresh(t *testing.T) {
	tx, tr := fixture(t, false,
		step{"subscriptions", 401, `{}`, ""},
		step{"/api/auth/session", 200, refreshed, ""},
		step{"subscriptions", 200, active, ""},
		step{"/api/auth/session", 200, `{"accessToken":"renewal-access","account":{"id":"fixture-account"}}`, ""},
		step{"subscriptions", 200, active, ""},
		step{"subscriptions/cancel", 200, `{}`, ""},
		step{"subscriptions", 200, off, ""},
	)
	if err := tx.finishPayment(); err != nil {
		t.Fatal(err)
	}
	if len(tr.steps) != 0 || tx.cp.AccessToken != "renewal-access" || tx.cp.Renewal != "off" || tx.cp.Phase != "completed" {
		t.Fatal("cancellation did not use its own refresh")
	}
}
func TestEmptyTokenRefreshRetries(t *testing.T) {
	for _, tc := range []struct {
		name       string
		attempts   int
		interval   float64
		configured bool
		success    bool
	}{
		{"default_success", 4, 3, false, true},
		{"default_exhausted", 4, 3, false, false},
		{"configured_success", 2, 1, true, true},
		{"configured_exhausted", 2, 1, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			steps := []step{{"subscriptions", 200, active, ""}}
			for i := 0; i < tc.attempts; i++ {
				body := `{}`
				if tc.success && i == tc.attempts-1 {
					body = refreshed
				}
				steps = append(steps, step{"/api/auth/session", 200, body, ""})
			}
			want := "session_expired"
			if tc.success {
				want = ""
				steps = append(steps, step{"subscriptions", 200, off, ""})
			}
			tx, tr := fixture(t, false, steps...)
			if tc.configured {
				tx.platform.RefreshRetries = tc.attempts
				tx.platform.RefreshInterval = tc.interval
			}
			if got := protocol_FlowErrorCode(tx.finishPayment()); got != want {
				t.Fatalf("got %s want %s", got, want)
			}
			if len(tr.steps) != 0 || len(tr.pauses) != tc.attempts-1 || tx.cp.Payment != "charged" || tx.cp.CancelAttempts != 0 {
				t.Fatal("refresh retry limit or payment state changed")
			}
			for _, pause := range tr.pauses {
				if pause != tc.interval {
					t.Fatalf("refresh interval = %v, want %v", pause, tc.interval)
				}
			}
		})
	}
}
func TestFullCancellationSequence(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, off, ""})
	writes := 0
	tx.saveFn = func(cp *model_Checkpoint, phase string) error {
		if phase == "cancellation_requested" {
			writes++
			if len(tr.paths) != 4 {
				t.Fatal("write preceded checkpoint")
			}
		}
		return nil
	}
	if err := tx.finishPayment(); err != nil {
		t.Fatal(err)
	}
	if writes != 1 || tx.cp.CancelAttempts != 1 || tx.cp.Renewal != "off" || len(tr.steps) != 0 {
		t.Fatal("wrong cancellation outcome")
	}
}
func TestCancelledOrUnknownSkipsWrite(t *testing.T) {
	for _, tc := range []struct{ body, want string }{{off, ""}, {`{}`, "renewal_state_unknown"}} {
		tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, tc.body, ""})
		if got := protocol_FlowErrorCode(tx.finishPayment()); got != tc.want {
			t.Fatalf("got %s want %s", got, tc.want)
		}
		for _, p := range tr.paths {
			if strings.HasPrefix(p, "POST") {
				t.Fatal("unexpected cancellation")
			}
		}
	}
}
func TestLostCancellationResponseChecksBeforeRetry(t *testing.T) {
	for _, closed := range []bool{true, false} {
		check := active
		if closed {
			check = off
		}
		steps := []step{{"subscriptions", 200, active, ""}, {"/api/auth/session", 200, refreshed, ""}, {"subscriptions", 200, active, ""}, {"subscriptions/cancel", 0, "", "network_error"}, {"subscriptions", 200, check, ""}}
		if !closed {
			steps = append(steps, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, off, ""})
		}
		tx, tr := fixture(t, false, steps...)
		if e := tx.finishPayment(); e != nil {
			t.Fatal(e)
		}
		want := 2
		if closed {
			want = 1
		}
		if tx.cp.CancelAttempts != want || len(tr.steps) != 0 {
			t.Fatal("bad cancellation retry")
		}
	}
}
func TestCheckpointFailurePreventsCancellation(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""})
	tx.saveFn = func(_ *model_Checkpoint, phase string) error {
		if phase == "cancellation_requested" {
			return protocol_NewError("checkpoint_failed")
		}
		return nil
	}
	if protocol_FlowErrorCode(tx.finishPayment()) != "checkpoint_failed" || len(tr.paths) != 4 {
		t.Fatal("cancellation crossed failed checkpoint")
	}
}
func TestBoundedActivationAndCancellation(t *testing.T) {
	missing := step{"subscriptions", 404, `{"detail":"No subscription found for account"}`, ""}
	tx, tr := fixture(t, false, missing, missing, missing, missing, missing)
	if protocol_FlowErrorCode(tx.finishPayment()) != "activation_pending" || len(tr.pauses) != 4 {
		t.Fatal("unbounded activation")
	}
	tx, tr = fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions", 200, active, ""})
	if protocol_FlowErrorCode(tx.finishPayment()) != "cancellation_pending" || len(tr.pauses) != 4 {
		t.Fatal("unbounded cancellation check")
	}
}
func TestRecoveryCannotResetCancellationLimit(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""})
	tx.cp.CancelAttempts = 2
	if protocol_FlowErrorCode(tx.finishPayment()) != "cancellation_pending" || len(tr.paths) != 4 {
		t.Fatal("replayed cancellation")
	}
}
func TestLandingDoesNotReplaceSubscriptionEvidence(t *testing.T) {
	tx, tr := fixture(t, true, step{"/checkout/verify", 403, `<html>challenge</html>`, ""}, step{"subscriptions", 200, active, ""})
	tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
	if tx.finishPayment() != nil || len(tr.paths) != 3 {
		t.Fatal("landing became authority")
	}
}
func TestRefresh403AndMismatchStop(t *testing.T) {
	for _, tc := range []struct {
		status     int
		body, want string
	}{{403, `<html>challenge</html>`, "invalid_json_response"}, {200, `{"accessToken":"other","account":{"id":"other-account"}}`, "account_mismatch"}} {
		tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", tc.status, tc.body, ""})
		if protocol_FlowErrorCode(tx.finishPayment()) != tc.want || len(tr.paths) != 3 || tx.cp.AccessToken != "new-access" {
			t.Fatal("refresh error was ignored")
		}
	}
}
func TestEndpointAllowlistAndProxyExpiry(t *testing.T) {
	tx, _ := fixture(t, false)
	cp := tx.cp
	for _, target := range []string{"https://api.stripe.com/v1/payment_intents", "https://chatgpt.com/backend-api/payments/checkout", "http://chatgpt.com/api/auth/session", "https://chatgpt.com.evil.test/api/auth/session", "https://chatgpt.com/backend-api/subscriptions?account_id=other"} {
		if requestAllowed(cp, "GET", target) {
			t.Fatal("endpoint accepted")
		}
	}
	if requestAllowed(cp, "POST", "https://chatgpt.com/backend-api/subscriptions/cancel") {
		t.Fatal("cancel without saved evidence")
	}
	cp.Activation = "active"
	cp.Phase = "cancellation_requested"
	cp.CancelAttempts = 1
	if !requestAllowed(cp, "POST", "https://chatgpt.com/backend-api/subscriptions/cancel") {
		t.Fatal("valid cancel blocked")
	}
}
func TestPipeCheckpointRequiresPositiveAcknowledgement(t *testing.T) {
	for _, s := range []string{"", "{}\n", "{\"ok\":false}\n", "not-json\n"} {
		var out bytes.Buffer
		w := &wire{in: bufio.NewScanner(strings.NewReader(s)), out: json.NewEncoder(&out)}
		if protocol_FlowErrorCode(w.checkpoint(&model_Checkpoint{}, "cancellation_requested")) != "checkpoint_failed" {
			t.Fatal("missing acknowledgement allowed")
		}
	}
}
func TestInitialCookieAndAccountIsolation(t *testing.T) {
	in := input{Operation: "refresh", State: state{Executor: executorVersion, AccountID: "fixture", Credentials: credentials{AccountID: "fixture", AccessToken: "access", SessionToken: "session"}}}
	in.State.RequestContext.Cookies = []cookieSnapshot{{Name: "__Secure-next-auth.session-token.0", Value: "a", Domain: "chatgpt.com", Path: "/"}, {Name: "__Secure-next-auth.session-token.1", Value: "b", Domain: "chatgpt.com", Path: "/"}, {Name: "__Secure-next-auth.session-token", Value: "stale", Domain: "chatgpt.com", Path: "/"}, {Name: "other", Value: "foreign", Domain: "evil.test", Path: "/"}}
	cp, err := initialCheckpoint(in)
	if err != nil || cp.SessionToken != "ab" || strings.Contains(cp.Cookies, "stale") || strings.Contains(cp.Cookies, "foreign") {
		t.Fatal("cookie state corrupted")
	}
	in.State.Context = cp
	in.State.AccountID = "other"
	if _, err = initialCheckpoint(in); err == nil {
		t.Fatal("cross-account context accepted")
	}
}
