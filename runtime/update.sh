#!/usr/bin/env bash
# toolshed-update — update Toolshed WITHOUT silently changing user state (fixes D2).
#
# Usage:
#   toolshed-update.sh --profile default [--ref <sha>] [--json]
#
# Contract (ADR-0010 §2):
#   capture state → update → restore config/state → verify grant+enabled+routing
#   on ANY failure: restore the pre-update config, exit non-zero. No half-states.
#
# Exit codes: 0 ok · 1 hermes not found · 5 update failed · 6 verification failed

set -u

REPO="Huy3ko/toolshed/runtime"
PLUGIN_NAME="hermes-token-router"
JSON=0; REF=""; PROFILES=""; TARGET_USER=""; TARGET_HOME=""
RESULT_LOG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILES="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --json) JSON=1; shift ;;
    # D2/Multi-User contract: update a FOREIGN agent's home. The updater derives
    # the target user from the home owner and runs every write step as that user
    # runs every write step under the target identity — root never owns plugin files.
    --home) TARGET_HOME="$2"; shift 2 ;;
    --user) TARGET_USER="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Resolve target user/home. Fail closed when ambiguous (ADR-0010 §Updater-Vertrag).
if [ -n "$TARGET_HOME" ]; then
  [ -d "$TARGET_HOME" ] || { echo "✗ target home not found: $TARGET_HOME" >&2; exit 4; }
  if [ -z "$TARGET_USER" ]; then
    TARGET_USER="$(stat -c '%U' "$TARGET_HOME")"
    [ -n "$TARGET_USER" ] || { echo "✗ cannot derive owner of $TARGET_HOME — pass --user explicitly" >&2; exit 4; }
  fi
fi
# Run-as wrapper: identity = target user when updating a foreign home, else current user
AS_USER() {
  if [ "$(id -un)" = "$TARGET_USER" ] || [ -z "$TARGET_USER" ]; then
    env HOME="$TH" "$@"
    return
  fi
  # Foreign identity: execute under the resolved target account with explicit HOME.
  local CMD="$1"; shift
  if [ "$(id -un)" = "root" ]; then
      runuser -u "$TARGET_USER" -- env HOME="$TH" "$CMD" "$@"
    else
      setpriv --reuid="$TARGET_USER" --regid="$TARGET_USER" --init-groups env HOME="$TH" "$CMD" "$@" 2>/dev/null         || { echo "✗ cannot drop privileges to $TARGET_USER from $(id -un) — rerun as root or as $TARGET_USER" >&2; return 1; }
    fi
}

say() { [ "$JSON" = "0" ] && echo "$@"; return 0; }
jadd() { RESULT_LOG="$RESULT_LOG$1\n"; }

# TH: Hermes-Config-Root. Suchkette unten deckt beide Layouts ab:
#   git-install:    <home>/.hermes/hermes-agent/venv/bin/hermes
#   source-install: <home>/src/hermes-agent/venv/bin/hermes
# Default-Auflösung (ohne --home): Bei --user wird das Home des ZIELUSERS aus
# Resolve the target user's real home via the system user database (getent)
# — never trust $HOME of the caller (multi-user fix, helper review 2026-08-23).
# USER_HOME = echtes Home des Zielusers; die Suchkette nutzt NUR noch dieses
# (Canary-Fund v0.1.5: $HOME-des-Aufrufers trifft im root-Lauf nie den
# Zieluser-Layout, alle 4 Einträge missen).
TH="${TARGET_HOME:-}"
USER_HOME="$HOME"
if [ -z "$TH" ]; then
  if [ -n "$TARGET_USER" ]; then
    USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    if [ -z "$USER_HOME" ] || [ "$USER_HOME" = "$TARGET_USER" ] || [ ! -d "$USER_HOME" ]; then
      echo "✗ cannot resolve home for target user: $TARGET_USER — pass --home explicitly" >&2
      exit 4
    fi
    TH="$USER_HOME/.hermes"
  else
    TH="$HOME/.hermes"
  fi
elif [ -n "$TARGET_USER" ]; then
  USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  [ -z "$USER_HOME" ] || [ "$USER_HOME" = "$TARGET_USER" ] && USER_HOME="$TH"
fi
HERMES_BIN="$(AS_USER bash -c 'command -v hermes' 2>/dev/null || true)"
if [ -z "$HERMES_BIN" ]; then
  for C in "$TH/hermes-agent/venv/bin/hermes" \
           "$USER_HOME/.hermes/hermes-agent/venv/bin/hermes" \
           "$USER_HOME/src/hermes-agent/venv/bin/hermes" \
           "$TH/src/hermes-agent/venv/bin/hermes"; do
    [ -x "$C" ] && HERMES_BIN="$C" && break
  done
fi
[ -z "$HERMES_BIN" ] && say "✗ hermes not found" && jadd '{"ok":false,"reason":"no hermes"}' && [ "$JSON" = "1" ] && printf "%b" "$RESULT_LOG" && exit 1

[ -z "$PROFILES" ] && PROFILES="default"
IFS=',' read -r -a TARGETS <<< "$PROFILES"

FAILED=()
for P in "${TARGETS[@]}"; do
  CFG="$(ls -d "${TH}/profiles/$P/plugins/$PLUGIN_NAME/config.yaml" \
             "${TH}/plugins/$PLUGIN_NAME/config.yaml" 2>/dev/null | head -1)"
  if [ -z "$CFG" ] || [ ! -f "$CFG" ]; then
    FAILED+=("$P:no-config"); jadd "{\"profile\":\"$P\",\"step\":\"find-config\",\"ok\":false}"; continue
  fi

  # ---------- 1. CAPTURE STATE ----------
  BACKUP="$CFG.preupdate.$(date +%s)"
  cp "$CFG" "$BACKUP"

  OLD_ENABLED=$(grep -m1 '^  enabled:' "$CFG" | awk '{print $2}')
  OLD_MODE=$(grep -m1 '^  mode:' "$CFG" | awk '{print $2}')
  OLD_FLOOR=$(grep -A6 'floor_toolsets:' "$CFG" | head -7)
  GRANT_BEFORE=$("$HERMES_BIN" -p "$P" plugins capabilities $PLUGIN_NAME 2>/dev/null | grep -c "tools.override: granted")
  OLD_COMMIT=$(cd "$(dirname "$CFG")" && git rev-parse --short HEAD 2>/dev/null || echo "?")

  say "── Profile: $P ────────────────────────────────────────────"
  say "  before: commit=$OLD_COMMIT enabled=$OLD_ENABLED mode=${OLD_MODE:-active} grant=$GRANT_BEFORE"
  jadd "{\"profile\":\"$P\",\"before\":{\"commit\":\"$OLD_COMMIT\",\"enabled\":\"$OLD_ENABLED\",\"grant\":$GRANT_BEFORE}}"

  # ---------- 2. UPDATE ----------
  REFARG=(); [ -n "$REF" ] && REFARG=(--ref "$REF")
  UPD_LOG="/tmp/toolshed_update_$P.log"
AS_USER "$HERMES_BIN" -p "$P" plugins install "$REPO" ${REFARG+"${REFARG[@]}"} --force > "$UPD_LOG" 2>&1
UPD_OUT=$(cat "$UPD_LOG")
  say "  [debug] upd_out FULL: [$UPD_OUT]"
  # Erfolg = exakte Token-Matches; "Installed"/"Location" können nicht als
  # Substring falsch-matchen, weil beide Tokens installer-eigen sind und
  # "Not Installed"/"Already installed" anders lauten. Kein ^-Anker: der
  # Installer rahmt Output in Unicode-Boxen (│ … │), Zeilenanfänger wäre
  # das Box-Zeichen (Helper-Fund, v0.1.5-Review).
  if ! echo "$UPD_OUT" | grep -qE "✓ Installed|Plugin installed:"; then
    say "  ✗ update failed — restoring config from backup"
    cp "$BACKUP" "$CFG"
    FAILED+=("$P:update"); jadd "{\"profile\":\"$P\",\"step\":\"update\",\"ok\":false}"; continue
  fi

  # ---------- 3. POST-INSTALL VERIFICATION (not just installer text) ----------
  # v0.1.5 lesson: the installer's success line alone is not proof.
  NEW_CFG="$(ls -d "${TH}/profiles/$P/plugins/$PLUGIN_NAME/config.yaml" \
                 "${TH}/plugins/$PLUGIN_NAME/config.yaml" 2>/dev/null | head -1)"
  if [ -z "$NEW_CFG" ] || [ ! -f "$NEW_CFG" ]; then
    say "  ✗ post-install: new plugin dir not found — restoring backup"
    cp "$BACKUP" "$CFG"
    FAILED+=("$P:postinstall-missing"); jadd "{\"profile\":\"$P\",\"step\":\"postinstall\",\"ok\":false}"; continue
  fi
  NEW_PLUGIN_DIR="$(dirname "$NEW_CFG")"
  LAYOUT_MARKER="$NEW_PLUGIN_DIR/layout_version"
  if ! grep -q "^layout_version: 2$" "$LAYOUT_MARKER" 2>/dev/null; then
    say "  ✗ post-install: layout_version=2 marker missing — expected runtime/v2 payload, got something else. Restoring backup."
    cp "$BACKUP" "$CFG"
    FAILED+=("$P:layout-not-v2"); jadd "{\"profile\":\"$P\",\"step\":\"verify-layout\",\"ok\":false}"; continue
  fi

  # ---------- 4. RESTORE USER CONFIG ----------
  # merge: keep new defaults, but restore user's enabled/mode/floor
  sed -i "s|^  enabled:.*|  enabled: $OLD_ENABLED|" "$NEW_CFG"
  if [ -n "$OLD_MODE" ]; then sed -i "s|^  mode:.*|  mode: $OLD_MODE|" "$NEW_CFG"; fi

  # ---------- 5. VERIFY ----------
  GRANT_CFG="${TH}/config.yaml"
GRANT_AFTER=0
[ -f "$GRANT_CFG" ] && grep -A2 "hermes-token-router:" "$GRANT_CFG" | grep -q "allow_tool_override: true" && GRANT_AFTER=1
  EN_AFTER=$(grep -m1 '^  enabled:' "$NEW_CFG" | awk '{print $2}')
  NEW_COMMIT=$(cd "$(dirname "$NEW_CFG")" && git rev-parse --short HEAD 2>/dev/null || echo "?")

  if [ "$EN_AFTER" != "$OLD_ENABLED" ]; then
    say "  ✗ enabled-state lost ($OLD_ENABLED → $EN_AFTER) — restoring backup"
    cp "$BACKUP" "$NEW_CFG"
    FAILED+=("$P:enabled-lost"); jadd "{\"profile\":\"$P\",\"step\":\"verify-enabled\",\"ok\":false}"; continue
  fi
  if [ "$GRANT_BEFORE" = "1" ] && [ "$GRANT_AFTER" = "0" ]; then
    say "  ✗ grant lost during update — restoring backup"
    cp "$BACKUP" "$NEW_CFG"
    FAILED+=("$P:grant-lost"); jadd "{\"profile\":\"$P\",\"step\":\"verify-grant\",\"ok\":false}"; continue
  fi

  say "  after: commit=$NEW_COMMIT enabled=$EN_AFTER grant=$GRANT_AFTER"
  jadd "{\"profile\":\"$P\",\"after\":{\"commit\":\"$NEW_COMMIT\",\"enabled\":\"$EN_AFTER\",\"grant\":$GRANT_AFTER},\"ok\":true}"
done

FAILCOUNT=${#FAILED[@]}
if [ "$FAILCOUNT" -eq 0 ]; then
  say ""
  say "✅ Update complete — config, enabled-state and grants preserved."
  jadd '{"summary":"ok"}'
  [ "$JSON" = "1" ] && printf "%b" "{\n$RESULT_LOG}"
  exit 0
else
  say ""
  say "❌ Update had failures: ${FAILED[*]}"
  jadd "{\"summary\":\"failed\"}"
  [ "$JSON" = "1" ] && printf "%b" "{\n$RESULT_LOG}"
  exit 6
fi
