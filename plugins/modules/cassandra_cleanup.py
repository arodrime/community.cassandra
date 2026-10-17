#!/usr/bin/python

# 2019 Rhys Campbell <rhys.james.campbell@googlemail.com>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_cleanup
author: Rhys Campbell (@rhysmeister)
short_description: Runs cleanup on a Cassandra node.
requirements:
  - nodetool
description:
    - Runs cleanup on a Cassandra node.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  keyspace:
    description:
      - Optional keyspace.
    type: str
  table:
    description:
      - Optional table name or list of table names.
    type: raw
  num_jobs:
    description:
      - Number of job threads.
    type: int
    default: 2
    aliases:
      - j
'''

EXAMPLES = '''
- name: Run cleanup on the Cassandra node
  community.cassandra.cassandra_cleanup:
'''

RETURN = '''
cassandra_cleanup:
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


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCommandKeyspaceTableNumJobs, fail_nodetool
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


def main():
    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(
        keyspace=dict(type='str', default=None, required=False, no_log=False),
        table=dict(type='raw', default=None, required=False),
        num_jobs=dict(type='int', default=2, aliases=['j'], required=False),
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=False,
    )

    cmd = 'cleanup'

    n = NodeToolCommandKeyspaceTableNumJobs(module, cmd)

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
        result['msg'] = "nodetool cleanup executed successfully"
        module.exit_json(**result)
    else:
        fail_nodetool(module, result, "cleanup", rc, out, err,
                      ("Check the node's system.log (disk space, nodetool compactionstats; no cleanup while a node "
                       "joins or leaves), then run it again"))


if __name__ == '__main__':
    main()
