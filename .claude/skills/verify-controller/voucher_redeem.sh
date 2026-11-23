#!/bin/sh
# Voucher round trip on a verify.py lab: the admin generates one Custom ₱0 Voucher of 600 s, the lab
# Device redeems it through the Portal API, then the ledger (/admin/state) and the Router (HotSpot user
# op-<Time Credit ID>) are read back. Run it through the helper:
#   python3 -I .claude/skills/verify-controller/verify.py exec <lab> -- sh .claude/skills/verify-controller/voucher_redeem.sh
# Writes $OPNW_OUT/voucher/*.json and prints one PASS/FAIL line. The lab is disposable, so nothing is undone:
# a redeemed Voucher cannot be deleted (the Controller refuses), and `down` removes the lab.
set -eu
out="$OPNW_OUT/voucher"
mkdir -p "$out"
seconds=600

# Generate: the reply 303s to /admin/vouchers?generated=<batch>.
redirect=$(curl -sS -o /dev/null -w '%{redirect_url}' -b "$OPNW_JAR" \
  --data "tier=custom&pesos=0&seconds=$seconds&count=1" "$OPNW_URL/admin/vouchers")
batch=$(printf '%s' "$redirect" | sed -n 's/.*[?&]generated=\([^&]*\).*/\1/p')
[ -n "$batch" ] || { echo "FAIL voucher: no batch in redirect '$redirect'"; exit 1; }
curl -sS -b "$OPNW_JAR" "$OPNW_URL/admin/vouchers/export?batch=$batch" > "$out/batch.csv"
code=$(sed -n '2p' "$out/batch.csv" | cut -d, -f1)

# Redeem as the Device, lower-cased as a customer might type it.
lower=$(printf '%s' "$code" | tr 'A-Z' 'a-z')
docker exec "$OPNW_DEVICE" curl -sS --data "code=$lower" "$OPNW_PORTAL/portal/api/redeem" > "$out/redeem.json"
bought=$(jq -r '.time_credit.bought // empty' "$out/redeem.json")

# Read back: the ledger's Time Credit for this Device, and its HotSpot user on the Router.
curl -sS -b "$OPNW_JAR" "$OPNW_URL/admin/state" > "$out/admin-state.json"
credit=$(jq -r --arg mac "$OPNW_DEVICE_MAC" --arg ip "$OPNW_DEVICE_IP" \
  '[.purchases.time_credits[] | select((.mac|ascii_upcase) == ($mac|ascii_upcase) and .ip == $ip)] | if length == 1 then .[0].id else empty end' \
  "$out/admin-state.json")
[ -n "$credit" ] || { echo "FAIL voucher: no single Time Credit for $OPNW_DEVICE_MAC $OPNW_DEVICE_IP"; exit 1; }
curl -sSk -u "admin:$OPNW_ROUTER_PASSWORD" "https://$OPNW_ROUTER/rest/ip/hotspot/user?name=op-$credit" > "$out/hotspot-user.json"
uptime=$(jq -r '.[0]["limit-uptime"] // empty' "$out/hotspot-user.json")
redeemed=$(curl -sS -b "$OPNW_JAR" "$OPNW_URL/admin/vouchers/export?batch=$batch" | sed -n '2p' | cut -d, -f4)

# RouterOS writes 600 s as 10m.
if [ "$bought" = "$seconds" ] && [ "$uptime" = "10m" ] && [ -n "$redeemed" ]; then
  echo "PASS voucher: code redeemed, bought=$bought, Time Credit $credit, Router limit-uptime=$uptime, redeemed_at=$redeemed"
else
  echo "FAIL voucher: bought='$bought' limit-uptime='$uptime' redeemed_at='$redeemed' (see $out)"
  exit 1
fi
