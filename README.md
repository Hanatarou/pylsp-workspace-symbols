# pylsp-workspace-symbols

[![CI](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml/badge.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![Downloads](https://img.shields.io/pypi/dm/pylsp-workspace-symbols)](https://pypistats.org/packages/pylsp-workspace-symbols)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/blob/main/LICENSE)

A [python-lsp-server](https://github.com/python-lsp/python-lsp-server) plugin that adds **workspace/symbol** support via [Jedi](https://github.com/davidhalter/jedi).

> **Why?** `pylsp` does not implement `workspace/symbol` natively. This plugin fills that gap, enabling "Go to Symbol in Workspace" in any LSP client — including [CudaText](https://cudatext.github.io/), Neovim, Emacs, and others.

---

## ✨ Features

- 🔍 **Workspace-wide symbol search** — find functions, classes, and modules across all files in the project
- ⚡ **Fast** — results in ~130ms after the first call (Jedi cache warm)
- 🔤 **Case-insensitive substring match** — `area` finds `calculate_area`, `Cal` finds `Calculator`
- 📁 **Smart folder exclusion** — automatically skips `.git`, `__pycache__`, `node_modules`, `.venv`, `dist`, `build`, and more
- ⚙️ **Configurable** — tune `max_symbols` and `ignore_folders` via pylsp settings
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
      }
    }
  }
}
```

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable the plugin |
| `max_symbols` | int | `500` | Maximum symbols returned. `0` means no limit |
| `ignore_folders` | list | `[]` | Extra folder names to skip (merged with built-in list) |

### Built-in ignored folders

`.git`, `.hg`, `.svn`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`,
`node_modules`, `.venv`, `venv`, `.env`, `env`, `dist`, `build`, `.eggs`, `egg-info`

## 🚀 Usage

Once installed, your LSP client will receive `workspaceSymbolProvider: true` in the server capabilities.
Use your client's "Go to Symbol in Workspace" command (typically `Ctrl+T` or `#` in the symbol picker).

### How it works

pylsp does not define a `pylsp_workspace_symbols` hookspec, so this plugin uses two hooks:

1. **`pylsp_experimental_capabilities`** — advertises `workspaceSymbolProvider: true` to the client during the `initialize` handshake.
2. **`pylsp_dispatchers`** — registers a custom JSON-RPC handler for `workspace/symbol` that calls Jedi's `project.complete_search("")` and filters results client-side by case-insensitive substring match.

> **Note:** `workspace/symbol` returns module-level definitions (functions, classes, modules).
> Local variables inside functions are not indexed — this is standard LSP behaviour,
> consistent with pyright and other Python language servers.

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

## 👤 Author

Bruno Eduardo — [github.com/Hanatarou](https://github.com/Hanatarou)

## 📄 License

MIT
