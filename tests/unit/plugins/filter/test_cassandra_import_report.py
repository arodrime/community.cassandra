from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import's report and the screen that ends the run: by urgency (a head with the verdict and its counts, TODO,
# TO REVIEW, FOR INFO, what to do next), for a reader who may be new to Cassandra and to the collection.

import os
import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    KEPT_PARTS_VAR, _compress, _self_check_items, cassandra_import_report, cassandra_import_self_check_advice, node_list)
from ansible_collections.community.cassandra.tests.unit.task_files import find_play

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


def paragraph(lines, first):
    """A paragraph of the head: its line starting with first, the rows under it (up to a blank line)."""
    at = next(i for i, line in enumerate(lines) if line.startswith(first))
    end = next((i for i in range(at + 1, len(lines)) if not lines[i].strip()), len(lines))
    return lines[at:end]


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
    assert lines[:12] == ["IMPORT my_cluster  5 nodes  nothing to act on", "",
                          "  in sync with Ansible: yes", "",
                          "  self-check passed",
                          "    every node read: its settings are in the inventory", "",
                          "  written:",
                          "    inventories/my_cluster.yml",
                          "    group_vars/my_cluster*/", "",
                          "  report:  reports/my_cluster/report.txt"]
    # each section: two blank lines, its title in a rule, a blank line; what the report is: in the details
    assert lines[12:16] == ["", "", "==== WHAT TO DO NEXT " + "=" * 76, ""]
    assert lines[heading(lines, "WORDS") - 2:heading(lines, "WORDS") + 2] == ["", "", "==== WORDS " + "=" * 89, ""]
    assert not has(lines, "TODO") and not has(lines, "WORTH A LOOK") and not has(lines, "DIFFERENT ON SOME NODES")
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
    # each command on a line of its own, what it should show under it, no arrow
    assert after[1:] == [
        "  2. Check that the inventory changes nothing on the nodes (both commands only look):",
        "       ansible-playbook community.cassandra.apply_config --check --diff",
        "     expected:  every node up to date",
        "       ansible-playbook community.cassandra.topology --check",
        "     expected:  nothing to do"]
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
    assert paragraph(lines, "IMPORT") == ["IMPORT my_cluster  4 nodes (3 read)  5 entries to act on", "  2 TODO",
                                          "  1 hand-edit entry", "  1 file left as is",
                                          "  1 setting that differs between nodes"]
    order = [heading(lines, title) for title in ("TODO", "HAND EDITS", "DIFFERENT ON SOME NODES",
                                                 "LEFT AS THE NODES HAVE THEM", "WHAT TO DO NEXT", "WHAT THIS REPORT IS",
                                                 "FOR INFO", "DETAILS")]
    assert order == sorted(order)
    # each TODO a numbered step: what, why, the command to copy, what to check after
    todo = section(lines, "TODO")
    flat = [" ".join(x.split()) for x in todo]
    assert flat[0] == "1. Let the import read node4 (1 node), then import again"
    joined = " ".join(flat)
    # what importing without it means, the exact command, what to check
    assert ("the inventory has no settings of it, and a playbook run on it would give it the cluster's settings, not"
            " its own") in joined
    assert "run: ansible-playbook community.cassandra.import_cluster -e import_force=true" in joined
    assert "check: the head of the new report says every node was read" in joined
    # paths from the current dir, no cd; the vault password said; a head -1 to check
    assert "run: ansible-vault encrypt inventories/group_vars/my_cluster/secrets.yml" in joined
    assert "check: head -1 inventories/group_vars/my_cluster/secrets.yml shows $ANSIBLE_VAULT;1.1;AES256" in joined
    assert "ansible-vault asks for a new password (keep it safe): every playbook run" in joined and "cd inventories" not in joined
    # a short "what to do next" at the end, the TODO first
    assert section(lines, "WHAT TO DO NEXT")[0].startswith("  1. Do the TODO steps above, in their order")
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
        "      set in group_vars/my_cluster/main.yml, host_vars/<node>/main.yml.",
        "      to give every node 8G: already in group_vars/my_cluster/main.yml: remove cassandra_max_heap_size from"
        " host_vars of 2 nodes (node4, node5)",
        "      then 2 nodes change, restart unknown (apply_config does not write it); no check (apply_config does not show"
        " it)"]
    # (the most common heap for the cluster, the others per host)
    assert paragraph(lines, "  written:")[1:] == ["    inventories/my_cluster.yml", "    group_vars/my_cluster*/",
                                                  "    host_vars/<node>/ (node4, node5)"]
    lines = report([node(1, keep={"cassandra_firewall_manage": False}), node(2)])
    # (a part Ansible does not manage on a node: not all verified)
    assert sync(lines) == ["  in sync with Ansible: not all verified", "    firewall not managed by Ansible on 1 node"]
    assert paragraph(lines, "  written:")[-1] == "    host_vars/node1/"
    seven = [node(i) for i in range(1, 5)] + [node(i, "16G") for i in range(5, 8)]
    assert paragraph(report(seven, screen=True), "  written:")[-1] == "    host_vars/<node>/ (node5..node7)"  # the screen: a range


def test_where_each_value_is_kept_and_the_dcs_name_the_nodes():
    nodes = [dict(node(1, "8G"), dc="dc1"), dict(node(2, "8G"), dc="dc1"), dict(node(3, "16G"), dc="dc2"),
             dict(node(4, "16G"), dc="dc2"), dict(node(5, "4G"), dc="dc3"), dict(node(6, "4G"), dc="dc3"),
             dict(node(7, "4G"), dc="dc3")]
    review = section(report(nodes), "DIFFERENT ON SOME NODES")
    assert review[:4] == [
        "    cassandra_max_heap_size: 4G    dc3 (3 nodes)   (the memory Java gives Cassandra)",
        "                             16G   dc2 (2 nodes)",
        "                             8G    dc1 (2 nodes)",
        "      set in group_vars/my_cluster/main.yml, host_vars/<node>/main.yml."]  # (no setting per DC or rack)


def test_things_to_do_and_hand_edits():
    nodes = [node(1, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(2, hand=["cassandra.yaml, line 10:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]),
             node(3, hand=["jvm-server.options, line 3:", "  + -Dcassandra.weird=1"], normalized=["x"]),
             node(4, read=False)]
    lines = report(nodes, ok=False, secrets_clear=["group_vars/my_cluster/secrets.yml"],
                   leftovers={"unsure": ["host_vars/old/main.yml"]},
                   self_check={"node1": {"differences": ["cassandra.yaml: concurrent_writes"], "notes": []}})
    # (a self-check not passed: a step to do, said calmly, not in red)
    assert lines[:3] == ["IMPORT my_cluster  4 nodes (3 read)  6 entries to act on", "  4 TODO", "  2 hand-edit entries"]
    # (a difference found: not in sync, whatever else is not verified)
    assert sync(lines) == ["  in sync with Ansible: no", "    apply_config would change 1 setting on 1 node"]
    assert paragraph(lines, "  self-check") == ["  self-check not passed yet: TODO step 2"]
    titles = [line for line in lines if re.match(r"^  \d\. ", line)]
    assert titles[:4] == ["  1. Let the import read node4 (1 node), then import again", "  2. Make the self-check pass",
                          "  3. Encrypt the passwords file",
                          "  4. Is host_vars/old/main.yml an old node of this cluster?"]
    at = lines.index("  4. Is host_vars/old/main.yml an old node of this cluster?")
    assert "     run:    git rm inventories/host_vars/old/main.yml" in lines[at:at + 5]
    at = lines.index("  2. Make the self-check pass")
    assert lines[at + 1:at + 3] == [
        "     the inventory does not reproduce 1 node yet; what differs and why: SELF-CHECK, right below",
        "     run:    choose one of the ways out it gives (To pass)"]
    # the self-check right under the TODO steps, as the stop says it
    at = lines.index(u"  SELF-CHECK \u2014 the inventory does not reproduce 1 node yet")
    assert lines[at + 1:at + 6] == ["    1 setting on this node has no inventory variable: apply_config would change it.",
                                    "", "    cassandra.yaml — 1 node    node1", "      concurrent_writes", ""]
    assert at < heading(lines, "HAND EDITS")
    # HAND EDITS: each file and its nodes once, what the self-check shows named, the others' values; the ways out once
    review = section(lines, "HAND EDITS")
    at = review.index("    cassandra.yaml (Cassandra's main settings file)    node1, node2")
    assert review[at:at + 5] == [  # (a blank line between entries)
        "    cassandra.yaml (Cassandra's main settings file)    node1, node2",
        "      1 setting no variable carries: SELF-CHECK, under TODO", "",
        "    jvm-server.options (Java's options)    node3", "      -Dcassandra.weird: 1"]
    assert "    to keep them as the nodes have them  -e import_keep_hand_edits=cassandra.yaml,jvm-server.options" in review
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
    assert lines[:2] == ["IMPORT my_cluster  3 nodes  nothing to act on", "  --check: inventory not written"]
    assert paragraph(lines, "  would write:")[1:] == ["    inventories/my_cluster.yml", "    group_vars/my_cluster*/"]
    assert " " in lines[:12] and "" not in lines[:12]  # (the head's blank lines: " ", blank on the screen)
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
    at = lines.index(u"  SELF-CHECK \u2014 the inventory does not reproduce 2 nodes yet")
    assert [line for line in lines[at:] if re.match(r"^    \S.*[.:]$", line) and "To pass" not in line][:2] == [
        "    1 setting on this node has no inventory variable: apply_config would change it.",
        "    1 other difference that the import does not explain:"]
    assert not [line for line in lines if "git commit" in line]  # not a valid inventory
    at = lines.index("    jmxremote.password    node2")
    assert lines[at + 1].split()[:3] == ["its", "users", "NOT"]


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
    assert lines[:3] == ["IMPORT my_cluster  5 nodes  2 entries to review", "  2 things worth a look", ""]
    # (not again as settings that differ)
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
    assert screen[:12] == [
        "IMPORT my_cluster  3 nodes  nothing to act on", "  --check: inventory not written", " ",
        "  in sync with Ansible: yes", " ",
        "  self-check passed",
        "    every node read: its settings are in the inventory", " ",
        "  unchanged:", "    inventories/my_cluster.yml", "    group_vars/my_cluster*/", " "]  # (" ": blank on the screen)
    assert screen[-1] == "report: reports/my_cluster/report.txt"
    assert not [line for line in screen if "Write the inventory" in line or "git commit" in line]
    # a thing to do still says so
    screen = report([node(1), node(2), node(3, read=False)], check=True, screen=True, changes=unchanged)
    assert section(screen, "TODO")[0] == "  1. Let the import read node3 (1 node), then import again"


def test_check_says_what_it_would_write():
    # a first import: every file new
    first = {"changed": ["my_cluster.yml", "group_vars/my_cluster/main.yml", "group_vars/my_cluster/secrets.yml"],
             "removed": [], "existed": []}
    screen = report([node(i) for i in range(1, 4)], check=True, screen=True, changes=first)
    assert paragraph(screen, "  would write:") == ["  would write:", "    inventories/my_cluster.yml",
                                                   "    group_vars/my_cluster*/"]
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
    hand = section(lines, "HAND EDITS")
    at = hand.index("    logback.xml (the logging settings)    all - node5")
    assert hand[at - 3:at + 7] == [
        "    cassandra.yaml (Cassandra's main settings file)    node5", "      concurrent_writes: 48", "",
        "    logback.xml (the logging settings)    all - node5",
        "      3 edits   node1, node2, node4 (3 nodes)", "      1 edit   node3 (1 node)", "",
        "    jvm11-server.options (Java's options, Java 11 only)    node4", "      1 edit", ""]
    assert lines[:2] == ["IMPORT my_cluster  5 nodes  3 entries to review", "  3 hand-edit entries"]  # (no TODO)
    # the ways out once: keep the files as the nodes have them, carry the edits, drop them
    assert "    to keep them as the nodes have them  -e import_keep_hand_edits=cassandra.yaml,logback.xml," \
           "jvm11-server.options" in hand
    assert "    their lines, node by node: reports/my_cluster/report.details.txt, DETAILS" in hand
    # two sets of as many edits: told apart, numbered among them
    nodes = [node(1, hand=same), node(2, hand=other), node(3, hand=[line.replace("<c/>", "<d/>") for line in same])]
    found = [re.search(r"\d+ edits?( \(variant \d\))?", line).group(0) for line in section(report(nodes), "HAND EDITS")
             if re.match(r"^      \d+ edit", line)]
    assert found == ["3 edits (variant 1)", "1 edit", "3 edits (variant 2)"]


def test_an_edit_with_each_node_s_own_address_is_one_entry():
    # (2026-11-13) 24 nodes, each its own JVM_OPTS line with its address: one entry, not one "variant" per node
    def rmi(i):
        return ["cassandra-env.sh, line 300:", "  + # mine", '  + JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.%d"' % i]
    nodes = [node(i, hand=rmi(i)) for i in (1, 2, 10)]
    hand = section(report(nodes), "HAND EDITS")
    at = hand.index("    cassandra-env.sh (the script that sets Java's options when Cassandra starts)    all")
    assert hand[at + 1] == "      1 edit"
    checks = dict(("node%d" % i, {"differences": [
        'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.%d", import would write'
        ' nothing' % i]}) for i in (1, 2, 10))
    items = _self_check_items(nodes, checks)
    assert [(i["where"], i["node"], i["ansible"], len(i["lines"])) for i in items] == [
        ("-Djava.rmi.server.hostname", "<its address>", "absent", 3)]
    # a setting's values too: one value for the three nodes
    checks = dict(("node%d" % i, {"differences": [
        "cassandra.yaml: listen_address: node has '10.0.0.%d', import would write '10.0.0.99'" % i]}) for i in (1, 2, 10))
    assert [(i["nodes"], i["node"], i["ansible"]) for i in _self_check_items(nodes, checks)] == [
        (["node1", "node10", "node2"], "<its address>", "10.0.0.99")]


def test_a_logback_hand_edit_says_the_templates_of_yours():
    # no variable carries it (the audit file at another path): its way out, beside keeping the file as it is
    hand = ["logback.xml, line 90:", "  -     <file>${cassandra.logdir}/audit/audit.log</file>",
            "  +     <file>/data/audit/audit.log</file>"]
    text = " ".join(" ".join(section(report([node(1, hand=hand)]), "HAND EDITS")).split())
    assert "to keep them as this node has them -e import_keep_hand_edits=logback.xml to carry them in the inventory" \
           " instead: a template piece for the end of logback.xml or for appenders before <root>" \
           " (cassandra_config_template_snippets), or your own template of the file" \
           " (cassandra_config_template_overrides); then import again" in text
    # a setting's row (a variable carries it, or a file of another kind): not said
    hand = ["cassandra.yaml, line 10:", "  + concurrent_writes: 48"]
    text = " ".join(" ".join(section(report([node(1, hand=hand)]), "HAND EDITS")).split())
    assert "cassandra_config_template" not in text and "cassandra_extra_settings for a cassandra.yaml setting" in text


def test_written_host_vars_counted_on_a_large_cluster():
    nodes = [node(i, "16G" if i % 4 == 1 else "8G") for i in range(1, 60, 2)]  # node1, node3...: no range
    lines = report(nodes, screen=True)
    assert paragraph(lines, "  written:")[-1] == "    host_vars/<node>/ of 15 nodes"  # (a tie: node1's 16G for the cluster, the 8G ones apart)
    again = {"changed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)] + ["group_vars/my_cluster/main.yml"],
             "removed": [], "existed": ["host_vars/node%d/main.yml" % i for i in range(1, 30)]
             + ["group_vars/my_cluster/main.yml"]}
    lines = report(nodes, screen=True, check=True, changes=again, width=300)
    assert [line for line in lines if "(changed: host_vars/<node>/main.yml of 29 nodes, group_vars/my_cluster/main.yml)"
            in line]


def test_the_screen_fits_the_terminal():
    nodes = [node(i, hand=["cassandra.yaml, line 10:", "  + setting_%d: 1" % i]) for i in range(1, 20)]
    for line in report(nodes, screen=True, width=80)[1:]:
        assert len(line) <= 80 or "-e import_keep_hand_edits=" in line, line


def test_a_secret_setting_of_a_hand_edit_is_never_shown():
    # (masked where the edits are made; this one as a line might come)
    lines = report([node(1, hand=["cassandra.yaml, line 10:", "  + ldap_password: s3cret"])])
    assert "s3cret" not in "\n".join(lines)
    at = lines.index("    cassandra.yaml (Cassandra's main settings file)    all")
    assert lines[at + 1] == "      ldap_password: ****"


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
    # a file with hand edits kept (import_keep_hand_edits): an entry of its own, its nodes
    hand = ["jmxremote.access: auditRole=readonly", "  + auditRole=readonly"]
    lines = report([node(1, hand=hand, keep={"cassandra_config_keep_files": ["jmxremote.access"]}), node(2)])
    review = section(lines, "LEFT AS THE NODES HAVE THEM")
    assert review[:2] == [
        "    jmxremote.access   node1 (1 node)",
        "      1 edit that no variable carries (each under DETAILS): Ansible leaves the file as each node has it; nothing"
        " to do while they are wanted"]
    assert "  1 file left as is" in paragraph(lines, "IMPORT")


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
    todo = section(lines, "TODO")
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


def test_self_check_stop_says_a_hand_edited_file_its_java_does_not_read():
    # jvm11-server.options edited by hand on 5.0 nodes on Java 17: never read, said so with the way out
    jvm11 = ["jvm11-server.options, line 40:", "  + -XX:+UseFoo"]
    checks = {"node1": {"differences": ["jvm11-server.options: UseFoo: node has '-XX:+UseFoo', import would write"
                                        " nothing"]}}
    java17 = {"versions": {"java": "17"}}
    lines = stop([node(1, hand=jvm11, **java17), node(2, **java17)], checks, width=400)
    assert lines[1] == ("  1 setting in jvm11-server.options, which Java 17 on this node does not read: no effect"
                        " while it runs that Java, but apply_config would change it.")
    assert [line for line in lines if re.match(r"^    keep jvm11-server.options as this node has it +-e"
                                               r" import_keep_hand_edits=jvm11-server.options$", line)]
    assert [line for line in lines if line.startswith("    or undo the edits on this node, then import again")]
    assert "no inventory variable" not in "\n".join(lines)  # (no second section for that file)
    # the file Java 17 reads, or a Java not known: no such section
    jvm17 = [line.replace("jvm11", "jvm17") for line in jvm11]
    checks17 = {"node1": {"differences": [checks["node1"]["differences"][0].replace("jvm11", "jvm17")]}}
    assert stop([node(1, hand=jvm17, **java17), node(2)], checks17, width=400)[1].startswith(
        "  1 setting on this node has no inventory variable")
    assert "does not read" not in "\n".join(stop([node(1, hand=jvm11), node(2)], checks, width=400))


def test_self_check_stop_golden():
    # the verdict and the first cause; each kind of difference with what apply_config would do, its files and nodes,
    # each setting and its two values, its ways out; the nodes not read; what was written; where the evidence is
    assert stop(failed_nodes(), CHECKS, WINS) == [
        u"SELF-CHECK FAILED — the inventory does not reproduce 4 nodes yet",
        "  some nodes in more than one problem below",
        "",
        "  2 settings on these nodes have no inventory variable: apply_config would change them.",
        "",
        "  cassandra-env.sh — 1 node    node1",
        "    KEYSTORE_PASSWORD",
        "      Node     ****",
        "      Ansible  absent",
        "",
        "  jvm17-server.options — 2 nodes    node2, node5",
        "    -XX:+UseFoo",
        "      Node     present",
        "      Ansible  absent",
        "",
        "  To pass:",
        "    keep cassandra-env.sh, jvm17-server.options as these nodes have them",
        "      -e import_keep_hand_edits=cassandra-env.sh,jvm17-server.options",
        "    take the change (apply_config writes it; its plan says what restarts)",
        "      -e import_strict=false",
        "",
        "  1 setting: a file of yours gives another value: apply_config would write it.",
        "",
        "  cassandra.yaml — 1 node    node3",
        "    concurrent_reads",
        "      Node     64",
        "      Ansible  16   (cassandra_concurrent_reads = 16)",
        "",
        "  To pass:",
        "    in host_vars/node3/local.yml: cassandra_concurrent_reads: 64, or remove that line, then import",
        "      again",
        "    take the change (apply_config writes it; its plan says what restarts)",
        "      -e import_strict=false",
        "",
        "  4 other differences that the import does not explain:",
        "",
        "  cassandra.yaml — 1 node    node1",
        "    server_encryption_options.keystore_password",
        "      Node     ****",
        "      Ansible  ****",
        "",
        "  jvm17-server.options — 2 nodes    node1, node2",
        "    the file",
        "      Node     absent",
        "      Ansible  present",
        "",
        "  owner, group and mode — 1 node    node2",
        "    not read on the node, the roles may change them",
        "",
        "  heap (cassandra-env.sh, else the unit's Environment) — 1 node    node3",
        "    MAX_HEAP_SIZE",
        "      Node     8G",
        "      Ansible  16G",
        "",
        "  To pass:",
        "    change these nodes as the inventory has them, then import again",
        "    take the change (apply_config writes it; its plan says what restarts)  -e import_strict=false",
        "      then review it before applying:",
        "        ansible-playbook community.cassandra.apply_config -e cassandra_hosts=my_cluster --check \\",
        "            --diff",
        "",
        "  1 node not read, so not checked: node4 (unreachable).",
        "",
        "  To pass:",
        "    start Cassandra or fix the access there, then import again",
        "    accept it unchecked                                         -e import_allow_unread=true",
        "",
        "  The inventory: written for inspection only, marked NOT VALID",
        "  Evidence (full paths, lines): reports/my_cluster/report.details.txt, SELF-CHECK"]


def test_self_check_stop_never_shows_a_secret():
    for lines in (stop(failed_nodes(), CHECKS, WINS), stop(failed_nodes(), CHECKS, WINS, width=60),
                  report(failed_nodes(), ok=False, self_check=CHECKS),
                  report(failed_nodes(), ok=False, self_check=CHECKS, details=True)):
        text = "\n".join(lines)
        assert "hunter2" not in text and "s3cret" not in text and "cassandra'" not in text
    # a secret your file sets: its value not shown in the way out either
    wins = {"theirs_win": [{"node": "node3", "key": "cassandra_jmx_password", "path": "host_vars/node3/local.yml"}],
            "yours": [{"node": "node3", "key": "cassandra_jmx_password", "value": "s3cret", "yours": "x",
                       "path": "host_vars/node3/local.yml"}]}
    checks = {"node3": {"differences": ["jmxremote.password: jmx_password: node has ****, import would write ****"]}}
    lines = stop([node(3)], checks, wins)
    flat = " ".join(" ".join(lines).split())
    assert "in host_vars/node3/local.yml: cassandra_jmx_password: ****, or remove that line, then import again" in flat
    assert "      Ansible  ****   (cassandra_jmx_password = ****)" in lines
    assert "s3cret" not in "\n".join(lines)


def test_self_check_stop_lines_fit_the_width():
    # wrapped at the terminal's width, an option, a path or a value never cut
    for width in (60, 79, 100, 160):
        lines = stop(failed_nodes(), CHECKS, WINS, width=width)
        long = [line for line in lines if len(line) > width]
        uncut = r"keystore_password|host_vars/node3/local.yml|import_keep_hand_edits=|report.details.txt|heap \("
        assert long == [line for line in long if re.search(uncut, line)]
        text = "\n".join(lines)
        for whole in ("-e import_keep_hand_edits=cassandra-env.sh,jvm17-server.options", "-e import_allow_unread=true",
                      "-e import_strict=false"):
            assert whole in text
        assert u"\xa0" not in text


def test_self_check_stop_hand_edits_only():
    nodes = [node(1, hand=LOGBACK), node(2, hand=LOGBACK), node(3)]
    checks = dict((n, {"differences": ['logback.xml: /configuration/logger[@name="x"]: node has <logger level="DEBUG"'
                                       ' name="x">, import would write nothing']}) for n in ("node1", "node2"))
    lines = stop(nodes, checks, check=True)
    assert lines[:8] == [u"SELF-CHECK FAILED — the inventory does not reproduce 2 nodes yet",
                         "  1 setting on these nodes has no inventory variable: apply_config would change it.", "",
                         "  logback.xml — 2 nodes    all - node3", "    x", "      Node     present (level DEBUG)",
                         "      Ansible  absent", ""]
    assert "  --check: the inventory not written" in lines
    # (each way out too long for its option beside it: the option on the line under it)
    at = lines.index("  To pass:")
    assert lines[at + 1:at + 5] == ["    keep logback.xml as these nodes have it", "      -e import_keep_hand_edits=logback.xml",
                                    "    take the change (apply_config writes it; its plan says what restarts)",
                                    "      -e import_strict=false"]


def test_self_check_stop_kept_files_are_not_hand_edits():
    # import_keep_hand_edits already: a difference left in a kept file (its owner, group and mode) is another kind,
    # keep_hand_edits not offered again for it
    kept = {"cassandra_config_keep_files": ["logback.xml"]}
    nodes = [node(1, hand=LOGBACK, keep=kept), node(2)]
    checks = {"node1": {"differences": ["logback.xml: owner:group mode: node has root:root 0600, import would write"
                                        " root:cassandra 0644"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_writes: node has 32, import would write 64"]}}
    lines = stop(nodes, checks)
    assert lines[1:15] == ["  1 + 1 nodes, counted by problem below", "",
                           "  3 other differences that the import does not explain:", "",
                           "  logback.xml — 1 node    node1", "    group", "      Node     root", "      Ansible  cassandra",
                           "    mode", "      Node     0600", "      Ansible  0644", "",
                           "  cassandra.yaml — 1 node    node2", "    concurrent_writes"]
    assert "keep logback.xml" not in "\n".join(lines)


def test_self_check_stop_values_per_node():
    # the nodes with the same values together, a group each
    checks = {"node1": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node2": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '32'"]},
              "node3": {"differences": ["cassandra.yaml: concurrent_reads: node has '128', import would write '32'"]},
              "node4": {"differences": ["cassandra.yaml: concurrent_writes: node has '8', import would write '16'"]},
              "node5": {"differences": ["cassandra.yaml: concurrent_writes: node has '9', import would write '32'"]}}
    lines = stop([node(i) for i in range(1, 6)], checks)
    assert lines[1:4] == ["  2 + 1 + 1 + 1 nodes, counted by problem below", "",
                          "  2 other differences that the import does not explain:"]
    groups = [line for line in lines if line.startswith("  cassandra.yaml")]
    assert groups == ["  cassandra.yaml — 2 nodes    node1, node2", "  cassandra.yaml — 1 node    node3",
                      "  cassandra.yaml — 1 node    node4", "  cassandra.yaml — 1 node    node5"]
    at = lines.index("  cassandra.yaml — 1 node    node3")
    assert lines[at:at + 4] == ["  cassandra.yaml — 1 node    node3", "    concurrent_reads", "      Node     128",
                                "      Ansible  32"]


def test_self_check_stop_could_not_run_or_not_read():
    nodes = [node(1), node(2, read=False)]
    error = 'the self-check could not run ("Render" failed): nothing says the variables reproduce the nodes'
    lines = stop(nodes, {}, error=error)
    assert lines[:2] == [u"SELF-CHECK FAILED — it could not run",
                         '  The self-check could not run ("Render" failed): nothing says the variables reproduce the'
                         ' nodes']
    assert "    see the error (it may show passwords), fix it, then import again  -e import_debug=true" in lines
    assert "  1 node not read, so not checked: node2 (unreachable)." in lines
    # no reason recorded: none guessed
    unread = node(2, read=False)
    unread.pop("reason")
    assert "  1 node not read, so not checked: node2 (reason not recorded)." in stop([node(1), unread], {}, error=error)
    assert "import_strict" not in "\n".join(lines) and "Evidence" not in "\n".join(lines)
    # the nodes not read accepted: not said
    lines = stop(nodes, {}, allow_unread=True, error=error)
    assert "allow_unread" not in "\n".join(lines)
    assert stop(nodes, {})[0] == u"SELF-CHECK FAILED — 1 node not read, so not checked"
    # no node read: never valid, allow_unread or not, so not offered
    none = [node(1, read=False), node(2, read=False)]
    for allow in (False, True):
        lines = stop(none, {}, allow_unread=allow)
        assert lines[0] == u"SELF-CHECK FAILED — no node read"
        assert "    start Cassandra or fix the access there, then import again" in lines
        assert "allow_unread" not in "\n".join(lines)
    # nothing known (no difference, every node read, no error): a way out all the same
    lines = stop([node(1)], {})
    assert lines[lines.index("  To pass:") + 1].startswith("    import again to see what failed (it may show")


def test_self_check_report_section():
    # report.txt (report.check.txt): the stop's lines under TODO, the node lists whole; each difference as the
    # self-check found it in report.details.txt
    lines = report(failed_nodes(), ok=False, self_check=CHECKS, layout_extra=WINS)
    at = lines.index(u"  SELF-CHECK — the inventory does not reproduce 4 nodes yet")
    evidence = lines.index("    Evidence (full paths, lines): reports/my_cluster/report.details.txt, SELF-CHECK", at)
    part = lines[at:lines.index("", evidence - 1)]
    assert part[1:8] == ["    some nodes in more than one problem below", "",
                         "    2 settings on these nodes have no inventory variable: apply_config would change them.",
                         "", "    cassandra-env.sh — 1 node    node1", "      KEYSTORE_PASSWORD", "        Node     ****"]
    assert "    jvm17-server.options — 2 nodes    node2, node5" in part and max(len(line) for line in part) <= 102
    assert "  2. Make the self-check pass" in lines and lines.index("  2. Make the self-check pass") < at
    # the screen: the step, the stop below it (import_strict) says how
    screen = report(failed_nodes(), ok=False, self_check=CHECKS, screen=True, strict=True)
    assert "  2. Make the self-check pass" in screen and not [line for line in screen if "SELF-CHECK —" in line]
    at = screen.index("  2. Make the self-check pass")
    assert screen[at + 1] == ("     the inventory does not reproduce 4 nodes yet; what differs and why: the SELF-CHECK"
                              " FAILED block at the end of the run")
    # passed: no section
    assert not [line for line in report(failed_nodes()[:1]) if "SELF-CHECK" in line]
    details = report(failed_nodes(), ok=False, self_check=CHECKS, details=True)
    at = details.index([line for line in details if line.startswith("==== SELF-CHECK")][0])
    assert details[at + 3:at + 5] == ["  node1 (1 node)", "    cassandra-env.sh: node has export KEYSTORE_PASSWORD=****"]  # (masked to the end)
    at = details.index("  node2, node5 (2 nodes)")
    assert details[at + 1] == "    jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write nothing"


def test_self_check_evidence_same_line_once_for_its_nodes():
    # a large cluster: the line every node has once, by the node groups; a value of its own on its own line
    nodes = [node(i) for i in range(1, 21)]
    line = ("jmxremote.access: controlRole: node has 'create javax.management.monitor.*,javax.management.timer.*"
            " readwrite unregister', import would write nothing")
    checks = dict(("node%d" % i, {"differences": [line]}) for i in range(1, 21))
    checks["node7"] = {"differences": [line.replace("readwrite unregister", "readwrite")]}
    checks["node1"] = {"differences": [line, line]}  # (twice: named once)
    details = report(nodes, ok=False, self_check=checks, details=True)
    at = details.index([x for x in details if x.startswith("==== SELF-CHECK")][0])
    part = details[at:details.index([x for x in details if x.startswith("==== WHERE") or x.startswith("==== FOR")][0])]
    assert " ".join(part).count("node has") == 2
    assert "  all - node7 (19 nodes)" in part and "  node7 (1 node)" in part
    lines = stop(nodes, checks)
    assert "  jmxremote.access — 19 nodes    all - node7" in lines and "  jmxremote.access — 1 node    node7" in lines


def test_self_check_stop_owner_of_a_hand_edited_file_is_not_a_hand_edit():
    # keep_hand_edits keeps the content of a file, not its owner, group and mode
    nodes = [node(1, hand=LOGBACK)]
    checks = {"node1": {"differences": ["logback.xml: owner:group mode: node has root:root 0600, import would write"
                                        " root:cassandra 0644",
                                        "logback.xml: /configuration/jmxConfigurator: node has <jmxConfigurator>,"
                                        " import would write nothing"]}}
    lines = stop(nodes, checks)
    assert [line for line in lines if line.startswith("  ") and line[2] != " " and line.rstrip()[-1] in ".:"
            and "To pass" not in line] == [
        "  1 setting on this node has no inventory variable: apply_config would change it.",
        "  2 other differences that the import does not explain:"]
    at = lines.index("  2 other differences that the import does not explain:")
    assert lines[at + 2:at + 5] == ["  logback.xml — 1 node    all", "    group", "      Node     root"]


def test_self_check_stop_your_file_by_its_exact_variable():
    # a file of yours wins for cassandra_dc and cassandra_tokens: not cdc_enabled nor num_tokens; its way out only
    # for a difference it makes
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
    at = lines.index("  1 setting: a file of yours gives another value: apply_config would write it.")
    assert lines[at + 2:at + 6] == ["  cassandra.service — 1 node    all", "    [Service] LimitNOFILE", "      Node     100000",
                                    "      Ansible  10   (cassandra_service_limit_nofile = 10)"]
    assert [line for line in lines if line.startswith("    in ")] == [
        "    in host_vars/node1/z.yml: cassandra_service_limit_nofile: 100000, or remove that line, then import again"]
    at = lines.index("  2 other differences that the import does not explain:")
    assert lines[at + 2:at + 9] == ["  cassandra.yaml — 1 node    all", "    cdc_enabled", "      Node     true",
                                    "      Ansible  false", "    num_tokens", "      Node     16", "      Ansible  8"]
    # a node not read alone: no way out about your files
    assert not [line for line in stop([node(1), node(2, read=False)], {}, wins) if "your" in line]


def test_self_check_stop_lines_of_odd_values():
    # a line with ": node has " in it stays a line; a sentence is never cut
    checks = {"node1": {"differences": ['cassandra-env.sh: node has JVM_OPTS="-Dx=a: node has b", import would write'
                                        ' nothing',
                                        "cassandra.yaml: cannot be read as its program reads it (node's or the roles'"
                                        " version)"]}}
    lines = stop([node(1)], checks, width=160)
    at = lines.index("  cassandra-env.sh — 1 node    all")
    assert lines[at + 1:at + 4] == ["    JVM_OPTS", "      Node     -Dx=a: node has b", "      Ansible  absent"]
    at = lines.index("  cassandra.yaml — 1 node    all")
    assert lines[at + 1] == "    cannot be read as its program reads it (node's or the roles' version)"


def test_self_check_stop_large_cluster_node_lists():
    # never a group label of the report's own (G1): the DCs, else the names
    nodes = [dict(node(i), dc="dc1" if i <= 15 else "dc2") for i in range(1, 31)]
    checks = dict(("node%d" % i, {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would"
                                                  " write '32'"]}) for i in range(1, 15))
    lines = stop(nodes, checks)
    assert "  cassandra.yaml — 14 nodes    dc1 - node15" in lines
    assert "      Node     64" in lines and not [line for line in lines if re.search(r"\bG\d", line)]


def test_self_check_stop_odd_values():
    # an empty value said so; a value of yours not known: not "None"
    checks = {"node1": {"differences": ["cassandra.yaml: cdc_raw_directory: node has '', import would write '/x'"]}}
    lines = stop([node(1)], checks)
    assert lines[lines.index("    cdc_raw_directory") + 1:][:2] == ["      Node     (empty)", "      Ansible  /x"]
    wins = {"theirs_win": [{"node": "node1", "key": "cassandra_seeds", "path": "host_vars/node1/z.yml"}]}
    checks = {"node1": {"differences": ["cassandra.yaml: seeds: node has 'a', import would write 'b'"]}}
    lines = stop([node(1)], checks, wins)
    text = "\n".join(lines)
    assert "      Ansible  b   (cassandra_seeds, in host_vars/node1/z.yml)" in lines
    assert "in host_vars/node1/z.yml: cassandra_seeds: the node's value, or remove that line, then import again" in \
        " ".join(" ".join(lines).split())
    assert "None" not in text


def test_self_check_stop_a_value_none_is_not_unset():
    # Cassandra's own value none (internode_encryption: none) is a value; a secret JVM option keeps its name
    checks = {"node1": {"differences": [
        "cassandra.yaml: server_encryption_options.internode_encryption: node has 'none', import would write 'all'",
        "cassandra.yaml: internode_compression: node has 'dc', import would write 'none'",
        "jvm17-server.options: -Djavax.net.ssl.keyStorePassword: node has ****, import would write ****"]}}
    lines = stop([node(1)], checks, width=160)
    at = lines.index("    server_encryption_options.internode_encryption")
    assert lines[at + 1:at + 6] == ["      Node     none", "      Ansible  all", "    internode_compression",
                                    "      Node     dc", "      Ansible  none"]
    at = lines.index("    -Djavax.net.ssl.keyStorePassword")
    assert lines[at + 1:at + 3] == ["      Node     ****", "      Ansible  ****"]


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
    # a file of yours giving the same cassandra_version and cassandra_service_restart as the nodes: they
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


def test_os_tuning_and_what_this_report_is_wrap_never_cut():
    # (owner's report) two disk groups cut to the same text could not be told apart; the first paragraph was cut
    disks = ["disks: /data/a, /data/b -> dm-3 (sdb, sdc), read_ahead_kb 4096 (8), scheduler none (none), SSD, %s" % x
             for x in ("first group", "second group")]
    nodes = [node(1, os={"lines": [disks[0]]}), node(2, os={"lines": [disks[1]]})]
    lines = report(nodes, width=100)
    at = lines.index("  OS tuning: the live value of each setting (and the collection's)")
    block = lines[at + 1:lines.index("", at)]
    text = " ".join(line.strip() for line in block)
    assert "first group" in text and "second group" in text and "..." not in text
    assert max(len(line) for line in block) <= 100
    head = next(i for i, line in enumerate(lines) if line.startswith("==== WHAT THIS REPORT IS ="))
    about = lines[head + 1:head + 6]
    assert "nothing on the nodes was changed." in " ".join(line.strip() for line in about)
    assert not [line for line in about if line.endswith("...")]


THP = "cassandra_linux: transparent_hugepage enabled (live): node has madvise, the roles would set never"
READ_AHEAD = "cassandra_linux: read_ahead_kb sdb (live): node has 128, the roles would set 4"


def test_self_check_stop_os_golden():
    # what cassandra_linux would change on nodes the roles set up (apply_config runs it): its own kind, the switch
    # that keeps it as the nodes have it
    assert stop([node(1), node(2)], {"node1": {"differences": [THP]}, "node2": {"differences": [THP]}}) == [
        u"SELF-CHECK FAILED \u2014 the inventory does not reproduce 2 nodes yet",
        "  1 setting outside the config files: the roles would change it.",
        "",
        "  OS (cassandra_linux) — 2 nodes    all",
        "    transparent_hugepage enabled (live)",
        "      Node     madvise",
        "      Ansible  never",
        "",
        "  To pass:",
        "    keep transparent huge pages as on these nodes  -e import_keep_hand_edits=thp",
        "    take the change (the roles make it)            -e import_strict=false",
        "      then review it before applying:",
        "        ansible-playbook community.cassandra.apply_config -e cassandra_hosts=my_cluster --check \\",
        "            --diff",
        "",
        "  The inventory: written for inspection only, marked NOT VALID",
        "  Evidence (full paths, lines): reports/my_cluster/report.details.txt, SELF-CHECK"]


def test_self_check_stop_os_finest_switch_on_its_nodes():
    lines = stop([node(1), node(2)], {"node1": {"differences": [READ_AHEAD]}}, width=400)
    assert [line for line in lines if re.match(r"^    keep the tuning of the data devices as on this node +-e"
                                               r" import_keep_hand_edits=disks$", line)]
    # time sync alone: its own switch; THP and the disks: both
    sync = "cassandra_linux: time sync chronyd: node has inactive, the roles would set started"
    assert "-e import_keep_hand_edits=timesync" in "\n".join(stop([node(1)], {"node1": {"differences": [sync]}}, width=400))
    both = "\n".join(stop([node(1)], {"node1": {"differences": [THP, READ_AHEAD]}}, width=400))
    assert "-e import_keep_hand_edits=thp,disks" in both
    # with differences of the inventory too: one verdict, a kind each
    mixed = {"node1": {"differences": [THP, "cassandra.yaml: concurrent_reads: node has '64', import would write '16'"]}}
    lines = stop([node(1)], mixed, width=400)
    assert lines[0] == u"SELF-CHECK FAILED \u2014 the inventory does not reproduce 1 node yet"
    assert "  1 setting outside the config files: the roles would change it." in lines
    assert "  1 other difference that the import does not explain:" in lines


def test_self_check_stop_unit_way_out():
    script = ("cassandra.service: the node starts Cassandra with the init script /etc/rc.d/init.d/cassandra (modified"
              " since its package installed it), the roles would start it with their systemd unit from its next restart")
    lines = stop([node(1), node(2)], {"node1": {"differences": [script]}}, width=400)
    assert lines[3:5] == ["  cassandra.service — 1 node    node1", "    the node starts Cassandra with the init script"
                          " /etc/rc.d/init.d/cassandra (modified since its package installed it), the roles would start"
                          " it with their systemd unit from its next restart"]
    assert [line for line in lines if re.match(r"^    keep the unit \(or init script\) Cassandra is started by as on"
                                               r" this node +-e import_keep_hand_edits=unit$", line)]


def test_files_written_with_no_effect_listed_apart_once_with_their_nodes():
    """The self-check's "no effect" notes: one line per file or value, with the nodes it would be rewritten on."""
    note = "cassandra_linux: 90-nproc.conf: absent -> cassandra nproc 32768, no effect (the node keeps the values it" \
           " has now)"
    checks = {"node1": {"differences": [], "notes": [note, "something else"]},
              "node2": {"differences": [], "notes": [note]}, "node3": {"differences": [], "notes": []}}
    lines = report([node(1), node(2), node(3)], self_check=checks, details=False)
    at = lines.index("APPLY_CONFIG WILL REWRITE (no effect)")
    flat = " ".join(" ".join(line.split()) for line in lines[at + 1:at + 3])
    assert flat.startswith("cassandra_linux: 90-nproc.conf: absent -> cassandra nproc 32768 all - node3 (2 nodes)")
    assert "something else" not in " ".join(lines)
    # the next step does not promise "nothing to apply": apply_config --check shows them
    steps = " ".join(" ".join(line.split()) for line in section(lines, "WHAT TO DO NEXT"))
    assert "expected: only the rewrites under APPLY_CONFIG WILL REWRITE (no effect)" in steps
    assert "expected: every node up to date" not in steps
    # none: no such section, nothing to apply
    lines = report([node(1)], details=False)
    assert "APPLY_CONFIG WILL REWRITE (no effect)" not in lines
    assert "expected: every node up to date" in " ".join(" ".join(line.split()) for line in lines)


def test_next_step_with_files_moved_and_rewrites_names_the_section():
    """JVM arguments moved and files rewritten with no effect: the next step names the section as it is titled."""
    note = "cassandra_linux: 90-nproc.conf: absent -> cassandra nproc 32768, no effect (the node keeps the values it" \
           " has now)"
    lines = report([node(1)], self_check={"node1": {"differences": [], "notes": [note], "writes": PROD_WRITES[:1]}},
                   details=False)
    assert "APPLY_CONFIG WILL REWRITE (no effect)" in lines
    steps = " ".join(" ".join(line.split()) for line in section(lines, "WHAT TO DO NEXT"))
    assert ("expected: WOULD CHANGE: the 1 file the head lists under the next apply_config, and the rewrites under"
            " APPLY_CONFIG WILL REWRITE (no effect); WHAT HAPPENS: no restart ansible-playbook") in steps


def test_parts_kept_said_in_one_sentence():
    """sysctl,firewall: "the kernel settings and the firewall", not joined by a semicolon."""
    sysctl = "cassandra_linux: sysctl vm.swappiness (/etc/sysctl.conf): node has 10, the roles would set removed"
    rule = "cassandra_firewall: firewall rule (not carried): node has iptables: -A INPUT, the roles would set not written"
    flat = " ".join(" ".join(stop([node(1)], {"node1": {"differences": [sysctl, rule]}}, width=400)).split())
    assert "keep the kernel settings and the firewall as on this node -e import_keep_hand_edits=sysctl,firewall" \
        in flat
    kept = [node(i, keep={"cassandra_import_kept": ["sysctl", "swap", "firewall"]}) for i in (1, 2)]
    flat = " ".join(" ".join(report(kept, details=False)).split())
    assert "the roles leave the kernel settings, swap and the firewall as they are" in flat
    assert "; the firewall" not in flat


def test_parts_kept_by_hand_listed_with_the_way_to_give_them_back():
    kept = [node(i, keep={"cassandra_import_kept": ["thp", "unit"]}) for i in (1, 2)]
    lines = report(kept + [node(3)], details=False)
    at = lines.index("KEPT BY HAND (your choice)")
    flat = " ".join(" ".join(line.split()) for line in lines[at + 1:at + 5])
    assert flat.startswith("thp, unit (import_keep_hand_edits=thp,unit) all - node3 (2 nodes)")
    assert "to give them to Ansible: import again without them in import_keep_hand_edits" in flat


def test_one_merged_keep_option_in_every_way_out():
    """The way out names what this import keeps already with the new names: running it keeps them all (no loop)."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import _keep_option
    layout = {"nodes": [{"keep": {"cassandra_config_keep_files": ["logback.xml"], "cassandra_import_kept": ["unit"]}},
                        {"keep": {"cassandra_import_kept": ["thp"]}}, {}]}
    option = _keep_option(layout, "cqlsh", "logback.xml").replace(u" ", " ")
    assert option == "-e import_keep_hand_edits=logback.xml,thp,unit,cqlsh"
    # every hand-edited file: true stands for the files, the parts stay named
    assert _keep_option(layout, "true").replace(u" ", " ") == "-e import_keep_hand_edits=true,thp,unit"
    assert _keep_option({}, "os").replace(u" ", " ") == "-e import_keep_hand_edits=os"


def test_an_entry_of_the_run_worth_a_look():
    # the playbook's own (the inventory of ansible.cfg a -i <node>, run did not read): an entry as the others
    look = [("the inventory of ansible.cfg (/p/inventories) was not read: this run had -i node1,",
             "its variables did not apply to this run")]
    lines = report([node(i) for i in range(1, 4)], details=False, look=look)
    assert section(lines, "WORTH A LOOK") == [
        "    the inventory of ansible.cfg (/p/inventories) was not read: this run had -i node1,",
        "      its variables did not apply to this run"]
    assert lines[:2] == ["IMPORT my_cluster  3 nodes  1 entry to review", "  1 thing worth a look"]
    assert section(report([node(i) for i in range(1, 4)], screen=True, look=look), "WORTH A LOOK")[0] == lines[
        heading(lines, "WORTH A LOOK") + 2]


def test_the_run_time_with_its_zone_heads_every_report(monkeypatch):
    # a report rewritten by a run that found the same says it is new: its time on the head line of
    # report.txt, report.details.txt and the screen, with the date and the zone
    monkeypatch.setenv("TZ", "Europe/Paris")
    import time
    time.tzset()
    try:
        nodes = [node(1), node(2)]
        when = 1794834120  # 2026-11-16 14:02 CET
        lines = report(nodes, run_time=str(when))
        heads = [line for line in lines if line.startswith("IMPORT my_cluster")]
        assert len(heads) == 2 and all("  2026-11-16 14:02 CET  " in h for h in heads), heads
        screen = report(nodes, run_time=when, screen=True)
        assert screen[0].startswith("IMPORT my_cluster  2026-11-16 14:02 CET  2 nodes")
        assert "CET" not in report(nodes)[0]  # (no time given: none said)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_the_build_heads_every_report(monkeypatch):
    # the collection's version and the commit it was built from, on a line of the head of report.txt,
    # report.details.txt and the screen (asked by import_cluster: build=true)
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_build
    monkeypatch.setattr(cassandra_build, "label", lambda: "community.cassandra 3.0.0-rc1, commit 1a2b3c4d5e6f")
    made_by = "  made by:  community.cassandra 3.0.0-rc1, commit 1a2b3c4d5e6f"
    nodes = [node(1), node(2)]
    lines = report(nodes, build=True)
    heads = [i for i, line in enumerate(lines) if line.startswith("IMPORT my_cluster")]
    assert len(heads) == 2 and lines.count(made_by) == 2
    assert lines[heads[1] + 1] == made_by  # (the details: under its head line)
    assert made_by in report(nodes, build=True, screen=True)
    assert made_by not in report(nodes)  # (not asked: not said)


def test_import_cluster_asks_for_the_build():
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "playbooks", "import_cluster.yml")) as f:
        text = f.read()
    assert text.count("cassandra_import_report(") == text.count("build=true") == 3


def test_import_cluster_notes_the_run_time_once():
    import yaml
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "playbooks", "import_cluster.yml")) as f:
        plays = yaml.safe_load(f)
    write = find_play(plays, "Write the inventory")
    assert write["tasks"][0]["ansible.builtin.set_fact"] == {"_import_run_time": "{{ now().timestamp() }}"}
    assert "_import_run_time" in write["vars"]["_report_args"]["run_time"]


def test_what_to_do_next_never_says_nothing_stops_you_on_a_failed_self_check():
    # (self-check pass) a failed self-check: the inventory is not to be used yet
    edits = ["cassandra.yaml, line 12:", "  + concurrent_writes: 48"]
    checks = {"node1": {"differences": ["cassandra.yaml: concurrent_writes: node has '48', import would write '32'"]}}
    failed = report([node(1, hand=edits), node(2)], ok=False, self_check=checks, details=False)
    passed = report([node(1, hand=edits), node(2)], details=False)
    assert "none of it stops you" not in "\n".join(failed)
    assert "none of it stops you" in "\n".join(section(passed, "WHAT TO DO NEXT")[0:1])


SYNC = "  in sync with Ansible: "


def sync(lines):
    """The head's in sync paragraph: its line, the rows under it (79 columns at most each, never a wrapped row:
    each row indented as deep as its kind)."""
    at = next(i for i, line in enumerate(lines) if line.startswith(SYNC))
    said = [lines[at]]
    for line in lines[at + 1:]:
        if not line.startswith("    "):
            break
        said.append(line)
    assert all(len(line) <= 79 for line in said), said
    return said


def writes(*rows):
    """A self-check's writes: (file, what, why...) each."""
    return [{"file": f, "what": what, "why": list(why), "diff": ["--- %s" % f, "+++ %s" % f, "-a", "+b"]
             if what == "rewritten" else []} for f, what, *why in rows]


PROD_WRITES = writes(("cassandra-env.sh", "rewritten", "layout"), ("jvm-server.options", "rewritten", "heap placement"),
                     ("jvm17-server.options", "rewritten", "heap placement"), ("cassandra.yaml", "header"),
                     ("logback.xml", "header"))


def test_in_sync_yes_lists_the_next_apply_config_s_writes_a_row_each():
    # (2026-11-25, owner: a difference with no effect takes the role's file once, announced: which files, why)
    assert sync(report([node(i) for i in range(1, 4)])) == ["  in sync with Ansible: yes"]
    os_note = "cassandra_linux: /etc/systemd/system/disable-thp.service: another content -> the role's" \
        ", no effect (the node keeps the values it has now)"
    checks = {"node%d" % i: {"differences": [], "notes": [os_note], "writes": PROD_WRITES} for i in range(1, 4)}
    checks["node3"] = dict(checks["node3"], writes=PROD_WRITES[1:])
    lines = report([node(i) for i in range(1, 4)], self_check=checks, screen=True, width=200)
    assert sync(lines) == ["  in sync with Ansible: yes",
                           "    the next apply_config, no restart:",
                           "      cassandra-env.sh       rewritten: layout  on node1, node2",
                           "      jvm-server.options     rewritten: heap placement",
                           "      jvm17-server.options   rewritten: heap placement",
                           "      cassandra.yaml         header line added",
                           "      logback.xml            header line added",
                           "      their diffs: reports/my_cluster/report.details.txt,",
                           "        REWRITES OF THE NEXT APPLY_CONFIG",
                           "      rewrites 1 other item with no effect"]
    said = " ".join(" ".join(line.split()) for line in lines)
    assert "expected: WOULD CHANGE: the 5 files the head lists under the next apply_config, and the rewrites under" \
        " APPLY_CONFIG WILL REWRITE (no effect); WHAT HAPPENS: no restart" in said, said
    # the diffs in the details, once per group of nodes with the same one
    details = report([node(i) for i in range(1, 4)], self_check=checks, details=True)
    at = details.index("REWRITES OF THE NEXT APPLY_CONFIG  (their diffs)")
    assert details[at + 1:at + 6] == ["  cassandra-env.sh on node1, node2:", "    --- cassandra-env.sh",
                                      "    +++ cassandra-env.sh", "    -a", "    +b"]
    assert "  jvm-server.options on node1..node3:" in details
    # the OS rewrites alone: not "other"
    checks = {"node%d" % i: {"differences": [], "notes": [os_note]} for i in range(1, 4)}
    assert sync(report([node(i) for i in range(1, 4)], self_check=checks, screen=True))[1:] == [
        "    the next apply_config, no restart:", "      rewrites 1 item with no effect"]


def test_in_sync_not_all_verified_says_what_and_where():
    lines = report([node(1), node(2), node(3, read=False)], ok=False, allow_unread=True)
    assert sync(lines) == ["  in sync with Ansible: not all verified", "    1 node not read (node3)"]
    # parts kept by hand (import_keep_hand_edits), a file left as the node has it: Ansible does not manage them; a
    # row each, never "a, b kept by hand"
    kept = [node(1, keep={KEPT_PARTS_VAR: ["sysctl", "firewall"]}), node(2, keep={KEPT_PARTS_VAR: ["sysctl", "firewall"]}),
            node(3, keep={"cassandra_config_keep_files": ["logback.xml"]})]
    assert sync(report(kept)) == ["  in sync with Ansible: not all verified",
                                  "    sysctl kept by hand on 2 nodes",
                                  "    firewall kept by hand on 2 nodes",
                                  "    logback.xml left as is on 1 node"]


def test_in_sync_no_counts_the_settings_and_the_nodes():
    check = {"node1": {"differences": ["cassandra.yaml: concurrent_reads: 8 -> 16", "x: 1 -> 2"]},
             "node2": {"differences": ["cassandra.yaml: concurrent_reads: 8 -> 16"]}}
    lines = report([node(1), node(2), node(3)], ok=False, self_check=check, screen=True, strict=False)
    assert sync(lines) == ["  in sync with Ansible: no", "    apply_config would change 3 settings on 2 nodes"]


def test_the_head_a_paragraph_per_subject_one_fact_per_row():
    # (owner, rc1) the head's subjects apart, each a short line then its rows; no row wider than 79 columns
    lines = report([node(1), node(2, "16G"), node(3, read=False)], ok=False, allow_unread=True, check=True,
                   screen=True, width=79, build=True)
    head = lines[:lines.index(next(x for x in lines if x.startswith("==== ")))]
    assert all(len(line) <= 79 for line in head), head
    paragraphs = [p.split("\n") for p in "\n".join(head).strip().split("\n \n")]
    assert paragraphs[:4] == [
        ["IMPORT my_cluster  3 nodes (2 read)  2 entries to review", "  1 setting that differs between nodes",
         "  1 node not read", "  --check: inventory not written"],
        ["  in sync with Ansible: not all verified", "    1 node not read (node3)"],
        ["  self-check not passed yet", "    the inventory does not reproduce the nodes"],
        ["  would write:", "    inventories/my_cluster.yml", "    group_vars/my_cluster*/", "    host_vars/node2/"]]
    assert paragraphs[4][0].startswith("  made by:  community.cassandra ") and len(paragraphs) == 5


def test_the_head_says_the_counts_a_row_each():
    lines = report([node(1), node(2, "16G"), node(3, read=False)], ok=False, allow_unread=True, details=False)
    assert lines[0] == "IMPORT my_cluster  3 nodes (2 read)  2 entries to review"
    assert all(re.match(r"  \d+ \w", row) for row in lines[1:3]) and lines[3] == "", lines[:4]


def test_a_dict_key_most_nodes_do_not_set_is_given_as_the_collections_default():
    """cassandra_config_file_permissions.<file>.mode on one node only: to give every node the collection's default,
    never "to give every node not set"."""
    def perms(i, mode):
        return dict(node(i), vars=dict(node(i)["vars"], cassandra_config_file_permissions={
            "cassandra.yaml": {"mode": mode}} if mode else {"cassandra-env.sh": {"mode": "0644"}}))
    full = " ".join(" ".join(report([perms(1, "0640"), perms(2, None), perms(3, None)], details=False)).split())
    assert "to give every node the collection's default: remove" in full
    assert "to give every node not set" not in full


def test_a_dict_one_node_sets_shows_only_the_sub_keys_that_differ_from_the_default():
    """e2e: vm.swappiness set again in h-n1's /etc/sysctl.conf, not carried: h-n1 alone gets
    cassandra_linux_sysctl, the default's other keys as they are. Its 12 keys at the default's value were listed as
    differing, the one dropped was not; a node's own limits the same (only nofile differs)."""
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file
    linux = role_file("cassandra_linux")
    sysctl = dict((k, str(v)) for k, v in linux["cassandra_linux_sysctl"].items() if k != "vm.swappiness")
    limits = dict(linux["cassandra_linux_limits"], nofile=65536)

    def with_vars(i, **more):
        return dict(node(i), vars=dict(node(i)["vars"], **more))
    nodes = [with_vars(1, cassandra_linux_sysctl=sysctl), with_vars(2), with_vars(3, cassandra_linux_limits=limits),
             with_vars(4)]
    review = " ".join(section(report(nodes), "DIFFERENT ON SOME NODES"))
    shown = sorted(set(re.findall(r"(cassandra_linux_\w+\.[\w.]+):", review)))
    assert shown == ["cassandra_linux_limits.nofile", "cassandra_linux_sysctl.vm.swappiness"]
    assert re.search(r"cassandra_linux_sysctl\.vm\.swappiness: +\(collection's default\) .*\(not set\) +node1", review)


def test_an_empty_dict_one_node_sets_is_one_row_not_the_defaults_sub_keys():
    """import writes cassandra_linux_limits: {} and cassandra_linux_sysctl: {} on a node whose files are not the
    role's: one row for that node, not a "(not set)" row per sub-key of the collection's default."""
    def with_vars(i, **more):
        return dict(node(i), vars=dict(node(i)["vars"], **more))
    for key in ("cassandra_linux_limits", "cassandra_linux_sysctl"):
        nodes = [with_vars(1, **{key: {}}), with_vars(2), with_vars(3)]
        review = " ".join(section(report(nodes), "DIFFERENT ON SOME NODES"))
        assert re.search(r"%s: " % key, review), review
        assert not re.findall(r"%s\.[\w.]+:" % key, review), review
    # a node's own dict next to it: its sub-keys compared with the default's, the empty one on its own row only
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file
    limits = dict(role_file("cassandra_linux")["cassandra_linux_limits"], nofile=65536)
    nodes = [with_vars(1, cassandra_linux_limits={}), with_vars(2, cassandra_linux_limits=limits), with_vars(3)]
    lines = section(report(nodes), "DIFFERENT ON SOME NODES")
    review = " ".join(lines)
    assert sorted(set(re.findall(r"(cassandra_linux_limits\.[\w.]+):", review))) == ["cassandra_linux_limits.nofile"]

    def rows(lines, name):
        """A setting's rows: (value, nodes), up to its "set in" line."""
        at = next(i for i, line in enumerate(lines) if line.strip().startswith(name + ":"))
        found = []
        for line in lines[at:]:
            if line.strip().startswith("set in"):
                return found
            text = line.strip()[len(name) + 1:] if line.strip().startswith(name + ":") else line.strip()
            value, members = re.split(r"\s{3,}", text.strip())[:2]
            found.append((value, members.split(" (")[0]))
        return found
    # node2 sets the dict (by sub-key): not "(not set)"; node1's {} on that row only, its sub-keys' rows without it
    assert rows(lines, "cassandra_linux_limits") == [("(collection's default)", "node3"),
                                                     ("(its own keys, below)", "node2"), ("{}", "node1")]
    assert rows(lines, "cassandra_linux_limits.nofile") == [("(collection's default)", "node3"), ("65536", "node2")]
    # the advice: only node2 changes (node1 keeps its {})
    said = " ".join(" ".join(line.split()) for line in lines)
    # (the dict written as given, not merged with the default: removing nofile would leave the file without it)
    assert "remove cassandra_linux_limits from host_vars/node2/ (the rest of it is the collection's default) then 1" \
        " node changes" in said, said
    assert "remove nofile" not in said
    assert "--limit node2 --check" in said and "node1,node2" not in said
    # every node sets it (one {}): the same, no "(not set) node1" row per sub-key of the default
    nodes = [with_vars(1, cassandra_linux_limits={}), with_vars(2, cassandra_linux_limits=limits),
             with_vars(3, cassandra_linux_limits=dict(role_file("cassandra_linux")["cassandra_linux_limits"]))]
    lines = section(report(nodes), "DIFFERENT ON SOME NODES")
    assert rows(lines, "cassandra_linux_limits") == [("(its own keys, below)", "all - node1"), ("{}", "node1")]
    assert sorted(set(re.findall(r"(cassandra_linux_limits\.[\w.]+):", " ".join(lines)))) == [
        "cassandra_linux_limits.nofile"]
    assert rows(lines, "cassandra_linux_limits.nofile") == [("1048576", "node3"), ("65536", "node2")]
    said = " ".join(" ".join(line.split()) for line in lines)
    assert "nofile: 1048576 in cassandra_linux_limits in host_vars/node2/ then 1 node changes" in said, said
    assert "(not set)" not in said and "node1,node2" not in said


def test_a_value_that_differs_by_case_only_differs():
    """pam_limits reads "unlimited" only: "Unlimited" is not the default's value."""
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file
    limits = dict(role_file("cassandra_linux")["cassandra_linux_limits"], memlock="Unlimited")

    def with_vars(i, **more):
        return dict(node(i), vars=dict(node(i)["vars"], **more))
    review = " ".join(section(report([with_vars(1, cassandra_linux_limits=limits), with_vars(2)]),
                              "DIFFERENT ON SOME NODES"))
    assert sorted(set(re.findall(r"(cassandra_linux_limits\.[\w.]+):", review))) == ["cassandra_linux_limits.memlock"]


def test_a_rewrite_apply_config_proves_no_restart_for_is_not_said_without_restart():
    # (self-review 2026-11-25) a file outside apply_config's proof of no restart (cassandra-env.sh, the JVM options
    # files, logback.xml): never under "no restart"
    yaml = dict(writes(("cassandra.yaml", "rewritten", "layout"))[0], proof=False)
    checks = {"node%d" % i: {"differences": [], "notes": [], "writes": PROD_WRITES[1:3] + [yaml]} for i in (1, 2)}
    lines = report([node(1), node(2)], self_check=checks, screen=True, width=200)
    assert sync(lines)[1:4] == ["    the next apply_config:",
                                "      cassandra.yaml         rewritten, may restart: layout",
                                "      jvm-server.options     rewritten: heap placement"]
    said = " ".join(" ".join(line.split()) for line in lines)
    assert "WHAT HAPPENS: a restart if it cannot prove none for cassandra.yaml" in said, said


def test_the_next_step_expects_the_writes_then_every_node_up_to_date():
    # e2e (a prod-shape import): "expected: every node up to date (header lines aside)", then apply_config --check
    # rewrote cassandra-env.sh and the heap's two files: the expected line names them now
    checks = {"node1": {"differences": [], "notes": [], "writes": PROD_WRITES[:1]},
              "node2": {"differences": [], "notes": [], "writes": []}}
    said = " ".join(" ".join(line.split()) for line in report([node(1), node(2)], self_check=checks, details=False))
    assert "expected: WOULD CHANGE: the 1 file the head lists under the next apply_config; WHAT HAPPENS: no" \
        " restart" in said, said
    checks["node1"]["writes"] = []
    said = " ".join(" ".join(line.split()) for line in report([node(1), node(2)], self_check=checks, details=False))
    assert "expected: every node up to date" in said and "WOULD CHANGE" not in said


def test_a_sub_key_of_a_dict_not_merged_with_its_default_is_advised_as_written():
    """cassandra_linux_limits is written as given (limits.yml), never merged with the collection's default: a node
    whose dict has another nofile, or lacks some of the default's keys, is told to set the default's value in it (or
    to remove the dict when the rest of it is the default's), never to remove a sub-key it does not have."""
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file
    stock = role_file("cassandra_linux")["cassandra_linux_limits"]

    def with_vars(i, **more):
        return dict(node(i), vars=dict(node(i)["vars"], **more))

    def said(nodes):
        return " ".join(" ".join(line.split()) for line in section(report(nodes), "DIFFERENT ON SOME NODES"))
    # only nofile of its own: memlock, nproc, as not there (not removed: set to the default's)
    text = said([with_vars(1), with_vars(2, cassandra_linux_limits={"nofile": 65536}), with_vars(3)])
    assert "nofile: %s (the collection's default) in cassandra_linux_limits in host_vars/node2/" % stock["nofile"] \
        in text, text
    assert "memlock: unlimited (the collection's default) in cassandra_linux_limits in host_vars/node2/" in text, text
    assert not re.search(r"remove (nofile|memlock|nproc|as) from", text), text
    assert "remove cassandra_linux_limits" not in text
    # another value besides nofile: the default's nofile written, the dict kept
    other = dict(stock, nofile=65536, nproc=4096)
    text = said([with_vars(1), with_vars(2, cassandra_linux_limits=other), with_vars(3)])
    assert "nofile: %s (the collection's default) in cassandra_linux_limits in host_vars/node2/" % stock["nofile"] \
        in text, text
    assert "remove cassandra_linux_limits" not in text and "remove nofile" not in text
    # the same for cassandra_linux_sysctl, written as given too
    sysctl = role_file("cassandra_linux")["cassandra_linux_sysctl"]
    text = said([with_vars(1), with_vars(2, cassandra_linux_sysctl=dict(sysctl, **{"vm.swappiness": 10})),
                 with_vars(3)])
    assert "remove cassandra_linux_sysctl from host_vars/node2/ (the rest of it is the collection's default)" in text, \
        text

