// Extracted from xupay/internal/cslive/fingerprint_clock.go; see SOURCE.json.
package main

import (
	fmt "fmt"
	time "time"
)

// Clock 只偏移协议指纹的墙上时钟，不参与截止时间或真实耗时。
type fingerprint_Clock struct {
	OffsetHours int
	Now         func() time.Time
}

func fingerprint_NewClock(offset int) (fingerprint_Clock, error) {
	if offset != 0 && offset != -24 {
		return fingerprint_Clock{}, fmt.Errorf("fingerprint_clock_offset_hours must be 0 or -24")
	}
	return fingerprint_Clock{OffsetHours: offset}, nil
}

func (c fingerprint_Clock) NowMS() float64 {
	now := time.Now
	if c.Now != nil {
		now = c.Now
	}
	t := now()
	return (float64(t.Unix()) + float64(t.Nanosecond())/1e9 + float64(c.OffsetHours)*3600) * 1000
}

func (c fingerprint_Clock) LocalTime(zone string) (time.Time, error) {
	if zone == "" {
		zone = "UTC"
	}
	loc, err := time.LoadLocation(zone)
	if err != nil {
		return time.Time{}, err
	}
	seconds := c.NowMS() / 1000
	return time.Unix(int64(seconds), int64((seconds-float64(int64(seconds)))*1e9)).In(loc), nil
}

func (c fingerprint_Clock) TimezoneOffsetMin(zone string) (int, error) {
	t, err := c.LocalTime(zone)
	if err != nil {
		return 0, err
	}
	_, seconds := t.Zone()
	return -(seconds / 60), nil
}
