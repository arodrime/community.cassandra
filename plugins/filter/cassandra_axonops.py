# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters for the AxonOps agent (cassandra_axonops role).

cassandra_axonops_java_agent: series (40x, 41x, 50x), Java major -> the
    AxonOps Java agent package built for them, e.g. axon-cassandra5.0-agent-jdk17
    ("" when none).
cassandra_axonops_config: the role's settings -> the text of axon-agent.yml.
cassandra_axonops_problems: the role's settings -> what is wrong with them,
    one line each ([] when nothing).
cassandra_axonops_changes: what the role read on the node and what it wants
    -> the packages installed, the ones replaced, the changes [{item, before,
    after}] (apply_config's plan).
cassandra_axonops_files: the package files given -> the packages and versions they
    hold, or why they do not fit.
cassandra_axonops_loader_refusals: the JVM options and the cassandra-env.sh
    snippet, the files kept by hand -> why the role could not load the Java
    agent itself ([] when it can).
cassandra_apply_config_axonops_steps: a node's steps in apply_config's plan
    and its extras -> the steps with the AxonOps agent's set-up.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import copy
import re

import yaml

from ansible.errors import AnsibleFilterError

# The Java agent packages of AxonOps (packages.axonops.com), by series and Java major. Every one ships
# /usr/share/axonops/axonops-jvm.options, which cassandra-env.sh sources (agent packages 1.1.0 and later).
JAVA_AGENTS = {
    "40x": {"8": "axon-cassandra4.0-agent-jdk8", "11": "axon-cassandra4.0-agent"},
    "41x": {"8": "axon-cassandra4.1-agent-jdk8", "11": "axon-cassandra4.1-agent"},
    "50x": {"11": "axon-cassandra5.0-agent-jdk11", "17": "axon-cassandra5.0-agent-jdk17"},
}
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
TLS_MODES = ("disabled", "TLS", "mTLS")
# the first Java agent package version that ships /usr/share/axonops/axonops-jvm.options
MIN_JAVA_AGENT = "1.1.0"
OPTIONS = "/usr/share/axonops/axonops-jvm.options"
# dpkg states of a package in place (the others: half done, or gone with its config left)
DPKG_DONE = ("installed", "triggers-awaited", "triggers-pending")
DPKG_GONE = ("not-installed", "config-files")
DEB_FILE = re.compile(r"^(?P<name>[^_/]+)_(?P<version>[^_/]+)_(?P<arch>[^_/]+)[.]deb$")
RPM_FILE = re.compile(r"^(?P<name>[^/]+)-(?P<version>[^-/]+)-(?P<release>[^-/]+)[.](?P<arch>[^./]+)[.]rpm$")


def _numbers(version):
    """"1.1.1-1" -> [1, 1, 1] (the version before its revision; a part that is no number counts 0)."""
    return [int(p) if p.isdigit() else 0 for p in re.split(r"[.]", str(version).split("-")[0].split(":")[-1])]


def _older(version, than):
    """Whether version is older than than: ("1.0.10", "1.1.0") -> True."""
    a, b = _numbers(version), _numbers(than)
    width = max(len(a), len(b))
    return a + [0] * (width - len(a)) < b + [0] * (width - len(b))


def cassandra_axonops_java_agent(series, java):
    """The Java agent package for Cassandra series on Java major java: ("50x", "17") ->
    "axon-cassandra5.0-agent-jdk17"; "" when AxonOps has none for them."""
    return JAVA_AGENTS.get(str(series), {}).get(str(java), "")


def _merged(base, extra):
    """base with extra merged in, dict by dict (extra wins); base is not changed."""
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merged(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def cassandra_axonops_problems(settings):
    """What is wrong with the role's settings (a dict of the cassandra_axonops_* values without their prefix, plus
    series and java: cassandra_version, cassandra_java_version), one line each, [] when nothing."""
    out = []
    if not settings.get("java_agent_package") and settings.get("series") is not None:
        series = str(settings["series"])
        agents = JAVA_AGENTS.get(series, {})
        if str(settings.get("java")) not in agents:
            out.append("AxonOps has no Java agent package for Cassandra %s on Java %s (it has: %s): set"
                       " cassandra_axonops_java_agent_package, or cassandra_java_version to one of them."
                       % (SERIES.get(series, series), settings.get("java"),
                          ", ".join("Java %s" % j for j in sorted(agents, key=int)) or "none"))
    port = str(settings.get("server_port", "")).strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        out.append("cassandra_axonops_server_port is %s: it must be a port number (1 to 65535)." % (port or '""'))
    if not isinstance(settings.get("ntp_hosts") or [], list):
        out.append("cassandra_axonops_ntp_hosts must be a list, e.g. [\"ntp1.example.com:123\"].")
    pinned = str(settings.get("java_agent_version") or "")
    if pinned and _older(pinned, MIN_JAVA_AGENT):
        out.append("cassandra_axonops_java_agent_version is %s: %s or later is needed (the first to ship"
                   " axonops-jvm.options, which cassandra-env.sh loads)." % (pinned, MIN_JAVA_AGENT))
    if not str(settings.get("server_host") or "").strip():
        out.append("cassandra_axonops_server_host is empty: set the AxonOps server's host name or address.")
    if not str(settings.get("org") or "").strip():
        out.append("cassandra_axonops_org is empty: set the organisation name the AxonOps server knows.")
    mode = str(settings.get("tls_mode") or "")
    if mode not in TLS_MODES:
        out.append("cassandra_axonops_tls_mode is %s: it must be one of %s." % (mode or '""', ", ".join(TLS_MODES)))
    if mode == "mTLS" and not (settings.get("tls_cert_file") and settings.get("tls_key_file")):
        out.append("cassandra_axonops_tls_mode mTLS needs cassandra_axonops_tls_cert_file and"
                   " cassandra_axonops_tls_key_file.")
    if not isinstance(settings.get("extra_settings") or {}, dict):
        out.append("cassandra_axonops_extra_settings must be a dict (sections of axon-agent.yml).")
    return out


def cassandra_axonops_config(settings):
    """The text of axon-agent.yml from the role's settings (a dict of the cassandra_axonops_* values without their
    prefix): the server, the agent's org, key and TLS, NTP, then extra_settings merged in (it wins)."""
    problems = cassandra_axonops_problems(settings)
    if problems:
        raise AnsibleFilterError(" ".join(problems))
    agent = {"org": str(settings["org"])}
    if settings.get("key"):
        agent["key"] = str(settings["key"])
    mode = str(settings["tls_mode"])
    tls = {"mode": mode}
    if mode != "disabled":
        tls["skipVerify"] = bool(settings.get("tls_skip_verify"))
        if settings.get("tls_ca_file"):
            tls["caFile"] = str(settings["tls_ca_file"])
    if mode == "mTLS":
        tls["certFile"] = str(settings["tls_cert_file"])
        tls["keyFile"] = str(settings["tls_key_file"])
    agent["tls"] = tls
    config = {"axon-server": {"hosts": str(settings["server_host"]), "port": int(str(settings["server_port"]).strip())},
              "axon-agent": agent}
    if settings.get("ntp_hosts"):
        config["NTP"] = {"hosts": [str(h) for h in settings["ntp_hosts"]]}
    config = _merged(config, settings.get("extra_settings") or {})
    return ("# Managed by Ansible (community.cassandra.cassandra_axonops)\n"
            + yaml.safe_dump(config, default_flow_style=False, sort_keys=False))


def _read(lines):
    """The node's read (files/read.sh's lines) -> ({package: version} of the ones in place, {package: dpkg state}
    of the ones left half done, {key: value})."""
    installed, unfinished, read = {}, {}, {}
    for line in lines or []:
        words = str(line).split()
        if not words:
            continue
        if words[0] == "package":
            if len(words) == 4 and words[2] in DPKG_DONE:
                installed[words[1]] = words[3]
            elif len(words) >= 3 and words[2] not in DPKG_GONE:
                unfinished[words[1]] = words[2]
        else:
            read[words[0]] = " ".join(words[1:])
    return installed, unfinished, read


def _service(active, enabled):
    """systemctl's words -> the plan's: ("inactive", "disabled") -> "stopped (inactive), not enabled at boot"."""
    if enabled in ("", "not-found"):
        return "not installed"
    state = "running" if active == "active" else "stopped (%s)" % (active or "unknown")
    return "%s, %s" % (state, "enabled at boot" if enabled == "enabled" else "not enabled at boot (%s)" % enabled)


def _differs(have, version):
    """Whether installed version have is not version wanted ("" = any): "1.1.1-1" is 1.1.1."""
    return not have or bool(version) and have != version and not have.startswith(version + "-")


def cassandra_axonops_changes(lines, wanted, config_sha1, user, group, server):
    """What the node needs. lines: files/read.sh's output; wanted: [[package, version ("" = any)]], axon-agent then
    the Java agent; config_sha1: the sha1 of the axon-agent.yml to write; user, group: Cassandra's account and group;
    server: "host:port, org" said for the config. -> {installed: {package: version}, removed: [the Java agent packages
    of another series or Java, installed], latest: [the packages to upgrade to the newest: a Java agent older than
    1.1.0, or without axonops-jvm.options], items: [{item, before, after}]}."""
    installed, unfinished, read = _read(lines)
    java_package = wanted[-1][0]
    removed = sorted(p for p in installed if p.startswith(("axon-cassandra", "axon-dse")) and p != java_package)
    items, latest = [], []
    for name, version in wanted:
        version = str(version or "")
        have = installed.get(name, "")
        if name in unfinished:
            items.append({"item": "package " + name, "before": "half installed (%s)" % unfinished[name],
                          "after": version or "installed (newest)"})
        elif _differs(have, version):
            items.append({"item": "package " + name, "before": have or "absent", "after": version or "installed (newest)"})
        elif name == java_package and not version and (_older(have, MIN_JAVA_AGENT) or read.get("options") == "no"):
            latest.append(name)
            why = "older than %s" % MIN_JAVA_AGENT if _older(have, MIN_JAVA_AGENT) else "no axonops-jvm.options"
            items.append({"item": "package " + name, "before": "%s (%s)" % (have, why),
                          "after": "newest (%s or later)" % MIN_JAVA_AGENT})
    items += [{"item": "package " + p, "before": installed[p], "after": "removed"} for p in removed]
    if "axonops" not in read.get("user_groups", "").split():
        items.append({"item": "%s in group axonops" % user, "before": "no", "after": "yes"})
    if group not in read.get("axonops_groups", "").split():
        items.append({"item": "axonops in group %s" % group, "before": "no", "after": "yes"})
    if read.get("config") != config_sha1:
        items.append({"item": "/etc/axonops/axon-agent.yml", "before": "absent" if "config" not in read
                      else "other settings", "after": "server " + server})
    elif read.get("config_stat", "root axonops 640") != "root axonops 640":
        items.append({"item": "/etc/axonops/axon-agent.yml owner, group, mode", "before": read["config_stat"],
                      "after": "root axonops 640"})
    active, enabled = read.get("active", ""), read.get("enabled", "")
    if (active, enabled) != ("active", "enabled"):
        items.append({"item": "service axon-agent", "before": _service(active, enabled),
                      "after": "running, enabled at boot"})
    return {"installed": installed, "removed": removed, "latest": latest, "items": items}


def cassandra_axonops_files(files, java_package, agent_version="", java_agent_version=""):
    """The package files of cassandra_axonops_package_files (paths) -> {wanted: [[package, version]] axon-agent then
    the Java agent, as the files give them; paths: [the files, axon-agent's first]; problems: [why they do not fit,
    one line each]}. Each file names its package and version (name_version_arch.deb,
    name-version-release.arch.rpm); one for axon-agent, one for java_package, pinned versions matching."""
    found, problems = {}, []
    for path in files or []:
        base = str(path).rsplit("/", 1)[-1]
        m = DEB_FILE.match(base) or RPM_FILE.match(base)
        if not m:
            problems.append("%s: not a package file name (name_version_arch.deb or name-version-release.arch.rpm)"
                            % base)
        elif m.group("name") not in ("axon-agent", java_package):
            problems.append("%s: package %s, neither axon-agent nor %s" % (base, m.group("name"), java_package))
        elif m.group("name") in found:
            problems.append("%s: a second file for %s" % (base, m.group("name")))
        else:
            found[m.group("name")] = (m.group("version"), str(path))
    for name, pinned in (("axon-agent", agent_version), (java_package, java_agent_version)):
        if name not in found:
            problems.append("no file for %s in cassandra_axonops_package_files" % name)
        elif str(pinned or "") and found[name][0] != str(pinned):
            problems.append("%s is %s in its file, the inventory pins %s" % (name, found[name][0], pinned))
        elif name == java_package and _older(found[name][0], MIN_JAVA_AGENT):
            problems.append("%s %s: %s or later is needed (it ships axonops-jvm.options)"
                            % (name, found[name][0], MIN_JAVA_AGENT))
    if problems:
        return {"wanted": [], "paths": [], "problems": problems}
    return {"wanted": [["axon-agent", found["axon-agent"][0]], [java_package, found[java_package][0]]],
            "paths": [found["axon-agent"][1], found[java_package][1]], "problems": []}


LOADER = re.compile(r"axonops-jvm[.]options|axon-(cassandra|dse)\S*[.]jar")


def cassandra_axonops_loader_refusals(options, keep_files):
    """Why cassandra_config could not load the AxonOps Java agent itself: options, the JVM options and the
    cassandra-env.sh snippet (one string); keep_files, cassandra_config_keep_files. [] when it can."""
    out = []
    if "cassandra-env.sh" in (keep_files or []):
        out.append("cassandra-env.sh is in cassandra_config_keep_files: the role could not add the line loading the"
                   " AxonOps Java agent there (or load the agent by hand and leave"
                   " cassandra_axonops_enabled false)")
    if LOADER.search(str(options or "")):
        out.append("the AxonOps Java agent is already loaded by cassandra_jvm_extra_options or your snippet of"
                   " cassandra-env.sh (cassandra_config_template_snippets): remove it there, the role writes the line"
                   " (cassandra_axonops_enabled)")
    return out


AXONOPS_STEP = "set up the AxonOps agent"
BEFORE = ("apply the repository, look for Cassandra ", "install Java ")  # done before it at the node's turn


def cassandra_apply_config_axonops_steps(steps, extras):
    """A node's steps in apply_config's plan, and its extras ([{section, ...}]) -> the steps with the AxonOps agent's
    set-up (section AxonOps) before the config is written (after the release's repository and the Java), and no
    "apply the ..." step for it alone."""
    steps = list(steps or [])
    sections = set(str(e.get("section")) for e in extras or [])
    if "AxonOps" not in sections or not steps:
        return steps
    if not sections & set(["Repository", "Packages", "Firewall"]):
        steps = [s for s in steps if s != "apply the repository, packages, firewall"]
    if not sections & set(["Packages", "Firewall"]):
        steps = [s for s in steps if s != "apply the packages, firewall"]
    at = next((i for i, s in enumerate(steps) if not s.startswith(BEFORE)), len(steps))
    return steps[:at] + [AXONOPS_STEP] + steps[at:]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_axonops_java_agent": cassandra_axonops_java_agent,
            "cassandra_axonops_config": cassandra_axonops_config,
            "cassandra_axonops_problems": cassandra_axonops_problems,
            "cassandra_axonops_changes": cassandra_axonops_changes,
            "cassandra_axonops_files": cassandra_axonops_files,
            "cassandra_axonops_loader_refusals": cassandra_axonops_loader_refusals,
            "cassandra_apply_config_axonops_steps": cassandra_apply_config_axonops_steps,
        }
