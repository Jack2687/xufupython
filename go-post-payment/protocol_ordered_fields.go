// Extracted from xupay/internal/cslive/protocol_ordered_fields.go; see SOURCE.json.
package main

import (
	bytes "bytes"
	json "encoding/json"
	fmt "fmt"
)

// Fields 保留请求字段和冻结确认表单的顺序。普通 map 不承诺插入顺序。
type protocol_Field struct {
	Key   string
	Value any
}

type protocol_Fields []protocol_Field

func protocol_O(values ...any) protocol_Fields {
	if len(values)%2 != 0 {
		panic("invalid ordered object")
	}
	f := make(protocol_Fields, 0, len(values)/2)
	for i := 0; i < len(values); i += 2 {
		f.Set(values[i].(string), values[i+1])
	}
	return f
}

func (f *protocol_Fields) Set(key string, value any) {
	for i := range *f {
		if (*f)[i].Key == key {
			(*f)[i].Value = value
			return
		}
	}
	*f = append(*f, protocol_Field{key, value})
}

func (f protocol_Fields) Get(key string) any {
	for _, p := range f {
		if p.Key == key {
			return p.Value
		}
	}
	return nil
}

func (f *protocol_Fields) Delete(key string) {
	for i, p := range *f {
		if p.Key == key {
			*f = append((*f)[:i], (*f)[i+1:]...)
			return
		}
	}
}

func (f *protocol_Fields) Update(other protocol_Fields) {
	for _, p := range other {
		f.Set(p.Key, p.Value)
	}
}

func (f protocol_Fields) Clone() protocol_Fields { return append(protocol_Fields{}, f...) }

func (f protocol_Fields) MarshalJSON() ([]byte, error) { return protocol_marshalProtocolJSON(f, 0) }

func (f *protocol_Fields) UnmarshalJSON(raw []byte) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, e := protocol_DecodeOrdered(d)
	if e != nil {
		return e
	}
	obj, ok := v.(protocol_Fields)
	if !ok {
		return fmt.Errorf("expected JSON object")
	}
	*f = obj
	return nil
}

func protocol_DecodeOrdered(d *json.Decoder) (any, error) {
	t, e := d.Token()
	if e != nil {
		return nil, e
	}
	switch t {
	case json.Delim('{'):
		f := protocol_Fields{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return nil, e
			}
			v, e := protocol_DecodeOrdered(d)
			if e != nil {
				return nil, e
			}
			f.Set(k.(string), v)
		}
		_, e = d.Token()
		return f, e
	case json.Delim('['):
		a := []any{}
		for d.More() {
			v, e := protocol_DecodeOrdered(d)
			if e != nil {
				return nil, e
			}
			a = append(a, v)
		}
		_, e = d.Token()
		return a, e
	}
	return t, nil
}
