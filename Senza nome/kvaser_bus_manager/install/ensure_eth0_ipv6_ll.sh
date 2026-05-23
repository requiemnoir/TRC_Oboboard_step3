#!/usr/bin/env bash
set -euo pipefail

# Ensure eth0 has an IPv6 link-local address so DoIP (fe80::/64) works reliably.
# Rationale: some systems bring eth0 UP without auto-configuring IPv6 LL,
# causing DoIP connections to fail with "Network is unreachable".

IFACE="${1:-eth0}"

MAX_WAIT_S="${KVBM_LL_MAX_WAIT_S:-8}"
SLEEP_S="${KVBM_LL_SLEEP_S:-0.2}"

if ! command -v ip >/dev/null 2>&1; then
  echo "ip command not found" >&2
  exit 0
fi

# If interface doesn't exist, nothing to do.
if ! ip link show dev "$IFACE" >/dev/null 2>&1; then
  echo "iface $IFACE not found" >&2
  exit 0
fi

# If IPv6 is disabled, warn and exit.
if [[ -f "/proc/sys/net/ipv6/conf/${IFACE}/disable_ipv6" ]]; then
  if [[ "$(cat "/proc/sys/net/ipv6/conf/${IFACE}/disable_ipv6" 2>/dev/null || echo 0)" != "0" ]]; then
    echo "ipv6 disabled on $IFACE" >&2
    exit 0
  fi
fi

# Helper: do we have a usable (non-tentative) link-local?
has_ready_ll() {
  # Example line:
  # inet6 fe80::1234/64 scope link noprefixroute
  # If 'tentative' is present, DAD hasn't completed yet and bind/connect may fail.
  ip -6 addr show dev "$IFACE" scope link | grep -q "inet6 fe80:" || return 1
  ip -6 addr show dev "$IFACE" scope link | grep -q "tentative" && return 1
  return 0
}

# Fast path
if has_ready_ll; then
  exit 0
fi

# Ask kernel to (re)generate IPv6 link-local: bounce addrgen/disable flag.
# We don't hardcode fe80::1 because that can stay tentative or conflict.
if [[ -f "/proc/sys/net/ipv6/conf/${IFACE}/addr_gen_mode" ]]; then
  # 0=EUI64, 1=None, 2=StablePrivacy, 3=Random
  # If it's 1 (none), set to stable privacy.
  mode="$(cat "/proc/sys/net/ipv6/conf/${IFACE}/addr_gen_mode" 2>/dev/null || echo 0)"
  if [[ "$mode" == "1" ]]; then
    echo 2 > "/proc/sys/net/ipv6/conf/${IFACE}/addr_gen_mode" 2>/dev/null || true
  fi
fi

# Trigger kernel IPv6 re-init on the iface.
echo 0 > "/proc/sys/net/ipv6/conf/${IFACE}/disable_ipv6" 2>/dev/null || true

# Force a refresh by toggling the link (safe; interface stays named the same)
ip link set dev "$IFACE" down || true
ip link set dev "$IFACE" up || true

# Wait for a ready LL
end_ts=$(( $(date +%s) + MAX_WAIT_S ))
while [[ $(date +%s) -lt $end_ts ]]; do
  if has_ready_ll; then
    break
  fi
  sleep "$SLEEP_S"
done

if ! has_ready_ll; then
  echo "no ready IPv6 link-local on $IFACE after ${MAX_WAIT_S}s" >&2
  # Still try to ensure route; backend may handle retries.
fi

# Ensure fe80::/64 route exists for this iface (usually auto-added by kernel).
if ! ip -6 route show dev "$IFACE" 2>/dev/null | grep -q "^fe80::/64"; then
  ip -6 route add fe80::/64 dev "$IFACE" 2>/dev/null || true
fi

# ── Mirror IPv4 address ──────────────────────────────────────────────────
# The gateway ECU (DID 0x096F) sends mirror data to 192.168.200.1.
# This was configured via ODIS/FAZIT and cannot be changed by our software
# (WriteDID returns NRC 0x24).  Ensure the address is always present on eth0.
MIRROR_IP="${KVBM_MIRROR_IP:-192.168.200.1}"
MIRROR_PREFIX="${KVBM_MIRROR_PREFIX:-24}"
if ! ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q "${MIRROR_IP}/"; then
  echo "adding mirror dest ${MIRROR_IP}/${MIRROR_PREFIX} to $IFACE"
  ip addr add "${MIRROR_IP}/${MIRROR_PREFIX}" dev "$IFACE" 2>/dev/null || true
fi
