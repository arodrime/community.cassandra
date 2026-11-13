from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import's report and the screen that ends the run: by urgency (a head with the verdict and its counts, TO DO,
# TO REVIEW, FOR INFO, what to do next), for a reader who may be new to Cassandra and to the collection.

import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    _compress, _self_check_items, cassandra_import_report, cassandra_import_self_check_advice, node_list)

ARROW = u"←"
WRITTEN = ["/p/inventories/my_cluster.yml", "/p/inventories"]
REPORT = "/p/reports/my_cluster/report.txt"
REPORT_CHECK = "/p/reports/my_cluster/report.check.txt"


def node(i, heap="8G", hand=None, read=True, **extra):
    return dict({"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": read,
                 "reason": "unreachable", "hand_edits": hand or [], "normalized": [], "notes": [],
                 "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16, "cassandra_max_heap_size": heap,
                          "cassandra_seeds": ["node1", "node3"], "cassandra_jmx_password": "s3cret"}}, **extra)


def report(nodes, ok=True, layout_extra=None, **kwargs):
    """report.txt then report.details.txt (as one reads them); the screen alone with screen=True; one of the
    files alone with details=False or True."""
    layout = dict(cassandra_inventory_layout(nodes, "My Cluster"), **(layout_extra or {}))
    kwargs.setdefault("cwd", "/p")
    kwargs.setdefault("in_git", True)
    kwargs.setdefault("width", 160)
    args = (layout, WRITTEN, kwargs.pop("report_file", REPORT), kwargs.pop("self_check", {}), ok)
    if kwargs.get("screen") or "details" in kwargs:
        return cassandra_import_report(*args, **kwargs)
    return cassandra_import_report(*args, **kwargs) + [""] + cassandra_import_report(*args, details=True, **kwargs)


def heading(lines, title):
    """Where a section starts: its title in a rule of = (==== TO REVIEW ====...)."""
    return next(i for i, line in enumerate(lines) if line.startswith("==== %s =" % title))


def has(lines, title):
    return any(line.startswith("==== %s =" % title) for line in lines)


def section(lines, title):
    """The lines of a section of the report, from under its rule (its blank lines first left out) up to the next
    rule or line from column 0 (a blank line between its entries)."""
    at = heading(lines, title) + 1
    while at < len(lines) and not lines[at].strip():
        at += 1
    end = next((i for i in range(at, len(lines)) if lines[i] and lines[i][0] != " "), len(lines))
    while end > at and not lines[end - 1].strip():
        end -= 1
    return lines[at:end]


def test_compress():
    assert _compress(["node3", "node1", "node2", "web", "node5"]) == "node1..node3, node5, web"
    assert _compress(["n1", "n2"]) == "n1, n2"
    assert _compress(["a", "b"]) == "a, b"
    assert _compress(["node%d" % i for i in range(1, 8)]) == "node1..node7"  # no count in a path


def test_node_lists_in_their_shortest_exact_form():
    every = ["n%d" % i for i in range(1, 13)]
    dcs = [("dc1", every[:6]), ("dc2", every[6:])]
    assert node_list(every, every, dcs) == "all (12 nodes)"
    assert node_list([n for n in every if n != "n4"], every, dcs) == "all - n4 (11 nodes)"
    assert node_list(every[:6] + ["n7"], every, dcs) == "dc1 + n7 (7 nodes)"
    assert node_list([n for n in every[:6] if n != "n3"] + ["n9"], every, dcs) == "dc1 - n3 + n9 (6 nodes)"
    assert node_list(every[6:], every, dcs) == "dc2 (6 nodes)"
    assert node_list(["n1", "n2"], every, dcs) == "n1, n2 (2 nodes)"
    assert node_list(["n5"], every, dcs) == "n5 (1 node)"
    # three DCs: groups added up, then a node taken; never a group label of the report's own (G1)
    three = [("dc1", every[:4]), ("dc2", every[4:8]), ("dc3", every[8:])]
    assert node_list(every[4:11], every, three) == "dc2 + dc3 - n12 (7 nodes)"
    # no group: the names (the screen: ranges)
    assert node_list(["n1", "n3", "n5"], every) == "n1, n3, n5 (3 nodes)"
    assert node_list(["n1", "n2", "n3", "n5"], every, full=False) == "n1..n3, n5 (4 nodes)"


def test_clean_import_nothing_to_act_on():
    lines = report([node(i) for i in range(1, 6)])
    assert lines[:4] == ["IMPORT my_cluster  5 nodes  nothing to act on",
                         "  self-check passed: the inventory gives every node that was read the settings it has now",
                         "  written: inventories/my_cluster.yml, group_vars/my_cluster*/",
                         "  report:  reports/my_cluster/report.txt"]
    # each section: two blank lines, its title in a rule, a blank line; what the report is: in the details
    assert lines[4:8] == ["", "", "==== WHAT TO DO NEXT " + "=" * 79, ""]
    assert lines[heading(lines, "WORDS") - 2:heading(lines, "WORDS") + 2] == ["", "", "==== WORDS " + "=" * 89, ""]
    assert not has(lines, "TO DO") and not has(lines, "WORTH A LOOK") and not has(lines, "DIFFERENT ON SOME NODES")
    assert has(report([node(1)], details=True), "WHAT THIS REPORT IS")
    assert not has(report([node(1)], details=False), "WHAT THIS REPORT IS")
    text = "\n".join(lines)
    assert "s3cret" not in text
    info = section(lines, "FOR INFO")
    assert "    cassandra_jmx_password:  (in secrets.yml)   all (5 nodes)   (the password nodetool logs in with)" in info
    assert "    cassandra_seeds:         node1, node3       all (5 nodes)" \
           "   (the nodes a starting node contacts first to join the cluster)" in info
    assert "differs" not in text and lines[-1] != "" and section(lines, "DETAILS")[0] == "  What the import read on each node"
    # what to do next: no -i when the inventory is ansible.cfg's, no -e cassandra_hosts for a cluster alone
    after = section(lines, "WHAT TO DO NEXT")
    assert after[0] == "  1. Review the files written, then keep them: git diff && git commit."
    assert " ".join(x.strip() for x in after[1:]) == (
        u"2. Check that the inventory changes nothing on the nodes (these two commands only look, they change nothing):"
        u" ansible-playbook community.cassandra.apply_config --check --diff \u2192 expect nothing to apply;"
        u" ansible-playbook community.cassandra.topology --check \u2192 expect nothing to do.")
    lines = report([node(1)], inventory="/p/inventories", hosts="my_cluster", width=300)
    assert [line for line in lines if "ansible-playbook -i inventories community.cassandra.apply_config -e"
            " cassandra_hosts=my_cluster --check --diff" in line]


def test_review_step_only_with_git():
    # the git step only when the inventory dir is in a git work tree
    lines = report([node(i) for i in range(1, 3)], in_git=False)
    assert "  1. Review the files written in inventories." in lines and "git" not in "\n".join(lines)


def test_sections_by_urgency_and_the_counts_in_the_head():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(2, "16G"), node(3, keep={"cassandra_config_keep_files": ["logback.xml"]}), node(4, read=False)]
    lines = report(nodes, secrets_clear=["group_vars/my_cluster/secrets.yml"])
    assert lines[0] == ("IMPORT my_cluster  4 nodes (3 read)  5 entries to act on: 2 TO DO, 1 hand-edit entry, 1 file left as is,"
                        " 1 setting that differs between nodes")
    order = [heading(lines, title) for title in ("TO DO", "HAND EDITS", "DIFFERENT ON SOME NODES",
                                                 "LEFT AS THE NODES HAVE THEM", "WHAT TO DO NEXT", "WHAT THIS REPORT IS",
                                                 "FOR INFO", "DETAILS")]
    assert order == sorted(order)
    # each TO DO a numbered step: what, why, the command to copy, what to check after
    todo = section(lines, "TO DO")
    flat = [" ".join(x.split()) for x in todo]
    assert flat[0] == "1. Let the import read node4 (1 node), then import again"
    joined = " ".join(flat)
    # what importing without it means, the exact command, what to check
    assert ("the inventory has no settings of it, and a playbook run on it would give it the cluster's settings, not"
            " its own") in joined
    assert "run: ansible-playbook community.cassandra.import_cluster -e import_cluster_force=true" in joined
    assert "check: the head of the new report says every node was read" in joined
    # paths from the current dir, no cd; the vault password said; a head -1 to check
    assert "run: ansible-vault encrypt inventories/group_vars/my_cluster/secrets.yml" in joined
    assert "check: head -1 inventories/group_vars/my_cluster/secrets.yml shows $ANSIBLE_VAULT;1.1;AES256" in joined
    assert "ansible-vault asks for a new password: keep it safe" in joined and "cd inventories" not in joined
    # a short "what to do next" at the end, the TO DO first
    assert section(lines, "WHAT TO DO NEXT")[0].startswith("  1. Do the TO DO steps above, in their order")
    # the screen: the same head, no FOR INFO details nor DETAILS, the report's path last
    screen = report(nodes, secrets_clear=["group_vars/my_cluster/secrets.yml"], screen=True)
    assert screen[0] == lines[0] and not has(screen, "WHAT THIS REPORT IS")
    assert screen[-1] == "report: reports/my_cluster/report.txt"


def test_all_is_every_node_never_only_the_ones_read():
    # node4 not read: the value every node read has is "all - node4", never "all"
    lines = report([node(1), node(2), node(3), node(4, read=False)])
    row = [line for line in lines if line.strip().startswith("cassandra_num_tokens:")][0]
    assert "all - node4 (3 nodes)" in row and "all (3 nodes)" not in row


def test_a_setting_said_in_plain_words_the_first_time():
    lines = report([node(1), node(2, "16G")])
    text = "\n".join(lines)
    assert text.count("(the memory Java gives Cassandra)") == 1
    assert text.index("(the memory Java gives Cassandra)") < text.index("\n==== FOR INFO =")


def test_values_that_differ_between_nodes_to_review():
    lines = report([node(1), node(2), node(3), node(4, "16G"), node(5, "16G")])
    review = section(lines, "DIFFERENT ON SOME NODES")
    assert review == [  # (a table: every value with its nodes; then how to give them one, step by step)
        "    cassandra_max_heap_size: 8G    node1, node2, node3 (3 nodes)   (the memory Java gives Cassandra)",
        "                             16G   node4, node5 (2 nodes)",
        "      set in host_vars/<node>/main.yml.",
        "      to give every node 8G: cassandra_max_heap_size: 8G in group_vars/my_cluster/main.yml, removed from host_vars of"
        " 5 nodes (node1, node2, node3, node4,",
        "        node5)",
        "      then 2 nodes change, restart unknown (apply_config does not write it); no check (apply_config does not show"
        " it)"]
    # (the heap per host)
    assert lines[2] == ("  written: inventories/my_cluster.yml, group_vars/my_cluster*/,"
                        " host_vars/<node>/ (node1, node2, node3, node4, node5)")
    lines = report([node(1, keep={"cassandra_firewall_manage": False}), node(2)])
    assert lines[2] == "  written: inventories/my_cluster.yml, group_vars/my_cluster*/, host_vars/node1/"
    seven = [node(i) for i in range(1, 5)] + [node(i, "16G") for i in range(5, 8)]
    assert report(seven, screen=True)[2].endswith("host_vars/<node>/ (node1..node7)")  # the screen: a range


def test_where_each_value_is_kept_and_the_dcs_name_the_nodes():
    nodes = [dict(node(1, "8G"), dc="dc1"), dict(node(2, "8G"), dc="dc1"), dict(node(3, "16G"), dc="dc2"),
             dict(node(4, "16G"), dc="dc2"), dict(node(5, "4G"), dc="dc3"), dict(node(6, "4G"), dc="dc3"),
             dict(node(7, "4G"), dc="dc3")]
    review = section(report(nodes), "DIFFERENT ON SOME NODES")
    assert review[:4] == [
        "    cassandra_max_heap_size: 4G    dc3 (3 nodes)   (the memory Java gives Cassandra)",
        "                             16G   dc2 (2 nodes)",
        "                             8G    dc1 (2 nodes)",
        "      set in group_vars/my_cluster_dc3/main.yml, group_vars/my_cluster_dc2/main.yml,"
        " group_vars/my_cluster_dc1/main.yml."]


def test_things_to_do_and_hand_edits():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(2, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(3, hand=["jvm-server.options, line 3:", "  + -Dcassandra.weird=1"], normalized=["x"]),
             node(4, read=False)]
    lines = report(nodes, ok=False, secrets_clear=["group_vars/my_cluster/secrets.yml"],
                   leftovers={"unsure": ["host_vars/old/main.yml"]},
                   self_check={"node1": {"differences": ["cassandra.yaml: concurrent_writes"], "notes": []}})
    # (a self-check not passed: a step to do, said calmly, not in red)
    assert lines[0].startswith("IMPORT my_cluster  4 nodes (3 read)  6 entries to act on: 4 TO DO, 2 hand-edit")
    assert lines[1] == "  self-check not passed yet: TO DO step 2"
    titles = [line for line in lines if re.match(r"^  \d\. ", line)]
    assert titles[:4] == ["  1. Let the import read node4 (1 node), then import again", "  2. Make the self-check pass",
                          "  3. Encrypt the passwords file",
                          "  4. Is host_vars/old/main.yml an old node of this cluster?"]
    at = lines.index("  4. Is host_vars/old/main.yml an old node of this cluster?")
    assert "     run:    git rm inventories/host_vars/old/main.yml" in lines[at:at + 5]
    at = lines.index("  2. Make the self-check pass")
    assert " ".join(x.strip() for x in lines[at + 1:at + 3]) == (
        "the self-check works out the files the inventory would write for each node and compares them with the node's:"
        " the inventory would change 1 setting on 1 node, so the inventory does not reproduce the nodes yet")
    # the self-check's details right under the TO DO steps; each node's lines under its item
    at = lines.index(u"  SELF-CHECK — the inventory would change 1 setting on 1 node; 1 node not read")
    assert lines[at + 1:at + 5] == ["    Hand edits no variable covers (cassandra_config would revert them):",
                                    "      - cassandra.yaml, on node1 (1 node):", "          concurrent_writes",
                                    "            node1 (1 node): cassandra.yaml: concurrent_writes"]
    assert at < heading(lines, "HAND EDITS")
    review = section(lines, "HAND EDITS")
    at = review.index("    cassandra.yaml (Cassandra's main settings file) concurrent_writes: 48   node1, node2 (2 nodes)")
    assert review[at:at + 4] == [  # (a blank line between entries)
        "    cassandra.yaml (Cassandra's main settings file) concurrent_writes: 48   node1, node2 (2 nodes)",
        "      to keep it: import again with -e import_cluster_keep_hand_edits=cassandra.yaml (Ansible then leaves"
        " cassandra.yaml as each node has it)",
        "",
        "    jvm-server.options (Java's options) -Dcassandra.weird: 1   node3 (1 node)"]
    assert "    also 1 edit of the layout only (spacing, order, comments): no effect, listed under DETAILS" in review
    details = lines[heading(lines, "DETAILS"):]
    assert "        + concurrent_writes: 48" in details and not [line for line in details if "SELF-CHECK" in line]
    # not a valid inventory: nothing to commit
    assert not [line for line in lines if "git commit" in line]


def test_left_as_it_is_grouped_by_nodes():
    keep = {"cassandra_repository_manage": False, "cassandra_linux_manage": False, "cassandra_firewall_manage": False}
    nodes = [node(i, keep=dict(keep)) for i in range(1, 4)] + [node(4, keep={"cassandra_firewall_manage": False})]
    lines = report(nodes)
    at = lines.index("  Parts Ansible leaves as each node has them (*_manage: false)")
    assert lines[at + 1:at + 3] == ["    repositories, OS settings   all - node4 (3 nodes)",
                                    "    firewall   all (4 nodes)"]


def test_left_alone_apart_from_installs_holds_and_values():
    # a *_install: false, a hold, a value as the node has it: not said as *_manage: false (screen and details)
    keep = {"cassandra_linux_manage": False, "cassandra_install_tools": False, "cassandra_package_hold": False,
            "cassandra_log_dir": "/srv/log"}
    nodes = [node(i, keep=dict(keep)) for i in range(1, 4)]
    left = section(report(nodes, screen=True), "LEFT AS THE NODES HAVE THEM")
    assert "    not managed by Ansible here: OS settings   all (3 nodes)" in left
    assert "    set to what these nodes have: cassandra_log_dir: /srv/log   all (3 nodes)" in left
    details = report(nodes, details=True)
    at = details.index("  Parts Ansible leaves as each node has them (*_manage: false)")
    assert details[at + 1:at + 8] == [
        "    OS settings   all (3 nodes)",
        "  Packages Ansible does not install, as these nodes have none (*_install: false)",
        "    cassandra_install_tools: False   all (3 nodes)",
        "  Packages Ansible does not hold (apt), as these nodes have them not held",
        "    cassandra_package_hold: False   all (3 nodes)",
        "  Variables set to what each node has",
        "    cassandra_log_dir: /srv/log   all (3 nodes)"]


def test_check_says_the_secrets_are_not_written_yet():
    lines = report([node(i) for i in range(1, 4)], check=True, screen=True, report_file=REPORT_CHECK,
                   secrets_clear=["group_vars/my_cluster/secrets.yml"])
    text = " ".join(line.strip() for line in lines)
    assert "the run without --check writes the passwords read on the nodes in clear text there" in text
    assert "are written in clear text" not in text
    assert lines[-2:] == ["details: reports/my_cluster/report.check.details.txt",
                          "report: reports/my_cluster/report.check.txt"]


def test_screen_and_check():
    lines = report([node(i) for i in range(1, 4)], check=True, screen=True, report_file=REPORT_CHECK,
                   leftovers={"stale": ["host_vars/gone/main.yml"]})
    assert lines[0] == "IMPORT my_cluster (--check, inventory not written)  3 nodes  nothing to act on"
    assert lines[2].startswith("  would write: ") and lines[3] == " "  # the report's path: last (" ": a blank line)
    assert "  1. Write the inventory: the same command without --check." in lines
    assert lines[-1] == "report: reports/my_cluster/report.check.txt"  # written, report.txt left
    assert not has(lines, "WORTH A LOOK") and not has(lines, "DETAILS")
    full = report([node(i) for i in range(1, 4)], check=True, leftovers={"stale": ["host_vars/gone/main.yml"]})
    assert "    would be removed (an earlier import's, not written again): host_vars/gone/main.yml" in full
    # the shareable copy named on the screen when asked
    lines = report([node(1)], screen=True, shareable=True)
    assert lines[-1] == ("a copy to share (names, addresses and secrets replaced; check it before sharing):"
                         " reports/my_cluster/report.shareable.txt")
    # the screen: the table only, the steps in report.txt (said by its path), which has them in full
    lines = report([node(1), node(2, "16G")], screen=True)
    flat = " ".join(" ".join(lines).split())
    assert "where each value is set, and how to give every node one: reports/my_cluster/report.txt" in flat
    assert "to give every node 16G" not in flat and "convergence" not in flat
    full = " ".join(" ".join(report([node(1), node(2, "16G")], details=False)).split())
    assert "to give every node 16G: cassandra_max_heap_size: 16G in group_vars/my_cluster/main.yml" in full


def test_self_check_named_with_hand_edits_elsewhere_and_a_note():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  + concurrent_writes: 48"]),
             node(2, hand=["jmxremote.password: its users NOT imported: set cassandra_jmx_users by hand"])]
    checks = {"node3": {"differences": ["owner, group and mode: x"], "notes": []},
              "node1": {"differences": ["cassandra.yaml: y"], "notes": []}}
    lines = report(nodes, ok=False, self_check=checks)
    assert "  1. Make the self-check pass" in lines
    at = lines.index("  HOW TO MAKE IT PASS")
    assert [line.split()[0] for line in lines[at + 1:] if line.startswith("    ") and not line.startswith("     ")][:3] \
        == ["hand", "other", "a"]
    assert not [line for line in lines if "git commit" in line]  # not a valid inventory
    assert [line for line in lines if line.startswith("    jmxremote.password:")][0].split()[1:4] == ["its", "users", "NOT"]


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
    lines = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, width=160)
    review = section(lines, "WORTH A LOOK")
    assert review[0:5] == [
        "    cassandra_config_group (the group of the config files): cassandra, yours: cassandraops"
        " (group_vars/all/standard.yml)   node1, node2 (2 nodes)",
        "      kept as the nodes have it (in host_vars/<node>/main.yml); to apply yours, delete it there, then apply_config",
        "",
        "    cassandra_config_user (the owner of the config files): root, yours: cassandra (group_vars/all/standard.yml)"
        "   node1, node2 (2 nodes)",
        "      kept as the nodes have it (in host_vars/<node>/main.yml); to apply yours, delete it there, then apply_config"]
    assert lines[0].endswith("2 entries to review: 2 things worth a look")  # (not again as settings that differ)
    # the screen has it too
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True, width=160)
    assert review[0] in screen and screen[-3:] == ["", "details: reports/my_cluster/report.details.txt", "report: reports/my_cluster/report.txt"]


def test_differs_one_line_per_file_of_yours():
    # a value of yours in group_vars/all, another in a host's own file: each with its nodes
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [node(i) for i in range(1, 4)]
    for n in nodes:
        n["vars"].update(cassandra_config_user="cassandra")
    layout = cassandra_inventory_layout_over(nodes, "My Cluster", [
        {"path": "group_vars/all/s.yml", "content": "cassandra_config_user: zz\n"},
        {"path": "host_vars/node1/mine.yml", "content": "cassandra_config_user: yy\n"}])
    text = "\n".join(cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True))
    assert "yours: zz (group_vars/all/s.yml)" in text and "yours: yy (host_vars/node1/mine.yml)" in text


def test_reimport_check_with_nothing_to_change():
    # the whole screen: the head, what to do next, the report's path; nothing else
    unchanged = {"changed": [], "removed": [], "existed": ["my_cluster.yml", "group_vars/my_cluster/main.yml"]}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=unchanged)
    assert screen[:4] == [
        "IMPORT my_cluster (--check, inventory not written)  3 nodes  nothing to act on",
        "  self-check passed: the inventory gives every node that was read the settings it has now",
        "  unchanged: inventories/my_cluster.yml, group_vars/my_cluster*/", " "]  # (a blank line on the screen)
    assert screen[-1] == "report: reports/my_cluster/report.txt"
    assert not [line for line in screen if "Write the inventory" in line or "git commit" in line]
    # a thing to do still says so
    screen = report([node(1), node(2), node(3, read=False)], check=True, screen=True, changes=unchanged)
    assert section(screen, "TO DO")[0] == "  1. Let the import read node3 (1 node), then import again"


def test_check_says_what_it_would_write():
    # a first import: every file new
    first = {"changed": ["my_cluster.yml", "group_vars/my_cluster/main.yml", "group_vars/my_cluster/secrets.yml"],
             "removed": [], "existed": []}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=first)
    assert screen[2] == "  would write: inventories/my_cluster.yml, group_vars/my_cluster*/"
    assert "  1. Write the inventory: the same command without --check (3 new files)." in screen
    # a re-import: the files that change, the ones it removes
    again = {"changed": ["group_vars/my_cluster/main.yml", "host_vars/node4/main.yml"],
             "removed": ["host_vars/gone/main.yml"], "existed": ["my_cluster.yml", "group_vars/my_cluster/main.yml"]}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=again)
    assert ("  1. Write the inventory: the same command without --check (1 new file; changed:"
            " group_vars/my_cluster/main.yml; removed: host_vars/gone/main.yml).") in screen


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
    assert ("    cassandra_extra_settings.streaming_connections_per_host: 2   node1, node2, node3 (3 nodes)"
            "   (cassandra.yaml settings without a variable of their own)") in lines
    at = lines.index("    cassandra_extra_settings.sstable.selected_format: bti         all - node5 (4 nodes)")
    assert lines[at + 1] == "                                                      (not set)   node5 (1 node)"
    at = lines.index("    cassandra_extra_settings.server_encryption_options.keystore_password:")
    assert lines[at + 1:at + 3] == ["      (in secrets.yml)            node1 (1 node)",  # (a long name: its values under it)
                                    "      (in secrets.yml, value 2)   node2 (1 node)"]
    assert lines[at + 5] == "      (in secrets.yml, value 5)   node5 (1 node)"  # (every value: none folded)
    assert "{" not in "\n".join(line for line in lines if line.startswith("    cassandra_extra_settings"))


def test_a_secret_named_sub_key_is_hidden_whatever_its_parents():
    nodes = [node(i) for i in range(1, 3)]
    for n in nodes:
        n["vars"]["cassandra_medusa_extra_settings"] = {"storage": {"pw": "plain1", "bucket": "b"}}
    lines = report(nodes)
    assert "plain1" not in "\n".join(lines)
    assert [line.split()[1] for line in lines if ".storage.bucket:" in line] == ["b"]


def test_a_file_mode_is_never_a_secret():
    # (2026-11-02) cassandra_config_file_permissions' jmxremote.password is a file's name: its mode shown, in main.yml
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_files
    nodes = [node(i) for i in range(1, 3)]
    for n in nodes:
        n["vars"]["cassandra_config_file_permissions"] = {"jmxremote.password": "0400", "cassandra.yaml": "0640"}
    lines = report(nodes)
    assert [line.split()[1] for line in lines if "cassandra_config_file_permissions.jmxremote.password:" in line] \
        == ["0400"]
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    secrets = [f for f in cassandra_inventory_files(layout) if f["secret"]]
    assert all("cassandra_config_file_permissions" not in f["content"] for f in secrets)
    # a key with a dot is still a secret's name when its value is not a mode (ssl.password, truststore.password)
    for n in nodes:
        n["vars"]["cassandra_medusa_extra_settings"] = {"grpc": {"ssl.password": "Leak1", "truststore.password": "Leak2"}}
    text = "\n".join(report(nodes))
    assert "Leak1" not in text and "Leak2" not in text


def test_an_empty_value_is_shown():
    # (2026-11-02) an empty string as '', a null as (empty): never nothing after the name
    nodes = [node(1), node(2)]
    for n in nodes:
        n["vars"].update(cassandra_rpm_conf_alternative="", cassandra_cdc_raw_directory=None)
    lines = report(nodes)
    assert [line.split()[1] for line in lines if line.strip().startswith("cassandra_rpm_conf_alternative:")] == ["''"]
    assert [line.split()[1] for line in lines if line.strip().startswith("cassandra_cdc_raw_directory:")] == ["(empty)"]


def test_long_end_lines_in_a_snippet():
    # (2026-11-02) a monitoring agent's block at the end of cassandra-env.sh: in its snippet, the report names it
    block = ["# BEGIN agent"] + ['JVM_OPTS="$JVM_OPTS -javaagent:/opt/agent/agent.jar=port=%d,config=/opt/agent/'
                                 'a-very-long-configuration-file-name.yaml"' % (9000 + i) for i in range(18)] + ["# END"]
    nodes = [node(i) for i in range(1, 4)]
    for n in nodes:
        n["vars"]["(cassandra-env.sh)"] = list(block)
    lines = report(nodes)
    # (the import writes them in a snippet next to the inventory: its path)
    rows = [line for line in lines if line.strip().startswith("cassandra_config_template_snippets")]
    assert rows == ["    cassandra_config_template_snippets.cassandra-env.sh: group_vars/my_cluster/cassandra-env.sh.snippet.j2"
                    "  all (3 nodes)"]
    assert "a-very-long-configuration" not in "\n".join(lines[:heading(lines, "DETAILS")])
    # a long value of its own: cut
    nodes[0]["vars"]["cassandra_extra_settings"] = {"x": "y" * 100}
    assert [line for line in report(nodes) if "yyy..." in line]


def test_hand_edits_by_file_counted():
    logback = ["logback.xml, line %d:" % i for i in (10, 20, 30)]
    same = [line for head in logback for line in (head, "  - <a/>", "  + <b/>", "  + <c/>")]
    other = same[:4]
    nodes = [node(1, hand=same), node(2, hand=same), node(3, hand=other),
             node(4, hand=same + ["jvm11-server.options, line 3:", "  + -XX:A=1", "  + -XX:B=2"]),
             node(5, hand=["cassandra.yaml, line 10:", "  + concurrent_writes: 48"] * 2)]
    lines = report(nodes)
    entries = [line for line in section(lines, "HAND EDITS") if line.startswith("    ") and line[4] != " "]
    assert entries == [
        "    cassandra.yaml (Cassandra's main settings file) concurrent_writes: 48   node5 (1 node)",
        "    logback.xml (the logging settings): 3 edits   node1, node2, node4 (3 nodes)",
        "    logback.xml: 1 edit   node3 (1 node)",
        "    jvm11-server.options (Java's options, Java 11 only): 1 edit   node4 (1 node)"]
    assert lines[0] == "IMPORT my_cluster  5 nodes  4 entries to review: 4 hand-edit entries"  # (no TO DO)
    # one way to keep them: the file left as the node has it
    assert ("the lines are under DETAILS, node by node; to keep them: import again with"
            " -e import_cluster_keep_hand_edits=logback.xml (Ansible then leaves logback.xml as each node has it)") in \
        " ".join(" ".join(lines).split())
    # two sets of as many edits: told apart, numbered among them
    nodes = [node(1, hand=same), node(2, hand=other), node(3, hand=[line.replace("<c/>", "<d/>") for line in same])]
    lines = report(nodes)
    found = [re.search(r"\d+ edits?( \(variant \d\))?", line).group(0) for line in lines
             if line.startswith("    logback.xml")]
    assert found == ["3 edits (variant 1)", "1 edit", "3 edits (variant 2)"]


def test_an_edit_with_each_node_s_own_address_is_one_entry():
    # (2026-11-13) 24 nodes, each its own JVM_OPTS line with its address: one entry, not one "variant" per node
    def rmi(i):
        return ["cassandra-env.sh, line 300:", "  + # mine", '  + JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.%d"' % i]
    nodes = [node(i, hand=rmi(i)) for i in (1, 2, 10)]
    entries = [line for line in section(report(nodes), "HAND EDITS") if line.startswith("    ") and line[4] != " "]
    assert entries == ["    cassandra-env.sh (the script that sets Java's options when Cassandra starts): 1 edit   all (3 nodes)"]
    checks = dict(("node%d" % i, {"differences": [
        'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.%d", import would write'
        ' nothing' % i]}) for i in (1, 2, 10))
    items = _self_check_items(nodes, checks)
    assert [(i["text"], len(i["lines"])) for i in items] == [
        ('JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<its address>"', 3)]


def test_a_logback_hand_edit_says_the_templates_of_yours():
    # no variable carries it (the audit file at another path): its way out, beside keeping the file as it is
    hand = ["logback.xml, line 90:", "  -     <file>${cassandra.logdir}/audit/audit.log</file>",
            "  +     <file>/data/audit/audit.log</file>"]
    text = " ".join(" ".join(section(report([node(1, hand=hand)]), "HAND EDITS")).split())
    assert "-e import_cluster_keep_hand_edits=logback.xml (Ansible then leaves logback.xml as each node has it); or" \
           " carry them: your own template of logback.xml (cassandra_config_template_overrides), or a template piece" \
           " for lines at its end (cassandra_config_template_snippets)" in text
    hand = ["cassandra.yaml, line 10:", "  + concurrent_writes: 48"]
    assert "cassandra_config_template_overrides" not in " ".join(section(report([node(1, hand=hand)]), "HAND EDITS"))


def test_written_host_vars_counted_on_a_large_cluster():
    nodes = [node(i, "16G" if i % 4 == 1 else "8G") for i in range(1, 60, 2)]  # node1, node3...: no range
    lines = report(nodes, screen=True)
    assert lines[2].endswith("host_vars/<node>/ of 30 nodes")
    again = {"changed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)] + ["group_vars/my_cluster/main.yml"],
             "removed": [], "existed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)]
             + ["group_vars/my_cluster/main.yml"]}
    lines = report(nodes, screen=True, check=True, changes=again, width=300)
    assert [line for line in lines if "(changed: host_vars/<node>/main.yml of 29 nodes, group_vars/my_cluster/main.yml)"
            in line]


def test_the_screen_fits_the_terminal():
    nodes = [node(i, hand=["cassandra.yaml, line 10:", "  + setting_%d: 1" % i]) for i in range(1, 20)]
    for line in report(nodes, screen=True, width=80)[1:]:
        assert len(line) <= 80 or "-e import_cluster_keep_hand_edits=" in line, line


def test_a_secret_setting_of_a_hand_edit_is_never_shown():
    # (masked where the edits are made; this one as a line might come)
    lines = report([node(1, hand=["cassandra.yaml, line 10:", "  + ldap_password: s3cret"])])
    assert "s3cret" not in "\n".join(lines)
    assert [line for line in lines if line.startswith("    cassandra.yaml (Cassandra's main settings file) ldap_password:"
                                                      " ****   all (1 node)")]


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


def test_kept_files_left_as_is_to_review():
    # a file with hand edits kept (import_cluster_keep_hand_edits): an entry of its own, its nodes
    hand = ["jmxremote.access: auditRole=readonly", "  + auditRole=readonly"]
    lines = report([node(1, hand=hand, keep={"cassandra_config_keep_files": ["jmxremote.access"]}), node(2)])
    review = section(lines, "LEFT AS THE NODES HAVE THEM")
    assert review[:2] == [
        "    jmxremote.access   node1 (1 node)",
        "      1 edit that no variable carries (each under DETAILS): Ansible leaves the file as each node has it; nothing"
        " to do while they are wanted"]
    assert "1 file left as is" in lines[0]


def test_the_screen_width_is_ansibles(monkeypatch):
    from ansible_collections.community.cassandra.plugins.filter import cassandra_output as module

    class Narrow(object):
        columns = 70
    monkeypatch.setattr(module, "Display", Narrow)
    nodes = [node(i, hand=["cassandra.yaml, line 10:", "  + setting_%d: 1" % i]) for i in range(1, 20)]
    screen = cassandra_import_report(cassandra_inventory_layout(nodes, "My Cluster"), WRITTEN, REPORT, {}, True,
                                     cwd="/p", in_git=True, screen=True)
    assert max(len(line) for line in screen[1:] if "keep_hand_edits=" not in line) <= 70 < max(
        len(line) for line in report(nodes, screen=True))
    assert max(len(line) for line in screen[1:]) <= 80  # (an option never cut: on a line of its own)


def test_keys_cassandra_rejects_a_step_to_do():
    rejected = {"keys": ["fake_option"], "release": "4.1.5"}
    nodes = [node(1, not_settings=rejected), node(2, not_settings=rejected), node(3, not_settings=rejected),
             node(4, not_settings={"keys": ["fake_option", "other_fake"], "release": "4.1.6"}), node(5)]
    lines = report(nodes)
    todo = section(lines, "TO DO")
    assert todo[:4] == [
        "  1. Remove fake_option from cassandra.yaml on node1, node2, node3 (3 nodes)",
        "     Cassandra 4.1.5 does not know this setting. It runs fine now, but would not start again with it after a"
        " restart; the import left it out of the inventory",
        "     run:    on each of these nodes, delete the line fake_option from cassandra.yaml (in the config directory"
        " DETAILS names)",
        "     check:  the next import no longer lists it"]
    assert todo[4] == "  2. Remove fake_option, other_fake from cassandra.yaml on node4 (1 node)"
    # the screen: the same step, nodes in short
    screen = report(nodes, screen=True)
    assert "  1. Remove fake_option from cassandra.yaml on node1..node3 (3 nodes)" in screen
    # none: no step
    assert not [line for line in report([node(1)]) if "does not know" in line]


def test_os_tuning_for_info():
    os_lines = {"lines": ["sysctl vm.max_map_count: 65530 (1048575)"]}
    nodes = [node(i, os=os_lines) for i in range(1, 4)]
    lines = report(nodes)
    at = lines.index("  OS tuning: the live value of each setting (and the collection's)")
    assert lines[at + 1] == "    sysctl vm.max_map_count: 65530 (1048575)   all (3 nodes)"
    nodes[0]["not_settings"] = {"keys": ["x"], "release": "4.1.5"}
    assert lines[at + 1] in report(nodes)


def test_versions_for_info():
    nodes = [node(i, versions={"cassandra": "5.0.9", "java": "17.0.12", "os": "Debian 12"}) for i in range(1, 4)]
    nodes[2]["versions"] = dict(nodes[2]["versions"], java="17.0.8")
    lines = report(nodes)
    at = lines.index("  Versions")
    assert lines[at + 1:at + 3] == [
        "    Cassandra 5.0.9, Java 17.0.12, Debian 12   all - node3 (2 nodes)",
        "    Cassandra 5.0.9, Java 17.0.8, Debian 12   node3 (1 node)"]


def test_a_jvm_warning_to_review_in_two_lines():
    warning = ("-Xss1024M (cassandra_jvm_extra_options, from jvm-server.options, Java 17): 1 GiB of stack for every"
               " thread. Fix or remove it in cassandra_jvm_extra_options: it is kept as it is until then.")
    lines = report([node(1, jvm_warnings=[warning]), node(2, jvm_warnings=[warning])])
    review = section(lines, "WORTH A LOOK")
    assert review[0:2] == [
        "    -Xss1024M (cassandra_jvm_extra_options, from jvm-server.options, Java 17)   all (2 nodes)",
        "      1 GiB of stack for every thread. Fix or remove it in cassandra_jvm_extra_options: it is kept as it is until"
        " then."]


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
        "    - cassandra-env.sh, on node1 (1 node):",
        "        export KEYSTORE_PASSWORD=****",
        "    - jvm17-server.options, on node2, node5 (2 nodes):",
        "        -XX:+UseFoo (not in the inventory)",
        "  Your inventory files override the import (Ansible will apply your value):",
        "    - cassandra.yaml concurrent_reads, on node3 (1 node):",
        "        node 64, inventory 16 (set in host_vars/node3/local.yml)",
        "  Other differences:",
        "    - cassandra.yaml server_encryption_options.keystore_password, on node1 (1 node):",
        "        node ****, inventory ****",
        "    - jvm17-server.options, on node1, node2 (2 nodes):",
        "        not on the node, the roles would create it",
        "    - owner, group and mode, on node2 (1 node):",
        "        not read on the node, the roles may change them",
        "    - heap MAX_HEAP_SIZE, on node3 (1 node):",
        "        node 8G, inventory 16G",
        "  Not read, so not checked: node4 (unreachable)",
        "",
        "HOW TO MAKE IT PASS",
        "  hand edits          to keep them as they are: import again with",
        "                      -e import_cluster_keep_hand_edits=true; these files are then not managed by",
        "                      Ansible (kept as the node has them): cassandra-env.sh on node1 (1 node);",
        "                      jvm17-server.options on node2, node5 (2 nodes). To bring them under Ansible:",
        "                      carry the edit in the inventory (cassandra_jvm*_extra_options, a template",
        "                      piece of yours for lines at the end of cassandra-env.sh or logback.xml,",
        "                      cassandra_config_template_snippets, cassandra_extra_settings, ...) or give the",
        "                      file your own template (cassandra_config_template_overrides), remove the file",
        "                      from cassandra_config_keep_files, import again. To drop them instead: undo",
        "                      them on the node, then import again",
        "  your files          in host_vars/node3/local.yml, set cassandra_concurrent_reads to the node's",
        "                      value (64) or remove the line, then import again",
        "  not read            start Cassandra or fix the access on node4 (1 node), then import again; or",
        "                      accept it unchecked: -e import_cluster_allow_unread=true",
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
                          "    - logback.xml, on all - node3 (2 nodes):", '        /configuration/logger[level="DEBUG" name="x"]']
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
                          "    - logback.xml owner:group mode, on node1 (1 node):", "        node root:root 0600, inventory root:cassandra 0644",
                          "    - cassandra.yaml concurrent_writes, on node2 (1 node):", "        node 32, inventory 64"]
    assert "keep_hand_edits" not in "\n".join(lines) and "  Other differences:" in lines


def test_self_check_stop_values_per_node():
    # nodes with the same value together; the inventory's value per node when it differs
    checks = {"node1": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node3": {"differences": ["cassandra.yaml: concurrent_reads: node has '128', import would write '32'"]},
              "node4": {"differences": ["cassandra.yaml: concurrent_writes: node has '8', import would write '16'"]},
              "node5": {"differences": ["cassandra.yaml: concurrent_writes: node has '9', import would write '32'"]}}
    lines = stop([node(i) for i in range(1, 6)], checks)
    at = lines.index("    - cassandra.yaml concurrent_reads, on node1..node3 (3 nodes):")
    assert lines[at + 1:at + 6] == ["        node1, node2 (2 nodes): node 64, inventory 32",
                                    "        node3 (1 node): node 128, inventory 32",
                                    "    - cassandra.yaml concurrent_writes, on node4, node5 (2 nodes):",
                                    "        node4 (1 node): node 8, inventory 16", "        node5 (1 node): node 9, inventory 32"]


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
    # the same grouping in report.txt (report.check.txt), under TO DO, each node's differences under its item, whole
    lines = report(failed_nodes(), ok=False, self_check=CHECKS, layout_extra=WINS)
    at = lines.index(u"  SELF-CHECK \u2014 the inventory would change 7 settings on 4 nodes; 1 node not read")
    end = lines.index("", lines.index("  HOW TO MAKE IT PASS", at))
    part = lines[at:end]
    assert part[1:13] == [
        "    Hand edits no variable covers (cassandra_config would revert them):",
        "      - cassandra-env.sh, on node1 (1 node):",
        "          export KEYSTORE_PASSWORD=****",
        "            node1 (1 node): cassandra-env.sh: node has export KEYSTORE_PASSWORD=****",  # (masked to the end)
        "      - jvm17-server.options, on node2, node5 (2 nodes):",
        "          -XX:+UseFoo (not in the inventory)",
        "            node2, node5 (2 nodes): jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write",
        "              nothing",
        "    Your inventory files override the import (Ansible will apply your value):",
        "      - cassandra.yaml concurrent_reads, on node3 (1 node):",
        "          node 64, inventory 16 (set in host_vars/node3/local.yml)",
        "            node3 (1 node): cassandra.yaml: concurrent_reads: node has '64', import would write '16'"]
    assert max(len(line) for line in part) <= 112
    assert "Details:" not in "\n".join(part)  # (it is the details)
    assert "  2. Make the self-check pass" in lines and lines.index("  2. Make the self-check pass") < at
    # the screen: the step, the stop below it (import_cluster_strict) says how
    screen = report(failed_nodes(), ok=False, self_check=CHECKS, screen=True)
    assert "  2. Make the self-check pass" in screen and not [line for line in screen if "SELF-CHECK \u2014" in line]
    # passed: no section
    assert not [line for line in report(failed_nodes()[:1]) if "SELF-CHECK" in line]


def test_self_check_report_same_line_once_for_its_nodes():
    # a large cluster: the line every node has once, by the node groups; a value of its own on its own line
    nodes = [node(i) for i in range(1, 21)]
    line = ("jmxremote.access: controlRole: node has 'create javax.management.monitor.*,javax.management.timer.*"
            " readwrite unregister', import would write nothing")
    checks = dict(("node%d" % i, {"differences": [line]}) for i in range(1, 21))
    checks["node7"] = {"differences": [line.replace("readwrite unregister", "readwrite")]}
    checks["node1"] = {"differences": [line, line]}  # (twice: named once)
    lines = report(nodes, ok=False, self_check=checks)
    at = lines.index(u"  SELF-CHECK \u2014 the inventory would change 1 setting on 20 nodes")
    part = lines[at:lines.index("  HOW TO MAKE IT PASS", at)]
    assert " ".join(part).count("node has") == 2
    assert [x for x in part if x.strip().startswith("node7 (1 node): jmxremote.access: controlRole: node has")]
    assert [x for x in part if x.strip().startswith("all - node7 (19 nodes): jmxremote.access")]
    assert "node1, node1" not in " ".join(part)


def test_self_check_stop_owner_of_a_hand_edited_file_is_not_a_hand_edit():
    # keep_hand_edits keeps the content of a file, not its owner, group and mode
    nodes = [node(1, hand=LOGBACK)]
    checks = {"node1": {"differences": ["logback.xml: owner:group mode: node has root:root 0600, import would write"
                                        " root:cassandra 0644",
                                        "logback.xml: node has /configuration/logger, import would write nothing"]}}
    lines = stop(nodes, checks)
    assert lines[3:9] == ["  Hand edits no variable covers (cassandra_config would revert them):",
                          "    - logback.xml, on all (1 node):", "        /configuration/logger",
                          "  Other differences:",
                          "    - logback.xml owner:group mode, on all (1 node):", "        node root:root 0600, inventory root:cassandra 0644"]


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
    assert lines[at + 1:at + 3] == ["    - cassandra.service [Service] LimitNOFILE, on all (1 node):",
                                    "        node 100000, inventory 10 (set in host_vars/node1/z.yml)"]
    assert lines[at + 3:at + 8] == ["  Other differences:", "    - cassandra.yaml cdc_enabled, on all (1 node):",
                                    "        node true, inventory false", "    - cassandra.yaml num_tokens, on all (1 node):",
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
    at = lines.index("    - cassandra-env.sh, on all (1 node):")
    assert lines[at + 1:at + 4] == ['        JVM_OPTS="-Dx=a: node has b"', "    - cassandra.yaml, on all (1 node):",
                                    "        cannot be read as its program reads it (node's or the roles' version)"]


def test_self_check_stop_large_cluster_node_lists():
    # never a group label of the report's own (G1): the DCs, else the names, with their count
    nodes = [dict(node(i), dc="dc1" if i <= 15 else "dc2") for i in range(1, 31)]
    checks = dict(("node%d" % i, {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would"
                                                  " write '32'"]}) for i in range(1, 15))
    lines = stop(nodes, checks)
    assert "    - cassandra.yaml concurrent_reads, on dc1 - node15 (14 nodes):" in lines
    assert "        node 64, inventory 32" in lines and not [line for line in lines if re.search(r"\bG\d", line)]


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
    assert "    - jvm17-server.options javax.net.ssl.keyStorePassword, on all (1 node):" in lines
    assert "        node ****, inventory ****" in lines


def test_auto_bootstrap_false_kept_on_its_nodes_with_a_warning():
    # host_vars only, even on every node (a node added later must not get it); a WARNING, the import not refused
    nodes = [node(i) for i in range(1, 4)]
    for n in nodes[:2]:
        n["vars"]["cassandra_auto_bootstrap"] = False
    layout = cassandra_inventory_layout(nodes, "My Cluster")
    assert [h for h, v in layout["host_vars"].items() if v.get("cassandra_auto_bootstrap") is False] == ["node1", "node2"]
    assert not [g for g, v in layout["group_vars"].items() if "cassandra_auto_bootstrap" in v]
    text = "\n".join(report(nodes))
    # an entry of two lines to review: the finding, then what to do and how to keep it
    assert ("    auto_bootstrap false: a node that loses its data would restart empty, without streaming it back   all -"
            " node3 (2 nodes)\n      remove cassandra_auto_bootstrap (the default is true); these nodes change at their"
            " next apply_config (a restart each); kept on purpose: list\n      auto_bootstrap_false in"
            " cassandra_settings_kept_on_purpose") in text
    every = [node(i) for i in range(1, 4)]
    for n in every:
        n["vars"]["cassandra_auto_bootstrap"] = False
    assert sorted(cassandra_inventory_layout(every, "My Cluster")["host_vars"]) == ["node1", "node2", "node3"]


def test_replace_flag_in_a_hand_edit_warned_with_its_line():
    # a node read is a member of the ring: a replace flag left in cassandra-env.sh, the exact line to remove
    nodes = [node(1, hand=["cassandra-env.sh, line 290:", '  + JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"']),
             node(2)]
    text = "\n".join(report(nodes))
    assert ("    replace flag -Dcassandra.replace_address=10.0.0.9 in the node's config: the node is bootstrapped already:"
            " Cassandra refuses its next start with this flag\n      node1 (1 node)\n      remove the line"
            ' JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"; kept on purpose: list replace_address in'
            " cassandra_settings_kept_on_purpose") in text
    # never offered to keep as a hand edit
    assert "      a replace flag: remove it, see Settings to review" in text


def test_a_suspicious_setting_does_not_change_whom_the_other_settings_are_for():
    # the WARNING names node1; the settings every node has are still for all of them
    nodes = [node(i) for i in range(1, 4)]
    nodes[0]["vars"]["cassandra_auto_bootstrap"] = False
    lines = report(nodes)
    row = [line for line in lines if line.strip().startswith("cassandra_num_tokens:")][0]
    assert "all (3 nodes)" in row, row


# --- a cluster like a real one taken over (made up): JMX opened by hand, the audit log in logback, the OS left ------

ENV_EDITS = ["cassandra-env.sh, line 249:",
             '  +   JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file=/etc/cassandra/jmxremote.password"',
             '  +   JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=%s"']
LOGBACK_EDITS = ["logback.xml, line 89:", '  -   <!-- <appender name="AUDIT" class="x"> -->', '  +   <appender name="AUDIT" class="x">']
OS_LINES = ["THP now: enabled always, defrag madvise (cassandra_linux: never never)",
            "THP set by /etc/rc.d/rc.local (not executable)",
            "tuned: profile virtual-guest (tuned active)",
            "tuned /usr/lib/tuned/throughput-performance/tuned.conf [disk]: readahead=>4096",
            "device vg-data (dm-3) (/var/lib/cassandra/data, SSD): read_ahead_kb 4096 (cassandra_linux: 4), scheduler none",
            "disk sdb (under vg-data (dm-3), SSD): read_ahead_kb 4096 (cassandra_linux: 4), scheduler mq-deadline"]


def taken_over(i):
    n = node(i, hand=[line % ("10.0.0.%d" % i) if "%s" in line else line for line in ENV_EDITS] + LOGBACK_EDITS,
             keep={"cassandra_config_keep_files": ["cassandra-env.sh", "logback.xml"], "cassandra_linux_manage": False},
             os={"lines": OS_LINES}, unit={"restart": "no"}, medusa={"install": True, "ini": False})
    n["vars"].update(cassandra_local_jmx=False)
    return n


def test_worth_a_look_what_was_seen_on_the_nodes():
    review = section(report([taken_over(i) for i in range(1, 4)], details=False), "WORTH A LOOK")
    entries = [line.strip() for line in review if line.startswith("    ") and not line.startswith("     ")]
    assert entries == [
        "transparent huge pages enabled always, set by /etc/rc.d/rc.local, which is not executable   all (3 nodes)",
        "read-ahead 4096 KB on the data SSDs (cassandra_linux: 4), and tuned (profile virtual-guest) sets"
        " readahead=>4096 at each start   all (3 nodes)",
        "Medusa installed, no /etc/medusa/medusa.ini: it is not set up there   all (3 nodes)"]
    # a node with them right: nothing said (its Restart= carried into cassandra_service_restart, as the import does)
    calm = node(1, os={"lines": ["THP now: enabled never, defrag never (same as cassandra_linux)",
                                 "device sda (/var/lib/cassandra, SSD): read_ahead_kb 8 (cassandra_linux: 4)"]},
                unit={"restart": "on-failure"}, medusa={"install": True, "ini": True})
    calm["vars"]["cassandra_service_restart"] = "on-failure"
    assert not has(report([calm], details=False), "WORTH A LOOK")


def test_restart_no_the_collections_default_is_not_worth_a_look():
    # Restart=no is cassandra_service_restart's default too: the inventory gives the node what it has, nothing said
    # (it was said "worth a look", with "cassandra_service's own unit restarts it": its default is no)
    nodes = [node(i, unit={"restart": "no"}) for i in range(1, 4)]
    lines = report(nodes)
    assert not has(lines, "WORTH A LOOK")
    assert not [line for line in lines if "unit restarts it" in line]


def test_restart_other_than_the_inventorys_says_who_restarts():
    # a node's Restart= other than the inventory's cassandra_service_restart: systemd's doing, both values said
    nodes = [node(1, unit={"restart": "no"}), node(2, unit={"restart": "no"})]
    for n in nodes:
        n["vars"]["cassandra_service_restart"] = "on-failure"
    review = [" ".join(line.split()) for line in section(report(nodes, details=False), "WORTH A LOOK")]
    text = " ".join(review)
    assert review[0] == ("Restart=no in Cassandra's systemd unit, cassandra_service_restart: on-failure in the"
                         " inventory all (2 nodes)")
    assert "systemd, not Ansible, restarts Cassandra, by the unit's Restart=" in text
    assert "Restart=no: systemd leaves a Cassandra that stops down until someone starts it" in text
    assert "Restart=on-failure: systemd starts Cassandra again when it dies on an error, a signal or a timeout" in text
    assert "the next run of cassandra_service writes Restart=on-failure in the unit" in text
    # a node keeping its own unit: the variable does not change it
    kept = node(1, unit={"restart": "always"}, keep={"cassandra_service_unit_manage": False})
    text = " ".join(" ".join(line.split()) for line in section(report([kept], details=False), "WORTH A LOOK"))
    assert "Restart=always in Cassandra's systemd unit, cassandra_service_restart: no in the inventory" in text
    assert "(cassandra_service_unit_manage: false): cassandra_service_restart does not change it" in text


def _java11_node(i, keep_file="jvm8-server.options", java="11.0.22"):
    hand = ["%s, line 12:" % keep_file, "  - #-Xmx4G", "  + -Xmx4G", "  + -XX:+UseParNewGC"]
    n = node(i, hand=hand, keep={"cassandra_config_keep_files": [keep_file]},
             versions={"cassandra": "4.1.5", "java": java, "os": "RHEL 8"})
    n["vars"]["cassandra_version"] = "41x"
    return n


def test_a_kept_options_file_its_java_does_not_read_carries_nothing():
    # Java 11 reads jvm11-server.options, not jvm8-server.options: the heap of jvm8's never in cassandra_heap_size
    # (it would change the heap the node runs), its options in no variable; the way out: out of keep_files
    other = node(2)
    other["vars"]["cassandra_version"] = "41x"
    lines = report([_java11_node(4), other], details=False)
    left = [" ".join(line.split()) for line in section(lines, "LEFT AS THE NODES HAVE THEM")]
    at = left.index("jvm8-server.options (Java's options, Java 8 only) node4 (1 node)")
    entry = left[at:]
    assert entry[:7] == [
        "jvm8-server.options (Java's options, Java 8 only) node4 (1 node)",
        "not read by the Java node4 runs (observed: Java 11)",
        "its edits have no effect on the running JVM: no variable carries them",
        "to give it back to Ansible:",
        "1. in host_vars/node4/main.yml: remove jvm8-server.options from cassandra_config_keep_files",
        "2. check: ansible-playbook community.cassandra.apply_config --limit node4 --check --diff → what its diff still"
        " shows, Ansible would undo",
        "3. the same without --check: it writes jvm8-server.options, then restarts the nodes (the same Java options:"
        " their Java does not read it)"]
    text = "\n".join(lines)
    assert "cassandra_heap_size" not in text and "cassandra_jvm8_extra_options" not in text
    # the Java that reads it (Java 8): its heap and options carried as before
    lines = report([_java11_node(4, java="1.8.0_402"), other], details=False)
    text = " ".join(" ".join(line.split()) for line in section(lines, "LEFT AS THE NODES HAVE THEM"))
    assert "cassandra_heap_size: 4G, cassandra_jvm8_extra_options: [-XX:+UseParNewGC]" in text
    assert "not read by the Java" not in text


def test_a_kept_options_file_its_java_does_not_read_in_the_convergence():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import (
        cassandra_import_convergence)
    other = node(2)
    other["vars"]["cassandra_version"] = "41x"
    lines = cassandra_import_convergence(cassandra_inventory_layout([_java11_node(4), other], "My Cluster"))
    at = lines.index("  KEPT BY HAND — cassandra_config_keep_files: left as their node has them")
    text = " ".join(" ".join(line.split()) for line in lines[at:])
    assert "jvm8-server.options kept on node4 (1 node) not read by the Java the node runs (observed: Java 11)" in text
    assert "its edits have no effect on the running JVM: no variable carries them" in text
    assert "take jvm8-server.options out of cassandra_config_keep_files (host_vars/node4/)" in text
    assert "cassandra_heap_size" not in text and "cassandra_jvm8_extra_options" not in text


def test_a_kept_file_given_back_step_by_step():
    lines = report([taken_over(i) for i in range(1, 4)], details=False)
    left = section(lines, "LEFT AS THE NODES HAVE THEM")
    flat = [" ".join(line.split()) for line in left]
    at = flat.index("cassandra-env.sh (the script that sets Java's options when Cassandra starts) all (3 nodes)")
    steps = " ".join(flat[at + 1:flat.index("", at)])
    # the password file the collection writes already: said given; the address each node's own, in its host_vars
    assert "to give it back to Ansible (cassandra_jmx_remote_password_file: already given):" in steps
    assert "1. in host_vars/<node>/main.yml of 3 nodes: cassandra_jmx_rmi_hostname: <each node's own address>" in steps
    assert "2. in host_vars/<node>/main.yml of 3 nodes: remove cassandra-env.sh from cassandra_config_keep_files" in steps
    assert "3. check: ansible-playbook community.cassandra.apply_config --limit node1,node2,node3 --check --diff" in steps
    assert "4. the same without --check: it writes cassandra-env.sh, then restarts the nodes" in steps
    # edits no variable carries: nothing to do while wanted
    assert "1 edit that no variable carries (each under DETAILS): Ansible leaves the file as each node has it; nothing" \
        " to do while they are wanted" in " ".join(flat[flat.index("logback.xml (the logging settings) all (3 nodes)"):])
    assert "    not managed by Ansible here: OS settings   all (3 nodes)" in left


def test_details_said_once_for_every_node():
    details = report([taken_over(i) for i in range(1, 4)], details=True)
    at = details.index("  == node1, node2, node3 (3 nodes): what every one has")
    shared = details[at:]
    # the edit with each node's own address: one line for all of them
    assert '        +   JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<its address>"' in shared
    assert not [line for line in details if "10.0.0.2" in line and "rmi" in line]
    assert not [line for line in details if line.startswith("  == node2")]  # (nothing of its own)
    # a node with lines of its own: those only, under the lines they belong to
    nodes = [taken_over(i) for i in range(1, 4)]
    nodes[1]["hand_edits"] += ["cassandra.yaml, line 10:", "  + concurrent_writes: 48"]
    details = report(nodes, details=True)
    at = details.index("  == node2: its own")
    own = details[at + 1:details.index("", at)]
    assert own[-2:] == ["      cassandra.yaml, line 10:", "        + concurrent_writes: 48"]
    assert own[0].startswith("    Hand edits no variable carries") and len(own) == 3  # (the line it belongs to, then its own only)


def report_over(nodes, yours):
    """report.txt with a file of yours (its values as the nodes have them are left to it, out of their variables)."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        cassandra_inventory_layout_over)
    layout = cassandra_inventory_layout_over(nodes, "My Cluster", [{"path": "group_vars/all.yml", "content": yours}])
    return cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, width=160), layout


def test_your_files_values_count_for_the_series_and_the_restart_policy():
    # (audit) a file of yours giving the same cassandra_version and cassandra_service_restart as the nodes: they
    # leave the nodes' variables, the report reads them from yours
    nodes = [_java11_node(4), node(2, unit={"restart": "always"})]
    nodes[1]["vars"].update(cassandra_version="41x", cassandra_service_restart="always")
    nodes[0]["vars"]["cassandra_service_restart"] = "always"
    nodes[0]["unit"] = {"restart": "always"}
    lines, layout = report_over(nodes, "cassandra_version: 41x\ncassandra_service_restart: always\n")
    assert "cassandra_version" in layout["from_yours"]["node4"]  # (else the test proves nothing)
    text = "\n".join(lines)
    assert "not read by the Java node4 runs (observed: Java 11)" in text
    assert "cassandra_heap_size: 4G" not in text
    assert not has(lines, "WORTH A LOOK")


def test_the_check_details_say_check_and_name_its_own_report():
    # (owner list) report.check.details.txt: inventory not written, and report.check.txt named, not report.txt
    lines = report([node(1), node(2)], check=True, report_file=REPORT_CHECK, details=True)
    text = "\n".join(lines)
    assert "IMPORT my_cluster (--check, inventory not written)  the details of report.check.txt" in lines
    assert "worked out (--check: not written) an Ansible inventory" in text and "and wrote an" not in text
    assert "report.check.txt says what to do" in text and "report.txt says" not in text


def test_packages_not_installed_apart_from_what_ansible_leaves():
    # (owner list) *_install: false is a package the nodes have none of, not a part Ansible leaves as it is
    keep = {"cassandra_linux_manage": False, "cassandra_install_jemalloc": False, "cassandra_dsbulk_install": False}
    left = section(report([node(1, keep=keep), node(2, keep=keep)], details=False), "LEFT AS THE NODES HAVE THEM")
    assert [" ".join(line.split()) for line in left] == [
        "not managed by Ansible here: OS settings all (2 nodes)",
        "not installed by Ansible here, as these nodes have none (true installs it): DSBulk, jemalloc all (2 nodes)"]


def test_worth_a_look_says_which_nodes_get_the_change():
    # (owner list) existing nodes or nodes added later: said with each change to make
    review = " ".join(" ".join(line.split()) for line in section(
        report([taken_over(i) for i in range(1, 4)], details=False), "WORTH A LOOK"))
    assert "these nodes: Ansible leaves their OS settings (cassandra_linux_manage: false), change it on them by hand;" \
           " nodes added later: cassandra_linux sets it" in review
    small = node(1)
    small["vars"]["cassandra_heap_size"] = "1G"
    review = " ".join(" ".join(line.split()) for line in section(report([small], details=False), "WORTH A LOOK"))
    assert "these nodes get it at their next apply_config (a restart each), nodes added later too" in review
