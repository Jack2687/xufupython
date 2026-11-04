// Extracted from xupay/internal/cslive/gptpay_flow_recovery.go; see SOURCE.json.
package main

import ()

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
