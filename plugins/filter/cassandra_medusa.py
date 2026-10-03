# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters for Cassandra Medusa's medusa.ini (cassandra_medusa role, import_cluster).

cassandra_medusa_ini: INI text -> {section: {key: value}}, keys lower case
    (Medusa reads them with configparser, case-insensitive), {} when unreadable.
cassandra_medusa_ini_changes: old text, new text, name -> the settings that
    change, as [{item, before, after}], secrets masked. Comments, order and
    spacing don't count, nor the case of true/false.
cassandra_medusa_import: a node's medusa.ini, its S3 credentials file and what
    was found about the install -> cassandra_medusa variables and report notes.

Their errors do not quote the files, which hold passwords.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import configparser
import os
import re

import yaml

from ansible.errors import AnsibleFilterError

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_medusa")
# s('section', 'key', variable ...) in the role's template
SETTING = re.compile(r"\bs\('(\w+)', '(\w+)', (cassandra_medusa_\w+)")
SECRET_KEY = re.compile(r"password|passwd|secret|_kspw$|_tspw$|sse_c_key|access_key", re.I)
BOOLEANS = ("true", "false")


def _parse(text):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text or "")
    return {s: dict(parser.items(s, raw=True)) for s in parser.sections()}


def cassandra_medusa_ini(text):
    try:
        return _parse(text)
    except configparser.Error:
        return {}


def _norm(value):
    value = str(value).strip()
    return value.lower() if value.lower() in BOOLEANS else value


def _flat(sections):
    return {(s, k): _norm(v) for s, keys in sections.items() for k, v in keys.items()}


def _shown(key, value):
    if value is None:
        return "absent"
    return "****" if SECRET_KEY.search(key) else value


def cassandra_medusa_ini_changes(old_text, new_text, name="medusa.ini"):
    """What writing new_text over old_text changes; old_text "" is a missing file."""
    try:
        new = _flat(_parse(new_text))
    except configparser.Error:
        raise AnsibleFilterError("cassandra_medusa_ini_changes: the new %s is not valid INI "
                                 "(message hidden: it may show a password)" % name)
    try:
        old = _flat(_parse(old_text))
    except configparser.Error:
        return [{"item": name, "before": "not valid INI", "after": "rewritten"}]
    out = []
    for section, key in sorted(set(old) | set(new)):
        before, after = old.get((section, key)), new.get((section, key))
        if before != after:
            out.append({"item": "%s [%s] %s" % (name, section, key),
                        "before": _shown(key, before), "after": _shown(key, after)})
    return out


def _role_settings():
    with open(os.path.join(ROLE, "templates", "medusa.ini.j2")) as f:
        mapping = {(section, key.lower()): var for section, key, var in SETTING.findall(f.read())}
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        defaults = yaml.safe_load(f)
    return mapping, defaults


def _credentials(text, profile):
    """The two S3 keys of an AWS credentials file the role would write the
    same, else None (a file with more in it is left alone)."""
    try:
        sections = _parse(text)
    except configparser.Error:
        return None
    keys = sections.get(profile or "default")
    if len(sections) != 1 or not keys or set(keys) != {"aws_access_key_id", "aws_secret_access_key"}:
        return None
    return keys["aws_access_key_id"], keys["aws_secret_access_key"]


def _perm(name, value):
    """An owner or group as is, a mode as 4 octal digits ("600" -> "0600")."""
    value = str(value or "")
    return ("0" + value.lstrip("0")).rjust(4, "0") if name == "mode" and value else value


def cassandra_medusa_import(ini_text, credentials_text=None, found=None):
    """found: {venv, python, version, link_dir, package, bin, login, profile_d, hint_missing, ini_owner, ini_group,
    ini_mode, key_owner, key_group, key_mode}
    (what import_cluster read on the node, "" when unknown).
    -> {'vars': cassandra_medusa variables, 'notes': [report lines], 'keep': the ones for this node
    only (host_vars: new nodes get the role's)}"""
    found = found or {}
    try:
        sections = _parse(ini_text)
    except configparser.Error:
        return {"vars": {}, "notes": ["Medusa: /etc/medusa/medusa.ini is not valid INI, NOT imported"]}
    mapping, defaults = _role_settings()
    out, extras = {}, {}
    for section, keys in sections.items():
        for key, value in keys.items():
            var = mapping.get((section, key))
            # "key =" written empty: the role leaves out a variable set to ""
            if var and value.strip() != "":
                out[var] = value
            else:
                extras.setdefault(section, {})[key] = value
    # the role writes the password only without the file: both kept as they are
    cassandra = sections.get("cassandra", {})
    if "nodetool_password" in cassandra and cassandra.get("nodetool_password_file_path"):
        out.pop("cassandra_medusa_nodetool_password", None)
        extras.setdefault("cassandra", {})["nodetool_password"] = cassandra["nodetool_password"]
    # a key absent from the file: "" for the role to leave it out too
    for var in mapping.values():
        if var not in out and str(defaults.get(var, "")) != "":
            out[var] = ""
    # a key equal to the role default needs no variable (defaults built from others do)
    for var in list(out):
        default = defaults.get(var)
        if default is not None and "{{" not in str(default) and _norm(out[var]) == _norm(default):
            del out[var]
    if extras:
        out["cassandra_medusa_extra_settings"] = extras

    notes = []
    key_file = sections.get("storage", {}).get("key_file", "")
    if key_file:
        keys = _credentials(credentials_text, sections.get("storage", {}).get("api_profile", ""))
        if keys:
            out["cassandra_medusa_s3_access_key_id"], out["cassandra_medusa_s3_secret_access_key"] = keys
        else:
            notes.append("Medusa key file %s kept as it is (not an AWS credentials file with only the two"
                         " keys, or unreadable): the role does not manage it" % key_file)
    # owner and mode of medusa.ini, and of the key file the role manages when they differ
    for name in ("owner", "group", "mode"):
        config = _perm(name, found.get("ini_" + name, "")) or _perm(name, defaults["cassandra_medusa_config_" + name])
        if config != _perm(name, defaults["cassandra_medusa_config_" + name]):
            out["cassandra_medusa_config_" + name] = config
        key = _perm(name, found.get("key_" + name, ""))
        if "cassandra_medusa_s3_access_key_id" in out and key and key != config:
            out["cassandra_medusa_key_file_" + name] = key

    version, venv, package = found.get("version", ""), found.get("venv", ""), found.get("package", "")
    keep = {}
    where = venv or found.get("bin", "")
    if package:
        notes.insert(0, "Medusa %s installed by the package %s, which the role does not manage (it installs"
                        " with pip): cassandra_medusa_enabled left false, medusa.ini imported"
                     % (version or "?", package))
    elif version:
        out["cassandra_medusa_enabled"] = True
        out["cassandra_medusa_version"] = version
        # not in a virtualenv: new nodes get it the same way, in their Python
        if venv != defaults["cassandra_medusa_venv"]:
            out["cassandra_medusa_venv"] = venv
        # a link elsewhere is kept there; none: the role adds the default one
        link_dir = found.get("link_dir", "")
        if venv and link_dir and link_dir != defaults["cassandra_medusa_link_dir"]:
            out["cassandra_medusa_link_dir"] = link_dir
        notes.insert(0, "Medusa %s in %s, medusa.ini imported (cassandra_medusa_enabled: true)" % (version, where))
        if venv and not link_dir:
            # none on this node: kept so (host_vars), new nodes get the role's
            keep["cassandra_medusa_link_dir"] = ""
            notes.append("Medusa: no %s/medusa link to it: none added on this node, new nodes get one"
                         % defaults["cassandra_medusa_link_dir"])
        if found.get("profile_d") == "yes":
            out["cassandra_medusa_profile_d"] = True
        if found.get("login"):
            notes.append("Medusa: its virtualenv is put in the PATH by a login profile of %s, which the roles leave"
                         " alone; new nodes don't get it (cassandra_medusa_profile_d: true adds"
                         " /etc/profile.d/cassandra-medusa.sh for every login)" % found["login"])
        # its Python (a full path: a name depends on the PATH)
        python = found.get("python", "")
        if not venv and python.startswith("/"):
            out["cassandra_medusa_python"] = python
        if not venv:
            notes.insert(1, "Medusa: %s is not in a virtualenv (Python %s): new nodes get it the same way,"
                            " pip installing into that Python (cassandra_medusa_venv: '')" % (where, python or "?"))
    elif found.get("untrusted"):
        notes.insert(0, "Medusa: %s or its Python is owned by neither root nor cassandra, not run to read"
                        " its version: medusa.ini imported, cassandra_medusa_enabled left false" % where)
    else:
        notes.insert(0, "Medusa: medusa.ini imported, but no Medusa install found (%s):"
                        " cassandra_medusa_enabled left false" % (where or "no medusa binary"))
    if found.get("hint_missing"):
        notes.append("Medusa: no medusa in %s (import_cluster_medusa_path) on this node, looked for it in the usual"
                     " places instead" % found["hint_missing"])
    return {"vars": out, "notes": notes, "keep": keep}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_medusa_ini": cassandra_medusa_ini,
            "cassandra_medusa_ini_changes": cassandra_medusa_ini_changes,
            "cassandra_medusa_import": cassandra_medusa_import,
        }
