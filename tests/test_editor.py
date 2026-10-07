import time

import pytest

import editor

INI = """; server settings
[General]
Name = My Server
Port=2456
# keep this
Public: true

[Rules]
Rate=1.5
"""

PALWORLD = (
    "[/Script/Pal.PalGameWorldSettings]\r\n"
    'OptionSettings=(Difficulty=None,ExpRate=1.000000,ServerName="A, B (test)",Crossplay=(Steam,Xbox),Pvp=False)\r\n'
)

LIST = "// admins\n76561190000000001\n\n76561190000000002\n"


def by_key(parsed):
    return {e["key"]: e for e in parsed["entries"]}


def test_parse_keyvalue_with_sections_and_types():
    parsed = editor.parse(INI)
    assert parsed["format"] == "keyvalue"
    entries = by_key(parsed)
    assert entries["Name"]["value"] == "My Server" and entries["Name"]["section"] == "General"
    assert entries["Port"]["type"] == "int"
    assert entries["Public"]["type"] == "bool"
    assert entries["Rate"]["type"] == "float" and entries["Rate"]["section"] == "Rules"
    assert entries["Public"]["comment"] == "keep this"  # comments are not entries, but ride along
    assert len(parsed["entries"]) == 4


def test_parse_list():
    parsed = editor.parse(LIST)
    assert parsed["format"] == "list"
    assert [e["value"] for e in parsed["entries"]] == ["76561190000000001", "76561190000000002"]


def test_parse_packed_struct_respects_quotes_and_brackets():
    fields = {f["key"]: f for f in by_key(editor.parse(PALWORLD))["OptionSettings"]["fields"]}
    assert list(fields) == ["Difficulty", "ExpRate", "ServerName", "Crossplay", "Pvp"]
    assert fields["ServerName"] == {"key": "ServerName", "value": "A, B (test)", "type": "string"}
    assert fields["Crossplay"]["type"] == "raw" and fields["Crossplay"]["value"] == "(Steam,Xbox)"
    assert fields["ExpRate"]["type"] == "float"


def test_render_changes_only_the_edited_line():
    entries = by_key(editor.parse(INI))
    out = editor.render(INI, "keyvalue", {str(entries["Port"]["line"]): "2457"})
    assert out == INI.replace("Port=2456", "Port=2457")


def test_render_keeps_separator_style_and_comments():
    entries = by_key(editor.parse(INI))
    out = editor.render(INI, "keyvalue", {str(entries["Name"]["line"]): "Other", str(entries["Public"]["line"]): "false"})
    assert "Name = Other" in out and "Public: false" in out
    assert out.startswith("; server settings\n") and "# keep this\n" in out


def test_render_struct_edits_one_setting_and_keeps_the_rest_and_crlf():
    line = by_key(editor.parse(PALWORLD))["OptionSettings"]["line"]
    out = editor.render(PALWORLD, "keyvalue", {str(line): {"ExpRate": "2.000000", "ServerName": "New, Name"}})
    assert out == PALWORLD.replace("ExpRate=1.000000", "ExpRate=2.000000").replace("A, B (test)", "New, Name")
    assert out.count("\r\n") == 2


def test_render_struct_ignores_unknown_names_and_refuses_nested_edits():
    line = by_key(editor.parse(PALWORLD))["OptionSettings"]["line"]
    assert editor.render(PALWORLD, "keyvalue", {str(line): {"Nope": "1"}}) == PALWORLD
    with pytest.raises(editor.EditError):  # the form shows nested parts read-only; an edit would be dropped silently
        editor.render(PALWORLD, "keyvalue", {str(line): {"Crossplay": "()"}})


@pytest.mark.parametrize("edit", [
    {"ExpRate": "1,Evil=2"}, {"ExpRate": "(x)"}, {"ServerName": 'a"b'}, {"ExpRate": "1\nX=1"},
    {"ServerName": 'a\\\\"b'},  # `a\\"b`: the quote is not escaped, the backslashes escape each other
    {"ServerName": "Foo\\"},  # would escape the closing quote
    {"ExpRate": "1\x0b2"}, {"ExpRate": "1\u20282"}, {"ExpRate": "1\x85"}, {"ExpRate": "1\n"},
])
def test_render_struct_refuses_values_that_would_break_the_line(edit):
    line = by_key(editor.parse(PALWORLD))["OptionSettings"]["line"]
    with pytest.raises(editor.EditError):
        editor.render(PALWORLD, "keyvalue", {str(line): edit})


def test_render_refuses_line_breaks_and_bad_line_numbers():
    with pytest.raises(editor.EditError):
        editor.render(INI, "keyvalue", {"4": "x\nInjected=1"})
    for bad in ("99", "-1", "abc"):
        with pytest.raises(editor.EditError):
            editor.render(INI, "keyvalue", {bad: "x"})
    with pytest.raises(editor.EditError):  # a comment line is not a setting
        editor.render(INI, "keyvalue", {"0": "x"})


def test_render_list_keeps_comments_and_handles_add_and_remove():
    assert editor.render(LIST, "list", {"entries": ["76561190000000001", "76561190000000002"]}) == LIST
    out = editor.render(LIST, "list", {"entries": ["76561190000000002"]})
    assert out == "// admins\n76561190000000002\n\n"
    out = editor.render(LIST, "list", {"entries": ["a", "b", "c"]})
    assert out == "// admins\na\n\nb\nc\n"
    assert editor.render("x", "list", {"entries": ["x", "y"]}) == "x\ny\n"


def test_render_is_a_noop_without_edits():
    assert editor.render(INI, "keyvalue", {}) == INI
    assert editor.render(PALWORLD, "keyvalue", {}) == PALWORLD


def test_digest_changes_with_the_text():
    assert editor.digest("a") == editor.digest("a") != editor.digest("b")


def test_candidates_and_dirs():
    assert editor.is_candidate("Game.INI", 100)
    assert not editor.is_candidate("a.ini", 1)  # stub
    assert not editor.is_candidate("a.ini", 10**7)  # generated data
    assert not editor.is_candidate("a.bin", 100)
    assert not editor.is_candidate("a.ini", None)
    assert editor.skip_dir("steamapps") and editor.skip_dir("valheim_server_Data")
    assert not editor.skip_dir(".config")


@pytest.mark.parametrize("path,ok", [
    ("/Saved/Config/a.ini", True), ("/", True), ("a.ini", False), ("/a/../b", False),
    ("/a\\b", False), ("/a\x00b", False), ("/" + "x" * 600, False), (None, False),
])
def test_safe_path(path, ok):
    assert editor.safe_path(path) is ok


SAMPLE = """; This configuration file is a sample of the default server settings.
; Changes to this file will NOT be reflected on the server.
; To change the server settings, modify Pal/Saved/Config/LinuxServer/PalWorldSettings.ini.

[/Script/Pal.PalGameWorldSettings]
OptionSettings=(ExpRate=1.000000)
"""


def test_header_is_the_comment_block_at_the_top():
    assert editor.parse(SAMPLE)["header"] == [
        "This configuration file is a sample of the default server settings.",
        "Changes to this file will NOT be reflected on the server.",
        "To change the server settings, modify Pal/Saved/Config/LinuxServer/PalWorldSettings.ini.",
    ]
    assert editor.render(SAMPLE, "keyvalue", {}) == SAMPLE  # showing it changes nothing


def test_header_edge_cases():
    assert editor.parse("[A]\n; about a\nx=1\n")["header"] == []  # not at the top
    assert editor.parse("\n\n# a\n// b\n\n# not part\nx=1\n")["header"] == ["a", "b"]
    assert editor.parse("x=1\n")["header"] == []
    assert editor.parse("")["header"] == []


COMMENTED = """; File about
; the whole file

[General]
; The name shown in the list.
; Keep it short.
Name = My Server
Port=2456
# not attached: a blank line follows

Public: true
; about the section below
[Rules]
Rate=1.5
; Packed settings.
OptionSettings=(A=1)
"""


def test_a_settings_comment_is_the_lines_right_above_it():
    entries = by_key(editor.parse(COMMENTED))
    assert entries["Name"]["comment"] == "The name shown in the list.\nKeep it short."
    assert "comment" not in entries["Port"]  # no comment directly above
    assert "comment" not in entries["Public"]  # cut off by a blank line
    assert "comment" not in entries["Rate"]  # the comment above [Rules] belongs to the section
    assert entries["OptionSettings"]["comment"] == "Packed settings."


def test_the_header_is_not_repeated_as_the_first_settings_comment():
    parsed = editor.parse("; about the file\n[A]\nx=1\n")
    assert parsed["header"] == ["about the file"] and "comment" not in parsed["entries"][0]
    glued = editor.parse("; about\nx=1\n")  # no blank line or section: it is the header only
    assert glued["header"] == ["about"] and "comment" not in glued["entries"][0]
    assert editor.render(COMMENTED, "keyvalue", {}) == COMMENTED


# ---- regressions from the code review ------------------------------------------------------

def test_a_lone_cr_file_round_trips_without_fusing_lines():
    text = "a=1\rb=2\rc=3\r"
    entries = by_key(editor.parse(text))
    assert [entries[k]["line"] for k in "abc"] == [0, 1, 2]
    assert editor.render(text, "keyvalue", {"0": "9"}) == "a=9\rb=2\rc=3\r"
    assert editor.render(text, "keyvalue", {"2": "7"}) == "a=1\rb=2\rc=7\r"
    assert editor.render(text, "keyvalue", {}) == text


def test_only_lf_crlf_and_cr_end_a_line():
    text = "a=1\x0cb=2\u2028c\n"  # a form feed and a line separator are part of the value
    parsed = editor.parse(text)
    assert [e["key"] for e in parsed["entries"]] == ["a"]
    assert editor.render(text, "keyvalue", {"0": "9"}) == "a=9\n"
    assert editor.render("a=1\r\nb=2\nc=3\r", "keyvalue", {"0": "x", "1": "y", "2": "z"}) == "a=x\r\nb=y\nc=z\r"


def test_a_list_with_cr_endings_keeps_them():
    text = "// admins\r1\r2\r"
    assert editor.parse(text)["format"] == "list"
    assert editor.render(text, "list", {"entries": ["1", "3"]}) == "// admins\r1\r3\r"
    assert editor.render(text, "list", {"entries": ["1", "2", "3"]}) == "// admins\r1\r2\r3\n"


STRUCT = 'OptionSettings=(A="Foo",B=1,Path="C:\\\\",C="x")\n'


def test_a_quoted_value_ending_in_a_double_backslash_parses_and_survives_an_edit():
    fields = {f["key"]: f["value"] for f in by_key(editor.parse(STRUCT))["OptionSettings"]["fields"]}
    assert fields == {"A": "Foo", "B": "1", "Path": "C:\\\\", "C": "x"}  # the closing quote is found, C is not swallowed
    out = editor.render(STRUCT, "keyvalue", {"0": {"B": "2"}})
    assert out == STRUCT.replace("B=1", "B=2")
    out = editor.render(STRUCT, "keyvalue", {"0": {"Path": "D:\\\\"}})  # an even run is fine
    assert out == STRUCT.replace("C:", "D:")


def test_an_escaped_quote_inside_a_struct_value_does_not_end_it():
    line = 'S=(A="say \\"hi\\", ok",B=1)\n'
    fields = {f["key"]: f["value"] for f in by_key(editor.parse(line))["S"]["fields"]}
    assert fields == {"A": 'say \\"hi\\", ok', "B": "1"}


def test_a_trailing_odd_backslash_is_refused_so_the_closing_quote_survives():
    with pytest.raises(editor.EditError):
        editor.render(STRUCT, "keyvalue", {"0": {"A": "Foo\\"}})
    # and the same text written with an even run keeps the struct intact
    out = editor.render(STRUCT, "keyvalue", {"0": {"A": "Foo\\\\"}})
    assert [f["value"] for f in by_key(editor.parse(out))["OptionSettings"]["fields"]] == ["Foo\\\\", "1", "C:\\\\", "x"]


def test_list_entries_that_would_read_as_comments_are_refused():
    for bad in ("# note", "; note", "//host", "  # padded"):
        with pytest.raises(editor.EditError):
            editor.render(LIST, "list", {"entries": ["76561190000000001", bad]})
    assert editor.render(LIST, "list", {"entries": ["a#b"]}) == "// admins\na#b\n\n"


def test_a_long_whitespace_line_is_handled_in_linear_time():
    started = time.monotonic()
    for pad in (40_000, 10_000):  # over the line limit, and just under it
        text = "k=a" + " " * pad + "b\nj=1\n"
        parsed = editor.parse(text)
        assert editor.parse(text)["format"] in ("keyvalue", "list")
        if pad < editor.MAX_LINE:
            assert by_key(parsed)["k"]["value"] == "a" + " " * pad + "b"
            assert editor.render(text, "keyvalue", {"1": "2"}) == text.replace("j=1", "j=2")
        else:  # skipped by parse, refused by render
            assert [e["key"] for e in parsed["entries"]] == ["j"]
            with pytest.raises(editor.EditError):
                editor.render(text, "keyvalue", {"1": "2"})
    assert editor.parse("k=a" + " " * 100_000 + "\n")["entries"] == []
    assert time.monotonic() - started < 2
