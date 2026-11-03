// Extracted from xupay/internal/cslive/protocol_form.go; see SOURCE.json.
package main

import (
	json "encoding/json"
	fmt "fmt"
	url "net/url"
	reflect "reflect"
	sort "sort"
	strconv "strconv"
	strings "strings"
)

// FormEncode 对有序字段逐项展开；普通 map 使用稳定键序，避免依赖 Go 的 map 迭代顺序。
// SDK 的循环对象和过深输入在 256 层停止，不让错误输入耗尽调用栈。
func protocol_FormEncode(value any) (string, error) {
	const maxDepth = 256
	var pairs []string
	var walk func(any, string, int) error
	walk = func(v any, key string, depth int) error {
		if depth > maxDepth {
			return fmt.Errorf("form exceeds depth limit")
		}
		if v == nil {
			return nil
		}
		if fields, ok := v.(protocol_Fields); ok {
			for _, f := range fields {
				next := f.Key
				if key != "" {
					next = key + "[" + f.Key + "]"
				}
				if err := walk(f.Value, next, depth+1); err != nil {
					return err
				}
			}
			return nil
		}
		rv := reflect.ValueOf(v)
		switch rv.Kind() {
		case reflect.Pointer, reflect.Interface:
			if rv.IsNil() {
				return nil
			}
			return walk(rv.Elem().Interface(), key, depth+1)
		case reflect.Map:
			if rv.Type().Key().Kind() != reflect.String {
				return fmt.Errorf("form map keys must be strings")
			}
			keys := rv.MapKeys()
			sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
			for _, k := range keys {
				next := k.String()
				if key != "" {
					next = key + "[" + next + "]"
				}
				if err := walk(rv.MapIndex(k).Interface(), next, depth+1); err != nil {
					return err
				}
			}
			return nil
		case reflect.Slice, reflect.Array:
			for i := 0; i < rv.Len(); i++ {
				if err := walk(rv.Index(i).Interface(), fmt.Sprintf("%s[%d]", key, i), depth+1); err != nil {
					return err
				}
			}
			return nil
		}
		var text string
		switch x := v.(type) {
		case json.Number:
			var err error
			text, err = protocol_protocolNumber(x)
			if err != nil {
				return err
			}
		case float64:
			text = protocol_protocolFloat(x)
		case float32:
			text = protocol_protocolFloat(float64(x))
		default:
			switch rv.Kind() {
			case reflect.String:
				text = rv.String()
			case reflect.Bool:
				text = strconv.FormatBool(rv.Bool())
			case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
				text = strconv.FormatInt(rv.Int(), 10)
			case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
				text = strconv.FormatUint(rv.Uint(), 10)
			case reflect.Float32, reflect.Float64:
				text = protocol_protocolFloat(rv.Float())
			default:
				return fmt.Errorf("unsupported form value type")
			}
		}
		pairs = append(pairs, url.QueryEscape(key)+"="+url.QueryEscape(text))
		return nil
	}
	if err := walk(value, "", 0); err != nil {
		return "", err
	}
	return strings.Join(pairs, "&"), nil
}
