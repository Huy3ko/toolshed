# Verification Note: Session Semantics & Recovery Paths (2026-08-25)

## Anlass

Reddit-Frage zur tatsächlichen Bedeutung von "locked" und zu Mid-Session-Recovery:
Was passiert, wenn ein Agent während einer laufenden Session eine Capability braucht,
die nicht in der projizierten Tool-Surface liegt — oder nicht weiß, dass sie existiert?

Anlass für eine Verifizierung war insbesondere die frühere Formulierung, die Surface
sei "locked/fixed for the duration of the session". Diese Formulierung ist **falsch**
und wurde in README/SECURITY.md korrigiert.

## Untersuchte Codepfade

- `src/toolshed/tools.py` — Recovery-Schema, `_ensure_recovery_tool`,
  `_expand_toolset`, `_apply_predicted_tools`
- `src/toolshed/__init__.py` — `request_toolset_handler`,
  `tool_request_middleware`, Plugin-Registrierung

## Codebefund

1. **Baseline-Surface stabil:** Der Router projiziert einmal pro Session (first-turn
   scope); willkürliches Re-Routing mid-session findet statt nur via definierte Pfade.
2. **Expliziter Pfad `request_toolset`:** Vom Shed selbst registriert (Toolset
   `router_recovery`), nach jedem Narrowing wieder angehängt (`_ensure_recovery_tool`).
   Akzeptiert Toolset-Namen oder einen `tool_name`, dessen Owner-Toolset aufgelöst
   wird. `_expand_toolset` mutiert `agent.tools`, `valid_tool_names` und
   `enabled_toolsets` live — dauerhaft für den Rest der Session
   (`state.predicted_toolsets.add`), keine neue Session nötig.
3. **Automatischer Pfad `tool_request_middleware`:** Ruft der Agent ein registriertes,
   aber weggefiltertes Tool direkt auf, expandiert die Middleware dessen Toolset vor
   dem Dispatch; der Original-Call läuft ohne invalid-tool Roundtrip durch. Definierter
   Recovery-Mechanismus, **nicht** explizite Agent-Intention.
4. **Keine Shed-eigene Grant-/Authorization-Prüfung im Eskalationspfad:** Die einzige
   Prüfung zwischen Request und Expansion ist der Registry-Existenzcheck. Unbekannte
   Namen werden fail-closed abgewiesen (Error + difflib-Closest-Matches + Liste der
   verfügbaren Toolsets). "installed ≠ authorized" ist im Eskalationspfad **nicht**
   implementiert; das `tools.override`-Grant wirkt nur auf Hermes-Ebene (Surface-
   manipulation generell), nicht auf einzelne Recovery-Requests.

## Runtime-Verifikation (isoliertes `router-test`-Profil, 2026-08-25)

Testsession `20260825_082539_5d6c11`:

- Startsurface ohne Web-Capability (`narrowed to 1 toolsets: ['mcp-agentmail']`);
  Modell bestätigte, dass keine Such-Tools sichtbar waren.
- Modell rief selbstständig `request_toolset` → Log: `tool request_toolset completed`
  → unmittelbar danach `tool web_search completed (1.37s)` mit korrektem Ergebnis.
- Folgeturn in derselben Session: zweite Websuche erfolgreich ohne erneuten Request —
  Expansion persistiert für die Session.
- Separater Lauf: `request_toolset('zeitmaschine')` → sauber fail-closed
  (`[unknown toolset: zeitmaschine]`), keine Halluzination, kein Crash.

Hauptprofil wurde nicht verändert; alle Läufe auf isoliertem Testprofil.

## Kernergebnis

> **Stable baseline + controlled mid-session recovery. Routing/context efficiency,
> not an authorization boundary.**

## Future Considerations (kein Implementierungsauftrag)

- Optionales `require_grant:`-Flag, falls Toolshed je als Policy-Grenze positioniert
  werden soll (aktuell bewusst nicht).
- Semantischer Discovery-Layer: C-/D-Experimente zeigten keinen messbaren kausalen
  Nutzen passiver Capability-Indizes; Wiederaufnahme nur bei realen Failure-Cases.
