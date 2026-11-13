from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The Prometheus exporter's Java agent: found in a JVM options file, a JVM_OPTS line of cassandra-env.sh or a block
# of it that only adds it (if ... fi), imported into the cassandra_prometheus_exporter_* variables, the block taken
# out; the role then writes the agent at the end of jvm-server.options and the JVM gets the same arguments. The
# exporter's config compared (a restart for its httpServer part only), and health_check's finding.

import pytest
import yaml

from ansible.utils.unsafe_proxy import AnsibleUnsafeText

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import cassandra_health_report
from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_config_change
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_prometheus import (
    agent, block_as_line, config_change, without_agent)

from .test_cassandra_import_check import check, import_node, seeded, stock
from .test_cassandra_jvm_args import imported, launch, role_files, settings

JAR = "/etc/cassandra-monitoring/lib/jmx_prometheus_javaagent-1.7.0.jar"
CONF = "/etc/cassandra-monitoring/conf/jmx/oss.yml"
OPTION = "-javaagent:%s=9404:%s" % (JAR, CONF)
# the shape of a block written by hand: the agent only when its jar and config are readable and its port free
OWNERS_BLOCK = (
    "if [ -r %s ] && [ -r %s ] && ! { cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; true; } | awk '$4 == \"0A\" &&"
    " substr($2, length($2) - 4) == \":24BC\" { f = 1 } END { exit !f }'; then JVM_OPTS=\"$JVM_OPTS %s\"; else echo"
    " \"... NOT loaded ...\" >&2; fi" % (JAR, CONF, OPTION))
# the same with guards that pass where the tests run (bash runs the files)
PASSING_BLOCK = ('if [ -n "$CASSANDRA_CONF" ]; then\n  JVM_OPTS="$JVM_OPTS %s"\nelse\n  echo "exporter NOT loaded" >&2\nfi'
                 % OPTION)
VARS = {"cassandra_prometheus_exporter_enabled": True, "cassandra_prometheus_exporter_dir": "/etc/cassandra-monitoring/lib",
        "cassandra_prometheus_exporter_config_file": CONF}


# --- the agent's option ------------------------------------------------------

@pytest.mark.parametrize("text, wanted", [
    (OPTION, {"jar": JAR, "version": "1.7.0", "dir": "/etc/cassandra-monitoring/lib", "address": "", "port": 9404,
              "config": CONF}),
    ('JVM_OPTS="$JVM_OPTS -javaagent:/opt/e/jmx_prometheus_javaagent-1.6.0.jar=127.0.0.1:9500:/etc/e.yml"',
     {"jar": "/opt/e/jmx_prometheus_javaagent-1.6.0.jar", "version": "1.6.0", "dir": "/opt/e", "address": "127.0.0.1",
      "port": 9500, "config": "/etc/e.yml"}),
    ("-javaagent:/opt/e/jmx_prometheus_javaagent-1.6.0.jar=[::1]:9500:/etc/e.yml",
     {"jar": "/opt/e/jmx_prometheus_javaagent-1.6.0.jar", "version": "1.6.0", "dir": "/opt/e", "address": "::1",
      "port": 9500, "config": "/etc/e.yml"}),
])
def test_the_agent_is_read_from_its_option(text, wanted):
    found = agent(text)
    assert dict((k, found[k]) for k in wanted) == wanted


@pytest.mark.parametrize("text", [
    "-javaagent:/opt/e/other_agent.jar=9404:/etc/e.yml",  # another agent
    "-javaagent:/opt/e/jmx_prometheus_javaagent-1.7.0.jar",  # no port nor config
    "-javaagent:relative/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/e.yml",  # not a path the role writes
])
def test_other_agents_are_not_the_exporter(text):
    assert agent(text) is None


def test_a_line_of_a_jvm_options_file():
    text, found = without_agent("jvm-server.options", "-ea\n%s\n-Xss256k\n" % OPTION)
    assert text == "-ea\n-Xss256k\n"
    assert (found["kind"], found["line"], found["lines"]) == ("line", 2, 1)
    # commented out: no agent; with other options on its line: left as it is
    assert without_agent("jvm-server.options", "#%s\n" % OPTION) == ("#%s\n" % OPTION, None)
    text, found = without_agent("jvm-server.options", "%s -Dx=1\n" % OPTION)
    assert text == "%s -Dx=1\n" % OPTION and found["kind"] == "kept"


@pytest.mark.parametrize("env, kind, lines", [
    ('A=1\nJVM_OPTS="$JVM_OPTS %s"\nB=2\n' % OPTION, "line", 1),
    ("A=1\n%s\nB=2\n" % OWNERS_BLOCK, "block", 1),
    ("A=1\n%s\nB=2\n" % PASSING_BLOCK, "block", 5),
])
def test_cassandra_env_sh_line_or_block(env, kind, lines):
    text, found = without_agent("cassandra-env.sh", env)
    assert text == "A=1\nB=2\n"
    assert (found["kind"], found["line"], found["lines"], found["option"]) == (kind, 2, lines, OPTION)


@pytest.mark.parametrize("env, why", [
    ('if true; then\n  X=1\n  JVM_OPTS="$JVM_OPTS %s"\nfi\n' % OPTION, "its block in cassandra-env.sh sets more than the agent"),
    ('JVM_OPTS="$JVM_OPTS %s -Dx=1"\n' % OPTION, "its line in cassandra-env.sh sets more than the agent"),
    ('if true; then\n  JVM_OPTS="$JVM_OPTS %s"\n' % OPTION, "its block in cassandra-env.sh is not closed"),
])
def test_a_block_doing_more_is_left_as_it_is(env, why):
    text, found = without_agent("cassandra-env.sh", env)
    assert text == env and (found["kind"], found["why"]) == ("kept", why)


def test_the_block_is_read_as_its_line_when_the_jvm_has_the_agent():
    env = "A=1\n%s\nB=2\n" % PASSING_BLOCK
    assert block_as_line(env, "/usr/bin/java\0%s\0org.apache.cassandra.service.CassandraDaemon" % OPTION) == (
        'A=1\nJVM_OPTS="$JVM_OPTS %s"\nB=2\n' % OPTION)
    assert block_as_line(env, "/usr/bin/java\0org.apache.cassandra.service.CassandraDaemon") == env  # guards failed


# --- the exporter's config ----------------------------------------------------

def test_a_change_of_the_rules_alone_is_reloaded_one_of_the_http_server_restarts():
    rules = "lowercaseOutputName: true\nrules:\n  - pattern: '.*'\n"
    http = "httpServer:\n  authentication:\n    basic: {username: prom, password: x}\n"
    assert config_change(rules, rules.replace("true", "true  # names")) == {"same": True, "restart": False}
    assert config_change(rules, rules.replace("'.*'", "'org.*'")) == {"same": False, "restart": False}
    assert config_change(rules, rules + http) == {"same": False, "restart": True}
    assert config_change(rules + http, rules + http.replace("x}", "y}")) == {"same": False, "restart": True}
    assert config_change(None, rules) == {"same": False, "restart": False}
    assert config_change("", rules + http) == {"same": False, "restart": True}
    assert config_change("{not yaml", rules) == {"same": False, "restart": True}  # what the agent read: not known
    assert cassandra_prometheus_config_change(rules, rules) == {"same": True, "restart": False, "diff": ""}


def test_open_telemetry_is_read_at_start_too():
    assert config_change("rules: []\n", "rules: []\nopenTelemetry: {endpoint: x}\n") == {"same": False, "restart": True}


def test_the_diff_of_the_rules_masks_the_passwords():
    live = "rules: []\nhttpServer:\n  authentication:\n    basic: {username: prom, password: old1}\n"
    new = "rules: [x]\nhttpServer:\n  authentication:\n    basic: {username: prom, password: new2}\n"
    diff = cassandra_prometheus_config_change(live, new, "/etc/cassandra/jmx_prometheus.yml")["diff"]
    assert diff.startswith("--- /etc/cassandra/jmx_prometheus.yml (live)") and "+rules: [x]" in diff
    assert "old1" not in diff and "new2" not in diff
    assert cassandra_prometheus_config_change(live, live)["diff"] == ""


def test_the_diff_masks_the_open_telemetry_headers():
    live = "rules: []\nopenTelemetry:\n  endpoint: http://otel:4317\n  headers:\n    Authorization: Bearer old1\n"
    new = ("rules: [x]\nopenTelemetry:\n  endpoint: http://otel:4317\n  headers:\n    Authorization: Bearer new2\n"
           "    X-Scope: {token: new3}\n")
    diff = cassandra_prometheus_config_change(live, new, "/etc/cassandra/jmx_prometheus.yml")["diff"]
    assert "+rules: [x]" in diff and "endpoint: http://otel:4317" in diff
    assert "old1" not in diff and "new2" not in diff and "new3" not in diff
    flow = cassandra_prometheus_config_change("rules: []\n", "openTelemetry: {headers: {Authorization: Bearer f1}}\n")["diff"]
    assert "f1" not in flow
    # a name inside a longer one is no secret (cassandra.yaml's num_tokens, the JMX AuthorizationProxy)
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
    for text in ("num_tokens: 16", "initial_token: 5", "-Dcassandra.jmx.authorizer=org.x.AuthorizationProxy"):
        assert out.mask(text) == text and not out.secret("cassandra_extra_settings", text)
    assert out.mask("token: t1") == "token: ****"


def test_the_config_with_open_telemetry_is_written_with_no_log():
    import os
    from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
    tasks = load_tasks(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "tasks",
                                    "prometheus_exporter.yml"))
    block = [t for t in tasks if t.get("name") == "The role's exporter config"][0]
    assert "openTelemetry is defined" in block["vars"]["_secrets"] and "httpServer is defined" in block["vars"]["_secrets"]


def test_the_record_of_the_parts_read_at_start():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_at_start
    a = cassandra_prometheus_at_start("rules: []\nhttpServer: {x: 1}\n")
    assert a.startswith("# Managed by Ansible") and "x: 1" not in a  # a digest, no secret
    assert cassandra_prometheus_at_start("rules: [y]\nhttpServer: {x: 1}\n") == a  # the rules: not in it
    assert cassandra_prometheus_at_start("rules: []\nhttpServer: {x: 2}\n") != a


# --- import_cluster ---------------------------------------------------------

def before_extra(files, lines):
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace(
        'JVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"', lines + '\nJVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"')
    return files


def at_the_end(files):
    return dict(files, **{"jvm-server.options": files["jvm-server.options"].rstrip("\n") + "\n" + OPTION + "\n"})


FORMS = [  # (id, the edit, where it was)
    ("a line of jvm-server.options", at_the_end, "jvm-server.options"),
    ("a JVM_OPTS line of cassandra-env.sh", lambda f: before_extra(dict(f), 'JVM_OPTS="$JVM_OPTS %s"' % OPTION),
     "cassandra-env.sh"),
    ("a block of cassandra-env.sh", lambda f: before_extra(dict(f), PASSING_BLOCK), "cassandra-env.sh"),
]


@pytest.mark.parametrize("case, edit, where", FORMS, ids=[c[0] for c in FORMS])
def test_the_agent_is_imported_into_the_variables(tmp_path, case, edit, where):
    files = edit(seeded(stock("50x")))
    cmdline = launch(files, "17", tmp_path)
    assert OPTION in cmdline.split("\0")  # bash gives it the JVM
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert dict((k, variables.get(k)) for k in VARS) == VARS
    for name in ("cassandra_prometheus_exporter_version", "cassandra_prometheus_exporter_port",
                 "cassandra_prometheus_exporter_checksum", "cassandra_prometheus_exporter_address",
                 "cassandra_jvm_extra_options", "cassandra_jvm17_extra_options", "cassandra_config_template_snippets"):
        assert name not in variables, name  # the role's (1.7.0, 9404, its checksum, every address), nothing else
    assert node["hand_edits"] == []
    assert [m for m in node["moved"] if m.startswith(where) and "-> cassandra_prometheus_exporter_*" in m]
    # the role writes the agent at the end of jvm-server.options, none in cassandra-env.sh: the JVM gets the same
    assert rendered["jvm-server.options"].rstrip("\n").endswith("\n" + OPTION)
    assert OPTION not in rendered["cassandra-env.sh"]
    after = launch(rendered, "17", tmp_path)
    assert settings(after) == settings(cmdline)
    # the self-check: nothing else changes; the files rewritten in the role's layout said so
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == []
    if where == "cassandra-env.sh":
        assert node["rewritten"] == ["cassandra-env.sh", "jvm-server.options"]
        assert [n for n in out["notes"] if "the JVM gets the same arguments" in n]


def test_the_block_moved_is_said_with_what_to_drop(tmp_path):
    files = before_extra(seeded(stock("50x")), PASSING_BLOCK)
    inventory, layout, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert any("(an if ... fi block) -> cassandra_prometheus_exporter_* (the role writes it in jvm-server.options; drop"
               " it from your own copies of cassandra-env.sh: its guards are not kept" in m for m in node["moved"])


def test_the_owners_block_whose_guards_failed(tmp_path):
    # its jar and config not there where the tests run: the JVM was started without the agent
    files = before_extra(seeded(stock("50x")), OWNERS_BLOCK)
    cmdline = launch(files, "17", tmp_path)
    assert OPTION not in cmdline.split("\0")
    imported_vars = import_node(files, "50x", jvm={"cmdline": cmdline, "java": "17", "environment": {}, "newer": []})
    variables = imported_vars[1]["nodes"][0]["vars"]
    assert dict((k, variables.get(k)) for k in VARS) == VARS
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
    out = cassandra_config_import(files, "50x", {"os_family": "Debian"}, jvm={"cmdline": cmdline, "java": "17"})
    assert [n for n in out["jvm_notes"] if "the running JVM does not have the Prometheus exporter's agent" in n]
    assert [m for m in out["moved"] if "its guards are not kept" in m and "checks its port before each restart" in m]
    # the role would load the agent the JVM does not have now: the self-check says it
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] != []


def exporter_import(files, cmdline, **read):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
    return cassandra_config_import(files, "50x", {"os_family": "Debian"},
                                   jvm={"cmdline": cmdline, "java": "17", "exporter": read})


def test_the_jar_is_imported_with_its_sha256(tmp_path):
    files = before_extra(seeded(stock("50x")), 'JVM_OPTS="$JVM_OPTS %s"' % OPTION.replace("1.7.0", "1.5.0"))
    out = exporter_import(files, launch(files, "17", tmp_path), sha256="ab" * 32)
    assert out["vars"]["cassandra_prometheus_exporter_version"] == "1.5.0"  # from the jar's name
    assert out["vars"]["cassandra_prometheus_exporter_checksum"] == "sha256:" + "ab" * 32  # never empty
    # not read: said, with the way out
    out = exporter_import(files, launch(files, "17", tmp_path))
    assert "cassandra_prometheus_exporter_checksum" not in out["vars"]
    assert [n for n in out["jvm_notes"] if "its checksum is not imported. Set cassandra_prometheus_exporter_checksum" in n]


def test_the_config_file_path_is_said_a_new_node_has_none(tmp_path):
    files = before_extra(seeded(stock("50x")), PASSING_BLOCK)
    out = exporter_import(files, launch(files, "17", tmp_path), sha256="ab" * 32, config="rules: []\n")
    assert out["vars"]["cassandra_prometheus_exporter_config_file"] == CONF
    assert [n for n in out["jvm_notes"] if "a node added later has none" in n
            and "-e import_cluster_carry_exporter_config=true" in n]


def test_its_config_carried_into_the_inventory(tmp_path):
    files = before_extra(seeded(stock("50x")), PASSING_BLOCK)
    config = ("rules:\n  - pattern: '.*'\nhttpServer:\n  authentication:\n    basic:\n      username: prometheus\n"
              "      password: s3cret\n")
    out = exporter_import(files, launch(files, "17", tmp_path), sha256="ab" * 32, config=config, carry=True)
    assert "cassandra_prometheus_exporter_config_file" not in out["vars"]
    assert yaml.safe_load(out["vars"]["cassandra_prometheus_exporter_config"]) == {"rules": [{"pattern": ".*"}]}
    assert out["vars"]["cassandra_prometheus_exporter_username"] == "prometheus"
    assert out["vars"]["cassandra_prometheus_exporter_password"] == "s3cret"  # (a secret: secrets.yml, by its name)
    assert [m for m in out["moved"] if "carried into cassandra_prometheus_exporter_config" in m]
    # the role writes it in the conf dir: the agent's option changes, jvm-server.options rewritten
    assert "jvm-server.options" in out["rewritten"]
    # what no variable covers: its path imported instead, said
    out = exporter_import(files, "", config="httpServer:\n  threads: {minimum: 2}\n", carry=True)
    assert out["vars"]["cassandra_prometheus_exporter_config_file"] == CONF
    assert [n for n in out["jvm_notes"] if "NOT carried" in n and "httpServer.threads.minimum" in n]


def test_carried_open_telemetry_headers_go_to_secrets_yml(tmp_path):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _split_secrets
    files = before_extra(seeded(stock("50x")), PASSING_BLOCK)
    config = "rules: []\nopenTelemetry:\n  endpoint: http://otel:4317\n  headers:\n    Authorization: Bearer t0ken\n"
    out = exporter_import(files, "", sha256="ab" * 32, config=config, carry=True)
    public, secrets = _split_secrets(out["vars"])
    assert "t0ken" in secrets["cassandra_prometheus_exporter_config"] and "t0ken" not in str(public)
    plain = exporter_import(files, "", sha256="ab" * 32, config="rules: []\n", carry=True)
    assert "cassandra_prometheus_exporter_config" in _split_secrets(plain["vars"])[0]


@pytest.mark.parametrize("unread", [{}, {"config": None}, {"config": ""}])  # '': none before ansible-core 2.19
def test_carry_asked_for_a_config_not_read(tmp_path, unread):
    files = before_extra(seeded(stock("50x")), PASSING_BLOCK)
    out = exporter_import(files, "", sha256="ab" * 32, carry=True, **unread)  # (its config not readable on the node)
    assert out["vars"]["cassandra_prometheus_exporter_config_file"] == CONF
    assert [n for n in out["jvm_notes"] if "NOT carried" in n and "could not be read on the node" in n
            and "import again" in n]
    assert not [n for n in out["jvm_notes"] if "-e import_cluster_carry_exporter_config=true" in n]  # (asked already)


def test_a_reimport_of_the_roles_own_config():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
    own = OPTION.replace(CONF, "/etc/cassandra/jmx_prometheus.yml")
    files = seeded(stock("50x"))
    files["jvm-server.options"] = files["jvm-server.options"].rstrip("\n") + "\n" + own + "\n"

    def imported_with(config):
        return cassandra_config_import(files, "50x", {"os_family": "Debian"},
                                       jvm={"cmdline": "", "java": "17", "exporter": {"sha256": "ab" * 32, "config": config}})
    import os
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "files",
                           "jmx_prometheus.yml")) as f:
        rules = f.read()
    header = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file.\n"
    # the role's rules: its defaults, nothing to set
    out = imported_with(header + rules)
    assert "cassandra_prometheus_exporter_config_file" not in out["vars"] and "cassandra_prometheus_exporter_config" not in out["vars"]
    assert not [n for n in out["jvm_notes"] if "a node added later has none" in n]
    # other rules, an httpServer of the variables: carried back into them
    out = imported_with(header + "rules: [{pattern: x}]\nhttpServer:\n  authentication:\n    basic: {username: u, password: p}\n")
    assert yaml.safe_load(out["vars"]["cassandra_prometheus_exporter_config"]) == {"rules": [{"pattern": "x"}]}
    assert out["vars"]["cassandra_prometheus_exporter_username"] == "u" and "Managed by" not in str(out["vars"])
    # not read: the role writes its own, said ('': none before ansible-core 2.19)
    for unread in (None, "", AnsibleUnsafeText("")):
        out = imported_with(unread)
        assert "cassandra_prometheus_exporter_config_file" not in out["vars"]
        assert "cassandra_prometheus_exporter_config" not in out["vars"]
        assert [n for n in out["jvm_notes"] if "the role writes its own rules there" in n]


def test_a_version_the_role_refuses_is_not_imported(tmp_path):
    files = before_extra(seeded(stock("50x")), 'JVM_OPTS="$JVM_OPTS %s"' % OPTION.replace("1.7.0", "1.0.1-rc"))
    out = exporter_import(files, "", sha256="ab" * 32)
    assert not [k for k in out["vars"] if k.startswith("cassandra_prometheus_exporter_")]
    assert [n for n in out["jvm_notes"] if "NOT imported (its version 1.0.1-rc is not a release number" in n]
    assert not [n for n in out["jvm_notes"] if "from none of the config files" in n]


def test_a_guard_on_one_line_is_said_dropped(tmp_path):
    files = before_extra(seeded(stock("50x")), '[ -r %s ] && JVM_OPTS="$JVM_OPTS %s"' % (JAR, OPTION))
    out = exporter_import(files, "")
    assert [m for m in out["moved"] if "its guards are not kept" in m]
    plain = before_extra(seeded(stock("50x")), 'JVM_OPTS="$JVM_OPTS %s"' % OPTION)
    assert not [m for m in exporter_import(plain, "")["moved"] if "guards" in m]


def test_the_moved_entries_name_their_file():
    """The exporter's agent moved from a JVM options file is said with that file, next to cassandra-env.sh's."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_import_report as rep
    assert "jvm options file" in rep.MOVED[3]
    moved = ["jvm-server.options line 212: -javaagent:x -> cassandra_prometheus_exporter_*",
             "cassandra-env.sh line 3: -Dx=1 -> cassandra_jvm17_extra_options"]
    pairs = rep._by_value([("n1",) + tuple(line.split(" -> ", 1)) for line in moved])
    assert sorted(p[0].split(" line", 1)[0] for p in pairs) == ["cassandra-env.sh", "jvm-server.options"]


def test_the_import_reads_the_jar_and_config_of_the_agent():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_agent_paths
    assert cassandra_prometheus_agent_paths({"cassandra-env.sh": "A=1\n%s\n" % OWNERS_BLOCK}) == {"jar": JAR, "config": CONF}
    assert cassandra_prometheus_agent_paths({"jvm-server.options": "-ea\n"}) == {}


@pytest.mark.parametrize("live", ["jvm17-server.options", "cassandra-env.sh"])
def test_the_agent_read_is_the_one_the_running_java_gets(live):
    # (audit r6) 5.0 on Java 17: an old agent in jvm11-server.options (not read), the live one in jvm17 or
    # cassandra-env.sh: the paths read are the live one's
    from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_agent_paths
    old = "-javaagent:/opt/old/jmx_prometheus_javaagent-0.20.0.jar=9404:/opt/old/old.yml"
    files = {"jvm11-server.options": old + "\n", live: OPTION + "\n" if live.endswith(".options")
             else 'JVM_OPTS="$JVM_OPTS %s"\n' % OPTION}
    assert cassandra_prometheus_agent_paths(files, "50x", "17") == {"jar": JAR, "config": CONF}
    assert cassandra_prometheus_agent_paths(files)["jar"] == "/opt/old/jmx_prometheus_javaagent-0.20.0.jar"  # (any)
    # the import: what was read of another agent's jar and config is not taken
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
    node = seeded(stock("50x"))
    node["jvm17-server.options"] += OPTION + "\n"
    read = {"sha256": "ab" * 32, "config": "rules: []\n", "jar": "/opt/old/jmx_prometheus_javaagent-0.20.0.jar",
            "config_file": "/opt/old/old.yml", "carry": True}
    out = cassandra_config_import(node, "50x", {"os_family": "Debian"}, jvm={"cmdline": "", "java": "17", "exporter": read})
    assert "cassandra_prometheus_exporter_checksum" not in out["vars"] and "cassandra_prometheus_exporter_config" not in out["vars"]
    assert [n for n in out["jvm_notes"] if "jar could not be read" in n]
    out = cassandra_config_import(node, "50x", {"os_family": "Debian"}, jvm={"cmdline": "", "java": "17", "exporter": dict(
        read, jar=JAR, config_file=CONF)})
    assert out["vars"]["cassandra_prometheus_exporter_checksum"] == "sha256:" + "ab" * 32


def test_a_node_without_the_exporter_imports_none():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import
    out = cassandra_config_import(seeded(stock("50x")), "50x", {"os_family": "Debian"})
    assert not [k for k in out["vars"] if k.startswith("cassandra_prometheus_exporter_")]


# --- health_check -----------------------------------------------------------

def test_an_exporter_not_answering_is_only_a_warning():
    finding = {"kind": "warning", "what": "exporter", "host": "n2", "port": 9404,
               "text": "Prometheus exporter not answering on port 9404 of n2"}
    report = cassandra_health_report({"n1": [], "n2": [finding]}, "c", ["n1", "n2"])
    assert report["healthy"]  # rc 0: the cluster's health does not hang on it (health_check says the warning)
