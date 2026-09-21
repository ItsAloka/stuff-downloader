"""Custom Windows-11-inspired dark theme (pure QSS, no extra dependencies).

Text colours are chosen for WCAG AA contrast on their surfaces:
TEXT #f3f3f3 on BG #1c1c1f ≈ 16:1, TEXT_DIM #b4b4bb ≈ 9:1, ACCENT_TEXT #06121a on ACCENT ≈ 10:1.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

BG = "#1c1c1f"
SIDEBAR = "#161618"
SURFACE = "#26262a"
SURFACE_HOVER = "#2f2f34"
BORDER = "#36363c"
TEXT = "#f3f3f3"
TEXT_DIM = "#b4b4bb"
ACCENT = "#4cc2ff"
ACCENT_HOVER = "#75d0ff"
ACCENT_TEXT = "#06121a"
SUCCESS = "#6ccb5f"
WARNING = "#fcc84a"
DANGER = "#ff8a80"

STYLE = f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 10pt; }}
QMainWindow, QWidget#central, QStackedWidget, QWidget#page {{ background: {BG}; color: {TEXT}; }}
QLabel {{ color: {TEXT}; background: transparent; }}

/* Sidebar */
QWidget#sidebarPanel {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 600; padding: 18px 16px 14px 16px; }}
QLabel#brandTag {{ color: {TEXT_DIM}; font-size: 8.5pt; padding: 0 16px 12px 16px; }}
QListWidget#sidebar {{ background: transparent; border: none; outline: none; }}
QListWidget#sidebar::item {{ color: {TEXT_DIM}; padding: 10px 14px; margin: 2px 8px;
                             border-radius: 6px; border-left: 3px solid transparent; }}
QListWidget#sidebar::item:hover {{ background: {SURFACE}; color: {TEXT}; }}
QListWidget#sidebar::item:selected {{ background: {SURFACE_HOVER}; color: {TEXT};
                                      border-left: 3px solid {ACCENT}; }}

/* Page hierarchy */
QLabel#pageTitle {{ font-family: "Segoe UI Variable Display", "Segoe UI", sans-serif;
                    font-size: 20pt; font-weight: 600; }}
QLabel#pageSubtitle {{ color: {TEXT_DIM}; font-size: 10pt; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; padding-top: 6px; }}
QLabel#muted {{ color: {TEXT_DIM}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#emptyState {{ background: transparent; border: 1px dashed {BORDER}; border-radius: 10px; }}

/* Inputs */
QLineEdit {{ background: {BG}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px;
             padding: 8px 12px; selection-background-color: {ACCENT};
             selection-color: {ACCENT_TEXT}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
QLineEdit#urlEdit {{ font-size: 11pt; padding: 11px 14px; }}
QLineEdit[readOnly="true"] {{ color: {TEXT_DIM}; }}

/* Buttons */
QPushButton {{ background: {SURFACE_HOVER}; color: {TEXT}; border: 1px solid {BORDER};
               border-radius: 8px; padding: 8px 16px; }}
QPushButton:hover {{ background: #3a3a40; }}
QPushButton:pressed {{ background: {SURFACE}; }}
QPushButton:disabled {{ color: #7a7a82; background: {SURFACE}; border-color: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: none;
                       font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #2c4a5a; color: #8aa3b0; }}
QPushButton#iconButton {{ padding: 6px 10px; border-radius: 6px; }}
QPushButton#rowButton {{ padding: 2px 10px; border-radius: 6px; }}
QPushButton:focus {{ outline: none; border: 1px solid {ACCENT}; }}

/* Progress */
QProgressBar {{ background: {BORDER}; border: none; border-radius: 3px; max-height: 6px;
                min-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QProgressBar[state="completed"]::chunk {{ background: {SUCCESS}; }}
QProgressBar[state="failed"]::chunk {{ background: {DANGER}; }}
QProgressBar[state="cancelled"]::chunk {{ background: {WARNING}; }}
QProgressBar[state="paused"]::chunk {{ background: {WARNING}; }}
QProgressBar[state="skipped"]::chunk {{ background: {SUCCESS}; }}

/* Status chips */
QLabel#chip {{ border-radius: 10px; padding: 2px 10px; font-size: 9pt; font-weight: 600;
               background: {SURFACE_HOVER}; color: {TEXT_DIM}; }}
QLabel#chip[state="active"] {{ background: #133247; color: {ACCENT}; }}
QLabel#chip[state="completed"] {{ background: #1c3a1a; color: {SUCCESS}; }}
QLabel#chip[state="failed"] {{ background: #45201e; color: {DANGER}; }}
QLabel#chip[state="cancelled"] {{ background: #44381a; color: {WARNING}; }}
QLabel#chip[state="paused"] {{ background: #44381a; color: {WARNING}; }}
QLabel#chip[state="queued"] {{ background: #23242a; color: {TEXT_DIM}; }}
QLabel#chip[state="skipped"] {{ background: #1c3a1a; color: {SUCCESS}; }}
QLabel#chip[state="ok"] {{ background: #1c3a1a; color: {SUCCESS}; }}
QLabel#chip[state="missing"] {{ background: #45201e; color: {DANGER}; }}

/* Tables */
QTableWidget {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
                border-radius: 10px;
                gridline-color: transparent; selection-background-color: {SURFACE_HOVER};
                selection-color: {TEXT}; }}
QTableWidget::item {{ padding: 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{ background: {SURFACE}; color: {TEXT_DIM}; border: none;
                        border-bottom: 1px solid {BORDER}; padding: 8px; font-weight: 600; }}

/* Footer */
QWidget#footer {{ background: {SIDEBAR}; border-top: 1px solid {BORDER}; }}
QLabel#footerText {{ color: {TEXT_DIM}; font-size: 9pt; padding: 6px 14px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def set_state(widget: QWidget, state: str) -> None:
    """Set the dynamic ``state`` property and re-apply QSS so the new look takes effect."""
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
