#include "core.h"

#include <string.h>

namespace sv {

// Constant-time MAC compare.
static bool macEq(const uint8_t* a, const uint8_t* b) {
  uint8_t d = 0;
  for (size_t i = 0; i < kMacLen; i++) d |= uint8_t(a[i] ^ b[i]);
  return d == 0;
}

// Restore order: newest valid of {RTC, flash ring} (a tie goes to flash);
// nothing valid means a random epoch with total 0 (it cannot collide with an
// epoch the Controller already knows). The slot stays blocked until a window
// opens and the Controller is reachable.
void Core::begin() {
  hw_.gate.setOpen(false);
  gateOpen_ = false;
  bootNonce_ = hw_.rand.random32();

  Rec rr, fr, pick;
  hw_.rtc.read(cfg_.rtcBlock, &rr, sizeof(Rec) / kRtcBlock);
  bool rv = recValid(rr);
  bool fv = ring_.scan(fr);
  bool have = true;
  if (rv && (!fv || (int32_t)(rr.seq - fr.seq) > 0)) pick = rr;
  else if (fv) pick = fr;
  else have = false;

  if (have) {
    epoch_ = pick.epoch;
    seq_ = pick.seq;
    durable_ = pick.total;
    tag_ = pick.tag;
  } else {
    epoch_ = hw_.rand.random32();
    seq_ = 0;
    durable_ = 0;
    tag_ = 0;
  }
  total_ = durable_;
  seen_ = durable_;
  if (!have) checkpoint(0);  // persist the new epoch before it is ever reported
}

// Writes {epoch, total, tag} to flash first, then RTC. Reports are only sent
// for a total that is already in flash.
void Core::checkpoint(uint32_t total) {
  Rec r = {};
  r.seq = ++seq_;
  r.epoch = epoch_;
  r.total = total;
  r.tag = tag_;
  sealRec(r);
  ring_.append(r);
  hw_.rtc.write(cfg_.rtcBlock, &r, sizeof(Rec) / kRtcBlock);
  durable_ = total;
  checkpoints_++;
}

// Window tag for a burst whose first edge was at `ts`: the window id if that
// window was still in force then, else 0. A new window cannot start while a
// burst is uncounted (see onOpen), so ts is never before the window started.
uint32_t Core::tagAt(uint32_t ts) const {
  return winId_ && (int32_t)(ts - winDeadline_) < 0 ? winId_ : 0;
}

void Core::sendReport(uint32_t now) {
  Report r = {};
  r.vendoId = cfg_.vendoId;
  r.epoch = epoch_;
  r.total = durable_;
  r.windowId = tag_;
  r.bootNonce = bootNonce_;
  r.rseq = ++rseq_;
  uint8_t m[kReportMsgLen];
  hw_.hmac.mac(cfg_.key, m, reportMsg(r, m), r.mac);
  hw_.net.sendReport(r);
  lastSendMs_ = now;
  sentOnce_ = true;
}

// Accepts an authentic open that echoes this boot's nonce and one of the last
// `openRecent` reports (so a captured or delayed open from earlier is dead).
// An id not above the current one is a duplicate: a no-op, the deadline is
// never extended. Otherwise the board just ignores the open and the Controller
// resends it, until all three hold: every counted pulse is saved and acked, and
// the previous window ended at least `windowGapMs` ago (so a coin accepted just
// before it ended has started its pulses and is tagged to the old window).
void Core::onOpen(const Open& o, uint32_t now) {
  if (o.vendoId != cfg_.vendoId || o.bootNonce != bootNonce_ || (uint32_t)(rseq_ - o.rseq) >= cfg_.openRecent) return;
  if ((int32_t)(o.windowId - winId_) <= 0) return;
  if (total_ != durable_ || pending()) return;
  if (winId_ && (int32_t)(now - (winDeadline_ + cfg_.windowGapMs)) < 0) return;
  uint8_t m[kOpenMsgLen], mac[kMacLen];
  hw_.hmac.mac(cfg_.key, m, openMsg(o, m), mac);
  if (!macEq(mac, o.mac)) return;

  winId_ = o.windowId;
  winDeadline_ = now + (o.expiresMs < cfg_.maxWindowMs ? o.expiresMs : cfg_.maxWindowMs);
  winOpen_ = true;
}

// An authentic ack for this boot and epoch advances `acked_`; one that echoes
// the latest rseq also proves the Controller is reachable right now.
void Core::onAck(const Ack& a, uint32_t now) {
  if (a.vendoId != cfg_.vendoId || a.epoch != epoch_ || a.bootNonce != bootNonce_) return;
  uint8_t m[kAckMsgLen], mac[kMacLen];
  hw_.hmac.mac(cfg_.key, m, ackMsg(a, m), mac);
  if (!macEq(mac, a.mac)) return;
  if (a.rseq == rseq_) {
    ackSeen_ = true;
    lastAckMs_ = now;
  }
  if ((int32_t)(a.total - durable_) <= 0 && (!ackedValid_ || (int32_t)(a.total - acked_) > 0)) {
    acked_ = a.total;
    ackedValid_ = true;
  }
}

// One main-loop step. Blocking flash work happens inside checkpoint(); the ISR
// keeps counting meanwhile and those pulses are picked up on the next step.
void Core::poll() {
  uint32_t now = hw_.clock.millis();

  Inbound in;
  while (hw_.net.recv(in)) {
    if (in.kind == Inbound::kOpen) onOpen(in.open, now);
    else onAck(in.ack, now);
  }

  // Consistent snapshot of the ISR words (the board masks interrupts here).
  uint32_t t, first, last;
  do {
    t = total_;
    first = firstEdgeMs_;
    last = lastEdgeMs_;
  } while (t != total_);

  if (t != seen_) {
    if (!bursting_) {
      bursting_ = true;
      burstTag_ = tagAt(first);  // a burst belongs to the window open at its first edge
    }
    seen_ = t;
    if (cfg_.persist == Persist::kPulse) {
      tag_ = burstTag_;
      checkpoint(t);
    }
  } else if (bursting_ && (int32_t)(now - last) >= (int32_t)cfg_.quietMs) {
    if (durable_ != t) {
      tag_ = burstTag_;
      checkpoint(t);
    }
    bursting_ = false;
    bursts_++;
    sendReport(hw_.clock.millis());
  }

  now = hw_.clock.millis();
  if (winOpen_ && (int32_t)(now - winDeadline_) >= 0) winOpen_ = false;

  uint32_t every = pending() ? cfg_.resendMs : cfg_.heartbeatMs;
  if (!sentOnce_ || (uint32_t)(now - lastSendMs_) >= every) sendReport(now);

  bool reachable = ackSeen_ && (int32_t)(now - lastAckMs_) < (int32_t)cfg_.reachMs;
  bool want = winOpen_ && reachable;
  if (want != gateOpen_) {
    hw_.gate.setOpen(want);
    gateOpen_ = want;
  }
}

}  // namespace sv
