from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import gossip_status, ring_state

STATUS = """Datacenter: dc1
===============
Status=Up/Down
|/ State=Normal/Leaving/Joining/Moving
--  Address                  Load       Tokens  Owns (effective)  Host ID                               Rack
UN  10.0.0.1                 1.1 MiB    16      100.0%            6d194555-f6eb-41d0-c000-000000000001  r1
DN  0:0:0:0:0:0:0:1          1.1 MiB    16      100.0%            6d194555-f6eb-41d0-c000-000000000002  r1
"""

GOSSIP = """/10.0.0.1
  generation:1
  STATUS:14:NORMAL,-1
/0:0:0:0:0:0:0:1
  generation:1
  STATUS:14:shutdown,true
"""


def test_ipv6_found_however_written():
    # nodetool prints IPv6 in full: ::1 is that node
    assert ring_state(STATUS, "::1") == "DN"
    assert ring_state(STATUS, "0:0:0:0:0:0:0:1") == "DN"
    assert gossip_status(GOSSIP, "::1") == "shutdown"
    assert ring_state(STATUS, "10.0.0.1") == "UN"
    assert ring_state(STATUS, "10.0.0.2") is None
    assert gossip_status(GOSSIP, "::2") is None
