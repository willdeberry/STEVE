"""Fusion add-in lifecycle helper.

This helper is installed beside STEVE and is intentionally independent of
STEVE's Python modules. It owns the default in-Fusion update path whenever
its runtime heartbeat is fresh; the detached installer remains the fallback.
"""
from pathlib import Path
import importlib.util
import json
import os
import sys
import threading
import traceback

import adsk.core

# Fusion does not guarantee that this add-in's directory is on sys.path.
# Load the sibling core explicitly and keep it independent of replaceable STEVE.
_core_path = Path(__file__).resolve().with_name("update_core.py")
_core_spec = importlib.util.spec_from_file_location("steve_updater_update_core", _core_path)
if _core_spec is None or _core_spec.loader is None:
    raise ImportError(f"Could not load updater core: {_core_path}")
_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_core)
apply_in_fusion = _core.apply_in_fusion
data_home = _core.data_home
read_request = _core.read_request
load_journal = _core.load_journal
update_journal = _core.update_journal
recover_journal = _core.recover_journal
write_ready = _core.write_ready
version_is_newer = _core.version_is_newer
clear_ready = _core.clear_ready
ready_path = _core.ready_path
revoke_ready = _core.revoke_ready
clear_ready_revocation = _core.clear_ready_revocation
clear_journal = _core.clear_journal
prepare_in_fusion = _core.prepare_in_fusion
confirm_in_fusion = _core.confirm_in_fusion
rollback_in_fusion = _core.rollback_in_fusion
confirm_rollback = _core.confirm_rollback
confirmed_matches = _core.confirmed_matches
confirmed_commit_verified = _core.confirmed_commit_verified

POLL_SECONDS = 1.0
EVENT_ID = "10X_STEVE_Updater"
STEVE_NAME = "STEVE"
_app_instance = None
_event_handler = None
_custom_event = None
_stop_event = None
_worker = None
_last_request = None
_failed_request = None
_pending_transaction = None
_rollback_transaction = None
_CONFIRM_ATTEMPTS = 5
_confirm_attempts = 0
_recovery_blocked = False


def _app():
    return adsk.core.Application.get()


def _addins_root():
    return Path(__file__).resolve().parent.parent


def _log(message):
    try:
        _app().log("STEVEUpdater: " + message)
    except Exception:
        pass


def _request_id(request):
    return request.get("requestId") if isinstance(request, dict) else None


def _request_matches_transaction(request, transaction):
    return (
        isinstance(request, dict)
        and request.get("requestId") == transaction.get("requestId")
        and request.get("version") == transaction.get("expectedVersion")
        and Path(request.get("package", "")).resolve() == Path(transaction.get("package", "")).resolve()
    )


def _signal_worker():
    while _stop_event and not _stop_event.wait(POLL_SECONDS):
        try:
            _app_instance.fireCustomEvent(EVENT_ID)
        except RuntimeError:
            return


def _reconstruct_rollback_from_journal():
    """Rebuild an acknowledgement-bound rollback transaction after a callback failure."""
    global _rollback_transaction, _recovery_blocked
    journal = load_journal(data_home())
    if not journal:
        return False
    if journal.get("phase") == "rollback_restoring":
        try:
            recover_journal(data_home())
        except Exception:
            journal = load_journal(data_home())
        else:
            journal = load_journal(data_home())
    if not journal or journal.get("phase") != "rollback_pending":
        return False
    transaction = {
        "transactionId": journal["transactionId"],
        "package": journal["package"],
        "installed": journal["installed"],
        "expectedVersion": journal["expectedVersion"],
        "requestId": journal["requestId"],
        "startAttemptId": journal["startAttemptId"],
        "previousVersion": journal["previousVersion"],
        "expectedTree": journal["expectedTree"],
        "previousTree": journal["previousTree"],
        "phase": "rollback_pending",
        "rollbackStartRequested": journal["rollbackStartRequested"],
    }
    if (not isinstance(transaction["rollbackStartRequested"], bool)
            or not _core._valid_tree_inventory(transaction["expectedTree"])
            or not _core._valid_tree_inventory(transaction["previousTree"])):
        raise ValueError("The reconstructed rollback evidence is invalid.")
    _rollback_transaction = transaction
    _recovery_blocked = False
    return True


def _installed_version():
    manifest = _addins_root() / STEVE_NAME / "STEVE.manifest"
    return json.loads(manifest.read_text(encoding="utf-8")).get("version")


def _revoke_and_clear_ready_safely():
    try:
        mode = revoke_ready(data_home())
        if mode == "marker":
            _clear_ready_safely()
            return not ready_path(data_home()).exists()
        # A revoked heartbeat is only a last-resort reader-visible block; it
        # cannot safely authorize this helper to continue, since write_ready()
        # could later replace it.
        return False
    except Exception:
        _log("readiness could not be durably revoked; detached installer required")
        return False


def _fail_startup_safely():
    """Withdraw helper readiness without touching durable update evidence."""
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker, _recovery_blocked
    _recovery_blocked = True
    _revoke_and_clear_ready_safely()
    if _stop_event:
        try:
            _stop_event.set()
        except Exception:
            pass
    if _worker and _worker is not threading.current_thread():
        try:
            _worker.join(timeout=2)
        except Exception:
            pass
    if _custom_event:
        try:
            _custom_event.remove(_event_handler)
        except Exception:
            pass
    if _app_instance:
        try:
            _app_instance.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
    _app_instance = _event_handler = _custom_event = _stop_event = _worker = None


def _clear_ready_safely():
    try:
        clear_ready(data_home())
    except Exception:
        try:
            revoke_ready(data_home())
        except Exception:
            pass
        _log("readiness cleanup failed; persisted revocation remains authoritative")


def _journal_blocks_updates():
    try:
        journal = load_journal(data_home())
        if journal and journal.get("phase") == "confirmed":
            if not confirmed_commit_verified(data_home(), journal):
                return True
            try:
                clear_journal(data_home())
            except Exception:
                return True
            return False
        return bool(journal)
    except Exception:
        return True


def _apply_pending():
    global _last_request, _failed_request, _pending_transaction, _rollback_transaction, _confirm_attempts, _recovery_blocked
    if _recovery_blocked:
        return
    if _rollback_transaction is not None:
        rollback_transaction = _rollback_transaction
        try:
            confirm_rollback(
                rollback_transaction,
                lambda: list(_app().scripts.itemsByName(STEVE_NAME) or []),
                rollback_transaction["previousVersion"],
                journal_home=data_home(),
            )
            _failed_request = rollback_transaction.get("requestId")
            _rollback_transaction = None
            _pending_transaction = None
            _confirm_attempts = 0
            _log("live update rolled back; use the detached installer")
            return
        except Exception:
            _confirm_attempts += 1
            if _confirm_attempts < _CONFIRM_ATTEMPTS:
                _log("lifecycle rollback confirmation pending (attempt "
                     + str(_confirm_attempts) + "/" + str(_CONFIRM_ATTEMPTS) + ")")
                return
            _log("live update recovery failed; restart Fusion and use the detached installer")
            _failed_request = rollback_transaction.get("requestId")
            try:
                update_journal(data_home(), "recovery_blocked", error="rollback confirmation exhausted")
            except Exception:
                _log("live update recovery state could not be persisted")
            _recovery_blocked = True
            _clear_ready_safely()
            _confirm_attempts = 0
            return
    if _pending_transaction is not None:
        transaction = _pending_transaction
        try:
            request = read_request(data_home())
            if not _request_matches_transaction(request, transaction):
                raise RuntimeError("The live update request was replaced before confirmation.")
            confirm_in_fusion(
                transaction,
                lambda: list(_app().scripts.itemsByName(STEVE_NAME) or []),
                installed_version=_installed_version,
                journal_home=data_home(),
                modules=sys.modules,
            )
            request = read_request(data_home())
            if request and confirmed_matches(
                    data_home(), request, transaction["installed"], transaction_id=transaction["transactionId"]):
                _log("updated STEVE to " + request["version"] + " for transaction " + transaction["transactionId"])
            else:
                _log("STEVE transaction " + transaction["transactionId"]
                     + " confirmed; a newer queued request remains")
            _pending_transaction = None
            _confirm_attempts = 0
            _last_request = None
            _failed_request = None
            return
        except Exception:
            _confirm_attempts += 1
            _log("lifecycle confirmation pending (attempt "
                 + str(_confirm_attempts) + "/" + str(_CONFIRM_ATTEMPTS) + ")")
            if _confirm_attempts < _CONFIRM_ATTEMPTS:
                return
            transaction = _pending_transaction
            try:
                _rollback_transaction = rollback_in_fusion(
                    transaction,
                    lambda: list(_app().scripts.itemsByName(STEVE_NAME) or []),
                    journal_home=data_home(),
                )
                _log("lifecycle rollback prepared; waiting for later Fusion callback")
            except Exception:
                _log("live update recovery failed; restart Fusion and use the detached installer")
                try:
                    if not _reconstruct_rollback_from_journal():
                        journal = load_journal(data_home())
                        if journal and journal.get("phase") not in {"rollback_restoring", "rollback_pending"}:
                            update_journal(data_home(), "recovery_blocked", error="rollback preparation failed")
                            _recovery_blocked = True
                except Exception:
                    _recovery_blocked = True
                    _clear_ready_safely()
            _confirm_attempts = 0
            return
    try:
        request = read_request(data_home())
    except Exception:
        _clear_ready_safely()
        _failed_request = "invalid-request"
        _log("live update request is legacy or invalid; use the detached installer")
        return
    if not request:
        return
    request_id = _request_id(request)
    if request_id is None or request_id == _last_request or request_id == _failed_request:
        return
    try:
        current = _installed_version()
        if confirmed_matches(data_home(), request, _addins_root() / STEVE_NAME):
            _last_request = request_id
            _log("ignored already-confirmed update request for " + request["version"])
            return
        if not version_is_newer(request["version"], current):
            _last_request = request_id
            _log("ignored stale update request for " + request["version"])
            return
        programs = list(_app().scripts.itemsByName(STEVE_NAME) or [])
        if not programs:
            raise RuntimeError("STEVE is not available in Fusion's Scripts collection.")
        _pending_transaction = prepare_in_fusion(
            programs,
            request["package"],
            _addins_root() / STEVE_NAME,
            request["version"],
            journal_home=data_home(),
            request_id=request_id,
        )
        _confirm_attempts = 0
        _log("lifecycle prepared; waiting for later Fusion callback")
    except Exception:
        _failed_request = request_id
        try:
            recovered = _reconstruct_rollback_from_journal()
            if recovered:
                _clear_ready_safely()
            else:
                journal = load_journal(data_home())
                if journal is not None:
                    update_journal(data_home(), "recovery_blocked", error="update preparation failed")
                    _recovery_blocked = True
                    _clear_ready_safely()
        except Exception:
            _recovery_blocked = True
            _clear_ready_safely()
        _log("update failed; restart Fusion to recover: " + traceback.format_exc())


class UpdateEvent(adsk.core.CustomEventHandler):
    def notify(self, args):
        if _recovery_blocked:
            _clear_ready_safely()
            return
        journal_present = _journal_blocks_updates()
        transaction_in_progress = _pending_transaction is not None or _rollback_transaction is not None
        if journal_present and not transaction_in_progress:
            _clear_ready_safely()
            return
        if journal_present:
            # Existing transaction callbacks may advance recovery, but do not
            # publish readiness while the journal still blocks new requests.
            _apply_pending()
            return
        try:
            write_ready(data_home())
        except Exception:
            _log("custom event callback ran but readiness could not be recorded")
            return
        _apply_pending()


def run(context):
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker, _rollback_transaction, _recovery_blocked
    if os.name == "nt":
        _recovery_blocked = True
        _clear_ready_safely()
        _log("in-Fusion updates disabled on Windows; use the detached installer")
        return
    _recovery_blocked = False
    if not _revoke_and_clear_ready_safely():
        _recovery_blocked = True
        _log("live update readiness could not be durably revoked; use the detached installer")
        return
    try:
        recover_journal(data_home())
        journal = load_journal(data_home())
        if journal is not None and journal.get("phase") != "rollback_pending":
            _clear_ready_safely()
            _recovery_blocked = True
            _log("live update recovery remains pending; use the detached installer")
            return
        if journal is not None:
            required = ("transactionId", "package", "installed", "expectedVersion", "requestId",
                        "startAttemptId", "previousVersion")
            if (any(not isinstance(journal.get(field), str) or not journal.get(field)
                    for field in required)
                    or not _core._valid_request_id(journal["requestId"])
                    or not _core._valid_request_id(journal["startAttemptId"])
                    or not _core._valid_version(journal["previousVersion"])):
                raise ValueError("The rollback journal is incomplete or invalid.")
            _rollback_transaction = {
                "transactionId": journal["transactionId"],
                "package": journal["package"],
                "installed": journal["installed"],
                "expectedVersion": journal["expectedVersion"],
                "requestId": journal["requestId"],
                "startAttemptId": journal["startAttemptId"],
                "previousVersion": journal["previousVersion"],
                "expectedTree": journal["expectedTree"],
                "previousTree": journal["previousTree"],
                "phase": "rollback_pending",
                "rollbackStartRequested": journal["rollbackStartRequested"]
                    if "rollbackStartRequested" in journal else False,
            }
            if not isinstance(_rollback_transaction["rollbackStartRequested"], bool):
                raise ValueError("The rollback start flag is invalid.")
    except Exception:
        _clear_ready_safely()
        _recovery_blocked = True
        _log("live update recovery failed; restart Fusion and use the detached installer")
        return
    try:
        _app_instance = _app()
        _stop_event = threading.Event()
        _event_handler = UpdateEvent()
        _custom_event = _app_instance.registerCustomEvent(EVENT_ID)
        _custom_event.add(_event_handler)
        _worker = threading.Thread(target=_signal_worker, name="STEVEUpdater-watch", daemon=True)
        _worker.start()
        clear_ready_revocation(data_home())
    except Exception:
        _fail_startup_safely()
        _log("helper startup failed; use the detached installer")
        return


def stop(context):
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker
    global _last_request, _failed_request, _pending_transaction, _rollback_transaction, _confirm_attempts
    if _stop_event:
        _stop_event.set()
    if _worker and _worker is not threading.current_thread():
        _worker.join(timeout=2)
    try:
        _clear_ready_safely()
    except Exception:
        pass
    if _custom_event and _event_handler:
        try:
            _custom_event.remove(_event_handler)
        except RuntimeError:
            pass
    if _app_instance:
        try:
            _app_instance.unregisterCustomEvent(EVENT_ID)
        except RuntimeError:
            pass
    _app_instance = _event_handler = _custom_event = _stop_event = _worker = None
    _last_request = None
    _failed_request = None
    _pending_transaction = None
    _rollback_transaction = None
    _confirm_attempts = 0
