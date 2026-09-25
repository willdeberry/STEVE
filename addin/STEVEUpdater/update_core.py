"""Self-contained lifecycle update core used by STEVEUpdater.

This module must not import anything from the replaceable STEVE add-in.
"""
from pathlib import Path
from pathlib import PurePosixPath
import hashlib
import json
import os
import re
import platform
import shutil
import sys
import tempfile
import time
from uuid import uuid4

REQUEST_NAME = "live-update.json"
READY_NAME = "steve-updater-ready.json"
READY_REVOKED_NAME = "steve-updater-ready-revoked.json"
JOURNAL_NAME = "live-update-journal.json"
STARTUP_ACK_NAME = "live-update-startup.json"


def _journal_path(home):
    return Path(home) / "pending-updates" / JOURNAL_NAME


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                        prefix=".journal-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
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
        if temporary and temporary.exists():
            temporary.unlink()


def _sync_directory(path):
    """Persist directory-entry updates on POSIX; Windows lacks portable dir fsync."""
    if os.name == "nt":
        return
    directory_fd = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sync_tree(root):
    """Persist copied files and directory entries before journaling installation."""
    root = Path(root)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        elif path.is_dir():
            _sync_directory(path)
    _sync_directory(root)


def _sync_rename_parent(source, destination):
    parent = Path(destination).parent
    _sync_directory(parent)
    if Path(source).parent != parent:
        _sync_directory(Path(source).parent)


def begin_journal(home, package, installed, expected_version, transaction_id=None, request_id=None,
                  expected_tree=None, previous_tree=None):
    journal = _journal_path(home)
    request_id = request_id or uuid4().hex
    if not _valid_request_id(request_id):
        raise ValueError("The live update request identity is invalid.")
    payload = {
        "transactionId": transaction_id or uuid4().hex,
        "package": str(Path(package).resolve()),
        "installed": str(Path(installed).resolve()),
        "expectedVersion": expected_version,
        "requestId": request_id,
        "startAttemptId": uuid4().hex,
        "phase": "validated",
    }
    if expected_tree is not None:
        if not _valid_tree_inventory(expected_tree):
            raise ValueError("The expected installation tree is invalid.")
        payload["expectedTree"] = expected_tree
    if previous_tree is not None:
        if not _valid_tree_inventory(previous_tree):
            raise ValueError("The previous installation tree is invalid.")
        payload["previousTree"] = previous_tree
    _write_json(journal, payload)
    return journal


def load_journal(home):
    journal = _journal_path(home)
    if not journal.is_file() or journal.is_symlink():
        return None
    payload = json.loads(journal.read_text(encoding="utf-8"))
    required = {
        "transactionId", "requestId", "startAttemptId", "package", "installed",
        "expectedVersion", "phase", "expectedTree", "previousTree",
    }
    phases = {"validated", "stopping", "backed_up", "installed", "starting", "confirmed",
              "rollback_restoring", "rollback_pending", "recovery_blocked", "failed"}
    if (not required.issubset(payload)
            or not isinstance(payload["transactionId"], str)
            or not payload["transactionId"]
            or not _valid_request_id(payload["requestId"])
            or not _valid_request_id(payload["startAttemptId"])
            or not isinstance(payload["package"], str)
            or not Path(payload["package"]).is_absolute()
            or not isinstance(payload["installed"], str)
            or not Path(payload["installed"]).is_absolute()
            or not _valid_version(payload["expectedVersion"])
            or payload["phase"] not in phases
            or ("rollbackStartRequested" in payload
                and not isinstance(payload["rollbackStartRequested"], bool))
            or ("expectedTree" in payload and not _valid_tree_inventory(payload["expectedTree"])
                )
            or ("previousTree" in payload and not _valid_tree_inventory(payload["previousTree"]))
            or (payload["phase"] in {"rollback_restoring", "rollback_pending"}
                and ("rollbackStartRequested" not in payload
                     or not isinstance(payload["rollbackStartRequested"], bool)))
            or (payload["phase"] not in {"rollback_restoring", "rollback_pending"}
                and "rollbackStartRequested" in payload)):
        raise ValueError("The live update journal is invalid.")
    return payload


def update_journal(home, phase, **changes):
    payload = load_journal(home)
    if payload is None:
        raise ValueError("The live update journal does not exist.")
    if not isinstance(phase, str) or phase not in {"validated", "stopping", "backed_up", "installed", "starting", "confirmed", "rollback_restoring", "rollback_pending", "recovery_blocked", "failed"}:
        raise ValueError("The live update journal phase is invalid.")
    payload.update(changes, phase=phase)
    _write_json(_journal_path(home), payload)
    return payload


def clear_journal(home):
    path = _journal_path(home)
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def confirmed_path(home):
    return Path(home) / "pending-updates" / "live-update-confirmed.json"


def write_confirmed(home, transaction):
    expected_tree = transaction.get("expectedTree")
    previous_tree = transaction.get("previousTree")
    if (not _valid_tree_inventory(expected_tree)
            or not _valid_tree_inventory(previous_tree)):
        raise ValueError("The confirmation receipt lacks immutable tree evidence.")
    payload = {
        "transactionId": transaction["transactionId"],
        "version": transaction["expectedVersion"],
        "package": str(Path(transaction["package"]).resolve()),
        "installed": str(Path(transaction["installed"]).resolve()),
        "requestId": transaction["requestId"],
        "expectedTree": expected_tree,
        "previousTree": previous_tree,
        "timestamp": time.time(),
    }
    path = confirmed_path(home)
    _write_json(path, payload)
    return path


def confirmed_matches(home, request, installed, transaction_id=None):
    try:
        payload = json.loads(confirmed_path(home).read_text(encoding="utf-8"))
        return (
            isinstance(request, dict)
            and (transaction_id is None or payload.get("transactionId") == transaction_id)
            and payload.get("requestId") == request.get("requestId")
            and _valid_request_id(request.get("requestId"))
            and payload.get("version") == request.get("version")
            and payload.get("package") == str(Path(request.get("package", "")).resolve())
            and payload.get("installed") == str(Path(installed).resolve())
            and _valid_tree_inventory(payload.get("expectedTree"))
            and _validate_installed_tree(
                installed, request.get("version"), payload.get("expectedTree")
            )
            and (_load_confirmed_receipt(home) or {}).get("expectedTree") == payload.get("expectedTree")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _load_confirmed_receipt(home):
    try:
        payload = json.loads(confirmed_path(home).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _load_startup_ack(home):
    try:
        return read_startup_ack(home)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def startup_ack_path(home):
    return Path(home) / "pending-updates" / STARTUP_ACK_NAME


def write_startup_ack(home, transaction_id, version, installed, phase="starting"):
    """Atomically acknowledge successful STEVE initialization for the journal attempt."""
    if not isinstance(transaction_id, str) or not transaction_id:
        raise ValueError("The startup acknowledgement transaction is invalid.")
    if not isinstance(version, str) or not _valid_version(version):
        raise ValueError("The startup acknowledgement version is invalid.")
    if phase not in {"starting", "rollback_pending"}:
        raise ValueError("The startup acknowledgement phase is invalid.")
    journal = load_journal(home)
    if (not journal or journal.get("transactionId") != transaction_id
            or journal.get("phase") != phase):
        raise ValueError("The startup acknowledgement transaction is not current.")
    start_attempt_id = journal.get("startAttemptId")
    if not _valid_request_id(start_attempt_id):
        raise ValueError("The startup acknowledgement attempt is invalid.")
    expected_version = (journal["expectedVersion"] if phase == "starting"
                        else journal.get("previousVersion"))
    expected_tree = (journal.get("expectedTree") if phase == "starting"
                     else journal.get("previousTree"))
    installed_path = Path(installed).resolve()
    if (version != expected_version
            or not isinstance(installed, (str, os.PathLike))
            or str(installed_path) != str(installed)
            or not _valid_tree_inventory(expected_tree)
            or not _validate_installed_tree(installed_path, expected_version, expected_tree)):
        raise ValueError("The startup acknowledgement does not match the current transaction.")
    payload = {
        "transactionId": transaction_id,
        "startAttemptId": start_attempt_id,
        "phase": phase,
        "version": version,
        "installed": str(installed_path),
        "timestamp": time.time(),
    }
    path = startup_ack_path(home)
    _write_json(path, payload)
    return path


def read_startup_ack(home):
    path = startup_ack_path(home)
    if not path.is_file() or path.is_symlink():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {"transactionId", "startAttemptId", "phase", "version", "installed", "timestamp"}
    if not required.issubset(payload):
        raise ValueError("The startup acknowledgement is invalid.")
    if (not isinstance(payload["transactionId"], str)
            or not isinstance(payload["phase"], str)
            or payload["phase"] not in {"starting", "rollback_pending"}
            or not isinstance(payload["version"], str)):
        raise ValueError("The startup acknowledgement is invalid.")
    if not isinstance(payload["timestamp"], (int, float)):
        raise ValueError("The startup acknowledgement is invalid.")
    return payload


def ready_revoked_path(home):
    return Path(home) / "pending-updates" / READY_REVOKED_NAME


def revoke_ready(home):
    payload = {"timestamp": time.time(), "reason": "helper-startup-failed", "revoked": True}
    try:
        _write_json(ready_revoked_path(home), payload)
        return "marker"
    except Exception:
        # If the separate marker cannot be persisted, invalidate the heartbeat
        # in place when possible; callers must not then delete that heartbeat.
        _write_json(ready_path(home), payload)
        return "heartbeat"


def clear_ready_revocation(home):
    path = ready_revoked_path(home)
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def ready_path(home):
    return Path(home) / "pending-updates" / READY_NAME


def write_ready(home):
    path = ready_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, {"pid": os.getpid(), "timestamp": time.time()})
    return path


def clear_ready(home):
    path = ready_path(home)
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def ready(home, max_age=5.0):
    try:
        path = ready_path(home)
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = payload["timestamp"]
        return (isinstance(payload.get("pid"), int) and isinstance(timestamp, (int, float))
                and time.time() - timestamp <= max_age)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def confirmed_commit_verified(home, payload):
    """Require receipt, identity, and complete installation before cleanup."""
    if not isinstance(payload, dict) or payload.get("phase") != "confirmed":
        return False
    installed = Path(payload.get("installed", ""))
    receipt = _load_confirmed_receipt(home)
    return bool(
        receipt
        and receipt.get("transactionId") == payload.get("transactionId")
        and receipt.get("requestId") == payload.get("requestId")
        and receipt.get("version") == payload.get("expectedVersion")
        and receipt.get("package") == payload.get("package")
        and receipt.get("installed") == str(installed.resolve())
        and receipt.get("expectedTree") == payload.get("expectedTree")
        and receipt.get("previousTree") == payload.get("previousTree")
        and _valid_tree_inventory(payload.get("expectedTree"))
        and _validate_installed_tree(
            installed, payload.get("expectedVersion"), payload.get("expectedTree")
        )
    )


def recover_journal(home):
    """Recover an interrupted transaction before accepting new requests."""
    payload = load_journal(home)
    if payload is None:
        return False
    if payload.get("phase") == "recovery_blocked":
        raise ValueError("The live update recovery is blocked; use the detached installer.")
    if payload.get("phase") == "confirmed":
        if not confirmed_commit_verified(home, payload):
            raise ValueError("The confirmed journal has no matching receipt or complete installation.")
        try:
            clear_journal(home)
        except OSError:
            return True
        return True
    if payload.get("phase") == "starting":
        receipt = _load_confirmed_receipt(home)
        installed = Path(payload.get("installed", ""))
        ack = _load_startup_ack(home)
        if (receipt and ack
                and receipt.get("transactionId") == payload.get("transactionId")
                and receipt.get("requestId") == payload.get("requestId")
                and receipt.get("version") == payload.get("expectedVersion")
                and receipt.get("package") == payload.get("package")
                and receipt.get("installed") == str(installed.resolve())
                and ack.get("transactionId") == payload.get("transactionId")
                and ack.get("startAttemptId") == payload.get("startAttemptId")
                and ack.get("phase") == "starting"
                and ack.get("version") == payload.get("expectedVersion")
                and ack.get("installed") == str(installed.resolve())
                and _valid_tree_inventory(payload.get("expectedTree"))
                and _validate_installed_tree(
                    installed, payload.get("expectedVersion"), payload.get("expectedTree")
                )):
            update_journal(
                home,
                "recovery_blocked",
                error="confirmation was committed before helper cleanup; use the detached installer",
            )
            return True
    backup_value = payload.get("backup")
    installed_value = payload.get("installed")
    if payload.get("phase") == "stopping" and not backup_value:
        installed = Path(installed_value or "")
        previous_version = payload.get("previousVersion")
        if (_valid_tree_inventory(payload.get("previousTree"))
                and _validate_installed_tree(installed, previous_version, payload["previousTree"])
                and not installed.is_symlink()):
            clear_journal(home)
            return True
        raise ValueError("The pre-swap recovery is ambiguous; use the detached installer.")
    if not isinstance(backup_value, str) or not isinstance(installed_value, str):
        raise ValueError("The live update journal has no safe recovery paths.")
    backup = Path(backup_value).resolve()
    installed = Path(installed_value).resolve()
    if (backup.parent != installed.parent or not backup.name.startswith(".STEVE-rollback-")
            or installed.name != "STEVE"):
        raise ValueError("The live update journal contains unsafe recovery paths.")
    if payload.get("phase") in {"rollback_restoring", "rollback_pending"}:
        previous_version = payload.get("previousVersion")
        previous_tree = payload.get("previousTree")
        installed_valid = (_valid_tree_inventory(previous_tree)
                           and _validate_installed_tree(installed, previous_version, previous_tree))
        if installed_valid and not backup.exists():
            if payload.get("phase") == "rollback_restoring":
                update_journal(home, "rollback_pending", rollbackStartRequested=False)
            return True
        if payload.get("phase") == "rollback_restoring" and installed_valid:
            raise ValueError("The rollback recovery is ambiguous; restored installation and backup both exist.")
        if (not _valid_tree_inventory(previous_tree)
                or not _validate_installed_tree(backup, previous_version, previous_tree)
                or backup.is_symlink() or not backup.is_dir()
                or installed.exists()):
            raise ValueError("The rollback recovery backup is not verified or is ambiguous.")
        backup.rename(installed)
        _sync_rename_parent(backup, installed)
        if not _validate_installed_tree(installed, previous_version, previous_tree):
            raise ValueError("The restored STEVE installation is not verified.")
        update_journal(home, "rollback_pending", rollbackStartRequested=False)
        return True
    if backup.is_symlink() or not backup.is_dir():
        if (payload.get("phase") == "failed" and not backup.exists()
                and installed.is_dir() and not installed.is_symlink()
                and not _path_has_symlink(installed)):
            previous_version = payload.get("previousVersion")
            try:
                manifest = json.loads((installed / "STEVE.manifest").read_text(encoding="utf-8"))
                current_version = manifest.get("version")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                current_version = None
            previous_tree = payload.get("previousTree")
            if (isinstance(previous_version, str) and _valid_version(previous_version)
                    and current_version == previous_version
                    and _valid_tree_inventory(previous_tree)
                    and _validate_installed_tree(installed, previous_version, previous_tree)):
                clear_journal(home)
                return True
            raise ValueError("The failed recovery installation is not verified.")
        raise ValueError("The live update backup is missing or unsafe.")
    if installed.exists() or installed.is_symlink():
        raise ValueError("The live update recovery is ambiguous; both installed and backup paths exist.")
    previous_version = payload.get("previousVersion")
    if not isinstance(previous_version, str) or not _valid_version(previous_version):
        raise ValueError("The rollback backup has no verified previous version.")
    previous_tree = payload.get("previousTree")
    try:
        backup_manifest = json.loads((backup / "STEVE.manifest").read_text(encoding="utf-8"))
        backup_version = backup_manifest.get("version")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        backup_version = None
    if (backup_version != previous_version
            or not _valid_tree_inventory(previous_tree)
            or not _validate_installed_tree(backup, previous_version, previous_tree)):
        raise ValueError("The rollback backup is not verified.")
    backup.rename(installed)
    _sync_rename_parent(backup, installed)
    if (not installed.is_dir() or installed.is_symlink()
            or _path_has_symlink(installed)
            or _installed_manifest_version(installed) != previous_version):
        raise ValueError("The restored STEVE installation is not verified.")
    if payload.get("phase") in {"starting", "rollback_pending"}:
        update_journal(home, "failed", error="live update interrupted; use the detached installer")
        raise ValueError("The live update was interrupted before confirmation; use the detached installer.")
    clear_journal(home)
    return True


def data_home(system=None):
    system = system or sys.platform
    if system == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "STEVE"
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "STEVE"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "STEVE"


def request_path(home):
    return Path(home) / "pending-updates" / REQUEST_NAME


def _safe_relative(relative):
    path = Path(relative)
    return bool(relative) and not path.is_absolute() and "\\" not in relative and ".." not in path.parts and "." not in path.parts


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


def _valid_request_id(request_id):
    return (isinstance(request_id, str) and len(request_id) == 32
            and all(character in "0123456789abcdef" for character in request_id))


def version_is_newer(version, current):
    if not _valid_version(version) or not _valid_version(current):
        return False
    return tuple(int(part) for part in version.split(".")) > tuple(int(part) for part in current.split("."))


def write_request(home, package, version):
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


def verify_staged(package, expected_version):
    """Revalidate every staged payload file immediately before replacement."""
    package = Path(package).resolve()
    sums = package / "SHA256SUMS"
    source = package / "STEVE"
    if not sums.is_file() or not source.is_dir():
        raise ValueError("The staged update is incomplete.")
    listed = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        if len(line) < 67 or line[64:66] != "  ":
            raise ValueError("The staged checksum manifest is invalid.")
        digest, relative = line[:64], line[66:]
        if any(character not in "0123456789abcdef" for character in digest) or not _safe_relative(relative):
            raise ValueError("The staged checksum manifest contains an invalid path.")
        if relative in listed:
            raise ValueError("The staged checksum manifest contains a duplicate path.")
        listed.add(relative)
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError("The staged update contains an unexpected file.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("The staged update checksum changed; download it again.")
    metadata = json.loads((source / "STEVE.manifest").read_text(encoding="utf-8"))
    if metadata.get("version") != expected_version:
        raise ValueError("The staged add-in version does not match the update request.")
    required = {"STEVE.manifest", "STEVE.py"}
    if not required.issubset(listed):
        raise ValueError("The staged update is incomplete.")
    actual_files = set()
    for path in source.rglob("*"):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError(f"The staged update contains an unexpected symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"The staged update contains an unexpected entry: {relative}")
        actual_files.add(relative)
    missing = listed - actual_files
    extra = actual_files - listed
    if missing or extra:
        detail = f"missing={sorted(missing)} extra={sorted(extra)}"
        raise ValueError(f"The staged update file list differs from SHA256SUMS: {detail}")


def _path_has_symlink(path):
    candidate = Path(path).absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def find_steve_program(programs, expected_path=None):
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
    modules = sys.modules if modules is None else modules
    for name in list(modules):
        if name == "STEVE" or name == "steve" or name.startswith("steve."):
            modules.pop(name, None)


def swap_staged_addin(package, installed, expected_version, journal_home=None, backup=None,
                      previous_version=None, expected_tree=None, previous_tree=None):
    package = Path(package).resolve()
    installed = Path(installed).resolve()
    verify_staged(package, expected_version)
    source = package / "STEVE"
    if installed.name != "STEVE" or installed.parent == installed:
        raise ValueError("The installed STEVE path is invalid.")
    previous_version = previous_version or _installed_manifest_version(installed)
    captured_previous_tree = previous_tree
    captured_expected_tree = expected_tree
    previous_tree = _tree_inventory(installed)
    expected_tree = _tree_inventory(source)
    if (captured_previous_tree is not None and previous_tree != captured_previous_tree
            or captured_expected_tree is not None and expected_tree != captured_expected_tree):
        raise ValueError("The staged or installed tree no longer matches its captured inventory.")
    if not _validate_installed_tree(installed, previous_version, previous_tree):
        raise ValueError("The installed STEVE tree is not verified.")
    backup = Path(backup) if backup is not None else installed.with_name(f".STEVE-rollback-{uuid4().hex}")
    backup = backup.resolve()
    if (backup.parent != installed.parent or not backup.name.startswith(".STEVE-rollback-")
            or backup.exists() or backup.is_symlink()):
        raise ValueError("The rollback backup path is invalid.")
    if journal_home is not None:
        update_journal(journal_home, "stopping", backup=str(backup))
    installed.rename(backup)
    _sync_rename_parent(installed, backup)
    if journal_home is not None:
        update_journal(journal_home, "backed_up", backup=str(backup))
    try:
        shutil.copytree(source, installed, ignore=shutil.ignore_patterns("STEVEUpdater"))
        _sync_tree(installed)
        if (expected_tree is not None
                and not _validate_installed_tree(installed, expected_version, expected_tree)):
            raise RuntimeError("The installed staged tree does not match its captured inventory.")
    except Exception:
        try:
            if journal_home is not None:
                update_journal(journal_home, "rollback_restoring", rollbackStartRequested=False)
            if (not _validate_installed_tree(backup, previous_version, previous_tree)
                    or backup.is_symlink() or not backup.is_dir()):
                raise RuntimeError("The previous installation is not verified for restoration.")
            if installed.exists() or installed.is_symlink():
                if installed.is_dir() and not installed.is_symlink():
                    shutil.rmtree(installed)
                else:
                    installed.unlink()
            if installed.exists():
                raise RuntimeError("The installed path could not be removed safely.")
            if not _validate_installed_tree(backup, previous_version, previous_tree):
                raise RuntimeError("The previous installation changed before restoration.")
            backup.rename(installed)
            _sync_rename_parent(backup, installed)
            if not _validate_installed_tree(installed, previous_version, previous_tree):
                raise RuntimeError("The previous installation was not restored safely.")
            if journal_home is not None:
                update_journal(journal_home, "rollback_pending", rollbackStartRequested=False)
        except Exception as restore_error:
            raise RuntimeError("The staged update failed and the previous installation could not be restored safely.") from restore_error
        raise

    def rollback():
        try:
            if not _validate_installed_tree(backup, previous_version, previous_tree):
                raise RuntimeError("The previous installation is not verified for restoration.")
            if journal_home is not None:
                update_journal(journal_home, "rollback_restoring", rollbackStartRequested=False)
            if installed.exists() or installed.is_symlink():
                if installed.is_dir() and not installed.is_symlink():
                    shutil.rmtree(installed)
                else:
                    installed.unlink()
            if backup.exists() and not installed.exists():
                backup.rename(installed)
                _sync_rename_parent(backup, installed)
            if not _validate_installed_tree(installed, previous_version, previous_tree):
                raise RuntimeError("The previous installation was not restored safely.")
            if journal_home is not None:
                update_journal(journal_home, "rollback_pending", rollbackStartRequested=False)
        except Exception as restore_error:
            raise RuntimeError("The previous STEVE installation could not be restored safely.") from restore_error

    return rollback


def _tree_inventory(root):
    """Return the exact regular-file inventory and SHA-256 contents of a tree."""
    root = Path(root)
    if (not root.is_dir() or root.is_symlink() or _path_has_symlink(root)):
        raise ValueError("The installation tree is not safe.")
    inventory = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            if path.is_dir() and not path.is_symlink():
                continue
            raise ValueError("The installation tree contains an unsafe entry.")
        relative = path.relative_to(root).as_posix()
        with path.open("rb") as stream:
            inventory[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
    return inventory


def _valid_tree_inventory(value):
    if not isinstance(value, dict) or not value:
        return False
    for relative, digest in value.items():
        if (not isinstance(relative, str) or not relative
                or "\\" in relative or relative.startswith("/")
                or relative != PurePosixPath(relative).as_posix()
                or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            return False
    return True


def _validate_installed_tree(installed, expected_version, expected_tree=None):
    """Verify a recovered add-in is complete, regular-file-only, and version-bound."""
    installed = Path(installed)
    if (not isinstance(expected_version, str) or not _valid_version(expected_version)
            or not installed.is_dir() or installed.is_symlink()
            or _path_has_symlink(installed)):
        return False
    try:
        manifest = json.loads((installed / "STEVE.manifest").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if (manifest.get("version") != expected_version
            or not (installed / "STEVE.py").is_file()):
        return False
    for path in installed.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            return False
    if expected_tree is not None:
        if not _valid_tree_inventory(expected_tree):
            return False
        try:
            if _tree_inventory(installed) != expected_tree:
                return False
        except (OSError, ValueError):
            return False
    return True


def _installed_manifest_version(installed):
    try:
        return json.loads((Path(installed) / "STEVE.manifest").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def prepare_in_fusion(programs, package, installed, expected_version, journal_home=None, request_id=None):
    """Stop STEVE and install a staged package; defer run() to a later callback."""
    request_id = request_id or uuid4().hex
    if not _valid_request_id(request_id):
        raise ValueError("The live update request identity is invalid.")
    verify_staged(package, expected_version)
    program = find_steve_program(programs, installed)
    if program is None:
        raise RuntimeError("Could not identify the running STEVE add-in safely.")
    previous_version = _installed_manifest_version(installed)
    previous_tree = _tree_inventory(installed)
    if not _validate_installed_tree(installed, previous_version, previous_tree):
        raise RuntimeError("The current STEVE installation is not verified.")
    expected_tree = _tree_inventory(Path(package).resolve() / "STEVE")
    if not getattr(program, "isRunning", True):
        raise RuntimeError("STEVE is not running; restart Fusion to apply this update.")
    backup = installed.with_name(f".STEVE-rollback-{uuid4().hex}")
    if journal_home is not None:
        begin_journal(
            journal_home, package, installed, expected_version, request_id=request_id,
            expected_tree=expected_tree, previous_tree=previous_tree,
        )
        update_journal(journal_home, "stopping", previousVersion=previous_version, backup=str(backup))
    program.stop()
    if getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not stop STEVE; restart Fusion to apply this update.")
    rollback = swap_staged_addin(
        package, installed, expected_version, journal_home=journal_home, backup=backup,
        previous_version=previous_version, expected_tree=expected_tree,
        previous_tree=previous_tree,
    )
    if journal_home is not None:
        update_journal(journal_home, "installed")
        update_journal(journal_home, "starting")
    journal = load_journal(journal_home) if journal_home is not None else None
    transaction_id = journal["transactionId"] if journal is not None else None
    return {
        "package": str(Path(package).resolve()),
        "installed": str(Path(installed).resolve()),
        "expectedVersion": expected_version,
        "program": program,
        "rollback": rollback,
        "transactionId": transaction_id,
        "requestId": request_id,
        "previousVersion": previous_version,
        "expectedTree": expected_tree,
        "previousTree": previous_tree,
        "startAttemptId": journal["startAttemptId"] if journal is not None else None,
        "startRequested": False,
        "phase": "starting",
    }


def confirm_in_fusion(transaction, programs_provider, installed_version=None, journal_home=None,
                      modules=None):
    """Start and confirm a prepared transaction from a later Fusion callback."""
    installed = Path(transaction["installed"])
    expected_version = transaction["expectedVersion"]
    expected_tree = transaction.get("expectedTree")
    previous_tree = transaction.get("previousTree")
    if (not _valid_tree_inventory(expected_tree)
            or not _valid_tree_inventory(previous_tree)):
        raise RuntimeError("The live update transaction lacks immutable tree evidence.")
    if journal_home is not None:
        journal = load_journal(journal_home)
        if journal is None or journal.get("phase") != "starting":
            raise RuntimeError("The live update transaction is not awaiting confirmation.")
        for field, expected in (("transactionId", transaction.get("transactionId")),
                                ("package", transaction.get("package")),
                                ("installed", str(installed.resolve())),
                                ("expectedVersion", expected_version),
                                ("requestId", transaction.get("requestId")),
                                ("startAttemptId", transaction.get("startAttemptId")),
                                ("expectedTree", expected_tree),
                                ("previousTree", previous_tree)):
            if journal.get(field) != expected:
                raise RuntimeError("The live update transaction no longer matches its journal.")
    program = find_steve_program(programs_provider(), installed)
    if program is None:
        raise RuntimeError("Could not reacquire STEVE after staged installation.")
    purge_steve_modules(modules)
    if not getattr(program, "isRunning", False) and not transaction.get("startRequested", False):
        start_attempt_id = uuid4().hex
        transaction["startAttemptId"] = start_attempt_id
        if journal_home is not None:
            update_journal(
                journal_home,
                "starting",
                startAttemptId=start_attempt_id,
                startRequested=True,
            )
        transaction["startRequested"] = True
        program.run()
    if not getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not restart STEVE; restart Fusion to apply this update.")
    if (not _validate_installed_tree(installed, expected_version, expected_tree)
            or installed_version is not None and installed_version() != expected_version):
        raise RuntimeError("Fusion restarted STEVE but the expected version is not active; restart Fusion to apply this update.")
    if journal_home is not None:
        ack = read_startup_ack(journal_home)
        expected_installed = str(installed.resolve())
        if (not ack or ack.get("transactionId") != transaction.get("transactionId")
                or ack.get("startAttemptId") != transaction.get("startAttemptId")
                or ack.get("phase") != "starting"
                or ack.get("version") != expected_version
                or ack.get("installed") != expected_installed):
            raise RuntimeError("STEVE startup acknowledgement is missing or does not match the transaction.")
        write_confirmed(journal_home, transaction)
        update_journal(journal_home, "confirmed")
        try:
            clear_journal(journal_home)
        except OSError:
            # The durable receipt and confirmed journal are the commit point;
            # cleanup can be retried on a later helper callback.
            pass
    transaction["phase"] = "confirmed"
    return expected_version


def rollback_in_fusion(transaction, programs_provider, journal_home=None):
    """Stop the staged instance and restore the journaled backup."""
    installed = Path(transaction["installed"])
    if journal_home is None:
        raise RuntimeError("Rollback requires a transaction journal.")
    journal = load_journal(journal_home)
    expected_tree = transaction.get("expectedTree")
    previous_tree = transaction.get("previousTree")
    if (journal is None or journal.get("phase") == "confirmed"
            or journal.get("transactionId") != transaction.get("transactionId")
            or journal.get("requestId") != transaction.get("requestId")
            or journal.get("package") != str(Path(transaction["package"]).resolve())
            or journal.get("installed") != str(Path(transaction["installed"]).resolve())
            or journal.get("expectedVersion") != transaction.get("expectedVersion")
            or journal.get("previousVersion") != transaction.get("previousVersion")
            or journal.get("startAttemptId") != transaction.get("startAttemptId")
            or journal.get("rollbackStartRequested") != transaction.get("rollbackStartRequested")
            or journal.get("expectedTree") != expected_tree
            or journal.get("previousTree") != previous_tree
            or not _valid_tree_inventory(expected_tree)
            or not _valid_tree_inventory(previous_tree)):
        raise RuntimeError("The rollback transaction no longer matches its journal.")
    program = find_steve_program(programs_provider(), installed)
    if program is not None and getattr(program, "isRunning", False):
        program.stop()
        if getattr(program, "isRunning", False):
            raise RuntimeError("Fusion did not stop the staged STEVE instance.")
    backup = Path(journal.get("backup", ""))
    previous_version = transaction.get("previousVersion")
    previous_tree = transaction.get("previousTree")
    if (not _valid_tree_inventory(previous_tree)
            or backup.parent != installed.parent
            or not backup.name.startswith(".STEVE-rollback-")
            or not _validate_installed_tree(backup, previous_version, previous_tree)):
        raise RuntimeError("The rollback journal contains an unsafe or unverified backup.")
    update_journal(journal_home, "rollback_restoring", rollbackStartRequested=False)
    try:
        if (not _validate_installed_tree(backup, previous_version, previous_tree)
                or backup.is_symlink() or not backup.is_dir()):
            raise RuntimeError("The rollback backup changed and is no longer verified.")
        if installed.exists() or installed.is_symlink():
            if installed.is_dir() and not installed.is_symlink():
                shutil.rmtree(installed)
            else:
                installed.unlink()
        if installed.exists():
            raise RuntimeError("The installed path could not be removed safely.")
        if not _validate_installed_tree(backup, previous_version, previous_tree):
            raise RuntimeError("The rollback backup changed before restoration.")
        backup.rename(installed)
        _sync_rename_parent(backup, installed)
        if not _validate_installed_tree(installed, previous_version, previous_tree):
            raise RuntimeError("The previous STEVE installation was not restored safely.")
    except Exception as restore_error:
        # Keep rollback_restoring durable. Restart recovery must inspect the
        # backup/install state rather than clearing a failed rollback journal.
        raise RuntimeError("The previous STEVE installation could not be restored safely.") from restore_error
    update_journal(journal_home, "rollback_pending", rollbackStartRequested=False)
    transaction["phase"] = "rollback_pending"
    transaction["rollbackStartRequested"] = False
    return transaction


def confirm_rollback(transaction, programs_provider, previous_version, journal_home=None):
    """Confirm the restored version from a later Fusion callback."""
    if journal_home is None:
        raise RuntimeError("Rollback confirmation requires a transaction journal.")
    journal = load_journal(journal_home)
    expected_tree = transaction.get("expectedTree")
    previous_tree = transaction.get("previousTree")
    if (not _valid_tree_inventory(expected_tree)
            or not _valid_tree_inventory(previous_tree)):
        raise RuntimeError("The rollback transaction lacks immutable tree evidence.")
    if (journal is None or journal.get("phase") != "rollback_pending"
            or journal.get("transactionId") != transaction.get("transactionId")
            or journal.get("package") != str(Path(transaction["package"]).resolve())
            or journal.get("installed") != str(Path(transaction["installed"]).resolve())
            or journal.get("expectedVersion") != transaction.get("expectedVersion")
            or journal.get("previousVersion") != transaction.get("previousVersion")
            or journal.get("startAttemptId") != transaction.get("startAttemptId")
            or journal.get("rollbackStartRequested") != transaction.get("rollbackStartRequested")
            or journal.get("expectedTree") != expected_tree
            or journal.get("previousTree") != previous_tree):
        raise RuntimeError("The rollback transaction no longer matches its journal.")
    installed = Path(transaction["installed"])
    program = find_steve_program(programs_provider(), installed)
    if program is None:
        raise RuntimeError("Could not reacquire STEVE after rollback.")
    if not getattr(program, "isRunning", False) and not transaction.get("rollbackStartRequested", False):
        start_attempt_id = uuid4().hex
        transaction["startAttemptId"] = start_attempt_id
        if journal_home is not None:
            update_journal(
                journal_home,
                "rollback_pending",
                startAttemptId=start_attempt_id,
                rollbackStartRequested=True,
            )
        transaction["rollbackStartRequested"] = True
        program.run()
    if not getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not restart the previous STEVE version.")
    if _installed_manifest_version(installed) != previous_version:
        raise RuntimeError("The previous STEVE version is not active after rollback.")
    ack = read_startup_ack(journal_home)
    expected_installed = str(installed.resolve())
    if (not ack
            or not _validate_installed_tree(installed, previous_version, previous_tree)
            or ack.get("transactionId") != transaction.get("transactionId")
            or ack.get("startAttemptId") != transaction.get("startAttemptId")
            or ack.get("phase") != "rollback_pending"
            or ack.get("version") != previous_version
            or ack.get("installed") != expected_installed):
        raise RuntimeError("Previous STEVE startup acknowledgement is missing or does not match the rollback transaction.")
    clear_journal(journal_home)
    transaction["phase"] = "rolled_back"
    return previous_version


def apply_in_fusion(programs, package, installed, expected_version, modules=None,
                    installed_version=None, journal_home=None, programs_provider=None,
                    diagnostic=None):
    if journal_home is not None:
        raise RuntimeError("The legacy synchronous update path cannot use a live-update journal; use prepare_in_fusion().")
    def report(event):
        if diagnostic is not None:
            try:
                diagnostic(event)
            except Exception:
                pass

    verify_staged(package, expected_version)
    program = find_steve_program(programs, installed)
    if program is None:
        raise RuntimeError("Could not identify the running STEVE add-in safely.")
    previous_version = _installed_manifest_version(installed)
    if not _validate_installed_tree(installed, previous_version):
        raise RuntimeError("The current STEVE installation is not verified.")
    if journal_home is not None:
        begin_journal(journal_home, package, installed, expected_version)
        update_journal(journal_home, "stopping", previousVersion=previous_version)
    if not getattr(program, "isRunning", True):
        raise RuntimeError("STEVE is not running; restart Fusion to apply this update.")
    program.stop()
    if getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not stop STEVE; restart Fusion to apply this update.")
    rollback = swap_staged_addin(
        package, installed, expected_version, journal_home=journal_home,
        previous_version=previous_version,
    )
    if journal_home is not None:
        update_journal(journal_home, "installed")
    active_program = program
    try:
        purge_steve_modules(modules)
        if programs_provider is not None:
            refreshed = find_steve_program(programs_provider(), installed)
            if refreshed is None:
                report("fresh-script-missing")
                raise RuntimeError("Could not reacquire the running STEVE add-in safely.")
            active_program = refreshed
            report("fresh-script-found")
        if journal_home is not None:
            update_journal(journal_home, "starting")
        try:
            active_program.run()
        except Exception:
            report("run-raised")
            raise
        if not getattr(active_program, "isRunning", False) and programs_provider is not None:
            refreshed = find_steve_program(programs_provider(), installed)
            if refreshed is not None:
                active_program = refreshed
                report("post-run-script-found")
        if not getattr(active_program, "isRunning", False):
            report("run-not-running")
            raise RuntimeError("Fusion did not restart STEVE; restart Fusion to apply this update.")
        if installed_version is not None and installed_version() != expected_version:
            report("version-not-active")
            raise RuntimeError("Fusion restarted STEVE but the expected version is not active; restart Fusion to apply this update.")
        if journal_home is not None:
            update_journal(journal_home, "confirmed")
    except Exception:
        if journal_home is not None:
            try:
                update_journal(journal_home, "failed", error="live update failed; recovery is required")
            except Exception:
                pass
        try:
            active_program.stop()
        except Exception:
            pass
        rollback()
        purge_steve_modules(modules)
        rollback_running = False
        try:
            program.run()
            rollback_running = bool(getattr(program, "isRunning", False))
        except Exception:
            rollback_running = False
        if journal_home is not None and rollback_running:
            # A journaled rollback requires the later acknowledgement-bound
            # protocol; isRunning alone is insufficient to clear recovery.
            try:
                update_journal(journal_home, "rollback_pending")
            except Exception:
                pass
            raise RuntimeError("Rollback restored files but was not startup-confirmed; use the detached installer.")
        raise
    return expected_version
