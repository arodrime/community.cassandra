from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a node where cqlsh does not start (e.g. the Apache rpm on EL9, cqlshlib for python 3.6 only) is
# seen at the read (cqlsh --version, its last error line) and gets a TO DO: what makes it start.

import os
import subprocess

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
with open(PLAYBOOK, encoding="utf-8") as _f:
    PLAYS = yaml.safe_load(_f)


def _tasks(play):
    for t in play.get("tasks", []) + play.get("pre_tasks", []):
        yield t
        yield from t.get("block", []) + t.get("rescue", []) + t.get("always", [])


def _read_script():
    for play in PLAYS:
        for t in _tasks(play):
            script = t.get("ansible.builtin.shell")
            if isinstance(script, str) and "own_cqlsh=yes" in script:
                return script
    raise AssertionError("the read script")


def test_the_read_says_when_cqlsh_does_not_start(tmp_path):
    script = _read_script()
    start = script.index("# cqlsh starts")
    end = script.index("fi\n", start) + 3
    snippet = script[start:end].replace("/usr/local/bin/cqlsh", str(tmp_path / "none"))
    (tmp_path / "bin").mkdir()
    cqlsh = tmp_path / "bin" / "cqlsh"

    def run(body):
        cqlsh.write_text("#!/bin/sh\n" + body)
        cqlsh.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path / "bin", os.environ["PATH"]))
        return subprocess.run(["/bin/sh", "-c", snippet], env=env, stdout=subprocess.PIPE, universal_newlines=True,
                              check=True).stdout
    broken = ("echo 'Traceback (most recent call last):' >&2\necho '  File \"/usr/bin/cqlsh.py\"' >&2\n"
              "echo \"ModuleNotFoundError: No module named 'cqlshlib'\" >&2\necho >&2\nexit 1\n")
    assert run(broken) == "cqlsh_error=ModuleNotFoundError: No module named 'cqlshlib'\n"
    assert run("echo 'cqlsh 6.2.0'\n") == ""  # it starts: nothing said


def test_the_todo_names_the_nodes_and_the_way(tmp_path):
    play = next(p for p in PLAYS if p.get("name") == "Write the inventory")
    template = play["vars"]["_cqlsh_todo"]

    def todo(errors):
        hostvars = {h: {"import_cluster_cqlsh_error": e} for h, e in errors.items()}
        variables = {"_nodes": [{"name": h} for h in errors], "hostvars": hostvars}
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))
    lib = "ModuleNotFoundError: No module named 'cqlshlib'"
    assert todo({"n1": "", "n2": ""}) == []
    assert todo({"n1": lib, "n2": lib, "n3": ""}) == [
        "cqlsh does not start on n1, n2 (cqlsh --version: %s). cassandra_install links the cqlshlib of the package into"
        " the Python cqlsh runs with (RedHat): remove cassandra_cqlsh_python_manage: false from the host_vars of n1, n2"
        " (this import wrote it), then run create_cluster (it starts or restarts nothing on a running cluster)." % lib]
    # another error: said as it is, no fix guessed
    out = todo({"n1": "python3: not found"})
    assert out == ["cqlsh does not start on n1 (cqlsh --version: python3: not found). Check cqlsh there"
                   " (cassandra_cqlsh_command gives the full path of another one)."]
