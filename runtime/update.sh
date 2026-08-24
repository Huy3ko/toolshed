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
    env HOME="$TH" HERMES_HOME="$TH" "$@"
    return $?
  fi
  # Foreign identity: execute under the resolved target account with explicit HOME.
  local CMD="$1"; shift
  if [ "$(id -un)" = "root" ]; then
      runuser -u "$TARGET_USER" -- env HOME="$TH" HERMES_HOME="$TH" "$CMD" "$@"
    else
      setpriv --reuid="$TARGET_USER" --regid="$TARGET_USER" --init-groups env HOME="$TH" HERMES_HOME="$TH" "$CMD" "$@" 2>/dev/null || { echo "✗ cannot drop privileges to $TARGET_USER from $(id -un) — rerun as root or as $TARGET_USER" >&2; return 1; }
    fi
}

say() { [ "$JSON" = "0" ] && echo "$@"; return 0; }
jadd() { RESULT_LOG="$RESULT_LOG$1\n"; }

# Atomic rollback: restore the ENTIRE pre-update plugin tree from the archive.
# Used on any failure after the tree backup exists; config-only restore is not
# enough for a layout migration (ADR-0011: no half-states).
rollback_tree() {
  local PDIR="$1" TB="$2"
  AS_USER rm -rf "$PDIR" || { echo "✗ ROLLBACK FAILED: could not remove $PDIR — manual recovery from $TB required" >&2; return 1; }
  AS_USER tar -xzf "$TB" -C "$(dirname "$PDIR")" || { echo "✗ ROLLBACK FAILED: could not extract $TB — manual recovery required" >&2; return 1; }
  return 0
}

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
# --home receives the user's HOME (e.g. /home/alice), not the .hermes root.
# Normalize: strip a trailing /.hermes so callers can pass either form.
case "$TH" in
  */.hermes) TH="${TH%/.hermes}" ;;
esac
# Deterministic target context: USER_HOME and TARGET_USER derive from --home,
# never from the invoking process. Root must not leak its own HOME here.
USER_HOME="$TH"
if [ -z "$TARGET_USER" ]; then
  TARGET_USER="$(stat -c '%U' "$USER_HOME")"
  [ -n "$TARGET_USER" ] || { echo "✗ cannot derive owner of $USER_HOME — pass --user explicitly" >&2; exit 4; }
fi
TH="$USER_HOME/.hermes"

# Preflight assertions: fail closed BEFORE any install when the target context
# does not resolve cleanly (multi-user bug class, found in v0.1.6 canary).
[ -d "$USER_HOME" ] || { echo "✗ target home does not exist: $USER_HOME" >&2; exit 4; }
[ "$(stat -c '%U' "$USER_HOME")" = "$TARGET_USER" ] || { echo "✗ owner of $USER_HOME is not $TARGET_USER" >&2; exit 4; }
HERMES_BIN=""
for C in "$TH/hermes-agent/venv/bin/hermes" \
         "$USER_HOME/src/hermes-agent/venv/bin/hermes"; do
  [ -x "$C" ] && HERMES_BIN="$C" && break
done
[ -n "$HERMES_BIN" ] || { echo "✗ no hermes binary under $USER_HOME — refusing to guess from caller PATH" >&2; exit 1; }

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
  PLUGIN_DIR="$(dirname "$CFG")"
  BACKUP="${PLUGIN_DIR}.config.preupdate.$(date +%s)"
  AS_USER cp "$CFG" "$BACKUP"

  # Full-tree backup for atomic migration (v1 -> v2 or any failed update).
  # The whole plugin dir is archived BEFORE anything is replaced; it is
  # deleted only after every verification passes, and restored wholesale
  # on any failure. No half-states (ADR-0010/ADR-0011 contract).
  TREE_BACKUP="${PLUGIN_DIR}.preupdate.$(date +%s).tgz"
  AS_USER tar -czf "$TREE_BACKUP" -C "$(dirname "$PLUGIN_DIR")" "$(basename "$PLUGIN_DIR")"
  [ -s "$TREE_BACKUP" ] || { FAILED+=("$P:tree-backup"); jadd "{\"profile\":\"$P\",\"step\":\"tree-backup\",\"ok\":false}"; continue; }

  OLD_ENABLED=$(grep -m1 '^  enabled:' "$CFG" | awk '{print $2}')
  OLD_MODE=$(grep -m1 '^  mode:' "$CFG" | awk '{print $2}')
  OLD_FLOOR=$(grep -A6 'floor_toolsets:' "$CFG" | head -7)
  GRANT_BEFORE=$("$HERMES_BIN" -p "$P" plugins capabilities $PLUGIN_NAME 2>/dev/null | grep -c "tools.override: granted")
  OLD_COMMIT=$(cd "$PLUGIN_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "?")

  # Layout detection via explicit marker, not path guessing.
  if grep -q "^layout_version: 2$" "$PLUGIN_DIR/layout_version" 2>/dev/null; then
    LAYOUT="v2"
  elif [ -d "$PLUGIN_DIR/adr" ] || [ -d "$PLUGIN_DIR/.github" ]; then
    LAYOUT="v1"
  else
    LAYOUT="unknown"
  fi

  say "── Profile: $P ────────────────────────────────────────────"
  say "  before: commit=$OLD_COMMIT enabled=$OLD_ENABLED mode=${OLD_MODE:-active} grant=$GRANT_BEFORE"
  jadd "{\"profile\":\"$P\",\"before\":{\"commit\":\"$OLD_COMMIT\",\"enabled\":\"$OLD_ENABLED\",\"grant\":$GRANT_BEFORE}}"

  # ---------- 2. UPDATE ----------
  REFARG=(); [ -n "$REF" ] && REFARG=(--ref "$REF")
  UPD_LOG="${TH}/.toolshed_update_${P}.log"
  UPD_OUT="$(AS_USER "$HERMES_BIN" -p "$P" plugins install "$REPO" ${REFARG+"${REFARG[@]}"} --force 2>&1)"
  UPD_RC=$?
  AS_USER rm -f "$UPD_LOG"
  say "  [debug] upd_out FULL: [$UPD_OUT]"
  # Erfolg = exakte Token-Matches; "Installed"/"Location" können nicht als
  # Substring falsch-matchen, weil beide Tokens installer-eigen sind und
  # "Not Installed"/"Already installed" anders lauten. Kein ^-Anker: der
  # Installer rahmt Output in Unicode-Boxen (│ … │), Zeilenanfänger wäre
  # das Box-Zeichen (Helper-Fund, v0.1.5-Review).
  if [ "$UPD_RC" -ne 0 ] || ! echo "$UPD_OUT" | grep -qE "✓ Installed|Plugin installed:"; then
    say "  ✗ update failed — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:update"); jadd "{\"profile\":\"$P\",\"step\":\"update\",\"ok\":false}"; continue
  fi

  # ---------- 3. POST-INSTALL VERIFICATION (not just installer text) ----------
  # Runtime v2 intentionally ships NO user config. Find the installed tree
  # first, then restore the user's config into it.
  NEW_PLUGIN_DIR="$(ls -d "${TH}/profiles/$P/plugins/$PLUGIN_NAME" \
                         "${TH}/plugins/$PLUGIN_NAME" 2>/dev/null | head -1)"
  if [ -z "$NEW_PLUGIN_DIR" ] || [ ! -d "$NEW_PLUGIN_DIR" ]; then
    say "  ✗ post-install: new plugin dir not found — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:postinstall-missing"); jadd "{\"profile\":\"$P\",\"step\":\"postinstall\",\"ok\":false}"; continue
  fi
  LAYOUT_MARKER="$NEW_PLUGIN_DIR/layout_version"
  if ! grep -q "^layout_version: 2$" "$LAYOUT_MARKER" 2>/dev/null; then
    say "  ✗ post-install: layout_version=2 marker missing — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:layout-not-v2"); jadd "{\"profile\":\"$P\",\"step\":\"verify-layout\",\"ok\":false}"; continue
  fi

  # ---------- 4. RESTORE USER CONFIG (merge, not overwrite) ----------
  # v2 config ships fresh defaults; only documented user-tunable keys are
  # carried over from the pre-update config. Unknown v1 keys are NOT copied
  # (helper review: wholesale overwrite could resurrect permissive v1 values).
  NEW_CFG="$NEW_PLUGIN_DIR/config.yaml"
  if [ ! -f "$NEW_CFG" ]; then
    say "  ✗ new config missing — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:restore"); jadd "{\"profile\":\"$P\",\"step\":\"restore\",\"ok\":false}"; continue
  fi
  AS_USER sed -i \
    -e "s|^\\(  enabled:\\).*|\\1 $OLD_ENABLED|" \
    "$NEW_CFG"
  if [ -n "$OLD_MODE" ]; then
    AS_USER sed -i "s|^\\(  expansion_mode:\\).*|\\1 $OLD_MODE|" "$NEW_CFG"
  fi
  # floor_toolsets: preserve the user's list verbatim when present in both.
  if [ -n "$OLD_FLOOR" ]; then
    NEW_FLOOR="$(grep -A6 'floor_toolsets:' "$NEW_CFG" | head -7)"
    if [ -n "$NEW_FLOOR" ]; then
      AS_USER python3 - "$CFG" "$NEW_CFG" <<'PYEOF'
import re, sys
old_p, new_p = sys.argv[1], sys.argv[2]
def grab(path):
    txt = open(path).read()
    m = re.search(r'^(  floor_toolsets:\s*\[[^\]]*\])\s*$', txt, re.M)
    return m.group(1) if m else None
old_val = grab(old_p)
new_txt = open(new_p).read()
if old_val:
    m = re.search(r'^(  floor_toolsets:\s*\[[^\]]*\])\s*$', new_txt, re.M)
    if m:
        open(new_p, "w").write(new_txt[:m.start()] + old_val + new_txt[m.end():])
PYEOF
      [ $? -eq 0 ] || { say "  ✗ floor_toolsets merge failed — rolling back whole plugin tree"; rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"; FAILED+=("$P:floor-merge"); jadd "{\"profile\":\"$P\",\"step\":\"floor-merge\",\"ok\":false}"; continue; }
    fi
  fi

  # ---------- 5. VERIFY ----------
  GRANT_CFG="${TH}/config.yaml"
GRANT_AFTER=0
[ -f "$GRANT_CFG" ] && grep -A2 "hermes-token-router:" "$GRANT_CFG" | grep -q "allow_tool_override: true" && GRANT_AFTER=1
  EN_AFTER=$(grep -m1 '^  enabled:' "$NEW_CFG" | awk '{print $2}')
  NEW_COMMIT=$(cd "$(dirname "$NEW_CFG")" && git rev-parse --short HEAD 2>/dev/null || echo "?")

  if [ "$EN_AFTER" != "$OLD_ENABLED" ]; then
    say "  ✗ enabled-state lost ($OLD_ENABLED → $EN_AFTER) — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:enabled-lost"); jadd "{\"profile\":\"$P\",\"step\":\"verify-enabled\",\"ok\":false}"; continue
  fi
  if [ "$GRANT_BEFORE" = "1" ] && [ "$GRANT_AFTER" = "0" ]; then
    say "  ✗ grant lost during update — rolling back whole plugin tree"
    rollback_tree "$PLUGIN_DIR" "$TREE_BACKUP"
    FAILED+=("$P:grant-lost"); jadd "{\"profile\":\"$P\",\"step\":\"verify-grant\",\"ok\":false}"; continue
  fi

  say "  after: commit=$NEW_COMMIT enabled=$EN_AFTER grant=$GRANT_AFTER layout=v2 (was $LAYOUT)"
  jadd "{\"profile\":\"$P\",\"after\":{\"commit\":\"$NEW_COMMIT\",\"enabled\":\"$EN_AFTER\",\"grant\":$GRANT_AFTER,\"layout_was\":\"$LAYOUT\"},\"ok\":true}"

  # ---------- 7. SUCCESS: only now drop the archived pre-update tree ----------
  # Migration/update fully verified — the tree backup has served its purpose.
  rm -f "$TREE_BACKUP"
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
