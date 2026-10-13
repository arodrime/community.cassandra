from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The names of the -e variables, as ansible-playbook parsed them: key=value,
# YAML/JSON and @file alike; nothing from the inventory.

import pytest

from ansible import context
from ansible.module_utils.common.collections import ImmutableDict
from ansible.parsing.dataloader import DataLoader
from ansible.utils import vars as ansible_vars

from ansible_collections.community.cassandra.plugins.lookup.cassandra_extra_vars import LookupModule


@pytest.fixture
def extra_vars(monkeypatch):
    def given(*options):
        monkeypatch.setattr(context, "CLIARGS", ImmutableDict(extra_vars=options))
        # ansible-core keeps what it parsed first: as for a new run
        monkeypatch.setattr(ansible_vars.load_extra_vars, "extra_vars", None, raising=False)
        return LookupModule(loader=DataLoader()).run([])
    return given


def test_every_form(extra_vars, tmp_path):
    vars_file = tmp_path / "vars.yml"
    vars_file.write_text("help_topic: status\n")
    assert extra_vars("cassandra_hosts=prod import_cluster_keep_hand_edit=true", '{"cassandra_target_nodes": "n1"}',
                      "@%s" % vars_file) == [
        "cassandra_hosts", "cassandra_target_nodes", "help_topic", "import_cluster_keep_hand_edit"]


def test_none(extra_vars):
    assert extra_vars() == []
