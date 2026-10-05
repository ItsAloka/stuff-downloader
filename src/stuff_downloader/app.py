from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PyQt6.QtWidgets import QApplication

from .core import paths
from .gui.main_window import MainWindow

log = logging.getLogger(__name__)


def _log_to_file() -> None:
    """Keep a small log in the data folder: the windowed build has no console to print to."""
    try:
        folder = paths.data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / "stuff-downloader.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8"
        )
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def _keep_running(kind, value, tb) -> None:
    """An error escaping a Qt handler is logged, not fatal. PyQt aborts the whole process when
    the default hook is in place, which closed the window mid-download and lost the queue."""
    log.critical("Unhandled error", exc_info=(kind, value, tb))


def run_app() -> int:
    _log_to_file()
    sys.excepthook = _keep_running
    app = QApplication(sys.argv)
    app.setApplicationName("Stuff Downloader")
    window = MainWindow()
    window.show()
    return app.exec()
