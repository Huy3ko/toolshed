# Changelog

All notable changes to Toolshed are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) (0.x = pre-stable).

## [0.2.0] — unreleased

### Fixed
- **A backup next to the plugin directory could be loaded as a second plugin instance.** Hermes
  registers every `plugin.yaml` it finds under a scanned plugins root. A backup of the plugin
  tree placed there as a *directory* is therefore registered as a second manifest with the same
  plugin key — same plugin, separate state (measured on Hermes v0.22.2: two manifests, both keyed
  `hermes-token-router`). The updater's backups are archive *files*, which are not scanned; the
  documentation now states the rule, `doctor.sh` reports the last backup at the path the updater
  actually writes to, and it warns when a second instance has appeared next to the plugin.
  `tests/test_backup_path_agreement.py` pins the contract: the updater's backup path and the
  doctor's search path stay in sync, no backup is ever created as a directory, and the
  second-instance behavior is proven against the real Hermes discovery code.

### Added
- **First-turn routing.** The tool surface is now narrowed in the *first* provider request of a
  session, not from the second turn onward. v0.1.x could not reach the first turn: the hook that
  shortens the surface fires before the model has selected a tool, and that tool call is the only
  source of the knowledge. The selection now runs at the `llm_request` stage, which executes
  inline with the agent on the stack.
- **A layer-mirror test** (`tests/test_layer_mirror.py`) enforces that the repository root, the
  package layer and the distribution payload stay byte-identical. It compares the file *lists* as
  well as the hashes, so a new file that never reached one layer cannot pass unnoticed.
- `tests/test_flat_root_payload.py` — the flat loader payload is importable in a subprocess whose
  `sys.path` contains only the repository root, the dynamic MCP rule build survives the flat
  layout, and the YAML loader is reachable there.

### Fixed
- **The router now actually narrows the tool surface.** The agent reference was resolved by
  walking the call stack for an `agent` local. `pre_llm_call` is a BOUNDED hook that runs on a
  daemon worker, where the caller's stack is truncated — so the walk could never succeed, and
  every turn logged `no agent reference; full set fallback` while the full tool surface stayed
  active. The agent is now resolved through the session: the inline `tool_request` middleware
  (which runs in the main thread, where the agent *is* reachable) learns the `session_id → agent`
  mapping, and the worker hook looks it up by session. No global `current_agent`, so parallel
  sessions never share an agent; without an unambiguous mapping the router stays fail-open on the
  full surface.
- **The flat plugin payload is importable again.** The repository root is the flat Hermes loader
  variant. A relative import without a package parent failed there, was swallowed by the exception
  handler, and silently produced **zero** dynamic MCP rules instead of four — the MCP servers were
  no longer routable, without a single error message. The fallback to the sibling module now lives
  in the file itself.
- **The configuration is no longer loaded with a hard `import yaml`.** PyYAML is not installed in
  the Hermes host (it ships `ruamel.yaml` and its own `hermes_yaml` wrapper). The import failed,
  the configuration was silently `{}`, and the router never narrowed. The loader now tries
  `yaml` → `ruamel.yaml` → `hermes_yaml`.
- **The provider message is measured correctly.** The middleware measured the last user message
  from the request, which in Hermes carries the complete context wrapper (measured: 8910
  characters for 98 characters of user text). The length guard therefore always declined. The
  core's `original_user_message` is used instead (measured: 314 characters).
- **Leading platform frames are stripped** before intent detection. An inbound peer-agent wrapper
  contains "Reply as you would to a colleague's request", which matched the `(email|mail|reply)`
  rule and called `mcp-agentmail` instead of the intended capability. Markers in the middle of a
  message are not treated as a cut point.
- **Refusal reasons are logged at a level the gateway shows.** All fail-open paths logged on
  DEBUG, and the gateway logs from WARNING — a skipped narrowing looked exactly like a successful
  one. The reasons now log on INFO, with the reason instead of the output.
- **Authorization is captured on the middleware path.** `authorized_toolsets` was only recorded in
  the hook path. Because `pre_llm_call` runs on a worker thread, the state stayed `None` and the
  middleware stayed permanently fail-open. The middleware now obtains the state itself — and
  invents no authorization: a host-set `authorization_captured=True` with
  `authorized_toolsets=None` remains authoritative.
- The "no agent reference" warning is now emitted at most once per process instead of once per
  turn, which had flooded the log.
- `runtime/` and `src/toolshed/` stay byte-aligned; the fixes are mirrored into all three layers.

### Fixed
- **The doctor checks the right Hermes installation.** It resolved the `hermes` binary from
  `PATH`, so in a multi-user setup it read the *other* agent's home and reported
  "could not read capability state" and "plugin not shown as enabled" for an installation
  that was healthy. The home now determines the binary; `PATH` is only the fallback.

### Changed
- The three release surfaces (`pyproject.toml`, `plugin.yaml`, `__about__.py`) now carry a single
  `__about__.py` content in all layers, so the version cannot drift between them.
- The tool-surface decision is now written into the provider request's `tools` list while
  `agent.tools` stays untouched. Hermes' own resolution of hidden tools and the recovery path are
  therefore preserved unchanged.

### Compatibility
- Built and validated against **Hermes v0.22.2**. The compatibility statement is a live
  measurement, not a static claim: the first-turn narrowing was observed in real gateway turns
  (`48 -> 11 tools`).
- Toolshed does not modify the Hermes core. It depends on two documented host surfaces — the
  `llm_request` middleware and the `tools.override` plugin grant. If a future host release changes
  either, the router degrades to the full surface and logs the reason.

## [0.1.7] — 2026-09-01

### Fixed
- Renamed the internal router helper module so the flat Hermes plugin loader
  cannot shadow Hermes' top-level `tools` package.
- Kept the routed tool surface session-sticky when Hermes exposes only the
  `pre_llm_call` compatibility hook.
- Made profile/session agent-reference collisions fail closed instead of
  selecting another live agent.
- Preserved actual tool usage capture in observe-only `shadow` mode.
- Confined doctor profile paths to the documented profile-name format.
- Corrected v2→v2 rollback verification and kept Source/Runtime/flat-loader
  copies aligned.
- Added concrete recovery-schema and Hermes-loader registration coverage.
- Captured the original grant before recovery/narrowing and rejected
  unauthorized expansions fail-closed.
- Made shadow state profile/home-scoped, successful-usage-only, and
  block-aware; aligned all config/template copies and removed invalid template
  frontmatter.
- Scoped the CI path scan to shipped source/documentation instead of ADR
  history, and kept stale pre-built wheels out of release snapshots.
- Made the wheel self-contained with package-relative telemetry imports and
  declared PyYAML as a runtime dependency.
- Made installer and updater `--json` output a valid JSON array for one or
  more profile records.
- Resolved named profiles from Hermes' context-local home and aligned Doctor's
  learning-state writability check with that active home.
- Treated Hermes' per-run `on_session_end` callback as a block boundary while
  preserving the live agent reference across turns; only a no-turn-id close
  flushes the whole session.

## [0.1.6] — 2026-08-24

### Fixed
- **Community install no longer blocked by Hermes' security scanner.**
  A fresh `hermes plugins install Huy3ko/toolshed` was classified DANGEROUS
  (external report, Termux): the scanner scans the whole cloned repo, and
  development artifacts (ADRs mentioning `~/.hermes/.env`, CI `pip install`,
  CONTRIBUTING) plus German comments in `update.sh` referencing `/etc/passwd`
  and `sudo -u` tripped critical/high patterns. Root cause: repo root was used
  as the install surface.

### Changed
- **Distribution v2: runtime payload moved to `runtime/`.** The public install
  command is now:

      hermes plugins install Huy3ko/toolshed/runtime

  Hermes natively supports subdirectory installs and scans only that
  directory. The runtime payload contains plugin.yaml, router code,
  learning/telemetry stores, update.sh, doctor.sh and a `layout_version: 2`
  marker — nothing else. The development tree at repo root is unchanged.
- **Updater hardening** (`runtime/update.sh`), all findings from an adversarial
  helper review, reproduced before fixing:
  - deterministic target context: `--home` → USER_HOME/TARGET_USER/HERMES_BIN
    resolved from ownership, never from caller `$HOME`; fail-closed preflight;
  - `HERMES_HOME` exported explicitly on every privileged call (fixes wrong-
    profile installs when invoked as root);
  - atomic v1→v2 migration: full-tree backup before anything is replaced,
    verified rollback (tree restored, v2 marker absent, ownership intact) with
    rollback failure as its own hard-fail state — no half-states;
  - config merge instead of overwrite: only documented user keys (`enabled`,
    `expansion_mode`, `floor_toolsets`) are carried into the fresh v2 config;
    captured *before* the old tree is replaced;
  - strict profile allowlist `[A-Za-z0-9_-]+` (no path traversal via
    `--profile`);
  - collision-safe backup names (epoch+PID); backups cleaned up under the
    target user's identity;
  - installer success requires exit code AND canonical success tokens, then a
    post-install `layout_version: 2` marker check.
- `update.sh`: updater comments translated to English; privilege drop uses
  `runuser`(root)/`setpriv`(fallback).

### Validation (helper canary, UID 1003, scanner ENABLED)
- Fresh install of `Huy3ko/toolshed/runtime` → scan clean → grant → enable →
  doctor OK → routing smoke OK.
- Migration v0.1.5 full-repo layout → v0.1.6 runtime layout: config, grants,
  enabled/mode preserved; ownership `hermes_helper:hermes_helper`; doctor OK.
- Repeat update v2→v2 green; rollback path exercised and verified.

## [0.1.5] — 2026-08-24

### Fixed
- `update.sh`: Hermes-Binary-Suchkette deckt jetzt beide kanonischen
  Installations-Layouts ab (`~/.hermes/hermes-agent/` git-install und
  `~/src/hermes-agent/` source-install), geordnet nach Layout-Wahrscheinlichkeit.
  Kommentar dokumentiert die Layout-Konvention. Fund: Helper adversarial
  review (2026-08-23), reproduziert von Vela gegen die realen Setups
  hermes_christiane + hermes_helper; zweiter Helper-Review eingearbeitet
  (Suchketten-Reihenfolge, Kommentar-Präzisierung).
- `update.sh`: Erfolgsdetektion des Plugin-Updates präzisiert — matcht auf
  installer-eigene Tokens (`✓ Installed`, `Plugin installed:`) statt breitem
  `Installed`-Substring (falsch-positiv bei "Already installed") und ohne
  Zeilenanfänger-Anker (Installer rahmt Output in Unicode-Boxen, Fund aus
  v0.1.5-Review: `^`-Anker hatte 100% False-Negative-Rate).
- `update.sh`: Multi-User-Bug geschlossen — ohne `--home` wird das Ziel-Home
  jetzt aus dem ZIELUSER (`--user`, via getent) bestimmt statt aus `$HOME`
  des Aufrufers. Bei `root + --user hermes_helper` zeigt TH jetzt korrekt auf
  `/home/hermes_helper/.hermes` statt `/root/.hermes`. Fehlgeschlagene
  Home-Auflösung (unbekannter User, nicht-existierendes Home) führt zu
  hartem Exit 4 mit Hinweis auf `--home` — kein stiller Fallback. Alle 6
  Testfälle der Auflösungsmatrix grün (eigenes Home, root+helper,
  root+christiane, explizites --home gewinnt, unbekannter User, leeres Home).

## [0.1.1] — 2026-08-23

### Fixed
- Root `__about__.py` version synced with `src/toolshed/` (single-source
  versioning pending; pyproject is the source of truth).

### Internal
- ADR-0007 extended: isolation test (2 profiles), uninstall/update findings,
  rollback validated via commit-SHA install.

## [0.1.0] — 2026-08-23

First public-ready release.

### Added
- Adaptive tool-surface routing: first-turn, session-sticky narrowing of
  visible tool schemas (deterministic rules; optional LLM classifier).
- Floor toolsets: critical capabilities (`terminal`, `file`, `skills`,
  `memory`, `web`) are never pruned.
- Dynamic MCP routing: any configured MCP server becomes routable without
  hardcoding (`_build_dynamic_mcp_rules`).
- Monotonic recovery: `request_toolset` re-adds missed capabilities;
  fail-open on router errors.
- Shadow learning bridge: observation → signature → profile store →
  scoring → prediction (never routes; config-gated).
- Profile-scoped state: multi-agent safe by construction.
- Explicit authorization contract: requires
  `hermes plugins enable hermes-token-router --allow-tool-override`.
  Without the grant the plugin stays inactive (fail-closed).

### Validated
- Fresh Hermes upstream install (independent helper agent, different model):
  ~31% input-token reduction on identical tasks vs router-off.
- Earlier controlled paired workloads: 32–70% input reduction.
- Lifecycle: install → enable/grant → routing → persistence → profile
  isolation → update → rollback → uninstall.
- Known limitation: gateway-restart/learning-persistence not yet validated
  in a systemd-user environment.

### Not included (deliberately)
- Capability-index / discovery mechanisms (control runs showed no causal
  benefit over native recovery — see ADR notes and experiment reports).

## [0.1.4] — 2026-08-23

### Added
- `doctor --home <path>` — diagnose foreign Hermes homes (multi-user setups, D4)
- `info` check level in doctor output (neither fail nor warn)
- Robust `.hermes` dir detection via structure markers (plugins/, config.yaml)

### Fixed
- **Multi-user ownership**: update.sh runs all write steps as the target user
  (`--home`/`--user` contract) — root never owns plugin files
- doctor state-dir ownership detection for multi-user installs
- doctor global.enabled consistency check counts only the global block
- doctor plugins-list check uses `--plain` + name-independent matching
- doctor stale-grant check no longer reads the global Hermes config
- Supply-chain hygiene: pinned dev dependencies in CI, unpinned pip refs removed

### Known limitations (unchanged)
- Raw `hermes plugins install --force` can still reset plugin config; use `update.sh`
- Gateway-restart / learning persistence not yet fully validated in systemd-user env

**Validation:** fresh-install canary on upstream Hermes b766607b / v0.20.5 (MiniMax-M3);
multi-user migration on independent runtime (hermes_christiane, own venv + gateway);
all three agents (Vela, Christiane, Helper) productively running v0.1.2→v0.1.4 path.
