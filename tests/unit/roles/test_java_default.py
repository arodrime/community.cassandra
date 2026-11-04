from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, java.yml: the Java package's own java is made the default java, found among the
# alternatives by the package's file list (a Temurin or Zulu path does not name java-<N>); none found: a stop
# that says the way out.

import os
import warnings

import pytest

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

ROLE_TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install", "tasks")
TASKS = load_tasks(os.path.join(ROLE_TASKS, "java.yml"))
BLOCK = next(t for t in TASKS if t["name"] == "Make the Java package's java the default java")
INNER = dict((t["name"], t) for t in BLOCK["block"])

ALTERNATIVES = ["java - auto mode", "  link best version is /usr/lib/jvm/java-11-openjdk-amd64/bin/java",
                "/usr/lib/jvm/java-11-openjdk-amd64/bin/java - priority 1111",
                "  slave java.1.gz: /usr/lib/jvm/java-11-openjdk-amd64/man/man1/java.1.gz",
                "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java - priority 1"]
TEMURIN_FILES = ["/.", "/usr", "/usr/lib/jvm", "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java",
                 "/usr/lib/jvm/temurin-17-jdk-amd64/bin/javac"]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def path(alternatives, files, version="17"):
    return render(BLOCK["vars"]["_path"], cassandra_java_alternatives={"stdout_lines": alternatives},
                  cassandra_java_files={"stdout_lines": files}, cassandra_java_version=version)


def test_the_package_lists_its_own_java():
    assert path(ALTERNATIVES, TEMURIN_FILES) == "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java"
    assert path(ALTERNATIVES, [], "11") == "/usr/lib/jvm/java-11-openjdk-amd64/bin/java"  # named by its path
    assert path(ALTERNATIVES, [], "17") == ""
    assert path([], TEMURIN_FILES) == ""  # not registered as an alternative
    assert INNER["Make it the default java"]["when"] == "_path != ''"
    assert INNER["Make it the default java"]["community.general.alternatives"] == {"name": "java", "path": "{{ _path }}"}


def test_the_file_list_is_read_from_the_package():
    files = INNER["List the files of the Java package"]
    argv = files["ansible.builtin.command"]["argv"]
    assert render(argv, ansible_facts={"os_family": "Debian"}, cassandra_java_package="temurin-17-jdk=17.0.9+9") == [
        "dpkg", "-L", "temurin-17-jdk"]
    assert render(argv, ansible_facts={"os_family": "RedHat"}, cassandra_java_package=["zulu17-jdk-headless"]) == [
        "rpm", "-ql", "zulu17-jdk-headless"]
    assert files["failed_when"] is False and files["check_mode"] is False


def test_none_found_stops_with_the_way_out():
    stop = INNER["Stop when its java is not among the alternatives"]
    assert stop["ansible.builtin.assert"]["that"] == "_path != ''"
    # not under --check (the package may not be installed yet), nor when the package is not installed (offline: the
    # role's own refusal follows, the molecule scenario checks it)
    assert stop["when"] == ["not ansible_check_mode", "cassandra_java_files.rc | default(1) == 0"]
    installed = "{{ " + stop["when"][1] + " }}"
    assert render(installed, cassandra_java_files={"rc": 1}) is False
    assert render(installed, cassandra_java_files={"rc": 0}) is True
    assert render(installed) is False  # not registered at all
    variables = dict(stop["vars"], _path="", cassandra_java_package="zulu17-jdk-headless", cassandra_java_version="17",
                     cassandra_java_alternatives={"stdout_lines": ALTERNATIVES}, ansible_facts={"os_family": "RedHat"})
    text = " ".join(render(stop["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert text.startswith("zulu17-jdk-headless is installed, but no java the alternatives list is its own, nor one named java-17 "
                           "(/usr/lib/jvm/java-11-openjdk-amd64/bin/java, /usr/lib/jvm/temurin-17-jdk-amd64/bin/java).")
    assert "Set cassandra_java_home" in text and "alternatives --set java <its home>/bin/java" in text
    variables.update(cassandra_java_alternatives={}, ansible_facts={"os_family": "Debian"})
    text = " ".join(render(stop["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert "(no alternative)" in text and "update-alternatives --install /usr/bin/java java" in text


# cassandra_java_set_default false: a /usr/bin/java there is left as it is (the Java of other programs), a missing
# one is still made (nodetool and the scripts need a java): a node added later under group_vars gets one
@pytest.mark.parametrize("set_default, exists, alternative, made", [
    (True, True, None, True), (True, False, None, True), (False, True, None, False), (False, False, False, True),
    (True, False, True, True),
    # false, /usr/bin/java missing while its alternative leads to a java: its link alone made again (below)
    (False, False, True, False),
])
def test_a_missing_system_java_is_made_either_way(set_default, exists, alternative, made):
    tarball = next(t for t in load_tasks(os.path.join(ROLE_TASKS, "java_tarball.yml"))
                   if t["name"] == "Make it the system java")
    variables = {"cassandra_java_set_default": set_default, "cassandra_java_link": {"stat": {"exists": exists}},
                 # skipped when /usr/bin/java is there: no stat
                 "cassandra_java_alternative_link": {"skipped": True} if alternative is None
                 else {"stat": {"exists": alternative}}}
    for when in (BLOCK["when"], tarball["when"]):
        condition = next(c for c in when if "cassandra_java_set_default" in c)
        assert render("{{ %s }}" % condition, **variables) is made


def test_the_alternative_is_looked_for_when_the_link_is_missing():
    names = [t["name"] for t in TASKS]
    look = TASKS[names.index("Look for the java alternative")]
    assert look["ansible.builtin.stat"]["path"] == "/etc/alternatives/java" and look["ansible.builtin.stat"]["follow"]
    assert look["when"] == "not cassandra_java_link.stat.exists"
    assert names.index("Look for the system java") < names.index(look["name"]) < names.index(
        "Java from a tarball, or already unpacked (cassandra_java_home)")


def test_the_system_java_is_looked_for_before_both():
    names = [t["name"] for t in TASKS]
    look = TASKS[names.index("Look for the system java")]
    assert look["ansible.builtin.stat"]["path"] == "/usr/bin/java"
    assert look["ansible.builtin.stat"]["follow"] is True  # a link leading nowhere is missing: made again
    assert look["register"] == "cassandra_java_link" and "when" not in look
    assert names.index("Look for the system java") < names.index(
        "Java from a tarball, or already unpacked (cassandra_java_home)") < names.index(BLOCK["name"])
    assert names.index("Install Java") < names.index("Look for the system java")  # the package's own counts


@pytest.mark.parametrize("exists, install_java, home, linked", [
    (False, True, "", True),  # a package Java whose alternative is selected already: its link made again
    (False, False, "/opt/java/jdk-17", True),  # a Java home
    (False, False, "", False),  # Java installed by other means: not the role's
    (True, True, "", False),
])
def test_a_missing_link_is_made_to_its_alternative(exists, install_java, home, linked):
    names = [t["name"] for t in TASKS]
    link = TASKS[names.index("Link the system java to its alternative")]
    assert names.index(BLOCK["name"]) < names.index(link["name"])  # after the alternative is selected
    assert link["ansible.builtin.file"] == {"src": "/etc/alternatives/java", "dest": "/usr/bin/java", "state": "link",
                                            "force": True}
    variables = {"cassandra_java_link": {"stat": {"exists": exists}}, "cassandra_install_java": install_java,
                 "_cassandra_java_home": home}
    assert all(render("{{ %s }}" % c, **variables) for c in link["when"]) is linked
