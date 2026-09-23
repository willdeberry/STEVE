"""Self-contained lifecycle update core used by STEVEUpdater.

This module must not import anything from the replaceable STEVE add-in.
"""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from uuid import uuid4

REQUEST_NAME = "live-update.json"
JOURNAL_NAME = "live-update-journal.json"


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
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def begin_journal(home, package, installed, expected_version, transaction_id=None):
    journal = _journal_path(home)
    payload = {
        "transactionId": transaction_id or uuid4().hex,
        "package": str(Path(package).resolve()),
        "installed": str(Path(installed).resolve()),
        "expectedVersion": expected_version,
        "phase": "validated",
    }
    _write_json(journal, payload)
    return journal


def load_journal(home):
    journal = _journal_path(home)
    if not journal.is_file() or journal.is_symlink():
        return None
    payload = json.loads(journal.read_text(encoding="utf-8"))
    required = {"transactionId", "package", "installed", "expectedVersion", "phase"}
    if not required.issubset(payload) or not isinstance(payload["transactionId"], str):
        raise ValueError("The live update journal is invalid.")
    return payload


def update_journal(home, phase, **changes):
    payload = load_journal(home)
    if payload is None:
        raise ValueError("The live update journal does not exist.")
    if not isinstance(phase, str) or phase not in {"validated", "stopping", "backed_up", "installed", "starting", "confirmed", "failed"}:
        raise ValueError("The live update journal phase is invalid.")
    payload.update(changes, phase=phase)
    _write_json(_journal_path(home), payload)
    return payload


def clear_journal(home):
    _journal_path(home).unlink(missing_ok=True)


def recover_journal(home):
    """Recover an interrupted transaction before accepting new requests."""
    payload = load_journal(home)
    if payload is None:
        return False
    if payload.get("phase") == "confirmed":
        clear_journal(home)
        return True
    backup_value = payload.get("backup")
    installed_value = payload.get("installed")
    if not isinstance(backup_value, str) or not isinstance(installed_value, str):
        raise ValueError("The live update journal has no safe recovery paths.")
    backup = Path(backup_value).resolve()
    installed = Path(installed_value).resolve()
    if (backup.parent != installed.parent or not backup.name.startswith(".STEVE-rollback-")
            or installed.name != "STEVE"):
        raise ValueError("The live update journal contains unsafe recovery paths.")
    if backup.is_symlink() or not backup.is_dir():
        raise ValueError("The live update backup is missing or unsafe.")
    if installed.exists() or installed.is_symlink():
        raise ValueError("The live update recovery is ambiguous; both installed and backup paths exist.")
    backup.rename(installed)
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


def write_request(home, package, version):
    if not _valid_version(version):
        raise ValueError("The live update request has an invalid version.")
    package = _trusted_staged(home, package)
    request = request_path(home)
    request.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=request.parent,
                                        prefix=".live-update-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"version": version, "package": str(package)}, stream, separators=(",", ":"))
            stream.flush()
        temporary.replace(request)
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
        if set(payload) != {"version", "package"}:
            raise ValueError("invalid fields")
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
        return {"version": version, "package": str(package)}
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
        if path.is_symlink() or not path.is_file():
            raise ValueError("The staged update contains an unexpected file.")
        actual_files.add(relative)
    if actual_files != listed:
        raise ValueError("The staged update contains an unexpected file.")


def find_steve_program(programs, expected_path=None):
    matches = []
    for program in programs or ():
        if not getattr(program, "isValid", False) or getattr(program, "name", None) != "STEVE":
            continue
        if expected_path is not None:
            location = getattr(program, "location", None)
            if location is None:
                continue
            location_path = Path(str(location))
            expected = Path(expected_path)
            if location_path.is_symlink() or expected.is_symlink():
                continue
            if location_path.resolve() != expected.resolve():
                continue
        matches.append(program)
    return matches[0] if len(matches) == 1 else None


def purge_steve_modules(modules=None):
    modules = sys.modules if modules is None else modules
    for name in list(modules):
        if name == "STEVE" or name == "steve" or name.startswith("steve."):
            modules.pop(name, None)


def swap_staged_addin(package, installed, expected_version, journal_home=None):
    package = Path(package).resolve()
    installed = Path(installed).resolve()
    verify_staged(package, expected_version)
    source = package / "STEVE"
    if installed.name != "STEVE" or installed.parent == installed:
        raise ValueError("The installed STEVE path is invalid.")
    backup = installed.with_name(f".STEVE-rollback-{uuid4().hex}")
    if journal_home is not None:
        update_journal(journal_home, "stopping", backup=str(backup))
    installed.rename(backup)
    if journal_home is not None:
        update_journal(journal_home, "backed_up", backup=str(backup))
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


def apply_in_fusion(programs, package, installed, expected_version, modules=None,
                    installed_version=None, journal_home=None):
    verify_staged(package, expected_version)
    program = find_steve_program(programs, installed)
    if program is None:
        raise RuntimeError("Could not identify the running STEVE add-in safely.")
    if journal_home is not None:
        begin_journal(journal_home, package, installed, expected_version)
        update_journal(journal_home, "stopping")
    if not getattr(program, "isRunning", True):
        raise RuntimeError("STEVE is not running; restart Fusion to apply this update.")
    program.stop()
    if getattr(program, "isRunning", False):
        raise RuntimeError("Fusion did not stop STEVE; restart Fusion to apply this update.")
    rollback = swap_staged_addin(package, installed, expected_version, journal_home=journal_home)
    if journal_home is not None:
        update_journal(journal_home, "installed")
    try:
        purge_steve_modules(modules)
        if journal_home is not None:
            update_journal(journal_home, "starting")
        program.run()
        if not getattr(program, "isRunning", False):
            raise RuntimeError("Fusion did not restart STEVE; restart Fusion to apply this update.")
        if installed_version is not None and installed_version() != expected_version:
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
            program.stop()
        except Exception:
            pass
        rollback()
        purge_steve_modules(modules)
        try:
            program.run()
        except Exception:
            pass
        raise
    return expected_version
