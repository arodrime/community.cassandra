#!/usr/bin/python

# 2021 Rhys Campbell <rhyscampbell@bluewin.ch>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_batchlogreplaythrottle
author: Rhys Campbell (@rhysmeister)
short_description: Sets the batch log replay throttle.
requirements:
  - nodetool
description:
    - Sets the batch log replay throttle.
    - Without C(value), only reads the current batch log replay throttle.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  value:
    description:
      - KB value to set batch log replay throttle to.
      - When omitted, the module only returns the current value and changes nothing.
    type: int
'''

EXAMPLES = '''
- name: Set batchlogreplaythrottle with module
  community.cassandra.cassandra_batchlogreplaythrottle:
    value: 1024

- name: Read the current batchlog replay throttle
  community.cassandra.cassandra_batchlogreplaythrottle:
  register: batchlogreplaythrottle
'''

RETURN = '''
msg:
  description: A breif description of what happened
  returned: success
  type: str
current:
  description:
    - The batch log replay throttle read before any change, in C(unit).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: int
  sample: 1024
unit:
  description: The unit printed by nodetool.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: KB/s
current_raw:
  description: The line printed by the get command.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: "Batchlog replay throttle: 1024 KB/s"
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolGetSetCommand, parse_nodetool_get
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


def main():
    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(
        value=dict(type='int')
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )

    set_cmd = "setbatchlogreplaythrottle  {0}".format(module.params['value'])
    get_cmd = "getbatchlogreplaythrottle"
    value = module.params['value']

    n = NodeToolGetSetCommand(module, get_cmd, set_cmd)

    rc = None
    out = ''
    err = ''
    result = {}

    (rc, out, err) = n.get_command()
    out = out.strip()

    if module.params['debug']:
        if out:
            result['stdout'] = out
        if err:
            result['stderr'] = err

    current, unit, line = parse_nodetool_get(out, int)
    if rc == 0 and current is not None:
        result['current'] = current
        result['unit'] = unit
        result['current_raw'] = line

    if value is None:
        if rc != 0:
            module.fail_json(name=n.get_cmd,
                             msg="get command failed", **result)
        if current is None:
            module.fail_json(name=n.get_cmd,
                             msg="unable to parse the get command output: {0}".format(out), **result)
        result['changed'] = False
        result['msg'] = "Batch log replay throttle is {0} KB/s".format(current)
    elif current == value:

        if rc != 0:
            result['changed'] = False
            module.fail_json(name=get_cmd,
                             msg="{0} command failed".format(get_cmd), **result)
        else:
            result['changed'] = False
            result['msg'] = "Batch log replay throttle is already {0} KB/s".format(value)
    else:

        if module.check_mode:
            result['changed'] = True
            result['msg'] = "Batch log replay throttle updated"
        else:
            (rc, out, err) = n.set_command()
            out = out.strip()
            if module.params['debug']:
                if out:
                    result['stdout'] = out
                if err:
                    result['stderr'] = err
            if rc != 0:
                result['changed'] = False
                module.fail_json(name=set_cmd,
                                 msg="{0} command failed".format(set_cmd), **result)
            else:
                result['changed'] = True
                result['msg'] = "Batch log replay throttle updated"

    module.exit_json(**result)


if __name__ == '__main__':
    main()
