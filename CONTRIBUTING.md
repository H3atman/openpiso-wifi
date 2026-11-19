# Contributing to OpenPiso WiFi

## Repo map

- **Controller** — [README](controller/README.md). Installed Controllers update from signed releases; see [Updates](controller/README.md#updates).
- **Vendo firmware** — [bench README](firmware/subvendo/bench/README.md) and [test plan](firmware/subvendo/TEST-PLAN.md).
- **Labs** — [CHR README](lab/chr/README.md), [phone README](lab/phones/README.md) and [dev README](lab/dev/README.md).
- **Docs** — [architecture](ARCHITECTURE.md), [glossary](GLOSSARY.md), [ADRs](docs/adr/) and [research](docs/research/).
- **Guide** — [docs/guide/](docs/guide/), the only source of the Website.
- **Brand** — [logo handover](brand/logo/kit/HANDOVER.md) and [guidelines](brand/logo/kit/GUIDELINES.md).

## Tasks

Run from the repo root; prerequisites and procedures live in the linked docs.

| Task | Command |
| --- | --- |
| Set up the pinned firmware venv and enable the shared git hook for every worktree | `make setup` |
| Run the offline CI checks: Go vet, native build, armv7 build and firmware host sim | `make check` |
| Run the firmware host sim only | `make sim` |
| Rebuild the firmware venv from [pinned requirements](firmware/subvendo/requirements.txt), including after deleting it | `make venv` |
| Run the E2E on a throwaway Docker lab of its own (needs Docker and `/dev/kvm`; see the [Controller README](controller/README.md#the-docker-lab-default)) | `make e2e` |
| Start the dev lab (shared CHR; read the dev README first) | `make dev` |

`setup`, `check`, `sim` and `venv` do not access the CHR lab or a physical board.
`e2e` creates and removes its own lab, so several can run at once. `dev` uses the
shared libvirt CHR lab; do not run it beside `make -C controller e2e LAB=libvirt`.
Physical-board procedures are not part of these targets; see the firmware bench README.

`make setup` sets the clone's shared `core.hooksPath` to `.githooks`, covering
all worktrees. The pre-commit hook checks staged files, not unstaged edits:
no `*.pyc`, `.env`, files over 500 KB (500000 bytes), or unformatted Go.
Controller changes also run `go vet` against the staged snapshot. Existing,
unchanged files are not checked. Put `go` and `gofmt` on `PATH`; `GO` (as in
`controller/Makefile`) and `GOFMT` can select explicit tool paths.

To build the Website locally into `/tmp/openpiso-site` (needs Docker only):

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/docs/guide":/src:ro -v /tmp/openpiso-site:/out -e JEKYLL_ENV=production -e PAGES_REPO_NWO=H3atman/openpiso-wifi --entrypoint sh ghcr.io/actions/jekyll-build-pages:v1.0.13 -c 'cd "$BUNDLE_APP_CONFIG" && bin/github-pages build --verbose --source /src --destination /out'
```

Then check it for broken internal links, anchors and missing images, as the Pages
workflow does (external URLs are not checked; exit code 2 means a broken link):

```sh
docker run --rm -v /tmp/openpiso-site:/check/openpiso-wifi:ro -w /check lycheeverse/lychee:0.24.2 --offline --include-fragments --no-progress --root-dir /check 'openpiso-wifi/**/*.html'
```

## Evidence policy

Use compressed PNG for screenshots and MP4 for demos, no GIF. The staged-file
limit of 500 KB applies inside `evidence/` too: compress evidence rather than
exempting it. Existing committed files are left alone unless staged again.
