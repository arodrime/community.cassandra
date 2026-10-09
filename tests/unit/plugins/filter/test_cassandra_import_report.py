from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import's report and the screen that ends the run: one setting per line, each value with the nodes that have it.

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    _compress, cassandra_import_report)

WRITTEN = ["/p/inventories/my_cluster.yml", "/p/inventories"]
REPORT = "/p/reports/my_cluster/report.txt"


def node(i, heap="8G", hand=None, read=True, **extra):
    return dict({"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": read,
                 "reason": "unreachable", "hand_edits": hand or [], "normalized": [], "notes": [],
                 "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16, "cassandra_max_heap_size": heap,
                          "cassandra_seeds": ["node1", "node3"], "cassandra_jmx_password": "s3cret"}}, **extra)


def report(nodes, ok=True, **kwargs):
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    kwargs.setdefault("cwd", "/p")
    kwargs.setdefault("in_git", True)
    return cassandra_import_report(layout, WRITTEN, REPORT, kwargs.pop("self_check", {}), ok, **kwargs)


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
        "  3. Self-check: the roles would change settings on node1 (see DETAILS)",
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
    lines = report([node(i) for i in range(1, 4)], check=True, screen=True,
                   leftovers={"stale": ["host_vars/gone/main.yml"]})
    assert lines[0] == u"IMPORT my_cluster (--check, nothing written) — 3 nodes read / 3 — SELF-CHECK PASSED"
    assert lines[1].startswith("Would write: ") and lines[2] == "Report:  not written under --check"
    assert "TO DO (1)" in lines and "  1. Write it: the same command without --check" in lines
    assert lines[-1] == "full report: written by the run without --check"
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
    assert "  2. Self-check: the roles would change settings on node1, node3 (see DETAILS)" in lines
    assert not [line for line in lines if "Review then commit" in line]  # not a valid inventory
    assert [line for line in lines if line.startswith("  jmxremote.password:")][0].split()[1:4] == [
        "its", "users", "NOT"]


def test_differs_from_your_group_vars_all():
    """Real case: group_vars/all sets the config files' owner; 2 nodes root:cassandra, 3 cassandra:svccassandra."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [node(i) for i in range(1, 6)]
    for n in nodes[:2]:
        n["vars"].update(cassandra_config_user="root", cassandra_config_group="cassandra")
    for n in nodes[2:]:
        n["vars"].update(cassandra_config_user="cassandra", cassandra_config_group="svccassandra")
    layout = cassandra_inventory_layout_over(nodes, "My Cluster", [{
        "path": "group_vars/all/standard.yml",
        "content": "cassandra_config_user: cassandra\ncassandra_config_group: svccassandra\n"}])
    lines = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True)
    assert ("  1. Differs from your group_vars/all, kept as found: cassandra_config_group, cassandra_config_user"
            " (see DIFFERS FROM YOUR group_vars/all)") in lines
    at = lines.index(u"DIFFERS FROM YOUR group_vars/all — kept as found; delete the line to apply your standard")
    assert lines[at + 1:at + 5] == [
        "  cassandra_config_group:  yours: svccassandra (group_vars/all/standard.yml)",
        u"                           cassandra   node1, node2   \u2190 kept, in host_vars",
        "  cassandra_config_user:   yours: cassandra (group_vars/all/standard.yml)",
        u"                           root        node1, node2   \u2190 kept, in host_vars"]
    # SETTINGS: the value the others get from group_vars/all, not "the collection's default"
    at = [i for i, line in enumerate(lines) if line.startswith("  cassandra_config_user:")][0]
    assert lines[at].split()[1:] == ["cassandra", "node3,", "node4,", "node5"]
    # the screen has no such section, its TO DO points to the report
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True)
    assert not [line for line in screen if line.startswith("DIFFERS")]
    assert any("(see DIFFERS FROM YOUR group_vars/all in the report)" in line for line in screen)


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
