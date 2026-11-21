# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): what every import module shares.

The constants: ROLE (cassandra_config's dir), SERIES, ADDRESSES and the facts they are written as (IPV4, HOSTNAME),
the KEEP switches and the variables written with them (IMPORTED, IMPORTED_FOR, SET_UP_BY_THE_ROLES), MISSING (a
value not there) and MISSING_NAME (an undefined error that names a variable).

The import's error rules: an unexpected error never shows its message, which may quote a value read from a node
(values_hidden, error_without_message: its type and where it happened only, in a cassandra_import*.py module);
undefined_name lets Ansible's own undefined-variable errors through, they name a variable and show no value;
value_shown masks a secret's value.

Filter (plugins/filter/cassandra_import.py names the filters):
cassandra_import_error: a failed task's result -> why it failed, without the
    values (the import's no_log tasks hold passwords).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re
import sys
import traceback

import jinja2

from ansible.errors import AnsibleFilterError, AnsibleUndefinedVariable
from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets


ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# The node's own address or name: written as the fact that gives it
ADDRESSES = ["cassandra_listen_address", "cassandra_rpc_address",
             "cassandra_broadcast_address", "cassandra_broadcast_rpc_address", "cassandra_jmx_rmi_hostname"]
# What the roles leave as it is on a node set up another way (host_vars only:
# a node added later gets it from the roles): {switch: what it keeps}
KEEP = {
    "cassandra_repository_manage": "the package repositories",
    "cassandra_cqlsh_python_manage": "cqlsh's Python (python3.11, cqlshlib link, wrapper)",
    "cassandra_linux_manage": "the OS settings (kernel, limits, THP, swap, time sync, disks)",
    "cassandra_service_unit_manage": "the systemd unit (or init script) Cassandra is started by",
    "cassandra_java_set_default": "the system java (/usr/bin/java), which is not the running one",
    "cassandra_firewall_manage": "the firewall (its package, service and ports), or none",
    "cassandra_data_disk_tuning": ("the tuning of its data devices (read-ahead, scheduler, udev rule, tuned profiles),"
                                   " which cassandra_linux would change"),
}
# Written next to the KEEP switches when one of IMPORTED_FOR (what a blank host would miss) is set, or parts kept by
# hand (cassandra_import_layout): add_node refuses a blank host rebuilt under that node's name
IMPORTED = "cassandra_imported_host"
IMPORTED_FOR = ("cassandra_cqlsh_python_manage", "cassandra_linux_manage", "cassandra_service_unit_manage")
# KEEP switches of nodes the roles set up (not another way)
SET_UP_BY_THE_ROLES = ("cassandra_data_disk_tuning",)
IPV4 = "{{ ansible_facts['default_ipv4']['address'] }}"
HOSTNAME = "{{ ansible_facts['hostname'] }}"

# a value not there (not None, which a node may have): the layout's nodes without a key, cassandra_drift
MISSING = object()

# The undefined errors naming a field or a variable, nothing else of the data.
# Jinja words a missing dict key (d[key]) the same way: nothing here or in the
# playbook may index a dict with a key read from the nodes.
MISSING_NAME = re.compile(r"(?:object'?|object of type '\w+') has no attribute '(\w+)'\s*$|^'(\w+)' is undefined\s*$")


def value_shown(key, value, show=None):
    """A value as the import shows it (same masking as cassandra_config's diff preview, plugin_utils
    cassandra_secrets): **** for a secret's (cassandra_secrets.hidden), else show(value) (None: the value)."""
    if cassandra_secrets.hidden(key, value):
        return cassandra_secrets.MASK
    return show(value) if show else value


def undefined_name(exc):
    """A missing variable or field of the nodes' data (Ansible's lazy
    templating), whose message names it and shows no value."""
    source = getattr(exc, "source", None)  # 2.19+: the marker behind the error
    return (isinstance(exc, (AnsibleUndefinedVariable, jinja2.exceptions.UndefinedError))
            and (source is None or type(source).__name__ == "UndefinedMarker")
            and bool(MISSING_NAME.search(str(exc))))


def error_without_message(name, where):
    """The error being handled, without its message (it may quote a value). Call it in an except block: it reads
    that error (sys.exc_info); raise what it returns out of the block (no chained message either)."""
    frames = [f for f in traceback.extract_tb(sys.exc_info()[2])
              if re.match(r"cassandra_import\w*[.]py$", os.path.basename(f[0]))]  # (every import module, this one too)
    at = " in %s(), line %d" % (frames[-1][2], frames[-1][1]) if frames else ""
    return AnsibleFilterError("%s: %s%s%s (message hidden: it may show a value read from the nodes)"
                              % (name, (where + ", ") if where else "", sys.exc_info()[0].__name__, at))


def values_hidden(func):
    """func, raising its unexpected errors without their message."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AnsibleFilterError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if undefined_name(exc):
                raise
            error = error_without_message(func.__name__, "")
        raise error
    return wrapper


# Parts of an error message that show no value: this filter's own messages,
# and the name of a missing field or variable
SAFE_REASONS = [
    (re.compile(r"(cassandra_(?:config_import|inventory_layout|inventory_files|import_nodes|import_self_check(?:_nodes)?): [^\n]*?"
                r"\(message hidden: "
                r"it may show a value read from the nodes\))"), r"\1"),
    (re.compile(r"(cassandra_import_nodes: [0-9A-Fa-f.:]+: \w+)"), r"\1"),
    (re.compile(r"(cassandra_config_import: unsupported series \w+)"), r"\1"),
    # the user's own -e value
    (re.compile(r"(import_keep_hand_edits: [^\n]*?: neither a config file of cassandra_config \([^)]*\) nor a part"
                r" \([^)]*\); or true \(every hand-edited file\), false)"), r"\1"),
    (re.compile(r"(cassandra_config_import: cannot read [\w, ]+ from cassandra\.yaml \([^)]*\))"), r"\1"),
    (re.compile(r"(cassandra_config_import: cassandra\.yaml is not valid YAML(?: \(line \d+\))?)"), r"\1"),
    (re.compile(r"(?:object'?|object of type '\w+') has no attribute '(\w+)'(?:\.|$)"), r"missing field '\1'"),
    (re.compile(r"(?:^|: )'(\w+)' is undefined(?:\.|$)"), r"undefined variable '\1'"),
]


def _safe_reason(msg):
    for regex, template in SAFE_REASONS:
        match = regex.search(msg)
        if match:
            return match.expand(template)
    return ""


def cassandra_import_error(result, label="address"):
    """A failed task's result (ansible_failed_result) -> why it failed, for the
    import's no_log tasks: the failed loop items (their `label` key) and what
    of the error shows no value, else ''.
    e.g. {"failed": True, "results": [{"failed": True, "item": {"address": "10.0.0.1"}, "msg": "..."}]}
    -> "10.0.0.1: failed" (its message shows a value: left out)."""
    out = []
    for res in (result or {}).get("results") or [result or {}]:
        if not isinstance(res, dict) or not boolean(res.get("failed", False), strict=False):
            continue
        item = res.get("item")
        where = item.get(label) if isinstance(item, dict) and label else None
        why = _safe_reason(str(res.get("msg", "")))
        text = ("%s: %s" % (where, why or "failed") if where else why)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)
