# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Owner, group and mode of the files cassandra_config writes.

cassandra_file_permissions: a file name, the role's variables -> the owner,
    group and mode the role gives that file.
cassandra_permission_changes: the stat of the files on a node, the role's
    variables -> the changes of owner, group or mode the role makes there, for
    its report.
cassandra_permissions_import: what import_cluster read on a node (the account
    Cassandra runs as, the stat of the config files, the JMX users' files and
    the directories) -> {'vars', 'notes', 'files'}: the variables that give the
    files the owner, group and mode they have, the report's notes, and the
    files' owner, group and mode for the self-check.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re

import yaml

from ansible.errors import AnsibleFilterError

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
JMX_FILES = ("jmxremote.password", "jmxremote.access")
# cassandra_config's defaults
DEFAULTS = {"user": "cassandra", "group": "cassandra", "owner": "root", "mode": "0640", "public_mode": "0644",
            "jmx_mode": "0400"}


@functools.lru_cache(maxsize=None)
def _all_files():
    with open(os.path.join(ROLE, "vars", "main.yml")) as f:
        return yaml.safe_load(f)["_cassandra_config_files"]


def _files_of(series):
    return _all_files()[SERIES[series]]


def restricted(name):
    """cassandra.yaml (keystore passwords) and the JVM options files (their extra
    options, e.g. -Djavax.net.ssl.keyStorePassword=): cassandra_config_mode."""
    return name == "cassandra.yaml" or bool(re.match(r"^jvm\d*-server\.options$", name))


def mode_text(mode):
    """A mode as 4 octal digits: a number as is (an unquoted 0644 read by YAML
    is 420), a text of octal digits in octal; anything else as given."""
    if isinstance(mode, bool):
        return str(mode)
    if isinstance(mode, int):
        return "%04o" % mode
    text = str(mode).strip()
    if re.match(r"^[0-7]{1,5}$", text):
        return "%04o" % int(text, 8)
    return text


def cassandra_file_permissions(name, settings):
    """name: a file cassandra_config writes; settings: {owner, group, mode,
    public_mode, files, user, user_group} (cassandra_config_owner, _group,
    _mode, _public_mode, _file_permissions, cassandra_user, cassandra_group)
    -> {owner, group, mode}: cassandra.yaml and the JVM options files get
    mode, the other config files public_mode, the JMX users' files are
    cassandra_user's, 0400; a file listed in files gets what it sets over that."""
    per_files = settings.get("files") or {}
    if not isinstance(per_files, dict):
        raise AnsibleFilterError("cassandra_config_file_permissions must be a dict {file name: {owner, group, mode}}")
    known = set(JMX_FILES).union(*[set(v) for v in _all_files().values()])
    unknown = sorted(set(per_files) - known)
    if unknown:
        raise AnsibleFilterError("cassandra_config_file_permissions: %s not a file cassandra_config writes (%s)"
                                 % (", ".join(map(str, unknown)), ", ".join(sorted(known))))
    if name in JMX_FILES:
        out = {"owner": settings["user"], "group": settings["user_group"], "mode": DEFAULTS["jmx_mode"]}
    else:
        out = {"owner": settings["owner"], "group": settings["group"],
               "mode": settings["mode"] if restricted(name) else settings["public_mode"]}
    per_file = per_files.get(name) or {}
    if not isinstance(per_file, dict) or set(per_file) - set(out):
        raise AnsibleFilterError("cassandra_config_file_permissions: %s must map to owner, group and/or mode" % name)
    out.update(per_file)
    out = {"owner": str(out["owner"]), "group": str(out["group"]), "mode": mode_text(out["mode"])}
    if not re.match(r"^[0-7]{4}$", out["mode"]):
        raise AnsibleFilterError("%s: mode %s: give it in octal, quoted (e.g. \"0640\")" % (name, out["mode"]))
    return out


def same_owner(wanted, name, number):
    """The node's owner (its name, or its number when it has none) is the one wanted (a name or a number)."""
    wanted = str(wanted)
    return wanted == str(name) or (wanted.isdigit() and str(number) == wanted)


def _differ(wanted, found):
    return not (same_owner(wanted["owner"], found["owner"], found.get("uid"))
                and same_owner(wanted["group"], found["group"], found.get("gid"))
                and mode_text(wanted["mode"]) == mode_text(found["mode"]))


def permission_differences(wanted, found):
    """wanted: {file: {owner, group, mode}} the roles would set; found: {file:
    {owner, group, mode, uid, gid}, or None when it could not be read} the node
    has -> one line per file that differs."""
    out = []
    for name in sorted(wanted):
        if name not in found:
            continue  # not on the node: said elsewhere
        if found[name] is None:
            out.append("%s: owner, group and mode not read on the node, the roles may change them" % name)
        elif _differ(wanted[name], found[name]):
            out.append("%s: owner:group mode: node has %s:%s %s, import would write %s:%s %s"
                       % (name, found[name]["owner"], found[name]["group"], mode_text(found[name]["mode"]),
                          wanted[name]["owner"], wanted[name]["group"], mode_text(wanted[name]["mode"])))
    return out


def _stat(result):
    """An ansible.builtin.stat result's stat -> {owner, group, mode, uid, gid} (a name, else the number)."""
    return {"owner": str(result.get("pw_name") or result.get("uid")), "group": str(result.get("gr_name") or result.get("gid")),
            "mode": mode_text(result.get("mode")), "uid": result.get("uid"), "gid": result.get("gid")}


def cassandra_permission_changes(results, settings, name_key="cassandra_config_file"):
    """results: the results of a stat loop over the files (each with its file
    name at name_key); settings: as for cassandra_file_permissions -> [{item,
    before, after}] for the files there whose owner, group or mode the role changes."""
    out = []
    for r in results:
        st = r.get("stat") or {}
        if not st.get("exists"):
            continue
        found, wanted = _stat(st), cassandra_file_permissions(r[name_key], settings)
        if _differ(wanted, found):
            out.append({"item": "%s (owner:group mode)" % st.get("path"),
                        "before": "%s:%s %s" % (found["owner"], found["group"], found["mode"]),
                        "after": "%s:%s %s" % (wanted["owner"], wanted["group"], wanted["mode"])})
    return out


def _common(values, prefer):
    """The value most files have; on a tie, prefer if it is one of them, else the first in order."""
    counts = dict((v, values.count(v)) for v in values)
    top = max(counts.values())
    best = sorted(v for v, c in counts.items() if c == top)
    return prefer if prefer in best else best[0]


def cassandra_permissions_import(files, account, series, dirs=None, jmx=True):
    """files: {name: stat} of the config files and the JMX users' files (an
    ansible.builtin.stat result's stat: exists false when missing, {} when not
    read); account: {user, group} Cassandra runs as ('': not read); series:
    40x, 41x, 50x; dirs: {path: stat} of the data, commitlog, hints,
    saved_caches and log directories; jmx: the JMX users are imported (the
    role writes their files)."""
    if series not in SERIES:
        raise AnsibleFilterError("cassandra_permissions_import: unsupported series %s" % series)
    files = dict((name, st) for name, st in (files or {}).items() if jmx or name not in JMX_FILES)
    found = dict((name, _stat(st)) for name, st in files.items() if (st or {}).get("exists"))
    # a stat that failed (no exists at all): None, for the self-check
    unread = dict((name, None) for name, st in files.items() if not st)
    out, notes = {}, []
    user, group = str(account.get("user") or ""), str(account.get("group") or "")
    if not user or not group:
        notes.append("The account Cassandra runs as could not be read: %s:%s assumed"
                     % (user or DEFAULTS["user"], group or DEFAULTS["group"]))
    user, group = user or DEFAULTS["user"], group or DEFAULTS["group"]
    if user != DEFAULTS["user"]:
        out["cassandra_user"] = user
    if group != DEFAULTS["group"]:
        out["cassandra_group"] = group
    if out:
        notes.append("Cassandra runs as %s:%s (cassandra_user, cassandra_group)" % (user, group))

    config = [name for name in _files_of(series) if name in found]
    settings = {"owner": DEFAULTS["owner"], "group": group, "mode": DEFAULTS["mode"],
                "public_mode": DEFAULTS["public_mode"], "user": user, "user_group": group}
    if config:
        yaml_file = found.get("cassandra.yaml", {})
        settings["owner"] = _common([found[n]["owner"] for n in config], yaml_file.get("owner"))
        settings["group"] = _common([found[n]["group"] for n in config], yaml_file.get("group"))
        closed = [found[n]["mode"] for n in config if restricted(n)]
        public = [found[n]["mode"] for n in config if not restricted(n)]
        settings["mode"] = _common(closed, yaml_file.get("mode")) if closed else DEFAULTS["mode"]
        settings["public_mode"] = _common(public, DEFAULTS["public_mode"]) if public else DEFAULTS["public_mode"]
        for key, var, default in (("owner", "cassandra_config_owner", DEFAULTS["owner"]),
                                  ("group", "cassandra_config_group", group),  # its default: cassandra_group
                                  ("mode", "cassandra_config_mode", DEFAULTS["mode"]),
                                  ("public_mode", "cassandra_config_public_mode", DEFAULTS["public_mode"])):
            if settings[key] != default:
                out[var] = settings[key]
    per_file = {}
    for name in config + [n for n in JMX_FILES if n in found]:
        want = cassandra_file_permissions(name, dict(settings, files={}))
        diff = dict((k, found[name][k]) for k in ("owner", "group", "mode") if found[name][k] != want[k])
        if diff:
            per_file[name] = diff
    if per_file:
        out["cassandra_config_file_permissions"] = per_file
        notes.append("Files whose owner, group or mode differ from the others' (kept as they are with"
                     " cassandra_config_file_permissions): "
                     + ", ".join("%s %s:%s %s" % (n, found[n]["owner"], found[n]["group"], found[n]["mode"])
                                 for n in sorted(per_file)))
    env = found.get("cassandra-env.sh")
    if env and re.match(r"^[0-7]+$", env["mode"]) and not int(env["mode"], 8) & 0o004:
        notes.append("cassandra-env.sh is not readable by other users (%s:%s %s): nodetool run by a user outside %s"
                     " cannot read the JMX port in it (kept as the node has it; the roles' default is 0644)"
                     % (env["owner"], env["group"], env["mode"], env["group"]))
    odd = []
    for path in sorted(dirs or {}):
        st = dirs[path] or {}
        if not st.get("exists"):
            continue
        d = _stat(st)
        if not (same_owner(user, d["owner"], d["uid"]) and same_owner(group, d["group"], d["gid"])):
            odd.append("%s %s:%s %s" % (path, d["owner"], d["group"], d["mode"]))
    if odd:
        notes.append("Directories not owned by %s:%s, the account Cassandra runs as (cassandra_config leaves them"
                     " as they are; it creates the missing ones %s:%s 0750): %s" % (user, group, user, group, ", ".join(odd)))
    return {"vars": out, "notes": notes, "files": dict(found, **unread)}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_file_permissions": cassandra_file_permissions,
            "cassandra_permission_changes": cassandra_permission_changes,
            "cassandra_permissions_import": cassandra_permissions_import,
        }
