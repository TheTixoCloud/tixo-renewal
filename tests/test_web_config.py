"""Tests for web_config.py: safe defaults (disabled, localhost-only) and persistence."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import web_config as wc


def test_default_settings_disabled_and_localhost():
    s = wc.WebSettings()
    assert s.enabled is False
    assert s.bind_host == "127.0.0.1"


def test_load_settings_absent_file_returns_safe_defaults(tmp_path):
    s = wc.load_web_settings(str(tmp_path))
    assert s.enabled is False
    assert s.bind_host == "127.0.0.1"


def test_save_and_load_roundtrips(tmp_path):
    config_dir = str(tmp_path)
    s = wc.WebSettings(enabled=True, bind_host="0.0.0.0", bind_port=9999,
                        default_language="fa", default_theme="light")
    wc.save_web_settings(config_dir, s)
    loaded = wc.load_web_settings(config_dir)
    assert loaded.enabled is True
    assert loaded.bind_host == "0.0.0.0"
    assert loaded.bind_port == 9999
    assert loaded.default_language == "fa"
    assert loaded.default_theme == "light"


def test_load_settings_malformed_file_falls_back_to_defaults(tmp_path):
    config_dir = str(tmp_path)
    path = wc.default_settings_path(config_dir)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write("{not valid json")
    s = wc.load_web_settings(config_dir)
    assert s.enabled is False


def test_invalid_port_rejected():
    with pytest.raises(ValueError):
        wc.WebSettings(bind_port=0)
    with pytest.raises(ValueError):
        wc.WebSettings(bind_port=70000)


def test_invalid_language_rejected():
    with pytest.raises(ValueError):
        wc.WebSettings(default_language="de")


def test_invalid_theme_rejected():
    with pytest.raises(ValueError):
        wc.WebSettings(default_theme="blue")
