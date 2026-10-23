"""Toolshed version — single source of truth for the installed plugin.

The Hermes directory loader imports this module directly (no package parent),
so ``importlib.metadata`` cannot be relied on to read the version of a
directory plugin. This file therefore holds ONE literal, and the release
process copies it byte-identically into the repo root, ``src/toolshed/`` and
``runtime/``. ``pyproject.toml`` remains the authority for packaged installs;
``tests/test_v017_contracts.py`` asserts that every release surface agrees.
"""
__version__ = "0.2.0"
