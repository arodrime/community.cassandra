from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install reads the installed packages by name (_cassandra_install_read_names), not with
# package_facts: every package its tasks look for in the snapshot must be among the names read, else the
# role would take an installed package for a missing one.

import glob
import os
import re

import yaml

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install")
# a name looked for in the snapshot: 'name' in, ['a', 'b'] | reject('in', ...), snapshot['name']
_LITERAL = (re.compile(r"'([\w.+-]+)' (?:not )?in _cassandra_install_(?:before|now)"),
            re.compile(r"\[((?:'[\w.+-]+',? ?)+)\] \| reject\('in', _cassandra_install_(?:before|now)"),
            re.compile(r"_cassandra_install_(?:before|now)\['([\w.+-]+)'\]"))
# a variable looked for in it: var in, var | reject('in', ...), snapshot[var]
_VARIABLE = (re.compile(r"(?<![\w'])(\[?\w+\]?) (?:not )?in _cassandra_install_(?:before|now)"),
             re.compile(r"(?<![\w'])(\[?\w+\]?)(?: \| flatten)? \| reject\('in', _cassandra_install_(?:before|now)"),
             re.compile(r"_cassandra_install_(?:before|now)\[(\w+)\]"))
# each made of names read: the role's packages (with a version: never among the names, as package_facts gives
# them), Java's package, jemalloc's, what cqlsh_python.yml needs, the new ones of report.yml
KNOWN = {"_cassandra_packages", "_cassandra_install_names", "item", "p", "_jemalloc", "_needed",
         "cassandra_java_package", "[cassandra_java_package]", "_java_package"}


def code():
    for path in sorted(glob.glob(os.path.join(ROLE, "tasks", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.lstrip().startswith("#"):
                    yield line.split("  # ")[0]


def test_every_package_looked_for_is_read():
    with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as f:
        others = yaml.safe_load(f)["_cassandra_install_read_others"]
    literal = set(n for line in code() for pattern in _LITERAL for m in pattern.finditer(line)
                  for n in re.findall(r"[\w.+-]+", m.group(1)))
    assert literal and literal <= set(others), sorted(literal - set(others))
    variables = set(m.group(1) for line in code() for pattern in _VARIABLE for m in pattern.finditer(line)
                    if m.group(1) != "not")
    assert variables <= KNOWN, sorted(variables - KNOWN)
