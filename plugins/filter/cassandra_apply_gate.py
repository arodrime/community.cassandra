# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""apply_config's parts and its fast compare (docs/docsite/rst/guide_roles.rst, Changing the configuration).

cassandra_apply_only: -e apply_only -> the parts the run compares and applies, the ones it leaves out, or why it
refuses them.
cassandra_apply_only_stops: what a run restricted to some parts cannot leave out on its nodes (a newer release of
the series, the AxonOps agent cassandra-env.sh loads).
cassandra_check_groups: the nodes whose cassandra_config checks read the same values, checked once.
cassandra_apply_gate_lines: the lines the report of the run says of the compare: the nodes whose config the
cassandra_config plan found unchanged (the rest of the role not run there), the parts left out.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import textwrap

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


def cassandra_apply_only(value):
    """value: -e apply_only (a comma-separated string, a list; '' or None: every part) -> {parts, left_out, refused}:
    the parts compared and applied (config and unit always), those left out, both in the order of PARTS; refused:
    the lines of the refusal ([] when every name is a part and config is among them).
    Example: "config,linux" -> {"parts": ["config", "unit", "linux"], "left_out": ["java", "install", ...],
    "refused": []}."""
    names = _names(value)
    if not names:
        return {"parts": list(PARTS), "left_out": [], "refused": []}
    unknown = [n for n in names if n not in PARTS]
    # (the way out: the names given that are parts, config first)
    way_out = "WAY OUT  -e apply_only=%s" % ",".join(["config"] + [n for n in PARTS if n in names and n != "config"])
    if unknown:
        refused = ["apply_only: %s: not a part of apply_config" % ", ".join(unknown),
                   "its parts: %s" % ", ".join(PARTS), way_out]
    elif "config" not in names:
        refused = ["apply_only: %s: config is not among them" % ", ".join(names),
                   "config is always compared: the restart decision reads it", way_out]
    else:
        refused = []
    parts = [p for p in PARTS if p in names or p in ALWAYS]
    return {"parts": parts, "left_out": [p for p in PARTS if p not in parts], "refused": refused}


def cassandra_apply_only_stops(names, hostvars, parts):
    """names: the nodes of the run; hostvars: their variables (cassandra_apply_config_version: a newer release of the
    series to install when its kind is release; cassandra_axonops_enabled); parts: the run's (cassandra_apply_only) ->
    the lines of the refusal, [] when the parts cover them: the release is installed by install, and cassandra-env.sh
    loads the AxonOps agent its part installs."""
    release, axonops = [], []
    for name in names:
        node = hostvars[name]
        if (node.get("cassandra_apply_config_version") or {}).get("kind") == "release":
            release.append(name)
        if str(node.get("cassandra_axonops_enabled", False)).lower() in ("true", "yes", "1", "on"):
            axonops.append(name)
    refused = []
    if release and "install" not in parts:
        refused += ["%s: a newer release of its series to install: apply_only leaves install out" % ", ".join(release),
                    "WAY OUT  add install to apply_only, or run without apply_only"]
    if axonops and "axonops" not in parts:
        refused += ["%s: cassandra-env.sh loads the AxonOps agent: apply_only leaves axonops out" % ", ".join(axonops),
                    "WAY OUT  add axonops to apply_only, or run without apply_only"]
    return refused


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
        lines += textwrap.wrap("not checked (apply_only): %s" % ", ".join(left_out), 79, subsequent_indent="  ",
                               break_long_words=False, break_on_hyphens=False)
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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_only": cassandra_apply_only,
            "cassandra_apply_only_stops": cassandra_apply_only_stops,
            "cassandra_apply_gate_lines": cassandra_apply_gate_lines,
            "cassandra_check_groups": cassandra_check_groups,
        }
