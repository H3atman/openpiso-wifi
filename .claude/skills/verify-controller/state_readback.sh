#!/bin/sh
# Read-only readback on a verify.py lab: the admin's /admin/state and the Device's /portal/api/state.
# Run it through the helper:
#   python3 -I .claude/skills/verify-controller/verify.py exec <lab> -- sh .claude/skills/verify-controller/state_readback.sh
# Writes $OPNW_OUT/state/{admin,portal}-state.json and prints one PASS/FAIL line. Changes nothing.
set -eu
out="$OPNW_OUT/state"
mkdir -p "$out"
curl -sS -b "$OPNW_JAR" "$OPNW_URL/admin/state" > "$out/admin-state.json"
docker exec "$OPNW_DEVICE" curl -sS "$OPNW_PORTAL/portal/api/state" > "$out/portal-state.json"
router=$(jq -r '.router.status' "$out/admin-state.json")
adopted=$(jq -r '.router.adopted_hotspot_servers | join(",")' "$out/admin-state.json")
rates=$(jq -r '[.rates[] | "\(.pesos)=\(.seconds)"] | join(",")' "$out/portal-state.json")
if [ "$router" = ready ] && [ "$adopted" = hs1 ] && [ "$rates" = "1=60,5=360" ]; then
  echo "PASS state: Router $router, Adopted $adopted, Portal rates $rates"
else
  echo "FAIL state: Router '$router', Adopted '$adopted', Portal rates '$rates' (see $out)"
  exit 1
fi
