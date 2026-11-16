# Controller pointers

- Start with the [Controller README](README.md).
- Before E2E: [CHR lab lock (one run at a time; parallel runs queue) and preflight](README.md#verification-and-chr-e2e-harness).
- E2E workflow: add your issue's own `--only <name>` scenario in [`e2e/run.py`](e2e/run.py) and iterate on it; run the full `make -C controller e2e` (~23 min) once before merge; `make -C controller e2e-armv7` only at milestones or for performance/memory changes. See [partial runs](README.md#verification-and-chr-e2e-harness).
- For evidence: [commit `*.summary.json`, leave full reports local](README.md#verification-and-chr-e2e-harness).
