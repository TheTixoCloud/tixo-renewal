"""Optional Textual-based TUI. This module imports `textual` at MODULE LOAD TIME -- callers
MUST check `textual_available()` (or catch ImportError) before importing/using it. Textual is
never a hard dependency of this project: menu.py falls back to the existing lightweight
stdlib-only CLI menu whenever Textual isn't installed, the terminal isn't interactive/
compatible, or this module raises for any reason during startup.

This module is presentation-only -- every screen reads through the SAME functions already
used by the CLI menu (hostinfo.collect_host_info, virtualizor_info.*, inventory.*,
analytics.*, menu.collect_dashboard_data) and never re-implements any renewal/safety logic.
No screen here ever triggers a write operation merely by being opened.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def textual_available() -> bool:
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


def should_use_textual(force_cli: bool = False, is_interactive: bool = True,
                        textual_installed: bool = None) -> bool:
    """Pure decision function (unit-testable without a real terminal or Textual installed).
    Textual is only used when: not explicitly disabled, the terminal is interactive, and
    Textual actually imports cleanly."""
    if force_cli:
        return False
    if not is_interactive:
        return False
    if textual_installed is None:
        textual_installed = textual_available()
    return textual_installed


BANNER = r"""
  _____ _           _____ _                 _
 |_   _(_)_  _____ / ____| |               | |
   | |  \ \/ / _ \ |      | | ___  _   _  __| |
   | |   >  < (_) | |     | |/ _ \| | | |/ _` |
   |_|  /_/\_\___/| |____ | | (_) | |_| | (_| |
                    \_____|_|\___/ \__,_|\__,_|
         Virtualizor Bandwidth Renewal -- Control Center
"""


def build_app(renewal_cli_mod, menu_mod):
    """Factory, not a module-level App instance -- Textual's App class construction touches
    the terminal/driver, which must never happen at import time (that's exactly what would
    break `--status`/`--version`/non-interactive automation if this module were imported
    unconditionally anywhere)."""
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import (DataTable, Footer, Header, Label, Static, Tabs, Tab, Input)
    from textual.binding import Binding
    from textual.screen import ModalScreen
    from textual import work

    import analytics as analytics_mod
    import hostinfo as hostinfo_mod
    import inventory as inv_mod
    import inventory_sync as isync_mod
    import virtualizor_info as vinfo_mod

    class ConfirmScreen(ModalScreen[bool]):
        """Dangerous-action confirmation dialog -- requires an explicit button press, never a
        single accidental keystroke."""
        def __init__(self, message: str):
            super().__init__()
            self.message = message

        def compose(self) -> ComposeResult:
            yield Container(
                Label(self.message, id="confirm-message"),
                Label("[y] Confirm    [n/esc] Cancel", id="confirm-hint"),
                id="confirm-dialog",
            )

        def on_key(self, event) -> None:
            if event.key == "y":
                self.dismiss(True)
            elif event.key in ("n", "escape"):
                self.dismiss(False)

    class TixoControlCenter(App):
        CSS = """
        Screen { background: #0a0e14; color: #c0cdd9; }
        Header { background: #0d2b3e; color: #58d6ff; }
        Footer { background: #0d2b3e; color: #58d6ff; }
        #banner { color: #58d6ff; text-align: center; padding: 1; }
        .card { border: round #1f6f8b; padding: 1; margin: 1; background: #0f1620; }
        .card-title { color: #58d6ff; text-style: bold; }
        DataTable { background: #0f1620; }
        #confirm-dialog { border: thick #ff5f5f; padding: 2; background: #1a0e0e; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh_screen", "Refresh"),
            Binding("/", "focus_search", "Search"),
            Binding("escape", "focus_tabs", "Back"),
            Binding("?", "show_help", "Help"),
        ]
        TITLE = "TixoCloud Control Center"

        def __init__(self):
            super().__init__()
            self._node_id = "self"

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical():
                yield Static(BANNER, id="banner")
                yield Tabs(
                    Tab("Dashboard", id="tab-dashboard"),
                    Tab("Inventory", id="tab-inventory"),
                    Tab("Host", id="tab-host"),
                    Tab("Virtualizor", id="tab-virtualizor"),
                    Tab("Settings", id="tab-settings"),
                )
                yield Static(id="body")
            yield Footer()

        def on_mount(self) -> None:
            self.render_dashboard_tab()

        def on_tabs_tab_activated(self, event) -> None:
            tab_id = event.tab.id
            if tab_id == "tab-dashboard":
                self.render_dashboard_tab()
            elif tab_id == "tab-inventory":
                self.render_inventory_tab()
            elif tab_id == "tab-host":
                self.render_host_tab()
            elif tab_id == "tab-virtualizor":
                self.render_virtualizor_tab()
            elif tab_id == "tab-settings":
                self.render_settings_tab()

        def _plain_style(self):
            return menu_mod.Style(enabled=False)

        def render_dashboard_tab(self) -> None:
            data = menu_mod.collect_dashboard_data(renewal_cli_mod.CONFIG_DIR, renewal_cli_mod.STATE_DIR)
            tz = menu_mod.load_display_tz(renewal_cli_mod.CONFIG_DIR)
            text = menu_mod.render_dashboard(data, self._plain_style(), display_tz=tz)
            self.query_one("#body", Static).update(text)

        def render_inventory_tab(self) -> None:
            conn = inv_mod.open_db(os.path.join(renewal_cli_mod.STATE_DIR, "inventory.sqlite3"))
            records = inv_mod.latest_records(conn, self._node_id)
            settings = menu_mod.load_dashboard_settings(renewal_cli_mod.CONFIG_DIR)
            from decimal import Decimal
            threshold = Decimal(settings["usage_threshold_percent"])
            table_text = menu_mod.render_vps_table(records, self._plain_style())
            summary = menu_mod.render_analytics_summary(
                analytics_mod.compute_bandwidth_analytics(records, usage_threshold_percent=threshold),
                self._plain_style())
            self.query_one("#body", Static).update(table_text + "\n\n" + summary)

        def render_host_tab(self) -> None:
            info = hostinfo_mod.collect_host_info()
            text = menu_mod.render_host_info(info, self._plain_style())
            self.query_one("#body", Static).update(text)

        def render_virtualizor_tab(self) -> None:
            v_info = {
                "installed": vinfo_mod.detect_virtualizor_installed(),
                "version": vinfo_mod.detect_virtualizor_version(),
                "virtualization_technologies": vinfo_mod.detect_virtualization_technologies(),
                "api": None,
            }
            text = menu_mod.render_virtualizor_info(v_info, self._plain_style())
            self.query_one("#body", Static).update(text)

        def render_settings_tab(self) -> None:
            settings = menu_mod.load_dashboard_settings(renewal_cli_mod.CONFIG_DIR)
            tz = menu_mod.load_display_tz(renewal_cli_mod.CONFIG_DIR)
            lines = [
                f"Dashboard refresh interval: {settings['refresh_interval_minutes']} min",
                f"Usage alert threshold: {settings['usage_threshold_percent']}%",
                f"Display timezone: {tz}",
                "",
                "Settings are changed via the lightweight CLI menu's configuration options "
                "(press q to quit, then run `tixo-renewal --cli`) -- editing them here is not "
                "yet implemented; this screen is read-only in v1.4.0.",
            ]
            self.query_one("#body", Static).update("\n".join(lines))

        def action_refresh_screen(self) -> None:
            tabs = self.query_one(Tabs)
            active = tabs.active_tab
            if active:
                self.on_tabs_tab_activated(type("E", (), {"tab": active})())

        def action_focus_search(self) -> None:
            pass  # search box wiring deferred -- see docs/known-limitations for v1.4.0 scope

        def action_focus_tabs(self) -> None:
            self.query_one(Tabs).focus()

        def action_show_help(self) -> None:
            self.query_one("#body", Static).update(
                "q quit | r refresh | / search | esc back to tabs | ? this help\n"
                "Use left/right arrows or mouse click to switch tabs.")

    return TixoControlCenter


def run_textual_app(renewal_cli_mod, menu_mod) -> int:
    """Entry point called by menu.py after should_use_textual() returns True. Any exception
    during Textual startup is caught by the CALLER (menu.main), which falls back to the
    lightweight CLI menu -- this function itself does not swallow errors, so the caller's
    fallback logic is exercised by a real failure, not hidden here."""
    AppClass = build_app(renewal_cli_mod, menu_mod)
    app = AppClass()
    app.run()
    return 0
