from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# update_java: a node already running cassandra_java_version is left alone, its
# Java read from the running JVM found by its process (not the unit's main PID,
# nor a package path that may not name the version).

import os
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


def test_the_restarted_node_runs_the_java_asked_for():
    import warnings
    from ansible.plugins.loader import init_plugin_loader
    with warnings.catch_warnings():  # already done under ansible-test
        warnings.simplefilter("ignore")
        init_plugin_loader()
    names = [t["name"] for t in ACTION_TASKS]
    assert names.index("Drain and restart the node") < names.index("Find the restarted Cassandra and its java") < names.index(
        "Read the Java version it runs") < names.index("Check it runs Java {{ cassandra_java_version }}")
    find = ACTION_TASKS[names.index("Find the restarted Cassandra and its java")]
    assert find["ansible.builtin.include_role"]["tasks_from"] == "jvm_started.yml" and find["when"] == "not ansible_check_mode"
    check = ACTION_TASKS[names.index("Check it runs Java {{ cassandra_java_version }}")]

    def runs(version, wanted="17", exe="/usr/lib/jvm/java-11-openjdk-amd64/bin/java"):
        variables = dict(check["vars"], cassandra_update_java_restarted=spec(version), cassandra_java_version=wanted,
                         cassandra_jvm={"pid": "123", "exe": exe}, inventory_hostname="n2")
        for k, v in variables.items():
            variables[k] = trust_as_template(v) if isinstance(v, str) else v
        templar = Templar(loader=DataLoader(), variables=variables)
        return (templar.template(trust_as_template("{{ %s }}" % check["ansible.builtin.assert"]["that"])),
                " ".join(templar.template(trust_as_template(check["ansible.builtin.assert"]["fail_msg"])).split()))

    assert runs("17")[0] is True
    assert runs("1.8", wanted="8")[0] is True
    assert runs("1.8", wanted="1.8")[0] is True  # the inventory's spelling, as the other checks read it
    assert runs("17", wanted="1.8")[1].startswith("n2 restarted on Java 17 (/usr/lib/jvm/java-11-openjdk-amd64/bin/java), not 8:")
    ok, text = runs("11")
    assert ok is False
    assert text.startswith("n2 restarted on Java 11 (/usr/lib/jvm/java-11-openjdk-amd64/bin/java), not 17: the default java was not switched.")
    assert "update-alternatives --display java" in text and "cassandra_java_home" in text and "run update_java again" in text
    assert runs(None)[1].startswith("n2 restarted on Java unknown (")


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
])
def test_java_home_must_lead_to_the_new_java(variables, why):
    assert _kept(**variables) == why


def _read(tmp_path, java_home, unit_kept=False, unit_env=""):
    """The read task's script, run: java_home given, the unit's Environment (a stub systemctl)."""
    read = TASKS["Read the JAVA_HOME Cassandra is given"]
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("sed", "head", "readlink", "tr"):
        (tools / tool).symlink_to(shutil.which(tool))
    (tools / "systemctl").write_text("#!/bin/sh\necho '%s'\n" % unit_env)
    (tools / "systemctl").chmod(0o755)
    env = {"PATH": str(tools), "GIVEN": java_home, "UNIT_KEPT": "yes" if unit_kept else "no"}
    out = subprocess.run(["/bin/sh", "-c", read["ansible.builtin.shell"]], env=env, stdout=subprocess.PIPE,
                         universal_newlines=True, check=True).stdout
    return out.splitlines()


def test_read_resolves_a_tarball_not_unpacked_yet(tmp_path):
    # several parts of the path missing: -m still resolves it, as the wanted home is
    home = str(tmp_path / "opt" / "java" / "jdk-17.0.9")
    assert _read(tmp_path, home) == [home, home]
    resolve = TASKS["Resolve the wanted Java home"]
    assert resolve["ansible.builtin.command"]["argv"][:2] == ["readlink", "-m"]


def test_read_a_java_8_jre_release_above_it(tmp_path):
    jdk = tmp_path / "jvm" / "java-1.8.0"
    (jdk / "jre").mkdir(parents=True)
    (jdk / "release").write_text('JAVA_VERSION="1.8.0_392"\n')
    assert _read(tmp_path, str(jdk / "jre")) == [str(jdk / "jre"), str(jdk / "jre"), "1.8.0_392"]


def test_read_a_kept_unit_from_the_unit(tmp_path):
    jdk = tmp_path / "jvm" / "jdk-11"
    jdk.mkdir(parents=True)
    (jdk / "release").write_text('JAVA_VERSION="11.0.21"\n')
    lines = _read(tmp_path, "/ignored", unit_kept=True, unit_env="MAX_HEAP_SIZE=8G JAVA_HOME=%s" % jdk)
    assert lines == [str(jdk), str(jdk), "11.0.21"]
    none = tmp_path / "none"
    none.mkdir()
    assert _read(none, "", unit_kept=True) == [""]  # no JAVA_HOME in the unit


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
                 "_nl": "\n"}
    kept = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(check["vars"]["_kept"]))
    assert kept == ["n1: no JAVA_HOME in cassandra_service_environment"]
    variables["_kept"] = kept
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(check["ansible.builtin.assert"]["fail_msg"]))
    assert text.split("\n")[:2] == [
        "Cassandra would not restart on Java 17: this node keeps its /usr/bin/java (cassandra_java_set_default: false),"
        " and its JAVA_HOME does not lead to it:", "  n1: no JAVA_HOME in cassandra_service_environment"]
    assert text.endswith("Nothing was done.")
    variables["_kept"] = ["n1: x", "n2: y"]
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(check["ansible.builtin.assert"]["fail_msg"]))
    assert "these nodes keep their /usr/bin/java" in text and "their JAVA_HOME" in text
