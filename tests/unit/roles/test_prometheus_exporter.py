from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The Prometheus exporter in cassandra_config: the -javaagent option at the end of jvm-server.options (none when
# off: the stock file), its config file (the role's rules, the user's, the httpServer part of the variables), the
# checks of its variables, its jar (a checksum, the mirror's credentials, offline, check mode), apply_config's
# plan (a restart for the httpServer part only), and cassandra_firewall's rule for its sources.

import os
import shutil
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
    for series in ("4.0", "4.1", "5.0"):
        rule = [r for r in gen_templates.RULES[series]["jvm-server.options"] if r[0].startswith("#-Dcassandra.expiration")][0]
        with open(os.path.join(ROLE, "templates", series, "jvm-server.options.j2")) as f:
            assert rule[1] in f.read().split("\n") and rule[1].endswith(gen_templates.JVM_AGENT)


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
    ({"_cassandra_config_snippet_env": 'JVM_OPTS="$JVM_OPTS -javaagent:/x/jmx_prometheus_javaagent-1.6.0.jar=1:/x.yml"'},
     "already in cassandra_jvm*_extra_options or your snippet of cassandra-env.sh"),
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
           others=("theirs.jar",), config_header=True, options=None, env=None, files="", to_write="", if_missing="",
           host=None, loaded=None, include=""):
    """What the cleanup finds in a directory and a conf dir made so (sh runs its script); options, env: the content
    of the conf dir's jvm-server.options, cassandra-env.sh ({dir}: the jars' dir, {conf}: the conf dir); files: the
    ones it reads there; to_write, if_missing, include: as the task gives them; host: {absolute path: text} of the
    host's files (the unit, its drop-ins, /etc/default/cassandra, ~cassandra/.cassandra.in.sh: /home/cassandra),
    made under a root of their own (the real host's never read); loaded: the units systemctl lists (None: those of
    host; [] none, systemd absent)."""
    jars, conf, bin_dir, root = tmp_path / "exporter", tmp_path / "conf", tmp_path / "bin", tmp_path / "root"
    for d in (jars, conf, bin_dir, root):
        d.mkdir(parents=True, exist_ok=True)
    for name, text in (("jvm-server.options", options), ("cassandra-env.sh", env)):
        if text is not None:
            (conf / name).write_text(text.replace("{dir}", str(jars)).replace("{conf}", str(conf)))
    for name in list(listed) + list(others):
        (jars / name).write_text("x")
    (jars / ".cassandra_config").write_text("".join(n + "\n" for n in listed))
    (conf / "jmx_prometheus.yml").write_text(("# Managed by Ansible (community.cassandra.cassandra_config): x\n"
                                              if config_header else "# mine\n") + "rules: []\n")
    (conf / "jmx_prometheus.at_start").write_text("x\n")
    for path, text in (host or {}).items():
        (root / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / path.lstrip("/")).write_text(text.replace("{dir}", str(jars)))
    loaded = [p for p in host or {} if "/systemd/" in p] if loaded is None else loaded
    # (systemctl cat: each file's "# <path>" line, its content, a blank line)
    (tmp_path / "listing").write_text("\n".join("# %s\n%s" % (p, host[p]) for p in loaded))
    (bin_dir / "systemctl").write_text("#!/bin/sh\ncat '%s'\n" % (tmp_path / "listing") if loaded else "#!/bin/sh\nexit 1\n")
    (bin_dir / "getent").write_text("#!/bin/sh\n[ \"$2\" = cassandra ] && echo 'cassandra:x:1:1::/home/cassandra:/bin/sh'\n")
    for tool in ("systemctl", "getent"):
        (bin_dir / tool).chmod(0o755)
    argv = find(CLEANUP, "Find what the exporter's agent no longer needs")["ansible.builtin.command"]["argv"]
    run = subprocess.run(argv[:4] + [str(jars), "jmx_prometheus_javaagent-1.7.0.jar", str(conf), on, files, to_write,
                                     if_missing, "cassandra", include, str(root)],
                         stdout=subprocess.PIPE, check=True,
                         env=dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"])))
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


def test_off_nothing_removed_while_the_next_start_may_still_load_it(tmp_path):
    # removed, the jar or config the next start loads would stop Cassandra: any line naming the exporter
    # (jmx_prometheus) keeps them (a false keep only costs a warning)
    role_agent = "-javaagent:{dir}/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/cassandra/jmx_prometheus.yml\n"
    assert unused(tmp_path / "a", "off", options="-ea\n" + role_agent, files="jvm-server.options") == [
        "kept|jvm-server.options|conf"]
    # (audit r6) the jar's path through a variable (s2), another jar reading the role's config (s5, f); (audit r8) the
    # role's config through $CASSANDRA_CONF (p1, var), the jar through two variables (p2), everything through
    # variables (varall), the jar elsewhere (varjar); (round 8, s11) an exporter of your own elsewhere: kept too
    for case, text in [
            ("s2", 'EXP={dir}\nJVM_OPTS="$JVM_OPTS -javaagent:$EXP/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/x.yml"'),
            ("p1", 'JVM_OPTS="$JVM_OPTS -javaagent:/usr/share/own/jmx_prometheus_javaagent-0.20.0.jar=9404:'
                   '$CASSANDRA_CONF/jmx_prometheus.yml"'),
            ("p2", 'PROM_DIR={dir}\nPROM_JAR="$PROM_DIR/jmx_prometheus_javaagent-1.7.0.jar"\n'
                   'JVM_OPTS="$JVM_OPTS -javaagent:$PROM_JAR=9404:/usr/share/own/mine.yml"'),
            ("varall", 'JVM_OPTS="$JVM_OPTS -javaagent:${EXPORTER_JAR}=7070:${CASSANDRA_CONF}/jmx_prometheus.yml"'),
            ("varjar", 'JVM_OPTS="$JVM_OPTS -javaagent:$CASSANDRA_HOME/lib/jmx_prometheus_javaagent-1.2.0.jar=7070:/etc/own.yml"'),
            ("f", "-javaagent:/usr/share/own/jmx_prometheus_javaagent-0.20.0.jar=9404:{conf}/jmx_prometheus.yml"),
            ("s11", "-javaagent:/usr/share/own/jmx_prometheus_javaagent-0.20.0.jar=9404:/usr/share/own/mine.yml")]:
        name = "cassandra-env.sh" if "JVM_OPTS" in text else "jvm-server.options"
        given = {"env" if name == "cassandra-env.sh" else "options": text + "\n"}
        assert unused(tmp_path / case, "off", files=name, **given) == ["kept|%s|conf" % name], case
    # jamm's agent in cassandra-env.sh, a commented agent, (c) a file the role writes (only looking: not read on the
    # node, s1): removed
    jamm = 'JVM_OPTS="$JVM_OPTS -javaagent:$CASSANDRA_HOME/lib/jamm-0.4.0.jar"\n'
    assert len(unused(tmp_path / "jamm", "off", env=jamm, files="cassandra-env.sh")) == 5
    assert len(unused(tmp_path / "b", "off", options="-ea\n#" + role_agent, files="jvm-server.options")) == 5
    assert len(unused(tmp_path / "c", "off", options="-ea\n" + role_agent)) == 5
    # (audit r8, r9) what a start reads besides the conf dir: the unit and its drop-ins (systemctl cat, and those on
    # disk), the EnvironmentFiles they name, /etc/default/cassandra, the cassandra.in.sh bin/cassandra sources; each
    # said with its kind (its way out)
    line = "JVM_EXTRA_OPTS=-javaagent:{dir}/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/x.yml"
    header = "# Managed by Ansible (community.cassandra.cassandra_service): change the role variables\n"
    unit = "/etc/systemd/system/cassandra.service"
    for case, host, said in [
            ("role-unit", {unit: header + "[Service]\nEnvironment=" + line}, "%s|role-unit" % unit),
            ("unit", {"/usr/lib/systemd/system/cassandra.service": "[Service]\nEnvironment=" + line},
             "/usr/lib/systemd/system/cassandra.service|unit"),
            ("override", {unit: "[Service]\n", unit + ".d/override.conf": "Environment=" + line},
             unit + ".d/override.conf|override"),
            ("drop-in", {unit: "[Service]\n", unit + ".d/50-agent.conf": "Environment=" + line},
             unit + ".d/50-agent.conf|drop-in"),
            ("environment-file", {unit: "EnvironmentFile = -/etc/cassandra.env\n", "/etc/cassandra.env": line},
             "/etc/cassandra.env|file"),
            ("quoted", {unit: 'EnvironmentFile="/etc/cassandra.env"\n', "/etc/cassandra.env": line},
             "/etc/cassandra.env|file"),
            ("default", {"/etc/default/cassandra": line}, "/etc/default/cassandra|file"),
            ("home", {"/home/cassandra/.cassandra.in.sh": line}, "/home/cassandra/.cassandra.in.sh|file"),
            ("package", {"/usr/share/cassandra/cassandra.in.sh": line}, "/usr/share/cassandra/cassandra.in.sh|file"),
            ("include-of-the-unit", {unit: "Environment=CASSANDRA_INCLUDE=/srv/in.sh\n", "/srv/in.sh": line},
             "/srv/in.sh|file"),
            ("include-of-an-environment-file", {unit: "EnvironmentFile=/etc/c.env\n",
                                                "/etc/c.env": 'CASSANDRA_INCLUDE="/srv/in.sh"\n', "/srv/in.sh": line},
             "/srv/in.sh|file")]:
        assert unused(tmp_path / case, "off", host=host) == ["kept|" + said], case
    # (audit r9, A1) a drop-in on disk systemd has not loaded yet (no daemon-reload): read, the next start reads it
    host = {unit: "[Service]\n", unit + ".d/50-agent.conf": "Environment=" + line}
    assert unused(tmp_path / "not-loaded", "off", host=host, loaded=[unit]) == [
        "kept|%s.d/50-agent.conf|drop-in" % unit]
    # systemd absent: the units on disk read all the same; nothing there: nothing kept (harmless)
    assert unused(tmp_path / "no-systemd", "off", host={unit: "Environment=" + line}, loaded=[]) == [
        "kept|%s|unit" % unit]
    assert len(unused(tmp_path / "nothing", "off", loaded=[])) == 5
    # the same unit through /lib and /usr/lib (merged /usr): said once
    lib = tmp_path / "merged" / "root" / "lib"
    lib.parent.mkdir(parents=True)
    lib.symlink_to("usr/lib")
    assert unused(tmp_path / "merged", "off", host={"/usr/lib/systemd/system/cassandra.service": "Environment=" + line},
                  loaded=[]) == ["kept|/usr/lib/systemd/system/cassandra.service|unit"]
    # comments (# and ;) and a "# /path" line inside a unit's content (not a file of systemctl cat's): not read
    host = {unit: "[Service]\n; Environment=%s\n# Environment=%s\n# /etc/c.env\n" % (line, line),
            "/etc/c.env": line}
    assert len(unused(tmp_path / "comments", "off", host=host)) == 5
    # CASSANDRA_INCLUDE of cassandra_service_environment
    assert unused(tmp_path / "include", "off", host={"/srv/mine.in.sh": line}, include="/srv/mine.in.sh") == [
        "kept|/srv/mine.in.sh|file"]
    # (audit r9, B1) the way out for an exporter of your own, followed to the letter: nothing left to keep
    yours = 'JVM_OPTS="$JVM_OPTS -javaagent:/opt/own/jmx_prometheus_javaagent-1.2.0.jar=7070:/opt/own/mine.yml"\n'
    case = tmp_path / "w1"
    assert unused(case, "off", env=yours, files="cassandra-env.sh") == ["kept|cassandra-env.sh|conf"]
    for name in ["exporter/.cassandra_config", "exporter/jmx_prometheus_javaagent-1.6.0.jar",
                 "exporter/jmx_prometheus_javaagent-1.7.0.jar", "conf/jmx_prometheus.yml", "conf/jmx_prometheus.at_start"]:
        (case / name).unlink()
    argv = find(CLEANUP, "Find what the exporter's agent no longer needs")["ansible.builtin.command"]["argv"]
    run = subprocess.run(argv[:4] + [str(case / "exporter"), "jmx_prometheus_javaagent-1.7.0.jar", str(case / "conf"),
                                     "off", "cassandra-env.sh", "", "", "cassandra", "", str(case / "root")],
                         stdout=subprocess.PIPE, check=True)
    assert run.stdout == b""
    # what is to be written (the unit's environment, only looking the role's render); a file kept but not there yet
    assert unused(tmp_path / "s6b", "off", to_write="cassandra_service_environment jvm17-server.options") == [
        "kept|cassandra_service_environment|role-unit", "kept|jvm17-server.options|conf"]
    assert unused(tmp_path / "m", "off", if_missing="jvm-server.options cassandra-env.sh",
                  env="-Dx=1\n") == ["kept|jvm-server.options|conf"]
    argv = find(CLEANUP, "Find what the exporter's agent no longer needs")["ansible.builtin.command"]["argv"]
    task_vars = find(CLEANUP, "Find what the exporter's agent no longer needs")["vars"]

    def args(**variables):
        variables = role_vars(**variables)
        variables.update((k, render(v, variables) if isinstance(v, str) else v) for k, v in task_vars.items())
        return [render(a, variables) for a in argv[-6:-1]]
    looking = dict(_cassandra_config_exporter_writes={"skipped": True, "changed": False})
    kept = ["cassandra.yaml", "jvm-server.options", "jvm8-server.options", "jvm17-server.options"]
    # a run that writes: the files the next start reads, as written, all the series' JVM files whatever
    # cassandra_java_version says (audit r7, s7: 11 given, the node runs 17); the account, its CASSANDRA_INCLUDE
    writes = dict(_cassandra_config_exporter_writes={"rc": 0, "changed": False}, cassandra_config_keep_files=kept)
    every = "cassandra-env.sh jvm-server.options jvm11-server.options jvm17-server.options"
    assert args(cassandra_java_version="11", **writes) == [every, "", "", "cassandra", ""]
    assert args(cassandra_service_user="c2", cassandra_service_environment={"CASSANDRA_INCLUDE": "/x.in.sh"},
                **writes)[-2:] == ["c2", "/x.in.sh"]
    # only looking: the series' files kept (a jvm8-server.options of 4.x kept on 5.0: not) as they are on the node;
    # the others as the role is to write them (audit r7: the preview and the run read the same)
    assert args(cassandra_config_keep_files=kept, **looking)[:3] == ["jvm-server.options jvm17-server.options", "", ""]
    agent = "-javaagent:/opt/prometheus-jmx-exporter/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/x.yml"

    def rendered(**variables):
        new = role_render(dict({"cassandra_version": "50x", "cassandra_service_unit_manage": False}, **variables),
                          FACTS, {})[0]
        return args(_cassandra_config_new=new, **dict(looking, **variables))[1:3]
    assert rendered() == ["", ""]  # the stock files (their commented agent, jamm's): nothing
    assert rendered(cassandra_jvm17_extra_options=[agent]) == ["jvm17-server.options", ""]
    # (audit r8) kept: read on the node, or, not there yet, as the role writes it
    assert rendered(cassandra_jvm17_extra_options=[agent], cassandra_config_keep_files=["jvm17-server.options"]) == [
        "", "jvm17-server.options"]
    snippet = tmp_path / "snippet.j2"
    snippet.write_text('JVM_OPTS="$JVM_OPTS %s"\n' % agent)
    assert rendered(cassandra_config_template_snippets={"cassandra-env.sh": str(snippet)}) == ["cassandra-env.sh", ""]
    snippet.write_text('# JVM_OPTS="$JVM_OPTS %s"\n' % agent)
    assert rendered(cassandra_config_template_snippets={"cassandra-env.sh": str(snippet)}) == ["", ""]
    # a run that writes reads the node only
    assert args(_cassandra_config_new={"jvm17-server.options": agent}, **writes)[1:3] == ["", ""]
    # (audit r7, s6) the unit's environment cassandra-env.sh appends (JVM_EXTRA_OPTS), in both
    environment = {"JVM_EXTRA_OPTS": agent, "LOCAL_JMX": "no"}
    for run in (writes, looking):
        assert args(cassandra_service_environment=environment, **run)[1] == "cassandra_service_environment"
        assert args(cassandra_service_environment={"LOCAL_JMX": "no"}, **run)[1] == ""
    # nothing of the role's there (an exporter set up by hand): nothing said
    bare = tmp_path / "d"
    bare.mkdir()
    (bare / "jvm-server.options").write_text(AGENT_LINE)
    run = subprocess.run(argv[:4] + [str(bare / "none"), "x.jar", str(bare), "off", "jvm-server.options", "", "",
                                     "cassandra", "", str(bare / "root")], stdout=subprocess.PIPE, check=True)
    assert run.stdout == b""
    # said, with the way out of each; nothing removed, nothing noted
    variables = role_vars(inventory_hostname="n1", cassandra_config_exporter_unused={"stdout_lines": [
        "kept|jvm-server.options|conf"]})
    said = find(CLEANUP, "Say what the exporter's agent in a file still needs is left in place")

    def message(*lines):
        variables["cassandra_config_exporter_unused"] = {"stdout_lines": list(lines)}
        variables.update((k, render(v, variables) if isinstance(v, str) else v)
                         for k, v in said["vars"].items() if k.startswith("_"))
        return " ".join(render(said["ansible.builtin.debug"]["msg"], variables).split())
    msg = message("kept|jvm-server.options|conf")
    assert when(said, variables) is True
    assert "but jvm-server.options still names its agent or config (jmx_prometheus)" in msg
    assert "Remove the agent's line (or comment it out with #) in jvm-server.options (the file, or your copy or" \
        " template, your snippet, the extra options)." in msg
    assert "Or, if that agent is your own exporter and not the role's, delete the role's files:" \
        " /opt/prometheus-jmx-exporter/.cassandra_config, the jars it lists, /etc/cassandra/jmx_prometheus.yml and" \
        " /etc/cassandra/jmx_prometheus.at_start." in msg
    msg = message("kept|cassandra_service_environment|role-unit", "kept|/etc/systemd/system/cassandra.service.d/"
                  "override.conf|override", "kept|/etc/systemd/system/cassandra.service.d/x.conf|drop-in",
                  "kept|/usr/lib/systemd/system/cassandra.service|unit", "kept|/etc/default/cassandra|file")
    assert "in cassandra_service_environment (change cassandra_service_environment, then run cassandra_service)," \
        " /etc/systemd/system/cassandra.service.d/override.conf (systemctl edit cassandra)," \
        " /etc/systemd/system/cassandra.service.d/x.conf (edit it, then systemctl daemon-reload)," \
        " /usr/lib/systemd/system/cassandra.service (systemctl edit --full cassandra), /etc/default/cassandra" \
        " (the file)." in msg
    variables["cassandra_config_exporter_unused"] = {"stdout_lines": ["kept|jvm-server.options|conf"]}
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


# --- apply_config -------------------------------------------------------------

FIND = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs"][0]
NOTE = find(FIND["tasks"], "Note what this node needs")


def note(exporter=(), files=()):
    variables = {"_cassandra_config_changes": list(files), "_cassandra_config_perm_changes": [],
                 "_cassandra_config_exporter_changes": list(exporter), "cassandra_config_restart_pending": False,
                 "cassandra_config_newer_files": [], "_cassandra_preflight_running": True, "cassandra_jvm": {},
                 "_cassandra_config_dir_notes": [], "cassandra_apply_config_unit": {"skipped": True}}
    for name, value in (NOTE.get("vars") or {}).items():
        variables[name] = render(value, variables) if isinstance(value, str) else value
    facts = NOTE["ansible.builtin.set_fact"]
    return dict((k, render(facts[k], variables)) for k in (
        "cassandra_apply_config_todo", "cassandra_apply_config_then", "cassandra_apply_config_restart_why",
        "cassandra_apply_config_kept_running"))


RULES = {"item": "/etc/cassandra/jmx_prometheus.yml", "kind": "config", "before": "current", "after": "1 line(s) changed",
         "diff": "-a\n+b", "restart": False}


def test_new_rules_are_written_without_a_restart():
    out = note([RULES])
    assert (out["cassandra_apply_config_todo"], out["cassandra_apply_config_then"]) in [(True, "none"), ("True", "none")]
    assert out["cassandra_apply_config_kept_running"] == ["the exporter rules: reloaded at its next scrape"]
    old = {"item": "jmx_prometheus_javaagent-1.6.0.jar", "kind": "removed", "restart": False}
    # (a jar removed may be the one the running JVM opened: a version changed or the exporter turned off)
    assert note([old])["cassandra_apply_config_kept_running"] == [
        "the exporter files removed: the running Cassandra keeps the ones it opened until its next restart"]


def test_its_https_authentication_or_open_telemetry_restart_the_node():
    out = note([dict(RULES, restart=True)])
    assert out["cassandra_apply_config_then"] == "restart"
    assert out["cassandra_apply_config_restart_why"] == "the exporter HTTPS, authentication or openTelemetry: read at start"
    # with the JVM options file (the agent itself): a restart for the files, no other reason given
    out = note([dict(RULES, restart=True)], files=["jvm-server.options"])
    assert (out["cassandra_apply_config_then"], out["cassandra_apply_config_restart_why"]) == ("restart", "")


def test_nothing_of_the_exporter_nothing_to_do():
    out = note()
    assert out["cassandra_apply_config_todo"] in (False, "False") and out["cassandra_apply_config_kept_running"] == []


def test_the_exporter_record_is_in_the_record_of_the_config_cassandra_started_with():
    variables = load("roles", "cassandra_config", "vars", "main.yml")
    assert "jmx_prometheus.at_start" in variables["_cassandra_config_snapshot_files"]
    assert "jmx_prometheus.at_start" in load("roles", "cassandra_operation", "vars", "main.yml")["_cassandra_service_config_files"]


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


# --- before a restart or a start ----------------------------------------------

CHECK_START = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "exporter_check.yml"))

# stand-ins on the PATH: runuser as an account that reads only what others may read; the java the start runs
RUNUSER = """#!/bin/sh
[ "$1" = -u ] && [ "$3" = -- ] && [ "$4 $5" = "test -r" ] || exit 9
echo "$2" >> "$0.log"
[ -n "$(find "$6" -maxdepth 0 -perm -o=r 2>/dev/null)" ]
"""


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))  # a port no other test of the run takes (pytest -n auto)
    port = s.getsockname()[1]
    s.close()
    return port


def agent_check(where, jar=True, config=True, port=None, java="17", options=None, given="", jar_mode=0o644):
    """What the check finds with a jvm-server.options (or options: {file: text}) giving an agent whose jar and
    config are there or not, the start's java of that version ("": none), or the option given."""
    import getpass
    where.mkdir()
    conf, bin_dir = where / "conf", where / "bin"
    conf.mkdir()
    bin_dir.mkdir()
    for name, text in (("runuser", RUNUSER), ("systemctl", "#!/bin/sh\n"),
                       ("java", "#!/bin/sh\necho 'openjdk version \"%s.0.2\" 2024-01-16' >&2\n" % java if java else "#!/bin/sh\n")):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    if jar:
        (where / "jmx_prometheus_javaagent-1.7.0.jar").write_text("x")
        (where / "jmx_prometheus_javaagent-1.7.0.jar").chmod(jar_mode)
    if config:
        (where / "e.yml").write_text("rules: []\n")
        (where / "e.yml").chmod(0o644)
    option = "-javaagent:%s/jmx_prometheus_javaagent-1.7.0.jar=127.0.0.1:%d:%s/e.yml" % (where, port or free_port(), where)
    for name, text in (options or {"jvm-server.options": "-ea\n%s\n" % option}).items():
        (conf / name).write_text(text.replace("{option}", option))
    argv = find(CHECK_START, "Check the exporter's jar, config and port")["ansible.builtin.command"]["argv"]
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]))
    run = subprocess.run(argv[:4] + [str(conf), getpass.getuser(), given.replace("{option}", option)],
                         stdout=subprocess.PIPE, check=True, env=env)
    return [line.replace(str(where), "") for line in run.stdout.decode().split("\n") if line]


def test_a_restart_onto_a_missing_jar_or_config_is_refused(tmp_path):
    assert agent_check(tmp_path / "a") == []
    assert agent_check(tmp_path / "b", jar=False) == ["jar|/jmx_prometheus_javaagent-1.7.0.jar"]
    assert agent_check(tmp_path / "c", config=False) == ["config|/e.yml"]


def test_the_jar_is_read_as_cassandras_account(tmp_path):
    # a jar the run's account reads but Cassandra's does not (root's, downloaded under umask 027): refused
    assert agent_check(tmp_path / "a", jar_mode=0o640) == ["jar|/jmx_prometheus_javaagent-1.7.0.jar"]
    assert os.access(str(tmp_path / "a" / "jmx_prometheus_javaagent-1.7.0.jar"), os.R_OK)
    import getpass
    with open(str(tmp_path / "a" / "bin" / "runuser.log")) as f:
        assert getpass.getuser() in f.read().split()  # (runuser -u <Cassandra's account> -- test -r)


@pytest.mark.parametrize("java, found", [
    ("17", ["jvm17"]),
    ("11", ["jvm11"]),
    ("1.8", ["jvm8"]),
    ("", ["jvm11", "jvm17", "jvm8"]),  # its version unknown: every file
])
def test_only_the_options_files_of_the_java_the_start_runs(tmp_path, java, found):
    options = dict(("jvm%s-server.options" % v, "-javaagent:/nowhere/jvm%s/jmx_prometheus_javaagent-1.7.0.jar=%d:/etc/hosts\n"
                    % (v, free_port())) for v in ("8", "11", "17"))
    options["jvm-server.options"] = "-ea\n"
    out = agent_check(tmp_path / "a", java=java, options=options)
    assert sorted(line.split("/")[2] for line in out if line.startswith("jar|")) == found


def test_a_port_in_use_by_another_process_is_refused(tmp_path):
    import socket
    if not shutil.which("ss"):
        pytest.skip("ss not here")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))  # its own port (pytest -n auto runs the tests side by side)
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        found = agent_check(tmp_path / "a", port=port)
        # the inventory's option, before anything is written (the plan): the same check
        given = agent_check(tmp_path / "b", port=port, options={}, given="{option}")
    finally:
        listener.close()
    assert len(found) == 1 and found[0].startswith("port|%d|%d|" % (port, os.getpid()))
    assert len(given) == 1 and given[0].startswith("port|%d|%d|" % (port, os.getpid()))


def check_found(lines, kinds=None):
    task = find(CHECK_START, "Note what stops the agent")
    variables = {"cassandra_exporter_check": {"stdout_lines": lines}}
    if kinds is not None:
        variables["_cassandra_exporter_check_kinds"] = kinds
    return render(task["ansible.builtin.set_fact"]["_cassandra_exporter_check_found"], variables)


def test_the_check_runs_before_each_restart_and_start():
    restart = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "action_restart.yml"))
    names = [t.get("name") for t in restart]
    assert names.index("Check the Prometheus exporter's agent can start") < names.index("Drain the node")
    apply = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
    names = [t["name"] for t in apply]
    assert names.index("Check the Prometheus exporter's agent can start") == names.index("Start the node and wait until it has joined") - 1
    stop = find(CHECK_START, "Stop before Cassandra is stopped or started")
    lines = ["port|9404|812|nginx", "jar|/opt/e/jmx_prometheus_javaagent-1.7.0.jar"]
    variables = {"inventory_hostname": "n1", "cassandra_service_user": "cassandra", "_cassandra_exporter_check_found": lines}
    why = render(stop["vars"]["_why"], variables)
    assert "port 9404 is in use by pid 812 (nginx)" in why
    assert "/opt/e/jmx_prometheus_javaagent-1.7.0.jar (its jar) is missing or not readable by cassandra" in why
    assert "nothing was done on this node" in why and "Then run again" in why
    # after a write (apply_config, update_java): said so, the node not stopped
    restart_vars = find(apply, "Drain and restart the node")["vars"]
    why = render(stop["vars"]["_why"], dict(variables, _cassandra_exporter_check_left=restart_vars["_cassandra_exporter_check_left"]))
    assert "nothing was done" not in why and "not stopped (left running as it is); the config written applies" in why
    java = load("roles", "cassandra_operation", "tasks", "action_update_java.yml")
    assert "the Java and the config put in place" in find(java, "Drain and restart the node")["vars"]["_cassandra_exporter_check_left"]
    assert "its config written" in find(apply, "Check the Prometheus exporter's agent can start")["vars"]["_cassandra_exporter_check_left"]
    # what is checked: everything by default, the kinds asked for else
    assert check_found(lines) == lines
    assert check_found(lines, ["port"]) == lines[:1]
    assert check_found([], ["port"]) == []


def preflight(tasks, name="Check the Prometheus exporter's port is free"):
    task = find(tasks, name)
    assert task["ansible.builtin.include_role"]["tasks_from"] == "exporter_check.yml"
    assert task["vars"]["_cassandra_exporter_check_kinds"] == ["port"]
    assert "_cassandra_config_jvm_agent" in task["vars"]["_cassandra_exporter_check_option"]
    return task


def test_the_port_is_checked_before_anything_is_written():
    # apply_config: in the plan, for a node to restart or start, before the question (the play stops at one)
    play = FIND
    names = [t.get("name") for t in play["tasks"]]
    task = preflight(play["tasks"])
    assert names.index("Compare the config") < names.index("Note what this node needs") \
        < names.index("Check the Prometheus exporter's port is free") < names.index("Refuse restarts while a node is not running")
    assert play["any_errors_fatal"] is True and task["vars"]["_cassandra_exporter_check_left"] == "nothing was changed"
    assert "cassandra_apply_config_then in ['restart', 'start']" in task["when"]
    # update_java: before the plan is shown
    play = [p for p in load("playbooks", "update_java.yml") if p.get("name") == "Check the Java change"][0]
    names = [t.get("name") for t in play["tasks"]]
    preflight(play["tasks"])
    assert names.index("Load the role defaults") < names.index("Check the Prometheus exporter's port is free") \
        < names.index("Show the plan and confirm the run")
    assert play["any_errors_fatal"] is True


def test_an_upgrade_gets_the_jar_and_checks_the_agent_before_the_drain():
    upgrade = load("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    names = [t["name"] for t in upgrade]
    assert names.index("Put the Prometheus exporter's jar in place") < names.index("Check the Prometheus exporter's agent can start") \
        < names.index("Drain the node") < names.index("Stop Cassandra") < names.index("Write the target series' config")
    jar = find(upgrade, "Put the Prometheus exporter's jar in place")["ansible.builtin.include_role"]
    assert (jar["tasks_from"], jar["public"]) == ("prometheus_exporter_jar.yml", True)
    check = find(upgrade, "Check the Prometheus exporter's agent can start")["vars"]
    for check_mode, state, kinds in [(False, "missing", ["jar", "port"]), (False, "ok", ["jar", "port"]),
                                     (True, "ok", ["jar", "port"]), (True, "other", ["jar", "port"]),
                                     (True, "missing", ["port"])]:  # --check downloads nothing: no jar to read
        assert render(check["_cassandra_exporter_check_kinds"], {
            "ansible_check_mode": check_mode, "_cassandra_config_exporter_jar_state": state}) == kinds
    # what a refusal leaves: never drained; the jar, when this run put it there
    left_alone = "not drained (still running its former version)"
    for download, left in [({"changed": True}, left_alone + "; the exporter's jar put in place"),
                           ({"changed": False}, left_alone), ({"skipped": True, "changed": False}, left_alone)]:
        assert render(check["_cassandra_exporter_check_left"], {"cassandra_config_exporter_download": download}) == left
    stop = find(CHECK_START, "Stop before Cassandra is stopped or started")
    why = render(stop["vars"]["_why"], {"inventory_hostname": "n1", "cassandra_service_user": "cassandra",
                                        "_cassandra_exporter_check_found": ["port|9404|812|nginx"],
                                        "_cassandra_exporter_check_left": left})
    assert "nothing was done" not in why and "Cassandra would not start: not drained (still running" in why
    # the jar's tasks are cassandra_config's own (the same download, the same stops)
    jar_tasks = [t.get("name") for t in walk(load_tasks(os.path.join(ROLE, "tasks", "prometheus_exporter_jar.yml")))]
    assert "Download the exporter's jar" in jar_tasks and "Stop where the jar could not be downloaded" in jar_tasks
    assert [t.get("name") for t in walk(EXPORTER)][:len(jar_tasks)] == jar_tasks


# --- cassandra_firewall -------------------------------------------------------

FIREWALL = load("roles", "cassandra_firewall", "vars", "main.yml")
FIREWALL_TASKS = load_tasks(os.path.join(TOP, "roles", "cassandra_firewall", "tasks", "firewall.yml"))


def port_sources(**variables):
    defaults = load("roles", "cassandra_firewall", "defaults", "main.yml")
    variables = dict(defaults, **variables)
    return render(FIREWALL["_cassandra_firewall_port_sources"], variables)


def test_the_exporters_port_is_open_to_its_sources_only():
    assert port_sources() == {}
    assert port_sources(cassandra_prometheus_exporter_enabled=True) == {}  # no source: closed
    assert port_sources(cassandra_prometheus_exporter_enabled=True, cassandra_prometheus_exporter_sources=["10.0.20.0/24"],
                        cassandra_firewall_port_sources={"7199/tcp": ["10.0.9.5"]}) == {
        "7199/tcp": ["10.0.9.5"], "9404/tcp": ["10.0.20.0/24"]}
    assert port_sources(cassandra_prometheus_exporter_enabled=True, cassandra_prometheus_exporter_port=9500,
                        cassandra_prometheus_exporter_sources=["10.0.20.7"]) == {"9500/tcp": ["10.0.20.7"]}
    assert port_sources(cassandra_prometheus_exporter_sources=["10.0.20.7"]) == {}  # exporter off
    # every rule of the role reads them so
    text = open(os.path.join(TOP, "roles", "cassandra_firewall", "tasks", "firewall.yml")).read()
    assert "cassandra_firewall_port_sources" not in text.replace("_cassandra_firewall_port_sources", "")


def test_a_port_open_to_no_one_is_said():
    task = find(FIREWALL_TASKS, "Say the exporter's port is not opened")
    defaults = load("roles", "cassandra_firewall", "defaults", "main.yml")

    def said(**variables):
        variables = dict(defaults, **variables)
        variables["_cassandra_firewall_port_sources"] = port_sources(**variables)
        variables.update((k, render(v, variables)) for k, v in task["vars"].items() if k.startswith("_"))
        return when(task, variables)
    assert said(cassandra_prometheus_exporter_enabled=True) is True
    assert said(cassandra_prometheus_exporter_enabled=True, cassandra_prometheus_exporter_sources=["10.0.20.7"]) is False
    assert said(cassandra_prometheus_exporter_enabled=True, open_ports=["9404/tcp"]) is False  # open to all by hand
    assert said() is False


# --- health_check -------------------------------------------------------------

def test_health_check_asks_the_exporter_from_the_node():
    play = [p for p in load("playbooks", "health_check.yml") if p.get("name") == "Cluster health"][0]
    ask = find(play["tasks"], "Check the Prometheus exporter answers")
    other = find(play["tasks"], "Check it answers over the other scheme")
    note = find(play["tasks"], "Note an exporter not answering")
    said = find(play["tasks"], "Say the exporters not answering")
    names = [t.get("name") for t in play["tasks"]]
    assert names.index("Check the cluster from this node") < names.index("Check the Prometheus exporter answers") \
        < names.index("Check it answers over the other scheme") < names.index("Note an exporter not answering") \
        < names.index("Say the exporters not answering") < names.index("Report")
    assert ask["failed_when"] is False and ask["check_mode"] is False

    def url(task, address="", keystore=""):
        variables = role_vars(cassandra_prometheus_exporter_address=address, cassandra_prometheus_exporter_tls_keystore=keystore)
        variables.update((k, render(v, variables)) for k, v in task["vars"].items())
        return render(task["ansible.builtin.uri"]["url"], variables)
    assert url(ask) == "http://127.0.0.1:9404/-/healthy" and url(other) == "https://127.0.0.1:9404/-/healthy"
    assert url(ask, keystore="/k.p12") == "https://127.0.0.1:9404/-/healthy"
    assert url(ask, "10.0.0.5") == "http://10.0.0.5:9404/-/healthy"
    assert url(ask, "fd00::5") == "http://[fd00::5]:9404/-/healthy"
    # the other scheme once, when the first got no answer (the uri module's -1)
    assert when(other, {"cassandra_health_exporter": {"status": -1}}) is True
    assert when(other, {"cassandra_health_exporter": {"status": 401}}) is False
    # no answer over either: a warning (text and host), never a problem of the report
    for first, second, noted in ((-1, -1, True), (-1, 200, False), (200, None, False)):
        variables = {"cassandra_health_exporter": {"status": first},
                     "cassandra_health_exporter_other": {"status": second} if second else {"skipped": True}}
        assert when(note, variables) is noted
    variables = role_vars(inventory_hostname="n2", cassandra_health_findings=[])
    finding = render(note["ansible.builtin.set_fact"]["cassandra_health_findings"], variables)[0]
    assert (finding["kind"], finding["host"], finding["text"]) == (
        "warning", "n2", "Prometheus exporter not answering on port 9404 of n2")
    assert said["ansible.builtin.debug"]["msg"].startswith("WARNING  Prometheus exporter not answering on")
    assert said["run_once"] is True


def test_its_config_is_not_readable_by_others():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import cassandra_file_permissions
    settings = {"owner": "root", "group": "cassandra", "mode": "0640", "public_mode": "0644", "files": {},
                "user": "cassandra", "user_group": "cassandra"}
    assert cassandra_file_permissions("jmx_prometheus.yml", settings) == {"owner": "root", "group": "cassandra",
                                                                          "mode": "0640"}
    # set apart like the other config files
    settings["files"] = {"jmx_prometheus.yml": {"mode": "0600"}}
    assert cassandra_file_permissions("jmx_prometheus.yml", settings)["mode"] == "0600"
