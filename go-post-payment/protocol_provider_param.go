// Extracted from xupay/internal/cslive/protocol_provider_param.go; see SOURCE.json.
package main

import (
	strings "strings"
)

// SafeProviderParam 仅接受已知请求字段路径，不接受字段值、任意标识符或原始错误描述。
func protocol_SafeProviderParam(value string) string {
	if len(value) == 0 || len(value) > 200 {
		return "redacted"
	}
	parts := strings.Split(strings.ReplaceAll(value, "]", ""), "[")
	if strings.Count(value, "[") != strings.Count(value, "]") {
		return "redacted"
	}
	allowed := protocol_StringSet("expected_payment_method_type", "expected_amount", "key", "_stripe_version", "return_url", "init_checksum", "version", "elements_session_client", "elements_options_client", "client_attribution_metadata", "consent", "terms_of_service", "guid", "muid", "sid", "eid", "radar_options", "radar_session", "session", "payment_method", "confirmation_token", "js_checksum", "rv_timestamp", "referrer", "last_displayed_line_item_group_details", "subtotal", "total_exclusive_tax", "total_inclusive_tax", "total_discount_amount", "shipping_rate_amount", "elements_init_source", "referrer_host", "stripe_js_id", "locale", "is_aggregation_expected", "session_id", "client_betas", "saved_payment_method", "enable_save", "enable_redisplay", "client_session_id", "checkout_session_id", "merchant_integration_source", "merchant_integration_version", "merchant_integration_subtype", "payment_intent_creation_flow", "payment_method_selection_flow", "elements_session_id", "checkout_config_id", "elements_session_config_id", "client_secret", "payment_method_data", "type", "card", "number", "cvc", "exp_month", "exp_year", "billing_details", "name", "email", "address", "line1", "line2", "city", "state", "postal_code", "country", "0", "1")
	for _, part := range parts {
		if !allowed[part] && !protocol_checkoutDiagnosticFields[part] {
			return "redacted"
		}
	}

	canonical := parts[0]
	for _, part := range parts[1:] {
		canonical += "[" + part + "]"
	}
	if canonical != value {
		return "redacted"
	}
	return value
}

// 仅保留结算请求的字段名，不接受字段值或未知参数。
var protocol_checkoutDiagnosticFields = protocol_StringSet("plan_name", "currency", "checkout_ui_mode", "cancel_url", "entry_point", "promo_code", "team_plan_data", "workspace_name", "quantity", "seat_quantity", "price_interval", "seat_quantities")
