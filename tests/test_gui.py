import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["VCAMSIM_NO_SERVICE"] = "1"

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from vcamsim import gui
from vcamsim.config import AppCfg, CameraCfg, to_dict


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def settle(app, predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def window(app, tmp_path):
    path = tmp_path / "config.yaml"
    AppCfg().save(path)
    w = gui.MainWindow(str(path))
    w.timer.stop()
    w.preview_timer.stop()
    yield w
    w.engine = None
    w.close()
    w.deleteLater()
    app.processEvents()


def test_failed_attach_retains_unsaved_edits(app, window, monkeypatch):
    window.cfg.cameras = [CameraCfg(name="Unsaved")]
    class Remote:
        mode = "service"
        def __init__(self, cfg, port):
            self.client = SimpleNamespace(put_config=lambda _: False)
        def refresh(self): return True
    monkeypatch.setattr(gui, "RemoteEngine", Remote)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    window._attach_service()
    settle(app, lambda: window._task_thread is None)
    assert window.cfg.cameras[0].name == "Unsaved"
    assert window._dirty()
    assert window.engine is None


def test_save_is_async_and_persists_edits(app, window):
    window.cfg.cameras = [CameraCfg(name="Saved")]
    window.save()
    settle(app, lambda: window._task_thread is None)
    assert not window._dirty()
    assert AppCfg.load(window.config_path).cameras[0].name == "Saved"


def test_filter_and_empty_state(app, window):
    assert not window.empty_state.isHidden()
    window.cfg.cameras = [CameraCfg(name="Lobby"), CameraCfg(name="Garage")]
    window.rebuild_table()
    window.camera_search.setText("garage")
    assert window.table.isRowHidden(0)
    assert not window.table.isRowHidden(1)
    assert window.empty_state.isHidden()
    window.camera_search.clear()
    assert not window.table.isRowHidden(0)


def test_password_is_hidden(app):
    dialog = gui.CameraDialog(CameraCfg())
    assert dialog.pwd.echoMode() == QLineEdit.Password
    dialog.deleteLater()
