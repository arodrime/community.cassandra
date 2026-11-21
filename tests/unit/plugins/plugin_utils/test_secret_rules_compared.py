# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# The collection has three rules of what is a secret: cassandra_secrets (the controller's output, import_cluster),
# its node-side port in cassandra_config_read (the config diff a node shows; the controller's apply_config preview
# too) and cassandra_medusa's SECRET_KEY (medusa.ini's keys). They are not the same today: this table pins where
# they differ, so that a change to one of them is a deliberate one (and this table is updated with it).

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_medusa import SECRET_KEY as MEDUSA_KEY
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_config_read as node
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets as secrets

# a name -> masked by (cassandra_secrets, cassandra_config_read on the node, medusa.ini's rule), as "name: value"
# in a line (medusa: the key alone)
RULES = [
    ("db_password", True, True, True),
    ("passwd", True, True, True),
    ("my_secret", True, True, True),
    ("private_key", True, True, False),
    ("sse_c_key", True, True, True),
    ("access_key", True, True, True),
    ("key_material", True, True, False),
    ("auth_token", True, True, False),
    ("ca_key", True, True, False),
    ("store_kspw", True, True, True),
    ("store_tspw", True, True, True),
    ("cassandra_pw", True, True, False),
    ("user_pass", True, True, False),
    ("db_pwd", True, True, False),  # (the node: as _pw inside a name)
    ("authorization", True, True, False),
    ("headers", True, True, False),
    ("token", True, True, False),
    # cassandra_secrets only
    ("api_token", True, False, False),
    ("apikey", True, False, False),
    ("access_token", True, False, False),
    ("bearer_token", True, False, False),
    ("refresh_token", True, False, False),
    ("session_token", True, False, False),
    ("private_token", True, False, False),
    ("passphrase", True, False, False),
    ("encryption_key", True, False, False),
    ("aws_credentials", True, False, False),
    # none of them
    ("num_tokens", False, False, False),
    ("initial_token", False, False, False),
    ("keystore", False, False, False),
]


@pytest.mark.parametrize("name, controller, on_node, medusa", RULES)
def test_the_three_secret_rules(name, controller, on_node, medusa):
    line = "%s: hunter2" % name
    assert (secrets.mask(line) != line, node.masked([line]) != [line], bool(MEDUSA_KEY.search(name))) \
        == (controller, on_node, medusa)
