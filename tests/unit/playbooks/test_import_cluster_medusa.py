from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: where the nodes' Medusa lives. The shell script is read from
# the playbook and run by sh on a fake root: its absolute paths are moved under
# it, and what a login shell of a user answers is <root>/login/<user><bash options>.

import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")


def find_task(node, name):
    if isinstance(node, dict):
        if node.get("name") == name:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = find_task(item, name)
            if found is not None:
                return found
    return None


with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
SCRIPT = find_task(PLAYS, "Look for Medusa")["ansible.builtin.shell"]


TRUSTED = """trusted() { case $(stat -c %U "$1" 2>/dev/null) in root|cassandra) ;; *) return 1 ;; esac; }"""


def function(name):
    return re.compile(r"^ *%s\(\) \{\n.*?^ *\}\n" % name, re.M | re.S)


AS_USER = function("as_user")
AS_CASSANDRA = function("as_cassandra")


def run(root, cwd=None, untrusted="", hint=""):
    # bash run as that user: here, its answer from a file (the users asked are logged)
    assert len(AS_USER.findall(SCRIPT)) == 1 and "setsid" in AS_USER.search(SCRIPT).group(0)
    script = AS_USER.sub('as_user() { echo "$1$2" >> "$ROOT/asked"; cat "$ROOT/login/$1$2" 2>/dev/null; }\n', SCRIPT)
    # the version read as cassandra: here, as the test's user
    assert len(AS_CASSANDRA.findall(script)) == 1 and "runuser" in AS_CASSANDRA.search(script).group(0)
    script = AS_CASSANDRA.sub('as_cassandra() { "$@"; }\n', script)
    script = re.sub(r"(?<=[\s\"'(=:])/(etc|opt|home|root|usr|srv|var)/", r"$ROOT/\1/", script)
    # owned by root or cassandra: here, all but $UNTRUSTED
    assert TRUSTED in script
    script = script.replace(TRUSTED, 'trusted() { [ "$1" != "$UNTRUSTED" ]; }')
    env = {"ROOT": str(root), "PATH": "/usr/bin:/bin", "UNTRUSTED": untrusted and str(root / untrusted),
           "IMPORT_CLUSTER_MEDUSA_PATH": hint and str(root / hint)}
    out = subprocess.run(["sh", "-c", script], env=env, cwd=str(cwd or root), capture_output=True, text=True,
                         check=True)
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def venv(path):
    """A virtualenv with medusa, its python the test's one."""
    write(path / "pyvenv.cfg", "home = /usr/bin\n")
    write(path / "bin" / "medusa", "#!%s/bin/python\nimport medusa\n" % path, 0o755)
    (path / "bin" / "python").symlink_to(sys.executable)


def test_venv_of_its_own_linked(tmp_path):
    venv(tmp_path / "srv/tools/medusa-venv")
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/medusa").symlink_to(tmp_path / "srv/tools/medusa-venv/bin/medusa")
    found = run(tmp_path)
    assert found["venv"] == str(tmp_path / "srv/tools/medusa-venv")
    assert found["link_dir"] == str(tmp_path / "usr/local/bin")
    assert "python" not in found and "login" not in found


def test_venv_from_the_shebang(tmp_path):
    """A script outside the virtualenv, run by its python."""
    venv(tmp_path / "srv/medusa-venv")
    write(tmp_path / "usr/local/bin/medusa", "#!%s/srv/medusa-venv/bin/python\n" % tmp_path, 0o755)
    found = run(tmp_path)
    assert found["venv"] == str(tmp_path / "srv/medusa-venv")
    assert "link_dir" not in found


def test_no_venv(tmp_path):
    """pip into a system Python: its Python is reported, no virtualenv."""
    write(tmp_path / "usr/local/bin/medusa", "#!/usr/bin/python3.11\nimport medusa\n", 0o755)
    found = run(tmp_path)
    assert found["bin"] == str(tmp_path / "usr/local/bin/medusa")
    assert found["python"] == "/usr/bin/python3.11"
    assert "venv" not in found


def test_env_shebang_does_not_depend_on_the_directory(tmp_path):
    write(tmp_path / "usr/local/bin/medusa", "#!/usr/bin/env python3\n", 0o755)
    write(tmp_path / "work/pyvenv.cfg", "")
    found = run(tmp_path, cwd=tmp_path / "work")
    assert found["python"] == "python3"
    assert "venv" not in found


def test_no_medusa(tmp_path):
    assert run(tmp_path) == {}


def login(root, user, options, answer):
    """What bash run as that user with those options answers to command -v medusa."""
    write(root / "login" / (user + options), answer)


def asked(root):
    path = root / "asked"
    return path.read_text().split() if path.exists() else []


@pytest.mark.parametrize("user, options", [("cassandra", "-lic"), ("cassandra", "-ic"), ("root", "-lic"),
                                           ("root", "-ic")])
def test_venv_in_the_path_of_a_login_shell(tmp_path, user, options):
    """Only in the PATH of a login shell (a profile activates it, no link): found, and who it is for."""
    venv(tmp_path / "data/medusa-venv")
    # a profile may print something before
    login(tmp_path, user, options, "Welcome\n%s/data/medusa-venv/bin/medusa\n" % tmp_path)
    found = run(tmp_path)
    assert found["venv"] == str(tmp_path / "data/medusa-venv")
    assert found["login"] == user
    assert "link_dir" not in found
    assert "version" in found


def test_login_shells_only_when_not_found_otherwise(tmp_path):
    """medusa in the PATH: no user's profile is run."""
    venv(tmp_path / "srv/v")
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/medusa").symlink_to(tmp_path / "srv/v/bin/medusa")
    found = run(tmp_path)
    assert found["venv"] == str(tmp_path / "srv/v") and "login" not in found
    assert asked(tmp_path) == []


def test_only_cassandra_and_root_login_shells(tmp_path):
    """Nothing found: the login shells of cassandra and root only, then the usual places."""
    assert run(tmp_path) == {}
    assert asked(tmp_path) == ["cassandra-lic", "cassandra-ic", "root-lic", "root-ic"]


@pytest.mark.parametrize("answer", ["medusa: alias for medusa-wrapper\n", "{root}/data/none/bin/medusa\n", ""])
def test_login_shell_answer_not_an_executable(tmp_path, answer):
    """An alias, a path that is not there, nothing: not taken."""
    login(tmp_path, "cassandra", "-lic", answer.format(root=tmp_path))
    assert run(tmp_path) == {}


@pytest.mark.parametrize("place", ["opt/cassandra-medusa", "opt/medusa", "usr/share/cassandra-medusa", "opt/tools"])
def test_usual_places_before_login_shells(tmp_path, place):
    """In a usual place: no profile is run."""
    venv(tmp_path / place)
    login(tmp_path, "cassandra", "-lic", "%s/data/v/bin/medusa\n" % tmp_path)
    found = run(tmp_path)
    assert found["venv"] == str(tmp_path / place) and "login" not in found
    assert asked(tmp_path) == []


def test_login_shell_answer_read_from_a_file():
    """Not from a pipe: a process a profile left running in a session of its own would hold it open."""
    assert re.search(r'as_user "\$u" "\$o" >"\$out"\n', SCRIPT)
    assert "as_user \"$u\" \"$o\" |" not in SCRIPT


def test_any_venv_under_opt(tmp_path):
    venv(tmp_path / "opt/backup-tools")
    assert run(tmp_path)["venv"] == str(tmp_path / "opt/backup-tools")


@pytest.mark.parametrize("hint", ["data/v", "data/v/bin", "data/v/bin/medusa"])
def test_path_given(tmp_path, hint):
    """import_cluster_medusa_path: the virtualenv or its medusa, before anything else."""
    venv(tmp_path / "data/v")
    venv(tmp_path / "opt/cassandra-medusa")
    found = run(tmp_path, hint=hint)
    assert found["venv"] == str(tmp_path / "data/v")
    assert asked(tmp_path) == []


def test_path_given_without_medusa(tmp_path):
    venv(tmp_path / "opt/cassandra-medusa")
    found = run(tmp_path, hint="data/none")
    assert found["hint_missing"] == str(tmp_path / "data/none")
    assert found["venv"] == str(tmp_path / "opt/cassandra-medusa")


def test_path_given_with_a_newline(tmp_path):
    """The name is reported on one line: it cannot add a line of its own."""
    found = run(tmp_path, hint="data/none\nversion=9")
    assert found["hint_missing"] == str(tmp_path / "data/none version=9")
    assert "version" not in found


def as_user_script(home):
    """The real as_user, run by the test's user (runuser left out), HOME given."""
    body = AS_USER.search(SCRIPT).group(0)
    body = body.replace('r=$(PATH=$PATH:/usr/sbin:/sbin command -v runuser) || return 0', 'r=')
    body = body.replace('"$r" -u "$1" -- ', '')
    assert "setsid timeout" in body
    body = body.replace('HOME="$(getent passwd "$1" | cut -d: -f6)"', 'HOME="%s"' % home)
    assert "runuser" not in body and "getent" not in body
    return body + 'as_user "$(id -un)" -lic | tail -n 1; as_user "$(id -un)" -ic | tail -n 1\n'


@pytest.mark.skipif(not shutil.which("script") or not shutil.which("setsid") or not os.path.exists("/bin/bash"),
                    reason="needs script, setsid and bash")
def test_login_shell_with_a_terminal(tmp_path):
    """Under a terminal (ssh -tt): the interactive bash must not stop on it; ~/.bash_profile and a ~/.bashrc
    that returns when not interactive are both read."""
    venv(tmp_path / "v")
    write(tmp_path / ".bash_profile", "echo hello\nPATH=%s/v/bin:$PATH\n" % tmp_path)
    write(tmp_path / ".bashrc", "case $- in *i*) ;; *) return ;; esac\nPATH=%s/v/bin:$PATH\n" % tmp_path)
    write(tmp_path / "t.sh", as_user_script(tmp_path))
    out = subprocess.run(["timeout", "25", "script", "-qec", "sh %s/t.sh" % tmp_path, "/dev/null"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    lines = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    assert lines[-2:] == [str(tmp_path / "v/bin/medusa")] * 2


def test_untrusted_venv_of_a_login_shell_is_not_run(tmp_path):
    """Found by a login shell, but owned by neither root nor cassandra: not run."""
    venv(tmp_path / "data/v")
    login(tmp_path, "cassandra", "-lic", "%s/data/v/bin/medusa\n" % tmp_path)
    found = run(tmp_path, untrusted="data/v/bin/medusa")
    assert found["untrusted"] == "yes" and "version" not in found


def test_version_not_read(tmp_path):
    """Its Python answers nothing (e.g. cassandra cannot read the virtualenv): said so."""
    venv(tmp_path / "opt/cassandra-medusa")
    (tmp_path / "opt/cassandra-medusa/bin/python").unlink()
    write(tmp_path / "opt/cassandra-medusa/bin/python", "#!/bin/sh\nexit 1\n", 0o755)
    found = run(tmp_path)
    assert found["version"] == "" and found["version_unread"] == "yes"


def as_cassandra_script(owners, cassandra_user):
    """The real as_cassandra without runuser, getent and stat answering for the test."""
    body = AS_CASSANDRA.search(SCRIPT).group(0)
    body = body.replace("r=$(PATH=$PATH:/usr/sbin:/sbin command -v runuser)", "r=")
    assert "runuser)" not in body
    return ('getent() { %s; }\nstat() { printf "%%s\\n" %s; }\nreal=/x py=/y\nreadlink() { echo "$2"; }\n'
            % ("true" if cassandra_user else "false", " ".join(owners)) + body
            + 'as_cassandra echo ran\n')


@pytest.mark.parametrize("owners, cassandra_user, ran", [
    (["root", "root"], False, True),
    (["root", "cassandra"], False, False),
    (["cassandra", "cassandra"], False, False),
    (["root", "root"], True, False),  # no runuser here: nothing run as root while there is a cassandra user
])
def test_version_as_root_only_when_root_owns_both(owners, cassandra_user, ran):
    out = subprocess.run(["sh", "-c", as_cassandra_script(owners, cassandra_user)], capture_output=True,
                         text=True)
    assert (out.stdout.strip() == "ran") == ran


def test_untrusted_medusa_is_not_run(tmp_path):
    """medusa in the PATH but owned by another user: its Python is not run."""
    venv(tmp_path / "srv/v")
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/medusa").symlink_to(tmp_path / "srv/v/bin/medusa")
    found = run(tmp_path, untrusted="srv/v/bin/medusa")
    assert found["untrusted"] == "yes"
    assert "version" not in found
    assert "version" in run(tmp_path)


def test_profile_d_of_the_role_is_not_a_user_profile(tmp_path):
    """The role's own file puts it in the login PATH: found (no link needed), not reported as a user's."""
    path = tmp_path / "srv/medusa"
    venv(path)
    write(tmp_path / "etc/profile.d/cassandra-medusa.sh",
          'case ":$PATH:" in *":%s/bin:"*) ;; *) PATH="%s/bin:$PATH" ;; esac\n' % (path, path))
    login(tmp_path, "cassandra", "-lic", "%s/bin/medusa\n" % path)
    found = run(tmp_path)
    assert found["profile_d"] == "yes"
    assert found["venv"] == str(path)
    assert "login" not in found


NOTE = find_task(PLAYS, "Note a Medusa without medusa.ini, or an import_cluster_medusa_path without Medusa")


@pytest.mark.parametrize("found, notes", [
    (["bin=/opt/m/bin/medusa", "version=0.30.1"], ["Medusa 0.30.1 in /opt/m/bin/medusa, but no /etc/medusa/medusa.ini: NOT imported"]),
    (["hint_missing=/srv/x"], ["Medusa: no medusa in /srv/x (import_cluster_medusa_path) on this node"]),
    (["hint_missing=/srv/x", "bin=/opt/m/bin/medusa"],
     ["Medusa ? in /opt/m/bin/medusa, but no /etc/medusa/medusa.ini: NOT imported",
      "Medusa: no medusa in /srv/x (import_cluster_medusa_path) on this node"]),
    (["ini=yes", "hint_missing=/srv/x"], None),
    ([], None),
])
def test_note_without_medusa_ini(found, notes):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    variables = {"import_cluster_medusa_found": {"stdout_lines": found}}
    variables.update((k, trust_as_template(v)) for k, v in NOTE["vars"].items())
    templar = Templar(loader=DataLoader(), variables=variables)
    when = all(templar.template(trust_as_template("{{ %s }}" % c)) for c in NOTE["when"])
    assert when == (notes is not None)
    if when:
        fact = NOTE["ansible.builtin.set_fact"]["import_cluster_medusa"]
        assert templar.template(trust_as_template(fact["notes"])) == notes
