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
    for name in ("_files", "_command"):
        variables[name] = render_with(task["vars"][name], variables)
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


def test_read_sh_says_the_options_file_and_the_accounts():
    import subprocess
    script = os.path.join(TOP, "roles", "cassandra_axonops", "files", "read.sh")
    out = subprocess.run(["sh", script], env=dict(os.environ, CASSANDRA_USER="no-such-user-x1"), check=True,
                         capture_output=True, text=True).stdout.splitlines()
    assert len([line for line in out if line in ("options yes", "options no")]) == 1
    assert out.count("account_no-such-user-x1 no") == 1
    assert len([line for line in out if line in ("cassandra_installed yes", "cassandra_installed no")]) == 1
    assert len([line for line in out if line in ("account_axonops yes", "account_axonops no")]) == 1


def test_a_planned_package_refreshes_the_apt_cache():
    # a mirror updated since the last refresh: the pinned version was not found; never in upgrade's window
    install = _find(_packages(), "Install them from the repositories")
    assert install["ansible.builtin.apt"]["update_cache"] is True


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
    assert key["always"][0]["ansible.builtin.file"]["state"] == "absent" and key["always"][0]["check_mode"] is False
    # the rescue only around what reaches the network; the variable named by the task that failed
    network = _find(tasks, "The signing key, the source and the cache")
    assert _names(network["block"])[:3] == ["Compare the signing key, put it in place when it differs",
                                            "Make a temporary file for the key", "Download the signing key"]
    debian = _find(tasks, "The AxonOps apt repository (Debian & Ubuntu)")
    assert "rescue" not in debian
    said = _said(network["rescue"][0], ansible_failed_task={"name": "Download the signing key"},
                 ansible_failed_result={"status_code": 401, "msg": "HTTP Error 401: Unauthorized https://u:pw@m/k"})
    assert said == ["STOPPED  cassandra_axonops  n1", "  Download the signing key failed:",
                    "    HTTP 401: HTTP Error 401: Unauthorized https://****@m/k",
                    "WAY OUT  check cassandra_axonops_repository_key_url (and the login), then run",
                    "  again"]
    # the other kind of key removed once the source names this one
    names = _names(tasks)
    assert names.index("The AxonOps apt source") < names.index("Remove the signing key of the other kind")


@pytest.mark.parametrize("check", [False, True])
def test_what_the_node_holds_is_refused_before_any_change(check):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import cassandra_axonops_read_problems
    lines = ["account_cass no", "account_axonops no", "package axon-agent installed 2.0.36",
             "package axon-cassandra4.1-agent half-installed 1.1.1", "package x unpacked 1", "package y unpacked 1"]
    got = cassandra_axonops_read_problems(lines, "cass", check)
    assert got[:4] == ["axon-cassandra4.1-agent is half-installed (dpkg): its unpack was interrupted.",
                       "x is unpacked (dpkg): its configuration was not finished.",
                       "y is unpacked (dpkg): its configuration was not finished.",
                       "axon-agent is installed, its account axonops is missing."]
    # WAY OUT, its steps numbered under it (each once: the dpkg one for x and y), run again last
    ways = [x for x in got[got.index("WAY OUT"):] if x.strip()]  # (a blank line between steps of several lines)
    assert ways[1:4] == ["  1. on the node: apt-get install --reinstall axon-cassandra4.1-agent",
                         "  2. on the node: dpkg --force-confdef --force-confold --configure -a",
                         "  3. on the node: apt-get install --reinstall axon-agent (dnf reinstall"]
    assert ways[-1].endswith(". run again") and all(len(x) <= 77 for x in got)
    # Cassandra's account: refused, --check too, unless Cassandra is not installed under --check (a blank host)
    assert "no account cass on the node (cassandra_axonops_cassandra_user)." in got
    assert any("in the inventory: set cassandra_axonops_cassandra_user" in x for x in got)
    blank = cassandra_axonops_read_problems(lines + ["cassandra_installed no"], "cass", check)
    assert ("no account cass on the node (cassandra_axonops_cassandra_user)." in blank) is not check
    assert cassandra_axonops_read_problems(["account_cass no", "cassandra_installed yes"], "cass", True)[0].startswith(
        "no account cass")


def test_the_account_check_follows_check_mode_not_the_compare():
    # the compare runs on installed nodes (apply_config, update_java, the upgrade's preflight): an account missing
    # there is refused in the plan
    task = _find(_tasks("roles", "cassandra_axonops", "tasks", "main.yml"), "Refuse what the node holds")
    assert "cassandra_axonops_cassandra_user, ansible_check_mode)" in task["vars"]["_said"]


def test_the_apt_source_without_python3_debian():
    # --check on a blank host: python3-debian not installed yet; looked for as cassandra_repository does
    from ansible_collections.community.cassandra.tests.unit.helpers import task_runs
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "repository.yml")
    assert _find(tasks, "Look for python3-debian")["ansible.builtin.command"] == 'python3 -c "import debian"'
    install = _find(tasks, "Install python3-debian")
    assert install["ansible.builtin.apt"] == {"name": "python3-debian", "update_cache": True}
    source = _find(tasks, "The AxonOps apt source")
    missing = {"cassandra_axonops_python_debian": {"rc": 1}}
    assert not task_runs(source, _cassandra_axonops_compare=True, **missing)
    assert task_runs(source, _cassandra_axonops_compare=False, **missing)
    names = _names(tasks)
    assert names.index("Install python3-debian") < names.index("The AxonOps apt source")
    # (a plan row for it, as cassandra_repository's)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import (
        cassandra_axonops_repository_items)
    assert cassandra_axonops_repository_items({"python_debian": True, "key_other": True}, "u", "k") == [
        {"item": "repository signing key of the other kind", "before": "present", "after": "removed"},
        {"item": "package python3-debian", "before": "absent", "after": "installed"}]


def test_the_marker_is_no_loader():
    # the role's "# Custom settings" above the AxonOps line and the snippet: no second loader, no override refused
    assert cassandra_axonops_loader_refusals("# Custom settings\nexport X=1", []) == []
    assert cassandra_axonops_loader_refusals("", [], "x\n# Custom settings\n{{ _cassandra_config_axonops_line }}") == []


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


@pytest.mark.parametrize("playbook, play, task, operation, before", [
    ("upgrade.yml", "Upgrade checks", "Refuse an upgrade with the AxonOps agent", "upgrade",
     "An upgrade downloads the new packages"),
    ("update_java.yml", "Check the Java change", "Refuse a Java change with the AxonOps agent", "update_java",
     "Check the Prometheus exporter's port is free"),
    ("apply_config.yml", "Find what each node needs", "Refuse a Java move with the AxonOps agent", "apply_config",
     "Note what this node needs"),
])
def test_a_java_or_series_change_refuses_the_axonops_agent_for_now(playbook, play, task, operation, before):
    # not supported yet in this version: refused before any change, --check too, with its way out; an INI
    # inventory's "false" or "no" is false (the agent off: never refused)
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with, task_runs
    plays = _tasks("playbooks", playbook)
    tasks = [p for p in plays if p.get("name") == play][0]["tasks"]
    names = _names(tasks)
    refuse = _find(tasks, task)
    hostvars = {"n1": {"cassandra_axonops_enabled": True, "cassandra_java_plan_todo": True},
                "n2": {"cassandra_axonops_enabled": "false", "cassandra_java_plan_todo": True},
                "n3": {"cassandra_axonops_enabled": "no", "cassandra_java_plan_todo": True},
                "n4": {"cassandra_java_plan_todo": True},
                "n5": {"cassandra_axonops_enabled": "true", "cassandra_java_plan_todo": False}}
    variables = {"ansible_play_hosts": list(hostvars), "hostvars": hostvars, "_phase": "canary",
                 "groups": {"cassandra_update_java_True": ["n1", "n2", "n3", "n4"]}, "_cassandra_cluster_shown": "c",
                 "cassandra_cluster_name": "c", "inventory_hostname": "n1"}
    for name in ("_nl", "_cluster", "_axonops"):
        variables[name] = render_with(refuse["vars"][name], variables)
    # (upgrade: every node of the run, n5 too; a Java move: the nodes moving only)
    assert variables["_axonops"] == (["n1", "n5"] if operation == "upgrade" else ["n1"])
    variables["_said"] = [render_with(x, variables) for x in refuse["vars"]["_said"]]
    msg = render_with(refuse["ansible.builtin.fail"]["msg"], variables).split("\n")
    assert msg[0] == "REFUSED  %s  c  nothing was changed" % operation
    assert any("with the AxonOps agent: not supported yet in this version" in x for x in msg)
    assert [x for x in msg if x.startswith("  WAY OUT  ")] and all(len(x) <= 79 for x in msg)
    assert task_runs(refuse, **variables) and refuse["run_once"] is True and "check_mode" not in refuse
    off = dict(variables)
    off["_axonops"] = []
    assert not task_runs(refuse, **off)
    assert names.index(task) < names.index(before)
    if operation == "upgrade":
        cleanup = dict(variables)
        cleanup["_phase"] = "cleanup"
        assert not task_runs(refuse, **cleanup)
    if operation == "apply_config":
        assert names.index("Work out the Java of the nodes running another one") < names.index(task)
        drift = dict(variables)
        drift["_cassandra_apply_config_compare_only"] = True
        assert not task_runs(refuse, **drift)


@pytest.mark.parametrize("path, before", [
    (("roles", "cassandra_operation", "tasks", "action_restart.yml"), "Drain the node"),
    (("roles", "cassandra_operation", "tasks", "action_reboot.yml"), "Reboot the host"),
])
def test_a_restart_or_reboot_checks_the_options_file_first(path, before):
    # a restart that writes no config (rolling_restart, rolling_reboot) on a node whose axonops-jvm.options is gone:
    # Cassandra would not start (sh stops on a missing sourced file); stopped before, the node left as it is
    from ansible_collections.community.cassandra.tests.unit.helpers import task_runs
    tasks = _tasks(*path)
    names = _names(tasks)
    check = _find(tasks, "Check the AxonOps Java agent can load")
    assert check["ansible.builtin.include_tasks"] == "axonops_check.yml"
    assert names.index("Check the AxonOps Java agent can load") < names.index(before)
    assert task_runs(check, cassandra_axonops_enabled=True) and not task_runs(check, cassandra_axonops_enabled="false")
    assert not task_runs(check)


def test_start_rack_checks_the_options_file_first():
    from ansible_collections.community.cassandra.tests.unit.helpers import task_runs
    play = [p for p in _tasks("playbooks", "start_rack.yml") if p.get("name") == "Start the rack"][0]
    names = _names(play["tasks"])
    assert names.index("Check the AxonOps Java agent can load") < names.index("Start Cassandra and wait until the node has joined")
    stop = _find(_tasks("roles", "cassandra_operation", "tasks", "axonops_check.yml"),
                 "Stop without the AxonOps Java agent's options file")
    assert task_runs(stop, cassandra_axonops_enabled=True, cassandra_operation_axonops_options={"stat": {"exists": False}})
    assert not task_runs(stop, cassandra_axonops_enabled=True, cassandra_operation_axonops_options={"stat": {"exists": True}})
    assert not task_runs(stop, cassandra_axonops_enabled=False)
    assert stop["vars"]["_lines"][-1].startswith("WAY OUT  run apply_config")


@pytest.mark.parametrize("enabled", [False, "false", None])
def test_cassandra_config_does_nothing_of_axonops_when_off(enabled):
    # the agent off (false, an INI "false", undefined): neither the look for the options file nor the loader refusal
    from ansible_collections.community.cassandra.tests.unit.helpers import task_runs
    variables = {} if enabled is None else {"cassandra_axonops_enabled": enabled}
    look = _find(_tasks("roles", "cassandra_config", "tasks", "main.yml"), "Look for the AxonOps Java agent's options file")
    refuse = _find(_tasks("roles", "cassandra_config", "tasks", "checks.yml"), "Refuse a second AxonOps Java agent loader")
    assert not task_runs(look, **variables)
    assert not task_runs(refuse, **dict(variables, _wrong=["x"]))
    assert task_runs(look, cassandra_axonops_enabled=True)


def test_package_files_as_axonops_serves_them(tmp_path):
    # the owner's air-gapped mirror: the files as AxonOps' apt pool and yum repository name them, a relative path
    # read from the inventory's directory, each one checked on the controller
    deb = ["axon-agent_2.0.36_amd64_769e74dfe82250707f76eda5243a64e9.deb",
           "axon-cassandra5.0-agent-jdk17_1.1.1_all_1b0268acf3bcb06bcec500670209101d.deb"]
    rpm = ["63d7129a9246118c5bc787a56bbdd6e566099038bd3dd9a8ae89f2cdfc3faf81-axon-agent-2.0.36-1.x86_64.rpm",
           "e86cb5af61c62c4fc49dac1c82fa600b141a1dc48308943527bb24104d4a73b8-axon-cassandra5.0-agent-jdk17-1.1.1-1.noarch.rpm"]
    (tmp_path / "files").mkdir()
    for name in deb + rpm:
        (tmp_path / "files" / name).write_text("x")
    out = cassandra_axonops_files(["files/" + f for f in deb], JDK17, "2.0.36", "1.1.1", str(tmp_path), True)
    assert out["problems"] == [] and out["wanted"] == [["axon-agent", "2.0.36"], [JDK17, "1.1.1"]]
    assert out["paths"] == [str(tmp_path / "files" / f) for f in deb]
    out = cassandra_axonops_files(["files/" + f for f in rpm], JDK17, "", "", str(tmp_path), True)
    assert out["problems"] == [] and out["wanted"] == [["axon-agent", "2.0.36-1"], [JDK17, "1.1.1-1"]]
    # missing on the controller: refused, --check too (the role's refusal comes before any change)
    missing = cassandra_axonops_files(["files/nope_1_all.deb"] + ["files/" + f for f in deb], JDK17, "", "",
                                      str(tmp_path), True)
    assert missing["problems"][0].startswith(str(tmp_path / "files" / "nope_1_all.deb") + ": no such file on the controller")
    # anything else: its expected forms said
    other = cassandra_axonops_files(["axon-agent-latest.deb"], JDK17)
    assert other["problems"][0].startswith("axon-agent-latest.deb: not a package file name (name_version_arch.deb or")


def test_packages_the_repositories_lack_are_refused_before_any_change():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import cassandra_axonops_unavailable
    items = [{"item": "package axon-agent", "before": "absent", "after": "2.0.36"},
             {"item": "package " + JDK17, "before": "absent", "after": "installed (newest)"},
             {"item": "package axon-cassandra5.0-agent-jdk11", "before": "1.1.1", "after": "removed"}]
    wanted = [["axon-agent", "2.0.36"], [JDK17, ""]]
    lines = ["available axon-agent 2.0.35", "available axon-agent 2.0.36-1", "available " + JDK17 + " 1.1.1"]
    assert cassandra_axonops_unavailable(lines, wanted, items) == []
    assert cassandra_axonops_unavailable(lines[:1] + lines[2:], wanted, items) == [
        "axon-agent 2.0.36: not in the repositories (they have 2.0.35)."]
    assert cassandra_axonops_unavailable(lines[:2], wanted, items) == [JDK17 + ": no repository of the host has it."]
    # (looked up under --check too, only when the repository is not written by this run)
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    look = _find(tasks, "Look for the packages in the repositories")
    assert look["check_mode"] is False and "cassandra_axonops_package_files | length == 0" in look["when"]
    names = _names(tasks)
    assert names.index("The repository") < names.index("Refuse packages the repositories do not have") < names.index(
        "Install, configure and start the agent")


def test_a_rack_restart_checks_the_options_file_of_every_node_before_any_drain():
    # rolling_restart -e cassandra_rolling_mode=rack: every node of the rack checked, none stopped when one lacks it
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with, task_runs
    block = _tasks("roles", "cassandra_operation", "tasks", "restart_batch.yml")[0]["block"]
    names = _names(block)
    look = _find(block, "Look for the AxonOps Java agent's options file on the rack's nodes")
    stop = _find(block, "Stop without the AxonOps Java agent's options file")
    assert names.index("Look for the AxonOps Java agent's options file on the rack's nodes") < names.index(
        "Stop without the AxonOps Java agent's options file") < names.index("Drain the rack's nodes")
    assert look["delegate_to"] == "{{ item }}" and look["check_mode"] is False
    hostvars = {"n1": {"cassandra_axonops_enabled": True}, "n2": {"cassandra_axonops_enabled": "false"}}
    assert task_runs(look, item="n1", hostvars=hostvars) and not task_runs(look, item="n2", hostvars=hostvars)
    results = {"results": [{"item": "n1", "stat": {"exists": False}}, {"item": "n2", "skipped": True},
                           {"item": "n3", "stat": {"exists": True}}]}
    missing = render_with(stop["vars"]["_missing"], {"cassandra_restart_axonops_options": results})
    assert missing == ["n1"]
    assert task_runs(stop, _missing=missing) and not task_runs(stop, _missing=[])
    msg = render_with(stop["ansible.builtin.fail"]["msg"], {"_missing": missing})
    assert msg.startswith("/usr/share/axonops/axonops-jvm.options is missing on n1 (") and "run apply_config" in msg


def test_the_restart_check_fits_79_columns_and_claims_only_what_is_sure():
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    stop = _find(_tasks("roles", "cassandra_operation", "tasks", "axonops_check.yml"),
                 "Stop without the AxonOps Java agent's options file")
    host = {"inventory_hostname": "cassandra-node-with-a-long-name-01.example.com"}
    said = render_with(stop["vars"]["_said"], dict(host, _lines=[render_with(x, host) for x in stop["vars"]["_lines"]]))
    assert said[0].startswith("STOPPED  cassandra-node-with-a-long-name-01.example.com  Cassandra not stopped")
    assert all(len(x) <= 79 for x in said)
    assert [x for x in said if x.startswith("WAY OUT  run apply_config")]
    # (apply_config wrote the config before its restart: "nothing was changed" would not be true)
    assert not [x for x in said if "nothing was changed" in x]


def test_a_reinstall_is_not_looked_up_in_the_repositories():
    # dnf 4 lists an installed version as not available: the reinstall of a package whose options file was deleted
    # would be refused as "no repository of the host has it"
    from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import cassandra_axonops_unavailable
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    items = [{"item": "package " + JDK17, "before": "1.1.1", "after": "reinstalled"}]
    assert cassandra_axonops_unavailable([], [[JDK17, "1.1.1"]], items) == []
    look = _find(_tasks("roles", "cassandra_axonops", "tasks", "main.yml"), "Look for the packages in the repositories")
    planned = render_with(look["vars"]["_planned"], {"_cassandra_axonops_changes": {"items": items + [
        {"item": "package axon-agent", "before": "absent", "after": "2.0.36"},
        {"item": "package x", "before": "1", "after": "removed"}]}})
    assert planned == ["axon-agent"]


def test_an_unreadable_repository_is_said_so_not_a_missing_package():
    # RedHat: the repository is skip_if_unavailable, dnf would list nothing from it; read first, its error said
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    tasks = _tasks("roles", "cassandra_axonops", "tasks", "main.yml")
    look = _find(tasks, "Look for the packages in the repositories")
    script = look["ansible.builtin.shell"]
    assert script.index("makecache --repo axonops") < script.index("for name in $NAMES")
    refuse = _find(tasks, "Refuse packages the repositories do not have")
    variables = {"cassandra_axonops_available": {"stdout_lines": [
        "unreadable Status code: 401 for https://u:p@m.example.com/yum/repodata/repomd.xml"]},
        "cassandra_axonops_repository_url": "https://m.example.com/yum", "_cassandra_axonops_changes": {"items": []},
        "cassandra_axonops_agent_version": "", "cassandra_axonops_java_agent_version": "",
        "_cassandra_axonops_java_package": JDK17}
    variables["_wanted"] = [["axon-agent", ""], [JDK17, ""]]
    for name in ("_lines", "_unreadable", "_url", "_missing", "_said"):
        variables[name] = render_with(refuse["vars"][name], variables)
    assert variables["_missing"][0].startswith("the AxonOps repository https://m.example.com/yum cannot be read: ")
    assert "u:p@" not in variables["_missing"][0]
    assert variables["_said"][-1].startswith("WAY OUT  check cassandra_axonops_repository_url")
