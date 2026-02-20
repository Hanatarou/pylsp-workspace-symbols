"""Tests for pylsp-workspace-symbols plugin."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Make sure the package is importable from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from pylsp_workspace_symbols.plugin import (
    _DEFAULT_IGNORE_FOLDERS,
    _in_ignored_folder,
    _search_symbols,
    pylsp_dispatchers,
    pylsp_experimental_capabilities,
    pylsp_settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(enabled=True, max_symbols=500, ignore_folders=None):
    return {
        "enabled": enabled,
        "max_symbols": max_symbols,
        "ignore_folders": ignore_folders or [],
    }


def _make_config(settings=None):
    cfg = MagicMock()
    cfg.plugin_settings.return_value = settings or _make_settings()
    return cfg


def _make_workspace(root_path="/tmp/project"):
    ws = MagicMock()
    ws.root_path = root_path
    return ws


def _make_jedi_name(name, type_="function", module_path="/tmp/project/mod.py",
                    module_name="mod", line=1, column=0):
    n = MagicMock()
    n.name = name
    n.type = type_
    n.module_path = Path(module_path) if module_path else None
    n.module_name = module_name
    n.line = line
    n.column = column
    return n


# ---------------------------------------------------------------------------
# _in_ignored_folder
# ---------------------------------------------------------------------------

class TestInIgnoredFolder:
    def test_ignores_venv(self):
        assert _in_ignored_folder("/project/.venv/lib/site.py", _DEFAULT_IGNORE_FOLDERS)

    def test_ignores_pycache(self):
        assert _in_ignored_folder("/project/pkg/__pycache__/mod.cpython-311.pyc",
                                  _DEFAULT_IGNORE_FOLDERS)

    def test_ignores_node_modules(self):
        assert _in_ignored_folder("/project/node_modules/react/index.js",
                                  _DEFAULT_IGNORE_FOLDERS)

    def test_ignores_egg_info(self):
        assert _in_ignored_folder("/project/mylib.egg-info/PKG-INFO",
                                  _DEFAULT_IGNORE_FOLDERS)

    def test_allows_normal_path(self):
        assert not _in_ignored_folder("/project/src/mymodule.py",
                                      _DEFAULT_IGNORE_FOLDERS)

    def test_windows_path_normalised(self):
        assert _in_ignored_folder(r"C:\project\.venv\lib\site.py",
                                  _DEFAULT_IGNORE_FOLDERS)


# ---------------------------------------------------------------------------
# pylsp_settings
# ---------------------------------------------------------------------------

class TestPylspSettings:
    def test_returns_defaults(self):
        cfg = MagicMock()
        result = pylsp_settings(cfg)
        plugin_cfg = result["plugins"]["jedi_workspace_symbols"]
        assert plugin_cfg["enabled"] is True
        assert plugin_cfg["max_symbols"] == 500
        assert plugin_cfg["ignore_folders"] == []


# ---------------------------------------------------------------------------
# pylsp_experimental_capabilities
# ---------------------------------------------------------------------------

class TestExperimentalCapabilities:
    def test_advertises_when_enabled(self):
        cfg = _make_config(_make_settings(enabled=True))
        ws = _make_workspace()
        result = pylsp_experimental_capabilities(cfg, ws)
        assert result == {"workspaceSymbolProvider": True}

    def test_empty_when_disabled(self):
        cfg = _make_config(_make_settings(enabled=False))
        ws = _make_workspace()
        result = pylsp_experimental_capabilities(cfg, ws)
        assert result == {}


# ---------------------------------------------------------------------------
# pylsp_dispatchers
# ---------------------------------------------------------------------------

class TestPylspDispatchers:
    def test_registers_handler_when_enabled(self):
        cfg = _make_config(_make_settings(enabled=True))
        ws = _make_workspace()
        dispatchers = pylsp_dispatchers(cfg, ws)
        assert "workspace/symbol" in dispatchers
        assert callable(dispatchers["workspace/symbol"])

    def test_empty_when_disabled(self):
        cfg = _make_config(_make_settings(enabled=False))
        ws = _make_workspace()
        assert pylsp_dispatchers(cfg, ws) == {}

    def test_handler_extracts_query_from_dict(self):
        cfg = _make_config(_make_settings(enabled=True))
        ws = _make_workspace()
        dispatchers = pylsp_dispatchers(cfg, ws)
        handler = dispatchers["workspace/symbol"]

        with patch("pylsp_workspace_symbols.plugin._search_symbols", return_value=[]) as mock_search:
            handler({"query": "foo"})
            mock_search.assert_called_once()
            _, _, query = mock_search.call_args[0]
            assert query == "foo"

    def test_handler_defaults_query_when_not_dict(self):
        cfg = _make_config(_make_settings(enabled=True))
        ws = _make_workspace()
        dispatchers = pylsp_dispatchers(cfg, ws)
        handler = dispatchers["workspace/symbol"]

        with patch("pylsp_workspace_symbols.plugin._search_symbols", return_value=[]) as mock_search:
            handler(None)
            _, _, query = mock_search.call_args[0]
            assert query == ""


# ---------------------------------------------------------------------------
# _search_symbols
# ---------------------------------------------------------------------------

class TestSearchSymbols:
    def _run(self, names, query="", max_symbols=500, ignore_folders=None):
        settings = _make_settings(max_symbols=max_symbols, ignore_folders=ignore_folders or [])
        ws = _make_workspace()
        mock_project = MagicMock()
        mock_project.complete_search.return_value = iter(names)

        with patch("pylsp_workspace_symbols.plugin._jedi") as mock_jedi:
            mock_jedi.Project.return_value = mock_project
            return _search_symbols(settings, ws, query)

    def test_returns_all_symbols_on_empty_query(self):
        names = [
            _make_jedi_name("calculate_area", "function"),
            _make_jedi_name("Circle", "class"),
        ]
        results = self._run(names, query="")
        assert len(results) == 2

    def test_substring_filter_case_insensitive(self):
        names = [
            _make_jedi_name("calculate_area", "function"),
            _make_jedi_name("Circle", "class"),
            _make_jedi_name("sum_list", "function"),
        ]
        results = self._run(names, query="calc")
        assert len(results) == 1
        assert results[0]["name"] == "calculate_area"

    def test_case_insensitive_match(self):
        names = [_make_jedi_name("Calculate", "function")]
        results = self._run(names, query="CALC")
        assert len(results) == 1

    def test_skips_param_type(self):
        names = [
            _make_jedi_name("my_param", "param"),
            _make_jedi_name("my_func", "function"),
        ]
        results = self._run(names, query="")
        assert len(results) == 1
        assert results[0]["name"] == "my_func"

    def test_skips_none_module_path(self):
        names = [_make_jedi_name("builtin_func", "function", module_path=None)]
        results = self._run(names, query="")
        assert results == []

    def test_respects_max_symbols(self):
        names = [_make_jedi_name(f"func_{i}", "function") for i in range(10)]
        results = self._run(names, query="", max_symbols=3)
        assert len(results) == 3

    def test_max_symbols_zero_means_no_limit(self):
        names = [_make_jedi_name(f"func_{i}", "function") for i in range(100)]
        results = self._run(names, query="", max_symbols=0)
        assert len(results) == 100

    def test_skips_ignored_folder(self):
        names = [
            _make_jedi_name("hidden", "function", module_path="/project/.venv/lib/mod.py"),
            _make_jedi_name("visible", "function", module_path="/project/src/mod.py"),
        ]
        results = self._run(names, query="")
        assert len(results) == 1
        assert results[0]["name"] == "visible"

    def test_result_structure(self):
        names = [_make_jedi_name("my_func", "function",
                                 module_path="/project/mod.py",
                                 module_name="mod", line=5, column=0)]
        results = self._run(names, query="my_func")
        assert len(results) == 1
        r = results[0]
        assert r["name"] == "my_func"
        assert r["kind"] == 12  # Function
        assert r["containerName"] == "mod"
        assert r["location"]["range"]["start"]["line"] == 4  # 1-based -> 0-based
        assert r["location"]["range"]["start"]["character"] == 0

    def test_returns_none_when_jedi_unavailable(self):
        settings = _make_settings()
        ws = _make_workspace()
        with patch("pylsp_workspace_symbols.plugin._jedi", None):
            result = _search_symbols(settings, ws, "")
        assert result is None

    def test_returns_none_on_jedi_exception(self):
        settings = _make_settings()
        ws = _make_workspace()
        with patch("pylsp_workspace_symbols.plugin._jedi") as mock_jedi:
            mock_jedi.Project.side_effect = RuntimeError("boom")
            result = _search_symbols(settings, ws, "")
        assert result is None
