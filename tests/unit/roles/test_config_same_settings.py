from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config leaves a running node's file alone when its settings are the
# same as the role's (a config written by hand: no header, other comments or
# layout), and writes remote JMX users the way the node has them.

import json
import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks")
HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file.\n"


def task(name, tasks_file="main.yml"):
    with open(os.path.join(TASKS, tasks_file), encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


SCRIPT = task("Diff them against the live files")["ansible.builtin.command"]["argv"][2]


def compare(tmp_path, name, live, new):
    """The preview's exit code for a live file and the role's: 0 same, 1 differ, 2 same settings."""
    (tmp_path / "live").mkdir()
    (tmp_path / "new").mkdir()
    live_path, new_path = tmp_path / "live" / name, tmp_path / "new" / name
    if live is not None:
        live_path.write_text(live)
    new_path.write_text(new)
    return subprocess.run([sys.executable, "-c", SCRIPT, str(live_path), str(new_path)],
                          stdout=subprocess.PIPE, check=False).returncode


@pytest.mark.parametrize("name, live, new, rc", [
    ("jvm-server.options", "-Xss256k\n", "-Xss256k\n", 0),
    # only the role's header line is missing (written by hand)
    ("jvm-server.options", "# stock comment\n-Xss256k\n", HEADER + "# stock comment\n-Xss256k\n", 2),
    ("cassandra-env.sh", "MAX_HEAP_SIZE=8G\n", HEADER + "# a comment\n\nMAX_HEAP_SIZE=8G\n", 2),
    ("cassandra-env.sh", "MAX_HEAP_SIZE=8G\n", HEADER + "MAX_HEAP_SIZE=4G\n", 1),
    # a switched-off line is a setting too
    ("cassandra-env.sh", "#JVM_OPTS=-Da\n", "JVM_OPTS=-Da\n", 1),
    # indentation is not a shell setting (the rmi line was " JVM_OPTS=..." before)
    ("cassandra-env.sh", ' JVM_OPTS="$JVM_OPTS -Dx=1"\n', HEADER + 'JVM_OPTS="$JVM_OPTS -Dx=1"\n', 2),
    ("jvm-server.options", " -Xss256k\n", HEADER + "-Xss256k\n", 1),  # bin/cassandra reads lines starting with '-'
    # shell words: a trailing comment, quotes that change nothing
    ("cassandra-env.sh", "MAX_HEAP_SIZE=512M   # small nodes\nJMX_PORT=7199  # default\n",
     HEADER + 'MAX_HEAP_SIZE="512M"\nJMX_PORT="7199"\n', 2),
    ("cassandra-env.sh", "MAX_HEAP_SIZE=512M\n", HEADER + 'MAX_HEAP_SIZE="1G"\n', 1),
    # an expansion: quotes count ('$X' is not "$X")
    ("cassandra-env.sh", "A='$B'\n", HEADER + 'A="$B"\n', 1),
    # JVM options: the lines bin/cassandra passes, in their order (the JVM and cassandra-env.sh read them so:
    # an -XX list flag set twice adds up, unlock options must come first, an agent twice loads twice)
    ("jvm-server.options", "-Xss256k\n# x\n  -Dy=1\n-Dx=1\n", HEADER + "-Xss256k\n-Dx=1\n", 2),
    ("jvm-server.options", "-Xss256k\n-Dx=1\n-Xmx1G\n", HEADER + "-Xss256k\n-Xmx1G\n-Dx=1\n", 2),  # a property anywhere
    ("jvm-server.options", "-Xss256k\n-Xmx1G\n", HEADER + "-Xmx1G\n-Xss256k\n", 1),
    ("jvm-server.options", "-Dx=1\n-Dy=2\n-Dx=3\n", HEADER + "-Dy=2\n-Dx=1\n-Dx=3\n", 1),  # a key twice: in order
    ("jvm-server.options", "-XX:OnOutOfMemoryError=/a\n-XX:OnOutOfMemoryError=b\n", HEADER + "-XX:OnOutOfMemoryError=b\n", 1),
    ("jvm-server.options", "-XX:+UseG1GC\n-XX:-UseG1GC\n", HEADER + "-XX:-UseG1GC\n", 1),
    ("jvm-server.options", "-javaagent:/a.jar\n-javaagent:/a.jar\n", HEADER + "-javaagent:/a.jar\n", 1),
    # bash reads these otherwise than their words: # in a word, ~, a continued line
    ("cassandra-env.sh", "LOCAL_JMX=yes#x\n", HEADER + "LOCAL_JMX=yes\n", 1),
    ("cassandra-env.sh", "X=~/d\n", HEADER + "X='~/d'\n", 1),
    ("cassandra-env.sh", "FOO=\\\n  bar\n", HEADER + "FOO=\\\nbar\n", 1),
    ("cassandra-env.sh", '  JVM_OPTS="$JVM_OPTS -Dx=1"  # c\n', HEADER + 'JVM_OPTS="$JVM_OPTS -Dx=1"  # c\n', 2),
    ("cassandra-env.sh", "  export X='a'  # c\n", HEADER + 'export X=a\n', 2),
    ("cassandra-env.sh", "export X=a\n", HEADER + 'X=a\n', 1),
    # a quote or heredoc going on to the next lines: their comments, blank lines and spaces count
    ("cassandra-env.sh", 'X="a\n# b\n"\n', HEADER + 'X="a\n"\n', 1),
    ("cassandra-env.sh", 'X="a\n\nb"\n', HEADER + 'X="a\nb"\n', 1),
    ("cassandra-env.sh", 'X="$a\n  b   c\n$d"\n', HEADER + 'X="$a\nb c\n$d"\n', 1),
    ("cassandra-env.sh", "cat >f <<EOF\na   b\n# c\nEOF\n", HEADER + "cat >f <<EOF\na b\nEOF\n", 1),
    # quotes that protect ; | & < > ( ): not the same command
    ("cassandra-env.sh", 'X="a|b"\n', HEADER + "X=a|b\n", 1),
    ("cassandra-env.sh", "a=1;b=2\n", HEADER + '"a=1;b=2"\n', 1),
    # CRLF: bash reads X=8G\r
    ("cassandra-env.sh", "X=8G\r\n", HEADER + "X=8G\n", 1),
    # a # in a word opens nothing for shlex, a string for bash
    ("cassandra-env.sh", 'X=a#"b\n#"\n', HEADER + 'X=a#"b\n# foo"\n', 1),
    # the JVM gets -Xmx8G\r; grep splits on \n only
    ("jvm-server.options", "-Xmx8G\r\n", HEADER + "-Xmx8G\n", 1),
    ("jvm-server.options", "-Da=1\x0b-Db=2\n", HEADER + "-Da=1\n-Db=2\n", 1),
    # key=value files: order, spacing and comments don't count
    ("cassandra-rackdc.properties", "rack=r1\ndc = dc1\n", HEADER + "# the DC\ndc=dc1\nrack=r1\n# prefer_local=true\n", 2),
    ("cassandra-rackdc.properties", "dc=dc1\nrack=r1\n", "dc=dc1\nrack=r2\n", 1),
    ("cassandra.yaml", "cluster_name: 'A'\nnum_tokens: 16\n", HEADER + "# tokens\nnum_tokens: 16\ncluster_name: 'A'\n", 2),
    ("cassandra.yaml", "cluster_name: 'A'\n", "cluster_name: 'B'\n", 1),
    # 1, 1.0 and true are not the same setting
    ("cassandra.yaml", "x: 1\n", HEADER + "x: true\n", 1),
    ("logback.xml", "<configuration>\n</configuration>\n",
     "<!-- Managed by Ansible -->\n<!--\n a licence\n-->\n<configuration>\n</configuration>\n", 2),
    ("logback.xml", "<configuration/>\n", "<configuration>\n</configuration>\n", 1),
    # no live file: written
    ("jvm-server.options", None, HEADER, 1),
])
def test_preview_tells_same_settings(tmp_path, name, live, new, rc):
    assert compare(tmp_path, name, live, new) == rc


def kept(results, initialized=True, normalize=False, slurped=None):
    templates = task("List the files to change and whether to ask first")["vars"]
    variables = dict(cassandra_config_normalize=normalize, cassandra_config_initialized={"stat": {"exists": initialized}},
                     cassandra_config_preview={"results": results}, cassandra_conf_dir="/etc/c",
                     cassandra_config_tmp={"path": "/tmp/t"})
    if slurped is not None:
        variables["cassandra_config_yaml_files"] = {"results": slurped}
    variables["_same_on_controller"] = render(templates["_same_on_controller"], **variables)
    return render(templates["_kept"], **variables)


RESULTS = [{"cassandra_config_file": "cassandra.yaml", "rc": 1, "stdout": "diff"},
           {"cassandra_config_file": "jvm-server.options", "rc": 2, "stdout": "diff"},
           {"cassandra_config_file": "logback.xml", "rc": 0, "stdout": ""}]


def test_same_settings_kept_on_a_running_node_only():
    assert kept(RESULTS) == ["jvm-server.options"]
    assert kept(RESULTS, initialized=False) == []  # a new node gets every file of the role
    assert kept(RESULTS, normalize=True) == []


TEMPLATES = os.path.join(TASKS, "..", "templates")
with open(os.path.join(TEMPLATES, "jmxremote.access.j2"), encoding="utf-8") as f:
    ACCESS = f.read()
with open(os.path.join(TEMPLATES, "jmxremote.password.j2"), encoding="utf-8") as f:
    PASSWORD = f.read()
JMX_SCRIPT = task("Compare the JMX files with the users", "access.yml")["ansible.builtin.command"]["argv"][2]


@pytest.mark.parametrize("create_unregister, access", [
    (True, "ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n    unregister\nmon readonly\n"),
    (False, "ops readwrite\nmon readonly\n"),
])
def test_jmx_access_rights(create_unregister, access):
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite", "create_unregister": create_unregister},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    assert render(ACCESS, cassandra_jmx_users=users) == access


def jmx_same(tmp_path, password, access, users):
    """What the JMX compare finds the same on a node with these files."""
    root = tmp_path / "etc" / "cassandra"
    root.mkdir(parents=True)
    for name, text in (("password", password), ("access", access)):
        if text is not None:
            (root / ("jmxremote." + name)).write_text(text)
    script = JMX_SCRIPT.replace("/etc/cassandra/", str(root) + "/")
    wanted = {"password": render(PASSWORD, cassandra_jmx_users=users),
              "access": render(ACCESS, cassandra_jmx_users=users)}
    out = subprocess.run([sys.executable, "-c", script], input=json.dumps(wanted).encode(),
                         stdout=subprocess.PIPE, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("password, access, same", [
    # written by hand: comments, spacing, rights in another order
    ("# JMX\nops  s3cret\nmon m0n\n", "# rights\nmon readonly\nops readwrite unregister create javax.management.monitor.*,javax.management.timer.*\n",
     ["password", "access"]),
    ("ops other\nmon m0n\n", "ops readwrite\nmon readonly\n", []),
    (None, None, []),
])
def test_jmx_files_with_the_same_users_are_kept(tmp_path, password, access, same):
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite"},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    assert jmx_same(tmp_path, password, access, users) == same


IDENTITY = task("Compare the settings a joined node must keep")["ansible.builtin.command"]["argv"][2]


@pytest.mark.parametrize("live, new, changes", [
    ("cluster_name: \"it's\"\nnum_tokens: 16\n", "cluster_name: 'it''s'\nnum_tokens: 16\n", []),
    ("cluster_name: 'X' # prod\nnum_tokens: 16 # fixed\n", "cluster_name: 'X'\nnum_tokens: 16\n", []),
    ("cluster_name: 'X'\ninitial_token: 0\n", "cluster_name: 'X'\nnum_tokens: 1\ninitial_token: 0\n", []),
    ("cluster_name: \"say \\\"hi\\\" a\\\\b\"\n", "cluster_name: 'say \"hi\" a\\b'\n", []),
    ("cluster_name: 'X'\nnum_tokens: 16\n", "cluster_name: 'Y'\nnum_tokens: 256\n",
     ["cluster_name: X -> Y", "num_tokens: 16 -> 256"]),
])
def test_identity_compared_as_yaml_reads_it(tmp_path, live, new, changes):
    assert identity_changes(tmp_path, live, new) == changes


def identity_changes(tmp_path, live, new, live_rackdc="dc=d\n", new_rackdc="dc=d\n"):
    for name, text in (("live.yaml", live), ("new.yaml", new), ("live.p", live_rackdc), ("new.p", new_rackdc)):
        (tmp_path / name).write_text(text)
    out = subprocess.run([sys.executable, "-c", IDENTITY] + [str(tmp_path / n) for n in ("live.yaml", "new.yaml", "live.p", "new.p")],
                         stdout=subprocess.PIPE, universal_newlines=True, check=True)
    return json.loads(out.stdout)


def test_rack_comment_is_part_of_the_value(tmp_path):
    # properties have no inline comment: the snitch's rack is "r1 # old"
    assert identity_changes(tmp_path, "num_tokens: 16\n", "num_tokens: 16\n", "rack=r1 # old\n", "rack=r1\n") == [
        "rack: r1 # old -> r1"]


@pytest.mark.parametrize("live, changes", [
    # a comment after the value, other quotes: the same identity
    ("cluster_name: Prod Cluster   # prod\nnum_tokens: 16  # vnodes\n", []),
    ('cluster_name: "Prod Cluster"\nnum_tokens: 16\n', []),
    ("cluster_name: 'Prod Cluster'\nnum_tokens: 8\nnum_tokens: 16\n", []),  # set twice: the last one
    ("cluster_name: Other\nnum_tokens: 16\n", ["cluster_name: Other -> Prod Cluster"]),
])
def test_identity_of_a_joined_node_as_cassandra_reads_it(tmp_path, live, changes):
    (tmp_path / "live.yaml").write_text(live)
    (tmp_path / "new.yaml").write_text("cluster_name: 'Prod Cluster'\nnum_tokens: 16\n")
    for name in ("live.properties", "new.properties"):
        (tmp_path / name).write_text("dc=dc1\nrack=r1\n")
    out = subprocess.run([sys.executable, "-c", IDENTITY] + [str(tmp_path / n) for n in (
        "live.yaml", "new.yaml", "live.properties", "new.properties")], stdout=subprocess.PIPE, check=True).stdout
    assert json.loads(out) == changes


def test_yaml_without_pyyaml_on_the_node_is_left_to_the_controller(tmp_path):
    """No PyYAML where the script runs: exit 3, the controller compares (cassandra_same_yaml)."""
    (tmp_path / "live").mkdir()
    (tmp_path / "new").mkdir()
    (tmp_path / "live" / "cassandra.yaml").write_text("x: 1  # c\n")
    (tmp_path / "new" / "cassandra.yaml").write_text(HEADER + "x: 1\n")
    (tmp_path / "yaml.py").write_text("raise ImportError('no PyYAML')\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    rc = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path / "live" / "cassandra.yaml"),
                         str(tmp_path / "new" / "cassandra.yaml")], stdout=subprocess.PIPE, env=env, check=False).returncode
    assert rc == 3


@pytest.mark.parametrize("live, new, same", [
    ("cluster_name: A # c\nx: 1\nx: 2\n", "cluster_name: 'A'\nx: 2\n", True),
    ("x: TRUE\n", "x: true\n", True),
    ("x: 1\n", "x: true\n", False),
    ("", "x: 1\n", False),
    ("x: [\n", "x: 1\n", False),
])
def test_same_yaml_on_the_controller(live, new, same):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_same_yaml import cassandra_same_yaml
    assert cassandra_same_yaml(live, new) is same


def test_controller_fallback_keeps_the_same_yaml():
    """The kept list takes the files the controller found the same."""
    results = [{"cassandra_config_file": "cassandra.yaml", "rc": 3, "stdout": "d"},
               {"cassandra_config_file": "logback.xml", "rc": 2, "stdout": "d"}]
    slurped = [{"item": ["cassandra.yaml", "/etc/c"], "content": "eDogMSAjIGMK"},  # x: 1 # c
               {"item": ["cassandra.yaml", "/tmp/t"], "content": "eDogMQo="}]  # x: 1
    assert kept(results, slurped=slurped) == ["logback.xml", "cassandra.yaml"]
    slurped[1]["content"] = "eDogMgo="  # x: 2
    assert kept(results, slurped=slurped) == ["logback.xml"]
    assert kept(results, slurped=slurped[:1]) == ["logback.xml"]  # one of them not read: not the same
