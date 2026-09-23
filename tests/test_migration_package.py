"""Tests for the first migration package that installs the lifecycle helper."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_package import check_helper  # noqa: E402


class MigrationPackageTests(unittest.TestCase):
    def test_repository_contains_a_separate_updater_addin(self):
        helper = ROOT / "addin" / "STEVEUpdater"
        self.assertTrue((helper / "STEVEUpdater.py").is_file())
        manifest = json.loads((helper / "STEVEUpdater.manifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["type"], "addin")
        self.assertEqual(manifest["autodeskProduct"], "Fusion")
        self.assertEqual(manifest["runOnStartup"], True)

    def test_build_payload_requires_the_helper(self):
        helper = ROOT / "addin" / "STEVEUpdater"
        check_helper(helper)
        self.assertTrue((helper / "STEVEUpdater.py").is_file())
        self.assertTrue((helper / "STEVEUpdater.manifest").is_file())

        missing = ROOT / ".cache" / "missing-steve-updater"
        with self.assertRaisesRegex(RuntimeError, "Missing the STEVEUpdater lifecycle helper"):
            check_helper(missing)

    def test_packaged_steve_payload_contains_the_helper_payload(self):
        helper = ROOT / "addin" / "STEVEUpdater"
        self.assertTrue((helper / "STEVEUpdater.py").is_file())
        self.assertTrue((helper / "STEVEUpdater.manifest").is_file())
        # build_package.py embeds this under STEVE so the existing checksum
        # manifest covers it; the native installer later promotes it to
        # AddIns/STEVEUpdater.
        self.assertTrue(helper.is_dir())

    def test_verifier_maps_archive_entries_to_promoted_install_layout(self):
        verifier = (ROOT / "scripts" / "verify_package.py").read_text(encoding="utf-8")
        self.assertIn('destination / "STEVE" / relative', verifier)
        self.assertIn('destination / "STEVEUpdater" / relative.removeprefix("STEVEUpdater/")', verifier)
        self.assertNotIn('relative.replace("STEVE/STEVEUpdater/", "STEVEUpdater/", 1)', verifier)


if __name__ == "__main__":
    unittest.main()
