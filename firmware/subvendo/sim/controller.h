// Minimal Controller model, a test fake only (the real Controller is Go).
// Verifies the HMAC, allows one open Coin Window per Vendo, keeps
// last_seen per (vendo_id, epoch), credits (total - last_seen) when positive
// and the window tag is one it issued, and audits the rest. It remembers the
// (boot_nonce, rseq) of the newest report and echoes it in every open and ack.
#pragma once
#include <set>

#include "fakes.h"
#include "wire.h"

namespace sim {

struct Controller {
  World& w;
  FakeNet& net;
  sv::Hmac& hmac;
  uint32_t vendoId;
  uint8_t key[sv::kKeyLen];
  uint32_t resendMs = 1000;

  bool up = true;
  // The single Coin Window this Vendo may have open.
  bool live = false;
  uint32_t curId = 0, deadline = 0, lastOpenSent = 0;
  std::set<uint32_t> issued;
  std::map<uint32_t, uint32_t> deadlineOf;  // window id -> when the Controller closed it

  std::map<uint32_t, uint32_t> lastSeen;      // epoch -> highest total credited or audited
  std::map<uint32_t, uint64_t> credited;      // window id -> pulses
  uint64_t audited = 0;                       // pulses sent to the audit queue
  uint64_t regressRows = 0;                   // reports with total below last_seen (audited as events)
  uint64_t forgedRejected = 0, creditedFromAttacker = 0, secondWindowRefused = 0;

  Controller(World& world, FakeNet& n, sv::Hmac& h, uint32_t id, const uint8_t* k)
      : w(world), net(n), hmac(h), vendoId(id) {
    memcpy(key, k, sizeof key);
  }

  // Opens a Coin Window for `durMs`; refused while one is still open.
  bool openWindow(uint32_t durMs) {
    if (live) {
      secondWindowRefused++;
      return false;
    }
    curId = ++nextId_;
    issued.insert(curId);
    deadline = w.now + durMs;
    deadlineOf[curId] = deadline;
    live = true;
    sendOpen();
    return true;
  }

  // Resends `open` with the remaining time until the window ends.
  void tick() {
    if (live && (int32_t)(w.now - deadline) >= 0) live = false;
    if (up && live && w.now - lastOpenSent >= resendMs) sendOpen();
  }

  void onReport(const sv::Report& r, bool attacker) {
    if (!up) return;
    uint8_t m[sv::kReportMsgLen], mac[sv::kMacLen];
    hmac.mac(key, m, sv::reportMsg(r, m), mac);
    if (r.vendoId != vendoId || memcmp(mac, r.mac, sizeof mac) != 0) {
      forgedRejected++;
      return;
    }
    // A new boot nonce replaces the echo; within a boot only a newer rseq does.
    if (r.bootNonce != nonce_ || (int32_t)(r.rseq - rseq_) > 0) nonce_ = r.bootNonce, rseq_ = r.rseq;
    uint32_t& last = lastSeen.emplace(r.epoch, 0).first->second;  // new epoch: baseline 0
    int32_t delta = (int32_t)(r.total - last);
    if (delta > 0) {
      if (attacker) creditedFromAttacker += uint32_t(delta);
      if (r.windowId != 0 && issued.count(r.windowId)) credited[r.windowId] += uint32_t(delta);
      else audited += uint32_t(delta);
      last = r.total;
    } else if (delta < 0) {
      regressRows++;
    }
    sv::Inbound a = {};
    a.kind = sv::Inbound::kAck;
    a.ack = {vendoId, r.epoch, r.total, r.bootNonce, r.rseq, {}};
    uint8_t am[sv::kAckMsgLen];
    hmac.mac(key, am, sv::ackMsg(a.ack, am), a.ack.mac);
    net.ctlSend(a);
  }

 private:
  void sendOpen() {
    sv::Inbound o = {};
    o.kind = sv::Inbound::kOpen;
    o.open = {vendoId, curId, deadline - w.now, nonce_, rseq_, {}};
    uint8_t m[sv::kOpenMsgLen];
    hmac.mac(key, m, sv::openMsg(o.open, m), o.open.mac);
    net.ctlSend(o);
    lastOpenSent = w.now;
  }
  uint32_t nextId_ = 0, nonce_ = 0, rseq_ = 0;
};

}  // namespace sim
