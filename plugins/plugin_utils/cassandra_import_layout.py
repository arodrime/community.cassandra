# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): every node's variables -> the inventory.

The cluster's group, where each value goes (the cluster's, a DC's or a rack's group_vars, or host_vars), the drift
between nodes, the snippets, and the user's own variables files of the inventory dir (a value of theirs kept or
written over). Also the ring's nodes and the names to reach them by.

Filters (plugins/filter/cassandra_import.py names the filters):
cassandra_ring_nodes: cassandra_status module's cluster_status -> list of nodes.
cassandra_ring_names: getent hosts on a given node -> the name to reach each
    node found in the ring by.
cassandra_import_group, cassandra_import_group_error: -e import_group -> the
    cluster's group, or why it is not an Ansible group name.
cassandra_import_known_group: the group an earlier import wrote for the given
    nodes.
cassandra_import_other_cluster: the hosts of a <cluster group>.yml there
    already when none of them is a node of this import.
cassandra_inventory_layout: every node's variables -> inventory groups,
    group_vars, host_vars, drift between nodes and the report.
cassandra_inventory_layout_over: the same, over the user's own variables
    files of the inventory dir (a value of theirs kept or written over).
cassandra_inventory_user_files: the inventory dir's vars files -> the user's
    own that apply to this cluster's hosts.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import copy
import functools
import json
import os
import re
from collections import namedtuple

import jinja2
import jinja2.sandbox
import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.filter.cassandra_repository_import import SERIES_TOKEN
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file, roles_with_defaults
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import ALWAYS, RACKDC
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import (
    ADDRESSES, HOSTNAME, IMPORTED, IMPORTED_FOR, IPV4, KEEP, MISSING, ROLE, SET_UP_BY_THE_ROLES, value_shown,
    values_hidden)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import (
    block_title, BLOCKS, CARRIED, cassandra_inventory_files, decrypt, KEEP_ORDER, KEPT_PARTS_VAR, list_secret, old_import,
    OWNABLE, SECRET_FILES, SECRET_LISTS, SECRET_MOVED_REF, SECRET_VAR, SNIPPET_LISTS, SNIPPETS, TagsLoader,
    TEMPLATE_MARK, UserUnsafe, UserLoader, vars_dir, Vaulted, written_for)


# Per node by nature: not reported as drift
PER_NODE = ADDRESSES + ["cassandra_initial_token", "cassandra_medusa_fqdn", "cassandra_rackdc_rack"]
# Kept on the node that has it only (host_vars), even when every node does: a node added later must not get it
NODE_LEVEL = ("cassandra_auto_bootstrap",)
# The KEEP switch put in the cluster's group_vars when every node was read and has it (a node added later gets it
# too)
JAVA_DEFAULT = "cassandra_java_set_default"


def cassandra_ring_nodes(cluster_status):
    """cluster_status: cassandra_status' -> [{address, state, dc, rack, host_id}], by DC.
    e.g. {"dc1": {"nodes": [{"address": "10.0.0.1", "status": "U", "state": "N", "rack": "r1", "host_id": "h1"}]}}
    -> [{"state": "UN", "address": "10.0.0.1", "dc": "dc1", "host_id": "h1", "rack": "r1"}]."""
    return [{"state": n["status"] + n["state"], "address": n["address"], "dc": dc,
             "host_id": n["host_id"], "rack": n["rack"]}
            for dc, info in sorted((cluster_status or {}).items()) for n in info["nodes"]]


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"
    return "c_" + s if s[0].isdigit() else s


# an Ansible group name (letters, digits, _; not a digit first), as -e import_group takes it
GROUP_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def cassandra_import_group_error(name):
    """-e import_group: why it is not an Ansible group name (all, ungrouped: Ansible's own) -> the stop's message,
    '' when it is one."""
    if not GROUP_NAME.match(str(name)) or _slug(name) in ("all", "ungrouped"):
        return ("import_group=%s: not an Ansible group name: letters, digits and _, not a digit first (nor all,"
                " ungrouped). Nothing was written." % name)
    return ""


def cassandra_import_group(name):
    """-e import_group: the cluster's group the import writes, slugged as a cluster name is (lower case) ->
    the group; AnsibleFilterError for one that is not an Ansible group name (cassandra_import_group_error).
    e.g. "Orders_EU" -> "orders_eu"."""
    error = cassandra_import_group_error(name)
    if error:
        raise AnsibleFilterError(error)
    return _slug(name)


def cassandra_import_known_group(groups, texts):
    """The group an earlier import wrote for the given nodes: groups: the groups the given nodes are in; texts:
    {group: the text of <import_dir>/<group>.yml, '' when none} -> that group when one only is the hosts file the
    import wrote for it (its first line), else ''."""
    found = sorted(set(g for g in groups or [] if written_for(str((texts or {}).get(g) or "")) == g))
    return found[0] if len(found) == 1 else ""


def _hosts_of(text):
    """A YAML hosts file the import wrote -> (its host names, the ansible_host of its hosts)."""
    try:
        data = yaml.load(str(text or ""), Loader=TagsLoader)  # nosec B506: the loader builds no object
    except yaml.YAMLError:
        return set(), set()
    names, addresses, seen = set(), set(), set()

    def walk(level):
        if not isinstance(level, dict) or id(level) in seen:
            return
        seen.add(id(level))
        for group in level.values():
            if isinstance(group, dict):
                for host, hvars in (group.get("hosts") if isinstance(group.get("hosts"), dict) else {}).items():
                    names.add(str(host))
                    if isinstance(hvars, dict) and hvars.get("ansible_host"):
                        addresses.add(str(hvars["ansible_host"]))
                walk(group.get("children"))
    walk(data)
    return names, addresses


def cassandra_import_other_cluster(text, layout):
    """text: the <cluster group>.yml there already; layout: cassandra_inventory_layout's -> the hosts of that file
    when none of them is a node of this import (by name, address or ansible_host: neither the inventory nor the
    report records the Host IDs), sorted; [] when one is (a re-import), or the file has no host."""
    names, addresses = _hosts_of(text)
    if not names:
        return []
    mine = set()
    for n in layout.get("nodes") or []:
        mine.update(str(v) for v in (n.get("name"), n.get("address"), n.get("hostname"), n.get("ansible_host")) if v)
    return [] if (names | addresses) & mine else sorted(names)


def _same(key, a, b):
    """Equal values; for a value written in its order (KEEP_ORDER), its mappings in the same order too."""
    if key in KEEP_ORDER:
        return json.dumps(a, default=str) == json.dumps(b, default=str)
    return a == b


def _place_highest(key, nodes, levels, group_vars, host_vars):
    """Put key at the highest level where all its nodes agree (levels: cluster, DC, rack, then the node itself):
    cassandra-rackdc.properties' dc= and rack= (RACKDC), a DC's or a rack's by nature. True if that is the
    cluster."""
    def walk(level, members):
        values = [n["vars"].get(key, MISSING) for n in members]
        if all(v is MISSING for v in values):
            return level == 0
        if level < len(levels) and all(_same(key, v, values[0]) for v in values):
            group_vars.setdefault(levels[level](members[0]), {})[key] = values[0]
            return level == 0
        if level == len(levels):
            host_vars.setdefault(members[0]["name"], {})[key] = values[0]
            return False
        parts = {}
        for n in members:
            parts.setdefault(levels[level + 1](n) if level + 1 < len(levels) else n["name"], []).append(n)
        for part in parts.values():
            walk(level + 1, part)
        return False
    return walk(0, nodes)


def _hosts_order(n):
    """A node's place in the hosts file the import writes (by DC, rack, name): "the first node" of a tie."""
    return (n["dc"], n["rack"], n["name"])


def _value_groups(key, nodes):
    """The nodes by value, in hosts file order (MISSING: the nodes without key) -> [(value, [nodes])]."""
    groups = []
    for n in sorted(nodes, key=_hosts_order):
        value = n["vars"].get(key, MISSING)
        group = next((g for g in groups if (g[0] is MISSING) == (value is MISSING)
                      and (value is MISSING or _same(key, g[0], value))), None)
        if group is None:
            groups.append((value, [n]))
        else:
            group[1].append(n)
    return groups


def _is_default(key, value, defaults):
    """value is the collection's default of key: MISSING (not set), or equal to a default that is a value (not
    made of other variables)."""
    if value is MISSING:
        return True
    return key in defaults and not templated(defaults[key]) and _same(key, value, defaults[key])


# a value your file gives a node over the cluster's group_vars (_given): nothing to write for it
YOURS_WIN = object()


def _fill(key, n, defaults, given):
    """What node n without key gets when the import does not set it, as a value the import can write in its
    host_vars (the cluster's group_vars set another): your files' value, else the collection's default -> (True,
    value), (True, YOURS_WIN) when a file of yours gives it over the cluster's, (False, None) when it cannot be
    written as a value (made of other variables or facts, a template of yours, no default: Ansible's undefined)."""
    mine = (given or {}).get(n["name"]) or {}
    value = mine[key] if key in mine else defaults.get(key, MISSING)
    if value is YOURS_WIN:
        return True, value
    if value is MISSING or not _writable(value):
        return False, None
    return True, copy.deepcopy(value)


def _writable(value):
    """value can be written as it is: plain data, no template, nothing vaulted."""
    if isinstance(value, dict):
        return all(_writable(k) and _writable(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_writable(v) for v in value)
    if isinstance(value, str):
        return not isinstance(value, UserUnsafe) and not TEMPLATE_MARK.search(value)
    return value is None or isinstance(value, (bool, int, float))


def _place(key, nodes, cluster, group_vars, host_vars, given=None, said=None):
    """Put key's most common value among the nodes read in the cluster's group_vars and each other node's in its
    host_vars (the whole value: Ansible replaces a dict). A tie: the collection's default when it is one of the
    values, else the first node's in the hosts file (said["tie"]). A node without key gets its value without the
    import written (your files', else the default), or, when that cannot be written as a value, nothing goes to
    the cluster's group_vars (said["per_node"]). A value per node by nature (PER_NODE) goes there only when
    several nodes share it. said: {"tie": [(key, how)], "per_node": [key]}, filled in place. True when every node
    has the same value (or none)."""
    said = said if said is not None else {}
    defaults = roles_defaults()
    groups = _value_groups(key, nodes)
    if len(groups) == 1:
        if groups[0][0] is not MISSING:
            group_vars.setdefault(cluster, {})[key] = groups[0][0]
        return True
    top = max(len(members) for dummy, members in groups)
    tied = [i for i, g in enumerate(groups) if len(g[1]) == top]
    chosen = next((i for i in tied if _is_default(key, groups[i][0], defaults)), tied[0])  # (its index in groups)
    value, members = groups[chosen]
    tie = None
    if len(tied) > 1:
        yours = value is MISSING and any(key in ((given or {}).get(n["name"]) or {}) for n in members)
        tie = (key, "yours" if yours else "default" if _is_default(key, value, defaults) else members[0]["name"])
    # whether the chosen value goes to the cluster's group_vars (a node's own by nature, or a node without key whose
    # value cannot be written: every node's in its host_vars)
    to_cluster = not (key in PER_NODE and top == 1)
    missing = next((m for v, m in groups if v is MISSING), [])
    fills = {}
    if to_cluster and value is not MISSING:
        for n in missing:
            ok, fill = _fill(key, n, defaults, given)
            if not ok:
                said.setdefault("per_node", []).append(key)
                to_cluster, fills = False, {}
                break
            fills[n["name"]] = fill
    if tie and to_cluster:
        said.setdefault("tie", []).append(tie)
    if to_cluster and value is not MISSING:
        group_vars.setdefault(cluster, {})[key] = value
    for i, (other, others) in enumerate(groups):
        if to_cluster and i == chosen:
            continue
        for n in others:
            if other is not MISSING:
                host_vars.setdefault(n["name"], {})[key] = other
            elif n["name"] in fills and fills[n["name"]] is not YOURS_WIN \
                    and not _same(key, fills[n["name"]], value):  # (the default there already)
                host_vars.setdefault(n["name"], {})[key] = fills[n["name"]]
    return False


def _show(key, value):
    if value is MISSING:
        return "(role default)"
    # json, not yaml.safe_dump: Ansible hands filters dict/str subclasses
    return value_shown(key, value, lambda v: json.dumps(v, sort_keys=key not in KEEP_ORDER, default=str))


def _medusa_fqdn_domain(read):
    """The domain D when every read node with a Medusa fqdn has exactly
    "<its short hostname>.D", the same D everywhere (what the cassandra_medusa
    default then writes back), else None."""
    domains = set()
    for n in read:
        if "cassandra_medusa_fqdn" not in n["vars"]:
            continue
        fqdn, hostname = str(n["vars"]["cassandra_medusa_fqdn"]), str(n.get("hostname") or "")
        if not hostname or not fqdn.startswith(hostname + ".") or len(fqdn) == len(hostname) + 1:
            return None
        domains.add(fqdn[len(hostname) + 1:])
    return domains.pop() if len(domains) == 1 else None


# A value that follows each node's name, the same before and after it on every node (Medusa's nodetool_host
# "<node>.example.net"): one template for all, Ansible gives each node its own. The names it may follow, as
# Ansible names them, and how the layout knows them. Not ansible_host: another inventory source may set it (a
# bastion's address), the self-check could not see it. Not the node's identity (its addresses, token, Medusa's
# fqdn) nor where its data is: a renamed host must not move them
NAME_VARS = (("inventory_hostname", lambda n: n["name"]),
             ("inventory_hostname_short", lambda n: n["name"].split(".")[0]))
NOT_FROM_NAMES = tuple(PER_NODE) + ("cassandra_cluster_name", "cassandra_dc", "cassandra_rack")
# nor where its data, config, keys and backups are (a directory, a path, a keystore, Medusa's prefix or bucket)
NOT_FROM_NAMES_PATTERN = re.compile(r"_(dir|dirs|directory|directories|path|paths|file|files|prefix|home|profile_d"
                                    r"|bucket|bucket_name)$|keystore|truststore|cert")


def _literal(text):
    """True when Ansible before 2.19 would make something else of text, rendered (a number, a list, True)."""
    try:
        return not isinstance(ast.literal_eval(text.strip()), str)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False


def _name_template(key, values):
    """values: [(node, value)] of the nodes that have key -> a template "<before>{{ <name> }}<after>" that gives
    each node exactly its value, None when there is none: fewer than 2 nodes or a single value, a value that is not
    a string (or that Ansible would read as another type), a secret, one node that does not follow the rule, two
    nodes with the same name, a { or } around it."""
    texts = [v for dummy, v in values]
    if key in NOT_FROM_NAMES or NOT_FROM_NAMES_PATTERN.search(key) or len(values) < 2 or not all(isinstance(v, str) for v in texts) \
            or len(set(texts)) < 2 or any(cassandra_secrets.hidden(key, v) or TEMPLATE_MARK.search(v) or _literal(v) for v in texts):
        return None
    for var, name_of in NAME_VARS:
        names = [str(name_of(n)) for n, dummy in values]
        if not all(names) or len(set(names)) < len(names):
            continue
        first, name = texts[0], names[0]
        at = first.find(name)
        while at >= 0:
            before, after = first[:at], first[at + len(name):]
            if not re.search(r"[{}]", before + after) and all(
                    v == before + n + after for v, n in zip(texts, names)):
                return "%s{{ %s }}%s" % (before, var, after)
            at = first.find(name, at + 1)
    return None


def vars_sort_key(key):
    """Keys in the order of the vars files: by block, then as listed there."""
    titles = [title for title, dummy in BLOCKS]
    order = dict(BLOCKS)[block_title(key)]
    return (titles.index(block_title(key)), order.index(key) if key in order else len(order), key)


def _differences(keys, read, dcg, rackg):
    """One line per key: each value and the nodes (or DC, rack) that have it."""
    lines = []
    for key in sorted(keys, key=vars_sort_key):
        groups = {}  # by value, shown masked
        for n in read:
            value = n["vars"].get(key, MISSING)
            same = "" if value is MISSING else json.dumps(value, sort_keys=key not in KEEP_ORDER, default=str)
            groups.setdefault(same, (_show(key, value), []))[1].append(n)
        parts = []
        for value, members in sorted(groups.values(), key=lambda g: (-len(g[1]), g[0], sorted(n["name"] for n in g[1]))):
            names = sorted(n["name"] for n in members)
            dc = [n for n in read if dcg(n) == dcg(members[0])]
            rack = [n for n in read if rackg(n) == rackg(members[0])]
            if dc == [n for n in read if n in members] and len(dc) > 1:
                where = "DC %s" % members[0]["dc"]
            elif rack == [n for n in read if n in members] and len(rack) > 1:
                where = "rack %s/%s" % (members[0]["dc"], members[0]["rack"])
            elif not parts and len(members) > 3:  # the most common value: the others are listed
                where = "%d nodes" % len(members)
            else:
                where = ", ".join(names)
            parts.append("%s on %s" % (value, where))
        lines.append("  %s: %s" % (key, "; ".join(parts)))
    return lines


def _without_ring_rackdc(node):
    """The node's variables without cassandra-rackdc.properties' dc= and rack= where
    they are its dc and rack (the role's default writes them: cassandra_dc, cassandra_rack)."""
    ring = {"cassandra_rackdc_dc": node["dc"], "cassandra_rackdc_rack": node["rack"]}
    return dict((k, node["vars"][k]) for k in node["vars"] if k not in ring or node["vars"][k] != ring[k])


def hand_edits_kept(node):
    """A node's hand edit lines -> (the ones in files cassandra_config_keep_files keeps, the others); a
    "<file>: ..." or "<file>, line N:" line starts the lines of a file."""
    keep = (node.get("keep") or {}).get("cassandra_config_keep_files") or []
    kept, edits, current = [], [], None
    for line in node["hand_edits"]:
        if not line.startswith(" "):
            current = re.split(r"[:,]", line, maxsplit=1)[0]
        (kept if current in keep else edits).append(line)
    return kept, edits


def _cluster_group(cluster, dummy):
    """The cluster's group (whatever the node) -> its name."""
    return cluster


def _dc_group(cluster, n):
    """The group of node n's DC -> its name (<cluster>_<dc>)."""
    return "%s_%s" % (cluster, _slug(n["dc"]))


def _rack_group(cluster, n):
    """The group of node n's rack -> its name (<cluster>_<dc>_<rack>)."""
    return "%s_%s" % (_dc_group(cluster, n), _slug(n["rack"]))


def _hosts_tree(nodes, cluster, dcg, rackg):
    """The hosts file's data: the cluster's group, a group per DC, one per rack under it, the nodes in their rack
    (their ansible_host) -> (hosts, group_vars: {group: {variable: value}} with each DC's and rack's)."""
    hosts = {"all": {"children": {cluster: {"children": {}}}}}
    group_vars = {cluster: {}}
    for n in sorted(nodes, key=lambda n: (n["dc"], n["rack"], n["name"])):
        dcs = hosts["all"]["children"][cluster]["children"]
        racks = dcs.setdefault(dcg(n), {"children": {}})["children"]
        entry = {"ansible_host": n["ansible_host"]} if n.get("ansible_host") else {}
        racks.setdefault(rackg(n), {"hosts": {}})["hosts"][n["name"]] = entry
        group_vars.setdefault(dcg(n), {})["cassandra_dc"] = n["dc"]
        group_vars.setdefault(rackg(n), {})["cassandra_rack"] = n["rack"]
    return hosts, group_vars


def _read_for_layout(nodes):
    """The nodes read, their variables without rackdc's dc= and rack= where they are the ring's, Medusa's fqdn as a
    domain when one rule gives every node its value -> (read, nodes (those read replaced by theirs), the nodes with
    a Medusa fqdn, the domain or None)."""
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    read = [dict(n, vars=_without_ring_rackdc(n)) for n in read]
    nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    # Medusa's fqdn is the node's folder in the backups: a rule only when it
    # gives every node its value exactly, else each node keeps its own
    medusa = [n for n in read if n["vars"].get("cassandra_medusa_fqdn")]  # "": Medusa works it out
    domain = _medusa_fqdn_domain(read) if medusa else None
    if domain is not None:
        read = [dict(n, vars=dict([(k, v) for k, v in n["vars"].items() if k != "cassandra_medusa_fqdn"]
                                  + [("cassandra_medusa_fqdn_domain", domain)])) for n in read]
        nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    return read, nodes, medusa, domain


def _name_templates(keys, read):
    """The keys whose value comes from each node's name by one template: that template put in the nodes' variables
    (in place) -> {key: template}."""
    templates = {}
    for key in keys:
        template = _name_template(key, [(n, n["vars"][key]) for n in read if key in n["vars"]])
        if template is not None:
            templates[key] = template
            for n in read:
                if key in n["vars"]:
                    n["vars"][key] = template
    return templates


def _layout_notes(cluster_name, cluster, nodes, read, host_vars, medusa, domain, templates, shared):
    """The report's head: the cluster, its group, the secrets set per node, rackdc's
    dc= and rack= kept, Medusa's fqdn, the values from each node's name, the names shared, the nodes not read ->
    [line]."""
    report = ["Cluster %s: %s, %d read" % (cluster_name, out.plural(len(nodes), "node"), len(read)),
              "Inventory group: %s (the playbooks find it in an inventory of this cluster alone; with other clusters:"
              " -e cassandra_hosts=%s, or CASSANDRA_CLUSTER=%s)" % (cluster, cluster, cluster), ""]
    # (the differences between nodes: in the report's TO REVIEW, each value summarised)
    per_node = sorted({k for v in host_vars.values() for k in v if k.startswith(SECRET_VAR)})
    if per_node:
        report += ["SECRETS OF HAND EDITS PER NODE: %s are set per node (host_vars/<node>/secrets.yml): a node"
                   " added later needs its own in its host_vars, or cassandra_config cannot render the lines that"
                   " refer to them" % ", ".join(per_node), ""]
    rackdc = [n for n in read if any(k in n["vars"] for k in RACKDC)]
    if rackdc:
        snitches = sorted({str(n["vars"].get("cassandra_endpoint_snitch")) for n in rackdc})
        report += ["cassandra-rackdc.properties: dc= and rack= kept as the nodes have them (cassandra_rackdc_dc,"
                   " cassandra_rackdc_rack) on %s: %s does not read them, the nodes' dc and rack are the ring's"
                   " (cassandra_dc, cassandra_rack). A node added later gets them from its groups, else its"
                   " cassandra_dc and cassandra_rack." % (", ".join(sorted(n["name"] for n in rackdc)),
                                                          " / ".join(snitches)), ""]
    if domain is not None:
        report += ["Medusa fqdn (each node's folder in the backups): <short hostname>.%s on every node,"
                   " kept as cassandra_medusa_fqdn_domain" % domain, ""]
    elif medusa:
        report += ["Medusa fqdn (each node's folder in the backups): no <short hostname>.<domain> rule gives every"
                   " node's value, kept as found (cassandra_medusa_fqdn)", ""]
    for key, template in sorted(templates.items()):
        report += ["Per node, from its name: %s = %s on every node that has it, one value in the inventory"
                   " (Ansible gives each node its own)" % (key, template), ""]
    if shared:
        report.append("SAME NAME for several nodes, named by their address instead: %s" % ", ".join(shared))
        report.append("")
    unread = [n for n in nodes if n not in read]
    if unread:
        report.append("NOT READ (in the inventory, but not imported):")
        report += ["  %s: %s" % (n["name"], n.get("reason", "unreachable")) for n in unread]
        left = sorted(set(k for n in unread for k in n.get("keep") or {}), key=lambda k: (k not in KEEP, k))
        if left:
            report.append("  The roles leave their setup as it is (host_vars: %s false)" % ", ".join(left))
        report.append("")
    return report


def _placement_lines(cluster, said):
    """The report's lines on the variables _place could not give their most common value plainly: a tie, one
    kept per node only -> [line]."""
    lines = []
    if said.get("tie"):
        lines.append("A TIE, as many nodes on each value: the one in group_vars/%s/ (the others in their nodes'"
                     " host_vars):" % cluster)
        words = {"default": "the collection's default", "yours": "the one your files give (not written)"}
        for key, how in sorted(said["tie"], key=lambda t: vars_sort_key(t[0])):
            lines.append("  %s: %s" % (key, words.get(how, "the one of %s, the first node in the hosts file" % how)))
        lines.append("")
    if said.get("per_node"):
        lines.append("IN HOST_VARS ONLY, not in group_vars/%s/: the nodes without them get a default made of other"
                     " variables (or a value of your files), which the import does not write for them alone:" % cluster)
        lines += ["  %s" % key for key in sorted(said["per_node"], key=vars_sort_key)]
        lines.append("")
    return lines


# the values left as the nodes have them that the cluster's group takes when every node has the same: the system
# java, the packages the nodes have none of and their hold, the DSBulk version. The others stay per node, as their
# notes say: a *_manage switch or a part kept by hand (a node set up another way: a host rebuilt under its name loses
# it, new nodes get the roles'), a file kept, Medusa's link none on a node (new nodes get one), the log dir of a
# unit kept
CLUSTER_KEEP = (JAVA_DEFAULT, "cassandra_install_tools", "cassandra_install_jemalloc", "cassandra_package_hold",
                "cassandra_dsbulk_install", "cassandra_dsbulk_version")


def _grouped_keep(read, nodes):
    """The values of CLUSTER_KEEP that go in the cluster's group: every node read, each with the same value, none
    with the variable among its own (which would beat the group's) -> sorted [variable]."""
    if not read or len(read) != len(nodes):
        return []
    first = read[0].get("keep") or {}
    return sorted(k for k, v in first.items()
                  if k in CLUSTER_KEEP and all(k in (n.get("keep") or {}) and n["keep"][k] == v
                                               and k not in (n.get("vars") or {}) for n in read))


def _grouped_keep_lines(read, cluster, grouped):
    """The report's lines on the values left as every node has them, written in the cluster's group -> [line]."""
    rows = [k for k in grouped if k != JAVA_DEFAULT]
    if not rows:
        return []
    return ["LEFT AS EVERY NODE HAS IT, in group_vars/%s/main.yml (nodes added later get it too):" % cluster] + [
        "  %s: %s" % (k, _show(k, read[0]["keep"][k])) for k in rows] + [""]


def _java_default_line(read, cluster, grouped):
    """The report's line on the system java the roles leave as it is (cassandra_java_set_default: false): on
    which nodes, why, where it is set, what true would do -> [line], [] when on no node."""
    names = [n["name"] for n in read if JAVA_DEFAULT in (n.get("keep") or {})]
    if not names:
        return []
    return ["SYSTEM JAVA: /usr/bin/java is another Java than the one Cassandra runs on %s; the roles leave it as it is"
            " (cassandra_java_set_default: false in %s). true would make the running Java the system java"
            " (alternatives), for every program that runs java"
            % (("every node", "group_vars/%s/main.yml, for nodes added later too: one without a /usr/bin/java gets"
                " the running Java's, one with another needs JAVA_HOME, e.g. in cassandra_service_environment"
                % cluster) if grouped else (", ".join(names), "their host_vars")), ""]


def _node_report(n, grouped=()):
    """A node read in the report: its notes, what the roles leave as it is on it (grouped: those in the cluster's
    group_vars left out), its hand edits (in files kept or not), its comment lines, its lines the role writes
    another way -> [line]."""
    report = ["== %s" % n["name"]]
    report += ["  " + note for note in n.get("notes", [])]
    other_way = dict((k, v) for k, v in (n.get("keep") or {}).items()
                     if k not in SET_UP_BY_THE_ROLES and k not in grouped)
    if other_way:
        report.append("  Set up another way, LEFT AS IT IS by the roles on this node (host_vars/%s/main.yml;"
                      " remove a line to let the role take it over, after --check --diff; a host"
                      " rebuilt under this name must lose them):" % n["name"])
        report += ["    %s (%s: false)" % (KEEP[k], k) for k in KEEP if k in other_way]
        report += ["    %s: %s, as this node has it" % (k, _show(k, v))
                   for k, v in sorted(other_way.items()) if k not in KEEP]
    for k in SET_UP_BY_THE_ROLES:
        if k in (n.get("keep") or {}):
            report.append("  Set up by the roles, LEFT AS IT IS on this node (host_vars/%s/main.yml): %s (%s: false;"
                          " remove the line to have it done, after --check --diff)" % (n["name"], KEEP[k], k))
    kept, edits = hand_edits_kept(n)
    if kept:
        report.append("  HAND EDITS no variable covers, in files LEFT AS THEY ARE on this node"
                      " (cassandra_config_keep_files; nodes added later get the role's):")
        report += ["    " + line for line in cassandra_secrets.mask_hunks(kept)]
    if edits:
        report.append("  Hand edits no variable carries (apply_config would put the file back as the collection writes it):")
        report += ["    " + line for line in cassandra_secrets.mask_hunks(edits)]
    if not n["hand_edits"]:
        report.append("  No hand edit left: cassandra_config would not change the config.")
    if n.get("comments"):
        report.append("  Comments only, no setting (e.g. the stock comments of the release the file came from):"
                      " no effect, kept unless the role rewrites the file for a setting:")
        report += ["    " + line for line in n["comments"]]
    if n["normalized"]:
        report.append("  Same setting, written another way by the role (no effect; on a node that is not blank, a"
                      " file whose settings are all the same is left as it is):")
        report += ["    " + cassandra_secrets.mask(line) for line in n["normalized"]]
    return report + [""]


@values_hidden
def cassandra_inventory_layout(nodes, cluster_name, group="", given=None):
    """nodes: [{name, address?, hostname?, dc, rack, ansible_host?, read: bool, reason?,
    vars, hand_edits, normalized, comments?, notes}] -> {'cluster_group', 'cluster_name', 'hosts', 'group_vars', 'host_vars',
    'differences', 'report', 'names' (address -> name in the hosts file)}. Nodes sharing a name are named by their address instead.
    group: the cluster's group (import_group, cassandra_import_group's), else the cluster name's. Each variable's
    most common value goes to the cluster's group_vars, each other node's to its host_vars (_place); the DC's and
    rack's group_vars hold their cassandra_dc and cassandra_rack (and rackdc's dc= and rack= where the nodes there
    share them). given: {node: {variable: value}} your files give the nodes (cassandra_inventory_layout_over).
    e.g. cassandra_inventory_layout(cassandra_import_nodes, "Prod Cluster") -> {"cluster_group": "prod_cluster",
    "hosts": {"all": {"children": {"prod_cluster": {"children": {"prod_cluster_dc1": ...}}}}}, ...}."""
    cluster = cassandra_import_group(group) if group else _slug(cluster_name)
    names = [n["name"] for n in nodes]
    shared = sorted({name for name in names if names.count(name) > 1})
    nodes = [dict(n, name=n.get("address") or n["name"]) if n["name"] in shared else n for n in nodes]
    dcg, rackg = functools.partial(_dc_group, cluster), functools.partial(_rack_group, cluster)
    levels = [functools.partial(_cluster_group, cluster), dcg, rackg]
    hosts, group_vars = _hosts_tree(nodes, cluster, dcg, rackg)
    host_vars = {}
    read, nodes, medusa, domain = _read_for_layout(nodes)
    read, nodes, snippets = _snippets_for_layout(read, nodes, cluster)
    keys = sorted({k for n in read for k in n["vars"]} - {"cassandra_dc", "cassandra_rack"})
    templates = _name_templates(keys, read)
    for key in [k for k in keys if k in NODE_LEVEL]:
        for n in read:
            if key in n["vars"]:
                host_vars.setdefault(n["name"], {})[key] = n["vars"][key]
    said, drift = {}, []  # (the variables whose values differ between nodes)
    for k in [k for k in keys if k not in NODE_LEVEL]:
        same = _place_highest(k, read, levels, group_vars, host_vars) if k in RACKDC \
            else _place(k, read, cluster, group_vars, host_vars, given, said)
        if not same and k not in PER_NODE:
            drift.append(k)
    differences = ["DIFFERENCES BETWEEN NODES (kept per group or node, check they are wanted):"]
    differences += _differences(drift, read, dcg, rackg) or ["  none"]
    # the system java left as it is on every node, all read: in the cluster's group (the nodes keep it in "keep");
    # so are the other values left as the nodes have them, the same on every node (the cluster's, not a node's)
    grouped = _grouped_keep(read, nodes)
    for k in grouped:
        group_vars[cluster][k] = read[0]["keep"][k]
    report = _layout_notes(cluster_name, cluster, nodes, read, host_vars, medusa, domain, templates,
                           shared)
    report += _placement_lines(cluster, said)
    report += _java_default_line(read, cluster, JAVA_DEFAULT in grouped)
    report += _grouped_keep_lines(read, cluster, grouped)
    for n in nodes:
        keep = dict((k, v) for k, v in (n.get("keep") or {}).items() if k not in grouped)
        if keep:
            host_vars.setdefault(n["name"], {}).update(keep)
        if any(k in IMPORTED_FOR for k in keep) or keep.get(KEPT_PARTS_VAR):
            host_vars[n["name"]][IMPORTED] = True
    for n in read:
        report += _node_report(n, grouped)
    report += _os_section(read)
    return {"cluster_group": cluster, "cluster_name": cluster_name, "hosts": hosts, "group_vars": group_vars,
            "host_vars": host_vars, "differences": "\n".join(differences), "report": "\n".join(report),
            "names": dict((n["address"], n["name"]) for n in nodes if n.get("address")), "nodes": nodes,
            "templates": templates, "trusted": _trusted_lines(read),
            "snippets": snippets}


def snippet_path(kind, name, point):
    """Where the import writes the snippet of an insertion point: group_vars/<cluster>/ when every node has the
    same, else host_vars/<node>/ (Ansible reads no .j2 there as variables, nor as an inventory); logback.xml:appenders
    as logback.xml.appenders."""
    return "%s/%s/%s.snippet.j2" % (kind, name, point.replace(":", "."))


def snippet_text(lines, trusted=()):
    """A snippet's text: the lines as the node has them; one that looks like a template (a {{ of the node's own)
    in a raw block, Jinja renders it as it is; trusted: the lines the import made that refer to a secret's
    variable (rendered)."""
    out = []
    for line in lines:
        out.append("{%% raw %%}%s\n{%% endraw %%}" % line if line not in trusted and TEMPLATE_MARK.search(line)
                   else line)
    return "\n".join(out) + "\n"


def _snippets_for_layout(read, nodes, cluster):
    """The nodes' lines of SNIPPET_LISTS moved to snippet files: in their variables, cassandra_config_template_snippets
    names them ({{ inventory_dir }}/...: a template the import made, trusted) -> (read, nodes, {path: text}). Lines
    with a secret the import could not take out alone (or one that would end a raw block): all of them in secrets.yml,
    cassandra_secret_<file>_lines, which the snippet renders. None when your own template renders the file, or when
    the node keeps it as it is (cassandra_config refuses a snippet for it)."""
    snippets = {}
    for key, name in SNIPPET_LISTS:
        having = [n for n in read if isinstance(n["vars"].get(key), list) and n["vars"][key]
                  and name.split(":", maxsplit=1)[0] not in (n.get("yours_templates") or ())
                  and name.split(":", maxsplit=1)[0] not in ((n.get("keep") or {}).get("cassandra_config_keep_files") or [])]
        texts = [_snippet_of(n, key, name) for n in having]
        # (the same text: a node's own {{ in a raw block, never the same as a line the import made)
        common = _common_snippet(read, having, texts)
        for n, text in zip(having, texts):
            path = snippet_path("group_vars", cluster, name) if text == common \
                else snippet_path("host_vars", n["name"], name)
            snippets[path] = text
            n["snippet_refs"] = list(n.get("snippet_refs") or []) + SECRET_MOVED_REF.findall(text) + re.findall(
                r"\{\{ (%s\w+) \}\}" % SECRET_VAR, text)
            value = "{{ inventory_dir }}/" + path
            n["vars"] = dict((k, v) for k, v in n["vars"].items() if k != key)
            yours = n.get("yours_snippets") or {}
            n["vars"][SNIPPETS] = dict(yours, **dict(n["vars"].get(SNIPPETS) or {}, **{name: value}))
            n["trusted"] = list(n.get("trusted") or []) + [value] + [v for v in yours.values() if isinstance(v, str)]
            n["notes"] = [note.replace("kept with %s" % key, "kept in %s (%s)" % (path, SNIPPETS))
                          if note.startswith(CARRIED + name + ":") else note for note in n.get("notes") or []]
    for n in read:  # (the lines not in a snippet: the node keeps the file, or your template renders it; their secrets)
        if [key for key, dummy in SNIPPET_LISTS if key in n["vars"]]:
            variables = dict((k, v) for k, v in n["vars"].items() if k not in dict(SNIPPET_LISTS))
            used = set(m for v in variables.values() if isinstance(v, list) for line in v if isinstance(line, str)
                       for m in SECRET_MOVED_REF.findall(line))
            used.update(n.get("snippet_refs") or [])
            n["vars"] = dict((k, v) for k, v in variables.items() if not k.startswith(SECRET_VAR) or k in used)
    nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    return read, nodes, snippets


def _common_snippet(read, having, texts):
    """The snippet text most nodes read have (the one of the first node in the hosts file on a tie), written once
    in the cluster's group_vars; None when most have none (the collection's default, on a tie too)."""
    count = {}
    for n, text in sorted(zip(having, texts), key=lambda p: _hosts_order(p[0])):
        count[text] = count.get(text, 0) + 1
    if not count:
        return None
    text = max(count, key=lambda t: count[t])  # (the first one of the highest count)
    return text if count[text] > len(read) - len(having) else None


def _snippet_of(n, key, name):
    """The text of node n's snippet of the lines of key (from the file name); lines a secret stays in (or that would
    end a raw block): all of them, as the node has them, in a variable of secrets.yml the snippet renders (set in
    n's variables, in place) -> text."""
    lines, trusted = n["vars"][key], n.get("trusted") or ()
    secret = list_secret(key, lines, trusted)
    if all(isinstance(line, str) and "endraw" not in line for line in lines) and not secret:
        return snippet_text(lines, trusted)
    var = SECRET_VAR + SECRET_FILES[name] + "_lines"
    whole = "\n".join(SECRET_MOVED_REF.sub(lambda m: str(n["vars"].get(m.group(1), m.group(0))), str(line))
                      for line in lines)
    variables = dict((k, v) for k, v in n["vars"].items() if k != key)
    used = set(m for k, v in variables.items() if isinstance(v, list) for line in v if isinstance(line, str)
               for m in SECRET_MOVED_REF.findall(line))
    n["vars"] = dict((k, v) for k, v in variables.items() if not k.startswith(SECRET_VAR) or k in used)
    n["vars"][var] = whole
    n["vars"][key] = lines  # (taken out by the caller)
    n["notes"] = list(n.get("notes") or []) + [
        "%s: its %d line(s) with a secret the import could not take out alone are in secrets.yml, %s, which its"
        " snippet renders" % (name, len(lines), var) if secret else
        "%s: its %d line(s) are in secrets.yml, %s, which its snippet renders: one has endraw, which would end the"
        " raw block a snippet writes it in (not a secret)" % (name, len(lines), var)]
    return "{{ %s }}\n" % var


def _marked(value):
    """The strings in value with a template mark."""
    if isinstance(value, dict):
        return [s for v in value.values() for s in _marked(v)]
    if isinstance(value, list):
        return [s for v in value for s in _marked(v)]
    return [value] if isinstance(value, str) and TEMPLATE_MARK.search(value) else []


def _trusted_lines(read):
    """The lines the import made that refer to a secret's variable, written as templates: not one a node has as
    its own text (a line with {{ read from a node is written !unsafe, never templated)."""
    made = set(line for n in read for line in n.get("trusted") or [])
    own = set(line for n in read for line in _marked(n["vars"]) if line not in (n.get("trusted") or []))
    return sorted(made - own)


@functools.lru_cache(maxsize=None)
def roles_defaults():
    """Every role's defaults (the variables a host gets when nothing sets them), read once: not to be changed."""
    out = {}
    for role in roles_with_defaults():
        out.update(role_file(role) or {})
    return out


@functools.lru_cache(maxsize=None)
def roles_vars():
    """Every role's vars/main.yml (what some defaults are made of: the log patterns of each series), read once:
    not to be changed; not those made of a template (rendered here as text, not as Ansible would)."""
    out = {}
    for role in roles_with_defaults():
        try:
            out.update((k, v) for k, v in (role_file(role, "vars") or {}).items() if not _marked(v))
        except (IOError, OSError):  # (no vars/main.yml)
            continue
    return out


def templated(value):
    return isinstance(value, str) and not isinstance(value, UserUnsafe) and re.search(r"\{[{%]", value) is not None


def resolved(key, values, defaults):
    """key's value as the node has it: its value read (never templated), else the collection's default (a
    template of other defaults and of the roles' vars rendered in a sandbox, a list or number again as Ansible
    would give it); MISSING when unknown (a default made of facts, a filter of Ansible's)."""
    if key in values:
        return values[key]
    if key not in defaults:
        return MISSING
    value = defaults[key]
    env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    for dummy in range(5):  # a default made of other ones
        if not templated(value):
            return value
        try:
            value = env.from_string(value).render(**dict(roles_vars(), **dict(defaults, **values)))
        except Exception:  # pylint: disable=broad-except
            return MISSING
        try:
            value = ast.literal_eval(value)  # "['/x']" -> ['/x'], "7199" -> 7199, as Ansible's templating
        except (ValueError, SyntaxError):
            pass
    return MISSING


# not read from the nodes: a value of the user's for them is never compared with a node's
NOT_READ = ("cassandra_install_url", "cassandra_install_username", "cassandra_java_tarballs",
            "cassandra_java_package", "cassandra_offline")
_TEMPLATE_KEYS = []
# the blocks of variables the import reads from every node it reads, and writes only when not the default
_READ_BLOCKS = ("Cluster & topology", "Directories", "Account & file owners", "Network & ports", "JMX",
                "JVM & heap (cassandra-env.sh, jvm*-server.options)", "Logging (logback.xml)", "systemd unit & service")


# Medusa's install, read with its version; the OS tuning the import reads (not where the role writes it)
_MEDUSA_INSTALL = ("cassandra_medusa_enabled", "cassandra_medusa_version", "cassandra_medusa_venv",
                   "cassandra_medusa_link_dir", "cassandra_medusa_profile_d", "cassandra_medusa_python")
_OS_READ = ("cassandra_linux_sysctl", "cassandra_linux_limits", "cassandra_data_readahead_kb", "cassandra_linux_timesync",
            "cassandra_linux_timesync_servers", "cassandra_linux_thp_enabled", "cassandra_linux_thp_defrag",
            "cassandra_linux_swap_off", "cassandra_linux_nproc_all", "cassandra_data_scheduler",
            "cassandra_data_lvm_readahead")
_MEDUSA_INI = []


def _medusa_ini_keys():
    """The variables medusa.ini is made of (cassandra_medusa's template), its owners and mode, its key file."""
    if not _MEDUSA_INI:
        from ansible_collections.community.cassandra.plugins.filter.cassandra_medusa import role_settings
        mapping, dummy = role_settings()
        others = ("config_user", "config_group", "config_mode", "key_file_user", "key_file_group", "key_file_mode",
                  "s3_access_key_id", "s3_secret_access_key", "extra_settings")
        _MEDUSA_INI.append(set(mapping.values()) | set("cassandra_medusa_" + k for k in others))
    return _MEDUSA_INI[0]


def _default_when_absent(key):
    return key not in NOT_READ and any(key in keys for title, keys in BLOCKS if title in _READ_BLOCKS)


def _template_keys():
    """The variables of the cassandra_config templates: the import reads the files they make whole, a variable
    it does not write there is at the collection's default on the node."""
    if not _TEMPLATE_KEYS:
        keys = set()
        for top, dummy, files in os.walk(os.path.join(ROLE, "templates")):
            for name in files:
                with open(os.path.join(top, name)) as f:
                    keys.update(re.findall(r"\b(cassandra_\w+)", f.read()))
        _TEMPLATE_KEYS.append(keys - set(NOT_READ))
    return _TEMPLATE_KEYS[0]


def group_chains(hosts):
    """The hosts file's data -> {host: [its groups, top down]}."""
    out = {}

    def walk(group, chain):
        for host in (group.get("hosts") or {}):
            out[host] = chain
        for child, sub in sorted((group.get("children") or {}).items()):
            walk(sub or {}, chain + [child])
    walk(hosts.get("all") or {}, [])
    return out


def _effective(name, chain, layout, user, mine=True):
    """The variables a host gets: the user's files and the import's (main.yml), as Ansible reads them (all, the
    groups top down, the host; for each, its dir if there is one, its files by name, else the first of
    <name>.yml, .yaml, .json) -> {key: (value, path)}. mine false: the import's dirs count, not its values."""
    out = {}
    for where in ["group_vars/all"] + ["group_vars/%s" % g for g in chain] + ["host_vars/%s" % name]:
        files = [(p, data) for p, data in user if vars_dir(p) == where]
        written = (layout.get("group_vars") or {}).get(where.split("/", 1)[1]) if where.startswith("group_vars/") \
            else (layout.get("host_vars") or {}).get(name)
        in_dir = [f for f in files if f[0].count("/") == 2]
        if in_dir or written:  # a dir: <name>.yml next to it is not read
            files = in_dir + ([(where + "/main.yml", written)] if written and mine else [])
        else:
            files = [f for ext in ("yml", "yaml", "json") for f in files if f[0] == "%s.%s" % (where, ext)][:1]
        for path, data in sorted(files, key=lambda f: f[0]):
            for key, value in data.items():
                out[key] = (value, path)
    return out


def cassandra_inventory_user_files(read, layout, password=""):
    """read: {path: text} of the vars files of the inventory dir; layout: cassandra_inventory_layout's.
    -> [{path, content}]: the user's own (not the import's) that apply to this cluster's hosts: group_vars/all,
    its groups', its hosts'; a vaulted one decrypted with password (else its content None). Not the user's: a
    file with the import's first line, a main.yml or secrets.yml laid out as an import writes it (an earlier
    release's, without that line: old_import). (The ones at the paths it writes: left out by the playbook, which
    lays out again without them.)
    e.g. {"group_vars/all.yml": "cassandra_heap_size: 8G"} -> [{"path": "group_vars/all.yml", "content":
    "cassandra_heap_size: 8G"}]."""
    groups = ["all"] + sorted(layout.get("group_vars") or {})
    hosts = [n["name"] for n in layout.get("nodes") or []]
    out = []
    for path, text in sorted((read or {}).items()):
        if not re.match(r"^(group_vars|host_vars)/[^/]+(/[^/]+)?[.](yml|yaml|json)$", path):
            continue
        kind, name = vars_dir(path).split("/", 1)
        if name not in (groups if kind == "group_vars" else hosts):
            continue
        if str(text).startswith("$ANSIBLE_VAULT"):
            text = decrypt(text, password)
            if text is None:  # not read: said in the report
                out.append({"path": path, "content": None})
                continue
        # an earlier import's, without its first line (never group_vars/all)
        earlier = name != "all" and OWNABLE.match(path) and old_import(text)
        if written_for(text) is None and not earlier:  # the import's: written again
            out.append({"path": path, "content": text})
    return out


@values_hidden
def cassandra_inventory_layout_over(nodes, cluster_name, user_files, inventory_dir="", group=""):
    """nodes, cluster_name: cassandra_inventory_layout's; user_files: cassandra_inventory_user_files' -> the layout
    of _layout_over (inventory_dir: where the inventory is, for a {{ inventory_dir }} in the path of your own
    template), without the user's files at the paths it writes (replaced, kept as <file>.<date>~: their
    values are not the user's any more), until they no longer meet; 'user_paths': the user's files counted.
    e.g. -> {"cluster_group": "prod", ..., "yours": [{"node": "db1", "key": "cassandra_heap_size", "value": "8G",
    "yours": "4G", "path": "group_vars/all.yml"}], "user_paths": ["group_vars/all.yml"]}. group: as for
    cassandra_inventory_layout."""
    files = list(user_files or [])
    for dummy in range(len(files) + 2):  # one file less each round
        layout = _layout_over(nodes, cluster_name, files, inventory_dir, group)
        written = set(f["path"] for f in cassandra_inventory_files(layout))
        kept = [f for f in files if f["path"] not in written]
        if len(kept) == len(files):
            break
        files = kept
    layout["user_paths"] = [f["path"] for f in files]
    return layout


def _yours_rendered(n, theirs, inventory_dir=""):
    """A node's files your own template renders (cassandra_config_template_overrides in a file of yours): not
    compared with the role's templates here (the self-check renders yours), so neither hand edits nor kept as
    they are (cassandra_config refuses a file both kept and overridden); the end lines carried from such a file
    that your template has itself are left out (it would render them twice); so are those your own snippet
    (cassandra_config_template_snippets) has. The files of your templates noted ('yours_templates': no snippet is
    written for them; a JVM options file's end lines stay its variable, which your template may render)."""
    snippets = (theirs.get(SNIPPETS) or (None,))[0]
    if isinstance(snippets, dict) and snippets:
        n["yours_snippets"] = dict(snippets)  # (a dict the import writes replaces yours: kept in it)
    for key, f in SNIPPET_LISTS:
        if isinstance(snippets, dict) and f in snippets and isinstance(n["vars"].get(key), list):
            _left_to_yours(n, key, f, _template_text(snippets[f], inventory_dir))
    overrides = (theirs.get("cassandra_config_template_overrides") or (None,))[0]
    if not isinstance(overrides, dict) or not overrides:
        return
    n["yours_templates"] = sorted(overrides)
    kept, edits = hand_edits_kept(dict(n, keep={"cassandra_config_keep_files": list(overrides)}))
    if kept:
        n["hand_edits"] = edits
        n["normalized"] = list(n.get("normalized") or []) + [
            "%s: your own template (cassandra_config_template_overrides), not compared with the role's here" % f
            for f in sorted(overrides) if any(line.startswith(f) for line in kept)]
    for key, f in SECRET_LISTS:
        if f in overrides and isinstance(n["vars"].get(key), list):
            _left_to_yours(n, key, f, _template_text(overrides[f], inventory_dir))
    keep = (n.get("keep") or {}).get("cassandra_config_keep_files")
    if keep:
        rest = [f for f in keep if f not in overrides]
        n["keep"] = dict((k, v) for k, v in n["keep"].items() if k != "cassandra_config_keep_files")
        if rest:
            n["keep"]["cassandra_config_keep_files"] = rest


def _template_text(path, inventory_dir):
    """The text of your template at path ({{ inventory_dir }} in it: inventory_dir), None when it cannot be read."""
    path = re.sub(r"\{\{\s*inventory_dir\s*\}\}", lambda m: inventory_dir, str(path)) if inventory_dir else str(path)
    if TEMPLATE_MARK.search(path) or not os.path.isabs(path) or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _left_to_yours(n, key, name, text):
    """The lines of key (carried from the file name) left out of n's variables when your template's text has them
    all, in their order, one after the other, blank lines aside (a JVM options file: in their order, others
    between them) (the node's lines, their moved secrets put back): it renders them
    itself. Their secrets no other line refers to too; said in its notes. (Its text as written: a line in a Jinja
    comment or a branch it does not render counts; the self-check then says what differs.)"""
    lines = n["vars"][key]
    if text is None or not lines or not all(isinstance(line, str) for line in lines):
        return
    node = [SECRET_MOVED_REF.sub(lambda m: str(n["vars"].get(m.group(1), m.group(0))), line).rstrip() for line in lines]
    node = [line for line in node if line.strip()]
    theirs = [line.rstrip() for line in text.split("\n") if line.strip()]  # (blank lines are not carried)
    if name.endswith(".options"):  # options, from anywhere in the file: in their order, others between them
        found = iter(theirs)
        if not all(line in found for line in node):
            return
    elif not any(theirs[i:i + len(node)] == node for i in range(len(theirs) - len(node) + 1)):
        return
    variables = dict((k, v) for k, v in n["vars"].items() if k != key)
    used = set(m for v in variables.values() if isinstance(v, list) for line in v if isinstance(line, str)
               for m in SECRET_MOVED_REF.findall(line))
    n["vars"] = dict((k, v) for k, v in variables.items() if not k.startswith(SECRET_VAR) or k in used)
    n["notes"] = [note for note in n.get("notes") or [] if not note.startswith(CARRIED + name + ":")] + [
        "%s: its %d line(s) at the end are in your own template (cassandra_config_template_overrides): not carried"
        % (name, len(lines))]


def user_data(user_files):
    """The user's files ([{path, content}]) -> ([(path, data)] of those that are a dict of variables, the paths of
    those not read (no content: vaulted, no password))."""
    user, unread = [], []
    for f in user_files or []:
        if f.get("content") is None:
            unread.append(f["path"])
            continue
        try:
            data = yaml.load(f["content"], Loader=UserLoader)  # nosec B506: the loader builds no object
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            user.append((f["path"], data))
    return user, unread


# What _layout_over compares the user's values with: the nodes' variables as found (live_vars: {node name: {key:
# value}}), every role's defaults, the keys not compared so far (filled as they come)
_Found = namedtuple("_Found", "live_vars defaults not_compared")


def _live_of(found, n, key):
    """The node's value of key: left as found on the node (host_vars), else read, else the collection's default ->
    the value, MISSING when unknown."""
    if key in (n.get("keep") or {}):  # left as found on the node (host_vars)
        return n["keep"][key]
    return resolved(key, found.live_vars[n["name"]], found.defaults)


def _read_on(found, n, key):
    """The node's value of key is known: read from it, left as found, or one the import writes only when
    it is not the default (the config files' and their owners', the unit's; Medusa's and the OS's when
    found there); not a variable the import cannot tell (install sources, policies)."""
    live = found.live_vars[n["name"]]
    return (key in live or key in (n.get("keep") or {}) or key in _template_keys() or _default_when_absent(key)
            or key in _medusa_ini_keys() and boolean((n.get("medusa") or {}).get("ini", False), strict=False)
            or key in _MEDUSA_INSTALL and boolean((n.get("medusa") or {}).get("install", False), strict=False)
            or key in _OS_READ and bool(n.get("os")))


def _for_node(value, name):
    """A template of the user's as it is for the node name, MISSING when the import cannot tell: the import's own
    address and host name templates as they are (a node's value equal to its fact is read as that same template),
    one of the node's name only rendered (in a sandbox), any other not."""
    if not templated(value) or value in (IPV4, HOSTNAME):
        return value
    env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    try:
        text = env.from_string(value).render(inventory_hostname=name, inventory_hostname_short=name.split(".")[0])
    except Exception:  # pylint: disable=broad-except  # (facts, other variables, filters of Ansible's)
        return MISSING
    try:
        return ast.literal_eval(text)  # "16" -> 16, as Ansible's templating
    except (ValueError, SyntaxError):
        return text


def _comparable(found, key, value, name):
    """A value of the user's as it can be compared with the node name's (a template as it is for that node), key
    added to found.not_compared when it cannot (a template the import cannot render, something vaulted) ->
    the value, MISSING."""
    if isinstance(value, Vaulted) or (isinstance(value, (dict, list)) and Vaulted in [
            type(v) for v in (value.values() if isinstance(value, dict) else value)]):
        value = MISSING
    elif templated(value):
        value = _for_node(value, name)
    if value is MISSING and key not in found.not_compared:
        found.not_compared.append(key)
    return value


def _left_to_theirs(nodes, layout, user, inventory_dir, found):
    """A value the user's files give already, as the node has it: left to theirs (taken out of the node's variables,
    in place), where Ansible reads it once written -> (from_yours: {node: {key: {value, path}}}, differ: [{node,
    key, value, yours, path}] the values of theirs a node read does not have)."""
    chains = group_chains(layout["hosts"])
    from_yours, differ = {}, []
    for n in nodes:
        name = layout["names"].get(n.get("address"), n["name"])
        theirs = _effective(name, chains.get(name, []), layout, user, mine=False)
        _yours_rendered(n, theirs, inventory_dir)
        for key in [k for k in n["vars"] if k in theirs and k not in ALWAYS and k not in ("cassandra_dc", "cassandra_rack")]:
            value = theirs[key][0]
            if key == "cassandra_install_url" and isinstance(value, str):  # (the series as the import writes it)
                value = re.sub(r"\{\{\s*cassandra_version\s*\}\}", SERIES_TOKEN, value)
            value = value if key == "cassandra_install_url" and not templated(value) else \
                _comparable(found, key, value, name)
            if value is not MISSING and _same(key, value, n["vars"][key]):
                from_yours.setdefault(name, {})[key] = {"value": n["vars"].pop(key), "path": theirs[key][1]}
        # a value of theirs the node does not have: kept as found (written by the import), listed
        if boolean(n.get("read", False), strict=False):
            for key, (value, path) in sorted(theirs.items()):
                if key in ALWAYS or key in ("cassandra_dc", "cassandra_rack") or not _read_on(found, n, key):
                    continue
                compared, live = _comparable(found, key, value, name), _live_of(found, n, key)
                if MISSING not in (compared, live) and not _same(key, compared, live):
                    differ.append({"node": name, "key": key, "value": live, "yours": value, "path": path})
    return from_yours, differ


def _write_over_theirs(nodes, layout, user, found, yours):
    """One round: the values the user's files would change on a node read written into its variables (in place),
    each listed in yours (in place) -> (whether a value was written, pins: [(node, key, value)] the values written
    that a host file of theirs still wins over)."""
    chains = group_chains(layout["hosts"])
    changed, pins = False, []
    for n in nodes:
        if not boolean(n.get("read", False), strict=False):
            continue
        name = layout["names"].get(n.get("address"), n["name"])
        own = ["group_vars/%s/main.yml" % g for g in chains.get(name, [])] + ["host_vars/%s/main.yml" % name]
        for key, (value, path) in sorted(_effective(name, chains.get(name, []), layout, user).items()):
            if path in own or not _read_on(found, n, key):
                continue
            compared = _comparable(found, key, value, name)
            live = _live_of(found, n, key)
            if live is MISSING and compared is not MISSING:
                if key not in found.not_compared:
                    found.not_compared.append(key)
            if MISSING in (compared, live) or _same(key, compared, live):
                continue
            if _same(key, n["vars"].get(key, MISSING), live):  # written, theirs wins (a host file of theirs)
                pins.append((name, key, live))
            else:
                n["vars"][key] = live
                changed = True
            if not any(y["node"] == name and y["key"] == key for y in yours):
                yours.append({"node": name, "key": key, "value": live, "yours": value, "path": path})
    return changed, pins


def _theirs_win(nodes, layout, user, yours):
    """A file of theirs that still wins (sorted after main.yml in a host's dir): said, not hidden -> [{node, key,
    path}]."""
    chains = group_chains(layout["hosts"])
    theirs_win = []
    for y in yours:
        n = next((n for n in nodes if layout["names"].get(n.get("address"), n["name"]) == y["node"]), None)
        got = _effective(y["node"], chains.get(y["node"], []), layout, user).get(y["key"])
        if n is not None and got and not _same(y["key"], got[0], y["value"]):
            theirs_win.append({"node": y["node"], "key": y["key"], "path": got[1]})
    return theirs_win


def _given(nodes, layout, user):
    """{node: {key: value}}: what your files give each node, as Ansible reads them next to the import's dirs;
    YOURS_WIN when it is a file that wins over the cluster's group_vars/<cluster group>/main.yml (a host's, a DC's
    or rack's, one of the cluster group's dir sorted after main.yml)."""
    chains = group_chains(layout["hosts"])
    out = {}
    for n in nodes:
        name = layout["names"].get(n.get("address"), n["name"])
        chain = chains.get(name, [])
        mine = out.setdefault(name, {})
        for key, (value, path) in _effective(name, chain, layout, user, mine=False).items():
            where = vars_dir(path)
            wins = where.startswith("host_vars/") or where.split("/", 1)[1] in chain[1:] or (
                chain and where == "group_vars/" + chain[0] and path > where + "/main.yml")
            mine[key] = YOURS_WIN if wins else value
    return out


def _layout_over(nodes, cluster_name, user_files, inventory_dir="", group=""):
    """cassandra_inventory_layout, with the user's own variables files of the inventory dir (user_files: [{path,
    content}], group_vars/all and those of this cluster's groups and hosts) taken into account: a node whose value
    one of them would change gets its value written (the node as it is wins), and is listed in 'yours': [{node,
    key, value, yours, path}] (so is one the import writes anyway, a value that differs between nodes); a value
    of theirs equal to the node's is not written again ('from_yours': {node: {key: {value, path}}}). 'not_compared': the keys
    of theirs not compared (a template, a vaulted value, a default not known here); 'unread_files': theirs not read
    (vaulted, no password); 'theirs_win': [{node, key, path}] a file of theirs still wins over the import's."""
    user, unread = user_data(user_files)
    defaults = roles_defaults()
    nodes = [dict(n, vars=dict(n.get("vars") or {})) for n in nodes]
    found = _Found(dict((n["name"], dict(n["vars"])) for n in nodes), defaults, [])  # (live_vars: as found)
    yours = []
    layout = cassandra_inventory_layout(nodes, cluster_name, group)
    from_yours, differ = _left_to_theirs(nodes, layout, user, inventory_dir, found)
    layout = cassandra_inventory_layout(nodes, cluster_name, group, _given(nodes, layout, user))
    for dummy in range(3):  # each round writes the values theirs would change; the layout again
        changed, pins = _write_over_theirs(nodes, layout, user, found, yours)
        if not changed:
            for name, key, live in pins:
                layout["host_vars"].setdefault(name, {})[key] = live
            break
        layout = cassandra_inventory_layout(nodes, cluster_name, group, _given(nodes, layout, user))
    theirs_win = _theirs_win(nodes, layout, user, yours)
    for d in differ:  # the ones the import writes anyway (a value that differs between nodes) too
        if not any(y["node"] == d["node"] and y["key"] == d["key"] for y in yours):
            yours.append(d)
    layout["yours"] = sorted(yours, key=lambda y: (y["key"], y["node"]))
    layout["user_keys"] = sorted(set(k for dummy, data in user for k in data))
    # the variables of your files no longer read (a release candidate's import wrote them): the playbooks refuse them
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import REMOVED
    layout["removed_in_yours"] = sorted((path, k) for path, data in user for k in data if k in REMOVED)
    layout["from_yours"] = from_yours
    layout["not_compared"] = sorted(found.not_compared)
    layout["unread_files"] = unread
    layout["theirs_win"] = theirs_win
    return layout


def _by_nodes(pairs):
    """pairs: [(node name, lines)] -> report lines, each line once under the
    nodes that have it: the ones every node has first."""
    order, where = [], {}
    for name, lines in pairs:
        for line in lines:
            if line not in where:
                where[line] = []
                order.append(line)
            if name not in where[line]:
                where[line].append(name)
    groups = []
    for line in order:
        names = tuple(sorted(where[line]))
        group = next((g for g in groups if g[0] == names), None)
        if group is None:
            groups.append((names, [line]))
        else:
            group[1].append(line)
    out = []
    for names, lines in sorted(groups, key=lambda g: len(g[0]) != len(pairs)):
        out.append("  On every node read:" if len(names) == len(pairs) and len(pairs) > 1 else "  On %s:" % ", ".join(names))
        out += ["    " + line for line in lines]
    return out


def _os_section(read):
    """The OS tuning found on the nodes (import_cluster's cassandra_os_import)."""
    nodes = [n for n in read if n.get("os")]
    if not nodes:
        return []
    out = ["OS TUNING FOUND ON THE NODES (read only, compared with what cassandra_linux and cassandra_service set):"]
    out += _by_nodes([(n["name"], n["os"].get("lines") or []) for n in nodes])
    carried = _by_nodes([(n["name"], n["os"].get("carried") or []) for n in nodes])
    if carried:
        out += ["", "OS TUNING CARRIED INTO THE VARIABLES, for the nodes added later (the nodes above with"
                " cassandra_linux_manage false are left as they are):"] + carried
    return out + [""]


_IPV4 = re.compile(r"^[0-9]+(?:[.][0-9]+){3}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?(?:[.][A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9])?)*$")


def _getent(text):
    """getent hosts output -> {address: [names]} and {name: [addresses]}."""
    names, addresses = {}, {}
    for line in (text or "").splitlines():
        fields = line.split()
        for name in fields[1:]:
            names.setdefault(fields[0], []).append(name)
            addresses.setdefault(name, []).append(fields[0])
    return names, addresses


def cassandra_ring_names(on_node, addresses, given):
    """on_node: getent hosts <addresses> on the given node that read the ring;
    addresses: the nodes found in the ring; given: that given node's
    inventory name. Returns {address: the name to reach it by}: the name the
    given node knows it by, in the form the given name has (its short name,
    or the fqdn when the given name has a domain); else the address (a given
    node named by its address: the others too).
    e.g. ("10.0.0.2  db2.example.com db2", ["10.0.0.2"], "db1") -> {"10.0.0.2": "db2"}; given "db1.example.com":
    {"10.0.0.2": "db2.example.com"}."""
    if _IPV4.match(str(given)) or ":" in str(given):
        return dict((a, a) for a in addresses)
    names, dummy = _getent(on_node)
    out = {}
    for address in addresses:
        found = [n for n in names.get(address) or [] if _HOSTNAME.match(n)]
        fqdn = next((n for n in found if "." in n), "")
        out[address] = (fqdn if "." in str(given) else (found[0].split(".")[0] if found else "")) or address
    return out
