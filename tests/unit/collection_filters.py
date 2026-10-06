from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import sys


def load_collection_filters():
    """Templar finds the community.cassandra.* filters through the collection
    loader, which ansible-test units installs (CI); installed here otherwise, for
    a plain pytest run of the files that use it."""
    from ansible.utils.collection_loader._collection_finder import _AnsibleCollectionFinder
    if not any(isinstance(f, _AnsibleCollectionFinder) for f in sys.meta_path):
        # .../ansible_collections/community/cassandra/tests/unit: the directory above ansible_collections
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
        _AnsibleCollectionFinder(paths=[root])._install()
