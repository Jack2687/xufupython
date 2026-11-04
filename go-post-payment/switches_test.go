package main

import "testing"

func TestOptionalStepsIndependentAndReuseLatestSession(t *testing.T) {
	for _, skipReturn := range []bool{false, true} {
		for _, skipRenewal := range []bool{false, true} {
			steps := []step{}
			if !skipReturn {
				steps = append(steps, step{"/checkout/verify", 200, `{}`, ""})
			}
			steps = append(steps, step{"subscriptions", 200, active, ""})
			if !skipRenewal {
				steps = append(steps, step{"/api/auth/session", 200, refreshed, ""})
			}
			steps = append(steps, step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, off, ""})
			tx, tr := fixture(t, false, steps...)
			tx.skipMerchantReturn = skipReturn
			tx.skipRenewalRefresh = skipRenewal
			tx.cp.ReturnURL = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_fixture"
			if e := tx.finishPayment(); e != nil {
				t.Fatal(e)
			}
			if len(tr.steps) != 0 || tx.cp.Renewal != "off" || tx.cp.CancelAttempts != 1 {
				t.Fatal("incorrect optional sequence")
			}
			for i, path := range tr.paths {
				if path != "GET /api/auth/session" && tr.headers[i].Get("Authorization") != "Bearer new-access" {
					t.Fatal("did not reuse refreshed access token")
				}
			}
		}
	}
}

func TestDisabledProactiveRefreshRecovers401WithConfiguredRetries(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""},
		step{"subscriptions", 401, `{}`, ""}, step{"/api/auth/session", 200, `{}`, ""}, step{"/api/auth/session", 200, refreshed, ""},
		step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 401, `{}`, ""}, step{"/api/auth/session", 200, refreshed, ""},
		step{"subscriptions", 200, active, ""}, step{"subscriptions/cancel", 200, `{}`, ""}, step{"subscriptions", 200, off, ""})
	tx.skipRenewalRefresh = true
	tx.platform.RefreshRetries = 2
	tx.platform.RefreshInterval = 5
	if e := tx.finishPayment(); e != nil {
		t.Fatal(e)
	}
	if len(tr.steps) != 0 || len(tr.pauses) != 1 || tr.pauses[0] != 5 || tx.cp.CancelAttempts != 2 {
		t.Fatal("401 recovery ignored limits or retry configuration")
	}
}

func TestDisabledProactiveRefreshStopsAfterBoundedUnauthorizedWrites(t *testing.T) {
	tx, tr := fixture(t, false, step{"subscriptions", 200, active, ""}, step{"subscriptions", 200, active, ""},
		step{"subscriptions/cancel", 401, `{}`, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""},
		step{"subscriptions/cancel", 401, `{}`, ""}, step{"/api/auth/session", 200, refreshed, ""}, step{"subscriptions", 200, active, ""})
	tx.skipRenewalRefresh = true
	if got := protocol_FlowErrorCode(tx.finishPayment()); got != "http_401" {
		t.Fatal(got)
	}
	if len(tr.steps) != 0 || tx.cp.CancelAttempts != 2 {
		t.Fatal("unbounded cancellation")
	}
}
