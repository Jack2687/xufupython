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
	t      *testing.T
	steps  []step
	paths  []string
	pauses []float64
}

func (f *fixtureTransport) Request(method, target string, opts protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	f.t.Helper()
	if len(f.steps) == 0 {
		f.t.Fatal("unexpected request")
	}
	s := f.steps[0]
	f.steps = f.steps[1:]
	f.paths = append(f.paths, method+" "+s.path)
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
func TestFullCancellationSequence(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, off, ""})
	writes := 0
	tx.saveFn = func(cp *model_Checkpoint, phase string) error {
		if phase == "cancellation_requested" {
			writes++
			if len(tr.paths) != 3 {
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
	if protocol_FlowErrorCode(tx.finishPayment()) != "checkpoint_failed" || len(tr.paths) != 3 {
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
	if protocol_FlowErrorCode(tx.finishPayment()) != "cancellation_pending" || len(tr.paths) != 3 {
		t.Fatal("replayed cancellation")
	}
}
func TestLandingDoesNotReplaceSubscriptionEvidence(t *testing.T) {
	tx, tr := fixture(t, true, step{"/checkout/verify", 403, `<html>challenge</html>`, ""}, step{"subscriptions", 200, active, ""})
	tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
	if tx.finishPayment() != nil || len(tr.paths) != 2 {
		t.Fatal("landing became authority")
	}
}
func TestRefresh403AndMismatchStop(t *testing.T) {
	for _, tc := range []struct {
		status     int
		body, want string
	}{{403, `<html>challenge</html>`, "invalid_json_response"}, {200, `{"accessToken":"other","account":{"id":"other-account"}}`, "account_mismatch"}} {
		tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"/api/auth/session", tc.status, tc.body, ""})
		if protocol_FlowErrorCode(tx.finishPayment()) != tc.want || len(tr.paths) != 2 || tx.cp.AccessToken != "old-access" {
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
