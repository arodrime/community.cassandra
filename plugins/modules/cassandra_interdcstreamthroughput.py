#!/usr/bin/python

# 2019 Rhys Campbell <rhys.james.campbell@googlemail.com>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_interdcstreamthroughput
author: Rhys Campbell (@rhysmeister)
short_description: Sets the inter-dc stream throughput.
requirements:
  - nodetool
description:
    - Sets the inter-dc stream throughput.
    - Without C(value), only reads the current inter-dc stream throughput.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  value:
    description:
      - Inter-dc stream throughput to set, in megabits per second (Mb/s), or 0 to disable throttling.
      - When omitted, the module only returns the current value and changes nothing.
    type: int
'''

EXAMPLES = '''
- name: Set inter dc stream throughput to 200
  community.cassandra.cassandra_interdcstreamthroughput:
    value: 200

- name: Read the current inter dc stream throughput
  community.cassandra.cassandra_interdcstreamthroughput:
  register: interdcstreamthroughput
'''

RETURN = '''
cassandra_interdcstreamthroughput:
  description: The return state of the executed command.
  returned: success
  type: str
current:
  description:
    - The inter-dc stream throughput read before any change, in C(unit).
    - 0 means unlimited (no throttling).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: float
  sample: 200.0
unit:
  description: The unit printed by nodetool, null when it prints C(unlimited).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: Mb/s
current_raw:
  description:
    - The line printed by the get command.
    - Since 4.1, nodetool labels it C(Current stream throughput).
  returned: when the get command succeeds and its output is parsed
  version_added: 2.1.0
  type: str
  sample: "Current stream throughput: 200.0 Mb/s"
'''

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import (
    NodeToolGetSetCommand, cassandra_version_at_least, parse_nodetool_get)
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

    set_cmd = "setinterdcstreamthroughput {0}".format(module.params['value'])
    get_cmd = "getinterdcstreamthroughput"

    n = NodeToolGetSetCommand(module, get_cmd, set_cmd)

    # cassandra_version may still be None here if not passed explicitly -
    # NodeToolGetSetCommand.__init__ (above) auto-detects it as a side
    # effect. Mutate n.get_cmd itself, not the get_cmd local variable:
    # n already copied its value by the time we get here, so reassigning
    # the local has no effect on the command that actually gets run.
    if cassandra_version_at_least(module.params['cassandra_version'], "4.1"):
        n.get_cmd += " -d"
    value = module.params['value']

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

    current, unit, line = parse_nodetool_get(out)
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
