#!/usr/bin/python

# 2019 Rhys Campbell <rhys.james.campbell@googlemail.com>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_traceprobability
author: Rhys Campbell (@rhysmeister)
short_description: Sets the trace probability.
requirements: [ nodetool ]
description:
    - Sets the trace probability.
    - Without C(value), only reads the current trace probability.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  value:
    description:
      - Trace probability between 0.0 and 1.0
      - When omitted, the module only returns the current value and changes nothing.
    type: float
'''

EXAMPLES = '''
- name: Set traceprobability to 0.9
  community.cassandra.cassandra_traceprobability:
    value: 0.9

- name: Read the current trace probability
  community.cassandra.cassandra_traceprobability:
  register: traceprobability
'''

RETURN = '''
cassandra_traceprobability:
  description: The return state of the executed command.
  returned: success
  type: str
current:
  description:
    - The trace probability read before any change.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: float
  sample: 0.0
current_raw:
  description: The line printed by the get command.
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: "Current trace probability: 0.0"
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolGetSetCommand, parse_nodetool_get
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


def main():
    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(
        value=dict(type='float')
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )

    set_cmd = "settraceprobability {0}".format(module.params['value'])
    get_cmd = "gettraceprobability"
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

    current, dummy, line = parse_nodetool_get(out)
    if rc == 0 and current is not None:
        result['current'] = current
        result['current_raw'] = line

    if value is None:
        if rc != 0:
            module.fail_json(name=n.get_cmd,
                             msg="get command failed", **result)
        if current is None:
            module.fail_json(name=n.get_cmd,
                             msg="unable to parse the get command output: {0}".format(out), **result)
        result['changed'] = False
    elif current == value:

        if rc != 0:
            result['changed'] = False
            module.fail_json(name=get_cmd,
                             msg="get command failed", **result)
    else:

        if module.check_mode:
            result['changed'] = True
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
                                 msg="set command failed", **result)
            else:
                result['changed'] = True

    module.exit_json(**result)


if __name__ == '__main__':
    main()
