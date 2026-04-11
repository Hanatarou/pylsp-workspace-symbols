"""pylsp-workspace-symbols: workspace/symbol, inlay hints, semantic tokens and more for python-lsp-server via Jedi.

Strategy: pylsp's hookspecs.py does not define hookspecs for workspace symbols,
inlay hints or semantic tokens as proper capabilities, so this plugin uses a
two-pronged approach:

  1. Capability injection (preferred): at import time, monkey-patch
     PythonLSPServer.capabilities() to insert workspaceSymbolProvider,
     inlayHintProvider and semanticTokensProvider directly into the proper
     capabilities dict.  This makes the plugin work out-of-the-box with clients
     that require proper capabilities (eglot, Neovim, CudaText, etc.).

  2. Fallback via pylsp_experimental_capabilities: if the injection fails
     (e.g. pylsp changed its internal API), the capabilities are announced
     via the experimental channel instead.

  3. Register a custom JSON-RPC dispatcher via pylsp_dispatchers that intercepts
     "workspace/symbol", "textDocument/inlayHint",
     "textDocument/semanticTokens/full" and "textDocument/semanticTokens/range"
     and calls our Jedi-backed implementations.

Semantic tokens implementation notes:
  - Uses jedi.Script.get_names(all_scopes=True) for a single O(n) pass over
    the file - no per-token ``goto`` calls, keeping latency low.
  - Token types follow the standard LSP legend that cuda_bun_lsp already
    declares in its initialize request, so no client changes are needed.
  - Modifiers: ``definition`` is set on definition sites; ``async`` on async
    functions/methods; ``defaultLibrary`` on builtins/stdlib names.
  - Disabled by default; enable via pylsp.plugins.semantic_tokens.enabled.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
import ast as _ast
import threading
import token as _token
import tokenize as _tokenize
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pylsp import hookimpl, uris

# NOTE: Jedi is already imported for workspace symbols, reuse it for inlay hints
try:
    import jedi as _jedi
except ImportError:  # pragma: no cover
    _jedi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

HAS_INLAY_DEPS = _jedi is not None
if HAS_INLAY_DEPS:
    log.info("pylsp_workspace_symbols: Jedi available for inlay hints (Jedi-only mode)")
else:
    log.warning("pylsp_workspace_symbols: Jedi not available - inlay hints disabled")

# ---------------------------------------------------------------------------
# Capability injection (monkey-patch)
# ---------------------------------------------------------------------------
# pylsp has no hookspec for proper capabilities, so we attempt to inject
# inlayHintProvider and workspaceSymbolProvider directly into
# PythonLSPServer.capabilities() at import time.
#
# Fallback: if the injection fails (pylsp internal API changed), the plugin
# continues to work via pylsp_experimental_capabilities below - clients that
# honour experimental capabilities will still receive the providers.
# ---------------------------------------------------------------------------

def _inject_capabilities() -> bool:
    """Inject inlayHintProvider and workspaceSymbolProvider into pylsp's
    server capabilities via monkey-patching.

    Returns True if injection succeeded, False otherwise.
    The caller must not raise on False - the experimental hook is the fallback.
    """
    try:
        from pylsp import python_lsp
        _original = python_lsp.PythonLSPServer.capabilities

        def _patched(self):
            caps = _original(self)
            caps.setdefault("workspaceSymbolProvider", True)
            caps.setdefault("inlayHintProvider", {
                "resolveProvider": False,
                "workDoneProgress": True,
            })
            caps.setdefault("callHierarchyProvider", True)
            caps.setdefault("typeHierarchyProvider", True)
            caps.setdefault("documentLinkProvider", {"resolveProvider": False})
            caps.setdefault("colorProvider", True)
            caps.setdefault("codeLensProvider", {"resolveProvider": False})
            # Merge our commands into executeCommandProvider without clobbering
            # pylsp-rope's existing commands (they register via the same key).
            existing_cmds = caps.get("executeCommandProvider", {}).get("commands", [])
            our_cmds = [
                "pylsp_workspace_symbols.run_file",
                "pylsp_workspace_symbols.run_test",
            ]
            caps["executeCommandProvider"] = {
                "commands": existing_cmds + [c for c in our_cmds if c not in existing_cmds]
            }
            caps.setdefault("semanticTokensProvider", {
                "legend": {
                    "tokenTypes": list(_ST_TOKEN_TYPES.keys()),
                    "tokenModifiers": list(_ST_TOKEN_MODIFIERS.keys()),
                },
                "full": {"delta": True},
                "range": True,
            })
            return caps

        python_lsp.PythonLSPServer.capabilities = _patched
        log.info("pylsp_workspace_symbols: capabilities injected into PythonLSPServer")
        return True
    except Exception as e:  # pragma: no cover
        log.warning(
            "pylsp_workspace_symbols: capability injection failed (%s) "
            "- falling back to pylsp_experimental_capabilities",
            e,
        )
        return False


# True -> proper capabilities announced (eglot, Neovim, etc. work out of the box)
# False -> fallback to pylsp_experimental_capabilities (CudaText, VSCode, etc.)
_CAPS_INJECTED = _inject_capabilities()

# Jedi name type -> LSP SymbolKind (1-based, per LSP spec)
_SYMBOL_KIND: Dict[str, int] = {
    "module": 2,        # Module
    "class": 5,         # Class
    "instance": 13,     # Variable
    "function": 12,     # Function
    "param": 13,        # Variable
    "path": 1,          # File
    "keyword": 14,      # Constant
    "property": 7,      # Property
    "statement": 13,    # Variable
}
_DEFAULT_KIND = 13  # Variable

# Folders to skip - noisy and irrelevant for symbol search
_DEFAULT_IGNORE_FOLDERS = {
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", ".env", "env",
    "dist", "build", ".eggs", "egg-info",  # matches any *.egg-info folder
}

# ---------------------------------------------------------------------------
# Semantic token legend
# Must match the tokenTypes / tokenModifiers declared in cuda_bun_lsp's
# initialize request (lsp_client.py SemanticTokensManager.TOKEN_TYPES /
# TOKEN_MODIFIERS) so the client can interpret the indices correctly.
# ---------------------------------------------------------------------------

# Token type name -> index in the legend list sent to the client.
# selfParameter and clsParameter extend the standard LSP legend so that
# the semantictokens.py client can apply distinct colors to self/cls.
_ST_TOKEN_TYPES: Dict[str, int] = {
    "namespace":      0,
    "type":           1,
    "class":          2,
    "enum":           3,
    "interface":      4,
    "struct":         5,
    "typeParameter":  6,
    "parameter":      7,
    "variable":       8,
    "property":       9,
    "enumMember":     10,
    "event":          11,
    "function":       12,
    "method":         13,
    "macro":          14,
    "keyword":        15,
    "modifier":       16,
    "comment":        17,
    "string":         18,
    "number":         19,
    "regexp":         20,
    "operator":       21,
    "decorator":      22,
    "selfParameter":  23,  # Python self -- distinct from regular parameter
    "clsParameter":   24,  # Python cls -- distinct from regular parameter
}

# Token modifier name -> bit index
_ST_TOKEN_MODIFIERS: Dict[str, int] = {
    "declaration":    0,
    "definition":     1,
    "readonly":       2,
    "static":         3,
    "deprecated":     4,
    "abstract":       5,
    "async":          6,
    "modification":   7,
    "documentation":  8,
    "defaultLibrary": 9,
    # Extensions matching basedpyright's legend for accurate cross-server parity
    "builtin":        10,  # builtins-module symbols (subset of defaultLibrary)
    "classMember":    11,  # methods/properties declared inside a class body
    "parameter":      12,  # applied to parameter/selfParameter/clsParameter tokens
}

# Jedi name type -> LSP semantic token type name
_JEDI_TYPE_TO_ST: Dict[str, str] = {
    "module":    "namespace",
    "class":     "class",
    "function":  "function",
    "instance":  "variable",
    "param":     "parameter",
    "keyword":   "keyword",
    "property":  "property",
    "statement": "variable",
    "path":      "namespace",
}

# stdlib / builtin module names for defaultLibrary modifier detection
_STDLIB_TOP = frozenset({
    "builtins", "__builtins__", "abc", "ast", "asyncio", "collections",
    "contextlib", "copy", "dataclasses", "datetime", "enum", "functools",
    "io", "itertools", "json", "logging", "math", "operator", "os",
    "pathlib", "pickle", "re", "shutil", "signal", "socket", "struct",
    "subprocess", "sys", "threading", "time", "traceback", "typing",
    "types", "unittest", "urllib", "warnings", "weakref",
})


# Cache: (path, hash(source)) -> jedi.Script.  Keyed by content hash so
# edits before save always get a fresh Script (jedi.Script is immutable).
_JEDI_CACHE: Dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()

# Semantic tokens delta cache: uri -> (result_id, data[]).
# Allows computing SemanticTokensDelta without re-running Jedi.
_ST_CACHE: Dict[str, tuple] = {}  # uri -> (result_id: str, data: List[int])
_ST_CACHE_LOCK = threading.Lock()
_ST_RESULT_ID_COUNTER: List[int] = [0]  # mutable counter shared across calls
_ST_COUNTER_LOCK = threading.Lock()     # dedicated lock - separate from cache lock


@hookimpl
def pylsp_settings(config) -> dict:
    """Declare default configuration for this plugin."""
    return {
        "plugins": {
            "jedi_workspace_symbols": {
                "enabled": True,
                "max_symbols": 500,
                "ignore_folders": [],
            },
            "inlay_hints": {
                "enabled": True,
                "show_assign_types": True,          # show types in assignments
                "show_return_types": True,          # show return types
                "show_raises": True,                # show raised exceptions
                "show_parameter_hints": True,       # show parameter names in function calls
                "max_hints_per_file": 200,          # maximum hints per file
            },
            "call_hierarchy": {
                "enabled": True,
            },
            "type_hierarchy": {
                "enabled": True,
            },
            "document_links": {
                "enabled": True,
            },
            "document_colors": {
                "enabled": True,
            },
            "code_lens": {
                "enabled": True,
                "show_references": True,
                "show_implementations": True,
                "cross_file_implementations": False,  # opt-in: adds I/O per class/method
                "show_run": True,
                "show_tests": True,
                "max_definitions": 150,
            },
            "semantic_tokens": {
                "enabled": False,  # opt-in: can be slow on very large files
            },
        }
    }


@hookimpl
def pylsp_code_lens(config, workspace, document) -> List[dict]:
    """pylsp hook: textDocument/codeLens - Jedi-backed code lenses.

    Called directly by pylsp (has native hookspec), so this hook takes
    priority over the dispatcher for codeLens requests.

    Returns lenses for:
      - "👥 N references"     on every top-level function, method, and class
      - "🔗 N implementations" on classes with subclasses and methods with overrides
                               (intra-file always; cross-file when cross_file_implementations=True)
      - "▶ Run"               on ``if __name__ == "__main__":`` blocks
                               (command: pylsp_workspace_symbols.run_file)
      - "🧪 Run test"         on ``test_*`` functions and ``Test*`` classes
                               (command: pylsp_workspace_symbols.run_test)
    """
    settings_cl = config.plugin_settings("code_lens")
    if not settings_cl.get("enabled", True):
        return []
    if not HAS_INLAY_DEPS:
        return []
    try:
        root_path = getattr(workspace, "root_path", None)
        return _get_code_lenses(
            document.source,
            document.path,
            document.uri,
            settings_cl,
            workspace_root=root_path,
        )
    except Exception as exc:
        log.exception("pylsp_workspace_symbols: pylsp_code_lens: %s", exc)
        return []


@hookimpl
def pylsp_experimental_capabilities(config, workspace) -> dict:
    """Advertise workspaceSymbolProvider and inlayHintProvider as fallback.

    Only used when direct capability injection into PythonLSPServer failed.
    If _CAPS_INJECTED is True, capabilities are already in the proper channel
    and this hook returns an empty dict to avoid announcing them twice.
    """
    if _CAPS_INJECTED:
        return {}

    settings_ws = config.plugin_settings("jedi_workspace_symbols")
    settings_ih = config.plugin_settings("inlay_hints")
    settings_ch = config.plugin_settings("call_hierarchy")
    settings_th = config.plugin_settings("type_hierarchy")

    caps: Dict[str, Any] = {}
    if settings_ws.get("enabled", True):
        caps["workspaceSymbolProvider"] = True
    # Check HAS_INLAY_DEPS (Jedi availability)
    if settings_ih.get("enabled", True) and HAS_INLAY_DEPS:
        caps["inlayHintProvider"] = {
            "resolveProvider": False,
            "workDoneProgress": True,
        }
        log.info("pylsp_workspace_symbols: announcing inlayHintProvider via experimental fallback")
    else:
        log.warning(
            "pylsp_workspace_symbols: inlayHintProvider not announced "
            "(HAS_INLAY_DEPS=%s)", HAS_INLAY_DEPS
        )
    if settings_ch.get("enabled", True):
        caps["callHierarchyProvider"] = True
    if settings_th.get("enabled", True):
        caps["typeHierarchyProvider"] = True
    if config.plugin_settings("document_links").get("enabled", True):
        caps["documentLinkProvider"] = {"resolveProvider": False}
    if config.plugin_settings("document_colors").get("enabled", True):
        caps["colorProvider"] = True
    if config.plugin_settings("code_lens").get("enabled", True) and HAS_INLAY_DEPS:
        caps["codeLensProvider"] = {"resolveProvider": False}
    if config.plugin_settings("semantic_tokens").get("enabled", False) and HAS_INLAY_DEPS:
        caps["semanticTokensProvider"] = {
            "legend": {
                "tokenTypes": list(_ST_TOKEN_TYPES.keys()),
                "tokenModifiers": list(_ST_TOKEN_MODIFIERS.keys()),
            },
            "full": {"delta": True},
            "range": True,
        }
    return caps


@hookimpl
def pylsp_dispatchers(config, workspace) -> dict:
    """Register custom JSON-RPC dispatch handlers for this plugin.

    Covers workspace/symbol, textDocument/inlayHint, call hierarchy,
    type hierarchy, document links, document colors, color presentation,
    and semantic tokens (full, delta, range).
    """
    settings_ws = config.plugin_settings("jedi_workspace_symbols")
    settings_ih = config.plugin_settings("inlay_hints")

    dispatch: Dict[str, Any] = {}

    # Workspace symbols
    if settings_ws.get("enabled", True):
        def _workspace_symbol(params) -> Optional[List[dict]]:
            # pylsp_jsonrpc calls handlers with the raw params dict as a single
            # positional argument: handler({"query": "foo"})
            query = params.get("query", "") if isinstance(params, dict) else ""
            return _search_symbols(settings_ws, workspace, query)

        dispatch["workspace/symbol"] = _workspace_symbol

    # Inlay hints
    # Check HAS_INLAY_DEPS (Jedi availability)
    if settings_ih.get("enabled", True) and HAS_INLAY_DEPS:
        def _inlay_hint(params) -> List[dict]:
            if not isinstance(params, dict):
                return []

            text_doc = params.get("textDocument") or {}
            uri = text_doc.get("uri")
            if not uri:
                return []

            range_ = params.get("range") or {"start": {"line": 0}, "end": {"line": 10**9}}
            start_line = range_.get("start", {}).get("line", 0)
            end_line = range_.get("end", {}).get("line", 10**9)

            try:
                document = workspace.get_document(uri)
                hints = _get_inlay_hints(document.source, document.path, settings_ih)
                return [
                    hint
                    for hint in hints
                    if start_line <= hint.get("position", {}).get("line", -1) <= end_line
                ]
            except Exception as e:
                log.exception("pylsp_workspace_symbols: failed for %s: %s", uri, e)
                return []

        dispatch["textDocument/inlayHint"] = _inlay_hint

    # -- Call hierarchy ----------------------------------------------------
    settings_ch = config.plugin_settings("call_hierarchy")
    if settings_ch.get("enabled", True) and HAS_INLAY_DEPS:
        def _prepare_call_hierarchy(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            pos = params.get("position", {})
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return None
            try:
                document = workspace.get_document(uri)
                return _call_hierarchy_prepare(
                    document.source, document.path,
                    pos.get("line", 0), pos.get("character", 0),
                )
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: prepareCallHierarchy: %s", exc)
                return None

        def _incoming_calls(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _call_hierarchy_incoming(params.get("item", {}), workspace)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: incomingCalls: %s", exc)
                return None

        def _outgoing_calls(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _call_hierarchy_outgoing(params.get("item", {}), workspace)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: outgoingCalls: %s", exc)
                return None

        dispatch["textDocument/prepareCallHierarchy"] = _prepare_call_hierarchy
        dispatch["callHierarchy/incomingCalls"] = _incoming_calls
        dispatch["callHierarchy/outgoingCalls"] = _outgoing_calls

    # -- Type hierarchy ----------------------------------------------------
    settings_th = config.plugin_settings("type_hierarchy")
    if settings_th.get("enabled", True) and HAS_INLAY_DEPS:
        def _prepare_type_hierarchy(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            pos = params.get("position", {})
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return None
            try:
                document = workspace.get_document(uri)
                return _type_hierarchy_prepare(
                    document.source, document.path,
                    pos.get("line", 0), pos.get("character", 0),
                )
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: prepareTypeHierarchy: %s", exc)
                return None

        def _supertypes(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _type_hierarchy_supertypes(params.get("item", {}))
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: supertypes: %s", exc)
                return None

        def _subtypes(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _type_hierarchy_subtypes(params.get("item", {}), workspace)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: subtypes: %s", exc)
                return None

        dispatch["textDocument/prepareTypeHierarchy"] = _prepare_type_hierarchy
        dispatch["typeHierarchy/supertypes"] = _supertypes
        dispatch["typeHierarchy/subtypes"] = _subtypes

    settings_dl = config.plugin_settings("document_links")
    if settings_dl.get("enabled", True):
        def _document_link(params) -> List[dict]:
            if not isinstance(params, dict):
                return []
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return []
            try:
                document = workspace.get_document(uri)
                return _collect_document_links(document.source, document.path, workspace)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: documentLink: %s", exc)
                return []

        dispatch["textDocument/documentLink"] = _document_link

    settings_dc = config.plugin_settings("document_colors")
    if settings_dc.get("enabled", True):
        def _document_color(params) -> List[dict]:
            if not isinstance(params, dict):
                return []
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return []
            try:
                document = workspace.get_document(uri)
                return _collect_document_colors(document.source)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: documentColor: %s", exc)
                return []

        dispatch["textDocument/documentColor"] = _document_color

        def _color_presentation(params) -> List[dict]:
            """textDocument/colorPresentation - representations for a picked color.

            Called by the editor's color picker after the user selects a new
            color value.  Returns alternative text representations so the user
            can choose which format to insert.
            """
            if not isinstance(params, dict):
                return []
            uri = (params.get("textDocument") or {}).get("uri")
            color = params.get("color") or {}
            range_ = params.get("range") or {}
            if not uri or not color:
                return []
            try:
                document = workspace.get_document(uri)
                # Extract the text currently at the range so we can infer format
                start = (range_.get("start") or {})
                end   = (range_.get("end") or {})
                sl, sc = start.get("line", 0), start.get("character", 0)
                el, ec = end.get("line", sl),  end.get("character", sc)
                src_lines = (document.source or "").splitlines()
                if sl < len(src_lines) and sl == el:
                    context_text = src_lines[sl][sc:ec]
                else:
                    context_text = ""
                return _color_presentations(color, range_, context_text)
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: colorPresentation: %s", exc)
                return []

        dispatch["textDocument/colorPresentation"] = _color_presentation

    # -- Semantic tokens -------------------------------------------------------
    settings_st = config.plugin_settings("semantic_tokens")
    if settings_st.get("enabled", False) and HAS_INLAY_DEPS:
        def _semantic_tokens_full(params) -> dict:
            if not isinstance(params, dict):
                return {"data": []}
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return {"data": []}
            try:
                document = workspace.get_document(uri)
                data = _get_semantic_tokens(document.source, document.path)
                result_id = _st_next_result_id()
                with _ST_CACHE_LOCK:
                    _ST_CACHE[uri] = (result_id, data)
                return {"resultId": result_id, "data": data}
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: semanticTokens/full: %s", exc)
                return {"data": []}

        def _semantic_tokens_full_delta(params) -> dict:
            """textDocument/semanticTokens/full/delta - incremental update.

            Returns SemanticTokensDelta if previousResultId matches the cache,
            otherwise falls back to a full SemanticTokens response so the client
            always gets a valid result.
            """
            if not isinstance(params, dict):
                return {"edits": []}
            uri = (params.get("textDocument") or {}).get("uri")
            previous_result_id = params.get("previousResultId", "")
            if not uri:
                return {"edits": []}
            try:
                document = workspace.get_document(uri)
                new_data = _get_semantic_tokens(document.source, document.path)
                new_result_id = _st_next_result_id()

                with _ST_CACHE_LOCK:
                    cached = _ST_CACHE.get(uri)
                    _ST_CACHE[uri] = (new_result_id, new_data)

                # If previousResultId matches our cache, return a delta.
                # Otherwise return a full response (client will resync).
                if cached and cached[0] == previous_result_id:
                    old_data = cached[1]
                    edits = _compute_st_delta(old_data, new_data)
                    return {"resultId": new_result_id, "edits": edits}
                else:
                    # Cache miss or first request after server restart:
                    # return full tokens so the client resyncs correctly.
                    return {"resultId": new_result_id, "data": new_data}
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: semanticTokens/full/delta: %s", exc)
                return {"edits": []}

        def _semantic_tokens_range(params) -> dict:
            if not isinstance(params, dict):
                return {"data": []}
            uri = (params.get("textDocument") or {}).get("uri")
            if not uri:
                return {"data": []}
            rng = params.get("range") or {}
            start_line = (rng.get("start") or {}).get("line", 0)
            end_line = (rng.get("end") or {}).get("line", 10 ** 9)
            try:
                document = workspace.get_document(uri)
                return {
                    "data": _get_semantic_tokens(
                        document.source, document.path,
                        start_line=start_line, end_line=end_line,
                    ),
                }
            except Exception as exc:
                log.exception("pylsp_workspace_symbols: semanticTokens/range: %s", exc)
                return {"data": []}

        dispatch["textDocument/semanticTokens/full"] = _semantic_tokens_full
        dispatch["textDocument/semanticTokens/full/delta"] = _semantic_tokens_full_delta
        dispatch["textDocument/semanticTokens/range"] = _semantic_tokens_range

    return dispatch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _in_ignored_folder(path: str, ignore_folders: set) -> bool:
    """Return True if *path* passes through any of the ignored folder names.

    Each path segment is checked against every entry in *ignore_folders*:
    - Exact match: catches ``node_modules``, ``.venv``, etc.
    - Suffix match: catches ``pylsp_workspace_symbols.egg-info`` via the
      ``egg-info`` token, since the segment ends with it.
    """
    normalized = path.replace("\\", "/")
    segments = normalized.split("/")
    return any(
        seg == folder or seg.endswith("." + folder)
        for seg in segments
        for folder in ignore_folders
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    """Python 3.8-compatible replacement for Path.is_relative_to (added in 3.9)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _search_symbols(settings: dict, workspace, query: str) -> Optional[List[dict]]:
    """Core Jedi-backed implementation of workspace/symbol.

    Always uses project.complete_search("") to enumerate all symbols, then
    filters client-side by case-insensitive substring match on `query`.
    This is necessary because project.search(query) in older bundled Jedi
    performs exact name matching and misses partial matches (e.g. 'area'
    won't find 'calculate_area').

    Results are restricted to files inside any known workspace folder.
    The set of workspace folders is read directly from the PythonLSPServer
    instance via ``workspace._endpoint._dispatcher.workspaces`` - the same
    dict that pylsp updates when it receives workspace/didChangeWorkspaceFolders.
    This is necessary because pylsp_dispatchers is called once at startup and
    the workspace captured in the closure never reflects later folder additions.

    Performance: ``sys_path=[root]`` restricts Jedi's indexing to each
    workspace folder only, reducing the symbol set from the full Python
    environment (stdlib + site-packages) to just project files.  In practice
    this yields a ~80x speedup on ``complete_search`` (7000ms -> 88ms on a
    typical environment) with no loss of correctness - the ``_is_relative_to``
    guard below discards the small number of typeshed ``.pyi`` stubs that
    still leak through.  ``get_references()`` is unaffected because each
    call hierarchy / type hierarchy request creates its own ``jedi.Script``
    with a separate project instance.
    """
    if _jedi is None:
        log.error("pylsp_workspace_symbols: jedi is not available")
        return None

    # max_symbols <= 0 means no limit
    max_symbols: int = settings.get("max_symbols", 500)
    ignore_folders: set = (
        set(settings.get("ignore_folders", [])) | _DEFAULT_IGNORE_FOLDERS
    )
    query_lower = query.lower()

    # Read all workspace roots from the live server dict.
    # Falls back to workspace.root_path if the internal API is unavailable.
    workspace_roots: List[Path] = []
    try:
        server = workspace._endpoint._dispatcher
        for ws in server.workspaces.values():
            p = Path(ws.root_path)
            if p not in workspace_roots:
                workspace_roots.append(p)
    except Exception:
        pass
    if not workspace_roots:
        workspace_roots = [Path(workspace.root_path)]

    try:
        # sys_path=[root] tells Jedi to index only that workspace folder,
        # not the entire Python environment.  This is the correct fix for the
        # "returns thousands of results from stdlib/site-packages" problem -
        # filtering after the fact still requires Jedi to enumerate everything
        # first, which is slow.  get_references() in call/type hierarchy is
        # unaffected: those requests create their own jedi.Script instances
        # with separate project objects that include the full sys_path.
        names: List[Any] = []
        for root in workspace_roots:
            _t0 = time.time()
            project = _jedi.Project(
                path=str(root),
                sys_path=[str(root)],
            )
            # Always use complete_search("") to get all names, then filter
            # client-side. project.search(query) in older bundled Jedi performs
            # exact name matching, so 'area' would never find 'calculate_area'.
            # Note: project.search("") returns nothing - hence complete_search.
            root_names = list(project.complete_search(""))
            log.info(
                "pylsp_workspace_symbols: complete_search yielded %d names in %.0fms"
                " (root=%s)",
                len(root_names), (time.time() - _t0) * 1000, root,
            )
            names.extend(root_names)
    except Exception:
        log.exception("pylsp_workspace_symbols: Jedi search failed")
        return None

    results: List[dict] = []
    for name in names:
        if max_symbols > 0 and len(results) >= max_symbols:
            break

        # params clutter the list and are not useful as workspace symbols
        if name.type == "param":
            continue

        # Client-side substring filter - empty query means "show all"
        if query_lower and query_lower not in name.name.lower():
            continue

        try:
            module_path = name.module_path
            if module_path is None:
                continue

            # Restrict to files inside any known workspace root.
            # complete_search() returns symbols from the entire Python
            # environment; without this guard, stdlib/.pyi/site-packages
            # symbols leak into results.
            if not any(_is_relative_to(module_path, root) for root in workspace_roots):
                continue

            if _in_ignored_folder(str(module_path), ignore_folders):
                continue

            uri = uris.from_fs_path(str(module_path))

            # LSP 3.17: WorkspaceSymbol.location may be {uri} without range
            # when the exact position is unknown.  Jedi always provides line/col
            # so we always emit a full Location; the {uri}-only form is kept as
            # a documented fallback in case Jedi returns None for both.
            if name.line is not None:
                line = max(0, (name.line or 1) - 1)   # Jedi 1-based -> LSP 0-based
                col = max(0, (name.column or 0))
                location: dict = {
                    "uri": uri,
                    "range": {
                        "start": {"line": line, "character": col},
                        "end": {"line": line, "character": col + len(name.name)},
                    },
                }
            else:
                # LSP 3.17 allows omitting range when position is unavailable
                location = {"uri": uri}

            results.append({
                "name": name.name,
                "kind": _SYMBOL_KIND.get(name.type, _DEFAULT_KIND),
                "location": location,
                "containerName": name.module_name,
            })
        except Exception:
            log.debug("pylsp_workspace_symbols: skipping %r", name, exc_info=True)
            continue

    log.info(
        "pylsp_workspace_symbols: workspace/symbol - raw=%d returned=%d (discarded=%d)",
        len(names), len(results), len(names) - len(results),
    )
    return results


def _build_ast_tables(source: str) -> dict:
    # Single ast.parse() pass -- O(n), no extra Jedi calls.
    # Builds three lookup tables used by _get_semantic_tokens to produce
    # token types and modifiers that Jedi alone cannot determine:
    #   type_overrides : (line0, name) -> LSP token type name
    #   mod_overrides  : (line0, name) -> set of LSP modifier names
    #   decorator_lines: line0 -> True
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return {"type_overrides": {}, "mod_overrides": {}, "decorator_lines": {}, "docstring_lines": set(), "overload_lines": set()}

    type_overrides  = {}
    mod_overrides   = {}
    decorator_lines = {}
    class_body_lines = set()   # line0 values inside any class body
    enum_class_names = set()   # names of Enum subclasses
    docstring_lines = set()    # lines with docstrings for documentation modifier
    overload_lines = set()     # lines with @overload decorator

    def _add_mod(line0, name_str, mod):
        mod_overrides.setdefault((line0, name_str), set()).add(mod)

    for node in _ast.walk(tree):

        # TypeVar / ParamSpec / TypeVarTuple -> typeParameter + readonly
        if isinstance(node, _ast.Assign):
            val = node.value
            if isinstance(val, _ast.Call):
                fname = ""
                if isinstance(val.func, _ast.Name):        fname = val.func.id
                elif isinstance(val.func, _ast.Attribute): fname = val.func.attr
                if fname in ("TypeVar", "ParamSpec", "TypeVarTuple"):
                    for t in node.targets:
                        if isinstance(t, _ast.Name):
                            type_overrides[(t.lineno - 1, t.id)] = "typeParameter"
                            _add_mod(t.lineno - 1, t.id, "readonly")

        # Final / ClassVar annotated assignments -> readonly modifier
        if isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            ann = node.annotation
            ann_id = ""
            if isinstance(ann, _ast.Name):
                ann_id = ann.id
            elif isinstance(ann, _ast.Subscript) and isinstance(ann.value, _ast.Name):
                ann_id = ann.value.id
            if ann_id in ("Final", "ClassVar"):
                _add_mod(node.target.lineno - 1, node.target.id, "readonly")

        # Module-level plain assignments to UPPER_CASE names -> readonly
        # Only plain _ast.Name targets (not attributes), at least 2 chars,
        # all uppercase with at least one letter.
        if isinstance(node, _ast.Assign):
            for _t in node.targets:
                if isinstance(_t, _ast.Name):
                    _nm = _t.id
                    if (len(_nm) >= 2
                            and _nm == _nm.upper()
                            and any(c.isalpha() for c in _nm)):
                        _add_mod(_t.lineno - 1, _nm, "readonly")

        # Enum members + class_body_lines + enum_class_names
        if isinstance(node, _ast.ClassDef):
            base_ids = set()
            for b in node.bases:
                if isinstance(b, _ast.Name):        base_ids.add(b.id)
                elif isinstance(b, _ast.Attribute): base_ids.add(b.attr)
            # Record every line inside the class body for method detection.
            for item in node.body:
                for child in _ast.walk(item):
                    if hasattr(child, 'lineno'):
                        class_body_lines.add(child.lineno - 1)
            if base_ids & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
                enum_class_names.add(node.name)
                for item in node.body:
                    if isinstance(item, _ast.Assign):
                        for t in item.targets:
                            if isinstance(t, _ast.Name):
                                type_overrides[(item.lineno - 1, t.id)] = "enumMember"
                    elif isinstance(item, _ast.AnnAssign) and isinstance(item.target, _ast.Name):
                        type_overrides[(item.lineno - 1, item.target.id)] = "enumMember"

        # Function / method modifiers + decorator lines
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            ln0  = node.lineno - 1
            nstr = node.name
            # Track docstring lines for documentation modifier
            if _ast.get_docstring(node):
                docstring_lines.add(ln0)
            if isinstance(node, _ast.AsyncFunctionDef):
                _add_mod(ln0, nstr, "async")
            # Register all parameter names so Jedi statement refs classify correctly.
            _all_args = (
                node.args.args + node.args.posonlyargs + node.args.kwonlyargs
            )
            if node.args.vararg:
                _all_args = _all_args + [node.args.vararg]
            if node.args.kwarg:
                _all_args = _all_args + [node.args.kwarg]
            for _arg in _all_args:
                _aln = _arg.lineno - 1
                _anm = _arg.arg
                if _anm not in ("self", "cls"):
                    type_overrides[(_aln, _anm)] = "parameter"
            dec_names = set()
            for d in node.decorator_list:
                decorator_lines[d.lineno - 1] = True
                dname = ""
                if isinstance(d, _ast.Name):        dname = d.id
                elif isinstance(d, _ast.Attribute): dname = d.attr
                elif isinstance(d, _ast.Call):
                    f = d.func
                    if isinstance(f, _ast.Name):        dname = f.id
                    elif isinstance(f, _ast.Attribute): dname = f.attr
                dec_names.add(dname)
            if "abstractmethod" in dec_names:
                pass  # BP does not emit 'abstract' modifier; omit it
            if "staticmethod" in dec_names or "classmethod" in dec_names:
                _add_mod(ln0, nstr, "static")
            if "deprecated" in dec_names:
                _add_mod(ln0, nstr, "deprecated")
            else:
                doc = (_ast.get_docstring(node) or "").lower()
                if "deprecated" in doc:
                    _add_mod(ln0, nstr, "deprecated")

        # Class decorator lines
        if isinstance(node, _ast.ClassDef):
            for d in node.decorator_list:
                decorator_lines[d.lineno - 1] = True
            # Track docstring lines for documentation modifier
            if _ast.get_docstring(node):
                docstring_lines.add(node.lineno - 1)
            # Track @overload decorators
            for d in node.decorator_list:
                dname = ""
                if isinstance(d, _ast.Name):
                    dname = d.id
                elif isinstance(d, _ast.Attribute):
                    dname = d.attr
                if dname == "overload":
                    overload_lines.add(node.lineno - 1)

    # Track augmented assignments (+=, -=, etc.) for modification modifier
    # NOTE: must be outside the main walk loop to avoid shadowing the loop variable 'node'
    augassign_vars: set = set()
    for _aug_node in _ast.walk(tree):
        if isinstance(_aug_node, _ast.AugAssign):
            if isinstance(_aug_node.target, _ast.Name):
                augassign_vars.add((_aug_node.lineno - 1, _aug_node.target.id))

    return {
        "type_overrides":   type_overrides,
        "mod_overrides":    mod_overrides,
        "decorator_lines":  decorator_lines,
        "class_body_lines": class_body_lines,
        "enum_class_names": enum_class_names,
        "docstring_lines":  docstring_lines,
        "overload_lines":   overload_lines,
        "augassign_vars":   augassign_vars,
    }


def _get_semantic_tokens(
    source: str,
    path: str,
    start_line: int = 0,
    end_line: int = 10 ** 9,
) -> List[int]:
    """Return the LSP semantic tokens data array for *source*.

    Two-phase O(n) approach -- no per-token goto calls:
      1. ast.parse() builds lookup tables for types/modifiers that Jedi
         cannot determine alone (enumMember, typeParameter, decorator,
         abstract/static/async/readonly/deprecated modifiers, self/cls).
      2. jedi.Script.get_names() provides the base token stream.

    The returned flat integer list encodes tokens as 5-tuples of relative
    offsets as required by LSP 3.16:
        [deltaLine, deltaStartChar, length, tokenTypeIndex, tokenModifiersBitmask]
    """
    if _jedi is None:
        return []

    _norm_path = os.path.normpath(path) if path else ""
    tables = _build_ast_tables(source)
    type_overrides   = tables["type_overrides"]
    mod_overrides    = tables["mod_overrides"]
    decorator_lines  = tables["decorator_lines"]
    class_body_lines = tables["class_body_lines"]
    enum_class_names = tables["enum_class_names"]

    try:
        script = _jedi.Script(code=source, path=path)
        names = script.get_names(all_scopes=True, definitions=True, references=True)
    except Exception:
        log.exception("pylsp_workspace_symbols: get_names failed for %s", path)
        return []

    # Tokenize pass -- O(n), zero extra Jedi calls.
    # at_positions : (line0, col) of each "@" token -> emits decorator
    # annstr_names : (line0, col, name, length) of names inside type annotation
    #                strings like "Callable[P, T]".
    #                Only strings in AST annotation positions are processed.
    at_positions: List[tuple] = []
    annstr_names: List[tuple] = []
    _src_lines = source.splitlines()
    try:
        for _tok in _tokenize.generate_tokens(io.StringIO(source).readline):
            if _tok.type == _token.OP and _tok.string == "@":
                at_positions.append((_tok.start[0] - 1, _tok.start[1]))
    except _tokenize.TokenError:
        pass
    try:
        _ann_tree = _ast.parse(source)

        def _collect_ann_str(_ann_node: Any, _out: list) -> None:
            """Collect names from forward-reference strings inside annotations,
            including inside Subscript nodes like ClassVar[List["Registry"]]."""
            if _ann_node is None:
                return
            if isinstance(_ann_node, _ast.Constant) and isinstance(_ann_node.value, str):
                try:
                    _expr2 = _ast.parse(_ann_node.value, mode="eval")
                except SyntaxError:
                    return
                _sl0 = _ann_node.lineno - 1
                _src2 = _src_lines[_sl0] if _sl0 < len(_src_lines) else ""
                _sf2  = _ann_node.col_offset + 1
                for _nn2 in _ast.walk(_expr2.body):
                    if isinstance(_nn2, _ast.Name):
                        # Find ALL occurrences of this name in the string, not just the first
                        _search_start = _sf2
                        while True:
                            _idx2 = _src2.find(_nn2.id, _search_start)
                            if _idx2 < 0:
                                break
                            _out.append((_sl0, _idx2, _nn2.id, len(_nn2.id)))
                            _search_start = _idx2 + len(_nn2.id)  # Continue search after this occurrence
            elif isinstance(_ann_node, _ast.Subscript):
                _collect_ann_str(_ann_node.value, _out)
                _collect_ann_str(_ann_node.slice, _out)
            elif isinstance(_ann_node, (_ast.Tuple, _ast.List)):
                for _elt2 in _ann_node.elts:
                    _collect_ann_str(_elt2, _out)
            elif isinstance(_ann_node, _ast.BinOp):
                _collect_ann_str(_ann_node.left, _out)
                _collect_ann_str(_ann_node.right, _out)

        for _anode in _ast.walk(_ann_tree):
            _anns: list = []
            if isinstance(_anode, _ast.AnnAssign):
                _anns = [_anode.annotation]
            elif isinstance(_anode, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _anns = [
                    _a.annotation for _a in (
                        _anode.args.args + _anode.args.posonlyargs +
                        _anode.args.kwonlyargs +
                        ([_anode.args.vararg] if _anode.args.vararg else []) +
                        ([_anode.args.kwarg]  if _anode.args.kwarg  else [])
                    ) if _a.annotation
                ]
                if _anode.returns:
                    _anns.append(_anode.returns)
            for _ann in _anns:
                _collect_ann_str(_ann, annstr_names)
    except SyntaxError:
        pass

    # Track variable assignments for modification modifier
    _assigned_vars: Dict[str, List[tuple]] = {}  # name -> [(line0, col), ...]

    # Collect raw (line0, col, length, type_idx, mod_mask) tuples, sorted by
    # position so we can compute deltas in a single pass.
    #
    # Two-sub-pass strategy for statement references:
    #   Pass A: classify all definitions and build fullname_to_type and
    #           name_to_type lookup dicts using our full classification logic.
    #   Pass B: classify all tokens; for unresolved statement references
    #           consult the dicts built in pass A before falling back to
    #           "variable". This resolves references like float/int/str
    #           (builtin classes), self/cls (selfParameter/clsParameter),
    #           and names whose definition appeared elsewhere in the file.
    #   No per-token goto() calls -- O(n) total.

    # Builtin and typing names that are always "class" regardless of context.
    _BUILTIN_TYPES: frozenset = frozenset({
        "float","int","str","bool","bytes","complex","list","dict","tuple",
        "set","frozenset","type","object",
        "Optional","List","Dict","Tuple","Set","FrozenSet","Type","Union",
        "Callable","Iterator","Generator","Sequence","Any","ClassVar","Final",
        "TypeVar","Generic","Protocol","TypedDict","NamedTuple","ParamSpec",
        "TypeVarTuple","Concatenate","Literal","Annotated",
        "dataclass","field","abstractmethod","ABC","ABCMeta",
        # Enum base classes (accessed as enum.Enum, enum.IntEnum, etc.)
        "Enum","IntEnum","StrEnum","Flag","IntFlag","EnumMeta",
    })

    # Builtin and stdlib names that are always "function" regardless of context.
    # Covers Python builtins and well-known stdlib callables that Jedi emits as
    # unresolved "statement" references (full_name lacks class/module prefix).
    _BUILTIN_FUNCTIONS: frozenset = frozenset({
        # Python builtin functions
        "print","len","range","enumerate","zip","map","filter","sorted","reversed",
        "sum","min","max","abs","round","pow","divmod","hash","id","hex","oct","bin",
        "chr","ord","repr","isinstance","issubclass","hasattr","getattr","setattr",
        "delattr","callable","iter","next","open","input","vars","dir","locals",
        "globals","exec","eval","compile","__import__","super","format","breakpoint",
        # list / deque methods
        "append","extend","insert","remove","pop","clear","sort","reverse","copy",
        # dict / set methods
        "update","add","discard","get","items","keys","values","setdefault","fromkeys",
        # str methods
        "join","split","strip","lstrip","rstrip","replace","find","index","count",
        "startswith","endswith","encode","decode","upper","lower","title","capitalize",
        "format_map","zfill","center","ljust","rjust",
        # re methods
        "match","search","findall","finditer","sub","subn","fullmatch","groups",
        "group","groupdict","span","start","end",
        # os / pathlib / io
        "getcwd","listdir","makedirs","mkdir","rmdir","remove","unlink","rename",
        "stat","walk","scandir","chdir","getenv","putenv","environ",
        "exists","isfile","isdir","basename","dirname","abspath","normpath","realpath",
        "read","write","readline","readlines","writelines","seek","tell","flush","close",
        # asyncio
        "sleep","gather","create_task","run","wait","shield","ensure_future",
        "get_event_loop","new_event_loop","set_event_loop",
        # warnings / logging
        "warn","simplefilter","filterwarnings","debug","info","warning","error",
        "critical","exception","getLogger","basicConfig",
        # itertools / functools
        "chain","product","combinations","permutations","islice","groupby","partial",
        "reduce","wraps","lru_cache","cache","cached_property",
        # json / pickle
        "dumps","loads","dump","load",
        # datetime
        "now","today","strftime","strptime","timedelta","fromisoformat",
        # collections
        "namedtuple","defaultdict","Counter","deque","OrderedDict",
        # threading / subprocess
        "Thread","start","join","run","communicate","check_output","call",
    })

    # Type priority: prefer more specific types when the same name has
    # multiple definitions (e.g. a class named after a builtin).
    _TYPE_PRIO = {
        "class":10,"namespace":9,"function":8,"method":8,
        "selfParameter":7,"clsParameter":7,"parameter":6,
        "property":5,"enumMember":5,"typeParameter":5,
        "decorator":4,"enum":4,"variable":1,
    }

    # Names used in raise/except/isinstance that Jedi emits as "statement"
    # but basedpyright classifies as "class".
    _EXCEPTION_CLASSES: frozenset = frozenset({
        "Exception","BaseException","ValueError","TypeError","KeyError",
        "IndexError","AttributeError","RuntimeError","StopIteration",
        "NotImplementedError","OSError","IOError","FileNotFoundError",
        "PermissionError","TimeoutError","ImportError","ModuleNotFoundError",
        "NameError","ZeroDivisionError","OverflowError","MemoryError",
        "RecursionError","SystemExit","KeyboardInterrupt","GeneratorExit",
        "ArithmeticError","LookupError","EOFError","ConnectionError",
        "UnicodeError","UnicodeDecodeError","UnicodeEncodeError",
        "SyntaxError","AssertionError","DeprecationWarning",
        "RuntimeWarning","UserWarning","Warning",
    })

    # self_attr_positions : (line0, col) of the attribute in self.X / cls.X -> property
    # obj_attr_positions  : (line0, col) of the attribute in any obj.X -> method
    self_attr_positions: set = set()
    obj_attr_positions:  set = set()
    try:
        _tl = list(_tokenize.generate_tokens(io.StringIO(source).readline))
        _i2 = 0
        while _i2 < len(_tl) - 2:
            _a, _b, _c = _tl[_i2], _tl[_i2 + 1], _tl[_i2 + 2]
            if (_a.type == _token.NAME
                    and _b.type == _token.OP and _b.string == "."
                    and _c.type == _token.NAME):
                _p = (_c.start[0] - 1, _c.start[1])
                obj_attr_positions.add(_p)
                if _a.string in ("self", "cls"):
                    self_attr_positions.add(_p)
                _i2 += 2
            else:
                _i2 += 1
    except _tokenize.TokenError:
        pass

    # regexp_positions: (line0, col, length) of strings in re.compile() calls
    # Stored as tuple with length so we can inject them as proper semantic tokens.
    regexp_positions: set = set()
    try:
        _src_lines = source.splitlines()
        for _node in _ast.walk(_ast.parse(source)):
            if isinstance(_node, _ast.Call):
                # Detect re.compile() calls
                if isinstance(_node.func, _ast.Attribute):
                    if _node.func.attr == "compile":
                        if isinstance(_node.func.value, _ast.Name):
                            if _node.func.value.id == "re":
                                if _node.args:
                                    first_arg = _node.args[0]
                                    if isinstance(first_arg, _ast.Constant):
                                        if isinstance(first_arg.value, str):
                                            # Calculate the actual string start position (after prefix like 'r', 'b', 'u')
                                            # AST col_offset points to the prefix, not the quote
                                            _line_idx = first_arg.lineno - 1
                                            _col_offset = first_arg.col_offset
                                            # Find the opening quote in the source line
                                            if _line_idx < len(_src_lines):
                                                _line_content = _src_lines[_line_idx]
                                                # Search for the quote character after the col_offset
                                                for _i in range(_col_offset, min(_col_offset + 3, len(_line_content))):
                                                    if _line_content[_i] in ('"', "'"):
                                                        _col_offset = _i
                                                        break
                                            # Store (line0, col, length) for token injection
                                            # Length includes the quotes
                                            regexp_positions.add((
                                                _line_idx,
                                                _col_offset,
                                                len(first_arg.value) + 2  # +2 for the quotes
                                            ))
    except SyntaxError:
        pass

    # type_alias_set: (line0, name) for X = A | B -> emits "type"
    # Also covers X = Optional[Y], X = List[Y] etc. (common type aliases)
    type_alias_set: set = set()
    try:
        for _ta in _ast.walk(_ast.parse(source)):
            if isinstance(_ta, _ast.Assign):
                _is_union = (isinstance(_ta.value, _ast.BinOp)
                             and isinstance(_ta.value.op, _ast.BitOr))
                _is_typing_alias = (
                    isinstance(_ta.value, _ast.Subscript)
                    and isinstance(_ta.value.value, _ast.Name)
                    and _ta.value.value.id in (
                        "Optional","Union","List","Dict","Tuple","Set",
                        "FrozenSet","Callable","Iterator","Generator",
                        "Sequence","Type","ClassVar","Final","Literal","Annotated",
                    )
                )
                # Also detect string annotations that look like type aliases
                _is_string_alias = (
                    isinstance(_ta.value, _ast.Constant)
                    and isinstance(_ta.value.value, str)
                    and any(n in _ta.value.value for n in (
                        "Optional", "Union", "List", "Dict", "Tuple", "Set",
                        "FrozenSet", "Callable", "Iterator", "Generator",
                        "Sequence", "Type", "ClassVar", "Final", "Literal", "Annotated",
                    ))
                )
                if _is_union or _is_typing_alias or _is_string_alias:
                    for _t in _ta.targets:
                        if isinstance(_t, _ast.Name):
                            type_alias_set.add((_t.lineno - 1, _t.id))
            # Python 3.12+ type statement: type Vector = list[float]
            elif isinstance(_ta, _ast.AnnAssign):
                # Detect pattern: variable with annotation at module level
                if hasattr(_ta, 'target') and isinstance(_ta.target, _ast.Name):
                    # Check if annotation suggests a type alias
                    ann_id = ""
                    if isinstance(_ta.annotation, _ast.Name):
                        ann_id = _ta.annotation.id
                    elif isinstance(_ta.annotation, _ast.Subscript):
                        if isinstance(_ta.annotation.value, _ast.Name):
                            ann_id = _ta.annotation.value.id
                    if ann_id in ("type", "Type", "TypeAlias"):
                        type_alias_set.add((_ta.target.lineno - 1, _ta.target.id))
    except SyntaxError:
        pass

    # class_direct_lines: lines of AnnAssign/Assign directly inside a ClassDef body.
    # Used in _classify to distinguish property (class member) from variable.
    # Includes __slots__ which Jedi emits as a statement without ": ".
    class_direct_lines: set = set()
    class_slot_lines:   set = set()
    static_property_names: set = set()  # names of class-level declared properties
    try:
        for _cd in _ast.walk(_ast.parse(source)):
            if isinstance(_cd, _ast.ClassDef):
                for _item in _cd.body:
                    if isinstance(_item, _ast.AnnAssign) and isinstance(_item.target, _ast.Name):
                        class_direct_lines.add(_item.lineno - 1)
                        static_property_names.add(_item.target.id)
                    elif isinstance(_item, _ast.Assign):
                        class_direct_lines.add(_item.lineno - 1)
                        for _t in _item.targets:
                            if isinstance(_t, _ast.Name):
                                static_property_names.add(_t.id)
                                if _t.id == "__slots__":
                                    class_slot_lines.add(_item.lineno - 1)
                                    # also collect slot names from the tuple/list literal
                                    if isinstance(_item.value, (
                                            _ast.Tuple, _ast.List, _ast.Set)):
                                        for _elt in _item.value.elts:
                                            if isinstance(_elt, _ast.Constant) and isinstance(_elt.value, str):
                                                static_property_names.add(_elt.value)
    except SyntaxError:
        pass

    # stdlib_attr_positions: (line0, col) of attributes where the object is a stdlib module.
    # These must NOT be promoted to "method" by obj_attr_positions.
    _STDLIB_NAMES: frozenset = frozenset({
        "os","sys","re","io","abc","ast","json","math","time","random","string",
        "collections","itertools","functools","pathlib","typing","types","copy",
        "warnings","logging","threading","asyncio","socket","struct","hashlib",
        "datetime","calendar","enum","dataclasses","contextlib","inspect",
        "weakref","operator","shutil","glob","tempfile","urllib","http",
        "subprocess","multiprocessing","queue","heapq","bisect","array",
        "pickle","shelve","sqlite3","csv","unittest","traceback","pprint",
    })
    stdlib_attr_positions: set = set()
    try:
        _tl3 = list(_tokenize.generate_tokens(io.StringIO(source).readline))
        _i3  = 0
        while _i3 < len(_tl3) - 2:
            _a3, _b3, _c3 = _tl3[_i3], _tl3[_i3 + 1], _tl3[_i3 + 2]
            if (_a3.type == _token.NAME
                    and _b3.type == _token.OP and _b3.string == "."
                    and _c3.type == _token.NAME
                    and _a3.string in _STDLIB_NAMES):
                stdlib_attr_positions.add((_c3.start[0] - 1, _c3.start[1]))
            _i3 += 1
    except _tokenize.TokenError:
        pass

    def _classify(n_: Any, is_ref: bool = False) -> Optional[str]:
        # Return the LSP token type name for a Jedi Name object.
        line0_ = n_.line - 1
        nstr_  = n_.name
        jtype_ = n_.type
        full_  = n_.full_name or ""
        pos_   = (line0_, nstr_)

        if pos_ in type_overrides:
            return type_overrides[pos_]
        if jtype_ == "param":
            if nstr_ == "self": return "selfParameter"
            if nstr_ == "cls":  return "clsParameter"
            return "parameter"
        if jtype_ == "statement" and line0_ in decorator_lines and not n_.is_definition():
            return "decorator"
        if jtype_ == "statement" and ": " in (n_.description or "") and n_.is_definition():
            # property only for direct ClassDef members.
            # Variables inside functions or at module level -> variable.
            return "property" if line0_ in class_direct_lines else "variable"
        if jtype_ == "statement" and n_.is_definition() and line0_ in class_slot_lines:
            # __slots__ = (...) inside a class body -> property
            return "property"
        if jtype_ == "function":
            return "method" if line0_ in class_body_lines else "function"
        if jtype_ == "class":
            return "enum" if nstr_ in enum_class_names else "class"
        if jtype_ in ("instance", "statement"):
            return "variable"
        if jtype_ == "module":   return "namespace"
        if jtype_ == "keyword":  return "keyword"
        if jtype_ == "property": return "property"
        if jtype_ == "path":     return "namespace"
        return None

    # Pass A: build lookup dicts from definitions.
    fullname_to_type: Dict[str, str] = {}
    name_to_type: Dict[str, str] = {}
    # Names registered as typeParameter (TypeVar/ParamSpec/TypeVarTuple)
    # All references to these names get 'readonly' modifier, not just definitions.
    typevar_names: set = {k[1] for k, v in type_overrides.items() if v == "typeParameter"}

    # Build positions of X in typevar.X (e.g. P.args, P.kwargs) now that typevar_names is ready.
    typevar_attr_positions: set = set()
    try:
        _tl_tv = list(_tokenize.generate_tokens(io.StringIO(source).readline))
        _itv = 0
        while _itv < len(_tl_tv) - 2:
            _ta, _tb, _tc = _tl_tv[_itv], _tl_tv[_itv + 1], _tl_tv[_itv + 2]
            if (_ta.type == _token.NAME and _ta.string in typevar_names
                    and _tb.type == _token.OP and _tb.string == "."
                    and _tc.type == _token.NAME):
                typevar_attr_positions.add((_tc.start[0] - 1, _tc.start[1]))
                _itv += 2
            else:
                _itv += 1
    except _tokenize.TokenError:
        pass

    for n_ in names:
        if not n_.is_definition() or n_.line is None:
            continue
        st_ = _classify(n_)
        if st_ is None:
            continue
        full_ = n_.full_name or ""
        if full_:
            fullname_to_type[full_] = st_
        nstr_ = n_.name
        if _TYPE_PRIO.get(st_, 0) > _TYPE_PRIO.get(name_to_type.get(nstr_, ""), 0):
            name_to_type[nstr_] = st_
        # Track variable definitions for modification detection
        if st_ == "variable":
            line0_ = n_.line - 1
            if nstr_ not in _assigned_vars:
                _assigned_vars[nstr_] = []
            _assigned_vars[nstr_].append((line0_, n_.column))

    raw: List[tuple] = []
    for name in names:
        if name.line is None or name.column is None:
            continue

        line0 = name.line - 1  # Jedi 1-based -> LSP 0-based
        if line0 < start_line or line0 > end_line:
            continue

        jtype = name.type
        nstr  = name.name
        full  = name.full_name or ""
        pos   = (line0, nstr)

        # Skip references to symbols defined in external files (stdlib,
        # site-packages, other project files).  Comparing module_path to the
        # current file is robust regardless of how Jedi sets the module name
        # (it varies between __main__ and the real dotted name depending on
        # whether path is supplied to jedi.Script).
        try:
            mp = str(name.module_path) if name.module_path else ""
            is_external = bool(mp) and os.path.normpath(mp) != _norm_path
        except Exception:
            is_external = False
        if not name.is_definition() and is_external:
            continue

        # --- token type ---
        # AST-derived overrides take priority over Jedi type for cases Jedi
        # cannot distinguish: TypeVar assignments, enum members, decorators.
        if name.is_definition():
            st_name = _classify(name)
            if st_name is None:
                continue
            # self.X = ... as a definition site -> property (self_attr_positions)
            if st_name == "variable" and (line0, name.column) in self_attr_positions:
                st_name = "property"
            # type alias: X = A | B or X = Optional[Y] -> "type"
            if st_name in ("variable", "property") and (line0, nstr) in type_alias_set:
                st_name = "type"
        else:
            # Pass B: resolve statement references via lookup dicts.
            if jtype == "statement":
                if pos in type_overrides:
                    st_name = type_overrides[pos]
                elif line0 in decorator_lines:
                    if full in fullname_to_type:
                        _ft = fullname_to_type[full]
                        st_name = _ft if _ft not in ("variable", "statement") else "decorator"
                    elif nstr in name_to_type:
                        _nt = name_to_type[nstr]
                        st_name = _nt if _nt not in ("variable", "statement") else "decorator"
                    elif nstr in _BUILTIN_FUNCTIONS:
                        st_name = "function"
                    elif nstr in _BUILTIN_TYPES:
                        st_name = "class"
                    else:
                        st_name = "decorator"
                elif nstr == "self":
                    st_name = "selfParameter"
                elif nstr == "cls":
                    st_name = "clsParameter"
                elif (line0, name.column) in self_attr_positions:
                    # self.X or cls.X -> property
                    st_name = "property"
                elif (line0, name.column) in stdlib_attr_positions:
                    # stdlib module attribute: let fullname_to_type resolve it,
                    # or fall back to "variable" (e.g. os.sep, sys.argv)
                    if full in fullname_to_type:
                        st_name = fullname_to_type[full]
                    elif nstr in name_to_type:
                        st_name = name_to_type[nstr]
                    elif nstr in {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "EnumMeta"}:
                        st_name = "enum"
                    elif nstr in _BUILTIN_TYPES:
                        st_name = "class"
                    elif nstr in _BUILTIN_FUNCTIONS:
                        st_name = "function"
                    else:
                        st_name = "variable"
                elif (line0, name.column) in typevar_attr_positions:
                    # typevar.X (e.g. P.args, P.kwargs) -> typeParameter
                    st_name = "typeParameter"
                elif (line0, name.column) in obj_attr_positions:
                    # obj.X -> method if X is a function/method defined in this file
                    if full in fullname_to_type:
                        _ft = fullname_to_type[full]
                        st_name = "method" if _ft in ("function", "method") else _ft
                    elif nstr in name_to_type:
                        _nt = name_to_type[nstr]
                        st_name = "method" if _nt in ("function", "method") else _nt
                    elif nstr in _BUILTIN_FUNCTIONS:
                        st_name = "method"
                    elif nstr in _BUILTIN_TYPES:
                        st_name = "class"
                    elif nstr in static_property_names:
                        st_name = "property"
                    else:
                        st_name = "variable"
                elif nstr in _EXCEPTION_CLASSES:
                    st_name = "class"
                elif full in fullname_to_type:
                    _ft = fullname_to_type[full]
                    if _ft == "variable" and nstr in name_to_type:
                        st_name = name_to_type[nstr]
                    else:
                        st_name = _ft
                elif nstr in _BUILTIN_TYPES:
                    st_name = "class"
                elif nstr in name_to_type:
                    st_name = name_to_type[nstr]
                elif nstr in _BUILTIN_FUNCTIONS:
                    # Well-known builtin/stdlib callable -- Jedi emits it as an
                    # unresolved statement ref because full_name lacks module prefix.
                    # Checked after name_to_type so file-local definitions win.
                    st_name = "function"
                else:
                    st_name = "variable"
            else:
                st_name = _classify(name)
                if st_name is None:
                    continue

        type_idx = _ST_TOKEN_TYPES.get(st_name)
        if type_idx is None:
            continue

        # Build modifier bitmask
        mod_mask = 0
        _is_self_attr_prop = (
            st_name == "property"
            and (line0, name.column) in self_attr_positions
        )
        if name.is_definition() and not _is_self_attr_prop:
            # BP only emits 'declaration' at definition sites, not 'definition'.
            # Exceptions that BP does NOT mark with 'declaration':
            #   - namespace (import statements: "import os", "from typing import X")
            #   - typeParameter (TypeVar/ParamSpec: only get 'readonly')
            #   - names imported via 'from X import Y' (just bindings)
            #   - self.X property assignments (instance attribute bindings)
            _is_import_binding = (
                st_name in ("namespace", "class", "function", "variable",
                            "type", "enumMember", "parameter", "typeParameter")
                and bool(name.module_path)
                and os.path.normpath(str(name.module_path)).lower() != _norm_path.lower()
            )
            # BP does not emit 'declaration' for class-scope property definitions
            # (NamedTuple fields, TypedDict fields, __slots__, ClassVar, Final)
            # or for enumMember definitions.
            _is_class_prop_def = (
                st_name in ("property", "enumMember") and line0 in class_direct_lines
            )
            if st_name not in ("typeParameter", "namespace") and not _is_import_binding and not _is_class_prop_def:
                mod_mask |= 1 << _ST_TOKEN_MODIFIERS["declaration"]

        # Apply AST-derived modifiers (static, async, readonly, deprecated).
        # 'abstract' is intentionally excluded: BP does not emit it.
        if pos in mod_overrides:
            for mod in mod_overrides[pos]:
                if mod == "abstract":
                    continue  # BP legend has no abstract modifier
                if mod in _ST_TOKEN_MODIFIERS:
                    mod_mask |= 1 << _ST_TOKEN_MODIFIERS[mod]

        # typeParameter references also get 'readonly' (not just the definition site)
        if st_name == "typeParameter" and (
            nstr in typevar_names
            or (line0, name.column) in typevar_attr_positions
        ):
            mod_mask |= 1 << _ST_TOKEN_MODIFIERS["readonly"]

        # classMember: methods and properties declared inside a class body
        if st_name in ("method", "property") and line0 in class_body_lines:
            mod_mask |= 1 << _ST_TOKEN_MODIFIERS["classMember"]

        # static: class-level properties (ClassVar, NamedTuple fields, __slots__,
        # Final class attributes, TypedDict fields) - BP emits 'static' for all
        # class-scope properties including self.X refs to class-declared names.
        if st_name == "property" and (
            line0 in class_direct_lines
            or nstr in static_property_names
        ):
            mod_mask |= 1 << _ST_TOKEN_MODIFIERS["static"]

        # parameter modifier: applied to all parameter-typed tokens
        if st_name in ("parameter", "selfParameter", "clsParameter"):
            mod_mask |= 1 << _ST_TOKEN_MODIFIERS["parameter"]

        # Modification modifier for reassigned variables
        # Only apply to references (not definitions) of variables we've seen before
        if not name.is_definition() and st_name == "variable":
            if nstr in _assigned_vars:
                # Check if this variable was defined on an earlier line
                for def_line, def_col in _assigned_vars[nstr]:
                    if def_line < line0:
                        mod_mask |= 1 << _ST_TOKEN_MODIFIERS["modification"]
                        break
            # Also check for augmented assignments (+=, -=, etc.) via AST
            elif nstr in tables.get("augassign_vars", set()):
                mod_mask |= 1 << _ST_TOKEN_MODIFIERS["modification"]

        # documentation modifier for symbols with docstrings
        # Only apply to function/class/method definitions, not all tokens on the line
        if line0 in tables.get("docstring_lines", set()):
            if st_name in ("function", "method", "class"):
                mod_mask |= 1 << _ST_TOKEN_MODIFIERS["documentation"]

        # overload declaration modifier
        if line0 in tables.get("overload_lines", set()):
            mod_mask |= 1 << _ST_TOKEN_MODIFIERS["declaration"]

        # Detect defaultLibrary + builtin.
        # Only apply to names whose module_name resolves to a stdlib module.
        # Skip tokens resolved via obj_attr_positions (method calls on user objects).
        _is_obj_attr = (line0, name.column) in obj_attr_positions
        try:
            mod_name = (name.module_name or "").split(".")[0]
            if mod_name in _STDLIB_TOP and not _is_obj_attr:
                _def_path = str(name.module_path) if name.module_path else ""
                _is_local = bool(_def_path) and os.path.normpath(_def_path).lower() == _norm_path.lower()
                if not _is_local:
                    mod_mask |= 1 << _ST_TOKEN_MODIFIERS["defaultLibrary"]
                    if mod_name == "builtins":
                        mod_mask |= 1 << _ST_TOKEN_MODIFIERS["builtin"]
            elif not _is_obj_attr and not name.is_definition():
                # Fallback for builtins Jedi doesn't resolve to a module
                # (e.g. 'float', 'str', 'len' used as annotations or calls)
                if nstr in _BUILTIN_TYPES or nstr in _BUILTIN_FUNCTIONS:
                    mod_mask |= 1 << _ST_TOKEN_MODIFIERS["defaultLibrary"]
                    mod_mask |= 1 << _ST_TOKEN_MODIFIERS["builtin"]
        except Exception:
            pass

        raw.append((line0, name.column, len(nstr), type_idx, mod_mask))

    # Inject "@" (decorator) tokens -- BP emits a "decorator" token for the
    # "@" symbol itself; Jedi never does.
    _dec_idx = _ST_TOKEN_TYPES.get("decorator")
    _cls_idx = _ST_TOKEN_TYPES.get("class")
    _tp_idx  = _ST_TOKEN_TYPES.get("typeParameter")
    _raw_pos = {(r[0], r[1]) for r in raw}
    if _dec_idx is not None:
        for _ln0, _col in at_positions:
            if start_line <= _ln0 <= end_line and (_ln0, _col) not in _raw_pos:
                raw.append((_ln0, _col, 1, _dec_idx, 0))
                _raw_pos.add((_ln0, _col))

    # Inject names from annotation strings ("Callable[P, T]").
    # Jedi does not parse string literals; BP resolves them via type inference.
    # typeParameter names (TypeVar/ParamSpec/TypeVarTuple) get readonly, consistent
    # with how the main token loop handles typeParameter references.
    if _tp_idx is not None and _cls_idx is not None:
        _ro_mask = 1 << _ST_TOKEN_MODIFIERS["readonly"]
        for _ln0, _col, _nm, _length in annstr_names:
            if start_line <= _ln0 <= end_line and (_ln0, _col) not in _raw_pos:
                # typeParameter if the name was declared as TypeVar/ParamSpec/TypeVarTuple.
                _is_tp = name_to_type.get(_nm) == "typeParameter"
                _st_idx = _tp_idx if _is_tp else _cls_idx
                # typeParameter references inside annotation strings also get readonly
                _mod = _ro_mask if _is_tp else 0
                raw.append((_ln0, _col, _length, _st_idx, _mod))
                _raw_pos.add((_ln0, _col))

    # Inject regexp tokens for re.compile() string patterns.
    # Jedi does not emit string literals as semantic tokens, so we inject them
    # manually like we do for '@' decorator tokens and annotation type names.
    _regexp_idx = _ST_TOKEN_TYPES.get("regexp")
    if _regexp_idx is not None:
        for _ln0, _col, _length in regexp_positions:
            if start_line <= _ln0 <= end_line and (_ln0, _col) not in _raw_pos:
                raw.append((_ln0, _col, _length, _regexp_idx, 0))
                _raw_pos.add((_ln0, _col))

    # Sort by (line, col) - Jedi may return names out of order
    raw.sort(key=lambda t: (t[0], t[1]))

    # Encode as relative deltas
    data: List[int] = []
    prev_line = 0
    prev_col = 0
    for line0, col, length, type_idx, mod_mask in raw:
        delta_line = line0 - prev_line
        delta_col = col if delta_line > 0 else col - prev_col
        data.extend([delta_line, delta_col, length, type_idx, mod_mask])
        prev_line = line0
        prev_col = col

    return data


def _st_next_result_id() -> str:
    """Return a monotonically increasing opaque result ID string."""
    with _ST_COUNTER_LOCK:
        _ST_RESULT_ID_COUNTER[0] += 1
        return str(_ST_RESULT_ID_COUNTER[0])


def _compute_st_delta(old_data: List[int], new_data: List[int]) -> List[dict]:
    """Compute SemanticTokensEdit[] from two flat integer token arrays.

    Uses SequenceMatcher (Ratcliff/Obershelp) with autojunk=False to avoid
    false "junk" detection on repeated integers (e.g. 0-deltas).

    Each edit covers a contiguous changed span in the *old* array:
        {"start": int, "deleteCount": int, "data": List[int]}
    where "data" is absent (or empty) for pure deletions.
    """
    if old_data == new_data:
        return []

    matcher = SequenceMatcher(None, old_data, new_data, autojunk=False)
    edits: List[dict] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        edit: dict = {
            "start": i1,
            "deleteCount": i2 - i1,
        }
        replacement = new_data[j1:j2]
        if replacement:
            edit["data"] = replacement
        edits.append(edit)

    return edits


@dataclass
class JediHint:
    """Internal container for a potential hint (Jedi-only implementation)."""
    kind: str  # "return", "assign", "raise", "parameter"
    line: int
    character: int
    label: str
    tooltip: Optional[str] = None

    def to_hint(self) -> Optional[dict]:
        """Build LSP InlayHint dict or None if no label."""
        if not self.label:
            return None

        # Validate character position (prevent out-of-bounds)
        if self.character < 0:
            self.character = 0
        elif self.character > 1000:  # Reasonable max for single line
            self.character = 1000

        # Handle parameter hints (appears BEFORE the argument)
        if self.kind == "parameter":
            return {
                "position": {
                    "line": self.line,
                    "character": self.character,
                },
                "label": self.label,
                "kind": 2,
                "paddingLeft": True,
                "paddingRight": False,
                "tooltip": self.tooltip or f"Parameter: {self.label}",
            }

        # Type hints (return, assign, raise)
        return {
            "position": {
                "line": self.line,
                "character": self.character,
            },
            "label": self.label,
            "kind": 1,
            "tooltip": self.tooltip,
        }


def _get_inlay_hints(source_code: str, path: str, settings: dict) -> List[dict]:
    """Compute inlay hints for the given source using Jedi inference."""
    if not HAS_INLAY_DEPS:
        log.warning("pylsp_workspace_symbols: Jedi not available - hints disabled")
        return []

    try:
        # Cache keyed by (path, source) so edits before save never return a
        # stale Script.  jedi.Script is immutable - there is no way to update
        # it in-place.
        cache_key = (path, hash(source_code))
        with _CACHE_LOCK:
            script = _JEDI_CACHE.get(cache_key)
            if script is None:
                script = _jedi.Script(code=source_code, path=path)
                _JEDI_CACHE[cache_key] = script

        # Use Jedi-based hint collection
        hints = _collect_jedi_hints(script, source_code, settings)

        # Apply max_hints limit
        max_hints = settings.get("max_hints_per_file", 200)
        if max_hints > 0 and len(hints) > max_hints:
            hints = hints[:max_hints]

        results = []
        for hint in hints:
            rendered = hint.to_hint()
            if rendered:
                results.append(rendered)
        return results

    except Exception as e:
        log.debug("pylsp_workspace_symbols: Jedi inlay hints failed for %s: %s", path, e)
        return []


def _collect_jedi_hints(script: _jedi.Script, source_code: str, settings: dict) -> List[JediHint]:
    """Collect all inlay hints by scanning source with regex + Jedi inference."""
    hints: List[JediHint] = []
    lines = source_code.splitlines()

    show_assign = settings.get("show_assign_types", True)
    show_return = settings.get("show_return_types", True)
    show_raise = settings.get("show_raises", True)
    show_params = settings.get("show_parameter_hints", True)

    # Collect hints by scanning source with regex + Jedi inference
    if show_return:
        hints.extend(_find_return_hints(script, source_code, lines))
    if show_assign:
        hints.extend(_find_assign_hints(script, source_code, lines))
    if show_raise:
        hints.extend(_find_raise_hints(script, source_code, lines))
    if show_params:
        hints.extend(_find_param_hints(script, source_code, lines))

    return hints


def _find_return_hints(script: _jedi.Script, source_code: str, lines: List[str]) -> List[JediHint]:
    """Find return type hints for unannotated functions.

    Strategy:
      1. Find every 'def' line without a '->' annotation.
      2. Scan the body for the first 'return <expr>'.
      3. Infer the type via _literal_type fast-path or script.infer().
      4. Emit a hint just before the trailing ':' of the def line.
    """
    hints = []
    def_pattern = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(')
    return_pattern = re.compile(r'^(\s*)return(?:\s+(.+))?$')

    for line_num, line in enumerate(lines, 1):
        def_match = def_pattern.match(line)
        if not def_match:
            continue

        indent, func_name = def_match.groups()
        func_indent_len = len(indent)

        # Skip functions that already carry an explicit return annotation.
        # Handle multiline signatures: scan forward until we find the closing
        # ')' of the parameter list - the '->' may be on a later line.
        # Strip inline comment first: "# esperado: -> str" would otherwise
        # falsely match and suppress the hint.
        line_no_comment = line[:line.find(' #')] if ' #' in line else line
        if '->' in line_no_comment:
            continue

        # Check for multiline signature: if the def line has no closing ')',
        # scan subsequent lines until we find ')' or '->'.
        if ')' not in line:
            has_return_annotation = False
            for sig_line_num in range(line_num + 1, min(line_num + 20, len(lines) + 1)):
                sig_line = lines[sig_line_num - 1]
                if '->' in sig_line:
                    has_return_annotation = True
                    break
                if ':' in sig_line and ')' in sig_line:
                    # Found closing of signature without '->'
                    break
                if sig_line.strip().startswith('def ') or sig_line.strip().startswith('class '):
                    break
            if has_return_annotation:
                continue

        # Scan body lines for the first meaningful statement.
        # Tracks whether we found ANY return to distinguish "returns None
        # explicitly" from "no return found yet".
        return_type: Optional[str] = None
        found_return = False

        for body_num in range(line_num + 1, len(lines) + 1):
            body_line = lines[body_num - 1]
            body_stripped = body_line.lstrip()

            # Stop when we leave the function body (back to same/outer indent),
            # ignoring blank lines and comment-only lines.
            if body_stripped and not body_stripped.startswith('#'):
                body_indent_len = len(body_line) - len(body_stripped)
                if body_indent_len <= func_indent_len:
                    # Reached end of function body without finding a return -
                    # implicit None return (e.g. body is just 'pass' or side effects)
                    if not found_return:
                        return_type = 'None'
                    break

            ret_match = return_pattern.match(body_line)
            if not ret_match:
                continue

            found_return = True
            ret_expr = (ret_match.group(2) or '').strip()
            # Strip inline comment from ret_expr
            if ' #' in ret_expr:
                ret_expr = ret_expr[:ret_expr.find(' #')].rstrip()

            # bare 'return', 'return None', 'return self/cls' -> None
            if not ret_expr or ret_expr in ('None', 'self', 'cls'):
                return_type = 'None'
                break

            # Fast path: detect literal return values without Jedi.
            # Jedi does not infer string/bool/None literals reliably.
            lit = _literal_type(ret_expr)
            if lit:
                return_type = lit
                break

            try:
                # Infer type of the returned expression.
                # Use find() instead of index() to avoid ValueError on
                # multiline expressions - fall back to start of line.
                ret_col = body_line.find(ret_expr, ret_match.start(2) or 0)
                if ret_col < 0:
                    ret_col = ret_match.start(2) or 0
                inferred = script.infer(line=body_num, column=ret_col)
                if inferred:
                    t = _format_jedi_type(inferred[0])
                    if t and t != "Unknown":
                        return_type = t
            except Exception as e:
                log.debug("pylsp_workspace_symbols: return infer failed for %s: %s", func_name, e)
            break  # Only use the first return statement found

        # Also treat a single-line function that ends with 'pass' as None
        if not return_type and not found_return:
            # Check if the only body content is 'pass' or '...'
            body_lines = [
                l.lstrip() for l in lines[line_num:line_num + 5]
                if l.strip() and not l.strip().startswith('#')
            ]
            if body_lines and body_lines[0] in ('pass', '...', 'pass\n', '...\n'):
                return_type = 'None'

        if not return_type:
            continue

        # Position hint just before the trailing ':' of the def line.
        # Strip inline comment first so rfind(':') does not land on
        # the ':' inside comments like "# esperado: -> Circle".
        line_code = line[:line.find(' #')] if ' #' in line else line
        colon_col = line_code.rfind(':')
        if colon_col == -1:
            colon_col = len(line_code.rstrip())

        hints.append(JediHint(
            kind="return",
            line=line_num - 1,
            character=colon_col,
            label=f"-> {return_type}",
            tooltip=f"Return type: -> {return_type}"
        ))

    return hints


_RE_LITERAL_INT   = re.compile(r'^-?\d+$')
_RE_LITERAL_FLOAT = re.compile(r'^-?\d*\.\d+([eE][+-]?\d+)?$')


def _literal_type(rhs: str) -> Optional[str]:
    """Return a type name for obvious literal RHS expressions, or None.

    Handles: None, bool, str (all quote styles/prefixes), int, float,
    list, tuple, dict, set.  Returns None for anything else so the caller
    can fall back to Jedi inference.
    """
    if not rhs:
        return None
    if rhs == "None":
        return "None"
    if rhs in ("True", "False"):
        return "bool"
    first = rhs[0].lower()
    if first in ('"', "'"):
        return "str"
    if first in ("f", "r", "b") and len(rhs) > 1:
        second = rhs[1].lower()
        if second in ('"', "'", "f", "r", "b"):
            return "bytes" if "b" in rhs[:3].lower() else "str"
    if _RE_LITERAL_INT.match(rhs):
        return "int"
    if _RE_LITERAL_FLOAT.match(rhs):
        return "float"
    if rhs[0] == "[":
        return "list"
    if rhs[0] == "(":
        return "tuple"
    if rhs[0] == "{":
        inner = rhs[1:rhs.rfind("}")]
        return "dict" if ":" in inner else "set"
    if rhs.startswith("lambda "):
        return "Callable"
    # Implicit tuple: "return 1, 'one'" produces ret_expr = "1, 'one'"
    # which has a comma but doesn't start with '(' '[' '{'
    if ',' in rhs and rhs[0] not in ('(', '[', '{'):
        return "tuple"
    return None


def _infer_param_type(param_name: str, line_num: int, lines: List[str]) -> Optional[str]:
    """Look up the enclosing function signature for param_name.

    Returns a type string if the parameter has a type annotation or a
    default value we can recognise via _literal_type.  Returns None if
    neither is available.

    This handles the common pattern:
        def __init__(self, radius: float, color="red"):
            self.radius = radius   # -> float  (from annotation)
            self.color = color     # -> str    (from default "red")
    """
    _param_re = re.compile(r'^(\s*)(?:async\s+)?def\s+\w+\s*\(')
    # Walk backwards from current line to find the enclosing def
    for i in range(line_num - 2, max(0, line_num - 60), -1):
        if _param_re.match(lines[i]):
            # Collect the full signature (may span multiple lines)
            sig_lines = []
            depth = 0
            for j in range(i, min(i + 30, len(lines))):
                sig_lines.append(lines[j])
                for ch in lines[j]:
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            break
                else:
                    continue
                break
            sig = ' '.join(sig_lines)

            # Match "param_name: SomeType" (annotation)
            ann_match = re.search(
                r'\b' + re.escape(param_name) + r'\s*:\s*([\w\[\], |]+?)\s*(?:=|,|\))',
                sig
            )
            if ann_match:
                return ann_match.group(1).strip().split('[')[0]  # strip generics

            # Match "param_name=default" (default value -> infer type)
            def_match = re.search(
                r'\b' + re.escape(param_name) + r'\s*=\s*([^,)]+)',
                sig
            )
            if def_match:
                default = def_match.group(1).strip()
                return _literal_type(default)

            return None
    return None


def _find_assign_hints(script: _jedi.Script, source_code: str, lines: List[str]) -> List[JediHint]:
    """Find assignment type hints for unannotated variables and self./cls. attributes.

    Handles literal RHS via _literal_type fast-path, function call RHS via
    Jedi get_signatures/infer, and bare parameter names via _infer_param_type.
    Skips annotated assignments ('var: Type = ...').
    """
    hints = []
    # Match both plain assignments and self./cls. attribute assignments:
    #   '    x = 42'               -> indent="    ", target="x",          rhs="42"
    #   '    self.radius = radius'  -> indent="    ", target="self.radius", rhs="radius"
    pattern = re.compile(r'^(\s*)((?:self|cls)\.\w+|\w+)\s*=\s*(.+)$')

    for line_num, line in enumerate(lines, 1):
        match = pattern.match(line)
        if not match:
            continue

        indent, target, value_expr = match.groups()
        stripped = value_expr.strip()

        # Skip definitions
        if stripped.startswith(('def ', 'class ', 'async ')):
            continue

        # Skip annotated assignments: 'var: Type = ...'
        # The pattern '\w+\s*:' before '=' means it's already annotated.
        # Note: 'self.x' won't falsely match this because '.'  breaks \w+
        if re.match(r'^\s*\w+\s*:', line):
            continue

        # Display label: for 'self.radius' show ': float' at 'self.radius'
        # hint_col points to the end of the target name
        hint_col = len(indent) + len(target)

        try:
            if '(' in stripped:
                # RHS is a function/method call.
                # Strategy A: function has explicit return annotation -> use it.
                # Strategy B: no annotation -> infer result after closing ')'.
                # Locate the '=' on this line dynamically to handle all spacing
                # variants: "x = f()", "x=f()", "x =f()", "x= f()".
                eq_pos = line.find('=', len(indent) + len(target))
                rhs_start = eq_pos + 1 if eq_pos >= 0 else len(indent) + len(target) + 3
                open_paren = line.find('(', rhs_start)
                if open_paren > 0:
                    # Strategy A
                    sigs = script.get_signatures(line=line_num, column=open_paren)
                    if sigs and sigs[0].return_annotation:
                        return_type = _format_jedi_type(sigs[0].return_annotation)
                        if return_type and return_type != "Unknown":
                            hints.append(JediHint(
                                kind="assign",
                                line=line_num - 1,
                                character=hint_col,
                                label=f": {return_type}",
                                tooltip=f"Type: {return_type}\n\nVariable: {target}"
                            ))
                            continue

                    # Strategy B: infer after closing ')'
                    close_paren = line.rfind(')')
                    if close_paren > open_paren:
                        inferred = script.infer(line=line_num, column=close_paren + 1)
                        if not inferred:
                            inferred = script.infer(line=line_num, column=open_paren + 1)
                        if inferred:
                            type_name = _format_jedi_type(inferred[0])
                            if type_name and type_name != "Unknown":
                                hints.append(JediHint(
                                    kind="assign",
                                    line=line_num - 1,
                                    character=hint_col,
                                    label=f": {type_name}",
                                    tooltip=f"Type: {type_name}\n\nVariable: {target}"
                                ))
                                continue

            # Non-call RHS: literals (str, int, bool, None, list, dict...),
            # attribute access, variables, etc.
            # Strip inline comment before processing.
            rhs = stripped.split(' #')[0].rstrip() if ' #' in stripped else stripped
            if not rhs:
                continue

            # Fast path: detect common literals directly without Jedi.
            # The bundled Jedi does not infer string/bool/None literals when
            # called with only code= (no path=), so we resolve these ourselves.
            type_name = _literal_type(rhs)

            # Self-attribute from parameter: 'self.x = param_name'
            # Jedi cannot infer the type of an unannotated parameter, but we
            # can look at the enclosing def signature for an annotation or a
            # default value and derive the type from there.
            if not type_name and re.match(r'^\w+$', rhs):
                type_name = _infer_param_type(rhs, line_num, lines)

            if not type_name:
                rhs_offset = line.index(rhs[0], len(indent) + len(target) + 2)
                col = min(rhs_offset, max(0, len(line) - 1))
                inferred = script.infer(line=line_num, column=col)
                if inferred:
                    type_name = _format_jedi_type(inferred[0])

            if type_name and type_name != "Unknown":
                hints.append(JediHint(
                    kind="assign",
                    line=line_num - 1,
                    character=hint_col,
                    label=f": {type_name}",
                    tooltip=f"Type: {type_name}\n\nVariable: {target}"
                ))
        except Exception as e:
            log.debug("pylsp_workspace_symbols: assign hint failed for %r: %s", target, e)

    return hints


def _find_raise_hints(script: _jedi.Script, source_code: str, lines: List[str]) -> List[JediHint]:
    """Find raised exception hints using regex + Jedi inference."""
    hints = []
    # Match raise statements: raise ExceptionName
    pattern = r'^\s*raise\s+(\w+)'

    for line_num, line in enumerate(lines, 1):
        match = re.search(pattern, line)
        if match:
            exc_name = match.group(1)
            try:
                # Infer the exception type via Jedi
                col = match.start(1)
                inferred = script.infer(line=line_num, column=col)
                if inferred:
                    exc_type = _format_jedi_type(inferred[0])
                    hints.append(JediHint(
                        kind="raise",
                        line=line_num - 1,
                        character=match.end(1),
                        label=f"Raises: {exc_type}",
                        tooltip=f"Raises: {exc_type}"
                    ))
            except Exception:
                # Fallback: use name as-is
                hints.append(JediHint(
                    kind="raise",
                    line=line_num - 1,
                    character=match.end(1),
                    label=f"Raises: {exc_name}",
                    tooltip=f"Raises: {exc_name}"
                ))

    return hints


def _find_param_hints(script: _jedi.Script, source_code: str, lines: List[str]) -> List[JediHint]:
    """Find parameter name hints for positional arguments at call sites.

    Skips keyword arguments, raise/assert lines, and noisy builtins.
    Uses Jedi get_signatures() to match positional args to parameter names.
    """
    hints = []
    # Python keywords that open a block - their '(' must not trigger hints
    _SKIP_NAMES = frozenset((
        'def', 'class', 'if', 'elif', 'while', 'for', 'with',
        'return', 'import', 'from', 'assert', 'raise', 'del',
        'lambda', 'not', 'and', 'or', 'in', 'is', 'yield',
    ))

    # Match function/method calls: name( or name.attr(
    call_pattern = re.compile(r'(\w+(?:\.\w+)*)\s*\(')

    # Builtin calls whose parameter names add no value as inlay hints
    _NOISY_BUILTINS = frozenset((
        'isinstance', 'issubclass', 'hasattr', 'getattr', 'setattr', 'delattr',
        'len', 'print', 'type', 'repr', 'str', 'int', 'float', 'bool', 'list',
        'dict', 'set', 'tuple', 'super', 'vars', 'dir', 'id', 'hash',
    ))

    for line_num, line in enumerate(lines, 1):
        # Skip definition lines entirely
        stripped = line.lstrip()
        if stripped.startswith(('def ', 'async def ', 'class ')):
            continue

        # Skip raise/assert lines -- the exception constructor args are noise
        if stripped.startswith(('raise ', 'assert ')):
            continue

        for call_match in call_pattern.finditer(line):
            func_expr = call_match.group(1)
            # The last segment of a dotted name (the actual callable)
            func_leaf = func_expr.split('.')[-1]
            if func_leaf in _SKIP_NAMES:
                continue
            if func_leaf in _NOISY_BUILTINS:
                continue

            # Column just after the '(' - Jedi needs to be inside the args
            open_paren_col = call_match.end()  # column after '('

            try:
                sigs = script.get_signatures(line=line_num, column=open_paren_col)
                if not sigs:
                    continue

                sig = sigs[0]
                # Parameter names, excluding self/cls and **kwargs / *args markers
                params = [
                    p.name.lstrip('*')
                    for p in sig.params
                    if p.name not in ('self', 'cls') and not p.name.startswith('**')
                ]
                if not params:
                    continue

                # Find the closing ')' for this call - simple scan (ignores nested)
                close_paren = line.find(')', open_paren_col)
                if close_paren == -1:
                    close_paren = len(line)
                args_str = line[open_paren_col:close_paren]

                # Split args by comma - simple; does not handle nested calls
                raw_args = args_str.split(',')

                cursor = open_paren_col  # walk along the original line
                for i, raw_arg in enumerate(raw_args):
                    if i >= len(params):
                        break

                    arg = raw_arg.strip()
                    if not arg:
                        cursor += len(raw_arg) + 1
                        continue

                    # Skip keyword arguments (already self-documenting)
                    if '=' in arg:
                        cursor += len(raw_arg) + 1
                        continue

                    # Find the exact column of this argument in the original line
                    arg_col = line.find(arg, cursor)
                    if arg_col < 0:
                        cursor += len(raw_arg) + 1
                        continue

                    hints.append(JediHint(
                        kind="parameter",
                        line=line_num - 1,
                        character=arg_col,
                        label=f"{params[i]}=",
                        tooltip=f"Parameter: {params[i]}="
                    ))

                    cursor = arg_col + len(arg) + 1  # advance past this arg + comma

            except Exception as e:
                log.debug("pylsp_workspace_symbols: param hint failed for %s: %s", func_expr, e)

    return hints


def _format_jedi_type(definition) -> str:
    """Format a Jedi definition/annotation object as a human-readable type string.

    Handles all the different shapes Jedi returns depending on context:
      - Plain str (return_annotation in Jedi 0.18+)
      - Jedi Name/Completion object from script.infer() or get_signatures()

    For inferred objects the priority is:
      1. description  - e.g. "instance str", "instance int", "class Circle"
      2. full_name    - e.g. "builtins.str", "mymodule.Circle"
      3. name         - last resort

    We intentionally do NOT use type_string because it includes full module
    paths which are too verbose for an inlay hint.
    """
    try:
        # Plain string (return_annotation on Jedi 0.18+)
        if isinstance(definition, str):
            s = definition.strip()
            return s if s else "Unknown"

        # Jedi Name object from script.infer() - has a .description like
        # "instance str", "instance int", "instance NoneType", "class Circle",
        # "function _helper"
        if hasattr(definition, 'description') and definition.description:
            desc = definition.description  # e.g. "instance str"
            parts = desc.split()
            if len(parts) == 2:
                kind, type_name = parts
                if kind in ('instance', 'class', 'module'):
                    # Normalize NoneType -> None
                    if type_name in ('NoneType', 'builtins.NoneType'):
                        return 'None'
                    # Shorten builtins
                    if type_name.startswith('builtins.'):
                        return type_name[len('builtins.'):]
                    return type_name.split('.')[-1] if '.' in type_name else type_name
                if kind == 'function':
                    # This is a function object, not a value - caller should
                    # use a different strategy (infer after ')' not at name)
                    return "Unknown"
            # Single-word description - use as-is
            if len(parts) == 1:
                t = parts[0]
                if t in ('NoneType', 'builtins.NoneType'):
                    return 'None'
                return t

        # full_name fallback (e.g. "builtins.str")
        if hasattr(definition, 'full_name') and definition.full_name:
            full = definition.full_name
            if full.startswith('builtins.'):
                return full[len('builtins.'):]
            return full.split('.')[-1] if '.' in full else full

        # name fallback
        if hasattr(definition, 'name') and definition.name:
            return definition.name

        return "Unknown"
    except Exception:
        return "Unknown"


@hookimpl
def pylsp_document_did_close(config, workspace, document):
    """Clears the Jedi Script and code lens caches when the document is closed."""
    with _CACHE_LOCK:
        stale = [k for k in _JEDI_CACHE if k[0] == document.path]
        for k in stale:
            del _JEDI_CACHE[k]
    uri = uris.from_fs_path(document.path)
    with _CL_CACHE_LOCK:
        _CL_CACHE.pop(uri, None)


@hookimpl
def pylsp_document_did_save(config, workspace, document):
    """Clears the caches on save to ensure up-to-date results."""
    with _CACHE_LOCK:
        stale = [k for k in _JEDI_CACHE if k[0] == document.path]
        for k in stale:
            del _JEDI_CACHE[k]
    uri = uris.from_fs_path(document.path)
    with _CL_CACHE_LOCK:
        _CL_CACHE.pop(uri, None)


# ---------------------------------------------------------------------------
# Call Hierarchy - implementation
# ---------------------------------------------------------------------------


def _ch_item_from_name(name: "_jedi.api.classes.Name", path: str = "") -> dict:
    """Build a CallHierarchyItem / TypeHierarchyItem dict from a Jedi Name."""
    module_path = str(name.module_path) if name.module_path else path
    line_0 = (name.line or 1) - 1   # 0-based
    col = name.column or 0
    symbol_uri = uris.from_fs_path(module_path) if module_path else ""
    span = {
        "start": {"line": line_0, "character": col},
        "end": {"line": line_0, "character": col + len(name.name)},
    }
    return {
        "name": name.name,
        "kind": _SYMBOL_KIND.get(name.type, _DEFAULT_KIND),
        "uri": symbol_uri,
        "range": span,
        "selectionRange": span,
        "data": {
            "path": module_path,
            "line": name.line or 1,   # 1-based kept for Jedi round-trips
            "column": col,
        },
    }


def _call_hierarchy_prepare(
    source_code: str, path: str, line: int, character: int
) -> Optional[List[dict]]:
    """textDocument/prepareCallHierarchy - resolve the callable under the cursor."""
    try:
        script = _jedi.Script(code=source_code, path=path)
        names = script.goto(line=line + 1, column=character)
        if not names:
            names = script.infer(line=line + 1, column=character)
        if not names or names[0].type not in ("function", "class"):
            return None
        return [_ch_item_from_name(names[0], path)]
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _call_hierarchy_prepare: %s", exc)
        return None


def _call_hierarchy_outgoing(item: dict, workspace) -> List[dict]:
    """callHierarchy/outgoingCalls - all callees inside the item's function body."""
    data = item.get("data", {})
    path = data.get("path", "")
    item_line = data.get("line", 1)   # 1-based

    if not path:
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        tree = _ast.parse(source)
        source_lines = source.splitlines()

        # Locate the function body end
        func_end = min(item_line + 300, len(source_lines))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.lineno == item_line:
                    func_end = node.end_lineno
                    break

        script = _jedi.Script(code=source, path=path)
        calls: List[dict] = []
        seen: set = set()

        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            call_line = node.lineno
            if not (item_line < call_line <= func_end):
                continue

            func_node = node.func
            if isinstance(func_node, _ast.Name):
                call_col = func_node.col_offset
            elif isinstance(func_node, _ast.Attribute):
                call_col = func_node.end_col_offset - len(func_node.attr)
            else:
                continue

            try:
                targets = script.goto(line=call_line, column=call_col)
                if not targets:
                    targets = script.infer(line=call_line, column=call_col)
                if not targets or targets[0].type not in ("function", "class"):
                    continue
                target = targets[0]
                if not target.module_path:
                    continue
                key = (str(target.module_path), target.line, target.column)
                if key in seen:
                    continue
                seen.add(key)
                from_ranges = [{
                    "start": {"line": call_line - 1, "character": call_col},
                    "end": {"line": call_line - 1, "character": call_col + len(target.name)},
                }]
                calls.append({"to": _ch_item_from_name(target), "fromRanges": from_ranges})
            except Exception:
                continue

        return calls
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _call_hierarchy_outgoing: %s", exc)
        return []


def _call_hierarchy_incoming(item: dict, workspace) -> List[dict]:
    """callHierarchy/incomingCalls - all call sites of the given callable."""
    data = item.get("data", {})
    path = data.get("path", "")
    item_line = data.get("line", 1)
    item_col = data.get("column", 0)

    if not path:
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        root_path = getattr(workspace, "root_path", None)
        project = _jedi.Project(path=root_path) if root_path else None
        script = _jedi.Script(code=source, path=path, project=project)
        refs = script.get_references(
            line=item_line, column=item_col, include_builtins=False
        )

        calls: List[dict] = []
        seen: set = set()
        # Cache file contents within this call to avoid reading the same file
        # twice - once for the import-check and once for get_context.
        _file_cache: Dict[str, str] = {}

        def _read_file(p: str) -> str:
            if p not in _file_cache:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    _file_cache[p] = fh.read()
            return _file_cache[p]

        import ast as _ast_chk  # hoisted out of the per-ref loop

        for ref in refs:
            if not ref.module_path:
                continue
            if _in_ignored_folder(str(ref.module_path), _DEFAULT_IGNORE_FOLDERS):
                continue
            # Skip the definition line itself
            if str(ref.module_path) == path and ref.line == item_line:
                continue

            ref_path = str(ref.module_path)
            ref_line = ref.line or 1
            ref_col  = ref.column or 0

            # Skip import statements - they are not call sites.
            # ref.type is unreliable; check the AST of the individual line.
            try:
                ref_src_lines = _read_file(ref_path).splitlines()
                ref_src_line = ref_src_lines[ref_line - 1] if ref_line <= len(ref_src_lines) else ""
                _node = _ast_chk.parse(ref_src_line.strip(), mode="single")
                if any(isinstance(n, (_ast_chk.Import, _ast_chk.ImportFrom))
                       for n in _ast_chk.walk(_node)):
                    continue
            except Exception:
                pass

            key = (ref_path, ref_line, ref_col)
            if key in seen:
                continue
            seen.add(key)

            # Resolve the enclosing caller - reuse already-read source.
            try:
                ref_source = _read_file(ref_path)
                ref_script = _jedi.Script(code=ref_source, path=ref_path)
                ctx = ref_script.get_context(line=ref_line, column=ref_col)
                if ctx and ctx.type in ("function", "class") and ctx.module_path:
                    from_item = _ch_item_from_name(ctx)
                else:
                    # Fallback: treat the reference as coming from module scope
                    from_item = {
                        "name": ref.module_name or "module",
                        "kind": 2,  # Module
                        "uri": uris.from_fs_path(ref_path),
                        "range": {
                            "start": {"line": ref_line - 1, "character": 0},
                            "end": {"line": ref_line - 1, "character": 0},
                        },
                        "selectionRange": {
                            "start": {"line": ref_line - 1, "character": 0},
                            "end": {"line": ref_line - 1, "character": 0},
                        },
                        "data": {"path": ref_path, "line": ref_line, "column": 0},
                    }
            except Exception:
                continue

            from_ranges = [{
                "start": {"line": ref_line - 1, "character": ref_col},
                "end": {
                    "line": ref_line - 1,
                    "character": ref_col + len(item.get("name", "")),
                },
            }]
            calls.append({"from": from_item, "fromRanges": from_ranges})

        return calls
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _call_hierarchy_incoming: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Type Hierarchy - implementation
# ---------------------------------------------------------------------------


def _type_hierarchy_prepare(
    source_code: str, path: str, line: int, character: int
) -> Optional[List[dict]]:
    """textDocument/prepareTypeHierarchy - resolve the class under the cursor."""
    try:
        script = _jedi.Script(code=source_code, path=path)
        names = script.goto(line=line + 1, column=character)
        if not names:
            names = script.infer(line=line + 1, column=character)
        if not names or names[0].type != "class":
            return None
        return [_ch_item_from_name(names[0], path)]
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _type_hierarchy_prepare: %s", exc)
        return None


def _type_hierarchy_supertypes(item: dict) -> List[dict]:
    """typeHierarchy/supertypes - direct parent classes of the given class."""
    data = item.get("data", {})
    path = data.get("path", "")
    item_line = data.get("line", 1)

    if not path:
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        tree = _ast.parse(source)
        script = _jedi.Script(code=source, path=path)
        supertypes: List[dict] = []
        seen: set = set()

        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ClassDef) or node.lineno != item_line:
                continue
            for base in node.bases:
                if isinstance(base, _ast.Name):
                    base_col = base.col_offset
                elif isinstance(base, _ast.Attribute):
                    base_col = base.end_col_offset - len(base.attr)
                else:
                    continue
                try:
                    targets = script.goto(line=base.lineno, column=base_col)
                    if not targets:
                        targets = script.infer(line=base.lineno, column=base_col)
                    if not targets or targets[0].type != "class":
                        continue
                    t = targets[0]
                    if not t.module_path:
                        continue
                    key = (str(t.module_path), t.line)
                    if key in seen:
                        continue
                    seen.add(key)
                    supertypes.append(_ch_item_from_name(t))
                except Exception:
                    continue
            break  # only the matching ClassDef

        return supertypes
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _type_hierarchy_supertypes: %s", exc)
        return []


def _type_hierarchy_subtypes(item: dict, workspace) -> List[dict]:
    """typeHierarchy/subtypes - classes that inherit from the given class."""
    data = item.get("data", {})
    path = data.get("path", "")
    item_line = data.get("line", 1)
    item_col = data.get("column", 0)
    class_name = item.get("name", "")

    if not path or not class_name:
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        root_path = getattr(workspace, "root_path", None)
        project = _jedi.Project(path=root_path) if root_path else None
        script = _jedi.Script(code=source, path=path, project=project)
        refs = script.get_references(
            line=item_line, column=item_col, include_builtins=False
        )

        subtypes: List[dict] = []
        seen: set = set()
        # Avoid re-reading and re-parsing the same file for multiple refs.
        _src_cache: Dict[str, str] = {}
        _script_cache: Dict[str, Any] = {}

        for ref in refs:
            if not ref.module_path:
                continue
            if _in_ignored_folder(str(ref.module_path), _DEFAULT_IGNORE_FOLDERS):
                continue
            if str(ref.module_path) == path and ref.line == item_line:
                continue

            ref_path = str(ref.module_path)
            ref_line = ref.line or 1

            try:
                if ref_path not in _src_cache:
                    with open(ref_path, encoding="utf-8", errors="replace") as fh:
                        _src_cache[ref_path] = fh.read()
                ref_src = _src_cache[ref_path]
                ref_tree = _ast.parse(ref_src)

                for node in _ast.walk(ref_tree):
                    if not isinstance(node, _ast.ClassDef):
                        continue
                    # Check whether any base expression falls on this ref line
                    if not any(b.lineno == ref_line for b in node.bases):
                        continue
                    key = (ref_path, node.lineno)
                    if key in seen:
                        continue
                    seen.add(key)

                    # Resolve the subclass name via Jedi for rich data.
                    # Reuse Script for the same file across multiple nodes.
                    try:
                        if ref_path not in _script_cache:
                            _script_cache[ref_path] = _jedi.Script(code=ref_src, path=ref_path)
                        rs = _script_cache[ref_path]
                        targets = rs.goto(
                            line=node.lineno,
                            column=node.col_offset + len("class "),
                        )
                        if not targets:
                            targets = rs.infer(
                                line=node.lineno,
                                column=node.col_offset + len("class "),
                            )
                        if targets and targets[0].type == "class":
                            subtypes.append(_ch_item_from_name(targets[0]))
                            continue
                    except Exception:
                        pass

                    # Fallback: build item from AST node directly
                    sub_line_0 = node.lineno - 1
                    sub_col = node.col_offset
                    span = {
                        "start": {"line": sub_line_0, "character": sub_col},
                        "end": {"line": sub_line_0, "character": sub_col + len(node.name)},
                    }
                    subtypes.append({
                        "name": node.name,
                        "kind": 5,  # Class
                        "uri": uris.from_fs_path(ref_path),
                        "range": span,
                        "selectionRange": span,
                        "data": {
                            "path": ref_path,
                            "line": node.lineno,
                            "column": sub_col,
                        },
                    })
            except Exception:
                continue

        return subtypes
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _type_hierarchy_subtypes: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Document Links - implementation
# ---------------------------------------------------------------------------

def _collect_document_links(source: str, path: str, workspace) -> List[dict]:
    """textDocument/documentLink - find clickable references in Python source.

    Passes (in order):
      1. URLs (http/https) in ``#`` comments, docstrings, and string literals.
      2. Import statements resolved to local ``.py`` files or ``__init__.py``
         packages, with Jedi fallback for stdlib and third-party modules.
      3. String literals that look like file-system paths.
      4. ``open()`` / ``Path()`` / ``pathlib.Path()`` call arguments.
    """
    results: List[dict] = []
    seen: set = set()
    base_dir = os.path.dirname(path) if path else ""
    root_path = getattr(workspace, "root_path", None) or base_dir
    lines = source.splitlines()

    def _add_link(line_0: int, col_start: int, col_end: int, target: str) -> None:
        key = (line_0, col_start)
        if key in seen:
            return
        seen.add(key)
        results.append({
            "range": {
                "start": {"line": line_0, "character": col_start},
                "end":   {"line": line_0, "character": col_end},
            },
            "target": target,
        })

    def _resolve_path(raw: str) -> Optional[str]:
        """Resolve a raw string to a file URI if the file exists on disk."""
        if not raw or len(raw) > 260:
            return None
        if os.path.isabs(raw):
            return uris.from_fs_path(raw) if os.path.exists(raw) else None
        candidate = os.path.normpath(os.path.join(base_dir, raw))
        if os.path.exists(candidate):
            return uris.from_fs_path(candidate)
        candidate2 = os.path.normpath(os.path.join(root_path, raw))
        if os.path.exists(candidate2):
            return uris.from_fs_path(candidate2)
        return None

    # ------------------------------------------------------------------ #
    # 1. URLs in comments, docstrings, and string literals
    # ------------------------------------------------------------------ #
    # Matches http:// and https:// URLs, stopping at whitespace or
    # common trailing punctuation that is unlikely to be part of the URL
    # (closing quotes, parens, brackets, angle-brackets, comma, period at
    # end-of-sentence).  The URL itself is captured in group 1.
    _URL_RE = re.compile(
        r"""https?://[^\s'"<>()\[\]]+""",
        re.IGNORECASE,
    )

    # Pre-compute triple-string spans so we can tell whether a given
    # position is inside a docstring / multi-line string.
    triple_spans = _find_triple_string_spans(source)

    # Build a char-offset -> line/col mapping for the URL scanner which
    # operates on the raw source rather than line-by-line.
    _line_offsets: List[int] = []  # _line_offsets[i] = char offset of line i
    _off = 0
    for _ln in source.splitlines(keepends=True):
        _line_offsets.append(_off)
        _off += len(_ln)

    def _offset_to_linecol(offset: int):
        """Binary-search line_offsets to convert a source char offset to (line, col)."""
        lo, hi = 0, len(_line_offsets) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _line_offsets[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo, offset - _line_offsets[lo]

    def _in_comment_or_string(offset: int) -> bool:
        """Return True if *offset* is inside a comment or any string literal."""
        # Inside a triple-quoted string?
        if any(ts <= offset < te for ts, te in triple_spans):
            return True
        # Walk the source up to *offset* tracking single-line quote and
        # comment state to handle regular strings and inline comments.
        line_no, col = _offset_to_linecol(offset)
        line_start = _line_offsets[line_no]
        seg = source[line_start:line_start + col]
        in_s, in_d = False, False
        for ch in seg:
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d:
                return True   # rest of line is a comment
        return in_s or in_d

    for m in _URL_RE.finditer(source):
        url = m.group(0)
        # Strip trailing punctuation characters that are commonly appended
        # after URLs in prose (e.g. "see https://example.com.")
        url = url.rstrip(".,;:!?)>]\"'")
        if not url:
            continue
        start_off = m.start()
        if not _in_comment_or_string(start_off):
            continue  # only emit URLs that appear in comments or strings
        line_no, col = _offset_to_linecol(start_off)
        _add_link(line_no, col, col + len(url), url)

    # ------------------------------------------------------------------ #
    # 2. Import statements -> local .py / __init__.py / Jedi fallback
    # ------------------------------------------------------------------ #
    def _resolve_module(mod_name: str) -> Optional[str]:
        """Return a file URI for *mod_name*.

        Strategy:
          (a) Local workspace: foo/bar.py or foo/bar/__init__.py
          (b) System Python stdlib: ask the system Python (via shutil.which)
              for its prefix, then search Lib/ there. This works even when
              the plugin itself runs under an embedded Python that only has
              .pyc files in a zip.
          (c) Current interpreter find_spec: handles cases where the plugin
              runs under a full Python installation.
        """
        import sys as _sys
        import shutil as _shutil
        import subprocess as _sp

        parts = mod_name.split(".")

        # (a) local workspace
        t = _resolve_path(os.path.join(*parts) + ".py")
        if t:
            return t
        t = _resolve_path(os.path.join(*parts, "__init__.py"))
        if t:
            return t

        # (b) system Python stdlib - ask the system python for its prefix.
        #     Cached at function level so we only run the subprocess once.
        if not hasattr(_resolve_module, "_sys_lib_dirs"):
            _resolve_module._sys_lib_dirs = []
            for exe_name in ("python3", "python"):
                exe = _shutil.which(exe_name)
                if not exe:
                    continue
                try:
                    # Single subprocess: get prefix, exec_prefix and version together
                    result = _sp.run(
                        [exe, "-c",
                         "import sys, json; "
                         "print(json.dumps({"
                         "'prefixes': [sys.prefix, sys.exec_prefix],"
                         "'version': sys.version[:4]"
                         "}))"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode != 0:
                        continue
                    import json as _json
                    data = _json.loads(result.stdout.strip())
                    sys_ver = data.get("version", "")
                    seen_prefixes: set = set()
                    for prefix in data.get("prefixes", []):
                        if prefix in seen_prefixes:
                            continue
                        seen_prefixes.add(prefix)
                        candidates = [
                            "Lib",  # Windows
                            "lib",  # Linux/macOS fallback
                        ]
                        if sys_ver:
                            # e.g. lib/python3.14 (Linux/macOS)
                            candidates.append(os.path.join("lib", "python" + sys_ver))
                        for lib_name in candidates:
                            lib_dir = os.path.normcase(os.path.join(prefix, lib_name))
                            if os.path.isdir(lib_dir) and lib_dir not in _resolve_module._sys_lib_dirs:
                                _resolve_module._sys_lib_dirs.append(lib_dir)
                    if _resolve_module._sys_lib_dirs:
                        break  # found a usable Python, stop
                except Exception:
                    continue

        for lib_dir in _resolve_module._sys_lib_dirs:
            candidate = os.path.join(lib_dir, *parts) + ".py"
            if os.path.isfile(candidate):
                return uris.from_fs_path(candidate)
            candidate2 = os.path.join(lib_dir, *parts, "__init__.py")
            if os.path.isfile(candidate2):
                return uris.from_fs_path(candidate2)

        # (c) current interpreter find_spec - works when running under a full
        #     Python installation (not embedded).
        try:
            import importlib.util as _ilu
            spec = _ilu.find_spec(mod_name)
            if spec is not None:
                origin = spec.origin
                if origin and origin.endswith(".py") and os.path.isfile(origin):
                    return uris.from_fs_path(origin)
                locs = list(spec.submodule_search_locations or [])
                for loc in locs:
                    init = os.path.join(loc, "__init__.py")
                    if os.path.isfile(init):
                        return uris.from_fs_path(init)
        except Exception:
            pass

        return None

    try:
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    target = _resolve_module(alias.name)
                    if target and node.lineno:
                        src_line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        col = src_line.find(alias.name)
                        if col >= 0:
                            _add_link(node.lineno - 1, col,
                                      col + len(alias.name), target)
            elif isinstance(node, _ast.ImportFrom):
                if node.module:
                    target = _resolve_module(node.module)
                    if target and node.lineno:
                        src_line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        col = src_line.find(node.module)
                        if col >= 0:
                            _add_link(node.lineno - 1, col,
                                      col + len(node.module), target)
    except Exception:
        pass

    # ------------------------------------------------------------------ #
    # 3. String literals that look like file-system paths
    # ------------------------------------------------------------------ #
    _PATH_RE = re.compile(
        r"""['"]((?:\.{1,2}/|/)[^'"*?\r\n<>|:]{1,255}|[a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-\.]+)+)['"]"""
    )
    for line_idx, line_text in enumerate(lines):
        for m in _PATH_RE.finditer(line_text):
            raw = m.group(1)
            target = _resolve_path(raw)
            if target:
                col_start = m.start() + 1   # skip opening quote
                col_end = col_start + len(raw)
                _add_link(line_idx, col_start, col_end, target)

    # ------------------------------------------------------------------ #
    # 4. open() / Path() / pathlib.Path() call arguments
    # ------------------------------------------------------------------ #
    _CALL_RE = re.compile(
        r"""\b(?:open|Path|pathlib\.Path)\s*\(\s*['"]([^'"]+)['"]"""
    )
    for line_idx, line_text in enumerate(lines):
        for m in _CALL_RE.finditer(line_text):
            raw = m.group(1)
            target = _resolve_path(raw)
            if target:
                col_start = m.start(1)
                col_end = col_start + len(raw)
                _add_link(line_idx, col_start, col_end, target)

    return results


# ---------------------------------------------------------------------------
# Document Colors - implementation
# ---------------------------------------------------------------------------

# CSS named colors - full W3C/CSS3 set relevant for Python codebases.
# Values are (r, g, b, a) in 0.0-1.0 range (alpha always 1.0).
_CSS_COLORS: Dict[str, tuple] = {
    "aliceblue": (0.941, 0.973, 1.0, 1.0),
    "antiquewhite": (0.98, 0.922, 0.843, 1.0),
    "aqua": (0.0, 1.0, 1.0, 1.0),
    "aquamarine": (0.498, 1.0, 0.831, 1.0),
    "azure": (0.941, 1.0, 1.0, 1.0),
    "beige": (0.961, 0.961, 0.863, 1.0),
    "bisque": (1.0, 0.894, 0.769, 1.0),
    "black": (0.0, 0.0, 0.0, 1.0),
    "blanchedalmond": (1.0, 0.922, 0.804, 1.0),
    "blue": (0.0, 0.0, 1.0, 1.0),
    "blueviolet": (0.541, 0.169, 0.886, 1.0),
    "brown": (0.647, 0.165, 0.165, 1.0),
    "burlywood": (0.871, 0.722, 0.529, 1.0),
    "cadetblue": (0.373, 0.620, 0.627, 1.0),
    "chartreuse": (0.498, 1.0, 0.0, 1.0),
    "chocolate": (0.824, 0.412, 0.118, 1.0),
    "coral": (1.0, 0.498, 0.314, 1.0),
    "cornflowerblue": (0.392, 0.584, 0.929, 1.0),
    "cornsilk": (1.0, 0.973, 0.863, 1.0),
    "crimson": (0.863, 0.078, 0.235, 1.0),
    "cyan": (0.0, 1.0, 1.0, 1.0),
    "darkblue": (0.0, 0.0, 0.545, 1.0),
    "darkcyan": (0.0, 0.545, 0.545, 1.0),
    "darkgoldenrod": (0.722, 0.525, 0.043, 1.0),
    "darkgray": (0.663, 0.663, 0.663, 1.0),
    "darkgreen": (0.0, 0.392, 0.0, 1.0),
    "darkgrey": (0.663, 0.663, 0.663, 1.0),
    "darkkhaki": (0.741, 0.718, 0.420, 1.0),
    "darkmagenta": (0.545, 0.0, 0.545, 1.0),
    "darkolivegreen": (0.333, 0.420, 0.184, 1.0),
    "darkorange": (1.0, 0.549, 0.0, 1.0),
    "darkorchid": (0.600, 0.196, 0.800, 1.0),
    "darkred": (0.545, 0.0, 0.0, 1.0),
    "darksalmon": (0.914, 0.588, 0.478, 1.0),
    "darkseagreen": (0.561, 0.737, 0.561, 1.0),
    "darkslateblue": (0.282, 0.239, 0.545, 1.0),
    "darkslategray": (0.184, 0.310, 0.310, 1.0),
    "darkslategrey": (0.184, 0.310, 0.310, 1.0),
    "darkturquoise": (0.0, 0.808, 0.820, 1.0),
    "darkviolet": (0.580, 0.0, 0.827, 1.0),
    "deeppink": (1.0, 0.078, 0.576, 1.0),
    "deepskyblue": (0.0, 0.749, 1.0, 1.0),
    "dimgray": (0.412, 0.412, 0.412, 1.0),
    "dimgrey": (0.412, 0.412, 0.412, 1.0),
    "dodgerblue": (0.118, 0.565, 1.0, 1.0),
    "firebrick": (0.698, 0.133, 0.133, 1.0),
    "floralwhite": (1.0, 0.98, 0.941, 1.0),
    "forestgreen": (0.133, 0.545, 0.133, 1.0),
    "fuchsia": (1.0, 0.0, 1.0, 1.0),
    "gainsboro": (0.863, 0.863, 0.863, 1.0),
    "ghostwhite": (0.973, 0.973, 1.0, 1.0),
    "gold": (1.0, 0.843, 0.0, 1.0),
    "goldenrod": (0.855, 0.647, 0.125, 1.0),
    "gray": (0.502, 0.502, 0.502, 1.0),
    "green": (0.0, 0.502, 0.0, 1.0),
    "greenyellow": (0.678, 1.0, 0.184, 1.0),
    "grey": (0.502, 0.502, 0.502, 1.0),
    "honeydew": (0.941, 1.0, 0.941, 1.0),
    "hotpink": (1.0, 0.412, 0.706, 1.0),
    "indianred": (0.804, 0.361, 0.361, 1.0),
    "indigo": (0.294, 0.0, 0.510, 1.0),
    "ivory": (1.0, 1.0, 0.941, 1.0),
    "khaki": (0.941, 0.902, 0.549, 1.0),
    "lavender": (0.902, 0.902, 0.980, 1.0),
    "lavenderblush": (1.0, 0.941, 0.961, 1.0),
    "lawngreen": (0.486, 0.988, 0.0, 1.0),
    "lemonchiffon": (1.0, 0.980, 0.804, 1.0),
    "lightblue": (0.678, 0.847, 0.902, 1.0),
    "lightcoral": (0.941, 0.502, 0.502, 1.0),
    "lightcyan": (0.878, 1.0, 1.0, 1.0),
    "lightgoldenrodyellow": (0.980, 0.980, 0.824, 1.0),
    "lightgray": (0.827, 0.827, 0.827, 1.0),
    "lightgreen": (0.565, 0.933, 0.565, 1.0),
    "lightgrey": (0.827, 0.827, 0.827, 1.0),
    "lightpink": (1.0, 0.714, 0.757, 1.0),
    "lightsalmon": (1.0, 0.627, 0.478, 1.0),
    "lightseagreen": (0.125, 0.698, 0.667, 1.0),
    "lightskyblue": (0.529, 0.808, 0.980, 1.0),
    "lightslategray": (0.467, 0.533, 0.600, 1.0),
    "lightslategrey": (0.467, 0.533, 0.600, 1.0),
    "lightsteelblue": (0.690, 0.769, 0.871, 1.0),
    "lightyellow": (1.0, 1.0, 0.878, 1.0),
    "lime": (0.0, 1.0, 0.0, 1.0),
    "limegreen": (0.196, 0.804, 0.196, 1.0),
    "linen": (0.980, 0.941, 0.902, 1.0),
    "magenta": (1.0, 0.0, 1.0, 1.0),
    "maroon": (0.502, 0.0, 0.0, 1.0),
    "mediumaquamarine": (0.400, 0.804, 0.667, 1.0),
    "mediumblue": (0.0, 0.0, 0.804, 1.0),
    "mediumorchid": (0.729, 0.333, 0.827, 1.0),
    "mediumpurple": (0.576, 0.439, 0.859, 1.0),
    "mediumseagreen": (0.235, 0.702, 0.443, 1.0),
    "mediumslateblue": (0.482, 0.408, 0.933, 1.0),
    "mediumspringgreen": (0.0, 0.980, 0.604, 1.0),
    "mediumturquoise": (0.282, 0.820, 0.800, 1.0),
    "mediumvioletred": (0.780, 0.082, 0.522, 1.0),
    "midnightblue": (0.098, 0.098, 0.439, 1.0),
    "mintcream": (0.961, 1.0, 0.980, 1.0),
    "mistyrose": (1.0, 0.894, 0.882, 1.0),
    "moccasin": (1.0, 0.894, 0.710, 1.0),
    "navajowhite": (1.0, 0.871, 0.678, 1.0),
    "navy": (0.0, 0.0, 0.502, 1.0),
    "oldlace": (0.992, 0.961, 0.902, 1.0),
    "olive": (0.502, 0.502, 0.0, 1.0),
    "olivedrab": (0.420, 0.557, 0.137, 1.0),
    "orange": (1.0, 0.647, 0.0, 1.0),
    "orangered": (1.0, 0.271, 0.0, 1.0),
    "orchid": (0.855, 0.439, 0.839, 1.0),
    "palegoldenrod": (0.933, 0.910, 0.667, 1.0),
    "palegreen": (0.596, 0.984, 0.596, 1.0),
    "paleturquoise": (0.686, 0.933, 0.933, 1.0),
    "palevioletred": (0.859, 0.439, 0.576, 1.0),
    "papayawhip": (1.0, 0.937, 0.835, 1.0),
    "peachpuff": (1.0, 0.855, 0.725, 1.0),
    "peru": (0.804, 0.522, 0.247, 1.0),
    "pink": (1.0, 0.753, 0.796, 1.0),
    "plum": (0.867, 0.627, 0.867, 1.0),
    "powderblue": (0.690, 0.878, 0.902, 1.0),
    "purple": (0.502, 0.0, 0.502, 1.0),
    "rebeccapurple": (0.400, 0.200, 0.600, 1.0),
    "red": (1.0, 0.0, 0.0, 1.0),
    "rosybrown": (0.737, 0.561, 0.561, 1.0),
    "royalblue": (0.255, 0.412, 0.882, 1.0),
    "saddlebrown": (0.545, 0.271, 0.075, 1.0),
    "salmon": (0.980, 0.502, 0.447, 1.0),
    "sandybrown": (0.957, 0.643, 0.376, 1.0),
    "seagreen": (0.180, 0.545, 0.341, 1.0),
    "seashell": (1.0, 0.961, 0.933, 1.0),
    "sienna": (0.627, 0.322, 0.176, 1.0),
    "silver": (0.753, 0.753, 0.753, 1.0),
    "skyblue": (0.529, 0.808, 0.922, 1.0),
    "slateblue": (0.416, 0.353, 0.804, 1.0),
    "slategray": (0.439, 0.502, 0.565, 1.0),
    "slategrey": (0.439, 0.502, 0.565, 1.0),
    "snow": (1.0, 0.980, 0.980, 1.0),
    "springgreen": (0.0, 1.0, 0.498, 1.0),
    "steelblue": (0.275, 0.510, 0.706, 1.0),
    "tan": (0.824, 0.706, 0.549, 1.0),
    "teal": (0.0, 0.502, 0.502, 1.0),
    "thistle": (0.847, 0.749, 0.847, 1.0),
    "tomato": (1.0, 0.388, 0.278, 1.0),
    "turquoise": (0.251, 0.878, 0.816, 1.0),
    "violet": (0.933, 0.510, 0.933, 1.0),
    "wheat": (0.961, 0.871, 0.702, 1.0),
    "white": (1.0, 1.0, 1.0, 1.0),
    "whitesmoke": (0.961, 0.961, 0.961, 1.0),
    "yellow": (1.0, 1.0, 0.0, 1.0),
    "yellowgreen": (0.604, 0.804, 0.196, 1.0),
}

# Sorted longest-first so multi-word names (e.g. "darkslategray") beat short
# prefixes (e.g. "dark") in the alternation built from this list.
_CSS_COLOR_NAMES_SORTED = sorted(_CSS_COLORS.keys(), key=len, reverse=True)

# Single compiled regex that matches any quoted CSS named color.
# Group 1 captures the name itself (without quotes).
_NAMED_COLOR_RE = re.compile(
    r"""['"](""" + "|".join(re.escape(n) for n in _CSS_COLOR_NAMES_SORTED) + r""")['"]""",
    re.IGNORECASE,
)

# Hex color regex - two variants kept separate so callers know which context
# was used for correct column attribution.
#
# _HEX_QUOTED_RE  - the safe form: "#rrggbb" or '#rgb' inside Python quotes.
#   Group 1 = the #-prefixed token; the surrounding quotes are consumed but
#   not captured so m.start(1) points directly at the '#'.
#
# _HEX_BARE_RE    - unquoted form for content inside triple-quoted strings /
#   CSS/HTML embedded in Python (e.g. `--color: #6200ea;`).
#   Anchored so '#' is NOT preceded by a word char (avoids CSS id selectors
#   like `#myId` which start with a letter after #) and IS followed only by
#   exactly 8, 6, or 3 hex digits with a non-hex-word delimiter after.
#   Group 1 = the #-prefixed token.
_HEX_QUOTED_RE = re.compile(
    r"""['"](#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))['"]"""
)
_HEX_BARE_RE = re.compile(
    r"""(?<!['"\\])(#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))(?![0-9a-fA-F'"])"""
)

# rgb() / rgba() functional notation.
# Accepts both integer (0-255) and percentage (0%-100%) for channels,
# and integer, float, or percentage for alpha.
# Group layout: (r_val, g_val, b_val, a_val_or_None)
_NUM = r"\s*(\d+(?:\.\d*)?%?)\s*"
_ALPHA = r"(?:[,/]\s*([\d.]+%?)\s*)?"
_RGB_FUNC_RE = re.compile(
    r"""\brgba?\s*\(""" + _NUM + r"," + _NUM + r"," + _NUM + _ALPHA + r"""\)""",
    re.IGNORECASE,
)

# hsl() / hsla() functional notation.
# H in degrees (0-360), S and L as percentages, optional alpha.
_HSL_FUNC_RE = re.compile(
    r"""\bhsla?\s*\(""" + _NUM + r"," + _NUM + r"," + _NUM + _ALPHA + r"""\)""",
    re.IGNORECASE,
)

# Tuple/list integer RGB(A): (255, 102, 0) or (255, 102, 0, 128)
_TUPLE_RGB_RE = re.compile(
    r"\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(\d{1,3}))?\s*\)"
)
# Tuple/list float RGB(A): (1.0, 0.4, 0.0) or (1.0, 0.4, 0.0, 0.5)
_TUPLE_FLOAT_RE = re.compile(
    r"\(\s*(0?\.\d+|1\.0|[01])\s*,\s*(0?\.\d+|1\.0|[01])\s*,\s*(0?\.\d+|1\.0|[01])\s*"
    r"(?:,\s*(0?\.\d+|1\.0|[01]))?\s*\)"
)

# Variable name patterns that suggest a color context - used only for
# bare tuple detection, not for functional notation or named colors.
_COLOR_VAR_RE = re.compile(
    r"\b(colou?rs?|bg|background|fg|foreground|fill|stroke|tint|hue|shade|"
    r"text_?colou?r|btn_?colou?r|border_?colou?r|accent|surface|on_surface|"
    r"on_bg|on_primary|on_secondary|on_error|shadow|outline|primary|secondary|"
    r"error|warning)\b",
    re.IGNORECASE,
)


def _hex_to_rgba(hex_str: str) -> Optional[tuple]:
    """Parse #rgb, #rrggbb, #rrggbbaa -> (r, g, b, a) in 0.0-1.0."""
    h = hex_str.lstrip("#")
    try:
        if len(h) == 3:
            r = int(h[0] * 2, 16) / 255.0
            g = int(h[1] * 2, 16) / 255.0
            b = int(h[2] * 2, 16) / 255.0
            return (r, g, b, 1.0)
        if len(h) == 6:
            return (
                int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0,
                1.0,
            )
        if len(h) == 8:
            return (
                int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0,
                int(h[6:8], 16) / 255.0,
            )
    except ValueError:
        pass
    return None


def _parse_css_value(raw: str) -> float:
    """Convert a CSS channel string ('128', '50%') to a 0.0-1.0 float."""
    raw = raw.strip()
    if raw.endswith("%"):
        return max(0.0, min(1.0, float(raw[:-1]) / 100.0))
    return max(0.0, min(1.0, float(raw) / 255.0))


def _parse_css_alpha(raw: Optional[str]) -> float:
    """Convert a CSS alpha string ('0.5', '50%', '1') to 0.0-1.0 float."""
    if raw is None:
        return 1.0
    raw = raw.strip()
    if raw.endswith("%"):
        return max(0.0, min(1.0, float(raw[:-1]) / 100.0))
    v = float(raw)
    # Alpha > 1 is treated as a 0-255 integer (non-standard but seen in the wild)
    return max(0.0, min(1.0, v if v <= 1.0 else v / 255.0))


def _hsl_to_rgb(h_deg: float, s_pct: float, l_pct: float) -> tuple:
    """Convert HSL (degrees, percent, percent) to (r, g, b) in 0.0-1.0."""
    s = s_pct / 100.0
    l = l_pct / 100.0
    c = (1.0 - abs(2.0 * l - 1.0)) * s
    h_prime = (h_deg % 360.0) / 60.0
    x = c * (1.0 - abs(h_prime % 2.0 - 1.0))
    if h_prime < 1:
        r1, g1, b1 = c, x, 0.0
    elif h_prime < 2:
        r1, g1, b1 = x, c, 0.0
    elif h_prime < 3:
        r1, g1, b1 = 0.0, c, x
    elif h_prime < 4:
        r1, g1, b1 = 0.0, x, c
    elif h_prime < 5:
        r1, g1, b1 = x, 0.0, c
    else:
        r1, g1, b1 = c, 0.0, x
    m = l - c / 2.0
    return (
        max(0.0, min(1.0, r1 + m)),
        max(0.0, min(1.0, g1 + m)),
        max(0.0, min(1.0, b1 + m)),
    )


def _rgb_to_hsl(r: float, g: float, b: float) -> tuple:
    """Convert (r, g, b) in 0.0-1.0 to (h_deg, s_pct, l_pct)."""
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    delta = cmax - cmin
    l = (cmax + cmin) / 2.0

    if delta == 0.0:
        h = 0.0
        s = 0.0
    else:
        s = delta / (1.0 - abs(2.0 * l - 1.0))
        if cmax == r:
            h = 60.0 * (((g - b) / delta) % 6.0)
        elif cmax == g:
            h = 60.0 * ((b - r) / delta + 2.0)
        else:
            h = 60.0 * ((r - g) / delta + 4.0)

    return (round(h % 360.0, 1), round(s * 100.0, 1), round(l * 100.0, 1))


def _color_presentations(color: dict, range_: dict, context_text: str) -> List[dict]:
    """Build ColorPresentation[] for textDocument/colorPresentation.

    Returns representations in order of likely preference, inferring the
    format the user is already using from *context_text* (the text currently
    in the requested range) so the best match is listed first.

    Formats returned:
      1. Hex (#rrggbb / #rrggbbaa)
      2. rgb() / rgba()
      3. hsl() / hsla()
      4. Integer tuple (r_int, g_int, b_int[, a_int])
      5. Named CSS color (only when exact match exists)
    """
    r = max(0.0, min(1.0, color.get("red",   0.0)))
    g = max(0.0, min(1.0, color.get("green", 0.0)))
    b = max(0.0, min(1.0, color.get("blue",  0.0)))
    a = max(0.0, min(1.0, color.get("alpha", 1.0)))

    ri = round(r * 255)
    gi = round(g * 255)
    bi = round(b * 255)
    ai = round(a * 255)

    has_alpha = a < 1.0

    # --- build all representations ---

    # Hex
    if has_alpha:
        hex_str = f"#{ri:02x}{gi:02x}{bi:02x}{ai:02x}"
    else:
        hex_str = f"#{ri:02x}{gi:02x}{bi:02x}"

    # rgb / rgba
    if has_alpha:
        rgb_str = f"rgba({ri}, {gi}, {bi}, {round(a, 3)})"
    else:
        rgb_str = f"rgb({ri}, {gi}, {bi})"

    # hsl / hsla
    h_deg, s_pct, l_pct = _rgb_to_hsl(r, g, b)
    if has_alpha:
        hsl_str = f"hsla({h_deg}, {s_pct}%, {l_pct}%, {round(a, 3)})"
    else:
        hsl_str = f"hsl({h_deg}, {s_pct}%, {l_pct}%)"

    # Integer tuple
    if has_alpha:
        tuple_str = f"({ri}, {gi}, {bi}, {ai})"
    else:
        tuple_str = f"({ri}, {gi}, {bi})"

    # Named color (exact RGB match, alpha must be 1.0)
    named: Optional[str] = None
    if not has_alpha:
        for name, rgba in _CSS_COLORS.items():
            if (
                abs(rgba[0] - r) < 0.002
                and abs(rgba[1] - g) < 0.002
                and abs(rgba[2] - b) < 0.002
            ):
                named = name
                break

    # Infer current format from context_text to sort best match first
    ctx = (context_text or "").strip().lower()
    if ctx.startswith("#"):
        order = [hex_str, rgb_str, hsl_str, tuple_str]
    elif ctx.startswith("rgb"):
        order = [rgb_str, hex_str, hsl_str, tuple_str]
    elif ctx.startswith("hsl"):
        order = [hsl_str, hex_str, rgb_str, tuple_str]
    elif ctx.startswith("("):
        order = [tuple_str, hex_str, rgb_str, hsl_str]
    elif named and ctx == named:
        order = [named, hex_str, rgb_str, hsl_str, tuple_str]
    else:
        order = [hex_str, rgb_str, hsl_str, tuple_str]

    if named and named not in order:
        order.append(named)

    def _make_presentation(label: str) -> dict:
        return {
            "label": label,
            "textEdit": {
                "range": range_,
                "newText": label,
            },
        }

    return [_make_presentation(label) for label in order if label]


# ---------------------------------------------------------------------------
# Code lens helpers
# ---------------------------------------------------------------------------


# (uri, source_hash) -> List[CodeLens dict]
_CL_CACHE: Dict[str, tuple] = {}
_CL_CACHE_LOCK = threading.Lock()


def _find_cross_file_subclasses(
    path: str,
    line1: int,
    col: int,
    workspace_root: Optional[str],
) -> List[Any]:
    """Return a list of AST ClassDef nodes from other files that directly
    inherit from the class defined at (line1, col) in *path*.

    Strategy: call ``jedi.Script.get_references()`` with the workspace project,
    then for each ref in a *different* file open the AST and check whether the
    ref position falls inside a ``ClassDef.bases`` list.

    Excluding *path* itself is critical: intra-file subclasses are already
    counted by the AST map in ``_get_code_lenses``; including them here would
    double-count.

    Returns an empty list on any error so callers degrade gracefully.
    """
    if _jedi is None or not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        project = _jedi.Project(path=workspace_root) if workspace_root else None
        script = _jedi.Script(code=source, path=path, project=project)
        refs = script.get_references(line=line1, column=col, include_builtins=False)

        nodes: List[Any] = []
        seen: set = set()
        _src_cache: Dict[str, str] = {}

        for ref in refs:
            if not ref.module_path:
                continue
            ref_path = str(ref.module_path)
            # Exclude own file - intra-file subclasses handled by AST map
            if ref_path == path:
                continue
            if _in_ignored_folder(ref_path, _DEFAULT_IGNORE_FOLDERS):
                continue

            ref_line = ref.line or 1
            try:
                if ref_path not in _src_cache:
                    with open(ref_path, encoding="utf-8", errors="replace") as fh:
                        _src_cache[ref_path] = fh.read()
                ref_src = _src_cache[ref_path]
                ref_tree = _ast.parse(ref_src)

                for node in _ast.walk(ref_tree):
                    if not isinstance(node, _ast.ClassDef):
                        continue
                    if not any(b.lineno == ref_line for b in node.bases):
                        continue
                    key = (ref_path, node.lineno)
                    if key in seen:
                        continue
                    seen.add(key)
                    nodes.append(node)
            except Exception:
                continue

        return nodes
    except Exception as exc:
        log.debug("pylsp_workspace_symbols: _find_cross_file_subclasses: %s", exc)
        return []


def _cl_cache_get(uri: str, source_hash: str) -> Optional[List[dict]]:
    with _CL_CACHE_LOCK:
        entry = _CL_CACHE.get(uri)
        if entry and entry[0] == source_hash:
            return entry[1]
    return None


def _cl_cache_set(uri: str, source_hash: str, lenses: List[dict]) -> None:
    with _CL_CACHE_LOCK:
        _CL_CACHE[uri] = (source_hash, lenses)


def _get_code_lenses(
    source: str,
    path: str,
    uri: str,
    settings: dict,
    workspace_root: Optional[str] = None,
) -> List[dict]:
    """Compute CodeLens[] for *source*.

    Strategy:
      1. Fast structural pass with ``ast`` to locate top-level and class-level
         definitions (functions, methods, classes) and special blocks.
         Also builds two maps for implementations counting (intra-file):
           - ``class_subclasses``: class_name -> count of direct subclasses
             defined in this file (used for the class-level lens).
           - ``method_overrides``: (class_name, method_name) -> count of
             subclasses in this file that define the same method (overrides).
      2. For each definition:
         a. Call ``jedi.Script.get_references()`` to count call sites
            (refs where ``is_definition() == False``).
         b. For classes/methods: count implementations/overrides from the
            intra-file AST map. If ``cross_file_implementations=True``,
            additionally call ``_count_cross_file_subtypes`` /
            ``_count_cross_file_overrides`` for cross-file counts.
      3. Emit lenses in order: references → implementations → run/test.

    Lens types produced:
      - ``👥 N references``     - every top-level/class function, method, class
      - ``🔗 N implementations`` - classes that have subclasses; methods that
                                   are overridden in at least one subclass
      - ``▶ Run``               - ``if __name__ == "__main__":`` block,
                                   command: ``pylsp_workspace_symbols.run_file``
      - ``🧪 Run test``         - ``def test_*`` functions and ``Test*`` classes,
                                   command: ``pylsp_workspace_symbols.run_test``
    """
    source_hash = str(hash(source))
    cached = _cl_cache_get(uri, source_hash)
    if cached is not None:
        return cached

    show_refs        = settings.get("show_references",          True)
    show_impls       = settings.get("show_implementations",     True)
    cross_file_impls = settings.get("cross_file_implementations", False)
    show_run         = settings.get("show_run",                 True)
    show_tests       = settings.get("show_tests",               True)
    max_defs         = settings.get("max_definitions",          150)

    lenses: List[dict] = []

    # -- structural pass ------------------------------------------------------
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return []

    # Collect definitions: (line_0based, col, name, kind, parent_class_name)
    # kind: "function" | "method" | "class" | "run" | "test_func" | "test_class"
    # parent_class_name: name of the enclosing ClassDef for methods, else None.
    defs: List[tuple] = []

    # Single ast.walk pass builds both maps at once.
    # class_bases:   child_class -> [base_name, ...]
    # class_methods: class_name -> {method_name, ...}
    class_bases: Dict[str, List[str]] = {}
    class_methods: Dict[str, set] = {}

    for top_node in _ast.walk(tree):
        if isinstance(top_node, _ast.ClassDef):
            class_bases[top_node.name] = [
                b for b in (
                    getattr(base, "id", getattr(base, "attr", ""))
                    for base in top_node.bases
                ) if b
            ]
            class_methods[top_node.name] = {
                child.name
                for child in top_node.body
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            }

    # Intra-file subclass count per class name.
    class_subclasses: Dict[str, int] = {}
    for bases in class_bases.values():
        for base in bases:
            class_subclasses[base] = class_subclasses.get(base, 0) + 1

    # method_overrides[(parent_class, method)] = count of subclasses that override it.
    method_overrides: Dict[tuple, int] = {}
    for child_class, bases in class_bases.items():
        for base in bases:
            child_meths = class_methods.get(child_class, set())
            for meth in child_meths:
                key = (base, meth)
                method_overrides[key] = method_overrides.get(key, 0) + 1

    # class_def_positions: class_name -> (line1_jedi, col) for cross-file override lookup.
    # Built from the same tree.body walk below so no extra pass needed.
    class_def_positions: Dict[str, tuple] = {}

    # -- collect defs with parent_class context ------------------------------
    for top_node in tree.body:
        if isinstance(top_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            keyword = "async def " if isinstance(top_node, _ast.AsyncFunctionDef) else "def "
            col  = top_node.col_offset + len(keyword)
            name = top_node.name
            kind = "test_func" if (name.startswith("test_") or name.startswith("Test")) else "function"
            defs.append((top_node.lineno - 1, col, name, kind, None))

        elif isinstance(top_node, _ast.ClassDef):
            col   = top_node.col_offset + len("class ")
            name  = top_node.name
            bases = [getattr(b, "id", getattr(b, "attr", "")) for b in top_node.bases]
            kind  = "test_class" if (name.startswith("Test") or "TestCase" in bases) else "class"
            defs.append((top_node.lineno - 1, col, name, kind, None))
            # Store Jedi-1-based (line, col) for cross-file override lookup
            class_def_positions[name] = (top_node.lineno, col)
            # Collect methods inside this class
            for child in top_node.body:
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    kw = "async def " if isinstance(child, _ast.AsyncFunctionDef) else "def "
                    mcol  = child.col_offset + len(kw)
                    mname = child.name
                    if mname.startswith("test_") or mname.startswith("Test"):
                        mkind = "test_func"
                    else:
                        mkind = "method"
                    defs.append((child.lineno - 1, mcol, mname, mkind, name))

        elif isinstance(top_node, _ast.If):
            test = top_node.test
            is_main = (
                isinstance(test, _ast.Compare)
                and isinstance(test.left, _ast.Name)
                and test.left.id == "__name__"
                and any(
                    isinstance(c, _ast.Constant) and c.value == "__main__"
                    for c in test.comparators
                )
            )
            if is_main:
                defs.append((top_node.lineno - 1, 0, "__main__", "run", None))

    # Cap to avoid excessive Jedi calls on very large files
    defs = defs[:max_defs]

    # -- Jedi script (shared for all symbols) --------------------------------
    jedi_script = None
    if _jedi is not None and (show_refs or show_impls):
        try:
            disk_source = source
            if path:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        disk_source = fh.read()
                except OSError:
                    pass
            project = _jedi.Project(path=workspace_root) if workspace_root else None
            jedi_script = _jedi.Script(code=disk_source, path=path, project=project)
        except Exception as exc:
            log.exception("pylsp_workspace_symbols: codeLens jedi.Script failed: %s", exc)

    # -- pre-compute inheritance usage positions ------------------------------
    # Maps (line_1based, col) -> base_name for every base-class usage in this
    # file. Used to exclude those positions from the 👥 references count.
    # NOTE: only covers intra-file subclasses. Cross-file inheritance positions
    # are computed lazily per-class during the emit loop via
    # _find_cross_file_subclasses, and stored in _cf_subclass_cache so the
    # I/O is shared with the 🔗 implementations lens.
    inheritance_positions: Dict[tuple, str] = {}
    for top_node in tree.body:
        if isinstance(top_node, _ast.ClassDef):
            for base in top_node.bases:
                base_name = getattr(base, "id", getattr(base, "attr", ""))
                if base_name and hasattr(base, "col_offset"):
                    inheritance_positions[(top_node.lineno, base.col_offset)] = base_name

    # Cache cross-file subclass nodes per class name to avoid calling
    # _find_cross_file_subclasses twice (once for references filter, once for
    # implementations count). Populated lazily in the emit loop below.
    _cf_subclass_cache: Dict[str, list] = {}

    # -- emit lenses ----------------------------------------------------------
    for line0, col, name, kind, parent_class in defs:
        line1 = line0 + 1  # Jedi is 1-based

        lens_range = {
            "start": {"line": line0, "character": col},
            "end":   {"line": line0, "character": col + len(name)},
        }

        # -- fetch Jedi refs once per symbol ----------------------------------
        jedi_refs: Optional[list] = None
        if jedi_script is not None and kind not in ("run",):
            try:
                jedi_refs = jedi_script.get_references(line1, col, include_builtins=False)
            except Exception as exc:
                log.debug(
                    "pylsp_workspace_symbols: codeLens get_references failed for %s:%s: %s",
                    name, line1, exc,
                )

        # -- 👥 reference lens ------------------------------------------------
        if show_refs and kind not in ("run",):
            label = "👥 ? references"
            if jedi_refs is not None:
                non_def_refs = [r for r in jedi_refs if not r.is_definition()]
                if kind in ("class", "test_class"):
                    # Exclude intra-file inheritance positions (always).
                    # Exclude cross-file inheritance positions only when
                    # cross_file_impls=True - the subclass list is already
                    # cached by the implementations block below, or fetched
                    # here and stored so the implementations block reuses it.
                    cf_inherit_pos: set = set()
                    if cross_file_impls and workspace_root:
                        if name not in _cf_subclass_cache:
                            _cf_subclass_cache[name] = _find_cross_file_subclasses(
                                path, line1, col, workspace_root
                            )
                        for cf_node in _cf_subclass_cache[name]:
                            for base in cf_node.bases:
                                base_id = getattr(base, "id", getattr(base, "attr", ""))
                                if base_id == name and hasattr(base, "col_offset"):
                                    cf_inherit_pos.add((cf_node.lineno, base.col_offset))
                    call_sites = [
                        r for r in non_def_refs
                        if (r.line, r.column) not in inheritance_positions
                        and (r.line, r.column) not in cf_inherit_pos
                    ]
                else:
                    call_sites = non_def_refs
                n = len(call_sites)
                label = f"👥 {n} reference{'s' if n != 1 else ''}"
            lenses.append({
                "range": lens_range,
                "command": {"title": label, "command": "", "arguments": []},
            })

        # -- 🔗 implementations lens ------------------------------------------
        # Intra-file count from AST maps (fast, no I/O).
        # Cross-file count via _cf_subclass_cache (shared with refs block).
        if show_impls and kind in ("class", "test_class", "method"):
            if kind in ("class", "test_class"):
                n_impl = class_subclasses.get(name, 0)
                if cross_file_impls and workspace_root:
                    if name not in _cf_subclass_cache:
                        _cf_subclass_cache[name] = _find_cross_file_subclasses(
                            path, line1, col, workspace_root
                        )
                    n_impl += len(_cf_subclass_cache[name])
            else:
                n_impl = method_overrides.get((parent_class, name), 0) if parent_class else 0
                if cross_file_impls and workspace_root and parent_class:
                    pc_pos = class_def_positions.get(parent_class)
                    if pc_pos:
                        if parent_class not in _cf_subclass_cache:
                            _cf_subclass_cache[parent_class] = _find_cross_file_subclasses(
                                path, pc_pos[0], pc_pos[1], workspace_root
                            )
                        n_impl += sum(
                            1 for node in _cf_subclass_cache[parent_class]
                            for child in node.body
                            if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                            and child.name == name
                        )

            if n_impl > 0:
                impl_label = f"🔗 {n_impl} implementation{'s' if n_impl != 1 else ''}"
                lenses.append({
                    "range": lens_range,
                    "command": {"title": impl_label, "command": "", "arguments": []},
                })

        # -- ▶ Run lens -------------------------------------------------------
        if show_run and kind == "run":
            lenses.append({
                "range": lens_range,
                "command": {
                    "title": "▶ Run",
                    "command": "pylsp_workspace_symbols.run_file",
                    "arguments": [{"path": path}],
                },
            })

        # -- 🧪 Run test lens -------------------------------------------------
        if show_tests and kind in ("test_func", "test_class"):
            lenses.append({
                "range": lens_range,
                "command": {
                    "title": "🧪 Run test",
                    "command": "pylsp_workspace_symbols.run_test",
                    "arguments": [{
                        "path": path,
                        "name": name,
                        "kind": "class" if kind == "test_class" else "function",
                    }],
                },
            })

    log.info(
        "pylsp_workspace_symbols: codeLens computed %d lens(es) for %s",
        len(lenses), uri,
    )
    _cl_cache_set(uri, source_hash, lenses)
    return lenses


def _find_triple_string_spans(source: str) -> List[tuple]:
    """Return list of (start, end) char offsets for every triple-quoted string
    in *source*.  Used to decide whether a bare ``#`` is a Python comment or
    string content.
    """
    spans: List[tuple] = []
    i = 0
    n = len(source)
    while i < n:
        # Find the next triple-quote opener (""" or ''')
        dq = source.find('"""', i)
        sq = source.find("'''", i)
        if dq == -1 and sq == -1:
            break
        # Pick the closer one; prefer """ on tie
        if sq == -1 or (dq != -1 and dq <= sq):
            delim = '"""'
            start = dq
        else:
            delim = "'''"
            start = sq
        # Find the matching closer after the opener
        close = source.find(delim, start + 3)
        if close == -1:
            # Unterminated - treat as running to end of file
            spans.append((start, n))
            break
        spans.append((start, close + 3))
        i = close + 3
    return spans


def _strip_inline_comment(line: str, line_start_offset: int,
                           triple_spans: List[tuple]) -> str:
    """Return the portion of *line* before the first bare Python ``#`` comment.

    ``line_start_offset`` is the character offset of the first character of
    *line* within the whole source file.  ``triple_spans`` is the list
    returned by :func:`_find_triple_string_spans`.

    A ``#`` that falls inside any triple-quoted string span is **not** treated
    as a comment delimiter.  Single- and double-quote context within the line
    is still tracked for the common case of quoted hex colors.

    Performance: the triple-span membership test is hoisted out of the
    per-character inner loop.  Instead of calling ``any(...)`` over all spans
    for every character (O(len * spans)), we check once whether the line
    overlaps any triple-quoted span at all, and if so mark which character
    offsets on this line are shielded.  For the common case of no overlap the
    fast path skips all span work entirely.
    """
    line_end = line_start_offset + len(line)

    # Fast path: line does not touch any triple-quoted span at all.
    # This is true for the vast majority of lines in normal source files.
    line_in_triple = any(ts < line_end and te > line_start_offset
                         for ts, te in triple_spans)

    in_single = False
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]

        if line_in_triple:
            abs_pos = line_start_offset + i
            if any(ts <= abs_pos < te for ts, te in triple_spans):
                i += 1
                continue

        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
        i += 1
    return line


def _collect_document_colors(source: str) -> List[dict]:
    """textDocument/documentColor - find color values in Python source.

    Detects:
      * Hex literals (quoted):   ``"#rgb"``, ``"#rrggbb"``, ``"#rrggbbaa"``
      * Hex literals (bare):     ``#rrggbb`` without quotes (CSS inside triple-strings)
      * CSS functions:           ``rgb()``, ``rgba()``, ``hsl()``, ``hsla()``
      * CSS named colors:        ``"red"``, ``"cornflowerblue"``, ...
      * Integer tuples near color-hinting variable names: ``(255, 0, 0)``
      * Float tuples  near color-hinting variable names: ``(1.0, 0.0, 0.0)``
      * pygame.Color integer tuples (any line with pygame + Color)
    """
    results: List[dict] = []
    lines = source.splitlines(keepends=True)

    # Pre-compute triple-quoted string spans once for the whole file so that
    # _strip_inline_comment can correctly distinguish # in CSS content from
    # Python inline comments.
    triple_spans = _find_triple_string_spans(source)

    # Track (line, col_start, col_end) already emitted - prevents duplicates
    # when multiple passes produce overlapping spans for the same color.
    _emitted: set = set()

    def _emit(line_0: int, col_start: int, col_end: int, rgba: tuple) -> None:
        key = (line_0, col_start, col_end)
        if key in _emitted:
            return
        _emitted.add(key)
        r, g, b, a = rgba
        results.append({
            "range": {
                "start": {"line": line_0, "character": col_start},
                "end":   {"line": line_0, "character": col_end},
            },
            "color": {"red": r, "green": g, "blue": b, "alpha": a},
        })

    offset = 0  # running char offset into source
    for line_idx, line_text_raw in enumerate(lines):
        line_text = line_text_raw.rstrip("\r\n")
        line_start = offset
        offset += len(line_text_raw)

        stripped = line_text.strip()

        # Skip pure comment lines and blank lines early.
        if not stripped or stripped.startswith("#"):
            continue

        # Strip the inline comment portion for pattern matching.
        code_part = _strip_inline_comment(line_text, line_start, triple_spans)

        # ------------------------------------------------------------------ #
        # 1a. Hex color literals inside quotes: "#rgb", '#rrggbb', "#rrggbbaa"
        # ------------------------------------------------------------------ #
        for m in _HEX_QUOTED_RE.finditer(code_part):
            hex_val = m.group(1)
            rgba = _hex_to_rgba(hex_val)
            if rgba:
                col = m.start(1)  # position of '#'
                _emit(line_idx, col, col + len(hex_val), rgba)

        # ------------------------------------------------------------------ #
        # 1b. Bare hex tokens (no quotes) - for CSS/HTML inside triple-strings
        #     and similar unquoted contexts (e.g. CSS custom properties).
        #     Only emit when the '#' char itself sits inside a triple-string
        #     span, preventing false positives on Python identifiers/comments.
        # ------------------------------------------------------------------ #
        for m in _HEX_BARE_RE.finditer(code_part):
            abs_hash = line_start + m.start(1)
            if any(ts <= abs_hash < te for ts, te in triple_spans):
                hex_val = m.group(1)
                rgba = _hex_to_rgba(hex_val)
                if rgba:
                    col = m.start(1)
                    _emit(line_idx, col, col + len(hex_val), rgba)

        # ------------------------------------------------------------------ #
        # 2. CSS functional notation: rgb(), rgba(), hsl(), hsla()
        #    Detected unconditionally - they are unambiguous.
        #    Record matched spans so tuple passes below can skip them.
        # ------------------------------------------------------------------ #
        func_spans: List[tuple] = []  # (start, end) col pairs of functional matches

        for m in _RGB_FUNC_RE.finditer(code_part):
            try:
                r = _parse_css_value(m.group(1))
                g = _parse_css_value(m.group(2))
                b = _parse_css_value(m.group(3))
                a = _parse_css_alpha(m.group(4))
                _emit(line_idx, m.start(), m.end(), (r, g, b, a))
                func_spans.append((m.start(), m.end()))
            except (ValueError, AttributeError):
                pass

        for m in _HSL_FUNC_RE.finditer(code_part):
            try:
                h_deg = float(m.group(1).rstrip("%"))
                s_pct = float(m.group(2).rstrip("%"))
                l_pct = float(m.group(3).rstrip("%"))
                a = _parse_css_alpha(m.group(4))
                r, g, b = _hsl_to_rgb(h_deg, s_pct, l_pct)
                _emit(line_idx, m.start(), m.end(), (r, g, b, a))
                func_spans.append((m.start(), m.end()))
            except (ValueError, AttributeError):
                pass

        def _inside_func_span(pos: int) -> bool:
            """Return True if *pos* falls within any already-matched CSS function span."""
            return any(fs <= pos < fe for fs, fe in func_spans)

        # ------------------------------------------------------------------ #
        # 3. CSS named colors inside quotes: "red", 'cornflowerblue', ...
        #    Detected unconditionally - quoted color names are explicit.
        # ------------------------------------------------------------------ #
        for m in _NAMED_COLOR_RE.finditer(code_part):
            name = m.group(1).lower()
            rgba = _CSS_COLORS.get(name)
            if rgba:
                col = m.start() + 1  # skip the opening quote
                _emit(line_idx, col, col + len(name), rgba)

        # ------------------------------------------------------------------ #
        # 4. pygame.Color integer tuples (unconditional when pygame present)
        # ------------------------------------------------------------------ #
        if "pygame" in code_part and "Color" in code_part:
            for m in _TUPLE_RGB_RE.finditer(code_part):
                if _inside_func_span(m.start()):
                    continue
                try:
                    r_i = int(m.group(1))
                    g_i = int(m.group(2))
                    b_i = int(m.group(3))
                    a_raw = m.group(4)
                    a_i = int(a_raw) if a_raw is not None else 255
                    if all(0 <= v <= 255 for v in (r_i, g_i, b_i, a_i)):
                        _emit(line_idx, m.start(), m.end(),
                              (r_i / 255.0, g_i / 255.0, b_i / 255.0, a_i / 255.0))
                except ValueError:
                    pass

        # ------------------------------------------------------------------ #
        # 5 & 6. Integer and float tuples gated by color-hinting variable name
        #         on the same line.  Skip spans already covered by rgb()/hsl().
        # ------------------------------------------------------------------ #
        if _COLOR_VAR_RE.search(code_part):
            # Integer RGB(A): (255, 0, 0) / (255, 0, 0, 128)
            for m in _TUPLE_RGB_RE.finditer(code_part):
                if _inside_func_span(m.start()):
                    continue
                try:
                    r_i = int(m.group(1))
                    g_i = int(m.group(2))
                    b_i = int(m.group(3))
                    a_raw = m.group(4)
                    a_i = int(a_raw) if a_raw is not None else 255
                    if all(0 <= v <= 255 for v in (r_i, g_i, b_i, a_i)):
                        _emit(line_idx, m.start(), m.end(),
                              (r_i / 255.0, g_i / 255.0, b_i / 255.0, a_i / 255.0))
                except ValueError:
                    pass

            # Float RGB(A): (1.0, 0.0, 0.0) / (1.0, 0.0, 0.0, 0.5)
            for m in _TUPLE_FLOAT_RE.finditer(code_part):
                if _inside_func_span(m.start()):
                    continue
                try:
                    r_f = float(m.group(1))
                    g_f = float(m.group(2))
                    b_f = float(m.group(3))
                    a_f = float(m.group(4)) if m.group(4) is not None else 1.0
                    if all(0.0 <= v <= 1.0 for v in (r_f, g_f, b_f, a_f)):
                        _emit(line_idx, m.start(), m.end(), (r_f, g_f, b_f, a_f))
                except ValueError:
                    pass

    return results
