#!/usr/bin/python
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_netstats
author: Alain Rodriguez (@arodrime)
short_description: Returns the mode of the node and whether it is streaming.
version_added: 2.1.0
requirements:
  - nodetool
description:
    - Runs nodetool netstats and returns the operating mode of the node
      (STARTING, JOINING, NORMAL, LEAVING, DECOMMISSIONED, DRAINING, DRAINED...)
      and whether it is streaming data (bootstrap, rebuild, repair...).
    - Never changes anything.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options
'''

EXAMPLES = '''
- name: Wait until the node has joined the ring
  community.cassandra.cassandra_netstats:
  register: netstats
  until: netstats.mode | default('') == 'NORMAL'
  retries: 60
  delay: 10

- name: Fail if the node is streaming
  community.cassandra.cassandra_netstats:
  register: netstats
  failed_when: netstats.streaming
'''

RETURN = '''
mode:
  description:
    - Operating mode of the node, as in the "Mode:" line.
    - Empty string when the output has no "Mode:" line.
  returned: success
  type: str
  sample: NORMAL
streaming:
  description: True if the node has stream sessions in progress.
  returned: success
  type: bool
streams:
  description: The stream sessions part of the output, empty when not streaming.
  returned: success
  type: list
  elements: str
sessions:
  description:
    - The stream sessions, one per session and direction, with their progress.
    - C(files) lists the files nodetool shows for the session (those started), with the
      C(keyspace.table) they belong to when their path shows it.
  returned: success
  type: list
  elements: dict
  sample:
    - operation: Bootstrap
      plan_id: 9a3f2c10-6b1e-11ef-8b1a-3d7c1c0a1b2c
      peer: 10.0.0.1
      direction: receiving
      files_total: 12
      files_done: 3
      bytes_total: 104857600
      bytes_done: 26214400
      files:
        - path: /var/lib/cassandra/data/ks/t-5a1c3b2e8d9f4a6b7c8d9e0f1a2b3c4d/nb-1-big-Data.db
          table: ks.t
          done: 8738133
          total: 8738133
stdout:
  description: Raw output of the nodetool netstats command.
  returned: success
  type: str
stderr:
  description: Error output of the nodetool command.
  returned: when debug is true, or on failure
  type: str
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCommandSimple
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_netstats import parse_netstats as parse_sessions


def parse_netstats(stdout):
    """mode, and the stream session lines between the mode line and the read repair statistics."""
    mode, streams, dummy = parse_sessions(stdout)
    return mode, streams


def main():
    argument_spec = cassandra_common_argument_spec()
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )

    cmd = 'netstats'
    n = NodeToolCommandSimple(module, cmd)

    result = {}

    (rc, out, err) = n.run_command()

    if module.params['debug'] and err:
        result['stderr'] = err

    if rc != 0:
        # stderr tells a stopped node (Connection refused) from others
        module.fail_json(name=cmd, msg="netstats command failed", **dict(result, stderr=err))

    mode, streams, sessions = parse_sessions(out)
    result['stdout'] = out
    result['mode'] = mode
    result['streams'] = streams
    result['streaming'] = len(streams) > 0
    result['sessions'] = sessions

    module.exit_json(**result)


if __name__ == '__main__':
    main()
