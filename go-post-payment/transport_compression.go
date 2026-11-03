// Extracted from xupay/internal/cslive/transport_compression.go; see SOURCE.json.
package main

import (
	bytes "bytes"
	flate "compress/flate"
	gzip "compress/gzip"
	zlib "compress/zlib"
	errors "errors"
	brotli "github.com/andybalholm/brotli"
	zstd "github.com/klauspost/compress/zstd"
	io "io"
	strings "strings"
)

// fhttp v0.6.9 在 HTTP/1.1 readLoop 内识别 deflate 时会等待自己的 EOF 通知，
// 形成死锁。关闭它的自动解压后，在正文读取完毕的调用方解码，保留 curl 的原响应头。
func transport_decodeTransportBody(content []byte, encoding string) ([]byte, error) {
	kinds := strings.Split(encoding, ",")
	for i := len(kinds) - 1; i >= 0; i-- {
		var reader io.Reader
		var closeReader func() error
		raw := bytes.NewReader(content)
		switch strings.ToLower(strings.TrimSpace(kinds[i])) {
		case "", "identity":
			continue
		case "gzip", "x-gzip":
			r, err := gzip.NewReader(raw)
			if err != nil {
				return nil, err
			}
			reader = r
			closeReader = r.Close
		case "deflate":
			r, err := zlib.NewReader(raw)
			if err == nil {
				reader = r
				closeReader = r.Close
			} else {
				r = flate.NewReader(bytes.NewReader(content))
				reader = r
				closeReader = r.Close
			}
		case "br":
			reader = brotli.NewReader(raw)
		case "zstd":
			r, err := zstd.NewReader(raw, zstd.WithDecoderConcurrency(1))
			if err != nil {
				return nil, err
			}
			reader = r
			closeReader = func() error { r.Close(); return nil }
		default:
			return nil, errors.New("unsupported content encoding")
		}
		decoded, err := io.ReadAll(reader)
		if closeReader != nil {
			closeErr := closeReader()
			if err == nil {
				err = closeErr
			}
		}
		if err != nil {
			return nil, err
		}
		content = decoded
	}
	return content, nil
}
