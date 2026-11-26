# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_suspicious_settings: {node: its variables} -> the findings of
SUSPICIOUS on some nodes (the health_check playbook's SETTINGS TO REVIEW block and the
import_cluster report say them; nothing is refused), two lines each: the
setting, its value, the nodes and why it matters; what to do, and how to keep
it on purpose (cassandra_settings_kept_on_purpose)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import as_bool

KEPT = "cassandra_settings_kept_on_purpose"
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
        return [("auto_bootstrap false on {nodes}: a node that loses its data would restart empty, without streaming"
                 " it back", "remove cassandra_auto_bootstrap (the default is true)")]
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
                why = ("the node is bootstrapped already: Cassandra refuses its next start with this flag"
                       if not m.group(1) else
                       "the node is a member of the ring: ignored now, it would replace a node again once its data is"
                       " wiped")
                remove = ("remove the line %s" % str(line).strip()) if whole else ("remove %s from %s" % (flag, where))
                found.append(("replace flag %s in %s on {nodes}: %s" % (flag, where, why), remove))
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
                continue  # commented out: sets nothing
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
        setting, change = "heap -Xmx%s in %s" % (heap, where), "change -Xmx in %s" % where
    elif values.get("cassandra_heap_size"):
        heap = values["cassandra_heap_size"]
        setting, change = "heap %s (cassandra_heap_size)" % heap, "set cassandra_heap_size"
    elif isinstance(environment, dict) and environment.get("MAX_HEAP_SIZE"):
        heap = environment["MAX_HEAP_SIZE"]
        setting, change = "heap %s (cassandra_service_environment.MAX_HEAP_SIZE)" % heap, "set cassandra_heap_size"
    else:
        setting, change = "heap not set", "set cassandra_heap_size"
    ram = _ram(values)
    top = min(31, ram // 2) if ram else 31
    if ram and top < 2:
        return [("%s on {nodes}: the host has %dG of RAM, too little for Cassandra (8G or more)" % (setting, ram),
                 "give the host more RAM")]
    room = "Cassandra needs 2G to %dG (%sunder 32G)" % (top, "at most half the RAM, " if ram else "")
    if not heap:
        # 4.x's cassandra-env.sh stops its auto-sizing at 8G (5.0's at half the RAM, 31G at most); 60G: a 64G host
        if ram and ram >= 60 and str(values.get("cassandra_version") or "50x") in ("40x", "41x"):
            return [("%s on {nodes}: Cassandra 4.x sizes it to 8G at most, with %dG of RAM; %s" % (setting, ram, room),
                     change)]
        return []
    size = _size(heap)
    if size is None:
        return []
    if size < 2 * GIB:
        return [("%s on {nodes}: too small, %s" % (setting, room), change)]
    if size >= 32 * GIB:
        return [("%s on {nodes}: 32G or more loses the JVM's compressed pointers, 31G holds more; %s" % (setting, room),
                 change)]
    if ram and size > ram * GIB // 2:
        return [("%s on {nodes}: more than half of the %dG of RAM, which the page cache and off-heap memory need; %s"
                 % (setting, ram, room), change)]
    return []


def _authenticator(values, node):
    """No client authentication, and a native transport address set to other than loopback (rpc_address; "": the
    hostname's; an rpc_interface of the extra settings). Only the settings are read: not who can connect."""
    authenticator = str(values.get("cassandra_authenticator") or "AllowAllAuthenticator").strip()
    rpc = values.get("cassandra_rpc_address", "localhost")
    extra = values.get("cassandra_extra_settings")
    not_loopback = bool(isinstance(extra, dict) and extra.get("rpc_interface")) or not LOCAL.match(
        str(rpc if rpc is not None else "localhost").strip())
    if authenticator.rsplit(".", maxsplit=1)[-1] == "AllowAllAuthenticator" and not_loopback:
        return [("no authentication (AllowAllAuthenticator) on {nodes}, whose client address (rpc_address or"
                 " rpc_interface) is not loopback: a client that can connect to it needs no login",
                 "set an authenticator (cassandra_authenticator: PasswordAuthenticator)")]
    return []


FAILURE_POLICY = {
    "cassandra_disk_failure_policy": "the node stays up with a failed data disk, its reads and writes there failing",
    "cassandra_commit_failure_policy": "the node acknowledges writes its commit log could not keep",
}


def _failure_policy(values, node):
    return [("%s ignore on {nodes}: %s" % (key[len("cassandra_"):], why), "set %s: stop (the default) or die" % key)
            for key, why in sorted(FAILURE_POLICY.items()) if str(values.get(key) or "").strip().lower() == "ignore"]


def _yaml_bytes(text):
    """A cassandra.yaml size (640000KiB, 1024MiB, 16MB, 5KiB) in bytes, else None."""
    m = re.match(r"^\s*(\d+)\s*(B|KiB|MiB|GiB|KB|MB|GB)?\s*$", str(text))
    if not m:
        return None
    unit = (m.group(2) or "B").replace("i", "").replace("B", "") or "B"
    return int(m.group(1)) * {"B": 1, "K": 1024, "M": 1024 ** 2, "G": GIB}[unit]


# (variable, Cassandra's default, the size above which it is said, why it matters)
LARGE = (
    ("cassandra_batch_size_fail_threshold", "50KiB", 1024 ** 2,
     "a batch this big is accepted, and a big batch weighs on the memory of the node that coordinates it"),
    ("cassandra_native_transport_max_frame_size", "16MiB", 64 * 1024 ** 2,
     "a client may send requests this big, held whole in the node's memory"),
)


def _large(values, node):
    found = []
    for key, default, most, why in LARGE:
        size = _yaml_bytes(values.get(key, ""))
        if size is not None and size > most:
            found.append(("%s %s on {nodes} (Cassandra's default: %s): %s"
                          % (key[len("cassandra_"):], values[key], default, why),
                          "lower %s, once the applications are known not to need it" % key))
    return found


def _symbolic(text):
    """A symbolic mode (u=rw,go=r; a=r,go-r; go+r) applied in order -> whether its group or others can read the
    file: their read bit as its clauses set it (=, +r, -r set it whatever the mode before); None when they leave it
    to the mode before (not known here), or when it is no symbolic mode."""
    read = {"u": None, "g": None, "o": None}  # (the read bit of each class: None while no clause set it)
    for clause in text.split(","):
        found = re.match(r"^([ugoa]*)([-+=])([rwxXst]*)$", clause)
        if not found:
            return None
        who, op, perms = found.groups()
        for c in set(who.replace("a", "ugo") or "ugo"):
            if op == "=" or "r" in perms:
                read[c] = "r" in perms if op == "=" else op == "+"
    if read["g"] or read["o"]:
        return True
    return False if read["g"] is False and read["o"] is False else None


def _readable(mode):
    """A file mode that lets users other than the owner read the file: octal ("0644", "644", "0o644"; 420, YAML's
    0644 unquoted) or symbolic ("u=rw,g=r", applied in order); None when it is neither, or a symbolic one leaves
    the read bits to the mode before ("u=rw": not known, no finding either way)."""
    if isinstance(mode, int) and not isinstance(mode, bool):
        return bool(mode & 0o044)
    text = str(mode or "").strip()
    if re.match(r"^(0o?)?[0-7]{3,4}$", text):  # (0644, 644, 0o644)
        return bool(int(text.replace("o", ""), 8) & 0o044)
    return _symbolic(text) if text else None


def _shown(mode):
    """A mode as the inventory writes it (YAML's 0644 unquoted is the number 420)."""
    return "0%o" % mode if isinstance(mode, int) and not isinstance(mode, bool) else str(mode)


def _where(values, plural=False):
    """How a finding says where a mode comes from: the node's file as the import read it, else the inventory."""
    verb = "are" if plural else "is"
    return verb + (" mode %s on {nodes}" if LINES in values else " set to mode %s in the inventory for {nodes}")


def _jmx_passwords_readable(values, node):
    """jmxremote.password readable by other users than its owner (its mode, as the inventory keeps it)."""
    perms = values.get("cassandra_config_file_permissions")
    entry = perms.get("jmxremote.password") if isinstance(perms, dict) else None
    mode = entry.get("mode") if isinstance(entry, dict) else entry  # ({mode, owner, group}, or the mode)
    if mode is not None and _readable(mode):
        return [(("jmxremote.password " + _where(values) + ": users other than its owner can read the JMX passwords")
                 % _shown(mode), "make it 0600: mode 0600 for jmxremote.password in cassandra_config_file_permissions,"
                 " then apply_config")]
    return []


def _key_file(values):
    """Whether the role writes Medusa's storage key file: S3 keys given, and a key file to write them in (the default
    one unless set to "")."""
    named = values.get("cassandra_medusa_key_file") if "cassandra_medusa_key_file" in values else "default"
    return bool(str(values.get("cassandra_medusa_s3_access_key_id") or "").strip()) and bool(str(named or "").strip())


def _medusa_secrets_readable(values, node):
    """medusa.ini and Medusa's storage key file (when the role writes one) readable by other users than their owner
    (their modes, as the inventory sets them; the key file's follows medusa.ini's unless set); none when Medusa is
    not on (cassandra_medusa_enabled: the role writes neither)."""
    found = []
    if as_bool(values.get("cassandra_medusa_enabled", False)) is not True:  # (as the playbooks read it: | bool)
        return found
    config, key_file = values.get("cassandra_medusa_config_mode"), values.get("cassandra_medusa_key_file_mode")
    own_key_mode = "cassandra_medusa_key_file_mode" in values
    if _readable(config):
        both = _key_file(values) and not own_key_mode
        files = "medusa.ini and the storage key file (its mode follows)" if both else "medusa.ini"
        found.append(((files + " " + _where(values, both) + ": users other than their owner can read Medusa's secrets")
                      % _shown(config), "remove cassandra_medusa_config_mode (the default is 0600), then apply_config"))
    if _key_file(values) and own_key_mode and _readable(key_file):
        found.append((("the storage key file of Medusa " + _where(values) + ": users other than its owner can read the"
                       " storage credentials") % _shown(key_file),
                      "remove cassandra_medusa_key_file_mode (the default follows cassandra_medusa_config_mode, 0600),"
                      " then apply_config"))
    return found


# (id, the check: (the node's variables, {name, member}) -> [(the finding, "{nodes}" where the nodes go, what to
# do)]); add a row for another one
SUSPICIOUS = [
    ("auto_bootstrap_false", _auto_bootstrap),
    ("replace_address", _replace),
    ("heap_size", _heap),
    ("allow_all_authenticator", _authenticator),
    ("failure_policy_ignore", _failure_policy),
    ("large_sizes", _large),
    ("jmx_passwords_readable", _jmx_passwords_readable),
    ("medusa_secrets_readable", _medusa_secrets_readable),
]


def _kept(values):
    """The ids of cassandra_settings_kept_on_purpose: a list, or a comma-separated string (anything else: none)."""
    kept = values.get(KEPT)
    if isinstance(kept, str):
        kept = kept.split(",")
    return set(str(a).strip() for a in kept) if isinstance(kept, (list, tuple, set)) else set()


def suspicious(nodes, members=None):
    """nodes: {name: {variable: value}} (a node's hostvars will do); members: the names in the ring (None: not
    known, the rows that need it say nothing) -> [(row id, the names, the finding with "{nodes}" where the nodes
    go, what to do)], in the order of SUSPICIOUS, without the nodes that keep a row on purpose
    (cassandra_settings_kept_on_purpose)."""
    members = None if members is None else set(str(m) for m in members)
    found = []
    for row, check in SUSPICIOUS:
        texts = {}
        for name, values in (nodes or {}).items():
            values = values or {}
            if row in _kept(values):
                continue
            node = {"name": str(name), "member": members is not None and str(name) in members}
            for text, fix in check(values, node):
                texts.setdefault((text, fix), []).append(str(name))
        found += [(row, names, text, fix) for (text, fix), names in texts.items()]
    return found


def how_to_keep(row):
    """What silences a row on a node that keeps the setting on purpose."""
    return "kept on purpose: list %s in %s" % (row, KEPT)


def finding_lines(row, names, text, fix, who=None):
    """A finding as two lines: the setting, its value, the nodes and why it matters; then what to do and how to
    accept it. who: the nodes' text (default: cassandra_output's nodes())."""
    return [text.format(nodes=who or out.nodes(names)), "  %s; %s" % (fix, how_to_keep(row))]


def unknown_kept(nodes):
    """[(an id of cassandra_settings_kept_on_purpose no row has, the nodes)]: a typo silences nothing."""
    ids = set(row for row, dummy in SUSPICIOUS)
    found = {}
    for name, values in (nodes or {}).items():
        for a in sorted(_kept(values or {}) - ids - set([""])):
            found.setdefault(a, []).append(str(name))
    return sorted(found.items())


def unknown_lines(nodes, who=None):
    """A line per id of cassandra_settings_kept_on_purpose no check has: "cassandra_settings_kept_on_purpose: no
    check is called X (node4); the ids are ..."."""
    return ["%s: no check is called %s (%s); the ids are %s" % (
        KEPT, a, (who or out.nodes)(names), ", ".join(row for row, dummy in SUSPICIOUS)) for a, names in unknown_kept(nodes)]


def cassandra_suspicious_settings(nodes, members=None):
    """Filter: the findings of suspicious(nodes, members), two lines each (finding_lines), then a line per id of
    cassandra_settings_kept_on_purpose no check has (health_check's SETTINGS TO REVIEW block).
    -> lines; none: []."""
    lines = []
    for row, names, text, fix in suspicious(nodes, members):
        lines += finding_lines(row, names, text, fix)
    return lines + unknown_lines(nodes)


def cassandra_restart_blockers(nodes, members=None):
    """Filter: the findings that make a member's NEXT START fail (a replace flag left on it), as WARNING lines for
    the plan of every operation (they are not posture: the next restart, planned or not, would fail). Two lines
    each: "WARNING  <finding>", then what to do. -> lines; none: []."""
    lines = []
    for row, names, text, fix in suspicious(nodes, members):
        if row == "replace_address":
            lines += ["WARNING  " + text.format(nodes=out.nodes(names)), "  %s before any restart" % fix]
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_suspicious_settings": cassandra_suspicious_settings,
                "cassandra_restart_blockers": cassandra_restart_blockers}
