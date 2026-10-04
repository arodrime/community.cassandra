#!/usr/bin/python
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_move
author: Alain Rodriguez (@arodrime)
short_description: Moves a single-token node to a new token.
version_added: 2.1.0
requirements:
  - nodetool
description:
    - Runs nodetool move on a node that has one token (num_tokens 1). The node
      streams the ranges it gains, the nodes that lose ranges keep them until a
      nodetool cleanup.
    - Waits until the move is over, which can take hours on a big node (run it with async).
    - A node already at the token is left as it is. A node with several tokens
      (vnodes), or already moving, is refused.
extends_documentation_fragment:
  - community.cassandra.nodetool_module_options
options:
  token:
    description:
      - The new token, an integer in the partitioner's range.
    type: str
    required: true
'''

EXAMPLES = '''
- name: Move the node, following the streams meanwhile
  community.cassandra.cassandra_move:
    token: "-3074457345618258603"
  async: 86400
  poll: 0
  register: move_job
'''

RETURN = '''
token_before:
  description: The node's token before the move.
  returned: success
  type: str
  sample: "-9223372036854775808"
token:
  description: The node's token now (the new token after a move, or under check mode).
  returned: success
  type: str
mode:
  description: The node's mode before the move (nodetool netstats).
  returned: success
  type: str
  sample: NORMAL
stdout:
  description: Output of the nodetool move command.
  returned: when debug is true
  type: str
stderr:
  description: Error output of the nodetool command.
  returned: when debug is true, or on failure
  type: str
'''

import re

from ansible.module_utils.basic import AnsibleModule
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCommandSimple
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_netstats import parse_netstats


def parse_info_tokens(stdout):
    """nodetool info -T: the node's tokens, one 'Token : <t>' line each."""
    return re.findall(r"(?m)^Token\s*:\s*(-?[0-9]+)\s*$", stdout or "")


def main():
    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(token=dict(type='str', required=True, no_log=False))
    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)
    token = module.params['token'].strip()
    if not re.match(r"^-?[0-9]+$", token):
        module.fail_json(msg="token must be one integer, got %s" % token)
    token = str(int(token))
    result = {"changed": False}

    (rc, out, err) = NodeToolCommandSimple(module, "info -T").run_command()
    if rc != 0:
        module.fail_json(msg="info command failed", stderr=err)
    tokens = parse_info_tokens(out)
    if len(tokens) != 1:
        module.fail_json(msg="the node has %d tokens: nodetool move works on a node with one token" % len(tokens))
    result['token_before'] = result['token'] = tokens[0]

    (rc, out, err) = NodeToolCommandSimple(module, "netstats").run_command()
    if rc != 0:
        module.fail_json(msg="netstats command failed", stderr=err, **result)
    result['mode'] = parse_netstats(out)[0]
    if tokens[0] == token:
        module.exit_json(msg="already at token %s" % token, **result)
    if result['mode'] != "NORMAL":
        module.fail_json(msg="the node is %s, not NORMAL: no move now" % result['mode'], **result)

    result['changed'] = True
    result['token'] = token
    if not module.check_mode:
        # "--": a negative token is not an option
        (rc, out, err) = NodeToolCommandSimple(module, "move -- %s" % token).run_command()
        if module.params['debug']:
            result['stdout'] = out
            result['stderr'] = err
        if rc != 0:
            result['token'] = tokens[0]
            module.fail_json(msg="move command failed", rc=rc, **dict(result, stderr=err))
    module.exit_json(msg="moved from %s to %s" % (tokens[0], token), **result)


if __name__ == '__main__':
    main()
