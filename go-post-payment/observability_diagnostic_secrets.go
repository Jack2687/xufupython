// Extracted from xupay/internal/cslive/observability_diagnostic_secrets.go; see SOURCE.json.
package main

import (
	base64 "encoding/base64"
	json "encoding/json"
	url "net/url"
	reflect "reflect"
	strings "strings"
)

// RequestDiagnosticSecrets 仅在失败观测时提取已知请求值，用于遮盖服务端回显。
// 有界遍历不调用用户提供的 Stringer，不保留正文，也不执行 SDK 对象中的方法。
func observability_RequestDiagnosticSecrets(rawURL string, options protocol_RequestOptions) []string {
	values := []string{}
	seen := map[string]bool{}
	add := func(value string) {
		if len(value) >= 1 && len(value) <= observability_diagnosticBodyLimit && len(values) < 512 && !seen[value] {
			values, seen[value] = append(values, value), true
		}
	}
	if u, err := url.Parse(rawURL); err == nil {
		for _, value := range observability_providerID.FindAllString(u.Path, -1) {
			add(value)
		}
		if secret, found := strings.CutPrefix(u.Path, "/p/session/"); found {
			add(secret)
		}
		for _, entries := range u.Query() {
			for _, value := range entries {
				add(value)
			}
		}
		if u.User != nil {
			add(u.User.Username())
			password, _ := u.User.Password()
			add(password)
		}
	}
	for _, field := range options.Headers {
		value, ok := field.Value.(string)
		if !ok {
			continue
		}
		add(value)
		if strings.EqualFold(field.Key, "Authorization") || strings.EqualFold(field.Key, "Proxy-Authorization") {
			kind, token, _ := strings.Cut(value, " ")
			add(strings.TrimSpace(token))
			if strings.EqualFold(kind, "Basic") {
				if decoded, err := base64.StdEncoding.DecodeString(token); err == nil {
					user, password, _ := strings.Cut(string(decoded), ":")
					add(user)
					add(password)
				}
			}
		}
		if strings.EqualFold(field.Key, "Cookie") {
			for _, part := range strings.Split(value, ";") {
				_, cookie, _ := strings.Cut(part, "=")
				add(strings.TrimSpace(cookie))
			}
		}
	}
	remaining := 2048
	var walk func(reflect.Value, int)
	walk = func(value reflect.Value, depth int) {
		remaining--
		if remaining < 0 || depth > 16 || !value.IsValid() {
			return
		}
		if value.Type() == reflect.TypeOf(protocol_Field{}) {
			walk(value.FieldByName("Value"), depth+1)
			return
		}
		switch value.Kind() {
		case reflect.Interface, reflect.Pointer:
			if !value.IsNil() {
				walk(value.Elem(), depth+1)
			}
		case reflect.String:
			add(value.String())
		case reflect.Map:
			iterator := value.MapRange()
			for iterator.Next() && remaining > 0 {
				walk(iterator.Value(), depth+1)
			}
		case reflect.Slice, reflect.Array:
			for i := 0; i < value.Len() && remaining > 0; i++ {
				walk(value.Index(i), depth+1)
			}
		case reflect.Struct:
			for i := 0; i < value.NumField() && remaining > 0; i++ {
				if value.Type().Field(i).IsExported() {
					walk(value.Field(i), depth+1)
				}
			}
		}
	}
	walk(reflect.ValueOf(options.Form), 0)
	walk(reflect.ValueOf(options.Body), 0)
	if options.Text != nil {
		add(*options.Text)
		if len(*options.Text) <= observability_diagnosticBodyLimit {
			var decoded any
			if json.Unmarshal([]byte(*options.Text), &decoded) == nil {
				walk(reflect.ValueOf(decoded), 0)
			}
		}
	}
	return values
}
