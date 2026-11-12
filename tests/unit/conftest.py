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


def _not_plain(value):
    """A str of another type in a report's data (before 2.19, Ansible's AnsibleUnsafeText)."""
    if isinstance(value, dict):
        return any(_not_plain(k) or _not_plain(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_not_plain(v) for v in value)
    return isinstance(value, str) and value.__class__ is not str


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # a failure whose traceback holds such a str cannot be sent from an xdist worker (can't serialize), which
    # stops the whole run and hides the failure: sent as its text instead
    report = (yield).get_result()
    if hasattr(item.config, "workerinput") and report.failed and _not_plain(report._to_json()):
        report.longrepr = str(report.longrepr)
