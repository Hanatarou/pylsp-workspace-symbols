"""pylsp-workspace-symbols: workspace/symbol support and inlay hints for python-lsp-server via Jedi.

Strategy: pylsp's hookspecs.py does not define hookspecs for workspace symbols or
inlay hints as proper capabilities, so this plugin uses a two-pronged approach:

  1. Capability injection (preferred): at import time, monkey-patch
     PythonLSPServer.capabilities() to insert workspaceSymbolProvider and
     inlayHintProvider directly into the proper capabilities dict.
     This makes the plugin work out-of-the-box with clients that require
     proper capabilities (eglot, Neovim, etc.).

  2. Fallback via pylsp_experimental_capabilities: if the injection fails
     (e.g. pylsp changed its internal API), the capabilities are announced
     via the experimental channel instead. Clients that honour experimental
     capabilities (CudaText, VSCode with pylsp, etc.) will still work.

  3. Register a custom JSON-RPC dispatcher via pylsp_dispatchers that intercepts
     the "workspace/symbol" and "textDocument/inlayHint" methods and calls
     our Jedi-backed implementations.
"""
from __future__ import annotations
import logging
import re
import threading
from dataclasses import dataclass
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


# Prevents re-creating Script objects for the same file path
_JEDI_CACHE: Dict[str, _jedi.Script] = {}
_CACHE_LOCK = threading.Lock()


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
        }
    }


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
    return caps


@hookimpl
def pylsp_dispatchers(config, workspace) -> dict:
    """Register handlers for both methods."""
    settings_ws = config.plugin_settings("jedi_workspace_symbols")
    settings_ih = config.plugin_settings("inlay_hints")

    dispatch: Dict[str, Any] = {}

    # Workspace symbols (your original handler)
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
                log.error("pylsp_workspace_symbols: failed for %s: %s", uri, e)
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
                log.error("pylsp_workspace_symbols: prepareCallHierarchy: %s", exc)
                return None

        def _incoming_calls(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _call_hierarchy_incoming(params.get("item", {}), workspace)
            except Exception as exc:
                log.error("pylsp_workspace_symbols: incomingCalls: %s", exc)
                return None

        def _outgoing_calls(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _call_hierarchy_outgoing(params.get("item", {}), workspace)
            except Exception as exc:
                log.error("pylsp_workspace_symbols: outgoingCalls: %s", exc)
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
                log.error("pylsp_workspace_symbols: prepareTypeHierarchy: %s", exc)
                return None

        def _supertypes(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _type_hierarchy_supertypes(params.get("item", {}))
            except Exception as exc:
                log.error("pylsp_workspace_symbols: supertypes: %s", exc)
                return None

        def _subtypes(params) -> Optional[List[dict]]:
            if not isinstance(params, dict):
                return None
            try:
                return _type_hierarchy_subtypes(params.get("item", {}), workspace)
            except Exception as exc:
                log.error("pylsp_workspace_symbols: subtypes: %s", exc)
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
                log.error("pylsp_workspace_symbols: documentLink: %s", exc)
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
                log.error("pylsp_workspace_symbols: documentColor: %s", exc)
                return []

        dispatch["textDocument/documentColor"] = _document_color

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


def _search_symbols(settings: dict, workspace, query: str) -> Optional[List[dict]]:
    """Core Jedi-backed implementation of workspace/symbol.

    Always uses project.complete_search("") to enumerate all symbols, then
    filters client-side by case-insensitive substring match on `query`.
    This is necessary because project.search(query) in older bundled Jedi
    performs exact name matching and misses partial matches (e.g. 'area'
    won't find 'calculate_area').
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

    try:
        project = _jedi.Project(path=workspace.root_path)
        # Always use complete_search("") to get all names, then filter
        # client-side. project.search(query) in older bundled Jedi performs
        # exact name matching, so 'area' would never find 'calculate_area'.
        # Note: project.search("") returns nothing - hence complete_search.
        names = project.complete_search("")
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

            if _in_ignored_folder(str(module_path), ignore_folders):
                continue

            uri = uris.from_fs_path(str(module_path))
            line = max(0, (name.line or 1) - 1)   # Jedi 1-based -> LSP 0-based
            col = max(0, (name.column or 0))

            results.append({
                "name": name.name,
                "kind": _SYMBOL_KIND.get(name.type, _DEFAULT_KIND),
                "location": {
                    "uri": uri,
                    "range": {
                        "start": {"line": line, "character": col},
                        "end": {"line": line, "character": col + len(name.name)},
                    },
                },
                "containerName": name.module_name,
            })
        except Exception:
            log.debug("pylsp_workspace_symbols: skipping %r", name, exc_info=True)
            continue

    return results


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
        # Create or get cached Jedi Script
        with _CACHE_LOCK:
            if path in _JEDI_CACHE:
                script = _JEDI_CACHE[path]
                # Update script with new source if needed (Jedi handles this internally)
            else:
                script = _jedi.Script(code=source_code, path=path)
                _JEDI_CACHE[path] = script

        # Use Jedi-based hint collection
        hints = _collect_jedi_hints(script, source_code, settings)
        
        # Apply max_hints limit
        max_hints = settings.get("max_hints_per_file", 200)
        if max_hints > 0 and len(hints) > max_hints:
            hints = hints[:max_hints]
            
        return [hint.to_hint() for hint in hints if hint.to_hint()]
        
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
                rhs_start = len(line) - len(line.lstrip()) + len(target) + 3
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
    """Clears the Jedi Script cache when the document is closed to avoid stale data."""
    with _CACHE_LOCK:
        _JEDI_CACHE.pop(document.path, None)


@hookimpl
def pylsp_document_did_save(config, workspace, document):
    """Clears the cache on save to ensure up-to-date types."""
    with _CACHE_LOCK:
        _JEDI_CACHE.pop(document.path, None)


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
    import ast as _ast

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

        for ref in refs:
            if not ref.module_path:
                continue
            if _in_ignored_folder(str(ref.module_path), _DEFAULT_IGNORE_FOLDERS):
                continue
            # Skip the definition line itself
            if str(ref.module_path) == path and ref.line == item_line:
                continue
            # Skip import statements - they are not call sites.
            # ref.type is unreliable for this; use AST on the source line instead.
            try:
                import ast as _ast_chk
                ref_src_lines = open(str(ref.module_path), encoding="utf-8", errors="replace").read().splitlines()
                ref_src_line = ref_src_lines[(ref.line or 1) - 1] if ref.line else ""
                _node = _ast_chk.parse(ref_src_line.strip(), mode="single")
                if any(isinstance(n, (_ast_chk.Import, _ast_chk.ImportFrom))
                       for n in _ast_chk.walk(_node)):
                    continue
            except Exception:
                pass

            ref_path = str(ref.module_path)
            ref_line = ref.line or 1
            ref_col = ref.column or 0
            key = (ref_path, ref_line, ref_col)
            if key in seen:
                continue
            seen.add(key)

            # Resolve the enclosing caller
            try:
                with open(ref_path, encoding="utf-8", errors="replace") as fh:
                    ref_source = fh.read()
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
    import ast as _ast

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
    import ast as _ast

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
                with open(ref_path, encoding="utf-8", errors="replace") as fh:
                    ref_src = fh.read()
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

                    # Resolve the subclass name via Jedi for rich data
                    try:
                        rs = _jedi.Script(code=ref_src, path=ref_path)
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
    import ast as _ast
    import os
    import re

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
    """
    in_single = False
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        abs_pos = line_start_offset + i

        # If this character is inside a triple-quoted string, it cannot be
        # a comment delimiter - consume without toggling quote state.
        in_triple = any(ts <= abs_pos < te for ts, te in triple_spans)
        if in_triple:
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
