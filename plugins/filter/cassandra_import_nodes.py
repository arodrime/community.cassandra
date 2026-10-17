# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The nodes of an import (import_cluster), from what was read on them.

cassandra_import_host_vars: hostvars, host names -> what the import read on
    each host (its facts, its import_cluster_* variables), read once.
cassandra_import_nodes: the ring (cassandra_ring_nodes), that and the given
    nodes -> one entry per ring node for cassandra_inventory_layout: the host
    read at its address, its name, what of its install, Java, unit... the
    inventory carries, what stays as it is on it, its notes.

Each host's variables are read once (hostvars[host] works its variables out
anew at each access: in a template, per node and per setting, the import of a
large cluster took hours).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    CARRIED, CONFIG_FILE, keep_files_option, values_hidden)  # (CONFIG_FILE: the report's too)
from ansible_collections.community.cassandra.plugins.filter.cassandra_java import cassandra_java_supported
from ansible_collections.community.cassandra.plugins.filter import cassandra_node_standing as standing

# the variables of a host the import reads (its facts, and what the play read on it)
KEYS = ("ansible_facts", "ansible_host", "import_cluster_address", "import_cluster_config",
        "import_cluster_conf_dir", "import_cluster_dsbulk", "import_cluster_error", "import_cluster_held",
        "import_cluster_java", "import_cluster_java_bin", "import_cluster_java_home", "import_cluster_java_package",
        "import_cluster_jmx_slurp", "import_cluster_jvm_cmdline",
        "import_cluster_keep", "import_cluster_log_dir", "import_cluster_medusa",
        "import_cluster_medusa_ini", "import_cluster_newer_files", "import_cluster_os", "import_cluster_package",
        "import_cluster_package_from_file", "import_cluster_packages", "import_cluster_perms", "import_cluster_repo",
        "import_cluster_repos_answer", "import_cluster_running", "import_cluster_series", "import_cluster_slurp",
        "import_cluster_storage_dir", "import_cluster_system_java", "import_cluster_unit",
        "import_cluster_unit_slurp", "import_cluster_version")
DSBULK = re.compile(r"^/usr/share/dsbulk-([0-9]+(?:[.][0-9]+)*)/bin/dsbulk$")
NOT_READ_KEEP = {"cassandra_repository_manage": False, "cassandra_linux_manage": False,
                 "cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False,
                 "cassandra_java_set_default": False, "cassandra_firewall_manage": False,
                 "cassandra_install_tools": False, "cassandra_install_jemalloc": False,
                 "cassandra_package_hold": False, "cassandra_dsbulk_install": False}


def _bool(value):
    """Ansible's bool filter (as the playbook's | bool: not convert_bool's, which takes y and t too)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.lower()
    return value in ("yes", "on", "1", "true", 1)


def _d(value):
    """A dict, {} for none."""
    return value if isinstance(value, dict) else {}


def cassandra_import_host_vars(hostvars, hosts):
    """hostvars, host names -> {host: {variable: value}} with KEYS only, each host's
    variables worked out once; a name that is not a host is left out.
    e.g. (hostvars, ["db1", "nohost"]) -> {"db1": {"ansible_host": "10.0.0.1", "import_cluster_version": "4.1.5",
    ...}}."""
    out = {}
    for h in hosts:
        if h in out or h not in hostvars:
            continue
        hv = hostvars[h]
        out[h] = dict((k, hv[k]) for k in KEYS if k in hv)
    return out


def _match(address, data, ordered):
    """The host the ring node at address is: the first one whose machine has that address (its interfaces' when
    known, else the address it was reached at), else the address."""
    for h in ordered:
        hv = data.get(h, {})
        f = _d(hv.get("ansible_facts"))
        addrs = list(f.get("all_ipv4_addresses") or []) + list(f.get("all_ipv6_addresses") or []) + [
            _d(f.get("default_ipv4")).get("address", ""), _d(f.get("default_ipv6")).get("address", "")]
        if "all_ipv4_addresses" not in f:
            addrs += [hv.get("ansible_host", ""), hv.get("import_cluster_address", "")]
        if re.match(r"^[0-9.]+$|.*:", str(hv.get("ansible_host", ""))):
            addrs.append(hv["ansible_host"])
        if address in addrs:
            return h
    return address


def _name(address, host, facts, given, host_names):
    """The node's inventory name: its address (host_names ip, or a host without facts not given), else the fact
    host_names (hostname, fqdn) or the host."""
    if host_names == "ip" or ("hostname" not in facts and host not in given):
        return address
    return facts.get(host_names) or host


def _java_vars(hv, facts, read, series, java_default):
    """The Java variables of a node read: its Java when not the series' default, its home and package when not the
    roles' ones, its support -> {variable: value}."""
    java = hv.get("import_cluster_java")
    os_family = facts.get("os_family", "")
    java_text = hv.get("import_cluster_java", "")
    java_package = ("openjdk-%s-jre-headless" % java_text if os_family == "Debian"
                    else "java-%s-amazon-corretto-headless" % java_text if facts.get("distribution", "") == "Amazon"
                    else "java-%s-openjdk-headless" % java_text)
    java_vars = {}
    if read and java != java_default.get(series, ""):
        java_vars["cassandra_java_version"] = java
    if read and hv.get("import_cluster_java_home", "") != "":
        java_vars["cassandra_java_home"] = hv["import_cluster_java_home"]
    if read and hv.get("import_cluster_java_package", "") not in ("", java_package):
        java_vars["cassandra_java_package"] = hv["import_cluster_java_package"]
    if read and hv.get("import_cluster_java_home", "") == "":
        java_vars["cassandra_java_tarballs"] = {}
    supported = cassandra_java_supported(series)
    if read and supported and java not in supported:
        java_vars["cassandra_java_allow_unsupported"] = True
    return java_vars


def _package_vars(hv, read, repo, package):
    """The install variables of a node read: its package version and method, its repository's -> {variable: value}."""
    pkg = {}
    if read and package:
        pkg["cassandra_package_version"] = re.sub(r"^([0-9]+\.[0-9]+\.[0-9]+).*$", r"\1", str(package))
    if read and package and _bool(hv.get("import_cluster_package_from_file", False)):
        pkg["cassandra_install_method"] = "packages"
    if read:
        pkg.update(repo["vars"])
    return pkg


def _hand_kept(config, read, keep_hand_edits):
    """The config files left as they are on the node: the hand-edited ones keep_hand_edits names, its jvm<N> files
    not imported -> {"cassandra_config_keep_files": [file]}, {} for none."""
    hand_kept = []
    for line in (config.get("hand_edits", []) if read else []):
        f = re.sub(r"[:,].*$", "", str(line))
        if CONFIG_FILE.match(f) and f not in hand_kept:
            hand_kept.append(f)
    # keep_hand_edits: all the files with hand edits (True), or the ones named
    hand_kept = [f for f in hand_kept if keep_hand_edits is True or f in (keep_hand_edits or [])]
    # a jvm<N> file its Java does not read, whose options are not imported: left as it is on the node
    if read:
        hand_kept += [f for f in config.get("keep_files") or [] if f not in hand_kept]
    return {"cassandra_config_keep_files": hand_kept} if hand_kept else {}


def _installed(hv, read, package, os_family):
    """What of the package install the roles leave as it is (tools, jemalloc, hold, DSBulk) or carry (the DSBulk
    version) -> {variable: value}."""
    pkgs = hv.get("import_cluster_packages", {})
    installed = {}
    if read and package:
        if "cassandra-tools" not in pkgs:
            installed["cassandra_install_tools"] = False
        if ("libjemalloc2" if os_family == "Debian" else "jemalloc") not in pkgs:
            installed["cassandra_install_jemalloc"] = False
        held = hv.get("import_cluster_held", [])
        if os_family == "Debian" and [p for p in ("cassandra", "cassandra-tools") if p in pkgs and p not in held]:
            installed["cassandra_package_hold"] = False
    dsbulk = DSBULK.match(str(hv.get("import_cluster_dsbulk", "")))
    if read and dsbulk:
        installed["cassandra_dsbulk_version"] = dsbulk.group(1)
    elif read:
        installed["cassandra_dsbulk_install"] = False
    return installed


def _unit_env(hv, read, env):
    """The unit's Environment carried (its CASSANDRA_LOG_DIR left out when it is the log directory) -> {name: value}."""
    if not read:
        return {}
    drop = "CASSANDRA_LOG_DIR" if env.get("CASSANDRA_LOG_DIR", "") == hv.get("import_cluster_log_dir", "") else ""
    return dict((k, v) for k, v in env.items() if k != drop)


def _place_vars(hv, read, unit, unit_env, os_family):
    """The directories and the unit's settings that are not the roles' defaults -> {variable: value}."""
    default_conf_dirs = (["/etc/cassandra/conf"] if os_family == "RedHat" else ["/etc/cassandra"] if os_family != ""
                         else ["/etc/cassandra", "/etc/cassandra/conf"])
    conf = {}
    if read and hv.get("import_cluster_conf_dir") not in default_conf_dirs:
        conf["cassandra_conf_dir"] = hv.get("import_cluster_conf_dir")
    if read and hv.get("import_cluster_log_dir", "") not in ("", "/var/log/cassandra"):
        conf["cassandra_log_dir"] = hv["import_cluster_log_dir"]
    if read and unit.get("restart", "") not in ("", "no"):
        conf["cassandra_service_restart"] = unit["restart"]
    if read and unit.get("boot", "") == "disabled":
        conf["cassandra_service_enabled"] = False
    if unit_env:
        conf["cassandra_service_environment"] = unit_env
    return conf


def _env_log_dir(read, keep, env, conf, config_vars):
    """The log directory of cassandra.yaml's variables, kept when the unit is kept and its CASSANDRA_LOG_DIR set while
    the directory found is another -> {"cassandra_log_dir": directory}, {} when not."""
    log_dir = config_vars.get("cassandra_log_dir", "/var/log/cassandra")
    return ({"cassandra_log_dir": log_dir}
            if read and "cassandra_service_unit_manage" in keep and env.get("CASSANDRA_LOG_DIR", "") != ""
            and conf.get("cassandra_log_dir", "") not in ("", log_dir) else {})


def _java_link(hv, read):
    """/usr/bin/java left as it is when it is another Java than the running one -> {"cassandra_java_set_default":
    False}, {} when not."""
    java_bin, system_java = hv.get("import_cluster_java_bin", ""), hv.get("import_cluster_system_java", "")
    return ({"cassandra_java_set_default": False}
            if read and java_bin != "" and system_java != "" and system_java != java_bin else {})


def _merged(first, *more):
    """A copy of the dict first updated with each of more in turn -> dict."""
    out = dict(first)
    for one in more:
        out.update(one)
    return out


def _versions_note(hv, java_link):
    """The note on the versions: Cassandra's, Java's (its home when not a package, its /usr/bin/java) -> text."""
    java_bin, system_java = hv.get("import_cluster_java_bin", ""), hv.get("import_cluster_system_java", "")
    return "Cassandra %s, Java %s%s%s" % (
        hv.get("import_cluster_version", "?"), hv.get("import_cluster_java", "?"),
        (" (not a package: %s)" % hv["import_cluster_java_home"]) if hv.get("import_cluster_java_home", "") else "",
        (", /usr/bin/java is %s: left as it is on this node (new nodes get the running Java as their system java)"
         % hv.get("import_cluster_system_java")) if java_link
        else ", /usr/bin/java is missing: the roles will point it at the running Java"
        if java_bin != "" and system_java == "" else "")


def _started_note(unit, keep, unit_env):
    """The note on how Cassandra is started: its unit, restart policy, boot, what becomes of it -> text."""
    return "started by %s, Restart=%s, at boot: %s%s%s%s)" % (
        unit.get("file", "") or "no systemd unit", unit.get("restart", "") or "?", unit.get("boot", "") or "?",
        " (kept on this node; new nodes get the unit of cassandra_service, with the restart policy"
        if "cassandra_service_unit_manage" in keep
        else " (cassandra_service replaces it with its own unit, keeping the restart policy",
        (", its Environment %s as cassandra_service_environment" % ", ".join(unit_env)) if unit_env else "",
        (", NOT its EnvironmentFile %s: put what it sets in cassandra_service_environment" % unit["env_files"])
        if unit.get("env_files", "") else "")


def _install_note(hv, package):
    """The note on the install: its package (installed from a file?), or not a package -> text."""
    return (("package %s%s" % (package, " installed from a file, no configured repository offers it"
                                        " (cassandra_install_method: packages; set cassandra_install_url, the"
                                        " directory of the package files, for new nodes)"
                               if _bool(hv.get("import_cluster_package_from_file", False)) else ""))
            if package else "NOT a package install: cassandra_install would install the package next to it")


def _reason(item, hv):
    """Why the node is not read (when it is not), in the words of where a node stands -> text."""
    running = _d(hv.get("import_cluster_running"))
    if "rc" not in running:
        return "unreachable"
    if running["rc"] != 0:
        # in the words of where a node stands (cassandra_node_standing)
        return (standing.WORDS["member_down"] if str(item.get("state", "")).startswith("D")
                else "Cassandra stopped there, %s in the ring (not seen down yet)" % item.get("state", "?"))
    if "import_cluster_error" in hv:
        return hv["import_cluster_error"]
    return "unsupported version %s" % hv["import_cluster_version"]


def _notes(item, host, hv, read, config, perms, medusa, repo, set_ansible_host, first):
    """The node's notes: first (its versions, install, unit), its permissions', Medusa's, the lines carried, how it
    is reached, its repository, its JVM's, its files newer than Cassandra -> [text]."""
    address = item["address"]
    notes = list(first)
    notes += list(perms.get("notes", [])) + list(medusa.get("notes", []))
    notes += [CARRIED + line for line in (config.get("carried", []) if read else [])]
    if host != address and hv.get("ansible_host", address) != address and set_ansible_host:
        notes.append("reached at %s, its hosts file has its ring address %s (import_cluster_set_ansible_host: false"
                     " leaves ansible_host out)" % (hv.get("ansible_host"), address))
    if read and not repo["manage"]:
        notes.append("Cassandra repository files left as they are: %s" % repo["why"])
    if read and not _bool(hv.get("import_cluster_repos_answer", True)):
        notes.append("no repository answered the package query (an air-gapped host?): set cassandra_offline: true"
                     " (group_vars) unless the roles may download from the mirror")
    if read:
        notes += list(config.get("jvm_notes") or [])
    if read and hv.get("import_cluster_newer_files", []):
        notes.append("%s written after Cassandra started: it may not run what they say. apply_config does not restart"
                     " it for them; rolling_restart applies them" % ", ".join(hv["import_cluster_newer_files"]))
    return notes


def _node(item, host, hv, given, jmx, java_default, host_names, set_ansible_host, keep_hand_edits):
    """The entry of the ring node item, read on host (hv: its variables, {} when none)."""
    facts = _d(hv.get("ansible_facts"))
    read = "import_cluster_config" in hv
    config = _d(hv.get("import_cluster_config"))
    unit = _d(hv.get("import_cluster_unit"))
    keep = _d(hv.get("import_cluster_keep"))
    medusa = _d(hv.get("import_cluster_medusa"))
    perms = _d(hv.get("import_cluster_perms"))
    series = hv.get("import_cluster_series")
    os_family = facts.get("os_family", "")
    address = item["address"]
    name = _name(address, host, facts, given, host_names)
    java_vars = _java_vars(hv, facts, read, series, java_default)
    repo = hv.get("import_cluster_repo", {"manage": True, "vars": {}, "why": ""})
    package = hv.get("import_cluster_package", "")
    pkg = _package_vars(hv, read, repo, package)
    hand_kept = _hand_kept(config, read, keep_hand_edits)
    installed = _installed(hv, read, package, os_family)
    env = _d(unit.get("env"))
    unit_env = _unit_env(hv, read, env)
    conf = _place_vars(hv, read, unit, unit_env, os_family)

    config_vars = _d(config.get("vars"))
    env_log_dir = _env_log_dir(read, keep, env, conf, config_vars)
    java_link = _java_link(hv, read)
    versions = _versions_note(hv, java_link)
    started = _started_note(unit, keep, unit_env)
    install = _install_note(hv, package)
    reason = _reason(item, hv)

    if read:
        tuning = _d(hv.get("import_cluster_os"))
        node_vars = _merged(config_vars, {"cassandra_version": series}, java_vars, pkg, conf, jmx,
                            _d(perms.get("vars")), _d(medusa.get("vars")), _d(tuning.get("vars")))
        node_keep = _merged(keep, env_log_dir, java_link, {"cassandra_firewall_manage": False}, _d(medusa.get("keep")),
                            _d(tuning.get("keep")), {} if repo["manage"] else {"cassandra_repository_manage": False},
                            hand_kept, installed)
    else:
        node_vars, node_keep = {}, dict(NOT_READ_KEEP)

    first = ["%s, %s, config in %s" % (versions, install, hv.get("import_cluster_conf_dir", "?")), started]
    notes = _notes(item, host, hv, read, config, perms, medusa, repo, set_ansible_host, first)

    return {
        "name": name,
        "address": address,
        "host": host,
        "hostname": facts.get("hostname", ""),
        # the address from the ring, whatever name the host was given or found by
        "ansible_host": address if set_ansible_host else "",
        "dc": item["dc"],
        "rack": item["rack"],
        "read": read,
        "reason": reason,
        "vars": node_vars,
        # the OS tuning found on it, for the report
        "os": hv.get("import_cluster_os", {}) if read else {},
        # what of Medusa was read: its medusa.ini (imported), its install (its version)
        "medusa": {
            "ini": read and "content" in _d(hv.get("import_cluster_medusa_ini"))
            and not [n for n in medusa.get("notes", []) if "NOT imported" in str(n)],
            "install": read and "cassandra_medusa_version" in _d(medusa.get("vars")),
        },
        # a node not read is an existing node too: nothing of it is taken over. The firewall (or none) is the
        # node's: no mark tells one the role set up
        "keep": node_keep,
        "hand_edits": config.get("hand_edits", []),
        "normalized": config.get("normalized", []),
        "comments": config.get("comments", []),
        # its lines the import made that refer to a secret's variable (written as templates)
        "trusted": config.get("trusted", []) if read else [],
        # cassandra.yaml keys its Cassandra rejects: not imported, the node would not restart with them
        "not_settings": config.get("not_settings") or {},
        # JVM options worth a look (why, the way out): in the report's TO DO
        "jvm_warnings": list(config.get("jvm_warnings") or []) if read else [],
        # cassandra-env.sh's hand edits moved to variables, the files rewritten then, the options the JVM does not use
        "moved": config.get("moved", []),
        "rewritten": config.get("rewritten", []),
        "cleanup": config.get("cleanup", []),
        "not_moved": config.get("not_moved", ""),
        "notes": notes,
    }


@values_hidden
def cassandra_import_nodes(ring, data, given, found=None, jmx=None, java_default=None, host_names="hostname",
                           set_ansible_host=True, keep_hand_edits=False):
    """ring: cassandra_ring_nodes; data: cassandra_import_host_vars of the given
    and found hosts (and of the ring addresses that are hosts); given, found:
    the given hosts, the ones found in the ring; jmx: the JMX login given for
    the import; java_default: {series: the Java the roles default to};
    host_names: hostname, fqdn or ip; set_ansible_host, keep_hand_edits: the
    import_cluster_ options (keep_hand_edits: a bool, or the config
    files to keep) -> [node] for cassandra_inventory_layout.
    e.g. -> [{"name": "db1", "address": "10.0.0.1", "dc": "dc1", "rack": "r1", "read": True, "reason": ...,
    "vars": {"cassandra_version": "41x", ...}, "keep": {"cassandra_firewall_manage": False, ...}, "notes": [...],
    ...}]."""
    ordered = list(given) + list(found or [])
    keep_files = keep_files_option(keep_hand_edits)
    out = []
    for item in ring:
        host = _match(item["address"], data, ordered)
        error = None
        try:
            out.append(_node(item, host, data.get(host, {}), given, jmx or {}, java_default or {}, host_names,
                             _bool(set_ansible_host), keep_files))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            error = AnsibleFilterError("cassandra_import_nodes: %s: %s" % (item.get("address"), type(exc).__name__))
        if error:
            raise error  # (out of the except: not chained to its message, which may show a value)
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_import_host_vars": cassandra_import_host_vars,
            "cassandra_import_nodes": cassandra_import_nodes,
        }
