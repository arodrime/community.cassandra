# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_suspicious_settings: {node: its variables} -> a WARNING line per
setting of SUSPICIOUS some nodes have (the preflight playbook and the
import_cluster report say them; nothing is refused): the nodes, the setting,
why it is risky, and how to keep it on purpose (cassandra_suspicious_ack)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

DASH = u"—"
ACK = "cassandra_suspicious_ack"
# lines a node has that no variable covers (the import's hand edits), checked as its extra JVM options are
LINES = "_cassandra_suspicious_lines"
GIB = 1024 ** 3
LOCAL = re.compile(r"^\[?(localhost(\.localdomain)?|ip6-localhost|(::ffff:)?127\.[0-9.]+|::1|0:0:0:0:0:0:0:1)\]?$")
REPLACE = re.compile(r"-Dcassandra\.replace_address(_first_boot)?=([\"']?)([^\s\"']*)\2")
JVM_OPTIONS = ("cassandra_jvm_extra_options", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options",
               "cassandra_jvm17_extra_options")


def _false(value):
    """A boolean variable set to false; anything else (null, unset, a template not resolved, "") is not."""
    try:
        return value is not None and not boolean(value, strict=True)
    except (TypeError, ValueError):
        return False


def _auto_bootstrap(values, node):
    if _false(values.get("cassandra_auto_bootstrap")):
        return [("auto_bootstrap: false", "a node that loses its data restarts empty without streaming it back;"
                                          " remove the setting (default true) unless you know why it is there")]
    return []


def _replace(values, node):
    """A replace flag left on a member of the ring (not on a commented line): where, and what to remove."""
    if not node["member"]:
        return []
    found = []
    places = [(var, values.get(var), False) for var in JVM_OPTIONS] + [("the node's config", values.get(LINES), True)]
    environment = values.get("cassandra_service_environment")
    if isinstance(environment, dict):
        places += [("cassandra_service_environment.%s" % k, [v], False) for k, v in sorted(environment.items())]
    for where, lines, whole in places:
        for line in lines if isinstance(lines, (list, tuple)) else []:
            if str(line).lstrip().startswith("#"):
                continue  # commented out: set nothing
            for m in REPLACE.finditer(str(line)):
                flag = "-Dcassandra.replace_address%s=%s" % (m.group(1) or "", m.group(3))
                why = ("this node is already bootstrapped: Cassandra refuses its next start with it"
                       if not m.group(1) else
                       "a member of the ring: ignored now, it would replace a node again after its data is wiped")
                remove = ("remove the line %s" % str(line).strip()) if whole else ("remove %s from %s" % (flag, where))
                found.append(("%s in %s" % (flag, where), "%s; %s" % (why, remove)))
    return found


def _size(text):
    """A JVM size (8G, 8192M, 1t, 8589934592) in bytes, else None."""
    m = re.match(r"^\s*(\d+)\s*([kKmMgGtT]?)\s*$", str(text))
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": GIB, "t": 1024 * GIB}[m.group(2).lower()] if m else None


def _xmx(values):
    """(where, -Xmx value) of the heap the JVM gets from an option: the extra JVM options (cassandra-env.sh then
    leaves MAX_HEAP_SIZE out), the unit's JVM_EXTRA_OPTS last (cassandra-env.sh adds it at the end); ("", "")."""
    found = ("", "")
    environment = values.get("cassandra_service_environment")
    places = [(var, values.get(var)) for var in JVM_OPTIONS]
    if isinstance(environment, dict):
        places.append(("cassandra_service_environment.JVM_EXTRA_OPTS", [environment.get("JVM_EXTRA_OPTS") or ""]))
    for where, lines in places:
        for line in lines if isinstance(lines, (list, tuple)) else []:
            if str(line).lstrip().startswith("#"):
                continue
            for m in re.finditer(r"(?:^|[\s\"'])-Xmx([0-9]+[kKmMgGtT]?)", str(line)):
                found = (where, m.group(1))
    return found


def _ram(values):
    """The host's RAM in GiB from its facts, rounded up (memtotal_mb is a bit under the RAM installed), else None."""
    facts = values.get("ansible_facts")
    ram = facts.get("memtotal_mb") if isinstance(facts, dict) else None
    return -(-int(ram) // 1024) if str(ram or "").isdigit() and int(ram) > 0 else None


def _heap(values, node):
    environment = values.get("cassandra_service_environment")
    where, heap = _xmx(values)
    if heap:
        setting, change = "-Xmx%s in %s" % (heap, where), "change -Xmx in %s" % where
    elif values.get("cassandra_heap_size"):
        heap = values["cassandra_heap_size"]
        setting, change = "cassandra_heap_size: %s" % heap, "set cassandra_heap_size"
    elif isinstance(environment, dict) and environment.get("MAX_HEAP_SIZE"):
        heap = environment["MAX_HEAP_SIZE"]
        setting, change = "cassandra_service_environment.MAX_HEAP_SIZE: %s" % heap, "set cassandra_heap_size"
    else:
        setting, change = "heap not set", "set cassandra_heap_size"
    ram = _ram(values)
    top = min(31, ram // 2) if ram else 31
    if ram and top < 2:
        return [(setting, "the host has %dG of RAM, too little for Cassandra (8G or more)" % ram)]
    advice = "%s between 2G and %dG (%snever 32G or more)" % (change, top, "at most half the RAM, " if ram else "")
    if not heap:
        # 4.x's cassandra-env.sh stops its auto-sizing at 8G (5.0's at half the RAM, 31G at most); 60G: a 64G host
        if ram and ram >= 60 and str(values.get("cassandra_version") or "50x") in ("40x", "41x"):
            return [(setting, "Cassandra 4.x sizes it to 8G at most, with %dG of RAM; %s" % (ram, advice))]
        return []
    size = _size(heap)
    if size is None:
        return []
    if size < 2 * GIB:
        return [(setting, "too small for Cassandra; %s" % advice)]
    if size >= 32 * GIB:
        return [(setting, "32G or more loses the JVM's compressed pointers (31G holds more); %s" % advice)]
    if ram and size > ram * GIB // 2:
        return [(setting, "more than half of the %dG of RAM, which the page cache and off-heap memory need; %s"
                 % (ram, advice))]
    return []


def _authenticator(values, node):
    """No client authentication on a native transport address other hosts reach (rpc_address; "": the
    hostname's; an rpc_interface of the extra settings)."""
    authenticator = str(values.get("cassandra_authenticator") or "AllowAllAuthenticator").strip()
    rpc = values.get("cassandra_rpc_address", "localhost")
    extra = values.get("cassandra_extra_settings")
    reached = bool(isinstance(extra, dict) and extra.get("rpc_interface")) or not LOCAL.match(
        str(rpc if rpc is not None else "localhost").strip())
    if authenticator.split(".")[-1] == "AllowAllAuthenticator" and reached:
        return [("authenticator: AllowAllAuthenticator", "any client that reaches the node's rpc address gets in; set"
                                                         " cassandra_authenticator: PasswordAuthenticator")]
    return []


FAILURE_POLICY = {
    "cassandra_disk_failure_policy": "the node stays up with a failed data disk, its reads and writes there failing;"
                                     " use stop (the default) or die",
    "cassandra_commit_failure_policy": "the node acknowledges writes its commit log could not keep; use stop (the"
                                       " default) or die",
}


def _failure_policy(values, node):
    return [("%s: ignore" % key[len("cassandra_"):], why) for key, why in sorted(FAILURE_POLICY.items())
            if str(values.get(key) or "").strip().lower() == "ignore"]


# (id, the check: (the node's variables, {name, member}) -> [(the setting, why)]); add a row for another one
SUSPICIOUS = [
    ("auto_bootstrap_false", _auto_bootstrap),
    ("replace_address", _replace),
    ("heap_size", _heap),
    ("allow_all_authenticator", _authenticator),
    ("failure_policy_ignore", _failure_policy),
]


def _acked(values):
    """The ids of cassandra_suspicious_ack: a list, or a comma-separated string (anything else: none)."""
    ack = values.get(ACK)
    if isinstance(ack, str):
        ack = ack.split(",")
    return set(str(a).strip() for a in ack) if isinstance(ack, (list, tuple, set)) else set()


def suspicious(nodes, members=None):
    """nodes: {name: {variable: value}} (a node's hostvars will do); members: the names in the ring (None: not
    known, the rows that need it say nothing) -> [(row id, the names, the setting, why)], in the order of
    SUSPICIOUS, without the nodes that acknowledge a row (cassandra_suspicious_ack)."""
    members = None if members is None else set(str(m) for m in members)
    found = []
    for row, check in SUSPICIOUS:
        texts = {}
        for name, values in (nodes or {}).items():
            values = values or {}
            if row in _acked(values):
                continue
            node = {"name": str(name), "member": members is not None and str(name) in members}
            for setting, why in check(values, node):
                texts.setdefault((setting, why), []).append(str(name))
        found += [(row, names, setting, why) for (setting, why), names in texts.items()]
    return found


def how_to_keep(row):
    """What silences a row on a node that keeps the setting on purpose."""
    return "kept on purpose? add %s to %s" % (row, ACK)


def unknown_acks(nodes):
    """[(an id of cassandra_suspicious_ack no row has, the nodes)]: a typo silences nothing."""
    ids = set(row for row, dummy in SUSPICIOUS)
    found = {}
    for name, values in (nodes or {}).items():
        for a in sorted(_acked(values or {}) - ids - set([""])):
            found.setdefault(a, []).append(str(name))
    return sorted(found.items())


def cassandra_suspicious_settings(nodes, members=None):
    """Filter: a "WARNING - node4: auto_bootstrap: false - ..." line per row of suspicious(nodes, members), then
    one per id of cassandra_suspicious_ack no row has."""
    return (["WARNING - %s: %s %s %s (%s)" % (out.nodes(names), setting, DASH, why, how_to_keep(row))
             for row, names, setting, why in suspicious(nodes, members)]
            + ["WARNING - %s: %s has %s, which no check has (%s): it silences nothing" % (
                out.nodes(names), ACK, a, ", ".join(row for row, dummy in SUSPICIOUS)) for a, names in unknown_acks(nodes)])


class FilterModule(object):
    def filters(self):
        return {"cassandra_suspicious_settings": cassandra_suspicious_settings}
