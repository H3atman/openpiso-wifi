// One scenario x one seed: build the world, drive it, check the invariants.
#pragma once
#include <string>
#include <vector>

#include "core.h"
#include "fakes.h"

namespace sim {

// Parameters of a scenario. Zero means "off" unless noted.
struct Scenario {
  const char* name;
  const char* desc;
  uint32_t durationMs = 120000;
  NetCfg net;
  uint32_t ctlDownEveryMs = 0, ctlDownMinMs = 0, ctlDownMaxMs = 0;  // Controller outages
  uint32_t cutEveryMs = 0;                                          // mean time between cuts
  CutKind cutKind = CutKind::kPower;
  uint32_t armedPermille = 0;     // share of cuts that land inside the next flash/RTC op
  uint32_t staleRtcPermille = 0;  // warm reset: RTC write before the reset is dropped
  uint32_t failPermille = 0;      // share of cuts that also damage storage
  int failMode = 0;               // 1 bad CRCs + RTC wiped, 2 newest flash record destroyed, 3 flash+RTC wiped
  uint32_t glitchEveryMs = 0;     // isolated glitch edges (mean spacing)
  uint32_t bouncePermille = 0;    // real edge followed by a 1-2 ms bounce
  uint32_t attackEveryMs = 0;     // forged/replayed message injection (mean spacing)
  bool replayOpensOnly = false;   // attacks are only replays of captured opens
  uint32_t coinEveryMs = 3000;    // mean spacing of coin attempts
  uint32_t winMinMs = 15000, winMaxMs = 40000;
  uint32_t gapMinMs = 500, gapMaxMs = 3000;  // Controller idle time between windows
  uint32_t startTotal = 0, startMs = 1000;
  uint32_t prefillRecords = 0;    // ring records written before boot, to make erases frequent
  bool lateCoins = false;         // coins are timed to land just before a window ends
  uint32_t maxSeeds = 0;          // 0 = run every seed; otherwise cap (long scenarios)
};

struct Result {
  // Ground truth (pulses).
  uint64_t coins = 0, blockedCoins = 0, acceptedPulses = 0;
  uint64_t realCounted = 0, realDropped = 0, lostDown = 0, spurious = 0, bounceLeaks = 0;
  uint64_t lostF4 = 0, f4Events = 0, f4InOp = 0, lostBump = 0, epochBumps = 0, cuts = 0;
  // Controller view.
  uint64_t credited = 0, audited = 0, regressRows = 0, forgedRejected = 0, secondWindowRefused = 0;
  uint64_t creditedFromAttacker = 0;
  // Fault coverage: proof the faults actually landed.
  uint64_t tornPrograms = 0, tornErases = 0, tornRtc = 0, staleRtc = 0, damaged = 0, attacks = 0;
  // Flash.
  uint64_t records = 0, erases = 0, maxSectorErases = 0, bursts = 0;
  // Invariants: 0 = pass; text of the first failure.
  int64_t unexplained = 0;
  uint64_t i1 = 0, i2 = 0, i3 = 0, i4 = 0, i5 = 0;
  std::string detail;
};

Result runOne(const Scenario& sc, uint64_t seed, sv::Persist persist);
const std::vector<Scenario>& scenarios();

}  // namespace sim
