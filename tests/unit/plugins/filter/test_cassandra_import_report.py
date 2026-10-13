from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import's report and the screen that ends the run: one setting per line, each value with the nodes that have it.

import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    _compress, cassandra_import_report, cassandra_import_self_check_advice)

ARROW = u"\u2190"
WRITTEN = ["/p/inventories/my_cluster.yml", "/p/inventories"]
REPORT = "/p/reports/my_cluster/report.txt"
REPORT_CHECK = "/p/reports/my_cluster/report.check.txt"
STRICT_OFF = "-e import_cluster_strict=false only writes the inventory anyway, for inspection (not safe to apply)"


def node(i, heap="8G", hand=None, read=True, **extra):
    return dict({"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": read,
                 "reason": "unreachable", "hand_edits": hand or [], "normalized": [], "notes": [],
                 "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16, "cassandra_max_heap_size": heap,
                          "cassandra_seeds": ["node1", "node3"], "cassandra_jmx_password": "s3cret"}}, **extra)


def report(nodes, ok=True, **kwargs):
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    kwargs.setdefault("cwd", "/p")
    kwargs.setdefault("in_git", True)
    kwargs.setdefault("width", 160)
    return cassandra_import_report(layout, WRITTEN, kwargs.pop("report_file", REPORT), kwargs.pop("self_check", {}), ok,
                                   **kwargs)


def test_compress():
    assert _compress(["node3", "node1", "node2", "web", "node5"]) == "node1..node3, node5, web"
    assert _compress(["n1", "n2"]) == "n1, n2"
    assert _compress(["a", "b"]) == "a, b"
    assert _compress(["node%d" % i for i in range(1, 8)]) == "node1..node7"  # no count in a path


def test_clean_import_ready():
    lines = report([node(i) for i in range(1, 6)])
    assert lines[:4] == [u"IMPORT my_cluster — 5 nodes read / 5 — SELF-CHECK PASSED",
                         "Written: inventories/my_cluster.yml, group_vars/my_cluster*/",
                         "Report:  reports/my_cluster/report.txt", ""]
    assert lines[4] == u"READY — nothing to do; review and commit:  git diff && git commit"
    text = "\n".join(lines)
    assert "s3cret" not in text
    assert "  cassandra_jmx_password:  (in secrets.yml)   all" in lines
    assert "  cassandra_seeds:         node1, node3       all" in lines
    assert "HAND EDITS" not in text and "differs" not in text
    assert lines[-1] != "" and "NEXT" in lines and "DETAILS" in lines
    # NEXT: no -i when the inventory is ansible.cfg's, no -e cassandra_hosts for a cluster alone
    at = lines.index("NEXT")
    assert lines[at + 1:at + 3] == [
        u"  ansible-playbook community.cassandra.apply_config --check --diff   → expect: nothing to apply",
        u"  ansible-playbook community.cassandra.topology --check              → expect: nothing to do"]
    lines = report([node(1)], inventory="/p/inventories", hosts="my_cluster")
    assert ("  ansible-playbook -i inventories community.cassandra.apply_config -e cassandra_hosts=my_cluster"
            u" --check --diff   → expect: nothing to apply") in lines


def test_review_step_only_with_git():
    # Q8: the git step only when the inventory dir is in a git work tree
    lines = report([node(i) for i in range(1, 3)], in_git=False)
    assert lines[4] == u"READY — nothing to do; review the files written in inventories"
    lines = report([node(1, hand=["cassandra.yaml, line 10:", "  + concurrent_writes: 48"])], in_git=False)
    assert "  2. Review the files written in inventories" in lines and "git" not in "\n".join(lines[:8])


def test_values_grouped_with_their_nodes():
    lines = report([node(1), node(2), node(3), node(4, "16G"), node(5, "16G")])
    at = lines.index("  cassandra_max_heap_size: 8G                 node1, node2, node3")  # the report: every name
    assert lines[at + 1] == u"                           16G                node4, node5          ← differs (host_vars)"
    # (the heap per host)
    assert lines[1] == ("Written: inventories/my_cluster.yml, group_vars/my_cluster*/,"
                        " host_vars/<node>/ (node1, node2, node3, node4, node5)")
    lines = report([node(1, keep={"cassandra_firewall_manage": False}), node(2)])
    assert lines[1] == "Written: inventories/my_cluster.yml, group_vars/my_cluster*/, host_vars/node1/"
    seven = [node(i) for i in range(1, 5)] + [node(i, "16G") for i in range(5, 8)]
    assert report(seven, screen=True)[1].endswith("host_vars/<node>/ (node1..node7)")  # the screen: a range
    screen = report([node(i) for i in range(1, 5)] + [node(i, "16G") for i in range(5, 7)], screen=True)
    assert "  cassandra_max_heap_size: 8G                 node1..node4" in screen  # the screen: ranges


def test_things_to_do_and_hand_edits():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(2, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(3, hand=["jvm-server.options, line 3:", "  + -Dcassandra.weird=1"], normalized=["x"]),
             node(4, read=False)]
    lines = report(nodes, ok=False, secrets_clear=["group_vars/my_cluster/secrets.yml"],
                   leftovers={"unsure": ["host_vars/old/main.yml"]},
                   self_check={"node1": {"differences": ["cassandra.yaml: concurrent_writes"], "notes": []}})
    assert lines[0] == u"IMPORT my_cluster — 3 nodes read / 4 — SELF-CHECK FAILED"
    at = lines.index("TO DO (5)")
    assert lines[at + 1:at + 6] == [
        "  1. Not read: node4 (unreachable): start Cassandra or fix the access, then import again"
        " (or -e import_cluster_allow_unread=true)",
        "  2. Hand edits the roles would revert: concurrent_writes, -Dcassandra.weird (see below)",
        "  3. Self-check: only hand edits no variable covers, in cassandra.yaml (node1): -e"
        " import_cluster_keep_hand_edits=true leaves those files as they are on their nodes (apply_config leaves"
        " them) and the self-check passes; " + STRICT_OFF + " (see DETAILS)",
        "  4. Passwords written in clear: cd inventories && ansible-vault encrypt group_vars/my_cluster/secrets.yml",
        "  5. Files of an earlier import whose cluster is not known, kept: host_vars/old/main.yml (remove them if they"
        " are this cluster's, or import again with -e import_cluster_adopt=true)"]
    at = lines.index(u"HAND EDITS — no variable covers them; cassandra_config would revert")
    assert lines[at + 1:at + 4] == ["  cassandra.yaml concurrent_writes:     48   node1, node2",
                                    "  jvm-server.options -Dcassandra.weird: 1    node3",
                                    "  + 1 layout-only edits, no effect (details at the end)"]
    # the full detail at the end
    details = lines[lines.index("DETAILS"):]
    assert "  SELF-CHECK node1: the roles would change" in details and "        + concurrent_writes: 48" in details


def test_left_as_it_is_grouped_by_nodes():
    keep = {"cassandra_repository_manage": False, "cassandra_linux_manage": False, "cassandra_firewall_manage": False}
    nodes = [node(i, keep=dict(keep)) for i in range(1, 4)] + [node(4, keep={"cassandra_firewall_manage": False})]
    lines = report(nodes)
    at = lines.index("LEFT AS IT IS (*_manage: false)")
    assert lines[at + 1:at + 3] == ["  repositories, OS settings:  node1, node2, node3", "  firewall:                   all"]


def test_screen_and_check():
    lines = report([node(i) for i in range(1, 4)], check=True, screen=True, report_file=REPORT_CHECK,
                   leftovers={"stale": ["host_vars/gone/main.yml"]})
    assert lines[0] == u"IMPORT my_cluster (--check, inventory not written) — 3 nodes read / 3 — SELF-CHECK PASSED"
    assert lines[1].startswith("Would write: ") and lines[2] == ""  # the report's path: last
    assert "TO DO (1)" in lines and "  1. Write it: the same command without --check" in lines
    assert lines[-1] == "full report (--check): reports/my_cluster/report.check.txt"  # written, report.txt left
    assert "HAND EDITS" not in "\n".join(lines) and "NEXT" not in lines and "DETAILS" not in lines
    full = report([node(i) for i in range(1, 4)], check=True, leftovers={"stale": ["host_vars/gone/main.yml"]})
    assert "Would be removed (an earlier import's, not written again): host_vars/gone/main.yml" in full


def test_where_each_value_is_kept():
    nodes = [dict(node(1, "8G"), dc="dc1"), dict(node(2, "8G"), dc="dc1"), dict(node(3, "16G"), dc="dc2"),
             dict(node(4, "16G"), dc="dc2"), dict(node(5, "4G"), dc="dc3"), dict(node(6, "4G"), dc="dc3"),
             dict(node(7, "4G"), dc="dc3")]
    lines = report(nodes)
    at = [i for i, line in enumerate(lines) if line.startswith("  cassandra_max_heap_size:")][0]
    assert lines[at].endswith("node5, node6, node7")
    assert sorted(lines[at + 1:at + 3]) == [
        u"                           16G                node3, node4          ← differs (group_vars/my_cluster_dc2)",
        u"                           8G                 node1, node2          ← differs (group_vars/my_cluster_dc1)"]


def test_self_check_named_with_hand_edits_elsewhere_and_a_note():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  + concurrent_writes: 48"]),
             node(2, hand=["jmxremote.password: its users NOT imported: set cassandra_jmx_users by hand"])]
    checks = {"node3": {"differences": ["owner, group and mode: x"], "notes": []},
              "node1": {"differences": ["cassandra.yaml: y"], "notes": []}}
    lines = report(nodes, ok=False, self_check=checks)
    assert ("  2. Self-check: the roles would change these settings: owner, group and mode: x (node3); fix them first"
            " (-e import_cluster_keep_hand_edits=true covers only the hand edits, in cassandra.yaml (node1)); "
            + STRICT_OFF + " (see DETAILS)") in lines
    assert not [line for line in lines if "Review then commit" in line]  # not a valid inventory
    assert [line for line in lines if line.startswith("  jmxremote.password:")][0].split()[1:4] == [
        "its", "users", "NOT"]


def test_differs_from_your_group_vars_all():
    """Real case: group_vars/all sets the config files' owner; 2 nodes root:cassandra, 3 cassandra:cassandraops."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [node(i) for i in range(1, 6)]
    for n in nodes[:2]:
        n["vars"].update(cassandra_config_user="root", cassandra_config_group="cassandra")
    for n in nodes[2:]:
        n["vars"].update(cassandra_config_user="cassandra", cassandra_config_group="cassandraops")
    layout = cassandra_inventory_layout_over(nodes, "My Cluster", [{
        "path": "group_vars/all/standard.yml",
        "content": "cassandra_config_user: cassandra\ncassandra_config_group: cassandraops\n"}])
    lines = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True)
    assert ("  1. Differs from your group_vars/all, kept as found: cassandra_config_group, cassandra_config_user"
            " (see DIFFERS FROM YOUR group_vars/all)") in lines
    at = lines.index(u"DIFFERS FROM YOUR group_vars/all — kept as found; delete the line to apply your standard")
    assert lines[at + 1:at + 5] == [
        "  cassandra_config_group:  yours: cassandraops (group_vars/all/standard.yml)",
        u"                           cassandra   node1, node2   \u2190 kept, in host_vars",
        "  cassandra_config_user:   yours: cassandra (group_vars/all/standard.yml)",
        u"                           root        node1, node2   \u2190 kept, in host_vars"]
    # SETTINGS: the value the others get from group_vars/all, not "the collection's default"
    at = [i for i, line in enumerate(lines) if line.startswith("  cassandra_config_user:")][0]
    assert lines[at].split()[1:] == ["cassandra", "node3,", "node4,", "node5"]
    # the screen has it too, its nodes as a range, then the report's path
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True,
                                     width=160)
    at = screen.index(u"DIFFERS FROM YOUR group_vars/all — kept as found; delete the line to apply your standard")
    assert screen[at + 2] == u"                           cassandra   node1, node2   \u2190 kept, in host_vars"
    assert screen[-2:] == ["", "full report: reports/my_cluster/report.txt"]
    assert any("(see DIFFERS FROM YOUR group_vars/all below)" in line for line in screen)


def test_differs_one_line_per_file_of_yours():
    # a value of yours in group_vars/all, another in a host's own file: each with its nodes
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [node(i) for i in range(1, 4)]
    for n in nodes:
        n["vars"].update(cassandra_config_user="cassandra")
    layout = cassandra_inventory_layout_over(nodes, "My Cluster", [
        {"path": "group_vars/all/s.yml", "content": "cassandra_config_user: zz\n"},
        {"path": "host_vars/node1/mine.yml", "content": "cassandra_config_user: yy\n"}])
    lines = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True)
    at = lines.index(u"DIFFERS FROM YOUR OWN VARIABLES — kept as found; delete the line to apply yours")
    text = "\n".join(lines[at:at + 6])
    assert "yours: zz (group_vars/all/s.yml)" in text and "yours: yy (host_vars/node1/mine.yml)" in text


SETTINGS_3 = [
    u"SETTINGS — not the collection's default",
    "  cassandra_seeds:         node1, node3       all",
    "  cassandra_num_tokens:    16                 all",
    "  cassandra_version:       50x                all",
    "  cassandra_jmx_password:  (in secrets.yml)   all",
    "  cassandra_max_heap_size: 8G                 all",
]


def test_reimport_check_with_nothing_to_change_is_ready():
    # the whole screen: header, verdict, SETTINGS, the report's path; nothing else
    unchanged = {"changed": [], "removed": [], "existed": ["my_cluster.yml", "group_vars/my_cluster/main.yml"]}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=unchanged)
    assert screen == [
        u"IMPORT my_cluster (--check, inventory not written) — 3 nodes read / 3 — SELF-CHECK PASSED",
        "Unchanged: inventories/my_cluster.yml, group_vars/my_cluster*/",
        "",
        u"READY — nothing to change",
        ""] + SETTINGS_3 + [
        "",
        "full report (--check): reports/my_cluster/report.txt"]
    # the same without --check: nothing to review either
    screen = report([node(i) for i in range(1, 4)], screen=True, changes=unchanged)
    assert screen[3] == u"READY — nothing to change" and screen[-1] == "full report: reports/my_cluster/report.txt"
    # a thing to do still says so, without "Write it"
    screen = report([node(1), node(2), node(3, read=False)], check=True, screen=True, changes=unchanged)
    assert screen[3:5] == ["TO DO (1)", "  1. Not read: node3 (unreachable): start Cassandra or fix the access, then"
                                        " import again (or -e import_cluster_allow_unread=true)"]


def test_check_says_what_it_would_write():
    # a first import: every file new
    first = {"changed": ["my_cluster.yml", "group_vars/my_cluster/main.yml", "group_vars/my_cluster/secrets.yml"],
             "removed": [], "existed": []}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=first)
    assert screen[1] == "Would write: inventories/my_cluster.yml, group_vars/my_cluster*/"
    assert screen[3:5] == ["TO DO (1)", "  1. Write it: the same command without --check (3 new files)"]
    # a re-import: the files that change, the ones it removes
    again = {"changed": ["group_vars/my_cluster/main.yml", "host_vars/node4/main.yml"],
             "removed": ["host_vars/gone/main.yml"], "existed": ["my_cluster.yml", "group_vars/my_cluster/main.yml"]}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=again)
    assert screen[4] == ("  1. Write it: the same command without --check (1 new file; changed:"
                         " group_vars/my_cluster/main.yml; removed: host_vars/gone/main.yml)")
    # changes not known (an older caller): as before
    assert "  1. Write it: the same command without --check" in report([node(1)], check=True, screen=True)


def test_a_dict_value_one_row_per_sub_key():
    nodes = [node(i) for i in range(1, 6)]
    for i, n in enumerate(nodes):
        n["vars"]["cassandra_extra_settings"] = {"streaming_connections_per_host": 2 if i < 3 else 1,
                                                 "sstable": {"selected_format": "bti"},
                                                 "server_encryption_options": {"keystore_password": "pw%d" % i}}
    del nodes[4]["vars"]["cassandra_extra_settings"]["sstable"]
    lines = report(nodes)
    text = "\n".join(lines)
    assert "pw0" not in text and "pw3" not in text
    at = [i for i, line in enumerate(lines) if line.startswith("  cassandra_extra_settings.streaming_connections")][0]
    assert lines[at].split()[1:] == ["2", "node1,", "node2,", "node3"]
    assert lines[at + 1].split()[:4] == ["1", "node4,", "node5", ARROW]
    sstable = [line for line in lines if line.startswith("  cassandra_extra_settings.sstable.selected_format:")]
    assert sstable[0].split()[1:] == ["bti", "node1,", "node2,", "node3,", "node4"]
    assert "(not set)" in lines[lines.index(sstable[0]) + 1]
    secret = [i for i, line in enumerate(lines) if "server_encryption_options.keystore_password" in line][0]
    assert "(in secrets.yml)" in lines[secret + 1] and "(in secrets.yml, value 5)" in "\n".join(lines[secret:secret + 7])
    assert "{" not in "\n".join(line for line in lines if line.startswith("  cassandra_extra_settings"))


def test_a_secret_named_sub_key_is_hidden_whatever_its_parents():
    nodes = [node(i) for i in range(1, 3)]
    for n in nodes:
        n["vars"]["cassandra_medusa_extra_settings"] = {"storage": {"pw": "plain1", "bucket": "b"}}
    lines = report(nodes)
    assert "plain1" not in "\n".join(lines)
    assert [line.split()[1] for line in lines if ".storage.bucket:" in line] == ["b"]


def test_hand_edits_by_file_counted():
    logback = ["logback.xml, line %d:" % i for i in (10, 20, 30)]
    same = [line for head in logback for line in (head, "  - <a/>", "  + <b/>", "  + <c/>")]
    other = same[:4]
    nodes = [node(1, hand=same), node(2, hand=same), node(3, hand=other),
             node(4, hand=same + ["jvm11-server.options, line 3:", "  + -XX:A=1", "  + -XX:B=2"]),
             node(5, hand=["cassandra.yaml, line 10:", "  + concurrent_writes: 48"] * 2)]
    lines = report(nodes)
    assert "  1. Hand edits the roles would revert: logback.xml, jvm11-server.options, concurrent_writes" \
           " (see below)" in lines  # each name once
    at = lines.index(u"HAND EDITS — no variable covers them; cassandra_config would revert")
    assert lines[at + 1:at + 5] == ["  cassandra.yaml concurrent_writes: 48        node5",
                                    "  logback.xml:                      3 edits   node1, node2, node4",
                                    "                                    1 edit    node3                 %s differs" % ARROW,
                                    "  jvm11-server.options:             1 edit    node4"]
    # two sets of as many edits: told apart, numbered among them
    nodes = [node(1, hand=same), node(2, hand=other), node(3, hand=[line.replace("<c/>", "<d/>") for line in same])]
    lines = report(nodes)
    at = [i for i, line in enumerate(lines) if line.startswith("  logback.xml:")][0]
    assert [re.search(r"\d+ edits?( \(variant \d\))?", line).group(0) for line in lines[at:at + 3]] == [
        "1 edit", "3 edits (variant 1)", "3 edits (variant 2)"]


def test_written_host_vars_counted_on_a_large_cluster():
    nodes = [node(i, "16G" if i % 4 == 1 else "8G") for i in range(1, 60, 2)]  # node1, node3...: no range
    lines = report(nodes, screen=True)
    assert lines[1].endswith("host_vars/<node>/ of 30 nodes")
    again = {"changed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)] + ["group_vars/my_cluster/main.yml"],
             "removed": [], "existed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)]
             + ["group_vars/my_cluster/main.yml"]}
    lines = report(nodes, screen=True, check=True, changes=again)
    assert ("  1. Write it: the same command without --check (changed: host_vars/<node>/main.yml of 29 nodes,"
            " group_vars/my_cluster/main.yml)") in lines


def test_the_screen_fits_the_terminal():
    nodes = [node(i, hand=["cassandra.yaml, line 10:", "  + setting_%d: 1" % i]) for i in range(1, 20)]
    for line in report(nodes, screen=True, width=80)[1:]:
        assert len(line) <= 80, line


def test_a_secret_setting_of_a_hand_edit_is_never_shown():
    # (masked where the edits are made; this one as a line might come)
    lines = report([node(1, hand=["cassandra.yaml, line 10:", "  + ldap_password: s3cret"])])
    assert "s3cret" not in "\n".join(lines)
    assert [line.split()[2] for line in lines if line.startswith("  cassandra.yaml ldap_password:")] == ["****"]


def test_the_screen_width_is_ansibles(monkeypatch):
    from ansible_collections.community.cassandra.plugins.filter import cassandra_import_report as module

    class Narrow(object):
        columns = 70
    monkeypatch.setattr(module, "Display", Narrow)
    nodes = [node(i, hand=["cassandra.yaml, line 10:", "  + setting_%d: 1" % i]) for i in range(1, 20)]
    screen = cassandra_import_report(cassandra_inventory_layout(nodes, "My Cluster"), WRITTEN, REPORT, {}, True,
                                     cwd="/p", in_git=True, screen=True)
    assert max(len(line) for line in screen[1:]) <= 70 < max(len(line) for line in report(nodes, screen=True))


def test_keys_cassandra_rejects_reported():
    rejected = {"keys": ["fake_option"], "release": "4.1.5"}
    nodes = [node(1, not_settings=rejected), node(2, not_settings=rejected), node(3, not_settings=rejected),
             node(4, not_settings={"keys": ["fake_option", "other_fake"], "release": "4.1.6"}), node(5)]
    lines = report(nodes)
    at = lines.index("TO DO (3)")
    assert lines[at + 1:at + 4] == [
        "  1. node1, node2, node3: cassandra.yaml has fake_option, which Cassandra 4.1.5 rejects: it would not"
        " restart; remove it",
        "  2. node4: cassandra.yaml has fake_option, other_fake, which Cassandra 4.1.6 rejects: it would not restart;"
        " remove them",
        "  3. Review then commit:  git diff && git commit"]
    at = lines.index(u"NOT A CASSANDRA SETTING — not imported: Cassandra refuses to start with it, cassandra_config"
                     " leaves it out")
    assert lines[at + 1:at + 5] == ["  fake_option  node1, node2, node3 (Cassandra 4.1.5)",
                                    "  fake_option  node4 (Cassandra 4.1.6)",
                                    "  other_fake   node4 (Cassandra 4.1.6)", ""]
    # the screen: the TO DO, nodes in short
    screen = report(nodes, screen=True)
    assert ("  1. node1..node3: cassandra.yaml has fake_option, which Cassandra 4.1.5 rejects: it would not restart;"
            " remove it") in screen
    assert not [line for line in screen if line.startswith("NOT A CASSANDRA SETTING")]
    # none: no TO DO, no section
    assert not [line for line in report([node(1)]) if "rejects" in line or "NOT A CASSANDRA" in line]


def test_keys_cassandra_rejects_leave_the_report_width():
    # the sections after NOT A CASSANDRA SETTING keep the report's width (a key's is not the line's)
    os_lines = {"lines": ["sysctl vm.max_map_count: 65530 (1048575)"]}
    nodes = [node(i, os=os_lines) for i in range(1, 4)]

    def tuning(lines):
        at = lines.index(u"OS TUNING \u2014 live value (collection's value)")
        return lines[at + 1:at + 3]
    plain = tuning(report(nodes))
    assert plain[0].endswith("(1048575)   all") and plain[1] == ""
    nodes[0]["not_settings"] = {"keys": ["x"], "release": "4.1.5"}
    assert tuning(report(nodes)) == plain


LOGBACK = ["logback.xml, line 5:", "  + <logger name=\"x\" level=\"DEBUG\"/>"]


def test_self_check_advice_hand_edits_only():
    # every difference in a file with hand edits: import_cluster_keep_hand_edits, not import_cluster_strict
    nodes = [node(1, hand=LOGBACK), node(2, hand=LOGBACK), node(3)]
    checks = {"node1": {"differences": ["logback.xml: logger x: node has DEBUG, import would write nothing"]},
              "node2": {"differences": ["logback.xml: logger x: node has DEBUG, import would write nothing"]}}
    advice = cassandra_import_self_check_advice(cassandra_inventory_layout(nodes, "My Cluster"), checks)
    assert advice == ("Only hand edits no variable covers, in logback.xml (node1, node2): -e"
                      " import_cluster_keep_hand_edits=true leaves those files as they are on their nodes (apply_config"
                      " leaves them) and the self-check passes; " + STRICT_OFF)
    todo = [line for line in report(nodes, ok=False, self_check=checks) if "Self-check:" in line][0]
    assert "import_cluster_keep_hand_edits=true leaves those files" in todo and "logback.xml (node1, node2)" in todo


def test_self_check_advice_settings_and_hand_edits():
    # settings differ too: listed first, with their nodes; keep_hand_edits covers the hand-edit files only
    nodes = [node(1, hand=LOGBACK), node(2), node(3)]
    checks = {"node1": {"differences": ["logback.xml: logger x: node has DEBUG, import would write nothing",
                                        "logback.xml: owner:group mode: node has root:root 0644, import would write"
                                        " cassandra:cassandra 0644"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_writes: node has 32, import would write 64",
                                        "heap (cassandra-env.sh, else the unit's Environment): MAX_HEAP_SIZE: node"
                                        " has 8G, import would write 16G"]},
              "node3": {"differences": ["cassandra.yaml: concurrent_writes: node has 32, import would write 64"]}}
    advice = cassandra_import_self_check_advice(cassandra_inventory_layout(nodes, "My Cluster"), checks)
    assert advice == ("The roles would change these settings: logback.xml owner:group mode (node1), cassandra.yaml"
                      " concurrent_writes (node2, node3), heap (cassandra-env.sh, else the unit's Environment):"
                      " MAX_HEAP_SIZE (node2); fix them first (-e import_cluster_keep_hand_edits=true covers only the"
                      " hand edits, in logback.xml (node1)); " + STRICT_OFF)


def test_self_check_advice_settings_only():
    # no hand edits: no keep_hand_edits; a difference in a file the inventory already keeps is a setting
    kept = {"cassandra_config_keep_files": ["logback.xml"]}
    nodes = [node(1, hand=LOGBACK, keep=kept), node(2)]
    checks = {"node1": {"differences": ["logback.xml: x: node has 1, import would write 2"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_writes: node has 32, import would write 64"]}}
    advice = cassandra_import_self_check_advice(cassandra_inventory_layout(nodes, "My Cluster"), checks)
    assert advice == ("The roles would change these settings: logback.xml x (node1), cassandra.yaml concurrent_writes"
                      " (node2); fix them first; " + STRICT_OFF)
    # none (the self-check could not run): import_cluster_strict alone
    assert cassandra_import_self_check_advice(cassandra_inventory_layout(nodes, "My Cluster"), {}) == \
        STRICT_OFF[0].upper() + STRICT_OFF[1:]
