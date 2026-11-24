# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Import rules no other test pinned at their own level: the young generation of a heap in the JVM options files,
# a templated collection default, a line with a template mark among the hand edits, and the lists of lines whose
# secrets go to secrets.yml.

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_import_files as files
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_import_jvm as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_import_layout as layout


def test_xmn_in_the_jvm_options_is_cassandra_heap_newsize():
    found = {"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn2G", "-Dx=1"]}
    live = {"jvm11-server.options": "-XX:+UseConcMarkSweepGC\n-Xms8G\n-Xmx8G\n-Xmn2G\n-Dx=1"}
    assert jvm.heap_in_options(found, "41x", "11", live) == {"jvm11-server.options": ["-Xms", "-Xmx", "-Xmn"]}
    assert found == {"cassandra_jvm11_extra_options": ["-Dx=1"], "cassandra_heap_size": "8G",
                     "cassandra_heap_newsize": "2G", "cassandra_heap_in_jvm_options": True}


def test_xmn_with_g1_leaves_the_heap_in_the_options():
    # (cassandra-env.sh stops on -Xmn with G1: nothing moved)
    found = {"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn2G"]}
    assert jvm.heap_in_options(found, "41x", "11", {"jvm11-server.options": "-XX:+UseG1GC\n-Xms8G"}) == {}
    assert found == {"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn2G"]}


@pytest.mark.parametrize("value, defaults, is_default", [
    (layout.MISSING, {}, True),                         # not set
    ("1", {"k": "1"}, True),                             # the default, a value
    ("2", {"k": "1"}, False),
    ("{{ x }}", {"k": "{{ x }}"}, False),                # a default made of other variables: never the node's text
    ("1", {}, False),                                    # no default
])
def test_a_templated_default_is_not_a_value(value, defaults, is_default):
    assert layout._is_default("k", value, defaults) is is_default


def test_a_line_with_a_template_mark_keeps_its_secret():
    # (a line of the node's with {{: left as it is, its list then goes to secrets.yml whole)
    lines = ["-Dx={{ y }} -Dtrust.password=abc", "-Djavax.net.ssl.keyStorePassword=jvmP4ss"]
    out, made, said = files.move_secrets({"cassandra_jvm_extra_options": lines})
    assert out == {"cassandra_jvm_extra_options": [
        "-Dx={{ y }} -Dtrust.password=abc",
        "-Djavax.net.ssl.keyStorePassword={{ cassandra_secret_jvm_javax_net_ssl_keystorepassword }}"],
        "cassandra_secret_jvm_javax_net_ssl_keystorepassword": "jvmP4ss"}
    assert made == ["-Djavax.net.ssl.keyStorePassword={{ cassandra_secret_jvm_javax_net_ssl_keystorepassword }}"]
    assert files.list_secret("cassandra_jvm_extra_options", lines)


# the lists of lines whose secrets go to secrets.yml, and the prefix of their secrets' variables
SECRET_LISTS = [("(cassandra-env.sh)", "env"), ("(logback.xml)", "logback"), ("(logback.xml:appenders)", "logback_appenders"),
                ("cassandra_jvm_extra_options", "jvm"), ("cassandra_jvm8_extra_options", "jvm8"),
                ("cassandra_jvm11_extra_options", "jvm11"), ("cassandra_jvm17_extra_options", "jvm17")]


@pytest.mark.parametrize("key, prefix", SECRET_LISTS)
def test_each_list_of_lines_moves_its_secrets(key, prefix):
    out, made, said = files.move_secrets({key: ["-Dstore.password=s3cr3t"], "cassandra_other": ["-Dstore.password=x"]})
    var = "cassandra_secret_%s_store_password" % prefix
    assert out == {key: ["-Dstore.password={{ %s }}" % var], var: "s3cr3t", "cassandra_other": ["-Dstore.password=x"]}
    assert files.list_secret(key, ["-Dstore.password=s3cr3t"]) and not files.list_secret("cassandra_other", ["x"])


def test_no_other_list_of_lines_moves_its_secrets():
    assert [key for key, dummy in files.SECRET_LISTS] == [key for key, dummy in SECRET_LISTS]


def test_the_import_knows_the_roles_env_marker():
    # the import drops the role's marker from the lines it carries: the same text as the role writes
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import ENV_MARKER
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "vars", "main.yml")
    with open(path) as f:
        assert yaml.safe_load(f)["_cassandra_config_env_marker"] == ENV_MARKER
