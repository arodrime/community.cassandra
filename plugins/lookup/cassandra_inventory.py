# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_inventory
short_description: The clusters of an inventory, read without the vault password and without the nodes
version_added: 2.1.0
description:
  - Reads an inventory as Ansible does (its groups, C(group_vars), C(host_vars) and C(-e) variables) and returns,
    per cluster group, its hosts with the variables the C(help) playbook shows (the cluster's name, versions,
    install method, Java, datacenter, rack, seeds, addresses, C(cassandra_node_state)) and the names of the
    C(cassandra_*) variables set.
  - Connects to no host. A vault-encrypted vars file that can't be decrypted (no vault password given) is
    skipped and named in C(vault_skipped); the other files are read. Secrets are never returned,
    only the variables listed above are templated.
  - The cluster groups are C(cassandra_hosts) when it is set, else the group found by
    the lookup M(community.cassandra.cassandra_hosts#lookup), else every top group of the inventory
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
    - "A dict: C(sources), C(vault_skipped), C(auto), C(options), C(clusters) (a list of C(name), C(hosts): C(name), C(vars), C(names))."
    - C(auto) is the group the operation playbooks take without C(-e cassandra_hosts) (empty when none);
      C(options) is the list of the command line options the printed commands need (C(-b), the vault and user options of this run).
  type: list
  elements: dict
"""

import os

from ansible import constants as C
from ansible import context
from ansible.errors import AnsibleError, AnsibleLookupError
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.vault import is_encrypted_file
from ansible.plugins.lookup import LookupBase
from ansible.template import Templar
from ansible.vars.manager import VariableManager

from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import cluster_group, top_groups

# the only variables templated and returned: none of them holds a secret
SHOWN = ("ansible_host", "ansible_become", "ansible_user", "ansible_ssh_private_key_file", "cassandra_jmx_username",
         "cassandra_jmx_password_file", "cassandra_cluster_name", "cassandra_version", "cassandra_package_version",
         "cassandra_install_method", "cassandra_java_version", "cassandra_java_home", "cassandra_dc", "cassandra_rack",
         "cassandra_seeds", "cassandra_listen_address", "cassandra_node_state", "cassandra_authenticator",
         "cassandra_num_tokens", "cassandra_allocate_tokens_for_local_replication_factor",
         "cassandra_endpoint_snitch")
# only whether they are set
SET = ("cassandra_java_tarball", "cassandra_cql_username", "cassandra_jmx_password")


class _Loader(DataLoader):
    """A loader that skips the vault-encrypted files it can't decrypt."""

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


def _value(templar, value):
    """The value templated; the raw text when it can't be (a fact of the node, a vaulted variable)."""
    if type(value).__name__ in ("EncryptedString", "AnsibleVaultEncryptedUnicode"):
        return "(vaulted)"
    try:
        value = templar.template(value)
    except Exception:  # pylint: disable=broad-except
        pass
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


def read(sources, given=None, loader_secrets=None):
    loader = _Loader()
    if loader_secrets:
        loader.set_vault_secrets(loader_secrets)
    inventory = InventoryManager(loader=loader, sources=sources)
    manager = VariableManager(loader=loader, inventory=inventory)
    groups = dict((name, [h.name for h in group.get_hosts()]) for name, group in inventory.groups.items())
    try:
        auto = cluster_group(None, groups)
    except ValueError:
        auto = ""
    names = [given] if given else ([auto] if auto else top_groups(groups))
    clusters = []
    for name in names:
        if name not in inventory.groups:
            raise AnsibleLookupError("cassandra_hosts: group '%s' not found in the inventory" % name)
        hosts = []
        for host in inventory.groups[name].get_hosts():
            variables = manager.get_vars(host=host, include_hostvars=False)
            templar = Templar(loader=loader, variables=variables)
            shown = dict((k, _value(templar, variables[k])) for k in SHOWN if k in variables)
            shown.update((k, bool(variables.get(k))) for k in SET if k in variables)
            hosts.append({"name": host.name, "vars": shown,
                          "names": sorted(k for k in variables if k.startswith("cassandra_"))})
        clusters.append({"name": name, "hosts": hosts})
    options = _options()
    # -b of this run is not enough: the commands are run on their own
    if not C.DEFAULT_BECOME and not all(h["vars"].get("ansible_become") in (True, "true", "True", "yes")
                              for c in clusters for h in c["hosts"]):
        options.insert(0, "-b")
    if loader.skipped and not C.DEFAULT_VAULT_PASSWORD_FILE and not any(o.startswith("--") and "vault" in o
                                                                       for o in options):
        options.append("--ask-vault-pass")  # the operations read the vaulted files help skipped
    return {"sources": list(sources), "vault_skipped": sorted(loader.skipped), "auto": auto, "clusters": clusters,
            "options": options}


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
        vault = getattr(self._loader, "_vault", None)
        return [read(sources, given or None, getattr(vault, "secrets", None))]
