"""Tests for the optional Textual TUI's startup/fallback DECISION logic. These never require
Textual to actually be installed (should_use_textual takes textual_installed as an explicit
injectable parameter for exactly this reason) -- the real App composition is smoke-tested
manually in a dev environment with Textual installed, not in this always-runs suite, since
Textual is never a hard dependency of this project.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import tui_textual


def test_should_use_textual_false_when_forced_cli():
    assert tui_textual.should_use_textual(force_cli=True, textual_installed=True) is False


def test_should_use_textual_false_when_not_interactive():
    assert tui_textual.should_use_textual(is_interactive=False, textual_installed=True) is False


def test_should_use_textual_false_when_not_installed():
    assert tui_textual.should_use_textual(textual_installed=False) is False


def test_should_use_textual_true_when_all_conditions_met():
    assert tui_textual.should_use_textual(force_cli=False, is_interactive=True,
                                           textual_installed=True) is True


def test_textual_available_reflects_real_import_state():
    # Whatever the real answer is, it must be a plain bool and never raise.
    result = tui_textual.textual_available()
    assert isinstance(result, bool)


def test_textual_unavailable_is_handled_gracefully(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name == "textual":
            raise ImportError("simulated: textual not installed")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert tui_textual.textual_available() is False
