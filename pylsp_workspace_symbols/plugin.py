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
