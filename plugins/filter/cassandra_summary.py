# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The ends of the node operations, as the other recaps (plugin_utils cassandra_output recap).

cassandra_op_recap: [{node, result}] (each node's cassandra_op_result) ->
    "DONE  rolling_restart  my_cluster  3 restarted", the nodes grouped by outcome.
cassandra_differs: {node: value} of a setting that must be the same on every
    node -> why the run stops, and where to fix it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# cassandra_service_node_action (the first word of a result) -> its playbook
PLAYBOOKS = {"add": "add_node", "decommission": "decommission_node", "restart": "rolling_restart",
             "reboot": "rolling_reboot", "replace": "replace_node", "move": "move_node", "update_java": "update_java",
             "upgrade": "upgrade", "upgradesstables": "upgrade", "cleanup": "cleanup", "clean": "cleanup",
             "apply_config": "apply_config", "update": "update_java", "apply": "apply_config",  # (the last two: would ...)
             "sync_clock": "sync_clocks", "clock_start": "sync_clocks", "clock_step": "sync_clocks",
             "clock_start_step": "sync_clocks", "clock_slew": "sync_clocks", "clock_slewing": "sync_clocks",
             "clock_start_slewing": "sync_clocks", "clock_step_slewing": "sync_clocks", "clock_start_step_slewing": "sync_clocks"}
# the same action, done
DONE = {"add": "added", "decommission": "decommissioned", "restart": "restarted", "reboot": "rebooted",
        "replace": "replaced", "move": "moved", "update_java": "Java updated", "upgrade": "upgraded",
        "upgradesstables": "SSTables upgraded", "cleanup": "cleaned up", "apply_config": "applied",
        "sync_clock": "clock set in a restart window", "clock_start": "time sync started", "clock_step": "clock stepped",
        "clock_start_step": "time sync started, clock stepped", "clock_slew": "clock corrected by its daemon",
        "clock_slewing": "clock being corrected by its daemon (minutes)",
        "clock_start_slewing": "time sync started, clock being corrected by it (minutes)",
        "clock_step_slewing": "clock stepped, being corrected by its daemon (minutes)",
        "clock_start_step_slewing": "time sync started, clock stepped, being corrected (minutes)"}

# the texts node_operation.yml, restart_batch.yml, cleanup_batch.yml and the playbooks record
_DONE = re.compile(r"^(?P<action>\w+) done(?P<how> with its rack)? in (?P<seconds>\d+)s$")
_WOULD = re.compile(r"^would (?P<what>(?P<action>\w+).*?)(?: \(--check: nothing was changed\))?$")
_FAILED = re.compile(r"^(?P<action>\w+) FAILED(?: after (?P<seconds>\d+)s)?:?\s*(?P<reason>.*)$", re.S)
_REFUSED = re.compile(r"^(?P<action>\w+) refused: (?P<reason>.*)$", re.S)
_SKIPPED = re.compile(r"^(?P<action>\w+) (?:skipped, |(?P<unfinished>not finished): )(?P<reason>.*)$")


def outcome(node, result):
    """A node's cassandra_op_result -> (the recap's outcome, its action or "")."""
    text = " ".join(str(result or "").split())
    m = _DONE.match(text)
    if m:
        return ({"node": node, "outcome": DONE.get(m.group("action"), m.group("action") + " done") + (m.group("how") or ""),
                 "seconds": int(m.group("seconds"))}, m.group("action"))
    m = _WOULD.match(text)
    if m:
        action = "clean" if m.group("what").startswith("clean up") else m.group("action")
        return {"node": node, "said": "would " + m.group("what")}, action
    m = _FAILED.match(text)
    if m:
        return ({"node": node, "status": "failed", "reason": m.group("reason").strip(),
                 "seconds": int(m.group("seconds")) if m.group("seconds") else None}, m.group("action"))
    m = _REFUSED.match(text)
    if m:
        return {"node": node, "status": "failed", "reason": "refused: " + m.group("reason").strip()}, m.group("action")
    m = _SKIPPED.match(text)
    if m and m.group("action") in PLAYBOOKS:  # done by the run before; a node of a batch stopped midway: said as such
        said = ("not finished (%s)" % m.group("reason")) if m.group("unfinished") else m.group("reason")
        return {"node": node, "said": said}, m.group("action")
    # not reached, not in this run (--limit), already in the ring / on that version, nothing to apply...
    reason = {"not in this run (--limit)": "left out by --limit"}.get(text, text or "not reached")
    return {"node": node, "status": "skipped", "reason": reason}, ""


def cassandra_op_recap(results, check=False, cluster="", operation="", action=""):
    """results: [{node, result}] in the order of the run, result each node's
    cassandra_op_result. The recap of plugin_utils cassandra_output: the verdict (DONE,
    FAILED, CHECK), then the nodes grouped by outcome, their times.
    operation: the playbook's name, else from action (the
    cassandra_service_node_action of the run), else from the actions
    recorded.
    -> lines: "rolling_restart done in 100s" on
    node4, 110s on node5 -> ["DONE  rolling_restart  my_cluster  2 restarted", "  node4, node5  restarted  (1m40s..1m50s
    each)"]."""
    outcomes, actions = [], []
    for item in results or []:
        said, done = outcome(item.get("node"), item.get("result"))
        outcomes.append(said)
        if done:
            actions.append(done)
    actions = ([action] if action else []) + actions
    operation = operation or (PLAYBOOKS.get(actions[0], actions[0]) if actions else "")
    return out.recap(operation, str(cluster or ""), outcomes, check=check)


def cassandra_differs(values, setting, group=""):
    """values: {node: value} of a setting that must be the same on every node
    (preflight). Why the run stops, the values by nodes, where to fix it.
    -> the lines that stop the run: why, then each value with its nodes."""
    by_value, shown_as = {}, {}
    for node, value in (values or {}).items():
        by_value.setdefault(str(value), []).append(node)  # as the assert compares them
        shown_as[str(value)] = (", ".join(str(v) for v in value) if isinstance(value, (list, tuple))
                                else str(value) if str(value) else '""')
    ranked = sorted(by_value.items(), key=lambda kv: (-len(kv[1]), out.nodes(kv[1])))
    shown = "; ".join("%s: %s" % (out.nodes(names), shown_as[value] if value != "None" else "(none)")
                      for value, names in ranked)
    where = "group_vars/%s" % group if group else "the cluster's group_vars"
    others = sorted((n for value, names in ranked[1:] for n in names),
                    key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    hosts = ", ".join("host_vars/%s" % n for n in others[:3]) + (", ..." if len(others) > 3 else "")
    if setting == "_cassandra_service_seeds":
        what = "The seed list (cassandra_seeds) is not the same on every node%s (%s)" % (
            " of %s" % group if group else "", shown)
        fix = ("Set cassandra_seeds once in %s (or leave it out: the collection picks the seeds), and remove it "
               "from %s." % (where, hosts or "the host_vars"))
    else:
        what = "%s is not the same on every node%s (%s)" % (setting, " of %s" % group if group else "", shown)
        fix = "Set it once in %s, and remove it from %s." % (where, hosts or "the host_vars")
    return "%s: every node needs the same. %s Nothing was changed." % (what, fix)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_op_recap": cassandra_op_recap,
            "cassandra_differs": cassandra_differs,
        }
