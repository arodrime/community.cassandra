#!/usr/bin/python

# 2019 Rhys Campbell <rhys.james.campbell@googlemail.com>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_drain
author: Rhys Campbell (@rhysmeister)
short_description: Drains a Cassandra node.
requirements:
  - nodetool
description:
    - Flushes all memtables from the node to SSTables on disk.
    - Cassandra stops listening for connections from the client and other nodes.
    - Restart Cassandra after running nodetool drain.
    - Use this command before upgrading a node to a new version of Cassandra.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options
'''

EXAMPLES = '''
- name: Drain Cassandra Node
  cassandra_drain:
'''

RETURN = '''
cassandra_drain:
  description: The return state of the executed command.
  returned: success
  type: str
error:
  description: What nodetool said went wrong (its error line, else its first line of output).
  returned: failure
  type: str
  version_added: 3.0.0
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCommandSimple, fail_nodetool
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


def main():
    argument_spec = cassandra_common_argument_spec()
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=False,
    )

    cmd = 'drain'

    n = NodeToolCommandSimple(module, cmd)

    rc = None
    out = ''
    err = ''
    result = {}

    (rc, out, err) = n.run_command()
    out = out.strip()
    err = err.strip()
    if module.params['debug']:
        if out:
            result['stdout'] = out
        if err:
            result['stderr'] = err

    if rc == 0:
        result['changed'] = True
        result['msg'] = "nodetool drain executed successfully"
        module.exit_json(**result)
    else:
        fail_nodetool(module, result, "drain", rc, out, err,
                      ("If its Mode (nodetool netstats) is DRAINING or DRAINED, the node is out of the cluster: "
                       "restart Cassandra on it. Else check its system.log, then drain it again"))


if __name__ == '__main__':
    main()
