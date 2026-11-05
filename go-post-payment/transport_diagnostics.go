package main

import (
	"context"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"syscall"
)

// Classify wrapped errors without logging err.Error(): it can contain proxy
// credentials, destination addresses and request URLs.
func transport_networkErrorClass(err error) string {
	var dns *net.DNSError
	var hostname x509.HostnameError
	var authority x509.UnknownAuthorityError
	var certificate x509.CertificateInvalidError
	var network net.Error
	switch {
	case errors.As(err, &dns):
		if dns.IsTimeout {
			return "dns_timeout"
		}
		return "dns_failure"
	case errors.As(err, &hostname), errors.As(err, &authority), errors.As(err, &certificate):
		return "tls_certificate"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, syscall.ECONNRESET):
		return "connection_reset"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "connection_refused"
	case errors.Is(err, syscall.EPIPE):
		return "broken_pipe"
	case errors.Is(err, syscall.ENETUNREACH), errors.Is(err, syscall.EHOSTUNREACH):
		return "network_unreachable"
	case errors.Is(err, io.ErrUnexpectedEOF):
		return "unexpected_eof"
	case errors.Is(err, io.EOF):
		return "eof"
	case errors.Is(err, transport_errProxy):
		return "proxy_handshake"
	case errors.As(err, &network) && network.Timeout():
		return "timeout"
	default:
		return "network_other"
	}
}

func transport_networkDiagnostics(err, requestContextErr, flowContextErr error) protocol_Map {
	fields := protocol_Map{}
	if err == nil {
		return fields
	}
	fields["network_error_class"] = transport_networkErrorClass(err)
	var op *net.OpError
	if errors.As(err, &op) {
		switch op.Op {
		case "dial", "read", "write":
			fields["network_error_op"] = op.Op
		}
	}
	switch {
	case errors.Is(flowContextErr, context.DeadlineExceeded):
		fields["timeout_source"] = "flow_deadline"
	case errors.Is(flowContextErr, context.Canceled):
		fields["timeout_source"] = "flow_canceled"
	case errors.Is(requestContextErr, context.DeadlineExceeded):
		fields["timeout_source"] = "request_deadline"
	case fields["network_error_class"] == "timeout" || fields["network_error_class"] == "dns_timeout":
		fields["timeout_source"] = "transport_timeout"
	}
	return fields
}
