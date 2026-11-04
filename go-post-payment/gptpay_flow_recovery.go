// Extracted from xupay/internal/cslive/gptpay_flow_recovery.go; see SOURCE.json.
package main

import ()

func (t *gptpay_transaction) finishPayment() error {
	// 付款成功后 OpenAI 会作废旧 access_token 并轮换会话，这里统一刷新一次拿到新令牌；
	// 之后的核实订阅、关闭续费都复用这个新令牌，不再各自刷新（付款后 OpenAI 数秒内不会
	// 再次轮换，紧接着的二次刷新会持续返回不含令牌的 200 而被误判为 session_expired）。
	if e := t.platform.Refresh(); e != nil {
		return e
	}
	if e := t.save(""); e != nil {
		return e
	}
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
	// 令牌已在 finishPayment 开头统一刷新，这里只轮询订阅是否生效，不再单独刷新。
	for i := 0; i < 5; i++ {
		if i > 0 {
			if e := t.http.Pause(2); e != nil {
				return e
			}
		}
		data, e := t.platform.Subscriptions(false)
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
	// 令牌已在 finishPayment 开头统一刷新，这里不再重复刷新，直接复用新令牌关闭续费。
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
