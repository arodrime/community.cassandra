from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config leaves a running node's file alone when its settings are the
# same as the role's (a config written by hand: no header, other comments or
# layout), and writes remote JMX users the way the node has them.

import base64
import json
import os
import subprocess
import sys
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import cassandra_same_settings  # noqa: E402 pylint: disable=wrong-import-position

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


def compare(tmp_path, name, live, new, storage_dir=""):
    """0: the same text (the preview's exit code), 1: differ, 2: the same settings (kept on a running node)."""
    (tmp_path / "live").mkdir()
    (tmp_path / "new").mkdir()
    live_path, new_path = tmp_path / "live" / name, tmp_path / "new" / name
    if live is not None:
        live_path.write_text(live)
    new_path.write_text(new)
    rc = subprocess.run([sys.executable, "-c", SCRIPT, str(live_path), str(new_path)],
                        stdout=subprocess.PIPE, check=False).returncode
    assert rc in (0, 1)
    return 2 if rc == 1 and cassandra_same_settings(live, new, name, storage_dir) else rc


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
    ("jvm-server.options", "-Xss256k\n-Xmx1G\n", HEADER + "-Xmx1G\n-Xss256k\n", 2),  # other options: any order
    ("jvm-server.options", "-Xss256k\n-XX:+AlwaysPreTouch\n-Xmx1G\n", HEADER + "-Xss256k\n-Xmx1G\n-XX:+AlwaysPreTouch\n", 2),
    ("jvm-server.options", "-Dx=1\n-Dy=2\n-Dx=3\n", HEADER + "-Dy=2\n-Dx=3\n", 2),  # a name twice: the last one
    ("jvm-server.options", "-Dx=1\n-Dx=3\n", HEADER + "-Dx=3\n-Dx=1\n", 1),
    ("jvm-server.options", "-Xss512k\n", HEADER + "-Xss256k\n-Xmx1G\n-Xss512k\n", 1),
    ("jvm-server.options", "-Xss512k\n-Xmx1G\n", HEADER + "-Xss256k\n-Xmx1G\n-Xss512k\n", 2),
    # but cassandra-env.sh looks for +UseG1GC in all of them
    ("jvm-server.options", "-XX:+UseG1GC\n-XX:-UseG1GC\n", HEADER + "-XX:-UseG1GC\n", 1),
    # one setting under two spellings: the last one counts
    ("jvm-server.options", "-XX:MaxHeapSize=8G\n-Xmx4G\n", HEADER + "-Xmx4G\n-XX:MaxHeapSize=8G\n", 1),
    # options without a name keep their order; an option starting with -- takes the next word
    ("jvm-server.options", "-Xloggc:/a\n-Xloggc:/b\n", HEADER + "-Xloggc:/b\n-Xloggc:/a\n", 1),
    ("jvm-server.options", "--add-exports A\n--add-opens B\n", HEADER + "--add-exports B\n--add-opens A\n", 1),
    ("jvm-server.options", "-Xss256k foo\n", HEADER + "-Xss256k\nfoo\n", 1),  # foo: not a line starting with '-'
    ("jvm-server.options", "-Xss256k   -Xmx1G\n", HEADER + "-Xmx1G\n-Xss256k\n", 2),  # each word is an option
    # these add up: given twice, both count
    ("jvm-server.options", "-XX:OnOutOfMemoryError=/a\n-XX:OnOutOfMemoryError=b\n", HEADER + "-XX:OnOutOfMemoryError=b\n", 1),
    ("jvm-server.options", "-javaagent:/a.jar\n-javaagent:/a.jar\n", HEADER + "-javaagent:/a.jar\n", 1),
    ("jvm-server.options", "-XX:StartFlightRecording=a\n-XX:StartFlightRecording=b\n", HEADER + "-XX:StartFlightRecording=b\n", 1),
    # an option the unlock one must come before
    ("jvm-server.options", "-XX:+UnlockDiagnosticVMOptions\n-XX:+LogVMOutput\n",
     HEADER + "-XX:+LogVMOutput\n-XX:+UnlockDiagnosticVMOptions\n", 1),
    ("jvm-server.options", "-XX:+UnlockDiagnosticVMOptions\n-Xss256k\n-XX:+LogVMOutput\n",
     HEADER + "-Xss256k\n-XX:+UnlockDiagnosticVMOptions\n-XX:+LogVMOutput\n", 2),
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
    # Cassandra reads a quoted number as the number, a key with no value as not set
    ("cassandra.yaml", 'concurrent_reads: "32"\nx:\n', HEADER + "concurrent_reads: 32\n", 2),
    ("cassandra.yaml", "x: TRUE\n", HEADER + "x: true\n", 2),
    ("cassandra.yaml", "x: yes\n", HEADER + "x: true\n", 2),  # a boolean setting
    # yes is true for a boolean setting only (a parameter map keeps the text, which parseBoolean reads false)
    ("cassandra.yaml", "p:\n  parameters:\n    fail_on_missing_provider: yes\n",
     HEADER + "p:\n  parameters:\n    fail_on_missing_provider: true\n", 1),
    ("cassandra.yaml", "x: 'yes'\n", HEADER + "x: true\n", 1),
    # an inline comment
    ("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS -Dx=1" # why\n', HEADER + 'JVM_OPTS="$JVM_OPTS -Dx=1"\n', 2),
    # prefer_local=false: as not set
    ("cassandra-rackdc.properties", "dc=dc1\nrack=r1\nprefer_local=false\n", HEADER + "dc=dc1\nrack=r1\n", 2),
    # a comment line never goes on to the next one: dc=dc2 counts
    ("cassandra-rackdc.properties", "dc=dc1\n# note \\\ndc=dc2\n", HEADER + "dc=dc1\n", 1),
    ("cassandra-rackdc.properties", "dc=d\\\n   c1\n", HEADER + "dc=dc1\n", 2),
    ("logback.xml", "<configuration>\n</configuration>\n",
     "<!-- Managed by Ansible -->\n<!--\n a licence\n-->\n<configuration>\n</configuration>\n", 2),
    ("logback.xml", "<configuration/>\n", "<configuration>\n</configuration>\n", 2),
    # re-indented, attributes in another order, a level in another case
    ("logback.xml", '<configuration>\n<root level="info"><appender-ref ref="A"/></root>\n</configuration>\n',
     '<configuration>\n  <root level="INFO">\n    <appender-ref ref="A" />\n  </root>\n</configuration>\n', 2),
    ("logback.xml", '<configuration><root level="INFO"/></configuration>', '<configuration><root level="WARN"/></configuration>', 1),
    ("logback.xml", "<configuration>\n", "<configuration/>\n", 1),  # not XML: not the same
    # no live file: written
    ("jvm-server.options", None, HEADER, 1),
])
def test_preview_tells_same_settings(tmp_path, name, live, new, rc):
    assert compare(tmp_path, name, live, new) == rc


def b64(text):
    return base64.b64encode(text.encode()).decode()


def kept(files, storage_dir="", given_dir=""):
    """The files left as they are, from {name: (live, role's)} read by "Read the files that differ"."""
    templates = task("List the files to change and whether to ask first")["vars"]
    results = []
    for name, (live, new) in files.items():
        if live is not None:
            results.append({"item": [name, "/etc/c"], "content": b64(live)})
        else:
            results.append({"item": [name, "/etc/c"], "failed": True})
        results.append({"item": [name, "/tmp/t"], "content": b64(new)})
    variables = dict(cassandra_conf_dir="/etc/c", cassandra_config_tmp={"path": "/tmp/t"},
                     cassandra_config_compared={"results": results}, cassandra_jvm={"storagedir": storage_dir},
                     cassandra_config_storage_dir=given_dir)
    return render(templates["_kept"], **variables)


def test_same_settings_kept_by_the_controller():
    assert kept({"cassandra.yaml": ("x: 1  # c\n", HEADER + "x: 1\n"), "logback.xml": ("<a/>", "<b/>"),
                 "jvm-server.options": (None, "-Xss256k\n")}) == ["cassandra.yaml"]


def test_dirs_left_to_the_storage_dir_are_kept():
    # a cassandra.yaml without the directories (a tarball's): they are under -Dcassandra.storagedir
    live = "cluster_name: A\n"
    new = ("cluster_name: A\ndata_file_directories:\n    - /var/lib/cassandra/data\ncommitlog_directory: /var/lib/cassandra/commitlog\n"
           "saved_caches_directory: /var/lib/cassandra/saved_caches\nhints_directory: /var/lib/cassandra/hints\n")
    assert kept({"cassandra.yaml": (live, new)}, "/var/lib/cassandra") == ["cassandra.yaml"]
    assert kept({"cassandra.yaml": (live, new)}, "/opt/cassandra/data") == []
    assert kept({"cassandra.yaml": (live, new)}) == []  # not running: unknown
    assert kept({"cassandra.yaml": (live, new)}, given_dir="/var/lib/cassandra") == ["cassandra.yaml"]  # imported


def test_compared_on_a_running_node_only():
    read = task("Read the files that differ, to compare their settings")
    assert "cassandra_config_initialized.stat.exists" in read["when"]
    assert "not cassandra_config_normalize | bool" in read["when"]


def test_preview_needs_no_pyyaml_on_the_node():
    assert "yaml" not in SCRIPT


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
with open(os.path.join(TASKS, "..", "vars", "main.yml"), encoding="utf-8") as f:
    SNITCHES = json.dumps(yaml.safe_load(f)["_cassandra_config_snitches_without_rackdc"])


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
    out = subprocess.run([sys.executable, "-c", IDENTITY] + [str(tmp_path / n) for n in ("live.yaml", "new.yaml", "live.p", "new.p")]
                         + [SNITCHES],
                         stdout=subprocess.PIPE, universal_newlines=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("live, new, changes", [
    # SimpleSnitch takes no dc or rack from the file: its dc= and rack= are not the node's
    ("endpoint_snitch: SimpleSnitch\n", "endpoint_snitch: SimpleSnitch\n", []),
    ("endpoint_snitch: org.apache.cassandra.locator.PropertyFileSnitch\n", "endpoint_snitch: PropertyFileSnitch\n",
     ["endpoint_snitch: org.apache.cassandra.locator.PropertyFileSnitch -> PropertyFileSnitch"]),
    ("endpoint_snitch: Ec2Snitch\n", "endpoint_snitch: Ec2Snitch\n", []),
    # GossipingPropertyFileSnitch does, on either side
    ("endpoint_snitch: GossipingPropertyFileSnitch\n", "endpoint_snitch: GossipingPropertyFileSnitch\n",
     ["dc: DC_EXAMPLE -> datacenter1", "rack: RACK_A -> rack1"]),
    ("endpoint_snitch: org.apache.cassandra.locator.GossipingPropertyFileSnitch\n",
     "endpoint_snitch: GossipingPropertyFileSnitch\n",
     ["dc: DC_EXAMPLE -> datacenter1",
      "endpoint_snitch: org.apache.cassandra.locator.GossipingPropertyFileSnitch -> GossipingPropertyFileSnitch",
      "rack: RACK_A -> rack1"]),
    ("endpoint_snitch: SimpleSnitch\n", "endpoint_snitch: GossipingPropertyFileSnitch\n",
     ["dc: DC_EXAMPLE -> datacenter1", "endpoint_snitch: SimpleSnitch -> GossipingPropertyFileSnitch",
      "rack: RACK_A -> rack1"]),
    # a snitch of another class may read them
    ("endpoint_snitch: com.example.SimpleSnitch\n", "endpoint_snitch: com.example.SimpleSnitch\n",
     ["dc: DC_EXAMPLE -> datacenter1", "rack: RACK_A -> rack1"]),
])
def test_rackdc_identity_only_where_the_snitch_reads_it(tmp_path, live, new, changes):
    assert identity_changes(tmp_path, live, new, "dc=DC_EXAMPLE\nrack=RACK_A\n", "dc=datacenter1\nrack=rack1\n") == changes


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
        "live.yaml", "new.yaml", "live.properties", "new.properties")] + [SNITCHES], stdout=subprocess.PIPE,
        check=True).stdout
    assert json.loads(out) == changes


RACKDC_GUARD = task("Refuse rackdc lines other than the node's dc and rack under a snitch that reads them")


@pytest.mark.parametrize("snitch, rackdc, ok", [
    ("SimpleSnitch", ("DC_EXAMPLE", "RACK_A"), True),  # not read: kept as the node has them
    ("org.apache.cassandra.locator.SimpleSnitch", ("DC_EXAMPLE", "RACK_A"), True),
    ("GossipingPropertyFileSnitch", ("datacenter1", "rack1"), True),
    # read: the node would move to the file's dc (a snitch change after an import under SimpleSnitch)
    ("GossipingPropertyFileSnitch", ("DC_EXAMPLE", "rack1"), False),
    ("GossipingPropertyFileSnitch", ("datacenter1", "RACK_A"), False),
    ("com.example.SimpleSnitch", ("DC_EXAMPLE", "RACK_A"), False),  # another class may read them
])
def test_rackdc_lines_are_the_node_dc_and_rack_under_a_snitch_that_reads_them(snitch, rackdc, ok):
    with open(os.path.join(TASKS, "..", "vars", "main.yml"), encoding="utf-8") as f:
        unread = yaml.safe_load(f)["_cassandra_config_snitches_without_rackdc"]
    that = RACKDC_GUARD["ansible.builtin.assert"]["that"]
    assert render("{{ %s }}" % that, cassandra_endpoint_snitch=snitch, cassandra_dc="datacenter1", cassandra_rack="rack1",
                  cassandra_rackdc_dc=rackdc[0], cassandra_rackdc_rack=rackdc[1],
                  _cassandra_config_snitches_without_rackdc=unread) is ok
