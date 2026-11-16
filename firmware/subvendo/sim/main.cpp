// Runs every scenario over N seeds under both checkpoint policies, prints a
// summary, and writes out/report.md and out/report.json. Exit code is 1 if any
// invariant failed. Output depends only on the seeds, so identical runs are
// byte-identical.
//   ./out/sim [--seeds N] [--seed0 S] [--only SCENARIO] [--policy per-burst|per-pulse] [--out DIR]
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "report.h"

namespace sim { bool provisioningScenarios(); }

using namespace sim;

int main(int argc, char** argv) {
  Seeds seeds = {1, 500};
  const char* only = nullptr;
  const char* policy = nullptr;
  std::string outDir = "out";
  for (int i = 1; i + 1 < argc; i += 2) {
    if (!strcmp(argv[i], "--seeds")) seeds.count = strtoull(argv[i + 1], nullptr, 10);
    else if (!strcmp(argv[i], "--seed0")) seeds.first = strtoull(argv[i + 1], nullptr, 10);
    else if (!strcmp(argv[i], "--only")) only = argv[i + 1];
    else if (!strcmp(argv[i], "--policy")) policy = argv[i + 1];
    else if (!strcmp(argv[i], "--out")) outDir = argv[i + 1];
  }

  std::vector<PolicyRun> policies = {{"per-burst", sv::Persist::kBurst, {}}, {"per-pulse", sv::Persist::kPulse, {}}};
  uint64_t failed = provisioningScenarios() ? 0 : 1;
  for (PolicyRun& p : policies) {
    if (policy && strcmp(policy, p.name)) continue;
    for (const Scenario& sc : scenarios()) {
      if (only && strcmp(only, sc.name)) continue;
      Agg a;
      a.sc = &sc;
      uint64_t n = sc.maxSeeds && sc.maxSeeds < seeds.count ? sc.maxSeeds : seeds.count;
      for (uint64_t k = 0; k < n; k++) a.take(runOne(sc, seeds.first + k, p.persist), seeds.first + k);
      printf("%-9s %-18s runs=%-4llu I1 %s I2 %s I3 %s I4 %s I5 %s\n", p.name, sc.name, (unsigned long long)a.runs,
             a.failRuns[0] ? "FAIL" : "ok", a.failRuns[1] ? "FAIL" : "ok", a.failRuns[2] ? "FAIL" : "ok",
             a.failRuns[3] ? "FAIL" : "ok", a.failRuns[4] ? "FAIL" : "ok");
      fflush(stdout);
      failed += a.failedRuns;
      p.aggs.push_back(a);
    }
  }
  // Drop the policies that were filtered out.
  for (size_t i = policies.size(); i-- > 0;)
    if (policies[i].aggs.empty()) policies.erase(policies.begin() + long(i));
  if (policies.empty()) {
    fprintf(stderr, "no scenario matched\n");
    return 2;
  }

  FILE* f = fopen((outDir + "/report.md").c_str(), "w");
  FILE* g = fopen((outDir + "/report.json").c_str(), "w");
  if (!f || !g) {
    fprintf(stderr, "cannot write reports to %s\n", outDir.c_str());
    return 2;
  }
  fputs(markdown(policies, seeds).c_str(), f);
  fputs(json(policies, seeds).c_str(), g);
  fclose(f);
  fclose(g);
  printf("%s: %llu failing runs\n", failed ? "FAIL" : "PASS", (unsigned long long)failed);
  return failed ? 1 : 0;
}
