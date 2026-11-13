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


def _fails(tmp_path, block, variables, query, password):
    server = HTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = "http://reader:%s@127.0.0.1:%d/f.tar.gz%s" % (password, server.server_port, query)
        # -vvv: the module's arguments, the retries' results shown
        rc, out = _play(tmp_path, [dict(block, when="true")], dict(variables(url), cassandra_install_username=""),
                        verbosity=3)
    finally:
        server.shutdown()
    assert rc != 0, out
    assert password not in out and "Signature=5" not in out
    return out.replace(str(server.server_port), "PORT")


@pytest.mark.skipif(HTTPServer is None, reason="Python 3")
@pytest.mark.parametrize("query, shown, password", [
    (SIGNED, "?****", "s3cret"),
    ("?X-Amz-Signature=5i'g\"<", "?****", "s3cret"),  # a query masked whole
    ("", "", "s3%2Fc%22r%20et"),  # a login percent-encoded: downloaded, masked
])
def test_a_failed_dsbulk_download_says_why(tmp_path, query, shown, password):
    block = _find(_tasks("cassandra_install", "dsbulk.yml"), "Download dsbulk, or say why not")
    out = _fails(tmp_path, block, lambda url: {
        "cassandra_dsbulk_url": url, "cassandra_dsbulk_version": "1.11.2", "cassandra_dsbulk_checksum": "",
        "_cassandra_install_dsbulk_home": str(tmp_path / "dsbulk-1.11.2"), "_on_mirror": False}, query, password)
    assert ("dsbulk 1.11.2 could not be downloaded from http://****@127.0.0.1:PORT/f.tar.gz%s: HTTP 404,"
            " Not Found. dsbulk was not installed. Check cassandra_dsbulk_url and cassandra_dsbulk_checksum (or set"
            " cassandra_dsbulk_install to false), then run again." % shown) in out


@pytest.mark.skipif(HTTPServer is None, reason="Python 3")
@pytest.mark.parametrize("query, shown, password", [
    (SIGNED, "?****", "s3cret"),
    ("?X-Amz-Signature=5i'g\"<", "?****", "s3cret"),  # a query masked whole
    ("", "", "s3%2Fc%22r%20et"),  # a login percent-encoded: downloaded, masked
])
def test_a_failed_signing_keys_download_says_why(tmp_path, query, shown, password):
    block = _find(_tasks("cassandra_repository", "repository.yml"), "Download the signing keys, or say why not")
    out = _fails(tmp_path, block, lambda url: {
        "cassandra_repository_key_url": url, "_cassandra_repository_key_path": str(tmp_path / "KEYS"),
        "cassandra_install_url": "", "cassandra_install_password": ""}, query, password)
    assert ("The Cassandra signing keys could not be downloaded from http://****@127.0.0.1:PORT/f.tar.gz%s:"
            " HTTP 404, Not Found. The repository source is left as it is. Check cassandra_repository_key_url"
            " (empty: the keys shipped with the role), then run again." % shown) in out


CHECKS = [
    ("cassandra_install", "java_check.yml", "The tarball URL's login is percent-encoded", "cassandra_java_tarball"),
    ("cassandra_install", "dsbulk.yml", "The dsbulk URL's login is percent-encoded", "cassandra_dsbulk_url"),
    ("cassandra_config", "prometheus_exporter_jar.yml", "The exporter's URL login is percent-encoded",
     "cassandra_prometheus_exporter_url"),
    ("cassandra_repository", "repository.yml", "The signing keys URL's login is percent-encoded",
     "cassandra_repository_key_url"),
]


# get_url would read another URL (a host, a "port" taken from the login) and show it whole in its error: refused
# before, the URL not shown (-vvv)
@pytest.mark.parametrize("role, tasks, name, variable", CHECKS)
@pytest.mark.parametrize("password", ['s3"cr3t', "s3 cr3t", "s3<cr3t", "s3/cr3t", "s3#cr3t", "s3?cr3t", "s3@cr3t"])
def test_a_login_to_encode_is_refused_unshown(tmp_path, role, tasks, name, variable, password):
    check = _find(_tasks(role, tasks), name, "ansible.builtin.assert")
    rc, out = _play(tmp_path, [check], {variable: "https://reader:%s@m.example.com/f.tgz" % password,
                                        "cassandra_java_version": "17"}, verbosity=3)
    assert rc != 0, out
    assert "cr3t" not in out and "m.example.com" not in out
    assert ("holds a login with a character that must be percent-encoded (/ # ? \" @ space...): write it as %2F %23"
            " %3F %22 %40 %20. The URL is not shown (its login), nothing was downloaded.") in out


@pytest.mark.parametrize("role, tasks, name, variable", CHECKS)
@pytest.mark.parametrize("url", ["https://reader:s3%2Fcr%22t@m.example.com/f.tgz", "https://m.example.com/v@2/f.tgz",
                                 "https://m.example.com/f.tgz?X-Amz-Signature=a@b", ""])
def test_a_login_percent_encoded_passes(tmp_path, role, tasks, name, variable, url):
    check = _find(_tasks(role, tasks), name, "ansible.builtin.assert")
    rc, out = _play(tmp_path, [check], {variable: url, "cassandra_java_version": "17"})
    assert rc == 0, out


@pytest.mark.parametrize("role, tasks, name", [
    ("cassandra_install", "java_tarball.yml", "Stop on the failed download"),
    ("cassandra_install", "dsbulk.yml", "Stop on the failed dsbulk download"),
    ("cassandra_repository", "repository.yml", "Stop on the failed signing keys download"),
])
def test_what_the_download_said_ends_without_a_dot(role, tasks, name):
    # the message adds its own: "... did not match.." otherwise
    said = _find(_tasks(role, tasks), name)["vars"]["_said"]
    failed = {"msg": "The checksum for /x did not match 0abc; it was 1def. "}
    assert Templar(loader=DataLoader(), variables={"ansible_failed_result": failed}).template(
        trust_as_template(said)) == "The checksum for /x did not match 0abc; it was 1def"
