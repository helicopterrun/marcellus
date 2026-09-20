#!/bin/sh
# Restart marcellus after N consecutive /healthz failures.
#
# Run by marcellus-healthcheck.timer every 60s. A single 503 is not a
# restart trigger: /healthz also reports MQTT reconnects and late scrub
# cycles, which clear on their own; restarting on the first one would turn a
# broker blip into a restart loop that drops push state. Three in a row
# (~3 min) is a wedge.
#
# The URL is derived from the sidecar config's bind_host/bind_port -- prod
# binds a LAN address, not loopback, so a hard-coded 127.0.0.1 would never
# connect and would restart the service forever.
set -u

CONFIG="${MARCELLUS_CONFIG:-/opt/marcellus/config/sidecar.yml}"
UNIT="${MARCELLUS_UNIT:-marcellus}"
THRESHOLD="${MARCELLUS_HEALTHCHECK_FAILS:-3}"
STATE="/run/${UNIT}-healthcheck.fails"

host=$(sed -n 's/^[[:space:]]*bind_host:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$CONFIG" | head -n1)
port=$(sed -n 's/^[[:space:]]*bind_port:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$CONFIG" | head -n1)
host=${host:-127.0.0.1}
port=${port:-5001}
[ "$host" = "0.0.0.0" ] && host=127.0.0.1

if curl -fsS -m 8 -o /dev/null "http://${host}:${port}/healthz"; then
    rm -f "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
if [ "$fails" -lt "$THRESHOLD" ]; then
    echo "healthcheck: ${host}:${port}/healthz failed (${fails}/${THRESHOLD})"
    exit 0
fi

echo "healthcheck: ${host}:${port}/healthz failed ${fails}x, restarting ${UNIT}"
rm -f "$STATE"
systemctl restart "$UNIT"
