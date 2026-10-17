from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import base64

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
    READ_AHEAD_LIVE, _line_settings, _masked, _patched, _yaml_settings, cassandra_apply_config_diff,
    cassandra_apply_config_outcomes, cassandra_apply_config_recap)
from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import preview

ARROW = u"←"


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


def test_the_four_drifts_setting_centric_target_first(tmp_path):
    env_perms = [{"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)", "before": "root:root 0640",
                  "after": "cassandra:cassandraops 0640"}]
    data_dir = [{"item": "data dir /var/lib/cassandra/data (owner:group mode)", "path": "/var/lib/cassandra/data",
                 "dir": "data dir", "before": "root:root 0750", "after": "cassandra:cassandra 0750"}]
    nodes = [Node(tmp_path, "node1"),
             Node(tmp_path, "node2", files={"jvm-server.options": JVM + "-Ddemo.drift=1\n"}),
             Node(tmp_path, "node3", dirs=data_dir, data_owner="root"),
             Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("3h", "6h")}, perms=env_perms, env_owner="root"),
             Node(tmp_path, "node5")]
    assert view(*nodes) == [
        "cassandra.yaml",
        "  max_hint_window:  3h                            node1..node3, node5          (inventory)",
        "                    6h                            node4                        %s differs" % ARROW,
        "cassandra-env.sh",
        "  owner/group/mode: cassandra:cassandraops 0640   node1..node3, node5          (inventory)",
        "                    root:root 0640                node4                        %s differs" % ARROW,
        "jvm-server.options",
        "  -Ddemo.drift=1:   present                       node2                        %s differs (not in the inventory)" % ARROW,
        "data dir /var/lib/cassandra/data",
        "  owner/group/mode: cassandra:cassandra 0750      node1, node2, node4, node5   (inventory)",
        "                    root:root 0750                node3                        %s differs" % ARROW]


def test_every_other_value_with_its_nodes_and_a_value_the_inventory_adds(tmp_path):
    nodes = [Node(tmp_path, "node%d" % i) for i in (1, 2)]
    nodes += [Node(tmp_path, "node3", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 64")}),
              Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")}),
              Node(tmp_path, "node5", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")})]
    lines = view(*nodes)
    assert lines == [
        "cassandra.yaml",
        "  concurrent_reads: 32   node1, node2   (inventory)",
        "                    64   node3          %s differs" % ARROW,
        "                    48   node4, node5   %s differs" % ARROW]
    # an option the inventory adds: there (inventory), not there on the nodes (differs)
    added = JVM + "-Dcassandra.ring_delay_ms=30000\n"
    lines = view(Node(tmp_path, "node1", files={"jvm-server.options": added}, targets={"jvm-server.options": added}),
                 Node(tmp_path, "node2", targets={"jvm-server.options": added}))
    assert lines == ["jvm-server.options",
                     "  -Dcassandra.ring_delay_ms=30000: present   node1   (inventory)",
                     "                                   absent    node2   %s differs" % ARROW]


def test_jvm_options_and_env_lines_as_settings(tmp_path):
    lines = view(Node(tmp_path, "node1"),
                 Node(tmp_path, "node2", files={"jvm-server.options": JVM.replace("256k", "512k"),
                                                "cassandra-env.sh": ENV.replace("8G", "16G")}))
    assert lines == [
        "cassandra-env.sh",
        "  MAX_HEAP_SIZE: \"8G\"    node1   (inventory)",
        "                 \"16G\"   node2   %s differs" % ARROW,
        "jvm-server.options",
        "  -Xss:          256k    node1   (inventory)",
        "                 512k    node2   %s differs" % ARROW]


def test_a_value_set_per_node_lists_no_node_on_it(tmp_path):
    # listen_address is each node's own: the nodes not changing are not "on" node3's value
    def yml(address):
        return "listen_address: %s\n" % address + YAML
    nodes = [Node(tmp_path, "node%d" % i, files={"cassandra.yaml": yml("10.0.0.%d" % i)},
                  targets={"cassandra.yaml": yml("10.0.0.%d" % i)}) for i in (1, 2)]
    nodes.append(Node(tmp_path, "node3", files={"cassandra.yaml": yml("10.0.0.9")}, targets={"cassandra.yaml": yml("10.0.0.3")}))
    assert view(*nodes) == ["cassandra.yaml",
                            "  listen_address: 10.0.0.3           (inventory, node3)",
                            "                  10.0.0.9   node3   %s differs" % ARROW]


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
    # a long name on a line of its own; a changed secret said, its values masked on both sides
    assert lines == [
        "cassandra.yaml",
        "  client_encryption_options.keystore_password:",
        "                                         ****      node1, node3   (inventory)",
        "                                         ****      node2          %s differs (masked)" % ARROW,
        "  ssl_private_key:                       ****                     (inventory, node3)",
        "                                         ****      node3          %s differs (masked)" % ARROW,
        "jvm-server.options",
        "  -Djavax.net.ssl.keyStorePassword=****: present   node1, node3   (inventory)",
        "                                         present   node2          %s differs (masked)" % ARROW]


def test_nested_keys_lists_and_layout_only(tmp_path):
    target = YAML.replace("enabled: false", "enabled: true").replace("10.0.0.1:7000", "10.0.0.1:7000,10.0.0.2:7000")
    target = target.replace("  - /var/lib/cassandra/data\n", "  - /var/lib/cassandra/data\n  - /data2\n")
    nodes = [Node(tmp_path, "node1", targets={"cassandra.yaml": target}),
             Node(tmp_path, "node2", targets={"cassandra.yaml": YAML.replace("# reads", "# the reads")})]
    assert view(*nodes) == [
        "cassandra.yaml",
        "  client_encryption_options.enabled: true                                   (inventory, node1)",
        "                                     false                          node1   %s differs" % ARROW,
        "  seed_provider.parameters.seeds:    10.0.0.1:7000,10.0.0.2:7000            (inventory, node1)",
        "                                     10.0.0.1:7000                  node1   %s differs" % ARROW,
        "  data_file_directories:             /var/lib/cassandra/data, /data2          (inventory, node1)",
        "                                     /var/lib/cassandra/data        node1   %s differs" % ARROW,
        "  comments or layout:                differ                         node2   (no setting changes)"]


def test_a_new_file_a_line_ending_and_a_file_changed_since_compared(tmp_path):
    crlf = ENV.replace('"8G"\n', '"8G"\r\n')
    nodes = [Node(tmp_path, "node1"),
             Node(tmp_path, "node2", files={"cassandra-env.sh": crlf}),
             Node(tmp_path, "node3", files={"jvm17-server.options": None}, targets={"jvm17-server.options": "-Xss1m\n"})]
    stale = Node(tmp_path, "node4", files={"cassandra.yaml": YAML.replace("3h", "6h")})
    stale.live[0]["content"] = b64("short: 1\n")  # rewritten since the compare: the diff does not fit
    lines = view(*(nodes + [stale]))
    assert lines == [
        "cassandra.yaml",
        "  node4: not read, or changed since it was compared: run apply_config again",
        "cassandra-env.sh",
        "  MAX_HEAP_SIZE: \"8G\"     node1, node3, node4   (inventory)",
        "                 \"8G\"\\r   node2                 %s differs" % ARROW,
        "jvm17-server.options",
        "  file:          absent   node3                 %s differs (written whole)" % ARROW]


def test_a_live_file_not_read_is_not_said_new(tmp_path):
    unread = Node(tmp_path, "node2", files={"cassandra.yaml": YAML.replace("3h", "6h")})
    unread.live = [r for r in unread.live if r["cassandra_apply_config_file"] != "cassandra.yaml"]
    assert view(Node(tmp_path, "node1"), unread) == [
        "cassandra.yaml", "  node2: not read, or changed since it was compared: run apply_config again"]


def test_nothing_differs():
    assert cassandra_apply_config_diff([{"name": "node1", "items": []}, {"name": "node2"}]) == []
    assert cassandra_apply_config_diff([]) == []


def test_the_conf_dir_alternative():
    items = [{"item": "/etc/cassandra/conf", "before": "/etc/cassandra/default.conf", "after": "/etc/cassandra/site"}]
    assert cassandra_apply_config_diff([{"name": "node1", "items": items}]) == [
        "/etc/cassandra/conf",
        "  points to: /etc/cassandra/site                   (inventory, node1)",
        "             /etc/cassandra/default.conf   node1   %s differs" % ARROW]


def test_a_change_next_to_a_secret_on_several_lines_is_read(tmp_path):
    # the live lines masked as the role's diff masks them (a secret's value lines, sse_c_key): the diff fits them
    def yml(tokens):
        return YAML + "sse_c_key: abc\nldap_password: |\n  LINE1\n  LINE2\nnum_tokens: %d\n" % tokens
    lines = view(Node(tmp_path, "node1", files={"cassandra.yaml": yml(16)}, targets={"cassandra.yaml": yml(8)}))
    assert [line.split()[:2] for line in lines[1:3]] == [["num_tokens:", "8"], ["16", "node1"]]
    assert lines[2].endswith(ARROW + " differs")
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
        said = [line for line in lines if "differs (masked)" in line]
        assert bool(said) == (key is not None), (live, lines)
        if key:
            assert [line for line in lines if line.lstrip().startswith(key)], lines
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
    assert lines[:2] == ["cassandra.yaml", "  server_encryption_options.ssl_context_factory.parameters.private_key:"]
    assert lines[2].split() == ["****", "node2", "(inventory)"]
    assert lines[3].split() == ["****", "node1", ARROW, "differs", "(masked)"]
    assert lines[4] == "  server_encryption_options.ssl_context_factory.parameters.trusted_certificates:"
    assert lines[5].split()[:4] == ["-----BEGIN", "CERTIFICATE-----", "...", "(2"] and lines[5].endswith("node2   (inventory)")
    assert lines[6].split()[:4] == ["-----BEGIN", "CERTIFICATE-----", "...", "(2"] and lines[6].endswith(ARROW + " differs")
    assert lines[5].split()[5] != lines[6].split()[5]  # their digests


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
             "notes": ["WARNING - data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)"]},
            {"name": "node4", "todo": True, "then": "restart", "done": True},
            {"name": "node5", "todo": False, "result": "nothing to apply"}]


def test_the_outcomes_grouped_in_the_plan():
    # the nodes changed first, in inventory order of their first node
    assert cassandra_apply_config_outcomes(outcome_nodes(), plan=True) == [
        "node2, node4  will apply, then restart",
        "node3  will apply, no restart (an owner, group or mode only: Cassandra reads them when it starts)",
        "node1, node5  nothing to apply"]
    nodes = [{"name": "node1", "todo": True, "then": "start"}, {"name": "node2", "todo": True, "then": "write"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        "node1  will apply, then start (its Cassandra is not running: started once written (down over"
        " max_hint_window? repair it))",
        "node2  will apply, left stopped (its Cassandra is stopped: left stopped, reads its config when it starts)"]
    # a file written back as the running Cassandra started with it: its note says why (not "an owner... only")
    note = "jvm-server.options written back as the running Cassandra started with it: no restart for it"
    nodes = [{"name": "node1", "todo": True, "then": "none", "notes": [note]}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == ["node1  will apply, no restart", "  " + note]


def test_the_check_recap_shows_the_settings_then_the_outcomes():
    diff = ["cassandra.yaml", "  max_hint_window:  3h   node1   (inventory)"]
    assert cassandra_apply_config_recap(outcome_nodes(), check=True, cluster="my_cluster", seconds=75, diff=diff,
                                        planned=["node1", "node2", "node3", "node4", "node5"]) == [
        "CHECK  apply_config  my_cluster  3 would apply, 2 nothing to apply (1m15s)",
        "",
        "cassandra.yaml",
        "  max_hint_window:  3h   node1   (inventory)",
        "",
        "node2, node4  would apply, then restart",
        "node3  would apply, no restart",
        "node1, node5  nothing to apply",
        "",
        "1 warning in the plan above (node3)."]  # said there, not under node3 again


def test_the_real_run_recap_says_the_outcomes_only():
    nodes = outcome_nodes()
    nodes[3] = {"name": "node4", "todo": True, "done": False, "result": "apply_config FAILED after 30s: no answer"}
    nodes.append({"name": "node6", "todo": True, "result": "not reached"})
    nodes.append({"name": "node7", "result": "not in this run (--limit)"})
    assert cassandra_apply_config_recap(nodes, cluster="my_cluster", diff=["cassandra.yaml", "  x"],
                                        planned=["node%d" % i for i in range(1, 8)]) == [
        "FAILED  apply_config  my_cluster  2 applied, 1 failed, 2 nothing to apply, 1 not reached, 1 not in this run",
        "node2  applied, restarted",
        "node3  applied, no restart",
        "node4  apply_config FAILED after 30s: no answer",
        "node1, node5  nothing to apply",
        "node6  not reached",
        "node7  not in this run (--limit)",
        "",
        "1 warning in the plan above (node3)."]


def test_nothing_to_apply_recap():
    nodes = [{"name": "node%d" % i, "todo": False, "result": "nothing to apply"} for i in (1, 2, 3)]
    assert cassandra_apply_config_recap(nodes) == ["DONE  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]
    assert cassandra_apply_config_recap(nodes, check=True, diff=[]) == [
        "CHECK  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]


def test_the_screen_sections_are_blocks_of_their_own():
    text = cassandra_screen({"operation": "apply_config", "summary": "apply the config on node2",
                             "sections": [[{"pre": ["cassandra.yaml", "  x: 1"]}], [], [{"pre": ["node2  would apply"]}]]})
    assert text == "apply_config: apply the config on node2\n\ncassandra.yaml\n  x: 1\n\nnode2  would apply"


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
        "NODE GROUPS",
        "  G1 = db11..db16, db21..db26 (12 nodes)",
        "  G2 = db071..db073, db075, db076 (5 nodes)",
        "",
        "cassandra.yaml",
        "  concurrent_reads: 32   G2           (inventory)",
        "                    64   G1 + db074   %s differs" % ARROW,
        "  max_hint_window:  3h   G1           (inventory)",
        "                    6h   G2 + db074   %s differs" % ARROW]
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
        "cassandra.yaml",
        "  max_hint_window:                 3h    node2, node3   (inventory)",
        "                                   6h    node1          %s differs" % ARROW,
        "OS settings",
        "  read_ahead_kb vg_data/cassandra: 4                    (inventory)",
        "                                   128   node1          %s differs" % ARROW,
        "                                   256   node2          %s differs" % ARROW,
        "  sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf):",
        "                                   1                    (inventory)",
        "                                   60    node1          %s differs" % ARROW]


def test_the_os_settings_alone_no_restart():
    nodes = [{"name": "node1", "todo": False, "os": True, "result": "not reached",
              "notes": ["read-ahead set live: the files Cassandra has open keep the old one until it restarts"]},
             {"name": "node2", "todo": True, "os": True, "then": "restart", "done": True},
             {"name": "node3", "todo": False, "result": "nothing to apply"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        "node1  will apply, no restart (OS settings only: applied live)",
        "  read-ahead set live: the files Cassandra has open keep the old one until it restarts",
        "node2  will apply, then restart",
        "node3  nothing to apply"]
    nodes[0]["os_done"] = True
    assert cassandra_apply_config_recap(nodes, check=True)[0] == "CHECK  apply_config  2 would apply, 1 nothing to apply"
    assert cassandra_apply_config_recap(nodes)[:2] == ["DONE  apply_config  2 applied, 1 nothing to apply",
                                                       "node1  applied, no restart"]
    # the run stopped before the OS settings: not said applied
    del nodes[0]["os_done"]
    assert "node1  not reached" in cassandra_apply_config_recap(nodes)
    # its config not reached, its OS settings applied: said
    nodes[1].update({"done": False, "os_done": True, "result": "not reached"})
    assert "node2  not reached, its OS settings applied" in cassandra_apply_config_recap(nodes)


def test_an_os_value_unknown_before():
    change = {"item": "LVM read-ahead vg/lv (sectors)", "before": None, "after": "8"}
    lines = cassandra_apply_config_diff([{"name": "node1", "os": [change]}])
    assert lines[-1].split() == ["(none)", "node1", ARROW, "differs"]


def test_a_key_added_by_hand_alone_is_a_difference_and_not_kept(tmp_path):
    # a running node's cassandra.yaml with only an unknown key appended (lineinfile): not the same settings as the
    # role's (cassandra_config keeps the file otherwise), shown as a key the inventory does not carry
    from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import cassandra_same_settings
    live = YAML + "fake_option: 42\n"
    assert not cassandra_same_settings(live, YAML, "cassandra.yaml")
    assert view(Node(tmp_path, "node1", files={"cassandra.yaml": live}), Node(tmp_path, "node2")) == [
        "cassandra.yaml",
        "  fake_option: 42   node1   %s differs (not in the inventory)" % ARROW]


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
    # the inventory value alone on its line, the nodes on the line of their own value, wrapped between two nodes,
    # the marker after them; an LVM volume one setting whatever its dm-N on each node
    lines = cassandra_apply_config_diff(os_nodes(), width=100)
    assert lines == [
        "OS settings",
        "  read_ahead_kb sdb:           4                                                      (inventory)",
        "                               4096                  db-node-1.example.net..db-node-3.example.net,",
        "                                                     db-node-5.example.net            %s differs" % ARROW,
        "  read_ahead_kb vgdata-lvdata: 4                                                      (inventory)",
        "                               4096                  db-node-1.example.net..db-node-3.example.net,",
        "                                                     db-node-5.example.net            %s differs" % ARROW,
        "  /etc/udev/rules.d/64-cassandra-data-disk.rules:",
        "                               present                                                (inventory)",
        "                               absent or different   db-node-1.example.net..db-node-3.example.net,",
        "                                                     db-node-5.example.net            %s differs" % ARROW]
    for width in (80, 100, 118):
        lines = cassandra_apply_config_diff(os_nodes(), width=width)
        assert all(len(line) <= width for line in lines), width
        assert "%s differs" % ARROW in "\n".join(lines) and "(inventory)" in "\n".join(lines)
        assert not [line for line in lines if line.strip() in ("differs", "(in", "ventory)")]


def test_os_settings_with_the_dm_kernel_name_when_verbose():
    lines = cassandra_apply_config_diff(os_nodes(), verbose=True)
    assert [line.split(":")[0].strip() for line in lines if "vgdata" in line] == [
        "read_ahead_kb vgdata-lvdata (dm-3)", "read_ahead_kb vgdata-lvdata (dm-4)", "read_ahead_kb vgdata-lvdata (dm-5)",
        "read_ahead_kb vgdata-lvdata (dm-7)"]


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
        "cassandra.yaml",
        "  fake_option: 42   node1   %s differs (not in the inventory)" % ARROW]
    live = YAML + "auto_bootstrap: false\n"
    assert "auto_bootstrap" in "\n".join(view(Node(tmp_path, "node2", files={"cassandra.yaml": live})))


PENDING = "restart pending (cassandra-env.sh changed since the running Cassandra started)"


def test_the_recap_drops_the_restart_pending_of_a_node_this_run_restarted():
    """It was computed before the restart: "node4  applied, restarted" then "restart pending" was wrong."""
    nodes = [{"name": "node4", "todo": True, "then": "restart", "done": True, "notes": [PENDING]},
             {"name": "node5", "todo": True, "then": "start", "done": True, "notes": [PENDING]},
             {"name": "node6", "todo": True, "then": "restart", "result": "not reached", "notes": [PENDING]}]
    lines = cassandra_apply_config_recap(nodes)
    assert lines[1:] == ["node4  applied, restarted", "node5  applied, started", "node6  not reached", "  " + PENDING]
    # under --check nothing was restarted: still pending; the plan says it too
    assert "  " + PENDING in cassandra_apply_config_recap(nodes, check=True)
    assert cassandra_apply_config_outcomes(nodes[:1], plan=True) == ["node4  will apply, then restart", "  " + PENDING]
    assert nodes[0]["notes"] == [PENDING]  # (the nodes given are left as they are)


def test_the_recap_ends_with_the_optional_restart_for_the_read_ahead_set_live():
    command = "ansible-playbook -i inv.yml community.cassandra.rolling_restart -e cassandra_hosts=prod"
    nodes = [{"name": "node%d" % i, "todo": False, "os": True, "os_done": True, "notes": [READ_AHEAD_LIVE]}
             for i in (2, 3, 5)] + [{"name": "node1", "todo": True, "then": "restart", "done": True}]
    lines = cassandra_apply_config_recap(nodes, restart_command=command)
    assert lines[-4:] == [
        "", "TO DO",
        "  1. optional, when convenient: restart node2, node3, node5 so that Cassandra reads the files it has open"
        " with the new read-ahead (one node at a time; the new files use it already):",
        "     " + command + " --limit node2,node3,node5"]
    # at a terminal's width: the text wrapped under its number, the command whole (to paste)
    narrow = cassandra_apply_config_recap(nodes, restart_command=command, width=60)
    todo = narrow[narrow.index("TO DO") + 1:]
    assert todo[-1] == "     " + command + " --limit node2,node3,node5"
    assert all(len(line) <= 60 for line in todo[:-1]) and todo[1].startswith("     ")
    # none under --check (nothing was set), nor without the command, nor for a node whose OS settings were not applied
    assert "TO DO" not in cassandra_apply_config_recap(nodes, check=True, restart_command=command)
    assert "TO DO" not in cassandra_apply_config_recap(nodes)
    for n in nodes:
        n["os_done"] = False
    assert "TO DO" not in cassandra_apply_config_recap(nodes, restart_command=command)
