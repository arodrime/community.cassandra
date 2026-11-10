from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install's Java tarball, run by ansible-playbook on localhost (the tasks taken from the role's files):
# a tarball on the controller is checked against cassandra_java_tarball_checksum before anything changes (it was
# copied and unpacked whatever its checksum); a download that fails says the URL (a login in it masked) and the
# HTTP status; cassandra_java_link is made, moved, or left when it already leads to the Java.

import hashlib
import os
import subprocess
import sys
import threading

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # Python 2: not run there (the controller's Python only)
    BaseHTTPRequestHandler = HTTPServer = None

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
TASKS = os.path.join(TOP, "roles", "cassandra_install", "tasks")


def _tasks(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find(tasks, name):
    todo = list(tasks)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def _play(tmp_path, tasks, variables, check=False):
    """Runs tasks on localhost with variables -> (rc, the output, spaces normalised)."""
    play = [{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "localhost,",
            "-c", "local", "-e", "ansible_python_interpreter=%s" % sys.executable, str(tmp_path / "play.yml")]
    if check:
        argv.append("--check")
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    return result.returncode, " ".join(result.stdout.decode(errors="replace").split())


def _controller_check(tmp_path, checksum, path=None, **more):
    tarball = tmp_path / "OpenJDK17U-jdk_x64_linux_hotspot_17.0.18_8.tar.gz"
    tarball.write_bytes(b"not really a tarball\n")
    variables = dict({"cassandra_java_tarball": str(path or tarball), "cassandra_java_tarball_checksum": checksum,
                      "cassandra_java_version": "17"}, **more)
    return _play(tmp_path, [_find(_tasks("java_check.yml"), "Check the Java tarball on the controller")], variables)


def test_a_controller_tarball_is_checked_against_its_checksum(tmp_path):
    good = hashlib.sha256(b"not really a tarball\n").hexdigest()
    (tmp_path / "good").mkdir()
    rc, out = _controller_check(tmp_path / "good", "sha256:" + good.upper())
    assert rc == 0, out
    (tmp_path / "bad").mkdir()
    rc, out = _controller_check(tmp_path / "bad", "sha256:" + "0" * 64)
    assert rc != 0, out
    assert ("The Java tarball %s on the controller has sha256 %s, not %s (cassandra_java_tarball_checksum). Nothing was"
            " changed on localhost. Give the right file or the right checksum (in cassandra_java_tarballs[\"17\"], or"
            " cassandra_java_tarball and cassandra_java_tarball_checksum), then run again."
            % (tmp_path / "bad" / "OpenJDK17U-jdk_x64_linux_hotspot_17.0.18_8.tar.gz", good, "0" * 64)) in out
    # another algorithm
    (tmp_path / "md5").mkdir()
    rc, out = _controller_check(tmp_path / "md5", "md5:" + hashlib.md5(b"not really a tarball\n").hexdigest())
    assert rc == 0, out
    # none given: only there
    (tmp_path / "none").mkdir()
    assert _controller_check(tmp_path / "none", "")[0] == 0
    (tmp_path / "missing").mkdir()
    rc, out = _controller_check(tmp_path / "missing", "", path=tmp_path / "missing" / "jdk.tar.gz")
    assert rc != 0 and "The Java tarball %s is not a file on the controller." % (tmp_path / "missing" / "jdk.tar.gz") in out
    # not <algorithm>:<hex>
    (tmp_path / "form").mkdir()
    rc, out = _controller_check(tmp_path / "form", "sha256:https://mirror/x.sha256")
    assert rc != 0 and ("a checksum URL is read only for a tarball the nodes download (cassandra_java_tarball a URL);"
                        " for a file on the controller give the checksum itself as <algorithm>:<hex>") in out
    (tmp_path / "algo").mkdir()
    rc, out = _controller_check(tmp_path / "algo", "crc32:1234")
    assert rc != 0 and "crc32:1234: give as <algorithm>:<hex> (md5, sha1, sha224, sha256, sha384 or sha512)" in out
    # unpacked there already: not needed
    (tmp_path / "done").mkdir()
    assert _controller_check(tmp_path / "done", "sha256:" + "0" * 64,
                             cassandra_java_check_unpacked={"stat": {"exists": True}})[0] == 0


def test_the_controller_check_runs_before_anything_changes():
    # java_check.yml: run by install before java.yml, and by update_java, upgrade and the new node checks in their plan
    names = [t["name"] for t in _tasks("install.yml")]
    assert names.index("Check the Java") < names.index("Java, the package or a tarball, as the default java")
    check = _find(_tasks("java_check.yml"), "Check the Java tarball on the controller")
    read = _find(check["block"], "Read the tarball's checksum on the controller")
    assert read["delegate_to"] == "localhost" and read["become"] is False and read["check_mode"] is False


class _NotFound(BaseHTTPRequestHandler if BaseHTTPRequestHandler else object):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_a_failed_download_says_the_url_and_the_http_status(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = "http://mirror-user:s3cret@127.0.0.1:%d/java/jdk-17.tar.gz" % server.server_port
        download = _find(_tasks("java_tarball.yml"), "Download the Java tarball")
        assert "block" in download and download["rescue"]
        download = dict(download, when="true")
        variables = {"cassandra_java_tarball": url, "cassandra_java_tarball_checksum": "",
                     "cassandra_java_tarball_username": "", "cassandra_java_tarball_password": "",
                     "_local": str(tmp_path / "jdk.tar.gz"), "cassandra_java_version": "17"}
        rc, out = _play(tmp_path, [download], variables)
    finally:
        server.shutdown()
    assert rc != 0, out
    assert ("TASK [Stop on the failed download]" in out and "localhost: the Java tarball" not in out)
    assert ("The Java tarball could not be downloaded from http://mirror-user:****@127.0.0.1:%d/java/jdk-17.tar.gz:"
            " HTTP 404, Not Found. Nothing was unpacked. Check that URL (cassandra_java_tarball, or the"
            " cassandra_java_tarballs entry of Java 17) and its checksum, then run again." % server.server_port) in out
    assert "s3cret" not in out
    # a 404 is final: asked once
    assert "retries left" not in out


def _link(tmp_path, home, link, check=False):
    tasks = _tasks("java_tarball.yml")
    return _play(tmp_path, [_find(tasks, "Resolve the Java link and this Java"), _find(tasks, "Point the Java link at this Java")],
                 {"_cassandra_java_home": str(home), "_cassandra_java_link": str(link)}, check=check)


def test_the_java_link_follows_the_java_in_use(tmp_path):
    old, new = tmp_path / "jre" / "jdk-11.0.30", tmp_path / "jre" / "jdk-11.0.32"
    old.mkdir(parents=True)
    new.mkdir()
    link = tmp_path / "jre" / "jdk"
    link.symlink_to("jdk-11.0.30")  # relative, made by hand
    rc, out = _link(tmp_path, old, link)
    assert rc == 0 and "changed=0" in out, out  # already leads there: left as it is
    assert os.readlink(str(link)) == "jdk-11.0.30"
    rc, out = _link(tmp_path, new, link, check=True)
    assert rc == 0 and "changed=1" in out and os.readlink(str(link)) == "jdk-11.0.30", out
    rc, out = _link(tmp_path, new, link)
    assert rc == 0 and "changed=1" in out and os.path.realpath(str(link)) == str(new), out
    # back (a rollback: cassandra_java_home set to the old one)
    rc, out = _link(tmp_path, old, link)
    assert rc == 0 and os.path.realpath(str(link)) == str(old), out
    # none yet: made
    rc, out = _link(tmp_path, new, tmp_path / "current")
    assert rc == 0 and os.path.realpath(str(tmp_path / "current")) == str(new), out


def test_the_java_link_refusals(tmp_path):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    tasks = _tasks("java_check.yml")
    home = _find(tasks, "The Java link is for a Java home")

    def refused(link, java_home="/apps/jre/jdk-11.0.30", conf="/etc/cassandra/conf"):
        variables = dict(_system=trust_as_template(home["vars"]["_system"]), _conf=trust_as_template(home["vars"]["_conf"]),
                         cassandra_java_link=link, _cassandra_java_link=link.rstrip("/") or "/",
                         _cassandra_java_home=java_home, cassandra_conf_dir=conf, inventory_hostname="n1")
        templar = Templar(loader=DataLoader(), variables=variables)
        if all(templar.template(trust_as_template("{{ %s }}" % t)) for t in home["ansible.builtin.assert"]["that"]):
            return ""
        return " ".join(templar.template(trust_as_template(home["ansible.builtin.assert"]["fail_msg"])).split())
    assert refused("/apps/jre/jdk/") == ""
    assert refused("/opt/java/current") == ""
    assert refused("/apps/jre/jdk", java_home="") == (
        "n1: cassandra_java_link is /apps/jre/jdk: a link is kept pointing at a Java home (cassandra_java_home, or a"
        " tarball), and this node has a Java package. Nothing was changed. Set cassandra_java_link to the path of a"
        " link of its own (e.g. /opt/java/current), or remove it.")
    assert "it must be an absolute path" in refused("jdk")
    assert "it must be an absolute path" in refused("/apps/jre/jdk-11.0.30/")
    # system paths and the config directory: never made a link
    for link in ("/usr/bin/java", "/etc/alternatives/java", "/bin", "/lib64", "/lib/jvm", "/usr/lib/jvm", "/usr/lib/x",
                 "/usr/lib/jvm/a/b", "/opt", "/usr", "/etc/x", "/etc/cassandra/conf", "/srv/conf/java"):
        assert "a system path or the config directory" in refused(link, conf="/srv/conf"), link
    # where Javas are linked by name: allowed (e.g. /usr/lib/jvm/java-17 -> /opt/jdk-17)
    for link in ("/usr/lib/jvm/java-17", "/usr/lib64/jvm/jre-11/", "/lib/jvm/current"):
        assert refused(link) == "", link

    # what is there: read by a script, refused unless absent, a link to a Java, or a link leading nowhere
    look = _find(tasks, "Look at the Java link")
    assert look["check_mode"] is False
    jdk = tmp_path / "jdk-11"
    (jdk / "bin").mkdir(parents=True)
    (jdk / "bin" / "java").write_text("#!/bin/sh\n")
    (jdk / "bin" / "java").chmod(0o755)
    other = tmp_path / "data"
    other.mkdir()
    (tmp_path / "to-java").symlink_to(jdk)
    (tmp_path / "to-data").symlink_to(other)
    (tmp_path / "nowhere").symlink_to(tmp_path / "gone")
    (tmp_path / "file").write_text("x")

    def kind(path):
        out = subprocess.run(["/bin/sh", "-c", look["ansible.builtin.shell"]], env={"L": str(path), "PATH": os.environ["PATH"]},
                             stdout=subprocess.PIPE, universal_newlines=True, check=True)
        return out.stdout.strip()
    # a package owns it (rpm -qf answers), or it leads straight into /etc/alternatives: refused
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rpm").write_text('#!/bin/sh\ncase "$4" in *owned*) echo java-17-openjdk-headless ;; *) exit 1 ;; esac\n')
    (tmp_path / "bin" / "rpm").chmod(0o755)
    (tmp_path / "owned").symlink_to(jdk)
    script = look["ansible.builtin.shell"]
    out = subprocess.run(["/bin/sh", "-c", script], env={"L": str(tmp_path / "owned"),
                                                         "PATH": "%s:%s" % (tmp_path / "bin", os.environ["PATH"])},
                         stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
    assert out == "owned by the package java-17-openjdk-headless"
    # a path no package owns: rpm says so on its standard output, failing (RHEL, Rocky): the link is taken
    (tmp_path / "bin" / "rpm").write_text('#!/bin/sh\necho "file $4 is not owned by any package"\nexit 1\n')
    (tmp_path / "bin" / "dpkg").write_text("#!/bin/sh\necho \"dpkg-query: no path found matching pattern $2\" >&2\nexit 1\n")
    (tmp_path / "bin" / "dpkg").chmod(0o755)
    (tmp_path / "mine").symlink_to(jdk)
    out = subprocess.run(["/bin/sh", "-c", script], env={"L": str(tmp_path / "mine"),
                                                         "PATH": "%s:%s" % (tmp_path / "bin", os.environ["PATH"])},
                         stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
    assert out == "link to a Java"
    alt = script.replace("/etc/alternatives/", str(tmp_path / "alternatives") + "/")
    (tmp_path / "alternatives").mkdir()
    (tmp_path / "alternatives" / "jre_17").symlink_to(jdk)
    (tmp_path / "via-alternatives").symlink_to(tmp_path / "alternatives" / "jre_17")
    (tmp_path / "jvm").mkdir()
    (tmp_path / "jvm" / "jre-17").symlink_to("../alternatives/jre_17")  # relative, straight into them

    def look_alt(link):
        return subprocess.run(["/bin/sh", "-c", alt], env={"L": str(link), "PATH": os.environ["PATH"]},
                              stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
    # a link straight into /etc/alternatives (as /usr/bin/java, /usr/lib/jvm/jre-17): theirs, refused
    assert look_alt(tmp_path / "via-alternatives") == "a link of the alternatives (to %s)" % (
        tmp_path / "alternatives" / "jre_17")
    assert look_alt(tmp_path / "jvm" / "jre-17") == "a link of the alternatives (to %s)" % (
        tmp_path / "alternatives" / "jre_17")
    # a link of its own whose chain goes through them further on (/opt/java/current -> /usr/lib/jvm/jre-17): taken
    (tmp_path / "current").symlink_to(tmp_path / "jvm" / "jre-17")
    assert look_alt(tmp_path / "current") == "link to a Java"
    assert [kind(tmp_path / n) for n in ("to-java", "nowhere", "absent", "data", "file", "to-data")] == [
        "link to a Java", "link leading nowhere", "absent", "a directory", "a file", "link to %s" % other]
    check = _find(tasks, "The Java link is a link to a Java, or not there yet")

    def allowed(stdout):
        templar = Templar(loader=DataLoader(), variables={"cassandra_java_check_link": {"stdout": stdout},
                                                          "_cassandra_java_link": "/apps/jre/jdk", "inventory_hostname": "n1"})
        return (templar.template(trust_as_template("{{ %s }}" % check["ansible.builtin.assert"]["that"])),
                " ".join(templar.template(trust_as_template(check["ansible.builtin.assert"]["fail_msg"])).split()))
    assert all(allowed(k)[0] for k in ("absent", "link to a Java", "link leading nowhere"))
    ok, msg = allowed("link to /srv/data")
    assert ok is False and msg == (
        "/apps/jre/jdk on n1 is link to /srv/data: cassandra_java_link only makes a link of its own, or moves one that"
        " leads to a Java (a bin/java or a release file there), not one a package or the alternatives own. Nothing was"
        " changed. Set cassandra_java_link to a path of its own (e.g. /opt/java/current), or move that one away.")
    assert allowed("owned by the package java-17-openjdk-headless")[0] is False
    assert allowed("a link of the alternatives (to /etc/alternatives/jre_17)")[0] is False
    assert allowed("a directory")[0] is False
    # the link moved before the system java is compared (a /usr/bin/java through it then leads to the new Java)
    names = [t["name"] for t in _tasks("java_tarball.yml")]
    assert names.index("Point the Java link at this Java") < names.index("Resolve the system java and this one")


def test_a_password_with_an_at_sign_is_masked_whole():
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    stop = _find(_tasks("java_tarball.yml"), "Stop on the failed download")
    variables = {"cassandra_java_tarball": "https://svc:p@ss@mirror.example/java/jdk-17.tar.gz",
                 "cassandra_java_version": "17", "_status": "404", "_said": "Not Found"}
    msg = " ".join(Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(stop["ansible.builtin.fail"]["msg"])).split())
    assert "https://svc:****@mirror.example/java/jdk-17.tar.gz" in msg and "ss@" not in msg


def test_a_usr_lib_jvm_link_imported_then_left_as_it_is(tmp_path):
    # /usr/lib/jvm/java-17 -> /opt/jdk-17 (the alternatives through it): the import writes it as cassandra_java_link
    # (test_import_cluster_java_link), java_check takes it, and the role leaves the link as it is (site.yml no-op)
    jdk = tmp_path / "opt" / "jdk-17"
    (jdk / "bin").mkdir(parents=True)
    (jdk / "bin" / "java").write_text("#!/bin/sh\n")
    (jdk / "bin" / "java").chmod(0o755)
    link = tmp_path / "usr" / "lib" / "jvm" / "java-17"
    link.parent.mkdir(parents=True)
    link.symlink_to(jdk)
    rc, out = _link(tmp_path, jdk, link, check=True)
    assert rc == 0 and "changed=0" in out, out
    rc, out = _link(tmp_path, jdk, link)
    assert rc == 0 and "changed=0" in out and os.readlink(str(link)) == str(jdk), out
