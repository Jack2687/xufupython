"""Safe Go checkout error codes, kept separate from post-payment errors."""
import re

# Protocol literals emitted by the bundled cs_live CLI. Never log arbitrary
# stdout/stderr, messages, credentials, or unknown strings as an error code.
SAFE_CODES = frozenset('''
radar_session_missing billing_automatic_tax_unrecognized_customer_location
parameter_unknown catalog_prices_invalid catalog_prices_region_mismatch
protocol_error network_timeout account_mismatch account_not_found action_required
activation_pending subscription_not_found approval_blocked authorized
cancellation_pending charged checkout_amount_changed checkout_currency_mismatch
checkout_id_invalid checkout_total_missing checkout_price_missing checkout_price_failed
checkout_context_mismatch checkpoint_failed confirmation_token_missing deadline_exceeded
device_id_missing existing_subscription homepage_unavailable hosted_page_unavailable
hosted_url_invalid hosted_url_missing https_required init_checksum_missing
invalid_json_response invalid_return_url network_error not_attempted not_charged
payment_intent_secret_missing payment_intent_state_unavailable
payment_confirmation_already_submitted payment_method_missing payment_page_id_missing
payment_not_submitted payment_pending pending personal_account_required
processor_entity_invalid publishable_key_missing recovery_failed renewal_state_unknown
sentinel_challenge_missing sentinel_context_mismatch sentinel_dx_missing
sentinel_incomplete sentinel_pow_missing sentinel_turnstile_error sentinel_turnstile_missing
session_expired session_token_required stripe_build_missing stripe_identity_missing
unexpected_redirect unknown unsupported_processor_entity checkout_ui_mode_invalid
payment_approval_failed payment_cancelled input_or_checkpoint_invalid cancelled
timeout connection proxy_connect dns tls invalid_session invalid_response
checkout_failed checkout_program_unavailable redacted_error
'''.split())


def safe_code(value):
    if isinstance(value, str) and (value in SAFE_CODES or re.fullmatch(r'http_[1-5]\d\d', value)):
        return value
    return 'redacted_error'


def classify(code):
    if re.fullmatch(r'http_[1-5]\d\d', code):
        status = int(code[5:])
        if status == 401: return 'invalid_session'
        if status == 403: return 'access_denied'
        if status == 408: return 'timeout'
        if status == 429 or status >= 500: return 'upstream_busy'
        return 'request_rejected'
    if code in {'network_timeout', 'timeout', 'deadline_exceeded'}: return 'timeout'
    if code == 'network_error': return 'connection'
    if code in {'connection', 'proxy_connect', 'dns', 'tls', 'account_mismatch'}: return code
    if code in {'invalid_session', 'session_expired', 'session_token_required'}: return 'invalid_session'
    if code == 'existing_subscription': return 'checkout_existing_subscription'
    if code in {'account_not_found', 'personal_account_required'}: return 'checkout_account_unavailable'
    if code.startswith('sentinel_'): return 'checkout_verification_failed'
    if code in {'invalid_response', 'invalid_json_response', 'protocol_error', 'input_or_checkpoint_invalid'}:
        return 'invalid_response'
    if code == 'checkout_program_unavailable': return code
    return 'checkout_failed'


class CheckoutError(Exception):
    def __init__(self, code, message='', *, exit_code=None):
        self.code = safe_code(code)
        self.category = classify(self.code)
        self.exit_code = exit_code if type(exit_code) is int and -65535 <= exit_code <= 65535 else None
        # Messages are deliberately excluded: a future upstream error may echo a token.
        super().__init__(self.code)
