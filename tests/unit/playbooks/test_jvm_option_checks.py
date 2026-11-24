from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# preflight warns about the extra JVM options worth a look that each node's
# Java reads, one line per warning with its nodes; a Java change (java_check.yml:
# update_java, upgrade, a new node, cassandra_install) is refused with an
# option that Java does not take. The expressions are read from the playbook
# and the task file, rendered by Ansible.

import base64
import os
import warnings

import pytest
import yaml

from ansible.plugins.loader import init_plugin_loader

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.helpers import render

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()
load_collection_filters()  # the community.cassandra filters, whatever ran before

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Cassandra preflight checks")
FIND = next(t for t in PLAY["tasks"] if t.get("name") == "Find the JVM options worth a look")
WARN = next(t for t in PLAY["tasks"] if t.get("name") == "Warn about the JVM options")
with open(os.path.join(TOP, "roles", "cassandra_install", "tasks", "java_check.yml"), encoding="utf-8") as f:
    REFUSE = next(t for t in yaml.safe_load(f) if t.get("name") == "The JVM options suit this Java")
# (update_java's plan and apply_config's Java change share it)
with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "java_plan.yml"), encoding="utf-8") as f:
    UPDATE_KEPT = next(t for t in yaml.safe_load(f) if t.get("name") == "The new Java reads an options file the role writes")
with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as f:
    UPGRADE_KEPT = [t for p in yaml.safe_load(f) for t in p.get("tasks") or []
                    if t.get("name") == "The target reads an options file the role writes"]
with open(os.path.join(TOP, "roles", "cassandra_install", "tasks", "java_options_kept.yml"), encoding="utf-8") as f:
    KEPT, KEPT_READ, HEAP, HEAP_LINE = yaml.safe_load(f)


def found(**variables):
    variables.setdefault("cassandra_version", "50x")
    variables["_java"] = render(FIND["vars"]["_java"], **variables)
    for k, v in FIND["vars"]["_options"].items():
        variables.setdefault("_options", {})[k] = render(v, **variables)
    return render(FIND["ansible.builtin.set_fact"]["_cassandra_preflight_jvm"], **variables)


def test_preflight_finds_what_the_java_reads():
    assert found() == []  # the role defaults: nothing
    assert len(found(cassandra_jvm_extra_options=["-Xss1024M"])) == 1
    # cassandra_java_version not loaded in preflight: the series' default (5.0: 17)
    assert len(found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"])) == 1
    assert found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"], cassandra_java_version="11") == []
    assert found(cassandra_jvm11_extra_options=["-Xss1024M"]) == []  # Java 17 does not read it
    # cassandra_java_versions overridden (not cassandra_java_version): its Java
    assert found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"],
                 cassandra_java_versions={"50x": "11"}) == []
    # 4.x on Java 17 (unsupported): the role's own jvm-server.options line, unless the node's file is kept
    assert len(found(cassandra_version="41x", cassandra_java_version="17")) == 1
    assert found(cassandra_version="41x", cassandra_java_version="17",
                 cassandra_config_keep_files=["jvm-server.options"]) == []
    # nor the variable's options in a kept file: the role does not write them there
    assert found(cassandra_jvm_extra_options=["-Xss1024M"], cassandra_config_keep_files=["jvm-server.options"]) == []


def test_preflight_one_line_per_warning_with_its_nodes():
    stack = found(cassandra_jvm_extra_options=["-Xss1024M"])
    hostvars = {"node1": {"_cassandra_preflight_jvm": stack}, "node2": {"_cassandra_preflight_jvm": stack},
                "node3": {"_cassandra_preflight_jvm": []}}
    variables = dict(WARN["vars"], ansible_play_hosts=["node1", "node2", "node3"], hostvars=hostvars)
    assert render("{{ %s }}" % WARN["when"], **variables) is True
    assert render(WARN["vars"]["_note"], **variables) == "WARNING  node1, node2: " + stack[0]
    hostvars = dict((h, {"_cassandra_preflight_jvm": []}) for h in hostvars)
    assert render("{{ %s }}" % WARN["when"], **dict(variables, hostvars=hostvars)) is False


def refused(**variables):
    variables.setdefault("cassandra_version", "50x")
    return render(REFUSE["vars"]["_refused"], **variables)


@pytest.mark.parametrize("java", ["18", "21"])
def test_a_java_change_is_refused_with_biased_locking(java):
    out = refused(cassandra_java_version=java, cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"])
    assert len(out) == 1 and "Remove it from cassandra_jvm_extra_options first" in out[0]
    # in the file of that Java too (5.0 on 18+: jvm17)
    assert len(refused(cassandra_java_version=java, cassandra_jvm17_extra_options=["-XX:+UseBiasedLocking"])) == 1


def test_a_java_change_goes_on_without_it():
    assert refused(cassandra_java_version="17", cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"]) == []
    # 5.0's stock jvm11 line: read by Java 11 only
    assert refused(cassandra_java_version="21", cassandra_jvm11_extra_options=["-XX:-UseBiasedLocking"]) == []
    assert refused(cassandra_java_version="21") == []  # other roles' defaults not loaded
    assert refused(cassandra_java_version="21", cassandra_jvm_extra_options=["-Xss1024M"]) == []  # a warning only


def test_a_java_change_with_the_role_s_own_line():
    # 4.1's jvm-server.options writes -XX:-UseBiasedLocking: refused on Java 21 (allow_unsupported), unless the
    # node keeps its own file
    assert len(refused(cassandra_version="41x", cassandra_java_version="21")) == 1
    assert refused(cassandra_version="41x", cassandra_java_version="21",
                   cassandra_config_keep_files=["jvm-server.options"]) == []


def kept_ok(running, target, keep, todo=True, series="50x"):
    variables = dict(cassandra_version=series, cassandra_java_version=target, cassandra_java_plan_major=running,
                     cassandra_java_plan_todo=todo, cassandra_config_keep_files=keep)
    if not render("{{ %s }}" % UPDATE_KEPT["when"], **variables):
        return True, variables
    variables.update((k, render(v, **variables)) for k, v in UPDATE_KEPT["vars"].items())
    variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
    return render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables), variables


def test_a_move_to_a_java_whose_file_is_kept_is_refused():
    # import_cluster kept jvm11-server.options of a 5.0 node on Java 17: back to Java 11 would read it as it is
    ok, variables = kept_ok("17", "11", ["jvm11-server.options"])
    assert ok is False
    msg = render(KEPT["ansible.builtin.assert"]["fail_msg"], inventory_hostname="node1", **variables)
    assert "jvm11-server.options is in cassandra_config_keep_files" in msg
    assert "cassandra_jvm11_extra_options" in msg and "Nothing was changed." in msg


def test_an_upgrade_checks_the_options_file_of_the_target():
    # 4.1 on Java 8 imported with jvm11 kept, upgraded to 5.0 on Java 11: the 4.1 file would be read as it is
    assert len(UPGRADE_KEPT) == 1 and "when" not in UPGRADE_KEPT[0]  # (every upgrade: its Java may change too)
    variables = dict(cassandra_version="50x", cassandra_java_version="11", cassandra_upgrade_from_series="41x",
                     cassandra_config_keep_files=["jvm11-server.options"],
                     cassandra_upgrade_java_spec={"stderr": "    java.specification.version = 1.8\n"})
    variables.update((k, render(v, **variables)) for k, v in UPGRADE_KEPT[0]["vars"].items())
    variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
    assert render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables) is False
    # the same series, the same Java: the file it reads already
    variables = dict(variables)
    variables.update(cassandra_upgrade_from_series="50x",
                     cassandra_upgrade_java_spec={"stderr": "    java.specification.version = 11\n"})
    variables.update((k, render(v, **variables)) for k, v in UPGRADE_KEPT[0]["vars"].items())
    assert variables["_cassandra_java_options_now"] == "jvm11-server.options"
    variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
    assert render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables) is True


@pytest.mark.parametrize("running, target, keep, todo, series", [
    ("17", "11", [], True, "50x"),  # not kept: the role writes it
    ("11", "11", ["jvm11-server.options"], False, "50x"),  # nothing to do on that node
    ("17", "21", ["jvm17-server.options"], True, "50x"),  # the same file before and after: read already
    ("11", "17", ["jvm11-server.options"], True, "41x"),  # 4.x reads jvm11 on both
    ("11", "17", ["cassandra.yaml"], True, "50x"),
])
def test_a_move_goes_on_otherwise(running, target, keep, todo, series):
    assert kept_ok(running, target, keep, todo, series)[0] is True


def switch(hostvars, running="17", target="11", series="50x", upgrade_from=None, kept=None):
    """The variables of java_options_kept.yml's tasks for a node with these variables, moved from the Java it runs
    (running, '' not known) by update_java, or from another series by upgrade."""
    variables = dict(hostvars, cassandra_version=series, cassandra_java_version=target)
    if upgrade_from:
        variables["cassandra_upgrade_from_series"] = upgrade_from
        if running:
            variables["cassandra_upgrade_java_spec"] = {"stderr": "    java.specification.version = %s\n"
                                                                  % ("1.8" if running == "8" else running)}
        variables.update((k, render(v, **variables)) for k, v in UPGRADE_KEPT[0]["vars"].items())
    else:
        variables["cassandra_java_plan_major"] = running
        variables.update((k, render(v, **variables)) for k, v in UPDATE_KEPT["vars"].items())
    variables["hostvars"] = {"node1": dict(hostvars)}
    variables["inventory_hostname"] = "node1"
    # the kept files as slurp reads them ({file: text}; None: not readable)
    variables["_cassandra_java_kept"] = {"results": [
        dict({"item": f}, **({"content": base64.b64encode(t.encode()).decode()} if t is not None else {"failed": True}))
        for f, t in (kept or {}).items()]}
    return variables


def heap_refused(hostvars, **kwargs):
    return render(HEAP["vars"]["_refused"], **switch(hostvars, **kwargs))


def heap_line(hostvars, **kwargs):
    """The node's heap lines, one text ("; " between the heap's and the young generation's), as printed (a no-break
    space keeps a size with its unit: a space here)."""
    lines = render(HEAP_LINE["ansible.builtin.set_fact"]["_cassandra_java_heap_lines"], **switch(hostvars, **kwargs))
    return "; ".join(lines).replace(u"\xa0", " ")


IMPORTED = {"cassandra_heap_size": "31G", "cassandra_jvm11_extra_options": ["-Xms24G", "-Xmx24G"]}


def test_the_heap_of_jvm11_imported_on_java_17_is_in_the_plan_of_the_move_to_java_11():
    # import_cluster's heap_in_jvm11_on_java17: the node runs 31G (cassandra-env.sh), its jvm11 file,
    # not read on Java 17, holds 24G: the inventory's value, a line of update_java's plan, not refused
    assert heap_refused(IMPORTED) == []
    line = heap_line(IMPORTED)
    assert line == ("heap after the move: 24 GiB (cassandra_jvm11_extra_options: -Xms24G -Xmx24G); inventory now:"
                    " 31 GiB (cassandra_heap_size)")
    # in the plan, under the node (the screen as printed)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
    with open(os.path.join(TOP, "playbooks", "update_java.yml"), encoding="utf-8") as f:
        plan = next(t for p in yaml.safe_load(f) for t in p.get("tasks") or []
                    if t.get("name") == "Show the plan and confirm the run")
    lines_n1 = render(HEAP_LINE["ansible.builtin.set_fact"]["_cassandra_java_heap_lines"], **switch(IMPORTED))
    hostvars = {"n1": {"_cassandra_java_heap_lines": lines_n1, "cassandra_java_plan_running": "/usr/lib/jvm/java-17/bin/java"},
                "n2": {"_cassandra_java_heap_lines": [], "cassandra_java_plan_running": "/usr/lib/jvm/java-17/bin/java"}}
    # (each node's lines: the plan's own task, before it)
    with open(os.path.join(TOP, "playbooks", "update_java.yml"), encoding="utf-8") as f:
        note = next(t for p in yaml.safe_load(f) for t in p.get("tasks") or [] if t.get("name") == "Note the lines of the plan")
    for v in hostvars.values():
        v["_cassandra_update_java_lines"] = render(note["ansible.builtin.set_fact"]["_cassandra_update_java_lines"],
                                                   _axonops=[], **v)
    blocks = render(plan["vars"]["_blocks"], _todo=["n1", "n2"], hostvars=hostvars)
    text = cassandra_screen({"operation": "update_java", "summary": "n1, n2 of c to Java 11", "blocks": blocks})
    lines = text.split("\n")
    at = lines.index("    runs /usr/lib/jvm/java-17/bin/java")  # (n1's block: its line under it, wrapped)
    assert " ".join(x.strip() for x in lines[at + 1:at + 3]) == line and lines[at + 3] == "  n2"
    assert "31 GiB" in lines[at + 1] or "31 GiB" in lines[at + 2]  # (never a number apart from its unit)
    assert text.count("heap after the move") == 1
    # the same heap, or no move of file: nothing said
    assert heap_line(dict(IMPORTED, cassandra_jvm11_extra_options=["-Xms31G", "-Xmx31G"])) == ""
    assert heap_line(IMPORTED, running="11", target="11") == ""
    # (Java 17 reads jvm17, no heap there: the one jvm11 gave goes)
    assert heap_line(IMPORTED, running="11", target="17") == (
        "heap after the move: 31 GiB (cassandra_heap_size); inventory now: 24 GiB (cassandra_jvm11_extra_options:"
        " -Xms24G -Xmx24G)")


@pytest.mark.parametrize("node, line", [
    ({"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G"]},
     "heap after the move: 8 GiB (cassandra_jvm11_extra_options: -Xms8G -Xmx8G); inventory now: cassandra-env.sh's"
     " own sizing"),
    ({"cassandra_heap_size": "lots", "cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G"]},
     "heap after the move: 8 GiB (cassandra_jvm11_extra_options: -Xms8G -Xmx8G); inventory now: lots"
     " (cassandra_heap_size)"),  # (not a size: shown as it is)
    ({"cassandra_jvm_gc": "CMS", "cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
      "cassandra_jvm11_extra_options": ["-Xmn2G"]},
     "young generation after the move: 2 GiB (cassandra_jvm11_extra_options: -Xmn2G); inventory now: 800 MiB"
     " (cassandra_heap_newsize)"),
])
def test_the_plan_line_says_the_heap_observed_against_the_inventory(node, line):
    assert heap_line(node) == line


@pytest.mark.parametrize("node, series, running, target, said", [
    # as cassandra-env.sh greps: the letters Xmx, Xms, Xmn
    ({"cassandra_jvm11_extra_options": ["-Xms8G", "-XX:MaxHeapSize=8g"]}, "50x", "17", "11", "not the other"),
    ({"cassandra_jvm_gc": "CMS", "cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
      "cassandra_jvm11_extra_options": ["-Xmn800M"]}, "50x", "17", "11", "-Xmn without -Xmx"),
    ({"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M", "cassandra_jvm11_extra_options": ["-Xmn800M"]},
     "41x", "8", "11", "-Xmn without -Xmx"),  # (4.x's CMS)
    ({"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn800M"]}, "50x", "17", "11", "-Xmn under G1"),
    # a GC change alone: jvm11 under CMS on 4.1, under G1 on 5.0, the file the same
    ({"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
      "cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn800M"]}, "50x", "11", "11", "-Xmn under G1"),
    ({"cassandra_prometheus_exporter_enabled": True,
      "cassandra_jvm11_extra_options": ["-javaagent:/opt/old/jmx_prometheus_javaagent-0.20.0.jar=9404:/x.yml"]},
     "50x", "17", "11", "the JVM would load two"),
])
def test_a_move_refused_when_cassandra_env_sh_would_stop(node, series, running, target, said):
    upgrade = {"upgrade_from": "41x"} if series == "50x" and running == target else {}
    out = heap_refused(node, series=series, running=running, target=target, **upgrade)
    assert len(out) == 1 and said in out[0] and out[0].endswith("then run again."), out
    variables = dict(HEAP["vars"], _refused=out, inventory_hostname="node1", cassandra_version=series,
                     cassandra_java_version=target)
    assert render("{{ %s }}" % HEAP["ansible.builtin.assert"]["that"], **variables) is False
    variables["_nl"] = "\n"
    msg = render(HEAP["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")
    assert msg == ["node1: " + line for line in out] + ["Nothing was changed."]  # (one refusal a line, its node)


@pytest.mark.parametrize("node, series", [
    ({"cassandra_jvm11_extra_options": ["-XX:NewSize=1g"], "cassandra_heap_size": "8G"}, "50x"),  # (no "Xmn")
    ({"cassandra_jvm11_extra_options": ["-XX:MaxNewSize=1g"]}, "50x"),
    ({"cassandra_jvm11_extra_options": ["-XX:MaxHeapSize=8g"]}, "50x"),  # (no Xmx nor Xms: cassandra-env.sh adds both)
    # the pair written by cassandra_heap_in_jvm_options in jvm-server.options: an -Xmx in jvm11 is not alone
    ({"cassandra_heap_in_jvm_options": True, "cassandra_heap_size": "8G", "cassandra_jvm11_extra_options": ["-Xmx16G"]},
     "50x"),
    ({"cassandra_jvm_gc": "CMS", "cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
      "cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn800M"]}, "50x"),
    ({"cassandra_jvm11_extra_options": ["-javaagent:/x/jmx_prometheus_javaagent-1.jar=1:/x"]}, "50x"),  # (exporter off)
])
def test_a_move_goes_on_with_what_starts(node, series):
    assert heap_refused(node, series=series) == []


def test_nothing_checked_when_the_file_and_the_gc_stay():
    node = {"cassandra_jvm11_extra_options": ["-Xmx8G"]}  # (a node that runs: what it reads starts)
    assert heap_refused(node, running="11", target="11") == []
    assert len(heap_refused(node, running="17", target="11")) == 1
    # the Java it runs not known: all is checked
    assert len(heap_refused(node, running="", target="11", upgrade_from="41x")) == 1


def test_an_upgrade_compares_with_the_java_the_node_runs():
    # 4.0 on Java 8 to 5.0 on Java 11: jvm11 read for the first time, checked
    node = {"cassandra_jvm11_extra_options": ["-Xmx8G"]}
    assert len(heap_refused(node, running="8", target="11", upgrade_from="40x")) == 1
    # 4.1 on Java 11 to 5.0 on Java 11: the file the same, the GC CMS -> G1: checked
    gc = {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
          "cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn800M"]}
    assert len(heap_refused(gc, running="11", target="11", upgrade_from="41x")) == 1
    # the plan line, against the file it reads now
    node = {"cassandra_heap_size": "8G", "cassandra_jvm17_extra_options": ["-Xms4G", "-Xmx4G"]}
    assert heap_line(node, running="11", target="17", upgrade_from="41x") == (
        "heap after the upgrade: 4 GiB (cassandra_jvm17_extra_options: -Xms4G -Xmx4G); inventory now: 8 GiB"
        " (cassandra_heap_size)")
    assert heap_line(dict(node, cassandra_jvm11_extra_options=["-Xms4G", "-Xmx4G"]), running="11", target="11",
                     upgrade_from="41x") == ""  # (it reads jvm11 already)


def test_the_heap_line_is_in_the_plan_of_upgrade_s_prepare_once():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
    with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as f:
        screens = [t for p in yaml.safe_load(f) for t in p.get("tasks") or []
                   if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "screen.yml"]
    with_heap = [t for t in screens if "_cassandra_java_heap_lines" in str(t["vars"]["cassandra_screen"])]
    assert len(with_heap) == 1 and "phase prepare" in with_heap[0]["vars"]["cassandra_screen"]["summary"]
    lines = ["heap after the upgrade: 4 GiB (cassandra_jvm17_extra_options: -Xms4G -Xmx4G)",
             "young generation after the upgrade: 1 GiB (cassandra_jvm17_extra_options: -Xmn1G)"]
    blocks = render(with_heap[0]["vars"]["cassandra_screen"]["blocks"], ansible_play_hosts=["n1", "n2"],
                    hostvars={"n1": {}, "n2": {"_cassandra_java_heap_lines": lines,
                                               "_cassandra_preflight": {"address": "10.0.0.2"}}})
    text = cassandra_screen({"operation": "upgrade", "summary": "c, phase prepare", "blocks": blocks})
    said = text.split("\n")[2:]
    assert said[0] == "  n2  10.0.0.2" and max(len(line) for line in said) <= 79
    # a line each (its next lines further in)
    starts = [line[4:] for line in said if line.startswith("    ") and line[4] != " "]
    assert [x for x, line in zip(starts, lines) if line.startswith(x)] == starts and len(starts) == 2
    assert " ".join(" ".join(said).split()) == " ".join(("n2 10.0.0.2 " + " ".join(lines)).split())


def test_the_way_out_names_the_variable_that_holds_the_option():
    # -Xmn in cassandra_jvm_extra_options (CMS on 4.1, G1 on 5.0): named, not the jvm11 variable
    out = heap_refused({"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
                        "cassandra_jvm_extra_options": ["-Xms8G", "-Xmx8G", "-Xmn800M"]},
                       series="50x", running="11", target="11", upgrade_from="41x")
    assert len(out) == 1 and "(-Xmn in cassandra_jvm_extra_options; G1 from cassandra_jvm11_gc (default G1))" in out[0], out
    # written by cassandra_heap_in_jvm_options: the heap variables named
    out = heap_refused({"cassandra_heap_in_jvm_options": True, "cassandra_heap_size": "8G",
                        "cassandra_heap_newsize": "800M"}, series="50x", running="11", target="11", upgrade_from="41x")
    assert "-Xmn in cassandra_heap_newsize" in out[0], out
    # MAX_HEAP_SIZE without HEAP_NEWSIZE under CMS: the heap variables
    out = heap_refused({"cassandra_jvm_gc": "CMS", "cassandra_heap_size": "8G", "cassandra_jvm11_extra_options": ["-Dx=1"]})
    assert "MAX_HEAP_SIZE without HEAP_NEWSIZE, the GC not G1 (cassandra_heap_size without cassandra_heap_newsize)" \
        in out[0], out
    # the other way round: the young generation without the heap
    out = heap_refused({"cassandra_jvm_gc": "CMS", "cassandra_heap_newsize": "800M",
                        "cassandra_jvm11_extra_options": ["-Dx=1"]})
    assert "HEAP_NEWSIZE without MAX_HEAP_SIZE, the GC not G1 (cassandra_heap_newsize without cassandra_heap_size)" \
        in out[0], out


def test_a_kept_jvm_server_options_is_read_as_the_node_has_it():
    # jvm-server.options kept as it is: the node's own text, not the role's template and variables
    node = {"cassandra_config_keep_files": ["jvm-server.options"], "cassandra_jvm11_extra_options": ["-Xmx8G"]}
    pair = "# the heap\n-Xms8G\n-Xmx8G\n"
    assert heap_refused(node, kept={"jvm-server.options": pair}) == []  # (no false refusal: its -Xms there)
    assert len(heap_refused(node, kept={"jvm-server.options": "-ea\n"})) == 1  # (-Xmx alone then)
    young = {"cassandra_config_keep_files": ["jvm-server.options"]}
    out = heap_refused(young, kept={"jvm-server.options": "-Xms8G\n-Xmx8G\n-Xmn2G\n"})  # (no false pass: G1 on 5.0)
    assert len(out) == 1 and "-Xmn in jvm-server.options (kept as it is: cassandra_config_keep_files)" in out[0], out
    # not readable: said, refused
    out = heap_refused(young, kept={"jvm-server.options": None})
    assert len(out) == 1 and ": not read (" in out[0]
    # a kept cassandra-env.sh: its own heap
    env = {"cassandra_config_keep_files": ["cassandra-env.sh"], "cassandra_jvm_gc": "CMS",
           "cassandra_jvm11_extra_options": ["-Dx=1"]}
    assert heap_refused(env, kept={"cassandra-env.sh": 'MAX_HEAP_SIZE="8G"\nHEAP_NEWSIZE="800M"\n'}) == []
    assert len(heap_refused(env, kept={"cassandra-env.sh": 'MAX_HEAP_SIZE="8G"\n'})) == 1
    assert len(KEPT_READ["loop"]) and KEPT_READ["failed_when"] is False


def test_a_same_series_upgrade_with_another_java_is_checked():
    # 5.0.2 on Java 11 to 5.0.4 on Java 17: jvm17 read for the first time
    node = {"cassandra_jvm17_extra_options": ["-Xmx8G"]}
    assert len(heap_refused(node, series="50x", running="11", target="17", upgrade_from="50x")) == 1
    assert heap_refused(node, series="50x", running="17", target="17", upgrade_from="50x") == []


def test_the_heap_line_when_the_new_file_has_none():
    # the old file gave the heap, the new one none: cassandra_heap_size, or cassandra-env.sh's sizing
    old = {"cassandra_heap_size": "31G", "cassandra_jvm17_extra_options": ["-Xms24G", "-Xmx24G"]}
    assert heap_line(old, running="17", target="11") == (
        "heap after the move: 31 GiB (cassandra_heap_size); inventory now: 24 GiB (cassandra_jvm17_extra_options:"
        " -Xms24G -Xmx24G)")
    assert heap_line({"cassandra_jvm17_extra_options": ["-Xms24G", "-Xmx24G"]}, running="17", target="11") == (
        "heap after the move: cassandra-env.sh's own sizing; inventory now: 24 GiB (cassandra_jvm17_extra_options:"
        " -Xms24G -Xmx24G)")


def test_a_patch_upgrade_keeping_the_file_the_node_reads_goes_on():
    # 4.1.5 -> 4.1.7 on Java 11, jvm11-server.options kept: the file it reads already, its Java read or not
    for spec in ({"stderr": "    java.specification.version = 11\n"}, {}):
        variables = dict(cassandra_version="41x", cassandra_java_version="11", cassandra_upgrade_from_series="41x",
                         cassandra_config_keep_files=["jvm11-server.options"], cassandra_upgrade_java_spec=spec)
        variables.update((k, render(v, **variables)) for k, v in UPGRADE_KEPT[0]["vars"].items())
        variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
        assert render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables) is True, spec
