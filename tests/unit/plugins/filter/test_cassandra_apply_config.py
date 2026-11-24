from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import base64

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
    READ_AHEAD_LIVE, _line_settings, _masked, _patched, _yaml_settings, cassandra_apply_config_diff,
    cassandra_apply_config_outcomes, cassandra_apply_config_recap)
from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import preview


def role_diff(tmp_path, live, new, name="cassandra.yaml"):
    """The diff cassandra_config's preview gives (its own reader: masked as the role masks)."""
    d = tmp_path / ("role-diff-%d" % len(list(tmp_path.iterdir())))
    d.mkdir()
    return preview(d, name, live, new)


YAML = ("cluster_name: 'Demo'\n"
        "max_hint_window: 3h\n"
        "client_encryption_options:\n"
        "  enabled: false\n"
        "  keystore_password: s3cret\n"
        "seed_provider:\n"
        "  - class_name: org.apache.cassandra.locator.SimpleSeedProvider\n"
        "    parameters:\n"
        "      - seeds: \"10.0.0.1:7000\"\n"
        "data_file_directories:\n"
        "  - /var/lib/cassandra/data\n"
        "concurrent_reads: 32  # reads\n")
JVM = "# G1\n-Xss256k\n-XX:+UseG1GC\n"
ENV = 'MAX_HEAP_SIZE="8G"\nJVM_OPTS="$JVM_OPTS -Dcassandra.jmx.local.port=7199"\n'


def b64(text):
    return base64.b64encode(text.encode()).decode()


def stat(owner, group="cassandra", mode="0640"):
    return {"exists": True, "pw_name": owner, "gr_name": group, "mode": mode}


class Node(object):
    """A node's compare data as apply_config gathers it: its items, its live files, its stats."""

    def __init__(self, tmp_path, name, files=None, targets=None, perms=None, dirs=None, data_owner="cassandra",
                 env_owner="cassandra"):
        files = dict({"cassandra.yaml": YAML, "jvm-server.options": JVM, "cassandra-env.sh": ENV}, **(files or {}))
        targets = dict({"cassandra.yaml": YAML, "jvm-server.options": JVM, "cassandra-env.sh": ENV}, **(targets or {}))
        self.name, self.items = name, []
        for f in sorted(targets):
            if files.get(f) != targets[f]:
                self.items.append({"item": "/etc/cassandra/conf/" + f, "before": "current", "after": "1 line(s) changed",
                                   "diff": role_diff(tmp_path, files.get(f), targets[f], f)})
        self.items += list(perms or []) + list(dirs or [])
        self.live = [{"cassandra_apply_config_file": f, "content": b64(text)} for f, text in files.items() if text is not None]
        self.stats = [{"cassandra_config_file": "cassandra-env.sh", "stat": stat(env_owner, "cassandraops")},
                      {"cassandra_config_file": "cassandra.yaml", "stat": stat("root")}]
        self.dir_stats = [{"item": ["data dir", "/var/lib/cassandra/data"], "stat": stat(data_owner, mode="0750")}]

    def data(self):
        return {"name": self.name, "items": self.items, "live": self.live, "stats": self.stats, "dir_stats": self.dir_stats}


def view(*nodes):
    return cassandra_apply_config_diff([n.data() for n in nodes])


def test_the_four_drifts_per_file_from_to_the_nodes_last(tmp_path):
    env_perms = [{"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)", "before": "root:root 0640",
                  "after": "cassandra:cassandraops 0640"}]
    data_dir = [{"item": "data dir /var/lib/cassandra/data (owner:group mode)", "path": "/var/lib/cassandra/data",
                 "dir": "data dir", "before": "root:root 0750", "after": "cassandra:cassandra 0750"}]
    nodes = [Node(tmp_path, "node1"),
             Node(tmp_path, "node2", files={"jvm-server.options": JVM + "-Ddemo.drift=1\n"}),
             Node(tmp_path, "node3", dirs=data_dir, data_owner="root"),
             Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("3h", "6h")}, perms=env_perms, env_owner="root"),
             Node(tmp_path, "node5")]
    # one table: the file on its first row, the setting, its change, the nodes that change (not the ones on the
    # inventory's value); the owner alone when the mode is the same
    assert view(*nodes) == [
        'Item               Node       Inventory',
        'cassandra.yaml  on node4',
        '  max_hint_window  6h         3h',
        'cassandra-env.sh  on node4',
        '  owner            root:root  cassandra:cassandraops',
        'jvm-server.options  on node2',
        '  -Ddemo.drift=1   present    absent',
        'data dir /var/lib/cassandra/data  on node3',
        '  owner            root:root  cassandra:cassandra']


def test_every_other_value_with_its_nodes_and_a_value_the_inventory_adds(tmp_path):
    nodes = [Node(tmp_path, "node%d" % i) for i in (1, 2)]
    nodes += [Node(tmp_path, "node3", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 64")}),
              Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")}),
              Node(tmp_path, "node5", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")})]
    lines = view(*nodes)
    assert lines == [
        'Item                Node  Inventory  Nodes',
        'cassandra.yaml',
        '  concurrent_reads  64    32         node3',
        '                    48    32         node4, node5']
    # an option the inventory adds: added on the nodes without it
    added = JVM + "-Dcassandra.ring_delay_ms=30000\n"
    lines = view(Node(tmp_path, "node1", files={"jvm-server.options": added}, targets={"jvm-server.options": added}),
                 Node(tmp_path, "node2", targets={"jvm-server.options": added}))
    assert lines == [
        'Item                               Node    Inventory',
        'jvm-server.options  on node2',
        '  -Dcassandra.ring_delay_ms=30000  absent  present']


def test_jvm_options_and_env_lines_as_settings(tmp_path):
    lines = view(Node(tmp_path, "node1"),
                 Node(tmp_path, "node2", files={"jvm-server.options": JVM.replace("256k", "512k"),
                                                "cassandra-env.sh": ENV.replace("8G", "16G")}))
    assert lines == [
        'Item             Node   Inventory',
        'cassandra-env.sh  on node2',
        '  MAX_HEAP_SIZE  "16G"  "8G"',
        'jvm-server.options  on node2',
        '  -Xss           512k   256k']


def test_a_value_set_per_node_lists_no_node_on_it(tmp_path):
    # listen_address is each node's own: the nodes not changing are not "on" node3's value
    def yml(address):
        return "listen_address: %s\n" % address + YAML
    nodes = [Node(tmp_path, "node%d" % i, files={"cassandra.yaml": yml("10.0.0.%d" % i)},
                  targets={"cassandra.yaml": yml("10.0.0.%d" % i)}) for i in (1, 2)]
    nodes.append(Node(tmp_path, "node3", files={"cassandra.yaml": yml("10.0.0.9")}, targets={"cassandra.yaml": yml("10.0.0.3")}))
    assert view(*nodes) == [
        'Item              Node      Inventory',
        'cassandra.yaml  on node3',
        '  listen_address  10.0.0.9  10.0.0.3']


def test_secrets_masked_a_changed_password_still_said(tmp_path):
    jvm = JVM + "-Djavax.net.ssl.keyStorePassword=jvmpass\n"
    nodes = [Node(tmp_path, "node1", files={"jvm-server.options": jvm}, targets={"jvm-server.options": jvm}),
             Node(tmp_path, "node2", files={"cassandra.yaml": YAML.replace("s3cret", "0ldpass"), "jvm-server.options": jvm},
                  targets={"jvm-server.options": jvm.replace("jvmpass", "newjvm")}),
             Node(tmp_path, "node3", files={"cassandra.yaml": YAML + "ssl_private_key: abc\n", "jvm-server.options": jvm},
                  targets={"cassandra.yaml": YAML + "ssl_private_key: def\n", "jvm-server.options": jvm})]
    lines = view(*nodes)
    text = "\n".join(lines)
    for secret in ("s3cret", "0ldpass", "jvmpass", "newjvm", "abc", "def"):
        assert secret not in text
    # a changed secret said, its values masked on both sides
    assert lines == [
        'Item                                           Node  Inventory       Nodes',
        'cassandra.yaml',
        '  client_encryption_options.keystore_password  ****  **** (another)  node2',
        '  ssl_private_key                              ****  **** (another)  node3',
        'jvm-server.options',
        '  -Djavax.net.ssl.keyStorePassword=****        ****  **** (another)  node2']
    # (the node lists in one place: every setting's row, never some on a file's row; e2e, review of the plan)


def test_nested_keys_lists_and_layout_only(tmp_path):
    target = YAML.replace("enabled: false", "enabled: true").replace("10.0.0.1:7000", "10.0.0.1:7000,10.0.0.2:7000")
    target = target.replace("  - /var/lib/cassandra/data\n", "  - /var/lib/cassandra/data\n  - /data2\n")
    nodes = [Node(tmp_path, "node1", targets={"cassandra.yaml": target}),
             Node(tmp_path, "node2", targets={"cassandra.yaml": YAML.replace("# reads", "# the reads")})]
    assert view(*nodes) == [
        'Item                                 Node                     Inventory                        Nodes',
        'cassandra.yaml',
        '  client_encryption_options.enabled  false                    true                             node1',
        '  seed_provider.parameters.seeds     10.0.0.1:7000            10.0.0.1:7000,10.0.0.2:7000      node1',
        '  data_file_directories              /var/lib/cassandra/data  /var/lib/cassandra/data, /data2  node1',
        "  comments or layout                 other                    the role's (no setting changes)  node2"]


def test_a_new_file_a_line_ending_and_a_file_changed_since_compared(tmp_path):
    crlf = ENV.replace('"8G"\n', '"8G"\r\n')
    nodes = [Node(tmp_path, "node1"),
             Node(tmp_path, "node2", files={"cassandra-env.sh": crlf}),
             Node(tmp_path, "node3", files={"jvm17-server.options": None}, targets={"jvm17-server.options": "-Xss1m\n"})]
    stale = Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("3h", "6h")})
    stale.live[0]["content"] = b64("short: 1\n")  # rewritten since the compare: the diff does not fit
    lines = view(*(nodes + [stale]))
    # a node not read: a line of its own, not a row of the table
    assert lines == [
        'Item             Node    Inventory',
        'cassandra.yaml  node4: not read, or changed since it was compared: run apply_config again',
        'cassandra-env.sh  on node2',
        '  MAX_HEAP_SIZE  "8G"\\r  "8G"',
        'jvm17-server.options  on node3',
        '  file           absent  written whole']


def test_a_live_file_not_read_is_not_said_new(tmp_path):
    unread = Node(tmp_path, "node2", files={"cassandra.yaml": YAML.replace("3h", "6h")})
    unread.live = [r for r in unread.live if r["cassandra_apply_config_file"] != "cassandra.yaml"]
    assert view(Node(tmp_path, "node1"), unread) == [
        'cassandra.yaml  node2: not read, or changed since it was compared: run apply_config again']


def test_nothing_differs():
    assert cassandra_apply_config_diff([{"name": "node1", "items": []}, {"name": "node2"}]) == []
    assert cassandra_apply_config_diff([]) == []


def test_the_conf_dir_alternative():
    items = [{"item": "/etc/cassandra/conf", "before": "/etc/cassandra/default.conf", "after": "/etc/cassandra/site"}]
    assert cassandra_apply_config_diff([{"name": "node1", "items": items}]) == [
        'Item         Node                         Inventory',
        '/etc/cassandra/conf  on node1',
        '  points to  /etc/cassandra/default.conf  /etc/cassandra/site']


def test_a_change_next_to_a_secret_on_several_lines_is_read(tmp_path):
    # the live lines masked as the role's diff masks them (a secret's value lines, sse_c_key): the diff fits them
    def yml(tokens):
        return YAML + "sse_c_key: abc\nldap_password: |\n  LINE1\n  LINE2\nnum_tokens: %d\n" % tokens
    lines = view(Node(tmp_path, "node1", files={"cassandra.yaml": yml(16)}, targets={"cassandra.yaml": yml(8)}))
    assert lines == [
        'Item          Node  Inventory',
        'cassandra.yaml  on node1',
        '  num_tokens  16    8']
    assert not [line for line in lines if "abc" in line or "LINE" in line]


def test_a_masked_change_is_said_on_its_own_key(tmp_path):
    # a comment line with a masked value: no secret change; a secret mapping's lines, a line going on from a
    # secret's: its key's change
    cases = [("ks_password: x\n# truststore_password: cassandra\nb: 2\n", "cassandra.yaml", "# truststore_password", None),
             ("my_secret:\n  user: a\n  pass: b\n", "cassandra.yaml", "pass: b", "my_secret"),
             ('JVM_OPTS="$JVM_OPTS -Dx.password=abc \\\n  -Dother=1"\n', "cassandra-env.sh", "-Dother=1", "JVM_OPTS"),
             ("a: 1 # ks_password: x\nb: 2\n", "cassandra.yaml", "# ks_password: x", None),  # (in a comment)
             ("-Dx.password=a\n-Xss256k\n-Dx.password=b\n", "jvm-server.options", "-Dx.password=b", "-Dx.password"),
             ("  -Dx.password=a\n-Xss256k\n  -Dx.password=b\n", "jvm-server.options", "-Dx.password=b", "-Dx.password"),
             ("ldap_password: |\n  L1\n\n  L3\nb: 2\n", "cassandra.yaml", "L3", "ldap_password")]  # (a blank line in it)
    for n, (live, name, old, key) in enumerate(cases):
        new = live.replace(old, old[:-1] + "2" if old[-1] != "d" else "# other")
        (tmp_path / str(n)).mkdir()
        lines = view(Node(tmp_path / str(n), "node1", files={name: live}, targets={name: new}))
        said = [line for line in lines if "**** (another)" in line]
        assert bool(said) == (key is not None), (live, lines)
        if key:
            assert [line for line in said if line.split()[0].startswith(key)], lines
            assert "(empty)" not in "\n".join(lines)  # (the value under the key: ****)


def test_a_block_value_is_its_key_s_value(tmp_path):
    # a PEM key or a certificate over several lines (4.1+ ssl_context_factory): a change of it is said on its key,
    # a secret's lines never shown, nor taken for keys
    def yml(key, cert):
        return (YAML + "server_encryption_options:\n  ssl_context_factory:\n    parameters:\n"
                "      private_key: |\n        -----BEGIN PRIVATE KEY-----\n        %s\n        -----END PRIVATE KEY-----\n"
                "      trusted_certificates: |\n        -----BEGIN CERTIFICATE-----\n        %s\n" % (key, cert))
    nodes = [Node(tmp_path, "node1", files={"cassandra.yaml": yml("MIIEold: SECRET", "CERT1")},
                  targets={"cassandra.yaml": yml("MIIEnew: SECRET", "CERT2")}),
             Node(tmp_path, "node2", files={"cassandra.yaml": yml("MIIEnew: SECRET", "CERT2")},
                  targets={"cassandra.yaml": yml("MIIEnew: SECRET", "CERT2")})]
    lines = view(*nodes)
    assert not [line for line in lines if "MIIE" in line or "SECRET" in line]
    assert lines[1].split() == ["cassandra.yaml", "on", "node1"]
    assert lines[2].split() == ["server_encryption_options.ssl_context_factory.parameters.private_key", "****", "****",
                                "(another)"]
    words = lines[3].split()
    assert words[0] == "server_encryption_options.ssl_context_factory.parameters.trusted_certificates"
    assert words[1:4] == ["-----BEGIN", "CERTIFICATE-----", "..."] and words[7:10] == words[1:4]
    assert words[6] != words[12]  # their digests (the value before, the value after)


def test_the_parsers():
    assert _yaml_settings(YAML.splitlines()) == {
        "cluster_name": ["Demo", 0], "max_hint_window": ["3h", 1], "client_encryption_options.enabled": ["false", 3],
        "client_encryption_options.keystore_password": ["s3cret", 4],
        "seed_provider.class_name": ["org.apache.cassandra.locator.SimpleSeedProvider", 6],
        "seed_provider.parameters.seeds": ["10.0.0.1:7000", 8], "data_file_directories": ["/var/lib/cassandra/data", 9],
        "concurrent_reads": ["32", 11]}
    found = _line_settings(["-Xmx4G", "-XX:+UseG1GC", "-Dx=1", "-Dx=2", "# c", ""], "jvm-server.options")
    assert found == {"-Xmx": ["4G", 0, "-Xmx4G"], "-XX:+UseG1GC": [None, 1, "-XX:+UseG1GC"],
                     "-Dx=1": [None, 2, "-Dx=1"], "-Dx=2": [None, 3, "-Dx=2"]}  # a key twice: by its lines
    assert _line_settings(["dc = dc1", "export X=1", "if true; then"], "cassandra-rackdc.properties") == {
        "dc": ["dc1", 0, "dc = dc1"], "X": ["1", 1, "export X=1"], "if true; then": [None, 2, "if true; then"]}
    assert _masked(["keystore_password: x", "a: 1"]) == ["keystore_password: ****", "a: 1"]
    assert _yaml_settings(["a: 'x'  # c", 'b: "y" # c', "c: z # c"]) == {"a": ["x", 0], "b": ["y", 1], "c": ["z", 2]}
    # a comment after a key with children: its children stay keys
    assert _yaml_settings(["opts: # TLS", "  enabled: true", "tags: &t", "  x: 1"]) == {
        "opts.enabled": ["true", 1], "tags.x": ["1", 3]}


def test_the_patch():
    diff = "--- a (live)\n+++ (new)\n@@ -2,2 +2,3 @@\n b\n-c\n+C\n+D\n"
    assert _patched(["a", "b", "c", "e"], diff) == (["a", "b", "C", "D", "e"], {2, 3})
    assert _patched([], "--- a (live)\n+++ (new)\n@@ -0,0 +1,1 @@\n+x\n") == (["x"], {0})
    assert _patched(["a"], diff) is None  # past its end: changed since
    assert _patched(["a", "b", "x", "e"], diff) is None  # a line the diff removes is not there: changed since
    assert _patched(["a", "B", "c", "e"], diff) is None  # nor one it keeps
    # the command module strips the "\r" ending the diff: its last line still fits
    assert _patched(["a", "x\r"], "--- a\n+++ b\n@@ -2 +1,0 @@\n-x") == (["a"], set())


# what each node gets

def outcome_nodes():
    return [{"name": "node1", "todo": False, "result": "nothing to apply"},
            {"name": "node2", "todo": True, "then": "restart", "done": True},
            {"name": "node3", "todo": True, "then": "none", "done": True,
             "notes": ["WARNING  data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)"]},
            {"name": "node4", "todo": True, "then": "restart", "done": True},
            {"name": "node5", "todo": False, "result": "nothing to apply"}]


def test_the_outcomes_grouped_in_the_plan():
    # the nodes changed first, in inventory order of their first node
    assert cassandra_apply_config_outcomes(outcome_nodes(), plan=True) == [
        'node2, node4  write + restart',
        'node3         write, no restart (owner or mode only, read at start)',
        'node1, node5  up to date']
    nodes = [{"name": "node1", "todo": True, "then": "start"}, {"name": "node2", "todo": True, "then": "write"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        'node1  write + start (Cassandra not running: started once written (down over max_hint_window? repair it))',
        'node2  write, left stopped (Cassandra stopped: left stopped, reads its config when it starts)']
    # left stopped because its unit failed (nothing written that would start it): not "stopped on purpose"
    nodes = [{"name": "node3", "todo": True, "then": "write", "unit_failed": True}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        'node3  write, left stopped (its cassandra unit failed: left as it is)']
    # a file written back as the running Cassandra started with it: its note says why (not "an owner... only")
    note = "jvm-server.options written back as the running Cassandra started with it: no restart for it"
    nodes = [{"name": "node1", "todo": True, "then": "none", "notes": [note]}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        'node1  write, no restart',
        '  jvm-server.options written back as the running Cassandra started with it: no restart for it']


def test_the_check_recap_shows_the_settings_then_the_outcomes():
    diff = ["cassandra.yaml  max_hint_window  6h -> 3h  node2"]
    assert cassandra_apply_config_recap(outcome_nodes(), check=True, cluster="my_cluster", seconds=75, diff=diff,
                                        planned=["node1", "node2", "node3", "node4", "node5"]) == [
                                            'CHECK  apply_config  my_cluster  2 would restart, 1 would apply, 2 up to date  1m15s',
                                            '  nothing was changed (--check)',
                                            '',
                                            'DIFFERENCES FOUND',
                                            '  cassandra.yaml  max_hint_window  6h -> 3h  node2',
                                            '',
                                            'WOULD DO',
                                            '  node2, node4  would apply, then restart',
                                            '  node3         would apply, no restart',
                                            '  node1, node5  up to date',
                                            '',
                                            '1 warning in the plan above (node3).']  # said there, not under node3 again


def test_the_real_run_recap_says_the_outcomes_only():
    nodes = outcome_nodes()
    nodes[3] = {"name": "node4", "todo": True, "done": False, "result": "apply_config FAILED after 30s: no answer"}
    nodes.append({"name": "node6", "todo": True, "result": "not done (the run stopped before it)"})
    nodes.append({"name": "node7", "result": "not in this run (--limit)"})
    assert cassandra_apply_config_recap(nodes, cluster="my_cluster", diff=["cassandra.yaml", "  x"],
                                        planned=["node%d" % i for i in range(1, 8)]) == [
                                            'FAILED  apply_config  my_cluster  1 restarted, 1 applied, 1 failed, 2 up to date,'
                                            ' 1 not done, 1 not in this run',
                                            'node2         applied, restarted',
                                            'node3         applied, no restart',
                                            'node4         apply_config FAILED after 30s: no answer',
                                            'node1, node5  up to date',
                                            'node6         not done (the run stopped before it)',
                                            'node7         not in this run (--limit)',
                                            '',
                                            '1 warning in the plan above (node3).']


def test_nothing_to_apply_recap():
    nodes = [{"name": "node%d" % i, "todo": False, "result": "nothing to apply"} for i in (1, 2, 3)]
    assert cassandra_apply_config_recap(nodes) == [
        'DONE  apply_config  3 up to date',
        'node1..node3  up to date']
    assert cassandra_apply_config_recap(nodes, check=True, diff=[]) == [
        'CHECK  apply_config  3 up to date',
        '  nothing was changed (--check)',
        '',
        'WOULD DO',
        '  node1..node3  up to date']


def test_the_screen_sections_are_blocks_of_their_own():
    text = cassandra_screen({"operation": "apply_config", "summary": "apply the config on node2",
                             "sections": [[{"pre": ["cassandra.yaml", "  x: 1"]}], [], [{"pre": ["node2  would apply"]}]]})
    assert text == "APPLY CONFIG  apply the config on node2\n\n  cassandra.yaml\n    x: 1\n\n  node2  would apply"


def test_the_settings_lines_of_several_values():
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import settings_lines
    assert settings_lines(["f", ("x", [("1", "node1", ""), ("2", "node2", "differs")])]) == [
        "f", "  x: 1   node1", "     2   node2   differs"]


def test_a_large_cluster_by_node_groups(tmp_path):
    """From 12 nodes on, the node lists by groups defined first: 'G1', 'G2 + db074'."""
    a = ["db1%d" % i for i in range(1, 7)] + ["db2%d" % i for i in range(1, 7)]
    b = ["db07%d" % i for i in range(1, 7)]
    nodes = [Node(tmp_path, n, files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 64")}) for n in a]
    nodes += [Node(tmp_path, n, files={"cassandra.yaml": YAML.replace("3h", "6h")} if n != "db074" else
                   {"cassandra.yaml": YAML.replace("reads: 32", "reads: 64").replace("3h", "6h")}) for n in b]
    lines = view(*nodes)
    assert lines == [
        'NODE GROUPS',
        '  G1 = db11..db16, db21..db26 (12 nodes)',
        '  G2 = db071..db073, db075, db076 (5 nodes)',
        '',
        'Item                Node  Inventory  Nodes',
        'cassandra.yaml',
        '  concurrent_reads  64    32         G1 + db074',
        '  max_hint_window   6h    3h         G2 + db074']
    # under 12 nodes: their names, as before
    assert "NODE GROUPS" not in view(*nodes[:11])


# the OS settings (cassandra_linux's items): after the config's, the same rows

def test_the_os_settings_that_differ_after_the_config(tmp_path):
    node1 = Node(tmp_path, "node1", files={"cassandra.yaml": YAML.replace("3h", "6h")}).data()
    node1["os"] = [{"item": "read_ahead_kb vg_data/cassandra", "before": "128", "after": "4"},
                   {"item": "sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)", "before": "60", "after": "1"}]
    node2 = Node(tmp_path, "node2").data()
    node2["os"] = [{"item": "read_ahead_kb vg_data/cassandra", "before": "256", "after": "4"}]
    node3 = Node(tmp_path, "node3").data()
    assert cassandra_apply_config_diff([node1, node2, node3]) == [
        'Item                                                      Node    Inventory  Nodes',
        'cassandra.yaml',
        '  max_hint_window                                         6h      3h         node1',
        'OS settings',
        '  read-ahead vg_data/cassandra                            128 KB  4 KB       node1',
        '                                                          256 KB  4 KB       node2',
        '  sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)  60      1          node1']


def test_the_os_settings_alone_no_restart():
    nodes = [{"name": "node1", "todo": False, "os": True, "result": "not done (the run stopped before it)",
              "notes": ["read-ahead set live: the files Cassandra has open keep the old one until it restarts"]},
             {"name": "node2", "todo": True, "os": True, "then": "restart", "done": True},
             {"name": "node3", "todo": False, "result": "nothing to apply"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        'node2  write + restart',
        'node1  OS settings, live (no restart)',  # (never "OS settings" twice)
        '  read-ahead set live: the files Cassandra has open keep the old one until it restarts',
        'node3  up to date']
    nodes[0]["os_done"] = True
    assert cassandra_apply_config_recap(nodes, check=True)[0] == "CHECK  apply_config  1 would restart, 1 would apply, 1 up to date"
    assert cassandra_apply_config_recap(nodes)[:2] == [
        'DONE  apply_config  1 restarted, 1 applied, 1 up to date',
        'node2  applied, restarted']
    # the run stopped before the OS settings: not said applied
    del nodes[0]["os_done"]
    assert "node1  not done (the run stopped before it)" in cassandra_apply_config_recap(nodes)
    # its config not reached, its OS settings applied: said
    nodes[1].update({"done": False, "os_done": True, "result": "not done (the run stopped before it)"})
    assert "node2  not done (the run stopped before it), its OS settings applied" in cassandra_apply_config_recap(nodes)


def test_a_node_not_running_said_so():
    # h-n4 stopped: "would apply, no restart" (its OS settings only), "up to date" (in sync): no word of
    # Cassandra not running there
    nodes = [{"name": "node1", "todo": False, "os": True, "then": "write"},
             {"name": "node2", "todo": False, "then": "write"},
             {"name": "node3", "todo": False, "then": "none"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True, check=True) == [
        "node1  would apply, left stopped", "node2  up to date, Cassandra not running", "node3  up to date"]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        "node1  OS settings, live (left stopped)", "node2  up to date, Cassandra not running",
        "node3  up to date"]
    nodes[0]["os_done"] = True
    assert cassandra_apply_config_recap(nodes, check=True)[0] == "CHECK  apply_config  1 would apply, 2 up to date"
    assert "node1  applied, left stopped" in cassandra_apply_config_recap(nodes)


def test_an_os_value_unknown_before():
    change = {"item": "LVM read-ahead vg/lv (sectors)", "before": None, "after": "8"}
    lines = cassandra_apply_config_diff([{"name": "node1", "os": [change]}])
    assert lines == [
        'Item                    Node     Inventory',
        'OS settings  on node1',
        '  LVM read-ahead vg/lv  unknown  4 KB']


def test_a_key_added_by_hand_alone_is_a_difference_and_not_kept(tmp_path):
    # a running node's cassandra.yaml with only an unknown key appended (lineinfile): not the same settings as the
    # role's (cassandra_config keeps the file otherwise), shown as a key the inventory does not carry
    from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import cassandra_same_settings
    live = YAML + "fake_option: 42\n"
    assert not cassandra_same_settings(live, YAML, "cassandra.yaml")
    assert view(Node(tmp_path, "node1", files={"cassandra.yaml": live}), Node(tmp_path, "node2")) == [
        'Item           Node  Inventory',
        'cassandra.yaml  on node1',
        '  fake_option  42    absent']


def os_nodes():
    """Five nodes, all but node4 with their data disk sdb and their LVM volume (another dm-N on each) to tune."""
    nodes = []
    for i in range(1, 6):
        items = [] if i == 4 else [
            {"item": "read_ahead_kb sdb", "before": "4096", "after": "4"},
            {"item": "read_ahead_kb vgdata-lvdata (dm-%d)" % (i + 2), "before": "4096", "after": "4"},
            {"item": "/etc/udev/rules.d/64-cassandra-data-disk.rules", "before": "absent or different", "after": "present"}]
        nodes.append({"name": "db-node-%d.example.net" % i, "items": [], "os": items})
    return nodes


def test_os_settings_by_the_stable_device_name_at_the_terminal_width():
    # an LVM volume one setting whatever its dm-N on each node; the read-ahead in KB; every node the same: the
    # nodes once, on the file's row
    nodes = "db-node-1.example.net..db-node-3.example.net, db-node-5.example.net"
    udev = "/etc/udev/rules.d/64-cassandra-data-disk.rules"
    assert cassandra_apply_config_diff(os_nodes(), width=200) == [
        "Item" + " " * len(udev) + "Node                 Inventory",
        "OS settings  on " + nodes,
        "  %s  4096 KB              4 KB" % "read-ahead sdb".ljust(len(udev)),
        "  %s  4096 KB              4 KB" % "read-ahead vgdata-lvdata".ljust(len(udev)),
        "  %s  absent or different  present" % udev]
    for width in (80, 100, 118):
        lines = cassandra_apply_config_diff(os_nodes(), width=width)
        assert all(len(line) <= width for line in lines), width
        assert "absent or different  present" in "\n".join(lines) and "db-node-5.example.net" in "\n".join(lines)


def test_rows_too_wide_have_their_nodes_under_the_node_value_every_row_the_same():
    # a narrow terminal: every row's nodes on its next line, at the column of the node's values; the
    # values never cut, in aligned Node and Inventory columns, the read-ahead with its unit
    swappiness = "sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)"
    nodes = [{"name": "h-n1", "items": [], "os": [{"item": "read_ahead_kb sdz", "before": "128", "after": "4"}]},
             {"name": "h-n2", "items": [], "os": [{"item": swappiness, "before": "60", "after": "1"}]}]
    at = 2 + len(swappiness) + 2
    assert cassandra_apply_config_diff(nodes, width=80) == [
        "Item".ljust(at) + "Node    Inventory",
        "OS settings",
        "  %s  128 KB  4 KB" % "read-ahead sdz".ljust(len(swappiness)), " " * at + "h-n1",
        "  %s  60      1" % swappiness, " " * at + "h-n2"]
    # wide enough: each row on its line
    assert cassandra_apply_config_diff(nodes, width=120)[2].endswith("128 KB  4 KB       h-n1")
    # a list of nodes wider than the room: cut after its commas, never inside a name
    many = ["cassandra-db-node-%d.example.net" % i for i in (1, 3, 5, 7)]
    lines = cassandra_apply_config_diff([{"name": n, "items": [], "os": [{"item": "read_ahead_kb sdb", "before": "4096",
                                                                          "after": "4"}]} for n in many]
                                        + [{"name": "cassandra-db-node-9.example.net", "items": []}], width=60)
    assert lines[1] == "OS settings  on cassandra-db-node-1.example.net," and "  read-ahead sdb  4096 KB  4 KB" in lines
    assert all(len(line) <= 60 for line in lines) and all(n in "\n".join(lines) for n in many), lines


def test_the_plan_indents_the_tables_inside_the_terminal_width():
    # a real run's plan shows the differences and the outcomes 2 columns in (cassandra_screen): built with margin=2,
    # the table stays inside the width
    swappiness = "sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)"
    nodes = [{"name": "h-n1", "items": [], "os": [{"item": "read_ahead_kb sdz", "before": "128", "after": "4"}]},
             {"name": "h-n2", "items": [], "os": [{"item": swappiness, "before": "60", "after": "1"}]}]
    width = max(len(line) for line in cassandra_apply_config_diff(nodes))  # one line per row, exactly
    assert cassandra_apply_config_diff(nodes, width=width) == cassandra_apply_config_diff(nodes)
    diff = cassandra_apply_config_diff(nodes, width=width, margin=2)
    outcomes = cassandra_apply_config_outcomes([{"name": "h-n1", "os": True}, {"name": "h-n2", "os": True}], plan=True,
                                               width=width, margin=2)
    sections = [[{"pre": diff}], [{"pre": outcomes}]]
    text = cassandra_screen({"operation": "apply_config", "summary": "2 nodes of prod", "sections": sections})
    assert "  OS settings" in text and all(len(line) <= width for line in text.split("\n")), text


def test_os_settings_with_the_dm_kernel_name_when_verbose():
    lines = cassandra_apply_config_diff(os_nodes(), verbose=True)
    assert [line.split("  4096", maxsplit=1)[0].strip() for line in lines if "vgdata" in line] == [
        "read-ahead vgdata-lvdata (dm-3)", "read-ahead vgdata-lvdata (dm-4)", "read-ahead vgdata-lvdata (dm-5)",
        "read-ahead vgdata-lvdata (dm-7)"]


def test_the_outcome_lines_at_the_width_cut_between_words():
    nodes = [{"name": "db-node-1.example.net", "todo": False, "os": True, "os_done": True, "then": "",
              "notes": ["read-ahead set live: tuned profile switch pending (to virtual-guest cassandra-disks): not done"
                        " while Cassandra runs (tuned applies all its settings again, live)."]}]
    lines = cassandra_apply_config_outcomes(nodes, plan=True, width=70)
    assert all(len(line) <= 70 for line in lines)
    assert nodes[0]["notes"][0] in " ".join(line.strip() for line in lines)


def test_auto_bootstrap_true_on_the_node_is_the_default_the_role_leaves_out(tmp_path):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import cassandra_same_settings
    for written in ("auto_bootstrap: true\n", "auto_bootstrap: True\n", "auto_bootstrap: 'true'\n", "auto_bootstrap: yes\n"):
        assert cassandra_same_settings(YAML + written, YAML, "cassandra.yaml"), written
    assert not cassandra_same_settings(YAML + "auto_bootstrap: false\n", YAML, "cassandra.yaml")
    # rewritten for another key: auto_bootstrap: true not shown as a difference, false is
    live = YAML + "auto_bootstrap: true\nfake_option: 42\n"
    assert view(Node(tmp_path, "node1", files={"cassandra.yaml": live})) == [
        'Item           Node  Inventory',
        'cassandra.yaml  on node1',
        '  fake_option  42    absent']
    live = YAML + "auto_bootstrap: false\n"
    assert "auto_bootstrap" in "\n".join(view(Node(tmp_path, "node2", files={"cassandra.yaml": live})))


PENDING = "restart pending (cassandra-env.sh changed since the running Cassandra started)"


def test_the_recap_drops_the_restart_pending_of_a_node_this_run_restarted():
    """It was computed before the restart: "node4  applied, restarted" then "restart pending" was wrong."""
    nodes = [{"name": "node4", "todo": True, "then": "restart", "done": True, "notes": [PENDING]},
             {"name": "node5", "todo": True, "then": "start", "done": True, "notes": [PENDING]},
             {"name": "node6", "todo": True, "then": "restart", "result": "not done (the run stopped before it)", "notes": [PENDING]}]
    lines = cassandra_apply_config_recap(nodes)
    assert lines[1:] == ["node4  applied, restarted", "node5  applied, started", "node6  not done (the run stopped before it)", "  " + PENDING]
    # under --check nothing was restarted: still pending; the plan says it too
    assert "    " + PENDING in cassandra_apply_config_recap(nodes, check=True)  # (under WOULD DO)
    assert cassandra_apply_config_outcomes(nodes[:1], plan=True) == ["node4  write + restart", "  " + PENDING]
    assert nodes[0]["notes"] == [PENDING]  # (the nodes given are left as they are)


def test_the_recap_ends_with_the_optional_restart_for_the_read_ahead_set_live():
    command = "ansible-playbook -i inv.yml community.cassandra.rolling_restart -e cassandra_hosts=prod"
    nodes = [{"name": "node%d" % i, "todo": False, "os": True, "os_done": True, "notes": [READ_AHEAD_LIVE]}
             for i in (2, 3, 5)] + [{"name": "node1", "todo": True, "then": "restart", "done": True}]
    lines = cassandra_apply_config_recap(nodes, restart_command=command)
    assert lines[-3:] == [
        "",
        "NEXT  optional, when convenient: restart node2, node3, node5 so that Cassandra reads the files it has open"
        " with the new read-ahead (one node at a time; the new files use it already):",
        "  " + command + " --limit node2,node3,node5"]
    # at a terminal's width: the text wrapped under its number, the command whole (to paste)
    narrow = cassandra_apply_config_recap(nodes, restart_command=command, width=60)
    todo = [line for line in narrow if line.startswith("NEXT  ")]
    todo = narrow[narrow.index(todo[0]):]
    assert todo[-1] == "  " + command + " --limit node2,node3,node5"
    assert all(len(line) <= 60 for line in todo[:-1]) and todo[1].startswith("  so that")

    def said(lines):
        return [line for line in lines if line.startswith(("NEXT", "TODO"))]
    # none under --check (nothing was set), nor without the command, nor for a node whose OS settings were not applied
    assert not said(cassandra_apply_config_recap(nodes, check=True, restart_command=command))
    assert not said(cassandra_apply_config_recap(nodes))
    # nor under a FAILED recap: the stop said what to do first, an optional restart is no NEXT then
    failed = nodes[:-1] + [dict(nodes[-1], done=False, result="restart FAILED after 600s: not NORMAL")]
    assert cassandra_apply_config_recap(failed, restart_command=command)[0].startswith("FAILED  apply_config")
    assert not said(cassandra_apply_config_recap(failed, restart_command=command))
    for n in nodes:
        n["os_done"] = False
    assert not said(cassandra_apply_config_recap(nodes, restart_command=command))


def test_the_nodes_moving_storage_compatibility_mode_to_none(tmp_path):
    """5.0's point of no return: said in the plan of apply_config (upgrade, UPGRADING -> NONE)."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_no_return
    yaml50 = YAML + "storage_compatibility_mode: NONE\n"
    nodes = [Node(tmp_path, "node1", files={"cassandra.yaml": yaml50.replace("NONE", "UPGRADING")},
                  targets={"cassandra.yaml": yaml50}),
             Node(tmp_path, "node2", targets={"cassandra.yaml": yaml50}, files={"cassandra.yaml": yaml50}),
             Node(tmp_path, "node3", files={"cassandra.yaml": yaml50.replace("NONE", "CASSANDRA_4")},
                  targets={"cassandra.yaml": yaml50.replace("NONE", "UPGRADING")}),
             Node(tmp_path, "node4", files={"cassandra.yaml": yaml50.replace("3h", "6h")}, targets={"cassandra.yaml": yaml50})]
    assert cassandra_apply_config_no_return([n.data() for n in nodes]) == ["node1"]
    assert cassandra_apply_config_no_return([]) == []


def test_a_service_has_its_own_label_and_check_separates_found_from_would_do():
    # a service by what it is, no generic header word over it; --check: what was found (the node's
    # value against the inventory's) apart from what a real run would do; nothing claimed done
    chronyd = {"item": "chronyd", "before": "inactive", "after": "active", "timesync": True, "service": True}
    nodes = [{"name": "h-n2", "items": [], "os": [chronyd]}]
    diff = cassandra_apply_config_diff(nodes)
    assert diff == ["Item               Node      Inventory", "OS settings  on h-n2", "  service chronyd  inactive  active"]
    recap = cassandra_apply_config_recap(
        [{"name": "h-n1", "todo": False, "result": "nothing to apply"},
         {"name": "h-n2", "todo": True, "done": True, "then": "restart", "why": "time sync",
          "steps": ["drain and stop Cassandra", "start chronyd", "wait up to 2m for clock sync", "start Cassandra"]}],
        check=True, cluster="c", diff=diff)
    head = ["CHECK  apply_config  c  1 would restart, 1 up to date", "  nothing was changed (--check)", ""]
    would = ["", "WOULD DO", "  h-n2  would drain and stop Cassandra, start chronyd, wait up to 2m for clock sync, start Cassandra",
             "  h-n1  up to date"]
    assert recap == head + ["DIFFERENCES FOUND"] + ["  " + line for line in diff] + would


def test_the_plan_and_the_check_say_the_steps_the_recap_what_was_done():
    # the steps a real run takes, its intentions: in the plan as they are, under --check "would ...";
    # after a real run, what was done ("applied, restarted"), the steps not said again
    node = {"name": "h-n1", "todo": True, "then": "restart", "why": "time sync",
            "steps": ["drain and stop Cassandra", "start chronyd", "wait up to 2m for clock sync", "start Cassandra"]}
    assert cassandra_apply_config_outcomes([node], plan=True) == [
        "h-n1  drain and stop Cassandra, start chronyd, wait up to 2m for clock sync, start Cassandra"]
    assert cassandra_apply_config_outcomes([dict(node, done=True)], check=True)[0].startswith("h-n1  would drain and stop Cassandra, ")
    assert cassandra_apply_config_recap([dict(node, done=True)])[1] == "h-n1  applied, restarted (time sync)"


def test_a_long_plan_row_goes_on_under_its_own_column():
    # the nodes' column left clear: the next line of an outcome under its text, not under the names
    steps = ["drain and stop Cassandra", "start chronyd", "wait up to 2m for clock sync", "start Cassandra"]
    nodes = [{"name": "h-n1", "todo": True, "then": "restart", "steps": steps, "why": "time sync", "result": "",
              "notes": []},
             {"name": "h-n2", "todo": False, "result": "nothing to apply"},
             {"name": "h-n3", "todo": False, "result": "nothing to apply"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True, width=77) == [
        "h-n1        drain and stop Cassandra, start chronyd, wait up to 2m for clock",
        "            sync, start Cassandra",
        "h-n2, h-n3  up to date"]


def test_every_file_on_the_same_nodes_says_them_once(tmp_path):
    # (2026-11-17, the owner's --check on 12 nodes) the node list said once above the table, "all 12 nodes" for the
    # whole run (more than 5 nodes: a list counted otherwise), not on each file's row
    names = ["h-n%d" % i for i in range(1, 13)]
    for n in names:
        (tmp_path / n).mkdir()
    same = [Node(tmp_path / n, n, files={"jvm-server.options": JVM + "-Xmx8G\n", "cassandra-env.sh": ENV + "# x\n"})
            for n in names]
    lines = view(*same)
    assert lines[0] == "on all 12 nodes:" and lines[1].startswith("Item")
    assert "cassandra-env.sh" in lines and "jvm-server.options" in lines  # (the file rows alone)
    assert not [line for line in lines[1:] if "h-n" in line or "12 nodes" in line]
    # one node apart: each file on its own nodes, as before
    other = Node(tmp_path / "h-n1", "h-n1", files={"jvm-server.options": JVM + "-Xmx8G\n"})
    lines = view(other, *same[1:])
    assert not lines[0].startswith("on ") and [line for line in lines if "cassandra-env.sh  on " in line]


def test_an_item_too_long_has_its_values_on_the_next_line(tmp_path):
    long_option = "--add-exports java.base/jdk.internal.util=ALL-UNNAMED"
    edited = JVM.replace("-Xss256k", "-Xss512k") + long_option + "\n"
    lines = cassandra_apply_config_diff([Node(tmp_path, "node1", files={"jvm-server.options": edited}).data()], width=50)
    at = next((i for i, line in enumerate(lines) if line.strip() == long_option), None)
    assert at is not None, lines
    assert lines[at + 1].split() == ["present", "absent", "node1"] or lines[at + 1].split()[:2] == ["present", "absent"]
    assert all(len(line) <= 50 for line in lines if long_option not in line), lines


def test_the_recap_says_the_proofs_by_their_own_words():
    # the reasons of cassandra_apply_config_no_effect, by the constants it says them with;
    # another reason of a node kept running is not one of them
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
        LOGBACK_SCANS, NOT_READ_BY_JAVA, SAME_JVM_ARGUMENTS)
    proofs = [SAME_JVM_ARGUMENTS, NOT_READ_BY_JAVA % ("jvm11-server.options", "17"), LOGBACK_SCANS]
    node = {"name": "n1", "todo": True, "done": True, "then": "none", "result": "applied",
            "kept_running": proofs + ["an owner, group or mode: read at start"]}
    text = "\n".join(cassandra_apply_config_recap([node]))
    assert "applied, not restarted (%s)" % "; ".join(proofs) in text, text


def test_what_the_next_restart_changes_is_said_under_the_node():
    # a file written with no restart that gives the JVM another option at its next start (no effect now): one line
    # under the node, in the plan and the recap, not among the reasons
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import SAME_JVM_ARGUMENTS
    later = "next restart: -XX:HeapDumpPath=/srv/zorglub/dumps added (no effect now)"
    node = {"name": "n1", "todo": True, "done": True, "then": "none", "result": "applied",
            "kept_running": [SAME_JVM_ARGUMENTS, later]}
    for lines in (cassandra_apply_config_recap([node]),
                  cassandra_apply_config_outcomes([dict(node, done=False)], plan=True)):
        text = "\n".join(lines)
        assert "(%s)" % SAME_JVM_ARGUMENTS in text and "; next restart" not in text, text
        assert "  " + later in [line.rstrip() for line in lines] or any(line.strip() == later for line in lines), text
