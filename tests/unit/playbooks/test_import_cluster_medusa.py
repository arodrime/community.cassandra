from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: where the nodes' Medusa lives. The shell script is read from
# the playbook and run by sh on a fake root: its absolute paths are moved under
# it, and the cassandra user's home is <root>/var/lib/cassandra.

import os
import re
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
    SCRIPT = find_task(yaml.safe_load(f), "Look for Medusa")["ansible.builtin.shell"]


TRUSTED = """trusted() { case $(stat -c %U "$1" 2>/dev/null) in root|cassandra) ;; *) return 1 ;; esac; }"""


def run(root, cwd=None, untrusted=""):
    script = re.sub(r"(?<=[\s\"'(=:])/(etc|opt|home|root|usr|srv|var)/", r"$ROOT/\1/", SCRIPT)
    script = script.replace("getent passwd cassandra | cut -d: -f6", "echo $ROOT/var/lib/cassandra")
    assert "h=/root " in script
    script = script.replace("h=/root ", "h=$ROOT/root ")
    # owned by root or cassandra: here, all but $UNTRUSTED
    assert TRUSTED in script
    script = script.replace(TRUSTED, 'trusted() { [ "$1" != "$UNTRUSTED" ]; }')
    env = {"ROOT": str(root), "PATH": "/usr/bin:/bin", "UNTRUSTED": untrusted and str(root / untrusted)}
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
    assert "python" not in found and "profile" not in found


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


@pytest.mark.parametrize("profile, line", [
    ("var/lib/cassandra/.bash_profile", "source {venv}/bin/activate\n"),
    ("var/lib/cassandra/.bashrc", ". \"$HOME/venvs/medusa/bin/activate\"\n"),
    ("root/.profile", "source ~/venvs/medusa/bin/activate\n"),
    ("etc/profile.d/medusa.sh", "export PATH={venv}/bin:$PATH\n"),
])
def test_venv_activated_by_a_profile(tmp_path, profile, line):
    """Only in a login profile (not in the PATH, no link): found, and the file named."""
    home = tmp_path / profile.rsplit("/", 1)[0]
    path = home / "venvs/medusa" if "venvs" in line else tmp_path / "data/medusa-venv"
    venv(path)
    # a commented-out activation of a virtualenv that exists does not count
    venv(tmp_path / "data/old")
    write(tmp_path / profile, "# source %s/data/old/bin/activate\n" % tmp_path + line.format(venv=path))
    found = run(tmp_path)
    assert found["venv"] == str(path)
    assert found["profile"] == str(tmp_path / profile)
    assert "link_dir" not in found


def test_other_users_profiles_are_not_read(tmp_path):
    """Run as root: a virtualenv activated by any user's profile would run their code."""
    venv(tmp_path / "home/alice/v")
    write(tmp_path / "home/alice/.bashrc", "source %s/home/alice/v/bin/activate\n" % tmp_path)
    assert run(tmp_path) == {}


@pytest.mark.parametrize("untrusted", ["etc/profile.d/v.sh", "data/v", "data/v/bin/medusa"])
def test_untrusted_files_are_not_read(tmp_path, untrusted):
    """A profile or a virtualenv owned by neither root nor cassandra."""
    venv(tmp_path / "data/v")
    write(tmp_path / "etc/profile.d/v.sh", "source %s/data/v/bin/activate\n" % tmp_path)
    assert run(tmp_path)["venv"] == str(tmp_path / "data/v")
    assert run(tmp_path, untrusted=untrusted) == {}


def test_untrusted_medusa_is_not_run(tmp_path):
    """medusa in the PATH but owned by another user: its Python is not run as root."""
    venv(tmp_path / "srv/v")
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/medusa").symlink_to(tmp_path / "srv/v/bin/medusa")
    found = run(tmp_path, untrusted="srv/v/bin/medusa")
    assert found["untrusted"] == "yes"
    assert "version" not in found
    assert "version" in run(tmp_path)


def test_profile_d_of_the_role_is_not_a_user_profile(tmp_path):
    """The role's own file: its virtualenv found (no link needed), the file not reported."""
    path = tmp_path / "srv/medusa"
    venv(path)
    write(tmp_path / "etc/profile.d/cassandra-medusa.sh",
          'case ":$PATH:" in *":%s/bin:"*) ;; *) PATH="%s/bin:$PATH" ;; esac\n' % (path, path))
    found = run(tmp_path)
    assert found["profile_d"] == "yes"
    assert found["venv"] == str(path)
    assert "profile" not in found
