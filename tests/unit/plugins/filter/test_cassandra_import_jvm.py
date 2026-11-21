from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster and the JVM options: a jvm<N>-server.options the running Java
# does not read (its options imported as they are), an option the role's
# template writes already, and the options worth a warning.

import os
import sys

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_config_import, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import load_role, render_template
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _compare_files
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report
from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_jvm_options import STACK_1G

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from import_nodes import node as import_node  # noqa: E402 pylint: disable=wrong-import-position

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}}
CMDLINE = "/usr/bin/java\0-ea\0-Xss256k\0-Xms31G\0-Xmx31G\0org.apache.cassandra.service.CassandraDaemon"


def node_files(series, **changes):
    """The config files cassandra_config writes with these variables."""
    env, ctx, files = load_role(series, FACTS)
    ctx.update(changes)
    return {name: "\n".join(render_template(env, series, name, ctx)[1]) for name in files}


def heap_in_jvm11_on_java17():
    """A 5.0 node on Java 17: its heap set in jvm11-server.options (not read), the real one in cassandra-env.sh."""
    return node_files("50x", cassandra_jvm11_extra_options=["-Xms24G", "-Xmx24G"], cassandra_heap_size="31G")


def test_options_of_a_file_the_java_does_not_read_are_imported():
    # the node's file as it is: the role writes it the same, the JVM keeps not reading it; nothing to say
    out = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", cmdline=CMDLINE)
    assert out["vars"]["cassandra_jvm11_extra_options"] == ["-Xms24G", "-Xmx24G"]
    assert out["vars"]["cassandra_heap_size"] == "31G"  # the heap the JVM runs with
    assert out["jvm_notes"] == [] and out["jvm_warnings"] == []
    assert "keep_files" not in out


def test_the_heap_imported_from_a_file_the_java_does_not_read_is_said_at_the_switch():
    # the move back to Java 11 would run that heap, the inventory's value: update_java says it in its
    # plan (it starts: not refused)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import (
        cassandra_jvm_switch_heap, cassandra_jvm_switch_refusals)
    imported = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", cmdline=CMDLINE)["vars"]
    assert cassandra_jvm_switch_refusals(imported, "50x", "11", "50x", "17") == []
    assert cassandra_jvm_switch_heap(imported, "50x", "11", "50x", "17") == [
        u"heap after the move: 24\xa0GiB (cassandra_jvm11_extra_options: -Xms24G -Xmx24G); inventory now: 31\xa0GiB"
        " (cassandra_heap_size)"]
    assert cassandra_jvm_switch_heap(imported, "50x", "21", "50x", "17") == []


@pytest.mark.parametrize("series, java, read, unread", [
    ("50x", "11", "cassandra_jvm11_extra_options", "cassandra_jvm17_extra_options"),
    ("41x", "8", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options"),
    ("41x", "11", "cassandra_jvm11_extra_options", "cassandra_jvm8_extra_options"),
    # 4.x reads jvm11 on any Java after 8
    ("40x", "17", "cassandra_jvm11_extra_options", "cassandra_jvm8_extra_options"),
])
def test_the_file_the_java_does_not_read_is_imported_too(series, java, read, unread):
    files = node_files(series, **{read: ["-Dread=1"], unread: ["-Dunread=1"]})
    out = cassandra_config_import(files, series, FACTS, java=java)
    assert out["vars"][read] == ["-Dread=1"]
    assert out["vars"][unread] == ["-Dunread=1"]
    assert out["jvm_notes"] == []


def test_without_the_java_every_file_is_imported_as_before():
    out = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS)
    assert out["vars"]["cassandra_jvm11_extra_options"] == ["-Xms24G", "-Xmx24G"]
    assert out["jvm_notes"] == []


def test_a_stack_of_1024m_is_imported_with_a_warning():
    files = node_files("50x", cassandra_jvm_extra_options=["-Xss1024M"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm_extra_options"] == ["-Xss1024M"]  # never fixed silently
    assert out["jvm_warnings"] == [
        "-Xss1024M (cassandra_jvm_extra_options, from jvm-server.options, Java 17): " + STACK_1G + ". Fix or remove"
        " it in cassandra_jvm_extra_options: it is kept as it is until then."]


def test_a_stack_set_twice_is_flagged_with_the_one_the_jvm_takes():
    # (2026-11-02) -Xss1024M in jvm-server.options, -Xss256k in jvm17-server.options: the 256k wins, the 1024M is dead
    files = node_files("50x", cassandra_jvm_extra_options=["-Xss1024M"], cassandra_jvm17_extra_options=["-Xss256k"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["jvm_warnings"] == [
        "-Xss1024M (cassandra_jvm_extra_options, from jvm-server.options, Java 17): set again by -Xss256k"
        " (cassandra_jvm17_extra_options, from jvm17-server.options), which the JVM takes: this one is never used;"
        " if it ever applied, " + STACK_1G + ". Remove it from cassandra_jvm_extra_options."]
    # a password option set twice: neither value shown
    files = node_files("50x", cassandra_jvm_extra_options=["-Djavax.net.ssl.keyStorePassword=FirstS3cret"],
                       cassandra_jvm17_extra_options=["-Djavax.net.ssl.keyStorePassword=OtherS3cret"])
    warnings = cassandra_config_import(files, "50x", FACTS, java="17")["jvm_warnings"]
    assert len(warnings) == 1 and "S3cret" not in warnings[0] and "keyStorePassword=****" in warnings[0]
    # the same option twice with the same value: nothing to say
    files = node_files("50x", cassandra_jvm_extra_options=["-Xss512k"], cassandra_jvm17_extra_options=["-Xss512k"])
    assert cassandra_config_import(files, "50x", FACTS, java="17")["jvm_warnings"] == []


def test_biased_locking_on_java_17_is_a_warning():
    files = node_files("50x", cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"])
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm17_extra_options"] == ["-XX:-UseBiasedLocking"]
    assert len(out["jvm_warnings"]) == 1
    assert out["jvm_warnings"][0].startswith("-XX:-UseBiasedLocking (cassandra_jvm17_extra_options, from"
                                             " jvm17-server.options, Java 17): biased locking is deprecated")
    # Java 11 reads 5.0's stock jvm11 line: nothing to say
    assert cassandra_config_import(node_files("50x"), "50x", FACTS, java="11")["jvm_warnings"] == []


def test_an_option_in_the_place_of_a_gc_line_is_carried():
    # the heap written over two of the G1 lines: not taken for them (it was dropped, the heap not imported)
    files = node_files("50x")
    files["jvm17-server.options"] = files["jvm17-server.options"].replace(
        "-XX:MaxTenuringThreshold=2", "-Xms31G").replace("-XX:G1HeapRegionSize=16m", "-Xmx31G")
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    extras = out["vars"]["cassandra_jvm17_extra_options"]
    assert extras[extras.index("-Xms31G"):][:2] == ["-Xms31G", "-Xmx31G"]


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


def test_an_option_carried_is_no_hand_edit_beside_another_edit():
    # -Xss1024M in the place of -Xss256k, carried at the end (the last one counts), a stock line removed: only the
    # removed line is a hand edit (the report said -Xss1024M both carried and a hand edit no variable carries)
    files = node_files("50x")
    files["jvm-server.options"] = files["jvm-server.options"].replace("-Xss256k", "-Xss1024M").replace(
        "-XX:+UseNUMA\n", "##-XX:+UseNUMA\n")
    out = cassandra_config_import(files, "50x", FACTS, java="17")
    assert out["vars"]["cassandra_jvm_extra_options"] == ["-Xss1024M"]
    assert [line for line in out["hand_edits"] if line.startswith("  ")] == ["  - -XX:+UseNUMA", "  + ##-XX:+UseNUMA"]
    assert "jvm-server.options: -Xss, at another place than the role writes it: the same for the JVM (the last one" \
           " counts)" in out["normalized"]


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


def test_the_node_does_not_keep_a_file_its_java_does_not_read():
    # imported as it is: not left out of the role's files (cassandra_config_keep_files), no note
    config = cassandra_config_import(heap_in_jvm11_on_java17(), "50x", FACTS, java="17", cmdline=CMDLINE)
    hv = {"import_cluster_config": config, "import_cluster_series": "50x", "import_cluster_java": "17"}
    entry = import_node(hv)
    assert "cassandra_config_keep_files" not in entry["keep"]
    assert not [n for n in entry["notes"] if "jvm11-server.options" in n]
    assert entry["vars"]["cassandra_jvm11_extra_options"] == ["-Xms24G", "-Xmx24G"]
    # kept as hand edits only: the files import_keep_hand_edits names
    config_edits = dict(config, hand_edits=["cassandra.yaml, line 3:", "  + x: 1"] + config["hand_edits"])
    entry = import_node(dict(hv, import_cluster_config=config_edits), keep_hand_edits=True)
    assert entry["keep"]["cassandra_config_keep_files"] == ["cassandra.yaml"]


def test_the_warnings_to_review_once_for_all_the_nodes():
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
    option, dummy, why = config["jvm_warnings"][0].partition("): ")
    at = lines.index("    %s)   all (2 nodes)" % option)  # (under WORTH A LOOK, once for both)
    assert lines[at + 1] == "      " + why and next(i for i, line in enumerate(lines) if line.startswith("==== WORTH A LOOK =")) < at


def test_self_check_names_the_file_a_41_on_java_17_reads():
    # 4.1 on Java 17 reads jvm11-server.options: missing on the node, a difference; jvm8's, a note
    rendered = {"jvm8-server.options": "-Da=1\n", "jvm11-server.options": "-Db=1\n"}
    out = _compare_files(rendered, {}, java="17", series="41x")
    assert out["differences"] == ["jvm11-server.options: not on the node, the roles would create it"]
    assert out["notes"] == ["jvm8-server.options: not on the node, the roles would create it (Java 17 does not read it)"]


def kept_env(series, **changes):
    """slurp's result of a kept cassandra-env.sh: the role's file of the series, rendered (heap variables given)."""
    import base64
    text = node_files(series, **changes)["cassandra-env.sh"]
    return [{"item": "cassandra-env.sh", "content": base64.b64encode(text.encode()).decode()}]


KEEP_ENV = {"cassandra_config_keep_files": ["cassandra-env.sh"]}


@pytest.mark.parametrize("variables, series, java, now_series, now_java, env_series", [
    # the stock cassandra-env.sh kept as it is: no false refusal (its checks, its MAX_HEAP_SIZE lines)
    ({}, "50x", "17", "50x", "11", "50x"),
    ({}, "41x", "11", "41x", "8", "41x"),
    ({}, "40x", "11", "40x", "8", "40x"),
    ({}, "50x", "11", "41x", "11", "41x"),  # (4.1 -> 5.0: the 4.1 file stays)
    ({"cassandra_jvm11_gc": "G1"}, "41x", "11", "41x", "8", "41x"),
])
def test_a_stock_kept_cassandra_env_sh_starts(variables, series, java, now_series, now_java, env_series):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_refusals
    assert cassandra_jvm_switch_refusals(dict(KEEP_ENV, **variables), series, java, now_series, now_java,
                                         kept_env(env_series)) == []


@pytest.mark.parametrize("variables, series, java, now_java, said", [
    # and the false passes, refused
    ({"cassandra_jvm11_extra_options": ["-Xms8G"]}, "41x", "11", "8", "not the other"),
    ({"cassandra_jvm11_extra_options": ["-Xmn1G"]}, "41x", "11", "8", "-Xmn without -Xmx"),
    ({"cassandra_jvm17_gc": "CMS", "cassandra_jvm17_extra_options": ["-Xms8G"]}, "50x", "17", "11", "not the other"),
])
def test_a_stock_kept_cassandra_env_sh_and_options_that_stop_it(variables, series, java, now_java, said):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_refusals
    out = cassandra_jvm_switch_refusals(dict(KEEP_ENV, **variables), series, java, series, now_java, kept_env(series))
    assert len(out) == 1 and said in out[0], out


def test_cassandra_env_sh_s_own_heap_before_its_checks_only():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import _env_heap
    text = node_files("50x", cassandra_heap_size="8G")["cassandra-env.sh"]
    env = _env_heap(text)
    assert env["heap"] == {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": None}  # (not the calculated one its checks set)
    assert not [w for w in env["words"] if w.startswith(("-Xms", "-Xmx", "-Xmn"))]  # (the ones it adds after its greps)
    assert env["unfollowed"] == []  # (its JVM_OPTS lines in ifs: none with the letters it greps)
    assert _env_heap("export MAX_HEAP_SIZE=4G\nexport HEAP_NEWSIZE='400M'\n")["heap"] == {
        "MAX_HEAP_SIZE": "4G", "HEAP_NEWSIZE": "400M"}


def test_a_kept_file_not_read_says_slurp_s_message():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_refusals
    out = cassandra_jvm_switch_refusals(KEEP_ENV, "50x", "17", "50x", "11", [
        {"item": "cassandra-env.sh", "failed": True, "msg": "Permission denied: /etc/cassandra/cassandra-env.sh"}])
    assert out == ["Java 17 reads jvm17-server.options: cassandra-env.sh (kept as it is: cassandra_config_keep_files):"
                   " not read (Permission denied: /etc/cassandra/cassandra-env.sh): what cassandra-env.sh finds is not"
                   " checked. Make it readable by root, then run again."]
    # not on the node: the role writes its own there (cassandra_config_keep_files keeps a file a node has)
    assert cassandra_jvm_switch_refusals(dict(KEEP_ENV, cassandra_jvm17_extra_options=["-Xms8G"]), "50x", "17", "50x",
                                         "11", [{"item": "cassandra-env.sh", "failed": True,
                                                 "msg": "file not found: /etc/cassandra/cassandra-env.sh"}])[0] \
        .startswith("Java 17 reads jvm17-server.options: cassandra-env.sh stops: it finds one of Xmx and Xms")


def test_the_heap_line_names_the_kept_cassandra_env_sh():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_heap
    line = cassandra_jvm_switch_heap(dict(KEEP_ENV, cassandra_jvm17_extra_options=["-Xms24G", "-Xmx24G"]), "50x", "17",
                                     "50x", "11", kept_env("50x", cassandra_heap_size="31G"))
    assert line == [u"heap after the move: 24\xa0GiB (cassandra_jvm17_extra_options: -Xms24G -Xmx24G); node's file now:"
                    u" 31\xa0GiB (MAX_HEAP_SIZE in cassandra-env.sh, kept as it is)"]


def test_an_empty_kept_file_is_read_and_an_unknown_running_java_said():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import (
        cassandra_jvm_switch_heap, cassandra_jvm_switch_refusals)
    # an empty kept jvm-server.options: read (its content ""), not "not read"
    empty = [{"item": "jvm-server.options", "content": ""}]
    out = cassandra_jvm_switch_refusals({"cassandra_config_keep_files": ["jvm-server.options"],
                                         "cassandra_jvm17_extra_options": ["-Xms8G", "-Xmx8G"]}, "50x", "17", "50x", "11",
                                        empty)
    assert out == []
    # the Java the node runs not read: its heap now not guessed
    assert cassandra_jvm_switch_heap({"cassandra_heap_size": "31G", "cassandra_jvm17_extra_options": ["-Xms24G", "-Xmx24G"]},
                                     "50x", "17", "50x", "") == [
        u"heap after the move: 24\xa0GiB (cassandra_jvm17_extra_options: -Xms24G -Xmx24G); now: not known (the Java the"
        " node runs was not read)"]


def _env_runs(text, pre, java, tmp_path):
    """Whether the cassandra-env.sh text runs through (sh, its JVM_OPTS from the options files: pre)."""
    import subprocess
    path = tmp_path / "cassandra-env.sh"
    path.write_text(text)
    for d in ("log", "dump"):
        (tmp_path / d).mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "JVM_OPTS": " ".join(pre), "JAVA_VERSION": str(java), "CASSANDRA_CONF": str(tmp_path),
           "CASSANDRA_HOME": str(tmp_path), "CASSANDRA_LOG_DIR": str(tmp_path / "log"), "JVM_ARCH": "64-Bit",
           "JVM_VENDOR": "OpenJDK"}
    result = subprocess.run(["/bin/sh", "-c", '. "$1"; echo "OPTS=$JVM_OPTS"', "x", str(path)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60,
                            check=False)
    return "OPTS=" in result.stdout


def _model_runs(text, pre):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_args import stock_options
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import SWITCH_GREPS, _env_heap
    env = _env_heap(text)
    words = pre + env["words"]
    return stock_options(dict((p, any(p in w for w in words)) for p in SWITCH_GREPS), env["heap"], []) is not None


PRE = ([], ["-XX:+UseG1GC"], ["-XX:+UseConcMarkSweepGC"], ["-Xms8G", "-Xmx8G"], ["-Xmx8G"],
       ["-Xmn1G", "-Xms8G", "-Xmx8G"], ["-XX:+UseG1GC", "-Xmn1G", "-Xms8G", "-Xmx8G"], ["-XX:+UseG1GC", "-Xms8G"])


@pytest.mark.parametrize("series, java", [("40x", "11"), ("41x", "11"), ("50x", "17")])
@pytest.mark.parametrize("heap", [{}, {"cassandra_heap_size": "8G"}, {"cassandra_heap_newsize": "800M"},
                                  {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M"},
                                  {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
                                   "cassandra_heap_in_jvm_options": True}])
def test_the_model_of_a_kept_cassandra_env_sh_is_the_shell_s(tmp_path, series, java, heap):
    # the role's own cassandra-env.sh, rendered, run by sh with options files' words: starts or stops as
    # the model says, for each heap and each set of options
    text = node_files(series, **heap)["cassandra-env.sh"]
    for pre in PRE:
        assert _model_runs(text, pre) is _env_runs(text, pre, java, tmp_path), (series, heap, pre)


NONE = {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None}


@pytest.mark.parametrize("lines, heap, words, unfollowed", [
    # the forms a kept cassandra-env.sh may have before its checks
    ('export JVM_OPTS="$JVM_OPTS -Xmx8G"\nexport MAX_HEAP_SIZE="8G"; HEAP_NEWSIZE=800M\n',
     {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "800M"}, ["-Xmx8G"], []),
    ('if [ -n "$X" ]; then MAX_HEAP_SIZE=8G; fi\n', {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None}, [],
     [("MAX_HEAP_SIZE", 1)]),
    ('[ -n "$X" ] && JVM_OPTS="$JVM_OPTS -Xms8G"\n', {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None}, [],
     [("JVM_OPTS", 1)]),
    ('if true\nthen\n    HEAP_NEWSIZE=1G\nfi\n', {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None}, [], [("HEAP_NEWSIZE", 3)]),
    ('if true; then JVM_OPTS="$JVM_OPTS -Dx=1"; fi\n# MAX_HEAP_SIZE=1G\n', {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None},
     [], []),  # (a condition without the letters it greps, a comment: nothing to follow)
    ('size() {\n    MAX_HEAP_SIZE=2G\n}\nMAX_HEAP_SIZE=4G # the heap\n', {"MAX_HEAP_SIZE": "4G", "HEAP_NEWSIZE": None},
     [], []),  # (a function's, not called before the checks: its own computation)
    # what it cannot follow: said, never guessed
    ('case "$X" in\n  x) MAX_HEAP_SIZE=8G ;;\nesac\n', NONE, [], [("MAX_HEAP_SIZE", 2)]),  # (a case arm)
    ('{ MAX_HEAP_SIZE=8G; }\n', NONE, [], [("MAX_HEAP_SIZE", 1)]),  # (a brace group)
    ('f() {\n  :\n} # end f\nMAX_HEAP_SIZE=8G\n', {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": None}, [], []),
    ('f() {\n  MAX_HEAP_SIZE=8G\n}\nf\n', NONE, [], [("f (a function that sets them)", 4)]),
    ('MAX_HEAP_SIZE=\\\n8G\n', NONE, [], [("MAX_HEAP_SIZE", 1)]),  # (a backslash continuation)
    ('JVM_OPTS="$JVM_OPTS\n -Xmn1G"\n', NONE, [], [("JVM_OPTS", 1)]),  # (an open quote)
    ('. /etc/cassandra/heap.sh\n', NONE, [], [("a file it sources", 1)]),
    ('X=-Xmn1G\nJVM_OPTS="$JVM_OPTS $X"\n', NONE, [], [("JVM_OPTS", 2)]),  # (a variable of its own)
    ('readonly MAX_HEAP_SIZE=8G\n', NONE, [], [("MAX_HEAP_SIZE", 1)]),
    (': ${MAX_HEAP_SIZE:=8G}\n', NONE, [], [("MAX_HEAP_SIZE", 1)]),
    ('MAX_HEAP_SIZE=$(echo 8G)\n', NONE, [], [("MAX_HEAP_SIZE", 1)]),
    ('cat >/dev/null <<EOF\nMAX_HEAP_SIZE=8G\nEOF\n', NONE, [], [("a here-document", 2)]),
    ('function g {\n  :\n}\n', NONE, [], [("the function keyword (not every sh has it)", 1)]),
    ('if true; then\n  :\n', NONE, [], [("a block left open (if, case, a loop, a function)", 5)]),
])
def test_cassandra_env_sh_forms(lines, heap, words, unfollowed):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import _env_heap
    env = _env_heap(lines + 'echo $JVM_OPTS | grep -q Xmn\nif [ "x$MAX_HEAP_SIZE" = "x" ]; then MAX_HEAP_SIZE=1G; fi\n')
    assert (env["heap"], env["words"], env["unfollowed"]) == (heap, words, unfollowed)


def test_a_kept_cassandra_env_sh_the_check_cannot_follow_is_refused():
    import base64
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_refusals
    text = node_files("50x")["cassandra-env.sh"].replace(
        "\n# Check what parameters", '\nif [ -n "$BIG" ]; then MAX_HEAP_SIZE=31G; fi\n# Check what parameters', 1)
    line = text.split("\n").index('if [ -n "$BIG" ]; then MAX_HEAP_SIZE=31G; fi') + 1
    out = cassandra_jvm_switch_refusals(KEEP_ENV, "50x", "17", "50x", "11", [
        {"item": "cassandra-env.sh", "content": base64.b64encode(text.encode()).decode()}])
    assert out == ["Java 17 reads jvm17-server.options: cassandra-env.sh (kept as it is: cassandra_config_keep_files),"
                   " line %d: sets MAX_HEAP_SIZE in a form this check cannot follow (a condition, a function called,"
                   " another file...). Check that line by hand; for the check to follow it, set it with a plain"
                   " assignment at the start of a line (or take cassandra-env.sh out of cassandra_config_keep_files: the"
                   " role writes its own), then run again." % line]


def test_the_heap_line_counts_the_options_cassandra_env_sh_adds_after_its_checks():
    import base64
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_heap
    text = node_files("50x")["cassandra-env.sh"] + '\nJVM_OPTS="$JVM_OPTS -Xms12G -Xmx12G"\n'
    kept = [{"item": "cassandra-env.sh", "content": base64.b64encode(text.encode()).decode()}]
    out = cassandra_jvm_switch_heap(dict(KEEP_ENV, cassandra_jvm17_extra_options=["-Xms24G", "-Xmx24G"]), "50x", "17",
                                    "50x", "11", kept)
    # the JVM takes cassandra-env.sh's last ones, 12G before and after: no line (24G in jvm17 does not win)
    assert out == []


def test_a_kept_options_file_of_the_new_java_is_read_as_the_node_has_it():
    import base64
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_switch_refusals
    variables = {"cassandra_config_keep_files": ["jvm17-server.options"], "cassandra_jvm17_extra_options": ["-Xms8G"]}
    kept = [{"item": "jvm17-server.options", "content": base64.b64encode(b"-XX:+UseG1GC\n-Xms8G\n-Xmx8G\n").decode()}]
    assert cassandra_jvm_switch_refusals(variables, "50x", "17", "50x", "", kept) == []  # (its own pair, not the variable)
    kept = [{"item": "jvm17-server.options", "content": base64.b64encode(b"-XX:+UseG1GC\n-Xmn2G\n-Xms8G\n-Xmx8G\n").decode()}]
    out = cassandra_jvm_switch_refusals(variables, "50x", "17", "50x", "", kept)
    assert len(out) == 1 and "-Xmn in jvm17-server.options (kept as it is: cassandra_config_keep_files)" in out[0], out
