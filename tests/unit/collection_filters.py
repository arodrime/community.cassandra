from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os


def load_collection_filters():
    """Templar finds the community.cassandra.* filters through the collection
    loader, which ansible-test units installs (CI); installed here otherwise, for
    a plain pytest run of the files that use it (this checkout first)."""
    from ansible.plugins.loader import init_plugin_loader
    from ansible.utils.collection_loader import AnsibleCollectionConfig
    if AnsibleCollectionConfig.collection_finder is None:
        # .../ansible_collections/community/cassandra/tests/unit: the directory above ansible_collections
        init_plugin_loader([os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))])
