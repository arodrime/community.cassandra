# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_same_yaml: two YAML texts hold the same settings (cassandra_config's
"same settings" test, on the controller for a node without PyYAML)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json

import yaml


def cassandra_same_yaml(live, new):
    """True when both texts load to the same data: comments, layout, quoting
    and a key set twice (the last one) do not count; 1, 1.0 and true differ."""
    if not live:
        return False
    try:
        return json.dumps(yaml.safe_load(live), sort_keys=True, default=str) == \
            json.dumps(yaml.safe_load(new), sort_keys=True, default=str)
    except yaml.YAMLError:
        return False


class FilterModule(object):
    def filters(self):
        return {"cassandra_same_yaml": cassandra_same_yaml}
