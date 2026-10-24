"""Backup paths must stay in sync between update.sh and doctor.sh.

Hermes registers EVERY ``plugin.yaml`` it finds under a scanned plugins root
(``hermes_cli/plugins_discovery.py::scan_directory``). A backup of the plugin
directory that is placed inside that root *as a directory* is therefore
registered as a SECOND instance of the same plugin — measured on Hermes
v0.22.2: two manifests, both keyed ``hermes-token-router``.

The updater avoids that by keeping its backups as sibling FILES next to the
plugin directory. These tests pin both halves of that contract:

  * update.sh and doctor.sh must agree on where the config backup lives.
  * No backup may be created as a directory next to the plugin directory.
  * A directory copy next to the plugin directory would be a second instance
    (behavioural check against the real Hermes discovery code).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO / "update.sh"
DOCTOR_SH = REPO / "doctor.sh"

PLUGIN_NAME = "hermes-token-router"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _hermes_core() -> Path:
    return Path(os.environ.get("HERMES_CORE", os.path.expanduser("~/.hermes/hermes-agent")))


def _scan_manifests(plugins_root: Path):
    """Run the real Hermes plugin discovery over *plugins_root*."""
    core = _hermes_core()
    if core not in sys.path:
        sys.path.insert(0, str(core))
    from hermes_cli.plugins_discovery import scan_directory

    return scan_directory(plugins_root, source="user")


class BackupPathAgreement(unittest.TestCase):
    """Static contract: the two scripts must name the same backup file."""

    def test_update_sh_writes_the_config_backup_next_to_the_plugin_dir(self):
        text = _read(UPDATE_SH)
        match = re.search(r'^\s*BACKUP="\$\{PLUGIN_DIR\}([^"]*)"', text, re.M)
        assert match is not None, (
            "update.sh: no BACKUP= assignment relative to $PLUGIN_DIR found"
        )
        suffix = match.group(1)
        self.assertFalse(
            suffix.startswith("/"),
            "update.sh: the config backup must stay next to the plugin dir",
        )
        self.assertNotIn(
            "/",
            suffix,
            "update.sh: the config backup must not be nested (a nested file is "
            "still safe, a nested *directory* would be scanned as a plugin)",
        )
        self.assertIn(
            "$STAMP",
            suffix,
            "update.sh: the config backup must be stamped (otherwise it is "
            "overwritten by the next run)",
        )

    def test_doctor_sh_looks_for_the_backup_path_update_sh_writes(self):
        update_text = _read(UPDATE_SH)
        doctor_text = _read(DOCTOR_SH)
        match = re.search(r'^\s*BACKUP="\$\{PLUGIN_DIR\}([^"]*)"', update_text, re.M)
        assert match is not None, "update.sh: BACKUP= assignment not found"
        expected = match.group(1).replace("$STAMP", "*")
        self.assertIn(
            expected,
            doctor_text,
            "doctor.sh does not look for the backup path that update.sh writes "
            f"(expected the pattern {expected!r} to appear in doctor.sh)",
        )

    def test_update_sh_never_creates_a_backup_directory(self):
        text = _read(UPDATE_SH)
        offenders = [
            line.strip()
            for line in text.splitlines()
            if re.search(r"mkdir", line) and "preupdate" in line
        ]
        self.assertEqual(
            offenders,
            [],
            "update.sh must not create a backup directory: a directory next to "
            "the plugin directory is scanned by Hermes as a second plugin "
            "instance",
        )

    def test_tree_backup_is_an_archive_file_not_a_directory(self):
        text = _read(UPDATE_SH)
        match = re.search(r'^\s*TREE_BACKUP="([^"]+)"', text, re.M)
        assert match is not None, "update.sh: TREE_BACKUP= assignment not found"
        self.assertTrue(
            match.group(1).endswith(".tgz"),
            "update.sh: the tree backup must be an archive file (.tgz)",
        )


class BackupDirectoryIsASecondPluginInstance(unittest.TestCase):
    """Behavioural proof of the hazard the docs and doctor.sh guard against."""

    @classmethod
    def setUpClass(cls):
        try:
            cls._scan = _scan_manifests
            cls._tmp = Path(tempfile.mkdtemp(prefix="toolshed-scan-"))
            cls._probe(cls._tmp)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"Hermes discovery unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        tmp = getattr(cls, "_tmp", None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _probe(root: Path) -> None:
        plugins = root / "plugins"
        plugin_dir = plugins / PLUGIN_NAME
        plugin_dir.mkdir(parents=True)
        shutil.copy(
            REPO / "runtime" / "plugin.yaml",
            plugin_dir / "plugin.yaml",
        )

    def test_plugin_alone_is_one_manifest(self):
        manifests = _scan_manifests(self._tmp / "plugins")
        self.assertEqual(
            len(manifests),
            1,
            "a single plugin directory must register exactly one manifest",
        )

    def test_backup_directory_beside_the_plugin_is_a_second_manifest(self):
        plugins = self._tmp / "plugins"
        backup_dir = plugins / f"{PLUGIN_NAME}.preupdate.123"
        shutil.copytree(plugins / PLUGIN_NAME, backup_dir)
        try:
            manifests = _scan_manifests(plugins)
            self.assertEqual(
                len(manifests),
                2,
                "a directory copy of the plugin next to it is registered as a "
                "SECOND manifest — this is why backups must be files",
            )
            self.assertEqual(
                [m.key for m in manifests],
                [PLUGIN_NAME, PLUGIN_NAME],
                "both manifests carry the same plugin key",
            )
        finally:
            shutil.rmtree(backup_dir, ignore_errors=True)

    def test_backup_file_beside_the_plugin_is_not_scanned(self):
        plugins = self._tmp / "plugins"
        backup_file = plugins / f"{PLUGIN_NAME}.preupdate.123.tgz"
        backup_file.write_bytes(b"not a real archive")
        try:
            manifests = _scan_manifests(plugins)
            self.assertEqual(
                len(manifests),
                1,
                "a backup FILE next to the plugin directory is not scanned",
            )
        finally:
            backup_file.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
