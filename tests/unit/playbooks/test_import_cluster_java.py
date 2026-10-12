from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the Java settings written for a node, so that site.yml stays
# a no-op after the import: a Java package is kept off a shared offer of
# tarballs, and a Java its series does not support (5.0 starts on 21) gets
# cassandra_java_allow_unsupported.

import pytest

from ansible_collections.community.cassandra.tests.unit.import_nodes import node


def java_vars(series, java, home=""):
    hv = {"import_cluster_series": series, "import_cluster_java": java, "import_cluster_java_home": home,
          "import_cluster_java_package": ""}
    return dict((k, v) for k, v in node(hv)["vars"].items() if k.startswith("cassandra_java_"))


@pytest.mark.parametrize("series, java", [("50x", "17"), ("50x", "11"), ("41x", "8"), ("40x", "11")])
def test_supported_java_not_flagged(series, java):
    assert "cassandra_java_allow_unsupported" not in java_vars(series, java)


def test_java_package_kept_off_the_tarball_offer():
    assert java_vars("50x", "17") == {"cassandra_java_tarballs": {}}
    assert java_vars("50x", "11") == {"cassandra_java_version": "11", "cassandra_java_tarballs": {}}


def test_unsupported_running_java_kept():
    assert java_vars("50x", "21", home="/opt/jdk-21") == {
        "cassandra_java_version": "21", "cassandra_java_home": "/opt/jdk-21", "cassandra_java_allow_unsupported": True}
