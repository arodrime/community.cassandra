from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The Prometheus exporter in cassandra_config: the -javaagent option at the end of jvm-server.options (none when
# off: the stock file), its config file (the role's rules, the user's, the httpServer part of the variables), the
# checks of its variables, its jar (a checksum, the mirror's credentials, offline, check mode), apply_config's
# plan (a restart for the httpServer part only), and cassandra_firewall's rule for its sources.

import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import lookup_loader
from ansible.template import Templar
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _defaults, _render as role_render
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()  # cassandra_same_origin (ansible-test installs the collection loader)

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


EXPORTER = load_tasks(os.path.join(ROLE, "tasks", "prometheus_exporter.yml"))
CHECK = find(load_tasks(os.path.join(ROLE, "tasks", "checks.yml")),
             "Refuse Prometheus exporter settings the agent would not start with")


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


# --- the exporter's config ----------------------------------------------------

def config(**variables):
    loader = DataLoader()
    templar = Templar(loader=loader, variables=trust(role_vars(**variables)))
    lookup = lookup_loader.get("ansible.builtin.template", loader=loader, templar=templar) or lookup_loader.get(
        "template", loader=loader, templar=templar)
    text = lookup.run([os.path.join(ROLE, "templates", "jmx_prometheus.yml.j2")], variables=role_vars(**variables))[0]
    return text, yaml.safe_load(text)


def test_the_roles_rules_by_default():
    text, data = config()
    assert text.startswith("# Managed by Ansible")
    with open(os.path.join(ROLE, "files", "jmx_prometheus.yml")) as f:
        assert data == yaml.safe_load(f)
    assert data["lowercaseOutputName"] is True and data["rules"] and "httpServer" not in data


def test_the_users_config_and_the_http_server_of_the_variables():
    text, data = config(cassandra_prometheus_exporter_config="rules:\n  - pattern: '.*'\n",
                        cassandra_prometheus_exporter_tls_keystore="/etc/ssl/exporter.p12",
                        cassandra_prometheus_exporter_tls_keystore_password="k: s#1",
                        cassandra_prometheus_exporter_tls_certificate_alias="node",
                        cassandra_prometheus_exporter_username="prometheus",
                        cassandra_prometheus_exporter_password="p'w")
    assert data == {"rules": [{"pattern": ".*"}], "httpServer": {
        "ssl": {"keyStore": {"filename": "/etc/ssl/exporter.p12", "password": "k: s#1"}, "certificate": {"alias": "node"}},
        "authentication": {"basic": {"username": "prometheus", "password": "p'w"}}}}


# --- the checks -------------------------------------------------------------

def refused(**variables):
    variables = role_vars(**dict({"cassandra_prometheus_exporter_enabled": True}, **variables))
    for name, value in CHECK["vars"].items():
        variables[name] = render(value, variables)
    return variables["_wrong"]


def test_good_settings_pass():
    assert refused() == []
    assert refused(cassandra_prometheus_exporter_address="::", cassandra_prometheus_exporter_port="9500",
                   cassandra_jvm_extra_options=["-Djava.net.preferIPv4Stack=false"],
                   cassandra_prometheus_exporter_config_file="/etc/monitoring/c.yml") == []
    assert refused(cassandra_prometheus_exporter_tls_keystore="/k.p12", cassandra_prometheus_exporter_tls_keystore_password="x",
                   cassandra_prometheus_exporter_tls_certificate_alias="a", cassandra_prometheus_exporter_username="u",
                   cassandra_prometheus_exporter_password="p", cassandra_prometheus_exporter_config="rules: []") == []
    assert true(CHECK["when"], role_vars()) is False  # off: nothing checked


@pytest.mark.parametrize("variables, said", [
    ({"cassandra_prometheus_exporter_version": 1.7}, "cassandra_prometheus_exporter_version must be a release number"),
    ({"cassandra_prometheus_exporter_version": "1.7.0; rm"}, "cassandra_prometheus_exporter_version must be a release number"),
    ({"cassandra_prometheus_exporter_port": 0}, "cassandra_prometheus_exporter_port must be a port number"),
    ({"cassandra_prometheus_exporter_port": 70000}, "cassandra_prometheus_exporter_port must be a port number"),
    ({"cassandra_prometheus_exporter_address": "a b"}, "cassandra_prometheus_exporter_address must be"),
    ({"cassandra_prometheus_exporter_dir": "opt/x"}, "cassandra_prometheus_exporter_dir must be an absolute path"),
    ({"cassandra_prometheus_exporter_dir": "/opt/my dir"}, "cassandra_prometheus_exporter_dir must be an absolute path"),
    ({"cassandra_prometheus_exporter_config_file": "/etc/a=b.yml"}, "cassandra_prometheus_exporter_config_file must be"),
    ({"cassandra_prometheus_exporter_config_file": "/etc/c.yml", "cassandra_prometheus_exporter_config": "rules: []"},
     "names a config managed by other means"),
    ({"cassandra_prometheus_exporter_tls_keystore": "/k.p12"}, "HTTPS needs"),
    ({"cassandra_prometheus_exporter_username": "u"}, "basic authentication needs"),
    ({"cassandra_prometheus_exporter_config": "- a list"}, "must be the YAML of a mapping"),
    # its own message, never the assert's error: line and column only (the text may hold a password)
    ({"cassandra_prometheus_exporter_config": "rules: [\n  password: s3cret"},
     "cassandra_prometheus_exporter_config is not valid YAML (line 2"),
    # the stock jvm-server.options prefers IPv4: the JVM could not listen on an IPv6 address
    ({"cassandra_prometheus_exporter_address": "fd00::1"}, "-Djava.net.preferIPv4Stack=true, the JVM could not listen"),
    ({"cassandra_prometheus_exporter_port": 9042}, "is also cassandra_native_transport_port"),
    ({"cassandra_prometheus_exporter_port": 7199}, "is also cassandra_jmx_port"),
    ({"cassandra_prometheus_exporter_port": 7000}, "is also cassandra_storage_port"),
    ({"cassandra_extra_settings": {"native_transport_port_ssl": 9404}},
     "9404 is also native_transport_port_ssl (cassandra_extra_settings)"),
    ({"cassandra_config_keep_files": ["jvm-server.options"]}, "jvm-server.options is in cassandra_config_keep_files"),
    ({"cassandra_jvm_extra_options": ["-javaagent:/opt/x/jmx_prometheus_javaagent-1.6.0.jar=9500:/etc/x.yml"]},
     "the JVM would load two"),
    ({"cassandra_env_extra_lines": ['JVM_OPTS="$JVM_OPTS -javaagent:/x/jmx_prometheus_javaagent-1.6.0.jar=1:/x.yml"']},
     "the JVM would load two"),
    ({"cassandra_prometheus_exporter_config": "httpServer: {}", "cassandra_prometheus_exporter_username": "u",
      "cassandra_prometheus_exporter_password": "p"}, "has its own httpServer"),
])
def test_settings_the_agent_would_not_start_with_are_refused(variables, said):
    wrong = refused(**variables)
    assert [w for w in wrong if said in w], wrong
    assert "s3cret" not in str(wrong)


# --- the jar ----------------------------------------------------------------

SHA = "0ad843261c567d1924a017d542e14c40f2afff523d940c25ee000742fde4b3a9"


def jar_vars(exists=False, sha=SHA, checksum="sha256:" + SHA, dir_there=True, offline=False, url=None, **more):
    """The variables of the jar's tasks: the jar there or not (its sha256), the checksum wanted."""
    variables = role_vars(cassandra_prometheus_exporter_enabled=True, cassandra_prometheus_exporter_checksum=checksum,
                          cassandra_offline=offline, **more)
    if url:
        variables["cassandra_prometheus_exporter_url"] = url
    variables["cassandra_config_exporter_stat"] = {"results": [{"stat": {"exists": exists, "checksum": sha}},
                                                               {"stat": {"exists": True}}, {"stat": {"exists": True}}]}
    state = find(EXPORTER, "Tell whether the jar there is the one wanted")
    task_vars = dict(variables, **dict((k, render(v, variables)) for k, v in state["vars"].items()))
    variables["_cassandra_config_exporter_jar_state"] = render(
        state["ansible.builtin.set_fact"]["_cassandra_config_exporter_jar_state"], task_vars)
    variables["cassandra_config_exporter_dir"] = {"stat": {"exists": dir_there, "isdir": dir_there}}
    return variables


def when(task, variables):
    conditions = task.get("when", [])
    return all(true(c, variables) for c in (conditions if isinstance(conditions, list) else [conditions]))


@pytest.mark.parametrize("variables, state", [
    ({}, "missing"),
    ({"exists": True}, "ok"),
    ({"exists": True, "sha": "0" * 64}, "other"),  # not the jar the checksum names
    ({"exists": True, "sha": "0" * 64, "checksum": ""}, "ok"),  # nothing to check it with: kept
    ({"exists": True, "checksum": "sha512:" + "a" * 128}, "unchecked"),  # get_url checks it
])
def test_the_jar_there_is_checked_by_its_sha256(variables, state):
    assert jar_vars(**variables)["_cassandra_config_exporter_jar_state"] == state


def test_the_jar_is_downloaded_with_its_checksum_and_the_mirrors_credentials():
    task = find(EXPORTER, "Download the exporter's jar")
    get = [t for t in task["block"] if "ansible.builtin.get_url" in t][0]
    args = get["ansible.builtin.get_url"]
    assert args["checksum"] == "{{ cassandra_prometheus_exporter_checksum }}"
    # a new jar 0644 whatever the umask (Cassandra's account reads it); no owner, and a jar there keeps its own
    # mode (site.yml after an import changes nothing of it)
    assert not set(args) & {"owner", "group"}
    for state, mode in (("missing", "0644"), ("other", "__omit"), ("unchecked", "__omit")):
        assert render(args["mode"], dict(jar_vars(), _cassandra_config_exporter_jar_state=state, omit="__omit")) == mode
    assert "did not match" in get["until"]  # a wrong checksum is final
    mirror = {"cassandra_install_url": "https://mirror.example.net/cassandra/", "cassandra_install_username": "u",
              "cassandra_install_password": "pw"}
    on_mirror = jar_vars(url="https://mirror.example.net/jars/jmx.jar", **mirror)
    on_mirror.update((k, render(v, on_mirror)) for k, v in get["vars"].items())
    assert render(args["url_username"], on_mirror) == "u" and render(args["url_password"], on_mirror) == "pw"
    elsewhere = jar_vars(url="https://other.example.org/jmx.jar", **mirror)
    elsewhere.update((k, render(v, elsewhere)) for k, v in get["vars"].items())
    assert true("_on_mirror | bool", elsewhere) is False  # never sent to another server
    # a jar the role downloaded is listed, for its removal later; one that cannot be: a clean stop
    assert task["block"][1]["ansible.builtin.lineinfile"]["line"] == "{{ _cassandra_config_exporter_jar | basename }}"
    stop = task["rescue"][0]["ansible.builtin.fail"]["msg"]
    assert "could not be downloaded" in stop and "run again" in stop
    # (an upgrade gets it before its drain: Cassandra still runs; site.yml before the start: not started)
    assert "neither stops nor starts Cassandra there: it is left as it is" in stop and "keeps running" not in stop


@pytest.mark.parametrize("variables, downloaded", [
    ({}, True),  # not there
    ({"exists": True}, False),  # there, the one wanted: left as it is
    ({"exists": True, "sha": "0" * 64}, True),  # another jar under its name
    ({"exists": True, "checksum": ""}, False),  # there, nothing to check it with: kept (no download each run)
    ({"dir_there": False}, False),  # check mode: its directory not created, nothing to compare with
])
def test_when_the_jar_is_downloaded(variables, downloaded):
    assert when(find(EXPORTER, "Download the exporter's jar"), jar_vars(**variables)) is downloaded


@pytest.mark.parametrize("variables, stops", [
    ({"offline": True}, True),
    ({"offline": True, "exists": True}, False),
    ({"offline": True, "exists": True, "sha": "0" * 64}, True),  # the wrong jar: no network tried
    ({"offline": True, "url": "file:///srv/jmx_prometheus_javaagent-1.7.0.jar"}, False),  # a copy on the node
    ({"offline": False}, False),
])
def test_offline_a_jar_not_the_one_wanted_stops_the_run(variables, stops):
    task = find(EXPORTER, "Stop when the jar is not the one wanted and nothing is downloaded")
    assert when(task, jar_vars(**variables)) is stops
    assert "Nothing is changed" in task["ansible.builtin.fail"]["msg"]


@pytest.mark.parametrize("version, url", [
    ("1.7.0", "https://github.com/prometheus/jmx_exporter/releases/download/1.7.0/jmx_prometheus_javaagent-1.7.0.jar"),
    ("1.1.0", "https://github.com/prometheus/jmx_exporter/releases/download/1.1.0/jmx_prometheus_javaagent-1.1.0.jar"),
    ("1.0.1", "https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/1.0.1/jmx_prometheus_javaagent-1.0.1.jar"),
    ("0.20.0", "https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/0.20.0/jmx_prometheus_javaagent-0.20.0.jar"),
])
def test_the_url_of_each_release(version, url):
    assert render("{{ cassandra_prometheus_exporter_url }}", role_vars(cassandra_prometheus_exporter_version=version)) == url


def test_the_jar_and_config_come_before_the_jvm_options_file():
    main = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    names = [t.get("name") for t in walk(main)]
    assert names.index("Confirm config changes") < names.index("The Prometheus exporter's jar and config") \
        < names.index("Template Cassandra config files") < names.index("Remove what the Prometheus exporter's agent no longer needs")
    assert find(main, "The Prometheus exporter's jar and config")["when"] == "cassandra_prometheus_exporter_enabled | bool"
    # (the record of the config Cassandra started with: written for the exporter's changes alone too)
    apply = load_tasks(os.path.join(ROLE, "tasks", "apply.yml"))
    assert "_cassandra_config_exporter_changes" in str(find(apply, "Record the config the running Cassandra started with")["when"])


def test_the_config_is_written_like_the_other_config_files():
    write = find(EXPORTER, "The role's exporter config")
    copy = find(EXPORTER, "Write the exporter's config")
    assert (copy["ansible.builtin.copy"]["owner"], copy["ansible.builtin.copy"]["mode"]) == (
        "{{ _cassandra_config_perm.owner }}", "{{ _cassandra_config_perm.mode }}")
    main = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    assert find(main, "The Prometheus exporter's jar and config")["vars"] == {"_cassandra_config_perm_file": "jmx_prometheus.yml"}
    # never Ansible's own diff (passwords, also in a config of yours with its own httpServer): the masked one
    assert copy["diff"] is False and copy["no_log"] == "{{ _secrets | bool }}"
    assert write["when"] == "cassandra_prometheus_exporter_config_file == ''"  # one managed by other means: not written
    there = find(EXPORTER, "Stop when the exporter config managed by other means is not there")
    assert "ansible_check_mode" not in str(there["when"])  # a missing file stops --check too: nothing to show


def changes(written=None, restart=False, download=None, jar_state="ok", conf_there=True, conf_stat=None, at_start=None,
            config_file=""):
    task = find(EXPORTER, "Note the exporter's changes")
    variables = role_vars(cassandra_prometheus_exporter_enabled=True, cassandra_prometheus_exporter_config_file=config_file)
    variables["_cassandra_config_perm"] = {"owner": "root", "group": "cassandra", "mode": "0640"}
    stat = dict({"exists": conf_there, "pw_name": "root", "gr_name": "cassandra", "mode": "0640"}, **(conf_stat or {}))
    variables["cassandra_config_exporter_stat"] = {"results": [{"stat": {"exists": jar_state != "missing"}}, {"stat": stat},
                                                               {"stat": {"exists": at_start is not None}}]}
    variables["_cassandra_config_exporter_jar_state"] = jar_state
    if download is not None:
        variables["cassandra_config_exporter_download"] = download
    variables["cassandra_config_exporter_written"] = {"skipped": True} if written is None else {"changed": written}
    variables["cassandra_config_exporter_at_start_written"] = {"changed": bool(at_start)}
    if written:
        variables["_cassandra_config_exporter_compared"] = {"same": False, "restart": restart,
                                                            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-rules: []\n+rules: [x]"}
    for name, value in task["vars"].items():
        variables[name] = render(value, variables)
    return render(task["ansible.builtin.set_fact"]["_cassandra_config_exporter_changes"], variables)


def test_what_changes_is_noted_for_the_report_and_apply_config():
    assert changes() == []
    assert changes(written=True) == [{"item": "/etc/cassandra/jmx_prometheus.yml", "kind": "config", "restart": False,
                                      "before": "current", "after": "2 line(s) changed",
                                      "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-rules: []\n+rules: [x]"}]
    assert changes(written=True, restart=True)[0]["restart"] is True
    assert changes(written=True, conf_there=False)[0]["before"] == "absent"
    assert changes(download={"changed": True}, jar_state="missing") == [
        {"item": "jmx_prometheus_javaagent-1.7.0.jar", "kind": "jar", "restart": False, "before": "absent",
         "after": "downloaded"}]
    # a jar there: "downloaded" only when it was
    assert changes(download={"changed": False}, jar_state="other") == []
    assert changes(download={"skipped": True}, jar_state="ok") == []
    # check mode, its directory not created yet: said as a download
    assert changes(download={"skipped": True}, jar_state="missing")[0]["after"] == "downloaded"
    # an owner or a mode set right, its content kept: in the report
    assert changes(conf_stat={"mode": "0644"}) == [{"item": "/etc/cassandra/jmx_prometheus.yml (owner:group mode)",
                                                    "kind": "perms", "restart": False, "before": "root:cassandra 0644",
                                                    "after": "root:cassandra 0640"}]
    # a config of yours whose parts read at start changed: a restart
    out = changes(config_file="/etc/monitoring/c.yml", at_start="other")
    assert out[0]["kind"] == "at_start" and out[0]["restart"] is True


# --- the cleanup ------------------------------------------------------------

CLEANUP = load_tasks(os.path.join(ROLE, "tasks", "prometheus_exporter_cleanup.yml"))


def unused(tmp_path, on, listed=("jmx_prometheus_javaagent-1.6.0.jar", "jmx_prometheus_javaagent-1.7.0.jar"),
           others=("theirs.jar",), config_header=True, kept="", options=None):
    """What the cleanup finds in a directory and a conf dir made so (sh runs its script); kept: the JVM's files
    kept by hand (cassandra_config_keep_files), options: the content of the conf dir's jvm-server.options."""
    jars, conf = tmp_path / "exporter", tmp_path / "conf"
    jars.mkdir(parents=True)
    conf.mkdir()
    if options is not None:
        (conf / "jvm-server.options").write_text(options)
    for name in list(listed) + list(others):
        (jars / name).write_text("x")
    (jars / ".cassandra_config").write_text("".join(n + "\n" for n in listed))
    (conf / "jmx_prometheus.yml").write_text(("# Managed by Ansible (community.cassandra.cassandra_config): x\n"
                                              if config_header else "# mine\n") + "rules: []\n")
    (conf / "jmx_prometheus.at_start").write_text("x\n")
    argv = find(CLEANUP, "Find what the exporter's agent no longer needs")["ansible.builtin.command"]["argv"]
    run = subprocess.run(argv[:4] + [str(jars), "jmx_prometheus_javaagent-1.7.0.jar", str(conf), on, kept],
                         stdout=subprocess.PIPE, check=True)
    return [line.replace(str(tmp_path), "") for line in run.stdout.decode().split("\n") if line]


def test_on_the_old_jars_the_role_downloaded_are_removed(tmp_path):
    assert unused(tmp_path, "on") == ["jar|/exporter/jmx_prometheus_javaagent-1.6.0.jar"]  # not theirs, not the one used


def test_off_everything_the_role_put_there(tmp_path):
    assert unused(tmp_path, "off") == ["jar|/exporter/jmx_prometheus_javaagent-1.6.0.jar",
                                       "jar|/exporter/jmx_prometheus_javaagent-1.7.0.jar", "list|/exporter/.cassandra_config",
                                       "config|/conf/jmx_prometheus.yml", "record|/conf/jmx_prometheus.at_start"]


def test_off_a_config_not_the_roles_stays(tmp_path):
    assert "config|/conf/jmx_prometheus.yml" not in unused(tmp_path, "off", config_header=False)


AGENT_LINE = "-javaagent:/opt/prometheus-jmx-exporter/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/cassandra/jmx_prometheus.yml\n"


def test_off_nothing_removed_while_a_file_kept_by_hand_still_loads_the_agent(tmp_path):
    # removed, the jar and config the kept jvm-server.options names would stop Cassandra at its next start
    assert unused(tmp_path / "a", "off", kept="jvm-server.options", options="-ea\n" + AGENT_LINE) == ["kept|jvm-server.options"]
    # kept by hand without the agent (removed from it), or not kept (the role wrote it without the agent): removed
    assert len(unused(tmp_path / "b", "off", kept="jvm-server.options", options="-ea\n#" + AGENT_LINE)) == 5
    assert len(unused(tmp_path / "c", "off", options="-ea\n" + AGENT_LINE)) == 5
    argv = find(CLEANUP, "Find what the exporter's agent no longer needs")["ansible.builtin.command"]["argv"]
    assert render(argv[-1], role_vars(cassandra_config_keep_files=["cassandra.yaml", "jvm-server.options",
                                                                   "jvm17-server.options", "cassandra-env.sh"])) \
        == "jvm-server.options jvm17-server.options cassandra-env.sh"
    # nothing of the role's there (an exporter set up by hand): nothing said
    bare = tmp_path / "d"
    bare.mkdir()
    (bare / "jvm-server.options").write_text(AGENT_LINE)
    run = subprocess.run(argv[:4] + [str(bare / "none"), "x.jar", str(bare), "off", "jvm-server.options"],
                         stdout=subprocess.PIPE, check=True)
    assert run.stdout == b""
    # said, with the way out; nothing removed, nothing noted
    variables = role_vars(inventory_hostname="n1", cassandra_config_exporter_unused={"stdout_lines": ["kept|jvm-server.options"]})
    said = find(CLEANUP, "Say what the exporter's agent in a file kept by hand still needs is left in place")
    variables.update((k, render(v, variables)) for k, v in said["vars"].items() if k.startswith("_"))
    assert when(said, variables) is True
    msg = render(said["ansible.builtin.debug"]["msg"], variables)
    assert "jvm-server.options (kept by hand" in msg and "Remove the agent's line from your copy" in msg
    assert render(find(CLEANUP, "Remove them")["loop"], variables) == []
    note = find(CLEANUP, "Note what is removed")
    for name, value in note["vars"].items():
        variables[name] = render(value, variables)
    assert variables["_removed"] == []


def test_the_removed_files_are_noted_without_a_restart():
    task = find(CLEANUP, "Note what is removed")
    variables = role_vars(cassandra_prometheus_exporter_enabled=True,
                          _cassandra_config_exporter_changes=[{"item": "x", "kind": "jar", "restart": False}],
                          cassandra_config_exporter_unused={"stdout_lines": ["jar|/opt/e/jmx_prometheus_javaagent-1.6.0.jar"]})
    for name, value in task["vars"].items():
        variables[name] = render(value, variables)
    assert render(task["ansible.builtin.set_fact"]["_cassandra_config_exporter_changes"], variables) == [
        {"item": "x", "kind": "jar", "restart": False},
        {"item": "jmx_prometheus_javaagent-1.6.0.jar", "kind": "removed", "restart": False, "before": "present",
         "after": "removed"}]
    main = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    assert "when" not in find(main, "Remove what the Prometheus exporter's agent no longer needs")  # off too


def test_the_exporter_record_is_in_the_record_of_the_config_cassandra_started_with():
    variables = load("roles", "cassandra_config", "vars", "main.yml")
    assert "jmx_prometheus.at_start" in variables["_cassandra_config_snapshot_files"]
    assert "jmx_prometheus.at_start" in load("roles", "cassandra_operation", "defaults", "main.yml")["_cassandra_service_config_files"]


def pending(files, record, ctime=50.0, fact="cassandra_config_newer_files"):
    """config_pending's verdict on the files now ([path, checksum], changed at ctime) and the record of the start
    ({path: checksum}; None: no record, e.g. a node just imported)."""
    task = find(load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "config_pending.yml")),
                "Note whether a restart is pending")
    import base64
    import json
    snapshot = json.dumps(dict(record or {}, _jvm={"pid": "1", "start": 100.0}))
    variables = {"cassandra_pending_snapshot": {"content": base64.b64encode(snapshot.encode()).decode()}
                 if record is not None else {"failed": True},
                 "cassandra_jvm": {"pid": "1", "start": 100.0},
                 "cassandra_pending_files": {"files": [{"path": p, "checksum": c, "ctime": ctime} for p, c in files]},
                 "cassandra_pending_unit_file": {"stat": {"exists": False}}}
    for name, value in task["vars"].items():
        variables[name] = render(value, variables)
    return render(task["ansible.builtin.set_fact"][fact], variables)


def test_a_change_of_the_parts_read_at_start_is_a_restart_pending():
    rec = "/etc/cassandra/jmx_prometheus.at_start"
    assert pending([(rec, "b")], {rec: "a"}) == ["jmx_prometheus.at_start"]
    assert pending([(rec, "a")], {rec: "a"}) == []
    # not in the record of that start (the exporter set up since): what its agent read is not known, nothing pending
    assert pending([(rec, "b")], {}) == []
    assert pending([("/etc/cassandra/cassandra.yaml", "b")], {}) == ["cassandra.yaml"]  # the other files as before


def test_after_an_import_the_exporter_record_changes_nothing():
    rec = "/etc/cassandra/jmx_prometheus.at_start"
    # the first site.yml or apply_config writes it, after the start, with no record of that start: not a file
    # written after Cassandra started (apply_config would say so at each run), nothing pending
    assert pending([(rec, "b")], None, ctime=150.0, fact="cassandra_config_unknown_files") == []
    assert pending([("/etc/cassandra/cassandra.yaml", "b")], None, ctime=150.0, fact="cassandra_config_unknown_files") \
        == ["cassandra.yaml"]
    assert pending([(rec, "b")], {}, ctime=150.0) == []
    # and its first write is no change (site.yml after an import: a no-op); written again, it is one
    write = find(EXPORTER, "Keep the record of its parts read at start")
    for there, changed in ((False, False), (True, True)):
        variables = {"cassandra_config_exporter_stat": {"results": [{}, {}, {"stat": {"exists": there}}]},
                     "cassandra_config_exporter_at_start_written": {"changed": True}}
        assert true(write["changed_when"], variables) is changed
    assert true(write["changed_when"], {"cassandra_config_exporter_stat": {"results": [{}, {}, {"stat": {"exists": True}}]},
                                        "cassandra_config_exporter_at_start_written": {"changed": False}}) is False


def test_its_config_is_not_readable_by_others():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import cassandra_file_permissions
    settings = {"owner": "root", "group": "cassandra", "mode": "0640", "public_mode": "0644", "files": {},
                "user": "cassandra", "user_group": "cassandra"}
    assert cassandra_file_permissions("jmx_prometheus.yml", settings) == {"owner": "root", "group": "cassandra",
                                                                          "mode": "0640"}
    # set apart like the other config files
    settings["files"] = {"jmx_prometheus.yml": {"mode": "0600"}}
    assert cassandra_file_permissions("jmx_prometheus.yml", settings)["mode"] == "0600"
