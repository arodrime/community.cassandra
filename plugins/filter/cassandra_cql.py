# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_cql_host: the address cqlsh, run on a node, reaches that node's Cassandra at: the one Cassandra binds
its native transport to, by its own rule (rpc_interface, else rpc_address).
cassandra_cql_login_outcome: what one CQL login of cql_login_try.yml says: ok, refused, or unchecked and why."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
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


_REFUSED = "AuthenticationFailed|Bad credentials|username and/or password are incorrect"
_UNREACHED = "Unable to connect|Connection refused|timed out|NoHostAvailable"


def _login_why(err, rc, node, host, port, user, command):
    """The why of a login not checked (cqlsh failed): from a fixed list, each from a matched error text, never from the
    exit code alone (127 is also a cqlsh script whose interpreter is missing); nothing of cqlsh's own text (it could
    quote the login)."""
    if re.search(_UNREACHED, err):
        return "cqlsh could not reach %s port %s from %s" % (host, port, node)
    # the program run: not found only when the error names it (cqlsh ran and missed another file: not this)
    cqlsh = (command.split() or ["cqlsh"])[0]
    name = re.escape(os.path.basename(cqlsh))
    if re.search("No such file or directory: b?'([^']*/)?" + name + "'|(^|[ /])" + name + ": (command )?not found", err):
        if cqlsh == "cqlsh":
            return "no cqlsh in the PATH of root on %s: cassandra_cqlsh_command gives the full path of one" % node
        return "%s not found on %s (cassandra_cqlsh_command): give the full path of a cqlsh there" % (cqlsh, node)
    # its error is not quoted (it could hold the login): the same command as root shows it
    see = "; to see its error, run as root on %s: %s %s %s -u %s" % (node, command, host, port, user)
    # the interpreter env did not find for the cqlsh script (/usr/bin/env: 'python3': No such file or directory)
    interpreter = (re.findall("/env: '?([^': ]+)'?: No such file or directory", err) + [""])[0]
    if interpreter:
        return "cqlsh failed on %s: no %s for its first line (#! /usr/bin/env %s)%s" % (node, interpreter, interpreter, see)
    if re.search("No such file or directory|FileNotFoundError", err):
        return "cqlsh failed on %s: it could not open a file it needs as root (cqlshrc, certificate...)%s" % (node, see)
    if re.search("No module named|ImportError|ModuleNotFoundError", err):
        return "cqlsh failed on %s: its Python could not load a module it needs%s" % (node, see)
    return "cqlsh failed on %s (exit code %s)%s" % (node, rc, see)


def cassandra_cql_login_outcome(result, node, host, port, user, command):
    """result: cql_login_try.yml's cassandra_cqlsh result; node: where cqlsh ran; host, port: where it connected;
    user: the login tried; command: cassandra_cqlsh_command -> {state: ok, refused (a refused login) or unchecked
    (cqlsh failed), why: "" unless unchecked}."""
    err = "%s %s" % (result.get("err", ""), result.get("msg", ""))
    if result.get("rc", 1) == 0:
        return {"state": "ok", "why": ""}
    if re.search(_REFUSED, err):
        return {"state": "refused", "why": ""}
    return {"state": "unchecked", "why": _login_why(err, result.get("rc", "?"), node, host, port, user, command)}


class FilterModule(object):
    def filters(self):
        return {"cassandra_cql_host": cassandra_cql_host,
                "cassandra_cql_login_outcome": cassandra_cql_login_outcome}
