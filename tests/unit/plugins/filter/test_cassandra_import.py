from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config_ignored_vars: the cassandra_config variables a series does not use (the upgrade playbook warns
# about the ones the inventory sets).

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_ignored_vars


def test_config_vars_the_target_series_ignores():
    names = ["cassandra_read_request_timeout_in_ms", "cassandra_read_request_timeout", "cassandra_heap_size",
             "cassandra_seeds", "some_other_var"]
    assert cassandra_config_ignored_vars(names, "41x") == ["cassandra_read_request_timeout_in_ms"]
    assert cassandra_config_ignored_vars(names, "40x") == ["cassandra_read_request_timeout"]


def test_an_unknown_series_is_refused():
    with pytest.raises(AnsibleFilterError, match="unsupported series 60x"):
        cassandra_config_ignored_vars(["cassandra_seeds"], "60x")
