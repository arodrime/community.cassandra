from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config_refusals called directly: what each check of cassandra_config's checks.yml refuses (the tasks and
# their messages: tests/unit/roles/test_config_*.py).

from ansible_collections.community.cassandra.plugins.filter.cassandra_config_refusals import (
    cassandra_config_audit_parameter_refusals, cassandra_config_exporter_refusals, cassandra_config_jmx_tls_refusals,
    cassandra_config_logback_refusals, cassandra_config_override_refusals, cassandra_config_rackdc_refusals,
    cassandra_config_snippet_refusals)

FILES = ["cassandra.yaml", "logback.xml"]


def test_overrides(tmp_path):
    template = tmp_path / "t.j2"
    template.write_text("x")
    assert cassandra_config_override_refusals({"logback.xml": str(template)}, FILES, []) == []
    assert cassandra_config_override_refusals(["logback.xml"], FILES, []) == ["a dict of {file name: template path} is expected"]
    assert cassandra_config_override_refusals({"x.yaml": "/t", 5: "/t", "logback.xml": "t.j2", "cassandra.yaml": "/none"},
                                              FILES, []) == [
        "x.yaml is not a file the role writes (cassandra.yaml, logback.xml)",
        "5 is not a file the role writes (cassandra.yaml, logback.xml)",
        "logback.xml: an absolute path on the controller is expected (e.g. under inventory_dir)",
        "cassandra.yaml: /none not found on the controller"]
    assert cassandra_config_override_refusals({"logback.xml": str(template)}, FILES, ["logback.xml"]) == [
        "logback.xml is also in cassandra_config_keep_files (left as the node has it): one or the other"]


def test_snippets(tmp_path):
    snippet = tmp_path / "s.j2"
    snippet.write_text("x")
    assert cassandra_config_snippet_refusals({"logback.xml:appenders": str(snippet)}, [], {}) == []
    assert cassandra_config_snippet_refusals({"jvm-server.options": str(snippet)}, [], {}) == [
        "jvm-server.options: only logback.xml, logback.xml:appenders and cassandra-env.sh take a snippet (the JVM options"
        " files: cassandra_jvm*_extra_options)"]
    assert cassandra_config_snippet_refusals({"logback.xml:appenders": str(snippet)}, [], {"logback.xml": "/t"}) == [
        "logback.xml:appenders: its file is also in cassandra_config_template_overrides (your whole template): put the"
        " lines in it, or use the role's template with the snippet"]


def logback(refs=(), levels=None, series="50x", cql_table=False, audit_file=True, overrides=None, **values):
    written = dict((name, "x") for name in [
        "cassandra_log_level", "cassandra_log_level_cassandra", "cassandra_log_system_level", "cassandra_log_pattern",
        "cassandra_log_system_max_file_size", "cassandra_log_system_max_history", "cassandra_log_system_total_size_cap",
        "cassandra_log_debug_max_file_size", "cassandra_log_debug_max_history", "cassandra_log_debug_total_size_cap",
        "cassandra_log_audit_max_file_size", "cassandra_log_audit_max_history", "cassandra_log_audit_total_size_cap",
        "cassandra_log_audit_dir", "cassandra_log_system_dir", "cassandra_log_debug_dir", "cassandra_log_cql_table_level"])
    written.update(values)
    return cassandra_config_logback_refusals(list(refs), ["SYSTEMLOG", "STDOUT"], ["LATE"], levels or {},
                                             {"ROOT": "cassandra_log_level"}, ["com.x"], series, cql_table, audit_file,
                                             overrides or {}, **written)


def test_logback():
    assert logback(["STDOUT"], {"com.y": "warn"}) == []
    assert logback(["NOPE", "LATE"]) == [
        "cassandra_logback_root_extra_refs: NOPE is not an appender of logback.xml (SYSTEMLOG, STDOUT); define it in your"
        " snippet logback.xml:appenders (cassandra_config_template_snippets), or remove it",
        "cassandra_logback_root_extra_refs: LATE is defined in your snippet logback.xml, after <root>, where logback (1.2)"
        " does not find it: move it to your snippet logback.xml:appenders (cassandra_config_template_snippets), before"
        " <root>"]
    assert logback(levels={"Root": "INFO", "com.x": "INFO", "com.z": "LOUD"}) == [
        "cassandra_log_levels: Root is set by cassandra_log_level: set it there, and remove Root from cassandra_log_levels",
        "cassandra_log_levels: com.x is also a logger of your snippet of logback.xml (cassandra_config_template_snippets):"
        " logback would set it twice; keep one of them",
        "cassandra_log_levels: com.z: LOUD is not a level (TRACE, DEBUG, INFO, WARN, ERROR, OFF, ALL; quoted: an unquoted"
        " OFF is a boolean in YAML)"]
    assert logback(series="41x", cql_table=True, cassandra_log_cql_table_level="W--ARN") == [
        "cassandra_log_cql_table: the system_views.system_logs table is 5.0 only: set it to false for Cassandra 41x"]


def test_logback_values_written_as_they_are():
    # the audit appender's values are commented out while cassandra_log_audit_file is false; 5.0's table level while
    # cassandra_log_cql_table is; an attribute ends at a "
    assert logback(cassandra_log_audit_dir="a--b") == []
    assert logback(audit_file=False, cassandra_log_audit_dir="a--b")[0].startswith(
        "cassandra_log_audit_dir: a--b has a --, which XML does not take in a comment, where the template writes it while"
        " cassandra_log_audit_file is false. Way out, one of:\n  write it without --\n  set cassandra_log_audit_file to"
        " true: logback.xml then writes the audit events (FileAuditLogger) to audit/audit.log, no longer to system.log\n")
    assert logback(audit_file=False, overrides={"logback.xml": "/t"}, cassandra_log_audit_dir="a--b") == []
    assert logback(series="41x", cassandra_log_cql_table_level="a--b") == []
    assert logback(cassandra_log_level='I"NFO', cassandra_log_pattern='%msg"', cassandra_log_system_max_history=7) == [
        'cassandra_log_level: I"NFO has a ", which ends the XML attribute there: write it &quot;']
    assert logback(cassandra_log_debug_dir="&amp;&nbsp;") == [
        "cassandra_log_debug_dir: &amp;&nbsp; has an & that starts no XML entity (&amp; &lt; &gt; &quot; &apos;"
        " &#number;): write it &amp;"]


def test_audit_parameters():
    assert cassandra_config_audit_parameter_refusals({"dir": "/x", "n": 1}, "^(?!(?i:y|n|yes|no|true|false|on|off|null)$)"
                                                     "[A-Za-z_][A-Za-z0-9_.-]*$", "[\\x00-\\x1f]") == ['key "n"']
    assert cassandra_config_audit_parameter_refusals({"dir": "a\nb", 5: 1, "x": [1], "b": True}, "^[a-z]+$",
                                                     "[\\x00-\\x1f]") == ["the value of dir", 'key "5"', "the value of x"]


def test_jmx_tls():
    words = dict(keystore="/k", keystore_password="p", truststore="", truststore_password="", protocols="",
                 cipher_suites="")
    assert cassandra_config_jmx_tls_refusals(True, False, False, **words) == []
    wrong = dict(words)
    wrong.update(keystore="", protocols="a b")
    assert cassandra_config_jmx_tls_refusals(True, True, True, **wrong) == [
        "cassandra_jmx_ssl_enabled needs remote JMX (cassandra_local_jmx: false): the TLS lines are in the remote branch"
        " of cassandra-env.sh",
        "cassandra_jmx_ssl_enabled needs cassandra_jmx_ssl_keystore and cassandra_jmx_ssl_keystore_password (the"
        " certificate JMX serves)",
        "cassandra_jmx_ssl_need_client_auth needs cassandra_jmx_ssl_enabled and cassandra_jmx_ssl_truststore (the"
        " certificates it accepts)",
        "cassandra_jmx_ssl_protocols: no blank, quote, $, ` or backslash (a word of a JVM_OPTS line of cassandra-env.sh)"]


def test_rackdc():
    assert cassandra_config_rackdc_refusals({"dc_suffix": "_x", "n": 3}) == []
    assert cassandra_config_rackdc_refusals({"dc": "x", "a b": 1, "s": " x", "t": "x\\", 5: [1]}) == ["dc", "a b", "s", "t", "5"]
    assert cassandra_config_rackdc_refusals(["dc_suffix=x"]) == ["(not a dict)"]


def exporter(**changes):
    args = dict(version="1.7.0", port=9404, address="", options="", keep_files=[], exporter_dir="/opt/x", config_file="",
                path_pattern="^/[^\\s\"'=]*$", config="", tls=0, auth=0, storage_port=7000, ssl_storage_port=7001,
                native_transport_port=9042, jmx_port=7199, native_transport_port_ssl="")
    args.update(changes)
    return cassandra_config_exporter_refusals(**args)


def test_exporter():
    assert exporter() == []
    assert exporter(version=1.7, port="00000", jmx_port=0) == [
        'cassandra_prometheus_exporter_version must be a release number like "1.7.0" (quoted)',
        "cassandra_prometheus_exporter_port must be a port number (1 to 65535)"]
    assert exporter(port="7199", address="fd00::1") == [
        "cassandra_prometheus_exporter_address fd00::1 is an IPv6 address: jvm-server.options sets"
        " -Djava.net.preferIPv4Stack=true, the JVM could not listen there and Cassandra would not start (add"
        " -Djava.net.preferIPv4Stack=false to cassandra_jvm_extra_options, or give an IPv4 address or none)",
        "cassandra_prometheus_exporter_port 7199 is also cassandra_jmx_port: give the exporter another port"]
    assert exporter(address="fd00::1", options="-Djava.net.preferIPv4Stack=false") == []
    assert exporter(exporter_dir="", config_file="rel.yml", tls=2, auth=1) == [
        "cassandra_prometheus_exporter_config_file must be an absolute path without blanks, quotes or =",
        "cassandra_prometheus_exporter_dir must not be empty",
        "cassandra_prometheus_exporter_config_file names a config managed by other means: the role writes none, so"
        " cassandra_prometheus_exporter_config, _tls_* and _username/_password would be ignored (set them in that file,"
        " or leave cassandra_prometheus_exporter_config_file empty)",
        "HTTPS needs cassandra_prometheus_exporter_tls_keystore, _tls_keystore_password and _tls_certificate_alias"
        " together",
        "basic authentication needs cassandra_prometheus_exporter_username and _password together"]
    assert exporter(config="httpServer:\n  ssl: {}", tls=3) == [
        "cassandra_prometheus_exporter_config has its own httpServer: set HTTPS and authentication there, or with the"
        " variables, not both"]
    assert exporter(config="rules: [") == ["cassandra_prometheus_exporter_config is not valid YAML (line 1, column 9)"]
