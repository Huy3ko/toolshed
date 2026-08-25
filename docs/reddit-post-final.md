**Title:** We kept optimizing Hermes tokens at the wrong layer — so we extended a tool router into something we now use ourselves

For quite a while we were trying to reduce token usage in our Hermes setup.

We tested context-compression approaches such as Headroom and they did save tokens, but for our use case we increasingly felt we were optimizing the wrong layer.

The conversation wasn't always the main problem.
The tool surface was.

With a larger Hermes setup, the model can carry a lot of tool schemas into provider requests even though only a small fraction of those tools are relevant to the current task.

We then tested AtlasOmnia/hermes-tool-router.

It was a very good starting point and, importantly, attacked the problem where we were actually seeing it: before the tool schemas reach the model.

We initially installed it externally as an experimental component.
Then we kept using it.
And while using it with several Hermes agents and longer sessions, we found more and more cases we wanted to solve. Eventually the changes became large enough that we made a properly attributed fork/build of our own:

**Toolshed**
https://github.com/Huy3ko/toolshed

The basic idea is simple:

Same capabilities. Less tool overhead.

![How Toolshed works](docs/toolshed-hero.png)

**1. The agent carries less tool overhead**

Toolshed sits outside the agent core and acts as a proxy for the visible tool surface.

Instead of sending every available tool schema when they aren't all needed:

```
user task
  → Toolshed determines the likely working set
  → relevant toolsets + protected floor stay visible
  → smaller provider request
```

If Toolshed is confident, it narrows.
If it isn't confident, it keeps the full tool surface.

In our tests we measured 24–70% lower input, depending on the workload and tool surface.

A fresh GitHub canary run went from:

15,263 → 8,696 input tokens

about **43% less input** in that particular run.

Those are measurements, not a promise that everyone will save 43%.

**2. The agent should not lose capabilities**

This was one of the things we cared about most.

There are two ways to approach tool-surface reduction: aggressive pruning (cut hard, save more, risk breaking the agent) or conservative narrowing (cut less, keep the agent fully functional). We deliberately chose the second.

Toolshed removes less from the tool list than a harder cutoff would — the savings per run are lower than what an aggressive pruner might show on paper. But the agent keeps full freedom in how it uses its tools: nothing is blocked, nothing is restricted, no capability is hidden behind a permission wall. The agent just carries less irrelevant schema noise.

So the behavior is deliberately conservative:

```
confident        → narrow
not sure         → full surface
needed capability missing later
                 → Hermes request_toolset recovery
                 → continue
```

The goal isn't to remove capabilities.
It's to avoid carrying all of them all the time — without getting in the agent's way.

We also tested adding a new MCP tool after installation. Toolshed picked up the expanded Hermes registry without us manually maintaining a second tool inventory.

**3. Long sessions are where it becomes especially interesting**

Toolshed doesn't constantly reshuffle the tool surface on every turn.

Once a useful baseline surface has been selected for a session, it stays stable unless recovery is
required — and recovery does not require restarting the session: `request_toolset` (explicit) or
automatic middleware recovery add the registered toolset mid-session, and it stays available for
the rest of the session.

That means the provider sees a smaller and more stable prefix:

```
turn 1  → select working surface
turn 2  → same surface
turn 3  → same surface
...
turn 20 → still stable
```

So there are potentially two benefits:
- less tool-schema input
- a more cache-friendly request structure across longer sessions

For us that was more interesting than optimizing only the first request.

**Multi-agent use**

We currently run Toolshed ourselves with multiple Hermes agents.

Each agent keeps its own grants, routing state, session state and learning/telemetry state.

So one agent's workflow data doesn't become another agent's state.

The routing mechanism can be used across several agents without merging their cognition or permissions.

That's important to us because we specifically do not want code, conclusions, prompts or generated assumptions leaking between agents.

**We also ended up treating it like an actual external program**

Once we decided other people might use it, we stopped treating it like one of our internal scripts.

We now have a tested path for:

install → explicit tools.override authorization → doctor → routing smoke → update with config/state preservation → rollback → reinstall

We found several bugs only because we tested this on fresh and multi-user Hermes environments — including ownership problems, update-state resets and security-scanner issues.

Those became fixes instead of local workarounds.

We are now running the released version ourselves, so this has moved from an experiment into actual dogfooding.

**What I would not expect from it**

Toolshed doesn't make the model smarter.
It doesn't guarantee a fixed percentage of savings.
If you only have a handful of tools, the difference may be small.
If a task genuinely needs almost the complete tool surface, Toolshed may deliberately keep almost everything visible.

And we're not claiming a global cross-agent semantic cache or cross-agent learning. Agent-specific state stays isolated.

The sweet spot seems to be: many tools + longer sessions + agents that normally carry much more tool context than the task needs.

We documented the architecture decisions, experiments that worked, and also the ones we rejected.

Repo: https://github.com/Huy3ko/toolshed

MIT licensed.

If anyone here runs Hermes with a fairly large tool/MCP surface, I'd genuinely be interested in seeing an independent before/after run. Different setups and failures would be more useful to us now than another test on our own machines.
