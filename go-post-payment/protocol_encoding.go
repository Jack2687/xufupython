// Extracted from xupay/internal/cslive/protocol_encoding.go; see SOURCE.json.
package main

import (
	bytes "bytes"
	rand "crypto/rand"
	json "encoding/json"
	fmt "fmt"
	uuid "github.com/google/uuid"
	io "io"
	big "math/big"
	reflect "reflect"
	sort "sort"
	strconv "strconv"
	strings "strings"
	utf16 "unicode/utf16"
)

// CompactJSON 对应 Python ensure_ascii=True，供参与后续编码的协议字段使用。
func protocol_CompactJSON(v any) (string, error) {
	encoded, e := protocol_marshalProtocolJSON(v, 0)
	if e != nil {
		return "", e
	}
	var b strings.Builder
	for _, r := range string(encoded) {
		if r < 128 {
			b.WriteRune(r)
		} else if r <= 0xffff {
			fmt.Fprintf(&b, "\\u%04x", r)
		} else {
			a, c := utf16.EncodeRune(r)
			fmt.Fprintf(&b, "\\u%04x\\u%04x", a, c)
		}
	}
	return b.String(), nil
}

func protocol_DecodeMap(raw []byte) (protocol_Map, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var m protocol_Map
	if err := d.Decode(&m); err != nil || m == nil {
		return nil, protocol_NewError("invalid_json_response")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return nil, protocol_NewError("invalid_json_response")
	}
	return m, nil
}

func protocol_JSONResponse(r *protocol_HTTPResponse, allowError bool) (protocol_Map, error) {
	if r == nil {
		return nil, protocol_NewError("invalid_json_response")
	}
	m, e := protocol_DecodeMap(r.Content)
	if e != nil {
		return nil, e
	}
	if r.StatusCode >= 400 && !allowError {
		return nil, protocol_NewError(fmt.Sprintf("http_%d", r.StatusCode))
	}
	if r.StatusCode >= 300 && r.StatusCode < 400 {
		return nil, protocol_NewError("unexpected_redirect")
	}
	return m, nil
}

func protocol_AsMap(v any) protocol_Map {
	switch x := v.(type) {
	case protocol_Map:
		return x
	case protocol_Fields:
		m := protocol_Map{}
		for _, p := range x {
			m[p.Key] = p.Value
		}
		return m
	}
	return protocol_Map{}
}

func protocol_AsString(v any) string {
	if v == nil {
		return ""
	}
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number:
		s, _ := protocol_protocolNumber(x)
		return s
	case float64:
		return protocol_protocolFloat(x)
	case float32:
		return protocol_protocolFloat(float64(x))
	}
	return fmt.Sprint(v)
}

func protocol_Truthy(v any) bool {
	if v == nil {
		return false
	}
	switch x := v.(type) {
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, e := strconv.ParseFloat(string(x), 64)
		return e == nil && f != 0
	}
	r := reflect.ValueOf(v)
	switch r.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return r.Int() != 0
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return r.Uint() != 0
	case reflect.Float32, reflect.Float64:
		return r.Float() != 0
	case reflect.Slice, reflect.Map, reflect.Array:
		return r.Len() != 0
	case reflect.Pointer, reflect.Interface:
		return !r.IsNil()
	}
	return true
}

func protocol_FirstTruthy(v ...any) any {
	for _, x := range v {
		if protocol_Truthy(x) {
			return x
		}
	}
	return nil
}

func protocol_Integer(v any) (int64, bool) {
	switch x := v.(type) {
	case json.Number:
		n, e := x.Int64()
		return n, e == nil
	case int:
		return int64(x), true
	case int64:
		return x, true
	case int32:
		return int64(x), true
	case int16:
		return int64(x), true
	case int8:
		return int64(x), true
	case uint:
		if uint64(x) <= 1<<63-1 {
			return int64(x), true
		}
	case uint64:
		if x <= 1<<63-1 {
			return int64(x), true
		}
	case uint32:
		return int64(x), true
	case uint16:
		return int64(x), true
	case uint8:
		return int64(x), true
	}
	return 0, false
}

func protocol_SortedKeys(m protocol_Map) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func protocol_RandomUUID() string { return uuid.NewString() }

func protocol_RandomAlphabet(n int, alphabet string) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		x, e := rand.Int(rand.Reader, big.NewInt(int64(len(alphabet))))
		if e != nil {
			panic("random source unavailable")
		}
		b.WriteByte(alphabet[x.Int64()])
	}
	return b.String()
}
