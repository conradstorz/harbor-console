#!/usr/bin/env bash
# sshd-firewall.sh -- restrict sshd (TCP 22) to the LAN subnet and the tailnet
# with ufw, so neither a router port-forward nor the host's global IPv6 address
# can expose it to the internet. sshd keeps its 0.0.0.0 / [::] bind; the
# firewall decides who gets an answer.
#
# Fault tolerance:
#   * refuses to run unless it can tell your own session will survive;
#   * backs up the ufw rule files and policy before touching anything;
#   * arms a systemd timer that rolls the backup back automatically unless you
#     confirm from a NEW ssh session within GRACE_MIN minutes;
#   * verifies the rules landed and that a container can no longer reach sshd,
#     and rolls back on the spot if either check fails;
#   * every step is idempotent, so re-running `apply` is safe.
#
# Usage (as root, from an SSH session you keep open until confirmed):
#   sudo bash sshd-firewall.sh apply      # back up, add rules, arm auto-rollback
#   sudo bash sshd-firewall.sh confirm    # from a NEW ssh session: cancel the rollback
#   sudo bash sshd-firewall.sh rollback   # restore the backup now
#   sudo bash sshd-firewall.sh status     # show rules, timer and backup
#
# Environment overrides: GRACE_MIN (default 10), FORCE=1 to skip the session check.

set -euo pipefail

PORT=22
GRACE_MIN="${GRACE_MIN:-10}"
TAILNET_IF=tailscale0
TAILNET_CIDR=100.64.0.0/10
STATE_DIR=/var/lib/sshd-firewall
BACKUP_DIR="$STATE_DIR/backup"
TIMER_UNIT=sshd-firewall-rollback
COMMENT_LAN='sshd via LAN'
COMMENT_TAILNET='sshd via tailnet'
COMMENT_DENY='sshd: LAN and tailnet only'
UFW_FILES=(/etc/ufw/user.rules /etc/ufw/user6.rules /etc/default/ufw)
SCRIPT_PATH="$(readlink -f "$0")"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------- helpers --

need_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo)"; }

need_cmds() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "missing command: $c"
  done
}

ufw_active() { ufw status 2>/dev/null | grep -q '^Status: active'; }

rule_count() { ufw show added 2>/dev/null | grep -c '^ufw ' || true; }

lan_iface() {
  ip -4 route show default | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

lan_cidr() {
  ip -4 route show dev "$1" scope link proto kernel | awk '{print $1; exit}'
}

in_cidr() {
  python3 - "$1" "$2" <<'PY'
import ipaddress, sys
try:
    ok = ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2], strict=False)
except ValueError:
    ok = False
sys.exit(0 if ok else 1)
PY
}

# Where is the session running this script coming from? sudo strips
# SSH_CONNECTION, so fall back to the login record for this tty. Empty means
# a local console, which cannot be locked out.
client_ip() {
  if [[ -n ${SSH_CONNECTION:-} ]]; then
    printf '%s\n' "${SSH_CONNECTION%% *}"
    return
  fi
  who -m 2>/dev/null | sed -n 's/.*(\([^)]*\)).*/\1/p' | head -n 1
}

timer_armed() { systemctl list-timers --all --no-legend "$TIMER_UNIT.timer" 2>/dev/null | grep -q "$TIMER_UNIT"; }

disarm_timer() {
  # Only the timer: when the timer fires, this script *is* the service, and
  # stopping it here would kill the rollback before it restores anything.
  if timer_armed; then
    systemctl stop "$TIMER_UNIT.timer" 2>/dev/null || true
    log "auto-rollback timer cancelled"
  fi
}

arm_timer() {
  disarm_timer
  systemd-run --quiet --unit="$TIMER_UNIT" --on-active="${GRACE_MIN}min" \
    --description="roll back sshd ufw rules unless confirmed" \
    /usr/bin/env bash "$SCRIPT_PATH" rollback
  log "auto-rollback armed: fires in ${GRACE_MIN} min unless you run '$0 confirm'"
}

# Delete every rule carrying one of our comments, so re-adding puts them back
# at the top in the right order. Rules are numbered only while ufw is active;
# when inactive, delete by specification instead.
remove_our_rules() {
  local n
  if ufw_active; then
    # highest number first, so deleting does not renumber what is still to go
    for n in $(ufw status numbered | grep -E "# ($COMMENT_LAN|$COMMENT_TAILNET|$COMMENT_DENY)$" \
                 | sed -n 's/^\[ *\([0-9]*\)\].*/\1/p' | sort -rn); do
      ufw --force delete "$n" >/dev/null || true
    done
  else
    ufw delete allow in from "$LAN_CIDR" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
    ufw delete allow in on "$TAILNET_IF" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
    ufw delete deny in to any port "$PORT" proto tcp >/dev/null 2>&1 || true
  fi
}

# Final order must be: 1 allow LAN, 2 allow tailnet, 3 deny. `insert 1` is
# rejected on an empty rule set, so append there instead; otherwise insert in
# reverse so each lands above the previous.
add_our_rules() {
  if (( $(rule_count) == 0 )); then
    ufw allow in from "$LAN_CIDR" to any port "$PORT" proto tcp comment "$COMMENT_LAN"
    ufw allow in on "$TAILNET_IF" to any port "$PORT" proto tcp comment "$COMMENT_TAILNET"
    ufw deny in to any port "$PORT" proto tcp comment "$COMMENT_DENY"
  else
    ufw insert 1 deny in to any port "$PORT" proto tcp comment "$COMMENT_DENY"
    ufw insert 1 allow in on "$TAILNET_IF" to any port "$PORT" proto tcp comment "$COMMENT_TAILNET"
    ufw insert 1 allow in from "$LAN_CIDR" to any port "$PORT" proto tcp comment "$COMMENT_LAN"
  fi
}

backup() {
  local stamp dir f
  stamp="$(date +%Y%m%dT%H%M%S)"
  dir="$BACKUP_DIR/$stamp"
  mkdir -p "$dir"
  for f in "${UFW_FILES[@]}"; do
    [[ -f $f ]] && cp -p "$f" "$dir/"
  done
  if ufw_active; then echo active > "$dir/state"; else echo inactive > "$dir/state"; fi
  ufw status numbered > "$dir/status.txt" 2>&1 || true
  ln -sfn "$dir" "$BACKUP_DIR/latest"
  log "backup written to $dir"
}

restore() {
  local dir="$BACKUP_DIR/latest" f
  [[ -d $dir ]] || die "no backup at $dir; nothing to restore"
  for f in "${UFW_FILES[@]}"; do
    [[ -f "$dir/$(basename "$f")" ]] && cp -p "$dir/$(basename "$f")" "$f"
  done
  if [[ $(cat "$dir/state" 2>/dev/null) == active ]]; then
    ufw reload >/dev/null
  else
    ufw --force disable >/dev/null
  fi
  log "restored ufw from $dir"
}

# A container must NOT be able to reach sshd at its bridge gateway any more.
# Returns 0 = blocked (good), 1 = reachable (bad), 2 = could not test.
container_test() {
  command -v docker >/dev/null 2>&1 || return 2
  local gw out
  gw="$(docker network inspect harbor -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  [[ -n $gw ]] || gw="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  [[ -n $gw ]] || return 2
  out="$(docker run --rm --pull=missing alpine nc -zv -w 3 "$gw" "$PORT" 2>&1 || true)"
  if grep -q 'open' <<<"$out"; then
    log "container at gateway $gw can still reach port $PORT: $out"
    return 1
  fi
  if grep -qiE 'error|cannot|unable' <<<"$out"; then
    log "container test inconclusive: $out"
    return 2
  fi
  log "container at gateway $gw cannot reach port $PORT (good)"
  return 0
}

rules_landed() {
  local status
  status="$(ufw status numbered 2>/dev/null)"
  grep -q "^\[ 1\].*# $COMMENT_LAN$"     <<<"$status" &&
  grep -q "^\[ 2\].*# $COMMENT_TAILNET$" <<<"$status" &&
  grep -q "^\[ 3\].*# $COMMENT_DENY$"    <<<"$status" &&
  grep -q "(v6).*# $COMMENT_DENY$"       <<<"$status"
}

# ---------------------------------------------------------------- actions --

cmd_apply() {
  need_root
  need_cmds ufw ip awk sed python3 systemd-run who

  LAN_IFACE="$(lan_iface)"; [[ -n $LAN_IFACE ]] || die "no default IPv4 route; cannot find the LAN interface"
  LAN_CIDR="$(lan_cidr "$LAN_IFACE")"; [[ -n $LAN_CIDR ]] || die "no kernel route on $LAN_IFACE; cannot derive the LAN subnet"
  ip link show "$TAILNET_IF" >/dev/null 2>&1 || die "$TAILNET_IF is not present; refusing, since tailnet access would be lost"
  log "LAN: $LAN_CIDR on $LAN_IFACE; tailnet: $TAILNET_IF; sshd port: $PORT"

  local me
  me="$(client_ip)"
  if [[ -z $me ]]; then
    log "session origin unknown or local console; assuming it cannot be locked out"
  elif in_cidr "$me" "$LAN_CIDR" || in_cidr "$me" "$TAILNET_CIDR"; then
    log "this session comes from $me, which the new rules allow"
  elif [[ ${FORCE:-0} == 1 ]]; then
    log "WARNING: this session comes from $me, which the new rules will NOT allow; FORCE=1 set, continuing"
  else
    die "this session comes from $me, which the new rules would block. Reconnect via LAN or tailnet, or set FORCE=1"
  fi

  local existing
  existing="$(ufw show added 2>/dev/null | grep -E "port $PORT\b|\b$PORT/tcp\b|OpenSSH" | grep -vE "$COMMENT_LAN|$COMMENT_TAILNET|$COMMENT_DENY" || true)"
  if [[ -n $existing ]]; then
    log "NOTE: other rules mention port $PORT; ours go above them, so they only matter if ours are removed:"
    printf '    %s\n' "$existing" >&2
  fi

  backup
  export LAN_CIDR

  # Keep the blast radius to port 22: if ufw is not yet active, `enable` would
  # otherwise apply its default deny to everything else the host serves.
  if ! ufw_active; then
    ufw default allow incoming >/dev/null
    log "ufw was inactive; default incoming policy set to allow so only port $PORT changes"
  fi

  remove_our_rules
  add_our_rules >/dev/null
  if ! ufw_active; then
    ufw --force enable >/dev/null
    log "ufw enabled"
  fi

  arm_timer

  if ! rules_landed; then
    log "rules did not land in the expected order; rolling back"
    ufw status numbered >&2 || true
    cmd_rollback
    exit 1
  fi
  log "rules in place:"
  ufw status numbered | sed -n '1,/^\[ 3\]/p' >&2

  if container_test; then
    :
  elif [[ $? -eq 1 ]]; then
    log "sshd is still reachable from a container; rolling back"
    cmd_rollback
    exit 1
  fi

  cat >&2 <<EOF

NEXT, within ${GRACE_MIN} minutes and WITHOUT closing this session:
  1. open a NEW ssh session over the LAN   (ssh <user>@$(ip -4 -o addr show dev "$LAN_IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n 1))
  2. open a NEW ssh session over the tailnet (ssh <user>@$(ip -4 -o addr show dev "$TAILNET_IF" | awk '{print $4}' | cut -d/ -f1 | head -n 1))
  3. in either new session run:  sudo bash $SCRIPT_PATH confirm
If step 3 does not happen, the rules roll back automatically and ufw returns
to exactly the state in $BACKUP_DIR/latest.
EOF
}

cmd_confirm() {
  need_root
  if ! timer_armed; then
    log "no auto-rollback timer is armed; nothing to confirm (rules stay as they are)"
    exit 0
  fi
  if ! rules_landed; then
    die "the rules are not in place; refusing to confirm. Run 'status' to see what is there"
  fi
  disarm_timer
  date -Is > "$STATE_DIR/confirmed"
  log "confirmed: sshd is reachable only from the LAN and the tailnet"
}

cmd_rollback() {
  need_root
  disarm_timer
  restore
  date -Is > "$STATE_DIR/rolled-back"
  ufw status numbered >&2 2>/dev/null || true
}

cmd_status() {
  need_root
  ufw status numbered 2>&1 || true
  echo
  if timer_armed; then
    echo "auto-rollback timer: ARMED"
    systemctl list-timers --no-legend "$TIMER_UNIT.timer" || true
  else
    echo "auto-rollback timer: not armed"
  fi
  [[ -f $STATE_DIR/confirmed ]]   && echo "last confirmed:   $(cat "$STATE_DIR/confirmed")"
  [[ -f $STATE_DIR/rolled-back ]] && echo "last rolled back: $(cat "$STATE_DIR/rolled-back")"
  [[ -L $BACKUP_DIR/latest ]]     && echo "latest backup:    $(readlink -f "$BACKUP_DIR/latest")"
  if rules_landed; then echo "sshd rules: in place (1 LAN, 2 tailnet, 3 deny)"; else echo "sshd rules: NOT in place"; fi
}

case "${1:-}" in
  apply)    cmd_apply ;;
  confirm)  cmd_confirm ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  *) sed -n '2,/^$/p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
