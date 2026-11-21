from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import tempfile
import warnings

import pytest

try:
    from ansible.plugins.loader import init_plugin_loader
    from ansible.utils.collection_loader import AnsibleCollectionConfig
except ImportError:  # the tests of the modules: ansible-test gives them ansible.module_utils only
    AnsibleCollectionConfig = None

# Under plain pytest, the collection loader before any test file imports from ansible_collections (ansible-test
# units sets it up itself): this checkout first, whichever test file comes first
if AnsibleCollectionConfig is not None and AnsibleCollectionConfig.collection_finder is None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        init_plugin_loader([os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))])


def pytest_addoption(parser):
    parser.addoption("--regen", action="store_true",
                     help="write the golden files again from the outputs (helpers.golden), then review git diff")


def pytest_configure(config):
    config.addinivalue_line("markers", "playbook: runs ansible-playbook (slow); -m 'not playbook' leaves them out")
    if config.getoption("--regen"):
        os.environ["CASSANDRA_GOLDEN_WRITE"] = "1"
    # the golden files rewritten, one name a line (helpers.golden), from the xdist workers too: they inherit it
    if os.environ.get("CASSANDRA_GOLDEN_WRITE") == "1" and not os.environ.get("CASSANDRA_GOLDEN_LOG"):
        handle, os.environ["CASSANDRA_GOLDEN_LOG"] = tempfile.mkstemp(prefix="cassandra-golden-")
        os.close(handle)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """A regen never passes silently: the number of golden files it rewrote, at the end of the run."""
    log = os.environ.get("CASSANDRA_GOLDEN_LOG")
    if os.environ.get("CASSANDRA_GOLDEN_WRITE") != "1" or not log or hasattr(config, "workerinput"):
        return
    with open(log, encoding="utf-8") as f:
        count = len(set(f.read().split()))
    os.remove(log)
    terminalreporter.write_line("%d golden file%s rewritten (CASSANDRA_GOLDEN_WRITE=1): review git diff"
                                % (count, "" if count == 1 else "s"))


@pytest.fixture(autouse=True)
def playbook_marked(request, monkeypatch):
    """helpers.run_playbook refuses to run in a test not marked playbook (the mark would be missing)."""
    marked = request.node.get_closest_marker("playbook") is not None
    monkeypatch.setenv("CASSANDRA_TEST_PLAYBOOK_MARK", "yes" if marked else "no")


# The ansible-playbook runs of the tests print without colours: ansible-test
# runs pytest with --color yes, which sets ANSIBLE_FORCE_COLOR (stronger than
# ANSIBLE_NOCOLOR) for every process it starts. A colour test sets its own.
# Nor ansible-core's deprecation warnings (of its own, or of a library): the
# outputs compared are the collection's.
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
