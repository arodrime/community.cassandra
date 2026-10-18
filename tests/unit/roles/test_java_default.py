from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, java.yml: the Java package's own java is made the default java, found among the
# alternatives by the package's file list (a Temurin or Zulu path does not name java-<N>); none found: a stop
# that says the way out.

import os
import warnings

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

TASKS = load_tasks(os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install", "tasks", "java.yml"))
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
    assert stop["when"] == "not ansible_check_mode"
    variables = dict(stop["vars"], _path="", cassandra_java_package="zulu17-jdk-headless", cassandra_java_version="17",
                     cassandra_java_alternatives={"stdout_lines": ALTERNATIVES}, ansible_facts={"os_family": "RedHat"})
    text = " ".join(render(stop["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert text.startswith("zulu17-jdk-headless is installed, but no java the alternatives list is its own, nor one named java-17 "
                           "(/usr/lib/jvm/java-11-openjdk-amd64/bin/java, /usr/lib/jvm/temurin-17-jdk-amd64/bin/java).")
    assert "Set cassandra_java_home" in text and "alternatives --set java <its home>/bin/java" in text
    variables.update(cassandra_java_alternatives={}, ansible_facts={"os_family": "Debian"})
    text = " ".join(render(stop["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert "(no alternative)" in text and "update-alternatives --install /usr/bin/java java" in text
