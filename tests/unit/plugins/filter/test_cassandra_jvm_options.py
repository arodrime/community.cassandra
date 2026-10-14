from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The JVM options checks (module_utils cassandra_jvm_options and its filter):
# which jvm<N>-server.options each Java reads, and the table of options worth a
# warning, or refused before a Java change.

import re

import pytest

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import (
    cassandra_jvm_option_checks, cassandra_jvm_options_file)


@pytest.mark.parametrize("series, java, name", [
    ("40x", "8", "jvm8-server.options"), ("40x", "1.8", "jvm8-server.options"), ("41x", "11", "jvm11-server.options"),
    # 4.x reads jvm11 on any Java after 8 (cassandra.in.sh)
    ("41x", "17", "jvm11-server.options"),
    ("50x", "11", "jvm11-server.options"), ("50x", "17", "jvm17-server.options"), ("50x", "21", "jvm17-server.options"),
    ("50x", "", ""), ("60x", "17", ""),
])
def test_the_file_each_java_reads(series, java, name):
    assert jvm.options_file(series, java) == name
    assert cassandra_jvm_options_file(series, java) == name


@pytest.mark.parametrize("option, size", [
    ("-Xss256k", 256 * 1024), ("-Xss1024M", 1024 ** 3), ("-Xss1m", 1024 ** 2), ("-Xss2097152", 2 * 1024 ** 2),
    ("-XX:ThreadStackSize=1024", 1024 ** 2),  # counted in KiB
    ("-XX:ThreadStackSize=1m", 1024 ** 3),  # its suffix too: 1m KiB
    ("-Xmx31G", 31 * 1024 ** 3), ("-Xss", None), ("-Dx=1", None),
])
def test_sizes(option, size):
    assert jvm.size(option) == size


@pytest.mark.parametrize("option, name", [
    ("-Xss256k", "-Xss"), ("-XX:ThreadStackSize=512", "-Xss"), ("-XX:-UseBiasedLocking", "UseBiasedLocking"),
    ("-XX:+UseBiasedLocking", "UseBiasedLocking"), ("-Dcassandra.x=1", "-Dcassandra.x"), ("-ea", None),
])
def test_option_names(option, name):
    assert jvm.option_name(option) == name


def warnings(option, java, variable="cassandra_jvm_extra_options"):
    return jvm.check([(option, variable, jvm.fix_it(variable))], java)


def test_a_stack_of_1024m_is_a_warning_with_the_usual_range_and_the_way_out():
    found = warnings("-Xss1024M", "17")
    assert found == ["-Xss1024M (cassandra_jvm_extra_options, Java 17): 1 GiB of stack for every thread, far above the"
                     " usual 256k to 1m (stock -Xss256k; M typed for k?): each of Cassandra's hundreds to thousands of"
                     " threads reserves that much, new threads may fail (unable to create native thread). Fix or remove"
                     " it in cassandra_jvm_extra_options: it is kept as it is until then."]


def test_only_the_option_that_counts_is_checked():
    # the last -Xss counts (jvm<N> after jvm-server)
    options = [("-Xss1024M", "a", "fix a"), ("-Xss256k", "b", "fix b")]
    assert jvm.check(options, "17") == []
    assert len(jvm.check(list(reversed(options)), "17")) == 1
    assert len(jvm.check([("-Xss256k", "a", "x"), ("-XX:ThreadStackSize=1m", "b", "x")], "17")) == 1


@pytest.mark.parametrize("option", ["-Xss256k", "-Xss512k", "-Xss1m", "-Xss2m", "-Xss8m", "-XX:ThreadStackSize=1024",
                                    "-Xss0", "-XX:ThreadStackSize=0"])  # 0: the platform's default
def test_usual_stacks_pass(option):
    assert warnings(option, "17") == []


@pytest.mark.parametrize("option", ["-Xss64k", "-Xss16m", "-XX:ThreadStackSize=1048576"])
def test_odd_stacks_warn(option):
    assert len(warnings(option, "11")) == 1


def test_a_tiny_stack_says_why():
    assert "below the usual 256k to 1m" in warnings("-Xss64k", "11")[0]


@pytest.mark.parametrize("option", ["-XX:-UseBiasedLocking", "-XX:+UseBiasedLocking"])
def test_biased_locking(option):
    assert warnings(option, "11") == []  # Java 11: still there
    assert warnings(option, "8") == []
    found = warnings(option, "17", "cassandra_jvm17_extra_options")
    assert found == ["%s (cassandra_jvm17_extra_options, Java 17): biased locking is deprecated on Java 17 (a warning at"
                     " each start), and gone after it: the JVM does not start with it on Java 21. Fix or remove it in"
                     " cassandra_jvm17_extra_options: it is kept as it is until then." % option]
    # refused before a move to a Java after 17 (21, or another one cassandra_java_allow_unsupported allows)
    assert jvm.check([(option, "x", "x")], "17", refusals=True) == []
    for java in ("18", "21", "25"):
        refused = jvm.check([(option, "cassandra_jvm_extra_options", jvm.remove_it("cassandra_jvm_extra_options"))],
                            java, refusals=True)
        assert refused == ["%s (cassandra_jvm_extra_options) refused for Java %s: biased locking is gone after Java 17:"
                           " the JVM does not start with it. Remove it from cassandra_jvm_extra_options first, then run"
                           " again." % (option, java)]


def test_operator_text_names_only_the_javas_cassandra_runs_on():
    texts = [jvm.check([("-XX:-UseBiasedLocking", "v", jvm.fix_it("v"))], java, refusals=r)
             for java in ("11", "17", "21") for r in (False, True)]
    assert not [t for found in texts for t in found if re.search(r"Java (15|16|18|19|20)\b", t)]


def test_a_stack_is_never_refused():
    assert jvm.check([("-Xss1024M", "x", "x")], "21", refusals=True) == []


def test_unknown_java_checks_nothing():
    assert jvm.check([("-Xss1024M", "x", "x")], "") == []


def test_several_options_on_a_line():
    assert jvm.words(["-Xss1024M -XX:-UseBiasedLocking", "# -Xss2G", ""]) == ["-Xss1024M", "-XX:-UseBiasedLocking"]
    assert jvm.words("-Xss1024M") == ["-Xss1024M"]  # a string, not its characters


def test_the_filter_reads_the_variables_of_the_java_only():
    variables = {"cassandra_jvm_extra_options": [], "cassandra_jvm11_extra_options": ["-XX:-UseBiasedLocking"],
                 "cassandra_jvm17_extra_options": ["-Xss1024M"]}
    # 5.0 on Java 17: jvm17's options, not jvm11's (which only Java 11 reads)
    found = cassandra_jvm_option_checks(variables, "50x", "17")
    assert len(found) == 1 and found[0].startswith("-Xss1024M (cassandra_jvm17_extra_options, Java 17)")
    # 4.1 on Java 17 (unsupported, allowed): jvm11's
    found = cassandra_jvm_option_checks(variables, "41x", "17")
    assert len(found) == 1 and found[0].startswith("-XX:-UseBiasedLocking (cassandra_jvm11_extra_options, Java 17)")
    # no Java given: the series' default (5.0: 17)
    assert len(cassandra_jvm_option_checks(variables, "50x")) == 1
    assert cassandra_jvm_option_checks(variables, "50x", "11") == []


def test_the_filter_refusals():
    variables = {"cassandra_jvm_extra_options": ["-XX:-UseBiasedLocking", "-Xss1024M"]}
    refused = cassandra_jvm_option_checks(variables, "50x", "21", refusals=True)
    assert len(refused) == 1 and refused[0].startswith("-XX:-UseBiasedLocking (cassandra_jvm_extra_options) refused")
    assert cassandra_jvm_option_checks(None, "50x", "21", refusals=True) == []
    assert cassandra_jvm_option_checks({"cassandra_jvm_extra_options": "-XX:-UseBiasedLocking"}, "50x", "21",
                                       refusals=True)  # a string: one option


def test_the_role_template_lines_count_too():
    # 4.x's jvm-server.options writes -XX:-UseBiasedLocking for Java 8/11: on an unsupported Java, no variable
    # removes it
    assert cassandra_jvm_option_checks({}, "41x", "11") == []
    found = cassandra_jvm_option_checks({}, "41x", "17")
    assert len(found) == 1 and found[0].startswith("-XX:-UseBiasedLocking (the role's jvm-server.options, Java 17)")
    assert found[0].endswith("The role writes it as Cassandra 4.1's stock file does, for Java 8 or 11: set"
                             " cassandra_java_version to one of them")
    assert len(cassandra_jvm_option_checks({}, "40x", "21", refusals=True)) == 1
    # 5.0's stock line is in jvm11-server.options, which Java 21 does not read
    assert cassandra_jvm_option_checks({}, "50x", "21", refusals=True) == []
    # a variable setting it again: the last one counts, its own way out
    found = cassandra_jvm_option_checks({"cassandra_jvm11_extra_options": ["-XX:+UseBiasedLocking"]}, "41x", "17")
    assert len(found) == 1 and "cassandra_jvm11_extra_options" in found[0]


def test_the_default_java_is_cassandra_install_s():
    import os
    import yaml
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import DEFAULT_JAVA
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_install", "defaults",
                           "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_java_versions"] == DEFAULT_JAVA
