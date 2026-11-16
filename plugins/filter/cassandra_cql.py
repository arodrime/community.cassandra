# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_cql_host: the address cqlsh, run on a node, reaches that node's Cassandra at: the one Cassandra binds
its native transport to, by its own rule (rpc_interface, else rpc_address)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

# the names of loopback (the address itself, 127.0.0.2 or ::1, is taken as written)
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")
# every address: loopback answers too
_ANY = ("0.0.0.0", "::", "0:0:0:0:0:0:0:0")


def _interface_address(facts, interface, prefer_ipv6):
    """The address of a network interface in the node's facts (eth0, bond0.12: bond0_12), '' when not known."""
    found = facts.get(re.sub(r"[^A-Za-z0-9_]", "_", interface)) or {}
    ipv4 = (found.get("ipv4") or {}).get("address", "") if isinstance(found.get("ipv4"), dict) else ""
    ipv6 = [a.get("address", "") for a in found.get("ipv6") or [] if isinstance(a, dict)]
    ipv6 = [a for a in ipv6 if a and not a.lower().startswith("fe80")] or ipv6  # (a link-local address: last)
    if prefer_ipv6 and ipv6:
        return ipv6[0]
    return ipv4 or (ipv6[0] if ipv6 else "")


def cassandra_cql_host(variables, facts=None, address=""):
    """variables: the node's (hostvars[node], or what the import read: cassandra_rpc_address, cassandra_extra_settings
    with an rpc_interface); facts: its ansible_facts (default: the ones in variables); address: the node's address
    when nothing else gives one (default: its ansible_host, else its inventory name).
    -> the host cqlsh on that node connects to:
    rpc_interface: that interface's address in the facts (rpc_interface_prefer_ipv6: its IPv6 one);
    rpc_address blank: Cassandra binds what the node's own name resolves to: that name (nodename), cqlsh on the node
    resolves it as Cassandra does; not set: the role's default, localhost; localhost, 0.0.0.0 or ::: 127.0.0.1;
    a Jinja expression (the import writes the hostname or fqdn fact for a node whose address is its name): what it
    gives; any other value as it is.
    e.g. {"cassandra_rpc_address": ""}, {"nodename": "db1.example"} -> "db1.example"."""
    variables = variables or {}
    facts = facts if facts is not None else variables.get("ansible_facts") or {}
    address = address or variables.get("ansible_host") or variables.get("inventory_hostname") or ""
    extra = variables.get("cassandra_extra_settings")
    extra = extra if isinstance(extra, dict) else {}
    if extra.get("rpc_interface"):
        return _interface_address(facts, str(extra["rpc_interface"]),
                                  str(extra.get("rpc_interface_prefer_ipv6", "")).lower() == "true") or address
    rpc = variables.get("cassandra_rpc_address", "localhost")
    rpc = "" if rpc is None else str(rpc).strip()
    if rpc == "":
        return facts.get("nodename") or facts.get("fqdn") or facts.get("hostname") or address
    if rpc.lower() in _LOOPBACK_NAMES or rpc in _ANY:
        return "127.0.0.1"
    if "{{" in rpc:
        for fact in ("hostname", "fqdn"):
            if re.search(r"ansible_facts.*\b%s\b|ansible_%s\b" % (fact, fact), rpc):
                return facts.get(fact) or address
        return address
    return rpc


class FilterModule(object):
    def filters(self):
        return {"cassandra_cql_host": cassandra_cql_host}
