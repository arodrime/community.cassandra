# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What the filters tell of a keyspace by its name."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def is_system_keyspace(name):
    """Cassandra's own keyspaces: system and system_* (system_auth, system_schema, ...); systemx is a user's."""
    name = str(name)
    return name == "system" or name.startswith("system_")
