// Extracted from xupay/internal/cslive/observability_http_diagnostics.go; see SOURCE.json.
package main

import (
	json "encoding/json"
	xml "encoding/xml"
	html "golang.org/x/net/html"
	strings "strings"
)

// HTTPFailureDiagnostics 仅允许少量错误字段和关联标识进入 EventSink；不返回原始正文。
func observability_HTTPFailureDiagnostics(response *protocol_HTTPResponse, secrets []string) protocol_Map {
	result := protocol_Map{}
	if response == nil || response.StatusCode < 300 {
		return result
	}
	secrets = append([]string(nil), secrets...)
	for _, value := range response.Cookies {
		if value != "" {
			secrets = append(secrets, value)
		}
	}
	media := observability_safeContentType(response.Headers.Get("Content-Type"))
	if media != "" {
		result["response_content_type"] = media
	}
	if value := response.Headers.Get("CF-Ray"); observability_diagnosticRay.MatchString(value) {
		result["cf_ray"] = value
	}
	for _, header := range []string{"Request-Id", "X-Request-Id"} {
		if value := response.Headers.Get(header); observability_diagnosticRequestID.MatchString(value) {
			result["response_request_id"] = value
			break
		}
	}
	content := response.Content
	truncated := len(content) > observability_diagnosticBodyLimit
	if truncated {
		content = content[:observability_diagnosticBodyLimit]
		result["response_truncated"] = true
	}
	put := func(key string, value any) {
		text, ok := value.(string)
		if !ok || text == "" {
			return
		}
		if key == "response_error_code" || key == "response_error_type" {
			result[key] = observability_safeResponseCode(text, secrets)
		} else {
			result[key] = observability_SafeResponseText(text, secrets)
		}
	}
	var decoded map[string]any
	if !truncated && json.Unmarshal(content, &decoded) == nil && decoded != nil {
		node := decoded
		if nested, ok := decoded["error"].(map[string]any); ok {
			node = nested
		} else if nested, ok := decoded["detail"].(map[string]any); ok {
			node = nested
		}
		put("response_error_code", node["code"])
		put("response_error_type", node["type"])
		if param, ok := node["param"].(string); ok && param != "" {
			result["provider_param"] = protocol_SafeProviderParam(param)
		}
		put("response_message", node["message"])
		put("response_detail", node["detail"])
		if value, ok := decoded["error"].(string); ok {
			put("response_message", value)
		}
		if items, ok := decoded["detail"].([]any); ok && len(items) > 0 {
			if first, ok := items[0].(map[string]any); ok {
				put("response_message", first["msg"])
				put("response_error_type", first["type"])
				if loc, ok := first["loc"].([]any); ok {
					parts := make([]string, 0, len(loc))
					for _, item := range loc {
						if text, ok := item.(string); ok && text != "" {
							parts = append(parts, text)
						}
					}
					put("response_detail", strings.Join(parts, "."))
				}
			}
		}
		return result
	}
	if media == "text/html" || strings.HasPrefix(strings.ToLower(strings.TrimSpace(string(content))), "<!doctype html") || strings.HasPrefix(strings.TrimSpace(string(content)), "<html") {
		title, summary := observability_htmlErrorText(string(content))
		put("response_title", title)
		put("response_summary", summary)
		if truncated && result["response_summary"] == nil {
			result["response_summary"] = "Response body omitted: size limit"
		}
		return result
	}
	if truncated {
		result["response_summary"] = "Response body omitted: size limit"
		return result
	}
	if media == "application/xml" || media == "text/xml" {
		var failure struct {
			XMLName xml.Name `xml:"Error"`
			Code    string   `xml:"Code"`
			Message string   `xml:"Message"`
		}
		if xml.Unmarshal(content, &failure) == nil {
			put("response_error_code", failure.Code)
			put("response_message", failure.Message)
		}
		return result
	}
	if media == "text/plain" {
		put("response_summary", string(content))
	}
	return result
}

var observability_htmlDiagnosticBlockedTags = protocol_StringSet("script", "style", "form", "textarea", "select", "template", "noscript", "svg", "math", "pre", "code")

func observability_htmlErrorText(source string) (string, string) {
	tokens := html.NewTokenizer(strings.NewReader(source))
	title, summary := []string{}, []string{}
	blocked, inTitle := "", false
	blockedDepth := 0
	for {
		kind := tokens.Next()
		if kind == html.ErrorToken {
			break
		}
		token := tokens.Token()
		if kind == html.StartTagToken {
			if blocked != "" {
				if token.Data == blocked {
					blockedDepth++
				}
			} else if observability_htmlDiagnosticBlockedTags[token.Data] {
				blocked, blockedDepth = token.Data, 1
			}
			if token.Data == "title" {
				inTitle = true
			}
		}
		if kind == html.EndTagToken {
			if token.Data == blocked {
				blockedDepth--
				if blockedDepth == 0 {
					blocked = ""
				}
			}
			if token.Data == "title" {
				inTitle = false
			}
		}
		if kind != html.TextToken || blocked != "" {
			continue
		}
		if inTitle {
			title = append(title, token.Data)
		} else {
			summary = append(summary, token.Data)
		}
	}
	return strings.Join(title, " "), strings.Join(summary, " ")
}
