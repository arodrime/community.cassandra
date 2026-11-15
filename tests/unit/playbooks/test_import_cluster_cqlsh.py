from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a node where cqlsh does not start (e.g. the Apache rpm on EL9, cqlshlib for python 3.6 only) is
# seen at the read (cqlsh --version, its last error line) and gets a TO DO: what makes it start.

import os
import re
import shlex
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


def _cqlsh_snippet(tmp_path, command="cqlsh"):
    """The read's cqlsh lines, run as a test can: cassandra_cqlsh_command given (split by the filter the playbook
    uses), /usr/local/bin away."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_cqlsh_words
    script = _read_script()
    start = script.index("# cqlsh starts")
    end = script.index("# what cassandra_install sets up", start)
    snippet = script[start:end].replace("/usr/local/bin/cqlsh", str(tmp_path / "none"))
    snippet = re.sub(r"(?m)^cmd=\{\{.*\}\}$", lambda m: "cmd=%s" % shlex.quote(command), snippet)
    snippet = re.sub(r"(?m)^set -- \{\{.*\}\}$",
                     lambda m: "set -- " + " ".join(shlex.quote(w) for w in cassandra_cqlsh_words(command)), snippet)
    assert "{{" not in snippet
    return snippet


def _read(tmp_path, body, command="cqlsh"):
    """The read on a node whose cqlsh in root's PATH is body (None: none)."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    cqlsh = tmp_path / "bin" / "cqlsh"
    if body is None:
        if cqlsh.exists():
            cqlsh.unlink()
    else:
        cqlsh.write_text("#!/bin/sh\n" + body)
        cqlsh.chmod(0o755)
    env = dict(os.environ, PATH="%s:/usr/bin:/bin" % (tmp_path / "bin"))
    out = subprocess.run(["/bin/sh", "-c", _cqlsh_snippet(tmp_path, command)], env=env, stdout=subprocess.PIPE,
                         universal_newlines=True, check=True).stdout
    # (the cqlsh that ran: the self-check's, said apart)
    return "".join(line for line in out.splitlines(True) if not line.startswith("cqlsh_ran="))


def test_the_read_says_when_cqlsh_does_not_start(tmp_path):
    broken = ("echo 'Traceback (most recent call last):' >&2\necho '  File \"/usr/bin/cqlsh.py\"' >&2\n"
              "echo \"ModuleNotFoundError: No module named 'cqlshlib'\" >&2\necho >&2\nexit 1\n")
    assert _read(tmp_path, broken) == "cqlsh_error=cqlsh --version: ModuleNotFoundError: No module named 'cqlshlib'\n"
    assert _read(tmp_path, "echo 'cqlsh 6.2.0'\n") == ""  # it starts: nothing said


def test_every_other_failure_is_said(tmp_path):
    # (round-3/4/5) no silent case, each saying what ran: none found, a full path missing or not executable, no
    # output, no answer, an unclosed quote, a blank value; a newline in the output never splits the line
    assert _read(tmp_path, None) == "cqlsh_error=cqlsh: not found in the PATH of root\n"
    assert _read(tmp_path, None, command="/opt/venv/bin/cqlsh") == "cqlsh_error=/opt/venv/bin/cqlsh: no such file\n"
    (tmp_path / "plain").write_text("x")
    assert _read(tmp_path, None, command=str(tmp_path / "plain")) == "cqlsh_error=%s: not executable\n" % (
        tmp_path / "plain")
    assert _read(tmp_path, "exit 3\n") == "cqlsh_error=cqlsh --version: exits 3 with no output\n"
    assert _read(tmp_path, "exit 124\n") == "cqlsh_error=cqlsh --version: no answer in 30 s\n"
    assert _read(tmp_path, "echo x\n", command="/usr/bin/cqlsh 'unclosed") == (
        "cqlsh_error=cassandra_cqlsh_command (/usr/bin/cqlsh 'unclosed): an unclosed quote\n")
    assert _read(tmp_path, "echo x\n", command="   ") == "cqlsh_error=cassandra_cqlsh_command is blank\n"
    assert _read(tmp_path, None, command="'/opt/a\nb") == "cqlsh_error=cassandra_cqlsh_command ('/opt/a b): an unclosed quote\n"


def test_a_command_of_several_words_is_run_as_the_module_runs_it(tmp_path):
    # (owner) the cassandra_cqlsh module splits cassandra_cqlsh_command (shlex) and runs its first word with the rest
    # as arguments: env CQLSH_PYTHON=<python> <cqlsh> is valid, read the same way; $VAR expanded as the module does
    (tmp_path / "bin").mkdir()
    cqlsh = tmp_path / "cqlsh"
    cqlsh.write_text('#!/bin/sh\n[ "$CQLSH_PYTHON" = /x/bin/python ] && [ "$1" = --version ] && echo "cqlsh 6.2.0"'
                     ' && exit 0\necho "wrong python: $CQLSH_PYTHON" >&2\nexit 1\n')
    cqlsh.chmod(0o755)
    assert _read(tmp_path, None, command="env CQLSH_PYTHON=/x/bin/python %s" % cqlsh) == ""
    assert _read(tmp_path, None, command="env CQLSH_PYTHON=/y %s" % cqlsh) == (
        "cqlsh_error=env CQLSH_PYTHON=/y %s --version: wrong python: /y\n" % cqlsh)
    # $VAR: the node's, expanded (python3's os.path.expandvars), an unknown one left as it is
    os.environ["CQLSH_TEST_DIR"] = str(tmp_path)
    try:
        assert _read(tmp_path, None, command="env CQLSH_PYTHON=/x/bin/python $CQLSH_TEST_DIR/cqlsh") == ""
        assert _read(tmp_path, None, command="${CQLSH_TEST_NONE}/cqlsh") == \
            "cqlsh_error=${CQLSH_TEST_NONE}/cqlsh: no such file\n"
    finally:
        del os.environ["CQLSH_TEST_DIR"]


def test_the_words_are_the_module_s():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_cqlsh_words
    assert cassandra_cqlsh_words("env CQLSH_PYTHON=/x/bin/python /usr/bin/cqlsh") == [
        "env", "CQLSH_PYTHON=/x/bin/python", "/usr/bin/cqlsh"]
    assert cassandra_cqlsh_words("") == ["cqlsh"] and cassandra_cqlsh_words("'a b") == []
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "plugins", "modules", "cassandra_cqlsh.py"),
              encoding="utf-8") as f:
        assert "shlex.split(module.params['cqlsh_cmd'])" in f.read()


def test_the_cqlsh_the_inventory_names_is_the_one_read(tmp_path):
    # cassandra_cqlsh_command set by the operator (a virtualenv's): that one is started, no TO DO back
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "cqlsh").write_text("#!/bin/sh\necho 'cqlsh 6.2.0'\n")
    (venv / "cqlsh").chmod(0o755)
    assert _read(tmp_path, "exit 1\n", command=str(venv / "cqlsh")) == ""


def test_the_todo_names_the_nodes_and_the_way(tmp_path):
    play = next(p for p in PLAYS if p.get("name") == "Write the inventory")
    template = play["vars"]["_cqlsh_todo"]

    def todo(errors, check=False, kept=False):
        hostvars = dict((h, {"import_cluster_cqlsh_error": e}) for h, e in errors.items())
        variables = {"_nodes": [{"name": h} for h in errors], "hostvars": hostvars, "ansible_check_mode": check}
        if kept:  # (cqlsh kept as it is: -e import_keep_hand_edits=cqlsh, a string or a list)
            variables["import_keep_hand_edits"] = "logback.xml,cqlsh" if kept is True else kept
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))
    lib = "cqlsh --version: ModuleNotFoundError: No module named 'cqlshlib'"
    assert todo({"n1": "", "n2": ""}) == []
    steps = todo({"n2": lib, "n1": lib, "n3": "", "n4": "cqlsh --version: exits 3 with no output",
                  "n5": "cassandra_cqlsh_command ('x): an unclosed quote"}, kept=True)
    assert [s["title"] for s in steps] == ["Make cqlsh start on {nodes:broken}",
                                           "Fix cassandra_cqlsh_command for {nodes:quoting}"]
    step = steps[0]
    assert step["nodes"] == {"broken": ["n1", "n2", "n4"], "kept": ["n1", "n2", "n4"], "quoting": ["n5"],
                             "e1": ["n1", "n2"], "e2": ["n4"], "e3": ["n5"]}
    assert step["why"] == ["{nodes:e1}: " + lib, "{nodes:e2}: cqlsh --version: exits 3 with no output",
                           "the playbooks that read the schema run cqlsh (cassandra_cqlsh_command, as root)"]
    assert step["run"][1].startswith("no other cqlsh: import again without cqlsh in import_keep_hand_edits (this"
                                     " import keeps it as it is on {nodes:kept}: cassandra_cqlsh_python_manage false),"
                                     " then run {command:create}, then the same without --check --diff")
    assert step["commands"] == {"create": {"playbook": "create_cluster", "extra": ["--check", "--diff"]}}
    assert steps[1]["why"] == ["{nodes:e3}: cassandra_cqlsh_command ('x): an unclosed quote"]
    assert steps[1]["run"].startswith("fix its quoting or give it a value")
    # the option as a list: kept the same (this run's names alone keep a part)
    lib_errors = {"n1": lib, "n2": lib}
    assert todo(lib_errors, kept=["logback.xml", "cqlsh"])[0]["nodes"]["kept"] == ["n1", "n2"]
    # cqlsh's Python left to the role there: no host_vars to change
    assert todo({"n1": "cqlsh: not found in the PATH of root"})[0]["run"][1].startswith(
        "no other cqlsh: run {command:create}")
    # in the report: node lists in short, a row per error and per way out, the command as the next ones are run
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report
    nodes = [{"name": "n%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": True, "hand_edits": [],
              "normalized": [], "notes": [], "vars": {"cassandra_version": "50x"}} for i in range(1, 6)]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=200, in_git=False, todo=steps, inventory="inventories", hosts="c")
    text = "\n".join(lines)
    assert "1. Make cqlsh start on all - n3, n5 (3 nodes)" in text or "Make cqlsh start on n1, n2, n4 (3 nodes)" in text
    assert "n1, n2 (2 nodes): " + lib in text
    assert ("ansible-playbook -i inventories community.cassandra.create_cluster -e cassandra_hosts=c --check --diff"
            in text)
    assert "Fix cassandra_cqlsh_command for n5 (1 node)" in text


def test_the_cqlshlib_link_of_the_role_is_its_own(tmp_path):
    # after create_cluster linked cqlshlib (python3 in range: no wrapper), the next import leaves cqlsh to the role
    script = _read_script()
    start = script.index("# (RedHat: the link of the package cqlshlib")
    end = script.index('echo "own_cqlsh=yes"', start) + len('echo "own_cqlsh=yes"')
    lib, pkg = tmp_path / "site-packages", tmp_path / "py36"
    lib.mkdir()
    (pkg / "cqlshlib").mkdir(parents=True)
    snippet = script[start:end].replace("/usr/lib/python3.6/site-packages/cqlshlib", str(pkg / "cqlshlib")).replace(
        "python3 -c \"import sysconfig; print(sysconfig.get_paths()['purelib'])\"", "echo %s" % lib)

    def run():
        return subprocess.run(["/bin/sh", "-c", snippet], stdout=subprocess.PIPE, universal_newlines=True,
                              check=False).stdout  # (a test that fails exits 1: its output says)
    assert run() == ""
    (lib / "cqlshlib").symlink_to(pkg / "cqlshlib")
    assert run() == "own_cqlsh=yes\n"


def test_the_python_the_roles_wrapper_names_is_read(tmp_path):
    """The wrapper and profile.d file as cassandra_install writes them (cqlsh_links.yml): their Python read back."""
    links = os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_install", "tasks", "cqlsh_links.yml")
    with open(links, encoding="utf-8") as f:
        task = next(t for t in yaml.safe_load(f) if t.get("name") == "Point cqlsh at its Python")
    script = _read_script()
    start = script.index("for f in /usr/local/bin/cqlsh /etc/profile.d/cqlsh.sh; do")
    loop = script[start:script.index("done", start) + 4]
    paths = {}
    for item in task["loop"]:
        content = Templar(loader=DataLoader(), variables={"_cassandra_cqlsh_python": "/usr/bin/python3.11"}).template(
            trust_as_template(item["content"]))
        path = tmp_path / os.path.basename(item["dest"])
        path.write_text(content)
        path.chmod(int(str(item["mode"]), 8))
        paths[item["dest"]] = str(path)
    for dest, path in paths.items():
        loop = loop.replace(dest, path)
    out = subprocess.run(["/bin/sh", "-c", loop], stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
    lines = out.splitlines()
    assert [line for line in lines if "_sum=" not in line] == [
        "cqlsh_wrapper=role", "cqlsh_wrapper_py=/usr/bin/python3.11", "cqlsh_profile=role",
        "cqlsh_profile_py=/usr/bin/python3.11"]
    # their content and mode, as the self-check works them out from the role
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _cqlsh_pointers
    sums = dict(line.split("=", 1) for line in lines if "_sum=" in line)
    want = _cqlsh_pointers("/usr/bin/python3.11")
    assert sums == {"cqlsh_wrapper_sum": want["/usr/local/bin/cqlsh"], "cqlsh_profile_sum": want["/etc/profile.d/cqlsh.sh"]}
