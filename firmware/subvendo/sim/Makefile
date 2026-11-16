# Host simulation of the subvendo core. Needs g++ (C++17) and OpenSSL headers.
#   make          build out/sim
#   make run      build and run all scenarios, writes out/report.{md,json}
#   make asan     same run under -fsanitize=address,undefined (fewer seeds)
CXX      ?= g++
CORE     := ../lib/subvendo_core
SRCS     := main.cpp report.cpp run.cpp scenarios.cpp provisioning.cpp $(CORE)/provisioning.cpp $(CORE)/core.cpp $(CORE)/ring.cpp $(CORE)/wire.cpp
FLAGS    := -std=c++17 -Wall -Wextra -Werror -I$(CORE) -I.
SEEDS    ?= 500

out/sim: $(SRCS) $(wildcard *.h) $(wildcard $(CORE)/*.h)
	@mkdir -p out
	$(CXX) $(FLAGS) -O2 $(SRCS) -lcrypto -o $@

out/sim_asan: $(SRCS) $(wildcard *.h) $(wildcard $(CORE)/*.h)
	@mkdir -p out
	$(CXX) $(FLAGS) -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=undefined $(SRCS) -lcrypto -o $@

run: out/sim
	./out/sim --seeds $(SEEDS)

asan: out/sim_asan
	mkdir -p out/asan && ./out/sim_asan --seeds 8 --out out/asan

# The core alone must build with the board's restrictions.
core-check:
	for f in core ring wire provisioning; do $(CXX) -std=c++17 -Wall -Wextra -Werror -fno-exceptions -fno-rtti -c $(CORE)/$$f.cpp -o /dev/null || exit 1; done

clean:
	rm -rf out/sim out/sim_asan out/asan

.PHONY: run asan core-check clean
