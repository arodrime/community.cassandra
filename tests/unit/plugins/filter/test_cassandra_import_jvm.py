from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster and the JVM options: a jvm<N>-server.options the running Java
# does not read (its options not imported, the heap there included), an option
# the role's template writes already, and the options worth a warning.

import os
import sys

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    _load_role, _render, cassandra_config_import, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _compare_files
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from import_nodes import node as import_node  # noqa: E402 pylint: disable=wrong-import-position

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}}
CMDLINE = "/usr/bin/java\0-ea\0-Xss256k\0-Xms31G\0-Xmx31G\0org.apache.cassandra.service.CassandraDaemon"


def node_files(series, **changes):
    """The config files cassandra_config writes with these variables."""
    env, ctx, files = _load_role(series, FACTS)
    ctx.update(changes)
    return {name: "\n".join(_render(env, series, name, ctx)[1]) for name in files}


def heap_in_jvm11_on_java17():
    """A 5.0 node on Java 17: its heap set in jvm11-server.options (not read), the real one in cassandra-env.sh."""
    return node_files("50x", cassandra_jvm11_extra_options=["-Xms24G", "-Xmx24G"], cassandra_heap_size="31G")


def test_options_of_a_file_the_java_does_not_read_are_dropped():
    out = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", cmdline=CMDLINE)
    assert "cassandra_jvm11_extra_options" not in out["vars"]
    assert out["vars"]["cassandra_heap_size"] == "31G"  # the heap the JVM runs with
    assert out["keep_files"] == ["jvm11-server.options"]  # the node's file left as it is: nothing to restart for
    assert out["jvm_notes"] == [
        "jvm11-server.options: -Xms24G, -Xmx24G not read by Java 17 (it reads jvm-server.options and"
        " jvm17-server.options): not imported, nodes added later get the role's file. They stay in this node's file,"
        " left as it is (cassandra_config_keep_files): they would apply on a move to the Java that reads it"
        " (update_java and upgrade refuse it until the file is out of cassandra_config_keep_files). To import them anyway: -e"
        " import_cluster_keep_unread_jvm_options=true",
        "Heap: -Xms24G (jvm11-server.options), -Xmx24G (jvm11-server.options), in a file Java 17 does not read, is"
        " not the one the JVM runs with (-Xms31G -Xmx31G): that comes from cassandra_heap_size 31G (cassandra-env.sh's"
        " MAX_HEAP_SIZE)"]
    assert out["jvm_warnings"] == []


def test_the_heap_note_without_a_heap_in_cassandra_env_sh():
    files = node_files("50x", cassandra_jvm11_extra_options=["-Xmx24G -Xms24G"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert "cassandra_heap_size" not in out["vars"]
    assert out["jvm_notes"][1] == (
        "Heap: -Xmx24G (jvm11-server.options), -Xms24G (jvm11-server.options), in a file Java 17 does not read, is not"
        " the one the JVM runs with: that comes from the unit's MAX_HEAP_SIZE if it sets one, else cassandra-env.sh's"
        " automatic sizing (Java 17)")


def test_the_heap_note_names_a_heap_in_a_file_the_java_reads():
    # cassandra-env.sh adds MAX_HEAP_SIZE's only without an -Xmx in the options files read
    files = node_files("50x", cassandra_jvm11_extra_options=["-Xmx24G"], cassandra_jvm17_extra_options=["-Xms16G",
                       "-Xmx16G"], cassandra_heap_size="31G")
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["jvm_notes"][1].endswith("that comes from -Xms16G (jvm17-server.options), -Xmx16G"
                                        " (jvm17-server.options)")


@pytest.mark.parametrize("read_heap", [["-Xmn2G"], ["-XX:MaxHeapSize=8g", "-XX:InitialHeapSize=8g"]])
def test_the_heap_note_names_cassandra_env_sh_unless_both_xmx_and_xms_are_read(read_heap):
    # cassandra-env.sh greps for Xmx and Xms: with only one of them, or the -XX spellings, it adds its own
    files = node_files("50x", cassandra_jvm11_extra_options=["-Xmx24G"], cassandra_jvm17_extra_options=read_heap,
                       cassandra_heap_size="31G")
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["jvm_notes"][1].endswith("that comes from cassandra_heap_size 31G (cassandra-env.sh's MAX_HEAP_SIZE)")


def test_the_heap_note_with_one_of_xmx_xms_read():
    files = node_files("50x", cassandra_jvm11_extra_options=["-Xmx24G"], cassandra_jvm17_extra_options=["-Xmx8G"],
                       cassandra_heap_size="31G")
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["jvm_notes"][1].endswith("that comes from -Xmx8G (jvm17-server.options) with the other of -Xmx/-Xms"
                                        " given another way (cassandra-env.sh refuses one of them alone in the options"
                                        " files)")


def test_options_of_a_file_the_java_does_not_read_kept_when_asked():
    out = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", keep_unread_jvm_options=True)
    assert out["vars"]["cassandra_jvm11_extra_options"] == ["-Xms24G", "-Xmx24G"]
    assert out["keep_files"] == []
    assert out["jvm_notes"] == ["jvm11-server.options: -Xms24G, -Xmx24G not read by Java 17 (it reads"
                                " jvm-server.options and jvm17-server.options): imported as"
                                " cassandra_jvm11_extra_options all the same (import_cluster_keep_unread_jvm_options)"]


@pytest.mark.parametrize("series, java, read, unread", [
    ("50x", "11", "cassandra_jvm11_extra_options", "cassandra_jvm17_extra_options"),
    ("41x", "8", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options"),
    ("41x", "11", "cassandra_jvm11_extra_options", "cassandra_jvm8_extra_options"),
    # 4.x reads jvm11 on any Java after 8
    ("40x", "17", "cassandra_jvm11_extra_options", "cassandra_jvm8_extra_options"),
])
def test_the_file_the_java_reads_is_imported(series, java, read, unread):
    files = node_files(series, **{read: ["-Dread=1"], unread: ["-Dunread=1"]})
    out = cassandra_config_import(files, series, FACTS, java=java)
    assert out["vars"][read] == ["-Dread=1"]
    assert unread not in out["vars"]


def test_without_the_java_every_file_is_imported_as_before():
    out = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS)
    assert out["vars"]["cassandra_jvm11_extra_options"] == ["-Xms24G", "-Xmx24G"]
    assert out["jvm_notes"] == [] and out["keep_files"] == []


def test_a_secret_option_dropped_is_masked():
    files = node_files("50x", cassandra_jvm11_extra_options=["-Djavax.net.ssl.keyStorePassword=S3cr3t"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert "S3cr3t" not in "\n".join(out["jvm_notes"])


def test_a_stack_of_1024m_is_imported_with_a_warning():
    files = node_files("50x", cassandra_jvm_extra_options=["-Xss1024M"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm_extra_options"] == ["-Xss1024M"]  # never fixed silently
    assert out["jvm_warnings"] == [
        "-Xss1024M (cassandra_jvm_extra_options, from jvm-server.options, Java 17): 1 GiB of stack for every thread,"
        " far above the usual 256k to 1m (stock -Xss256k; M typed for k?): each of Cassandra's hundreds to thousands"
        " of threads reserves that much, new threads may fail (unable to create native thread). Fix or remove it in"
        " cassandra_jvm_extra_options: it is kept as it is until then."]


def test_biased_locking_on_java_17_is_a_warning():
    files = node_files("50x", cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm17_extra_options"] == ["-XX:-UseBiasedLocking"]
    assert len(out["jvm_warnings"]) == 1
    assert out["jvm_warnings"][0].startswith("-XX:-UseBiasedLocking (cassandra_jvm17_extra_options, from"
                                             " jvm17-server.options, Java 17): biased locking is deprecated")
    # Java 11 reads 5.0's stock jvm11 line: nothing to say
    assert cassandra_config_import(node_files("50x"), "50x", FACTS, java="11")["jvm_warnings"] == []


def test_an_option_the_template_writes_is_not_carried_again():
    files = node_files("50x")
    lines = files["jvm-server.options"].split("\n")
    lines.remove("-Xss256k")
    lines.append("-Xss256k")  # moved to the end
    files["jvm-server.options"] = "\n".join(lines)
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert "cassandra_jvm_extra_options" not in out["vars"]
    assert out["jvm_notes"] == ["jvm-server.options: -Xss256k, which its template writes already: not carried in"
                                " cassandra_jvm_extra_options"]
    # the same options: cassandra_config leaves the file as it is, no hand edit to revert
    assert out["hand_edits"] == []
    assert "jvm-server.options: the same options, some at another place: left as it is (cassandra_config_normalize:" \
           " true rewrites it)" in out["normalized"]


@pytest.mark.parametrize("line", ["-ea", "-da:net.openhft..."])
def test_an_option_without_a_name_is_carried_again(line):
    # the template writes it, but the JVM gets each of them: given twice, twice (dropped by the first version)
    out = cassandra_config_import(node_files("50x", cassandra_jvm_extra_options=[line]), "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm_extra_options"] == [line]
    assert out["hand_edits"] == [] and out["jvm_notes"] == []


def test_a_comment_is_not_an_option_moved():
    files = node_files("50x")
    files["jvm-server.options"] = "# my own note\n" + files["jvm-server.options"]
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert not [n for n in out["normalized"] if "some at another place" in n]


def test_an_option_set_again_with_another_value_is_carried():
    out = cassandra_config_import(node_files("50x", cassandra_jvm_extra_options=["-Xss512k"]), "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm_extra_options"] == ["-Xss512k"]


def test_an_option_of_another_file_is_carried():
    # in jvm17-server.options: its template does not write it (bin/cassandra passes both, the self-check compares
    # each file)
    out = cassandra_config_import(node_files("50x", cassandra_jvm17_extra_options=["-Xss256k"]), "50x", FACTS,
                                  java="17")
    assert out["vars"]["cassandra_jvm17_extra_options"] == ["-Xss256k"]


def test_the_node_keeps_the_file_and_the_report_says_why():
    config = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", cmdline=CMDLINE)
    hv = {"import_cluster_config": config, "import_cluster_series": "50x", "import_cluster_java": "17"}
    entry = import_node(hv)
    assert entry["keep"]["cassandra_config_keep_files"] == ["jvm11-server.options"]
    assert config["jvm_notes"][0] in entry["notes"]
    # with the hand edits kept: both
    config_edits = dict(config, hand_edits=["cassandra.yaml, line 3:", "  + x: 1"] + config["hand_edits"])
    entry = import_node(dict(hv, import_cluster_config=config_edits), keep_hand_edits=True)
    assert entry["keep"]["cassandra_config_keep_files"] == ["cassandra.yaml", "jvm11-server.options"]


def test_the_warnings_go_to_the_todo_once_for_all_the_nodes():
    config = cassandra_config_import(node_files("50x", cassandra_jvm_extra_options=["-Xss1024M"]), "50x", FACTS,
                                     java="17")
    nodes = []
    for i in (1, 2):
        entry = import_node({"import_cluster_config": config, "import_cluster_series": "50x",
                             "import_cluster_java": "17"})
        nodes.append(dict(entry, name="node%d" % i, address="10.0.0.%d" % i))
    assert not [n for n in nodes[0]["notes"] if "WARNING" in n]  # in the TO DO only, as the rejected keys
    layout = cassandra_inventory_layout(nodes, "c")
    lines = cassandra_import_report(layout, ["c.yml", "."], "report.txt", {}, True, width=400, in_git=False)
    todo = [line for line in lines if ". WARNING" in line]
    assert todo == ["  1. WARNING  node1, node2: " + config["jvm_warnings"][0]]


def test_self_check_names_the_file_a_41_on_java_17_reads():
    # 4.1 on Java 17 reads jvm11-server.options: missing on the node, a difference; jvm8's, a note
    rendered = {"jvm8-server.options": "-Da=1\n", "jvm11-server.options": "-Db=1\n"}
    out = _compare_files(rendered, {}, java="17", series="41x")
    assert out["differences"] == ["jvm11-server.options: not on the node, the roles would create it"]
    assert out["notes"] == ["jvm8-server.options: not on the node, the roles would create it (Java 17 does not read it)"]
