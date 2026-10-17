from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A free-form shell/command task is split by Ansible before it runs: an
# apostrophe in a comment of the script ("the RPM's ...") fails the whole
# playbook at parse time ("unbalanced jinja2 block or quotes").

import glob
import os

import pytest
import yaml

from ansible.parsing.splitter import split_args

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
FILES = sorted(f for pattern in ("playbooks/*.yml", "roles/*/tasks/*.yml")
               for f in glob.glob(os.path.join(ROOT, pattern)))
FREE_FORM = ("ansible.builtin.shell", "ansible.builtin.command", "shell", "command")


def free_form(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FREE_FORM and isinstance(value, str):
                yield value
            else:
                yield from free_form(value)
    elif isinstance(node, list):
        for item in node:
            yield from free_form(item)


class Loader(yaml.SafeLoader):
    """Safe loader that also reads Ansible's tags (!unsafe, !vault)."""


Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                             if isinstance(node, yaml.ScalarNode) else None)


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.relpath(p, ROOT))
def test_free_form_scripts_split(path):
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=Loader)  # nosec: safe loader
    for script in free_form(data):
        split_args(script)
