from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .gui.main_window import MainWindow


def run_app() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Stuff Downloader")
    window = MainWindow()
    window.show()
    return app.exec()
