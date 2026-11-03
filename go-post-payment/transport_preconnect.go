// Extracted from xupay/internal/cslive/transport_preconnect.go; see SOURCE.json.
package main

import (
	context "context"
	fhttp "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	proxy "golang.org/x/net/proxy"
	net "net"
	url "net/url"
	sync "sync"
	time "time"
)

// preparedConnection 只预建隧道，不发送 HTTP；连接仅交给指定请求一次。
type transport_preparedConnection struct {
	ready   chan struct{}
	cancel  context.CancelFunc
	conn    net.Conn
	address string
	started time.Time
	base    proxy.ContextDialer
	once    sync.Once
}

// Preconnect 为本笔交易的下一条指定 POST 提前完成 TCP/代理握手。
// 不支持并发调用 Transport 方法；后台只使用独立拨号器，不读写交易会话。
func (t *transport_Transport) Preconnect(raw string) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || t.prepared != nil {
		return
	}
	port := u.Port()
	if port == "" {
		port = "443"
	}
	ctx, cancel := context.WithTimeout(t.ctx, t.timeout)
	p := &transport_preparedConnection{ready: make(chan struct{}), cancel: cancel, address: net.JoinHostPort(u.Hostname(), port), started: time.Now(), base: t.client.GetDialer()}
	t.prepared, t.preparedURL = p, raw
	go func() {
		defer close(p.ready)
		p.conn, _ = p.base.DialContext(ctx, "tcp", p.address)
	}()
}

func (p *transport_preparedConnection) Close() {
	p.cancel()
	<-p.ready
	p.once.Do(func() {
		if p.conn != nil {
			_ = p.conn.Close()
		}
	})
}

func (p *transport_preparedConnection) Dial(network, address string) (net.Conn, error) {
	return p.DialContext(context.Background(), network, address)
}

func (p *transport_preparedConnection) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	if address != p.address {
		return p.base.DialContext(ctx, network, address)
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-p.ready:
	}
	var conn net.Conn
	p.once.Do(func() {
		if time.Since(p.started) <= 15*time.Second && ctx.Err() == nil {
			conn = p.conn
		} else if p.conn != nil {
			_ = p.conn.Close()
		}
	})
	if conn != nil {
		return conn, nil
	}

	return p.base.DialContext(ctx, network, address)
}

func (t *transport_Transport) preparedClient(raw string) (tls_client.HttpClient, error) {
	if t.prepared == nil || raw != t.preparedURL {
		return t.newClient()
	}
	p := t.prepared
	options := append([]tls_client.HttpClientOption{}, t.options...)
	options = append(options, tls_client.WithProxyDialerFactory(func(string, time.Duration, *net.TCPAddr, fhttp.Header, tls_client.Logger) (proxy.ContextDialer, error) {
		return p, nil
	}))
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), options...)
}
