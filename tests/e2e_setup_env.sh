#!/usr/bin/env bash
# Baut ein isoliertes HERMES_HOME, in dem der Token-Router aus dem REPO-Stand
# (runtime/) als Plugin installiert ist. Der E2E-Test laeuft damit gegen die
# Quelle des Repos - nicht gegen eine installierte Kopie.
#
# Aufruf:  tests/e2e_setup_env.sh [ZIEL-HOME]
# Danach:  TOOLSHED_E2E_HERMES_DIR=<hermes-agent> \
#          TOOLSHED_E2E_HOME=<ZIEL-HOME> \
#          python -m pytest tests/test_e2e_real_dispatcher.py
set -euo pipefail
T="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
E2E_HOME="${1:-${TMPDIR:-/tmp}/toolshed-e2e-home}"

rm -rf "$E2E_HOME"
mkdir -p "$E2E_HOME/plugins" "$E2E_HOME/sessions" "$E2E_HOME/logs"

# Plugin aus dem Repo-Stand - ohne Bytecode-Reste
cp -r "$T/runtime" "$E2E_HOME/plugins/hermes-token-router"
find "$E2E_HOME/plugins/hermes-token-router" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -f "$E2E_HOME/plugins/hermes-token-router"/*.pyc 2>/dev/null || true

# Router-Konfiguration: aktiv, deterministisch, mit Floor
cat > "$E2E_HOME/plugins/hermes-token-router/config.yaml" <<'YAML'
global:
  enabled: true
  mode: active
  routing_scope: first_turn
  expansion_mode: monotonic
  shrink_mid_session: false
  floor_toolsets: ["terminal", "file", "skills", "memory", "web"]
  deterministic_rules_enabled: true
  confidence_threshold: 0.90
  router_timeout_ms: 750
  router_hard_timeout_ms: 1200
  fail_open: true
  auto_recover_registered_tools: true
  classifier:
    enabled: false
    provider: null
    model: null
    base_url: null
    api_key_env: null
  metrics:
    enabled: true
    local_only: true
    retain_days: 30
profiles: {}
shadow:
  enabled: false
YAML

# Hermes-Config: nur das Token-Router-Plugin aktivieren
cat > "$E2E_HOME/config.yaml" <<'YAML'
plugins:
  enabled:
    - hermes-token-router
  disabled: []
YAML

echo "E2E-HOME: $E2E_HOME"
echo "Hash-Vergleich (muss identisch sein):"
sha256sum "$E2E_HOME/plugins/hermes-token-router/__init__.py" "$T/runtime/__init__.py"
