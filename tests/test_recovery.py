"""Recovery-/Fail-open-Integrationstests (skizziert; E2E auf frischem Profil
erfolgt manuell nach der Skill-Anleitung hermes-plugin-testing)."""

def test_recovery_tool_schema_shape():
    """Das request_toolset-Schema muss die dokumentierten Felder enthalten."""
    from toolshed.router_tools import build_recovery_tool_schema

    schema = build_recovery_tool_schema({"web", "terminal"})
    parameters = schema["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["toolsets"] == {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "uniqueItems": True,
        "description": parameters["properties"]["toolsets"]["description"],
    }
    assert parameters["properties"]["tool_name"]["type"] == "string"
    assert parameters["properties"]["reason"] == {
        "type": "string",
        "maxLength": 200,
    }


def test_config_defaults_are_generic():
    """ADR-0002: Keine Vela-Pfade/Profilnamen in den Defaults."""
    import pathlib
    cfg = pathlib.Path(__file__).parent.parent / "src" / "toolshed" / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    for banned in ("/srv/", "hermes_hugo", "router-test", "/home/"):
        assert banned not in text, f"ADR-0002 violation: {banned} in config.yaml"
