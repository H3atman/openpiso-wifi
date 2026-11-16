#include "run.h"

#include <algorithm>
#include <cstdio>
#include <memory>

#include "controller.h"
#include "core.h"

namespace sim {

namespace {

// One edge on the acceptor line. kind: 0 real coin pulse, 1 isolated glitch,
// 2 contact bounce right after a real edge.
struct Edge {
  uint32_t at;
  int kind;
  uint32_t win;  // window the coin was inserted in (real edges)
};

constexpr uint32_t kSlack = 600;  // max flash op (400 ms) plus margin, for the gate check

struct Run {
  const Scenario& sc;
  World w;
  FakeFlash fl;
  FakeRtc rtc;
  FakeGate gate;
  OsslHmac hm;
  FakeNet net;
  sv::Config cfg;
  sv::Hw hw;
  Controller ctl;
  std::unique_ptr<sv::Core> core;
  Result r;

  bool begun = false, active = true;  // active: new coins, glitches, attacks, cuts allowed
  std::vector<Edge> edges;
  std::map<uint32_t, uint64_t> realByWin;
  // Live state just before a cut, to measure what the restore lost.
  bool ghostValid = false, ghostInOp = false;
  uint32_t ghostEpoch = 0, ghostTotal = 0;
  std::vector<std::pair<uint32_t, uint32_t>> bumps;  // (old epoch, live total) when the epoch changed
  uint32_t nextCoin, nextWindow, nextGlitch = 0, nextAttack = 0, nextCtlDown = 0, ctlUpAt = 0;
  uint32_t nextCut = 0, armDeadline = 0, lateCoinAt = 0, genuineAckAt;
  std::vector<uint32_t> attackerIds, genuineIds;  // open ids delivered since the last poll
  uint32_t lastWid = 0;

  static sv::Config makeCfg(sv::Persist p) {
    sv::Config c;
    for (uint32_t i = 0; i < sv::kKeyLen; i++) c.key[i] = uint8_t(i * 7 + 3);
    c.persist = p;
    return c;
  }

  Run(const Scenario& s, uint64_t seed, sv::Persist p)
      : sc(s), w(seed, s.startMs), fl(w, 4), rtc(w), net(w), cfg(makeCfg(p)),
        hw{w, fl, rtc, gate, hm, net, w}, ctl(w, net, hm, cfg.vendoId, cfg.key), genuineAckAt(s.startMs) {
    net.cfg = s.net;
    nextCoin = w.now + w.rng.range(500, s.coinEveryMs);
    nextWindow = w.now + 1000;
    if (s.glitchEveryMs) nextGlitch = w.now + w.rng.range(1, s.glitchEveryMs * 2);
    if (s.attackEveryMs) nextAttack = w.now + w.rng.range(1, s.attackEveryMs * 2);
    if (s.ctlDownEveryMs) nextCtlDown = w.now + w.rng.range(1, s.ctlDownEveryMs * 2);
    // Pre-seed the ring and the Controller baseline so wraps and erases happen early.
    ctl.lastSeen[1] = s.startTotal;
    uint32_t n = s.prefillRecords ? s.prefillRecords : (s.startTotal ? 1 : 0);
    for (uint32_t i = 0; i < n; i++) {
      sv::Rec rec = {};
      rec.seq = i + 1;
      rec.epoch = 1;
      rec.total = s.startTotal;
      sv::sealRec(rec);
      uint32_t off = (i / sv::kSlotsPerSector) * sv::kSectorSize + (i % sv::kSlotsPerSector) * sizeof rec;
      memcpy(&fl.mem[off], &rec, sizeof rec);
    }
    w.tick = [this] { tick(); };
  }

  void fail(uint64_t& counter, const char* what) {
    if (counter++ == 0 && r.detail.empty()) {
      char b[96];
      snprintf(b, sizeof b, "%s at t=%u", what, w.now);
      r.detail = b;
    }
  }

  // --- acceptor -----------------------------------------------------------

  // A coin of `v` pulses is offered. Taken only if SET is open right now.
  void insertCoin(uint32_t v) {
    if (!begun || !gate.open) {
      r.blockedCoins++;
      return;
    }
    r.coins++;
    r.acceptedPulses += v;
    uint32_t win = core->windowOpen() ? core->windowId() : 0;
    uint32_t t = w.now + w.rng.range(20, 60), period = w.rng.range(65, 100);
    for (uint32_t i = 0; i < v; i++, t += period + w.rng.below(6)) {
      edges.push_back({t, 0, win});
      if (w.rng.chance(sc.bouncePermille)) edges.push_back({t + w.rng.range(1, 2), 2, 0});
    }
  }

  void deliverEdge(const Edge& e) {
    if (!begun) {
      if (e.kind == 0) r.lostDown++;
      return;
    }
    uint32_t before = core->total();
    core->onPulse(w.now);
    bool counted = core->total() != before;
    if (e.kind == 0) {
      if (counted) r.realCounted++, realByWin[e.win]++;
      else r.realDropped++;
    } else if (counted) {
      r.spurious++;
      if (e.kind == 2) r.bounceLeaks++;
    }
  }

  // --- attacker -----------------------------------------------------------

  // Injects one forged or replayed message of a random kind.
  void attack() {
    r.attacks++;
    uint32_t k = w.rng.below(7);
    auto& reps = net.capturedReports;
    auto& outs = net.capturedToBoard;
    if (sc.replayOpensOnly) {
      if (outs.empty()) return;
      const sv::Inbound& m = outs[w.rng.below(uint32_t(outs.size()))];
      if (m.kind == sv::Inbound::kOpen) net.injectToBoard(m);
      return;
    }
    if (k == 0 && !reps.empty()) {  // replayed report
      net.injectToCtl(reps[w.rng.below(uint32_t(reps.size()))], false);
    } else if (k == 1 && !reps.empty()) {  // forged report: inflated total, stale mac
      sv::Report f = reps[w.rng.below(uint32_t(reps.size()))];
      f.total += 50;
      net.injectToCtl(f, true);
    } else if (k == 2 && !outs.empty()) {  // replayed open or ack
      net.injectToBoard(outs[w.rng.below(uint32_t(outs.size()))]);
    } else if (k == 3) {  // forged open, future id, random mac
      sv::Inbound o = {};
      o.kind = sv::Inbound::kOpen;
      o.open = {cfg.vendoId, ctl.curId + 1 + w.rng.below(5), 60000, w.rng.below(4), w.rng.below(4), {}};
      for (auto& b : o.open.mac) b = uint8_t(w.rng.below(256));
      net.injectToBoard(o);
    } else if (k == 4 && !reps.empty()) {  // type confusion: report mac reused as ack and as open
      const sv::Report& p = reps[w.rng.below(uint32_t(reps.size()))];
      sv::Inbound a = {};
      a.kind = sv::Inbound::kAck;
      a.ack = {p.vendoId, p.epoch, p.total, p.bootNonce, p.rseq, {}};
      memcpy(a.ack.mac, p.mac, sv::kMacLen);
      net.injectToBoard(a);
      sv::Inbound o = {};
      o.kind = sv::Inbound::kOpen;
      o.open = {p.vendoId, ctl.curId + 1, p.total, p.bootNonce, p.rseq, {}};
      memcpy(o.open.mac, p.mac, sv::kMacLen);
      net.injectToBoard(o);
    } else if (k == 5) {  // second window requested while one is open
      ctl.openWindow(10000);
    } else {  // replay the oldest captured open
      for (auto& m : outs)
        if (m.kind == sv::Inbound::kOpen) {
          net.injectToBoard(m);
          break;
        }
    }
  }

  // --- per-ms world step --------------------------------------------------

  void tick() {
    // Network delivery.
    for (size_t i = 0; i < net.toCtl.size();) {
      if ((int32_t)(w.now - net.toCtl[i].at) >= 0) {
        auto m = net.toCtl[i];
        net.toCtl.erase(net.toCtl.begin() + long(i));
        ctl.onReport(m.m, m.attacker);
      } else i++;
    }
    for (size_t i = 0; i < net.toBoard.size();) {
      if ((int32_t)(w.now - net.toBoard[i].at) >= 0) {
        auto m = net.toBoard[i];
        net.toBoard.erase(net.toBoard.begin() + long(i));
        if (begun) {
          net.inbox.push_back(m);
          if (m.m.kind == sv::Inbound::kOpen) (m.attacker ? attackerIds : genuineIds).push_back(m.m.open.windowId);
          else if (!m.attacker) genuineAckAt = w.now;
        }
      } else i++;
    }
    ctl.tick();

    // Acceptor line.
    for (size_t i = 0; i < edges.size();) {
      if ((int32_t)(w.now - edges[i].at) >= 0) {
        Edge e = edges[i];
        edges.erase(edges.begin() + long(i));
        deliverEdge(e);
      } else i++;
    }

    // Drivers.
    if (active && (int32_t)(w.now - nextCoin) >= 0) {
      static const uint32_t vals[] = {1, 1, 5, 5, 10};
      insertCoin(vals[w.rng.below(5)]);
      nextCoin = w.now + w.rng.range(sc.coinEveryMs / 2, sc.coinEveryMs * 3 / 2);
    }
    if (active && lateCoinAt && (int32_t)(w.now - lateCoinAt) >= 0) {
      lateCoinAt = 0;
      insertCoin(10);  // longest burst, so it is still in flight when the window ends
    }
    if (active && ctl.up && !ctl.live && (int32_t)(w.now - nextWindow) >= 0) {
      uint32_t d = w.rng.range(sc.winMinMs, sc.winMaxMs);
      ctl.openWindow(d);
      nextWindow = w.now + d + w.rng.range(sc.gapMinMs, sc.gapMaxMs);
      if (sc.lateCoins) lateCoinAt = w.now + d + w.rng.range(10, 40) - w.rng.range(0, 120);
    }
    if (nextGlitch && active && (int32_t)(w.now - nextGlitch) >= 0) {
      deliverEdge({w.now, 1, 0});
      nextGlitch = w.now + w.rng.range(1, sc.glitchEveryMs * 2);
    }
    if (nextAttack && active && (int32_t)(w.now - nextAttack) >= 0) {
      attack();
      nextAttack = w.now + w.rng.range(1, sc.attackEveryMs * 2);
    }
    if (nextCtlDown && active && (int32_t)(w.now - nextCtlDown) >= 0) {
      ctl.up = false;
      ctlUpAt = w.now + w.rng.range(sc.ctlDownMinMs, sc.ctlDownMaxMs);
      nextCtlDown = ctlUpAt + w.rng.range(1, sc.ctlDownEveryMs * 2);
    }
    if (ctlUpAt && (int32_t)(w.now - ctlUpAt) >= 0) ctl.up = true, ctlUpAt = 0;
    if (nextCut && active && begun && (int32_t)(w.now - nextCut) >= 0) {
      nextCut = 0;
      if (w.rng.chance(sc.armedPermille)) w.armed = true, w.armSkip = w.rng.below(3), armDeadline = w.now + 20000;
      else w.cutAt = w.now;
      w.kind = sc.cutKind;
    }
    if (w.armed && (int32_t)(w.now - armDeadline) >= 0) w.armed = false, scheduleCut();

    // I3: the slot may only be open while the Controller has acked recently. A replayed
    // ack for the current rseq can extend reachability by at most one heartbeat.
    if (gate.open && (int32_t)(w.now - genuineAckAt) > (int32_t)(cfg.reachMs + cfg.heartbeatMs + kSlack)) fail(r.i3, "slot open without a fresh genuine ack");
  }

  void scheduleCut() {
    if (sc.cutEveryMs) nextCut = w.now + w.rng.range(sc.cutEveryMs / 2, sc.cutEveryMs * 3 / 2);
  }

  // --- cut and restore ----------------------------------------------------

  // Storage damage beyond a plain cut, to force the restore fallbacks.
  void damage() {
    r.damaged++;
    auto slotValid = [&](uint32_t i, sv::Rec& rec) {
      memcpy(&rec, &fl.mem[i * sizeof rec], sizeof rec);
      return sv::recValid(rec);
    };
    uint32_t slots = uint32_t(fl.mem.size() / sizeof(sv::Rec));
    sv::Rec rec;
    if (sc.failMode == 1) {  // every record gets a bad CRC; RTC is wiped by the cut kind
      for (uint32_t i = 0; i < slots; i++)
        if (slotValid(i, rec)) fl.mem[i * sizeof rec + 28] ^= 1;
    } else if (sc.failMode == 2) {  // newest flash record destroyed, RTC keeps the truth
      uint32_t best = slots;
      uint32_t bseq = 0;
      for (uint32_t i = 0; i < slots; i++)
        if (slotValid(i, rec) && (best == slots || (int32_t)(rec.seq - bseq) > 0)) best = i, bseq = rec.seq;
      if (best < slots) fl.mem[best * sizeof rec + 28] ^= 1;
    } else if (sc.failMode == 3) {  // flash blank, RTC wiped: nothing left to learn the epoch from
      std::fill(fl.mem.begin(), fl.mem.end(), 0xFF);
    }
  }

  void applyCut() {
    r.cuts++;
    bool inOp = w.cutInOp;
    w.cutInOp = false;
    if (core && begun) {
      ghostValid = true;
      ghostInOp = inOp;
      ghostEpoch = core->epoch();
      ghostTotal = core->total();
    }
    if (core) r.bursts += core->bursts();
    begun = false;
    core.reset();
    gate.open = false;  // hardware assumption: SET defaults to blocked
    net.inbox.clear();
    attackerIds.clear(), genuineIds.clear();
    bool wipeRtc = w.kind != CutKind::kWarmKeepRtc || sc.failMode == 1 || sc.failMode == 3;
    if (wipeRtc) for (auto& b : rtc.mem) b = uint8_t(w.rng.below(256));
    else if (w.rng.chance(sc.staleRtcPermille)) memcpy(rtc.mem, rtc.prev, sizeof rtc.mem), r.staleRtc++;
    if (w.rng.chance(sc.failPermille)) damage();
    w.cutAt = 0, w.armed = false, nextCut = 0;
    w.advance(w.rng.range(300, 1500));  // boot time; edges and messages are lost meanwhile
  }

  void boot() {
    for (;;) {
      try {
        core.reset(new sv::Core(cfg, hw));
        core->begin();
        begun = true;
        break;
      } catch (PowerCut&) {
        applyCut();
      }
    }
    if (ghostValid) {
      if (core->epoch() == ghostEpoch) {
        int32_t lost = (int32_t)(ghostTotal - core->total());
        if (lost < 0) fail(r.i5, "restore above the live count");
        else if (lost > 0) r.lostF4 += uint32_t(lost), r.f4Events++, r.f4InOp += ghostInOp;
        auto it = net.maxReported.find(core->epoch());
        if (it != net.maxReported.end() && (int32_t)(core->durableTotal() - it->second) < 0)
          fail(r.i5, "restored total below a reported total");
      } else {
        bumps.push_back({ghostEpoch, ghostTotal});
        r.epochBumps++;
      }
      ghostValid = false;
    }
    lastWid = 0;
    scheduleCut();
  }

  // I4: a window that opens must come from a genuine, issued open. A replayed copy
  // may open a window the Controller already closed only within the freshness bound
  // (the last `openRecent` reports, so openRecent * heartbeat, plus network slack).
  void checkWindow() {
    uint32_t wid = core->windowId();
    if (wid != lastWid) {
      bool viaAttacker = std::find(attackerIds.begin(), attackerIds.end(), wid) != attackerIds.end();
      bool viaGenuine = std::find(genuineIds.begin(), genuineIds.end(), wid) != genuineIds.end();
      bool stale = wid && ctl.issued.count(wid) &&
                   (int32_t)(w.now - ctl.deadlineOf[wid]) > (int32_t)(cfg.openRecent * cfg.heartbeatMs + 1000);
      if (wid && (!ctl.issued.count(wid) || (viaAttacker && !viaGenuine && !(ctl.live && ctl.curId == wid) && stale)))
        fail(r.i4, "forged or replayed open adopted");
      lastWid = wid;
    }
    attackerIds.clear(), genuineIds.clear();
  }

  void loopUntil(uint32_t end) {
    while ((int32_t)(end - w.now) > 0) {
      try {
        w.advance(1);
        core->poll();
        checkWindow();
      } catch (PowerCut&) {
        applyCut();
        boot();
      }
    }
  }

  Result go();
};

Result Run::go() {
  boot();
  uint32_t end = sc.startMs + sc.durationMs;
  loopUntil(end);

  // Drain: clean network, Controller up, no new coins, faults or cuts.
  active = false;
  w.cutAt = 0, w.armed = false, nextCut = 0;
  net.cfg = NetCfg{};
  net.cfg.maxDelay = 20;
  ctl.up = true, ctlUpAt = 0;
  loopUntil(w.now + 30000);

  r.bursts += core->bursts();
  for (auto& kv : ctl.credited) r.credited += kv.second;
  r.audited = ctl.audited, r.regressRows = ctl.regressRows;
  r.forgedRejected = ctl.forgedRejected, r.secondWindowRefused = ctl.secondWindowRefused;
  r.creditedFromAttacker = ctl.creditedFromAttacker;
  for (auto& b : bumps) {
    auto it = ctl.lastSeen.find(b.first);
    uint32_t seen = it == ctl.lastSeen.end() ? 0 : it->second;
    int32_t lost = (int32_t)(b.second - seen);
    if (lost > 0) r.lostBump += uint32_t(lost);
  }
  r.records = fl.programs;
  r.tornPrograms = fl.tornPrograms, r.tornErases = fl.tornErases, r.tornRtc = rtc.torn;
  for (uint32_t e : fl.erases) r.erases += e, r.maxSectorErases = std::max<uint64_t>(r.maxSectorErases, e);

  // I1: nothing is credited that was not counted, per window and overall.
  for (auto& kv : ctl.credited)
    if (kv.second > realByWin[kv.first] + r.spurious) fail(r.i1, "window credited more than counted");
  if (r.credited > r.realCounted + r.spurious) fail(r.i1, "total credited exceeds counted");
  // I2: every counted pulse is credited, audited, or listed as residual loss.
  r.unexplained = int64_t(r.realCounted + r.spurious) - int64_t(r.credited + r.audited + r.lostF4 + r.lostBump);
  if (r.unexplained != 0) fail(r.i2, "counted pulses unaccounted for");
  if (r.creditedFromAttacker) fail(r.i4, "attacker message credited");
  return r;
}

}  // namespace

Result runOne(const Scenario& sc, uint64_t seed, sv::Persist persist) {
  Run run(sc, seed, persist);
  return run.go();
}

}  // namespace sim
