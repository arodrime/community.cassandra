#!/usr/bin/python

# 2020 Rhys Campbell <rhys.james.campbell@googlemail.com>
# https://github.com/rhysmeister
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function


DOCUMENTATION = '''
---
module: cassandra_schema
author: Rhys Campbell (@rhysmeister)
short_description: Validates the schema version as seen from the node.
requirements: [ nodetool ]
description:
    - Validates the schema version as seen from the node.
    - Ensure that all nodes are have the same schema version.
    - Can poll multiple times to wait for the schema version to converge.
    - Can also specify a schema version if required.
    - Schema version is obtained through the usage of the nodetool describecluster command.
    - The nodes agree when describecluster lists one schema version. The nodes it lists as C(UNREACHABLE)
      are not a version and are not counted (their schema is not known); they are returned in C(unreachable_nodes).
    - It polls until the nodes agree (on I(uuid) when given) or I(poll) runs out, then fails with each
      version and its nodes.

extends_documentation_fragment:
  - community.cassandra.nodetool_module_options

options:
  uuid:
    description:
      - The expected schema version.
    type: str
    aliases:
      - is
  poll:
    description:
      - The maximum number of times to call nodetool describecluster.
    type: int
    default: 1
  interval:
    description:
      - The number of seconds to wait between poll executions.
    type: int
    default: 30
'''

EXAMPLES = '''
- name: Ensure all Cassandra nodes are in schema agreement.
  community.cassandra.cassandra_schema:

- name: Ensure all Cassandra nodes have the expected schema version
  community.cassandra.cassandra_schema:
    is: 1176b7ac-8993-395d-85fd-41b89ef49fbb

- name: Poll schema version a max of 5 times with a 30 second interval
  community.cassandra.cassandra_schema:
    poll: 5
    interval: 30
'''

RETURN = '''
msg:
  description: A message indicating what has happened.
  returned: always
  type: str
rc:
  description: Return code of the last executed command.
  returned: when nodetool failed
  type: int
agreed:
  description:
    - Whether the nodes agree on one schema version (the expected one with I(uuid)).
    - "The result of a task run with C(failed_when: false) keeps it, when C(failed) does not."
  returned: always
  type: bool
schema_status:
  description: The schema versions of the last successful read, each with its nodes.
  returned: always
  type: dict
  sample: {"86afa796-d883-3932-aa73-6b017cef0d19": ["10.0.0.1:7000", "10.0.0.2:7000"]}
unreachable_nodes:
  description:
    - The nodes describecluster lists as UNREACHABLE in the last successful read.
    - "Not C(unreachable): Ansible reads that key of a result as the host itself unreachable."
  returned: when there are some
  type: list
  elements: str
iterations:
  description: The number of describecluster calls, when more than one.
  returned: when more than one
  type: int
'''

from ansible.module_utils.basic import AnsibleModule
import re
import time
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCmd
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_common_options import cassandra_common_argument_spec


class NodeToolStatusCommand(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;

        - status_command

    """

    def __init__(self, module):
        NodeToolCmd.__init__(self, module)
        self.status_cmd = "describecluster"

    def status_command(self):
        return self.nodetool_cmd(self.status_cmd)


def nodetool_status_poll(module):
    '''
    Calls nodetool describecluster a maximum of poll times with the
    indicated interval. Returns as soon as the nodes agree on one schema
    version (the expected one when uuid is given), or when poll runs out.
    '''
    schema_status = {}  # the versions of the last successful read
    unreachable = []
    cluster_schema_list = []
    iterations = 0
    return_codes = []
    stdout_list = []
    stderr_list = []
    poll = module.params['poll']
    interval = module.params['interval']
    uuid = module.params['uuid']

    while iterations < poll:
        iterations += 1
        n = NodeToolStatusCommand(module)
        (rc, out, err) = n.status_command()
        stdout_list.append(out.strip())
        stderr_list.append(err.strip())
        return_codes.append(rc)
        if rc == 0:
            schema_status = cluster_schema(out)
            unreachable = unreachable_nodes(out)
            cluster_schema_list.append(schema_status)
            if agreed(schema_status, uuid):
                break
        if iterations < poll:
            time.sleep(interval)  # not agreed yet, or nodetool failed: check again in a bit
    return schema_status, unreachable, cluster_schema_list, iterations, \
        return_codes, stdout_list, stderr_list


def agreed(schema_status, uuid=None):
    """One schema version (UNREACHABLE nodes are not one), the expected one when uuid is given."""
    return len(schema_status) == 1 and (not uuid or uuid in schema_status)


def unreachable_nodes(stdout):
    """The nodes describecluster lists under UNREACHABLE (no schema version known for them)."""
    match = re.search(r"^\s*UNREACHABLE: \[(.*)\]\s*$", stdout, re.MULTILINE)
    return [n.strip() for n in match.group(1).split(",") if n.strip()] if match else []


def versions_text(schema_status):
    """'<version> on <nodes>; ...' for a message."""
    return "; ".join("%s on %s" % (v, ", ".join(nodes)) for v, nodes in sorted(schema_status.items()))


def cluster_schema(stdout):
    '''
    Extract the schema versions from the nodetool describecluster stdout
    Returns a dict in the following format...
        {
            "d4f18346-f81f-3786-aed4-40e03558b299:" [127.0.0.1]
        }
    The UNREACHABLE line is not a schema version: see unreachable_nodes.
    '''
    return_dict = {}

    # match the schema id and the list of hosts
    regex = r"(\{){0,1}[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{12}(\}){0,1}: \[.*\]"

    for match in re.finditer(regex, stdout, re.MULTILINE):
        uuid, host_list = match.group().split(":", 1)
        return_dict[uuid.strip()] = host_list.strip()[1:-1].split(", ")

    return return_dict


def main():
    argument_spec = cassandra_common_argument_spec()
    argument_spec.update(
        uuid=dict(type='str', aliases=['is']),
        poll=dict(type='int', default=1),
        interval=dict(type='int', default=30)
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=False,
    )

    uuid = module.params['uuid']
    debug = module.params['debug']

    schema_status, unreachable, cluster_schema_list, iterations, \
        return_codes, stdout_list, stderr_list = nodetool_status_poll(module)

    result = {'agreed': agreed(schema_status, uuid) and return_codes[-1] == 0}

    result['schema_status'] = schema_status
    if unreachable:
        result['unreachable_nodes'] = unreachable  # (a key 'unreachable' would make Ansible mark the host unreachable)
    if iterations > 1:
        result['iterations'] = iterations

    if debug:
        result['cluster_schema_list'] = cluster_schema_list
        result['return_codes'] = return_codes
        if stderr_list:
            result['stderr_list'] = stderr_list
        if stdout_list:
            result['stdout_list'] = stdout_list

    not_counted = " (unreachable, not counted: {0})".format(", ".join(unreachable)) if unreachable else ""
    if return_codes[-1] != 0:  # the last call failed: what nodetool said
        result['msg'] = "nodetool error: {0}".format(stderr_list[-1] or stdout_list[-1])
        result['rc'] = return_codes[-1]
        module.fail_json(**result)
    elif not schema_status:
        result['msg'] = "nodetool describecluster listed no schema version"
        module.fail_json(**result)
    elif agreed(schema_status, uuid):
        result['msg'] = ("The cluster has reached consensus with the expected version" if uuid
                         else "The cluster has reached schema consensus") + not_counted
    elif len(schema_status) == 1:
        result['msg'] = "The cluster agrees on schema version {0}, not the expected {1}".format(
            list(schema_status)[0], uuid)
        module.fail_json(**result)
    else:
        result['msg'] = "The cluster has not reached consensus on the schema: {0} versions, {1}{2}".format(
            len(schema_status), versions_text(schema_status), not_counted)
        module.fail_json(**result)

    # Everything is good
    module.exit_json(**result)


if __name__ == '__main__':
    main()
