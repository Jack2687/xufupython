package main

import (
	"context"
	fhttptrace "github.com/bogdanfinn/fhttp/httptrace"
	utls "github.com/bogdanfinn/utls"
	stdtrace "net/http/httptrace"
	"sync"
	"time"
)

// Only fixed labels, booleans and timings: never retain addresses, headers or errors.
// Custom TLS dialers may omit TLS hooks. Missing measurements stay absent.
type transport_requestTiming struct {
	mu                                              sync.Mutex
	started, connected, written, firstByte, headers time.Time
	stage                                           string
	stageStarted                                    time.Time
	metrics                                         protocol_Map
}
type transport_timingKey struct{}

func transport_timing(ctx context.Context) *transport_requestTiming {
	t, _ := ctx.Value(transport_timingKey{}).(*transport_requestTiming)
	return t
}

func (t *transport_requestTiming) set(key string, value any) {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.metrics == nil {
		t.metrics = protocol_Map{}
	}
	t.metrics[key] = value
}

func (t *transport_requestTiming) begin(stage string) time.Time {
	now := time.Now()
	if t == nil {
		return now
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.stage, t.stageStarted = stage, now
	return now
}

func (t *transport_requestTiming) end(key string, start time.Time, err error) {
	if t == nil || start.IsZero() {
		return
	}
	t.set(key, float64(time.Since(start))/float64(time.Millisecond))
	if err == nil {
		t.begin("connection")
	}
}

func (t *transport_requestTiming) context(ctx context.Context) context.Context {
	ctx = context.WithValue(ctx, transport_timingKey{}, t)
	// net.Dialer uses the standard trace key, not fhttp's private nettrace key.
	var dns, connect time.Time
	var mu sync.Mutex
	ctx = stdtrace.WithClientTrace(ctx, &stdtrace.ClientTrace{
		DNSStart:     func(stdtrace.DNSStartInfo) { mu.Lock(); defer mu.Unlock(); dns = t.begin("dns") },
		DNSDone:      func(info stdtrace.DNSDoneInfo) { mu.Lock(); defer mu.Unlock(); t.end("dns_ms", dns, info.Err) },
		ConnectStart: func(string, string) { mu.Lock(); defer mu.Unlock(); connect = t.begin("tcp_connect") },
		ConnectDone:  func(_, _ string, err error) { mu.Lock(); defer mu.Unlock(); t.end("connect_ms", connect, err) },
	})
	return fhttptrace.WithClientTrace(ctx, t.trace())
}

func (t *transport_requestTiming) trace() *fhttptrace.ClientTrace {
	var tlsStarted time.Time
	return &fhttptrace.ClientTrace{
		TLSHandshakeStart: func() {
			t.mu.Lock()
			defer t.mu.Unlock()
			tlsStarted = time.Now()
			t.stage, t.stageStarted = "tls_handshake", tlsStarted
		},
		TLSHandshakeDone: func(_ utls.ConnectionState, err error) {
			t.mu.Lock()
			start := tlsStarted
			t.mu.Unlock()
			t.end("tls_ms", start, err)
		},
		GotConn: func(info fhttptrace.GotConnInfo) {
			t.set("connection_reused", info.Reused)
			if info.WasIdle {
				t.set("connection_idle_ms", float64(info.IdleTime)/float64(time.Millisecond))
			}
			t.mu.Lock()
			defer t.mu.Unlock()
			t.connected = time.Now()
		},
		WroteRequest: func(info fhttptrace.WroteRequestInfo) {
			if info.Err == nil {
				t.mu.Lock()
				defer t.mu.Unlock()
				t.written = time.Now()
			}
		},
		GotFirstResponseByte: func() {
			t.mu.Lock()
			defer t.mu.Unlock()
			if t.firstByte.IsZero() {
				t.firstByte = time.Now()
			}
		},
	}
}

func (t *transport_requestTiming) receivedHeaders(status int) {
	if t == nil {
		return
	}
	t.set("response_status", status)
	t.mu.Lock()
	defer t.mu.Unlock()
	t.headers = time.Now()
}

func (t *transport_requestTiming) fields(finished time.Time) protocol_Map {
	t.mu.Lock()
	defer t.mu.Unlock()
	fields := protocol_Map{}
	for k, v := range t.metrics {
		fields[k] = v
	}
	add := func(key string, a, b time.Time) {
		if !a.IsZero() && !b.IsZero() && !b.Before(a) {
			fields[key] = float64(b.Sub(a)) / float64(time.Millisecond)
		}
	}
	add("connection_ready_ms", t.started, t.connected)
	add("request_write_ms", t.connected, t.written)
	add("first_byte_ms", t.started, t.firstByte)
	add("response_wait_ms", t.written, t.firstByte)
	fields["request_written"] = !t.written.IsZero()
	fields["response_started"] = !t.firstByte.IsZero() || !t.headers.IsZero()
	fields["response_headers_received"] = !t.headers.IsZero()
	stage, since := t.stage, t.stageStarted
	if stage == "" {
		stage, since = "connection", t.started
	}
	if !t.connected.IsZero() {
		stage, since = "request_write", t.connected
	}
	if !t.written.IsZero() {
		stage, since = "response_headers", t.written
	}
	// A first byte does not mean all response headers arrived.
	if !t.headers.IsZero() {
		stage, since = "response_body", t.headers
	}
	fields["network_stage"] = stage
	add("network_stage_elapsed_ms", since, finished)
	return fields
}
