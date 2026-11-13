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
from ansible.release import __version__ as ansible_version
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
from ansible_collections.community.cassandra.tests.unit.roles.test_java_tarball_checks import _NotFound, _play

MIRROR = ("For a file on the mirror (the server of cassandra_install_url): take the login out of the URL and set"
          " cassandra_install_username and cassandra_install_password.")
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
    ("cassandra_install", "java_tarball.yml", "Download the Java tarball", "cassandra_java_tarball",
     "cassandra_java_tarball_checksum"),
    ("cassandra_install", "dsbulk.yml", "Download dsbulk", "cassandra_dsbulk_url", "cassandra_dsbulk_checksum"),
    ("cassandra_config", "prometheus_exporter_jar.yml", "Download the exporter's jar", "cassandra_prometheus_exporter_url",
     "cassandra_prometheus_exporter_checksum"),
    ("cassandra_repository", "repository.yml", "Download the signing keys", "cassandra_repository_key_url", None),
]


@pytest.mark.parametrize("role, tasks, name, variable, checksum", DOWNLOADS)
@pytest.mark.parametrize("url, sums, hidden", [
    ("https://u:pw@m.example.com/f.tgz", "", True),
    ("https://T0KEN@m.example.com/f.tgz", "", True),
    ("https://m.example.com/f.tgz" + SIGNED, "", True),
    ("https://u:pw@m.example.com/f.tgz" + SIGNED, "", True),
    ("https://m.example.com/f.tgz#access_token=abc", "", True),
    ("https://m.example.com/f.tgz", "", False),  # its error stays shown
    # a checksum URL with a login, a query or a fragment
    ("https://m.example.com/f.tgz", "sha256:https://u:pw@m.example.com/f.sha256", True),
    ("https://m.example.com/f.tgz", "sha256:https://m.example.com/f.sha256" + SIGNED, True),
    ("https://m.example.com/f.tgz", "sha256:https://m.example.com/f.sha256#t=abc", True),
    ("https://m.example.com/f.tgz", "sha256:0123abcd", False),
])
def test_no_log_where_the_url_holds_a_secret(role, tasks, name, variable, checksum, url, sums, hidden):
    download = _find(_tasks(role, tasks), name, "ansible.builtin.get_url")
    variables = {variable: url}
    if checksum:
        variables[checksum] = sums
    elif sums:  # no checksum there
        return
    assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template(download["no_log"])) is hidden


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
    ("cassandra_install", "java_check.yml", "The tarball URL holds a plain login, if any", "cassandra_java_tarball",
     "Take the login out of the URL and set cassandra_java_tarball_username and cassandra_java_tarball_password."),
    ("cassandra_install", "dsbulk.yml", "The dsbulk URL holds a plain login, if any", "cassandra_dsbulk_url", MIRROR),
    ("cassandra_config", "prometheus_exporter_jar.yml", "The exporter's URL holds a plain login, if any",
     "cassandra_prometheus_exporter_url", MIRROR),
    ("cassandra_repository", "repository.yml", "The signing keys URL holds a plain login, if any",
     "cassandra_repository_key_url", MIRROR),
]
DECODES = tuple(int(n) for n in ansible_version.split(".")[:2]) >= (2, 19)


def _check(tmp_path, role, tasks, name, variable, url):
    check = _find(_tasks(role, tasks), name, "ansible.builtin.assert")
    return _play(tmp_path, [check], {variable: url, "cassandra_java_version": "17"}, verbosity=3)


# get_url would read another URL (a host, a "port" taken from the login), or send the login as it is, and show
# the URL whole in its error: refused before, the URL not shown (-vvv)
@pytest.mark.parametrize("role, tasks, name, variable, way_out", CHECKS)
@pytest.mark.parametrize("url", [
    'https://reader:s3"cr3t@m.example.com/f.tgz',
    "https://reader:s3 cr3t@m.example.com/f.tgz",
    "https://reader:s3<cr3t@m.example.com/f.tgz",
    "https://reader:s3/cr3t@m.example.com/f.tgz",
    "https://reader:/s3cr3t@m.example.com/f.tgz",  # a / at the start
    "https://AbC/s3cr3t+tok@m.example.com/f.tgz",  # a token alone, a / in it
    "https://reader:12#s3cr3t@m.example.com/f.tgz",  # a "port" 12 before the #, the ?
    "https://reader:12?s3cr3t@m.example.com/f.tgz",
    "https://reader:s3@cr3t@m.example.com/f.tgz",
    "https://m.example.com/s3cr3t@v2/f.tgz",  # an @ in the path
])
def test_a_login_not_plain_is_refused_unshown(tmp_path, role, tasks, name, variable, way_out, url):
    rc, out = _check(tmp_path, role, tasks, name, variable, url)
    assert rc != 0, out
    assert "cr3t" not in out and "m.example.com" not in out
    assert "the part before its last @ is not a plain login." in out
    assert "The URL is not shown; nothing was downloaded." in out
    assert way_out in out
    assert "An @ in the path is written %40. Then run again." in out
    assert ("On ansible-core 2.19 or later (this run: %s), a login can be percent-encoded instead (/ as %%2F, @ as"
            " %%40...)." % ansible_version) in out


# ansible-core 2.16 sends a percent-encoded login as it is (401): refused there, taken on 2.19+
@pytest.mark.parametrize("role, tasks, name, variable, way_out", CHECKS)
@pytest.mark.parametrize("url", ["https://reader:s3%2Fcr%22t@m.example.com/f.tgz", "https://reader%40corp.com:s3cr3t@m.example.com/f.tgz"])
def test_a_login_percent_encoded_needs_2_19(tmp_path, role, tasks, name, variable, way_out, url):
    rc, out = _check(tmp_path, role, tasks, name, variable, url)
    assert (rc == 0) is DECODES, out
    assert "cr3t" not in out or rc == 0


@pytest.mark.parametrize("role, tasks, name, variable, way_out", CHECKS)
@pytest.mark.parametrize("url", ["https://reader:s3cr3t!$&'()*+,;=~@m.example.com:8443/f.tgz", "https://T0KEN@m.example.com/f.tgz",
                                 "https://m.example.com/v%402/f.tgz", "https://m.example.com/f.tgz" + SIGNED, "", "/srv/f.tgz"])
def test_a_plain_login_passes(tmp_path, role, tasks, name, variable, way_out, url):
    rc, out = _check(tmp_path, role, tasks, name, variable, url)
    assert rc == 0, out


@pytest.mark.skipif(HTTPServer is None, reason="Python 3")
@pytest.mark.parametrize("query, shown", [(SIGNED, "?****"), ("", "")])
def test_a_failed_exporter_download_says_why(tmp_path, query, shown):
    block = _find(_tasks("cassandra_config", "prometheus_exporter_jar.yml"), "Download the exporter's jar")
    jar = str(tmp_path / "jmx_prometheus_javaagent.jar")
    out = _fails(tmp_path, block, lambda url: {
        "cassandra_prometheus_exporter_url": url, "cassandra_prometheus_exporter_checksum": "",
        "cassandra_prometheus_exporter_dir": str(tmp_path), "_cassandra_config_exporter_jar": jar,
        "_cassandra_config_exporter_jar_state": "missing", "cassandra_install_url": ""}, query, "s3cret")
    assert ("jar could not be downloaded to %s from"
            " http://****@127.0.0.1:PORT/f.tar.gz%s (HTTP 404, Not Found). The JVM options" % (jar, shown)) in out


@pytest.mark.parametrize("role, tasks, name", [
    ("cassandra_install", "java_tarball.yml", "Stop on the failed download"),
    ("cassandra_install", "dsbulk.yml", "Stop on the failed dsbulk download"),
    ("cassandra_repository", "repository.yml", "Stop on the failed signing keys download"),
    ("cassandra_config", "prometheus_exporter_jar.yml", "Stop where the jar could not be downloaded"),
])
def test_what_the_download_said_ends_without_a_dot(role, tasks, name):
    # the message adds its own: "... did not match.." otherwise
    said = _find(_tasks(role, tasks), name)["vars"]["_said"]
    failed = {"msg": "The checksum for /x did not match 0abc; it was 1def. "}
    assert Templar(loader=DataLoader(), variables={"ansible_failed_result": failed}).template(
        trust_as_template(said)) == "The checksum for /x did not match 0abc; it was 1def"
