// Extracted from xupay/internal/cslive/protocol_json_encoding.go; see SOURCE.json.
package main

import (
	bytes "bytes"
	json "encoding/json"
	fmt "fmt"
	math "math"
	reflect "reflect"
	sort "sort"
	strconv "strconv"
	strings "strings"
)

// Python 的 float 保留 .0，科学计数的阈值与 Go JSON 不同。
// 这些字符参与 Stripe opaque 编码，不能仅比较解码后的数值。
func protocol_protocolFloat(value float64) string {
	if math.IsNaN(value) {
		return "nan"
	}
	if math.IsInf(value, 1) {
		return "inf"
	}
	if math.IsInf(value, -1) {
		return "-inf"
	}
	magnitude := math.Abs(value)
	if magnitude != 0 && (magnitude < 1e-4 || magnitude >= 1e16) {
		return strconv.FormatFloat(value, 'e', -1, 64)
	}
	s := strconv.FormatFloat(value, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// json.Number 保留输入类型以便金额校验拒绝浮点；重新编码时再采用 Python 数值格式。
func protocol_protocolNumber(value json.Number) (string, error) {
	if value == "" {
		return "", fmt.Errorf("invalid protocol number")
	}
	if _, err := json.Marshal(value); err != nil {
		return "", fmt.Errorf("invalid protocol number")
	}
	text := string(value)
	if strings.ContainsAny(text, ".eE") {
		v, err := value.Float64()
		if err != nil || math.IsInf(v, 0) || math.IsNaN(v) {
			return "", fmt.Errorf("invalid protocol number")
		}
		return protocol_protocolFloat(v), nil
	}
	if text == "-0" {
		return "0", nil
	}
	return text, nil
}

func protocol_marshalProtocolJSON(value any, depth int) ([]byte, error) {
	if depth > 256 {
		return nil, fmt.Errorf("protocol JSON exceeds depth limit")
	}
	switch v := value.(type) {
	case json.Number:
		text, err := protocol_protocolNumber(v)
		return []byte(text), err
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, fmt.Errorf("invalid protocol number")
		}
		return []byte(protocol_protocolFloat(v)), nil
	case float32:
		return protocol_marshalProtocolJSON(float64(v), depth+1)
	case protocol_Fields:
		var result bytes.Buffer
		result.WriteByte('{')
		for i, field := range v {
			if i > 0 {
				result.WriteByte(',')
			}
			key, err := protocol_marshalProtocolJSON(field.Key, depth+1)
			if err != nil {
				return nil, err
			}
			encoded, err := protocol_marshalProtocolJSON(field.Value, depth+1)
			if err != nil {
				return nil, err
			}
			result.Write(key)
			result.WriteByte(':')
			result.Write(encoded)
		}
		result.WriteByte('}')
		return result.Bytes(), nil
	case protocol_Map:
		if v == nil {
			return []byte("null"), nil
		}
		fields := protocol_Fields{}
		for _, key := range protocol_SortedKeys(v) {
			fields = append(fields, protocol_Field{key, v[key]})
		}
		return protocol_marshalProtocolJSON(fields, depth+1)
	}
	reflected := reflect.ValueOf(value)
	if reflected.IsValid() {
		switch reflected.Kind() {
		case reflect.Interface, reflect.Pointer:
			if reflected.IsNil() {
				return []byte("null"), nil
			}
			return protocol_marshalProtocolJSON(reflected.Elem().Interface(), depth+1)
		case reflect.Float32, reflect.Float64:
			return protocol_marshalProtocolJSON(reflected.Float(), depth+1)
		case reflect.Map:
			if reflected.Type().Key().Kind() != reflect.String {
				return nil, fmt.Errorf("protocol JSON requires string object keys")
			}
			if reflected.IsNil() {
				return []byte("null"), nil
			}
			keys := reflected.MapKeys()
			sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
			fields := protocol_Fields{}
			for _, key := range keys {
				fields = append(fields, protocol_Field{key.String(), reflected.MapIndex(key).Interface()})
			}
			return protocol_marshalProtocolJSON(fields, depth+1)
		case reflect.Struct, reflect.Func, reflect.Chan, reflect.UnsafePointer, reflect.Complex64, reflect.Complex128:

			return nil, fmt.Errorf("unsupported protocol JSON type")
		}
	}
	if reflected.IsValid() && (reflected.Kind() == reflect.Array || reflected.Kind() == reflect.Slice) {

		if _, ok := value.([]byte); !ok {
			if reflected.Kind() == reflect.Slice && reflected.IsNil() {
				return []byte("null"), nil
			}
			var result bytes.Buffer
			result.WriteByte('[')
			for i := 0; i < reflected.Len(); i++ {
				if i > 0 {
					result.WriteByte(',')
				}
				encoded, err := protocol_marshalProtocolJSON(reflected.Index(i).Interface(), depth+1)
				if err != nil {
					return nil, err
				}
				result.Write(encoded)
			}
			result.WriteByte(']')
			return result.Bytes(), nil
		}
	}
	var result bytes.Buffer
	encoder := json.NewEncoder(&result)
	encoder.SetEscapeHTML(false)
	if reflected.IsValid() {

		if _, number := value.(json.Number); !number {
			switch reflected.Kind() {
			case reflect.String:
				value = reflected.String()
			case reflect.Bool:
				value = reflected.Bool()
			case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
				value = reflected.Int()
			case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
				value = reflected.Uint()
			}
		}
	}
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(result.Bytes(), []byte{'\n'}), nil
}
