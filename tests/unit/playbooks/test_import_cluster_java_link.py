from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a node whose /usr/bin/java is another Java than the running one keeps it
# (cassandra_java_set_default: false: in the cluster's group_vars when every node was read and does, else in that
# node's host_vars), and cassandra_install then leaves /usr/bin/java as it is, with a package Java or a Java home:
# site.yml after the import changes nothing there. A node with no /usr/bin/java gets one (nodetool and the scripts
# need a java); a node whose Java was updated since Cassandra started (its directory gone) runs the new build of it
# once restarted: the same Java.

import os
import shutil
import subprocess

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.parsing.splitter import split_args
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    GENERATED, cassandra_inventory_files, cassandra_inventory_layout, cassandra_inventory_leftovers)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import (
    cassandra_import_convergence)
from ansible_collections.community.cassandra.tests.unit.import_nodes import node

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
OFF = {"cassandra_java_set_default": False}
NOTE = "the system java (/usr/bin/java), which is not the running one (cassandra_java_set_default: false)"
TRUE_WOULD = ("true would make the running Java the system java (alternatives), for every program that runs java")
OLD_RHEL = "/usr/lib/jvm/java-11-openjdk-11.0.20.0.8-1.el8.x86_64/bin/java"
NEW_RHEL = "/usr/lib/jvm/java-11-openjdk-11.0.21.0.9-1.el8.x86_64/bin/java"

with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name, tasks=None):
    todo = list(tasks) if tasks is not None else [t for play in PLAYS for t in play.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def java_off(hv):
    keep = node(hv)["keep"]
    return dict((k, v) for k, v in keep.items() if k == "cassandra_java_set_default")


@pytest.mark.parametrize("system, running, expected", [
    ("/apps/jdk17/bin/java", "/apps/jdk17/bin/java", {}),
    # Java 17 tarball run through JAVA_HOME, /usr/bin/java the Java 11 package of other programs
    ("/usr/lib/jvm/java-11-openjdk-11.0.25/bin/java", "/srv/java/jdk-17.0.20+1-jre/bin/java", OFF),
    # none, or leading nowhere: the roles make it (nodetool needs one)
    ("", "/srv/java/jdk-17/bin/java", {}),
    # RHEL's Java 8: /usr/bin/java its jre/bin/java, Cassandra run by its bin/java: the same JDK
    ("/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.382.b05-2.el8.x86_64/jre/bin/java",
     "/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.382.b05-2.el8.x86_64/bin/java", {}),
    ("/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.382.b05-2.el8.x86_64/jre/bin/java",
     "/usr/lib/jvm/java-11-openjdk-11.0.21.0.9-1.el8.x86_64/bin/java", OFF),
    # a home ending in jre (a Temurin JRE) is not a Java 8 jre/
    ("/srv/java/jdk-17.0.20+1-jre/bin/java", "/srv/java/jdk-17.0.20+1/bin/java", OFF),
])
def test_java_link(system, running, expected):
    assert java_off({"import_cluster_system_java": system, "import_cluster_java_bin": running}) == expected


def test_running_java_unknown_writes_nothing():
    assert java_off({"import_cluster_system_java": "", "import_cluster_java_bin": ""}) == {}


def test_jdk_updated_while_cassandra_runs():
    # RHEL: the JDK updated since Cassandra started, its directory gone ("... (deleted)"), /usr/bin/java its new
    # build: the same Java, nothing written, a restart pending said
    hv = {"import_cluster_java_updated": OLD_RHEL, "import_cluster_java_bin": NEW_RHEL,
          "import_cluster_system_java": NEW_RHEL, "import_cluster_java_package": "java-11-openjdk-headless"}
    out = node(hv)
    assert "cassandra_java_set_default" not in out["keep"]
    assert ("; restart pending on a new JDK: Cassandra still runs %s, removed by an update since it started, and runs"
            " its new build %s once restarted" % (OLD_RHEL, NEW_RHEL)) in out["notes"][0]
    assert "cassandra_java_home" not in str(out["vars"])


@pytest.mark.parametrize("system, note", [
    ("/usr/lib/jvm/java-11/bin/java", ", /usr/bin/java is /usr/lib/jvm/java-11/bin/java"),
    ("", ", /usr/bin/java is missing: the roles will point it at the running Java"),
])
def test_versions_note(system, note):
    hv = {"import_cluster_system_java": system, "import_cluster_java_bin": "/srv/jdk-17/bin/java"}
    assert node(hv)["notes"][0].startswith("Cassandra 4.1.5, Java 11" + note)


def layout(*keeps, **unread):
    base = {"dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"}, "hand_edits": [],
            "normalized": [], "notes": []}
    nodes = [dict(base, name="n%d" % (i + 1), address="10.0.0.%d" % (i + 1), keep=dict(k))
             for i, k in enumerate(keeps)]
    nodes += [{"name": name, "address": "10.0.1.%d" % (i + 1), "dc": "dc1", "rack": "r1", "read": False,
               "reason": "unreachable", "keep": dict(keep)} for i, (name, keep) in enumerate(sorted(unread.items()))]
    return cassandra_inventory_layout(nodes, "c")


def test_mixed_cluster_per_node():
    # only the nodes that need it, in their host_vars: the others, and a node added later, get the running Java
    out = layout(OFF, {}, OFF)
    assert out["host_vars"]["n1"] == OFF and out["host_vars"]["n3"] == OFF
    assert "n2" not in out["host_vars"]
    assert "cassandra_java_set_default" not in str(out["group_vars"])
    assert NOTE in out["report"]
    assert ("SYSTEM JAVA: /usr/bin/java is another Java than the one Cassandra runs on n1, n3; the roles leave it as"
            " it is (cassandra_java_set_default: false in their host_vars). " + TRUE_WOULD) in out["report"]


def test_every_node_in_group_vars():
    out = layout(OFF, OFF)
    assert out["group_vars"]["c"]["cassandra_java_set_default"] is False
    assert out["host_vars"] == {}
    # said once for the cluster, not under each node
    assert NOTE not in out["report"]
    assert ("SYSTEM JAVA: /usr/bin/java is another Java than the one Cassandra runs on every node; the roles leave it"
            " as it is (cassandra_java_set_default: false in group_vars/c/main.yml, for nodes added later too: one"
            " without a /usr/bin/java gets the running Java's, one with another needs JAVA_HOME, e.g. in"
            " cassandra_service_environment). " + TRUE_WOULD) in out["report"]
    # the nodes still say they keep it (the convergence and the report read it there)
    assert all(n["keep"] == OFF for n in out["nodes"])


def test_every_node_with_other_switches():
    out = layout(dict(OFF, cassandra_linux_manage=False), OFF)
    assert out["group_vars"]["c"]["cassandra_java_set_default"] is False
    assert out["host_vars"]["n1"] == {"cassandra_linux_manage": False, "cassandra_imported_host": True}
    assert "n2" not in out["host_vars"]


def test_a_node_not_read_keeps_it_per_node():
    # every node read has it, one node was not read: not every node of the cluster known, host_vars only; the node
    # not read keeps its own (its setup unknown), said on its NOT READ line
    out = layout(OFF, OFF, n9=dict(OFF, cassandra_linux_manage=False))
    assert "cassandra_java_set_default" not in str(out["group_vars"])
    assert out["host_vars"]["n1"] == OFF and out["host_vars"]["n2"] == OFF
    assert out["host_vars"]["n9"]["cassandra_java_set_default"] is False
    assert "on n1, n2; the roles leave it as it is (cassandra_java_set_default: false in their host_vars)" in out["report"]
    assert "The roles leave their setup as it is (host_vars: cassandra_java_set_default, cassandra_linux_manage false)" \
        in out["report"]


def test_a_node_not_read_line_without_the_grouped_switch():
    # grouped (every node read), then a node not read on the next import: back in host_vars, its NOT READ line
    # names only what is in its host_vars
    out = layout(OFF, OFF)
    assert "NOT READ" not in out["report"]
    out = layout(OFF, OFF, n9={"cassandra_linux_manage": False})
    assert "(host_vars: cassandra_linux_manage false)" in out["report"]
    assert "cassandra_java_set_default" not in str(out["host_vars"].get("n9"))


def test_reimport_moves_host_vars_to_group_vars():
    # a first import with n2 on the running Java (n1's line in host_vars), the next one with both on another: n1's
    # host_vars file is stale (removed), the group's main.yml says it
    def files(out):
        return dict((f["path"], GENERATED % "c" + "\n" + f["content"]) for f in cassandra_inventory_files(out))
    first, second = files(layout(OFF, {})), files(layout(OFF, OFF))
    assert "cassandra_java_set_default: false" in first["host_vars/n1/main.yml"]
    assert "cassandra_java_set_default" not in first["group_vars/c/main.yml"]
    assert "cassandra_java_set_default: false" in second["group_vars/c/main.yml"]
    assert not any(p.startswith("host_vars/") for p in second)
    out = cassandra_inventory_leftovers(sorted(first), dict((p, t.split("\n")[0]) for p, t in first.items()),
                                        sorted(second), "c")
    assert out["stale"] == ["host_vars/n1/main.yml"] and out["conflicts"] == []


@pytest.mark.parametrize("keeps, said", [((OFF, OFF), False), ((OFF, {}), True)])
def test_convergence_names_host_vars_only(keeps, said):
    # the CONVERGENCE section lists the switches in host_vars: not one in the cluster's group_vars
    text = "\n".join(cassandra_import_convergence(layout(*keeps), width=160))
    assert "CONVERGENCE" in text
    assert ("Also in host_vars: cassandra_java_set_default: false" in text) is said


def test_convergence_hides_the_java_switch_only():
    # another switch in the cluster's group_vars (yours) is still said to be in host_vars
    out = layout(dict(OFF, cassandra_linux_manage=False), OFF)
    out["group_vars"]["c"]["cassandra_linux_manage"] = False
    text = "\n".join(cassandra_import_convergence(out, width=160))
    assert "Also in host_vars: cassandra_linux_manage: false (LEFT AS IT IS above)" in text


def test_same_java_everywhere_writes_nothing():
    out = layout({}, {})
    assert "cassandra_java_set_default" not in str(out["group_vars"]) + str(out["host_vars"])
    assert "SYSTEM JAVA" not in out["report"]


def test_unread_nodes_keep_it_too():
    hv = {"import_cluster_system_java": "", "import_cluster_java_bin": "/x/bin/java"}
    assert node(hv, read=False)["keep"]["cassandra_java_set_default"] is False


def _java_lines():
    script = task("Read the running Cassandra (conf dir, Cassandra and Java versions)")["ansible.builtin.shell"]
    lines = script.split("\n")
    start = next(i for i, x in enumerate(lines) if x.strip().startswith("bin=$(readlink /proc/$pid/exe"))
    end = next(i for i, x in enumerate(lines) if x.strip() == 'echo "sysjava=$sys"')
    return lines[start:end + 1]


def test_import_task_prints_the_java_fields():
    text = "\n".join(_java_lines())
    for field in ("javaupdated=$bin", "java=$jv", "javabin=$bin", "javapkg=$(package \"$bin\")", "sysjava=$sys"):
        assert 'echo "%s"' % field in text
    assert "sys=$([ -e /usr/bin/java ] && readlink -f /usr/bin/java)" in text
    work_out = task("Work out the series and the conf dir")["ansible.builtin.set_fact"]
    assert work_out["import_cluster_java_updated"] == "{{ _kv.javaupdated | default('') }}"
    assert work_out["import_cluster_system_java"] == "{{ _kv.sysjava | default('') }}"


def _write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def _run(tmp_path, running, system, rpm=None, dpkg=None, unit_env="", java_home=""):
    """The task's Java lines over tmp_path: the JVM run from running (a JVM whose directory is gone cannot run, as
    a real one: its libraries went too), /usr/bin/java a chain of links to system (none when None), rpm answering
    rpm ({"files": {package: [files]}, "owners": {file: package}}; none: no rpm), dpkg -S answering dpkg ({file:
    "package:arch"}), the unit's Environment unit_env, and nothing else of the machine running the test."""
    usr_bin_java = tmp_path / "usr" / "bin" / "java"
    usr_bin_java.parent.mkdir(parents=True)
    if system is not None:
        (tmp_path / "alternative").symlink_to(system)
        usr_bin_java.symlink_to(tmp_path / "alternative")
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("sed", "head", "cut", "readlink", "grep", "tr"):
        (tools / tool).symlink_to(shutil.which(tool))
    if rpm is not None:
        cases = ["  '-ql %s') printf '%%s\\n' %s ;;" % (pkg, " ".join("'%s'" % f for f in files))
                 for pkg, files in rpm.get("files", {}).items()]
        cases += ["  '-qf %s') echo %s ;;" % (f, pkg) for f, pkg in rpm.get("owners", {}).items()]
        _write(tools / "rpm", "\n".join(["#!/bin/sh", 'case "$1" in -qf) k="$1 $4" ;; *) k="$1 $2" ;; esac',
                                         'case "$k" in'] + cases
               + ['  *) echo "package $2 is not installed"; exit 1 ;;', "esac", ""]), 0o755)
    cases = "".join('  "%s") echo "%s: %s" ;;\n' % (f, pkg, f) for f, pkg in (dpkg or {}).items())
    _write(tools / "dpkg", '#!/bin/sh\ncase "$2" in\n%s  *) exit 1 ;;\nesac\n' % cases, 0o755)
    _write(tools / "systemctl", "#!/bin/sh\necho '%s'\n" % unit_env, 0o755)
    exe = tmp_path / "proc_exe"  # /proc/<pid>/exe: the JVM as it still runs, only while its directory is there
    _write(exe, '#!/bin/sh\n[ -d "%s" ] || exit 127\necho "    java.specification.version = 11" >&2\n'
           % os.path.dirname(str(running)), 0o755)
    lines = ['bin="%s"' % running] + _java_lines()[1:]
    environ = tmp_path / "proc_environ"  # /proc/<pid>/environ: the JVM's environment
    environ.write_bytes(b"PATH=/bin\0" + (("JAVA_HOME=%s\0" % java_home).encode() if java_home else b""))
    script = "\n".join(lines).replace("/usr/bin/java", str(usr_bin_java)).replace("/proc/$pid/exe", str(exe)).replace(
        "/proc/$pid/environ", str(environ))
    out = subprocess.run(["/bin/sh", "-c", script], env={"PATH": str(tools)}, stdout=subprocess.PIPE,
                         universal_newlines=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def _jdk(tmp_path, name, version, jre=False, release=True):
    """A Java home: its java says its version when run, its release file names it (unless not release)."""
    home = tmp_path / "jvm" / name
    java = home / "jre" / "bin" / "java" if jre else home / "bin" / "java"
    spec = version.split(".")[0] if not version.startswith("1.") else ".".join(version.split(".")[:2])
    _write(java, '#!/bin/sh\necho "    java.specification.version = %s" >&2\n' % spec, 0o755)
    if release:
        _write(home / "release", 'JAVA_VERSION="%s"\n' % version)
    return java


def _gone(tmp_path, name, jre=False):
    return tmp_path / "jvm" / name / ("jre/bin/java" if jre else "bin/java")


def test_lines_resolve_symlink_chains(tmp_path):
    java = _jdk(tmp_path, "jdk-17.0.20", "17.0.20")
    (tmp_path / "current").symlink_to(java.parent.parent)
    found = _run(tmp_path, java, tmp_path / "current" / "bin" / "java")
    # the "current" link /usr/bin/java goes through: kept pointing at the Java in use (cassandra_java_link)
    assert found == {"java": "17", "javabin": str(java), "javapkg": "", "sysjava": str(java),
                     "javalink": str(tmp_path / "current")}


def test_lines_java_link_of_a_hand_made_layout(tmp_path):
    # /usr/bin/java -> /apps/cassandra/jre/jdk/bin/java, /apps/cassandra/jre/jdk -> jdk-11.0.30 (relative)
    java = _jdk(tmp_path, "jdk-11.0.30", "11.0.30")
    jre = tmp_path / "apps" / "cassandra" / "jre"
    jre.mkdir(parents=True)
    (jre / "jdk-11.0.30").symlink_to(java.parent.parent)
    (jre / "jdk").symlink_to("jdk-11.0.30")
    assert _run(tmp_path, java, jre / "jdk" / "bin" / "java")["javalink"] == str(jre / "jdk")
    # a link to another Java than the running one: none
    other = tmp_path / "other"
    other.mkdir()
    java = _jdk(other, "jdk-11.0.30", "11.0.30")
    old = _jdk(other, "jdk-11.0.20", "11.0.20")
    (other / "jdk").symlink_to(old.parent.parent)
    assert "javalink" not in _run(other, java, other / "jdk" / "bin" / "java")
    # no link on the way (the alternatives straight to the home): none
    plain = tmp_path / "plain"
    plain.mkdir()
    java = _jdk(plain, "jdk-11.0.30", "11.0.30")
    assert "javalink" not in _run(plain, java, java)


def test_java_link_written_with_the_java_home_only():
    hv = {"import_cluster_java_home": "/apps/cassandra/jre/jdk-11.0.30", "import_cluster_java_link": "/apps/cassandra/jre/jdk",
          "import_cluster_java_bin": "/apps/cassandra/jre/jdk-11.0.30/bin/java"}
    out = node(hv)["vars"]
    assert out["cassandra_java_home"] == "/apps/cassandra/jre/jdk-11.0.30"
    assert out["cassandra_java_link"] == "/apps/cassandra/jre/jdk"
    work_out = task("Work out the series and the conf dir")["ansible.builtin.set_fact"]
    # a package Java: none (cassandra_install keeps the link for a Java home only)
    assert work_out["import_cluster_java_link"] == "{{ _kv.javalink | default('') if _kv.javapkg | default('') == '' else '' }}"


def test_lines_without_a_system_java(tmp_path):
    java = _jdk(tmp_path, "jdk-17.0.20", "17.0.20")
    assert _run(tmp_path, java, None)["sysjava"] == ""


def test_lines_package_of_dpkg(tmp_path):
    # no rpm: dpkg -S names the package (openjdk-11-jre-headless:amd64 -> openjdk-11-jre-headless)
    java = _jdk(tmp_path, "java-11-openjdk-amd64", "11.0.21")
    found = _run(tmp_path, java, java, dpkg={str(java): "openjdk-11-jre-headless:amd64"})
    assert found["javapkg"] == "openjdk-11-jre-headless" and found["java"] == "11"


def test_lines_without_a_release_file_run_the_java(tmp_path):
    java = tmp_path / "jvm" / "jdk-11" / "bin" / "java"
    _write(java, "#!/bin/sh\n", 0o755)
    assert _run(tmp_path, java, None)["java"] == "11"


def test_lines_tarball_updated_in_place(tmp_path):
    # the same path, a new build of the same major put there ("(deleted)" stripped, the path there again): read
    java = _jdk(tmp_path, "jdk-17", "17.0.21")
    found = _run(tmp_path, java, java)
    assert "javaupdated" not in found and found["java"] == "17" and found["javabin"] == str(java)


def test_lines_rhel_jdk_updated_while_cassandra_runs(tmp_path):
    # java-11-openjdk-11.0.20... removed by the update to 11.0.21 (java-11-openjdk-headless): the new build read as
    # the Java it runs, its package too (so no cassandra_java_home of the directory gone); the old JVM cannot run
    new = _jdk(tmp_path, "java-11-openjdk-11.0.21.0.9-1.el8.x86_64", "11.0.21")
    old = _gone(tmp_path, "java-11-openjdk-11.0.20.0.8-1.el8.x86_64")
    rpm = {"files": {"java-11-openjdk-headless": [str(new.parent), str(new)]},
           "owners": {str(new): "java-11-openjdk-headless"}}
    found = _run(tmp_path, old, new, rpm)
    assert found == {"javaupdated": str(old), "java": "11", "javabin": str(new),
                     "javapkg": "java-11-openjdk-headless", "sysjava": str(new)}
    work_out = task("Work out the series and the conf dir")["ansible.builtin.set_fact"]
    assert "_kv.javapkg | default('') == ''" in work_out["import_cluster_java_home"]  # a package: no home


def test_lines_rhel_java_11_updated_while_the_system_java_is_17(tmp_path):
    # its own package's new build, not /usr/bin/java (Java 17): read; the node then keeps its /usr/bin/java
    new = _jdk(tmp_path, "java-11-openjdk-11.0.21.0.9-1.el8.x86_64", "11.0.21")
    j17 = _jdk(tmp_path, "java-17-openjdk-17.0.9.0.9-2.el8.x86_64", "17.0.9")
    old = _gone(tmp_path, "java-11-openjdk-11.0.20.0.8-1.el8.x86_64")
    rpm = {"files": {"java-11-openjdk-headless": [str(new)]},
           "owners": {str(new): "java-11-openjdk-headless", str(j17): "java-17-openjdk-headless"}}
    found = _run(tmp_path, old, j17, rpm)
    assert found["javaupdated"] == str(old) and found["javabin"] == str(new) and found["java"] == "11"
    hv = {"import_cluster_java_updated": found["javaupdated"], "import_cluster_java_bin": found["javabin"],
          "import_cluster_system_java": found["sysjava"], "import_cluster_java_package": found["javapkg"]}
    out = node(hv)
    assert out["keep"]["cassandra_java_set_default"] is False
    assert "restart pending on a new JDK" in out["notes"][0]


def test_lines_java_8_jre_updated(tmp_path):
    # RHEL's Java 8: its jre/bin/java gone, the package's new jre/bin/java, the release file above jre/
    new = _jdk(tmp_path, "java-1.8.0-openjdk-1.8.0.392.b08-4.el8.x86_64", "1.8.0_392", jre=True)
    old = _gone(tmp_path, "java-1.8.0-openjdk-1.8.0.382.b05-2.el8.x86_64", jre=True)
    rpm = {"files": {"java-1.8.0-openjdk-headless": [str(new)]}, "owners": {str(new): "java-1.8.0-openjdk-headless"}}
    found = _run(tmp_path, old, new, rpm)
    assert found["javaupdated"] == str(old) and found["javabin"] == str(new) and found["java"] == "1.8"


def test_lines_package_of_another_major_is_not_its_build(tmp_path):
    # java-latest-openjdk went from 17 to 21: not the Java it ran; nor /usr/bin/java (21): left to the operator
    new = _jdk(tmp_path, "java-latest-openjdk-21.0.1.0.12-1.rolling.el8.x86_64", "21.0.1")
    old = _gone(tmp_path, "java-latest-openjdk-17.0.9.0.9-1.rolling.el8.x86_64")
    rpm = {"files": {"java-latest-openjdk-headless": [str(new)]}, "owners": {str(new): "java-latest-openjdk-headless"}}
    found = _run(tmp_path, old, new, rpm)
    assert "javaupdated" not in found
    assert found["java"] == "" and found["javabin"] == str(old)  # cannot be run any more: the node is not read
    error = task("Note a running Java that cannot be read")
    assert "import_cluster_java | default('') == ''" in error["when"]
    assert "restart Cassandra (rolling_restart), then import again" in error["ansible.builtin.set_fact"]["import_cluster_error"]


def test_lines_tarball_gone_and_the_system_java_of_its_major(tmp_path):
    new = _jdk(tmp_path, "jdk-17.0.21+9", "17.0.21")
    old = _gone(tmp_path, "jdk-17.0.20+8")
    found = _run(tmp_path, old, new, {})
    assert found["javaupdated"] == str(old) and found["javabin"] == str(new) and found["javapkg"] == ""


def test_lines_candidate_without_a_release_file_is_run(tmp_path):
    # the new build of its package has no release file: its major from the java run once
    new = _jdk(tmp_path, "java-11-openjdk-11.0.21.0.9-1.el8.x86_64", "11.0.21", release=False)
    old = _gone(tmp_path, "java-11-openjdk-11.0.20.0.8-1.el8.x86_64")
    found = _run(tmp_path, old, None, {"files": {"java-11-openjdk-headless": [str(new)]}})
    assert found["javaupdated"] == str(old) and found["javabin"] == str(new)


def test_lines_candidate_resolved_and_the_system_one_first(tmp_path):
    # rpm -ql names a link (resolved), and two builds of the package: the one /usr/bin/java is wins
    first = _jdk(tmp_path, "java-11-openjdk-11.0.21.0.9-1.el8.x86_64", "11.0.21")
    second = _jdk(tmp_path, "java-11-openjdk-11.0.22.0.7-1.el8.x86_64", "11.0.22")
    (tmp_path / "jvm" / "jre-11").symlink_to(second.parent.parent)
    old = _gone(tmp_path, "java-11-openjdk-11.0.20.0.8-1.el8.x86_64")
    rpm = {"files": {"java-11-openjdk-headless": [str(first), str(tmp_path / "jvm" / "jre-11" / "bin" / "java")]}}
    found = _run(tmp_path, old, second, rpm)
    assert found["javabin"] == str(second)
    again = tmp_path / "again"  # no /usr/bin/java: the first one
    again.mkdir()
    assert _run(again, old, None, rpm)["javabin"] == str(first)


def test_lines_tarball_gone_and_a_package_of_another_vendor(tmp_path):
    # a tarball gone, /usr/bin/java a package Java of its major: not its build, not read (restart, then import)
    new = _jdk(tmp_path, "java-17-openjdk-17.0.9.0.9-2.el8.x86_64", "17.0.9")
    old = _gone(tmp_path, "jdk-17.0.8+7")
    found = _run(tmp_path, old, new, {"owners": {str(new): "java-17-openjdk-headless"}})
    assert "javaupdated" not in found and found["java"] == ""


@pytest.mark.parametrize("unit_env, gone", [
    ("JAVA_HOME={old} MAX_HEAP_SIZE=8G", True),
    ("JAVA_HOME={old}/", True),
    ("JAVA_HOME={old}/jre", True),
    ("JAVA_HOME=/opt/java/jdk-17", False),
    ("", False),
])
def test_lines_unit_java_home_of_the_java_gone(tmp_path, unit_env, gone):
    new = _jdk(tmp_path, "java-11-openjdk-11.0.21.0.9-1.el8.x86_64", "11.0.21")
    old = _gone(tmp_path, "java-11-openjdk-11.0.20.0.8-1.el8.x86_64")
    home = str(old.parent.parent)
    found = _run(tmp_path, old, new, {"files": {"java-11-openjdk-headless": [str(new)]}},
                 unit_env=unit_env.format(old=home))
    assert found["javaupdated"] == str(old)
    assert found.get("javahomegone") == (home if gone else None)


def test_java_home_gone_refuses_the_node():
    # read as its new build, but its unit would start the Java gone: not imported, the way out said
    error = task("Note a JAVA_HOME of a Java gone")
    assert "import_cluster_java_home_gone | default('') != ''" in error["when"]
    assert "import_cluster_error is not defined" in error["when"]
    variables = {"import_cluster_java_home_gone": "/usr/lib/jvm/java-11-openjdk-11.0.20",
                 "import_cluster_java_bin": "/usr/lib/jvm/java-11-openjdk-11.0.21/bin/java"}
    text = " ".join(Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(error["ansible.builtin.set_fact"]["import_cluster_error"])).split())
    assert text == ("its unit sets JAVA_HOME=/usr/lib/jvm/java-11-openjdk-11.0.20, the Java removed by an update since"
                    " Cassandra started: the next start would fail. Point JAVA_HOME at its new build"
                    " (/usr/lib/jvm/java-11-openjdk-11.0.21) in the unit (systemctl edit cassandra), restart Cassandra,"
                    " then import again")
    names = [t.get("name") for play in PLAYS for t in play.get("tasks", [])]
    assert names.index("Note a running Java that cannot be read") < names.index(error["name"]) \
        < names.index("Read this node's config")
    work_out = task("Work out the series and the conf dir")["ansible.builtin.set_fact"]
    assert work_out["import_cluster_java_home_gone"] == "{{ _kv.javahomegone | default('') }}"


def test_read_task_splits_as_ansible_does():
    # (a quote in a comment of the script, e.g. "java's", fails the task before it runs)
    split_args(task("Read the running Cassandra (conf dir, Cassandra and Java versions)")["ansible.builtin.shell"])


def test_lines_java_gone_and_another_one(tmp_path):
    # the running Java 11 gone, /usr/bin/java a Java 17: not its new build, the node left to the operator
    new = _jdk(tmp_path, "jdk-17.0.9", "17.0.9")
    old = _gone(tmp_path, "jdk-11.0.20")
    found = _run(tmp_path, old, new)
    assert "javaupdated" not in found and found["java"] == "" and found["sysjava"] == str(new)


def test_role_leaves_the_system_java():
    gate = ("cassandra_java_set_default | bool or not cassandra_java_system_link.stat.exists"
            " and not cassandra_java_alternative_link.stat.exists | default(false)")
    with open(os.path.join(ROOT, "roles", "cassandra_install", "tasks", "java_tarball.yml")) as f:
        link = task("Make it the system java", yaml.safe_load(f))
    assert gate in link["when"]
    with open(os.path.join(ROOT, "roles", "cassandra_install", "tasks", "java.yml")) as f:
        alt = task("Make the Java package's java the default java", yaml.safe_load(f))  # the block: its when
    assert gate in alt["when"]


def test_lines_java_link_from_the_java_home_cassandra_runs_with(tmp_path):
    # /usr/bin/java straight to the home, Cassandra started with JAVA_HOME=/apps/jre/jdk (a link to it)
    java = _jdk(tmp_path, "jdk-11.0.30", "11.0.30")
    link = tmp_path / "apps" / "jre" / "jdk"
    link.parent.mkdir(parents=True)
    link.symlink_to(java.parent.parent)
    assert _run(tmp_path, java, java, java_home=str(link) + "/")["javalink"] == str(link)
    # a JAVA_HOME that is the home itself, or another Java: none
    other = tmp_path / "other"
    other.mkdir()
    java = _jdk(other, "jdk-11.0.30", "11.0.30")
    assert "javalink" not in _run(other, java, java, java_home=str(java.parent.parent))


def test_lines_java_link_of_a_usr_lib_jvm_name(tmp_path):
    # /usr/bin/java -> alternatives -> /usr/lib/jvm/java-17/bin/java, /usr/lib/jvm/java-17 -> /opt/jdk-17
    java = _jdk(tmp_path, "jdk-17", "17.0.9")
    jvm = tmp_path / "usr" / "lib" / "jvm"
    jvm.mkdir(parents=True)
    (jvm / "java-17").symlink_to(java.parent.parent)
    assert _run(tmp_path, java, jvm / "java-17" / "bin" / "java")["javalink"] == str(jvm / "java-17")
