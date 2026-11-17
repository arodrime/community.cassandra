from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A failed self-check as the operator reads it: why the inventory does not reproduce the nodes yet, each setting in
# its file's own words with the node's value and Ansible's, grouped by file and nodes, the ways out with their
# options; the evidence (full paths, raw lines) in report.details.txt; the shareable copy keeps the settings' names.

import re

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _compare
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    cassandra_import_report, cassandra_import_self_check_advice)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_shareable import cassandra_import_shareable
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import xml_differences
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_differences as differences

REPORT = "/p/reports/my_cluster/report.txt"
LOGBACK_EDIT = ["logback.xml, line 5:", "  + <x/>"]


def node(i, hand=None, **extra):
    return dict({"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": True,
                 "hand_edits": hand or [], "normalized": [], "notes": [],
                 "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16}}, **extra)


def stop(nodes, checks, width=80, layout_extra=None, **kwargs):
    layout = dict(cassandra_inventory_layout(nodes, "My Cluster"), **(layout_extra or {}))
    return cassandra_import_self_check_advice(layout, checks, report_file=REPORT, cwd="/p", width=width, **kwargs)


def block(lines, head):
    """The lines of a group: from its file's line to the next blank one."""
    at = lines.index(head)
    end = lines.index("", at)
    return lines[at:end]


# --- each kind of difference, in its file's own words --------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    # YAML: the dotted key, the values as Cassandra reads them
    ("cassandra.yaml: client_encryption_options.enabled: node has 'true', import would write 'false'",
     ("cassandra.yaml", "client_encryption_options.enabled", "true", "false")),
    ("cassandra.yaml: cdc_raw_directory: node has '', import would write nothing",
     ("cassandra.yaml", "cdc_raw_directory", "(empty)", "absent")),
    # a JVM option: its name, each side's value; a flag present or absent; written two ways: the options
    ("jvm17-server.options: -Xss: node has '-Xss512k', import would write '-Xss256k'",
     ("jvm17-server.options", "-Xss", "512k", "256k")),
    ("jvm17-server.options: UseZGC: node has '-XX:+UseZGC', import would write nothing",
     ("jvm17-server.options", "-XX:+UseZGC", "present", "absent")),
    ("jvm17-server.options: -Dx: node has nothing, import would write '-Dx=1'",
     ("jvm17-server.options", "-Dx", "absent", "1")),
    ("jvm17-server.options: UseFoo: node has '-XX:-UseFoo', import would write '-XX:+UseFoo'",
     ("jvm17-server.options", "-XX:UseFoo", "-XX:-UseFoo", "-XX:+UseFoo")),
    # cassandra-env.sh: a variable, a JVM_OPTS option, another line
    ('cassandra-env.sh: node has JMX_PORT="${JMX_PORT:-7299}", import would write JMX_PORT=7199',
     ("cassandra-env.sh", "JMX_PORT", "${JMX_PORT:-7299}", "7199")),
    ('cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dchronicle.analytics.disable=true", import would write nothing',
     ("cassandra-env.sh", "-Dchronicle.analytics.disable", "true", "absent")),
    ("cassandra-env.sh: node has ulimit -n 100000, import would write nothing",
     ("cassandra-env.sh", "a line", "ulimit -n 100000", "absent")),
    # the heap, wherever it is set
    ("heap (cassandra-env.sh, else the unit's Environment): MAX_HEAP_SIZE: node has '8G', import would write '16G'",
     (differences.HEAP_FILE, "MAX_HEAP_SIZE", "8G", "16G")),
    # a .properties key
    ("cassandra-rackdc.properties: prefer_local: node has 'true', import would write nothing",
     ("cassandra-rackdc.properties", "prefer_local", "true", "absent")),
    # XML: an element's text, an attribute, an element one side has alone (its text, else present)
    ('logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern: node has'
     " '/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i', import would write"
     " '/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i.zip'",
     ("logback.xml", "AUDIT / rollingPolicy / fileNamePattern", "/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i",
      "/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i.zip")),
    ("logback.xml: /configuration/root/@level: node has 'DEBUG', import would write 'INFO'",
     ("logback.xml", "root / @level", "DEBUG", "INFO")),
    ("logback.xml: /configuration/jmxConfigurator: node has <jmxConfigurator>, import would write nothing",
     ("logback.xml", "jmxConfigurator", "present", "absent")),
    ('logback.xml: /configuration/logger[@name="org.x"]: node has <logger level="DEBUG" name="org.x">, import would'
     " write nothing", ("logback.xml", "org.x", "present (level DEBUG)", "absent")),
    ('logback.xml: /configuration/appender[@name="SYSTEMLOG"]/rollingPolicy/maxHistory: node has'
     " <maxHistory>14</maxHistory>, import would write nothing",
     ("logback.xml", "SYSTEMLOG / rollingPolicy / maxHistory", "14", "absent")),
    ('logback.xml: /configuration/root/appender-ref[@ref="ASYNC"]: node has nothing, import would write'
     ' <appender-ref ref="ASYNC">', ("logback.xml", "root / appender-ref ASYNC", "absent", "present")),
    ("logback.xml: /configuration/appender[@name=\"A\"]/encoder/pattern[2]: node has '%m', import would write nothing",
     ("logback.xml", "A / encoder / pattern (2)", "%m", "absent")),
    # the file's own attributes: each one that differs
    ("logback.xml: owner:group mode: node has root:root 0600, import would write root:cassandra 0600",
     ("logback.xml", "group", "root", "cassandra")),
    # a unit's property, medusa.ini's key, a JMX user, a file the roles would create
    ("cassandra.service: [Service] LimitNOFILE: node has '100000', import would write '10'",
     ("cassandra.service", "[Service] LimitNOFILE", "100000", "10")),
    ("medusa.ini: [storage] bucket_name: node has 'b1', import would write 'b2'",
     ("medusa.ini", "[storage] bucket_name", "b1", "b2")),
    ("jvm17-server.options: not on the node, the roles would create it",
     ("jvm17-server.options", "the file", "absent", "present")),
    # the OS: what cassandra_linux names, its value and the one it would set
    ("cassandra_linux: sysctl vm.swappiness (/etc/sysctl.conf): node has 10, the roles would set removed",
     ("OS (cassandra_linux)", "sysctl vm.swappiness (/etc/sysctl.conf)", "10", "removed")),
])
def test_each_kind_in_its_files_own_words(text, expected):
    found = differences.read(text)
    assert [(f.file, f.where, f.node, f.ansible) for f in found][-1] == expected


def test_a_difference_that_is_no_setting_is_a_sentence():
    found = differences.read("cassandra.yaml: cannot be read as its program reads it (node's or the roles' version)")
    assert found == [differences.Finding("cassandra.yaml", None, None, None,
                                         "cannot be read as its program reads it (node's or the roles' version)")]
    found = differences.read("logback.xml: node has /configuration/a | /configuration/b, import would write"
                             " /configuration/b | /configuration/a")
    assert found[0].sentence == "the same elements, in another order"


# --- the check: XML element by element, cassandra-env.sh line by line ----------------------------------------------

NODE_XML = """<configuration scan="true">
  <appender name="AUDIT" class="R"><rollingPolicy class="P"><fileNamePattern>a.%i</fileNamePattern>
  <maxHistory>14</maxHistory></rollingPolicy></appender>
  <root level="debug"><appender-ref ref="AUDIT"/></root>
  <jmxConfigurator/>
</configuration>"""
ROLE_XML = """<configuration scan="true">
  <appender name="AUDIT" class="R"><rollingPolicy class="P"><fileNamePattern>a.%i.zip</fileNamePattern>
  </rollingPolicy></appender>
  <root level="INFO"><appender-ref ref="AUDIT"/><appender-ref ref="ASYNC"/></root>
</configuration>"""


def test_xml_differences_element_by_element():
    found = [(path, a if a is None or isinstance(a, str) else a.tag, b if b is None or isinstance(b, str) else b.tag)
             for path, a, b in xml_differences(NODE_XML, ROLE_XML)]
    assert found == [
        ('/configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern', "a.%i", "a.%i.zip"),
        ('/configuration/appender[@name="AUDIT"]/rollingPolicy/maxHistory', "maxHistory", None),
        ("/configuration/root/@level", "DEBUG", "INFO"),
        ('/configuration/root/appender-ref[@ref="ASYNC"]', None, "appender-ref"),
        ("/configuration/jmxConfigurator", "jmxConfigurator", None)]
    # the same elements in another order: none here (the self-check says it by their lines)
    assert xml_differences("<c><a/><b/></c>", "<c><b/><a/></c>") == []


def test_the_check_says_xml_by_path_and_shell_by_line():
    assert _compare("logback.xml", NODE_XML, ROLE_XML)[:2] == [
        'logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern: node has \'a.%i\','
        " import would write 'a.%i.zip'",
        'logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/maxHistory: node has'
        " <maxHistory>14</maxHistory>, import would write nothing"]
    # a line each, with the line of the other side that sets the same (" | " is a shell pipe, not a separator)
    node_env = 'JMX_PORT=7299\nJAVA_VERSION=$(java -version 2>&1 | head -1)\nJVM_OPTS="$JVM_OPTS -Dx=1"\n'
    role_env = 'JVM_OPTS="$JVM_OPTS -Dx=2"\nJMX_PORT=7199\n'
    assert _compare("cassandra-env.sh", node_env, role_env) == [
        "cassandra-env.sh: node has JMX_PORT=7299, import would write JMX_PORT=7199",
        "cassandra-env.sh: node has JAVA_VERSION=$(java -version 2>&1 | head -1), import would write nothing",
        'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dx=1", import would write JVM_OPTS="$JVM_OPTS -Dx=2"']
    # a secret in an element: masked by its name
    assert "s3cret" not in " ".join(_compare("logback.xml", "<c><password>s3cret</password></c>", "<c/>")
                                    + _compare("logback.xml", '<c><a password="s3cret"/></c>', "<c><a/></c>"))


# --- the stop -------------------------------------------------------------------------------------------------------

def logback_checks(names):
    return dict((n, {"differences": [
        'logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern: node has'
        " '/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i', import would write"
        " '/var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i.zip'",
        "logback.xml: /configuration/jmxConfigurator: node has <jmxConfigurator>, import would write nothing"]})
        for n in names)


def test_the_stop_golden():
    # the approved layout: verdict and cause, the file and its nodes once, each setting and its two values, the ways
    # out with their options, where the evidence is
    nodes = [node(i, hand=LOGBACK_EDIT if i in (1, 5, 6, 7) else None) for i in range(1, 8)]
    assert stop(nodes, logback_checks(["node1", "node5", "node6", "node7"])) == [
        u"SELF-CHECK FAILED — the inventory does not reproduce 4 nodes yet",
        "  2 settings on these nodes have no inventory variable: apply_config would",
        "  change them.",
        "",
        "  logback.xml    node1, node5..node7",
        "    AUDIT / rollingPolicy / fileNamePattern",
        "      Node     /var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i",
        "      Ansible  /var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i.zip",
        "    jmxConfigurator",
        "      Node     present",
        "      Ansible  absent",
        "",
        "  To pass:",
        "    keep logback.xml as these nodes have it",
        "      -e import_keep_hand_edits=logback.xml",
        "    take the change (apply_config writes it; its plan says what restarts)",
        "      -e import_strict=false",
        "      then review it before applying:",
        "        ansible-playbook community.cassandra.apply_config \\",
        "            -e cassandra_hosts=my_cluster --check --diff",
        "",
        "  The inventory: written for inspection only, marked NOT VALID",
        "  Evidence (full paths, lines):",
        "    reports/my_cluster/report.details.txt, SELF-CHECK"]


def test_the_stop_says_the_review_command_after_import_strict_false():
    # (owner, 2026-11-16) taking the change: the command that shows it before applying, with the run's inventory
    nodes = [node(1, hand=LOGBACK_EDIT), node(2)]
    lines = stop(nodes, logback_checks(["node1"]), inventory="inventories/prod.yml", hosts="prod", width=110)
    at = [i for i, line in enumerate(lines) if line.endswith("-e import_strict=false")][0]
    assert lines[at + 1:at + 4] == [
        "      then review it before applying:",
        "        ansible-playbook -i inventories/prod.yml community.cassandra.apply_config -e cassandra_hosts=prod \\",
        "            --check --diff"]
    assert sum("then review it before applying:" in line for line in lines) == 1


def test_the_stop_groups_by_file_and_nodes():
    # the same setting with other values on other nodes: a group each; node lists in their shortest form
    nodes = [node(i) for i in range(1, 6)]
    yaml = "cassandra.yaml: concurrent_reads: node has '%s', import would write '32'"
    checks = {"node1": {"differences": [yaml % 64]}, "node2": {"differences": [yaml % 64]},
              "node3": {"differences": [yaml % 64, "jvm17-server.options: UseZGC: node has '-XX:+UseZGC', import"
                                                   " would write nothing"]},
              "node5": {"differences": [yaml % 128]}}
    lines = stop(nodes, checks)
    assert block(lines, "  cassandra.yaml    node1..node3") == [
        "  cassandra.yaml    node1..node3", "    concurrent_reads", "      Node     64", "      Ansible  32"]
    assert "  cassandra.yaml    node5" in lines and "  jvm17-server.options    node3" in lines
    assert lines[1] == "  2 other differences that the import does not explain:"
    all_but = stop([node(i) for i in range(1, 6)], dict(("node%d" % i, {"differences": [yaml % 64]})
                                                        for i in (1, 2, 3, 5)))
    assert "  cassandra.yaml    all - node4" in all_but


def test_the_stop_says_a_file_of_yours_after_ansibles_value():
    wins = {"theirs_win": [{"node": "node1", "key": "cassandra_concurrent_reads", "path": "group_vars/all/perf.yml"}],
            "yours": [{"node": "node1", "key": "cassandra_concurrent_reads", "value": 64, "yours": 16,
                       "path": "group_vars/all/perf.yml"}]}
    checks = {"node1": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '16'"]}}
    lines = stop([node(1), node(2)], checks, layout_extra=wins)
    assert lines[1] == "  1 setting: a file of yours gives another value: apply_config would write it."
    assert block(lines, "  cassandra.yaml    node1") == [
        "  cassandra.yaml    node1", "    concurrent_reads", "      Node     64",
        "      Ansible  16   (cassandra_concurrent_reads = 16)"]
    text = " ".join(" ".join(lines).split())
    assert "in group_vars/all/perf.yml: cassandra_concurrent_reads: 64, or remove that line, then import again" in text


def test_the_stop_keeps_the_os_parts():
    checks = {"node1": {"differences": ["cassandra_linux: sysctl vm.swappiness (/etc/sysctl.conf): node has 10, the"
                                        " roles would set removed"]},
              "node2": {"differences": ["cassandra_firewall: port 7000/tcp: node has closed, the roles would set open"]}}
    lines = stop([node(1), node(2)], checks)
    assert lines[1] == "  2 settings outside the config files: the roles would change them."
    assert block(lines, "  OS (cassandra_linux)    node1") == [
        "  OS (cassandra_linux)    node1", "    sysctl vm.swappiness (/etc/sysctl.conf)", "      Node     10",
        "      Ansible  removed"]
    assert "      -e import_keep_hand_edits=sysctl,firewall" in lines or \
        [line for line in lines if line.endswith("-e import_keep_hand_edits=sysctl,firewall")]
    # no "hand edit" wording for what the import does not establish
    assert "hand edit" not in " ".join(lines).replace("import_keep_hand_edits", "")


def test_the_stop_wraps_long_values_under_80_columns_never_cut():
    value = "/".join(["/data"] + ["segment%d" % i for i in range(20)])
    checks = {"node1": {"differences": ["cassandra.yaml: cdc_raw_directory: node has '%s', import would write"
                                        " nothing" % value]}}
    lines = stop([node(1)], checks)
    rows = lines[lines.index("    cdc_raw_directory") + 1:]
    shown = []
    for line in rows:
        if line.startswith("      Ansible"):
            break
        shown.append(line[15:])
    assert "".join(shown) == value and all(len(line) <= 80 for line in lines)
    assert all(line.startswith(" " * 15) for line in rows[1:len(shown)])


def test_the_stop_never_shows_a_secret():
    checks = {"node1": {"differences": [
        "cassandra.yaml: server_encryption_options.keystore_password: node has 'hunter2', import would write 'x'",
        "logback.xml: /configuration/appender[@name=\"DB\"]/password: node has 'hunter2', import would write nothing"]}}
    for lines in (stop([node(1, hand=LOGBACK_EDIT)], checks), stop([node(1)], checks, width=40)):
        assert "hunter2" not in "\n".join(lines)


def test_the_report_has_the_stop_under_todo_and_the_evidence_in_the_details():
    nodes = [node(i, hand=LOGBACK_EDIT if i in (1, 2) else None) for i in range(1, 4)]
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    checks = logback_checks(["node1", "node2"])
    args = (layout, ["/p/inventories/my_cluster.yml", "/p/inventories"], REPORT, checks, False)
    lines = cassandra_import_report(*args, cwd="/p", in_git=True, details=False)
    at = lines.index(u"  SELF-CHECK — the inventory does not reproduce 2 nodes yet")
    assert lines[at + 3:at + 6] == ["    logback.xml    all - node3", "      AUDIT / rollingPolicy / fileNamePattern",
                                    "        Node     /var/log/cassandra/audit/audit.log.%d{yyyy-MM-dd}.%i"]
    assert "configuration/appender" not in "\n".join(lines)  # (the full path: evidence, in the details)
    # HAND EDITS: the file and its nodes once, the settings named where they are, the ways out once
    hand = lines[lines.index("==== HAND EDITS " + "=" * 84):]
    assert "    logback.xml (the logging settings)    all - node3" in hand
    assert "      2 settings no variable carries: SELF-CHECK, under TO DO" in hand
    details = cassandra_import_report(*args, cwd="/p", in_git=True, details=True)
    evidence = details[details.index([d for d in details if d.startswith("==== SELF-CHECK")][0]):]
    assert "  all - node3 (2 nodes)" in evidence
    assert ('    logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern: node has'
            in " ".join(evidence))


def test_the_screen_shows_the_hand_edits_whole_without_the_stop():
    # import_strict: false: no stop below the screen, the HAND EDITS show what differs
    nodes = [node(1, hand=LOGBACK_EDIT), node(2)]
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    args = (layout, ["/p/inventories/my_cluster.yml", "/p/inventories"], REPORT, logback_checks(["node1"]), False)
    screen = cassandra_import_report(*args, cwd="/p", in_git=True, screen=True, width=80, strict=False)
    assert "      AUDIT / rollingPolicy / fileNamePattern" in screen and "        Node     present" in screen
    strict = cassandra_import_report(*args, cwd="/p", in_git=True, screen=True, width=80, strict=True)
    assert "      AUDIT / rollingPolicy / fileNamePattern" not in strict
    assert [line for line in strict if "no variable carries: the SELF-CHECK FAILED block" in line]


# --- the shareable copy -------------------------------------------------------------------------------------------

def test_the_shareable_copy_keeps_the_settings_names():
    nodes = [node(1, hand=["cassandra-env.sh, line 300:", "  + x"]), node(2)]
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    checks = {"node1": {"differences": [
        'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dchronicle.analytics.disable=true", import would write'
        " nothing",
        "medusa.ini: [storage] search_obscurekey_from_name: node has 'zorblax.example.org', import would write"
        " nothing",
        'logback.xml: /configuration/logger[@name="com.zorblax.payments"]: node has <logger level="DEBUG"'
        ' name="com.zorblax.payments">, import would write nothing']}}
    lines = stop(nodes, checks)
    text = "\n".join(cassandra_import_shareable(lines + ["  " + d for d in checks["node1"]["differences"]],
                                                layout)["text"])
    for name in ("    -Dchronicle.analytics.disable", "    [storage] search_obscurekey_from_name"):
        assert name in text.split("\n"), text
    # the settings of the evidence too; the values, the node names and what the site named (a logger's package:
    # pre-push audit, rc13) masked
    assert "zorblax" not in text and "node1" not in text
    assert re.search(r"logback\.xml: /configuration/logger\[@name=\"domain\d+\"\]: node has", text), text
    assert re.search(r"medusa\.ini: \[storage\] search_obscurekey_from_name: node has '\S+'", text)
