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


def node(i, heap="8G", hand=None, read=True, **extra):
    return dict({"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": read,
                 "reason": "unreachable", "hand_edits": hand or [], "normalized": [], "notes": [],
                 "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16, "cassandra_max_heap_size": heap,
                          "cassandra_seeds": ["node1", "node3"], "cassandra_jmx_password": "s3cret"}}, **extra)


def report(nodes, ok=True, layout_extra=None, **kwargs):
    layout = dict(cassandra_inventory_layout(nodes, "My Cluster"), **(layout_extra or {}))
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
        "  3. Self-check: the inventory would change 1 setting on 1 node (how to make it pass: SELF-CHECK below)",
        "  4. Passwords written in clear: cd inventories && ansible-vault encrypt group_vars/my_cluster/secrets.yml",
        "  5. Files of an earlier import whose cluster is not known, kept: host_vars/old/main.yml (remove them if they"
        " are this cluster's, or import again with -e import_cluster_adopt=true)"]
    at = lines.index(u"HAND EDITS — no variable covers them; cassandra_config would revert")
    assert lines[at + 1:at + 4] == ["  cassandra.yaml concurrent_writes:     48   node1, node2",
                                    "  jvm-server.options -Dcassandra.weird: 1    node3",
                                    "  + 1 layout-only edits, no effect (details at the end)"]
    # the self-check's differences in a section of their own, each node's under its item; the hand edits in DETAILS
    at = lines.index(u"SELF-CHECK — FAILED: the inventory would change 1 setting on 1 node; 1 node not read")
    assert lines[at + 1:at + 5] == ["  Hand edits no variable covers (cassandra_config would revert them):",
                                    "    - cassandra.yaml, on node1:", "        concurrent_writes",
                                    "          node1: cassandra.yaml: concurrent_writes"]
    details = lines[lines.index("DETAILS"):]
    assert "        + concurrent_writes: 48" in details and not [line for line in details if "SELF-CHECK" in line]


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
    assert "  2. Self-check: the inventory would change 2 settings on 2 nodes (how to make it pass: SELF-CHECK below)" \
        in lines
    at = lines.index("HOW TO MAKE IT PASS")
    assert [line.split()[0] for line in lines[at + 1:] if line.startswith("  ") and not line.startswith("   ")][:3] == [
        "hand", "other", "a"]
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


def test_a_secret_value_on_several_lines_of_a_hand_edit_is_never_shown():
    # (masked where the edits are made, from the whole file; the report and the screen mask each hunk again)
    hand = ["cassandra.yaml, line 10:", "  - #ldap_password: x", "  + ldap_password: |", "  +   LEAK1", "  +   LEAK2",
            "cassandra-env.sh, line 3:", '  + export X_PASSWORD="LEAK3', '  + LEAK4"']
    for keep in (None, {"cassandra_config_keep_files": ["cassandra.yaml", "cassandra-env.sh"]}):
        extra = {"keep": keep} if keep else {}
        for kwargs in ({}, {"screen": True}, {"check": True, "report_file": REPORT_CHECK}):
            text = "\n".join(report([node(1, hand=hand, **extra), node(2)], **kwargs))
            assert re.findall(r"LEAK\d", text) == [], (keep, kwargs)
        assert "ldap_password: ****" in cassandra_inventory_layout([node(1, hand=hand, **extra)], "c")["report"]
    # a head too: a properties key as Java reads it holds no value, masked all the same
    head = "cassandra-rackdc.properties: keystore.password:c2VjcmV0SEC=="
    assert "c2VjcmV0" not in cassandra_inventory_layout([node(1, hand=[head])], "c")["report"]


def test_kept_files_said_kept_as_is():
    # a file with hand edits kept (import_cluster_keep_hand_edits): why, in the rows of what is kept
    hand = ["jmxremote.access: auditRole=readonly", "  + auditRole=readonly"]
    text = "\n".join(report([node(1, hand=hand, keep={"cassandra_config_keep_files": ["jmxremote.access"]}), node(2)]))
    assert re.search(r"kept as is \(hand edits the collection's variables cannot express yet\):?\s+jmxremote\.access", text)


def test_the_screen_width_is_ansibles(monkeypatch):
    from ansible_collections.community.cassandra.plugins.filter import cassandra_output as module

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


HEAP = "heap (cassandra-env.sh, else the unit's Environment)"
# what the self-check gives: a hand edit (its secret masked), a JVM option, a value a file of yours keeps, a secret
# in clear (masked here all the same), a file the roles would create, a line of its own
CHECKS = {"node1": {"differences": ["cassandra-env.sh: node has export KEYSTORE_PASSWORD=****, import would write nothing",
                                    "cassandra.yaml: server_encryption_options.keystore_password: node has 'hunter2',"
                                    " import would write 'cassandra'",
                                    "jvm17-server.options: not on the node, the roles would create it"]},
          "node2": {"differences": ["jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write nothing",
                                    "jvm17-server.options: not on the node, the roles would create it",
                                    "owner, group and mode: not read on the node, the roles may change them"]},
          "node3": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '16'",
                                    HEAP + ": MAX_HEAP_SIZE: node has '8G', import would write '16G'"]},
          "node5": {"differences": ["jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write nothing"]}}
WINS = {"theirs_win": [{"node": "node3", "key": "cassandra_concurrent_reads", "path": "host_vars/node3/local.yml"}],
        "yours": [{"node": "node3", "key": "cassandra_concurrent_reads", "value": 64, "yours": 16,
                   "path": "host_vars/node3/local.yml"}]}


def failed_nodes():
    jvm = ["jvm17-server.options, line 40:", "  + -XX:+UseFoo"]
    return [node(1, hand=["cassandra-env.sh, line 311:", "  + export KEYSTORE_PASSWORD=****"]), node(2, hand=jvm),
            node(3), node(4, read=False), node(5, hand=jvm)]


def stop(nodes, checks, layout_extra=None, **kwargs):
    layout = dict(cassandra_inventory_layout(nodes, "My Cluster"), **(layout_extra or {}))
    kwargs.setdefault("report_file", REPORT)
    kwargs.setdefault("cwd", "/p")
    kwargs.setdefault("width", 100)
    return cassandra_import_self_check_advice(layout, checks, **kwargs)


def test_self_check_stop_golden():
    # the verdict, what differs grouped by what makes it pass, how, where the details are; blank lines between
    assert stop(failed_nodes(), CHECKS, WINS) == [
        "SELF-CHECK FAILED  my_cluster  the inventory would change 7 settings on 4 nodes; 1 node not read",
        "  (written for inspection only, marked NOT VALID)",
        "",
        "  Hand edits no variable covers (cassandra_config would revert them):",
        "    - cassandra-env.sh, on node1:",
        "        export KEYSTORE_PASSWORD=****",
        "    - jvm17-server.options, on node2, node5:",
        "        -XX:+UseFoo (not in the inventory)",
        "  Your inventory files override the import (Ansible will apply your value):",
        "    - cassandra.yaml concurrent_reads, on node3:",
        "        node 64, inventory 16 (set in host_vars/node3/local.yml)",
        "  Other differences:",
        "    - cassandra.yaml server_encryption_options.keystore_password, on node1:",
        "        node ****, inventory ****",
        "    - jvm17-server.options, on node1, node2:",
        "        not on the node, the roles would create it",
        "    - owner, group and mode, on node2:",
        "        not read on the node, the roles may change them",
        "    - heap MAX_HEAP_SIZE, on node3:",
        "        node 8G, inventory 16G",
        "  Not read, so not checked: node4 (unreachable)",
        "",
        "HOW TO MAKE IT PASS",
        "  hand edits          to keep them as they are: import again with",
        "                      -e import_cluster_keep_hand_edits=true; these files are then NOT managed by",
        "                      Ansible (kept as the node has them): cassandra-env.sh on node1;",
        "                      jvm17-server.options on node2, node5. To bring them under Ansible: carry the",
        "                      edit in the inventory (cassandra_jvm*_extra_options, cassandra_env_extra_lines",
        "                      and cassandra_logback_extra_lines for lines at the end of the file,",
        "                      cassandra_extra_settings, ...) or give the file your own template",
        "                      (cassandra_config_template_overrides), remove the file from",
        "                      cassandra_config_keep_files, import again. To drop them instead: undo them on",
        "                      the node, then import again",
        "  your files          in host_vars/node3/local.yml, set cassandra_concurrent_reads to the node's",
        "                      value (64) or remove the line, then import again",
        "  not read            start Cassandra or fix the access on node4, then import again; or accept it",
        "                      unchecked: -e import_cluster_allow_unread=true",
        "  other               not sorted out here: see the details (reports/my_cluster/report.txt, section",
        "                      SELF-CHECK); change the node, or take it as a planned change (below)",
        "  a change you want   it is a planned change: import again with -e import_cluster_strict=false (it",
        "                      only writes the inventory), then review with",
        "                      ansible-playbook community.cassandra.apply_config --check --diff before",
        "                      applying",
        "",
        "Details: reports/my_cluster/report.txt, section SELF-CHECK"]


def test_self_check_stop_never_shows_a_secret():
    for lines in (stop(failed_nodes(), CHECKS, WINS), stop(failed_nodes(), CHECKS, WINS, width=60),
                  report(failed_nodes(), ok=False, self_check=CHECKS)):
        text = "\n".join(lines)
        assert "hunter2" not in text and "s3cret" not in text and "cassandra'" not in text
    # a secret your file sets: its value not shown in the hint either
    wins = {"theirs_win": [{"node": "node3", "key": "cassandra_jmx_password", "path": "host_vars/node3/local.yml"}],
            "yours": [{"node": "node3", "key": "cassandra_jmx_password", "value": "s3cret", "yours": "x",
                       "path": "host_vars/node3/local.yml"}]}
    checks = {"node3": {"differences": ["jmxremote.password: jmx_password: node has ****, import would write ****"]}}
    lines = stop([node(3)], checks, wins)
    assert "  your files          in host_vars/node3/local.yml, set cassandra_jmx_password to the node's value" \
        in lines and "                      (****) or remove the line, then import again" in lines
    assert "s3cret" not in "\n".join(lines)


def test_self_check_stop_lines_fit_the_width():
    # wrapped at the terminal's width, an option or a command never cut
    for width in (60, 79, 100, 160):
        lines = stop(failed_nodes(), CHECKS, WINS, width=width)
        long = [line for line in lines if len(line) > width]
        # (a name, a path or a command longer than the room: whole)
        uncut = r"apply_config --check --diff|keystore_password|host_vars/node3/local.yml|keep_hand_edits=true"
        assert long == [line for line in long if re.search(uncut, line)]
        text = "\n".join(lines)
        for whole in ("-e import_cluster_keep_hand_edits=true", "-e import_cluster_allow_unread=true",
                      "-e import_cluster_strict=false", "ansible-playbook community.cassandra.apply_config --check --diff"):
            assert whole in text
        assert u"\xa0" not in text


def test_self_check_stop_hand_edits_only():
    # every difference in a file with hand edits: import_cluster_keep_hand_edits, then a change you want
    nodes = [node(1, hand=LOGBACK), node(2, hand=LOGBACK), node(3)]
    checks = dict((n, {"differences": ["logback.xml: node has /configuration/logger[level=\"DEBUG\" name=\"x\"],"
                                       " import would write nothing"]}) for n in ("node1", "node2"))
    lines = stop(nodes, checks, check=True)
    assert lines[:2] == ["SELF-CHECK FAILED  my_cluster  the inventory would change 1 setting on 2 nodes",
                         "  (--check: the inventory not written)"]
    assert lines[3:6] == ["  Hand edits no variable covers (cassandra_config would revert them):",
                          "    - logback.xml, on node1, node2:", '        /configuration/logger[level="DEBUG" name="x"]']
    hints = [line.split("   ")[0].strip() for line in lines[lines.index("HOW TO MAKE IT PASS") + 1:]
             if line[2:3] != " " and line.startswith("  ")]
    assert hints == ["hand edits", "a change you want"]


def test_self_check_stop_kept_files_are_not_hand_edits():
    # import_cluster_keep_hand_edits already: a difference left in a kept file (its owner, group and mode) is
    # another kind, keep_hand_edits not offered again
    kept = {"cassandra_config_keep_files": ["logback.xml"]}
    nodes = [node(1, hand=LOGBACK, keep=kept), node(2)]
    checks = {"node1": {"differences": ["logback.xml: owner:group mode: node has root:root 0600, import would write"
                                        " root:cassandra 0644"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_writes: node has 32, import would write 64"]}}
    lines = stop(nodes, checks)
    assert lines[3:8] == ["  Other differences:",
                          "    - logback.xml owner:group mode, on node1:", "        node root:root 0600, inventory root:cassandra 0644",
                          "    - cassandra.yaml concurrent_writes, on node2:", "        node 32, inventory 64"]
    assert "keep_hand_edits" not in "\n".join(lines) and "  Other differences:" in lines


def test_self_check_stop_values_per_node():
    # nodes with the same value together; the inventory's value per node when it differs
    checks = {"node1": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node3": {"differences": ["cassandra.yaml: concurrent_reads: node has '128', import would write '32'"]},
              "node4": {"differences": ["cassandra.yaml: concurrent_writes: node has '8', import would write '16'"]},
              "node5": {"differences": ["cassandra.yaml: concurrent_writes: node has '9', import would write '32'"]}}
    lines = stop([node(i) for i in range(1, 6)], checks)
    at = lines.index("    - cassandra.yaml concurrent_reads, on node1..node3:")
    assert lines[at + 1:at + 6] == ["        node1, node2: node 64, inventory 32", "        node3: node 128, inventory 32",
                                    "    - cassandra.yaml concurrent_writes, on node4, node5:",
                                    "        node4: node 8, inventory 16", "        node5: node 9, inventory 32"]


def test_self_check_stop_could_not_run_or_not_read():
    nodes = [node(1), node(2, read=False)]
    error = 'the self-check could not run ("Render" failed): nothing says the variables reproduce the nodes'
    lines = stop(nodes, {}, error=error)
    assert lines[:2] == ["SELF-CHECK FAILED  my_cluster  it could not run; 1 node not read",
                         "  (written for inspection only, marked NOT VALID)"]  # (said whole)
    assert lines[3] == '  The self-check could not run ("Render" failed): nothing says the variables reproduce the nodes'
    text = "\n".join(lines)
    assert "-e import_cluster_debug=true to see the error (it may show passwords)" in " ".join(text.split())
    assert "import_cluster_strict" not in text
    # the nodes not read accepted: not said
    lines = stop(nodes, {}, allow_unread=True, error=error)
    assert lines[0].startswith("SELF-CHECK FAILED  my_cluster  it could not run (written")
    assert "allow_unread" not in "\n".join(lines)
    assert stop(nodes, {})[0].startswith("SELF-CHECK FAILED  my_cluster  1 node not read (written")
    # no node read: never valid, allow_unread or not, so not offered
    none = [node(1, read=False), node(2, read=False)]
    for allow in (False, True):
        lines = stop(none, {}, allow_unread=allow)
        assert lines[0].startswith("SELF-CHECK FAILED  my_cluster  no node read (written")
        assert "  not read   no node could be read: start Cassandra or fix the access on one of them at least, then" \
            in lines and "allow_unread" not in "\n".join(lines)
    # nothing known (no difference, every node read, no error): a hint all the same
    lines = stop([node(1)], {})
    assert lines[lines.index("HOW TO MAKE IT PASS") + 1].startswith("  unknown   import again with -e import_cluster_debug")


def test_self_check_report_section():
    # the same grouping in report.txt (report.check.txt), each node's differences under its item, whole
    lines = report(failed_nodes(), ok=False, self_check=CHECKS, layout_extra=WINS)
    at = lines.index(u"SELF-CHECK — FAILED: the inventory would change 7 settings on 4 nodes; 1 node not read")
    end = lines.index("", lines.index("HOW TO MAKE IT PASS", at))
    section = lines[at:end]
    assert section[1:12] == [
        "  Hand edits no variable covers (cassandra_config would revert them):",
        "    - cassandra-env.sh, on node1:",
        "        export KEYSTORE_PASSWORD=****",
        "          node1: cassandra-env.sh: node has export KEYSTORE_PASSWORD=****",  # (masked to the end of the line)
        "    - jvm17-server.options, on node2, node5:",
        "        -XX:+UseFoo (not in the inventory)",
        "          node2, node5: jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write nothing",
        "  Your inventory files override the import (Ansible will apply your value):",
        "    - cassandra.yaml concurrent_reads, on node3:",
        "        node 64, inventory 16 (set in host_vars/node3/local.yml)",
        "          node3: cassandra.yaml: concurrent_reads: node has '64', import would write '16'"]
    assert max(len(line) for line in section) <= 110
    assert "Details:" not in "\n".join(section)  # (it is the details)
    todo = "  3. Self-check: the inventory would change 7 settings"
    assert "  1. " in "\n".join(lines) and [line for line in lines if line.startswith(todo)]
    # the screen points to it, or to the stop below it (import_cluster_strict)
    screen = report(failed_nodes(), ok=False, self_check=CHECKS, screen=True)
    assert [line for line in screen if line.endswith("(how to make it pass: SELF-CHECK in the report)")]
    screen = report(failed_nodes(), ok=False, self_check=CHECKS, screen=True, strict=True)
    assert [line for line in screen if line.endswith("(how to make it pass: SELF-CHECK FAILED below)")]
    assert not [line for line in screen if line.startswith("SELF-CHECK")]
    # passed: no section
    assert not [line for line in report(failed_nodes()[:1]) if line.startswith("SELF-CHECK")]


def test_self_check_report_same_line_once_for_its_nodes():
    # a large cluster: the line every node has once, by the node groups; a value of its own on its own line
    nodes = [node(i) for i in range(1, 21)]
    line = ("jmxremote.access: controlRole: node has 'create javax.management.monitor.*,javax.management.timer.*"
            " readwrite unregister', import would write nothing")
    checks = dict(("node%d" % i, {"differences": [line]}) for i in range(1, 21))
    checks["node7"] = {"differences": [line.replace("readwrite unregister", "readwrite")]}
    checks["node1"] = {"differences": [line, line]}  # (twice: named once)
    lines = report(nodes, ok=False, self_check=checks)
    at = lines.index(u"SELF-CHECK — FAILED: the inventory would change 1 setting on 20 nodes")
    section = lines[at:lines.index("HOW TO MAKE IT PASS", at)]
    assert " ".join(section).count("node has") == 2
    assert [x for x in section if x.strip().startswith("node7: jmxremote.access: controlRole: node has")]
    assert not [x for x in section if x.strip().startswith("node1: ")]
    assert "node1, node1" not in " ".join(section)


def test_self_check_stop_owner_of_a_hand_edited_file_is_not_a_hand_edit():
    # keep_hand_edits keeps the content of a file, not its owner, group and mode
    nodes = [node(1, hand=LOGBACK)]
    checks = {"node1": {"differences": ["logback.xml: owner:group mode: node has root:root 0600, import would write"
                                        " root:cassandra 0644",
                                        "logback.xml: node has /configuration/logger, import would write nothing"]}}
    lines = stop(nodes, checks)
    assert lines[3:9] == ["  Hand edits no variable covers (cassandra_config would revert them):",
                          "    - logback.xml, on node1:", "        /configuration/logger",
                          "  Other differences:",
                          "    - logback.xml owner:group mode, on node1:", "        node root:root 0600, inventory root:cassandra 0644"]


def test_self_check_stop_your_file_by_its_exact_variable():
    # a file of yours wins for cassandra_dc and cassandra_tokens: not cdc_enabled nor num_tokens; its hint only for
    # a difference it makes
    wins = {"theirs_win": [{"node": "node1", "key": "cassandra_dc", "path": "host_vars/node1/z.yml"},
                           {"node": "node1", "key": "cassandra_tokens", "path": "host_vars/node1/z.yml"},
                           {"node": "node1", "key": "cassandra_service_limit_nofile", "path": "host_vars/node1/z.yml"}],
            "yours": [{"node": "node1", "key": "cassandra_service_limit_nofile", "value": 100000, "yours": 10,
                       "path": "host_vars/node1/z.yml"}]}
    checks = {"node1": {"differences": ["cassandra.yaml: cdc_enabled: node has 'true', import would write 'false'",
                                        "cassandra.yaml: num_tokens: node has '16', import would write '8'",
                                        "cassandra.service: [Service] LimitNOFILE: node has '100000', import would"
                                        " write '10'"]}}
    lines = stop([node(1)], checks, wins, width=160)
    at = lines.index("  Your inventory files override the import (Ansible will apply your value):")
    assert lines[at + 1:at + 3] == ["    - cassandra.service [Service] LimitNOFILE, on node1:",
                                    "        node 100000, inventory 10 (set in host_vars/node1/z.yml)"]
    assert lines[at + 3:at + 8] == ["  Other differences:", "    - cassandra.yaml cdc_enabled, on node1:",
                                    "        node true, inventory false", "    - cassandra.yaml num_tokens, on node1:",
                                    "        node 16, inventory 8"]
    hints = [line for line in lines if line.startswith("  your files")]
    assert hints == ["  your files          in host_vars/node1/z.yml, set cassandra_service_limit_nofile to the node's value"
                     " (100000) or remove the line, then import again"]
    # a node not read alone: no hint about your files
    assert not [line for line in stop([node(1), node(2, read=False)], {}, wins) if "your files" in line]


def test_self_check_stop_lines_of_odd_values():
    # a line with ": node has " in it stays a line; a sentence is never cut on the screen
    checks = {"node1": {"differences": ['cassandra-env.sh: node has JVM_OPTS="-Dx=a: node has b", import would write'
                                        ' nothing',
                                        "cassandra.yaml: cannot be read as its program reads it (node's or the roles'"
                                        " version)"]}}
    lines = stop([node(1)], checks, width=160)
    at = lines.index("    - cassandra-env.sh, on node1:")
    assert lines[at + 1:at + 4] == ['        JVM_OPTS="-Dx=a: node has b"', "    - cassandra.yaml, on node1:",
                                    "        cannot be read as its program reads it (node's or the roles' version)"]


def test_self_check_stop_large_cluster_by_node_groups():
    # from GROUPS_FROM nodes on: the node lists by groups, defined under the verdict
    nodes = [node(i) for i in range(1, 31)]
    checks = dict(("node%d" % i, {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would"
                                                  " write '32'"]}) for i in range(1, 31, 2))
    lines = stop(nodes, checks)
    assert lines[3] == "NODE GROUPS" and lines[4].startswith("  G1 = node1, node3, node5")
    assert "    - cassandra.yaml concurrent_reads, on G1:" in lines and "        node 64, inventory 32" in lines


def test_self_check_stop_groups_are_the_summarys():
    # the summary just above defines G1, G2...: the stop names the same nodes by the same labels
    nodes = [dict(node(i), name="db%02d" % i) for i in range(1, 17)]
    for n in nodes[6:]:
        n["hand_edits"] = ["cassandra-env.sh, line 3:", "  + JVM_OPTS=x"]
    checks = dict(("db%02d" % i, {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would"
                                                  " write '32'"]}) for i in range(3, 9))
    checks.update(("db%02d" % i, {"differences": ["cassandra-env.sh: node has JVM_OPTS=x, import would write"
                                                  " nothing"]}) for i in range(11, 17))
    screen = report(nodes, ok=False, self_check=checks, screen=True, strict=True)
    lines = stop(nodes, checks)

    def defined(block):
        at = block.index("NODE GROUPS")
        return [line for line in block[at + 1:] if re.match(r"^  G\d+ = ", line)]
    assert defined(lines) == defined(screen) and defined(lines)


def test_self_check_stop_odd_values():
    # an empty value shown as such; a value of yours not known: not "None"
    checks = {"node1": {"differences": ["cassandra.yaml: cdc_raw_directory: node has '', import would write '/x'"]}}
    assert "        node '', inventory /x" in stop([node(1)], checks)
    wins = {"theirs_win": [{"node": "node1", "key": "cassandra_seeds", "path": "host_vars/node1/z.yml"}]}
    checks = {"node1": {"differences": ["cassandra.yaml: seeds: node has 'a', import would write 'b'"]}}
    text = " ".join("\n".join(stop([node(1)], checks, wins)).split())
    assert "set cassandra_seeds to the node's value (see the DIFFERS FROM YOUR... section)" in text and "None" not in text


def test_self_check_stop_a_value_none_is_not_unset():
    # Cassandra's own value none (internode_encryption: none) is a value; a secret JVM option keeps its name
    checks = {"node1": {"differences": [
        "cassandra.yaml: server_encryption_options.internode_encryption: node has 'none', import would write 'all'",
        "cassandra.yaml: internode_compression: node has 'dc', import would write 'none'",
        "jvm17-server.options: javax.net.ssl.keyStorePassword: node has ****, import would write ****"]}}
    lines = stop([node(1)], checks, width=160)
    assert "        node none, inventory all" in lines and "        node dc, inventory none" in lines
    assert "    - jvm17-server.options javax.net.ssl.keyStorePassword, on node1:" in lines
    assert "        node ****, inventory ****" in lines


def test_auto_bootstrap_false_kept_on_its_nodes_with_a_warning():
    # host_vars only, even on every node (a node added later must not get it); a WARNING, the import not refused
    nodes = [node(i) for i in range(1, 4)]
    for n in nodes[:2]:
        n["vars"]["cassandra_auto_bootstrap"] = False
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    assert [h for h, v in layout["host_vars"].items() if v.get("cassandra_auto_bootstrap") is False] == ["node1", "node2"]
    assert not [g for g, v in layout["group_vars"].items() if "cassandra_auto_bootstrap" in v]
    lines = report(nodes)
    assert any(u"WARNING - node1, node2: auto_bootstrap: false, kept as found \u2014 a node that loses its data restarts"
               u" empty without streaming it back; remove the setting (default true) unless you know why it is there"
               u" (kept on purpose? add auto_bootstrap_false to cassandra_suspicious_ack)" in line for line in lines)
    every = [node(i) for i in range(1, 4)]
    for n in every:
        n["vars"]["cassandra_auto_bootstrap"] = False
    assert sorted(cassandra_inventory_layout(every, "My Cluster")["host_vars"]) == ["node1", "node2", "node3"]


def test_replace_flag_in_a_hand_edit_warned_with_its_line():
    # a node read is a member of the ring: a replace flag left in cassandra-env.sh, the exact line to remove
    nodes = [node(1, hand=["cassandra-env.sh, line 290:", '  + JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"']),
             node(2)]
    lines = report(nodes)
    assert any(u"WARNING - node1: -Dcassandra.replace_address=10.0.0.9 in the node's config, kept as found \u2014 this"
               u" node is already bootstrapped: Cassandra refuses its next start with it; remove the line"
               u' JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9" (kept on purpose? add replace_address to'
               u" cassandra_suspicious_ack)" in line for line in lines)


def test_a_suspicious_setting_does_not_change_whom_the_other_settings_are_for():
    # the WARNING names node1; the settings every node has are still for all of them
    nodes = [node(i) for i in range(1, 4)]
    nodes[0]["vars"]["cassandra_auto_bootstrap"] = False
    lines = report(nodes)
    row = [line for line in lines if line.strip().startswith("cassandra_num_tokens:")][0]
    assert row.split()[-1] == "all", row
