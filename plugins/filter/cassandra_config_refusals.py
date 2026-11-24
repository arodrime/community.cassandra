# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Why cassandra_config refuses the variables of a file it writes (tasks/checks.yml), before anything is read on
the node. Each filter gives the list of what is wrong (each item a part of the stop message), [] when nothing is.

cassandra_config_override_refusals: cassandra_config_template_overrides (your own template of a file the role writes).
cassandra_config_snippet_refusals: cassandra_config_template_snippets (your own lines in logback.xml or cassandra-env.sh).
cassandra_config_logback_refusals: the appender-refs, the logger levels and the values written as they are in
    logback.xml.
cassandra_config_audit_parameter_refusals: cassandra_audit_logger_parameters.
cassandra_config_jmx_tls_refusals: remote JMX over TLS (cassandra_jmx_ssl_*).
cassandra_config_rackdc_refusals: cassandra_rackdc_extra_settings.
cassandra_config_exporter_refusals: the Prometheus exporter's agent (cassandra_prometheus_exporter_*).

They test the values as the Jinja of the tasks did: a text by its str() (Jinja's ~ and | string), a regular expression
on its text (Ansible's match and search tests), | bool and | int left to the task."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re
import textwrap
from collections.abc import Mapping
from numbers import Number

from jinja2.filters import do_int

from ansible.module_utils.common.text.converters import to_text
from ansible.plugins.filter.core import from_yaml, to_json
from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_config_error

TEMPLATE_PATH = "an absolute path on the controller is expected (e.g. under inventory_dir)"
SNIPPET_FILES = ["logback.xml", "logback.xml:appenders", "cassandra-env.sh"]  # (their insertion points)


def _match(pattern, value):
    """Ansible's match test: the pattern at the start of the value's text."""
    return re.match(pattern, to_text(value, errors="surrogate_or_strict")) is not None


def _search(pattern, value):
    """Ansible's search test: the pattern anywhere in the value's text."""
    return re.search(pattern, to_text(value, errors="surrogate_or_strict")) is not None


def _join(items, sep):
    return sep.join(str(item) for item in items)


def _template_path_wrong(name, path):
    """Why the controller path of your template (or snippet) is wrong, "" when it is not."""
    if not isinstance(path, str) or not _match("/", path):
        return "%s: %s" % (name, TEMPLATE_PATH)
    if not os.path.isfile(path):
        return "%s: %s not found on the controller" % (name, path)
    return ""


def cassandra_config_override_refusals(overrides, files, keep_files):
    """overrides: {file name: template path}; files: the files the role writes for the series; keep_files:
    cassandra_config_keep_files. e.g. {"x.yaml": "/t.j2"} -> ["x.yaml is not a file the role writes (...)"]."""
    if not isinstance(overrides, Mapping):
        return ["a dict of {file name: template path} is expected"]
    out = []
    for name, path in overrides.items():
        wrong_path = _template_path_wrong(name, path)
        if name not in files:
            out.append("%s is not a file the role writes (%s)" % (name, _join(files, ", ")))
        elif wrong_path:
            out.append(wrong_path)
        elif name in keep_files:
            out.append("%s is also in cassandra_config_keep_files (left as the node has it): one or the other" % name)
    return out


def cassandra_config_snippet_refusals(snippets, keep_files, overrides):
    """snippets: {logback.xml, logback.xml:appenders or cassandra-env.sh: template path}; keep_files and overrides:
    cassandra_config_keep_files, cassandra_config_template_overrides (a snippet goes in the role's own template)."""
    if not isinstance(snippets, Mapping):
        return ["a dict of {file name: template path} is expected"]
    out = []
    for name, path in snippets.items():
        wrong_path = _template_path_wrong(name, path)
        if name not in SNIPPET_FILES:
            out.append("%s: only logback.xml, logback.xml:appenders and cassandra-env.sh take a snippet"
                       " (the JVM options files: cassandra_jvm*_extra_options)" % name)
        elif wrong_path:
            out.append(wrong_path)
        elif name.split(":")[0] in keep_files:
            out.append("%s: its file is also in cassandra_config_keep_files (left as the node has it): one or the other"
                       % name)
        elif name.split(":")[0] in overrides:
            out.append("%s: its file is also in cassandra_config_template_overrides (your whole template): put the"
                       " lines in it, or use the role's template with the snippet" % name)
    return out


# The values logback.xml's template writes as they are: (variable, in an attribute, the variable whose false leaves
# the part they are in commented out: "audit" for cassandra_log_audit_file, "" for none)
WRITTEN = [("cassandra_log_level", True, ""),
           ("cassandra_log_level_cassandra", True, ""),
           ("cassandra_log_system_level", False, ""),
           ("cassandra_log_pattern", False, "audit"),
           ("cassandra_log_system_max_file_size", False, ""),
           ("cassandra_log_system_max_history", False, ""),
           ("cassandra_log_system_total_size_cap", False, ""),
           ("cassandra_log_debug_max_file_size", False, ""),
           ("cassandra_log_debug_max_history", False, ""),
           ("cassandra_log_debug_total_size_cap", False, ""),
           ("cassandra_log_audit_max_file_size", False, "audit"),
           ("cassandra_log_audit_max_history", False, "audit"),
           ("cassandra_log_audit_total_size_cap", False, "audit"),
           ("cassandra_log_audit_dir", False, "audit"),
           ("cassandra_log_system_dir", False, ""),
           ("cassandra_log_debug_dir", False, "")]
LEVELS = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "OFF", "ALL"]
# what turning the switch on does to logback.xml (said in the way out of a -- in a commented-out part)
TURNED_ON = {"cassandra_log_audit_file": ": logback.xml then writes the audit events (FileAuditLogger) to"
                                         " audit/audit.log, no longer to system.log",
             "cassandra_log_cql_table": ": logback.xml then writes the logs to the system_views.system_logs table too"}
# an & that starts no entity XML knows (logback's parser knows only XML's own five)
STRAY_AMPERSAND = "&(?!(?:amp|lt|gt|quot|apos);|#[0-9]+;|#x[0-9A-Fa-f]+;)"


def _ref_refusals(refs, known, after_root):
    if isinstance(refs, (str, Mapping)):
        return ["cassandra_logback_root_extra_refs: a list of appender names is expected"]
    out = []
    for ref in refs:
        if ref in known:
            continue
        if ref in after_root:
            out.append("cassandra_logback_root_extra_refs: %s is defined in your snippet logback.xml, after <root>, where"
                       " logback (1.2) does not find it: move it to your snippet logback.xml:appenders"
                       " (cassandra_config_template_snippets), before <root>" % ref)
        else:
            out.append("cassandra_logback_root_extra_refs: %s is not an appender of logback.xml (%s); define it in your"
                       " snippet logback.xml:appenders (cassandra_config_template_snippets), or remove it"
                       % (ref, _join(known, ", ")))
    return out


def _cql_table_refusals(refs, cql_table, series):
    out = []
    if cql_table and series != "50x":
        out.append("cassandra_log_cql_table: the system_views.system_logs table is 5.0 only: set it to false for"
                   " Cassandra %s" % series)
    if cql_table and "CQLLOG" in refs:
        out.append("cassandra_logback_root_extra_refs: CQLLOG is named by cassandra_log_cql_table already: remove it"
                   " from the list")
    return out


def _level_refusals(levels, own_loggers, snippet_loggers):
    if not isinstance(levels, Mapping):
        return ["cassandra_log_levels: a dict of {logger name: level} is expected"]
    out = []
    for name, level in levels.items():
        own = "ROOT" if str(name).upper() == "ROOT" else name  # (logback finds the root logger by any case)
        if own in own_loggers:
            out.append("cassandra_log_levels: %s is set by %s: set it there, and remove %s from cassandra_log_levels"
                       % (name, own_loggers[own], name))
        elif name in snippet_loggers:
            out.append("cassandra_log_levels: %s is also a logger of your snippet of logback.xml"
                       " (cassandra_config_template_snippets): logback would set it twice; keep one of them" % name)
        elif str(level).upper() not in LEVELS:
            out.append("cassandra_log_levels: %s: %s is not a level (TRACE, DEBUG, INFO, WARN, ERROR, OFF, ALL; quoted:"
                       " an unquoted OFF is a boolean in YAML)" % (name, level))
    return out


def _xml_value_refusals(var, value, attribute, commented, own_template):
    """What in a value written as it is would leave logback.xml unreadable (XML), each with its way out.
    commented: the switch whose false leaves the value in a comment ("" for none)."""
    text = str(value)
    out = []
    if "<" in text:
        out.append("%s: %s has a <, which ends the XML value there: write it &lt;" % (var, text))
    if _search(STRAY_AMPERSAND, text):
        out.append("%s: %s has an & that starts no XML entity (&amp; &lt; &gt; &quot; &apos; &#number;): write it &amp;"
                   % (var, text))
    if not attribute and "]]>" in text:
        out.append("%s: %s has a ]]>, which XML does not take in a text: write it ]]&gt;" % (var, text))
    if attribute and '"' in text:
        out.append('%s: %s has a ", which ends the XML attribute there: write it &quot;' % (var, text))
    if commented and "--" in text and not own_template:  # (your own template of logback.xml: its comments are yours)
        # (one refusal of a list: no WAY OUT label in it, the alternatives numbered, each within 79 columns)
        ways = ["write it without --", "set %s to true%s" % (commented, TURNED_ON[commented]),
                "add logback.xml to cassandra_config_keep_files: the node keeps its logback.xml as it is",
                "write logback.xml from your own template (cassandra_config_template_overrides)"]
        out.append("\n".join(["%s: %s has a --, which XML does not take in a comment, where the template writes it while"
                               " %s is false. One of:" % (var, text, commented)]
                              + [line for i, way in enumerate(ways) for line in textwrap.wrap(
                                  way, 79, initial_indent="  %d. " % (i + 1), subsequent_indent="     ",
                                  break_long_words=False, break_on_hyphens=False)]))
    return out


def cassandra_config_logback_refusals(refs, known, after_root, levels, own_loggers, snippet_loggers, series, cql_table,
                                      audit_file, overrides, **values):
    """refs: cassandra_logback_root_extra_refs; known: the appenders logback.xml defines before <root>; after_root:
    the ones your snippet logback.xml defines after it; levels: cassandra_log_levels; own_loggers: {logger: the
    variable that sets its level}; snippet_loggers: the loggers of your snippets; series: cassandra_version;
    cql_table, audit_file: cassandra_log_cql_table, cassandra_log_audit_file as booleans; overrides:
    cassandra_config_template_overrides; values: the variables of WRITTEN, and cassandra_log_cql_table_level (5.0).
    e.g. refs ["NOPE"] -> ["cassandra_logback_root_extra_refs: NOPE is not an appender of logback.xml (...); ..."]."""
    out = _ref_refusals(refs, known, after_root) + _cql_table_refusals(refs, cql_table, series)
    out += _level_refusals(levels, own_loggers, snippet_loggers)
    switch = {"": "", "audit": "" if audit_file else "cassandra_log_audit_file"}
    written = [(var, values[var], attribute, switch[commented]) for var, attribute, commented in WRITTEN]
    if series == "50x":
        written.append(("cassandra_log_cql_table_level", values["cassandra_log_cql_table_level"], False,
                        "" if cql_table else "cassandra_log_cql_table"))
    written += [("cassandra_log_levels", name, True, "") for name in (levels if isinstance(levels, Mapping) else {})]
    if not isinstance(refs, (str, Mapping)):
        written += [("cassandra_logback_root_extra_refs", ref, True, "") for ref in refs]
    own_template = "logback.xml" in (overrides if isinstance(overrides, Mapping) else {})
    for var, value, attribute, commented in written:
        out += _xml_value_refusals(var, value, attribute, commented, own_template)
    return out


def cassandra_config_audit_parameter_refusals(parameters, plain_key, unwritable):
    """parameters: cassandra_audit_logger_parameters (not a mapping: none here, the task says so); plain_key: the
    pattern of a key written plain; unwritable: the characters a value cannot hold. Each key that cannot be written
    plain (shown as JSON), and the key of each value that cannot be written (the value not shown: it may be a secret).
    e.g. {"on": 1, "dir": "a\\nb"} -> ['key "on"', "the value of dir"]."""
    out = []
    for key, value in (parameters.items() if isinstance(parameters, Mapping) else []):
        if not (isinstance(key, str) and _match(plain_key, key)):
            out.append("key " + to_json(str(key)))
        elif not isinstance(value, (str, Number)) or _search(unwritable, str(value)):  # (a boolean is a Number)
            out.append("the value of " + key)
    return out


# what bash would split on or expand in a word of a JVM_OPTS="..." line
UNSAFE_WORD = r'[\s"$`\\]'
JMX_TLS_WORDS = ["keystore", "keystore_password", "truststore", "truststore_password", "protocols", "cipher_suites"]


def cassandra_config_jmx_tls_refusals(enabled, on_local_jmx, need_client_auth, **words):
    """enabled, need_client_auth: cassandra_jmx_ssl_enabled and cassandra_jmx_ssl_need_client_auth as booleans;
    on_local_jmx: enabled with cassandra_local_jmx true; words: the cassandra_jmx_ssl_<word> of JMX_TLS_WORDS
    (keystore=...)."""
    out = []
    if on_local_jmx:
        out.append("cassandra_jmx_ssl_enabled needs remote JMX (cassandra_local_jmx: false): the TLS lines are in the"
                   " remote branch of cassandra-env.sh")
    if enabled and not (words["keystore"] and words["keystore_password"]):
        out.append("cassandra_jmx_ssl_enabled needs cassandra_jmx_ssl_keystore and cassandra_jmx_ssl_keystore_password"
                   " (the certificate JMX serves)")
    if need_client_auth and not (enabled and words["truststore"]):
        out.append("cassandra_jmx_ssl_need_client_auth needs cassandra_jmx_ssl_enabled and cassandra_jmx_ssl_truststore"
                   " (the certificates it accepts)")
    for name in JMX_TLS_WORDS:
        if _search(UNSAFE_WORD, str(words[name])):
            out.append("cassandra_jmx_ssl_%s: no blank, quote, $, ` or backslash (a word of a JVM_OPTS line of"
                       " cassandra-env.sh)" % name)
    return out


RACKDC_KEY = "^[A-Za-z0-9_.-]+$"
RACKDC_VALUE_WRONG = r"(^[ \t\f]|[\r\n]|\\$)"  # (a blank first, a line break, a backslash last)


def cassandra_config_rackdc_refusals(settings):
    """cassandra_rackdc_extra_settings -> the keys Java's Properties would not read back as written (key=value);
    "(not a dict)" when it is not a mapping. e.g. {"dc": "x", "a b": 1} -> ["dc", "a b"]."""
    out = []
    for key, value in (settings.items() if isinstance(settings, Mapping) else [["(not a dict)", ""]]):
        if not (str(key) not in ["dc", "rack", "prefer_local"] and _match(RACKDC_KEY, str(key))
                and isinstance(value, (str, Number)) and not _search(RACKDC_VALUE_WRONG, str(value))):
            out.append(str(key))
    return out


RELEASE_NUMBER = "^[0-9]+([.][0-9]+)*$"
ADDRESS = "^([A-Za-z0-9_.-]+|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*)?$"  # a host name, an IPv4 or an IPv6 address, or empty


def _exporter_port_refusals(port, address, options, taken):
    out = []
    if not _match("^[0-9]{1,5}$", str(port)) or do_int(port) < 1 or do_int(port) > 65535:
        out.append("cassandra_prometheus_exporter_port must be a port number (1 to 65535)")
    if not _match(ADDRESS, str(address)):
        out.append("cassandra_prometheus_exporter_address must be a host name or an address (IPv6 without brackets),"
                   " or empty for every address")
    elif ":" in str(address) and "-Djava.net.preferIPv4Stack=false" not in options:
        out.append("cassandra_prometheus_exporter_address %s is an IPv6 address: jvm-server.options sets"
                   " -Djava.net.preferIPv4Stack=true, the JVM could not listen there and Cassandra would not start"
                   " (add -Djava.net.preferIPv4Stack=false to cassandra_jvm_extra_options, or give an IPv4 address or"
                   " none)" % address)
    for var, other in taken:
        if str(other) == str(port):
            out.append("cassandra_prometheus_exporter_port %s is also %s: give the exporter another port" % (other, var))
    return out


def _exporter_file_refusals(keep_files, options, paths, path_pattern):
    out = []
    if "jvm-server.options" in keep_files:
        out.append("jvm-server.options is in cassandra_config_keep_files: the role could not write the agent there"
                   " (remove it from the list, or keep the exporter set up by hand and this variable off)")
    if _search("jmx_prometheus_javaagent", options):
        out.append("a jmx_prometheus_javaagent agent is already in cassandra_jvm*_extra_options or your snippet of"
                   " cassandra-env.sh (cassandra_config_template_snippets): the JVM would load two (the second exits"
                   " on its port); remove it there, the role writes it")
    for var, path in paths:
        if str(path) not in ["", "None"] and not _match(path_pattern, str(path)):
            out.append(var + " must be an absolute path without blanks, quotes or =")
    if str(paths[0][1]) in ["", "None"]:
        out.append("cassandra_prometheus_exporter_dir must not be empty")
    return out


def _exporter_config_refusals(config_file, config, tls, auth):
    out = []
    if config_file and (config or tls or auth):
        out.append("cassandra_prometheus_exporter_config_file names a config managed by other means: the role writes"
                   " none, so cassandra_prometheus_exporter_config, _tls_* and _username/_password would be ignored"
                   " (set them in that file, or leave cassandra_prometheus_exporter_config_file empty)")
    if tls not in [0, 3]:
        out.append("HTTPS needs cassandra_prometheus_exporter_tls_keystore, _tls_keystore_password and"
                   " _tls_certificate_alias together")
    if auth == 1:
        out.append("basic authentication needs cassandra_prometheus_exporter_username and _password together")
    error = cassandra_prometheus_config_error(config)
    if error:
        out.append(error)
    elif config and "httpServer" in from_yaml(config) and (tls or auth):
        out.append("cassandra_prometheus_exporter_config has its own httpServer: set HTTPS and authentication there, or"
                   " with the variables, not both")
    return out


def cassandra_config_exporter_refusals(version, port, address, options, keep_files, exporter_dir, config_file,
                                       path_pattern, config, tls, auth, storage_port, ssl_storage_port,
                                       native_transport_port, jmx_port, native_transport_port_ssl):
    """The exporter's agent makes the JVM exit when it cannot start: version, port, address, exporter_dir,
    config_file: cassandra_prometheus_exporter_*; options: the JVM options the node's Java reads and your snippet of
    cassandra-env.sh, as one text; keep_files: cassandra_config_keep_files; path_pattern: an absolute path the
    options file can hold; config: cassandra_prometheus_exporter_config (trimmed); tls, auth: how many of the HTTPS
    and of the basic authentication variables are set; the other ports of the node (the last one
    cassandra_extra_settings', "" when not there)."""
    out = []
    if not isinstance(version, str) or not _match(RELEASE_NUMBER, version):
        out.append('cassandra_prometheus_exporter_version must be a release number like "1.7.0" (quoted)')
    taken = [("cassandra_storage_port", storage_port), ("cassandra_ssl_storage_port", ssl_storage_port),
             ("cassandra_native_transport_port", native_transport_port), ("cassandra_jmx_port", jmx_port),
             ("native_transport_port_ssl (cassandra_extra_settings)", native_transport_port_ssl)]
    out += _exporter_port_refusals(port, address, options, taken)
    out += _exporter_file_refusals(keep_files, options, [("cassandra_prometheus_exporter_dir", exporter_dir),
                                                         ("cassandra_prometheus_exporter_config_file", config_file)],
                                   path_pattern)
    return out + _exporter_config_refusals(config_file, config, tls, auth)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_config_audit_parameter_refusals": cassandra_config_audit_parameter_refusals,
            "cassandra_config_exporter_refusals": cassandra_config_exporter_refusals,
            "cassandra_config_jmx_tls_refusals": cassandra_config_jmx_tls_refusals,
            "cassandra_config_logback_refusals": cassandra_config_logback_refusals,
            "cassandra_config_override_refusals": cassandra_config_override_refusals,
            "cassandra_config_rackdc_refusals": cassandra_config_rackdc_refusals,
            "cassandra_config_snippet_refusals": cassandra_config_snippet_refusals,
        }
