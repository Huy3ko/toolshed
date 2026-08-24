{
  "title": "Distribution v2: minimal runtime payload + subdirectory install + layout migration",
  "status": "proposed",
  "date": "2026-08-24",
  "context": "External report (Termux, fresh Hermes): `hermes plugins install Huy3ko/toolshed` blocked with DANGEROUS verdict on v0.1.5. Investigation (2026-08-24) established:\n\n1. Hermes plugin installer clones the WHOLE repo and scans the whole clone (tools/plugin_guard.py rglob over tmp_target; EXCLUDED_DIRS covers only .git/__pycache__/venv/node_modules/caches — NOT adr/, .github/, docs/, CONTRIBUTING.md).\n2. Verdict rule: any critical finding -> dangerous; any high -> caution; medium/low never drive the verdict.\n3. plugin.yaml has NO file-scope mechanism. The scanner does not read it for scope.\n4. BUT the installer natively supports SUBDIRECTORY installs: `owner/repo/path/to/plugin` (hermes_cli/plugins_cmd.py::_resolve_git_url). The scan then runs on tmp_target = the subdir only (_scan_plugin_tree(tmp_target, ...)).\n5. Install metadata pins exact revisions; update.sh already drives `plugins install $REPO --force` per profile with state capture/restore (ADR-0010 contract).\n6. Interim fix (commit 660db87, pushed to main): removed scanner-triggering literals from update.sh comments (/etc/passwd in a comment = critical traversal; 'sudo -u' wording = high), replaced privilege-drop with runuser(root)/setpriv(fallback). Local scan of full repo after fix: SAFE (only 4 informational mediums from dev docs). Helper canary still showed old content on first run — cache/ref question resolved below.\n7. Ref/cache resolution: installer always fresh-clones (--depth 1) into a temp dir per install; no cross-install cache. The stale canary result was caused by GitHub serving a cached view? No — root cause identified: helper ran install BEFORE 660db87 was pushed. Post-push runs must re-verify.\n\ndecision": "",
  "options_considered": [
    {
      "name": "A: keep single repo, keep comments scanner-clean forever",
      "rejected_because": "Fragile: every future doc/comment mentioning sudo or /etc/passwd reintroduces findings. Docs are written for humans, not for a regex scanner."
    },
    {
      "name": "B: separate minimal runtime repository",
      "pros": "Cleanest scan surface; dev repo fully free.",
      "cons": "Two repos to sync; version skew risk between runtime repo and source repo; update.sh REPO constant and CI both need rewiring; contributors must push twice."
    },
    {
      "name": "C (CHOSEN): same repo, move runtime payload into a dedicated subdirectory, community installs point at owner/repo/runtime",
      "pros": "Hermes natively supports owner/repo/subdir installs AND scans only that subdir (_resolve_subdir_within before _scan_plugin_tree). Dev files stay at repo root where they belong. Single repo, single CI, no version skew. update.sh keeps working because it lives inside the scanned payload.",
      "cons": "Existing installed users have the full-repo layout under ~/.hermes/plugins/hermes-token-router/; their updater must migrate layout v1 -> v2 without losing config/grants/state."
    }
  ],
  "decision": "Option C. Runtime payload moves to `runtime/` at repo root. Community install identifier becomes `Huy3ko/toolshed/runtime`. update.sh gains an explicit one-time layout migration (v1 full-repo install -> v2 runtime-subdir install), preserving config.yaml, grants, enabled/mode state across the move.",
  "frozen_invariants": [
    "runtime/ is the ONLY public installation surface from v0.1.6 on",
    "v1 = full-repo layout (pre-0.1.6); v2 = runtime-subdir layout (post)",
    "v1 -> v2 is a MIGRATION, not a normal update: config, grants, enabled/mode/floor, state and ownership must survive byte-identical or verified-equivalent",
    "migration must be rollback-capable at every step before the old tree is removed",
    "Hermes security scanner stays ENABLED throughout; no bypass, no scan_on_install=false",
    "Fresh Install and Existing-Migration are separate canary cases; both must pass plus a repeat v2->v2 update"
  ],
  "layout_contract": {
    "v1": "full-repo checkout at plugins/<name>/ (pre-0.1.6)",
    "v2": "runtime-only checkout at plugins/<name>/ (post-0.1.6, installed via Huy3ko/toolshed/runtime)"
  },
  "migration_invariants": [
    "config.yaml (per profile) survives byte-identical",
    "grants survive (they live outside the plugin dir, verified by doctor)",
    "enabled/mode survive",
    "no root-owned files created (AS_USER contract holds)",
    "on any failure: rollback to pre-migration tree",
    "doctor reports origin=runtime-v2 afterwards"
  ],
  "update_sh_changes": [
    "detect current layout (presence of adr/ or .github/ => v1)",
    "v1: backup whole plugin dir -> install new payload via plugins install Huy3ko/toolshed/runtime --force -> restore per-profile config.yaml -> verify grant+enabled+routing -> remove archived v1 tree only on success",
    "v2: normal update path as today (already implemented)",
    "REPO constant becomes Huy3ko/toolshed/runtime"
  ],
  "acceptance": [
    "fresh install: hermes plugins install Huy3ko/toolshed/runtime passes scan with scanning ENABLED (verdict safe or caution-confirmed, no dangerous)",
    "migration: v0.1.5-style install -> update -> all invariants hold (helper-tested)",
    "repeat update v2->v2 works",
    "rollback path tested",
    "doctor green, routing smoke green"
  ]
}
