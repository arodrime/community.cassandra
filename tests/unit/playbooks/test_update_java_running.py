from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# update_java: a node already running cassandra_java_version is left alone, its
# Java read from the running JVM found by its process (not the unit's main PID,
# nor a package path that may not name the version).

import os
import pathlib
import shutil
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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "update_java.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
TASKS = dict((t["name"], t) for p in PLAYS for t in p.get("tasks", []))
NOTE = TASKS["Note what this node needs"]


def spec(version):
    if version is None:
        return {"stderr": "Error: could not find libjava.so\n", "rc": 1}
    return {"stderr": "Property settings:\n    java.home = /x\n    java.specification.version = %s\n    java.vm.vendor = Azul\n"
                      "openjdk version \"%s\"\n" % (version, version)}


def todo(exe, version, wanted="17", java_home="", resolved=""):
    variables = {"cassandra_jvm": {"exe": exe} if exe else {}, "cassandra_update_java_spec": spec(version) if exe else {},
                 "cassandra_java_version": wanted, "_cassandra_java_home": java_home,
                 "cassandra_update_java_home": {"stdout": resolved}}
    for k, v in NOTE["vars"].items():
        variables[k] = trust_as_template(v)
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(NOTE["ansible.builtin.set_fact"]["cassandra_update_java_todo"]))


@pytest.mark.parametrize("exe, version, wanted, expected", [
    ("/usr/lib/jvm/zulu17/bin/java", "17", "17", False),  # the path does not name java-17
    ("/usr/lib/jvm/java-11-openjdk-11.0.25/bin/java", "11", "17", True),
    ("/usr/lib/jvm/java-1.8.0-openjdk/jre/bin/java", "1.8", "8", False),
    ("", "", "17", True),  # not running
    # its JDK removed by an update since it started (java cannot run): restarted onto the installed Java
    ("/usr/lib/jvm/java-17-openjdk-17.0.9/bin/java", None, "17", True),
])
def test_package_java(exe, version, wanted, expected):
    assert todo(exe, version, wanted) is expected


def test_java_home():
    assert todo("/srv/java/jdk-17.0.20+1-jre/bin/java", "17", java_home="/srv/java/jre17",
                resolved="/srv/java/jdk-17.0.20+1-jre") is False
    assert todo("/opt/other/jdk-17/bin/java", "17", java_home="/srv/java/jre17", resolved="/srv/java/jdk-17.0.20+1-jre") is True


def test_running_java_found_by_its_process():
    find = TASKS["Find the running Cassandra and its java"]["ansible.builtin.include_role"]
    assert find["tasks_from"] == "jvm_started.yml"
    assert "systemd_service" not in str(PLAYS[1])


# after its restart, the node's Java is read again: a default java not switched (the alternatives entry not found)
# would leave it on the old Java, reported done
ACTION = os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_operation", "tasks", "action_update_java.yml")
with open(ACTION, encoding="utf-8") as f:
    ACTION_TASKS = yaml.safe_load(f)


def _template(template, variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def _java(path, version, home=None):
    """A java at path saying its version, as -XshowSettings:properties -version does."""
    spec = version.split(".")[0] if not version.startswith("1.") else ".".join(version.split(".")[:2])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "Property settings:\n    java.home = %s\n    java.specification.version = %s\n'
                    '    java.version = %s" >&2\n' % (home or path.parent.parent, spec, version))
    path.chmod(0o755)
    return path


def _restarted(tmp_path, running, expected, java_home=""):
    """The read task over tmp_path: the JVM runs running (/proc/<pid>/exe a link to it), started with java_home."""
    read = ACTION_TASKS[[t["name"] for t in ACTION_TASKS].index("Read the Java it runs and the one installed")]
    proc = tmp_path / "proc"
    proc.mkdir(parents=True)
    (proc / "exe").symlink_to(running)
    (proc / "environ").write_bytes(b"PATH=/bin\0" + (b"JAVA_HOME=" + java_home.encode() + b"\0" if java_home else b""))
    script = read["ansible.builtin.shell"].replace("/proc/$PID/", str(proc) + "/")
    out = subprocess.run(["/bin/sh", "-c", script], env={"PATH": os.environ["PATH"], "PID": "1", "EXPECTED": str(expected)},
                         stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
    return {"stdout_lines": out.splitlines()}


def _checked(restarted, wanted="11", expected="/opt/new/bin/java", version="11"):
    check = ACTION_TASKS[[t["name"] for t in ACTION_TASKS].index("Check it runs the Java installed for it")]
    variables = dict(check["vars"], cassandra_update_java_restarted=restarted, cassandra_java_version=version,
                     _cassandra_update_java_wanted=wanted, _cassandra_update_java_expected=expected,
                     inventory_hostname="n2")
    ok = all(_template("{{ %s }}" % that, variables) for that in check["ansible.builtin.assert"]["that"])
    return ok, _template(check["ansible.builtin.assert"]["fail_msg"], variables)


def test_the_restarted_node_runs_the_java_installed_for_it(tmp_path):
    from ansible.plugins.loader import init_plugin_loader
    import warnings
    with warnings.catch_warnings():  # already done under ansible-test
        warnings.simplefilter("ignore")
        init_plugin_loader()
    names = [t["name"] for t in ACTION_TASKS]
    # what it moves to worked out first, also under --check: an undefined variable stops it before the drain
    assert names[0] == "Note the Java this node moves to" and "when" not in ACTION_TASKS[0]
    assert names.index("Drain and restart the node") < names.index("Find the restarted Cassandra and its java") < names.index(
        "Read the Java it runs and the one installed") < names.index("Check it runs the Java installed for it")
    find = ACTION_TASKS[names.index("Find the restarted Cassandra and its java")]
    assert find["ansible.builtin.include_role"]["tasks_from"] == "jvm_started.yml" and find["when"] == "not ansible_check_mode"

    new = _java(tmp_path / "opt" / "new" / "bin" / "java", "11.0.32")
    old = _java(tmp_path / "opt" / "old" / "bin" / "java", "11.0.30")
    (tmp_path / "opt" / "link").symlink_to(tmp_path / "opt" / "new")
    # the same binary (the installed one given through a link), the same version
    assert _checked(_restarted(tmp_path / "a", new, tmp_path / "opt" / "link" / "bin" / "java"))[0] is True
    # the same major, another build: its unit's JAVA_HOME pinned the old one (a false success before)
    ok, text = _checked(_restarted(tmp_path / "b", old, new, java_home=str(tmp_path / "opt" / "old")), expected=str(new))
    assert ok is False
    assert text.split("\n") == [
        "n2 restarted on %s (Java 11.0.30), not on the Java installed for it: %s (Java 11.0.32)." % (old, new),
        "It was started with JAVA_HOME=%s." % (tmp_path / "opt" / "old"),
        "WAY OUT  point that JAVA_HOME at %s (or remove it) where it is set (the unit: systemctl cat cassandra;"
        " cassandra.in.sh), restart Cassandra on n2, then resume" % (tmp_path / "opt" / "new")]
    # no JAVA_HOME: the system java was not switched
    ok, text = _checked(_restarted(tmp_path / "c", old, new), expected="/usr/bin/java")
    assert ok is False and text.split("\n")[1] == (
        "WAY OUT  make /usr/bin/java the java Cassandra starts with (update-alternatives --display java; alternatives"
        " on RedHat), or set cassandra_java_home to the Java 11 home, then resume")
    # another major ("1.8" in the inventory reads as 8)
    java8 = _java(tmp_path / "opt" / "j8" / "bin" / "java", "1.8.0_392")
    assert _checked(_restarted(tmp_path / "d", java8, java8), wanted="8")[0] is True
    assert _checked(_restarted(tmp_path / "e", java8, java8), wanted="11")[0] is False


def test_each_post_restart_check_alone(tmp_path):
    # the same binary, another version (replaced in place since): refused
    java = _java(tmp_path / "opt" / "new" / "bin" / "java", "11.0.32")
    restarted = _restarted(tmp_path / "a", java, java)
    restarted["stdout_lines"] = [line.replace("running_version=11.0.32", "running_version=11.0.30")
                                 for line in restarted["stdout_lines"]]
    assert _checked(restarted, expected=str(java))[0] is False
    # another binary, the same version (Temurin and OpenJDK 11.0.32): refused
    other = _java(tmp_path / "opt" / "other" / "bin" / "java", "11.0.32")
    assert _checked(_restarted(tmp_path / "b", other, java), expected=str(java))[0] is False
    # a Java 8 JDK run by its jre/bin/java for its bin/java: the same Java home
    jdk = tmp_path / "opt" / "jdk8"
    jre_java = _java(jdk / "jre" / "bin" / "java", "1.8.0_392")
    jdk_java = _java(jdk / "bin" / "java", "1.8.0_392")
    assert _checked(_restarted(tmp_path / "c", jre_java, jdk_java), wanted="8", expected=str(jdk_java))[0] is True


def test_the_java_it_moves_to_with_the_role_defaults_only():
    # no cassandra_java_version in the inventory (import_cluster writes none for the series' default): the role's
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_install", "defaults", "main.yml"),
              encoding="utf-8") as f:
        defaults = yaml.safe_load(f)
    note = ACTION_TASKS[0]["ansible.builtin.set_fact"]
    variables = dict(defaults, cassandra_version="41x", ansible_facts={"os_family": "RedHat", "distribution": "Rocky"},
                     **ACTION_TASKS[0]["vars"])
    assert _template(note["_cassandra_update_java_wanted"], variables) == "11"
    assert _template(note["_cassandra_update_java_expected"], variables).strip() == "/usr/bin/java"
    variables["cassandra_java_home"] = "/opt/jdk-11.0.30/"
    assert _template(note["_cassandra_update_java_expected"], variables).strip() == "/opt/jdk-11.0.30/bin/java"
    # a JAVA_HOME read for the node (the plan checked it leads to the new Java): its java, as the plan compared it
    variables["cassandra_update_java_given"] = {"stdout_lines": ["/usr/lib/jvm/temurin-17/", "/usr/lib/jvm/temurin-17"]}
    variables.update(dict((k, v) for k, v in ACTION_TASKS[0]["vars"].items()))
    assert _template(note["_cassandra_update_java_expected"], variables).strip() == "/usr/lib/jvm/temurin-17/bin/java"
    # the play that runs it loads the role defaults first (play scoped: the first play's are gone)
    move = PLAYS[[p["name"] for p in PLAYS].index("Move to the new Java, one node at a time")]
    names = [t["name"] for t in move["tasks"]]
    assert names.index("Load the role defaults") < names.index("Update this node")
    load = move["tasks"][names.index("Load the role defaults")]
    assert "cassandra_install" in load["loop"] and load["ansible.builtin.include_role"]["public"] is True


def _kept(**variables):
    note = TASKS["Note what JAVA_HOME Cassandra restarts with"]
    base = {"cassandra_update_java_todo": True, "cassandra_java_set_default": False, "cassandra_java_version": "17",
            "_cassandra_java_home": "", "cassandra_update_java_home": {}, "cassandra_update_java_given": {},
            "cassandra_service_unit_manage": True, "cassandra_java_package": "java-17-openjdk-headless"}
    base.update(variables)
    for k, v in note["vars"].items():
        base[k] = trust_as_template(v)
    return " ".join(Templar(loader=DataLoader(), variables=base).template(
        trust_as_template(note["ansible.builtin.set_fact"]["cassandra_update_java_kept"])).split())


def given(*lines):
    return {"cassandra_update_java_given": {"stdout_lines": list(lines)}}


TARBALL = {"_cassandra_java_home": "/opt/java/jdk-17.0.9", "cassandra_update_java_home": {"stdout": "/opt/java/jdk-17.0.9"}}
KEPT = {"cassandra_service_unit_manage": False}
WHERE_KEPT = "its unit (kept: cassandra_service_unit_manage false; edit it by hand, systemctl edit cassandra)"


@pytest.mark.parametrize("variables, why", [
    ({"cassandra_java_set_default": True}, ""),  # the new Java made the system java
    ({"cassandra_update_java_todo": False}, ""),  # already on it: not restarted
    (given(), "no JAVA_HOME in cassandra_service_environment"),
    (dict(KEPT, **given()), "no JAVA_HOME in " + WHERE_KEPT),
    # a tarball: JAVA_HOME resolved is its home (a "current" link to it)
    (dict(TARBALL, **given("/opt/java/current", "/opt/java/jdk-17.0.9", "17.0.9")), ""),
    # not unpacked yet: no release file, the paths resolved alike
    (dict(TARBALL, **given("/opt/java/jdk-17.0.9", "/opt/java/jdk-17.0.9")), ""),
    # JAVA_HOME of the old Java, imported from the unit: refused
    (dict(TARBALL, **given("/opt/java/current", "/opt/java/jdk-11.0.21", "11.0.21")),
     "JAVA_HOME=/opt/java/current in cassandra_service_environment is not the new Java (/opt/java/jdk-17.0.9)"),
    # a kept unit: its own JAVA_HOME (read from it), the way out by hand
    (dict(TARBALL, **dict(KEPT, **given("/opt/java/jdk-11", "/opt/java/jdk-11", "11.0.21"))),
     "JAVA_HOME=/opt/java/jdk-11 in " + WHERE_KEPT + " is not the new Java (/opt/java/jdk-17.0.9)"),
    (dict(TARBALL, **dict(KEPT, **given("/opt/java/jdk-17.0.9", "/opt/java/jdk-17.0.9", "17.0.9"))), ""),
    # a package: JAVA_HOME a Java 17 installed here
    (given("/usr/lib/jvm/java-17", "/usr/lib/jvm/java-17-openjdk", "17.0.9"), ""),
    (given("/usr/lib/jvm/java-11", "/usr/lib/jvm/java-11-openjdk", "11.0.21"),
     "JAVA_HOME=/usr/lib/jvm/java-11 in cassandra_service_environment is not a Java 17 installed here: install"
     " java-17-openjdk-headless first (update_java installs it only after this check)"),
    (dict(given("/usr/lib/jvm/jre-1.8.0", "/usr/lib/jvm/java-1.8.0/jre", "1.8.0_392"), cassandra_java_version="8"),
     ""),
    (given("/usr/lib/jvm/java-17", "/usr/lib/jvm/java-17"),
     "JAVA_HOME=/usr/lib/jvm/java-17 in cassandra_service_environment is not a Java 17 installed here: install"
     " java-17-openjdk-headless first (update_java installs it only after this check)"),  # not installed yet
    # cassandra_java_set_default true: a JAVA_HOME that pins the old Java still wins over the system java
    (dict(TARBALL, cassandra_java_set_default=True, **dict(KEPT, **given("/opt/java/jdk-11", "/opt/java/jdk-11", "11.0.21", "unit"))),
     "JAVA_HOME=/opt/java/jdk-11 in " + WHERE_KEPT + " is not the new Java (/opt/java/jdk-17.0.9)"),
    (dict(TARBALL, cassandra_java_set_default=True,
          **given("/opt/java/jdk-11", "/opt/java/jdk-11", "11.0.21", "/usr/share/cassandra/cassandra.in.sh")),
     "JAVA_HOME=/opt/java/jdk-11 in /usr/share/cassandra/cassandra.in.sh (edit it by hand) is not the new Java"
     " (/opt/java/jdk-17.0.9)"),
    # a JAVA_HOME that is cassandra_java_link: moved to the new Java before the restart
    (dict(TARBALL, _cassandra_java_link="/apps/jre/jdk", **given("/apps/jre/jdk/", "/apps/jre/jdk-11.0.30", "11.0.30")), ""),
    (dict(TARBALL, _cassandra_java_link="/apps/jre/jdk", **given("/apps/jre/other", "/apps/jre/jdk-11.0.30", "11.0.30")),
     "JAVA_HOME=/apps/jre/other in cassandra_service_environment is not the new Java (/opt/java/jdk-17.0.9)"),
])
def test_java_home_must_lead_to_the_new_java(variables, why):
    assert _kept(**variables) == why


IN_SH = ("/usr/sbin/cassandra.in.sh", "/usr/share/cassandra/cassandra.in.sh", "/usr/local/share/cassandra/cassandra.in.sh",
         "/opt/cassandra/cassandra.in.sh", "/etc/default/cassandra")


def _read(tmp_path, java_home, unit_kept=False, unit_env="", show=None, files=None, awk="awk"):
    """The read task's script, run: java_home given, the unit's Environment and other properties (show: {property:
    value}, a stub systemctl), files: {path: text} written under tmp_path (the fixed paths the script reads, moved
    there)."""
    read = TASKS["Read the JAVA_HOME Cassandra is given"]
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("sed", "head", "tail", "readlink", "tr", "grep", "cut", "echo"):
        (tools / tool).symlink_to(shutil.which(tool))
    (tools / "awk").symlink_to(shutil.which(awk))
    show = dict({"Environment": unit_env}, **(show or {}))
    cases = "".join("  *' %s '*) echo '%s' ;;\n" % (k, v) for k, v in show.items())
    (tools / "systemctl").write_text('#!/bin/sh\ncase " $* " in\n%s  *) echo ;;\nesac\n' % cases)
    (tools / "systemctl").chmod(0o755)
    script = read["ansible.builtin.shell"]
    for path in IN_SH:
        script = script.replace(path, str(tmp_path) + path)
    for path, text in (files or {}).items():
        target = pathlib.Path(path) if path.startswith(str(tmp_path)) else tmp_path / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    env = {"PATH": str(tools), "GIVEN": java_home, "UNIT_KEPT": "yes" if unit_kept else "no", "CASSANDRA_USER": "nobody-here"}
    out = subprocess.run(["/bin/sh", "-c", script], env=env, stdout=subprocess.PIPE,
                         universal_newlines=True, check=True).stdout
    return out.splitlines()


def test_read_resolves_a_tarball_not_unpacked_yet(tmp_path):
    # several parts of the path missing: -m still resolves it, as the wanted home is
    home = str(tmp_path / "opt" / "java" / "jdk-17.0.9")
    assert _read(tmp_path, home) == [home, home, "", "cassandra_service_environment"]
    resolve = TASKS["Resolve the wanted Java home"]
    assert resolve["ansible.builtin.command"]["argv"][:2] == ["readlink", "-m"]


def test_read_a_java_8_jre_release_above_it(tmp_path):
    jdk = tmp_path / "jvm" / "java-1.8.0"
    (jdk / "jre").mkdir(parents=True)
    (jdk / "release").write_text('JAVA_VERSION="1.8.0_392"\n')
    assert _read(tmp_path, str(jdk / "jre"))[:3] == [str(jdk / "jre"), str(jdk / "jre"), "1.8.0_392"]


def test_read_a_kept_unit_from_the_unit(tmp_path):
    jdk = tmp_path / "jvm" / "jdk-11"
    jdk.mkdir(parents=True)
    (jdk / "release").write_text('JAVA_VERSION="11.0.21"\n')
    lines = _read(tmp_path, "/ignored", unit_kept=True, unit_env="MAX_HEAP_SIZE=8G JAVA_HOME=%s" % jdk)
    assert lines == [str(jdk), str(jdk), "11.0.21", "unit"]
    none = tmp_path / "none"
    none.mkdir()
    assert _read(none, "", unit_kept=True) == []  # no JAVA_HOME in the unit


def test_read_a_drop_in_beats_the_role_s_unit(tmp_path):
    # the role writes the unit (cassandra_service_environment); a drop-in of its own still sets JAVA_HOME after it
    drop_in = str(tmp_path / "etc" / "systemd" / "system" / "cassandra.service.d" / "java.conf")
    lines = _read(tmp_path, "/opt/new", show={"DropInPaths": drop_in},
                  files={drop_in: '[Service]\nEnvironment="JAVA_HOME=/opt/java/jdk-11.0.30" "MAX_HEAP_SIZE=1G"\n'})
    assert lines[0] == "/opt/java/jdk-11.0.30" and lines[3] == drop_in
    # its EnvironmentFile too
    other = tmp_path / "other"
    other.mkdir()
    drop_in = str(other / "cassandra.service.d" / "env.conf")
    env_file = str(other / "etc" / "sysconfig" / "cassandra")
    lines = _read(other, "/opt/new", show={"DropInPaths": drop_in},
                  files={drop_in: "[Service]\nEnvironmentFile=-%s\n" % env_file, env_file: "export JAVA_HOME='/opt/jdk-8'\n"})
    assert lines[0] == "/opt/jdk-8" and lines[3] == env_file


def test_read_a_kept_unit_s_environment_file_and_init_script_defaults(tmp_path):
    env_file = str(tmp_path / "etc" / "sysconfig" / "cassandra")
    lines = _read(tmp_path, "", unit_kept=True, unit_env="JAVA_HOME=/opt/a",
                  show={"EnvironmentFiles": "%s (ignore_errors=yes)" % env_file}, files={env_file: 'JAVA_HOME="/opt/b"\n'})
    assert lines[0] == "/opt/b" and lines[3] == env_file
    # a unit generated from an init script: it reads /etc/default/cassandra
    other = tmp_path / "other"
    other.mkdir()
    lines = _read(other, "", unit_kept=True, show={"SourcePath": "/etc/rc.d/init.d/cassandra"},
                  files={"/etc/default/cassandra": "JAVA_HOME=/opt/c\n"})
    assert lines[0] == "/opt/c" and lines[3].endswith("/etc/default/cassandra")


def test_read_cassandra_in_sh_wins_and_an_expression_is_not_read(tmp_path):
    # bin/cassandra reads the first cassandra.in.sh it finds, after the environment
    lines = _read(tmp_path, "/opt/new", files={"/usr/share/cassandra/cassandra.in.sh": "#JAVA_HOME=/x\nJAVA_HOME=/opt/old\n",
                                               "/opt/cassandra/cassandra.in.sh": "JAVA_HOME=/opt/not-read\n"})
    assert lines[0] == "/opt/old" and lines[3].endswith("/usr/share/cassandra/cassandra.in.sh")
    other = tmp_path / "other"
    other.mkdir()
    lines = _read(other, "/opt/new", files={"/usr/share/cassandra/cassandra.in.sh": 'JAVA_HOME="${JAVA_HOME:-/opt/x}"\n'})
    assert lines[0] == "/opt/new" and lines[3] == "cassandra_service_environment"


def test_refused_before_any_restart_when_the_system_java_is_kept():
    # before the plan: the first node would restart on its old Java, and the run stop there
    read = TASKS["Read the JAVA_HOME Cassandra is given"]
    note = TASKS["Note what JAVA_HOME Cassandra restarts with"]
    check = TASKS["Cassandra restarts on the new Java"]
    names = list(TASKS)
    assert names.index("Note what this node needs") < names.index(read["name"]) < names.index(note["name"]) \
        < names.index(check["name"]) < names.index("Show the plan and confirm the run")
    assert read["environment"]["GIVEN"] == "{{ cassandra_service_environment.JAVA_HOME | default('') }}"
    for manage, kept in ((False, "yes"), (True, "no"), ("false", "yes")):
        assert Templar(loader=DataLoader(), variables={"cassandra_service_unit_manage": manage}).template(
            trust_as_template(read["environment"]["UNIT_KEPT"])) == kept
    assert check["run_once"] is True
    hosts = {"n1": {"cassandra_update_java_kept": "no JAVA_HOME in cassandra_service_environment"}, "n2": {},
             "n3": {"cassandra_update_java_kept": ""}}
    variables = {"ansible_play_hosts_all": list(hosts), "hostvars": hosts, "cassandra_java_version": "17",
                 "_nl": "\n", "cassandra_java_set_default": False}
    kept = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(check["vars"]["_kept"]))
    assert kept == ["n1: no JAVA_HOME in cassandra_service_environment"]
    variables["_kept"] = kept
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(check["ansible.builtin.assert"]["fail_msg"]))
    assert text.split("\n") == [
        "Cassandra would not restart on Java 17: the JAVA_HOME this node starts Cassandra with does not lead to it."
        " Nothing was done.", "  n1: no JAVA_HOME in cassandra_service_environment",
        "WAY OUT  set JAVA_HOME to the home of the new Java where the line says (cassandra_java_home, a tarball one,"
        " cassandra_java_link, or the package one, installed), or remove cassandra_java_set_default: false to make it"
        " the system java; then run update_java again"]
    variables.update(_kept=["n1: x", "n2: y"], cassandra_java_set_default=True)
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(check["ansible.builtin.assert"]["fail_msg"]))
    assert "the JAVA_HOME these nodes start Cassandra with" in text
    assert text.endswith(", or remove it there: the new Java is made the system java; then run update_java again")
    # read whatever cassandra_java_set_default says
    read_when = read["when"] if isinstance(read["when"], list) else [read["when"]]
    assert read_when == ["cassandra_update_java_todo | bool"]


def test_a_node_left_drained_is_refused_before_the_question():
    # a plain re-run after a run stopped with a node drained: said at once (no wait of cassandra_service_health_timeout
    # at the first node, no "start the server"), the resume offered when there is a progress file to resume
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    names = list(TASKS)
    read, check = TASKS["Read whether Cassandra runs drained there"], TASKS["No node of the run is left drained"]
    assert names.index("Note what this node needs") < names.index(read["name"]) < names.index(check["name"]) \
        < names.index("Show the plan and confirm the run")
    assert read["check_mode"] is False and read["failed_when"] is False  # --check says it too

    def runs(task, **variables):
        when = task["when"] if isinstance(task["when"], list) else [task["when"]]
        return all(_template("{{ %s }}" % w, variables) for w in when)
    # every node of the run, one already on the new Java too; none under a resume (it takes that node from DRAINED)
    assert runs(read, cassandra_update_java_todo=False, cassandra_jvm={"pid": "1"}) is True
    assert runs(read, cassandra_jvm={"pid": "1"}, cassandra_rolling_resume=True) is False
    assert runs(read, cassandra_jvm={}) is False
    assert check["run_once"] is True
    assert runs(check) is True and runs(check, cassandra_rolling_resume="true") is False

    hosts = {"n1": {"inventory_hostname": "n1", "cassandra_update_java_netstats": {"mode": "NORMAL"}},
             "n2": {"inventory_hostname": "n2", "cassandra_update_java_netstats": {"skipped": True}},
             "n3": {"inventory_hostname": "n3", "cassandra_update_java_netstats": {"mode": "DRAINED"}}}
    variables = dict(check["vars"], ansible_play_hosts_all=list(hosts), hostvars=hosts, _progress=["/inv/x.done"],
                     _cassandra_run_command_args={"inventory": ["/inv/hosts.yml"], "default_inventory": "/inv/hosts.yml",
                                                  "hosts": "", "cwd": "/inv"})
    assert _template("{{ _drained }}", variables) == ["n3"]
    assert _template("{{ %s }}" % check["ansible.builtin.assert"]["that"], variables) is False
    assert _template(check["ansible.builtin.assert"]["fail_msg"], variables).split("\n") == [
        "Cassandra runs drained on n3 (nodetool netstats: Mode: DRAINED; gossip and the native transport are off"
        " there). Nothing was done.",
        "WAY OUT  resume: the resumed run takes the node it stopped at first, from DRAINED:",
        "  ansible-playbook community.cassandra.update_java -e cassandra_rolling_resume=true",
        "  or restart Cassandra on n3 (systemctl restart cassandra), then run update_java again"]
    # no progress file of update_java for this cluster: nothing to resume
    variables["_progress"] = []
    assert _template(check["ansible.builtin.assert"]["fail_msg"], variables).split("\n")[1:] == [
        "WAY OUT  restart Cassandra on n3 (systemctl restart cassandra), then run update_java again"]
    hosts["n3"]["cassandra_update_java_netstats"] = {"mode": "NORMAL"}
    assert _template("{{ %s }}" % check["ansible.builtin.assert"]["that"], variables) is True


def test_read_a_fallback_counts_only_when_nothing_set_it(tmp_path):
    # the packages' cassandra.in.sh style: a JAVA_HOME set only when none is
    fallback = 'if [ -z "$JAVA_HOME" ]; then\n    JAVA_HOME=/opt/fallback\nfi\n[ -n "$JAVA_HOME" ] || JAVA_HOME=/opt/other\n'
    lines = _read(tmp_path, "/opt/new", files={"/usr/share/cassandra/cassandra.in.sh": fallback})
    assert lines[0] == "/opt/new" and lines[3] == "cassandra_service_environment"
    bare = tmp_path / "bare"
    bare.mkdir()
    lines = _read(bare, "", files={"/usr/share/cassandra/cassandra.in.sh": fallback})
    assert lines[0] == "/opt/fallback" and lines[3].endswith("cassandra.in.sh")  # the first fallback sets it
    # under a test on something else: counts when that directory is there
    cond = tmp_path / "cond"
    cond.mkdir()
    old = cond / "opt" / "old"
    in_sh = 'if [ -x /x ]; then\n  JAVA_HOME=%s\nfi\n' % old
    assert _read(cond, "/opt/new", files={"/usr/share/cassandra/cassandra.in.sh": in_sh})[0] == "/opt/new"
    old.mkdir(parents=True)
    (cond / "again").mkdir()
    assert _read(cond / "again", "/opt/new", files={"/usr/share/cassandra/cassandra.in.sh": in_sh})[0] == str(old)


@pytest.mark.parametrize("awk", ["mawk", "busybox", "gawk"])
def test_read_one_line_forms_and_quoted_values(tmp_path, awk):
    import shutil as sh
    if not sh.which(awk):
        pytest.skip("no %s here" % awk)
    if awk == "busybox":
        wrapper = tmp_path / "bb-awk"
        wrapper.write_text("#!/bin/sh\nexec %s awk \"$@\"\n" % sh.which("busybox"))
        wrapper.chmod(0o755)
        awk = str(wrapper)
    there = tmp_path / "jdk 11"  # a directory with a space in its name
    there.mkdir()

    def first(text, given="/opt/new", name="t"):
        d = tmp_path / name
        d.mkdir()
        return _read(d, given, files={"/usr/share/cassandra/cassandra.in.sh": text}, awk=awk)[0]
    # one-line if: on JAVA_HOME a fallback, else a test on something else (counts when the directory is there)
    assert first('if [ -z "$JAVA_HOME" ]; then JAVA_HOME=/opt/fb; fi\n', name="a") == "/opt/new"
    assert first('if [ -z "$JAVA_HOME" ]; then JAVA_HOME=/opt/fb; fi\n', given="", name="b") == "/opt/fb"
    assert first('if [ -d "%s" ]; then JAVA_HOME="%s"; fi\n' % (there, there), name="c") == str(there)
    assert first('if [ -d /nowhere ]; then JAVA_HOME=/nowhere; fi\n', name="d") == "/opt/new"
    # [ -d x ] && JAVA_HOME=x: an override when the directory is there, not a fallback
    assert first('[ -d "%s" ] && JAVA_HOME="%s"\n' % (there, there), name="e") == str(there)
    assert first("[ -d /nowhere ] && JAVA_HOME=/nowhere\n", name="f") == "/opt/new"
    # a quoted value with spaces, kept whole
    assert first("JAVA_HOME='/opt/my java'\n", name="g") == "/opt/my java"


def test_read_cassandra_include_and_quoted_values(tmp_path):
    # a kept unit giving CASSANDRA_INCLUDE: that file, not the packages' cassandra.in.sh; a value with spaces quoted
    include = str(tmp_path / "srv" / "my.in.sh")
    lines = _read(tmp_path, "", unit_kept=True,
                  unit_env='"JVM_OPTS=-Da=1 -Db=2" CASSANDRA_INCLUDE=%s JAVA_HOME=/opt/unit' % include,
                  files={include: "JAVA_HOME=/opt/included\n", "/usr/share/cassandra/cassandra.in.sh": "JAVA_HOME=/opt/pkg\n"})
    assert lines[0] == "/opt/included" and lines[3] == include
    other = tmp_path / "other"
    other.mkdir()
    lines = _read(other, "", unit_kept=True, unit_env='"JVM_OPTS=-Da=1 JAVA_HOME=/not/it" JAVA_HOME=/opt/unit')
    assert lines[0] == "/opt/unit"
