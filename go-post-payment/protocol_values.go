// Extracted from xupay/internal/cslive/protocol_values.go; see SOURCE.json.
package main

import (
	regexp "regexp"
)

func protocol_Matches(pattern, value string) bool {
	return regexp.MustCompile("^(?:" + pattern + ")$").MatchString(value)
}

func protocol_StringSet(v ...string) map[string]bool {
	m := map[string]bool{}
	for _, s := range v {
		m[s] = true
	}
	return m
}
