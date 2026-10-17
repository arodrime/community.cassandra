from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install refuses a Java (java_check.yml) while the JVM options that
# Java reads hold one it does not start with. The expression is read from the
# task file, rendered by Ansible.

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()
load_collection_filters()  # the community.cassandra filters, whatever ran before
TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "roles", "cassandra_install", "tasks", "java_check.yml"), encoding="utf-8") as f:
    REFUSE = next(t for t in yaml.safe_load(f) if t.get("name") == "The JVM options suit this Java")


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def refused(**variables):
    variables.setdefault("cassandra_version", "50x")
    return render(REFUSE["vars"]["_refused"], **variables)


@pytest.mark.parametrize("java", ["18", "21"])
def test_a_java_change_is_refused_with_biased_locking(java):
    out = refused(cassandra_java_version=java, cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"])
    assert len(out) == 1 and "Remove it from cassandra_jvm_extra_options first" in out[0]
    # in the file of that Java too (5.0 on 18+: jvm17)
    assert len(refused(cassandra_java_version=java, cassandra_jvm17_extra_options=["-XX:+UseBiasedLocking"])) == 1


def test_a_java_change_goes_on_without_it():
    assert refused(cassandra_java_version="17", cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"]) == []
    # 5.0's stock jvm11 line: read by Java 11 only
    assert refused(cassandra_java_version="21", cassandra_jvm11_extra_options=["-XX:-UseBiasedLocking"]) == []
    assert refused(cassandra_java_version="21") == []  # other roles' defaults not loaded
    assert refused(cassandra_java_version="21", cassandra_jvm_extra_options=["-Xss1024M"]) == []  # a warning only


def test_a_java_change_with_the_role_s_own_line():
    # 4.1's jvm-server.options writes -XX:-UseBiasedLocking: refused on Java 21 (allow_unsupported), unless the
    # node keeps its own file
    assert len(refused(cassandra_version="41x", cassandra_java_version="21")) == 1
    assert refused(cassandra_version="41x", cassandra_java_version="21",
                   cassandra_config_keep_files=["jvm-server.options"]) == []
