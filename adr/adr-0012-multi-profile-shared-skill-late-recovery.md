# ADR-0012: Multi-Profile / Shared-Skill-Source / Late-Recovery-Test

- **Status:** Proposed — not implemented
- **Datum:** 2026-08-26
- **Bereich:** Compatibility / topology / security-boundary validation
- **Bezug:** öffentliche Frage aus r/hermesagent; Vela-X-Research-Notiz

## Kontext

Ein Nutzer beschreibt eine Topologie mit mehreren persistenten Hermes-Profilen,
die dasselbe Git-basierte externe `SKILL.md`-Repository verwenden. Die Profile
haben getrennte Konfigurationen, Memory, Sessions, Grants und lokalen Runtime-
State. Ein geteilter Skill kann später ein Tool benötigen, das nicht in der
initialen Tool-Surface des jeweiligen Profils sichtbar ist.

Diese exakte Kombination wurde bisher **nicht end-to-end validiert**.

## Entscheidung / Arbeitsannahme

Toolshed soll unterhalb der Hermes-Profilentscheidung arbeiten und sie nicht
erzwingen:

```text
shared procedural source
≠ shared runtime state
≠ shared memory
≠ shared permissions
≠ shared agent identity
```

Dabei bleiben die Grenzen getrennt:

- Toolshed routet bzw. begrenzt die sichtbare Tool-Surface.
- Hermes-Grants/Permissions bleiben die Autorisierungsinstanz.
- Registry-Existenz ist keine Berechtigung.
- Recovery eines bekannten Tools ist keine semantische Discovery.
- Ein gemeinsames Skill-Repository ist eine gemeinsame Quelle, aber kein
  gemeinsamer Agent-State.

## Nicht behauptet

Dieser ADR behauptet nicht, dass die beschriebene Topologie bereits
unterstützt oder validiert ist. Insbesondere sind noch nicht bewiesen:

- Installation und Aktivierung von Toolshed pro Profil in genau dieser
  Topologie;
- gemeinsamer Skill-Source bei getrennten Versionsständen;
- Late-Recovery eines zunächst nicht sichtbaren Tools;
- Isolation von Grants, Routing-State, Sessions und Memory;
- Verhalten bei Skill-Update während laufender Sessions;
- Verhalten nach Hermes-/Toolshed-Update.

## Späterer Integrationstest

Erst nach Abschluss des inhaltlichen Harness-Builds und als eigener
Compatibility-/Canary-Zyklus:

```text
pinned shared skill source
→ Profile A / Profile B
→ getrennte initial tool surfaces
→ skill requires initially absent tool
→ bounded recovery
→ no cross-profile surface/state leakage
→ session continuity preserved
```

Zusätzlich separat prüfen:

```text
Hermes authorization/grants
→ Toolshed darf weder ersetzen noch umgehen
```

## Profile vs. Sessions

- Mehrere Sessions: getrennte Arbeitskontexte derselben persistenten
  Agenten-/Profilidentität.
- Mehrere Profile: getrennte persistente Runtime-Grenzen mit eigener
  Konfiguration, Memory, Sessions, Grants und lokalem State.

Toolshed ist nicht der Grund, diese Wahl zu treffen. Er soll beide Topologien
respektieren.

## Gemeinsame Git-Quelle — offene Risiken

Ein gemeinsames Repository ist nicht automatisch unproblematisch:

- Skill-Versionen können zwischen Profilen auseinanderlaufen.
- Ein Update während einer Session kann die Reproduzierbarkeit verändern.
- Unterschiedliche Toolregistries können dieselbe Skill-Anforderung anders
  auflösen.
- Ein mutierbarer Branch ist keine stabile Eingabe.
- Für reproduzierbare Tests müssen Commit/Revision und Update-Zeitpunkt
  festgehalten werden.

## Grenzen des Toolshed

Die Formulierung bleibt ausdrücklich:

> Toolshed is a tool-surface routing and context-efficiency layer, not the
> authorization boundary.

Ob Hermes’ native Recovery in einer konkreten Version und Topologie greift,
muss der Test zeigen. Nicht aus der Architektur folgern.

## Konsequenz

- Kein aktueller Toolshed-Code-Change.
- Keine Compatibility-Matrix vor dem Harness-Fertigstand.
- Dieser ADR wird erst beim späteren Canary-/Release-Zyklus aktiviert.
- Ein späterer PASS muss die exakte Topologie und die verwendete Hermes-/Toolshed-
  Revision nennen.
