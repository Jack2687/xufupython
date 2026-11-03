// Extracted from xupay/internal/cslive/transport_proxy.go; see SOURCE.json.
package main

import (
	bufio "bufio"
	context "context"
	tls "crypto/tls"
	base64 "encoding/base64"
	binary "encoding/binary"
	errors "errors"
	fhttp "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	proxy "golang.org/x/net/proxy"
	io "io"
	net "net"
	http "net/http"
	url "net/url"
	strconv "strconv"
	strings "strings"
	time "time"
)

var transport_errProxy = errors.New("proxy connection failed")

type transport_transportProxyDialer struct {
	url     *url.URL
	dialer  net.Dialer
	timeout time.Duration
	// 仅本地证书测试设置；生产使用系统根证书。
	tlsConfig *tls.Config
}

func transport_parseTransportProxy(raw string) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return nil, transport_errProxy
	}
	port := ""
	switch u.Scheme {
	case "http":
		port = "80"
	case "https":
		port = "443"
	case "socks4", "socks4a", "socks5", "socks5h":
		port = "1080"
	default:
		return nil, transport_errProxy
	}
	if u.Port() == "" {
		u.Host = net.JoinHostPort(u.Hostname(), port)
	} else if n, err := strconv.Atoi(u.Port()); err != nil || n < 1 || n > 65535 {
		return nil, transport_errProxy
	}
	return u, nil
}

func transport_transportProxyFactory(raw string, timeout time.Duration, local *net.TCPAddr, _ fhttp.Header, _ tls_client.Logger) (proxy.ContextDialer, error) {
	u, err := transport_parseTransportProxy(raw)
	if err != nil {
		return nil, err
	}
	return &transport_transportProxyDialer{url: u, timeout: timeout, dialer: net.Dialer{Timeout: timeout, LocalAddr: local}}, nil
}

func (d *transport_transportProxyDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	if network != "tcp" && network != "tcp4" && network != "tcp6" {
		return nil, transport_errProxy
	}
	budget := d.timeout
	if budget <= 0 {
		budget = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	c, err := d.dialer.DialContext(ctx, "tcp", d.url.Host)
	if err != nil {
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			_ = c.Close()
		}
	}()

	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		if err := c.SetDeadline(deadline); err != nil {
			return nil, err
		}
	}
	var tunnel net.Conn = c
	switch d.url.Scheme {
	case "http", "https":
		if d.url.Scheme == "https" {
			conf := &tls.Config{MinVersion: tls.VersionTLS12, ServerName: d.url.Hostname(), NextProtos: []string{"http/1.1"}}
			if d.tlsConfig != nil {
				conf = d.tlsConfig.Clone()
				conf.ServerName = d.url.Hostname()
				conf.NextProtos = []string{"http/1.1"}
			}
			t := tls.Client(c, conf)
			if err := t.HandshakeContext(ctx); err != nil {
				return nil, err
			}
			tunnel = t
		}
		tunnel, err = d.connect(tunnel, address)
	case "socks4", "socks4a":
		err = d.socks4(ctx, c, address)
	case "socks5", "socks5h":
		err = d.socks5(ctx, c, address)
	default:
		err = transport_errProxy
	}
	if err != nil {
		return nil, err
	}
	if !stop() || ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err := c.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	success = true
	return tunnel, nil
}

func (d *transport_transportProxyDialer) connect(c net.Conn, address string) (net.Conn, error) {
	r := &http.Request{Method: "CONNECT", URL: &url.URL{Host: address}, Host: address, Header: make(http.Header)}
	if d.url.User != nil {
		password, _ := d.url.User.Password()
		token := base64.StdEncoding.EncodeToString([]byte(d.url.User.Username() + ":" + password))
		r.Header.Set("Proxy-Authorization", "Basic "+token)
	}
	if err := r.Write(c); err != nil {
		return nil, err
	}
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, r)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, transport_errProxy
	}

	return &transport_proxyBufferedConn{Conn: c, reader: br}, nil
}

type transport_proxyBufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *transport_proxyBufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

func transport_proxyTarget(ctx context.Context, address string, local, ipv4 bool) (string, int, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return "", 0, transport_errProxy
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return "", 0, transport_errProxy
	}
	if local && net.ParseIP(host) == nil {
		network := "ip"
		if ipv4 {
			network = "ip4"
		}
		ips, err := net.DefaultResolver.LookupIP(ctx, network, host)
		if err != nil {
			return "", 0, err
		}
		if len(ips) == 0 {
			return "", 0, transport_errProxy
		}
		host = ips[0].String()
	}
	return host, n, nil
}

func (d *transport_transportProxyDialer) socks4(ctx context.Context, c net.Conn, address string) error {
	host, port, err := transport_proxyTarget(ctx, address, d.url.Scheme == "socks4", true)
	if err != nil {
		return err
	}
	user := ""
	if d.url.User != nil {
		user = d.url.User.Username()
	}
	if strings.ContainsRune(user, 0) || strings.ContainsRune(host, 0) {
		return transport_errProxy
	}
	packet := []byte{4, 1, byte(port >> 8), byte(port), 0, 0, 0, 1}
	ip := net.ParseIP(host)
	remote := ip == nil
	if !remote {
		v4 := ip.To4()
		if v4 == nil {
			return transport_errProxy
		}
		copy(packet[4:8], v4)
	} else if d.url.Scheme != "socks4a" {
		return transport_errProxy
	}
	packet = append(packet, []byte(user)...)
	packet = append(packet, 0)
	if remote {
		packet = append(packet, []byte(host)...)
		packet = append(packet, 0)
	}
	if err := transport_writeProxy(c, packet); err != nil {
		return err
	}
	r := make([]byte, 8)
	if _, err := io.ReadFull(c, r); err != nil {
		return err
	}
	if r[0] != 0 || r[1] != 90 {
		return transport_errProxy
	}
	return nil
}

func (d *transport_transportProxyDialer) socks5(ctx context.Context, c net.Conn, address string) error {
	host, port, err := transport_proxyTarget(ctx, address, d.url.Scheme == "socks5", false)
	if err != nil {
		return err
	}
	user, password := "", ""
	if d.url.User != nil {
		user = d.url.User.Username()
		password, _ = d.url.User.Password()
	}
	methods := []byte{5, 1, 0}
	if d.url.User != nil {
		methods = []byte{5, 2, 0, 2}
	}
	if len(user) > 255 || len(password) > 255 || len(host) > 255 {
		return transport_errProxy
	}
	if err := transport_writeProxy(c, methods); err != nil {
		return err
	}
	answer := make([]byte, 2)
	if _, err := io.ReadFull(c, answer); err != nil {
		return err
	}
	if answer[0] != 5 {
		return transport_errProxy
	}
	switch answer[1] {
	case 0:
	case 2:
		if d.url.User == nil {
			return transport_errProxy
		}
		packet := append([]byte{1, byte(len(user))}, []byte(user)...)
		packet = append(packet, byte(len(password)))
		packet = append(packet, []byte(password)...)
		if err := transport_writeProxy(c, packet); err != nil {
			return err
		}
		if _, err := io.ReadFull(c, answer); err != nil {
			return err
		}
		if answer[0] != 1 || answer[1] != 0 {
			return transport_errProxy
		}
	default:
		return transport_errProxy
	}
	packet := []byte{5, 1, 0}
	ip := net.ParseIP(host)
	if ip == nil {
		packet = append(packet, 3, byte(len(host)))
		packet = append(packet, []byte(host)...)
	} else if v4 := ip.To4(); v4 != nil {
		packet = append(packet, 1)
		packet = append(packet, v4...)
	} else {
		packet = append(packet, 4)
		packet = append(packet, ip.To16()...)
	}
	packet = binary.BigEndian.AppendUint16(packet, uint16(port))
	if err := transport_writeProxy(c, packet); err != nil {
		return err
	}
	head := make([]byte, 4)
	if _, err := io.ReadFull(c, head); err != nil {
		return err
	}
	if head[0] != 5 || head[1] != 0 || head[2] != 0 {
		return transport_errProxy
	}
	n := 0
	switch head[3] {
	case 1:
		n = 4
	case 4:
		n = 16
	case 3:
		var b [1]byte
		if _, err := io.ReadFull(c, b[:]); err != nil {
			return err
		}
		n = int(b[0])
	default:
		return transport_errProxy
	}
	_, err = io.CopyN(io.Discard, c, int64(n+2))
	return err
}

func transport_writeProxy(c net.Conn, p []byte) error {
	for len(p) > 0 {
		n, err := c.Write(p)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrUnexpectedEOF
		}
		p = p[n:]
	}
	return nil
}

func transport_transportProxyOption(raw string) tls_client.HttpClientOption {
	return tls_client.WithProxyDialerFactory(func(_ string, timeout time.Duration, local *net.TCPAddr, h fhttp.Header, l tls_client.Logger) (proxy.ContextDialer, error) {
		return transport_transportProxyFactory(raw, timeout, local, h, l)
	})
}
