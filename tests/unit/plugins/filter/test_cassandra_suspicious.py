from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The sensitive or suspicious settings preflight and the import report warn about (a table: one row per setting).

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import (
    SUSPICIOUS, cassandra_suspicious_settings)


@pytest.mark.parametrize("value, warned", [(False, True), ("false", True), ("no", True), (0, True), (True, False),
                                           (None, False), ("true", False), ("", False), ("maybe", False),
                                           ("{{ undefined_x }}", False)])  # a template not resolved: not a false
def test_auto_bootstrap(value, warned):
    lines = cassandra_suspicious_settings({"node4": {"cassandra_auto_bootstrap": value}, "node5": {}})
    assert lines == ([u"WARNING  node4: auto_bootstrap: false — a node that loses its data restarts empty without"
                      u" streaming it back; remove the setting (default true) unless you know why it is there"]
                     if warned else [])


def test_one_line_per_setting_with_its_nodes():
    nodes = dict(("node%d" % i, {"cassandra_auto_bootstrap": False}) for i in (1, 2, 3))
    assert len(cassandra_suspicious_settings(nodes)) == 1 and cassandra_suspicious_settings(nodes)[0].startswith(
        "WARNING  node1..node3: ")
    assert cassandra_suspicious_settings({}) == [] and cassandra_suspicious_settings(None) == []


def test_every_row_is_a_known_variable():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import _role_variables
    assert [row[0] for row in SUSPICIOUS if row[0] not in _role_variables()] == []
