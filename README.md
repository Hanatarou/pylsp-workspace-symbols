# pylsp-workspace-symbols

[![CI](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml/badge.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pylsp-workspace-symbols)](https://pypi.org/project/pylsp-workspace-symbols)
[![Downloads](https://img.shields.io/pypi/dm/pylsp-workspace-symbols)](https://pypistats.org/packages/pylsp-workspace-symbols)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Hanatarou/pylsp-workspace-symbols/blob/main/LICENSE)

A [python-lsp-server](https://github.com/python-lsp/python-lsp-server) plugin that adds **workspace symbol search**, **inlay hints**, **code lens**, **semantic tokens**, **call/type hierarchy**, **document links** and **document colors** via [Jedi](https://github.com/davidhalter/jedi).

> **Why?** `pylsp` does not implement several LSP features natively. This plugin fills those gaps — enabling "Go to Symbol in Workspace", rich type inference hints, code lens overlays, semantic token highlighting, call/type hierarchy navigation, clickable import links, and inline color previews in any LSP client — including [CudaText](https://cudatext.github.io/), Neovim, Emacs, and others — with broad client compatibility out of the box.

---

## ✨ Features

- 🔍 **Workspace-wide symbol search** — find functions, classes, and modules across all files in the project
- 💡 **Inlay hints** — inline type annotations inferred by Jedi for assignments, return types, raised exceptions, and parameter names at call sites
- 🔭 **Code lens** — per-definition overlays showing reference counts (`👥 N references`), subclass/override counts (`🔗 N implementations`), run entry points (`▶ Run`), and test markers (`🧪 Run test`)
- 🎨 **Semantic tokens** — Jedi-backed token classification for editors that support `textDocument/semanticTokens` (opt-in, disabled by default)
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
      "code_lens": {
        "enabled": true,
        "show_references": true,
        "show_implementations": true,
        "cross_file_implementations": false,
        "show_run": true,
        "show_tests": true,
        "max_definitions": 150
      },
      "semantic_tokens": {
        "enabled": false
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

### Code lens options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable all code lenses |
| `show_references` | bool | `true` | Show `👥 N references` above every function, method, and class |
| `show_implementations` | bool | `true` | Show `🔗 N implementations` on classes with subclasses and methods with overrides |
| `cross_file_implementations` | bool | `false` | Extend `🔗` counts to subclasses/overrides in other files. Opt-in — adds one `get_references()` call + file I/O per class/method |
| `show_run` | bool | `true` | Show `▶ Run` above `if __name__ == "__main__":` blocks |
| `show_tests` | bool | `true` | Show `🧪 Run test` above `test_*` functions and `Test*` classes |
| `max_definitions` | int | `150` | Maximum number of definitions to process per file |

### Semantic token options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable/disable semantic token highlighting. Opt-in — can be slow on very large files |

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

### Code lens

Your LSP client will receive `codeLensProvider: true`. The following overlays are shown above
definitions:

- **`👥 N references`** — every top-level function, method, and class. Inheritance usages
  (`class Dog(Animal)`) are excluded from the reference count and counted as implementations instead.
  Cross-file inheritance usages are also excluded when `cross_file_implementations=true`.
- **`🔗 N implementations`** — classes with direct subclasses; methods overridden in at least one
  subclass. Intra-file always; cross-file when `cross_file_implementations=true` (opt-in).
- **`▶ Run`** — `if __name__ == "__main__":` entry point blocks.
  Fires `workspace/executeCommand` with command `pylsp_workspace_symbols.run_file`.
- **`🧪 Run test`** — `test_*` functions and `Test*` / `unittest.TestCase` subclasses.
  Fires `workspace/executeCommand` with command `pylsp_workspace_symbols.run_test`,
  passing `{"path": ..., "name": ..., "kind": "function"|"class"}` as arguments.

### Semantic tokens

Your LSP client will receive `semanticTokensProvider` when `semantic_tokens.enabled` is `true`.
Token types follow an extended LSP legend: `namespace`, `type`, `class`, `enum`, `interface`,
`struct`, `typeParameter`, `parameter`, `variable`, `property`, `enumMember`, `function`,
`method`, `macro`, `keyword`, `comment`, `string`, `number`, `regexp`, `operator`,
`decorator`, plus Python-specific `selfParameter` and `clsParameter`.
Modifiers include `declaration`, `readonly`, `static`, `deprecated`, `async`,
`modification`, `documentation`, `defaultLibrary`, `builtin`, `classMember`, and `parameter`.
Disabled by default — enable explicitly if your client supports it and you want Jedi-backed
token classification in addition to your editor's built-in lexer.

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

Results are **limited to files inside the known workspace folders**. All open workspace roots are
read from the live server at query time via `server.workspaces`, so folders added after startup
(`workspace/didChangeWorkspaceFolders`) are included correctly. Each root is searched with
`sys_path=[root]`, restricting Jedi's indexing to that folder only — avoiding the full Python
environment (stdlib + site-packages). This yields an ~80x speedup on `complete_search` compared
to the default Jedi project. A `_is_relative_to()` guard (Python 3.8-compatible replacement for
`Path.is_relative_to`, which requires 3.9+) provides a second layer of filtering.

> **Note:** `workspace/symbol` returns module-level definitions (functions, classes, modules).
> Local variables inside functions are not indexed — this is standard LSP behaviour,
> consistent with pyright and other Python language servers.

### Inlay hints

The plugin handles the `textDocument/inlayHint` request using a hybrid approach:

1. **Regex scan** — fast pass over the source to locate `def`, assignment, `raise`, and call patterns.
2. **`_literal_type` fast-path** — resolves common literals (`"str"`, `42`, `True`, `[...]`, etc.) without calling Jedi.
3. **Jedi inference** — for non-literal expressions, `script.infer()` and `script.get_signatures()` are used to resolve types.
4. **Signature fallback** — for `self.attr = param` assignments, the enclosing `def` signature is inspected for type annotations or default values.

### Code lens

Handled via the native `pylsp_code_lens` hookspec. Uses a two-pass approach:

1. **AST pass** — single `ast.walk` over the file to collect all definitions and build intra-file
   maps of subclass relationships (`class_subclasses`) and method overrides (`method_overrides`).
   Also pre-computes inheritance usage positions (intra-file) to correctly separate reference
   counts from implementation counts.
2. **Jedi reference pass** — one `script.get_references()` call per definition to count
   non-definition references. Cross-file inheritance positions are excluded from the reference
   count using `_find_cross_file_subclasses` (only when `cross_file_implementations=True`).
   Results are cached by `(uri, hash(source))` so repeated requests on an unchanged file skip
   all work.

When `cross_file_implementations=True`, `_find_cross_file_subclasses` uses
`script.get_references()` with the workspace project to find subclasses in other files,
verifying each ref against the AST of the referenced file. A per-request `_cf_subclass_cache`
ensures the I/O is shared between the references filter and the implementations count.

### Semantic tokens

Handled via `textDocument/semanticTokens/full`, `full/delta`, and `range` dispatchers.
Uses a two-phase O(n) approach — no per-token `goto` calls:

1. **AST pass** — single `ast.parse()` walk to build lookup tables for token types and
   modifiers that Jedi alone cannot determine: `enumMember`, `typeParameter`, `decorator`,
   `@classmethod`/`@staticmethod` → `static`, `ClassVar`/`Final` → `readonly`,
   `deprecated` (via decorator or docstring), `async`, and augmented assignments → `modification`.
2. **Jedi pass** — `jedi.Script.get_names(all_scopes=True)` provides the base token stream.
   A two-sub-pass strategy resolves statement references via lookup dicts built in phase 1,
   avoiding any per-token inference calls.

Beyond the two main passes, a token injection stage handles symbols that neither Jedi nor
the AST walk emit directly: `@` decorator markers, names inside forward-reference annotation
strings, type parameter attributes (e.g. `ParamSpec.args`), and regexp patterns in
`re.compile()` calls. Each injected token receives the correct type and modifiers using the
same lookup tables built in the AST pass. Delta computation uses `SequenceMatcher` to
produce minimal `SemanticTokensEdit[]` arrays.

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
- [LSP code lens specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_codeLens)
- [LSP semantic tokens specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_semanticTokens)
- [LSP call hierarchy specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_prepareCallHierarchy)
- [LSP type hierarchy specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_prepareTypeHierarchy)
- [LSP document links specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentLink)
- [LSP document colors specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentColor)

## 👤 Author

Bruno Eduardo — [github.com/Hanatarou](https://github.com/Hanatarou)

## 📄 License

MIT
