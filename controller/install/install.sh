#!/bin/sh
# Installs the OpenPiso Controller as the `openpiso` systemd service on stock
# Armbian / Raspberry Pi OS Lite. Run as root from the directory holding this
# script and openpiso.service:
#
#   sudo sh install.sh --binary ./openpiso --portal-url http://10.0.0.2 \
#     --admin-network 192.168.1.0/24 [--listen 0.0.0.0:80] [--password-stdin] \
#     [--release-url URL]
#
# First install sets the admin password: read from stdin with --password-stdin,
# otherwise prompted on the terminal. It is handed to the binary's init-only
# mode through the environment, never argv, a file, or a log.
# Reinstalling over an existing ledger keeps the ledger and its password.
#
# Self-update: a daily timer checks the signed release (openpiso-update-check.*) and
# the admin page's Update button starts openpiso-update.service through
# openpiso-update.path; both run this binary as root with state in
# /var/lib/openpiso-update. --release-url replaces the GitHub Releases base URL
# (a lab or mirror); without it the binary's default is used.
#
# Host-wide setting: journald logs survive reboots, capped at 32M, with writes
# batched every minute to spare the SD card. This applies to all services,
# not only the Controller (/etc/systemd/journald.conf.d/openpiso-journal.conf).
set -eu
umask 077

usage() {
	echo "usage: sh install.sh --binary PATH --portal-url URL --admin-network CIDR [--listen ADDR:PORT] [--password-stdin] [--release-url URL]" >&2
	exit 2
}

fail() {
	echo "install.sh: $*" >&2
	exit 1
}

binary= portal_url= admin_network= listen=0.0.0.0:80 password_stdin= release_url=
while [ $# -gt 0 ]; do
	case $1 in
	--binary) [ $# -ge 2 ] || usage; binary=$2; shift 2 ;;
	--portal-url) [ $# -ge 2 ] || usage; portal_url=$2; shift 2 ;;
	--admin-network) [ $# -ge 2 ] || usage; admin_network=$2; shift 2 ;;
	--listen) [ $# -ge 2 ] || usage; listen=$2; shift 2 ;;
	--password-stdin) password_stdin=1; shift ;;
	--release-url) [ $# -ge 2 ] || usage; release_url=$2; shift 2 ;;
	*) usage ;;
	esac
done
[ -n "$binary" ] && [ -n "$portal_url" ] && [ -n "$admin_network" ] || usage

[ "$(id -u)" = 0 ] || fail "must run as root"
[ -d /run/systemd/system ] || fail "systemd is not running"
command -v runuser >/dev/null || fail "runuser (util-linux) is required"
[ -f "$binary" ] || fail "binary not found: $binary"
here=$(cd "$(dirname "$0")" && pwd)
units="openpiso.service openpiso-update.service openpiso-update.path openpiso-update-check.service openpiso-update-check.timer"
for unit in $units; do
	[ -f "$here/$unit" ] || fail "$unit not found next to install.sh"
done

# Values end up unquoted in the env file and as single ExecStart arguments,
# so only allow characters that need no quoting in either place.
nl='
'
for value in "$portal_url" "$admin_network" "$listen" "$release_url"; do
	case $value in *"$nl"*) fail "invalid value: $value" ;; esac
	if printf '%s' "$value" | grep -q '[^][A-Za-z0-9._:/%?=&+~@-]'; then
		fail "unsupported characters in: $value"
	fi
done
case $release_url in "" | http://?* | https://?*) ;; *) fail "--release-url must be an http(s) URL" ;; esac
port=${listen##*:}
case $port in "" | *[!0-9]*) fail "--listen must be ADDR:PORT" ;; esac
host=${listen%:*}
case $host in "" | 0.0.0.0) host=127.0.0.1 ;; "[::]") host="[::1]" ;; esac

data=/var/lib/openpiso
ledger=$data/openpiso.db

# Polls /healthz at $1 (host:port) until the installed service answers.
wait_healthy() {
	tries=0
	while [ $tries -lt 120 ]; do
		if command -v curl >/dev/null; then
			curl -gfsS -o /dev/null --max-time 2 "http://$1/healthz" 2>/dev/null && return 0
		else
			wget -q -O /dev/null -T 2 "http://$1/healthz" 2>/dev/null && return 0
		fi
		tries=$((tries + 1))
		sleep 1
	done
	return 1
}
command -v curl >/dev/null || command -v wget >/dev/null || fail "curl or wget is required"

# Collect the password before changing anything, and only on first install.
password=
if [ ! -e "$ledger" ]; then
	if [ -n "$password_stdin" ]; then
		IFS= read -r password || [ -n "$password" ] || fail "no password on stdin"
	elif [ -t 0 ]; then
		trap 'stty echo' EXIT INT TERM
		stty -echo
		printf 'Admin password: ' >&2
		IFS= read -r password || true
		printf '\nConfirm password: ' >&2
		IFS= read -r confirm || true
		stty echo
		trap - EXIT INT TERM
		echo >&2
		[ "$password" = "$confirm" ] || fail "passwords do not match"
		unset confirm
	else
		fail "no terminal for the password prompt; use --password-stdin"
	fi
	[ -n "$password" ] || fail "admin password must not be empty"
else
	echo "Existing ledger at $ledger: keeping it and its admin password."
fi

# No update may start while the install replaces the binary and units.
for unit in openpiso-update.path openpiso-update-check.timer openpiso; do
	if systemctl is-active --quiet "$unit"; then
		systemctl stop "$unit"
	fi
done

if ! id -u openpiso >/dev/null 2>&1; then
	useradd --system --user-group --home-dir "$data" --no-create-home --shell /usr/sbin/nologin openpiso
fi
install -d -o openpiso -g openpiso -m 0700 "$data"
chmod 0700 "$data"
install -o root -g root -m 0755 "$binary" /usr/local/bin/openpiso
# Root-owned update state the Controller can read but never write.
install -d -o root -g root -m 0755 /var/lib/openpiso-update
# A request left by an interrupted update would start one as soon as the path unit
# starts. Root only ever unlinks this file, never follows it.
rm -f "$data/update-request"

install -d -o root -g root -m 0700 /etc/openpiso
{
	echo "OPENPISO_LISTEN=$listen"
	echo "OPENPISO_PORTAL_URL=$portal_url"
	echo "OPENPISO_ADMIN_NETWORK=$admin_network"
	[ -z "$release_url" ] || echo "OPENPISO_RELEASE_URL=$release_url"
} >/etc/openpiso/openpiso.env.new
chmod 0600 /etc/openpiso/openpiso.env.new
mv /etc/openpiso/openpiso.env.new /etc/openpiso/openpiso.env

install -d -m 0755 /etc/systemd/journald.conf.d
install -d -m 2755 -g systemd-journal /var/log/journal
# Remove the previous OpenPiso drop-in so it cannot override the new policy.
rm -f /etc/systemd/journald.conf.d/openpiso-volatile.conf
cat >/etc/systemd/journald.conf.d/openpiso-journal.conf <<'EOF'
# Installed by OpenPiso: logs survive reboots, capped at 32M.
# Host-wide setting; batched writes spare the SD card.
[Journal]
Storage=persistent
SystemMaxUse=32M
SyncIntervalSec=1m
ForwardToSyslog=no
EOF
chmod 0644 /etc/systemd/journald.conf.d/openpiso-journal.conf
systemctl restart systemd-journald
journalctl --flush

# Initialize privately without opening a listener. Publish the fresh ledger
# only on success, so an interrupted install can safely be retried.
if [ -n "$password" ]; then
	initial_data=$(mktemp -d "$data/.initialize.XXXXXX")
	chown openpiso:openpiso "$initial_data"
	trap 'rm -rf "$initial_data"' EXIT INT TERM
	OPENPISO_ADMIN_PASSWORD=$password runuser -u openpiso -- \
		/usr/local/bin/openpiso -data "$initial_data" -init-only
	password=
	for file in "$initial_data"/openpiso.db*; do
		mv "$file" "$data/"
	done
	rmdir "$initial_data"
	trap - EXIT INT TERM
fi

for unit in $units; do
	install -o root -g root -m 0644 "$here/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --quiet openpiso
systemctl start openpiso
wait_healthy "$host:$port" || fail "openpiso service did not become healthy; see journalctl -u openpiso"
systemctl enable --quiet openpiso-update-check.timer openpiso-update.path
systemctl start openpiso-update-check.timer openpiso-update.path
echo "OpenPiso Controller is running on $listen (portal $portal_url)."
