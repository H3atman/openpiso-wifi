// Package vendoprotocol defines the ESP8266 Vendo wire format from issue #15.
// A frame is a one-byte tag, big-endian u32 fields, and a full HMAC-SHA256.
package vendoprotocol

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
)

const (
	Report byte = 0x01
	Open   byte = 0x02
	Ack    byte = 0x03
	Close  byte = 0x04
	Closed byte = 0x05
	MACLen      = sha256.Size
)

var ErrFrame = errors.New("invalid Vendo frame")
var ErrHMAC = errors.New("invalid Vendo HMAC")

// Message uses the same field names across tags; fields absent from a tag are
// not serialized. Open/Close use the Vendo's newest freshness pair; Ack uses
// the pair from the specific Report/Closed being acknowledged.
type Message struct {
	Tag       byte
	VendoID   uint32
	Epoch     uint32
	Total     uint32
	WindowID  uint32
	ExpiresMS uint32
	BootNonce uint32
	RSeq      uint32
}

func messageLen(tag byte) int {
	switch tag {
	case Report, Closed:
		return 25
	case Open, Ack:
		return 21
	case Close:
		return 17
	default:
		return 0
	}
}

func (m Message) Sign(key []byte) ([]byte, error) {
	if len(key) != 32 || messageLen(m.Tag) == 0 {
		return nil, ErrFrame
	}
	fields := []uint32{m.VendoID}
	switch m.Tag {
	case Report, Closed:
		fields = append(fields, m.Epoch, m.Total, m.WindowID)
	case Open:
		fields = append(fields, m.WindowID, m.ExpiresMS)
	case Ack:
		fields = append(fields, m.Epoch, m.Total)
	case Close:
		fields = append(fields, m.WindowID)
	}
	fields = append(fields, m.BootNonce, m.RSeq)
	body := make([]byte, messageLen(m.Tag), messageLen(m.Tag)+MACLen)
	body[0] = m.Tag
	for index, value := range fields {
		binary.BigEndian.PutUint32(body[1+4*index:], value)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(body)
	return mac.Sum(body), nil
}

// Parse checks structure only. The caller must Verify before trusting any field.
func Parse(frame []byte) (Message, error) {
	if len(frame) == 0 || messageLen(frame[0]) == 0 || len(frame) != messageLen(frame[0])+MACLen {
		return Message{}, ErrFrame
	}
	m := Message{Tag: frame[0], VendoID: binary.BigEndian.Uint32(frame[1:])}
	position := 5
	next := func() uint32 {
		value := binary.BigEndian.Uint32(frame[position:])
		position += 4
		return value
	}
	switch m.Tag {
	case Report, Closed:
		m.Epoch, m.Total, m.WindowID = next(), next(), next()
	case Open:
		m.WindowID, m.ExpiresMS = next(), next()
	case Ack:
		m.Epoch, m.Total = next(), next()
	case Close:
		m.WindowID = next()
	}
	m.BootNonce, m.RSeq = next(), next()
	return m, nil
}

func Verify(frame, key []byte) error {
	if len(key) != 32 || len(frame) < MACLen {
		return ErrHMAC
	}
	message := frame[:len(frame)-MACLen]
	mac := hmac.New(sha256.New, key)
	mac.Write(message)
	if !hmac.Equal(frame[len(message):], mac.Sum(nil)) {
		return ErrHMAC
	}
	return nil
}
