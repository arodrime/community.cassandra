# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters for Cassandra Medusa's medusa.ini (cassandra_medusa role).

cassandra_medusa_ini: INI text -> {section: {key: value}}, keys lower case
    (Medusa reads them with configparser, case-insensitive), {} when unreadable.
cassandra_medusa_ini_changes: old text, new text, name -> the settings that
    change, as [{item, before, after}], secrets masked. Comments, order and
    spacing don't count, nor the case of true/false, except for the keys
    Medusa compares as written (resolve_ip_addresses, nodetool_ssl).

Their errors do not quote the files, which hold passwords.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import configparser
import re

from ansible.errors import AnsibleFilterError

SECRET_KEY = re.compile(r"password|passwd|secret|_kspw$|_tspw$|sse_c_key|access_key", re.I)
BOOLEANS = ("true", "false")
# Medusa compares these with "False" and "true" exactly
CASE_SENSITIVE = ("resolve_ip_addresses", "nodetool_ssl")


def _parse(text):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text or "")
    return {s: dict(parser.items(s, raw=True)) for s in parser.sections()}


def cassandra_medusa_ini(text):
    try:
        return _parse(text)
    except configparser.Error:
        return {}


def _norm(key, value):
    value = str(value).strip()
    return value.lower() if value.lower() in BOOLEANS and key not in CASE_SENSITIVE else value


def _flat(sections):
    return {(s, k): _norm(k, v) for s, keys in sections.items() for k, v in keys.items()}


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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_medusa_ini": cassandra_medusa_ini,
            "cassandra_medusa_ini_changes": cassandra_medusa_ini_changes,
        }
