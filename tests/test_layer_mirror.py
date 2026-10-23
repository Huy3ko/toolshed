"""The three payload layers must stay byte-identical.

Toolshed ships the same plugin in three places: the repository root (the flat
Hermes directory-loader payload), ``src/toolshed/`` (the pip package) and
``runtime/`` (the distribution payload that ``plugins install
Huy3ko/toolshed/runtime`` delivers). The Hermes loader imports whichever
copy sits in the plugin directory, so a file that was changed in one layer but
not the others does not fail loudly — it silently serves a different
implementation depending on where the tree was installed from.

That drift is not hypothetical: four divergent copies of the router existed
before this test. A hash comparison of only the files present in *both*
layers cannot see it either, because a file missing from one layer is
invisible to it. The file *lists* are therefore compared as well, and the
shell scripts are included: ``doctor.sh``/``install.sh``/``update.sh`` live in
the root and in ``runtime/`` but not in the pip package.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROOT_LAYER = ROOT
SRC_LAYER = ROOT / "src" / "toolshed"
RUNTIME_LAYER = ROOT / "runtime"

# Files that belong to one layer only and must therefore never be mirrored.
ROOT_ONLY = {
    "pyproject.toml",
    "config.template.yaml",
    "doctor.sh",
    "install.sh",
    "update.sh",
}
SRC_ONLY = set()

# The package layer is the packaged variant; the flat layers carry the
# distribution helper scripts and the layout marker next to the payload.
RUNTIME_ONLY = {"doctor.sh", "install.sh", "update.sh", "layout_version"}

# Every file that is part of the plugin payload proper and must be identical
# in all three layers.
PAYLOAD = [
    "__about__.py",
    "__init__.py",
    "classifier.py",
    "compat.py",
    "config.py",
    "config.yaml",
    "config.template.yaml",
    "diagnostics.py",
    "intent.py",
    "llm_request_routing.py",
    "models.py",
    "plugin.yaml",
    "policy.py",
    "registry_adapter.py",
    "router_tools.py",
    "state.py",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layer_files(layer: Path) -> set[str]:
    """Relative paths of the payload files in one layer (no caches, no packages)."""
    files = set()
    for path in layer.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(layer)
        if rel.parts and rel.parts[0] in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
            continue
        if rel.suffix in {".pyc", ".pyo"}:
            continue
        files.add(str(rel))
    return files


@pytest.mark.parametrize("relative", PAYLOAD)
def test_payload_file_is_byte_identical_in_all_three_layers(relative: str) -> None:
    """A payload file must have the same bytes in root, src/toolshed and runtime."""
    digests = {}
    for name, layer in (
        ("root", ROOT_LAYER),
        ("src", SRC_LAYER),
        ("runtime", RUNTIME_LAYER),
    ):
        path = layer / relative
        assert path.is_file(), f"{relative} is missing in the {name} layer ({layer})"
        digests[name] = _sha256(path)
    assert digests["root"] == digests["src"] == digests["runtime"], (
        f"{relative} differs between layers: {digests}"
    )


def test_no_payload_file_is_missing_from_any_layer() -> None:
    """Every file present in one payload layer must exist in the others.

    A hash comparison over the files common to both layers reports "all equal"
    even when a new file never reached the other layer — the import of that
    layer then fails at load time with a ModuleNotFoundError.

    The comparison is over the *payload* subset only. The repository root
    additionally carries the project documentation (README, CHANGELOG, LICENSE,
    ADR, tests); those are not payload and are deliberately not mirrored.
    """
    src_files = _layer_files(SRC_LAYER)
    runtime_files = _layer_files(RUNTIME_LAYER) - RUNTIME_ONLY
    root_files = _layer_files(ROOT_LAYER) - ROOT_ONLY

    # The payload subset: the plugin files plus the two store packages. The
    # repository tree (docs, tests, ADR, CI) is not payload.
    repo_only_dirs = ("adr", "docs", "tests", "learning", "src", "runtime", ".github")
    payload_root = {
        f for f in root_files
        if "/" not in f or f.startswith(("learning_store/", "telemetry_store/"))
    }
    payload_root = {
        f for f in payload_root
        if not any(f.startswith(d + "/") for d in repo_only_dirs)
    }
    # The root's own documentation files are not payload.
    payload_root -= {
        ".gitignore", "BETRIEBSZUSTAND-20261023.md", "CHANGELOG.md", "CONTRIBUTING.md",
        "LICENSE", "NOTICE", "README.md", "SECURITY.md", "pyproject.toml",
    }

    # The package layer and the distribution payload carry the same file set;
    # config.template.yaml is listed in the setuptools package-data, so it is
    # part of both.
    assert src_files == runtime_files, (
        f"src/toolshed and runtime differ in file list: "
        f"only in src={sorted(src_files - runtime_files)}, "
        f"only in runtime={sorted(runtime_files - src_files)}"
    )
    # The repository root is the flat loader payload and does not carry
    # config.template.yaml (the flat loader never reads it). That asymmetry is
    # asserted explicitly rather than subtracted silently, so that a future
    # change to it is a deliberate, visible decision.
    assert "config.template.yaml" in src_files
    assert "config.template.yaml" in runtime_files
    assert "config.template.yaml" not in payload_root
    assert payload_root == runtime_files - {"config.template.yaml"}, (
        f"repo root and runtime differ in payload file list: "
        f"only in root={sorted(payload_root - (runtime_files - {'config.template.yaml'}))}, "
        f"only in runtime={sorted((runtime_files - {'config.template.yaml'}) - payload_root)}"
    )


def test_shell_helpers_are_byte_identical_in_root_and_runtime() -> None:
    """The bundled installer/updater/doctor are copied into the installed tree."""
    for relative in ("doctor.sh", "install.sh", "update.sh"):
        root_path = ROOT_LAYER / relative
        runtime_path = RUNTIME_LAYER / relative
        assert root_path.is_file(), f"{relative} is missing in the repo root"
        assert runtime_path.is_file(), f"{relative} is missing in runtime/"
        assert _sha256(root_path) == _sha256(runtime_path), f"{relative} differs between root and runtime"


def test_layout_version_marker_exists_only_in_the_runtime_layer() -> None:
    """``layout_version`` marks the distributed payload; it is not part of the package."""
    assert (RUNTIME_LAYER / "layout_version").is_file()
    assert not (ROOT_LAYER / "layout_version").exists()
    assert not (SRC_LAYER / "layout_version").exists()
