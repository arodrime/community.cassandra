from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster on many nodes: no JVM started per node when its files say the versions (the Cassandra jar on
# the classpath, the release file of its Java), dnf's cached metadata asked first, and one phase line per phase
# (the ops callback prints nothing else while they run).

import os
import subprocess

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
READ = next(p for p in PLAYS if p["name"] == "Read every node")
RUNNING = next(t for t in READ["tasks"] if t["name"] == "Read the running Cassandra (conf dir, Cassandra and Java versions)")
SCRIPT = RUNNING["ansible.builtin.shell"]


def snippet(start, end):
    lines = SCRIPT.split("\n")
    first = next(i for i, line in enumerate(lines) if line.strip().startswith(start))
    last = next(i for i, line in enumerate(lines) if i > first and line.strip().startswith(end))
    return "\n".join(lines[first:last + 1])


def run(script, tmp_path):
    out = subprocess.run(["sh", "-c", script], cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, check=False, timeout=30)
    return out.stdout.strip()


@pytest.mark.parametrize("classpath, version", [
    ("/etc/cassandra/conf:/usr/share/cassandra/apache-cassandra-4.1.5.jar:/usr/share/cassandra/lib/x.jar", "4.1.5"),
    ("/opt/cassandra/conf:/opt/cassandra/lib/apache-cassandra-5.0.4.jar", "5.0.4"),
    ("/opt/c/lib/apache-cassandra-4.0.13-SNAPSHOT.jar", "from nodetool"),  # not a plain version: nodetool's
])
def test_version_from_the_jar(tmp_path, classpath, version):
    (tmp_path / "cmdline").write_bytes(b"java\0-cp\0" + classpath.encode() + b"\0org.apache.cassandra.service.CassandraDaemon\0")
    script = snippet("ver=$(", "echo \"version=$ver\"").replace("/proc/$pid/cmdline", "cmdline").replace(
        "{{ _cassandra_service_nodetool }} version", "echo 'ReleaseVersion: from nodetool'")
    assert run(script, tmp_path) == "version=" + version


@pytest.mark.parametrize("layout, release, java", [
    ("jdk/bin/java", 'JAVA_VERSION="11.0.20"', "11"),
    ("jdk/bin/java", 'JAVA_VERSION="17"', "17"),
    ("jdk/jre/bin/java", 'JAVA_VERSION="1.8.0_382"', "1.8"),  # JDK 8: its release file above jre/
    ("jdk/bin/java", None, "21"),  # no release file: the Java run once
])
def test_java_version_from_its_release_file(tmp_path, layout, release, java):
    (tmp_path / os.path.dirname(layout)).mkdir(parents=True)
    if release:
        (tmp_path / "jdk" / "release").write_text("IMPLEMENTOR=x\n" + release + "\n")
    script = "bin=%s/%s\n" % (tmp_path, layout) + snippet("jv=$(", "echo \"java=$jv\"").replace(
        "/proc/$pid/exe -XshowSettings:properties -version 2>&1", "echo '    java.specification.version = 21'")
    assert run(script, tmp_path) == "java=" + java


def test_dnf_cached_metadata_first():
    query = snippet("bad=x", "done")
    assert 'for c in -C ""' in query and "grep -q '^PKG ' && break" in query


def test_one_line_per_phase():
    names = ["Say what is read first", "Say what is read on the nodes", "Say what is read next on the nodes",
             "Say the inventory is laid out", "Say the self-check runs", "Say the inventory is written"]
    found = [t for play in PLAYS for t in play.get("tasks", []) if t.get("name") in names]
    assert [t["name"] for t in found] == names
    assert all(t["vars"]["cassandra_output"] is True for t in found)
    phase = found[1]
    variables = {"ansible_play_hosts_all": ["n%d" % i for i in range(30)], "_forks": "5"}
    text = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(phase["ansible.builtin.debug"]["msg"]))
    assert text == ("Reading 30 nodes: facts, packages, the running Cassandra, its config (5 at a time: forks = 30 in"
                    " ansible.cfg reads them together)...")
    variables["_forks"] = "50"
    text = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(phase["ansible.builtin.debug"]["msg"]))
    assert text == "Reading 30 nodes: facts, packages, the running Cassandra, its config..."
