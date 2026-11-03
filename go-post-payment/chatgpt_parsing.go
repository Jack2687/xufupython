// Extracted from xupay/internal/cslive/chatgpt_parsing.go; see SOURCE.json.
package main

import (
	url "net/url"
	strings "strings"
	unicode "unicode"
)

func chatgpt_ValidateReturnURL(raw string, cp *model_Checkpoint) (string, error) {
	parsed, err := url.Parse(raw)
	invalid := err != nil
	if !invalid {
		invalid = parsed.Scheme != "https" || parsed.Host != "chatgpt.com" || parsed.User != nil || parsed.Path != "/checkout/verify" || parsed.Fragment != "" || strings.Contains(raw, "\\")
	}
	for _, r := range raw {
		if unicode.IsSpace(r) || r < 32 {
			invalid = true
		}
	}
	if !invalid {
		query, queryErr := url.ParseQuery(parsed.RawQuery)
		invalid = queryErr != nil || !chatgpt_oneQuery(query, "stripe_session_id", cp.CheckoutID)
		for _, field := range [][2]string{{"account_id", cp.AccountID}, {"processor_entity", cp.Entity}} {
			if _, exists := query[field[0]]; exists && !chatgpt_oneQuery(query, field[0], field[1]) {
				invalid = true
			}
		}
	}
	if invalid {
		return "", &protocol_ProtocolError{Code: "invalid_return_url"}
	}
	return raw, nil
}

func chatgpt_oneQuery(query url.Values, key, expected string) bool {
	values := query[key]
	return len(values) == 1 && values[0] == expected
}

func chatgpt_RenewalState(data protocol_Map) string {
	states := map[string]bool{}
	items := []any{data}
	if values, ok := protocol_FirstTruthy(data["subscriptions"], data["items"]).([]any); ok {
		items = append(items, values...)
	}
	for _, value := range items {
		item := protocol_AsMap(value)
		if item == nil {
			continue
		}
		if item["will_renew"] == false || item["cancel_at_period_end"] == true || protocol_Truthy(item["cancels_at"]) || protocol_Truthy(item["cancel_at"]) {
			states["off"] = true
		}
		if item["will_renew"] == true || item["cancel_at_period_end"] == false {
			states["on"] = true
		}
	}
	if len(states) == 1 {
		for state := range states {
			return state
		}
	}
	return "unknown"
}

func chatgpt_SubscriptionActive(data protocol_Map, plan string) bool {
	if !model_MatchesSubscriptionType(plan, strings.ToLower(protocol_AsString(data["plan_type"]))) {
		return false
	}
	return protocol_Truthy(protocol_FirstTruthy(data["active_until"], data["current_period_end"], data["expires_at"], data["billing_period_end"])) || data["will_renew"] == true
}
