# Reddit-Post Toolshed — Draft

**Subreddit:** r/hermesagent
**Typ:** Technischer Erfahrungsbericht (keine Release-Ankündigung)
**Bild:** Original Toolshed-Diagramm (docs/toolshed-hero.png)
**Sprache:** Englisch (Reddit-Community)

---

## Title

**I built a tool-surface proxy for Hermes that cut input tokens by 43% on a fresh install — it shrinks the tool list before the provider request and recovers missing capabilities on demand**

---

## Body

We've been running multiple Hermes agents on a single VPS for a while now, and one thing kept bothering us: every visible tool ships its full JSON schema with every API call. With 50+ registered tools that's ~67 KB of schema overhead per request before the agent even starts thinking.

We tried context compression tools (Headroom worked, but compressed the wrong layer for us — the conversation, not the tool list). Then we found u/AtlasOmnia's hermes-token-router, which approached the problem from the tool side and was already very well built. We installed it externally, used it, and ended up extending it so far for our own multi-agent use case that we built our own fork: **Toolshed**.

### What it does

Toolshed sits as an external proxy between the agent and the provider request. It classifies the first user turn, narrows the visible tool surface to the likely working set (plus a protected floor that never gets pruned), and keeps every missing capability reachable through Hermes' native `request_toolset` recovery path.

- **Narrow when confident** — smaller provider request
- **Fail open when uncertain** — full tool surface, nothing removed
- **Recover when needed** — missing toolsets are added on demand during the session
- **Multi-agent isolation** — each profile gets its own routing state, grants and learning
- **Explicit authorization** — `tools.override` must be granted; no silent privileges
- **Session-sticky surfaces** — stable prefix = cheap turns (cache hits)

### Measured results

| Setup | Router OFF | Toolshed ON | Reduction |
|---|---:|---:|---:|
| Fresh install (canary) | 15,263 input tokens | 8,696 input tokens | **~43%** |
| Controlled paired workloads | — | — | **24–70%** |

Results depend on tool count and workload. No universal savings guarantee.

### What's been validated

- Fresh install from GitHub on upstream Hermes (b766607b / v0.20.5) with a different model (MiniMax-M3)
- Guided installer with profile detection and explicit grant consent
- State-preserving updates (config, enabled-state and grants survive)
- Rollback to previous release commit
- Multi-user support (independent Unix users with separate Hermes homes)
- Dynamic MCP tool addition — new servers are recognized without manual config patching
- Adversarial testing: prompt injection via repo content, read/write boundary, stale capability handling
- Productive operation across three agents (interactive, family member, batch worker)

### What it does NOT do

- It doesn't make the model smarter — it just removes irrelevant schema noise
- It doesn't guarantee fixed savings — results scale with your tool surface
- Cross-agent learning is not a feature (isolation is, deliberately)

### Repo

https://github.com/Huy3ko/toolshed

MIT, fork of hermes-token-router (attribution included). The repo includes full ADRs documenting every architectural decision, including the mechanisms we tested and rejected (passive capability indexes, activation rules — neither showed causal benefit over the existing recovery path in controlled tests).

---

If anyone here is running Hermes with a larger tool surface, we'd genuinely be interested in a comparison run from an external setup. The installer handles profile detection, grant consent and verification — and `doctor` diagnoses any issues.

---

## Hinweise für Hugo (nicht Teil des Posts):

1. **Flair:** "Technical Help" oder "Discussion" — nicht "Release"
2. **Timing:** Wochenende oder Werktag früh (US-Zeit) für beste Sichtbarkeit
3. **Regel 3 (90/10 Self-Promotion):** Der Post ist als Erfahrungsbericht formuliert, nicht als Werbung — sollte passen
4. **Kommentar-Bereitschaft:** Ich beantworte technische Fragen; du Owner-Fragen
5. **Bild:** Original Toolshed-Diagramm (docs/toolshed-hero.png) — bereits im Draft hochgeladen

---

## Varianten

### Kürzere Alternative (falls der Post zu lang wirkt):

**Title:** gleich

**Body (gekürzt):**

Running multiple Hermes agents on one VPS, we kept hitting the same wall: 50+ tools = ~67 KB of schema JSON per request before the agent even starts.

We tried context compression (Headroom — good, but wrong layer for us), then found u/AtlasOmnia's hermes-token-router. After extending it heavily for our multi-agent setup, we built our own fork: **Toolshed**.

It sits as an external proxy: classifies the first turn, narrows the tool surface to the likely working set, keeps floor tools always loaded, and lets the agent recover missing capabilities via `request_toolset`.

**Measured:** ~43% less input on a fresh install, 24–70% in controlled A/B workloads. No fixed guarantee — scales with tool surface.

**Validated:** fresh install from GitHub, multi-user (independent Unix users), state-preserving updates, rollback, dynamic MCP addition, adversarial prompt-injection testing.

**Doesn't do:** make the model smarter, guarantee fixed savings, cross-agent learning (deliberate).

Repo: https://github.com/Huy3ko/toolshed — MIT, full ADRs included.

If anyone's running a bigger Hermes tool surface, we'd love a comparison from an external setup.

---
