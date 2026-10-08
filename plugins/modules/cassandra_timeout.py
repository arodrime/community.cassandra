#!/usr/bin/python

# 2021 Rhys Campbell <rhyscampbell@bluewin.ch>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_timeout
author: Rhys Campbell (@rhysmeister)
short_description: Manages the timeout on the Cassandra node.
requirements:
  - nodetool
description:
    - Manages the timeout.
    - Set the specified timeout in ms, or 0 to disable timeout.
    - Without C(timeout), only reads the current timeout of C(timeout_type).

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  timeout:
    description:
      - Timeout in milliseconds.
      - When omitted, the module only returns the current value and changes nothing.
    type: int
  timeout_type:
    description:
      - Type of timeout.
    type: str
    choices:
      - read
      - range
      - write
      - counterwrite
      - cascontention
      - truncate
      - internodeconnect
      - internodeuser
      - internodestreaminguser
      - misc
    default: read
'''

EXAMPLES = '''
- name: Set read timeout to 1000 ms
  community.cassandra.cassandra_timeout:
    timeout: 1000
    timeout_type: read

- name: Disable write timeout
  community.cassandra.cassandra_timeout:
    timeout: 0
    timeout_type: write

- name: Read the current read timeout
  community.cassandra.cassandra_timeout:
    timeout_type: read
  register: read_timeout
'''

RETURN = '''
cassandra_timeout:
  description: The return state of the executed command.
  returned: success
  type: str
current:
  description:
    - The timeout read before any change, in C(unit).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: int
  sample: 5000
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
  sample: "Current timeout for type read: 5000 ms"
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolGetSetCommand, parse_nodetool_get
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


def main():

    timeout_type_choices = ['read', 'range', 'write', 'counterwrite', 'cascontention', 'truncate',
                            'internodeconnect', 'internodeuser', 'internodestreaminguser', 'misc']

    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(
        timeout=dict(type='int'),
        timeout_type=dict(type='str', choices=timeout_type_choices, default='read')
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )

    timeout = module.params['timeout']
    timeout_type = module.params['timeout_type']
    set_cmd = "settimeout {0} {1}".format(timeout_type, timeout)
    get_cmd = "gettimeout {0}".format(timeout_type)

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

    if timeout is None:
        if rc != 0:
            module.fail_json(name=n.get_cmd,
                             msg="get command failed", **result)
        if current is None:
            module.fail_json(name=n.get_cmd,
                             msg="unable to parse the get command output: {0}".format(out), **result)
        result['changed'] = False
        result['msg'] = "{0} timeout is {1} ms".format(timeout_type, current)
    elif current == timeout:

        if rc != 0:
            module.fail_json(name=get_cmd,
                             msg="get command failed", **result)
        result['changed'] = False
        result['msg'] = "{0} timeout unchanged".format(timeout_type)
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
        result['msg'] = "{0} timeout changed".format(timeout_type)

    module.exit_json(**result)


if __name__ == '__main__':
    main()
