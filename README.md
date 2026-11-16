# OpenPiso WiFi

## Repo map

- **Controller** — [README](controller/README.md). Installed Controllers update from signed releases; see [Updates](controller/README.md#updates).
- **Vendo firmware** — [bench README](firmware/subvendo/bench/README.md) and [test plan](firmware/subvendo/TEST-PLAN.md).
- **Labs** — [CHR README](lab/chr/README.md), [phone README](lab/phones/README.md) and [dev README](lab/dev/README.md).
- **Docs** — [architecture](ARCHITECTURE.md), [glossary](GLOSSARY.md), [ADRs](docs/adr/) and [research](docs/research/).
- **Brand** — [logo handover](brand/logo/kit/HANDOVER.md) and [guidelines](brand/logo/kit/GUIDELINES.md).

## Tasks

Run from the repo root; prerequisites and procedures live in the linked docs.

| Task | Command |
| --- | --- |
| Set up the pinned firmware venv and enable the shared git hook for every worktree | `make setup` |
| Run the offline CI checks: Go vet, native build, armv7 build and firmware host sim | `make check` |
| Run the firmware host sim only | `make sim` |
| Rebuild the firmware venv from [pinned requirements](firmware/subvendo/requirements.txt), including after deleting it | `make venv` |
| Run the CHR E2E (exclusive shared-lab access; read the Controller README first) | `make e2e` |
| Start the dev lab (shared CHR; read the dev README first) | `make dev` |

`setup`, `check`, `sim` and `venv` do not access the CHR lab or a physical board.
`e2e` and `dev` are explicit lab operations; do not run them concurrently.
Physical-board procedures are not part of these targets; see the firmware bench README.

`make setup` sets the clone's shared `core.hooksPath` to `.githooks`, covering
all worktrees. The pre-commit hook checks staged files, not unstaged edits:
no `*.pyc`, `.env`, files over 500 KB (500000 bytes), or unformatted Go.
Controller changes also run `go vet` against the staged snapshot. Existing,
unchanged files are not checked. Put `go` and `gofmt` on `PATH`; `GO` (as in
`controller/Makefile`) and `GOFMT` can select explicit tool paths.

## Evidence policy

Use compressed PNG for screenshots and MP4 for demos, no GIF. The staged-file
limit of 500 KB applies inside `evidence/` too: compress evidence rather than
exempting it. Existing committed files are left alone unless staged again.

## License

OpenPiso WiFi is licensed under the GNU General Public License v3.0; see
[LICENSE](LICENSE).
