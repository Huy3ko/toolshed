"""Tests fuer die session-gebundene Agent-Registry.

BEFUND 22.10.2026 (Vela): `pre_llm_call` laeuft in Hermes auf einem
Daemon-Worker-Thread (hermes_cli/plugins_dispatch.py:42, 271). Der
Aufrufer-Stack - und damit das `agent`-Local aus
`agent/turn_context.build_turn_context` - ist dort abgeschnitten.
`_get_agent_from_stack()` (runtime/state.py) findet deshalb nichts:

    WARNING hermes_token_router: no agent reference; full set fallback

Der Router verengt die Werkzeugflaeche also NIE. Er routet faktisch nicht.

LOESUNG: Die Agentenidentitaet wird ueber die Session transportiert, nicht
ueber den Stack. Die Registry ist session-gebunden (kein globales
`current_agent`), damit parallele Sessions sich nicht gegenseitig die
Werkzeugauswahl uebernehmen.

VERTRAG:
  1. Zuordnung ist pro Session. Zwei Sessions bekommen nie denselben Agent.
  2. Eine Session mit mehreren Agenten (Neustart) loest NICHT auf ->
     fail-closed, kein Raten.
  3. Ohne Zuordnung: dokumentierter Fallback (kein Raten, kein Fehlalarm
     pro Turn, sondern ein klarer, einmaliger Hinweis).
  4. Der Fallback darf nicht die Werkzeugflaeche anderer Sessions nutzen.
"""
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import state as rt_state  # noqa: E402


class _FakeAgent:
    def __init__(self, session_id, toolsets=("terminal", "file")):
        self.tools = [{"function": {"name": "terminal"}}]
        self.session_id = session_id
        self.enabled_toolsets = list(toolsets)


class SessionBoundRegistry(unittest.TestCase):
    def setUp(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}
        self.addCleanup(self._reset)

    def _reset(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}

    # --- 1) Zuordnung pro Session ------------------------------------------

    def test_two_sessions_get_their_own_agent(self):
        """Zwei Sessions -> zwei Agenten. Kein Uebernehmen."""
        a = _FakeAgent("sess-A")
        b = _FakeAgent("sess-B")
        rt_state._store_agent_ref(a, "sess-A")
        rt_state._store_agent_ref(b, "sess-B")
        self.assertIs(rt_state._get_agent_ref("sess-A"), a)
        self.assertIs(rt_state._get_agent_ref("sess-B"), b)
        self.assertIsNot(rt_state._get_agent_ref("sess-A"),
                         rt_state._get_agent_ref("sess-B"))

    # --- 2) Mehrere Agenten in einer Session -> nicht aufloesbar -----------

    def test_ambiguous_session_resolves_to_none(self):
        """Neustart in derselben Session: zwei Agenten hinterlegt. Dann ist
        die Zuordnung mehrdeutig und wird NICHT geraten."""
        alt = _FakeAgent("sess-A")
        neu = _FakeAgent("sess-A")
        rt_state._store_agent_ref(alt, "sess-A")
        rt_state._store_agent_ref(neu, "sess-A")
        self.assertIsNone(rt_state._get_agent_ref("sess-A"),
                          "mehrdeutige Session wurde aufgeloest - das rät")

    # --- 3) Unbekannte Session -> None -------------------------------------

    def test_unknown_session_resolves_to_none(self):
        self.assertIsNone(rt_state._get_agent_ref("gibt-es-nicht"))

    # --- 4) Nebenlaeufigkeit: parallele Sessions isoliert --------------------

    def test_parallel_sessions_stay_isolated(self):
        """Zehn Threads, je eine Session, je ein eigener Agent. Nach dem
        Lauf muss jede Session genau ihren Agenten liefern."""
        agenten = {}
        fehler = []

        def arbeite(i):
            try:
                sess = f"parallel-{i}"
                agent = _FakeAgent(sess)
                rt_state._store_agent_ref(agent, sess)
                # Lesen aus einem anderen Thread (wie der Worker-Thread):
                for _ in range(20):
                    gefunden = rt_state._get_agent_ref(sess)
                    if gefunden is not agent:
                        fehler.append(f"{sess}: falscher Agent {gefunden!r}")
                        return
            except Exception as exc:  # pragma: no cover
                fehler.append(f"{sess}: {exc!r}")

        threads = [threading.Thread(target=arbeite, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fehler, [], "Nebenlaeufigkeit: Zuordnung nicht stabil")
        for i in range(10):
            sess = f"parallel-{i}"
            agent = rt_state._get_agent_ref(sess)
            self.assertIsNotNone(agent, f"{sess} verschwunden")
            self.assertEqual(agent.session_id, sess)

    def test_concurrent_writes_to_one_session_are_resolved_consistently(self):
        """Gleichzeitige Schreibzugriffe auf DIESELBE Session duerfen keine
        halbfertige Zuordnung erzeugen: entweder der eine oder der andere
        Agent, aber nie ein fremder."""
        a = _FakeAgent("sess-x")
        b = _FakeAgent("sess-x")
        gefunden = []
        stop = threading.Event()

        def schreiber(agent):
            for _ in range(200):
                rt_state._store_agent_ref(agent, "sess-x")

        def leser():
            while not stop.is_set():
                v = rt_state._get_agent_ref("sess-x")
                if v is not None:
                    gefunden.append(v)

        sw = [threading.Thread(target=schreiber, args=(a,)),
              threading.Thread(target=schreiber, args=(b,))]
        lr = threading.Thread(target=leser)
        lr.start()
        for t in sw:
            t.start()
        for t in sw:
            t.join()
        stop.set()
        lr.join()
        for v in gefunden:
            self.assertIn(v, (a, b),
                          "ein fremder Agent wurde fuer sess-x ausgeliefert")

    # --- 5) Lebensdauer: Freigabe -------------------------------------------

    def test_drop_releases_the_session(self):
        """Beim Session-Ende wird die Zuordnung freigegeben - kein
        unbegrenztes Wachstum."""
        agent = _FakeAgent("sess-done")
        rt_state._store_agent_ref(agent, "sess-done")
        self.assertIsNotNone(rt_state._get_agent_ref("sess-done"))
        rt_state._drop_agent_ref("sess-done")
        self.assertIsNone(rt_state._get_agent_ref("sess-done"))

    def test_drop_of_one_session_keeps_the_other(self):
        a = _FakeAgent("sess-1")
        b = _FakeAgent("sess-2")
        rt_state._store_agent_ref(a, "sess-1")
        rt_state._store_agent_ref(b, "sess-2")
        rt_state._drop_agent_ref("sess-1")
        self.assertIsNone(rt_state._get_agent_ref("sess-1"))
        self.assertIs(rt_state._get_agent_ref("sess-2"), b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
