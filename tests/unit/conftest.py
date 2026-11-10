from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The ansible-playbook runs of the tests print without colours: ansible-test
# runs pytest with --color yes, which sets ANSIBLE_FORCE_COLOR (stronger than
# ANSIBLE_NOCOLOR) for every process it starts. A colour test sets its own.
# Nor ansible-core's deprecation warnings (of its own, or of a library): the
# outputs compared are the collection's.

import pytest


@pytest.fixture(autouse=True)
def no_color(monkeypatch):
    monkeypatch.delenv("ANSIBLE_FORCE_COLOR", raising=False)
    monkeypatch.setenv("ANSIBLE_NOCOLOR", "1")
    monkeypatch.setenv("ANSIBLE_DEPRECATION_WARNINGS", "False")
