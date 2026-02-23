# pylsp-workspace-symbols

[![CI](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml/badge.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![Downloads](https://img.shields.io/pypi/dm/pylsp-workspace-symbols)](https://pypistats.org/packages/pylsp-workspace-symbols)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/blob/main/LICENSE)

A [python-lsp-server](https://github.com/python-lsp/python-lsp-server) plugin that adds **workspace/symbol search** and **inlay hints** via [Jedi](https://github.com/davidhalter/jedi).

> **Why?** `pylsp` does not implement `workspace/symbol` natively, and its inlay hints support is limited. This plugin fills both gaps, enabling "Go to Symbol in Workspace" and rich type inference hints in any LSP client — including [CudaText](https://cudatext.github.io/), Neovim, Emacs, and others — with broad client compatibility out of the box.

---

## ✨ Features

- 🔍 **Workspace-wide symbol search** — find functions, classes, and modules across all files in the project
- 💡 **Inlay hints** — inline type annotations inferred by Jedi for assignments, return types, raised exceptions, and parameter names at call sites
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

## 👤 Author

Bruno Eduardo — [github.com/Hanatarou](https://github.com/Hanatarou)

## 📄 License

MIT
