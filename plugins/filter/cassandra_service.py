# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What the cassandra_service role works out and says, kept out of its task files.

cassandra_service_changes: the change report items of the role (the unit, the drop-in of the package's init
script, the service, its start at boot).
cassandra_service_account_refusal: why the account Cassandra runs as is refused (account_check.yml).
cassandra_service_other_seeds: the entries of cassandra_seeds that are not this node."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_topology import cassandra_seed_host

UNIT = "/etc/systemd/system/cassandra.service"


def cassandra_service_changes(before, state, check, unit_written, dropin, dropin_existed, dropin_written, dropin_removed,
                              jvm_rc, enabled):
    """The change report items of cassandra_service, each {item, before, after}.

    before: the snapshot taken first (active, enabled, unit); state: cassandra_service_state; check: --check;
    unit_written: the role's unit written; dropin: the path of the drop-in, dropin_existed / dropin_written /
    dropin_removed: what became of it; jvm_rc: pgrep's for a unit active with no JVM (1: none running);
    enabled: cassandra_service_enabled as a boolean.
    {"active": "inactive", "enabled": "enabled", "unit": ""}, "started", ... -> [{"item": "cassandra",
    "before": "inactive", "after": "active"}]."""
    would = "would be " if check else ""  # --check changed nothing
    want = "inactive" if state == "stopped" else "active"
    items = []
    if unit_written:
        items.append({"item": UNIT,
                      "before": (before["unit"] or "absent") if "/etc/systemd/system/" not in before["unit"] else "different",
                      "after": "would be a native unit" if check else "native unit"})
    if dropin_written:
        items.append({"item": dropin, "before": "different" if dropin_existed else "absent", "after": would + "written"})
    if dropin_removed:
        items.append({"item": dropin, "before": "present", "after": would + "removed"})
    if state == "restarted":
        items.append({"item": "cassandra", "before": before["active"] or "unknown",
                      "after": "would restart" if check else "restarted"})
    elif before["active"] != want:
        items.append({"item": "cassandra", "before": before["active"] or "unknown", "after": would + want})
    elif jvm_rc == 1 and state == "started":  # a unit left active with no Cassandra running: started again
        items.append({"item": "cassandra", "before": "active, no Cassandra running", "after": would + "started again"})
    if (before["enabled"] == "enabled") != enabled:
        items.append({"item": "cassandra at boot", "before": before["enabled"] or "unknown",
                      "after": would + ("enabled" if enabled else "disabled")})
    return items


def cassandra_service_account_refusal(account, host, groups, unreadable, role_unit):
    """Why the account Cassandra runs as is refused, from what account_check.yml read on the node (account: its
    key=value lines as a dict). role_unit: Cassandra starts with the role's unit (else with the node's own: its
    User=/Group=). {"user": "dbsvc", "nouser": "yes", ...}, "n1" -> "the user dbsvc does not exist on n1: ..."."""
    if "nouser" in account:
        return "the user %s does not exist on %s%s" % (
            account["user"], host,
            ": create it (the Cassandra package creates cassandra), or set cassandra_user" if role_unit
            else " (the User= of the unit it starts with, not the one of the role): create it, or fix the unit")
    if "nogroup" in account:
        return "the group %s does not exist on %s%s" % (
            account["group"], host,
            ": create it, or set cassandra_group" if role_unit
            else " (the Group= of the unit it starts with, not the one of the role): create it, or fix the unit")
    return ("Cassandra %s as %s:%s (groups: %s), which could not read %s. Set cassandra_user and cassandra_group (one"
            " setting for the unit and the config files), or make the group of the unit and cassandra_config_group"
            " agree." % ("would run" if role_unit else "runs (the unit it starts with, not the one of the role)",
                         account["user"], account["group"], ", ".join(str(g) for g in groups),
                         ", ".join(str(f) for f in unreadable)))


def cassandra_service_other_seeds(seeds, mine):
    """The entries of seeds (a list, or a comma-separated string) whose host is none of mine (this node's names
    and addresses), whatever port they carry, trimmed, empty ones left out.
    ["10.0.0.1:7001", " 10.0.0.4", ""], ["node4", "10.0.0.4"] -> ["10.0.0.1:7001"]."""
    entries = seeds.split(",") if isinstance(seeds, str) else seeds
    trimmed = [str(e).strip() for e in entries]
    return [e for e in trimmed if e and cassandra_seed_host(e) not in mine]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_service_account_refusal": cassandra_service_account_refusal,
            "cassandra_service_changes": cassandra_service_changes,
            "cassandra_service_other_seeds": cassandra_service_other_seeds,
        }
