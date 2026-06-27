"""Tests for _on_type_format and all its sub-handlers.

Covers every trigger character advertised in documentOnTypeFormattingProvider:
  \\n  ->  _otf_newline_edits   (smart indent / dedent)
  :   ->  _otf_colon_space_edits  (space after colon)
          _otf_colon_edits        (clause dedent, fallback)
  {   ->  _otf_fstring_edits  (promote to f-string)
  #   ->  _otf_hash_space_edits   (PEP 8 space after hash)
  "   ->  _otf_docstring_edits    (Google-style docstring template)
  ) ] } -> _otf_closer_align_edits (align closer to opener indent)

Run with:
    pytest tests/test_on_type_formatting.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pylsp_workspace_symbols.plugin import _on_type_format  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_OPTIONS = {"tabSize": 4, "insertSpaces": True}
DEFAULT_SETTINGS = {
    "indent_size": 4,
    "dedent_keywords": True,
    "colon_dedent": True,
    "colon_space": True,
    "bracket_indent": True,
    "auto_format_strings": True,
    "hash_space": True,
    "auto_docstring": True,
    "closer_align": True,
    "debug": False,
}


def fmt(
    source: str,
    line: int,
    character: int,
    ch: str,
    settings: dict | None = None,
    options: dict | None = None,
):
    """Thin wrapper around _on_type_format."""
    return _on_type_format(
        source,
        {"line": line, "character": character},
        ch,
        options or DEFAULT_OPTIONS,
        settings or DEFAULT_SETTINGS,
    )


def apply_edits(source: str, edits: list) -> str:
    """Apply a list of LSP TextEdit dicts to *source* and return the result."""
    lines = source.split("\n")
    for edit in sorted(
        edits,
        key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
        reverse=True,
    ):
        sl = edit["range"]["start"]["line"]
        sc = edit["range"]["start"]["character"]
        el = edit["range"]["end"]["line"]
        ec = edit["range"]["end"]["character"]
        new_text = edit["newText"]
        if sl == el:
            line = lines[sl]
            lines[sl] = line[:sc] + new_text + line[ec:]
        else:
            first = lines[sl][:sc] + new_text
            last = lines[el][ec:]
            lines = lines[:sl] + [first + last] + lines[el + 1:]
    return "\n".join(lines)


# ===========================================================================
# TRIGGER: \n — smart indent
# ===========================================================================

class TestNewlineIndent:

    def test_indent_after_if(self):
        src = "if True:\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_for(self):
        src = "for i in range(10):\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_def(self):
        src = "def foo():\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_class(self):
        src = "class Bar:\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_while(self):
        src = "while True:\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_with(self):
        src = 'with open("f") as fh:\n'
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_indent_after_try(self):
        src = "try:\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_no_extra_indent_after_dict(self):
        src = 'd = {"key": "value"}\n'
        edits = fmt(src, line=1, character=0, ch="\n")
        result = apply_edits(src, edits) if edits else src + "\n"
        assert not result.split("\n")[1].startswith("    ")

    def test_hanging_indent_open_paren(self):
        src = "result = foo(\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_hanging_indent_open_bracket(self):
        src = "data = [\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")

    def test_hanging_indent_open_brace(self):
        src = "config = {\n"
        edits = fmt(src, line=1, character=0, ch="\n")
        assert edits
        assert apply_edits(src, edits).split("\n")[1].startswith("    ")


class TestNewlineDedent:
    # The client inserts the new line with the inherited indentation before
    # firing onTypeFormatting.  Source must include that indented new line
    # and character must point to the end of the inherited whitespace.

    def test_dedent_after_return(self):
        src = "def foo():\n    return 42\n    "
        edits = fmt(src, line=2, character=4, ch="\n")
        assert edits
        assert not any(e["newText"].startswith("    ") for e in edits)

    def test_dedent_after_pass(self):
        src = "if True:\n    pass\n    "
        edits = fmt(src, line=2, character=4, ch="\n")
        assert edits
        assert not any(e["newText"].startswith("    ") for e in edits)

    def test_dedent_after_break(self):
        src = "for i in range(10):\n    break\n    "
        edits = fmt(src, line=2, character=4, ch="\n")
        assert edits
        assert not any(e["newText"].startswith("    ") for e in edits)

    def test_dedent_after_continue(self):
        src = "for i in range(10):\n    continue\n    "
        edits = fmt(src, line=2, character=4, ch="\n")
        assert edits
        assert not any(e["newText"].startswith("    ") for e in edits)

    def test_dedent_after_raise(self):
        src = "def foo():\n    raise ValueError()\n    "
        edits = fmt(src, line=2, character=4, ch="\n")
        assert edits
        assert not any(e["newText"].startswith("    ") for e in edits)


# ===========================================================================
# TRIGGER: : — space after colon
# ===========================================================================

class TestColonSpace:

    def test_space_after_colon_dict(self):
        src = 'x = {"key":"value"}'
        edits = fmt(src, line=0, character=11, ch=":")
        assert edits
        result = apply_edits(src, edits)
        assert '"key": ' in result or '": "' in result

    def test_space_after_colon_annotation(self):
        src = "name:str = 'hello'"
        edits = fmt(src, line=0, character=5, ch=":")
        assert edits
        assert "name: str" in apply_edits(src, edits)

    def test_no_space_if_already_space(self):
        src = 'x = {"key": "value"}'
        edits = fmt(src, line=0, character=11, ch=":")
        assert not edits

    def test_no_space_in_slice(self):
        src = "subset = items[1:2]"
        edits = fmt(src, line=0, character=17, ch=":")
        assert not edits

    def test_no_space_in_string(self):
        src = 'x = "key:value"'
        edits = fmt(src, line=0, character=10, ch=":")
        assert not edits


# ===========================================================================
# TRIGGER: : — clause dedent
# ===========================================================================

class TestColonDedent:

    def test_else_dedents_to_if(self):
        src = "if True:\n    pass\n        else:"
        edits = fmt(src, line=2, character=13, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False})
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[2].lstrip() == "else:"

    def test_elif_dedents_to_if(self):
        src = "if True:\n    pass\n        elif False:"
        edits = fmt(src, line=2, character=18, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False})
        assert edits
        assert apply_edits(src, edits).split("\n")[2].lstrip() == "elif False:"

    def test_except_dedents_to_try(self):
        src = "try:\n    pass\n        except Exception:"
        edits = fmt(src, line=2, character=26, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False})
        assert edits
        assert apply_edits(src, edits).split("\n")[2].lstrip() == "except Exception:"

    def test_finally_dedents_to_try(self):
        src = "try:\n    pass\n        finally:"
        edits = fmt(src, line=2, character=16, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False})
        assert edits
        assert apply_edits(src, edits).split("\n")[2].lstrip() == "finally:"

    def test_no_dedent_for_dict(self):
        src = 'd = {"key": "value"}'
        edits = fmt(src, line=0, character=11, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False})
        assert not edits


# ===========================================================================
# TRIGGER: { — f-string promotion
# ===========================================================================

class TestFstringPromotion:

    def test_promotes_plain_string(self):
        src = 'greeting = "hello {"'
        edits = fmt(src, line=0, character=19, ch="{")
        assert edits
        assert 'f"hello {' in apply_edits(src, edits)

    def test_promotes_single_quoted(self):
        src = "path = 'user/id{name'"
        edits = fmt(src, line=0, character=16, ch="{")
        assert edits
        assert "f'" in apply_edits(src, edits)

    def test_no_promotion_if_already_fstring(self):
        src = 'already = f"hello {"'
        edits = fmt(src, line=0, character=19, ch="{")
        assert not edits

    def test_no_promotion_for_bytes(self):
        src = 'raw = b"bytes {"'
        edits = fmt(src, line=0, character=15, ch="{")
        assert not edits

    def test_no_promotion_outside_string(self):
        src = "config = {"
        edits = fmt(src, line=0, character=10, ch="{")
        assert not edits


# ===========================================================================
# TRIGGER: # — PEP 8 space after hash
# ===========================================================================

class TestHashSpace:

    def test_space_after_inline_hash(self):
        src = "x = 1  #inline comment"
        edits = fmt(src, line=0, character=8, ch="#")
        assert edits
        assert "# inline" in apply_edits(src, edits)

    def test_no_space_if_already_space(self):
        src = "x = 1  # comment"
        edits = fmt(src, line=0, character=8, ch="#")
        assert not edits

    def test_no_space_for_shebang(self):
        src = "#!/usr/bin/env python"
        edits = fmt(src, line=0, character=1, ch="#")
        assert not edits

    def test_no_space_for_double_hash(self):
        src = "## section separator"
        edits = fmt(src, line=0, character=1, ch="#")
        assert not edits

    def test_no_space_inside_string(self):
        src = 'label = "a#b"'
        edits = fmt(src, line=0, character=10, ch="#")
        assert not edits

    def test_space_inserted_when_hash_is_last_char(self):
        src = "x = 1  #"
        edits = fmt(src, line=0, character=8, ch="#")
        assert edits
        result = apply_edits(src, edits)
        assert result == "x = 1  # "


# ===========================================================================
# TRIGGER: " — Google-style docstring template
# ===========================================================================

class TestDocstringTemplate:

    def test_expands_after_def_with_params_and_return(self):
        src = 'def add(x: int, y: int) -> int:\n    """'
        edits = fmt(src, line=1, character=7, ch='"')
        assert edits
        result = apply_edits(src, edits)
        assert "Args:" in result
        assert "Returns:" in result
        assert "x: Description." in result
        assert "y: Description." in result

    def test_expands_after_def_no_params_no_return(self):
        src = "def side_effect():\n    \"\"\""
        edits = fmt(src, line=1, character=7, ch='"')
        assert edits
        result = apply_edits(src, edits)
        assert "Args:" not in result
        assert "Returns:" not in result
        assert '"""' in result

    def test_expands_after_class(self):
        src = "class MyClass:\n    \"\"\""
        edits = fmt(src, line=1, character=7, ch='"')
        assert edits
        result = apply_edits(src, edits)
        assert "Summary." in result

    def test_skips_self_param(self):
        src = "class Foo:\n    def method(self, value: int) -> bool:\n        \"\"\""
        edits = fmt(src, line=2, character=11, ch='"')
        assert edits
        result = apply_edits(src, edits)
        # "self" must not appear inside the Args section
        args_section = result.split("Args:")[-1] if "Args:" in result else ""
        assert "self" not in args_section
        assert "value: Description." in result

    def test_no_expansion_when_not_triple_quote(self):
        src = 'x = "hello"'
        edits = fmt(src, line=0, character=3, ch='"')
        assert not edits

    def test_no_expansion_when_content_follows(self):
        src = 'def foo():\n    """existing docstring"""'
        edits = fmt(src, line=1, character=7, ch='"')
        assert not edits

    def test_blank_lines_have_no_trailing_spaces(self):
        src = 'def add(x: int, y: int) -> int:\n    """'
        edits = fmt(src, line=1, character=7, ch='"')
        assert edits
        result = apply_edits(src, edits)
        for line in result.split("\n"):
            if not line.strip():
                assert line == "", f"Blank line has trailing spaces: {repr(line)}"


# ===========================================================================
# TRIGGER: ) ] } — closer alignment
# ===========================================================================

class TestCloserAlign:

    def test_paren_aligns_to_column_0(self):
        src = "result = foo(\n    arg,\n    )"
        edits = fmt(src, line=2, character=5, ch=")")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[2] == ")"

    def test_bracket_aligns_to_column_0(self):
        src = "data = [\n    1,\n    ]"
        edits = fmt(src, line=2, character=5, ch="]")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[2] == "]"

    def test_brace_aligns_to_column_0(self):
        src = 'config = {\n    "k": "v",\n    }'
        edits = fmt(src, line=2, character=5, ch="}")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[2] == "}"

    def test_paren_aligns_to_indented_opener(self):
        src = "def foo():\n    result = bar(\n        arg,\n        )"
        edits = fmt(src, line=3, character=9, ch=")")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[3] == "    )"

    def test_no_edit_if_already_aligned(self):
        src = "result = foo(\n    arg,\n)"
        edits = fmt(src, line=2, character=1, ch=")")
        assert not edits

    def test_closer_with_trailing_comma(self):
        src = "result = foo(\n    arg,\n    ),"
        edits = fmt(src, line=2, character=6, ch=")")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[2] == "),"

    def test_nested_closers(self):
        src = 'x = {\n    "k": [\n        1,\n        ],\n    }'
        edits = fmt(src, line=3, character=9, ch="]")
        assert edits
        result = apply_edits(src, edits)
        assert result.split("\n")[3] == '    ],'


# ===========================================================================
# Settings flags
# ===========================================================================

class TestSettingsFlags:

    def test_colon_space_disabled(self):
        src = 'x = {"key":"value"}'
        edits = fmt(src, line=0, character=11, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False, "colon_dedent": False})
        assert not edits

    def test_colon_dedent_disabled(self):
        src = "if True:\n    pass\n        else:"
        edits = fmt(src, line=2, character=13, ch=":",
                    settings={**DEFAULT_SETTINGS, "colon_space": False, "colon_dedent": False})
        assert not edits

    def test_auto_format_strings_disabled(self):
        src = 'greeting = "hello {"'
        edits = fmt(src, line=0, character=19, ch="{",
                    settings={**DEFAULT_SETTINGS, "auto_format_strings": False})
        assert not edits

    def test_hash_space_disabled(self):
        src = "x = 1  #inline comment"
        edits = fmt(src, line=0, character=8, ch="#",
                    settings={**DEFAULT_SETTINGS, "hash_space": False})
        assert not edits

    def test_auto_docstring_disabled(self):
        src = 'def add(x: int, y: int) -> int:\n    """'
        edits = fmt(src, line=1, character=7, ch='"',
                    settings={**DEFAULT_SETTINGS, "auto_docstring": False})
        assert not edits

    def test_closer_align_disabled(self):
        src = "result = foo(\n    arg,\n    )"
        edits = fmt(src, line=2, character=5, ch=")",
                    settings={**DEFAULT_SETTINGS, "closer_align": False})
        assert not edits

    def test_dedent_keywords_disabled(self):
        src = "def foo():\n    return 42\n"
        edits = fmt(src, line=2, character=0, ch="\n",
                    settings={**DEFAULT_SETTINGS, "dedent_keywords": False})
        if edits:
            result = apply_edits(src, edits)
            line = result.split("\n")[2]
            assert line.startswith("    ") or line == ""
