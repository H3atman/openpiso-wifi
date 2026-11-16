// Hardware-independent subvendo logic: window-gated cumulative coin total.
// See docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md Q5
// and firmware/subvendo/TEST-PLAN.md for the decisions this implements.
// No heap, no exceptions, no Arduino headers.
#pragma once
#include "ring.h"

namespace sv {

// When the total is saved to flash (always in the main loop, never in the ISR).
enum class Persist : uint8_t {
  kBurst,  // once, when the burst ends (default)
  kPulse,  // after every counted pulse; a slow erase only delays the next save
};

struct Config {
  uint32_t vendoId = 1;
  uint8_t key[kKeyLen] = {};
  Persist persist = Persist::kBurst;
  uint32_t debounceMs = 3;      // edges closer than this to the last accepted edge are dropped
  uint32_t quietMs = 130;       // quiet time that ends a burst (KiTifi value; bench CH-926 later)
  uint32_t windowGapMs = 300;   // min time between a window's end and the next one (CH-926 bench decides)
  uint32_t heartbeatMs = 2000;  // idle report period
  uint32_t resendMs = 1000;     // report period while the latest total is unacked
  uint32_t reachMs = 6000;      // Controller is reachable if a fresh ack arrived this recently
  uint32_t openRecent = 3;      // an open must echo one of the last this-many reports
  uint32_t maxWindowMs = 600000;
  uint32_t ringSectors = 2;     // >= 2
  uint32_t rtcBlock = 32;       // >= 32: eboot owns the first 32 blocks
};

class Core {
 public:
  Core(const Config& cfg, Hw& hw) : cfg_(cfg), hw_(hw), ring_(hw.flash, cfg.ringSectors) {}

  // Restores state (RTC, else flash ring, else a random epoch), draws the boot
  // nonce and blocks the slot.
  void begin();

  // ISR entry (IRAM_ATTR on the board). Touches only three volatile words.
  // `nowMs` is the ms clock read by the caller, so no call leaves this function.
  void onPulse(uint32_t nowMs) {
    uint32_t dt = nowMs - lastEdgeMs_;
    if (edgeSeen_ && dt < cfg_.debounceMs) return;
    if (!edgeSeen_ || dt >= cfg_.quietMs) firstEdgeMs_ = nowMs;  // first edge of a burst
    edgeSeen_ = true;
    lastEdgeMs_ = nowMs;
    total_ = total_ + 1;
  }

  // Main loop step: messages, burst grouping, checkpoint, report, gate.
  void poll();

  // False while a Coin Window is open or a burst is being counted. The board
  // must never restart itself unless this is true.
  bool canReboot() const { return !winOpen_ && !bursting_; }

  // Read-only state for the simulator and logs.
  uint32_t epoch() const { return epoch_; }
  uint32_t total() const { return total_; }  // live count, includes the not-yet-checkpointed
  uint32_t durableTotal() const { return durable_; }
  uint32_t checkpoints() const { return checkpoints_; }
  uint32_t bursts() const { return bursts_; }
  uint32_t flashErases() const { return ring_.erases(); }
  bool windowOpen() const { return winOpen_; }
  bool slotOpen() const { return gateOpen_; }
  uint32_t windowId() const { return winId_; }

 private:
  void checkpoint(uint32_t total);
  void onOpen(const Open& o, uint32_t now);
  void onAck(const Ack& a, uint32_t now);
  void sendReport(uint32_t now);
  uint32_t tagAt(uint32_t ts) const;  // window tag for a burst whose first edge was at ts
  bool pending() const { return !ackedValid_ || (int32_t)(durable_ - acked_) != 0; }

  Config cfg_;
  Hw& hw_;
  Ring ring_;

  // Shared with the ISR.
  volatile uint32_t total_ = 0;
  volatile uint32_t firstEdgeMs_ = 0;
  volatile uint32_t lastEdgeMs_ = 0;
  volatile bool edgeSeen_ = false;

  // Persistent state (mirrors the last checkpoint record).
  uint32_t epoch_ = 0, seq_ = 0, durable_ = 0, tag_ = 0;

  // Burst grouping.
  bool bursting_ = false;
  uint32_t seen_ = 0, burstTag_ = 0, bursts_ = 0;

  // Coin Window. winId_ 0 = none yet this boot; ids only ever increase within a boot.
  bool winOpen_ = false, gateOpen_ = false;
  uint32_t winId_ = 0, winDeadline_ = 0;

  // Reporting and reachability.
  bool ackedValid_ = false, ackSeen_ = false, sentOnce_ = false;
  uint32_t bootNonce_ = 0;
  uint32_t acked_ = 0, rseq_ = 0, lastSendMs_ = 0, lastAckMs_ = 0;
  uint32_t checkpoints_ = 0;
};

}  // namespace sv
