#!/usr/bin/python

# 2021 Rhys Campbell <rhyscampbell@bluewin.ch>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_maxhintwindow
author: Rhys Campbell (@rhysmeister)
short_description: Set the specified max hint window in ms.
requirements:
  - nodetool
description:
    - Set the specified max hint window in ms.
    - Without C(value), only reads the current max hint window.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  value:
    description:
      - MS value to set the max hint window to.
      - When omitted, the module only returns the current value and changes nothing.
    type: int
'''

EXAMPLES = '''
- name: Set max hint window with module
  community.cassandra.cassandra_maxhintwindow:
    value: 10800000

- name: Read the current max hint window
  community.cassandra.cassandra_maxhintwindow:
  register: maxhintwindow
'''

RETURN = '''
msg:
  description: A breif description of what happened
  returned: success
  type: str
current:
  description:
    - The max hint window read before any change, in C(unit).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: int
  sample: 10800000
unit:
  description: The unit printed by nodetool.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: ms
current_raw:
  description: The line printed by the get command.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: "Current max hint window: 10800000 ms"
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

    set_cmd = "setmaxhintwindow  -- {0}".format(module.params['value'])
    get_cmd = "getmaxhintwindow"
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
        result['msg'] = "Max Hint Window is {0} ms".format(current)
    elif current == value:

        if rc != 0:
            result['changed'] = False
            module.fail_json(name=get_cmd,
                             msg="{0} command failed".format(get_cmd), **result)
        else:
            result['changed'] = False
            result['msg'] = "Max Hint Window is already {0} ms".format(value)
    else:

        if module.check_mode:
            result['changed'] = True
            result['msg'] = "Max Hint Window updated"
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
                result['msg'] = "Max Hint Window updated"

    module.exit_json(**result)


if __name__ == '__main__':
    main()
