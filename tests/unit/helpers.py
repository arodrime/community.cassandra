# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What the unit tests share: render a template as a play does, run ansible-playbook on this checkout, fake a
command on the PATH, and build the nodes of a ring view.

Rendering:
  render_with(template, variables)       the template and each string variable trusted, as task vars are
  render(template, **variables)          the same, the variables as keywords
  render_as_given(template, variables)   only the template trusted: the variables as given
  trusted(value)                         every string inside value trusted (nested dicts and lists too)
  templar_for(variables)                 a Templar over variables
  task_runs(task, **variables)           whether the task's when conditions all hold over variables

Running a playbook:
  run_playbook(args, env, ...)           ansible-playbook <args> -> (rc, its output). A test that runs one is
                                         marked @pytest.mark.playbook (-m "not playbook" skips them all). With
                                         CASSANDRA_TEST_DUMP=<dir> set, each output is also written to
                                         <dir>/<test id>.txt, to compare the screens of two branches (diff -r;
                                         under plain pytest: ansible-test units does not pass the variable on).
  playbook_command(args, env, unset)     its argv and environment, for a test that starts it itself (on a pty...)

Golden files:
  golden(path, text)                     the text kept in the file path; with pytest --regen (or
                                         CASSANDRA_GOLDEN_WRITE=1), text written into it first
  golden_lines(path, lines)              the same, a list of lines
"""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re
import subprocess
import sys

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

# .../ansible_collections/community/cassandra: the checkout, and the directory above ansible_collections
TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))

# ansible-playbook as the tests start it: this Python, so this ansible-core
PLAYBOOK = [sys.executable, "-c", "from ansible.cli.playbook import main; main()"]
# the nodes reached without ssh, with this Python
LOCAL = ["-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]


def templar_for(variables):
    return Templar(loader=DataLoader(), variables=variables)


def trusted(value):
    """value with every string in it trusted as a template, nested dicts and lists too."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trusted(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trusted(v) for v in value]
    return value


def render_as_given(template, variables):
    """template rendered over variables as they are (a string variable is not trusted)."""
    return templar_for(variables).template(trust_as_template(template))


def render_with(template, variables):
    """template rendered over variables, each string variable trusted (as a play's vars are)."""
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return render_as_given(template, variables)


def render(template, **variables):
    """render_with, the variables given as keywords."""
    return render_with(template, variables)


def task_runs(task, **variables):
    """Whether task runs over variables: each of its when conditions (a string or a list) true, as Ansible
    evaluates them (each string variable trusted, as a play's vars are)."""
    when = task.get("when", [])
    return all(render_with("{{ %s }}" % condition, variables) is True for condition in
               ([when] if isinstance(when, str) else when))


def path_with(bin_dir):
    """PATH with bin_dir first: its commands found before the system's."""
    return "%s:%s" % (bin_dir, os.environ.get("PATH", ""))


def fake_command(bin_dir, name, script):
    """Writes the executable bin_dir/name holding script (bin_dir created if needed)."""
    bin_dir = str(bin_dir)
    if not os.path.isdir(bin_dir):
        os.makedirs(bin_dir)
    path = os.path.join(bin_dir, name)
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def fake_nodetool(bin_dir, script):
    """A nodetool that runs script: put bin_dir first on the PATH (path_with) for the run to use it."""
    return fake_command(bin_dir, "nodetool", script)


def ring_node(address, status="U", state="N", rack="r1", **more):
    """One node of a cluster_status view, as the cassandra_status module reads nodetool status."""
    return dict({"address": address, "status": status, "state": state, "rack": rack}, **more)


def view(host, *nodes):
    """What host sees: dc1 holding nodes."""
    return {"from": host, "result": {"cluster_status": {"dc1": {"nodes": list(nodes)}}}}


def golden(path, text):
    """The text kept in the golden file path (its content without the last newline). After a wanted change of
    the output, run the test with pytest --regen (or CASSANDRA_GOLDEN_WRITE=1 set): text is written into the file
    first, and git diff shows what changed."""
    if os.environ.get("CASSANDRA_GOLDEN_WRITE"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    with open(path, encoding="utf-8") as f:
        return f.read()[:-1]


def golden_lines(path, lines):
    """golden for a list of lines (one line of the file each)."""
    return golden(path, "\n".join(lines)).split("\n")


_dumped = {}


def playbook_command(args, env=None, unset=()):
    """-> argv, environment: ansible-playbook args as run_playbook starts it (for a test that starts it itself,
    on a terminal...). env: set over the environment of the test, with this checkout as the collection path, no
    colours, no localhost warning, no retry files. unset: variables removed from it (ANSIBLE_CONFIG...)."""
    if os.environ.get("CASSANDRA_TEST_PLAYBOOK_MARK") == "no":  # (conftest sets it for each test)
        raise AssertionError("a test that runs ansible-playbook is marked @pytest.mark.playbook")
    full = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
                ANSIBLE_RETRY_FILES_ENABLED="0")
    full.pop("CASSANDRA_TEST_PLAYBOOK_MARK", None)
    full.update(env or {})
    for name in unset:
        full.pop(name, None)
    return PLAYBOOK + [str(a) for a in args], full


def run_playbook(args, env=None, unset=(), cwd=None, timeout=300, stdin=None):
    """ansible-playbook args, run in cwd -> (rc, output: stdout and stderr together, decoded). env and unset: as
    for playbook_command. stdin: as for subprocess (subprocess.DEVNULL: no terminal to read from)."""
    argv, full = playbook_command(args, env, unset)
    proc = subprocess.run(argv, env=full, cwd=None if cwd is None else str(cwd), stdin=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False)
    out = proc.stdout.decode("utf-8", "replace")
    _dump(out)
    return proc.returncode, out


def _dump(out):
    where = os.environ.get("CASSANDRA_TEST_DUMP")
    if not where:
        return
    test = os.environ.get("PYTEST_CURRENT_TEST", "unknown").rsplit(" (", 1)[0]
    name = re.sub(r"[^A-Za-z0-9_.\[\]=,-]+", "_", test.replace("::", "__"))[-200:]
    _dumped[name] = _dumped.get(name, 0) + 1
    if _dumped[name] > 1:
        name += "_%d" % _dumped[name]
    if not os.path.isdir(where):
        os.makedirs(where)
    # the temporary dirs of pytest differ from run to run
    out = re.sub(r"/[^\s'\"]*/pytest-of-[^/]+/pytest-[0-9]+(/popen-gw[0-9]+)?", "<tmp>", out)
    with open(os.path.join(where, name + ".txt"), "w") as f:
        f.write(out)
