from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Each Python file of plugins/ says who holds its copyright and its license
# (GPL v3.0+) in its first lines: the collection's two lines, or its author's
# (the modules written before them). An edit that drops them is caught here:
# sanity does not check filter plugins for it.

import os

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

COLLECTION = ["# Copyright: Contributors to the community.cassandra collection",
              "# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)"]
GPL = "# GNU General Public License v3.0+"

# without a header, as upstream has them: the empty package files, the module
# utils and doc fragment written that way, a file a script generates
NO_HEADER = [
    "plugins/doc_fragments/nodetool_module_options.py",
    "plugins/module_utils/__init__.py",
    "plugins/module_utils/cassandra_common_options.py",
    "plugins/module_utils/nodetool_cmd_objects.py",
    "plugins/module_utils/nodetool_status.py",
    "plugins/plugin_utils/__init__.py",
    "plugins/plugin_utils/cassandra_yaml_properties.py",  # (tools/gen_yaml_properties.py)
]


def header(path):
    """the comment lines a file starts with, its shebang, coding line and blank lines left out"""
    lines = []
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#!") or line.startswith("# -*- coding") or (not line.strip() and not lines):
                continue
            if not line.startswith("#"):
                break
            lines.append(line)
    return lines


def python_files():
    found = []
    for root, dirs, files in os.walk(os.path.join(TOP, "plugins")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        found += [os.path.relpath(os.path.join(root, f), TOP).replace(os.sep, "/") for f in files if f.endswith(".py")]
    return sorted(found)


def test_each_python_file_of_plugins_has_its_copyright_and_license_lines():
    missing = []
    for path in python_files():
        if path in NO_HEADER:
            continue
        lines = header(path)
        collection = lines[:2] == COLLECTION
        # an author's: "# 2019 Rhys Campbell <...>" or "# Copyright: (c) 2019, ...", then the license
        author = bool(lines) and (lines[0][2:6].isdigit() or lines[0].startswith("# Copyright: (c) "))
        if not collection and not (author and any(line.startswith(GPL) for line in lines)):
            missing.append(path)
    assert missing == []


def test_the_files_without_a_header_are_still_so():
    # the list above exact: a file of it given a license line (or gone) leaves it
    assert [p for p in NO_HEADER if p not in python_files() or any(line.startswith(GPL) for line in header(p))] == []


def test_a_coding_line_before_the_collection_lines_is_left_out(tmp_path):
    # path absolute: os.path.join(TOP, path) keeps it as it is
    f = tmp_path / "f.py"
    f.write_text("\n".join(["# -*- coding: utf-8 -*-"] + COLLECTION + ["", "import os", ""]), encoding="utf-8")
    assert header(str(f)) == COLLECTION
