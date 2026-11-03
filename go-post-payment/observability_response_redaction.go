// Extracted from xupay/internal/cslive/observability_response_redaction.go; see SOURCE.json.
package main

import (
	dep_html "html"
	mime "mime"
	url "net/url"
	regexp "regexp"
	strings "strings"
	unicode "unicode"
	utf8 "unicode/utf8"
)

const observability_diagnosticBodyLimit = 32 * 1024

const observability_diagnosticTextLimit = 240

var observability_diagnosticANSI = regexp.MustCompile("\x1b(?:\\[[0-?]*[ -/]*[@-~]|\\][^\x07]*(?:\x07|\x1b\\\\))")

var observability_diagnosticURL = regexp.MustCompile(`(?i)\b(?:https?|socks[45]h?|socks4a)://[^\s<>"']+`)

var observability_diagnosticEmail = regexp.MustCompile(`(?i)\b[A-Z0-9.!#$%&'*+/=?^_` + "`" + `{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b`)

var observability_diagnosticAuth = regexp.MustCompile(`(?i)\b(?:bearer|basic)\s+[^\s,;<>"']+`)

var observability_diagnosticJWT = regexp.MustCompile(`[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`)

var observability_diagnosticCredential = regexp.MustCompile(`(?i)\b(?:sk|pk|rk|sess|session|token|access_token|refresh_token|cs_live|ctoken|pi|seti|pm|cus)_[A-Za-z0-9_=-]+`)

var observability_diagnosticSensitiveLabel = regexp.MustCompile(`(?i)\b(?:authorization|set-cookie|cookie|session|token|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|api[_ -]?key|password|passwd|secret|proxy|card[_ -]?number|cvc|cvv|email|phone|billing|address|name|ip(?: address)?)\b\s*(?::|=|\bis\b)\s*.*`)

var observability_diagnosticQuoted = regexp.MustCompile(`"[^"]*"|'[^']*'|` + "`[^`]*`")

var observability_diagnosticOpaque = regexp.MustCompile(`[A-Za-z0-9_+/=-]{20,}`)

var observability_diagnosticIP = regexp.MustCompile(`\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b|\b(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.%]*`)

var observability_diagnosticNumbers = regexp.MustCompile(`\b(?:[0-9][ -]*){3,}\b`)

var observability_diagnosticCode = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`)

var observability_diagnosticConventionalCode = regexp.MustCompile(`^[a-z]+(?:[_.-][a-z]+)+$`)

var observability_diagnosticHex = regexp.MustCompile(`^[a-fA-F0-9]{16,64}$`)

var observability_diagnosticRay = regexp.MustCompile(`^[a-fA-F0-9]{16,32}-[A-Z]{3}$`)

var observability_diagnosticRequestID = regexp.MustCompile(`^(?:req_[A-Za-z0-9]{1,80}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})$`)

// SafeResponseText 是诊断文本唯一的输出边界；它不输出响应字段、属性或原始异常。
// 请求中已知的值优先遮盖，随后清除常见凭据、个人信息和终端控制序列。
func observability_SafeResponseText(value string, secrets []string) string {
	if len(value) > observability_diagnosticBodyLimit {
		return "[response text omitted: size limit]"
	}
	value = dep_html.UnescapeString(value)
	value = observability_diagnosticANSI.ReplaceAllString(value, " ")
	value = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.In(r, unicode.Cf) {
			if unicode.IsSpace(r) {
				return ' '
			}
			return -1
		}
		return r
	}, value)
	value = observability_redactKnownValues(value, secrets)
	value = strings.Join(strings.Fields(value), " ")
	for _, pattern := range []*regexp.Regexp{observability_diagnosticURL, observability_diagnosticEmail, observability_diagnosticAuth, observability_diagnosticJWT, observability_diagnosticCredential, observability_diagnosticSensitiveLabel, observability_diagnosticQuoted, observability_diagnosticIP, observability_diagnosticOpaque, observability_diagnosticNumbers} {
		value = pattern.ReplaceAllString(value, "[redacted]")
	}
	if utf8.RuneCountInString(value) > observability_diagnosticTextLimit {
		value = string([]rune(value)[:observability_diagnosticTextLimit]) + "…"
	}
	return value
}

// 所有已知值在原文上定位后一次性遮盖，避免后续短值再次改写占位符。
// 很短的 Cookie、开关和地区值必须作为独立词/值出现，不能破坏普通英文单词。
func observability_redactKnownValues(value string, secrets []string) string {
	mask := make([]bool, len(value))
	word := func(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' }
	for _, secret := range secrets {
		if secret == "" || len(secret) > observability_diagnosticBodyLimit {
			continue
		}
		for _, candidate := range []string{secret, url.QueryEscape(secret)} {
			for offset := 0; offset < len(value); {
				found := strings.Index(value[offset:], candidate)
				if found < 0 {
					break
				}
				start := offset + found
				end := start + len(candidate)
				offset = end
				if utf8.RuneCountInString(secret) < 8 {
					first, _ := utf8.DecodeRuneInString(candidate)
					last, _ := utf8.DecodeLastRuneInString(candidate)
					before, _ := utf8.DecodeLastRuneInString(value[:start])
					after, _ := utf8.DecodeRuneInString(value[end:])
					if start > 0 && word(first) && word(before) || end < len(value) && word(last) && word(after) {
						continue
					}
				}
				for index := start; index < end; index++ {
					mask[index] = true
				}
			}
		}
	}
	var result strings.Builder
	for i := 0; i < len(value); {
		if !mask[i] {
			result.WriteByte(value[i])
			i++
			continue
		}
		result.WriteString("[redacted]")
		for i < len(value) && mask[i] {
			i++
		}
	}
	return result.String()
}

func observability_safeResponseCode(value string, secrets []string) string {
	if !observability_diagnosticCode.MatchString(value) || observability_diagnosticCredential.MatchString(value) || observability_diagnosticJWT.MatchString(value) || observability_diagnosticHex.MatchString(value) {
		return "redacted"
	}
	if len(value) >= 20 && !(observability_diagnosticConventionalCode.MatchString(value) || strings.ToUpper(value) == value && observability_diagnosticConventionalCode.MatchString(strings.ToLower(value))) {
		return "redacted"
	}
	if observability_redactKnownValues(value, secrets) != value {
		return "redacted"
	}
	return value
}

func observability_safeContentType(value string) string {
	if len(value) > 256 {
		return ""
	}
	media, _, err := mime.ParseMediaType(value)
	if err != nil || !protocol_Matches(`[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+`, media) {
		return ""
	}
	return strings.ToLower(media)
}
