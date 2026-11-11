from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# logback.xml's variables, each read back by the import from a node's file (round trip: the import's variables,
# the self-check renders the same settings), written only when not the default of the node's series: the audit
# file (the stock AUDIT appender), the appenders <root> names besides the stock ones, the loggers' levels, the
# node's own appenders (its snippet logback.xml:appenders).

import re

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import cassandra_import_self_check
from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_import_check import (
    FACTS, import_node, seeded, stock)

SERIES = ("40x", "41x", "50x")
MINE = ['  <appender name="MINE" class="ch.qos.logback.core.FileAppender">',
        "    <file>${cassandra.logdir}/mine.log</file>",
        "    <encoder><pattern>%msg%n</pattern></encoder>",
        "  </appender>"]


def audit_on(text):
    """The stock AUDIT appender and its logger uncommented in place, as an operator does."""
    for old, new in (('  <!-- <appender name="AUDIT"', '  <appender name="AUDIT"'),
                     ('SizeAndTimeBasedRollingPolicy"> -->', 'SizeAndTimeBasedRollingPolicy">'),
                     ("<!-- <fileNamePattern>${cassandra.logdir}/audit/", "<fileNamePattern>${cassandra.logdir}/audit/"),
                     (".%i.zip</fileNamePattern> -->", ".%i.zip</fileNamePattern>"),
                     ("<!-- <maxFileSize>50MB</maxFileSize>", "<maxFileSize>50MB</maxFileSize>"),
                     ("  </appender> -->", "  </appender>"),
                     ('  <!-- <logger name="org.apache.cassandra.audit"', '  <logger name="org.apache.cassandra.audit"'),
                     ("  </logger> -->", "  </logger>")):
        assert old in text, old
        text = text.replace(old, new, 1)
    return text


def file_auditing(files, series):
    """cassandra.yaml: audit on, with FileAuditLogger."""
    text = files["cassandra.yaml"]
    text = re.sub(r"(?m)^(audit_logging_options:\n\s+)enabled: false", r"\1enabled: true", text)
    text = re.sub(r"(?m)^(\s+- class_name: )BinAuditLogger", r"\1FileAuditLogger", text, count=1)
    assert "class_name: FileAuditLogger" in text
    return dict(files, **{"cassandra.yaml": text})


def node_files(series, logback=None, auditing=False):
    files = seeded(stock(series))
    if logback:
        files["logback.xml"] = logback(files["logback.xml"])
    return file_auditing(files, series) if auditing else files


def imported(files, series):
    """The variables the import writes, and the self-check of its inventory (its snippets about to be written)."""
    inventory, layout = import_node(files, series)
    check = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra",
                                        "11", "", {}, inventory_dir="/p/inv")
    variables = {}
    for f in inventory:
        if f["path"].endswith(".yml"):
            variables.update(yaml.safe_load(f["content"]) or {})
    return variables, dict((f["path"], f["content"]) for f in inventory if f["path"].endswith(".j2")), check


@pytest.mark.parametrize("series", SERIES)
def test_the_stock_file_writes_no_logback_variable(series):
    variables, snippets, check = imported(node_files(series), series)
    assert check["differences"] == []
    # (the stock file sends the logs to the console too: cassandra_log_console, as before)
    assert not [k for k in variables if k.startswith(("cassandra_log", "cassandra_logback"))
                and k != "cassandra_log_console"] and snippets == {}


@pytest.mark.parametrize("series", SERIES)
def test_the_audit_block_uncommented_in_place(series):
    # audit to its own file, Cassandra's audit off (or BinAuditLogger): written, as it differs from its default
    variables, snippets, check = imported(node_files(series, audit_on), series)
    assert variables["cassandra_log_audit_file"] is True and snippets == {}
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_file_auditing_with_the_audit_block_is_the_default(series):
    variables, snippets, check = imported(node_files(series, audit_on, auditing=True), series)
    assert "cassandra_log_audit_file" not in variables
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_file_auditing_into_system_log_on_purpose(series):
    # FileAuditLogger, the AUDIT block left commented: its events in system.log, kept so (false written)
    variables, snippets, check = imported(node_files(series, auditing=True), series)
    assert variables["cassandra_log_audit_file"] is False
    assert check["differences"] == []


def root_ref(name):
    def edit(text):
        return text.replace('    <appender-ref ref="SYSTEMLOG" />',
                            '    <appender-ref ref="SYSTEMLOG" />\n    <appender-ref ref="%s"/>' % name, 1)
    return edit


@pytest.mark.parametrize("series", SERIES)
def test_an_appender_ref_added_to_root(series):
    variables, snippets, check = imported(node_files(series, lambda t: root_ref("AUDIT")(audit_on(t))), series)
    assert variables["cassandra_logback_root_extra_refs"] == ["AUDIT"]
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_an_appender_of_its_own_in_the_middle_and_named_by_root(series):
    def edit(text):
        text = text.replace("  <!-- STDOUT console appender", "\n".join(MINE) + "\n\n  <!-- STDOUT console appender")
        return root_ref("MINE")(text)
    variables, snippets, check = imported(node_files(series, edit), series)
    assert variables["cassandra_logback_root_extra_refs"] == ["MINE"]
    assert variables["cassandra_config_template_snippets"] == {
        "logback.xml:appenders": "{{ inventory_dir }}/group_vars/test_cluster/logback.xml.appenders.snippet.j2"}
    assert snippets["group_vars/test_cluster/logback.xml.appenders.snippet.j2"].endswith("\n".join(MINE) + "\n")
    assert "(logback.xml:appenders)" not in variables
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_simple_loggers_as_levels(series):
    # one in the middle, one at the end: cassandra_log_levels; one with more than a level stays an end line
    def edit(text):
        text = text.replace("  <root level", '  <logger name="com.example.a" level="WARN"/>\n  <root level', 1)
        return text.replace("</configuration>", '  <logger name="com.example.b" level="ERROR" />\n'
                                                '  <logger name="com.example.c" level="INFO" additivity="false"/>\n'
                                                "</configuration>")
    variables, snippets, check = imported(node_files(series, edit), series)
    assert variables["cassandra_log_levels"] == {"com.example.a": "WARN", "com.example.b": "ERROR"}
    assert list(snippets.values()) == ['  <logger name="com.example.c" level="INFO" additivity="false"/>\n']
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_a_level_from_a_property_stays_an_end_line(series):
    # (rc9 r2 audit) level="${LVL:-INFO}": not a level cassandra_config takes in cassandra_log_levels
    line = '  <logger name="com.example.p" level="${LVL:-INFO}"/>'
    variables, snippets, check = imported(node_files(series, lambda t: t.replace(
        "</configuration>", line + "\n</configuration>")), series)
    assert "cassandra_log_levels" not in variables
    assert list(snippets.values()) == [line + "\n"]
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_an_appender_named_in_single_quotes_and_named_by_root(series):
    # (rc9 r2 audit) name='Q': the ref of <root> to it kept
    def edit(text):
        mine = "  <appender name='Q' class='ch.qos.logback.core.ConsoleAppender'/>"
        return root_ref("Q")(text.replace("  <root level", mine + "\n  <root level", 1))
    variables, snippets, check = imported(node_files(series, edit), series)
    assert variables["cassandra_logback_root_extra_refs"] == ["Q"]
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_an_appender_named_with_spaces_and_named_by_root(series):
    # (rc9 r3 audit) name = "Q" (spaces around =), a filename= after it: its name is Q
    def edit(text):
        mine = '  <appender name = "Q" class="ch.qos.logback.core.FileAppender" filename="f"/>'
        return root_ref("Q")(text.replace("  <root level", mine + "\n  <root level", 1))
    variables, snippets, check = imported(node_files(series, edit), series)
    assert variables["cassandra_logback_root_extra_refs"] == ["Q"]
    assert check["differences"] == []


def test_a_level_for_a_logger_a_variable_sets_is_not_taken():
    def edit(text):
        return text.replace("</configuration>", '  <logger name="org.apache.cassandra.audit" level="WARN"/>\n'
                                                "</configuration>")
    out = cassandra_config_import(node_files("50x", edit), "50x", FACTS, "", "/var/lib/cassandra")
    assert "cassandra_log_levels" not in out["vars"]


def rotation(text):
    """system.log kept 14 days, debug.log files of 100MB, audit.log 10GB in all (in their appender each)."""
    blocks = text.split("<appender name=")
    for i, block in enumerate(blocks):
        if block.startswith('"SYSTEMLOG"'):
            blocks[i] = block.replace("<maxHistory>7</maxHistory>", "<maxHistory>14</maxHistory>", 1)
        elif block.startswith('"DEBUGLOG"'):
            blocks[i] = block.replace("<maxFileSize>50MB</maxFileSize>", "<maxFileSize>100MB</maxFileSize>", 1)
        elif block.startswith('"AUDIT"'):
            blocks[i] = block.replace("<totalSizeCap>5GB</totalSizeCap>", "<totalSizeCap>10GB</totalSizeCap>", 1)
    return "<appender name=".join(blocks)


@pytest.mark.parametrize("series", SERIES)
def test_the_rotation_of_each_file(series):
    variables, snippets, check = imported(node_files(series, lambda t: rotation(audit_on(t))), series)
    assert dict((k, v) for k, v in variables.items() if re.match(r"cassandra_log_\w+_(max|total)_", k)) == {
        "cassandra_log_system_max_history": 14, "cassandra_log_debug_max_file_size": "100MB",
        "cassandra_log_audit_total_size_cap": "10GB"}
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_one_pattern_for_every_line(series):
    def edit(text):
        return re.sub(r"<pattern>[^<]*</pattern>", "<pattern>%d %level %msg%n</pattern>", text)
    variables, snippets, check = imported(node_files(series, edit), series)
    assert variables["cassandra_log_pattern"] == "%d %level %msg%n"
    assert check["differences"] == []


def test_the_system_logs_table_of_5_0():
    def edit(text):
        text = text.replace('  <!-- <appender name="CQLLOG"', '  <appender name="CQLLOG"')
        text = text.replace("      <level>WARN</level>\n    </filter>\n  </appender> -->",
                            "      <level>ERROR</level>\n    </filter>\n  </appender>")
        return text.replace('    <!--\n    <appender-ref ref="CQLLOG"/>\n    -->', '    <appender-ref ref="CQLLOG"/>')
    variables, snippets, check = imported(node_files("50x", edit), "50x")
    assert variables["cassandra_log_cql_table"] is True and variables["cassandra_log_cql_table_level"] == "ERROR"
    assert "cassandra_logback_root_extra_refs" not in variables
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_one_encoders_own_pattern_is_not_everyones(series):
    # (audit) the console's pattern alone changed: not cassandra_log_pattern (it would change the others)
    def edit(text):
        at = text.index('<appender name="STDOUT"')
        return text[:at] + re.sub(r"<pattern>[^<]*</pattern>", "<pattern>%msg%n</pattern>", text[at:], count=1)
    out = cassandra_config_import(node_files(series, edit), series, FACTS, "", "/var/lib/cassandra")
    assert "cassandra_log_pattern" not in out["vars"]
    assert [line for line in out["hand_edits"] if line.startswith("logback.xml")]


@pytest.mark.parametrize("series", SERIES)
def test_the_stock_metrics_appender_uncommented_is_carried(series):
    # (audit) LogbackMetrics: no variable of its own, its appender in the snippet with root's ref to it
    def edit(text):
        text = text.replace("  <!-- Uncomment bellow and corresponding appender-ref to activate logback metrics\n"
                            '  <appender name="LogbackMetrics" class="com.codahale.metrics.logback.InstrumentedAppender" />\n'
                            "   -->",
                            '  <appender name="LogbackMetrics" class="com.codahale.metrics.logback.InstrumentedAppender" />')
        return text.replace("    <!--\n    <appender-ref ref=\"LogbackMetrics\" />\n    -->",
                            '    <appender-ref ref="LogbackMetrics" />')
    files = node_files(series, edit)
    assert '    <appender-ref ref="LogbackMetrics" />\n' in files["logback.xml"]
    variables, snippets, check = imported(files, series)
    assert variables["cassandra_logback_root_extra_refs"] == ["LogbackMetrics"]
    assert any("LogbackMetrics" in text for text in snippets.values())
    assert check["differences"] == []


def test_an_appender_with_a_secret_its_secret_in_secrets_yml():
    # an appender with a password (an element's text: not taken out alone): the lines of the snippet
    # logback.xml:appenders in secrets.yml, which the snippet renders (never in clear in it); root's ref written
    def edit(text):
        text = text.replace("  <root level", '  <appender name="DB" class="ch.qos.logback.core.ConsoleAppender">\n'
                                             "    <password>s3cret</password>\n  </appender>\n  <root level", 1)
        return root_ref("DB")(text)
    inventory, layout = import_node(node_files("50x", edit), "50x")
    clear = "\n".join(f["content"] for f in inventory if not f["secret"])
    assert "s3cret" not in clear and "{{ cassandra_secret_logback_appenders_lines }}" in clear
    assert "s3cret" in "\n".join(f["content"] for f in inventory if f["secret"])
    variables, snippets, check = imported(node_files("50x", edit), "50x")
    assert variables["cassandra_logback_root_extra_refs"] == ["DB"] and check["differences"] == []


def test_a_ref_to_an_appender_not_carried_is_not_written():
    # (audit) the AUDIT appender live and named by <root>, without the audit logger: no audit file, so no ref to it
    def edit(text):
        text = audit_on(text).replace('  <logger name="org.apache.cassandra.audit" additivity="false" level="INFO">\n'
                                      '    <appender-ref ref="AUDIT"/>\n  </logger>', "")
        return root_ref("AUDIT")(text)
    out = cassandra_config_import(node_files("50x", edit), "50x", FACTS, "", "/var/lib/cassandra")
    assert out["vars"].get("cassandra_log_audit_file") is not True
    assert "cassandra_logback_root_extra_refs" not in out["vars"]


def test_the_root_logger_by_another_name_is_not_a_level():
    def edit(text):
        return text.replace("</configuration>", '  <logger name="root" level="DEBUG"/>\n</configuration>')
    out = cassandra_config_import(node_files("50x", edit), "50x", FACTS, "", "/var/lib/cassandra")
    assert "root" not in (out["vars"].get("cassandra_log_levels") or {})


@pytest.mark.parametrize("series", SERIES)
def test_the_rotation_of_a_commented_audit_block_is_not_read(series):
    # (audit) the AUDIT block commented out, its maxHistory edited there: no effect, no variable
    def edit(text):
        at = text.index('<appender name="AUDIT"')
        return text[:at] + text[at:].replace("<maxHistory>30</maxHistory>", "<maxHistory>60</maxHistory>", 1)
    out = cassandra_config_import(node_files(series, edit), series, FACTS, "", "/var/lib/cassandra")
    assert "cassandra_log_audit_max_history" not in out["vars"]


@pytest.mark.parametrize("series", SERIES)
@pytest.mark.parametrize("auditing, logger, block, written", [
    (False, None, False, None),                 # audit off, the block commented: the stock file
    (True, "BinAuditLogger", False, None),     # BinAuditLogger: the block not wanted
    (True, "BinAuditLogger", True, True),      # BinAuditLogger and the block on all the same: kept so
    (True, "FileAuditLogger", True, None),     # FileAuditLogger with the block: the default
    (True, "FileAuditLogger", False, False),   # FileAuditLogger without it (to system.log): kept so
])
def test_the_audit_file_against_its_default_resolved_with_the_nodes_values(series, auditing, logger, block, written):
    files = seeded(stock(series))
    if block:
        files["logback.xml"] = audit_on(files["logback.xml"])
    if auditing:
        text = re.sub(r"(?m)^(audit_logging_options:\n\s+)enabled: false", r"\1enabled: true", files["cassandra.yaml"])
        files["cassandra.yaml"] = re.sub(r"(?m)^(\s+- class_name: )BinAuditLogger", r"\g<1>" + logger, text, count=1)
    variables, snippets, check = imported(files, series)
    assert variables.get("cassandra_log_audit_file") is written
    assert check["differences"] == []


@pytest.mark.parametrize("series", SERIES)
def test_a_urls_password_in_an_appender_goes_to_secrets_yml(series):
    # (rc9 audit) <url>https://bob:pw@h/x</url> in an appender of its own: the password in secrets.yml, the snippet
    # refers to it; never in clear in a file of the inventory written in clear
    def edit(text):
        block = ('  <appender name="H" class="ch.qos.logback.core.ConsoleAppender">\n'
                 "    <url>https://bob:hunter2xx@h/x</url>\n  </appender>")
        return root_ref("H")(text.replace("  <root level", block + "\n  <root level", 1))
    inventory, layout = import_node(node_files(series, edit), series)
    clear = "\n".join(f["content"] for f in inventory if not f["secret"])
    assert "hunter2xx" not in clear and "hunter2xx" not in layout["report"]
    assert "<url>https://bob:{{ cassandra_secret_logback_appenders_bob_password }}@h/x</url>" in clear
    variables, snippets, check = imported(node_files(series, edit), series)
    assert check["differences"] == []


@pytest.mark.parametrize("password, taken", [("p@ss@XYZ1", True), ("it'sXYZ1", False), ("it{sXYZ1", False)])
@pytest.mark.parametrize("series", SERIES)
def test_a_urls_password_with_an_at_a_quote_or_a_brace_in_an_appender_never_in_clear(series, password, taken):
    # (rc9 r2 audit) an @: the password up to the last @ in secrets.yml; a quote or a brace: the lines whole
    def edit(text):
        block = ('  <appender name="H" class="ch.qos.logback.core.ConsoleAppender">\n'
                 "    <url>https://bob:%s@h/x</url>\n  </appender>" % password)
        return root_ref("H")(text.replace("  <root level", block + "\n  <root level", 1))
    inventory, layout = import_node(node_files(series, edit), series)
    clear = "\n".join(f["content"] for f in inventory if not f["secret"])
    assert "XYZ1" not in clear and "XYZ1" not in layout["report"]
    assert ("<url>https://bob:{{ cassandra_secret_logback_appenders_bob_password }}@h/x</url>" in clear) is taken
    assert password in "\n".join(f["content"] for f in inventory if f["secret"])
    variables, snippets, check = imported(node_files(series, edit), series)
    assert check["differences"] == []


def live_pattern(pattern):
    """The pattern of the appenders logback reads (not those of the audit block, when commented out)."""
    def edit(text):
        parts = text.split('<!-- <appender name="AUDIT"')
        parts[0] = re.sub(r"<pattern>[^<]*</pattern>", "<pattern>%s</pattern>" % pattern, parts[0])
        return '<!-- <appender name="AUDIT"'.join(parts)
    return edit


@pytest.mark.parametrize("series", SERIES)
def test_a_pattern_with_two_dashes_and_the_audit_block_commented_out_is_not_carried(series):
    # (rc9 r3 audit) the template writes the pattern in the commented-out audit appender too, where XML does not
    # take a --: cassandra_config would refuse it; the lines stay hand edits, the report says why and what to do
    files = node_files(series, live_pattern("%-5level [%thread] -- %msg%n"))
    variables, snippets, check = imported(files, series)
    assert "cassandra_log_pattern" not in variables
    # (its lines, as for any hand edit: no longer "cannot be read as its program reads it")
    assert check["differences"] and all(re.match(r"logback\.xml: node has .*/encoder/pattern = %-5level \[%thread\] -- "
                                                 r"%msg%n, import would write ", d) for d in check["differences"])
    out = cassandra_config_import(files, series, FACTS, "", "/var/lib/cassandra")
    assert ("logback.xml: its log pattern (%-5level [%thread] -- %msg%n) has a --, which the role's template would"
            " write in the audit appender it leaves commented out (cassandra_log_audit_file: false), where XML does"
            " not take it: not carried in cassandra_log_pattern. Write the pattern without -- on the nodes and import"
            " again, or keep logback.xml as it is (-e import_cluster_keep_hand_edits=logback.xml)") in out["hand_edits"]
    dummy, layout = import_node(files, series)
    assert "its log pattern (%-5level [%thread] -- %msg%n) has a --" in " ".join(layout["report"].split())


@pytest.mark.parametrize("series", SERIES)
def test_a_pattern_with_two_dashes_and_the_audit_appender_on_is_carried(series):
    files = node_files(series, lambda text: live_pattern("%-5level [%thread] -- %msg%n")(audit_on(text)), auditing=True)
    variables, snippets, check = imported(files, series)
    assert variables["cassandra_log_pattern"] == "%-5level [%thread] -- %msg%n"
    assert check["differences"] == []
