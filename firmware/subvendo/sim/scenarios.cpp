#include "run.h"

namespace sim {

namespace {

// Builds a scenario from the defaults plus a few overrides.
template <class F>
Scenario make(const char* name, const char* desc, F tweak) {
  Scenario s;
  s.name = name;
  s.desc = desc;
  tweak(s);
  return s;
}

}  // namespace

const std::vector<Scenario>& scenarios() {
  static const std::vector<Scenario> all = {
      make("baseline", "clean network, no faults", [](Scenario&) {}),
      make("lossy", "25% loss each way, duplicates, delay up to 400 ms (F1, F2)", [](Scenario& s) {
        s.net = {250, 250, 100, 5, 400};
      }),
      make("ack_loss", "50% of Controller->board messages lost (F2, T26)", [](Scenario& s) {
        s.net.lossToBoard = 500;
      }),
      make("reorder", "delays up to 1.5 s with duplicates: older totals arrive late (T17)", [](Scenario& s) {
        s.net = {0, 0, 200, 5, 1500};
      }),
      make("dup", "50% of messages duplicated (T19)", [](Scenario& s) { s.net.dup = 500; }),
      make("ctl_down", "Controller down 5-20 s at intervals (F1, T29)", [](Scenario& s) {
        s.ctlDownEveryMs = 25000, s.ctlDownMinMs = 5000, s.ctlDownMaxMs = 20000;
      }),
      make("power_cut", "power loss every ~9 s, half inside a flash/RTC op (F3, F4, T12-T14, T28)", [](Scenario& s) {
        s.cutEveryMs = 9000, s.armedPermille = 500, s.cutKind = CutKind::kPower;
      }),
      make("warm_rtc_kept", "warm reset, RTC survives, 30% stale RTC (F3, T14, T15; bench Q4)", [](Scenario& s) {
        s.cutEveryMs = 9000, s.armedPermille = 500, s.staleRtcPermille = 300, s.cutKind = CutKind::kWarmKeepRtc;
      }),
      make("warm_rtc_lost", "warm reset, RTC lost (F3; bench Q4 negative case)", [](Scenario& s) {
        s.cutEveryMs = 9000, s.armedPermille = 500, s.cutKind = CutKind::kWarmLoseRtc;
      }),
      make("erase_cut", "ring nearly full so sector erases are frequent, cuts aimed at flash ops (T13)", [](Scenario& s) {
        s.prefillRecords = 124, s.cutEveryMs = 4000, s.armedPermille = 800, s.cutKind = CutKind::kPower;
      }),
      make("restore_fail", "cuts that also give every record a bad CRC: epoch bump (T22)", [](Scenario& s) {
        s.cutEveryMs = 15000, s.failPermille = 600, s.failMode = 1, s.cutKind = CutKind::kPower;
      }),
      make("restore_rtc_only", "newest flash record destroyed, RTC kept: restore from RTC (T16)", [](Scenario& s) {
        s.cutEveryMs = 12000, s.failPermille = 700, s.failMode = 2, s.cutKind = CutKind::kWarmKeepRtc;
      }),
      make("restore_fail_wipe", "flash and RTC wiped: nothing survives, the epoch is random (T22, F-A)", [](Scenario& s) {
        s.cutEveryMs = 30000, s.failPermille = 1000, s.failMode = 3, s.cutKind = CutKind::kPower;
      }),
      make("expiry_inflight", "coins land just before the window ends (F7, T18, T25)", [](Scenario& s) {
        s.lateCoins = true, s.coinEveryMs = 9000, s.winMinMs = 8000, s.winMaxMs = 12000;
      }),
      make("handover_inflight", "back-to-back windows, slow network, 10-pulse coin in flight at the handover (T30)", [](Scenario& s) {
        s.lateCoins = true, s.coinEveryMs = 9000, s.winMinMs = 6000, s.winMaxMs = 9000;
        s.gapMinMs = 0, s.gapMaxMs = 200, s.net = {0, 0, 0, 5, 1500};
      }),
      make("glitch", "isolated glitch edges and contact bounce (F9)", [](Scenario& s) {
        s.glitchEveryMs = 4000, s.bouncePermille = 300;
      }),
      make("forged_replay", "forged and replayed messages, with reboots in between (F8, T20, T21)", [](Scenario& s) {
        s.attackEveryMs = 300, s.cutEveryMs = 15000, s.armedPermille = 300, s.cutKind = CutKind::kPower;
        s.net.dup = 100;
      }),
      make("wrap", "millis() and total wrap during the run (F10, T23)", [](Scenario& s) {
        s.startTotal = 0xFFFFFFF0u, s.startMs = 0xFFFFD000u;
        s.net = {100, 100, 100, 5, 100};
        s.cutEveryMs = 20000, s.cutKind = CutKind::kPower;
      }),
      make("stale_open", "short windows, 80% of Controller->board lost: whole windows are never received, then replayed late (T31)", [](Scenario& s) {
        s.net.lossToBoard = 800, s.winMinMs = 2000, s.winMaxMs = 3000, s.attackEveryMs = 300, s.replayOpensOnly = true;
      }),
      make("chaos","loss, reorder, outages, cuts, glitches and attacks together", [](Scenario& s) {
        s.net = {150, 150, 150, 5, 800};
        s.ctlDownEveryMs = 30000, s.ctlDownMinMs = 3000, s.ctlDownMaxMs = 15000;
        s.cutEveryMs = 12000, s.armedPermille = 400, s.staleRtcPermille = 200, s.cutKind = CutKind::kWarmKeepRtc;
        s.glitchEveryMs = 6000, s.bouncePermille = 200, s.attackEveryMs = 700, s.lateCoins = true;
      }),
      make("wear", "~6000 coins, ~4 per Coin Window, clean network: flash erases per coin (I6)", [](Scenario& s) {
        s.durationMs = 36000000, s.coinEveryMs = 6000, s.winMinMs = 20000, s.winMaxMs = 30000;
        s.maxSeeds = 1;
      }),
  };
  return all;
}

}  // namespace sim
