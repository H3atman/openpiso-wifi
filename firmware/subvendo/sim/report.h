// Aggregation over seeds and the md/json reports.
#pragma once
#include <string>
#include <vector>

#include "run.h"

namespace sim {

// Results of one scenario over all seeds, under one checkpoint policy.
struct Agg {
  const Scenario* sc = nullptr;
  uint64_t runs = 0, failedRuns = 0, failRuns[5] = {0, 0, 0, 0, 0};
  uint64_t worstSum = 0;  // sum over runs of the worst sector's erase count
  Result sum;             // counters summed over runs (maxSectorErases is the max)
  uint64_t firstFailSeed = 0;
  std::string firstFail;

  void take(const Result& r, uint64_t seed);
};

// Every scenario under one policy.
struct PolicyRun {
  const char* name;
  sv::Persist persist;
  std::vector<Agg> aggs;
};

struct Seeds {
  uint64_t first, count;
};

std::string markdown(const std::vector<PolicyRun>& policies, Seeds seeds);
std::string json(const std::vector<PolicyRun>& policies, Seeds seeds);

}  // namespace sim
