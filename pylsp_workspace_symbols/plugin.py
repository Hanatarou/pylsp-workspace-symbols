"""pylsp-workspace-symbols: workspace/symbol support for python-lsp-server via Jedi.

Strategy: pylsp's hookspecs.py does not define a pylsp_workspace_symbols hookspec,
so we cannot add the capability via the normal hook mechanism.  Instead we:

  1. Announce the capability via pylsp_experimental_capabilities (so the client
     sees workspaceSymbolProvider: True in the server capabilities).
  2. Register a custom JSON-RPC dispatcher via pylsp_dispatchers that intercepts
     the "workspace/symbol" method and calls our Jedi-backed implementation.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pylsp import hookimpl, uris

try:
    import jedi as _jedi
except ImportError:  # pragma: no cover
    _jedi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

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


@hookimpl
def pylsp_settings(config) -> dict:
    """Declare default configuration for this plugin."""
    return {
        "plugins": {
            "jedi_workspace_symbols": {
                "enabled": True,
                "max_symbols": 500,
                "ignore_folders": [],
            }
        }
    }


@hookimpl
def pylsp_experimental_capabilities(config, workspace) -> dict:
    """Advertise workspaceSymbolProvider capability to the client."""
    settings = config.plugin_settings("jedi_workspace_symbols")
    if not settings.get("enabled", True):
        return {}
    # pylsp merges this dict into the top-level ServerCapabilities sent during
    # the initialize handshake.
    return {"workspaceSymbolProvider": True}


@hookimpl
def pylsp_dispatchers(config, workspace) -> dict:
    """Register a handler for the workspace/symbol JSON-RPC method."""
    settings = config.plugin_settings("jedi_workspace_symbols")
    if not settings.get("enabled", True):
        return {}

    def _workspace_symbol(params) -> Optional[List[dict]]:
        # pylsp_jsonrpc calls handlers with the raw params dict as a single
        # positional argument: handler({"query": "foo"})
        query = params.get("query", "") if isinstance(params, dict) else ""
        return _search_symbols(settings, workspace, query)

    return {"workspace/symbol": _workspace_symbol}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _in_ignored_folder(path: str, ignore_folders: set) -> bool:
    """Return True if *path* passes through any of the ignored folder names."""
    normalized = path.replace("\\", "/")
    return any(
        f"/{folder}/" in normalized or normalized.endswith(f"/{folder}")
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
