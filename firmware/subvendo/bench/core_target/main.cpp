// Build check: lib/subvendo_core compiled with the ESP8266 core 3.1.2 xtensa GCC under -Werror,
// instantiated against stub HALs. Not meant to do anything useful on a board; the RAM/flash
// numbers of this env are the core's footprint plus the Arduino runtime.
#include <Arduino.h>
#include <string.h>
#include "core.h"

namespace {

struct StubClock : sv::Clock {
  uint32_t millis() override { return ::millis(); }
};

// 2 sectors of RAM flash, enough to exercise the ring code paths.
struct StubFlash : sv::Flash {
  uint8_t mem[2 * sv::kSectorSize];
  StubFlash() { memset(mem, 0xFF, sizeof mem); }
  void read(uint32_t a, void* d, size_t n) override { memcpy(d, mem + a, n); }
  void eraseSector(uint32_t s) override { memset(mem + s * sv::kSectorSize, 0xFF, sv::kSectorSize); }
  void program(uint32_t a, const void* s, size_t n) override {
    const uint8_t* p = static_cast<const uint8_t*>(s);
    for (size_t i = 0; i < n; i++) mem[a + i] &= p[i];
  }
};

struct StubRtc : sv::Rtc {
  uint32_t mem[sv::kRtcBlocks] = {};
  void read(uint32_t b, void* d, size_t n) override { memcpy(d, mem + b, n * sv::kRtcBlock); }
  void write(uint32_t b, const void* s, size_t n) override { memcpy(mem + b, s, n * sv::kRtcBlock); }
};

struct StubGate : sv::Gate {
  void setOpen(bool) override {}
};

struct StubHmac : sv::Hmac {
  void mac(const uint8_t*, const uint8_t*, size_t, uint8_t out[sv::kMacLen]) override {
    memset(out, 0, sv::kMacLen);
  }
};

struct StubNet : sv::Transport {
  uint32_t sent = 0;
  void sendReport(const sv::Report&) override { sent++; }
  bool recv(sv::Inbound&) override { return false; }
};

// The real seam is the hardware RNG (RANDOM_REG32, also exposed as os_random()).
struct StubRandom : sv::Random {
  uint32_t random32() override { return RANDOM_REG32; }
};

StubClock clock_;
StubFlash flash_;
StubRtc rtc_;
StubGate gate_;
StubHmac hmac_;
StubNet net_;
StubRandom rand_;
sv::Hw hw{clock_, flash_, rtc_, gate_, hmac_, net_, rand_};
sv::Config cfg;
sv::Core core(cfg, hw);

void IRAM_ATTR onCoin() { core.onPulse(millis()); }

}  // namespace

void setup() {
  Serial.begin(115200);
  core.begin();
  attachInterrupt(digitalPinToInterrupt(2), onCoin, RISING);
}

void loop() {
  core.poll();
  Serial.printf("total=%u durable=%u epoch=%u\n", core.total(), core.durableTotal(), core.epoch());
  delay(1000);
}
