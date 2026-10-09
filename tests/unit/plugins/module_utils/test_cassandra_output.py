from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import time

import pytest

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

GIB = 1024 ** 3
MIB = 1024 ** 2
ARROW = u"←"


# --- sizes, rates, durations ---

@pytest.mark.parametrize("count, scale, text", [
    (0, None, "0 B"),
    (512, None, "512 B"),
    (2048, None, "2.0 KiB"),
    (260.6 * MIB, None, "260.6 MiB"),  # alone: the unit it reads >= 1 in
    (41.2 * GIB, None, "41.2 GiB"),
    (5 * 1024 * GIB, None, "5.0 TiB"),
    (0, 100 * GIB, "0.0 GiB"),
    (0.3 * GIB, 100 * GIB, "0.3 GiB"),  # the unit of the scale
    (52 * 1024, 100 * GIB, "52.0 KiB"),  # never 0.0 GiB for bytes there
])
def test_size(count, scale, text):
    assert out.size(count, scale) == text


def test_amount_and_parse_size():
    assert out.amount(52.2 * GIB, 100 * GIB) == "52.2/100.0 GiB"
    assert out.amount(52 * 1024, 100 * GIB) == "52.0 KiB/100.0 GiB"
    assert out.amount(1 * GIB, 2 * GIB, " / ") == "1.0 / 2.0 GiB"
    assert out.parse_size("94.39 MiB") == 94.39 * MIB
    assert out.parse_size("1,5 GiB") == 1.5 * GIB
    assert out.parse_size("100 bytes") == 100
    assert out.parse_size("3 KB") == 3072
    assert out.parse_size("?") is None and out.parse_size(None) is None


def test_rate_duration_count_plural():
    assert out.rate(89 * MIB) == "89 MiB/s"
    assert out.rate(3.2 * MIB) == "3.2 MiB/s"
    assert out.rate(1.1 * GIB) == "1.1 GiB/s"
    assert out.rate(2048) == "2 KiB/s" and out.rate(10) == "10 B/s"
    assert [out.duration(s) for s in (45, 560, 690, 3 * 3600 + 60, 2 * 86400 + 4 * 3600)] == \
        ["45s", "9m20s", "11m30s", "3h01m", "2d04h"]
    assert out.duration(560, short=True) == "9m" and out.duration(-5) == "0s"
    assert out.count(1240) == "1 240"
    assert out.plural(1, "node") == "1 node" and out.plural(3, "node") == "3 nodes"
    assert out.plural(2, "copy", "copies") == "2 copies"


def test_clock_today_and_another_day():
    now = time.mktime((2026, 10, 8, 12, 0, 0, 0, 0, -1))
    assert out.clock(now + 3600, now).startswith("13:00 ")
    assert out.clock(now + 2 * 86400, now).startswith("2026-10-10 12:00 ")


# --- node lists (Q3) ---

@pytest.mark.parametrize("names, text", [
    ([], ""),
    (["node1"], "node1"),
    (["node1", "node2"], "node1, node2"),  # 2 listed
    (["node1", "node2", "node3"], "node1..node3"),  # a range from 3
    (["node3", "node1", "node2", "node5"], "node1..node3, node5"),  # sorted, a gap breaks the range
    (["node1", "node2", "node4", "node5"], "node1, node2, node4, node5"),  # never node1..node5
    (["node1", "node2", "node3", "node5", "node7", "node8"], "6 nodes: node1..node3, node5, node7, node8"),
    (["node%d" % i for i in range(1, 6)], "node1..node5"),  # 5: no count
    (["node9", "node10", "node11"], "node9..node11"),
    (["node09", "node10", "node11"], "node09..node11"),
    (["node008", "node009", "node010"], "node008..node010"),
    (["n1.dc1", "n2.dc1", "n3.dc1"], "n1.dc1..n3.dc1"),
    (["dc1-n1", "dc1-n2", "dc2-n3"], "dc1-n1, dc1-n2, dc2-n3"),  # another number changes
    (["a", "b", "c"], "a, b, c"),
    (["node1", "node1", "node2"], "node1, node2"),
])
def test_nodes(names, text):
    assert out.nodes(names) == text


def test_nodes_order_and_full():
    assert out.nodes(["node3", "node1", "node2"], keep_order=True) == "node3, node1, node2"
    assert out.nodes(["node4", "node5", "node6", "node1"], keep_order=True) == "node4..node6, node1"
    names = ["node%d" % i for i in range(1, 8)]
    assert out.nodes(names, full=True) == ", ".join(names)
    assert out.full_list(["node10", "node2"]) == "node2, node10"


# --- secrets ---

def test_mask_secret_shown():
    assert out.mask("keystore_password: changeit") == "keystore_password: ****"
    assert out.mask("-Djavax.net.ssl.keyStorePassword=abc -Xmx8G") == "-Djavax.net.ssl.keyStorePassword=****"
    assert out.mask("concurrent_reads: 32") == "concurrent_reads: 32"
    assert out.secret("cassandra_jmx_password", "x") and not out.secret("cassandra_jmx_password_file", "/etc/x")
    assert out.secret("opts", "-Dtruststore_password=y") and not out.secret("cassandra_jmx_password", "")
    assert out.secret("server_encryption_options", {"keystore_password": "x"})
    assert out.shown("keystore_password", "x") == "****"
    assert [out.shown("k", v) for v in ("text", 32, True, None, ["a"], {"b": 1})] == \
        ["text", "32", "true", "null", '["a"]', '{"b": 1}']


# --- a setting and its value per node ---

def test_setting_lines_all_and_differs():
    assert out.setting_lines("concurrent_reads", {"node1": 32, "node2": 32}) == ["concurrent_reads:  32   all"]
    lines = out.setting_lines("concurrent_reads", {"node1": 32, "node2": 32, "node3": 64, "node4": 32})
    assert lines == ["concurrent_reads:  32   node1, node2, node4",
                     "                   64   node3                 %s differs" % ARROW]


def test_setting_lines_not_all_when_nodes_missing():
    # node3 of the run has no value: not "all"
    assert out.setting_lines("x", {"node1": 1, "node2": 1}, all_nodes=["node1", "node2", "node3"]) == \
        ["x:  1   node1, node2"]


def test_setting_lines_expected_notes_where_and_secrets():
    lines = out.setting_lines("heap", {"n1": "8G", "n2": "8G", "n3": "16G"}, expected="16G",
                              where={"8G": "host_vars"}, notes={"16G": "set by hand"})
    assert lines == ["heap:  8G    n1, n2   in host_vars  %s differs" % ARROW,
                     "       16G   n3       (collection value)  (set by hand)"]
    assert out.setting_lines("heap", {"n1": "8G"}, where="group_vars/all.yml") == ["heap:  8G   all   in group_vars/all.yml"]
    lines = out.setting_lines("keystore_password", {"n1": "s3cret1", "n2": "s3cret2"})
    assert "s3cret" not in " ".join(lines) and lines[0].startswith("keystore_password:  ****   n1")
    assert out.setting_lines("x", {}) == []


def test_by_nodes():
    lines = out.by_nodes([("node1", ["a", "b"]), ("node2", ["a", "c"]), ("node3", ["a", "c"])])
    assert lines == ["all", "  a", "node1", "  b", "node2, node3", "  c"]


# --- plan ---

def test_plan_order_warnings_above_the_question():
    lines = out.plan("decommission_node", "my_cluster", "2 nodes, one at a time",
                     steps=[{"node": "node7", "dc": "dc1", "rack": "rack_a", "text": "load 47.1 GiB -> node1,node4"},
                            {"node": "node8", "dc": "dc1", "rack": "rack_b", "text": "load 48.1 GiB"}],
                     facts=[["dc1 after", "6 nodes"], ["ring now", "8 UN"], "a plain fact"],
                     warnings=["not inside tmux/screen"], question="Decommission node7, node8? (yes/no)",
                     version="4.1.5")
    assert lines == [
        "PLAN  decommission_node  my_cluster (Cassandra 4.1.5)  2 nodes, one at a time",
        "  1.  node7  dc1/rack_a  load 47.1 GiB -> node1,node4",
        "  2.  node8  dc1/rack_b  load 48.1 GiB",
        "",
        "dc1 after:  6 nodes",
        "ring now:   8 UN",
        "a plain fact",
        "",
        "WARNING  not inside tmux/screen",
        "",
        "Decommission node7, node8? (yes/no)",
    ]


def test_plan_check_and_refused():
    lines = out.plan("cleanup", "c1", steps=["node1"], question="Go?", check=True)
    assert lines == ["PLAN  cleanup  c1", "  1.  node1", "", "--check: nothing will be changed"]
    assert out.plan("decommission_node", "c1", verdict="REFUSED", facts=["dc1 after: 2 nodes"]) == \
        ["REFUSED  decommission_node  c1", "", "dc1 after: 2 nodes"]
    # an empty block: no blank line for it, never two in a row
    assert out.plan("cleanup", "c1", steps=["node1"], warnings=["w", " "], question="Go?") == \
        ["PLAN  cleanup  c1", "  1.  node1", "", "WARNING  w", "", "Go?"]
    assert out.plan("cleanup", "c1", facts=["f"], warnings=["w"], check=True) == \
        ["PLAN  cleanup  c1", "", "f", "", "WARNING  w", "", "--check: nothing will be changed"]


# --- progress (Q2) ---

NOW = time.mktime((2026, 10, 8, 13, 24, 0, 0, 0, -1))


def test_progress_line_going_with_peers_always():
    lines = out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=52.2 * GIB, total=100 * GIB,
                              speed=89 * MIB, now=NOW, start=NOW - 600,
                              peers=[{"name": "node1", "way": "from", "done": 18 * GIB, "total": 34 * GIB},
                                     {"name": "node3", "way": "from", "done": 4.2 * GIB, "total": 17 * GIB,
                                      "stalled": True},
                                     {"name": "node2", "way": "from", "done": 3 * GIB, "total": 3 * GIB}])
    eta = time.strftime("%H:%M", time.localtime(NOW + 47.8 * GIB / (89 * MIB)))
    assert lines == [
        "[1/2] node5 bootstrap  JOINING  [#####-----]  52%%  52.2/100.0 GiB  89 MiB/s  ETA %s (9m)  10m" % eta,
        "      from node1 18.0/34.0 GiB ok   from node3 4.2/17.0 GiB stalled   from node2 3.0/3.0 GiB done"]


def test_progress_line_waiting_stalled_done():
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", now=NOW, start=NOW - 32) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  32s"]
    # no stream yet (a bootstrap's ring delay): not called stalled, the time without progress shown after a minute
    assert out.progress_line(1, 2, "node5", "bootstrap", mode="JOINING", now=33, start=0, idle=33, limit=3600) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  33s"]
    assert out.progress_line(1, 2, "node5", "bootstrap", mode="JOINING", now=120, start=0, idle=120, limit=3600) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  no progress 2m/1h00m  2m"]
    # data left, a check or two without a byte: no news; a minute: STALLED and the limit
    assert "STALLED" not in out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB,
                                              now=NOW, start=NOW - 900, idle=50, limit=900)[0]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 900, idle=360, limit=900) == \
        ["[1/2] node5 bootstrap  JOINING  STALLED 6m/15m  37%  37.5/100.0 GiB  15m"]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 900, idle=900, limit=900, status="stalled") == \
        ["[1/2] node5 bootstrap  JOINING  STALLED 15m/15m  37%  37.5/100.0 GiB  15m"]
    # all sent, the end not there yet (index builds): finishing, quiet for a while
    assert out.progress_line(2, 3, "node4", "decommission", "LEAVING", done=GIB, total=GIB, now=NOW, start=NOW - 600,
                             idle=300, limit=3600)[0].endswith("all sent, finishing  no progress 5m/1h00m  10m")
    assert out.progress_line(0, 0, "node5", "bootstrap", done=10 * GIB, total=10 * GIB, now=NOW, start=NOW - 60,
                             status="done") == ["node5 bootstrap  done  10.0/10.0 GiB  1m"]
    assert out.progress_line(2, 3, "node4", "decommission", "LEAVING", done=1 * GIB, total=1 * GIB, speed=MIB,
                             now=NOW, start=NOW)[0].endswith("all sent, finishing  0s")


# --- recap (Q4) ---

def restarts(times):
    return [{"node": "node%d" % (i + 1), "outcome": "restarted", "seconds": t} for i, t in enumerate(times)]


def test_recap_grouped_with_the_slow_node_apart():
    lines = out.recap("rolling_restart", "my_cluster", restarts([109, 112, 400, 115, 118, 110]), seconds=690)
    assert lines == ["DONE  rolling_restart  my_cluster  6 restarted (11m30s)",
                     "  node1, node2, node4..node6  restarted  (1m49s..1m58s each)",
                     "  node3                       restarted  6m40s  %s slow (median 1m53s)" % ARROW]


def test_recap_no_outlier_under_the_factor_and_one_node():
    assert out.recap("rolling_restart", "c", restarts([100, 140]))[1:] == ["  node1, node2  restarted  (1m40s..2m20s each)"]
    assert out.recap("rolling_restart", "c", restarts([100]))[1:] == ["  node1  restarted  1m40s"]


def test_recap_failed_and_skipped():
    lines = out.recap("rolling_restart", "my_cluster", restarts([100]) + [
        {"node": "node2", "status": "failed", "reason": "not UN after 10m"},
        {"node": "node3", "status": "skipped", "reason": "after the failure"},
        {"node": "node4", "status": "skipped", "reason": "after the failure"}])
    assert lines == ["FAILED  rolling_restart  my_cluster  1 restarted, 1 failed, 2 not touched",
                     "  node1         restarted                        1m40s",
                     "  node2         FAILED: not UN after 10m",
                     "  node3, node4  not touched (after the failure)"]


def test_recap_check_mode_says_would_and_nothing_changed():
    lines = out.recap("apply_config", "my_cluster", [{"node": "node%d" % i, "outcome": "applied, restarted",
                                                      "seconds": 3} for i in range(1, 5)], check=True, seconds=20,
                      extra=["  cassandra.yaml"])
    assert lines == ["CHECK  apply_config  my_cluster  would apply, restart 4 nodes   nothing was changed",
                     "  node1..node4  would apply, restart",
                     "",
                     "  cassandra.yaml"]
    assert out.would("restarted") == "would restart" and out.would("frobbed") == "would be frobbed"
    assert out.recap("x", "c", []) == ["DONE  x  c  nothing to do"]


# --- what changed ---

def test_perm_lines():
    assert out.perm_lines([
        {"file": "cassandra.yaml", "before": {"owner": "root", "group": "cassandra", "mode": "0640"},
         "after": {"owner": "cassandra", "group": "cassandra", "mode": "0640"}},
        {"file": "same.sh", "before": {"owner": "a", "group": "b", "mode": "0644"},
         "after": {"owner": "a", "group": "b", "mode": "0644"}}]) == \
        ["  cassandra.yaml  owner/group/mode  root/cassandra 0640 -> cassandra/cassandra 0640"]


def test_diff_lines_changed_only_nested_and_masked():
    before = {"concurrent_reads": 32, "num_tokens": 16,
              "server_encryption_options": {"keystore_password": "old", "protocol": "TLS"}}
    after = {"concurrent_reads": 64, "num_tokens": 16, "new_one": True,
             "server_encryption_options": {"keystore_password": "new", "protocol": "TLS"}}
    lines = out.diff_lines(before, after)
    assert lines == ["  - concurrent_reads: 32", "  + concurrent_reads: 64", "  + new_one: true",
                     "  server_encryption_options:", "  -   keystore_password: ****", "  +   keystore_password: ****"]
    assert "old" not in "".join(lines) and "new\n" not in "\n".join(lines)


def test_changed_lines_masks_and_drops_headers():
    diff = "--- a\n+++ b\n@@ -1 +1 @@\n context\n-keystore_password: old\n+keystore_password: new\n-x: 1\n+x: 2\n"
    assert out.changed_lines(diff) == ["  -keystore_password: ****", "  +keystore_password: ****", "  -x: 1", "  +x: 2"]


# --- what is left to do (Q8) ---

def test_command_full_and_minimal():
    assert out.command("cleanup", "inventory.yml", "my_cluster", ["node1", "node2"],
                       {"cassandra_cleanup_jobs": 2}) == \
        "ansible-playbook -i inventory.yml community.cassandra.cleanup -e cassandra_hosts=my_cluster" \
        " --limit node1,node2 -e cassandra_cleanup_jobs=2"
    assert out.command("status") == "ansible-playbook community.cassandra.status"  # ansible.cfg's inventory
    assert out.command("topology", ["/srv/inv/a.yml", "/srv/inv/b.yml"], cwd="/srv") == \
        "ansible-playbook -i inv/a.yml -i inv/b.yml community.cassandra.topology"
    assert out.command("x", extra={"msg": "two words"}) == \
        """ansible-playbook community.cassandra.x -e '{"msg": "two words"}'"""
    assert out.command("x", options=["--vault-id", "prod@file"], extra=["-e a=1"]) == \
        "ansible-playbook --vault-id prod@file community.cassandra.x -e a=1"


def test_todo():
    assert out.todo([]) == []
    assert out.todo(["remove node7 from the inventory", None,
                     {"text": "cleanup the nodes that gave data", "command": "ansible-playbook x"}]) == [
        "TO DO", "  1. remove node7 from the inventory", "  2. cleanup the nodes that gave data:",
        "     ansible-playbook x"]


def test_inventory_steps_with_and_without_git(tmp_path):
    plain = tmp_path / "plain"
    (plain / "inv").mkdir(parents=True)
    hosts = plain / "inv" / "hosts.yml"
    hosts.write_text("all: {}\n")
    assert not out.in_git_work_tree(str(hosts))
    assert out.inventory_steps(str(hosts), cwd=str(plain)) == ["Review the inventory: inv/hosts.yml"]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "inv").mkdir()
    hosts = repo / "inv" / "hosts.yml"
    hosts.write_text("all: {}\n")
    assert out.in_git_work_tree(str(hosts)) and out.in_git_work_tree(str(repo / "inv"))
    steps = out.inventory_steps(str(hosts), message="Remove node7", cwd=str(repo))
    assert steps == ["Review the inventory: inv/hosts.yml",
                     {"text": "commit it", "command": "git add inv/hosts.yml && git commit -m 'Remove node7'"}]
    assert out.inventory_steps("x.yml", in_git=False) == ["Review the inventory: x.yml"]


@pytest.mark.parametrize("text, masked", [
    ('"keystore_password": "x"', '"keystore_password": ****'),
    ("{'keystore_password': 'x', 'a': 1}", "{'keystore_password': ****, 'a': 1}"),
    ("sse_c_key = x", "sse_c_key = ****"),
    ("access_key: x", "access_key: ****"),
    ("password: >-\n  x\n  y\nnext: 1", "password: ****\n  ****\n  ****\nnext: 1"),
    ("cqlsh -u a -p x", "cqlsh -u a -p ****"),
    ("nodetool -pw x status", "nodetool -pw **** status"),
    ("--password x", "--password ****"),
    # an unquoted value to the end of the line: a comma or a brace is part of it
    ("keystore_password: abc,def", "keystore_password: ****"),
    ("keystore_password: ab}cd", "keystore_password: ****"),
    ("password: abc, def", "password: ****"),
    ("-Dssl.keyStorePassword=ab,cd -Dx=1", "-Dssl.keyStorePassword=****"),
    ("{password: abc, user: u}", "{password: ****, user: u}"),
    # a brace in the value or Jinja on the line: still to the end of the line; every secret of a flow mapping
    ("keystore_password: P@ss{1,2}word", "keystore_password: ****"),
    ("-Dcassandra.jmx.password=a{b}c,d", "-Dcassandra.jmx.password=****"),
    ("msg: {{ x }} password=hunter2,xyz", "msg: {{ x }} password=****"),
    ("{password: abc, secret: b, user: u}", "{password: ****, secret: ****, user: u}"),
    ('{"a": {"password": "x"}, "b": 1}', '{"a": {"password": ****}, "b": 1}'),
])
def test_mask_more_forms(text, masked):
    assert out.mask(text) == masked


def test_changed_lines_mask_a_value_with_a_comma():
    assert out.changed_lines(["+keystore_password: ab,cd"]) == ["  +keystore_password: ****"]


def test_hidden_values_in_lists_dicts_and_a_dict_becoming_a_value():
    assert out.shown("opts", ["a", ["-Dpassword=x"]]) == "****"
    assert out.shown("env", {"CASSANDRA_PASS": "x"}) == "****"
    assert out.shown("ldap_bind_pw", "x") == "****" and out.shown("num_tokens", 16) == "16"
    lines = out.diff_lines({"ks": {"keystore_password": "old"}}, {"ks": "x"})
    assert "x" not in " ".join(line.split(":")[-1] for line in lines) and "old" not in " ".join(lines)


@pytest.mark.parametrize("text", ["nodetool -p 7199 status", "mkdir -p /var/lib/cassandra", "ssh -p 2222 host",
                                  "credentials_validity_in_ms: 2000", "num_tokens: 16"])
def test_mask_leaves_ports_paths_and_settings(text):
    assert out.mask(text) == text


def test_mask_cqlsh_password_and_block_values():
    assert out.mask("cqlsh -u admin -p s3cr3t node1") == "cqlsh -u admin -p **** node1"
    assert out.mask("x_password: >-\n  line1\n\n  line2\nnext: 1") == "x_password: ****\n  ****\n\n  ****\nnext: 1"
    assert out.changed_lines("-  keystore_password: |\n-    s3cr3t\n+  other: 1\n") == \
        ["  -  keystore_password: ****", "  -    ****", "  +  other: 1"]
    assert out.diff_lines({}, {"a": 1}) == ["  + a: 1"] and out.diff_lines(None, {}) == []
