"""In-Fusion updater coordination tests use API stand-ins, never Autodesk Fusion."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addin/STEVE"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"))
import update_core as core_module  # noqa: E402
import steve.live_update as live_update_module  # noqa: E402
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
    prepare_in_fusion,
    confirm_in_fusion,
    rollback_in_fusion,
    confirm_rollback,
    write_startup_ack,
    write_confirmed,
    confirmed_matches,
    confirmed_commit_verified,
)
from steve.live_update import (
    acknowledge_startup,
    find_steve_program,
    live_update_ready,
    purge_steve_modules,
    read_request,
    request_path,
    write_request,
    apply_in_fusion,
    swap_staged_addin,
)


_real_begin_journal = begin_journal
_real_write_confirmed = write_confirmed
_real_update_journal = update_journal


def _fixture_inventory(path, fallback="fixture.py"):
    path = Path(path)
    if path.is_dir():
        inventory = core_module._tree_inventory(path)
        if inventory:
            return inventory
    return {fallback: "0" * 64}


def begin_journal(home, package, installed, expected_version, **kwargs):
    installed_tree = _fixture_inventory(installed)
    package_tree = _fixture_inventory(Path(package) / "STEVE", fallback="STEVE.manifest")
    kwargs.setdefault("expected_tree", package_tree)
    kwargs.setdefault("previous_tree", installed_tree)
    return _real_begin_journal(home, package, installed, expected_version, **kwargs)


def update_journal(home, phase, **changes):
    if phase in {"backed_up", "rollback_restoring", "rollback_pending"}:
        backup = changes.get("backup")
        if backup and Path(backup).is_dir():
            changes["previousTree"] = _fixture_inventory(backup)
        if phase in {"rollback_restoring", "rollback_pending"}:
            changes.setdefault("rollbackStartRequested", False)
    elif phase == "failed":
        backup = changes.get("backup")
        if backup and Path(backup).is_dir():
            changes.setdefault("previousTree", _fixture_inventory(backup))
    return _real_update_journal(home, phase, **changes)


def write_confirmed(home, transaction):
    transaction = dict(transaction)
    installed = Path(transaction["installed"])
    transaction.setdefault("expectedTree", _fixture_inventory(installed))
    transaction.setdefault("previousTree", _fixture_inventory(installed, fallback="previous.py"))
    return _real_write_confirmed(home, transaction)


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
    def test_validate_installed_tree_rejects_missing_expected_file(self):
        with tempfile.TemporaryDirectory() as temp:
            installed = Path(temp) / "STEVE"
            installed.mkdir()
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("ok")
            expected = {"STEVE.manifest": "a" * 64, "STEVE.py": "b" * 64, "steve/extra.py": "c" * 64}
            self.assertFalse(core_module._validate_installed_tree(installed, "0.5.0", expected))

    def test_validate_installed_tree_rejects_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            installed = Path(temp).resolve() / "STEVE"
            installed.mkdir()
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("ok")
            expected = core_module._tree_inventory(installed)
            expected["STEVE.py"] = "0" * 64
            self.assertFalse(core_module._validate_installed_tree(installed, "0.5.0", expected))

    def test_rollback_reconstruction_rejects_non_boolean_start_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            journal = begin_journal(home, home / "pkg", home / "STEVE", "0.5.0")
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload.update({"phase": "rollback_pending", "previousVersion": "0.4.0",
                            "backup": str(home / ".STEVE-rollback-x"),
                            "rollbackStartRequested": "false"})
            journal.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_journal(home)

    def test_startup_ack_rejects_journal_with_invalid_identity_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            journal = begin_journal(home, home / "pkg", home / "STEVE", "0.5.0")
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload["startAttemptId"] = "not-an-id"
            journal.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(live_update_module.acknowledge_startup(home))
            self.assertFalse((home / "pending-updates" / "live-update-startup.json").exists())

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
            if os.name == "nt":
                self.assertFalse(live_update_ready(home))
            else:
                self.assertTrue(live_update_ready(home))
            payload = json.loads(ready_path(home).read_text(encoding="utf-8"))
            payload["timestamp"] = time.time() - 10
            ready_path(home).write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(live_update_ready(home))
            clear_ready(home)
            self.assertFalse(ready_path(home).exists())

    def test_clear_ready_syncs_parent_directory_after_unlink(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            write_ready(home)
            with patch.object(core_module, "_sync_directory") as sync_directory:
                clear_ready(home)
            self.assertFalse(ready_path(home).exists())
            sync_directory.assert_called_once_with(ready_path(home).parent)

    def test_readiness_rejects_fresh_heartbeat_when_recovery_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            write_ready(home)
            journal = home / "pending-updates" / JOURNAL_NAME
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"phase": "recovery_blocked"}), encoding="utf-8")
            self.assertFalse(live_update_ready(home))

    def test_readiness_rejects_fresh_heartbeat_when_journal_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            write_ready(home)
            journal = home / "pending-updates" / JOURNAL_NAME
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text("not-json", encoding="utf-8")
            self.assertFalse(live_update_ready(home))

    def test_readiness_rejects_blocked_journal_even_when_heartbeat_remains(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            write_ready(home)
            journal = home / "pending-updates" / JOURNAL_NAME
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"phase": "recovery_blocked"}), encoding="utf-8")
            self.assertTrue(ready_path(home).exists())
            self.assertFalse(live_update_ready(home))

    def test_readiness_rejects_malformed_request_with_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            write_ready(home)
            request = home / "pending-updates" / "live-update.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text('{"version":"0.5.0"}', encoding="utf-8")
            self.assertFalse(live_update_ready(home))

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
            self.assertEqual(read_request(home)["version"], "0.5.0")
            self.assertEqual(read_request(home)["package"], str(staged.resolve()))
            self.assertRegex(read_request(home)["requestId"], r"^[0-9a-f]{32}$")
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
            request.write_text(json.dumps({"requestId": "a" * 32, "version": "not-a-version", "package": str(package)}))
            with self.assertRaisesRegex(ValueError, "version"):
                read_request(home)
            request.write_text(json.dumps({"requestId": "b" * 32, "version": "0.6.0", "package": str(package)}))
            with self.assertRaisesRegex(ValueError, "version"):
                read_request(home)

    def test_purges_only_steve_modules_and_keeps_unrelated_modules(self):
        modules = {
            "STEVE": object(), "steve.controller": object(), "steve.live_update": object(),
            "steveish": object(), "other": object(),
        }
        purge_steve_modules(modules)
        self.assertEqual(set(modules), {"steveish", "other"})

    def test_sync_tree_handles_copytree_preserved_read_only_files_on_windows(self):
        if os.name != "nt":
            self.skipTest("copytree read-only durability contract is Windows-specific")
        import shutil
        import stat

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            source_file = source / "read-only.bin"
            source_file.write_bytes(b"durability regression")
            source_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            shutil.copytree(source, destination)
            destination_file = destination / source_file.name
            before_bytes = destination_file.read_bytes()
            before_mode = stat.S_IMODE(destination_file.stat().st_mode)
            core_module._sync_tree(destination)
            self.assertEqual(destination_file.read_bytes(), before_bytes)
            self.assertEqual(stat.S_IMODE(destination_file.stat().st_mode), before_mode)

    def test_sync_tree_propagates_regular_file_fsync_failure(self):
        import stat

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            destination = root / "destination"
            destination.mkdir()
            destination_file = destination / "payload.bin"
            destination_file.write_bytes(b"must remain unchanged")
            before_mode = stat.S_IMODE(destination_file.stat().st_mode)
            real_fsync = core_module.os.fsync

            def fail_regular_file_fsync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("injected regular-file fsync failure")
                return real_fsync(fd)

            with patch.object(core_module.os, "fsync", side_effect=fail_regular_file_fsync):
                with self.assertRaisesRegex(OSError, "injected regular-file fsync failure"):
                    core_module._sync_tree(destination)
            self.assertEqual(destination_file.read_bytes(), b"must remain unchanged")
            self.assertEqual(stat.S_IMODE(destination_file.stat().st_mode), before_mode)

    def test_swap_restores_previous_installation_after_sync_failure(self):
        import hashlib

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            installed = root / "AddIns" / "STEVE"
            source.mkdir(parents=True)
            installed.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            sums = "\n".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
                for path in sorted(source.iterdir())
            ) + "\n"
            (package / "SHA256SUMS").write_text(sums)
            with patch.object(core_module, "_sync_tree", side_effect=OSError("sync barrier failed")):
                with self.assertRaisesRegex(OSError, "sync barrier failed"):
                    core_swap_staged_addin(package, installed, "0.5.0")
            self.assertEqual((installed / "STEVE.manifest").read_text(), '{"version":"0.4.0"}')
            self.assertEqual((installed / "STEVE.py").read_text(), "old")
            self.assertFalse(list(installed.parent.glob(".STEVE-rollback-*")))

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

    def test_update_event_advances_existing_transaction_while_journal_blocks_new_requests(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location("steve_updater_bridge_test", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            home = Path(tempfile.mkdtemp())
            try:
                begin_journal(home, "/staged", "/addins/STEVE", "0.5.0", request_id="e" * 32)
                module.data_home = lambda: home
                module._pending_transaction = {"transactionId": "tx-existing"}
                module._rollback_transaction = None
                with patch.object(module, "_apply_pending") as apply_pending, \
                     patch.object(module, "_clear_ready_safely") as clear_ready_safely, \
                     patch.object(module, "write_ready") as write_ready:
                    module.UpdateEvent().notify(None)
                apply_pending.assert_called_once_with()
                clear_ready_safely.assert_not_called()
                write_ready.assert_not_called()
            finally:
                import shutil
                shutil.rmtree(home)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_successful_rollback_ack_clears_pending_and_rollback_transactions(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location("steve_updater_rollback_idle_test", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            home = Path(tempfile.mkdtemp())
            try:
                transaction = {"requestId": "f" * 32, "previousVersion": "0.4.0"}
                module.data_home = lambda: home
                module._pending_transaction = {"requestId": "f" * 32}
                module._rollback_transaction = transaction
                module._recovery_blocked = False
                with patch.object(module, "confirm_rollback", return_value="0.4.0") as confirm, \
                     patch.object(module, "_log"), \
                     patch.object(module, "_clear_ready_safely"):
                    module._apply_pending()
                    module._apply_pending()
                confirm.assert_called_once()
                self.assertIsNone(module._pending_transaction)
                self.assertIsNone(module._rollback_transaction)
            finally:
                import shutil
                shutil.rmtree(home)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_helper_reconstructs_rollback_pending_with_immutable_inventories(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location("steve_updater_reconstruct_test", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                home = root / "home"
                package = root / "pending" / "STEVE-0.5.0" / "STEVE"
                installed = root / "AddIns" / "STEVE"
                backup = root / "AddIns" / ".STEVE-rollback-test"
                for tree, version, body in ((package, "0.5.0", "new"), (backup, "0.4.0", "old")):
                    tree.mkdir(parents=True)
                    (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                    (tree / "STEVE.py").write_text(body)
                expected_tree = core_module._tree_inventory(package)
                previous_tree = core_module._tree_inventory(backup)
                _real_begin_journal(
                    home, package.parent, installed, "0.5.0", request_id="a" * 32,
                    expected_tree=expected_tree, previous_tree=previous_tree,
                )
                _real_update_journal(
                    home, "rollback_pending", previousVersion="0.4.0", backup=str(backup),
                    rollbackStartRequested=False,
                )
                module.data_home = lambda: home
                self.assertTrue(module._reconstruct_rollback_from_journal())
                self.assertEqual(module._rollback_transaction["expectedTree"], expected_tree)
                self.assertEqual(module._rollback_transaction["previousTree"], previous_tree)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_same_session_prepare_failure_reconstructs_actionable_rollback(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location("steve_updater_same_session_failure_test", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                home = root / "home"
                package = root / "pending" / "STEVE-0.5.0" / "STEVE"
                backup = root / "AddIns" / ".STEVE-rollback-test"
                for tree, version, body in ((package, "0.5.0", "new"), (backup, "0.4.0", "old")):
                    tree.mkdir(parents=True)
                    (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                    (tree / "STEVE.py").write_text(body)
                expected_tree = core_module._tree_inventory(package)
                previous_tree = core_module._tree_inventory(backup)
                module.data_home = lambda: home
                module._pending_transaction = None
                module._rollback_transaction = None
                module._confirm_attempts = 0
                module._recovery_blocked = False
                request = {
                    "requestId": "b" * 32,
                    "version": "0.5.0",
                    "package": str(package.parent),
                }
                def prepare_then_fail(*args, **kwargs):
                    _real_begin_journal(
                        home, package.parent, root / "AddIns" / "STEVE", "0.5.0",
                        request_id="b" * 32, expected_tree=expected_tree,
                        previous_tree=previous_tree,
                    )
                    _real_update_journal(
                        home, "rollback_pending", previousVersion="0.4.0",
                        backup=str(backup), rollbackStartRequested=False,
                    )
                    raise RuntimeError("prepare failed after durable journal")
                with patch.object(module, "read_request", return_value=request), \
                     patch.object(module, "_installed_version", return_value="0.4.0"), \
                     patch.object(module, "_app", return_value=types.SimpleNamespace(
                         scripts=types.SimpleNamespace(itemsByName=lambda name: [object()]))), \
                     patch.object(module, "prepare_in_fusion", side_effect=prepare_then_fail), \
                     patch.object(module, "_clear_ready_safely") as clear_ready, \
                     patch.object(module, "_log"):
                    module._apply_pending()
                clear_ready.assert_called_once_with()
                self.assertIsNotNone(module._rollback_transaction)
                self.assertEqual(module._rollback_transaction["expectedTree"], expected_tree)
                self.assertEqual(module._rollback_transaction["previousTree"], previous_tree)
                self.assertEqual(module._rollback_transaction["phase"], "rollback_pending")
                self.assertFalse(module._recovery_blocked)
                self.assertEqual(load_journal(home)["phase"], "rollback_pending")
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_successful_helper_startup_clears_revocation_marker(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        home = None
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location(
                "steve_updater_successful_start", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            home = Path(tempfile.mkdtemp())
            core_module.revoke_ready(home)
            self.assertTrue((home / "pending-updates" / "steve-updater-ready-revoked.json").is_file())

            if os.name == "nt":
                with patch.object(module, "_clear_ready_safely") as clear_ready:
                    module.run(None)
                self.assertTrue(module._recovery_blocked)
                clear_ready.assert_called_once_with()
                self.assertFalse(module._event_handler)
                self.assertFalse(module._worker)
                return

            class Event:
                def add(self, handler):
                    pass
                def remove(self, handler):
                    pass

            app = types.SimpleNamespace(
                registerCustomEvent=lambda name: Event(),
                unregisterCustomEvent=lambda name: None,
            )
            module.data_home = lambda: home
            module._app = lambda: app
            module.recover_journal = lambda path: False
            module.load_journal = lambda path: None
            with patch.object(module.threading, "Thread") as thread:
                module.run(None)
                thread.return_value.start.assert_called_once()
            self.assertFalse((home / "pending-updates" / "steve-updater-ready-revoked.json").exists())
            self.assertFalse(module._recovery_blocked)
        finally:
            import shutil
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_readiness_revocation_blocks_surviving_heartbeat_when_clear_fails(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        fake_adsk = types.ModuleType("adsk")
        fake_core = types.ModuleType("adsk.core")
        fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
        fake_adsk.core = fake_core
        previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
        home = None
        try:
            sys.modules["adsk"] = fake_adsk
            sys.modules["adsk.core"] = fake_core
            spec = importlib.util.spec_from_file_location(
                "steve_updater_revocation", helper / "STEVEUpdater.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            home = Path(tempfile.mkdtemp())
            write_ready(home)
            module.data_home = lambda: home
            with patch.object(module, "clear_ready", side_effect=OSError("heartbeat removal failed")):
                module._fail_startup_safely()
            self.assertTrue(ready_path(home).is_file())
            revoked = home / "pending-updates" / "steve-updater-ready-revoked.json"
            self.assertTrue(revoked.is_file())
            self.assertTrue(module._recovery_blocked)
            self.assertFalse(live_update_ready(home))
        finally:
            import shutil
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_helper_startup_failures_withdraw_readiness_and_cleanup_partial_state(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        for failure_stage in ("register", "add", "start"):
            fake_adsk = types.ModuleType("adsk")
            fake_core = types.ModuleType("adsk.core")
            fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
            fake_adsk.core = fake_core
            previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
            try:
                sys.modules["adsk"] = fake_adsk
                sys.modules["adsk.core"] = fake_core
                spec = importlib.util.spec_from_file_location(
                    "steve_updater_startup_failure_" + failure_stage,
                    helper / "STEVEUpdater.py",
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                home = Path(tempfile.mkdtemp())
                event = types.SimpleNamespace(
                    add=(lambda handler: (_ for _ in ()).throw(RuntimeError("add failed")))
                    if failure_stage == "add" else (lambda handler: None),
                    remove=lambda handler: None,
                )
                app = types.SimpleNamespace(
                    registerCustomEvent=(lambda name: (_ for _ in ()).throw(RuntimeError("register failed")))
                    if failure_stage == "register" else (lambda name: event),
                    unregisterCustomEvent=lambda name: None,
                )
                module.data_home = lambda: home
                module._app = lambda: app
                module.recover_journal = lambda path: False
                module.load_journal = lambda path: None
                module._clear_ready_safely = Mock()
                if failure_stage == "start":
                    class FailingThread:
                        def __init__(self, *args, **kwargs):
                            pass
                        def start(self):
                            raise RuntimeError("start failed")
                        def join(self, timeout=None):
                            pass
                    thread_patch = patch.object(module.threading, "Thread", FailingThread)
                else:
                    thread_patch = patch.object(module.threading, "Thread")
                with thread_patch as thread:
                    if failure_stage != "start":
                        thread.return_value.start.side_effect = RuntimeError("start failed") if failure_stage == "add" else None
                    module.run(None)
                self.assertTrue(module._recovery_blocked)
                module._clear_ready_safely.assert_called()
                self.assertIsNone(module._custom_event)
                self.assertIsNone(module._worker)
            finally:
                import shutil
                shutil.rmtree(locals().get("home", Path(tempfile.mkdtemp())), ignore_errors=True)
                for name, value in previous.items():
                    if value is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value

    def test_helper_startup_failure_observables_preserve_journal_and_cleanup(self):
        import importlib.util
        import types

        helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
        for failure_stage in ("register", "add", "start"):
            fake_adsk = types.ModuleType("adsk")
            fake_core = types.ModuleType("adsk.core")
            fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
            fake_adsk.core = fake_core
            previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
            home = None
            try:
                sys.modules["adsk"] = fake_adsk
                sys.modules["adsk.core"] = fake_core
                spec = importlib.util.spec_from_file_location(
                    "steve_updater_observable_" + failure_stage, helper / "STEVEUpdater.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                home = Path(tempfile.mkdtemp()).resolve()
                package = home / "pending-updates" / "STEVE-0.5.0"
                installed = home / "AddIns" / "STEVE"
                backup = home / "AddIns" / ".STEVE-rollback-test"
                for tree, version, body in ((package / "STEVE", "0.5.0", "new"),
                                             (backup, "0.4.0", "old")):
                    tree.mkdir(parents=True)
                    (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                    (tree / "STEVE.py").write_text(body)
                expected_tree = core_module._tree_inventory(package / "STEVE")
                previous_tree = core_module._tree_inventory(backup)
                _real_begin_journal(
                    home, package, installed, "0.5.0", request_id="e" * 32,
                    expected_tree=expected_tree, previous_tree=previous_tree)
                _real_update_journal(
                    home, "rollback_pending", previousVersion="0.4.0",
                    backup=str(backup), rollbackStartRequested=False)
                journal_path = home / "pending-updates" / JOURNAL_NAME
                journal_bytes = journal_path.read_bytes()
                write_ready(home)

                removed = []
                registered = []
                added = []
                unregistered = []
                joined = []
                stopped = []
                class Event:
                    def add(self, handler):
                        added.append(True)
                        if failure_stage == "add":
                            raise RuntimeError("add failed")
                    def remove(self, handler):
                        removed.append(True)
                        if failure_stage == "add":
                            raise RuntimeError("remove failed")
                event = Event()
                def register(name):
                    registered.append(True)
                    if failure_stage == "register":
                        raise RuntimeError("register failed")
                    return event
                app = types.SimpleNamespace(
                    registerCustomEvent=register,
                    unregisterCustomEvent=lambda name: unregistered.append(True),
                )
                module.data_home = lambda: home
                if os.name == "nt":
                    with patch.object(module, "_clear_ready_safely") as clear_ready:
                        module.run(None)
                    self.assertTrue(module._recovery_blocked)
                    clear_ready.assert_called_once_with()
                    self.assertEqual(journal_path.read_bytes(), journal_bytes)
                    self.assertFalse(registered)
                    self.assertFalse(added)
                    self.assertFalse(unregistered)
                    continue
                module._app = lambda: app
                module.recover_journal = lambda path: False
                thread = types.SimpleNamespace(
                    start=lambda: (_ for _ in ()).throw(RuntimeError("start failed")),
                    join=lambda timeout=None: joined.append(timeout),
                )
                with patch.object(module.threading, "Thread", lambda *args, **kwargs: thread):
                    module.run(None)
                self.assertTrue(module._recovery_blocked)
                self.assertFalse(ready_path(home).exists())
                self.assertEqual(journal_path.read_bytes(), journal_bytes)
                self.assertEqual(len(unregistered), 1)
                if failure_stage in {"add", "start"}:
                    self.assertGreaterEqual(
                        len(removed), 1,
                        f"{failure_stage}: custom={module._custom_event!r} handler={module._event_handler!r}",
                    )
                if failure_stage == "start":
                    self.assertGreaterEqual(len(joined), 1)
            finally:
                import shutil
                if home is not None:
                    shutil.rmtree(home, ignore_errors=True)
                for name, value in previous.items():
                    if value is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value

        import importlib.util
        import types

        home = Path(tempfile.mkdtemp())
        try:
            write_ready(home)
            with patch.object(live_update_module.os, "name", "nt"):
                self.assertFalse(live_update_ready(home))

            helper = Path(__file__).resolve().parents[1] / "addin/STEVEUpdater"
            fake_adsk = types.ModuleType("adsk")
            fake_core = types.ModuleType("adsk.core")
            fake_core.CustomEventHandler = type("CustomEventHandler", (), {})
            fake_adsk.core = fake_core
            previous = {name: sys.modules.get(name) for name in ("adsk", "adsk.core")}
            try:
                sys.modules["adsk"] = fake_adsk
                sys.modules["adsk.core"] = fake_core
                spec = importlib.util.spec_from_file_location("steve_updater_windows_test", helper / "STEVEUpdater.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with patch.object(module.os, "name", "nt"), patch.object(module, "_clear_ready_safely") as clear_ready:
                    module.run(None)
                self.assertTrue(module._recovery_blocked)
                clear_ready.assert_called_once_with()
            finally:
                for name, value in previous.items():
                    if value is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value
        finally:
            import shutil
            shutil.rmtree(home)

    def test_swap_rejects_captured_inventory_mismatch_before_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            source = package / "STEVE"
            installed = root / "AddIns" / "STEVE"
            source.mkdir(parents=True)
            installed.mkdir(parents=True)
            (source / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (source / "STEVE.py").write_text("new")
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            sums = "\n".join(
                f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}"
                for path in sorted(source.iterdir())
            ) + "\n"
            (package / "SHA256SUMS").write_text(sums)
            expected_tree = core_module._tree_inventory(source)
            previous_tree = core_module._tree_inventory(installed)
            previous_tree["STEVE.py"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "inventory"):
                core_swap_staged_addin(
                    package, installed, "0.5.0", expected_tree=expected_tree,
                    previous_tree=previous_tree,
                )
            self.assertTrue(installed.is_dir())
            self.assertFalse(list(installed.parent.glob(".STEVE-rollback-*")))

    def test_rollback_rejects_stale_journal_binding_before_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-stale"
            installed.mkdir(parents=True)
            backup.mkdir(parents=True)
            for tree, version, body in ((installed, "0.5.0", "new"), (backup, "0.4.0", "old")):
                (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                (tree / "STEVE.py").write_text(body)
            expected_tree = core_module._tree_inventory(installed)
            previous_tree = core_module._tree_inventory(backup)
            begin_journal(
                home, root / "pending" / "STEVE-0.5.0", installed, "0.5.0",
                request_id="a" * 32, expected_tree=expected_tree,
                previous_tree=previous_tree,
            )
            update_journal(home, "rollback_pending", backup=str(backup), previousVersion="0.4.0")
            transaction = dict(load_journal(home))
            transaction["requestId"] = "b" * 32
            program = Program(location=str(installed), running=True)
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                rollback_in_fusion(transaction, lambda: [program], journal_home=home)
            self.assertEqual(program.stop_calls, 0)
            self.assertTrue(installed.is_dir())
            self.assertTrue(backup.is_dir())

    def test_confirm_rollback_rejects_changed_attempt_state_before_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            previous_tree = core_module._tree_inventory(installed)
            expected_tree = {"STEVE.py": "1" * 64}
            begin_journal(
                home, root / "pending" / "STEVE-0.5.0", installed, "0.5.0",
                request_id="c" * 32, expected_tree=expected_tree,
                previous_tree=previous_tree,
            )
            update_journal(
                home, "rollback_pending", previousVersion="0.4.0",
                rollbackStartRequested=True, startAttemptId="d" * 32,
            )
            transaction = dict(load_journal(home))
            transaction["rollbackStartRequested"] = False
            program = Program(location=str(installed), running=False)
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                confirm_rollback(transaction, lambda: [program], "0.4.0", journal_home=home)
            self.assertEqual(program.run_calls, 0)

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
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
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

    def test_prepare_in_fusion_stops_and_swaps_without_starting_until_next_callback(self):
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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            home = root / "home"

            transaction = prepare_in_fusion(
                [program], package, installed, "0.5.0", journal_home=home,
            )

            self.assertEqual(transaction["phase"], "starting")
            self.assertEqual(program.stop_calls, 1)
            self.assertEqual(program.run_calls, 0)
            self.assertEqual(load_journal(home)["phase"], "starting")
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                confirm_in_fusion(
                    transaction, lambda: [program], installed_version=lambda: "0.5.0",
                    journal_home=home, modules={},
                )
            write_startup_ack(home, transaction["transactionId"], transaction["expectedVersion"], transaction["installed"])

            confirm_in_fusion(
                transaction, lambda: [program], installed_version=lambda: "0.5.0",
                journal_home=home, modules={},
            )
            self.assertEqual(program.run_calls, 1)
            self.assertIsNone(load_journal(home))
            self.assertTrue((home / "pending-updates" / "live-update-confirmed.json").is_file())

    def test_confirmation_requires_transaction_bound_startup_ack(self):
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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            home = root / "home"
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)
            program.isRunning = True

            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                confirm_in_fusion(
                    transaction, lambda: [program], installed_version=lambda: "0.5.0",
                    journal_home=home, modules={},
                )
            self.assertEqual(load_journal(home)["phase"], "starting")

            write_startup_ack(home, transaction["transactionId"], transaction["expectedVersion"], transaction["installed"])
            confirm_in_fusion(
                transaction, lambda: [program], installed_version=lambda: "0.5.0",
                journal_home=home, modules={},
            )
            self.assertIsNone(load_journal(home))
            self.assertTrue((home / "pending-updates" / "live-update-confirmed.json").is_file())

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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            home = root / "home"
            with self.assertRaisesRegex(RuntimeError, "legacy synchronous update path"):
                core_apply_in_fusion([program], package, installed, "0.5.0", {}, journal_home=home,
                                     installed_version=lambda: "0.5.0")
            self.assertIsNone(load_journal(home))

    def test_confirm_can_retry_on_later_callback_until_running(self):
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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            attempts = [0]

            def delayed_run():
                attempts[0] += 1
                program.run_calls += 1
                program.isRunning = attempts[0] > 1

            program.run = delayed_run
            home = root / "home"
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)

            with self.assertRaisesRegex(RuntimeError, "restart STEVE"):
                confirm_in_fusion(
                    transaction, lambda: [program],
                    installed_version=lambda: "0.5.0", journal_home=home, modules={},
                )
            self.assertEqual(load_journal(home)["phase"], "starting")
            self.assertEqual(program.run_calls, 1)
            program.isRunning = True
            write_startup_ack(home, transaction["transactionId"], transaction["expectedVersion"], transaction["installed"])

            confirm_in_fusion(
                transaction, lambda: [program],
                installed_version=lambda: "0.5.0", journal_home=home, modules={},
            )
            self.assertIsNone(load_journal(home))
            self.assertTrue((home / "pending-updates" / "live-update-confirmed.json").is_file())
            self.assertEqual(program.run_calls, 1)

    def test_rollback_restores_old_files_then_confirms_old_version_later(self):
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
            (installed / "STEVE.py").write_text("old")
            (installed / "STEVE.py").write_text("old")
            (installed / "old.py").write_text("old")
            home = root / "home"
            program = Program(location=str(installed))
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)
            program.isRunning = True

            rollback_in_fusion(transaction, lambda: [program], journal_home=home)
            self.assertEqual(load_journal(home)["phase"], "rollback_pending")
            self.assertTrue((installed / "old.py").is_file())
            self.assertTrue((installed / "STEVE.py").is_file())
            self.assertEqual(program.stop_calls, 2)
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                confirm_rollback(transaction, lambda: [program], "0.4.0", journal_home=home)
            write_startup_ack(home, transaction["transactionId"], "0.4.0", transaction["installed"], phase="rollback_pending")

            confirm_rollback(transaction, lambda: [program], "0.4.0", journal_home=home)
            self.assertEqual(transaction["phase"], "rolled_back")
            self.assertIsNone(load_journal(home))
            self.assertTrue(program.isRunning)

    def test_confirm_does_not_repeat_run_request_after_failed_start(self):
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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            def failed_run():
                program.run_calls += 1
                program.isRunning = False
            program.run = failed_run
            home = root / "home"
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)

            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "restart STEVE"):
                    confirm_in_fusion(
                        transaction, lambda: [program],
                        installed_version=lambda: "0.5.0", journal_home=home, modules={},
                    )
            self.assertEqual(program.run_calls, 1)
            self.assertEqual(load_journal(home)["phase"], "starting")

    def test_confirm_observes_already_running_script_without_calling_run_again(self):
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
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            home = root / "home"
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)
            program.isRunning = True
            write_startup_ack(home, transaction["transactionId"], transaction["expectedVersion"], transaction["installed"])

            confirm_in_fusion(
                transaction, lambda: [program],
                installed_version=lambda: "0.5.0", journal_home=home, modules={},
            )

            self.assertEqual(program.run_calls, 0)
            self.assertIsNone(load_journal(home))
            self.assertTrue((home / "pending-updates" / "live-update-confirmed.json").is_file())

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
            (installed / "STEVE.py").write_text("old")

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

    def test_apply_rechecks_fresh_script_after_run_before_declaring_failure(self):
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
            (installed / "STEVE.py").write_text("old")

            initial = Program(location=str(installed))
            fresh = Program(location=str(installed), running=False)
            reads = [0]
            original_run = fresh.run
            def delayed_run():
                original_run()
                fresh.isRunning = False
            fresh.run = delayed_run
            def programs_after_run():
                reads[0] += 1
                if reads[0] >= 2:
                    fresh.isRunning = True
                return [fresh]

            core_apply_in_fusion(
                [initial], package, installed, "0.5.0", {},
                installed_version=lambda: "0.5.0",
                programs_provider=programs_after_run,
            )
            self.assertGreaterEqual(reads[0], 2)
            self.assertEqual(fresh.run_calls, 1)
            self.assertTrue(fresh.isRunning)

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

    def test_failed_rollback_keeps_journal_when_old_program_does_not_restart(self):
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
            (installed / "STEVE.py").write_text("old")
            home = root / "home"
            program = Program(location=str(installed))
            def broken_run():
                program.run_calls += 1
                program.isRunning = False
            program.run = broken_run

            with self.assertRaisesRegex(RuntimeError, "legacy synchronous update path"):
                core_apply_in_fusion(
                    [program], package, installed, "0.5.0", {},
                    installed_version=lambda: "0.5.0",
                    journal_home=home,
                )

            self.assertIsNone(load_journal(home))
            self.assertEqual(json.loads((installed / "STEVE.manifest").read_text())["version"], "0.4.0")

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
            (installed / "STEVE.py").write_text("old")
            (installed / "old.py").write_text("old")
            home = root / "home"
            program = Program(location=str(installed))
            attempts = [0]
            def broken_then_recover():
                attempts[0] += 1
                program.run_calls += 1
                program.isRunning = attempts[0] > 1
            program.run = broken_then_recover

            with self.assertRaisesRegex(RuntimeError, "legacy synchronous update path"):
                core_apply_in_fusion(
                    [program], package, installed, "0.5.0", {},
                    installed_version=lambda: "0.5.0",
                    journal_home=home,
                )

            self.assertTrue((installed / "old.py").is_file())
            self.assertIsNone(load_journal(home))

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
            (installed / "STEVE.py").write_text("current")
            begin_journal(home, root / "pending", installed, "0.5.3")
            update_journal(
                home,
                "failed",
                backup=str(root / "AddIns/.STEVE-rollback-missing"),
                previousVersion="0.5.6",
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

            with self.assertRaisesRegex(ValueError, "not verified"):
                recover_journal(home)
            self.assertIsNotNone(load_journal(home))

    def test_recover_journal_refuses_unconfirmed_starting_transaction_after_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-starting"
            backup.mkdir(parents=True)
            (backup / "old.py").write_text("old")
            (backup / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (backup / "STEVE.py").write_text("old")
            begin_journal(home, root / "pending", installed, "0.5.0")
            update_journal(home, "starting", backup=str(backup), previousVersion="0.4.0",
                           previousTree=_fixture_inventory(backup))

            with self.assertRaisesRegex(ValueError, "interrupted"):
                recover_journal(home)
            self.assertTrue((installed / "old.py").is_file())
            self.assertEqual(load_journal(home)["phase"], "failed")

    def test_recover_journal_restores_backup_for_unconfirmed_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-test"
            backup.mkdir(parents=True)
            (backup / "old.py").write_text("old")
            (backup / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (backup / "STEVE.py").write_text("old")
            begin_journal(home, root / "pending", installed, "0.5.0")
            update_journal(home, "backed_up", backup=str(backup), previousVersion="0.4.0")
            recover_journal(home)
            self.assertTrue((installed / "old.py").is_file())
            self.assertIsNone(load_journal(home))

    def test_recover_journal_preserves_rollback_restoring_after_restore_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-restore-failure"
            backup.mkdir(parents=True)
            (backup / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (backup / "STEVE.py").write_text("old")
            begin_journal(home, root / "pending-updates" / "STEVE-0.5.0", installed, "0.5.0", request_id="3" * 32)
            update_journal(home, "rollback_restoring", previousVersion="0.4.0", backup=str(backup))
            with patch.object(Path, "rename", side_effect=OSError("restore failed")):
                with self.assertRaisesRegex(OSError, "restore failed"):
                    backup.rename(installed)
            self.assertEqual(load_journal(home)["phase"], "rollback_restoring")
            self.assertTrue(recover_journal(home))
            self.assertEqual(load_journal(home)["phase"], "rollback_pending")
            self.assertTrue((installed / "STEVE.manifest").is_file())

    def test_rollback_rejects_corrupt_backup_before_deleting_installed_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("new")
            backup = root / "AddIns" / ".STEVE-rollback-corrupt"
            backup.mkdir(parents=True)
            (backup / "STEVE.manifest").write_text("not-json")
            home = root / "home"
            begin_journal(home, root / "pending" / "STEVE-0.5.1", installed, "0.5.1", transaction_id="tx-corrupt", request_id="c" * 32)
            update_journal(home, "installed", backup=str(backup), previousVersion="0.4.0")
            transaction = {
                "transactionId": "tx-corrupt", "expectedVersion": "0.5.1",
                "previousVersion": "0.4.0", "package": str(root / "pending" / "STEVE-0.5.1"),
                "installed": str(installed), "requestId": "c" * 32,
            }
            program = Program(location=str(installed))
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                rollback_in_fusion(transaction, lambda: [program], journal_home=home)
            self.assertEqual(json.loads((installed / "STEVE.manifest").read_text())["version"], "0.5.0")
            self.assertEqual(load_journal(home)["phase"], "installed")

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
            (installed / "STEVE.py").write_text("old")
            (installed / "STEVE.py").write_text("old")
            home = root / "home"
            program = Program(location=str(installed))
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home, request_id="a" * 32)
            program.isRunning = True
            original_update = core_module.update_journal

            def fail_after_restore(journal_home, phase, **changes):
                if phase == "rollback_pending":
                    raise OSError("simulated journal write failure")
                return original_update(journal_home, phase, **changes)

            with patch.object(core_module, "update_journal", side_effect=fail_after_restore):
                with self.assertRaisesRegex(OSError, "simulated"):
                    rollback_in_fusion(transaction, lambda: [program], journal_home=home)

            self.assertEqual(load_journal(home)["phase"], "rollback_restoring")
            self.assertFalse(any(installed.parent.glob(".STEVE-rollback-*")))
            self.assertEqual(json.loads((installed / "STEVE.manifest").read_text())["version"], "0.4.0")
            self.assertTrue(recover_journal(home))
            self.assertEqual(load_journal(home)["phase"], "rollback_pending")

    def test_recover_journal_preserves_restored_rollback_pending_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            (installed / "STEVE.py").write_text("old")
            backup = root / "AddIns" / ".STEVE-rollback-consumed"
            begin_journal(home, root / "pending-updates" / "STEVE-0.5.0", installed, "0.5.0", transaction_id="tx-rollback")
            update_journal(
                home, "rollback_pending", backup=str(backup), previousVersion="0.4.0",
            )
            self.assertTrue(recover_journal(home))
            self.assertEqual(load_journal(home)["phase"], "rollback_pending")
            self.assertTrue((installed / "STEVE.manifest").is_file())

    def test_recover_journal_clears_confirmed_transaction_without_touching_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "new.py").write_text("new")
            begin_journal(home, root / "pending", installed, "0.5.0")
            update_journal(home, "confirmed", backup=str(root / "AddIns/.STEVE-rollback-test"))
            with self.assertRaisesRegex(ValueError, "matching receipt"):
                recover_journal(home)
            self.assertTrue((installed / "new.py").is_file())
            self.assertIsNotNone(load_journal(home))

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
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("old")
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
    def test_confirmed_receipt_matches_only_exact_request_and_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("new")
            transaction = {
                "transactionId": "tx-1",
                "expectedVersion": "0.5.0",
                "package": str((root / "pending-updates" / "STEVE-0.5.0").resolve()),
                "installed": str(installed),
                "requestId": "c" * 32,
            }
            request = {"requestId": "c" * 32, "version": "0.5.0", "package": transaction["package"]}
            write_confirmed(home, transaction)
            self.assertTrue(confirmed_matches(home, request, installed))
            self.assertFalse(confirmed_matches(home, {"requestId": "d" * 32, "version": "0.5.0", "package": request["package"]}, installed))
            self.assertFalse(confirmed_matches(home, {"version": "0.5.0", "package": request["package"]}, installed))
            self.assertFalse(confirmed_matches(home, request, root / "AddIns" / "OTHER"))

    def test_confirmed_journal_rejects_receipt_inventory_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            package = root / "pending-updates" / "STEVE-0.5.0"
            staged = package / "STEVE"
            installed = root / "AddIns" / "STEVE"
            staged.mkdir(parents=True)
            installed.mkdir(parents=True)
            for tree, version, body in ((staged, "0.5.0", "new"), (installed, "0.4.0", "old")):
                (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                (tree / "STEVE.py").write_text(body)
            expected_tree = core_module._tree_inventory(staged)
            previous_tree = core_module._tree_inventory(installed)
            transaction = {
                "transactionId": "tx-receipt-tree",
                "requestId": "a" * 32,
                "package": str(package),
                "installed": str(installed),
                "expectedVersion": "0.5.0",
                "expectedTree": expected_tree,
                "previousTree": previous_tree,
            }
            begin_journal(
                home, package, installed, "0.5.0", request_id=transaction["requestId"],
                expected_tree=expected_tree, previous_tree=previous_tree,
            )
            update_journal(home, "confirmed")
            write_confirmed(home, transaction)
            receipt_path = core_module.confirmed_path(home)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["expectedTree"] = dict(receipt["expectedTree"], **{"STEVE.py": "0" * 64})
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertFalse(confirmed_commit_verified(home, load_journal(home)))

    def test_confirm_in_fusion_rejects_transaction_inventory_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0"
            staged = package / "STEVE"
            installed = root / "AddIns" / "STEVE"
            staged.mkdir(parents=True)
            installed.mkdir(parents=True)
            for tree, version, body in ((staged, "0.5.0", "new"), (installed, "0.4.0", "old")):
                (tree / "STEVE.manifest").write_text(json.dumps({"version": version}))
                (tree / "STEVE.py").write_text(body)
            sums = "\n".join(
                f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}"
                for path in sorted(staged.iterdir())
            ) + "\n"
            (package / "SHA256SUMS").write_text(sums)
            home = root / "home"
            program = Program(location=str(installed))
            transaction = prepare_in_fusion([program], package, installed, "0.5.0", journal_home=home)
            transaction["expectedTree"] = dict(transaction["expectedTree"], **{"STEVE.py": "0" * 64})
            with self.assertRaisesRegex(RuntimeError, "no longer matches its journal"):
                confirm_in_fusion(
                    transaction, lambda: [program], installed_version=lambda: "0.5.0",
                    journal_home=home, modules={},
                )

    def test_confirmed_receipt_does_not_match_same_package_with_new_request_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            package = root / "pending-updates" / "STEVE-0.5.0"
            transaction = {
                "transactionId": "tx-same-package",
                "expectedVersion": "0.5.0",
                "package": str(package),
                "installed": str(installed),
                "requestId": "e" * 32,
            }
            write_confirmed(home, transaction)
            replacement = {"requestId": "f" * 32, "version": "0.5.0", "package": str(package)}
            self.assertFalse(confirmed_matches(home, replacement, installed))

    def test_confirmed_receipt_rejects_mismatched_transaction_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("new")
            transaction = {
                "transactionId": "tx-receipt",
                "expectedVersion": "0.5.0",
                "package": str((root / "pending-updates" / "STEVE-0.5.0").resolve()),
                "installed": str(installed),
                "requestId": "1" * 32,
            }
            write_confirmed(home, transaction)
            request = {"requestId": "1" * 32, "version": "0.5.0", "package": transaction["package"]}
            self.assertTrue(confirmed_matches(home, request, installed, transaction_id="tx-receipt"))
            self.assertFalse(confirmed_matches(home, request, installed, transaction_id="other-tx"))

    def test_recover_journal_rejects_corrupt_rollback_backup_before_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            backup = root / "AddIns" / ".STEVE-rollback-corrupt"
            backup.mkdir(parents=True)
            (backup / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (backup / "tampered.py").write_text("bad")
            begin_journal(home, root / "pending" / "STEVE-0.5.0", installed, "0.5.0")
            update_journal(home, "rollback_pending", backup=str(backup), previousVersion="0.4.0")
            with self.assertRaisesRegex(ValueError, "not verified"):
                recover_journal(home)
            self.assertTrue(backup.is_dir())
            self.assertFalse(installed.exists())
            self.assertEqual(load_journal(home)["phase"], "rollback_pending")

    def test_recover_journal_reconciles_receipt_before_restoring_starting_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("new")
            package = root / "pending" / "STEVE-0.5.0"
            expected_tree = core_module._tree_inventory(installed)
            begin_journal(
                home, package, installed, "0.5.0", transaction_id="tx-receipt", request_id="a" * 32,
                expected_tree=expected_tree,
            )
            update_journal(home, "starting", backup=str(root / "AddIns" / ".STEVE-rollback-old"))
            transaction = {
                "transactionId": "tx-receipt", "expectedVersion": "0.5.0",
                "package": str(package), "installed": str(installed), "requestId": "a" * 32,
            }
            write_confirmed(home, transaction)
            write_startup_ack(home, "tx-receipt", "0.5.0", installed)
            self.assertTrue(recover_journal(home))
            self.assertTrue((installed / "STEVE.py").is_file())
            self.assertEqual(load_journal(home)["phase"], "recovery_blocked")

    def test_confirmation_keeps_confirmed_journal_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = root / "pending-updates" / "STEVE-0.5.0" / "STEVE"
            package.mkdir(parents=True)
            (package / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (package / "STEVE.py").write_text("new")
            sums = [f"{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(package.iterdir())]
            (package.parent / "SHA256SUMS").write_text("\n".join(sums) + "\n")
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            program = Program(location=str(installed))
            home = root / "home"
            transaction = prepare_in_fusion([program], package.parent, installed, "0.5.0", journal_home=home)
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                confirm_in_fusion(transaction, lambda: [program], installed_version=lambda: "0.5.0", journal_home=home, modules={})
            write_startup_ack(home, transaction["transactionId"], "0.5.0", installed)
            with patch.object(core_module, "clear_journal", side_effect=OSError("cleanup failed")):
                confirm_in_fusion(transaction, lambda: [program], installed_version=lambda: "0.5.0", journal_home=home, modules={})
            self.assertEqual(load_journal(home)["phase"], "confirmed")
            self.assertTrue((home / "pending-updates" / "live-update-confirmed.json").is_file())

    def test_startup_ack_failure_cleans_temporary_file_and_reports_none(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.5.0"}')
            (installed / "STEVE.py").write_text("new")
            begin_journal(home, root / "pending" / "STEVE-0.5.0", installed, "0.5.0", transaction_id="tx-ack", request_id="b" * 32)
            update_journal(home, "starting")
            with patch.object(live_update_module.os, "fsync", side_effect=OSError("fsync failed")):
                self.assertIsNone(live_update_module.acknowledge_startup(home))
            self.assertFalse(any((home / "pending-updates").glob(".live-update-startup.json.*.tmp")))
    def test_journal_write_fails_closed_when_parent_directory_fsync_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            if os.name == "nt":
                staged = home / "staged"
                installed = home / "AddIns" / "STEVE"
                journal = begin_journal(home, staged, installed, "0.5.0", request_id="c" * 32)
                self.assertEqual(load_journal(home)["expectedVersion"], "0.5.0")
                self.assertEqual(journal, home / "pending-updates" / JOURNAL_NAME)
                self.assertFalse(any((home / "pending-updates").glob(".journal-*.tmp")))
                return
            original_fsync = core_module.os.fsync
            calls = [0]

            def fail_directory_fsync(fd):
                calls[0] += 1
                if calls[0] == 2:
                    raise OSError("directory fsync failed")
                return original_fsync(fd)

            with patch.object(core_module.os, "fsync", side_effect=fail_directory_fsync):
                with self.assertRaisesRegex(OSError, "directory fsync"):
                    begin_journal(home, "/staged", "/addins/STEVE", "0.5.0", request_id="c" * 32)
            self.assertFalse(any((home / "pending-updates").glob(".journal-*.tmp")))

    def test_clear_journal_requires_parent_directory_durability(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            begin_journal(home, "/staged", "/addins/STEVE", "0.5.0", request_id="d" * 32)
            if os.name == "nt":
                clear_journal(home)
                self.assertFalse((home / "pending-updates" / JOURNAL_NAME).exists())
                self.assertFalse(any((home / "pending-updates").glob(".journal-*.tmp")))
            else:
                original_fsync = core_module.os.fsync

                def fail_cleanup_directory_fsync(fd):
                    raise OSError("cleanup directory fsync failed")

                with patch.object(core_module.os, "fsync", side_effect=fail_cleanup_directory_fsync):
                    with self.assertRaisesRegex(OSError, "cleanup directory fsync"):
                        clear_journal(home)
                self.assertFalse(any((home / "pending-updates").glob(".journal-*.tmp")))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            (installed / "STEVE.manifest").write_text('{"version":"0.4.0"}')
            (installed / "STEVE.py").write_text("old")
            backup = root / "AddIns" / ".STEVE-rollback-test"
            begin_journal(home, root / "pending-updates" / "STEVE-0.5.0", installed, "0.5.0", request_id="2" * 32)
            update_journal(home, "recovery_blocked", previousVersion="0.4.0", backup=str(backup))
            with self.assertRaisesRegex(ValueError, "recovery"):
                recover_journal(home)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            installed = root / "AddIns" / "STEVE"
            installed.mkdir(parents=True)
            transaction = {
                "transactionId": "tx-confirmed",
                "expectedVersion": "0.5.0",
                "package": str((root / "pending-updates" / "STEVE-0.5.0").resolve()),
                "installed": str(installed),
                "previousVersion": "0.4.0",
            }
            begin_journal(home, transaction["package"], installed, transaction["expectedVersion"], transaction_id=transaction["transactionId"])
            update_journal(home, "confirmed", previousVersion="0.4.0", backup=str(root / "AddIns" / ".STEVE-rollback-test"))
            with self.assertRaisesRegex(RuntimeError, "rollback transaction"):
                rollback_in_fusion(transaction, lambda: [], journal_home=home)


if __name__ == "__main__":
    unittest.main()
