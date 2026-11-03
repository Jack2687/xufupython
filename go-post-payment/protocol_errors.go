// Extracted from xupay/internal/cslive/protocol_errors.go; see SOURCE.json.
package main

import (
	context "context"
	errors "errors"
)

func protocol_FlowErrorCode(err error) string {
	if err == nil {
		return ""
	}
	var pe *protocol_ProtocolError
	if errors.As(err, &pe) {
		return pe.Code
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return "deadline_exceeded"
	}
	return "protocol_error"
}

type protocol_ProtocolError struct{ Code string }

func protocol_NewError(code string) error { return &protocol_ProtocolError{Code: code} }

func (e *protocol_ProtocolError) Error() string { return e.Code }
