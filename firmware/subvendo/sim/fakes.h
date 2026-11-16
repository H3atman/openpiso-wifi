// Deterministic fakes for every hardware seam, plus the fault machinery.
// Time advances only through World::advance(), one ms at a time, so pulses,
// network delivery and power cuts interleave with core code, including in the
// middle of a flash program/erase.
#pragma once
#include <openssl/hmac.h>

#include <cstring>
#include <deque>
#include <functional>
#include <map>
#include <vector>

#include "hal.h"

namespace sim {

// splitmix64: tiny, fixed algorithm, same stream on every platform.
struct Rng {
  uint64_t s;
  explicit Rng(uint64_t seed) : s(seed * 0x9E3779B97F4A7C15ull + 0x1234567ull) {}
  uint64_t next() {
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
  }
  uint32_t below(uint32_t n) { return n ? uint32_t(next() % n) : 0; }
  uint32_t range(uint32_t lo, uint32_t hi) { return lo + below(hi - lo + 1); }
  bool chance(uint32_t permille) { return below(1000) < permille; }
};

// Thrown out of World::advance() when the board loses power or resets.
struct PowerCut {
  uint32_t elapsed;
};
enum class CutKind { kPower, kWarmKeepRtc, kWarmLoseRtc };

// Clock, seeded random numbers (the board's hardware RNG) and cut plan. `tick`
// runs once per simulated ms (pulses, network, Controller, drivers).
struct World : sv::Clock, sv::Random {
  Rng rng;
  uint32_t now;
  uint32_t cutAt = 0;     // absolute time of a timed cut; 0 = none
  bool armed = false;     // cut inside an upcoming flash/RTC operation
  uint32_t armSkip = 0;   // operations to let finish before the armed cut
  CutKind kind = CutKind::kPower;
  bool busy = false;      // set while a flash/RTC op is in progress
  bool cutInOp = false;   // the last cut landed inside a flash/RTC op
  std::function<void()> tick;

  World(uint64_t seed, uint32_t startMs) : rng(seed), now(startMs) {}
  uint32_t millis() override { return now; }
  uint32_t random32() override { return uint32_t(rng.next()); }

  void advance(uint32_t ms) {
    for (uint32_t e = 0; e < ms; e++) {
      if (cutAt && (int32_t)(now - cutAt) >= 0) {
        cutAt = 0;
        throw PowerCut{e};
      }
      now++;
      tick();
    }
  }

  // A flash/RTC op lasting `dur` ms. If a cut lands inside it, `torn` applies
  // the partial effect before the cut propagates.
  template <class F>
  void timedOp(uint32_t dur, F torn) {
    busy = true;
    try {
      if (armed && armSkip > 0) {
        armSkip--;
        advance(dur);
      } else if (armed) {
        armed = false;
        uint32_t at = rng.below(dur);
        advance(at);
        throw PowerCut{at};
      }
      advance(dur);
    } catch (PowerCut& c) {
      busy = false;
      cutInOp = true;
      torn();
      throw;
    }
    busy = false;
  }
};

// NOR flash: program ANDs bits in, erase sets 0xFF. Torn ops leave partial data.
struct FakeFlash : sv::Flash {
  World& w;
  std::vector<uint8_t> mem;
  std::vector<uint32_t> erases;
  uint32_t programs = 0, tornPrograms = 0, tornErases = 0;
  FakeFlash(World& world, uint32_t sectors)
      : w(world), mem(sectors * sv::kSectorSize, 0xFF), erases(sectors, 0) {}

  void read(uint32_t addr, void* dst, size_t len) override { memcpy(dst, &mem[addr], len); }

  // 0.4-3 ms per page program.
  void program(uint32_t addr, const void* src, size_t len) override {
    const uint8_t* d = static_cast<const uint8_t*>(src);
    programs++;
    w.timedOp(w.rng.range(1, 3), [&] {
      tornPrograms++;
      uint32_t k = w.rng.below(uint32_t(len) + 1);  // bytes fully programmed
      for (uint32_t i = 0; i < k; i++) mem[addr + i] &= d[i];
      if (k < len) mem[addr + k] &= uint8_t(d[k] | w.rng.below(256));  // half-programmed byte
    });
    for (size_t i = 0; i < len; i++) mem[addr + i] &= d[i];
  }

  // 45-400 ms per sector erase. A torn erase leaves a prefix erased, a band of
  // indeterminate bytes, and the old data after it.
  void eraseSector(uint32_t sector) override {
    uint32_t base = sector * sv::kSectorSize;
    erases[sector]++;
    w.timedOp(w.rng.range(45, 400), [&] {
      tornErases++;
      uint32_t k = w.rng.below(sv::kSectorSize + 1);
      for (uint32_t i = 0; i < k; i++) mem[base + i] = 0xFF;
      for (uint32_t i = k; i < k + 64 && i < sv::kSectorSize; i++) mem[base + i] = uint8_t(w.rng.below(256));
    });
    memset(&mem[base], 0xFF, sv::kSectorSize);
  }
};

struct FakeRtc : sv::Rtc {
  World& w;
  uint8_t mem[512];
  uint8_t prev[512];  // contents before the last write, for the "stale RTC" fault
  uint32_t torn = 0;
  explicit FakeRtc(World& world) : w(world) {
    memset(mem, 0xA5, sizeof mem);
    memcpy(prev, mem, sizeof mem);
  }
  void read(uint32_t block, void* dst, size_t n) override { memcpy(dst, mem + block * 4, n * 4); }
  void write(uint32_t block, const void* src, size_t n) override {
    memcpy(prev, mem, sizeof mem);
    w.timedOp(1, [&] {
      torn++;
      uint32_t j = w.rng.below(uint32_t(n) + 1);  // blocks that landed
      memcpy(mem + block * 4, src, j * 4);
    });
    memcpy(mem + block * 4, src, n * 4);
  }
};

struct FakeGate : sv::Gate {
  bool open = false;
  void setOpen(bool o) override { open = o; }
};

// OpenSSL on the PC; the board uses BearSSL br_hmac_*.
struct OsslHmac : sv::Hmac {
  void mac(const uint8_t* key, const uint8_t* msg, size_t len, uint8_t out[sv::kMacLen]) override {
    unsigned n = sv::kMacLen;
    HMAC(EVP_sha256(), key, int(sv::kKeyLen), msg, len, out, &n);
  }
};

// Lossy, duplicating, reordering datagram network in both directions, plus an
// attacker that can inject messages (flagged so the invariants can tell).
struct NetCfg {
  uint32_t lossToCtl = 0, lossToBoard = 0;  // permille
  uint32_t dup = 0;                         // permille
  uint32_t minDelay = 5, maxDelay = 40;     // ms; wide spread causes reordering
};

struct FakeNet : sv::Transport {
  World& w;
  NetCfg cfg;
  struct Rep { uint32_t at; bool attacker; sv::Report m; };
  struct Msg { uint32_t at; bool attacker; sv::Inbound m; };
  std::vector<Rep> toCtl;
  std::vector<Msg> toBoard;
  std::deque<Msg> inbox;  // delivered to the board, read by recv()
  std::vector<sv::Report> capturedReports;
  std::vector<sv::Inbound> capturedToBoard;
  std::map<uint32_t, uint32_t> maxReported;  // epoch -> highest total ever sent
  bool lastRecvAttacker = false;
  bool boardUp = true;

  FakeNet(World& world) : w(world) {}

  uint32_t delay() { return w.rng.range(cfg.minDelay, cfg.maxDelay); }

  void sendReport(const sv::Report& r) override {
    auto it = maxReported.find(r.epoch);
    if (it == maxReported.end() || (int32_t)(r.total - it->second) > 0) maxReported[r.epoch] = r.total;
    capturedReports.push_back(r);
    if (w.rng.chance(cfg.lossToCtl)) return;
    toCtl.push_back({w.now + delay(), false, r});
    if (w.rng.chance(cfg.dup)) toCtl.push_back({w.now + delay(), false, r});
  }

  // Controller -> board.
  void ctlSend(const sv::Inbound& m) {
    capturedToBoard.push_back(m);
    if (w.rng.chance(cfg.lossToBoard)) return;
    toBoard.push_back({w.now + delay(), false, m});
    if (w.rng.chance(cfg.dup)) toBoard.push_back({w.now + delay(), false, m});
  }

  bool recv(sv::Inbound& in) override {
    if (inbox.empty()) return false;
    in = inbox.front().m;
    lastRecvAttacker = inbox.front().attacker;
    inbox.pop_front();
    return true;
  }

  void injectToBoard(const sv::Inbound& m) { toBoard.push_back({w.now + delay(), true, m}); }
  // A forged report is flagged as attacker traffic. An unmodified replay is
  // indistinguishable from a network duplicate, so it is injected unflagged.
  void injectToCtl(const sv::Report& r, bool forged) { toCtl.push_back({w.now + delay(), forged, r}); }
};

}  // namespace sim
