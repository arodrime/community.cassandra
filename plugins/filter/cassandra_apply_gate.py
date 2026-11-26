# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""apply_config's parts and its fast compare (docs/docsite/rst/guide_roles.rst, Changing the configuration).

cassandra_apply_only: -e apply_only -> the parts the run compares and applies, the ones it leaves out, or why it
refuses them.
cassandra_apply_only_stops: what a run restricted to some parts cannot leave out on its nodes (a newer release of
the series, the AxonOps agent cassandra-env.sh loads).
cassandra_config_user_files, cassandra_config_user_request: the files of the users' ~/.cassandra cassandra_config
writes, and its plan's request to compare them on the node.
cassandra_cqlsh_credential_refusals: the cqlsh credentials entries cassandra_config cannot write.
cassandra_check_groups: the nodes whose cassandra_config checks read the same values, checked once.
cassandra_apply_resume_refusal: a resume with other parts than the interrupted run's, refused.
cassandra_apply_gate_lines: the lines the report of the run says of the compare: the nodes whose config the
cassandra_config plan found unchanged (the rest of the role not run there), the parts left out.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the parts of a run, in the order the compare looks at them
PARTS = ("config", "unit", "java", "linux", "install", "repository", "medusa", "firewall", "axonops")
# compared in every run: the restart decision reads them (config: the files changed since the start; unit: written
# at every restart, as rolling_restart writes it)
ALWAYS = ("config", "unit")


def _names(value):
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def cassandra_apply_only(value, command=""):
    """value: -e apply_only (a comma-separated string, a list; '', None or all: every part); command: the command of
    this run (cassandra_command, without apply_only) -> {parts, left_out, refused}: the parts compared and applied
    (config and unit always), those left out, both in the order of PARTS; refused: the lines of the refusal ([] when
    every name is a part and config is among them), its way out the command with the parts given, config first.
    Example: "config,linux" -> {"parts": ["config", "unit", "linux"], "left_out": ["java", "install", ...],
    "refused": []}."""
    names = _names(value)
    if not names or names == ["all"]:  # (all: every part, as without apply_only; said so to resume a run)
        return {"parts": list(PARTS), "left_out": [], "refused": []}
    unknown = [n for n in names if n not in PARTS and n != "all"]
    # (the parts given that are parts, config first; all among them, in any case: all)
    given = "all" if any(n.lower() == "all" for n in names) else ",".join(
        ["config"] + [n for n in PARTS if n in names and n != "config"])
    command = command or "ansible-playbook community.cassandra.apply_config"
    way_out = ["WAY OUT  run:", "  %s -e apply_only=%s" % (command, given)]
    if unknown:
        refused = ["apply_only: not a part of apply_config:"] + ["  " + n for n in unknown] + ["its parts:"] + [
            "  " + p for p in PARTS] + ["  all (every part, given alone)"] + way_out
    elif "all" in names:
        refused = ["apply_only: all stands for every part: give it alone", "WAY OUT  run:",
                   "  %s -e apply_only=all" % (command or "ansible-playbook community.cassandra.apply_config")]
    elif "config" not in names:
        refused = ["apply_only: config is not among the parts given",
                   "config is always compared: the restart decision reads it"] + way_out
    else:
        refused = []
    parts = [p for p in PARTS if p in names or p in ALWAYS]
    return {"parts": parts, "left_out": [p for p in PARTS if p not in parts], "refused": refused}


def _on(value):
    return str(value).lower() in ("true", "yes", "1", "on")


def cassandra_apply_only_stops(names, hostvars, parts, command="", over=""):
    """names: the nodes of the run; hostvars: their variables (cassandra_apply_config_version: a newer release of the
    series to install when its kind is release; cassandra_axonops_enabled; cassandra_java_plan_todo: the node moves to
    another Java); parts: the run's (cassandra_apply_only); command: the command of this run, without apply_only;
    over: a resume's, the command that starts over (a resume keeps the interrupted run's parts) -> the lines of the
    refusal, [] when the parts cover them: the release is installed by install, cassandra-env.sh loads the AxonOps
    agent its part installs, the Java move is java's. Its way out: the command with those parts too."""
    needed = (("install", "a newer release of its series to install",
               lambda node: (node.get("cassandra_apply_config_version") or {}).get("kind") == "release"),
              ("axonops", "cassandra-env.sh loads the AxonOps agent", lambda node: _on(node.get("cassandra_axonops_enabled"))),
              ("java", "another Java to move to (cassandra_java_version)", lambda node: _on(node.get("cassandra_java_plan_todo"))))
    refused, more = [], []
    for part, why, needs in needed:
        nodes = [name for name in names if needs(hostvars[name])]
        if nodes and part not in parts:
            refused += ["apply_only leaves %s out; %s, on these nodes:" % (part, why), "  " + out.nodes(nodes)]
            more.append(part)
    if refused:
        with_them = ",".join(p for p in PARTS if p in parts or p in more)
        if over:
            refused += ["a resumed run keeps the parts of the interrupted one: start over",
                        "WAY OUT  start over with those parts too:", "  %s -e apply_only=%s" % (over, with_them)]
        else:
            refused += ["WAY OUT  run with those parts too:", "  %s -e apply_only=%s" % (
                command or "ansible-playbook community.cassandra.apply_config", with_them)]
    return refused


def cassandra_apply_resume_refusal(resumed, recorded, current, resume_command, over_command):
    """A resume (-e cassandra_rolling_resume=true) of apply_config with other parts than the interrupted run's ->
    the lines of its refusal, [] when it may go on. resumed: the progress file it resumes; recorded: the parts the
    interrupted run applied ('' not recorded; all: every part);
    current: this run's (all: every part, '': no -e apply_only given); resume_command: the command that resumes with
    the parts recorded (all when none); over_command: the one that starts over."""
    if not recorded:
        if current:  # (given with apply_only: the operator says which)
            return []
        return ["%s: no record of the parts of the interrupted run" % os.path.basename(resumed),
                "WAY OUT  resume with every part:", "  " + resume_command,
                "WAY OUT  or start over without resuming:", "  " + over_command]
    if recorded == (current or "all"):
        return []

    def said(parts):
        return "every part" if parts == "all" else "apply_only=" + parts
    return ["the interrupted run applied " + said(recorded), "this one: " + said(current or "all"),
            "a resumed run applies the parts the interrupted one did", "WAY OUT  resume with them:",
            "  " + resume_command]


def cassandra_apply_gate_lines(gates, left_out=None, full=False, verify=False):
    """gates: of each node, its _cassandra_apply_config_gate: the rest of cassandra_config not run ('unchanged'), or
    run ('role'); '' (not compared) left out; left_out: the parts left out (apply_only) -> the report's lines.
    Example: ["unchanged", "role"] -> ["config: 1 of 2 nodes found unchanged by its plan, 1 compared by the role"]."""
    gates = [g for g in gates or [] if g]
    lines = []
    if gates:
        if full:
            lines.append("config: every node compared by the role (apply_full)")
        else:
            unchanged = gates.count("unchanged")
            lines.append("config: %d of %d nodes found unchanged by its plan, %d compared by the role"
                         % (unchanged, len(gates), len(gates) - unchanged))
        if verify:
            lines.append("apply_verify: the role compared them too, its facts the same")
    if left_out:
        lines += ["not checked (apply_only):"] + ["  " + p for p in left_out]
    return lines


def cassandra_check_groups(names, hostvars):
    """names: the nodes of the play; hostvars: their variables (_cassandra_config_check_key: the sha1 of the values
    cassandra_config's checks read) -> {first node: [the nodes with its key]}, in the order of names; a node without
    a key in a group of its own."""
    first, groups = {}, {}
    for name in names:
        key = hostvars[name].get("_cassandra_config_check_key") or "(none) " + name
        groups.setdefault(first.setdefault(key, name), []).append(name)
    return groups


def cassandra_config_user_files(credentials, series, nodetool_users):
    """cassandra_cqlsh_credentials, cassandra_version, the users whose nodetool gets the JMX TLS settings -> the files
    cassandra_config writes in their ~/.cassandra: [{user, name, template, item}] (item: the credential, for its
    template), as access.yml writes them."""
    out = []
    for cred in credentials or []:
        out.append({"user": cred["os_user"], "name": "cqlshrc", "template": "cqlshrc.j2", "item": cred})
        if series != "40x":
            out.append({"user": cred["os_user"], "name": "credentials", "template": "cqlsh_credentials.j2", "item": cred})
    for user in nodetool_users or []:
        out.append({"user": user, "name": "nodetool-ssl.properties", "template": "nodetool-ssl.properties.j2", "item": {}})
    return out


def cassandra_config_user_request(files, texts):
    """The files (cassandra_config_user_files) and their texts, rendered in that order -> the users step's request of
    cassandra_config_read.py: [{user, files: [{name, text}]}], a user once, in the order of files."""
    users = {}
    order = []
    for f, text in zip(files, texts):
        if f["user"] not in users:
            users[f["user"]] = []
            order.append(f["user"])
        users[f["user"]].append({"name": f["name"], "text": text})
    return [{"user": u, "files": users[u]} for u in order]


def cassandra_cqlsh_credential_refusals(credentials):
    """cassandra_cqlsh_credentials -> a line for each entry cassandra_config cannot write (no os_user, username or
    password: by its place and its os_user, never its password), [] when it can write them all."""
    out = []
    for index, entry in enumerate(credentials or [], 1):
        entry = entry if isinstance(entry, dict) else {}
        missing = [k for k in ("os_user", "username", "password") if entry.get(k) in (None, "")]
        if missing:
            out.append("  entry %d (%s): no %s" % (index, entry.get("os_user") or "no os_user", ", no ".join(missing)))
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_only": cassandra_apply_only,
            "cassandra_apply_only_stops": cassandra_apply_only_stops,
            "cassandra_apply_gate_lines": cassandra_apply_gate_lines,
            "cassandra_apply_resume_refusal": cassandra_apply_resume_refusal,
            "cassandra_check_groups": cassandra_check_groups,
            "cassandra_config_user_files": cassandra_config_user_files,
            "cassandra_cqlsh_credential_refusals": cassandra_cqlsh_credential_refusals,
            "cassandra_config_user_request": cassandra_config_user_request,
        }
