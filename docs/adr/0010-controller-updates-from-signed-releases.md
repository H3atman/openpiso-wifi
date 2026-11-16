# The Controller updates itself from signed releases through a root helper

Shops run the Controller unattended on cheap boards, and most Operators will never
SSH in to install a fix. The Controller should learn about new releases and let
the Operator install one from the admin page, without widening what the
unprivileged Controller process can do and without trusting the download host.

## Decision

Releases are published on GitHub Releases. Each carries `openpiso-linux-armv7`,
`openpiso-linux-arm64`, `openpiso-linux-amd64`, `release.json` (version, and the
SHA-256 and size of each binary) and `release.json.sig`, a base64 Ed25519
signature over the exact bytes of `release.json`. The public key is the tracked
file `controller/release.pub`, compiled into the binary at build time. The
private seed exists only as the `RELEASE_SIGNING_KEY` Actions secret; the
Release workflow signs the manifest and verifies it against the committed key
before publishing.

Updates are applied by a root helper, not by the Controller process. The helper
is the same binary: `openpiso update check` runs from a daily systemd timer, and
`openpiso update apply` runs from a path unit. It fetches
`<releases>/latest/download/release.json`, verifies the signature against the
compiled-in key, and refuses any version that is not strictly newer than the
running one. On apply it downloads the platform binary and checks its size and
hash, stops the service, backs up the ledger, swaps the binary, restarts, and
checks `/healthz`. On failure it restores the previous binary and the ledger
backup together, because ledger migrations are forward-only and an old binary
cannot read a migrated ledger.

The check is automatic; the apply is the Operator's choice. The admin page shows
the running version and, when the helper's status says a newer release exists,
an Update button. The button only drops a request file that the path unit
reacts to. The helper's state lives in root-owned
`/var/lib/openpiso-update/status.json`, which the Controller reads.

`OPENPISO_RELEASE_URL` (install.sh `--release-url`) points the helper at a
mirror or lab server; the signature check is unchanged. Operators opt out with
`systemctl disable --now openpiso-update-check.timer openpiso-update.path`.

## Consequences

- The Controller process still talks only to the Router. The root helper is the
  only component that reaches the internet, and only `github.com` (or the
  configured mirror).
- Trust rests on the signing key, not on GitHub or TLS. A compromised GitHub
  account or mirror can withhold releases but cannot install a binary.
- A downgrade or replay of an older signed release is refused, so a bad release
  is fixed by publishing a newer one, never by re-publishing an old one.
- Losing the signing key, or having to rotate a leaked one, means installed
  Controllers cannot trust a new key: every Shop must reinstall by hand with a
  binary built against the new `release.pub`.
- A failed update costs a short outage of new logins while the service restarts;
  active Devices stay online under Router enforcement, as in any Controller
  restart ([ADR 0001](0001-mikrotik-enforces-controller-off-path.md)).

## Alternatives considered

- Fully automatic apply: an unattended restart during business hours, or a bad
  release reaching every Shop at once, is worse than an Operator's click.
- A checksum over HTTPS only: it trusts whoever controls the GitHub account or
  the mirror, and a mirror could not be trusted at all.
- The Controller downloading its own update: it would need internet access and
  write access to its own binary, undoing its unprivileged sandbox.
- Distro packages (apt repository): they need a hosted repository and signing
  per distro, and Armbian and Raspberry Pi OS boards would still need the same
  rollback of the ledger.
