from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config: cassandra-rackdc.properties' other settings (cassandra_rackdc_extra_settings): written key=value
# after prefer_local, as Java's Properties reads them back; refused when they would not be.

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import load_role, render_template
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import properties_settings
from ansible_collections.community.cassandra.tests.unit.roles.test_config_declared import run

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}
CHECK = "Refuse rackdc extra settings the file cannot hold"


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_written_after_prefer_local(series):
    env, ctx, dummy = load_role(series, FACTS)
    ctx.update(cassandra_rackdc_dc="dc1", cassandra_rackdc_rack="r1",
               cassandra_rackdc_extra_settings={"dc_suffix": "_zorglub", "ec2_naming_scheme": "legacy", "x.y-z": 3})
    lines = render_template(env, series, "cassandra-rackdc.properties", ctx)[1]
    at = lines.index("# prefer_local=true")
    assert lines[at + 1:at + 4] == ["dc_suffix=_zorglub", "ec2_naming_scheme=legacy", "x.y-z=3"]
    assert properties_settings("\n".join(lines)) == {"dc": "dc1", "rack": "r1", "dc_suffix": "_zorglub",
                                                     "ec2_naming_scheme": "legacy", "x.y-z": "3"}


@pytest.mark.parametrize("settings, wrong", [
    ({}, None),
    ({"dc_suffix": "_x", "ec2_naming_scheme": "legacy", "metadata_url": "http://h-n1:80/a b"}, None),
    ({"dc_suffix": ""}, None),
    ({"dc": "dc9"}, "dc"),
    ({"prefer_local": "true"}, "prefer_local"),
    ({"a b": "x"}, "a b"),
    ({"a:b": "x"}, "a:b"),
    ({"dc_suffix": " _x"}, "dc_suffix"),
    ({"dc_suffix": "_x\\"}, "dc_suffix"),
    ({"dc_suffix": "_x\ny=1"}, "dc_suffix"),
    ({"dc_suffix": ["_x"]}, "dc_suffix"),
    (["dc_suffix=_x"], "(not a dict)"),
])
def test_refused_when_the_file_would_not_read_them_back(settings, wrong):
    passed, msg = run(CHECK, "50x", cassandra_rackdc_extra_settings=settings)
    assert passed is (wrong is None)
    if wrong:
        assert msg.startswith("cassandra_rackdc_extra_settings: %s: a key of letters" % wrong)


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_a_boolean_given_as_text_is_read_as_the_boolean(series):
    # (audit rc3 r4 B2: cassandra_prefer_local "false" from an INI file turned prefer_local on)
    env, ctx, dummy = load_role(series, FACTS)
    ctx.update(cassandra_rackdc_dc="dc1", cassandra_rackdc_rack="r1", cassandra_prefer_local="false")
    assert "# prefer_local=true" in render_template(env, series, "cassandra-rackdc.properties", ctx)[1]
    ctx.update(cassandra_prefer_local="true")
    assert "prefer_local=true" in render_template(env, series, "cassandra-rackdc.properties", ctx)[1]
