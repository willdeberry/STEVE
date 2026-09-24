"""Fusion add-in lifecycle helper.

This helper is installed beside STEVE and is intentionally independent of
STEVE's Python modules. It owns the default in-Fusion update path whenever
its runtime heartbeat is fresh; the detached installer remains the fallback.
"""
from pathlib import Path
import importlib.util
import json
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
recover_journal = _core.recover_journal
write_ready = _core.write_ready
version_is_newer = _core.version_is_newer
clear_ready = _core.clear_ready

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


def _app():
    return adsk.core.Application.get()


def _addins_root():
    return Path(__file__).resolve().parent.parent


def _log(message):
    try:
        _app().log("STEVEUpdater: " + message)
    except Exception:
        pass


def _request_key():
    request = data_home() / "pending-updates" / "live-update.json"
    try:
        stat = request.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return None


def _signal_worker():
    while _stop_event and not _stop_event.wait(POLL_SECONDS):
        try:
            _app_instance.fireCustomEvent(EVENT_ID)
        except RuntimeError:
            return


def _installed_version():
    manifest = _addins_root() / STEVE_NAME / "STEVE.manifest"
    return json.loads(manifest.read_text(encoding="utf-8")).get("version")


def _apply_pending():
    global _last_request, _failed_request
    key = _request_key()
    if key is None or key == _last_request or key == _failed_request:
        return
    try:
        request = read_request(data_home())
        if not request:
            return
        current = _installed_version()
        if not version_is_newer(request["version"], current):
            (data_home() / "pending-updates" / "live-update.json").unlink(missing_ok=True)
            _last_request = key
            _log("ignored stale update request for " + request["version"])
            return
        programs = list(_app().scripts.itemsByName(STEVE_NAME) or [])
        if not programs:
            raise RuntimeError("STEVE is not available in Fusion's Scripts collection.")
        apply_in_fusion(
            programs,
            request["package"],
            _addins_root() / STEVE_NAME,
            request["version"],
            modules=sys.modules,
            installed_version=_installed_version,
            journal_home=data_home(),
            programs_provider=lambda: list(_app().scripts.itemsByName(STEVE_NAME) or []),
        )
        (data_home() / "pending-updates" / "live-update.json").unlink(missing_ok=True)
        _last_request = key
        _failed_request = None
        _log("updated STEVE to " + request["version"])
    except Exception:
        _failed_request = key
        _log("update failed; restart Fusion to recover: " + traceback.format_exc())


class UpdateEvent(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            write_ready(data_home())
        except Exception:
            _log("custom event callback ran but readiness could not be recorded")
            return
        _apply_pending()


def run(context):
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker
    try:
        recover_journal(data_home())
    except Exception:
        _log("live update recovery failed; restart Fusion and use the detached installer")
        return
    _app_instance = _app()
    _stop_event = threading.Event()
    _event_handler = UpdateEvent()
    _custom_event = _app_instance.registerCustomEvent(EVENT_ID)
    _custom_event.add(_event_handler)
    _worker = threading.Thread(target=_signal_worker, name="STEVEUpdater-watch", daemon=True)
    _worker.start()
    _apply_pending()


def stop(context):
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker, _last_request
    if _stop_event:
        _stop_event.set()
    if _worker and _worker is not threading.current_thread():
        _worker.join(timeout=2)
    try:
        clear_ready(data_home())
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
