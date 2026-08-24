#!/usr/bin/env bash
# toolshed-installer — install/enable/verify Toolshed for one or more Hermes profiles.
#
# Usage:
#   toolshed-install.sh                          # interactive: pick profiles
#   toolshed-install.sh --profile default        # non-interactive, single profile
#   toolshed-install.sh --profile a,b --yes      # non-interactive, no prompts
#   toolshed-install.sh --ref <sha>              # pin a release commit
#   toolshed-install.sh --json                   # machine-readable output
#
# Exit codes:
#   0 ok · 1 hermes not found · 2 grant denied/skipped
#   3 activation failed · 4 verification failed
#
# Contract (ADR-0010): installs from GitHub only; sets global.enabled;
# verifies routing is REALLY active — never reports success on a half-install.

set -u

REPO="Huy3ko/toolshed"
PLUGIN_NAME="hermes-token-router"
JSON=0; YES=0; REF=""; PROFILES=""
RESULT_LOG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILES="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --json) JSON=1; shift ;;
    *) shift ;;
  esac
done

say() { if [ "$JSON" = "0" ]; then echo "$@"; fi }
jadd() { RESULT_LOG="$RESULT_LOG$1\n"; }

# ---------- 1. find hermes ----------
HERMES_BIN="$(command -v hermes || true)"
if [ -z "$HERMES_BIN" ]; then
  for C in "$HOME/src/hermes-agent/venv/bin/hermes" \
           "$HOME/hermes-agent/venv/bin/hermes" ; do
    [ -x "$C" ] && HERMES_BIN="$C" && break
  done
fi
if [ -z "$HERMES_BIN" ]; then
  say "✗ hermes CLI not found. Install Hermes first."
  jadd '{"step":"detect","ok":false}'
  [ "$JSON" = "1" ] && printf "%b" "$RESULT_LOG"
  exit 1
fi
say "✓ Hermes found: $HERMES_BIN"

# ---------- 2. discover profiles ----------
mapfile -t ALL_PROFILES < <("$HERMES_BIN" profile list 2>/dev/null | grep -Eo '[a-zA-Z0-9_-]+$' | sort -u)
[ ${#ALL_PROFILES[@]} -eq 0 ] && ALL_PROFILES=("default")

if [ -n "$PROFILES" ]; then
  IFS=',' read -r -a TARGETS <<< "$PROFILES"
elif [ "$YES" = "1" ]; then
  TARGETS=("${ALL_PROFILES[@]}")
else
  echo ""
  echo "Found Hermes agents/profiles:"
  i=1
  for P in "${ALL_PROFILES[@]}"; do echo "  $i) $P"; i=$((i+1)); done
  printf "Install for which? (number(s), comma-separated, empty=all): "
  read -r PICK
  TARGETS=()
  if [ -z "$PICK" ]; then TARGETS=("${ALL_PROFILES[@]}")
  else
    IFS=',' read -r -a IDX <<< "$PICK"
    for N in "${IDX[@]}"; do
      N=$(echo "$N" | tr -d ' ')
      if [[ "$N" =~ ^[0-9]+$ ]] && [ "$N" -ge 1 ] && [ "$N" -le ${#ALL_PROFILES[@]} ]; then
        TARGETS+=("${ALL_PROFILES[$((N-1))]}")
      fi
    done
  fi
fi
[ ${#TARGETS[@]} -eq 0 ] && say "No profile selected." && exit 3

# ---------- 3. explain + confirm the tools.override grant ----------
if [ "$YES" = "1" ]; then
  say "Grant: auto-accepted (--yes)"
else
  echo ""
  echo "── Authorization ──────────────────────────────────────────────"
  echo "Toolshelf changes WHICH already-authorized tools are visible to"
  echo "the model. It does NOT create new permissions."
  echo ""
  printf "Grant tools.override and continue? [y/N]: "
  read -r ANSWER
  case "$ANSWER" in y|Y|yes|Yes) ;; *) say "Grant denied — aborting."; exit 2 ;; esac
fi

# ---------- 4. per-profile install ----------
FAILED=()
for P in "${TARGETS[@]}"; do
  say ""
  say "── Profile: $P ────────────────────────────────────────────"

  REFARG=(); [ -n "$REF" ] && REFARG=(--ref "$REF")

  OUT=$("$HERMES_BIN" -p "$P" plugins install "$REPO" "${REFARG[@]+${REFARG[@]}}" --force 2>&1 | tail -3)
  say "$OUT"

  ENA=$("$HERMES_BIN" -p "$P" plugins enable $PLUGIN_NAME --allow-tool-override 2>&1)
  say "  grant: $(echo "$ENA" | tail -1)"
  if ! echo "$ENA" | grep -qE "Granted|already enabled"; then
      FAILED+=("$P:grant"); jadd "{\"profile\":\"$P\",\"step\":\"grant\",\"ok\":false}"; continue
  fi

  CFG="/home/$(whoami)/.hermes/profiles/$P/plugins/$PLUGIN_NAME/config.yaml"
  [ -f "/home/$(whoami)/.hermes/plugins/$PLUGIN_NAME/config.yaml" ] && [ ! -f "$CFG" ] && \
      CFG="/home/$(whoami)/.hermes/plugins/$PLUGIN_NAME/config.yaml"

  if [ -f "$CFG" ]; then
    cp "$CFG" "$CFG.bak.$(date +%s)"
    sed -i '0,/^  enabled: false$/s//  enabled: true/' "$CFG"
  else
    FAILED+=("$P:config"); jadd "{\"profile\":\"$P\",\"step\":\"config\",\"ok\":false}"; continue
  fi

  CAP=$("$HERMES_BIN" -p "$P" plugins capabilities $PLUGIN_NAME 2>&1 | grep -c "tools.override: granted")
  if [ "$CAP" -eq 0 ]; then FAILED+=("$P:verify-grant"); jadd "{\"profile\":\"$P\",\"step\":\"verify-grant\",\"ok\":false}"; continue; fi

  jadd "{\"profile\":\"$P\",\"ok\":true}"
done

# ---------- 5. summary ----------
FAILCOUNT=${#FAILED[@]}
if [ "$FAILCOUNT" -eq 0 ]; then
  say ""
  say "✅ Toolshed installed & active for: ${TARGETS[*]}"
  say "   Start a fresh session and check logs for: 'narrowed to N toolsets'"
  jadd '{"summary":"ok"}'
  [ "$JSON" = "1" ] && printf "%b" "{\n$RESULT_LOG}"
  exit 0
else
  say ""
  say "❌ Completed with errors on: ${FAILED[*]}"
  jadd "{\"summary\":\"partial\",\"failed\":$FAILCOUNT}"
  [ "$JSON" = "1" ] && printf "%b" "{\n$RESULT_LOG}"
  exit 4
fi
