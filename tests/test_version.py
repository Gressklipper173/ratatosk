"""The version in VERSION (shown in the page and used for the image) and the newest CHANGELOG
entry must say the same thing, so a release can't ship with one of them forgotten."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_top_changelog_entry_matches_version():
    version = _read("VERSION").strip()
    # headings look like "## 0.1.0 (unreleased)" or "## 0.2.0 - 2026-11-01"
    headings = re.findall(r"^## (\d+\.\d+\.\d+)\b", _read("CHANGELOG.md"), re.M)
    assert headings, "CHANGELOG.md has no '## <version>' heading"
    assert headings[0] == version, f"top CHANGELOG entry is {headings[0]}, VERSION says {version}"
