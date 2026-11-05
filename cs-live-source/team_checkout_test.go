//go:build linux || darwin

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"
)

type teamCaptureTransport struct{ body map[string]any }

func (s *teamCaptureTransport) Request(method, url string, o protocol_RequestOptions) (*protocol_HTTPResponse, error) {
	b, e := json.Marshal(o.Body)
	if e != nil {
		return nil, e
	}
	if e = json.Unmarshal(b, &s.body); e != nil {
		return nil, e
	}
	return &protocol_HTTPResponse{StatusCode: 200, Headers: http.Header{}, Content: []byte(`{}`)}, nil
}
func (*teamCaptureTransport) Pause(float64) error { return nil }
func (*teamCaptureTransport) Close()              {}
func (*teamCaptureTransport) Deadline() time.Time { return time.Now().Add(time.Minute) }

func TestTeamCheckoutExactUpstreamSeatPayload(t *testing.T) {
	for _, counts := range [][2]int{{2, 0}, {1, 1}, {0, 2}, {2, 4}} {
		for _, promo := range []string{"", "OWN-TEAM"} {
			transport := &teamCaptureTransport{}
			cp := &model_Checkpoint{Plan: "team", Country: "US", Currency: "USD", Metadata: protocol_Map{"team_default_seats": counts[0], "team_prolite_seats": counts[1], "team_price_interval": "month"}}
			p := &chatgpt_Platform{HTTP: transport, CP: cp, PromoCode: promo}
			if _, err := p.Checkout("chatgptteamplan", [2]string{}, "hosted"); err != nil {
				t.Fatal(err)
			}
			b := transport.body
			if b["plan_name"] != "chatgptteamplan" {
				t.Fatal("wrong plan")
			}
			team := b["team_plan_data"].(map[string]any)
			if team["quantity"] != float64(counts[0]+counts[1]) || team["seat_quantity"] != float64(counts[0]+counts[1]) {
				t.Fatal("wrong total")
			}
			got := map[string]int{}
			for _, v := range team["seat_quantities"].([]any) {
				r := v.(map[string]any)
				got[r["seat_type"].(string)] = int(r["quantity"].(float64))
			}
			want := map[string]int{}
			if counts[0] > 0 {
				want["default"] = counts[0]
			}
			if counts[1] > 0 {
				want["prolite"] = counts[1]
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("got %v want %v", got, want)
			}
			if promo == "" {
				if _, ok := b["promo_code"]; ok {
					t.Fatal("empty promo sent")
				}
			} else if b["promo_code"] != promo {
				t.Fatal("promo lost")
			}
		}
	}
}

type teamCaptureClient struct{ options gptpay_CreateCheckoutOptions }

func (c *teamCaptureClient) CreateCheckout(_ context.Context, _ model_Session, o gptpay_CreateCheckoutOptions) (model_CheckoutResult, error) {
	c.options = o
	return model_CheckoutResult{CheckoutID: "cs_fixture", CheckoutURL: "https://pay.example.test"}, nil
}
func (*teamCaptureClient) Pay(context.Context, model_Session, model_Card, gptpay_PayOptions) (model_Result, error) {
	return model_Result{}, errors.New("unexpected payment")
}
func (*teamCaptureClient) Recover(context.Context, model_Checkpoint) (model_Result, error) {
	return model_Result{}, errors.New("unexpected recovery")
}
func (*teamCaptureClient) CancelRenewal(context.Context, model_Session, string) (model_Result, error) {
	return model_Result{}, errors.New("unexpected cancellation")
}

func TestTeamCLIOptionalPromoAndMinimum(t *testing.T) {
	for _, count := range [][2]string{{"2", "0"}, {"1", "1"}, {"0", "2"}} {
		client := &teamCaptureClient{}
		out := &bytes.Buffer{}
		stderr := &bytes.Buffer{}
		runner := cli_NewRunner()
		runner.Out = out
		runner.Err = stderr
		runner.SessionInput = func(context.Context, string) (model_Session, error) { return model_Session{}, nil }
		runner.ClientFactory = func(model_Config, gptpay_ClientOptions) cli_Client { return client }
		t.Setenv("TEAM_TEST_PROMO", "OWN-SECRET-CODE")
		args := []string{"--country", "US", "--plan", "team", "--seats", count[0], "--seats-5x", count[1], "--promo-code-env", "TEAM_TEST_PROMO"}
		if code := runner.CreateCheckout(context.Background(), args); code != 0 {
			t.Fatalf("exit %d: %s", code, stderr.String())
		}
		if client.options.PromoCode != "OWN-SECRET-CODE" {
			t.Fatal("promo not passed to checkout")
		}
		if strings.Contains(out.String()+stderr.String(), "OWN-SECRET-CODE") {
			t.Fatal("promo leaked")
		}
	}
	for _, args := range [][]string{{"--country", "US", "--plan", "team", "--seats", "1"}, {"--country", "US", "--plan", "plus", "--promo-code-env", "TEAM_TEST_PROMO"}} {
		if _, err := cli_parseArguments(args, false, &bytes.Buffer{}); err == nil {
			t.Fatal("invalid options accepted")
		}
	}
}
