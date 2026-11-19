from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config adds its header line ("Managed by Ansible ...: change the role variables, not this file") to a
# file it leaves as it is (the same settings as the role's, written another way, e.g. imported): the header alone,
# the rest of the file byte for byte (files/config_header.py, on the node), never when the file with it would not
# have the same settings (cassandra_config_header: the proof that no restart is needed for it).

import hashlib
import json
import os
import subprocess
import sys

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    HEADER_MARK, _config_header, cassandra_config_header, cassandra_same_settings)

YAML_HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file."
XML_HEADER = "<!-- Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file. -->"
SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "files",
                      "config_header.py")
H = b"# header"


@pytest.mark.parametrize("data, added", [
    (b"a: 1\n", b"# header\na: 1\n"),
    (b"", b"# header\n"),
    (b"a: 1\r\nb: 2\r\n", b"# header\r\na: 1\r\nb: 2\r\n"),  # CRLF: the file's own line end
    (b"#!/bin/sh\nX=1\n", b"#!/bin/sh\n# header\nX=1\n"),  # an interpreter line stays first
    (b'<?xml version="1.0"?>\n<configuration/>\n', b'<?xml version="1.0"?>\n# header\n<configuration/>\n'),
    (b"\xef\xbb\xbfa: 1\n", b"\xef\xbb\xbf# header\na: 1\n"),  # after a UTF-8 BOM
    (b"#!/bin/sh", b"#!/bin/sh\n# header\n"),  # that line alone, no line end
    (b"a: \xff\n", b"# header\na: \xff\n"),  # bytes that are not UTF-8: as they are
])
def test_the_header_goes_first_the_rest_byte_for_byte(data, added):
    assert _config_header().with_header(data, H) == added


@pytest.mark.parametrize("name, live, new, header", [
    # imported as the node had it: same settings, no header
    ("cassandra.yaml", "cluster_name: 'X'  # prod\nnum_tokens: 16\n",
     YAML_HEADER + "\ncluster_name: 'X'\nnum_tokens: 16\n", YAML_HEADER),
    ("jvm-server.options", "-Xss256k\n", YAML_HEADER + "\n# stack\n-Xss256k\n", YAML_HEADER),
    ("cassandra-env.sh", 'MAX_HEAP_SIZE="8G"\n', YAML_HEADER + '\nMAX_HEAP_SIZE="8G"\n', YAML_HEADER),
    ("cassandra-rackdc.properties", "dc=dc1\nrack=r1\n", YAML_HEADER + "\ndc=dc1\nrack=r1\n", YAML_HEADER),
    # logback.xml: its XML comment, after an XML declaration that must stay first
    ("logback.xml", '<?xml version="1.0"?>\n<configuration scan="true"/>\n',
     XML_HEADER + '\n<configuration scan="true"/>\n', XML_HEADER),
    ("logback.xml", '<configuration scan="true"/>\n', XML_HEADER + '\n<configuration scan="true"/>\n', XML_HEADER),
])
def test_a_file_with_the_same_settings_gets_the_header_and_keeps_them(name, live, new, header):
    assert cassandra_same_settings(live, new, name)
    assert cassandra_config_header(live, new, name) == header
    # the file with it: the same settings still (no restart), and no header to add again
    added = _config_header().with_header(live.encode(), header.encode()).decode()
    assert cassandra_same_settings(added, new, name)
    assert cassandra_config_header(added, new, name) == ""


@pytest.mark.parametrize("live, new", [
    ("# Managed by Ansible (community.cassandra.cassandra_config): an older wording\nnum_tokens: 16\n",
     YAML_HEADER + "\nnum_tokens: 16\n"),  # it has one already
    ("num_tokens: 16\n", "num_tokens: 16\n"),  # the role's file has none (a template of your own)
    ("", YAML_HEADER + "\nnum_tokens: 16\n"),  # nothing read
    (None, YAML_HEADER + "\nnum_tokens: 16\n"),
])
def test_no_header_added(live, new):
    assert cassandra_config_header(live, new, "cassandra.yaml") == ""


def test_no_header_when_the_file_with_it_would_not_have_the_same_settings():
    # (a template of your own whose header line is a setting too: first, it would set X before the others)
    live, new = "X=2\nX=1\n", "X=2\nX=1  # " + HEADER_MARK + "\n"
    assert cassandra_same_settings(live, new, "cassandra-env.sh")
    assert cassandra_config_header(live, new, "cassandra-env.sh") == ""


def run(request):
    out = subprocess.run([sys.executable, SCRIPT], input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                         check=True).stdout
    return json.loads(out)


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def test_the_node_adds_it_in_place_once(tmp_path):
    path = tmp_path / "cassandra.yaml"
    path.write_bytes(b"num_tokens: 16\n")
    os.chmod(path, 0o640)
    inode = os.stat(path).st_ino
    item = {"path": str(path), "header": YAML_HEADER, "mark": HEADER_MARK, "sha1": sha1(b"num_tokens: 16\n")}
    done = run([item])[str(path)]
    assert path.read_bytes() == (YAML_HEADER + "\nnum_tokens: 16\n").encode()
    assert done == {"status": "added", "before": sha1(b"num_tokens: 16\n"), "after": sha1(path.read_bytes())}
    # in place: the same file, its mode
    assert os.stat(path).st_ino == inode and os.stat(path).st_mode & 0o777 == 0o640
    # again: it has it, left as it is
    assert run([item])[str(path)]["status"] == "present"
    assert path.read_bytes().count(HEADER_MARK.encode()) == 1


def test_the_node_leaves_a_file_changed_since_it_was_compared(tmp_path):
    path = tmp_path / "cassandra.yaml"
    path.write_bytes(b"num_tokens: 32\n")  # edited by hand after the compare
    item = {"path": str(path), "header": YAML_HEADER, "mark": HEADER_MARK, "sha1": sha1(b"num_tokens: 16\n")}
    assert run([item])[str(path)]["status"] == "changed"
    assert path.read_bytes() == b"num_tokens: 32\n"


def test_the_node_follows_no_link_and_says_a_missing_file(tmp_path):
    target = tmp_path / "shared.yaml"
    target.write_bytes(b"num_tokens: 16\n")
    link = tmp_path / "cassandra.yaml"
    link.symlink_to(target)
    items = [{"path": str(link), "header": YAML_HEADER, "mark": HEADER_MARK, "sha1": sha1(b"num_tokens: 16\n")},
             {"path": str(tmp_path / "gone.yaml"), "header": YAML_HEADER, "mark": HEADER_MARK, "sha1": ""}]
    done = run(items)
    assert done[str(link)]["status"] == "link" and done[str(tmp_path / "gone.yaml")]["status"] == "missing"
    assert target.read_bytes() == b"num_tokens: 16\n"


def test_find_lists_the_files_without_the_mark_with_their_sha1(tmp_path):
    # (what access.yml asks before the plan: the JMX files left as they are that get the header; nothing written)
    without, having = tmp_path / "jmxremote.access", tmp_path / "jmxremote.password"
    without.write_bytes(b"zorglub readwrite\n")
    having.write_bytes(H + b"\nzorglub s3cret\n")
    items = [{"file": f.name, "path": str(f), "header": "# header", "mark": "# header"}
             for f in (without, having, tmp_path / "gone")]
    assert run({"find": items}) == [dict(items[0], sha1=sha1(b"zorglub readwrite\n"))]
    assert without.read_bytes() == b"zorglub readwrite\n"
