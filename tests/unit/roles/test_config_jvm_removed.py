from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config: stock option lines of the JVM options files switched off (cassandra_jvm_removed_options for
# jvm-server.options, cassandra_jvm<N>_removed_options for jvm<N>-server.options): written commented out; refused when
# the file has no such line.

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import load_role, render_template
from ansible_collections.community.cassandra.tests.unit.roles.test_config_declared import run

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}
CHECK = "Refuse removed JVM options the role does not write"


@pytest.mark.parametrize("series, name", [("40x", "jvm-server.options"), ("41x", "jvm-server.options"),
                                          ("50x", "jvm-server.options"), ("50x", "jvm17-server.options")])
def test_written_commented_out(series, name):
    env, ctx, dummy = load_role(series, FACTS)
    tpl, lines = render_template(env, series, name, ctx)
    # (a stock line no other rule writes: the first of its file; the GC lines are cassandra_jvm_gc's)
    var = "cassandra_%s_removed_options" % name.split("-")[0]
    options = [line for t, line in zip(tpl, lines) if var in t]
    ctx[var] = [options[0], "-XX:+HeapDumpOnOutOfMemoryError"]
    after = render_template(env, series, name, ctx)[1]
    assert len(after) == len(lines)
    assert [a for a, b in zip(after, lines) if a != b] == ["#" + o for o in options if o in ctx[var]]
    # (the other files: as they are)
    for other in [n for n in ("jvm-server.options", "jvm11-server.options", "jvm17-server.options") if n != name]:
        assert render_template(env, "50x", other, ctx)[1] == render_template(env, "50x", other, dict(ctx, **{var: []}))[1]


@pytest.mark.parametrize("version, var, removed, unknown", [
    ("50x", "cassandra_jvm_removed_options", [], None),
    ("50x", "cassandra_jvm_removed_options", ["-XX:+HeapDumpOnOutOfMemoryError", "-Djava.net.preferIPv4Stack=true"], None),
    ("50x", "cassandra_jvm11_removed_options", ["--add-exports java.base/jdk.internal.util=ALL-UNNAMED"], None),
    ("40x", "cassandra_jvm8_removed_options", ["-XX:+PrintGCDetails"], None),
    ("50x", "cassandra_jvm_removed_options", ["-XX:+UseNUMA=1"], "-XX:+UseNUMA=1"),
    ("50x", "cassandra_jvm17_removed_options", ["-XX:+UseG1GC"], "-XX:+UseG1GC"),  # (cassandra_jvm_gc writes it)
    ("50x", "cassandra_jvm17_removed_options", ["-XX:+UseNUMA"], "-XX:+UseNUMA"),  # (jvm-server.options')
    ("40x", "cassandra_jvm_removed_options", ["-XX:+PrintGCDetails"], "-XX:+PrintGCDetails"),  # (jvm8-server.options')
])
def test_refused_when_the_file_has_no_such_line(version, var, removed, unknown):
    passed, msg = run(CHECK, version, **{var: removed})
    assert passed is (unknown is None)
    if unknown:
        assert msg.startswith("%s: %s: not a stock line of that file of %s" % (var, unknown, version))
