from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import (
    cassandra_apply_config_axonops_steps,
    cassandra_axonops_changes,
    cassandra_axonops_config,
    cassandra_axonops_files,
    cassandra_axonops_loader_refusals,
    cassandra_axonops_java_agent,
    cassandra_axonops_problems,
)
from ansible_collections.community.cassandra.tests.unit.helpers import TOP

SETTINGS = {"server_host": "axon.example.com", "server_port": 1888, "org": "acme", "key": "", "tls_mode": "disabled",
            "tls_skip_verify": False, "tls_ca_file": "", "tls_cert_file": "", "tls_key_file": "", "ntp_hosts": [],
            "extra_settings": {}, "java_agent_package": "", "series": "50x", "java": "17"}


@pytest.mark.parametrize("series, java, package", [
    ("40x", "8", "axon-cassandra4.0-agent-jdk8"),
    ("40x", "11", "axon-cassandra4.0-agent"),
    ("41x", "8", "axon-cassandra4.1-agent-jdk8"),
    ("41x", 11, "axon-cassandra4.1-agent"),
    ("50x", "11", "axon-cassandra5.0-agent-jdk11"),
    ("50x", "17", "axon-cassandra5.0-agent-jdk17"),
    ("50x", "8", ""),
    ("41x", "17", ""),
    ("30x", "8", ""),
])
def test_java_agent_package(series, java, package):
    assert cassandra_axonops_java_agent(series, java) == package


def test_settings_fine():
    assert cassandra_axonops_problems(SETTINGS) == []


@pytest.mark.parametrize("change, said", [
    ({"server_host": ""}, "cassandra_axonops_server_host is empty"),
    ({"org": " "}, "cassandra_axonops_org is empty"),
    ({"tls_mode": "tls"}, "cassandra_axonops_tls_mode is tls: it must be one of disabled, TLS, mTLS"),
    ({"tls_mode": "mTLS", "tls_cert_file": "/c.pem"}, "cassandra_axonops_tls_mode mTLS needs"),
    ({"extra_settings": ["x"]}, "cassandra_axonops_extra_settings must be a dict"),
    ({"java": "8"}, "AxonOps has no Java agent package for Cassandra 5.0 on Java 8 (it has: Java 11, Java 17)"),
    ({"series": "30x", "java": "8"}, "AxonOps has no Java agent package for Cassandra 30x on Java 8 (it has: none)"),
])
def test_settings_refused(change, said):
    problems = cassandra_axonops_problems(dict(SETTINGS, **change))
    assert len(problems) == 1 and problems[0].startswith(said), problems


def test_a_package_given_is_not_checked_against_the_java():
    assert cassandra_axonops_problems(dict(SETTINGS, java="8", java_agent_package="axon-cassandra5.0-agent-jdk17")) == []


def test_config_minimal():
    text = cassandra_axonops_config(SETTINGS)
    assert text.startswith("# Managed by Ansible (community.cassandra.cassandra_axonops)\n")
    assert yaml.safe_load(text) == {"axon-server": {"hosts": "axon.example.com", "port": 1888},
                                    "axon-agent": {"org": "acme", "tls": {"mode": "disabled"}}}
    # the agent's own order: server first
    assert text.index("axon-server:") < text.index("axon-agent:")


def test_config_full():
    config = yaml.safe_load(cassandra_axonops_config(dict(
        SETTINGS, server_port="443", key="s3cr3t", tls_mode="mTLS", tls_skip_verify=True, tls_ca_file="/ca.pem",
        tls_cert_file="/c.pem", tls_key_file="/k.pem", ntp_hosts=["ntp1.example.com:123"],
        extra_settings={"axon-agent": {"disable_command_exec": True, "tls": {"skipVerify": False}},
                        "backup": {"restore_parallelism": 4}})))
    assert config == {
        "axon-server": {"hosts": "axon.example.com", "port": 443},
        "axon-agent": {"org": "acme", "key": "s3cr3t", "disable_command_exec": True,
                       "tls": {"mode": "mTLS", "skipVerify": False, "caFile": "/ca.pem", "certFile": "/c.pem",
                               "keyFile": "/k.pem"}},
        "NTP": {"hosts": ["ntp1.example.com:123"]},
        "backup": {"restore_parallelism": 4}}


def test_config_tls_without_client_certificate():
    config = yaml.safe_load(cassandra_axonops_config(dict(SETTINGS, tls_mode="TLS")))
    assert config["axon-agent"]["tls"] == {"mode": "TLS", "skipVerify": False}


def test_config_refused_settings_raise():
    with pytest.raises(AnsibleFilterError, match="cassandra_axonops_org is empty"):
        cassandra_axonops_config(dict(SETTINGS, org=""))


def test_extra_settings_left_unchanged():
    extra = {"axon-agent": {"tls": {"mode": "TLS"}}}
    cassandra_axonops_config(dict(SETTINGS, extra_settings=extra))
    assert extra == {"axon-agent": {"tls": {"mode": "TLS"}}}


WANTED = [["axon-agent", "2.0.36"], ["axon-cassandra5.0-agent-jdk17", ""]]
SET_UP = ["package axon-agent installed 2.0.36", "package axon-cassandra5.0-agent-jdk17 installed 1.1.1-1",
          "user_groups cassandra axonops", "axonops_groups axonops cassandra", "enabled enabled", "active active",
          "options yes", "config abc", "config_stat root axonops 640"]


def changes(lines, wanted=None):
    return cassandra_axonops_changes(lines, wanted or WANTED, "abc", "cassandra", "cassandra", "h:1888, org o")


def test_changes_none_when_set_up():
    assert changes(SET_UP) == {"installed": {"axon-agent": "2.0.36", "axon-cassandra5.0-agent-jdk17": "1.1.1-1"},
                               "removed": [], "latest": [], "reinstall": [], "items": []}
    # a pinned version matches its version-revision
    assert changes(SET_UP, [["axon-agent", "2.0.36"], ["axon-cassandra5.0-agent-jdk17", "1.1.1"]])["items"] == []


def test_changes_on_a_blank_node():
    # dpkg lists a package removed with its config left: not installed
    lines = ["package axon-agent config-files 2.0.30", "user_groups cassandra", "axonops_groups ", "enabled ",
             "active inactive", "options no"]
    assert changes(lines)["items"] == [
        {"item": "package axon-agent", "before": "absent", "after": "2.0.36"},
        {"item": "package axon-cassandra5.0-agent-jdk17", "before": "absent", "after": "installed (newest)"},
        {"item": "cassandra in group axonops", "before": "no", "after": "yes"},
        {"item": "axonops in group cassandra", "before": "no", "after": "yes"},
        {"item": "/etc/axonops/axon-agent.yml", "before": "absent", "after": "server h:1888, org o"},
        {"item": "service axon-agent", "before": "not installed", "after": "running, enabled at boot"}]


def test_changes_version_config_mode_and_service():
    lines = [line for line in SET_UP if not line.startswith(("config", "active"))] + [
        "config other", "config_stat root root 644", "active inactive"]
    lines[0] = "package axon-agent installed 2.0.35"
    assert changes(lines)["items"] == [
        {"item": "package axon-agent", "before": "2.0.35", "after": "2.0.36"},
        {"item": "/etc/axonops/axon-agent.yml", "before": "other settings", "after": "server h:1888, org o"},
        {"item": "service axon-agent", "before": "stopped (inactive), enabled at boot",
         "after": "running, enabled at boot"}]
    lines[lines.index("config other")] = "config abc"
    assert changes(lines)["items"][1] == {"item": "/etc/axonops/axon-agent.yml owner, group, mode",
                                          "before": "root root 644", "after": "root axonops 640"}


def test_changes_another_java_agent_replaced():
    lines = SET_UP[:1] + ["package axon-cassandra5.0-agent-jdk11 installed 1.1.1"] + SET_UP[2:]
    out = changes(lines)
    assert out["removed"] == ["axon-cassandra5.0-agent-jdk11"]
    assert out["items"] == [
        {"item": "package axon-cassandra5.0-agent-jdk17", "before": "absent", "after": "installed (newest)"},
        {"item": "package axon-cassandra5.0-agent-jdk11", "before": "1.1.1", "after": "removed"}]


@pytest.mark.parametrize("options, keep, said", [
    ("-Xss256k", [], []),
    ("-Xss256k . /usr/share/axonops/axonops-jvm.options", [], ["the AxonOps Java agent is already loaded"]),
    ('JVM_OPTS="$JVM_OPTS -javaagent:/usr/share/axonops/axon-cassandra4.1-agent.jar=/etc/axonops/axon-agent.yml"', [],
     ["the AxonOps Java agent is already loaded"]),
    ("", ["cassandra-env.sh"], ["cassandra-env.sh is in cassandra_config_keep_files"]),
])
def test_loader_refusals(options, keep, said):
    got = cassandra_axonops_loader_refusals(options, keep)
    assert [g[:len(s)] for g, s in zip(got, said)] == said and len(got) == len(said)


RESTART = ["write the config", "drain and restart Cassandra"]


@pytest.mark.parametrize("steps, sections, expected", [
    # none: as they are
    (RESTART, ["Packages"], RESTART),
    ([], ["AxonOps"], []),
    # the agent alone: set up before the config, no "apply the ..." step for it
    (RESTART, ["AxonOps"], ["set up the AxonOps agent"] + RESTART),
    (["write the config", "apply the repository, packages, firewall", "drain and restart Cassandra"], ["AxonOps"],
     ["set up the AxonOps agent", "write the config", "drain and restart Cassandra"]),
    # with packages: their step kept
    (["write the config", "apply the repository, packages, firewall", "drain and restart Cassandra"],
     ["AxonOps", "Packages"],
     ["set up the AxonOps agent", "write the config", "apply the repository, packages, firewall",
      "drain and restart Cassandra"]),
    # after the release's repository and the Java
    (["apply the repository, look for Cassandra 5.0.9 in it", "install Java 17", "write the config",
      "apply the packages, firewall", "drain and stop Cassandra", "start Cassandra"], ["Repository", "AxonOps"],
     ["apply the repository, look for Cassandra 5.0.9 in it", "install Java 17", "set up the AxonOps agent",
      "write the config", "drain and stop Cassandra", "start Cassandra"]),
])
def test_apply_config_steps(steps, sections, expected):
    extras = [{"section": s, "item": "x"} for s in sections]
    assert cassandra_apply_config_axonops_steps(steps, extras) == expected


def test_the_node_turn_sets_up_the_agent_before_the_config():
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "action_apply_config.yml")) as f:
        tasks = yaml.safe_load(f)
    names = [t["name"] for t in tasks]
    setup = tasks[names.index("Set up the AxonOps agent")]
    assert names.index("Set up the AxonOps agent") < names.index("Write the config")
    assert setup["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_axonops"
    # only where the plan found its changes (section AxonOps), never under check mode
    assert setup["when"] == ["not ansible_check_mode",
                             "'AxonOps' in cassandra_apply_config_extras | default([]) | map(attribute='section')"]


@pytest.mark.parametrize("have, options, key, before, after", [
    ("1.0.10", "yes", "latest", "1.0.10 (older than 1.1.0)", "newest (1.1.0 or later)"),
    ("1.1.1", "no", "reinstall", "1.1.1 (no axonops-jvm.options)", "reinstalled"),
])
def test_a_java_agent_without_its_options_file_is_upgraded(have, options, key, before, after):
    # a Java agent from before 1.1.0 (the axonops.axonops collection's default 1.0.10): cassandra-env.sh could not
    # load it; with no version pinned, the plan upgrades it to the newest instead of seeing nothing to do
    lines = [line for line in SET_UP if not line.startswith(("package axon-cassandra", "options"))] + [
        "package axon-cassandra5.0-agent-jdk17 installed " + have, "options " + options]
    out = changes(lines)
    assert out[key] == ["axon-cassandra5.0-agent-jdk17"]
    assert out["items"] == [{"item": "package axon-cassandra5.0-agent-jdk17", "before": before, "after": after}]


@pytest.mark.parametrize("change, said", [
    ({"server_port": "18x"}, "cassandra_axonops_server_port is 18x: it must be a port number"),
    ({"server_port": 70000}, "cassandra_axonops_server_port is 70000"),
    ({"ntp_hosts": "ntp1"}, "cassandra_axonops_ntp_hosts must be a list"),
    ({"java_agent_version": "1.0.10"}, "cassandra_axonops_java_agent_version is 1.0.10: 1.1.0 or later is needed"),
])
def test_more_settings_refused(change, said):
    problems = cassandra_axonops_problems(dict(SETTINGS, **change))
    assert len(problems) == 1 and problems[0].startswith(said), problems


JDK17 = "axon-cassandra5.0-agent-jdk17"


def test_package_files_fit():
    out = cassandra_axonops_files(["f/axon-cassandra5.0-agent-jdk17_1.1.1_all.deb", "f/axon-agent_2.0.36_amd64.deb"],
                                  JDK17, "2.0.36", "")
    assert out == {"wanted": [["axon-agent", "2.0.36"], [JDK17, "1.1.1"]],
                   "paths": ["f/axon-agent_2.0.36_amd64.deb", "f/axon-cassandra5.0-agent-jdk17_1.1.1_all.deb"],
                   "problems": []}
    # (rpm: version-release, as read.sh reads an installed one; a pin with or without its release fits)
    rpm = cassandra_axonops_files(["axon-agent-2.0.36-1.x86_64.rpm", JDK17 + "-1.1.1-1.noarch.rpm"], JDK17,
                                  "2.0.36", "1.1.1-1")
    assert rpm["wanted"] == [["axon-agent", "2.0.36-1"], [JDK17, "1.1.1-1"]]
    # their versions are what the plan compares with: installed as they are, nothing to do
    lines = [line.replace("installed 2.0.36", "installed 2.0.36-1") for line in SET_UP]
    assert cassandra_axonops_changes(lines, rpm["wanted"], "abc", "cassandra", "cassandra", "x")["items"] == []


@pytest.mark.parametrize("files, pins, said", [
    (["axon-agent_2.0.36_amd64.deb"], ("", ""), "no file for axon-cassandra5.0-agent-jdk17"),
    (["axon-agent_2.0.36_amd64.deb", "axon-cassandra5.0-agent-jdk11_1.1.1_all.deb"], ("", ""),
     "axon-cassandra5.0-agent-jdk11_1.1.1_all.deb: package axon-cassandra5.0-agent-jdk11, neither"),
    (["axon-agent_2.0.35_amd64.deb", JDK17 + "_1.1.1_all.deb"], ("2.0.36", ""),
     "axon-agent is 2.0.35 in its file, the inventory pins 2.0.36"),
    (["axon-agent_2.0.36_amd64.deb", JDK17 + "_1.0.10_all.deb"], ("", ""), JDK17 + " 1.0.10: 1.1.0 or later"),
    (["axon-agent.deb", "axon-agent_2.0.36_amd64.deb", JDK17 + "_1.1.1_all.deb"], ("", ""),
     "axon-agent.deb: not a package file name"),
])
def test_package_files_refused(files, pins, said):
    out = cassandra_axonops_files(files, JDK17, *pins)
    assert out["wanted"] == [] and out["problems"][0].startswith(said), out["problems"]


def _tasks(*path):
    with open(os.path.join(TOP, *path)) as f:
        return yaml.safe_load(f)


def _names(tasks):
    out = []
    for t in tasks:
        out.append(t.get("name"))
        out += _names(t.get("block") or [])
    return out


def _find(tasks, name):
    for t in tasks:
        if t.get("name") == name:
            return t
        found = _find(t.get("block") or [], name)
        if found:
            return found
    return None


def test_redhat_swaps_the_java_agent_in_one_transaction():
    # the old Java agent was removed before the new one was installed: a failed install left cassandra-env.sh
    # loading a file no longer there
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "packages.yml")
    names = _names(tasks)
    assert "Remove the Java agent packages of another series or Java (RedHat)" not in names
    swap = _find(tasks, "Replace the Java agent package of another series or Java")
    assert "'swap', _cassandra_axonops_removed[0], _cassandra_axonops_wanted[1]" in swap["ansible.builtin.command"]["argv"]
    install = _find(tasks, "Install them")
    assert install["ansible.builtin.dnf"]["name"] == "{{ _cassandra_axonops_wanted[:1] if _swap else _cassandra_axonops_wanted }}"
    assert "_cassandra_axonops_java_change | bool" in install["vars"]["_swap"]
    assert "_cassandra_axonops_java_change | bool" in swap["when"]
    # Debian: the old one removed only after the new one is in and its options file checked
    assert names.index("Remove the Java agent packages of another series or Java (Debian & Ubuntu)") > names.index(
        "Stop without the options file cassandra-env.sh loads") > names.index("Install the AxonOps packages (Debian & Ubuntu)")


def test_repository_secrets_and_keyrings():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "repository.yml")
    yum = _find(tasks, "The AxonOps yum repository (RedHat)")
    assert yum["diff"] is False and "0600" in yum["ansible.builtin.yum_repository"]["mode"]
    # a mirror refusing the login broke every dnf run of the node, the collection's compare included
    assert yum["ansible.builtin.yum_repository"]["skip_if_unavailable"] is True
    # the key: downloaded to a temporary file outside /etc (check mode too), compared, put in place for real only,
    # the temporary file always removed; no key directory made under check mode
    assert "check_mode" not in _find(tasks, "Create the key directory")
    key = _find(tasks, "Compare the signing key, put it in place when it differs")
    assert _find(key["block"], "Make a temporary file for the key")["check_mode"] is False
    assert _find(key["block"], "Download the signing key")["ansible.builtin.get_url"]["dest"] == (
        "{{ cassandra_axonops_key_tmp.path }}")
    assert "not _cassandra_axonops_compare | bool" in _find(key["block"], "Put the signing key in place")["when"]
    assert key["always"][0]["ansible.builtin.file"]["state"] == "absent"
    assert key["always"][0]["check_mode"] is False
    # a failure says what and why, the URLs masked, then its way out
    debian = _find(tasks, "The AxonOps apt repository (Debian & Ubuntu)")
    assert "cassandra_url_masked" in debian["rescue"][0]["vars"]["_cause"]
    assert "WAY OUT" in debian["rescue"][0]["vars"]["_said"]


def test_no_account_created_and_a_group_change_restarts_the_agent():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    names = _names(tasks)
    assert names.index("Look for the account axonops") < names.index("Stop without the account axonops") < names.index(
        "The groups")
    # Cassandra's account and the rest of what the node holds: refused before any change, --check too
    assert names.index("Refuse what the node holds") < names.index("The repository") < names.index(
        "Install, configure and start the agent")
    assert "repository" not in _names(_find(tasks, "Install, configure and start the agent")["block"])
    start = _find(tasks, "Start axon-agent, or restart it for its new package or config")
    assert "cassandra_axonops_groups is changed" in start["vars"]["_changed"]


def test_the_plans_of_upgrade_and_update_java_show_it():
    plays = _tasks("playbooks", "upgrade.yml")
    checks = [p for p in plays if p.get("name") == "Upgrade checks"][0]
    assert _find(checks["tasks"], "Show the AxonOps agent's changes (nothing is done)")["vars"]["_cassandra_compare"]
    plays = _tasks("playbooks", "update_java.yml")
    check = [p for p in plays if p.get("name") == "Check the Java change"][0]
    assert _find(check["tasks"], "Compare the AxonOps agent")["vars"]["_cassandra_compare"]
    assert "_cassandra_update_java_lines" in _find(check["tasks"], "Show the plan and confirm the run")["vars"]["_blocks"]


LOADER_LINE = "-javaagent:/usr/share/axonops/axon-cassandra5.0-agent.jar=/etc/axonops/axon-agent.yml"


@pytest.mark.parametrize("where", ["cassandra_jvm_extra_options", "cassandra_jvm8_extra_options",
                                   "cassandra_jvm11_extra_options", "cassandra_jvm17_extra_options", "env",
                                   "env_jvm_opts", "snippet"])
def test_cassandra_config_reads_every_loader(where):
    # a loader in a jvm<N> list or in the unit's JVM_EXTRA_OPTS was missed: the JVM loaded the agent twice
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    task = _find(_tasks("roles", "cassandra_config", "tasks", "checks.yml"), "Refuse a second AxonOps Java agent loader")
    variables = dict(cassandra_jvm_extra_options=[], cassandra_jvm8_extra_options=[], cassandra_jvm11_extra_options=[],
                     cassandra_jvm17_extra_options=[], cassandra_service_environment={}, _cassandra_config_snippet_env="",
                     cassandra_config_keep_files=[], cassandra_axonops_enabled=True, inventory_hostname="n1",
                     cassandra_config_template_overrides={})
    if where == "env":
        variables["cassandra_service_environment"] = {"JVM_EXTRA_OPTS": LOADER_LINE}
    elif where == "env_jvm_opts":
        variables["cassandra_service_environment"] = {"JVM_OPTS": LOADER_LINE}
    elif where == "snippet":
        variables["_cassandra_config_snippet_env"] = 'JVM_OPTS="$JVM_OPTS %s"' % LOADER_LINE
    else:
        variables[where] = [LOADER_LINE]
    for name in ("_lists", "_environment", "_env", "_override", "_options", "_wrong", "_said"):
        variables[name] = render_with(task["vars"][name], variables)
    assert variables["_wrong"][0].startswith("the AxonOps Java agent is already loaded")
    assert variables["_said"][-1].startswith("WAY OUT  ")
    msg = render_with(task["ansible.builtin.fail"]["msg"], dict(variables, _nl="\n"))
    assert msg.startswith("REFUSED  cassandra_config  n1  nothing was changed")


def test_cassandra_config_refuses_a_missing_options_file():
    tasks = _tasks("roles", "cassandra_config", "tasks", "main.yml")
    look = _find(tasks, "Look for the AxonOps Java agent's options file")
    refuse = _find(tasks, "Refuse to load an AxonOps Java agent not installed")
    # a command: skipped under check mode (apply_config's compare: the agent comes later)
    assert look["ansible.builtin.command"]["argv"][:2] == ["test", "-e"]
    assert refuse["when"][1:] == ["cassandra_config_axonops_options is not skipped",
                                  "cassandra_config_axonops_options.rc != 0"]
    assert [line for line in refuse["vars"]["_said"] if line.startswith("WAY OUT  ")]
    names = _names(tasks)
    assert names.index("Refuse to load an AxonOps Java agent not installed") < names.index("Write the files and directories")


@pytest.mark.parametrize("sections, said", [
    (["AxonOps"], "AXONOPS_LIVE"),
    (["Packages"], "EXTRAS_LIVE"),
    (["Packages", "AxonOps"], "EXTRAS_AXONOPS_LIVE"),
])
def test_why_no_restart_names_the_agent(sections, said):
    from ansible_collections.community.cassandra.plugins.filter import cassandra_apply_config as plan
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    play = [p for p in _tasks("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs"][0]
    task = _find(play["tasks"], "Note what this node needs")
    variables = {"_extras": [{"section": s, "item": "x"} for s in sections]}
    for name in ("_axonops", "_others", "_extras_live"):
        variables[name] = render_with(task["vars"][name], variables)
    assert variables["_extras_live"] == getattr(plan, said)
    assert plan._proven(variables["_extras_live"])


REPO = dict(SETTINGS, repository_manage=True, repository_username="", repository_password="",
            repository_key_url="https://k", os_family="Debian")


@pytest.mark.parametrize("change, said", [
    ({"repository_password": "p"}, "cassandra_axonops_repository_password is set without"),
    ({"repository_key_url": ""}, "cassandra_axonops_repository_key_url is empty"),
    ({"repository_username": "u", "repository_password": "a b"}, "cassandra_axonops_repository_username or _password"),
])
def test_repository_settings_refused_in_the_plan(change, said):
    assert cassandra_axonops_problems(REPO) == []
    problems = cassandra_axonops_problems(dict(REPO, **change))
    assert len(problems) == 1 and problems[0].startswith(said), problems
    # not managed, or yum (no key needed): nothing
    assert cassandra_axonops_problems(dict(REPO, repository_manage=False, **change)) == []


@pytest.mark.parametrize("override, said", [
    ("", []),
    ("x\n{{ ('\\n' ~ _cassandra_config_axonops_line) }}", []),
    ("x\n", ["cassandra-env.sh is in cassandra_config_template_overrides and your template does not write"]),
    ("x\n{{ _cassandra_config_axonops_line }}\n. /usr/share/axonops/axonops-jvm.options",
     ["your cassandra-env.sh template (cassandra_config_template_overrides) loads the AxonOps Java agent itself"]),
])
def test_a_cassandra_env_template_of_yours_must_write_the_line(override, said):
    got = cassandra_axonops_loader_refusals("", [], override)
    assert [g[:len(s)] for g, s in zip(got, said)] == said and len(got) == len(said)


def _packages():
    return _tasks("roles", "cassandra_axonops", "tasks", "packages.yml")


@pytest.mark.parametrize("family, files, argv", [
    ("Debian", [], ["apt-get", "-y", "install", "--reinstall", JDK17]),
    ("Debian", ["/var/cache/cassandra-axonops/a.deb", "/var/cache/cassandra-axonops/j.deb"],
     ["apt-get", "-y", "install", "--reinstall", "/var/cache/cassandra-axonops/j.deb"]),
    ("RedHat", [], ["dnf", "-y", "reinstall", JDK17]),
    ("RedHat", ["/var/cache/cassandra-axonops/a.rpm", "/var/cache/cassandra-axonops/j.rpm"],
     ["dnf", "-y", "--nogpgcheck", "reinstall", "/var/cache/cassandra-axonops/j.rpm"]),
])
def test_the_reinstall(family, files, argv):
    # a package file given (air-gapped): reinstalled from it, the repositories may not have it
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    task = _find(_packages(), "Install the Java agent package again")
    variables = {"ansible_facts": {"os_family": family}, "_cassandra_axonops_reinstall": [JDK17],
                 "_cassandra_axonops_wanted": files or ["axon-agent", JDK17],
                 "_cassandra_axonops_files": {"paths": files}}
    variables["_files"] = render_with(task["vars"]["_files"], variables)
    assert render_with(task["ansible.builtin.command"]["argv"], variables) == argv
    assert task["environment"]["DEBIAN_FRONTEND"] == "noninteractive"
    assert task["when"] == "_cassandra_axonops_reinstall | length > 0"


@pytest.mark.parametrize("java_change, removed, left", [
    (True, ["a", "b"], ["b"]),  # the first one swapped already
    (False, ["a", "b"], ["a", "b"]),  # left over next to the right one: all of them (the plan converges)
    (True, ["a"], []),
])
def test_redhat_removes_every_package_left_over(java_change, removed, left):
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with, task_runs
    task = _find(_packages(), "Remove the other Java agent packages (RedHat)")
    variables = {"_cassandra_axonops_java_change": java_change, "_cassandra_axonops_removed": removed,
                 "ansible_facts": {"os_family": "RedHat"}}
    variables["_left"] = render_with(task["vars"]["_left"], variables)
    assert variables["_left"] == left
    assert task_runs(task, **variables) is bool(left)
    deb = _find(_packages(), "Remove the Java agent packages of another series or Java (Debian & Ubuntu)")
    assert deb["ansible.builtin.apt"]["name"] == "{{ _cassandra_axonops_removed }}"
    assert "_cassandra_axonops_removed | length > 0" in deb["when"]


def test_repository_rows_never_show_a_secret():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import (
        cassandra_axonops_repository_items)
    assert cassandra_axonops_repository_items({}, "u", "k") == []
    rows = cassandra_axonops_repository_items({"source": True, "key": True, "credentials": True}, "https://m/a", "https://m/k")
    assert rows == [{"item": "repository https://m/a", "before": "differs", "after": "written"},
                    {"item": "repository signing key", "before": "differs", "after": "https://m/k"},
                    {"item": "repository credentials", "before": "differs", "after": "set (hidden)"}]


@pytest.mark.parametrize("check", [False, True])
def test_what_the_node_holds_is_refused_before_any_change(check):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import cassandra_axonops_read_problems
    # every problem at once, then their ways out
    lines = ["account_cass no", "account_axonops no", "package axon-agent installed 2.0.36",
             "package axon-cassandra4.1-agent half-installed 1.1.1", "package x unpacked 1"]
    got = cassandra_axonops_read_problems(lines, "cass", check)
    assert got[:3] == ["axon-cassandra4.1-agent is half-installed (dpkg): its unpack was interrupted.",
                       "x is unpacked (dpkg): its configuration was not finished.",
                       "axon-agent is installed, its account axonops is missing."]
    assert got[-1] == "  then run again."
    assert "WAY OUT  on the node: apt-get install --reinstall axon-cassandra4.1-agent" in got
    assert "  then: dpkg --force-confdef --force-confold --configure -a" in got
    # Cassandra's account: a real run only (--check installs no Cassandra on a blank host)
    assert ("no account cass on the node (cassandra_axonops_cassandra_user)." in got) is not check
    assert cassandra_axonops_read_problems(["account_cass no", "account_axonops no"], "cass", True) == []
    assert cassandra_axonops_read_problems(["account_cass no"], "cass")[0].startswith("no account cass")


def test_the_refusal_passes_check_mode():
    task = _find(_tasks("roles", "cassandra_axonops", "tasks", "main.yml"), "Refuse what the node holds")
    assert "_cassandra_axonops_compare | bool" in task["vars"]["_said"]


def test_read_sh_says_the_options_file_and_the_accounts():
    import subprocess
    script = os.path.join(TOP, "roles", "cassandra_axonops", "files", "read.sh")
    out = subprocess.run(["sh", script], env=dict(os.environ, CASSANDRA_USER="no-such-user-x1"), check=True,
                         capture_output=True, text=True).stdout.splitlines()
    assert len([line for line in out if line in ("options yes", "options no")]) == 1
    assert out.count("account_no-such-user-x1 no") == 1
    assert len([line for line in out if line in ("account_axonops yes", "account_axonops no")]) == 1


def _said(task, **variables):
    """The message of a stop task: its vars rendered in order, then its msg's lines."""
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    variables = dict({"inventory_hostname": "n1", "_nl": "\n"}, **variables)
    for name, value in task["vars"].items():
        if isinstance(value, list):
            variables[name] = [render_with(v, variables) for v in value]
        else:
            variables[name] = render_with(value, variables) if isinstance(value, str) else value
    return variables["_said"]


def test_upgrade_downloads_before_the_drain_and_installs_after_the_target():
    tasks = _tasks("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    names = _names(tasks)
    download = _find(tasks, "Download the AxonOps packages of the target series")
    assert download["vars"]["_cassandra_axonops_download_only"] is True
    assert names.index("Download the AxonOps packages of the target series") < names.index("Drain the node")
    setup = _find(tasks, "Set up the AxonOps agent for the target series")
    # once the target is installed (a failed install never leaves the former version with the target's agent),
    # before its config; from the packages downloaded before the drain
    assert names.index("Install the target version") < names.index("Set up the AxonOps agent for the target series") < (
        names.index("Write the target series' config"))
    assert setup["block"][0]["vars"]["_cassandra_axonops_cached"] is True
    said = _said(setup["rescue"][0], cassandra_package_version="5.0.9", ansible_failed_task={"name": "Install them"},
                 ansible_failed_result={"msg": "E: no https://u:pw@mirror/x"}, _cassandra_axonops_java_package=JDK17)
    assert said == ["STOPPED  upgrade  n1", "  Cassandra is stopped for its upgrade, 5.0.9",
                    "  installed, its config not written", "  the AxonOps agent: Install them:",
                    "    E: no https://****@mirror/x",
                    "WAY OUT  fix the cause on n1, install",
                    "  " + JDK17 + " there by hand, then run",
                    "  this phase again: it says how to write the config and start the node"]


def test_update_java_swaps_after_the_config_and_leaves_the_check_to_the_role():
    tasks = _tasks("roles", "cassandra_operation", "tasks", "action_update_java.yml")
    names = _names(tasks)
    assert names.index("Write the config") < names.index("Set up the AxonOps agent for the new Java") < names.index(
        "Drain and restart the node")
    # its config check of the options file left to the role (which installs it, then checks it): a deleted options
    # file, an agent before 1.1.0 or AxonOps newly on converge in one run
    assert _find(tasks, "Write the config")["vars"]["_cassandra_config_axonops_later"] is True
    look = _find(_tasks("roles", "cassandra_config", "tasks", "main.yml"), "Look for the AxonOps Java agent's options file")
    assert "not _cassandra_config_axonops_later | default(false) | bool" in look["when"]
    said = _said(_find(tasks, "Set up the AxonOps agent for the new Java")["rescue"][0],
                 ansible_failed_task={"name": "Install them"}, ansible_failed_result={"msg": "boom"})
    assert said[1] == "  Cassandra still runs on its former Java; the config of the new Java is"
    assert said[-1] == "WAY OUT  fix the cause, then run update_java again" and all(len(x) <= 79 for x in said)


def test_a_planned_package_refreshes_the_apt_cache():
    # a mirror updated since the last refresh: the pinned version was not found; never in upgrade's window
    install = _find(_packages(), "Install them from the repositories")
    assert install["ansible.builtin.apt"]["update_cache"] == "{{ not _cassandra_axonops_cached | default(false) | bool }}"


@pytest.mark.parametrize("name, module", [("Upgrade the Java agent package to the newest", "ansible.builtin.apt"),
                                          ("Upgrade the Java agent package to the newest (RedHat)",
                                           "ansible.builtin.dnf")])
def test_the_upgrade_to_the_newest(name, module):
    task = _find(_packages(), name)
    assert task[module]["name"] == "{{ _cassandra_axonops_latest }}" and task[module]["state"] == "latest"
    when = task["when"] if isinstance(task["when"], list) else [task["when"]]
    assert "_cassandra_axonops_latest | length > 0" in when


def test_redhat_reads_its_repository_before_a_package_from_it():
    # skip_if_unavailable hides why dnf finds no package: the repository read first, its own error said
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "repository_read.yml")
    probe = _find(tasks, "Read the AxonOps repository")
    assert probe["ansible.builtin.command"]["argv"] == ["dnf", "-q", "makecache", "--repo", "axonops",
                                                        "--setopt=axonops.skip_if_unavailable=False"]
    stop = _find(tasks, "Stop when the AxonOps repository cannot be read")
    said = _said(stop, cassandra_axonops_repository_url="https://u:pw@mirror/yum",
                 cassandra_axonops_repo_probe={"stderr_lines": ["", "Errors during downloading metadata for repository 'axonops':"
                                               " Status code: 401 for https://u:pw@mirror/yum/repodata/repomd.xml"]})
    assert said[1] == "  the AxonOps repository https://****@mirror/yum cannot be read:"
    assert ":pw@" not in str(said) and said[-1].startswith("WAY OUT  ") and all(len(x) <= 79 for x in said)
    names = _names(_packages())
    assert names.index("Read the AxonOps repository (RedHat)") < names.index("Install the AxonOps packages (RedHat)")


def test_the_stop_when_the_removal_took_the_options_file():
    tasks = _packages()
    names = _names(tasks)
    assert names.index("Remove the other Java agent packages (RedHat)") < names.index(
        "Check the options file is still there") < names.index("Stop when the removal took the options file")
    said = _find(tasks, "Stop when the removal took the options file")["vars"]["_said"]
    assert said[-1].startswith("WAY OUT  run this again: it reinstalls")


def test_the_failed_write_says_its_cause_never_the_key():
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    write = _find(_tasks("roles", "cassandra_axonops", "tasks", "main.yml"), "Write /etc/axonops/axon-agent.yml, or say why not")
    stop = write["rescue"][0]
    variables = {"inventory_hostname": "n1", "_cassandra_axonops_config": "key: s3cr3t",
                 "ansible_failed_result": {"msg": "chown failed: failed to look up group axonops", "content": "s3cr3t"}}
    variables["_cause"] = render_with(stop["vars"]["_cause"], variables)
    said = render_with(stop["vars"]["_said"], variables)
    assert said[1:3] == ["  /etc/axonops/axon-agent.yml not written:", "    chown failed: failed to look up group axonops"]
    assert "s3cr3t" not in str(said) and all(len(line) <= 79 for line in said)


def test_repository_rows_reach_the_plan_under_check_mode():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    names = _names(tasks)
    assert names.index("The repository") < names.index("List the changes")
    assert "_cassandra_axonops_repository_items" in _find(tasks, "List the changes")["ansible.builtin.set_fact"][
        "_cassandra_axonops_items"]
    # (outside the block skipped under check mode)
    assert "The repository" not in _names(_find(tasks, "Install, configure and start the agent")["block"])


def test_the_apt_source_without_python3_debian():
    # --check on a blank host: python3-debian not installed yet (cassandra_repository installs it in a real run),
    # deb822_repository failed to import; a real run installs it first
    from ansible_collections.community.cassandra.tests.unit.helpers import task_runs
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "repository.yml")
    source = _find(tasks, "The AxonOps apt source")
    missing = {"cassandra_axonops_python_debian": {"stdout": ""}}
    assert not task_runs(source, _cassandra_axonops_compare=True, **missing)
    assert task_runs(source, _cassandra_axonops_compare=False, **missing)
    install = _find(tasks, "Install python3-debian")
    assert task_runs(install, _cassandra_axonops_compare=False, **missing)
    assert not task_runs(install, _cassandra_axonops_compare=True, **missing)
    names = _names(tasks)
    assert names.index("Install python3-debian") < names.index("The AxonOps apt source")


def test_a_stop_of_the_role_gives_its_facts_to_the_window_stop():
    tasks = _tasks("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    msg = ("STOPPED  cassandra_axonops  n1\n  /etc/axonops/axon-agent.yml not written:\n"
           "    can not use content with a dir as dest\nWAY OUT  fix the cause on the node, then run again")
    said = _said(_find(tasks, "Set up the AxonOps agent for the target series")["rescue"][0],
                 cassandra_package_version="5.0.9", ansible_failed_task={"name": "Stop on the failed write"},
                 _cassandra_axonops_java_package=JDK17, ansible_failed_result={"msg": msg})
    assert said[4:6] == ["    /etc/axonops/axon-agent.yml not written: can not use content with a dir as",
                         "      dest"]


def test_upgrade_prepares_everything_but_the_java_agent_before_the_drain():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    block = _find(tasks, "Install, configure and start the agent")
    assert block["when"] == "not _cassandra_axonops_compare | bool"
    names = _names(block["block"])
    assert names.index("Download the Java agent package, install axon-agent") < names.index("The groups")
    download = _tasks("roles", "cassandra_axonops", "tasks", "download.yml")
    agent = _find(download, "Install axon-agent (Debian & Ubuntu)")
    assert "'package axon-agent' in _cassandra_axonops_items | map(attribute='item')" in agent["when"]
