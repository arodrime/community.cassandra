# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra.yaml with one token per node (num_tokens: 1): initial_token
written, no vnode-only setting active; and import_cluster reading it back."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    _load_role, _render, cassandra_config_import)

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}}
SERIES = ["40x", "41x", "50x"]
TOKEN = "-9223372036854775808"
DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "defaults", "main.yml")


def cassandra_yaml(series, **changes):
    env, ctx, dummy = _load_role(series, FACTS)
    ctx.update(changes)
    if "cassandra_num_tokens" in changes and "cassandra_allocate_tokens_for_local_replication_factor" not in changes:
        # the role default follows num_tokens (Ansible templates it lazily)
        with open(DEFAULTS) as f:
            raw = yaml.safe_load(f)["cassandra_allocate_tokens_for_local_replication_factor"]
        ctx["cassandra_allocate_tokens_for_local_replication_factor"] = env.from_string(raw).render(**ctx)
    return "\n".join(_render(env, series, "cassandra.yaml", ctx)[1])


def active(text, key):
    return re.findall(r"(?m)^%s:.*$" % key, text)


@pytest.mark.parametrize("series", SERIES)
def test_single_token_rendering(series):
    text = cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=TOKEN)
    conf = yaml.safe_load(text)
    assert conf["num_tokens"] == 1
    assert conf["initial_token"] == int(TOKEN)
    assert "allocate_tokens_for_local_replication_factor" not in conf
    assert "allocate_tokens_for_keyspace" not in conf
    assert "# allocate_tokens_for_local_replication_factor: 3" in text.split("\n")
    assert active(text, "initial_token") == ["initial_token: %s" % TOKEN]


@pytest.mark.parametrize("series", SERIES)
@pytest.mark.parametrize("token", [0, -2 ** 63, "42"])
def test_single_token_any_type(series, token):
    # a token written unquoted in the inventory is a number, 0 included
    conf = yaml.safe_load(cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=token))
    assert conf["initial_token"] == int(token)


@pytest.mark.parametrize("series", SERIES)
def test_vnodes_rendering_unchanged(series):
    text = cassandra_yaml(series)
    conf = yaml.safe_load(text)
    assert conf["num_tokens"] == 16
    assert conf["allocate_tokens_for_local_replication_factor"] == 3
    assert "initial_token" not in conf
    assert "# initial_token:" in text.split("\n")  # as in the stock file


@pytest.mark.parametrize("series", SERIES)
def test_single_token_without_token_yet(series):
    # create_cluster/add_node give it; without, the line stays commented out
    conf = yaml.safe_load(cassandra_yaml(series, cassandra_num_tokens=1))
    assert "initial_token" not in conf and "allocate_tokens_for_local_replication_factor" not in conf


@pytest.mark.parametrize("series", SERIES)
def test_single_token_hint_kept_when_set(series):
    # set explicitly (e.g. imported from a node that has it): written
    conf = yaml.safe_load(cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=TOKEN,
                                         cassandra_allocate_tokens_for_local_replication_factor=3))
    assert conf["allocate_tokens_for_local_replication_factor"] == 3


@pytest.mark.parametrize("series", SERIES)
def test_import_single_token_node(series):
    live = cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=TOKEN)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["hand_edits"] == [] and out["normalized"] == []
    assert out["vars"]["cassandra_num_tokens"] == 1
    assert out["vars"]["cassandra_initial_token"] == TOKEN
    assert out["vars"]["cassandra_allocate_tokens_for_local_replication_factor"] == ""
    assert "cassandra_extra_settings" not in out["vars"]


@pytest.mark.parametrize("series", SERIES)
def test_import_single_token_with_hint_left_active(series):
    # num_tokens set to 1 by hand in a stock file: the hint stays as it is
    live = cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=TOKEN,
                          cassandra_allocate_tokens_for_local_replication_factor=3)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["hand_edits"] == [] and out["normalized"] == []
    assert out["vars"]["cassandra_allocate_tokens_for_local_replication_factor"] == 3
    assert out["vars"]["cassandra_initial_token"] == TOKEN


@pytest.mark.parametrize("series", SERIES)
def test_null_token_and_hint_left_out(series):
    # `cassandra_initial_token:` left empty in host_vars is null: commented out, not "initial_token: None"
    text = cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=None,
                          cassandra_allocate_tokens_for_local_replication_factor=None)
    conf = yaml.safe_load(text)
    assert "initial_token" not in conf and "allocate_tokens_for_local_replication_factor" not in conf


@pytest.mark.parametrize("series", SERIES)
@pytest.mark.parametrize("line", ["initial_token: '%s'" % TOKEN, 'initial_token: "%s"' % TOKEN])
def test_import_quoted_token(series, line):
    live = cassandra_yaml(series, cassandra_num_tokens=1, cassandra_initial_token=TOKEN).replace(
        "initial_token: %s" % TOKEN, line)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"]["cassandra_initial_token"] == TOKEN
