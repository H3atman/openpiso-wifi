#include "report.h"

#include <cstdio>
#include <cstring>

namespace sim {

namespace {

constexpr double kCoinsPerDay = 300;      // stated wear-projection rate
constexpr double kSectorCycles = 100000;  // W25Q32JV endurance per sector (unconfirmed, bench Q7)

void add(Result& a, const Result& b) {
  a.coins += b.coins, a.blockedCoins += b.blockedCoins, a.acceptedPulses += b.acceptedPulses;
  a.realCounted += b.realCounted, a.realDropped += b.realDropped, a.lostDown += b.lostDown;
  a.spurious += b.spurious, a.bounceLeaks += b.bounceLeaks, a.lostF4 += b.lostF4;
  a.f4Events += b.f4Events, a.f4InOp += b.f4InOp, a.lostBump += b.lostBump;
  a.epochBumps += b.epochBumps, a.cuts += b.cuts, a.credited += b.credited, a.audited += b.audited;
  a.regressRows += b.regressRows, a.forgedRejected += b.forgedRejected;
  a.secondWindowRefused += b.secondWindowRefused, a.records += b.records, a.erases += b.erases;
  a.bursts += b.bursts, a.unexplained += b.unexplained;
  a.tornPrograms += b.tornPrograms, a.tornErases += b.tornErases, a.tornRtc += b.tornRtc;
  a.staleRtc += b.staleRtc, a.damaged += b.damaged, a.attacks += b.attacks;
  if (b.maxSectorErases > a.maxSectorErases) a.maxSectorErases = b.maxSectorErases;
}

std::string n(uint64_t v) { return std::to_string(v); }

std::string fx(double v, int digits) {
  char b[32];
  snprintf(b, sizeof b, "%.*f", digits, v);
  return b;
}

std::string ratio(double a, double b, int digits) { return b ? fx(a / b, digits) : "-"; }

std::string row(std::initializer_list<std::string> cells) {
  std::string r = "|";
  for (const std::string& c : cells) r += " " + c + " |";
  return r + "\n";
}

std::string header(std::initializer_list<const char*> cols) {
  std::string h = "|", s = "|";
  for (const char* c : cols) h += std::string(" ") + c + " |", s += "---|";
  return h + "\n" + s + "\n";
}

std::string jnum(const std::string& s) { return s == "-" ? "null" : s; }

std::string passFail(uint64_t fail, uint64_t runs) { return fail ? "FAIL " + n(fail) + "/" + n(runs) : "PASS"; }

// Years until the worst sector reaches its erase limit at 300 coins/day.
// `scale` rescales erases per coin (used for the 5-pulses-per-coin line).
std::string years(const Agg& a, double scale) {
  if (!a.worstSum || !a.sum.coins) return "-";
  double perCoin = scale * double(a.worstSum) / double(a.sum.coins);
  return fx(kSectorCycles / (perCoin * kCoinsPerDay) / 365.0, 0);
}

const Agg* wearAgg(const PolicyRun& p) {
  for (const Agg& a : p.aggs)
    if (!strcmp(a.sc->name, "wear") && a.sum.coins) return &a;
  return nullptr;
}

// Wear years if every coin were 5 pulses. Per-burst saves once per coin whatever its
// pulses; per-pulse saves scale with the pulses per coin the run actually used.
std::string years5(const PolicyRun& p) {
  const Agg* a = wearAgg(p);
  if (!a) return "-";
  double ppc = double(a->sum.acceptedPulses) / double(a->sum.coins);
  return years(*a, p.persist == sv::Persist::kPulse ? 5.0 / ppc : 1.0);
}

// Totals over the scenarios that cut power or reset the board.
Result cutTotals(const PolicyRun& p, uint64_t& runs) {
  Result t;
  runs = 0;
  for (const Agg& a : p.aggs)
    if (a.sc->cutEveryMs) add(t, a.sum), runs += a.runs;
  return t;
}

std::string hex(const uint8_t* p, size_t len) {
  std::string s;
  char b[3];
  for (size_t i = 0; i < len; i++) snprintf(b, sizeof b, "%02x", p[i]), s += b;
  return s;
}

// Fixed sample messages so a Go Controller can check its byte layout and MAC.
std::string vectors() {
  OsslHmac h;
  uint8_t key[sv::kKeyLen], m[32], mac[sv::kMacLen];
  for (uint32_t i = 0; i < sv::kKeyLen; i++) key[i] = uint8_t(i * 7 + 3);
  auto one = [&](const char* title, size_t len) {
    h.mac(key, m, len, mac);
    return std::string(title) + "\n  msg = " + hex(m, len) + "\n  mac = " + hex(mac, 32) + "\n";
  };
  std::string s = "key = " + hex(key, sizeof key) + "\n";
  sv::Report r = {1, 2, 300, 7, 0xA1B2C3D4u, 9, {}};
  s += one("report{vendo=1 epoch=2 total=300 window=7 nonce=0xa1b2c3d4 rseq=9}", sv::reportMsg(r, m));
  sv::Open o = {1, 7, 45000, 0xA1B2C3D4u, 9, {}};
  s += one("open{vendo=1 window=7 expires_ms=45000 nonce=0xa1b2c3d4 rseq=9}", sv::openMsg(o, m));
  sv::Ack a = {1, 2, 300, 0xA1B2C3D4u, 9, {}};
  s += one("ack{vendo=1 epoch=2 total=300 nonce=0xa1b2c3d4 rseq=9}", sv::ackMsg(a, m));
  return s;
}

// Invariant and fault-coverage tables of one policy.
std::string invariantTables(const PolicyRun& p) {
  std::string md =
      "Counts are pulses summed over all seeds. `counted` = real pulses the board counted plus spurious edges. "
      "I2 requires counted = credited + audit + lost (F4) + lost (restore-failed).\n\n" +
      header({"scenario", "runs", "coins taken", "coins refused (blocked)", "counted", "credited", "audit", "lost F4", "lost down",
              "lost restore-failed", "spurious", "unexplained", "I1", "I2", "I3", "I4", "I5"});
  for (const Agg& a : p.aggs) {
    const Result& s = a.sum;
    md += row({a.sc->name, n(a.runs), n(s.coins), n(s.blockedCoins), n(s.realCounted + s.spurious), n(s.credited), n(s.audited),
               n(s.lostF4), n(s.lostDown), n(s.lostBump), n(s.spurious), std::to_string((long long)s.unexplained),
               passFail(a.failRuns[0], a.runs), passFail(a.failRuns[1], a.runs), passFail(a.failRuns[2], a.runs),
               passFail(a.failRuns[3], a.runs), passFail(a.failRuns[4], a.runs)});
  }
  md += "\nFault coverage (faults that actually landed, summed over seeds):\n\n" +
        header({"scenario", "cuts", "torn flash programs", "torn sector erases", "torn RTC writes", "stale RTC applied",
                "storage damaged", "attacker msgs", "Controller rejected bad MAC", "second window refused", "regress reports"});
  for (const Agg& a : p.aggs) {
    const Result& s = a.sum;
    md += row({a.sc->name, n(s.cuts), n(s.tornPrograms), n(s.tornErases), n(s.tornRtc), n(s.staleRtc), n(s.damaged), n(s.attacks),
               n(s.forgedRejected), n(s.secondWindowRefused), n(s.regressRows)});
  }
  return md;
}

}  // namespace

void Agg::take(const Result& r, uint64_t seed) {
  runs++;
  const uint64_t f[5] = {r.i1, r.i2, r.i3, r.i4, r.i5};
  bool bad = false;
  for (int i = 0; i < 5; i++)
    if (f[i]) failRuns[i]++, bad = true;
  if (bad) {
    failedRuns++;
    if (firstFail.empty()) firstFailSeed = seed, firstFail = r.detail;
  }
  worstSum += r.maxSectorErases;
  add(sum, r);
}

std::string markdown(const std::vector<PolicyRun>& policies, Seeds seeds) {
  sv::Config def;
  std::string md = "# Subvendo simulation report\n\n";
  md += "Seeds: " + n(seeds.first) + ".." + n(seeds.first + seeds.count - 1) + " per scenario (" + n(seeds.count) +
        " seeds; the `wear` scenario uses one). Output is a pure function of the seeds. Every scenario runs under both checkpoint policies.\n\n";
  md += "Core config: debounce " + n(def.debounceMs) + " ms, quiet time " + n(def.quietMs) + " ms, window gap " + n(def.windowGapMs) +
        " ms, heartbeat " + n(def.heartbeatMs) + " ms, resend " + n(def.resendMs) + " ms, reachable if acked within " + n(def.reachMs) +
        " ms, open must echo one of the last " + n(def.openRecent) + " reports, ring " + n(def.ringSectors) + " sectors.\n\n";

  md += "## Checkpoint policy comparison\n\n"
        "`per-burst` (default) saves once when a burst ends; `per-pulse` saves after every counted pulse, in the main loop. "
        "Years = worst sector reaching " + n((uint64_t)kSectorCycles) + " erase cycles (W25Q32JV figure, bench Q7) at " +
        n((uint64_t)kCoinsPerDay) + " coins/day with a " + n(def.ringSectors) +
        "-sector ring. Short runs erase 0 or 1 sectors, so only the `wear` row is a usable projection (`-` = no erase happened).\n\n" +
        header({"scenario", "policy", "lost F4 pulses", "lost per losing cut", "records per coin", "erases per burst",
                "worst sector erases", "years @300 coins/day"});
  for (size_t i = 0; i < policies[0].aggs.size(); i++)
    for (const PolicyRun& p : policies) {
      const Agg& a = p.aggs[i];
      const Result& s = a.sum;
      md += row({a.sc->name, p.name, n(s.lostF4), ratio(double(s.lostF4), double(s.f4Events), 2),
                 ratio(double(s.records), double(s.coins), 2), ratio(double(s.erases), double(s.bursts), 4), n(s.maxSectorErases),
                 years(a, 1.0)});
    }
  md += "\nWear in the `wear` scenario if every coin were 5 pulses (default coins are 1, 1, 5, 5, 10 pulses):";
  for (const PolicyRun& p : policies) md += std::string(" ") + p.name + " " + years5(p) + " years;";
  md += "\n\n";

  md += "### Residual F4 loss (coin counted, power cut before its checkpoint)\n\n"
        "Summed over the scenarios that cut power or reset. Loss is bounded by the time from a pulse to the end of its flash record. "
        "It is always reported, never hidden.\n\n" +
        header({"policy", "runs with cuts", "cuts", "cuts that lost pulses", "pulses lost", "per losing cut", "losing cuts inside a flash/RTC op",
                "restore-failed pulses", "epoch bumps", "pulses of an accepted coin arriving while down"});
  for (const PolicyRun& p : policies) {
    uint64_t runs;
    Result t = cutTotals(p, runs);
    md += row({p.name, n(runs), n(t.cuts), n(t.f4Events), n(t.lostF4), ratio(double(t.lostF4), double(t.f4Events), 2), n(t.f4InOp),
               n(t.lostBump), n(t.epochBumps), n(t.lostDown)});
  }

  for (const PolicyRun& p : policies) {
    md += std::string("\n## Invariants: ") + p.name + (p.persist == sv::Persist::kBurst ? " (default)" : "") + "\n\n" + invariantTables(p);
  }

  md += "\n## Scenario descriptions\n\n";
  for (const Agg& a : policies[0].aggs) md += std::string("- `") + a.sc->name + "`: " + a.sc->desc + "\n";

  md += "\n## Failures\n\n";
  bool any = false;
  for (const PolicyRun& p : policies)
    for (const Agg& a : p.aggs)
      if (!a.firstFail.empty()) {
        md += std::string("- ") + p.name + " `" + a.sc->name + "`: first failing seed " + n(a.firstFailSeed) + ": " + a.firstFail +
              ". Reproduce: `./out/sim --policy " + p.name + " --only " + a.sc->name + " --seed0 " + n(a.firstFailSeed) + " --seeds 1`\n";
        any = true;
      }
  if (!any) md += "none\n";

  md += "\n## Canonical message vectors (for the Go Controller)\n\n```\n" + vectors() + "```\n";
  return md;
}

std::string json(const std::vector<PolicyRun>& policies, Seeds seeds) {
  uint64_t failed = 0;
  std::string js = "{\n  \"seeds\": {\"first\": " + n(seeds.first) + ", \"count\": " + n(seeds.count) + "},\n  \"policies\": [\n";
  for (size_t k = 0; k < policies.size(); k++) {
    const PolicyRun& p = policies[k];
    js += std::string("    {\"name\":\"") + p.name + "\",\"default\":" + (p.persist == sv::Persist::kBurst ? "true" : "false") +
          ",\"scenarios\":[\n";
    for (size_t i = 0; i < p.aggs.size(); i++) {
      const Agg& a = p.aggs[i];
      const Result& s = a.sum;
      failed += a.failedRuns;
      js += std::string("      {\"name\":\"") + a.sc->name + "\",\"runs\":" + n(a.runs) + ",\"coins\":" + n(s.coins) +
            ",\"blocked_coins\":" + n(s.blockedCoins) + ",\"counted\":" + n(s.realCounted + s.spurious) + ",\"credited\":" + n(s.credited) +
            ",\"audited\":" + n(s.audited) + ",\"lost_f4\":" + n(s.lostF4) + ",\"losing_cuts\":" + n(s.f4Events) +
            ",\"lost_down\":" + n(s.lostDown) + ",\"lost_restore_failed\":" + n(s.lostBump) + ",\"spurious\":" + n(s.spurious) +
            ",\"unexplained\":" + std::to_string((long long)s.unexplained) + ",\"cuts\":" + n(s.cuts) + ",\"records\":" + n(s.records) +
            ",\"bursts\":" + n(s.bursts) + ",\"erases\":" + n(s.erases) + ",\"worst_sector_erases\":" + n(s.maxSectorErases) +
            ",\"worst_sector_erases_sum\":" + n(a.worstSum) + ",\"years_at_300_coins_per_day\":" + jnum(years(a, 1.0)) +
            ",\"fail_runs\":{\"I1\":" + n(a.failRuns[0]) + ",\"I2\":" + n(a.failRuns[1]) + ",\"I3\":" + n(a.failRuns[2]) +
            ",\"I4\":" + n(a.failRuns[3]) + ",\"I5\":" + n(a.failRuns[4]) + "}}" + (i + 1 < p.aggs.size() ? "," : "") + "\n";
    }
    uint64_t runs;
    Result t = cutTotals(p, runs);
    js += "    ],\"residual_f4\":{\"cuts\":" + n(t.cuts) + ",\"losing_cuts\":" + n(t.f4Events) + ",\"pulses_lost\":" + n(t.lostF4) +
          ",\"cuts_inside_flash_op\":" + n(t.f4InOp) + ",\"restore_failed_pulses\":" + n(t.lostBump) + ",\"epoch_bumps\":" + n(t.epochBumps) +
          "},\"years_at_5_pulses_per_coin\":" + jnum(years5(p)) + "}" + (k + 1 < policies.size() ? "," : "") + "\n";
  }
  return js + "  ],\n  \"failed_runs\": " + n(failed) + "\n}\n";
}

}  // namespace sim
