# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ipaddress
import re

from ansible.errors import AnsibleFilterError

# "host:" has no port, as Cassandra reads it
_BRACKETED = re.compile(r"^\[([^\]]+)\](?::(\d*))?$")
_HOST_PORT = re.compile(r"^([^:]+):(\d*)$")


def _is_ip(text):
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _entry(seed):
    if seed.startswith("[") or seed.count(":") == 1:
        match = (_BRACKETED if seed.startswith("[") else _HOST_PORT).match(seed)
        if not match:
            raise AnsibleFilterError("cassandra_seed_list: invalid seed %r (host, host:port or [ipv6]:port)" % seed)
        host, port = match.groups()
        if port and not 0 < int(port) < 65536:
            raise AnsibleFilterError("cassandra_seed_list: invalid port in seed %r (1-65535)" % seed)
    else:  # a name, an IPv4 address or a bare IPv6 address
        host, port = seed, None
        if ":" in host and not _is_ip(host.split("%")[0]):
            raise AnsibleFilterError("cassandra_seed_list: invalid seed %r (host, host:port or [ipv6]:port)" % seed)
    return {"host": host.lower(), "port": int(port) if port else None}


def cassandra_seed_list(seeds):
    """cassandra_seeds (a list, or the comma-separated string cassandra.yaml
    takes) as [{'host', 'port'}], host in lower case, port None when the seed has none."""
    if seeds is None:
        return []
    if isinstance(seeds, (list, tuple)):
        seeds = ",".join(str(s) for s in seeds)  # as the template writes it: an item may hold commas too
    elif not isinstance(seeds, str):
        raise AnsibleFilterError("cassandra_seed_list: a list or a comma-separated string expected, got %r" % (seeds,))
    return [_entry(seed) for seed in (s.strip() for s in seeds.split(",")) if seed]


class FilterModule(object):
    def filters(self):
        return {"cassandra_seed_list": cassandra_seed_list}
