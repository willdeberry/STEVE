"""In-Fusion updater coordination tests use API stand-ins, never Autodesk Fusion."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addin/STEVE"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"))
from update_core import (  # noqa: E402
    JOURNAL_NAME,
    apply_in_fusion as core_apply_in_fusion,
    swap_staged_addin as core_swap_staged_addin,
    begin_journal,
    clear_journal,
    clear_ready,
    load_journal,
    ready_path,
    recover_journal,
    update_journal,
    version_is_newer,
    write_ready,
    find_steve_program as core_find_steve_program,
)
from steve.live_update import (
    find_steve_program,
    live_update_ready,
    purge_steve_modules,
    read_request,
    request_path,
    write_request,
    apply_in_fusion,
    swap_staged_addin,
)


class Program:
    def __init__(self, name="STEVE", program_id="steve-id", running=True, location=None):
        self.name = name
        self.id = program_id
        self.location = location
        self.folder = location
        self.isValid = True
        self.isRunning = running
        self.stop_calls = 0
        self.run_calls = 0

    def stop(self):
        self.stop_calls += 1
        self.isRunning = False

    def run(self):
        self.run_calls += 1
        self.isRunning = True


class LiveUpdateTests(unittest.TestCase):
    def test_version_ordering_rejects_equal_older_and_invalid_versions(self):
        self.assertTrue(version_is_newer("0.4.9", "0.4.8"))
        self.assertFalse(version_is_newer("0.4.8", "0.4.8"))
        self.assertFalse(version_is_newer("0.4.7", "0.4.8"))
        self.assertFalse(version_is_newer("v0.4.9", "0.4.8"))

    def test_helper_readiness_accepts_fresh_heartbeat_and_rejects_absent_or_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.assertFalse(live_update_ready(home))
            write_ready(home)
            self.assertTrue(live_update_ready(home))
            payload = json.loads(ready_path(home).read_text(encoding="utf-8"))
            payload["timestamp"] = time.time() - 10
            ready_path(home).write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(live_update_ready(home))
            clear_ready(home)
            self.assertFalse(ready_path(home).exists())

    def test_request_is_atomic_and_round_trips_only_staged_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            staged = home / "pending-updates" / "STEVE-0.5.0-macos-arm64"
            staged.mkdir(parents=True)
            (staged / "STEVE").mkdir()
            (staged / "STEVE/STEVE.manifest").write_text('{"version":"0.5.0"}')
            (staged / "STEVE/STEVE.py").write_text("# fixture")
            request = write_request(home, staged, "0.5.0")
            self.assertEqual(request, request_path(home))
            self.assertEqual(read_request(home), {"version": "0.5.0", "package": str(staged.resolve())})
            self.assertFalse(request.with_suffix(".tmp").exists())

    def test_request_rejects_untrusted_or_incomplete_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            outside = Path(temp) / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, "pending-updates"):
                write_request(home, outside, "0.5.0")
            with self.assertRaisesRegex(ValueError, "version"):
                write_request(home, home / "pending-updates" / "STEVE-0.5.0-macos-arm64", "bad")

    def test_finds_exact_steve_program_not_similar_name(self):
        decoy = Program(name="STEVE Helper", program_id="decoy")
        steve = Program(name="STEVE", program_id="expected", location="expected")
        self.assertIs(find_steve_program([decoy, steve], "expected"), steve)
        self.assertIsNone(find_steve_program([decoy], "expected"))

    def test_enum_only_program_is_rejected_without_folder_path(self):
        program = Program(location=0)
        program.folder = None
        self.assertIsNone(find_steve_program([program], "/addins/STEVE"))

        with tempfile.TemporaryDirectory() as temp:
            expected = Path(temp).resolve() / "AddIns" / "STEVE"
            expected.mkdir(parents=True)
            program = Program(location=0)
            program.folder = str(expected)
            self.assertIs(find_steve_program([program], expected), program)

        expected = Path("/addins/STEVE")
        missing = Program()
        mismatched = Program()
        mismatched.folder = "/other/STEVE"
        linked = Program(location=0)
        linked.folder = "/addins/link-to-steve"
        valid = Program(location=0)
        valid.folder = str(expected)
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"
            real.mkdir()
            link = Path(temp) / "link"
            link.symlink_to(real)
            linked.folder = str(link)
            self.assertIsNone(find_steve_program([missing], expected))
            self.assertIsNone(find_steve_program([mismatched], expected))
            self.assertIsNone(find_steve_program([linked], real))
        self.assertIs(find_steve_program([valid], expected), valid)

    def test_parent_symlinks_are_rejected_by_both_identity_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real_parent = root / "real-parent"
            real_parent.mkdir()
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            expected = real_parent / "AddIns" / "STEVE"
            expected.mkdir(parents=True)
            program = Program(location=0)
            program.folder = str(linked_parent / "AddIns" / "STEVE")
            self.assertIsNone(find_steve_program([program], expected))
            self.assertIsNone(core_find_steve_program([program], expected))
            program.folder = str(expected)
            linked_expected = linked_parent / "AddIns" / "STEVE"
            self.assertIsNone(find_steve_program([program], linked_expected))
            self.assertIsNone(core_find_steve_program([program], linked_expected))

    def test_final_and_dangling_symlinks_are_rejected_by_both_identity_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real = root / "real"
            real.mkdir()
            expected = real / "STEVE"
            expected.mkdir()
            linked = root / "linked"
            linked.symlink_to(expected, target_is_directory=True)
            dangling = root / "dangling"
            dangling.symlink_to(root / "missing", target_is_directory=True)
            for folder, target in ((linked, expected), (dangling, expected)):
                program = Program(location=0)
                program.folder = str(folder)
                self.assertIsNone(find_steve_program([program], target))
                self.assertIsNone(core_find_steve_program([program], target))

    def test_read_request_rejects_invalid_version_and_package_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            package = home / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("fixture")
            request = request_path(home)
            request.write_text(json.dumps({"version": "not-a-version", "package": str(package)}))
            with self.assertRaisesRegex(ValueError, "version"):
                read_request(home)
            request.write_text(json.dumps({"version": "0.6.0", "package": str(package)}))
            with self.assertRaisesRegex(ValueError, "version"):
                read_request(home)

    def test_purges_only_steve_modules_and_keeps_unrelated_modules(self):
        modules = {
            "STEVE": object(), "steve.controller": object(), "steve.live_update": object(),
            "steveish": object(), "other": object(),
        }
        purge_steve_modules(modules)
        self.assertEqual(set(modules), {"steveish", "other"})

    def test_swap_keeps_lifecycle_helper_outside_steve(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            (source / "STEVEUpdater").mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            (source / "STEVEUpdater/STEVEUpdater.py").write_text("helper")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "old.py").write_text("old")
            rollback = swap_staged_addin(package, installed, "0.5.0")
            self.assertEqual((installed / "STEVE.py").read_text(), "new")
            self.assertFalse((installed / "STEVEUpdater").exists())
            self.assertTrue(list(installed.parent.glob(".STEVE-rollback-*")))
            rollback()
            self.assertEqual((installed / "old.py").read_text(), "old")

        program = Program(location="/installed")
        modules = {"steve.controller": object(), "other": object()}
        calls = []
        def swap(package, installed, version):
            calls.append((package, installed, version))
            return lambda: calls.append("rollback")
        self.assertEqual(apply_in_fusion([program], Path("/staged"), Path("/installed"), swap, "0.5.0", modules), "0.5.0")
        self.assertEqual(program.stop_calls, 1)
        self.assertEqual(program.run_calls, 1)
        self.assertEqual(calls, [(Path("/staged"), Path("/installed"), "0.5.0")])
        self.assertEqual(set(modules), {"other"})

    def test_staged_extra_file_or_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            import hashlib
            sums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(source.iterdir())]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            (source / "unexpected.txt").write_text("not signed")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "unexpected"):
                core_apply_in_fusion([Program()], package, installed, "0.5.0", {})

    def test_updater_core_is_independent_of_replaceable_steve_package(self):
        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        source = (helper / "STEVEUpdater.py").read_text(encoding="utf-8")
        core = (helper / "update_core.py").read_text(encoding="utf-8")
        self.assertNotIn("from steve.", source)
        self.assertNotIn("import steve", source)
        self.assertNotIn("from steve.", core)
        self.assertNotIn("import steve", core)

    def test_helper_import_does_not_require_replaceable_steve_directory(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        old_path = list(sys.path)
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            sys.path = [entry for entry in sys.path
                        if Path(entry).resolve() != helper.resolve()
                        and Path(entry).resolve() != (helper.parent / "STEVE").resolve()]
            spec = importlib.util.spec_from_file_location("steve_updater_isolated", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path = old_path
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self.assertTrue(module)

    def test_nested_directories_are_allowed_during_staged_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            (source / "steve" / "panel").mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            (source / "steve" / "controller.py").write_text("controller")
            (source / "steve" / "panel" / "index.html").write_text("panel")
            import hashlib
            files = [path for path in sorted(source.rglob("*")) if path.is_file()]
            sums = "\n".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(source).as_posix()}"
                for path in files
            ) + "\n"
            (package / "SHA256SUMS").write_text(sums)
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            program = Program(location=str(installed))
            core_apply_in_fusion([program], package, installed, "0.5.0", {}, installed_version=lambda: "0.5.0")
            self.assertEqual(program.stop_calls, 1)
            self.assertTrue((installed / "steve" / "panel" / "index.html").is_file())

    def test_staged_symlink_is_rejected_before_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            (source / "linked.py").symlink_to(source / "STEVE.py")
            sums = [
                f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}"
                for path in sorted(source.iterdir()) if not path.is_symlink()
            ]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            program = Program()
            with self.assertRaisesRegex(ValueError, "unexpected"):
                core_apply_in_fusion([program], package, installed, "0.5.0", {})
            self.assertEqual(program.stop_calls, 0)

    def test_apply_writes_confirmed_journal_after_new_version_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            sums = [f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(source.iterdir())]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            program = Program(location=str(installed))
            core_apply_in_fusion([program], package, installed, "0.5.0", {}, journal_home=root / "home",
                                 installed_version=lambda: "0.5.0")
            self.assertEqual(load_journal(root / "home")["phase"], "confirmed")

    def test_apply_reacquires_script_after_swap_when_pre_stop_object_is_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            sums = [f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(source.iterdir())]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')

            stale = Program(location=str(installed))
            replacement = Program(location=str(installed), running=False)
            def stale_run():
                stale.run_calls += 1
                stale.isRunning = False
            stale.run = stale_run
            def programs_after_swap():
                return [replacement]

            core_apply_in_fusion(
                [stale], package, installed, "0.5.0", {},
                installed_version=lambda: "0.5.0",
                programs_provider=programs_after_swap,
            )
            self.assertEqual(stale.run_calls, 0)
            self.assertEqual(replacement.run_calls, 1)
            self.assertTrue(replacement.isRunning)

    def test_apply_rolls_back_and_restarts_old_program_when_new_run_fails(self):
        program = Program(location="/installed")
        def broken_run():
            program.run_calls += 1
            program.isRunning = False
        program.run = broken_run
        calls = []
        def swap(package, installed, version):
            calls.append("swap")
            return lambda: calls.append("rollback")
        with self.assertRaisesRegex(RuntimeError, "restart STEVE"):
            apply_in_fusion([program], Path("/staged"), Path("/installed"), swap, "0.5.0", {})
        self.assertEqual(calls, ["swap", "rollback"])
        self.assertEqual(program.stop_calls, 1)
        self.assertEqual(program.run_calls, 2)

    def test_failed_live_update_clears_journal_after_filesystem_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            sums = [f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(source.iterdir())]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "old.py").write_text("old")
            home = root / "home"
            program = Program(location=str(installed))
            def broken_run():
                program.run_calls += 1
                program.isRunning = False
            program.run = broken_run

            with self.assertRaisesRegex(RuntimeError, "restart STEVE"):
                core_apply_in_fusion(
                    [program], package, installed, "0.5.0", {},
                    installed_version=lambda: "0.5.0",
                    journal_home=home,
                )

            self.assertTrue((installed / "old.py").is_file())
            self.assertIsNone(load_journal(home))
            self.assertFalse(recover_journal(home))

    def test_journal_is_atomic_and_survives_phase_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            journal = begin_journal(home, "/staged", "/addins/STEVE", "0.5.0", transaction_id="tx")
            self.assertEqual(journal.name, JOURNAL_NAME)
            self.assertEqual(load_journal(home)["phase"], "validated")
            update_journal(home, "stopping", backup="/backup")
            loaded = load_journal(home)
            self.assertEqual(loaded["transactionId"], "tx")
            self.assertEqual(loaded["backup"], "/backup")
            clear_journal(home)
            self.assertIsNone(load_journal(home))

    def test_recover_journal_clears_stale_failed_transaction_after_newer_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.6"}')
            begin_journal(home, root / "pending", installed, "0.5.3")
            update_journal(
                home,
                "failed",
                backup=str(root / "AddIns/.STEVE-rollback-missing"),
                error="live update failed; recovery is required",
            )

            self.assertTrue(recover_journal(home))
            self.assertIsNone(load_journal(home))
            self.assertTrue((installed / "STEVE.manifest").is_file())

    def test_recover_journal_keeps_failed_transaction_when_version_is_not_newer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.2"}')
            begin_journal(home, root / "pending", installed, "0.5.3")
            update_journal(home, "failed", backup=str(root / "AddIns/.STEVE-rollback-missing"))

            with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                recover_journal(home)
            self.assertIsNotNone(load_journal(home))

    def test_recover_journal_restores_backup_for_unconfirmed_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-test"
            backup.mkdir(parents=True)
            (backup / "old.py").write_text("old")
            begin_journal(home, root / "pending", installed, "0.5.0")
            update_journal(home, "backed_up", backup=str(backup))
            recover_journal(home)
            self.assertTrue((installed / "old.py").is_file())
            self.assertIsNone(load_journal(home))

    def test_recover_journal_clears_confirmed_transaction_without_touching_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "new.py").write_text("new")
            begin_journal(home, root / "pending", installed, "0.5.0")
            update_journal(home, "confirmed", backup=str(root / "AddIns/.STEVE-rollback-test"))
            recover_journal(home)
            self.assertTrue((installed / "new.py").is_file())
            self.assertIsNone(load_journal(home))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            digest = __import__('hashlib').sha256((source / "STEVE.manifest").read_bytes()).hexdigest()
            digest_py = __import__('hashlib').sha256((source / "STEVE.py").read_bytes()).hexdigest()
            (package / "SHA256SUMS").write_text(
                f"{digest}  STEVE.manifest\n{digest_py}  STEVE.py\n"
            )
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "old.py").write_text("old")
            home = root / "home"
            begin_journal(home, package, installed, "0.5.0")
            rollback = core_swap_staged_addin(package, installed, "0.5.0", journal_home=home)
            journal = load_journal(home)
            self.assertEqual(journal["phase"], "backed_up")
            backup = Path(journal["backup"])
            self.assertTrue(backup.is_dir())
            self.assertTrue((installed / "STEVE.py").is_file())
            rollback()

    def test_staged_checksum_tampering_is_rejected_before_swap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            source.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            sums = [f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(source.iterdir())]
            (package / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            (source / "STEVE.py").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                core_apply_in_fusion([Program()], package, root / "AddIns/STEVE", "0.5.0", {})
        program = Program(location="/installed")
        modules = {}
        def swap(package, installed, version):
            return lambda: None
        with self.assertRaisesRegex(RuntimeError, "version"):
            apply_in_fusion([program], Path("/staged"), Path("/installed"), swap, "0.5.0", modules,
                            installed_version=lambda: "0.4.0")


if __name__ == "__main__":
    unittest.main()
