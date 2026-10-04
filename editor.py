"""Reading and rewriting game-server config files (no Flask, no network).

Pelican's file API hands over plain text; this module turns it into fields for a form and puts
edited values back without disturbing anything else in the file. Two shapes are understood:

* key/value (`key=value` or `key: value`, optionally under `[section]` headers). A value of the
  form `(a=1,b="x")` (Unreal Engine's packed struct, e.g. Palworld's `OptionSettings`) is split
  into one field per setting.
* list (one value per line, e.g. an admin list).

Comments start with `;`, `#` or `//`. A save only replaces the values that were edited, so
comments, blank lines, ordering and line endings survive.
"""

import hashlib
import os
import re

CANDIDATE_EXTENSIONS = {".ini", ".cfg", ".properties", ".txt"}
# Install trees (SteamCMD, Unity) hold thousands of asset files; skipped by name. Dot-directories
# are walked on purpose: some games keep their real settings in `.config`.
SKIP_DIR_NAMES = {"Steam", "steamapps", "steamcmd", "docker"}
SKIP_DIR_SUFFIXES = ("_Data",)
MAX_DEPTH = 6
MAX_ENTRIES = 500
MIN_SIZE = 16  # engine boilerplate is often a one-byte stub
MAX_SIZE = 256 * 1024
COMMENT_PREFIXES = (";", "#", "//")

_KEYVALUE = re.compile(r"^(\s*)([^\s=:#;]+)(\s*[=:]\s*)(.*?)\s*$")
_SECTION = re.compile(r"^\s*\[(.+)\]\s*$")


class EditError(ValueError):
    """The edit can't be applied; the message is safe to show."""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- which files are worth offering ------------------------------------------------------

def skip_dir(name):
    return name in SKIP_DIR_NAMES or name.endswith(SKIP_DIR_SUFFIXES)


def is_candidate(name, size):
    return os.path.splitext(name)[1].lower() in CANDIDATE_EXTENSIONS and isinstance(size, int) and MIN_SIZE <= size <= MAX_SIZE


def safe_path(path):
    """True for an absolute path with no `..`, NUL or backslash parts."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "\\" in path or len(path) > 512:
        return False
    return ".." not in path.split("/")


# ---- parsing -----------------------------------------------------------------------------

def _is_comment(line):
    return line.strip().startswith(COMMENT_PREFIXES)


def _kind(value):
    v = value.strip()
    if v.lower() in ("true", "false"):
        return "bool"
    if re.fullmatch(r"-?\d+", v):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", v):
        return "float"
    return "string"


def _split_struct(inner):
    """The comma-separated parts of a struct body, ignoring commas inside quotes or parentheses."""
    parts, depth, quoted, start = [], 0, False, 0
    for i, ch in enumerate(inner):
        if quoted:
            if ch == '"' and inner[i - 1] != "\\":
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    parts.append(inner[start:])
    return parts


def _is_struct(value):
    v = value.strip()
    return len(v) >= 2 and v.startswith("(") and v.endswith(")")


def _field(raw):
    """(display value, kind) of one struct value as written in the file."""
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1], "string"
    if _is_struct(raw):
        return raw, "raw"  # a nested list or struct is edited as one piece of text
    return raw, _kind(raw) if raw else "string"


def _struct_fields(value):
    fields = []
    for part in _split_struct(value.strip()[1:-1]):
        if "=" not in part:
            continue
        name, raw = part.split("=", 1)
        shown, kind = _field(raw)
        fields.append({"key": name.strip(), "value": shown, "type": kind})
    return fields


def _header(lines):
    """The comment block at the top of the file (prefixes removed), for showing above the form.
    It ends at the first line that is not a comment or at the first blank line after it.
    Returns (comment lines, number of file lines the header covers)."""
    out = []
    for number, line in enumerate(lines):
        text = line.strip()
        if not text:
            if out:
                return out, number
            continue
        prefix = next((p for p in COMMENT_PREFIXES if text.startswith(p)), None)
        if not prefix:
            return out, number
        out.append(text[len(prefix):].strip())
    return out, len(lines)


def _comment_text(line):
    text = line.strip()
    return text[len(next(p for p in COMMENT_PREFIXES if text.startswith(p))):].strip()


def parse(text):
    """{"format": "keyvalue"|"list", "entries": [...], "header": [comment line, ...]}. Every entry has `line` (0-based) and
    `value`; key/value entries also have `key`, `section`, `type`, `comment` (the comment lines right above
    the setting, if any) and, for packed structs, `fields`."""
    lines = text.splitlines()
    content = pairs = 0
    for line in lines:
        if not line.strip() or _is_comment(line):
            continue
        content += 1
        if not _SECTION.match(line) and _KEYVALUE.match(line):
            pairs += 1
    fmt = "keyvalue" if content and pairs * 2 >= content else "list"

    header, header_end = _header(lines)
    entries, section, notes = [], None, []
    for number, line in enumerate(lines):
        if not line.strip():
            notes = []  # a blank line cuts a comment off from what follows
            continue
        if _is_comment(line):
            if number >= header_end:
                notes.append(_comment_text(line))
            continue
        if fmt == "list":
            entries.append({"line": number, "value": line.strip()})
            continue
        header_match = _SECTION.match(line)
        if header_match:
            section = header_match.group(1)
            notes = []
            continue
        match = _KEYVALUE.match(line)
        if not match:
            notes = []
            continue
        _, key, _, value = match.groups()
        entry = {"line": number, "key": key, "section": section, "value": value, "type": _kind(value)}
        if notes:
            entry["comment"] = "\n".join(notes)  # the comment lines directly above the setting
        notes = []
        if _is_struct(value):
            entry["fields"] = _struct_fields(value)
        entries.append(entry)
    return {"format": fmt, "entries": entries, "header": header}


# ---- writing back ------------------------------------------------------------------------

def _one_line(value):
    if not isinstance(value, str) or "\n" in value or "\r" in value or "\x00" in value:
        raise EditError("a value can't contain a line break")
    return value


def _render_struct(original, edits):
    out = []
    for part in _split_struct(original.strip()[1:-1]):
        name, sep, raw = part.partition("=")
        key = name.strip()
        if not sep or key not in edits or _is_struct(raw):
            out.append(part)  # untouched, and nested structs are never rewritten
            continue
        new = _one_line(edits[key])
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            if re.search(r'(?<!\\)"', new):
                raise EditError(f"{key}: a quote inside the text must be written as \\\"")
            out.append(f'{name}="{new}"')
        else:
            if any(c in new for c in ',()"'):
                raise EditError(f"{key}: this value can't contain commas, brackets or quotes")
            out.append(f"{name}={new}")
    return "(" + ",".join(out) + ")"


def _ending(line):
    for end in ("\r\n", "\n"):
        if line.endswith(end):
            return line[: -len(end)], end
    return line, ""


def render(original, fmt, edits):
    """`original` with the edits applied.

    key/value: `edits` maps a line number (as in `parse`) to the new value text, or, for a packed
    struct, to {setting: new value} for the settings that changed.
    list: `edits` is {"entries": [value, ...]}, the whole list as it should now read."""
    lines = original.splitlines(keepends=True)

    if fmt == "keyvalue":
        for number, new in edits.items():
            try:
                index = int(number)
            except (TypeError, ValueError):
                raise EditError("unknown line")
            if not 0 <= index < len(lines):
                raise EditError("unknown line")
            body, end = _ending(lines[index])
            match = _KEYVALUE.match(body)
            if not match or _is_comment(body):
                raise EditError("that line is not a setting any more; reload the file")
            lead, key, sep, value = match.groups()
            if isinstance(new, dict):
                if not _is_struct(value):
                    raise EditError("that line is not a packed setting any more; reload the file")
                value = _render_struct(value, new)
            else:
                value = _one_line(new)
            lines[index] = f"{lead}{key}{sep}{value}{end}"
        return "".join(lines)

    entries = edits.get("entries") if isinstance(edits, dict) else None
    if not isinstance(entries, list):
        raise EditError("a list file needs its entries")
    pending = [_one_line(e).strip() for e in entries if str(e).strip()]
    out = []
    for line in lines:
        if not line.strip() or _is_comment(line):
            out.append(line)
        elif pending:
            out.append(pending.pop(0) + (_ending(line)[1] or "\n"))
        # else: this entry was removed
    if pending and out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out += [e + "\n" for e in pending]
    return "".join(out)
