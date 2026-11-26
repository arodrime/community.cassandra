from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's config files written since the running Cassandra started (audit round 3, 2026-11-26): taken as
# apply_config takes them (its record of that start when it has one), else by their times, so that a re-import of
# a cluster apply_config manages says what apply_config will do.

import base64
import hashlib
import json

from ansible_collections.community.cassandra.plugins.filter.cassandra_import_nodes import cassandra_import_newer_files

JVM = {"pid": "4242", "start": "1000.00"}
FILES = {"files": [{"path": "/etc/cassandra/conf/cassandra.yaml", "ctime": 1500.0},
                   {"path": "/etc/cassandra/conf/jvm-server.options", "ctime": 1500.0},
                   {"path": "/etc/cassandra/conf/logback.xml", "ctime": 900.0}]}
TEXT = {"cassandra.yaml": b"a: 1\n", "jvm-server.options": b"-Xss256k\n", "logback.xml": b"<configuration/>\n"}
SLURPED = [{"item": k, "content": base64.b64encode(v).decode()} for k, v in TEXT.items()]


def sha1(text):
    return hashlib.sha1(text).hexdigest()


def record(**more):
    data = dict({"_jvm": {"pid": 4242, "start": 1000.5}}, **more)
    return base64.b64encode(json.dumps(data).encode()).decode()


def test_without_a_record_the_files_times():
    assert cassandra_import_newer_files(FILES, JVM) == ["cassandra.yaml", "jvm-server.options"]
    # (a record of another JVM: as none)
    other = base64.b64encode(json.dumps({"_jvm": {"pid": 1, "start": 1000.5}}).encode()).decode()
    assert cassandra_import_newer_files(FILES, JVM, other, SLURPED) == ["cassandra.yaml", "jvm-server.options"]


def test_with_the_record_of_that_start_its_checksums():
    # written since the start, but as the record says that start read them (or as the role renders the same
    # settings): not newer for apply_config; a file changed from the record: newer, whatever its time
    rec = record(**{"/etc/cassandra/conf/cassandra.yaml": sha1(TEXT["cassandra.yaml"]),
                    "_same_settings": {"/etc/cassandra/conf/jvm-server.options": sha1(TEXT["jvm-server.options"])},
                    "/etc/cassandra/conf/logback.xml": sha1(b"<configuration scan='true'/>\n")})
    assert cassandra_import_newer_files(FILES, JVM, rec, SLURPED) == ["logback.xml"]
    # a file not read: by its time
    assert cassandra_import_newer_files(FILES, JVM, rec, SLURPED[2:]) == ["cassandra.yaml", "jvm-server.options",
                                                                          "logback.xml"]
