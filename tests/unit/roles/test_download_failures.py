from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The downloads of a URL (the Java tarball, dsbulk, the exporter's jar, the signing keys): no_log where the URL
# holds a login or a query (ansible-core shows them in the module's arguments, the retries and the error, even on
# 2.19+ that masks the result's url only); a failure says why, the URL masked, run by ansible-playbook -vvv on
# localhost (the tasks taken from the roles' files).

import os
import threading

import pytest

try:
    from http.server import HTTPServer
except ImportError:  # Python 2: not run there (the controller's Python only)
    HTTPServer = None

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
from ansible_collections.community.cassandra.tests.unit.roles.test_java_tarball_checks import _NotFound, _play

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")
SIGNED = "?X-Amz-Signature=5ig"


def _tasks(role, name):
    return load_tasks(os.path.join(ROLES, role, "tasks", name))


def _find(tasks, name, module=None):
    """The task (or block) named name, the one running module when given."""
    todo = list(tasks)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name and (module is None or module in t):
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


DOWNLOADS = [
    ("cassandra_install", "java_tarball.yml", "Download the Java tarball", "cassandra_java_tarball"),
    ("cassandra_install", "dsbulk.yml", "Download dsbulk", "cassandra_dsbulk_url"),
    ("cassandra_config", "prometheus_exporter_jar.yml", "Download the exporter's jar", "cassandra_prometheus_exporter_url"),
    ("cassandra_repository", "repository.yml", "Download the signing keys", "cassandra_repository_key_url"),
]


@pytest.mark.parametrize("role, tasks, name, variable", DOWNLOADS)
@pytest.mark.parametrize("url, hidden", [
    ("https://u:pw@m.example.com/f.tgz", True),
    ("https://T0KEN@m.example.com/f.tgz", True),
    ("https://m.example.com/f.tgz" + SIGNED, True),
    ("https://u:pw@m.example.com/f.tgz" + SIGNED, True),
    ("https://m.example.com/f.tgz", False),  # its error stays shown
])
def test_no_log_where_the_url_holds_a_secret(role, tasks, name, variable, url, hidden):
    download = _find(_tasks(role, tasks), name, "ansible.builtin.get_url")
    assert Templar(loader=DataLoader(), variables={variable: url}).template(trust_as_template(download["no_log"])) is hidden


def _fails(tmp_path, block, variables, query):
    server = HTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = "http://reader:s3cret@127.0.0.1:%d/f.tar.gz%s" % (server.server_port, query)
        # -vvv: the module's arguments, the retries' results shown
        rc, out = _play(tmp_path, [dict(block, when="true")], dict(variables(url), cassandra_install_username=""),
                        verbosity=3)
    finally:
        server.shutdown()
    assert rc != 0, out
    assert "s3cret" not in out and "5ig" not in out
    return out.replace(str(server.server_port), "PORT")


@pytest.mark.skipif(HTTPServer is None, reason="Python 3")
@pytest.mark.parametrize("query, shown", [(SIGNED, "?****"), ("", "")])
def test_a_failed_dsbulk_download_says_why(tmp_path, query, shown):
    block = _find(_tasks("cassandra_install", "dsbulk.yml"), "Download dsbulk, or say why not")
    out = _fails(tmp_path, block, lambda url: {
        "cassandra_dsbulk_url": url, "cassandra_dsbulk_version": "1.11.2", "cassandra_dsbulk_checksum": "",
        "_cassandra_install_dsbulk_home": str(tmp_path / "dsbulk-1.11.2"), "_on_mirror": False}, query)
    assert ("dsbulk 1.11.2 could not be downloaded from http://****@127.0.0.1:PORT/f.tar.gz%s: HTTP 404,"
            " Not Found. Nothing was installed. Check cassandra_dsbulk_url and cassandra_dsbulk_checksum (or set"
            " cassandra_dsbulk_install to false), then run again." % shown) in out


@pytest.mark.skipif(HTTPServer is None, reason="Python 3")
@pytest.mark.parametrize("query, shown", [(SIGNED, "?****"), ("", "")])
def test_a_failed_signing_keys_download_says_why(tmp_path, query, shown):
    block = _find(_tasks("cassandra_repository", "repository.yml"), "Download the signing keys, or say why not")
    out = _fails(tmp_path, block, lambda url: {
        "cassandra_repository_key_url": url, "_cassandra_repository_key_path": str(tmp_path / "KEYS"),
        "cassandra_install_url": "", "cassandra_install_password": ""}, query)
    assert ("The Cassandra signing keys could not be downloaded from http://****@127.0.0.1:PORT/f.tar.gz%s:"
            " HTTP 404, Not Found. The repository is left as it is. Check cassandra_repository_key_url (empty:"
            " the keys shipped with the role), then run again." % shown) in out
