"""Fusion add-in lifecycle helper.

This helper is installed beside STEVE and is intentionally independent of
STEVE's Python modules. The first migration release only installs it; the
live-update request path is disabled until a later release is validated in
Fusion.
"""
from pathlib import Path
import json
import sys
import threading
import traceback

import adsk.core

# Fusion exposes the helper's own directory while loading this add-in. Keep
# the helper self-contained so replacing STEVE cannot affect its imports.
from update_core import (  # noqa: E402
    apply_in_fusion,
    data_home,
    read_request,
    recover_journal,
)

ENABLED = True
POLL_SECONDS = 1.0
EVENT_ID = "10X_STEVE_Updater"
STEVE_NAME = "STEVE"
_app_instance = None
_event_handler = None
_custom_event = None
_stop_event = None
_worker = None
_last_request = None


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
        key = _request_key()
        if key is None or key == _last_request:
            continue
        try:
            _app_instance.fireCustomEvent(EVENT_ID)
        except RuntimeError:
            return


def _installed_version():
    manifest = _addins_root() / STEVE_NAME / "STEVE.manifest"
    return json.loads(manifest.read_text(encoding="utf-8")).get("version")


def _apply_pending():
    global _last_request
    key = _request_key()
    if key is None or key == _last_request:
        return
    _last_request = key
    try:
        request = read_request(data_home())
        if not request:
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
        )
        (data_home() / "pending-updates" / "live-update.json").unlink(missing_ok=True)
        _log("updated STEVE to " + request["version"])
    except Exception:
        _log("update failed; restart Fusion to recover: " + traceback.format_exc())


class UpdateEvent(adsk.core.CustomEventHandler):
    def notify(self, args):
        _apply_pending()


def run(context):
    global _app_instance, _event_handler, _custom_event, _stop_event, _worker
    if not ENABLED:
        _log("installed; in-Fusion updates are disabled pending live validation")
        return
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
