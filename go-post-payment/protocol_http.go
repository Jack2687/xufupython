// Extracted from xupay/internal/cslive/protocol_http.go; see SOURCE.json.
package main

import (
	http "net/http"
	time "time"
)

type protocol_Map = map[string]any

// RequestOptions 保留调用方的字段顺序，并区分 JSON、表单和原始正文。
type protocol_RequestOptions struct {
	Headers protocol_Fields
	Form    any
	Body    any
	Text    *string
}

type protocol_HTTPResponse struct {
	StatusCode int
	Headers    http.Header
	Content    []byte
	Cookies    map[string]string
}

// HTTPTransport 只由单笔交易串行使用；交易之间不共享凭据或连接。
type protocol_HTTPTransport interface {
	Request(method, url string, options protocol_RequestOptions) (*protocol_HTTPResponse, error)
	Pause(seconds float64) error
	Close()
	Deadline() time.Time
}
