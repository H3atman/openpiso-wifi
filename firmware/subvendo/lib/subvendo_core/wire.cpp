#include "wire.h"

namespace sv {

static uint8_t* be32(uint8_t* p, uint32_t v) {
  p[0] = uint8_t(v >> 24);
  p[1] = uint8_t(v >> 16);
  p[2] = uint8_t(v >> 8);
  p[3] = uint8_t(v);
  return p + 4;
}

size_t reportMsg(const Report& r, uint8_t* out) {
  uint8_t* p = out;
  *p++ = kTagReport;
  p = be32(p, r.vendoId);
  p = be32(p, r.epoch);
  p = be32(p, r.total);
  p = be32(p, r.windowId);
  p = be32(p, r.bootNonce);
  p = be32(p, r.rseq);
  return size_t(p - out);
}

size_t openMsg(const Open& o, uint8_t* out) {
  uint8_t* p = out;
  *p++ = kTagOpen;
  p = be32(p, o.vendoId);
  p = be32(p, o.windowId);
  p = be32(p, o.expiresMs);
  p = be32(p, o.bootNonce);
  p = be32(p, o.rseq);
  return size_t(p - out);
}

size_t ackMsg(const Ack& a, uint8_t* out) {
  uint8_t* p = out;
  *p++ = kTagAck;
  p = be32(p, a.vendoId);
  p = be32(p, a.epoch);
  p = be32(p, a.total);
  p = be32(p, a.bootNonce);
  p = be32(p, a.rseq);
  return size_t(p - out);
}

// Bitwise CRC-32 (IEEE, reflected). Slow but tiny and called once per burst.
uint32_t crc32(const void* data, size_t len) {
  const uint8_t* p = static_cast<const uint8_t*>(data);
  uint32_t c = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; i++) {
    c ^= p[i];
    for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
  }
  return ~c;
}

void sealRec(Rec& r) {
  r.magic = kRecMagic;
  r.crc = crc32(&r, sizeof(Rec) - sizeof(uint32_t));
}

bool recValid(const Rec& r) {
  return r.magic == kRecMagic && r.crc == crc32(&r, sizeof(Rec) - sizeof(uint32_t));
}

}  // namespace sv
