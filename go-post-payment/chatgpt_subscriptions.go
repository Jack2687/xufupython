// Extracted from xupay/internal/cslive/chatgpt_subscriptions.go; see SOURCE.json.
package main

import (
	bytes "bytes"
	json "encoding/json"
	io "io"
	mime "mime"
	http "net/http"
	url "net/url"
	strings "strings"
)

const chatgpt_subscriptionsPath = "/backend-api/subscriptions"

const chatgpt_subscriptionNotFoundDetail = "No subscription found for account"

const chatgpt_subscriptionErrorBodyLimit = 4096

func (p *chatgpt_Platform) Subscriptions(renewal bool) (protocol_Map, error) {
	mode := "payment"
	if renewal {
		mode = "renewal"
	}
	return p.Call("GET", chatgpt_subscriptionsPath+"?account_id="+url.QueryEscape(p.CP.AccountID), chatgpt_PlatformCallOptions{Mode: mode})
}

// 该响应仅说明订阅查询尚无结果，不代表扣款失败，也不授权再次付款。
// 精确限制请求、状态和业务正文，避免把路由错误或通用404解释为尚未到账。
func chatgpt_isSubscriptionNotFound(method, path string, response *protocol_HTTPResponse) bool {
	if method != http.MethodGet || path != chatgpt_subscriptionsPath || response == nil || response.StatusCode != http.StatusNotFound || len(response.Content) > chatgpt_subscriptionErrorBodyLimit {
		return false
	}
	if contentType := response.Headers.Get("Content-Type"); contentType != "" {
		mediaType, _, err := mime.ParseMediaType(contentType)
		if err != nil || mediaType != "application/json" && !(strings.HasPrefix(mediaType, "application/") && strings.HasSuffix(mediaType, "+json")) {
			return false
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(response.Content))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return false
	}
	detail := ""
	seen := map[string]bool{}
	for decoder.More() {
		token, err := decoder.Token()
		key, ok := token.(string)
		if err != nil || !ok || seen[key] {
			return false
		}
		seen[key] = true
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return false
		}
		if key == "detail" && json.Unmarshal(value, &detail) != nil {
			return false
		}
	}
	end, err := decoder.Token()
	if err != nil || end != json.Delim('}') || decoder.Decode(new(any)) != io.EOF {
		return false
	}
	return detail == chatgpt_subscriptionNotFoundDetail
}
