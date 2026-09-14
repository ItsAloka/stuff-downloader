"""Marshals core events (from runner reader threads) onto the Qt main thread."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from ..core.protocol import Event


class EventBridge(QObject):
    event_received = pyqtSignal(object)

    def post(self, event: Event) -> None:
        # Emitting from a worker thread to a main-thread receiver is a queued connection.
        self.event_received.emit(event)
