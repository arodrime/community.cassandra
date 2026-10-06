# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_nodes
short_description: The hosts of the cluster, without those marked absent
version_added: 2.1.0
description:
  - Returns the hosts of the cluster's group (lookup C(community.cassandra.cassandra_hosts)) whose
    C(cassandra_node_state) is C(present), the default, in the inventory's order.
  - A host marked C(cassandra_node_state=absent) (a host var or a group var) is no longer part of the
    cluster for the playbooks; the playbook C(community.cassandra.topology) removes it from the ring
    if it is still there. Delete its lines from the inventory afterwards, or leave them.
  - Use C(query) (or C(wantlist=true)) for a list.
options:
  state:
    description:
      - C(present) returns the hosts that are part of the cluster, C(absent) the hosts marked absent.
    type: str
    default: present
    choices: [present, absent]
  group:
    description:
      - The group to read instead of the cluster's group (e.g. C(all)).
    type: str
author: Alain Rodriguez (@arodrime)
"""

EXAMPLES = r"""
- name: Run on the nodes of the cluster
  hosts: "{{ query('community.cassandra.cassandra_nodes') }}"
  tasks:
    - name: Show the hosts marked absent
      ansible.builtin.debug:
        msg: "{{ query('community.cassandra.cassandra_nodes', state='absent') }}"
"""

RETURN = r"""
_raw:
  description: The host names.
  type: list
  elements: str
"""

from ansible.errors import AnsibleLookupError
from ansible.plugins.lookup import LookupBase
from ansible.plugins.loader import lookup_loader

STATES = ("present", "absent")


def node_states(hosts, read):
    """hosts: names; read(host) -> its cassandra_node_state or None. Returns
    {host: 'present' or 'absent'}, or raises ValueError naming the hosts with
    another value."""
    states, wrong = {}, []
    for host in hosts:
        value = read(host)
        value = "present" if value is None or str(value).strip() == "" else str(value).strip().lower()
        if value not in STATES:
            wrong.append("%s (%s)" % (host, value))
        states[host] = value
    if wrong:
        raise ValueError("cassandra_node_state must be present or absent: %s" % ", ".join(wrong))
    return states


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        self.set_options(var_options=variables, direct=kwargs)
        state = self.get_option("state")
        group = self.get_option("group")
        if not group:
            hosts_lookup = lookup_loader.get("community.cassandra.cassandra_hosts", loader=self._loader, templar=self._templar)
            group = hosts_lookup.run([], variables)[0]
        hosts = list((variables.get("groups") or {}).get(group) or [])
        hostvars = variables.get("hostvars") or {}

        def read(host):
            value = hostvars[host].get("cassandra_node_state") if host in hostvars else None
            if isinstance(value, str) and "{{" in value:
                value = self._templar.template(value)
            return value

        try:
            states = node_states(hosts, read)
        except ValueError as exc:
            raise AnsibleLookupError(str(exc))
        return [h for h in hosts if states[h] == state]
