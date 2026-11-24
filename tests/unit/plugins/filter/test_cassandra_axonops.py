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


def test_a_half_installed_package_is_said_so():
    lines = [line for line in SET_UP if not line.startswith("package axon-agent")] + [
        "package axon-agent half-configured 2.0.36"]
    assert changes(lines)["items"] == [{"item": "package axon-agent", "before": "half installed (half-configured)",
                                        "after": "2.0.36"}]


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
    rpm = cassandra_axonops_files(["axon-agent-2.0.36-1.x86_64.rpm", JDK17 + "-1.1.1-1.noarch.rpm"], JDK17)
    assert rpm["wanted"] == [["axon-agent", "2.0.36"], [JDK17, "1.1.1"]]
    # their versions are what the plan compares with: installed as they are, nothing to do
    lines = [line.replace("1.1.1-1", "1.1.1") for line in SET_UP]
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
    assert install["ansible.builtin.dnf"]["name"].startswith("{{ _cassandra_axonops_wanted[:1] if _cassandra_axonops_removed")
    # Debian: the old one removed only after the new one is in and its options file checked
    assert names.index("Remove the Java agent packages of another series or Java (Debian & Ubuntu)") > names.index(
        "Stop without the options file cassandra-env.sh loads") > names.index("Install the AxonOps packages (Debian & Ubuntu)")


def test_repository_secrets_and_keyrings():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "repository.yml")
    yum = _find(tasks, "The AxonOps yum repository (RedHat)")
    assert yum["diff"] is False and "0600" in yum["ansible.builtin.yum_repository"]["mode"]
    names = _names(tasks)
    assert names.index("Create the key directory") < names.index("Download the signing key")
    assert names.index("Apt credentials without whitespace") < names.index("Credentials of the AxonOps apt repository")


def test_no_account_created_and_a_group_change_restarts_the_agent():
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    names = _names(tasks)
    assert names.index("Look for the accounts") < names.index("Stop without an account to add to a group") < names.index(
        "The groups")
    start = _find(tasks, "Start axon-agent, or restart it for its new package or config")
    assert "cassandra_axonops_groups is changed" in start["vars"]["_changed"]


@pytest.mark.parametrize("path, before", [
    (("roles", "cassandra_operation", "tasks", "action_upgrade.yml"), "Drain the node"),
    (("roles", "cassandra_operation", "tasks", "action_update_java.yml"), "Write the config"),
])
def test_upgrade_and_java_change_set_up_the_agent_first(path, before):
    # they wrote the line of the new series or Java with the former Java agent package in place
    tasks = _tasks(*path)
    names = _names(tasks)
    setup = [n for n in names if str(n).startswith("Set up the AxonOps agent")]
    assert len(setup) == 1 and names.index(setup[0]) < names.index(before)
    assert tasks[names.index(setup[0])]["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_axonops"


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
                                   "cassandra_jvm11_extra_options", "cassandra_jvm17_extra_options", "env", "snippet"])
def test_cassandra_config_reads_every_loader(where):
    # a loader in a jvm<N> list or in the unit's JVM_EXTRA_OPTS was missed: the JVM loaded the agent twice
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    task = _find(_tasks("roles", "cassandra_config", "tasks", "checks.yml"), "Refuse a second AxonOps Java agent loader")
    variables = dict(cassandra_jvm_extra_options=[], cassandra_jvm8_extra_options=[], cassandra_jvm11_extra_options=[],
                     cassandra_jvm17_extra_options=[], cassandra_service_environment={}, _cassandra_config_snippet_env="",
                     cassandra_config_keep_files=[], cassandra_axonops_enabled=True, inventory_hostname="n1")
    if where == "env":
        variables["cassandra_service_environment"] = {"JVM_EXTRA_OPTS": LOADER_LINE}
    elif where == "snippet":
        variables["_cassandra_config_snippet_env"] = 'JVM_OPTS="$JVM_OPTS %s"' % LOADER_LINE
    else:
        variables[where] = [LOADER_LINE]
    for name in ("_lists", "_env", "_options", "_wrong", "_said"):
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
    assert refuse["vars"]["_said"][-2].startswith("WAY OUT  ")
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


def test_a_planned_package_refreshes_the_apt_cache():
    # a mirror updated since the last refresh: the pinned version was not found
    install = _find(_tasks("roles", "cassandra_axonops", "tasks", "packages.yml"), "Install them from the repositories")
    assert install["ansible.builtin.apt"]["update_cache"] is True
