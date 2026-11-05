package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	tlsclient "github.com/bogdanfinn/tls-client"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func diagnosticTransport(t *testing.T, proxyURL string, timeout float64) (*transport_Transport, *[]protocol_Map) {
	t.Helper()
	tr, err := transport_NewTransport(context.Background(), proxyURL, timeout, time.Now().Add(10*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(tr.Close)
	events := []protocol_Map{}
	tr.BindObserver(&observability_Observer{sink: func(event string, fields protocol_Map) {
		if event == "http_request" {
			events = append(events, fields)
		}
	}}, "chatgpt")
	return tr, &events
}

func TestNetworkErrorClassesDoNotLeakWrappedSecrets(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{context.DeadlineExceeded, "timeout"}, {context.Canceled, "canceled"},
		{&net.DNSError{Name: "private-host", Err: "secret", IsTimeout: true}, "dns_timeout"},
		{&net.DNSError{Name: "private-host", Err: "secret"}, "dns_failure"},
		{x509.UnknownAuthorityError{}, "tls_certificate"},
		{syscall.ECONNRESET, "connection_reset"}, {syscall.ECONNREFUSED, "connection_refused"},
		{syscall.EPIPE, "broken_pipe"}, {syscall.ENETUNREACH, "network_unreachable"},
		{io.EOF, "eof"}, {io.ErrUnexpectedEOF, "unexpected_eof"}, {transport_errProxy, "proxy_handshake"},
		{errors.New("https://user:secret@private-host/?token=secret"), "network_other"},
	}
	for _, tc := range cases {
		err := &url.Error{Op: "POST", URL: "https://user:secret@private-host/?token=secret", Err: fmt.Errorf("secret: %w", tc.err)}
		fields := transport_networkDiagnostics(err, nil, nil)
		if fields["network_error_class"] != tc.want {
			t.Fatalf("got %v want %s", fields, tc.want)
		}
		data, _ := json.Marshal(fields)
		if strings.Contains(string(data), "secret") || strings.Contains(string(data), "private-host") {
			t.Fatal("sensitive error leaked")
		}
	}
	if transport_networkDiagnostics(context.DeadlineExceeded, context.DeadlineExceeded, nil)["timeout_source"] != "request_deadline" {
		t.Fatal("request deadline lost")
	}
	if transport_networkDiagnostics(context.DeadlineExceeded, context.DeadlineExceeded, context.DeadlineExceeded)["timeout_source"] != "flow_deadline" {
		t.Fatal("flow deadline lost")
	}
}

func TestProxyRejectionEvidencePreservesGetAndPostRetryRules(t *testing.T) {
	for _, method := range []string{"POST", "GET"} {
		t.Run(method, func(t *testing.T) {
			var calls atomic.Int32
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != "CONNECT" {
					t.Error("expected CONNECT")
				}
				w.WriteHeader(407)
			}))
			defer proxy.Close()
			tr, events := diagnosticTransport(t, strings.Replace(proxy.URL, "http://", "http://user:secret@", 1), 1)
			_, err := tr.Request(method, "https://example.invalid/subscriptions/cancel", protocol_RequestOptions{})
			if err == nil {
				t.Fatal("expected rejection")
			}
			want := 1
			if method == "GET" {
				want = 3
			}
			if int(calls.Load()) != want || len(*events) != want {
				t.Fatalf("retry policy changed: %d calls, %d events", calls.Load(), len(*events))
			}
			for _, e := range *events {
				if e["proxy_connect_status"] != 407 || e["proxy_tunnel_established"] != false || e["request_written"] != false || e["network_stage"] != "proxy_tunnel" || e["network_error_class"] != "proxy_handshake" {
					t.Fatalf("bad proxy evidence: %v", e)
				}
				if e["proxy_connect_ms"] == nil || e["proxy_tunnel_ms"] == nil {
					t.Fatal("missing proxy timings")
				}
				data, _ := json.Marshal(e)
				if strings.Contains(string(data), "secret") {
					t.Fatal("proxy credential leaked")
				}
			}
		})
	}
}

func TestResponseTimeoutDistinguishesHeadersAndBody(t *testing.T) {
	for _, body := range []bool{false, true} {
		t.Run(fmt.Sprint(body), func(t *testing.T) {
			release := make(chan struct{})
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if body {
					w.Header().Set("Content-Length", "100")
					w.WriteHeader(200)
					_, _ = w.Write([]byte("ok"))
					w.(http.Flusher).Flush()
				}
				select {
				case <-r.Context().Done():
				case <-release:
				}
			}))
			defer func() { close(release); server.Close() }()
			tr, events := diagnosticTransport(t, "", .2)
			roots := x509.NewCertPool()
			roots.AddCert(server.Certificate())
			tr.options = append(tr.options, tlsclient.WithTransportOptions(&tlsclient.TransportOptions{DisableCompression: true, RootCAs: roots}))
			_, err := tr.Request("POST", server.URL+"/cancel", protocol_RequestOptions{})
			if err == nil || len(*events) != 1 {
				t.Fatalf("expected one failed write, got %v", err)
			}
			e := (*events)[0]
			stage := "response_headers"
			if body {
				stage = "response_body"
			}
			if e["network_stage"] != stage || e["request_written"] != true || e["response_headers_received"] != body || e["response_started"] != body || e["network_error_class"] != "timeout" {
				t.Fatalf("bad response evidence: %v", e)
			}
			if body && (e["response_status"] != 200 || e["response_bytes_received"] != 2) {
				t.Fatalf("partial response lost: %v", e)
			}
			if e["request_timeout_ms"] != float64(200) || e["timeout_source"] == nil {
				t.Fatal("timeout budget missing")
			}
		})
	}
}

func TestFirstResponseByteIsNotCompleteHeaders(t *testing.T) {
	timing := &transport_requestTiming{started: time.Now(), written: time.Now()}
	timing.trace().GotFirstResponseByte()
	fields := timing.fields(time.Now())
	if fields["network_stage"] != "response_headers" || fields["response_started"] != true || fields["response_headers_received"] != false {
		t.Fatalf("partial headers mislabeled: %v", fields)
	}
}
