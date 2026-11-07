//go:build linux || darwin

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

type homepageTransport struct{ response *protocol_HTTPResponse }

func (h *homepageTransport) Request(string, string, protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	return h.response, nil
}
func (*homepageTransport) Pause(float64) error { return nil }
func (*homepageTransport) Close()              {}
func (*homepageTransport) Deadline() time.Time { return time.Now().Add(time.Minute) }

type homepageErrorClient struct {
	teamCaptureClient
	err error
}

func (c *homepageErrorClient) CreateCheckout(context.Context, model_Session, gptpay_CreateCheckoutOptions) (model_CheckoutResult, error) {
	return model_CheckoutResult{}, c.err
}

func TestHomepageFailureEvidenceThroughCLI(t *testing.T) {
	secret := "DO-NOT-LOG-COOKIE-OR-TOKEN"
	for _, status := range []int{200, 302, 403} {
		response := &protocol_HTTPResponse{StatusCode: status, Headers: http.Header{
			"Content-Type": {"text/html"}, "Location": {"https://example.test/" + secret},
			"Set-Cookie": {"session=" + secret}, "Cf-Mitigated": {"challenge"}},
			Content: []byte("<html><title>Just a moment</title>" + secret + "</html>")}
		cp := model_DefaultCheckpoint()
		clock, _ := fingerprint_NewClock(0)
		platform := chatgpt_NewPlatform(&homepageTransport{response}, &cp, clock)
		err := platform.Homepage()
		want := "device_id_missing"
		if status != 200 {
			want = "homepage_unavailable"
		}
		if protocol_FlowErrorCode(err) != want {
			t.Fatalf("wrong error: %v", err)
		}
		evidence := checkoutHomepageErrorDiagnostics(err)
		if evidence == nil || evidence.Status != status || !evidence.ChallengeDetected || evidence.DeviceIDPresent || evidence.DeviceCookieReceived || evidence.Redirected != (status == 302) {
			t.Fatalf("incorrect diagnostics: %+v", evidence)
		}
		out, stderr := &bytes.Buffer{}, &bytes.Buffer{}
		runner := cli_NewRunner()
		runner.Out = out
		runner.Err = stderr
		runner.SessionInput = func(context.Context, string) (model_Session, error) { return model_Session{}, nil }
		runner.ClientFactory = func(model_Config, gptpay_ClientOptions) cli_Client { return &homepageErrorClient{err: err} }
		if runner.CreateCheckout(context.Background(), []string{"--country", "PH", "--plan", "pro5x"}) != 1 {
			t.Fatal("failure lost")
		}
		if strings.Contains(out.String()+stderr.String(), secret) {
			t.Fatal("sensitive evidence leaked")
		}
		var payload map[string]any
		if json.Unmarshal(out.Bytes(), &payload) != nil || payload["error_code"] != want || payload["diagnostics"] == nil {
			t.Fatal("CLI dropped evidence")
		}
	}
}

func TestHomepageCookieHandlingUnchanged(t *testing.T) {
	for _, existing := range []bool{false, true} {
		cp := model_DefaultCheckpoint()
		response := &protocol_HTTPResponse{StatusCode: 200, Headers: http.Header{"Content-Type": {"text/html"}}, Content: []byte("<html></html>")}
		if existing {
			cp.Cookies = "oai-did=existing-device"
		} else {
			response.Headers.Add("Set-Cookie", "oai-did=server-device; Path=/")
		}
		clock, _ := fingerprint_NewClock(0)
		platform := chatgpt_NewPlatform(&homepageTransport{response}, &cp, clock)
		if err := platform.Homepage(); err != nil || cp.DeviceID == "" {
			t.Fatalf("valid device lost: %v", err)
		}
	}
}
