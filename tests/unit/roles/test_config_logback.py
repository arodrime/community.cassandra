from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config's logback.xml: with the defaults, each series' stock file line for line; the audit file (the
# stock AUDIT appender and its logger, uncommented), the appenders <root> names besides the stock ones, the
# loggers' levels; the checks that refuse what logback could not read, and the warning on audit events going to
# system.log.

import os
import xml.etree.ElementTree as ET

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _load_role, _render
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

load_collection_filters()  # (the role's messages use the collection's filters)

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config")
STOCK = os.path.join(ROLE, "molecule", "default", "files", "stock-%s")
VERSIONS = {"4.0": "4.0.21", "4.1": "4.1.12", "5.0": "5.0.9"}
with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as f:
    ROLE_VARS = yaml.safe_load(f)
with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as f:
    DEFAULTS = yaml.safe_load(f)


def trusted(value):
    return trust_as_template(value) if isinstance(value, str) and "{" in value else value


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template),
                                                                      escape_backslashes=False)


def role_ctx(series, **variables):
    ctx = dict((k, trusted(v)) for k, v in dict(DEFAULTS, **ROLE_VARS).items())
    ctx.update(cassandra_version=series[0] + series[2] + "x", cassandra_config_template_snippets={})
    ctx.update(variables)
    return ctx


def logback(series, **variables):
    with open(os.path.join(ROLE, "templates", series, "logback.xml.j2"), encoding="utf-8") as f:
        text = f.read().split("\n", 1)[1]  # (without the header line)
    return render(text, **role_ctx(series, **variables))


@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_the_defaults_give_the_stock_file(series):
    with open(os.path.join(STOCK % VERSIONS[series], "logback.xml.stock"), encoding="utf-8") as f:
        stock = f.read()
    # (the stock file sends the logs to the console too: cassandra_log_console)
    assert logback(series, cassandra_log_console=True).split("\n") == stock.split("\n")


@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_the_audit_file(series):
    root = ET.fromstring(logback(series, cassandra_log_audit_file=True))
    audit = [a for a in root.findall("appender") if a.get("name") == "AUDIT"]
    assert audit and audit[0].find("rollingPolicy/fileNamePattern").text.endswith("/audit/audit.log.%d{yyyy-MM-dd}.%i.zip")
    assert audit[0].find("rollingPolicy/maxFileSize").text == "50MB"
    logger = [lg for lg in root.findall("logger") if lg.get("name") == "org.apache.cassandra.audit"]
    assert [r.get("ref") for r in logger[0].findall("appender-ref")] == ["AUDIT"]
    assert not [a for a in ET.fromstring(logback(series)).findall("appender") if a.get("name") == "AUDIT"]


@pytest.mark.parametrize("auditing, logger, on", [
    (True, "FileAuditLogger", True), (True, "org.apache.cassandra.audit.FileAuditLogger", True),
    (True, "BinAuditLogger", False), (False, "FileAuditLogger", False)])
def test_the_audit_file_follows_file_auditing_by_default(auditing, logger, on):
    ctx = role_ctx("5.0", cassandra_audit_logging_enabled=auditing, cassandra_audit_logger_class_name=logger)
    assert render("{{ cassandra_log_audit_file }}", **ctx) is on


@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_root_refs_and_levels(series):
    root = ET.fromstring(logback(series, cassandra_log_audit_file=True, cassandra_logback_root_extra_refs=["AUDIT"],
                                 cassandra_log_levels={"com.example": "WARN", "org.x": "ERROR"}))
    assert [r.get("ref") for r in root.find("root").findall("appender-ref")][-1] == "AUDIT"
    assert [(lg.get("name"), lg.get("level")) for lg in root.findall("logger")][-3:] == [
        ("org.apache.cassandra", "DEBUG"), ("com.example", "WARN"), ("org.x", "ERROR")]


def task(name):
    todo = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


REFUSE = task("Refuse logback appender-refs, levels and values the role cannot write")
WARN = task("Warn about audit events going to system.log")


def refused(appenders="", **variables):
    ctx = role_ctx("5.0", _cassandra_config_snippet_appenders=appenders, _cassandra_config_snippet_logback="",
                   **variables)
    for name in ("_known", "_after_root", "_snippet_loggers"):
        ctx[name] = trust_as_template(REFUSE["vars"][name])
    return render(REFUSE["vars"]["_wrong"], **ctx)


def test_refs_to_known_appenders_pass():
    assert refused(cassandra_logback_root_extra_refs=["STDOUT", "AUDIT"], cassandra_log_audit_file=True) == []
    assert refused('  <appender name="MINE" class="x"/>', cassandra_logback_root_extra_refs=["MINE"]) == []
    assert refused(cassandra_log_levels={"com.example": "warn"}) == []


@pytest.mark.parametrize("variables, said", [
    ({"cassandra_logback_root_extra_refs": ["AUDIT"]}, "cassandra_logback_root_extra_refs: AUDIT is not an appender of"
                                                       " logback.xml (SYSTEMLOG, DEBUGLOG, ASYNCDEBUGLOG, STDOUT); define"
                                                       " it in your snippet logback.xml:appenders"),
    ({"cassandra_logback_root_extra_refs": "MINE"}, "cassandra_logback_root_extra_refs: a list of appender names"),
    ({"cassandra_log_levels": {"org.apache.cassandra": "INFO"}}, "cassandra_log_levels: org.apache.cassandra is set by"
                                                                 " cassandra_log_level_cassandra: set it there, and"
                                                                 " remove org.apache.cassandra from"),
    ({"cassandra_log_levels": {"org.apache.cassandra.audit": "INFO"}}, "cassandra_log_levels: org.apache.cassandra.audit"
                                                                       " is set by cassandra_log_audit_file"),
    ({"cassandra_log_levels": {"ROOT": "INFO"}}, "cassandra_log_levels: ROOT is set by cassandra_log_level"),
    ({"cassandra_log_levels": {"com.x": "LOUD"}}, "cassandra_log_levels: com.x: LOUD is not a level"),
    ({"cassandra_log_levels": ["com.x"]}, "cassandra_log_levels: a dict of {logger name: level} is expected"),
])
def test_refusals(variables, said):
    wrong = refused(**variables)
    assert len(wrong) == 1 and wrong[0].startswith(said), wrong


@pytest.mark.parametrize("auditing, logger, audit_file, warned", [
    (True, "FileAuditLogger", False, True), (True, "FileAuditLogger", True, False),
    (True, "BinAuditLogger", False, False), (False, "FileAuditLogger", False, False)])
def test_warns_when_file_audit_events_go_to_system_log(auditing, logger, audit_file, warned):
    ctx = role_ctx("5.0", cassandra_audit_logging_enabled=auditing, cassandra_audit_logger_class_name=logger,
                   cassandra_log_audit_file=audit_file)
    assert all(render("{{ %s }}" % w, **ctx) for w in WARN["when"]) is warned


def generator():
    import sys
    sys.path.insert(0, os.path.join(ROLE, "..", "..", "tools"))
    try:
        import gen_templates
    finally:
        sys.path.pop(0)
    return gen_templates


@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_the_templates_are_the_generators(series):
    # every template but cassandra.yaml (derived from the reference's) is what tools/gen_templates.py writes
    gen = generator()
    for name, rules in sorted(gen.RULES[series].items()):
        if name == "cassandra.yaml":
            continue
        with open(os.path.join(STOCK % VERSIONS[series], name + ".stock"), encoding="utf-8") as f:
            lines = f.read().split("\n")
        gen.replace_lines(name, lines, rules)
        with open(os.path.join(ROLE, "templates", series, name + ".j2"), encoding="utf-8") as f:
            assert f.read().split("\n")[1:] == lines, name


def test_a_rule_scoped_to_an_appender_fails_when_the_release_has_no_such_appender():
    gen = generator()
    with open(os.path.join(STOCK % "5.0.9", "logback.xml.stock"), encoding="utf-8") as f:
        lines = f.read().replace('<appender name="AUDIT"', '<appender name="AUDITLOG"').split("\n")
    with pytest.raises(AssertionError, match=r"<appender/logger name='AUDIT'> found 0 times \(the release moved it\?\)"):
        gen.replace_lines("logback.xml", lines, gen.AUDIT_FILE)
    # a line the appender has not (another release's): said with the appender
    with open(os.path.join(STOCK % "5.0.9", "logback.xml.stock"), encoding="utf-8") as f:
        lines = f.read().replace("      <!-- <maxFileSize>50MB</maxFileSize>", "      <!-- <maxFileSize>64MB</maxFileSize>").split("\n")
    with pytest.raises(AssertionError, match=r"logback.xml, <AUDIT>: '      <!-- <maxFileSize>50MB</maxFileSize>' found 0"):
        gen.replace_lines("logback.xml", lines, gen.AUDIT_FILE)


def test_the_system_logs_table_is_5_0_only():
    assert refused(cassandra_log_cql_table=True) == []
    root = ET.fromstring(logback("5.0", cassandra_log_cql_table=True, cassandra_log_cql_table_level="ERROR"))
    assert "CQLLOG" in [r.get("ref") for r in root.find("root").findall("appender-ref")]
    assert [a.find("filter/level").text for a in root.findall("appender") if a.get("name") == "CQLLOG"] == ["ERROR"]
    ctx = role_ctx("4.1", cassandra_log_cql_table=True, _cassandra_config_snippet_appenders="",
                   _cassandra_config_snippet_logback="")
    for name in ("_known", "_after_root", "_snippet_loggers"):
        ctx[name] = trust_as_template(REFUSE["vars"][name])
    assert render(REFUSE["vars"]["_wrong"], **ctx) == [
        "cassandra_log_cql_table: the system_views.system_logs table is 5.0 only: set it to false for Cassandra 41x"]


def test_the_root_by_any_case_and_a_kept_logback():
    # (audit) logback finds the root logger by its name in any case; a logback.xml kept as it is: nothing refused
    assert refused(cassandra_log_levels={"root": "DEBUG"}) == [
        "cassandra_log_levels: root is set by cassandra_log_level: set it there, and remove root from cassandra_log_levels"]
    ctx = role_ctx("5.0", cassandra_config_keep_files=["logback.xml"])
    assert render("{{ %s }}" % REFUSE["when"], **ctx) is False


def test_a_ref_to_an_appender_after_root_and_a_logger_set_twice_are_refused():
    end = '  <appender name="LATE" class="x"/>\n  <logger name="com.x" level="INFO"/>'
    ctx = role_ctx("5.0", _cassandra_config_snippet_appenders="", _cassandra_config_snippet_logback=end,
                   cassandra_logback_root_extra_refs=["LATE"], cassandra_log_levels={"com.x": "WARN"})
    for name in ("_known", "_after_root", "_snippet_loggers"):
        ctx[name] = trust_as_template(REFUSE["vars"][name])
    assert render(REFUSE["vars"]["_wrong"], **ctx) == [
        "cassandra_logback_root_extra_refs: LATE is defined in your snippet logback.xml, after <root>, where logback"
        " (1.2) does not find it: move it to your snippet logback.xml:appenders (cassandra_config_template_snippets),"
        " before <root>",
        "cassandra_log_levels: com.x is also a logger of your snippet of logback.xml (cassandra_config_template_snippets):"
        " logback would set it twice; keep one of them"]


def test_the_role_refuses_the_variables_no_longer_read():
    # (rc9 audit) the role run alone (a site playbook) refuses them too, before anything is written
    tasks = load_tasks(os.path.join(ROLE, "tasks", "checks.yml"))
    names = [t.get("name") for t in tasks]
    refuse = tasks[names.index("Refuse the variables no longer read")]
    assert "when" not in refuse and names.index(refuse["name"]) < names.index("Refuse template snippets the role cannot use")
    msg = render(refuse["ansible.builtin.assert"]["fail_msg"], _removed=["cassandra_env_extra_lines"])
    assert msg == ("cassandra_env_extra_lines is no longer read: cassandra-env.sh would be written without its lines."
                   " Re-import the cluster (import_cluster), which writes these lines into snippet files"
                   " (cassandra_config_template_snippets), then remove cassandra_env_extra_lines from the inventory"
                   " files that set it. Nothing is changed.")


CONFIRM = load_tasks(os.path.join(ROLE, "tasks", "confirm.yml"))
MOVES = next(t for t in CONFIRM if t.get("name") == "Find whether the audit events would move to audit.log")


@pytest.mark.parametrize("live_audit, in_inventory, audit_file, said", [
    (False, False, True, True),   # the default turns it on, the node has it off: said
    (True, False, True, False),   # the node has it on already
    ("class", False, True, False),  # (rc9 r2 audit) on already, its class before its name, in single quotes
    ("spaces", False, True, False),  # (rc9 r3 audit) on already, name = "AUDIT"
    (False, True, True, False),   # set in the inventory: wanted
    (False, False, False, False),  # off
])
def test_said_when_the_default_moves_the_audit_events(live_audit, in_inventory, audit_file, said):
    # (rc9 audit) an inventory imported by a release candidate, FileAuditLogger, the stock block commented out
    import base64
    with open(os.path.join(STOCK % "5.0.9", "logback.xml.stock"), encoding="utf-8") as f:
        live = f.read()
    if live_audit == "class":
        live = live.replace('  <!-- <appender name="AUDIT" class="ch.qos.logback.core.rolling.RollingFileAppender">',
                            "  <appender class='ch.qos.logback.core.rolling.RollingFileAppender' name='AUDIT'>")
        assert "name='AUDIT'" in live
    elif live_audit == "spaces":
        live = live.replace('  <!-- <appender name="AUDIT"', '  <appender name = "AUDIT"')
        assert 'name = "AUDIT"' in live
    elif live_audit:
        live = live.replace('  <!-- <appender name="AUDIT"', '  <appender name="AUDIT"')
    hostvars = {"n1": {"cassandra_log_audit_file": audit_file} if in_inventory else {}}
    ctx = dict(inventory_hostname="n1", hostvars=hostvars, cassandra_log_audit_file=audit_file,
               _cassandra_config_changes=["logback.xml"],
               _cassandra_config_compared={"logback.xml": base64.b64encode(live.encode()).decode()})
    assert render(MOVES["ansible.builtin.set_fact"]["_cassandra_config_audit_moves"], **ctx) is said


@pytest.mark.parametrize("variables, said", [
    # (rc9 r2 audit) values written as they are that would leave logback.xml unreadable, on every node
    ({"cassandra_log_pattern": "%msg -- %n"}, "cassandra_log_pattern: %msg -- %n has a --, which XML does not take in a"
                                              " comment, where the template writes it while cassandra_log_audit_file is"
                                              " false. Way out, one of:"
                                              "\n  write it without --"
                                              "\n  set cassandra_log_audit_file to true: logback.xml then writes the audit"
                                              " events (FileAuditLogger) to audit/audit.log, no longer to system.log"
                                              "\n  add logback.xml to cassandra_config_keep_files: the node keeps its"
                                              " logback.xml as it is"
                                              "\n  write logback.xml from your own template"
                                              " (cassandra_config_template_overrides)"),
    ({"cassandra_log_pattern": "%msg < %n"}, "cassandra_log_pattern: %msg < %n has a <, which ends the XML value there:"
                                             " write it &lt;"),
    ({"cassandra_log_pattern": "%msg & %n"}, "cassandra_log_pattern: %msg & %n has an & that starts no XML entity"
                                             " (&amp; &lt; &gt; &quot; &apos; &#number;): write it &amp;"),
    # (rc9 r3 audit) an entity logback's parser does not know; ]]> in a text
    ({"cassandra_log_pattern": "%msg&nbsp;%n"}, "cassandra_log_pattern: %msg&nbsp;%n has an & that starts no XML"
                                                " entity"),
    ({"cassandra_log_pattern": "%msg ]]> %n"}, "cassandra_log_pattern: %msg ]]> %n has a ]]>, which XML does not take"
                                               " in a text: write it ]]&gt;"),
    ({"cassandra_log_cql_table_level": "W--ARN"}, "cassandra_log_cql_table_level: W--ARN has a --, which XML does not"
                                                  " take in a comment, where the template writes it while"
                                                  " cassandra_log_cql_table is false. Way out, one of:\n  write it"
                                                  " without --\n  set cassandra_log_cql_table to true: logback.xml then"
                                                  " writes the logs to the system_views.system_logs table too\n"),
    ({"cassandra_log_levels": {'com."x': "INFO"}}, 'cassandra_log_levels: com."x has a ", which ends the XML attribute'
                                                   " there: write it &quot;"),
    ({"cassandra_log_level": "IN<FO"}, "cassandra_log_level: IN<FO has a <"),
    ({"cassandra_log_audit_max_file_size": "5--0MB"}, "cassandra_log_audit_max_file_size: 5--0MB has a --"),
])
def test_values_that_would_leave_logback_xml_unreadable_are_refused(variables, said):
    wrong = refused(**variables)
    assert len(wrong) == 1 and wrong[0].startswith(said), wrong
    with pytest.raises(ET.ParseError):
        ET.fromstring(logback("5.0", **variables))


@pytest.mark.parametrize("series", sorted(VERSIONS))
@pytest.mark.parametrize("variables", [
    {"cassandra_log_pattern": "%msg -- %n", "cassandra_log_audit_file": True},  # (the audit appender on: no comment)
    {"cassandra_log_pattern": "%msg &amp; &lt;x&gt; &#38; &quot;&apos;&#x26; %n"},
    {"cassandra_log_level": "${LVL:-INFO}", "cassandra_log_levels": {"com.x": "warn"}},
])
def test_values_logback_xml_takes_pass(series, variables):
    assert refused(**variables) == []
    ET.fromstring(logback(series, **variables))


def test_each_refusal_on_lines_of_its_own():
    msg = render(REFUSE["ansible.builtin.assert"]["fail_msg"], _wrong=["a: one", "b: two\n  way out"])
    assert msg == "a: one\nb: two\n  way out\nNothing is changed."


def test_a_double_dash_is_yours_with_your_own_template():
    # (rc9 r3 audit) the comment it would break is the role's template's: not refused with your own
    assert refused(cassandra_log_pattern="%msg -- %n",
                   cassandra_config_template_overrides={"logback.xml": "/t/logback.xml.j2"}) == []


def test_an_appender_named_with_spaces_around_its_equal_sign_is_known():
    # (rc9 r3 audit) name = "Q"; a filename= after it is not its name
    assert refused('  <appender name = "Q" class="x" filename="f"/>', cassandra_logback_root_extra_refs=["Q"]) == []
    wrong = refused('  <appender name="Q" class="x" filename="f"/>', cassandra_logback_root_extra_refs=["f"])
    assert len(wrong) == 1 and wrong[0].startswith("cassandra_logback_root_extra_refs: f is not an appender"), wrong


def test_an_appender_named_in_single_quotes_is_known():
    # (rc9 r2 audit) name='Q' as name="Q"
    assert refused("  <appender name='Q' class='x'/>", cassandra_logback_root_extra_refs=["Q"]) == []


def test_a_g1_value_left_empty_comments_its_stock_line_out():
    # the owner's practice: tune G1 by commenting a stock line out, never by deleting it
    env, ctx, dummy = _load_role("50x", {"os_family": "Debian"})
    lines = _render(env, "50x", "jvm17-server.options", dict(ctx, cassandra_jvm_g1_new_size_percent="",
                                                             cassandra_jvm17_max_gc_pause_millis="",
                                                             cassandra_jvm_g1_rset_updating_pause_time_percent=7))[1]
    assert "#-XX:+UnlockExperimentalVMOptions" in lines and "#-XX:G1NewSizePercent=50" in lines
    assert "#-XX:MaxGCPauseMillis=300" in lines and "-XX:G1RSetUpdatingPauseTimePercent=7" in lines
    assert "-XX:MaxTenuringThreshold=2" in lines  # (the others as they are)


def test_the_heap_in_the_jvm_options():
    env, ctx, dummy = _load_role("50x", {"os_family": "Debian"})
    on = dict(ctx, cassandra_heap_size="31G", cassandra_heap_in_jvm_options=True, cassandra_jvm_extra_options=["-Dx=1"])
    assert _render(env, "50x", "jvm-server.options", on)[1][-4:-1] == ["-Xms31G", "-Xmx31G", "-Dx=1"]
    assert '#MAX_HEAP_SIZE="20G"' in _render(env, "50x", "cassandra-env.sh", on)[1]
    assert 'MAX_HEAP_SIZE="31G"' in _render(env, "50x", "cassandra-env.sh", dict(on, cassandra_heap_in_jvm_options=False))[1]
    # (audit r4) -e cassandra_heap_in_jvm_options=false gives the string "false": off too
    off = dict(on, cassandra_heap_in_jvm_options="false")
    assert 'MAX_HEAP_SIZE="31G"' in _render(env, "50x", "cassandra-env.sh", off)[1]
    assert "-Xmx31G" not in _render(env, "50x", "jvm-server.options", off)[1]
    # 4.x: its stock heap lines, -Xmn with the young generation
    env, ctx, dummy = _load_role("41x", {"os_family": "Debian"})
    on = dict(ctx, cassandra_heap_size="8G", cassandra_heap_newsize="800M", cassandra_heap_in_jvm_options=True)
    lines = _render(env, "41x", "jvm-server.options", on)[1]
    assert "-Xms8G" in lines and "-Xmx8G" in lines and "-Xmn800M" in lines and "#-Xms4G" not in lines
    assert "#-Xms4G" in _render(env, "41x", "jvm-server.options", dict(ctx, cassandra_heap_size="8G"))[1]
