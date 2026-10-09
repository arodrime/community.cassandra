from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import base64
import os
import subprocess
import sys

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
    ROLE_MASK, _line_settings, _patched, _yaml_settings, cassandra_apply_config_diff, cassandra_apply_config_outcomes,
    cassandra_apply_config_recap)
from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen

ARROW = u"←"
ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def _walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key))


with open(ROLE, encoding="utf-8") as f:
    DIFF_SCRIPT = next(t for t in _walk(yaml.safe_load(f)) if t.get("name") == "Diff them against the live files")[
        "ansible.builtin.command"]["argv"][2]


def role_diff(tmp_path, live, new, name="cassandra.yaml"):
    """The diff cassandra_config's preview gives (its own script: masked as the role masks)."""
    a, b = tmp_path / ("live-" + name), tmp_path / ("new-" + name)
    if live is not None:
        a.write_bytes(live.encode())
    b.write_bytes(new.encode())
    run = subprocess.run([sys.executable, "-c", DIFF_SCRIPT, str(a), str(b)], stdout=subprocess.PIPE, check=False)
    assert run.returncode in (0, 1)
    return run.stdout.decode()


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
        self.stats = [{"cassandra_config_file": "cassandra-env.sh", "stat": stat(env_owner, "svccassandra")},
                      {"cassandra_config_file": "cassandra.yaml", "stat": stat("root")}]
        self.dir_stats = [{"item": ["data dir", "/var/lib/cassandra/data"], "stat": stat(data_owner, mode="0750")}]

    def data(self):
        return {"name": self.name, "items": self.items, "live": self.live, "stats": self.stats, "dir_stats": self.dir_stats}


def view(*nodes):
    return cassandra_apply_config_diff([n.data() for n in nodes])


def test_the_four_drifts_setting_centric_target_first(tmp_path):
    env_perms = [{"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)", "before": "root:root 0640",
                  "after": "cassandra:svccassandra 0640"}]
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
        "  owner/group/mode: cassandra:svccassandra 0640   node1..node3, node5          (inventory)",
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
    assert ROLE_MASK.sub(r"\1****", "keystore_password: x") == "keystore_password: ****"
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
             "notes": ["WARNING data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)"]},
            {"name": "node4", "todo": True, "then": "restart", "done": True},
            {"name": "node5", "todo": False, "result": "nothing to apply"}]


def test_the_outcomes_grouped_in_the_plan():
    # the nodes changed first, in inventory order of their first node
    assert cassandra_apply_config_outcomes(outcome_nodes(), plan=True) == [
        "node2, node4  would apply, then restart",
        "node3  would apply, no restart (an owner, group or mode only: Cassandra reads them when it starts)",
        "  WARNING data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)",
        "node1, node5  nothing to apply"]
    nodes = [{"name": "node1", "todo": True, "then": "start"}, {"name": "node2", "todo": True, "then": "write"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        "node1  would apply, then start (its Cassandra is not running: started once written (down over"
        " max_hint_window? repair it))",
        "node2  would apply, left stopped (its Cassandra is stopped: left stopped, reads its config when it starts)"]


def test_the_check_recap_shows_the_settings_then_the_outcomes():
    diff = ["cassandra.yaml", "  max_hint_window:  3h   node1   (inventory)"]
    assert cassandra_apply_config_recap(outcome_nodes(), check=True, cluster="my_cluster", seconds=75, diff=diff) == [
        "CHECK  apply_config  my_cluster  3 would apply, 2 nothing to apply (1m15s)",
        "",
        "cassandra.yaml",
        "  max_hint_window:  3h   node1   (inventory)",
        "",
        "node2, node4  would apply, then restart",
        "node3  would apply, no restart",
        "  WARNING data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)",
        "node1, node5  nothing to apply"]


def test_the_real_run_recap_says_the_outcomes_only():
    nodes = outcome_nodes()
    nodes[3] = {"name": "node4", "todo": True, "done": False, "result": "apply_config FAILED after 30s: no answer"}
    nodes.append({"name": "node6", "todo": True, "result": "not reached"})
    nodes.append({"name": "node7", "result": "not in this run (--limit)"})
    assert cassandra_apply_config_recap(nodes, cluster="my_cluster", diff=["cassandra.yaml", "  x"]) == [
        "FAILED  apply_config  my_cluster  2 applied, 1 failed, 2 nothing to apply, 1 not reached, 1 not in this run",
        "node2  applied, restarted",
        "node3  applied, no restart",
        "  WARNING data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are)",
        "node4  apply_config FAILED after 30s: no answer",
        "node1, node5  nothing to apply",
        "node6  not reached",
        "node7  not in this run (--limit)"]


def test_nothing_to_apply_recap():
    nodes = [{"name": "node%d" % i, "todo": False, "result": "nothing to apply"} for i in (1, 2, 3)]
    assert cassandra_apply_config_recap(nodes) == ["DONE  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]
    assert cassandra_apply_config_recap(nodes, check=True, diff=[]) == [
        "CHECK  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]


def test_the_screen_sections_are_blocks_of_their_own():
    text = cassandra_screen({"operation": "apply_config", "summary": "apply the config on node2",
                             "sections": [[{"pre": ["cassandra.yaml", "  x: 1"]}], [], [{"pre": ["node2  would apply"]}]]})
    assert text == "apply_config: apply the config on node2\n\ncassandra.yaml\n  x: 1\n\nnode2  would apply"


def test_the_settings_lines_filter_takes_lists():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_settings_lines
    assert cassandra_settings_lines(["f", ["x", [["1", "node1", ""], ["2", "node2", "differs"]]]]) == [
        "f", "  x: 1   node1", "     2   node2   differs"]
