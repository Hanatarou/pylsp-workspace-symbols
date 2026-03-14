# pylsp-workspace-symbols

[![CI](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml/badge.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![Downloads](https://img.shields.io/pypi/dm/pylsp-workspace-symbols)](https://pypistats.org/packages/pylsp-workspace-symbols)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/blob/main/LICENSE)

A [python-lsp-server](https://github.com/python-lsp/python-lsp-server) plugin that adds **workspace symbol search**, **inlay hints**, **call/type hierarchy**, **document links** and **document colors** via [Jedi](https://github.com/davidhalter/jedi).

> **Why?** `pylsp` does not implement several LSP features natively. This plugin fills those gaps — enabling "Go to Symbol in Workspace", rich type inference hints, call/type hierarchy navigation, clickable import links, and inline color previews in any LSP client — including [CudaText](https://cudatext.github.io/), Neovim, Emacs, and others — with broad client compatibility out of the box.

---

## ✨ Features

- 🔍 **Workspace-wide symbol search** — find functions, classes, and modules across all files in the project
- 💡 **Inlay hints** — inline type annotations inferred by Jedi for assignments, return types, raised exceptions, and parameter names at call sites
- 🌳 **Call hierarchy** — navigate callers and callees of any function via `callHierarchy/incomingCalls` and `callHierarchy/outgoingCalls`
- 🧬 **Type hierarchy** — explore supertypes and subtypes of any class via `typeHierarchy/supertypes` and `typeHierarchy/subtypes`
- 🔗 **Document links** — clickable links for URLs in comments/strings and import statements (resolves to stdlib source when available)
- 🎨 **Document colors** — inline color previews for CSS/hex/RGB/HSL color literals in source files
- 🔌 **Broad client compatibility** — capabilities announced via proper LSP channel (works with Neovim, eglot, and any client that does not support experimental capabilities), with automatic fallback to the experimental channel
- ⚡ **Fast** — results in ~130ms after the first call (Jedi cache warm)
- 🔤 **Case-insensitive substring match** — `area` finds `calculate_area`, `Cal` finds `Calculator`
- 📁 **Smart folder exclusion** — automatically skips `.git`, `__pycache__`, `node_modules`, `.venv`, `dist`, `build`, and more
- ⚙️ **Configurable** — tune all options via pylsp settings
- 🐍 **Python 3.8+** — compatible with all modern Python versions

## 📦 Installation

```bash
pip install pylsp-workspace-symbols
```

The plugin is discovered automatically by `pylsp` via its entry point — no manual configuration needed.

## ⚙️ Configuration

Add to your LSP client's `pylsp` settings (e.g. in `settings.json` or equivalent):

```json
{
  "pylsp": {
    "plugins": {
      "jedi_workspace_symbols": {
        "enabled": true,
        "max_symbols": 500,
        "ignore_folders": []
      },
      "inlay_hints": {
        "enabled": true,
        "show_assign_types": true,
        "show_return_types": true,
        "show_raises": true,
        "show_parameter_hints": true,
        "max_hints_per_file": 200
      },
      "call_hierarchy": {
        "enabled": true
      },
      "type_hierarchy": {
        "enabled": true
      },
      "document_links": {
        "enabled": true
      },
      "document_colors": {
        "enabled": true
      }
    }
  }
}
```

### Workspace symbol options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable workspace symbol search |
| `max_symbols` | int | `500` | Maximum symbols returned. `0` means no limit |
| `ignore_folders` | list | `[]` | Extra folder names to skip (merged with built-in list) |

### Inlay hint options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable all inlay hints |
| `show_assign_types` | bool | `true` | Show inferred types for unannotated assignments (`x = 42` → `: int`) |
| `show_return_types` | bool | `true` | Show inferred return types for unannotated functions (`def f():` → `-> str`) |
| `show_raises` | bool | `true` | Show raised exception types (`raise ValueError(...)` → `Raises: ValueError`) |
| `show_parameter_hints` | bool | `true` | Show parameter names at call sites (`f(1, 2)` → `a=1, b=2`) |
| `max_hints_per_file` | int | `200` | Maximum hints per file. `0` means no limit |

### Call hierarchy options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable call hierarchy (`callHierarchy/incomingCalls`, `callHierarchy/outgoingCalls`) |

### Type hierarchy options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable type hierarchy (`typeHierarchy/supertypes`, `typeHierarchy/subtypes`) |

### Document links options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable document links (URLs in comments/strings and import resolution) |

### Document colors options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable document color previews (hex, RGB, HSL, CSS named colors) |

### Built-in ignored folders

`.git`, `.hg`, `.svn`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`,
`node_modules`, `.venv`, `venv`, `.env`, `env`, `dist`, `build`, `.eggs`, `egg-info`

## 🚀 Usage

### Workspace symbol search

Once installed, your LSP client will receive `workspaceSymbolProvider: true` in the server capabilities.
Use your client's "Go to Symbol in Workspace" command (typically `Ctrl+T` or `#` in the symbol picker).

### Inlay hints

Your LSP client will receive `inlayHintProvider: true` in the server capabilities. Hints are
rendered inline by the client automatically. The following hint types are supported:

- **Assignment hints** — unannotated variable assignments, including `self.attr` in `__init__`
- **Return hints** — unannotated `def` and `async def` functions, inferred from the first `return` statement
- **Raise hints** — `raise ExceptionType(...)` statements
- **Parameter hints** — positional argument names at call sites (keyword args are skipped as self-documenting)

Inlay hints respect type annotations already present in the source — annotated functions and
variables are never hinted twice.

### Call hierarchy

Your LSP client will receive `callHierarchyProvider: true`. Place the cursor on any function name
and invoke "Show Call Hierarchy" to see incoming callers and outgoing callees. Compatible with
any LSP client that supports the standard `callHierarchy/*` requests (Neovim, eglot, CudaText, etc.).

### Type hierarchy

Your LSP client will receive `typeHierarchyProvider: true`. Place the cursor on any class name
and invoke "Show Type Hierarchy" to explore supertypes and subtypes. Compatible with
any LSP client that supports the standard `typeHierarchy/*` requests (Neovim, eglot, CudaText, etc.).

### Document links

Your LSP client will receive `documentLinkProvider: true`. The following are turned into clickable links:

- **URLs** — `http://` and `https://` links in comments and strings
- **Import statements** — resolved to the corresponding `.py` source file in the system Python's `Lib/` directory (when Python is installed and available on PATH); modules without a `.py` source (C extensions, frozen modules, embedded-only `.pyc`) are silently skipped
- **Workspace path literals** — relative path strings (e.g. `"./config.json"`, `"../data/file.csv"`) and `open()`/`Path()` calls that reference files present in the workspace

### Document colors

Your LSP client will receive `colorProvider: true`. Inline color swatches are shown for:
hex (`#rgb`, `#rrggbb`, `#rrggbbaa`), `rgb()`/`rgba()`, `hsl()`/`hsla()`, and CSS named colors.

## 🔍 How it works

### Workspace symbols

`pylsp` does not define a `pylsp_workspace_symbols` hookspec, so this plugin uses a two-pronged approach:

1. **Capability injection (preferred)** — at import time, monkey-patches `PythonLSPServer.capabilities()` to insert `workspaceSymbolProvider: true` and `inlayHintProvider` directly into the proper LSP capabilities dict. This makes the plugin work out-of-the-box with clients that require proper capabilities, such as Neovim and eglot.
2. **Experimental fallback** — if the injection fails (e.g. pylsp changes its internal API), capabilities are announced via `pylsp_experimental_capabilities` instead. Clients that honour the experimental channel (CudaText, VSCode with pylsp, etc.) will still work.
3. **`pylsp_dispatchers`** — registers a custom JSON-RPC handler for `workspace/symbol` that calls Jedi's `project.complete_search()` and filters results client-side by case-insensitive substring match.

> **Note:** `workspace/symbol` returns module-level definitions (functions, classes, modules).
> Local variables inside functions are not indexed — this is standard LSP behaviour,
> consistent with pyright and other Python language servers.

### Inlay hints

The plugin handles the `textDocument/inlayHint` request using a hybrid approach:

1. **Regex scan** — fast pass over the source to locate `def`, assignment, `raise`, and call patterns.
2. **`_literal_type` fast-path** — resolves common literals (`"str"`, `42`, `True`, `[...]`, etc.) without calling Jedi.
3. **Jedi inference** — for non-literal expressions, `script.infer()` and `script.get_signatures()` are used to resolve types.
4. **Signature fallback** — for `self.attr = param` assignments, the enclosing `def` signature is inspected for type annotations or default values.

### Call hierarchy

Handled via `callHierarchy/incomingCalls` and `callHierarchy/outgoingCalls` dispatchers. Uses Jedi's `script.goto()` and `script.get_references()` to resolve callers and callees, building LSP-compliant `CallHierarchyItem` structures with correct range information.

### Type hierarchy

Handled via `typeHierarchy/supertypes` and `typeHierarchy/subtypes` dispatchers. Uses Jedi's `script.goto()` and static class inheritance analysis to build the type tree.

### Document links

Three-pass collection over the source:

1. **URL pass** — regex scan for `http://` and `https://` URLs in comments, docstrings, and string literals.
2. **Import pass** — AST parse to find all `import` and `from ... import` statements; resolves each module name to a `.py` file by querying the system Python's `sys.prefix` via a single cached subprocess call.
3. **Path literal pass** — detects relative path strings, `open()` calls and `Path()` calls whose argument resolves to an existing file inside the workspace root.

Modules without a `.py` source (C extensions, frozen modules, `.pyc`-only embedded builds) are silently skipped.

### Document colors

Regex-based scan over the source for color literals: hex (`#rgb`, `#rrggbb`, `#rrggbbaa`), `rgb()`/`rgba()`, `hsl()`/`hsla()`, and the full set of CSS named colors. Each match is returned as an LSP `ColorInformation` with normalised `[0.0, 1.0]` RGBA components.

## 🧪 Tests

```bash
pip install -e ".[dev]"
pytest
```

## 🤝 Contributing

Issues and pull requests are welcome!
Please open an issue before submitting a large change.

## 📚 References

- [python-lsp-server](https://github.com/python-lsp/python-lsp-server)
- [Jedi](https://github.com/davidhalter/jedi)
- [LSP workspace/symbol specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspace_symbol)
- [LSP inlay hints specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_inlayHint)
- [LSP call hierarchy specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_prepareCallHierarchy)
- [LSP type hierarchy specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_prepareTypeHierarchy)
- [LSP document links specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentLink)
- [LSP document colors specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentColor)

## 👤 Author

Bruno Eduardo — [github.com/Hanatarou](https://github.com/Hanatarou)

## 📄 License

MIT
