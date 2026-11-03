// Extracted from xupay/internal/cslive/transport_timing.go; see SOURCE.json.
package main

import (
	fhttptrace "github.com/bogdanfinn/fhttp/httptrace"
	sync "sync"
	time "time"
)

// 仅采集阶段时间，不保留目标地址、请求头或原始错误。
// 自定义 TLS 拨号器不保证触发握手钩子，因此连接准备包含代理建链和 TLS，
// 不将缺失的底层测量伪装为零耗时。
type transport_requestTiming struct {
	mu                            sync.Mutex
	started                       time.Time
	connected, written, firstByte time.Time
}

func (t *transport_requestTiming) trace() *fhttptrace.ClientTrace {
	return &fhttptrace.ClientTrace{
		GotConn: func(fhttptrace.GotConnInfo) {
			t.mu.Lock()
			defer t.mu.Unlock()
			if t.connected.IsZero() {
				t.connected = time.Now()
			}
		},
		WroteRequest: func(info fhttptrace.WroteRequestInfo) {
			if info.Err == nil {
				t.mu.Lock()
				defer t.mu.Unlock()
				if t.written.IsZero() {
					t.written = time.Now()
				}
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

func (t *transport_requestTiming) fields(finished time.Time) protocol_Map {
	t.mu.Lock()
	defer t.mu.Unlock()
	fields := protocol_Map{}
	add := func(key string, a, b time.Time) {
		if !a.IsZero() && !b.IsZero() && !b.Before(a) {
			fields[key] = float64(b.Sub(a)) / float64(time.Millisecond)
		}
	}
	add("connection_ready_ms", t.started, t.connected)
	add("request_write_ms", t.connected, t.written)
	add("first_byte_ms", t.started, t.firstByte)
	add("response_wait_ms", t.written, t.firstByte)

	stage, since := "connection", t.started
	if !t.connected.IsZero() {
		stage, since = "request_write", t.connected
	}
	if !t.written.IsZero() {
		stage, since = "response_headers", t.written
	}
	if !t.firstByte.IsZero() {
		stage, since = "response_body", t.firstByte
	}
	fields["network_stage"] = stage
	add("network_stage_elapsed_ms", since, finished)
	return fields
}
