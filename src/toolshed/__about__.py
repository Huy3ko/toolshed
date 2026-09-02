"""Single-source version: pyproject.toml is the truth.

The flat Hermes plugin loader imports this module directly (no package), so
we cannot rely on `importlib.metadata` (which needs an installed dist). We
keep ONE literal in this file and derive it everywhere — never maintain a
second copy.
"""
__version__ = "0.1.7"
