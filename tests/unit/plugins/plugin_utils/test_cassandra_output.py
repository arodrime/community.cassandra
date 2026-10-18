from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import time

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def test_plural():
    assert out.plural(1, "node") == "1 node" and out.plural(3, "node") == "3 nodes"
    assert out.plural(2, "copy", "copies") == "2 copies"


def test_duration():
    assert [out.duration(s) for s in (45, 560, 690, 3 * 3600 + 60, 2 * 86400 + 4 * 3600)] == \
        ["45s", "9m20s", "11m30s", "3h01m", "2d04h"]
    assert out.duration(560, short=True) == "9m" and out.duration(-5) == "0s"


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


def test_one_mark():
    assert out.warning("swap left on") == ["WARNING - swap left on"]
    assert out.warning("WARNING - swap left on") == ["WARNING - swap left on"]  # marked already: kept
    # a longer one: its next lines indented under it, those indented already kept as they are
    assert out.warning("a:\nb\n    c") == ["WARNING - a:", "  b", "    c"]
    assert out.warning(["a:", "b"]) == ["WARNING - a:", "  b"]
    assert out.warning("") == []
    assert out.is_warning("  WARNING - x") and not out.is_warning("NOTE  x") and not out.is_warning("not a WARNING")
    assert out.warning_text("WARNING - data disks: x") == "data disks: x"
    assert out.warning_text("plain") == "plain"


def test_warnings_once_a_blank_line_apart():
    lines = out.warning_lines(["a", "b:\n  b1\n  b2", "a", "c"])
    assert lines == ["WARNING - a", out.PARAGRAPH, "WARNING - b:", "  b1", "  b2", out.PARAGRAPH, "WARNING - c"]
    seen = set()
    assert out.warning_lines(["a"], seen) == ["WARNING - a"]
    assert out.warning_lines(["a", "b"], seen) == ["WARNING - b"]  # said already: left out


P = out.PARAGRAPH  # a blank line between two paragraphs of a block
GIB = 1024 ** 3
MIB = 1024 ** 2


@pytest.mark.parametrize("count, scale, text", [
    (0, None, "0 B"),
    (512, None, "512 B"),
    (1023, None, "1023 B"),
    (1024, None, "1.0 KiB"),
    (2048, None, "2.0 KiB"),
    (1023.97 * 1024, None, "1.0 MiB"),  # not 1024.0 KiB
    (260.6 * MIB, None, "260.6 MiB"),  # alone: the unit it reads >= 1 in
    (1000 * MIB, None, "1000.0 MiB"),
    (1024 * MIB, None, "1.0 GiB"),
    (41.2 * GIB, None, "41.2 GiB"),
    (1023.99 * GIB, None, "1.0 TiB"),
    (5 * 1024 * GIB, None, "5.0 TiB"),
    (0, 100 * GIB, "0.0 GiB"),
    (0.3 * GIB, 100 * GIB, "0.3 GiB"),  # the unit of the scale
    (52 * 1024, 100 * GIB, "<0.1 GiB"),  # never 0.0 for a non-zero size
    (5 * GIB, 0, "5.0 GiB"),  # no scale: its own unit
    (-5, None, "0 B"),
])
def test_size(count, scale, text):
    assert out.size(count, scale) == text


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
        "TO DO", "  1. remove node7 from the inventory", P, "  2. cleanup the nodes that gave data:",
        "     ansible-playbook x"]


# --- recap ---

def restarts(times):
    return [{"node": "node%d" % (i + 1), "outcome": "restarted", "seconds": t} for i, t in enumerate(times)]


def test_recap_grouped_with_the_slow_node_apart():
    lines = out.recap("rolling_restart", "my_cluster", restarts([109, 112, 400, 115, 118, 110]), seconds=690)
    assert lines == ["DONE  rolling_restart  my_cluster  6 restarted (11m30s)",
                     "  node1, node2, node4..node6  restarted  (1m49s..1m58s each)",
                     "  node3                       restarted  6m40s  %s slow (median 1m53s)" % out.ARROW]


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
                     "  node2         FAILED",
                     "    not UN after 10m",
                     "  node3, node4  not touched (after the failure)"]


def test_recap_columns_not_stretched_by_a_long_reason():
    reason = "Cluster not healthy, nothing more is done: " + "x" * 300
    lines = out.recap("rolling_restart", "c", restarts([96]) + [{"node": "node2", "status": "failed", "reason": reason}])
    assert lines[1:] == ["  node1  restarted  1m36s", "  node2  FAILED", "    " + reason]


def test_recap_check_mode_says_would_and_nothing_changed():
    lines = out.recap("apply_config", "my_cluster", [{"node": "node%d" % i, "outcome": "applied, restarted",
                                                      "seconds": 3} for i in range(1, 5)], check=True, seconds=20,
                      extra=["  cassandra.yaml"])
    assert lines == ["CHECK  apply_config  my_cluster  would apply, restart on 4 nodes   nothing was changed",
                     "  node1..node4  would apply, restart",
                     "",
                     "  cassandra.yaml"]
    assert out.would("restarted") == "would restart" and out.would("frobbed") == "would be frobbed"
    assert out.recap("x", "c", []) == ["NOTHING TO DO  x  c  nothing to do"]


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


@pytest.mark.parametrize("text", ["nodetool -p 7199 status", "mkdir -p /var/lib/cassandra", "ssh -p 2222 host",
                                  "credentials_validity_in_ms: 2000", "num_tokens: 16"])
def test_mask_leaves_ports_paths_and_settings(text):
    assert out.mask(text) == text


# --- node groups, wrapping (a large cluster) ---

ARROW = out.ARROW
BIG_A = ["db1%d" % i for i in range(1, 7)] + ["db2%d" % i for i in range(1, 7)]  # 12
BIG_B = ["db07%d" % i for i in range(1, 7)]  # 6
BIG_C = ["db10%d" % i for i in range(1, 4)] + ["db111"]  # 4
BIG = BIG_A + BIG_B + BIG_C


def test_node_groups_are_the_partition_of_the_lists():
    sets = [BIG_A, BIG_B + BIG_C, [n for n in BIG_B if n != "db074"], BIG_A + ["db074"], BIG_C[:3]]
    groups = out.node_groups(sets, BIG)
    # db074 and db111 are in other lists than their neighbours: their names; db101..db103 a group of 3
    assert groups == [("G1", BIG_A), ("G2", ["db071", "db072", "db073", "db075", "db076"]),
                      ("G3", ["db101", "db102", "db103"])]
    assert out.who(BIG_B + BIG_C[:3], BIG, groups) == "G2 + G3 + db074"
    assert out.who(BIG_A + ["db074"], BIG, groups) == "G1 + db074"
    assert out.who(BIG, BIG, groups) == "all"
    assert out.who([n for n in BIG if n != "db111"], BIG, groups) == "all except db111"
    assert out.who(BIG_B + BIG_C, BIG, groups) == "all except G1"  # shorter than G2 + G3 + db074, db111
    assert out.who(["db101", "db074"], BIG, groups) == "db074, db101"
    singles = ["a1", "b2", "c3", "d4", "f6", "g7"]
    assert out.who(BIG_A + singles, BIG_A + BIG_B + singles + ["e5"], [("G1", BIG_A), ("G2", BIG_B)]) == \
        "all except (G2 + e5)"  # read as one
    # a list of one node splits nothing (a value per node would leave no group); the nodes in no list (not
    # read) make no group either
    assert out.node_groups([[n] for n in BIG] + [BIG_A, BIG_B + BIG_C], BIG) == [("G1", BIG_A), ("G2", BIG_B + BIG_C)]
    assert out.node_groups([[n] for n in BIG] + [BIG_A], BIG) == [("G1", BIG_A)]
    # under GROUPS_FROM nodes, or every node alike: none
    assert out.node_groups([BIG_A[:3]], BIG_A[:11]) == []
    assert out.node_groups([BIG], BIG) == []


def test_group_lines():
    groups = [("G1", BIG_A), ("G2", BIG_B)]
    assert out.group_lines(groups) == ["NODE GROUPS", "  G1 = db11..db16, db21..db26 (12 nodes)",
                                       "  G2 = db071..db076 (6 nodes)"]
    assert out.group_lines(groups, full=True, width=60) == [
        "NODE GROUPS",
        "  G1 = db11, db12, db13, db14, db15, db16, db21, db22, db23,",
        "       db24, db25, db26 (12 nodes)",
        "  G2 = db071, db072, db073, db074, db075, db076 (6 nodes)"]
    assert out.group_lines([]) == []


def test_wrap_list_never_cuts_a_name():
    text = "db11..db16, db21..db26, db071..db076, db101..db103"
    assert out.wrap_list(text, 30) == ["db11..db16, db21..db26,", "db071..db076, db101..db103"]
    assert out.wrap_list("G1 + G2 + db074", 6) == ["G1", "+ G2", "+ db074"]
    assert out.wrap_list("a-very-long-node-name.example.net", 10) == ["a-very-long-node-name.example.net"]
    assert out.wrap_list(text, 200) == [text]
    assert out.wrap_list("all except db006..db009, db101", 22) == ["all except", "db006..db009, db101"]
    assert out.wrap_list("all except (G2 + db074)", 15) == ["all except (G2", "+ db074)"]


def test_wrap_value_and_fit():
    assert out.wrap_value("short", 20) == ["short"]
    assert out.wrap_value("a b c d e f g h i j k l", 10) == ["a b c d e", "f g h i j", "k l"]
    assert out.wrap_value("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]  # a value may be cut, not a name
    lines = ["TO DO (1)", "  1. " + " ".join(["word"] * 20), "short"]
    assert out.fit(lines, 40) == ["TO DO (1)", "  1. word word word word word word word", "     word word word word word word word",
                                  "     word word word word word word", "short"]
    assert out.fit(["  " + "y" * 50], 20) == ["  " + "y" * 50]  # one long word: whole
    assert out.fit(lines, None) == lines


def test_settings_lines_in_a_width():
    entries = [("heap", [("31G", "G1", ""), ("16G", "G2 + db074", ARROW + " differs (group_vars/big_dc2)")]),
               ("listen_address", [("{{ ansible_facts['default_ipv4']['address'] }}", "all", "")]),
               ("seeds", [("db11, db071, db101", "db11..db16, db21..db26, db071..db076, db101..db103", "")])]
    assert out.settings_lines(entries, width=70) == [
        "  heap:           31G                  G1",
        "                  16G                  G2 + db074",
        "                                       %s differs (group_vars/big_dc2)" % ARROW,
        "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all",
        "  seeds:          db11, db071, db101   db11..db16, db21..db26,",
        "                                       db071..db076, db101..db103"]
    # narrower: the long value goes on under its column, the note too
    narrow = out.settings_lines(entries, width=60)
    assert narrow[2:5] == ["%s%s differs (group_vars/big_dc2)" % (" " * 30, ARROW),  # moved left to fit
                           "  listen_address: {{ ansible_facts['default_ipv4']['add  all",
                           "                  ress'] }}"]
    assert all(len(line) <= 60 for line in narrow)
    # wide enough: a long value pushes its nodes, the notes after them
    wide = out.settings_lines(entries, width=200)
    assert wide[1] == "                  16G                  G2 + db074%s   %s differs (group_vars/big_dc2)" % (" " * 20, ARROW)
    assert wide[2] == "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all"
    # no width: as before
    assert out.settings_lines(entries)[2] == "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all"


# --- a secret's value on several lines: every line of it masked ---

# a secret name in the key line's comment or inside the value, a YAML tag or anchor, the shell's $'...', an XML element
BLOCK_SECRETS_AFTER_AUDIT = [
    "  keystore_password: |  # PEM password: see vault\n    LEAK1\n    LEAK2\n  next: 1",
    "keystore_password: | # rotate the password: yearly\n  LEAK1\n  LEAK2\nnext: 1",
    "keystore_password:  # old_password: x\n    LEAK1\nnext: 1",
    "export KS_PASSWORD=\"LEAK0 # a password: x\nLEAK1\"\nnext=1",
    "keystore_password: !!str \"LEAK1\nLEAK2\"\nnext: 1",
    "keystore_password: &a 'LEAK1\nLEAK2'\nnext: 1",
    "export KS_PASSWORD=$'LEAK1\nLEAK2'\nnext=1",
    "JVM_OPTS=\"$JVM_OPTS -Da.password=x -Db.password='LEAK1\nLEAK2'\"\nnext=1",
    "<password>LEAK1</password>\n<next>1</next>",
    "<password><![CDATA[LEAK1]]></password>\n<next>1</next>",
    "JVM_OPTS=\"$JVM_OPTS -Dx.password:$x\n  LEAK1\"\nnext=1",  # $ after a colon, inside a shell quote
    "  <secretKey attr=\"1\">LEAK1</secretKey>\n<next>1</next>",
    "ldap_password=x\\\r\n  LEAK1\r\nnext=1",  # a CRLF line continued by its backslash
    "password: $abc\n  LEAK1\nother: 1",  # a YAML plain value starting with $: its more indented lines
    "password: $(x\n  LEAK1\nother: 1",
    "password: &a\n- LEAK1\nother: 1",  # an anchor alone: the list under the key
    "password: !!seq\n- LEAK1\nother: 1",
]


@pytest.mark.parametrize("text", [
    "keystore_password: |\n  LEAK1\n  LEAK2\nnext: 1",
    "keystore_password: >-\n  LEAK1\n\n  LEAK2\nnext: 1",
    "keystore_password: |+2\n    LEAK1\n  LEAK2\nnext: 1",
    "  keystore_password: LEAK1\n    LEAK2\n  next: 1",  # a plain value on more indented lines
    "keystore_password: \"LEAK1\nLEAK2\"\nnext: 1",  # a quote left open: its lines, whatever their indent
    "keystore_password: \"LEAK1\n  LEAK2\n  LEAK3\"\nnext: 1",
    "opts: {keystore_password: \"LEAK1, x\n  LEAK2\"}\nnext: 1",
    "keystore_password: \"LEAK1 \\\"\n  LEAK2 \\\n  LEAK3\"\nnext: 1",
    "keystore_password: 'LEAK1 it''s\n  LEAK2'\nnext: 1",
    "keystore_password: [LEAK1,\n  LEAK2]\nnext: 1",
    "keystore_password: {a: LEAK1,\n  b: \"LEAK2 }\"\n  }\nnext: 1",
    "opts: {a: 1, keystore_password: \"LEAK1\n  LEAK2\"}\nnext: 1",
    "opts: {keystore_password: x, other_password: \"LEAK1\n  LEAK2\"}\nnext: 1",  # the line's last secret
    "keystore_password:\n  LEAK1\n  LEAK2\nnext: 1",  # the value under the key
    "keystore_password:  # rotated\n  a: LEAK1\n  b: LEAK2\nnext: 1",
    "secrets:\n- LEAK1\n- LEAK2\nnext: 1",  # a list at the key's column
    "export KEYSTORE_PASSWORD=\"LEAK1\nLEAK2\"\nnext=1",  # cassandra-env.sh
    "KEYSTORE_PASSWORD='LEAK1\nLEAK2'\nnext=1",
    "-Djavax.net.ssl.keyStorePassword=\"LEAK1\nLEAK2\"\n-Xss256k",  # jvm options
    "keystore.password=LEAK1\\\n    LEAK2\\\\\\\n    LEAK3\nnext=1",  # a properties file's continued line
    "export KS_PASSWORD=x TS_PASSWORD=\"LEAK1\nLEAK2\"\nnext=1",  # the line's last secret
    "KEY_PASSWORD=\"LEAK1\"\\\nLEAK2\nnext=1",  # a quote closed, then a backslash
    "KEY_PASSWORD=\"LEAK1\nLEAK2\"\\\nLEAK3\nnext=1",
    "PASSWORDS=(LEAK1\n  LEAK2)\nnext=1",  # a shell array
    "opts: {keystore_password: a\n  LEAK1, x: 1}\nnext: 1",  # a flow mapping's plain value on the next line
    "opts: {keystore_password: [a,\n  LEAK1], x: 1}\nnext: 1",
] + BLOCK_SECRETS_AFTER_AUDIT)
def test_mask_every_line_of_a_secret_value(text):
    masked = out.mask(text)
    assert "LEAK" not in masked, masked
    assert masked.rsplit("\n", maxsplit=1)[-1] == text.rsplit("\n", maxsplit=1)[-1]  # the next setting shown
    assert len(masked.split("\n")) == len(text.split("\n"))


@pytest.mark.parametrize("text", [
    "- keystore_password: x\n  store_type: JCEKS",  # a list item's next key
    "keystore_password: \"x\"\n  next: 1",  # closed on its line
    "keystore_password: x\nnext: 1",
    "keystore_password: {a: x}\n  next: 1",
    "opts: {keystore_password: x,\n  next: 1}",
    "keystore_password=x\n  next=1",  # not YAML: an indented line is another setting
    "keystore_password=x\\\\\nnext=1",  # an even number of backslashes: not continued
    "keystore_password: [it's, x]\n  next: 1",  # a quote inside a word: not one that opens a value
    "# keystore_password: x\n# next: 1",
    "keystore_password: [x, y]  # see [1\n  next: 1",  # a comment: no bracket, no quote in it
    "JVM_OPTS=\"$JVM_OPTS -Djavax.net.ssl.trustStorePassword=\"\nJVM_OPTS=\"$JVM_OPTS -Dx=1\"",  # its quote closes
    "echo \"the password: \"\n  next",  # (inside a quote: not a YAML key)
    "# keystore_password: \"see the vault\nnum_tokens: 8",  # a comment goes on to no other line
    "opts: {keystore_password: a, x: 1,\n  next: 1}",
])
def test_mask_stops_where_the_secret_value_does(text):
    masked = out.mask(text).split("\n")
    assert masked[0] != text.split("\n")[0] or "x" not in masked[0]
    assert masked[1] == text.split("\n")[1]


@pytest.mark.parametrize("text", [
    "ldap.password=LEAK1 #x\\\n  LEAK2\nnext=1",  # (a # inside a properties value)
    "ldap_password: LEAK1\\\nLEAK2\nnext: 1",  # a properties file with key: value
    "keystore_password: \"LEAK1\\\nLEAK2\"\nnext: 1",
])
def test_mask_a_line_after_a_backslash(text):
    masked = out.mask(text)
    assert "LEAK" not in masked and masked.endswith("\nnext" + text.split("\nnext")[1])


def test_mask_a_long_word_is_quick():
    start = time.time()
    for text in ("a" * 4000, "password" * 500, "x_" * 2000 + "=1"):
        out.mask(text)
    assert time.time() - start < 5


def test_mask_an_element_s_text_only():
    assert out.mask("<password>LEAK1</password> x") == "<password>****</password> x"
    assert out.mask("<password></password>") == "<password></password>"
    assert out.mask("<password> LEAK1 </password>\r") == "<password>****</password>\r"
    for text in ("<password><![CDATA[LEAK1 a</b]]></password>", "<password><!-- c -->LEAK1</password>",
                 "<password>a<b/>LEAK1</password>", "<password>a<b>x</b>LEAK1</password>",
                 "<password><!-- </x> -->LEAK1</password>"):  # its text up to its own closing tag
        assert out.mask(text) == "<password>****</password>", text


def test_mask_a_line_of_many_secrets_is_quick():
    start = time.time()
    assert out.mask("<password>" + "<![CDATA[" * 30000).startswith("<password>****")  # (unclosed: to the end)
    assert "LEAK" not in out.mask("{" + ", ".join('"password%d": "LEAK"' % i for i in range(2000)) + "}")
    assert time.time() - start < 5


def test_a_node_range_longer_than_its_room_cut_after_its_dots():
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import wrap_list
    text = "db-node-1.example.net..db-node-3.example.net, db-node-5.example.net"
    assert wrap_list(text, 30) == ["db-node-1.example.net..", "db-node-3.example.net,", "db-node-5.example.net"]
    assert wrap_list(text, 50) == ["db-node-1.example.net..db-node-3.example.net,", "db-node-5.example.net"]
    assert wrap_list("node1..node3", 8) == ["node1..", "node3"]  # (no space added inside a range)
