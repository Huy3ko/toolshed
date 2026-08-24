# Changelog

All notable changes to Toolshed are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) (0.x = pre-stable).

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
