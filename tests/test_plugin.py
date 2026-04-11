"""Tests for pylsp-workspace-symbols plugin."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure the package is importable from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from pylsp_workspace_symbols.plugin import (
    _DEFAULT_IGNORE_FOLDERS,
    _cl_cache_get,
    _cl_cache_set,
    _in_ignored_folder,
    _literal_type,
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
    def test_workspace_symbols_defaults(self):
        cfg = MagicMock()
        result = pylsp_settings(cfg)
        ws = result["plugins"]["jedi_workspace_symbols"]
        assert ws["enabled"] is True
        assert ws["max_symbols"] == 500
        assert ws["ignore_folders"] == []

    def test_call_hierarchy_defaults(self):
        cfg = MagicMock()
        assert pylsp_settings(cfg)["plugins"]["call_hierarchy"]["enabled"] is True

    def test_type_hierarchy_defaults(self):
        cfg = MagicMock()
        assert pylsp_settings(cfg)["plugins"]["type_hierarchy"]["enabled"] is True

    def test_document_links_defaults(self):
        cfg = MagicMock()
        assert pylsp_settings(cfg)["plugins"]["document_links"]["enabled"] is True

    def test_document_colors_defaults(self):
        cfg = MagicMock()
        assert pylsp_settings(cfg)["plugins"]["document_colors"]["enabled"] is True

    def test_code_lens_defaults(self):
        cfg = MagicMock()
        cl = pylsp_settings(cfg)["plugins"]["code_lens"]
        assert cl["enabled"] is True
        assert cl["show_references"] is True
        assert cl["show_implementations"] is True
        assert cl["show_run"] is True
        assert cl["show_tests"] is True
        assert cl["max_definitions"] == 150

    def test_semantic_tokens_disabled_by_default(self):
        cfg = MagicMock()
        assert pylsp_settings(cfg)["plugins"]["semantic_tokens"]["enabled"] is False


# ---------------------------------------------------------------------------
# pylsp_experimental_capabilities
# ---------------------------------------------------------------------------

class TestExperimentalCapabilities:
    def test_advertises_when_enabled(self):
        cfg = _make_config(_make_settings(enabled=True))
        ws = _make_workspace()
        from pylsp_workspace_symbols import plugin
        result = pylsp_experimental_capabilities(cfg, ws)
        if plugin._CAPS_INJECTED:
            assert result == {}
        else:
            assert result["workspaceSymbolProvider"] is True
            assert "inlayHintProvider" in result

    def test_empty_when_disabled(self):
        cfg = _make_config(_make_settings(enabled=False))
        ws = _make_workspace()
        result = pylsp_experimental_capabilities(cfg, ws)
        assert result == {}


# ---------------------------------------------------------------------------
# pylsp_dispatchers
# ---------------------------------------------------------------------------

class TestPylspDispatchers:
    def test_registers_workspace_symbol_handler(self):
        cfg = _make_config(_make_settings(enabled=True))
        dispatchers = pylsp_dispatchers(cfg, _make_workspace())
        assert "workspace/symbol" in dispatchers
        assert callable(dispatchers["workspace/symbol"])

    def test_empty_when_disabled(self):
        cfg = _make_config(_make_settings(enabled=False))
        assert pylsp_dispatchers(cfg, _make_workspace()) == {}

    def test_handler_extracts_query_from_dict(self):
        cfg = _make_config(_make_settings(enabled=True))
        dispatchers = pylsp_dispatchers(cfg, _make_workspace())
        handler = dispatchers["workspace/symbol"]
        with patch("pylsp_workspace_symbols.plugin._search_symbols", return_value=[]) as mock_search:
            handler({"query": "foo"})
            mock_search.assert_called_once()
            _, _, query = mock_search.call_args[0]
            assert query == "foo"

    def test_handler_defaults_query_when_not_dict(self):
        cfg = _make_config(_make_settings(enabled=True))
        dispatchers = pylsp_dispatchers(cfg, _make_workspace())
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
        assert len(self._run(names, query="")) == 2

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
        assert len(self._run(names, query="CALC")) == 1

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
        assert self._run(names, query="") == []

    def test_respects_max_symbols(self):
        names = [_make_jedi_name(f"func_{i}", "function") for i in range(10)]
        assert len(self._run(names, query="", max_symbols=3)) == 3

    def test_max_symbols_zero_means_no_limit(self):
        names = [_make_jedi_name(f"func_{i}", "function") for i in range(100)]
        assert len(self._run(names, query="", max_symbols=0)) == 100

    def test_skips_ignored_folder(self):
        names = [
            _make_jedi_name("hidden", "function", module_path="/tmp/project/.venv/lib/mod.py"),
            _make_jedi_name("visible", "function", module_path="/tmp/project/src/mod.py"),
        ]
        results = self._run(names, query="")
        assert len(results) == 1
        assert results[0]["name"] == "visible"

    def test_result_structure(self):
        names = [_make_jedi_name("my_func", "function",
                                 module_path="/tmp/project/mod.py",
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
        with patch("pylsp_workspace_symbols.plugin._jedi", None):
            result = _search_symbols(_make_settings(), _make_workspace(), "")
        assert result is None

    def test_returns_none_on_jedi_exception(self):
        with patch("pylsp_workspace_symbols.plugin._jedi") as mock_jedi:
            mock_jedi.Project.side_effect = RuntimeError("boom")
            result = _search_symbols(_make_settings(), _make_workspace(), "")
        assert result is None


# ---------------------------------------------------------------------------
# _literal_type
# ---------------------------------------------------------------------------

class TestLiteralType:
    def test_none(self):           assert _literal_type("None")    == "None"
    def test_bool_true(self):      assert _literal_type("True")    == "bool"
    def test_bool_false(self):     assert _literal_type("False")   == "bool"
    def test_int(self):            assert _literal_type("42")      == "int"
    def test_negative_int(self):   assert _literal_type("-7")      == "int"
    def test_float(self):          assert _literal_type("3.14")    == "float"
    def test_string_double(self):  assert _literal_type('"hello"') == "str"
    def test_string_single(self):  assert _literal_type("'hello'") == "str"
    def test_fstring(self):        assert _literal_type('f"hi {x}"') == "str"
    def test_bytes(self):          assert _literal_type('b"data"') == "bytes"
    def test_list(self):           assert _literal_type("[1, 2]")  == "list"
    def test_tuple_explicit(self): assert _literal_type("(1, 2)")  == "tuple"
    def test_dict(self):           assert _literal_type('{"k": 1}') == "dict"
    def test_set(self):            assert _literal_type("{1, 2}")  == "set"
    def test_lambda(self):         assert _literal_type("lambda x: x") == "Callable"
    def test_implicit_tuple(self): assert _literal_type("1, 'a'") == "tuple"
    def test_unknown(self):        assert _literal_type("func()")  is None
    def test_empty(self):          assert _literal_type("")        is None


# ---------------------------------------------------------------------------
# _cl_cache
# ---------------------------------------------------------------------------

class TestClCache:
    def setup_method(self):
        from pylsp_workspace_symbols import plugin
        with plugin._CL_CACHE_LOCK:
            plugin._CL_CACHE.clear()

    def test_miss_on_empty_cache(self):
        assert _cl_cache_get("file:///a.py", "abc123") is None

    def test_hit_after_set(self):
        lenses = [{"range": {}, "command": {"title": "test"}}]
        _cl_cache_set("file:///a.py", "abc123", lenses)
        assert _cl_cache_get("file:///a.py", "abc123") == lenses

    def test_miss_on_wrong_hash(self):
        _cl_cache_set("file:///a.py", "abc123", [])
        assert _cl_cache_get("file:///a.py", "wrong") is None

    def test_miss_on_wrong_uri(self):
        _cl_cache_set("file:///a.py", "abc123", [])
        assert _cl_cache_get("file:///b.py", "abc123") is None

    def test_overwrite_same_uri(self):
        _cl_cache_set("file:///a.py", "h1", [{"old": True}])
        _cl_cache_set("file:///a.py", "h2", [{"new": True}])
        assert _cl_cache_get("file:///a.py", "h2") == [{"new": True}]
        assert _cl_cache_get("file:///a.py", "h1") is None


# ---------------------------------------------------------------------------
# pylsp_code_lens (hook)
# ---------------------------------------------------------------------------

class TestPylspCodeLens:
    def _make_doc(self, source, path="/tmp/project/mod.py"):
        doc = MagicMock()
        doc.source = source
        doc.path = path
        doc.uri = f"file://{path}"
        return doc

    def _make_cfg(self, enabled=True):
        cfg = MagicMock()
        cfg.plugin_settings.return_value = {
            "enabled": enabled,
            "show_references": True,
            "show_implementations": True,
            "show_run": True,
            "show_tests": True,
            "max_definitions": 150,
        }
        return cfg

    def test_returns_empty_when_disabled(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        doc = self._make_doc("def foo(): pass")
        assert pylsp_code_lens(self._make_cfg(enabled=False), _make_workspace(), doc) == []

    def test_returns_list_for_simple_function(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        doc = self._make_doc("def foo():\n    pass\n")
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        assert isinstance(result, list)
        titles = [l["command"]["title"] for l in result]
        assert any("reference" in t for t in titles)

    def test_run_lens_on_main_block(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = 'if __name__ == "__main__":\n    pass\n'
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        titles = [l["command"]["title"] for l in result]
        assert "\u25b6 Run" in titles

    def test_test_lens_on_test_function(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        doc = self._make_doc("def test_something():\n    assert True\n")
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        titles = [l["command"]["title"] for l in result]
        assert any("Run test" in t for t in titles)

    def test_implementations_lens_on_subclass(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = "class Base:\n    pass\n\nclass Child(Base):\n    pass\n"
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        titles = [l["command"]["title"] for l in result]
        assert any("implementation" in t for t in titles)

    def test_returns_empty_on_syntax_error(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        doc = self._make_doc("def (broken syntax")
        assert pylsp_code_lens(self._make_cfg(), _make_workspace(), doc) == []

    def test_cache_used_on_repeated_call(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens, _CL_CACHE, _CL_CACHE_LOCK
        with _CL_CACHE_LOCK:
            _CL_CACHE.clear()
        source = "def foo():\n    pass\n"
        doc = self._make_doc(source)
        ws = _make_workspace()
        result1 = pylsp_code_lens(self._make_cfg(), ws, doc)
        result2 = pylsp_code_lens(self._make_cfg(), ws, doc)
        assert result1 == result2


# ---------------------------------------------------------------------------
# dispatcher feature registration
# ---------------------------------------------------------------------------

class TestDispatcherFeatureRegistration:
    def _cfg(self, overrides=None):
        base = {
            "call_hierarchy":        {"enabled": True},
            "type_hierarchy":        {"enabled": True},
            "document_links":        {"enabled": True},
            "document_colors":       {"enabled": True},
            "jedi_workspace_symbols":{"enabled": True},
            "inlay_hints":           {"enabled": True},
            "code_lens":             {"enabled": True},
            "semantic_tokens":       {"enabled": True},
        }
        if overrides:
            base.update(overrides)
        cfg = MagicMock()
        cfg.plugin_settings.side_effect = lambda key: base.get(key, {})
        return cfg

    def test_all_handlers_registered(self):
        d = pylsp_dispatchers(self._cfg(), _make_workspace())
        for method in (
            "workspace/symbol",
            "textDocument/inlayHint",
            "textDocument/prepareCallHierarchy",
            "callHierarchy/incomingCalls",
            "callHierarchy/outgoingCalls",
            "textDocument/prepareTypeHierarchy",
            "typeHierarchy/supertypes",
            "typeHierarchy/subtypes",
            "textDocument/documentLink",
            "textDocument/documentColor",
            "textDocument/colorPresentation",
            "textDocument/semanticTokens/full",
            "textDocument/semanticTokens/full/delta",
            "textDocument/semanticTokens/range",
        ):
            assert method in d, f"Missing dispatcher: {method}"

    def test_semantic_tokens_absent_when_disabled(self):
        d = pylsp_dispatchers(self._cfg({"semantic_tokens": {"enabled": False}}), _make_workspace())
        assert "textDocument/semanticTokens/full" not in d

    def test_call_hierarchy_absent_when_disabled(self):
        d = pylsp_dispatchers(self._cfg({"call_hierarchy": {"enabled": False}}), _make_workspace())
        assert "callHierarchy/incomingCalls" not in d

    def test_type_hierarchy_absent_when_disabled(self):
        d = pylsp_dispatchers(self._cfg({"type_hierarchy": {"enabled": False}}), _make_workspace())
        assert "typeHierarchy/supertypes" not in d

    def test_document_links_absent_when_disabled(self):
        d = pylsp_dispatchers(self._cfg({"document_links": {"enabled": False}}), _make_workspace())
        assert "textDocument/documentLink" not in d

    def test_document_colors_absent_when_disabled(self):
        d = pylsp_dispatchers(self._cfg({"document_colors": {"enabled": False}}), _make_workspace())
        assert "textDocument/documentColor" not in d
        assert "textDocument/colorPresentation" not in d


# ---------------------------------------------------------------------------
# pylsp_settings - cross_file_implementations
# ---------------------------------------------------------------------------

class TestCrossFileImplementationsSetting:
    def test_default_is_false(self):
        cfg = MagicMock()
        result = pylsp_settings(cfg)
        cl = result["plugins"]["code_lens"]
        assert cl["cross_file_implementations"] is False

    def test_other_code_lens_defaults_unchanged(self):
        cfg = MagicMock()
        cl = pylsp_settings(cfg)["plugins"]["code_lens"]
        assert cl["show_references"] is True
        assert cl["show_implementations"] is True
        assert cl["show_run"] is True
        assert cl["show_tests"] is True
        assert cl["max_definitions"] == 150


# ---------------------------------------------------------------------------
# pylsp_code_lens - run/test commands
# ---------------------------------------------------------------------------

class TestCodeLensCommands:
    def _make_doc(self, source, path="/tmp/project/mod.py"):
        doc = MagicMock()
        doc.source = source
        doc.path = path
        doc.uri = f"file://{path}"
        return doc

    def _make_cfg(self):
        cfg = MagicMock()
        cfg.plugin_settings.return_value = {
            "enabled": True,
            "show_references": True,
            "show_implementations": True,
            "cross_file_implementations": False,
            "show_run": True,
            "show_tests": True,
            "max_definitions": 150,
        }
        return cfg

    def test_run_lens_has_real_command(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = 'if __name__ == "__main__":\n    pass\n'
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        run_lenses = [l for l in result if "▶" in l["command"]["title"]]
        assert len(run_lenses) == 1
        assert run_lenses[0]["command"]["command"] == "pylsp_workspace_symbols.run_file"
        assert run_lenses[0]["command"]["arguments"][0]["path"] == doc.path

    def test_run_test_lens_has_real_command_for_function(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = "def test_something():\n    assert True\n"
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        test_lenses = [l for l in result if "Run test" in l["command"]["title"]]
        assert len(test_lenses) == 1
        cmd = test_lenses[0]["command"]
        assert cmd["command"] == "pylsp_workspace_symbols.run_test"
        args = cmd["arguments"][0]
        assert args["path"] == doc.path
        assert args["name"] == "test_something"
        assert args["kind"] == "function"

    def test_run_test_lens_has_real_command_for_class(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        import unittest as _ut
        source = "import unittest\nclass TestFoo(unittest.TestCase):\n    pass\n"
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(), _make_workspace(), doc)
        test_lenses = [l for l in result if "Run test" in l["command"]["title"]]
        assert any(l["command"]["arguments"][0]["kind"] == "class" for l in test_lenses)


# ---------------------------------------------------------------------------
# _find_cross_file_subclasses
# ---------------------------------------------------------------------------

class TestFindCrossFileSubclasses:
    def test_returns_empty_when_jedi_unavailable(self):
        from pylsp_workspace_symbols.plugin import _find_cross_file_subclasses
        with patch("pylsp_workspace_symbols.plugin._jedi", None):
            result = _find_cross_file_subclasses("/tmp/x.py", 1, 0, "/tmp")
        assert result == []

    def test_returns_empty_on_missing_file(self):
        from pylsp_workspace_symbols.plugin import _find_cross_file_subclasses
        result = _find_cross_file_subclasses("/nonexistent/file.py", 1, 0, "/nonexistent")
        assert result == []

    def test_returns_list_type(self):
        from pylsp_workspace_symbols.plugin import _find_cross_file_subclasses
        # Valid call with real workspace - should return a list (possibly empty)
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            src = tmp + "/base.py"
            open(src, "w").write("class Base:\n    pass\n")
            result = _find_cross_file_subclasses(src, 1, 6, tmp)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# cross_file_implementations in code lens (integration)
# ---------------------------------------------------------------------------

class TestCrossFileImplementationsLens:
    def _make_cfg(self, cross_file=False):
        cfg = MagicMock()
        cfg.plugin_settings.return_value = {
            "enabled": True,
            "show_references": True,
            "show_implementations": True,
            "cross_file_implementations": cross_file,
            "show_run": True,
            "show_tests": True,
            "max_definitions": 150,
        }
        return cfg

    def _make_doc(self, source, path="/tmp/project/base.py"):
        doc = MagicMock()
        doc.source = source
        doc.path = path
        doc.uri = f"file://{path}"
        return doc

    def test_intra_file_implementations_always_counted(self):
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = "class Base:\n    pass\n\nclass Child(Base):\n    pass\n"
        doc = self._make_doc(source)
        result = pylsp_code_lens(self._make_cfg(cross_file=False), _make_workspace(), doc)
        impl_lenses = [l for l in result if "implementation" in l["command"]["title"]]
        assert any("1" in l["command"]["title"] for l in impl_lenses)

    def test_cross_file_disabled_no_extra_io(self):
        """With cross_file=False, _find_cross_file_subclasses should never be called."""
        from pylsp_workspace_symbols import plugin
        from pylsp_workspace_symbols.plugin import pylsp_code_lens
        source = "class Base:\n    pass\n"
        doc = self._make_doc(source)
        with patch.object(plugin, "_find_cross_file_subclasses", wraps=plugin._find_cross_file_subclasses) as mock_cf:
            pylsp_code_lens(self._make_cfg(cross_file=False), _make_workspace(), doc)
            mock_cf.assert_not_called()


# ---------------------------------------------------------------------------
# _get_semantic_tokens
# ---------------------------------------------------------------------------

class TestSemanticTokens:
    # Tests for semantic token classification added in v0.6.0.

    def _get_tokens(self, source, path='/tmp/test.py'):
        from pylsp_workspace_symbols.plugin import (
            _get_semantic_tokens, _ST_TOKEN_TYPES, _ST_TOKEN_MODIFIERS)
        data = _get_semantic_tokens(source, path)
        tokens = []
        line = col = 0
        i = 0
        while i + 5 <= len(data):
            dl, dc, length, tidx, mmask = data[i:i+5]
            line += dl
            col = dc if dl > 0 else col + dc
            type_name = next(
                (k for k, v in _ST_TOKEN_TYPES.items() if v == tidx), None)
            mods = {k for k, v in _ST_TOKEN_MODIFIERS.items() if mmask & (1 << v)}
            tokens.append((line, col, length, type_name, mods))
            i += 5
        return tokens

    def _find(self, tokens, name, source):
        lines = source.splitlines()
        return [
            (ln, col, length, typ, mods)
            for ln, col, length, typ, mods in tokens
            if ln < len(lines) and lines[ln][col:col+length] == name
        ]

    def test_classmethod_gets_static_modifier(self):
        src = 'class Foo:\n    @classmethod\n    def bar(cls): pass\n'
        bar = self._find(self._get_tokens(src), 'bar', src)
        assert bar, 'token bar not found'
        assert any('static' in m for *_, m in bar)

    def test_staticmethod_gets_static_modifier(self):
        src = 'class Foo:\n    @staticmethod\n    def baz(x): pass\n'
        baz = self._find(self._get_tokens(src), 'baz', src)
        assert any('static' in m for *_, m in baz)

    def test_regular_method_no_static(self):
        src = 'class Foo:\n    def bar(self): pass\n'
        bar = self._find(self._get_tokens(src), 'bar', src)
        assert bar
        assert not any('static' in m for *_, m in bar)

    def test_modification_after_reassignment(self):
        # modification fires on references that follow a prior definition.
        # Uses a real temp file so Jedi can resolve references correctly.
        import tempfile, os
        src = 'x = 0\nprint(x)\nx = 1\nprint(x)\n'
        with tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False,
                                         dir=tempfile.gettempdir()) as f:
            f.write(src)
            path = f.name
        try:
            x_tokens = self._find(self._get_tokens(src, path), 'x', src)
            assert any('modification' in m for *_, m in x_tokens)
        finally:
            os.unlink(path)

    def test_typevar_gets_readonly(self):
        src = 'from typing import TypeVar\nT = TypeVar("T")\n'
        t = self._find(self._get_tokens(src), 'T', src)
        assert any('readonly' in m for *_, m in t)

    def test_paramspec_gets_readonly(self):
        src = 'from typing import ParamSpec\nP = ParamSpec("P")\n'
        p = self._find(self._get_tokens(src), 'P', src)
        assert any('readonly' in m for *_, m in p)

    def test_typevartuple_gets_readonly(self):
        src = 'from typing import TypeVarTuple\nTs = TypeVarTuple("Ts")\n'
        ts = self._find(self._get_tokens(src), 'Ts', src)
        assert ts, 'token Ts not found'
        assert any('readonly' in m for *_, m in ts)

    def test_enum_member_gets_readonly(self):
        src = 'import enum\nclass Color(enum.Enum):\n    RED = 1\n'
        red = self._find(self._get_tokens(src), 'RED', src)
        assert red and any(t[3] == 'enumMember' for t in red)
        assert any('readonly' in m for *_, m in red)

    def test_classvar_gets_readonly(self):
        src = 'from typing import ClassVar\nclass R:\n    _n: ClassVar[int] = 0\n'
        n = self._find(self._get_tokens(src), '_n', src)
        assert n, 'token _n not found'
        assert any('readonly' in m for *_, m in n)

    def test_deprecated_via_docstring(self):
        src = 'def old(x):\n    """Deprecated: use new instead."""\n    return x\n'
        fn = self._find(self._get_tokens(src), 'old', src)
        assert fn, 'token old not found'
        assert any('deprecated' in m for *_, m in fn)


# ---------------------------------------------------------------------------
# _search_symbols - multi-root workspace
# ---------------------------------------------------------------------------

class TestSearchSymbolsMultiRoot:
    def _run_multi(self, names_per_root, query="", max_symbols=500):
        """Simulate multiple workspace roots via server.workspaces."""
        from pylsp_workspace_symbols.plugin import _search_symbols

        fake_workspaces = {}
        for i, (root, names) in enumerate(names_per_root.items()):
            ws = MagicMock()
            ws.root_path = root
            fake_workspaces[str(i)] = ws

        workspace = MagicMock()
        workspace.root_path = next(iter(names_per_root))
        workspace._endpoint._dispatcher.workspaces = fake_workspaces

        settings = _make_settings(max_symbols=max_symbols)

        call_count = [0]
        original_names = list(names_per_root.values())

        def fake_project(path, sys_path):
            idx = call_count[0]
            call_count[0] += 1
            proj = MagicMock()
            proj.complete_search.return_value = iter(original_names[idx])
            return proj

        with patch("pylsp_workspace_symbols.plugin._jedi") as mock_jedi:
            mock_jedi.Project.side_effect = fake_project
            return _search_symbols(settings, workspace, query)

    def test_symbols_from_both_roots_returned(self):
        names_a = [_make_jedi_name("func_a", "function",
                                   module_path="/proj_a/mod.py")]
        names_b = [_make_jedi_name("func_b", "function",
                                   module_path="/proj_b/mod.py")]
        results = self._run_multi({"/proj_a": names_a, "/proj_b": names_b})
        result_names = {r["name"] for r in results}
        assert "func_a" in result_names
        assert "func_b" in result_names

    def test_single_root_still_works(self):
        names = [_make_jedi_name("only_func", "function",
                                 module_path="/proj/mod.py")]
        results = self._run_multi({"/proj": names})
        assert len(results) == 1
        assert results[0]["name"] == "only_func"

    def test_max_symbols_respected_across_roots(self):
        names_a = [_make_jedi_name(f"a_{i}", "function",
                                   module_path=f"/proj_a/m{i}.py") for i in range(5)]
        names_b = [_make_jedi_name(f"b_{i}", "function",
                                   module_path=f"/proj_b/m{i}.py") for i in range(5)]
        results = self._run_multi({"/proj_a": names_a, "/proj_b": names_b},
                                  max_symbols=6)
        assert len(results) == 6
