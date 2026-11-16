// Hardware seams for the subvendo core. The board implements these with the
// Arduino ESP8266 core (millis, SPI flash, system_rtc_mem_*, digitalWrite,
// BearSSL br_hmac_*, WiFi UDP/HTTP); the simulator implements them with fakes.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sv {

constexpr size_t kMacLen = 32;
constexpr size_t kKeyLen = 32;
constexpr uint32_t kSectorSize = 4096;  // flash erase unit
constexpr uint32_t kPageSize = 256;     // flash program unit (writes must not cross a page)
constexpr uint32_t kRtcBlock = 4;       // RTC user memory is written in 4-byte blocks
constexpr uint32_t kRtcBlocks = 128;    // 512 B of user memory

// Monotonic ms clock, wraps at 2^32. Compare with (int32_t)(a - b).
struct Clock {
  virtual uint32_t millis() = 0;
};

// SPI flash region owned by the ring. Addresses are relative to the region.
// A program can only clear bits (1 -> 0); erase sets a whole sector to 0xFF.
struct Flash {
  virtual void read(uint32_t addr, void* dst, size_t len) = 0;
  virtual void eraseSector(uint32_t sector) = 0;
  virtual void program(uint32_t addr, const void* src, size_t len) = 0;
};

// RTC user memory. `block` is relative to user memory; the core uses blocks
// >= 32 only (eboot owns the first 32).
struct Rtc {
  virtual void read(uint32_t block, void* dst, size_t nBlocks) = 0;
  virtual void write(uint32_t block, const void* src, size_t nBlocks) = 0;
};

// The SET pin: true lets the acceptor take coins, false makes it return them.
struct Gate {
  virtual void setOpen(bool open) = 0;
};

// HMAC-SHA256 with a 32-byte key.
struct Hmac {
  virtual void mac(const uint8_t* key, const uint8_t* msg, size_t len, uint8_t out[kMacLen]) = 0;
};

// Hardware random numbers (ESP8266: RANDOM_REG32 / os_random()). Used for the
// epoch of a fresh start and for the per-boot nonce.
struct Random {
  virtual uint32_t random32() = 0;
};

// board -> Controller. `bootNonce` is drawn at every boot and tells boots apart;
// `rseq` counts reports within a boot (it restarts at 0 each boot).
struct Report {
  uint32_t vendoId, epoch, total, windowId, bootNonce, rseq;
  uint8_t mac[kMacLen];
};
// Controller -> board: open a Coin Window for `expiresMs` counted from receipt.
// (bootNonce, rseq) echo the latest report the Controller received.
struct Open {
  uint32_t vendoId, windowId, expiresMs, bootNonce, rseq;
  uint8_t mac[kMacLen];
};
// Controller -> board: report (epoch, total, bootNonce, rseq) was processed.
struct Ack {
  uint32_t vendoId, epoch, total, bootNonce, rseq;
  uint8_t mac[kMacLen];
};
struct Inbound {
  enum Kind : uint8_t { kOpen, kAck } kind;
  Open open;
  Ack ack;
};

// Best-effort datagram-style transport: no delivery or order guarantee.
struct Transport {
  virtual void sendReport(const Report& r) = 0;
  virtual bool recv(Inbound& in) = 0;  // false when nothing is waiting
};

struct Hw {
  Clock& clock;
  Flash& flash;
  Rtc& rtc;
  Gate& gate;
  Hmac& hmac;
  Transport& net;
  Random& rand;
};

}  // namespace sv
