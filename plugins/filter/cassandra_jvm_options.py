# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The JVM options checks of preflight and of a Java change (module_utils
cassandra_jvm_options: the table of checks).

cassandra_jvm_option_checks: a node's extra options variables, its series and
    Java -> the warnings on the options that Java gets from the files the role
    writes (refusals=true: the options a move to that Java is refused with).
cassandra_jvm_options_file: series, Java -> the jvm<N>-server.options that
    Java reads besides jvm-server.options.
cassandra_jvm_options_variable: an options file -> its extra options variable."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os

from ansible_collections.community.cassandra.plugins.filter.cassandra_java import cassandra_java_supported
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config", "templates")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# cassandra_install's cassandra_java_versions: the Java when none is given
DEFAULT_JAVA = {"40x": "11", "41x": "11", "50x": "17"}


@functools.lru_cache(maxsize=None)
def _template_options(series, name):
    """The option lines the role's template of a file always writes."""
    path = os.path.join(TEMPLATES, SERIES.get(series, ""), name + ".j2")
    if series not in SERIES or not os.path.exists(path):
        return ()
    with open(path) as f:
        return tuple(jvm.words([line for line in f.read().split("\n") if "{{" not in line]))


def cassandra_jvm_option_checks(variables, series, java="", refusals=False, kept=None):
    """variables: {cassandra_jvm_extra_options: [...], cassandra_jvm<N>_extra_options: [...]}; java: the Java
    major the node runs or moves to ('': the series' default); kept: cassandra_config_keep_files, the files
    the role leaves as they are (neither its lines nor the variable's in them) -> [message]."""
    series = str(series)
    java = str(java or DEFAULT_JAVA.get(series, ""))
    stock = ("The role writes it as Cassandra %s's stock file does, for Java %s: set cassandra_java_version to one"
             " of them" % (SERIES.get(series, series), " or ".join(cassandra_java_supported(series))))
    options = []
    for name in jvm.read_files(series, java):  # each file's own lines, then its extra options
        var = jvm.options_variable(name)
        if name in (kept or []):  # the node's own file, which the role leaves as it is: not known here
            continue
        options += [(o, "the role's %s" % name, stock) for o in _template_options(series, name)]
        options += [(o, var, jvm.remove_it(var) if refusals else jvm.fix_it(var))
                    for o in jvm.words((variables or {}).get(var) or [])]
    return jvm.check(options, java, refusals=bool(refusals))


def cassandra_jvm_options_file(series, java):
    """The jvm<N>-server.options a Java reads besides jvm-server.options, '' when not known: "50x", "17" ->
    "jvm17-server.options"."""
    return jvm.options_file(str(series), java)


def cassandra_jvm_options_variable(name):
    """jvm11-server.options -> cassandra_jvm11_extra_options."""
    return jvm.options_variable(str(name))


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_jvm_option_checks": cassandra_jvm_option_checks,
            "cassandra_jvm_options_file": cassandra_jvm_options_file,
            "cassandra_jvm_options_variable": cassandra_jvm_options_variable,
        }
