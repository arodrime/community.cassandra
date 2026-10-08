# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_inventory
short_description: The clusters of an inventory, read without decrypting anything and without the nodes
version_added: 2.1.0
description:
  - Reads an inventory as Ansible does (its groups, C(group_vars) and C(host_vars)) and returns,
    per cluster group, its hosts with the variables the C(help) playbook shows (the cluster's name, versions,
    install method, Java, datacenter, rack, seeds, addresses, C(cassandra_node_state)) and the names of the
    C(cassandra_*) variables set.
  - Connects to no host and decrypts nothing, even when the run has the vault password, so that no secret
    can end up in the output (or in a RUNBOOK.md written from it). A vault-encrypted vars file is skipped and
    named in C(vault_skipped); an inline vaulted value, or a value templated from one, is returned as C((vaulted)).
    Only the variables listed above are templated; a value whose template can't be resolved (a fact of the node, a
    variable of a skipped file) or would run a lookup is returned as its raw text. The C(-e) variables are not read.
  - Like the operation playbooks, it reads the C(group_vars) and C(host_vars) next to the inventory and next to
    the playbooks (C(playbook_dir)), not the ones of the current directory.
  - The cluster groups are C(cassandra_hosts) when it is set, else the one C(CASSANDRA_CLUSTER) names, else the group found by
    the lookup P(community.cassandra.cassandra_hosts#lookup), else every top group of the inventory
    (one per cluster when the inventory holds several, as C(all) > C(<cluster>) > C(<cluster>_<dc>)).
options:
  sources:
    description: The inventory sources to read. Default, the ones of the run (C(ansible_inventory_sources)).
    type: list
    elements: str
author: Alain Rodriguez (@arodrime)
"""

EXAMPLES = r"""
- name: The clusters of the inventory
  ansible.builtin.debug:
    msg: "{{ lookup('community.cassandra.cassandra_inventory') }}"
"""

RETURN = r"""
_raw:
  description:
    - "A dict: C(sources), C(vault_skipped), C(auto), C(options), C(imported), C(clusters)
      (a list of C(name), C(hosts): C(name), C(vars), C(names))."
    - C(auto) is the group the operation playbooks take without C(-e cassandra_hosts) (empty when none);
      C(imported) the clusters import_cluster wrote there (their C(<cluster>.yml));
      C(options) is the list of the command line options the
      printed commands need (the vault and user options of this run; no C(-b), the playbooks become root
      on the nodes themselves); C(vault_prompt_added) whether
      C(--ask-vault-pass) is there because help found vaulted values.
  type: list
  elements: dict
"""

import os
import re

from collections.abc import Mapping

from ansible import constants as C
from ansible import context
from ansible.errors import AnsibleError, AnsibleLookupError
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.vault import is_encrypted_file
from ansible.plugins.lookup import LookupBase
from ansible.template import Templar
from ansible.utils.unsafe_proxy import wrap_var
from ansible.vars.manager import VariableManager

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import written_for
from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import ENV, UNKNOWN, cluster_group, top_groups

# the only variables templated and returned: none of them holds a secret
SHOWN = ("ansible_host", "ansible_port", "ansible_user", "ansible_ssh_private_key_file", "cassandra_jmx_username",
         "cassandra_jmx_password_file", "cassandra_cluster_name", "cassandra_version", "cassandra_package_version",
         "cassandra_install_method", "cassandra_java_version", "cassandra_java_home", "cassandra_dc", "cassandra_rack",
         "cassandra_seeds", "cassandra_listen_address", "cassandra_node_state", "cassandra_authenticator",
         "cassandra_num_tokens", "cassandra_allocate_tokens_for_local_replication_factor",
         "cassandra_endpoint_snitch")
# only whether they are set
SET = ("cassandra_java_tarball", "cassandra_cql_username", "cassandra_jmx_password")


class _Loader(DataLoader):
    """A loader that skips the vault-encrypted files (it has no vault secret)."""

    def __init__(self, *args, **kwargs):
        super(_Loader, self).__init__(*args, **kwargs)
        self.skipped = set()

    def load_from_file(self, file_name, *args, **kwargs):
        try:
            return super(_Loader, self).load_from_file(file_name, *args, **kwargs)
        except AnsibleError:
            path = self.path_dwim(file_name)
            try:
                with open(path, "rb") as f:
                    vaulted = is_encrypted_file(f)
            except (IOError, OSError):
                vaulted = False
            if not vaulted:
                raise
            self.skipped.add(path)
            return {}


VAULTED = "(vaulted)"
_OTHER_MASK = "@@@@@@@@@@@@"  # templated with each mask (nothing in common): a result that differs comes from a vault
# a template that would run a lookup on the controller (a file, a command, a secret store): never templated,
# directly or through another variable
_LOOKUP = re.compile(r"\b(lookup|query|q)\b")


def _vaulted(value):
    """An inline !vault value: read, it would be decrypted (with the run's vault password, if any)."""
    return type(value).__name__ in ("EncryptedString", "AnsibleVaultEncryptedUnicode")


def _masked(value, mask=VAULTED):
    """value with every inline vaulted value replaced by mask, every template that runs a lookup made
    unsafe (kept as text), and whether there was a vaulted value."""
    if _vaulted(value):
        return mask, True
    if isinstance(value, Mapping):
        out, found = {}, False
        for key, item in value.items():
            out[key], one = _masked(item, mask)
            found = found or one
        return out, found
    if isinstance(value, (list, tuple)):
        pairs = [_masked(item, mask) for item in value]
        return [p[0] for p in pairs], any(p[1] for p in pairs)
    if isinstance(value, str) and ("{{" in value or "{%" in value) and _LOOKUP.search(value):
        return wrap_var(str(value)), False
    return value, False


def _template(templar, value):
    try:
        return templar.template(value)
    except Exception:  # pylint: disable=broad-except
        return value


def _value(templars, value, other, found=True):
    """The value templated (with each mask); the raw text when it can't be (a fact of the node) or would run
    a lookup; VAULTED when it comes from a vaulted value. other: the same value, masked with _OTHER_MASK."""
    if _LOOKUP.search(str(value)):
        return str(value)
    first, second = _template(templars[0], value), _template(templars[1], other)
    if found and (first != second or VAULTED in str(first)):  # found: the host has a vaulted value
        return VAULTED
    value = first
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    return str(value)


def _options():
    """The options of this run the printed commands need too."""
    args = context.CLIARGS or {}
    out = []
    for path in args.get("vault_password_files") or []:
        out += ["--vault-password-file", path]
    for vault_id in args.get("vault_ids") or []:
        out += ["--vault-id", vault_id]
    if args.get("ask_vault_pass"):
        out.append("--ask-vault-pass")
    if args.get("remote_user"):
        out += ["-u", args["remote_user"]]
    if args.get("private_key_file"):
        out += ["--private-key", args["private_key_file"]]
    if args.get("become_ask_pass"):
        out.append("-K")
    return out


def read(sources, given=None, basedir=None, env=None):
    """basedir: the playbooks' dir (its group_vars/host_vars apply, as for the operations); env: the value of
    CASSANDRA_CLUSTER, when given is not set."""
    loader = _Loader()  # no vault secret: nothing is decrypted
    if basedir:
        loader.set_basedir(basedir)
    inventory = InventoryManager(loader=loader, sources=sources)
    manager = VariableManager(loader=loader, inventory=inventory)
    # the -e variables are left out: the run's loader read (and decrypted) them already
    manager._extra_vars = {}  # pylint: disable=protected-access
    groups = dict((name, [h.name for h in group.get_hosts()]) for name, group in inventory.groups.items())
    try:
        auto = cluster_group(None, groups)
    except ValueError:
        auto = ""
    if not given and env:

        def cluster_name(name):
            raw = manager.get_vars(host=inventory.get_host(name), include_hostvars=False)
            try:
                return Templar(loader=loader, variables=_masked(raw)[0]).template(raw.get("cassandra_cluster_name"))
            except Exception:  # pylint: disable=broad-except
                return UNKNOWN  # as the playbooks' lookup: not known from the inventory alone

        try:
            given = cluster_group(None, groups, env, cluster_name)
        except ValueError as exc:
            raise AnsibleLookupError(str(exc))
    names = [given] if given else ([auto] if auto else top_groups(groups))
    clusters, vaulted = [], False
    for name in names:
        if name not in inventory.groups:
            raise AnsibleLookupError("cassandra_hosts: group '%s' not found in the inventory" % name)
        if not inventory.groups[name].get_hosts():
            raise AnsibleLookupError("cassandra_hosts: group '%s' has no host" % name)
        hosts = []
        for host in inventory.groups[name].get_hosts():
            raw = manager.get_vars(host=host, include_hostvars=False)
            variables, found = _masked(raw)
            other = _masked(raw, _OTHER_MASK)[0]
            vaulted = vaulted or found
            templars = (Templar(loader=loader, variables=variables), Templar(loader=loader, variables=other))
            shown = dict((k, _value(templars, variables[k], other[k], found)) for k in SHOWN if k in variables)
            shown.update((k, bool(variables[k])) for k in SET if k in variables)
            hosts.append({"name": host.name, "vars": shown,
                          "names": sorted(k for k in variables if k.startswith("cassandra_"))})
        clusters.append({"name": name, "hosts": hosts})
    options = _options()  # no -b: the playbooks ask for root on the nodes themselves
    prompt_added = False
    vault_given = C.DEFAULT_VAULT_PASSWORD_FILE or C.DEFAULT_VAULT_IDENTITY_LIST or any(
        o.startswith("--") and "vault" in o for o in options)
    if (loader.skipped or vaulted) and not vault_given:
        options.append("--ask-vault-pass")  # the operations read the vaulted values help skipped
        prompt_added = True
    return {"sources": list(sources), "vault_skipped": sorted(loader.skipped), "auto": auto, "clusters": clusters,
            "options": options, "imported": _imported(sources, [c["name"] for c in clusters]),
            "vault_prompt_added": prompt_added}


def _written_by_import(path):
    try:
        with open(path, encoding="utf-8") as f:
            return written_for(f.readline()) is not None
    except (IOError, OSError, UnicodeDecodeError):
        return False


def _imported(sources, names):
    """The clusters of names import_cluster wrote there (their <cluster>.yml starts with the import's line)."""
    first = sources[0] if sources else ""
    whole = os.path.isdir(first)
    where = first if whole else os.path.dirname(first)
    return [n for n in names if (whole or os.path.basename(first) == n + ".yml")
            and _written_by_import(os.path.join(where, n + ".yml"))]


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        self.set_options(var_options=variables, direct=kwargs)
        sources = self.get_option("sources") or variables.get("ansible_inventory_sources") or []
        sources = [os.path.abspath(os.path.expanduser(str(s))) if os.path.exists(os.path.expanduser(str(s))) else str(s)
                   for s in sources]
        given = variables.get("cassandra_hosts")
        if isinstance(given, str):
            given = self._templar.template(given)
        return [read(sources, given or None, variables.get("playbook_dir"), os.environ.get(ENV))]
