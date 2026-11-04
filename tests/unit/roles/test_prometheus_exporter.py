from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The Prometheus exporter in cassandra_config: the -javaagent option at the end of jvm-server.options (none when
# off: the stock file), its config file (the role's rules, the user's, the httpServer part of the variables), the
# checks of its variables, its jar (a checksum, the mirror's credentials, offline, check mode), apply_config's
# plan (a restart for the httpServer part only), and cassandra_firewall's rule for its sources.

import os
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _defaults, _render as role_render

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template


TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
ROLE = os.path.join(TOP, "roles", "cassandra_config")
FACTS = {"os_family": "Debian"}
JAR = "/opt/prometheus-jmx-exporter/jmx_prometheus_javaagent-1.7.0.jar"


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def find(tasks, name):
    return next(t for t in walk(tasks) if t.get("name") == name)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=trust(variables)).template(trust_as_template(template))


def true(expression, variables):
    return render("{{ %s }}" % expression, variables) in (True, "True")


def role_vars(**variables):
    """cassandra_config's defaults and vars with these, as Ansible gives them to the role."""
    return dict(_defaults(), cassandra_version="50x", cassandra_conf_dir="/etc/cassandra", ansible_facts=FACTS,
                role_path=ROLE, **variables)


# --- jvm-server.options -------------------------------------------------------

@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_off_the_jvm_options_file_is_as_before(series):
    off, errors, dummy, dummy2 = role_render({"cassandra_version": series, "cassandra_service_unit_manage": False}, FACTS, {})
    assert errors == [] and "javaagent" not in off["jvm-server.options"].replace("#-javaagent", "")
    assert off["jvm-server.options"].rstrip("\n").endswith("#-Dcassandra.expiration_date_overflow_policy=REJECT")
    on, errors, dummy, dummy2 = role_render({"cassandra_version": series, "cassandra_service_unit_manage": False,
                                             "cassandra_prometheus_exporter_enabled": True,
                                             "cassandra_jvm_extra_options": ["-Dmine=1"]}, FACTS, {})
    assert errors == []
    # the agent last, after the extra options; nothing else changes
    assert on["jvm-server.options"].rstrip("\n").split("\n")[-2:] == [
        "-Dmine=1", "-javaagent:%s=9404:/etc/cassandra/jmx_prometheus.yml" % JAR]
    assert [f for f in on if f != "jvm-server.options" and on[f] != off[f]] == []


@pytest.mark.parametrize("variables, option", [
    ({}, "-javaagent:%s=9404:/etc/cassandra/jmx_prometheus.yml" % JAR),
    ({"cassandra_prometheus_exporter_port": 9500, "cassandra_prometheus_exporter_address": "10.0.0.1"},
     "-javaagent:%s=10.0.0.1:9500:/etc/cassandra/jmx_prometheus.yml" % JAR),
    ({"cassandra_prometheus_exporter_address": "fd00::1"}, "-javaagent:%s=[fd00::1]:9404:/etc/cassandra/jmx_prometheus.yml" % JAR),
    ({"cassandra_prometheus_exporter_version": "1.5.0", "cassandra_prometheus_exporter_dir": "/srv/exporter/",
      "cassandra_prometheus_exporter_config_file": "/etc/monitoring/cassandra.yml"},
     "-javaagent:/srv/exporter/jmx_prometheus_javaagent-1.5.0.jar=9404:/etc/monitoring/cassandra.yml"),
    ({"cassandra_prometheus_exporter_enabled": False}, ""),
])
def test_the_agent_option(variables, option):
    variables = role_vars(**dict({"cassandra_prometheus_exporter_enabled": True}, **variables))
    assert render("{{ _cassandra_config_jvm_agent }}", variables) == option


def test_the_template_generator_writes_the_same_line():
    sys.path.insert(0, os.path.join(TOP, "tools"))
    try:
        import gen_templates
    finally:
        sys.path.pop(0)
    rule = [r for r in gen_templates.COMMON["jvm-server.options"] if r[0].startswith("#-Dcassandra.expiration")][0]
    for series in ("4.0", "4.1", "5.0"):
        with open(os.path.join(ROLE, "templates", series, "jvm-server.options.j2")) as f:
            assert rule[1] in f.read().split("\n")
