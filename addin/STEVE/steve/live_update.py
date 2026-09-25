"""Coordination primitives for the experimental in-Fusion updater.

The current release still uses the detached installer. This module is the
platform-neutral core for a future helper add-in that owns the Fusion
stop/swap/run handoff. It deliberately contains no Autodesk imports.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from uuid import uuid4

from .transport import data_home
from .version import VERSION


def _core_module():
    """Load the sibling helper core without importing replaceable STEVE modules."""
    import importlib.util
    core_path = Path(__file__).resolve().parents[2] / "STEVEUpdater" / "update_core.py"
    spec = importlib.util.spec_from_file_location("steve_update_core_ack", core_path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load the update core for acknowledgement validation.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

REQUEST_NAME = "live-update.json"
READY_NAME = "steve-updater-ready.json"
READY_REVOKED_NAME = "steve-updater-ready-revoked.json"
JOURNAL_NAME = "live-update-journal.json"
STARTUP_ACK_NAME = "live-update-startup.json"


def ready_path(home):
    return Path(home) / "pending-updates" / READY_NAME


def live_update_ready(home=None, max_age=5.0):
    """Return true only while heartbeat and persisted recovery state are safe."""
    try:
        if os.name == "nt":
            return False
        home = Path(home or data_home())
        revoked = home / "pending-updates" / READY_REVOKED_NAME
        if revoked.exists() or revoked.is_symlink():
            return False
        journal = home / "pending-updates" / JOURNAL_NAME
        if journal.exists() or journal.is_symlink():
            return False
        request = request_path(home)
        if request.exists() or request.is_symlink():
            read_request(home)
        path = ready_path(home)
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = payload["timestamp"]
        return (isinstance(payload.get("pid"), int) and not payload.get("revoked", False)
                and isinstance(timestamp, (int, float)) and time.time() - timestamp <= max_age)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _write_startup_ack(path, payload):
    """Write an acknowledgement with platform-specific durability semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def acknowledge_startup(home=None):
    """Acknowledge only a fully validated, current transaction attempt."""
    home = Path(home or data_home())
    try:
        core = _core_module()
        journal = core.load_journal(home)
        if journal is None or journal.get("phase") not in {"starting", "rollback_pending"}:
            return None
        installed_value = journal.get("installed")
        if not isinstance(installed_value, str) or not Path(installed_value).is_absolute():
            return None
        installed = Path(installed_value).resolve()
        if str(installed) != installed_value or core._path_has_symlink(installed):
            return None
        expected_version = (journal.get("expectedVersion")
                            if journal["phase"] == "starting"
                            else journal.get("previousVersion"))
        expected_tree = (journal.get("expectedTree")
                         if journal["phase"] == "starting"
                         else journal.get("previousTree"))
        if (not core._valid_version(expected_version)
                or not core._valid_tree_inventory(expected_tree)
                or not core._validate_installed_tree(installed, expected_version, expected_tree)):
            return None
        payload = {
            "transactionId": journal["transactionId"],
            "startAttemptId": journal["startAttemptId"],
            "phase": journal["phase"],
            "version": expected_version,
            "installed": installed_value,
            "timestamp": time.time(),
        }
        return _write_startup_ack(home / "pending-updates" / STARTUP_ACK_NAME, payload)
    except (OSError, ImportError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def request_path(home):
    return Path(home) / "pending-updates" / REQUEST_NAME


def _trusted_staged(home, package):
    home = Path(home).resolve()
    package = Path(package).resolve()
    pending = (home / "pending-updates").resolve()
    if not package.is_relative_to(pending):
        raise ValueError("The live update package must be under pending-updates.")
    if not package.is_dir() or package.is_symlink():
        raise ValueError("The live update package is not a directory.")
    if not (package / "STEVE" / "STEVE.manifest").is_file() or not (package / "STEVE" / "STEVE.py").is_file():
        raise ValueError("The live update package is incomplete.")
    return package


def _valid_version(version):
    return (isinstance(version, str) and version.count(".") == 2
            and all(part.isdigit() for part in version.split(".")))


def write_request(home, package, version):
    """Atomically publish a bounded request for the separate helper add-in."""
    if os.name == "nt":
        raise RuntimeError("In-Fusion live updates are disabled on Windows; use the detached installer.")
    if not _valid_version(version):
        raise ValueError("The live update request has an invalid version.")
    package = _trusted_staged(home, package)
    request = request_path(home)
    request.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid4().hex
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=request.parent,
                                        prefix=".live-update-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"requestId": request_id, "version": version, "package": str(package)}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(request)
        if os.name != "nt":
            directory_fd = os.open(request.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return request


def _valid_request_id(request_id):
    return isinstance(request_id, str) and len(request_id) == 32 and all(
        character in "0123456789abcdef" for character in request_id
    )


def read_request(home):
    request = request_path(home)
    if not request.is_file() or request.is_symlink():
        return None
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
        if set(payload) != {"requestId", "version", "package"} or not _valid_request_id(payload["requestId"]):
            raise ValueError("invalid request identity")
        version = payload["version"]
        if not _valid_version(version):
            raise ValueError("invalid version")
        package = _trusted_staged(home, payload["package"])
        package_name = package.name
        if package_name != f"STEVE-{version}" and not package_name.startswith(f"STEVE-{version}-"):
            raise ValueError("package version does not match request")
        manifest = json.loads((package / "STEVE" / "STEVE.manifest").read_text(encoding="utf-8"))
        if manifest.get("version") != version:
            raise ValueError("package manifest version does not match request")
        return {"requestId": payload["requestId"], "version": version, "package": str(package)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"The live update request is invalid: {exc}") from exc


def _path_has_symlink(path):
    candidate = Path(path).absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def find_steve_program(programs, expected_path=None):
    """Return exactly one valid STEVE API program, optionally path-bound."""
    matches = []
    for program in programs or ():
        if not getattr(program, "isValid", False) or getattr(program, "name", None) != "STEVE":
            continue
        if expected_path is not None:
            folder = getattr(program, "folder", None)
            if folder is None:
                # Compatibility for local stand-ins; Fusion's location is an enum.
                location = getattr(program, "location", None)
                if not isinstance(location, (str, os.PathLike)):
                    continue
                folder = location
            if _path_has_symlink(folder) or _path_has_symlink(expected_path):
                continue
            folder_path = Path(str(folder))
            expected = Path(expected_path)
            if folder_path.resolve() != expected.resolve():
                continue
        matches.append(program)
    return matches[0] if len(matches) == 1 else None


def purge_steve_modules(modules=None):
    """Remove only STEVE package modules before Fusion calls the new run()."""
    modules = sys.modules if modules is None else modules
    for name in list(modules):
        if name == "STEVE" or name == "steve" or name.startswith("steve."):
            modules.pop(name, None)


def swap_staged_addin(package, installed, expected_version):
    """Replace only the STEVE folder and return a rollback callback."""
    package = Path(package).resolve()
    installed = Path(installed).resolve()
    source = package / "STEVE"
    manifest = source / "STEVE.manifest"
    if not source.is_dir() or not manifest.is_file():
        raise ValueError("The staged update does not contain a STEVE add-in.")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("version") != expected_version:
        raise ValueError("The staged add-in version does not match the update request.")
    if installed.name != "STEVE" or installed.parent == installed:
        raise ValueError("The installed STEVE path is invalid.")
    backup = installed.with_name(f".STEVE-rollback-{uuid4().hex}")
    installed.rename(backup)
    try:
        shutil.copytree(source, installed, ignore=shutil.ignore_patterns("STEVEUpdater"))
    except Exception:
        if installed.exists():
            shutil.rmtree(installed, ignore_errors=True)
        backup.rename(installed)
        raise

    def rollback():
        if installed.exists():
            shutil.rmtree(installed, ignore_errors=True)
        if backup.exists() and not installed.exists():
            backup.rename(installed)

    return rollback


def apply_in_fusion(programs, package, installed, swap, expected_version, modules=None, installed_version=None):
    """Stop, swap, reload and verify one managed add-in.

    ``swap`` must replace ``installed`` from ``package`` and return a rollback
    callable. The Autodesk-facing helper owns the API program collection and
    invokes this on Fusion's main thread. ``installed_version`` is an optional
    post-run manifest reader used to prove the new code is active.
    """
    program = find_steve_program(programs, installed)
    if program is None:
        raise RuntimeError("Could not identify the running STEVE add-in safely.")
    if not getattr(program, "isRunning", True):
        raise RuntimeError("STEVE is not running; restart Fusion to apply this update.")
    program.stop()
    if getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not stop STEVE; restart Fusion to apply this update.")
    rollback = swap(package, installed, expected_version)
    try:
        purge_steve_modules(modules)
        program.run()
        if not getattr(program, "isRunning", False):
            raise RuntimeError("Fusion did not restart STEVE; restart Fusion to apply this update.")
        if installed_version is not None and installed_version() != expected_version:
            raise RuntimeError("Fusion restarted STEVE but the expected version is not active; restart Fusion to apply this update.")
    except Exception:
        rollback()
        purge_steve_modules(modules)
        try:
            program.run()
        except Exception:
            pass
        raise
    return expected_version
