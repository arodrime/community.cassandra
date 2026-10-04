#!/usr/bin/python
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_ring
author: Alain Rodriguez (@arodrime)
short_description: Returns the tokens of the ring, per datacenter.
version_added: 2.1.0
requirements:
  - nodetool
description:
    - Runs nodetool ring and returns, per datacenter, a line per token with
      the node's address, rack, status and state.
    - With one token per node (num_tokens 1), one line per node.
    - Never changes anything.
extends_documentation_fragment:
  - community.cassandra.nodetool_module_options
'''

EXAMPLES = '''
- name: Read the ring
  community.cassandra.cassandra_ring:
  register: ring

- name: Show the tokens of dc1
  ansible.builtin.debug:
    msg: "{{ ring.ring.dc1 | map(attribute='token') | list }}"
'''

RETURN = '''
ring:
  description: Per datacenter, one entry per token, in nodetool's order (by token).
  returned: success
  type: dict
  sample:
    dc1:
      - address: 10.100.100.1
        rack: r1
        status: Up
        state: Normal
        token: "-9223372036854775808"
      - address: 10.100.100.2
        rack: r1
        status: Up
        state: Normal
        token: "-3074457345618258603"
stderr:
  description: Error output of the nodetool command.
  returned: when debug is true, or on failure
  type: str
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCommandSimple
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import parse_ring


def main():
    module = AnsibleModule(
        argument_spec=cassandra_common_argument_spec(),
        supports_check_mode=True,
    )
    n = NodeToolCommandSimple(module, "ring")
    result = {"changed": False}
    (rc, out, err) = n.run_command()
    if module.params['debug'] and err:
        result['stderr'] = err
    if rc != 0:
        module.fail_json(msg="ring command failed", **dict(result, stderr=err))
    result['ring'] = parse_ring(out)
    module.exit_json(**result)


if __name__ == '__main__':
    main()
