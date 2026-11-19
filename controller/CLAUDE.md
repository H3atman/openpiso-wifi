# Controller pointers

- Start with the [Controller README](README.md).
- E2E runs on [a throwaway Docker lab per run](README.md#the-docker-lab-default) (Docker and `/dev/kvm`, no sudo); runs in different worktrees can go at once. `--lab libvirt` / `LAB=libvirt` selects the shared libvirt lab and its lock until #39.
- E2E workflow: add your issue's own `--only <name>` scenario in [`e2e/run.py`](e2e/run.py) and iterate on it; run the full `make -C controller e2e` (~22 min) once before merge; `make -C controller e2e-armv7` only at milestones or for performance/memory changes. See [partial runs](README.md#verification-and-chr-e2e-harness).
- For evidence: [commit `*.summary.json`, leave full reports local](README.md#verification-and-chr-e2e-harness).
