from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster and CQL authentication: the login given (inventory, -e) checked with a real login, not asked;
# none given, asked on the terminal (3 tries), each one checked; no terminal (or --check, or
# cassandra_operation_confirm false): a TO DO. No password in the output, under the ops and the default callback.

import fcntl
import os
import pty
import select
import subprocess
import sys
import termios
import time

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template
    from ansible.utils.unsafe_proxy import wrap_var as fact  # a fact (set_fact's): not templated again
else:
    def fact(value):  # 2.19+: data is never templated again
        return value

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_inventory_files, cassandra_inventory_layout, cassandra_inventory_layout_over)

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))
PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
GOOD, BAD = "S3cr3tGood", "Wr0ngPw"

# a cqlsh that takes alice / GOOD, refuses any other login as Cassandra's driver does
CQLSH = """#!/bin/sh
user=""; pw=""
while [ $# -gt 0 ]; do
  case "$1" in --username) user="$2"; shift;; --password) pw="$2"; shift;; esac; shift
done
if [ "$user" = alice ] && [ "$pw" = "%s" ]; then echo " release_version"; echo " 4.1.5"; exit 0; fi
echo "Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': AuthenticationFailed('Failed to\
 authenticate to 127.0.0.1:9042: Error from server: code=0100 [Bad credentials] message=\\"Provided username $user\
 and/or password are incorrect\\"')})" >&2
exit 2
""" % GOOD

LOGIN = """
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: cql_login.yml
      vars:
        _cassandra_cql_login_from: localhost
        _cassandra_cql_login_host: 127.0.0.1
        _cassandra_cql_login_given: {user: "{{ given_user | default('') }}", password: "{{ given_password | default('') }}"}
        _cassandra_cql_login_ask: "{{ ask | default(true) | bool }}"
    - ansible.builtin.debug:
        msg: >-
          STATE={{ cassandra_cql_login.state }} USER={{ cassandra_cql_login.user }} ASKED={{ cassandra_cql_login.asked }}
          WHY={{ cassandra_cql_login.why }} GOOD={{ cassandra_cql_login.password == '%s' }}
      vars:
        cassandra_output: true
""" % GOOD


def env_for(tmp_path, callback="community.cassandra.ops"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "cqlsh").write_text(CQLSH)
    (bin_dir / "cqlsh").chmod(0o755)
    (tmp_path / "login.yml").write_text(LOGIN)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK=callback, ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_PYTHON_INTERPRETER=sys.executable,
               PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")))
    env.pop("ANSIBLE_CONFIG", None)
    return env


def argv(*args):
    return [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "login.yml"] + list(args)


def run(tmp_path, *args, **kw):
    proc = subprocess.run(argv(*args), env=env_for(tmp_path, **kw), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, check=False, cwd=str(tmp_path), timeout=300)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def on_a_terminal(tmp_path, answers, timeout=240):
    """Runs login.yml on a terminal of its own, typing each answer once its prompt shows."""
    master, slave = pty.openpty()

    def own_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(argv(), stdin=slave, stdout=slave, stderr=slave, env=env_for(tmp_path), cwd=str(tmp_path),
                            close_fds=True, preexec_fn=own_terminal)  # pylint: disable=subprocess-popen-preexec-fn
    os.close(slave)
    out, seen, pending, end = b"", 0, list(answers), time.time() + timeout
    while time.time() < end:
        ready = select.select([master], [], [], 0.2)[0]
        if ready:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            out += data
        if pending and pending[0][0].encode() in out[seen:]:
            seen = len(out)
            time.sleep(0.3)  # pause drops the keys typed before its prompt
            os.write(master, pending.pop(0)[1].encode() + b"\r")
        if not ready and proc.poll() is not None:
            break
    proc.wait(timeout=30)
    os.close(master)
    return proc.returncode, out.decode("utf-8", "replace")


def state(output):
    line = next(line for line in output.splitlines() if "STATE=" in line)  # (a terminal's escapes before it)
    return " ".join(line[line.index("STATE="):].split())


@pytest.mark.parametrize("callback", ["community.cassandra.ops", "default"])
def test_given_login_checked_not_asked(tmp_path, callback):
    rc, output = run(tmp_path, "-e", "given_user=alice", "-e", "given_password=%s" % GOOD, callback=callback)
    assert rc == 0, output
    assert "STATE=ok USER=alice ASKED=False" in " ".join(output.split()) and "CQL user" not in output
    assert GOOD not in output.replace("GOOD=True", "")
    rc, output = run(tmp_path, "-e", "given_user=alice", "-e", "given_password=%s" % BAD, callback=callback)
    assert rc == 0, output
    assert "STATE=refused USER=alice ASKED=False WHY= GOOD=False" in " ".join(output.split())
    assert BAD not in output and "CQL user" not in output  # refused: said in TO DO, not asked


def test_no_terminal_and_not_asked(tmp_path):
    rc, output = run(tmp_path)
    assert rc == 0, output
    assert state(output).startswith("STATE=no_terminal USER= ASKED=True")
    rc, output = run(tmp_path, "-e", "ask=false")
    assert state(output).startswith("STATE=not_asked") and "CQL user" not in output


def test_asked_on_a_terminal_until_a_login_works(tmp_path):
    rc, output = on_a_terminal(tmp_path, [("CQL user", "alice"), ("Password of the CQL user alice", BAD),
                                          ("refused that login (1 of 3)", "alice"),
                                          ("Password of the CQL user alice", GOOD)])
    assert rc == 0, output
    assert state(output) == "STATE=ok USER=alice ASKED=True WHY= GOOD=True"
    assert GOOD not in output and BAD not in output  # typed without echo, never shown


def test_three_refused_logins_and_none_typed(tmp_path):
    answers = []
    for dummy in range(3):
        answers += [("CQL user", "alice"), ("Password of the CQL user alice", BAD)]
    rc, output = on_a_terminal(tmp_path, answers)
    assert rc == 0, output
    assert state(output).startswith("STATE=refused USER=alice ASKED=True")
    assert output.count("Password of the CQL user alice") == 3 and BAD not in output
    rc, output = on_a_terminal(tmp_path, [("CQL user", "")])  # Enter alone: none, set later
    assert state(output).startswith("STATE=none USER= ASKED=True")


# --- the import's side: when it asks, what it writes, what it says ---

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
WRITE = next(play for play in PLAYS if play["name"] == "Write the inventory")


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def node(name, read=True, **variables):
    return {"name": name, "host": name, "address": "10.0.0.%s" % name[-1], "read": read, "vars": variables}


def test_asked_only_with_password_authentication():
    on = WRITE["vars"]["_cql_on"]
    assert render(on, _nodes=[node("n1", cassandra_authenticator="PasswordAuthenticator"), node("n2")]) is True
    assert render(on, _nodes=[node("n1", cassandra_authenticator="org.apache.cassandra.auth.PasswordAuthenticator")])
    assert render(on, _nodes=[node("n1", cassandra_authenticator="AllowAllAuthenticator")]) is False
    assert render(on, _nodes=[node("n1")]) is False and render(on) is False
    assert render(on, _nodes=[node("n1", False, cassandra_authenticator="PasswordAuthenticator")]) is False  # not read


def test_what_is_left_to_do():
    base = dict(_cql_on=True, _layout={"cluster_group": "prod"}, _cql_from="n1", ansible_check_mode=False,
                _cql_own={"user": "", "password": ""})

    def todo(login, **changes):
        return render(WRITE["vars"]["_cql_todo"], **dict(base, cassandra_cql_login=login, **changes))

    assert todo({"state": "ok"}) == [] and todo({"state": "ok"}, _cql_on=False) == []
    assert todo({"state": "refused", "asked": False, "user": "alice"}) == [
        "CQL login refused by n1 (cassandra_cql_username alice): fix cassandra_cql_username and"
        " cassandra_cql_password where you set them"]
    assert "3 logins refused by n1, none written" in todo({"state": "refused", "asked": True, "user": "alice"})[0]
    assert "where the import keeps them (group_vars/prod/)" in todo(
        {"state": "refused", "asked": False, "user": "alice", "password": GOOD},
        _cql_own={"user": "alice", "password": GOOD})[0]
    unset = todo({"state": "no_terminal"})[0]
    assert "import again on a terminal, it asks for it" in unset and "group_vars/prod/" in unset
    assert "none typed: import again to type one" in todo({"state": "none"})[0]
    assert "not asked (cassandra_operation_confirm false)" in todo({"state": "not_asked"})[0]
    assert "the run without --check asks for it" in todo({"state": "not_asked"}, ansible_check_mode=True)[0]
    assert "not checked (no cqlsh on n1), written as typed" in todo(
        {"state": "unchecked", "asked": True, "user": "a", "why": "no cqlsh on n1"})[0]
    # the report gets them
    assert WRITE["vars"]["_report_args"]["todo"] == "{{ _cql_todo }}" and "todo=_report_args.todo" in WRITE["vars"]["_report"]


def test_cql_address_of_the_node():
    step = next(t for t in WRITE["tasks"] if t.get("name") == "Check the CQL login, or ask for one")
    assert step["ansible.builtin.include_role"]["tasks_from"] == "cql_login.yml"
    v = step["vars"]
    hostvars = {"n1": {"ansible_facts": {"hostname": "node1", "fqdn": "node1.example.com"}}}

    def host(**variables):
        nodes = [node("n1", **variables)]
        values = dict(_nodes=fact(nodes), hostvars=hostvars)
        for k in ("_cql_node", "_cql_vars", "_cql_facts", "_cql_rpc"):
            values[k] = render(v[k], **values)
        return render(v["_cassandra_cql_login_host"], **values)

    assert host() == "127.0.0.1"  # rpc_address localhost, the default
    assert host(cassandra_rpc_address="0.0.0.0") == "127.0.0.1"
    assert host(cassandra_rpc_address="10.9.9.9") == "10.9.9.9"
    assert host(cassandra_rpc_address="{{ ansible_facts['hostname'] }}") == "node1"
    assert host(cassandra_rpc_address="{{ ansible_facts['default_ipv4']['address'] }}") == "10.0.0.1"


def test_logs_in_as_the_operations_do():
    # no quoting, port or TLS of its own: a login that works here works for keyspaces.yml's cqlsh too
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_service", "tasks", "cql_login_try.yml")) as f:
        mine = yaml.safe_load(f)[0]["community.cassandra.cassandra_cqlsh"]
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_service", "tasks", "keyspaces.yml")) as f:
        theirs = yaml.safe_load(f)[0]["community.cassandra.cassandra_cqlsh"]
    assert sorted(set(mine) - {"execute"}) == sorted(set(theirs) - {"execute", "transform"})


def test_which_login_is_written():
    # typed and accepted, else the one the import wrote before (kept, even refused: never lost); yours: not written
    write = next(t for t in WRITE["tasks"] if t.get("name") == "Write the CQL login with the cluster's variables")
    own = {"user": "alice", "password": GOOD}

    def written(login, own_login=None):
        """The login written, None when none."""
        variables = dict(cassandra_cql_login=login, _cql_own=own if own_login is None else own_login)
        variables["_typed"] = render(write["vars"]["_typed"], **variables)
        if not render("{{ (%s) | bool }}" % write["when"][1], **variables):
            return None
        return dict((k, render(v, **variables)) for k, v in write["vars"]["_login"].items())

    bob = {"cassandra_cql_username": "bob", "cassandra_cql_password": "x"}
    alice = {"cassandra_cql_username": "alice", "cassandra_cql_password": GOOD}
    assert written({"asked": True, "state": "ok", "user": "bob", "password": "x"}) == bob
    assert written({"asked": True, "state": "unchecked", "user": "bob", "password": "x"}) == bob
    assert written({"asked": True, "state": "refused", "user": "bob", "password": "x"}) == alice  # own kept
    assert written({"asked": True, "state": "refused", "user": "bob", "password": "x"}, {"user": "", "password": ""}) is None
    assert written({"asked": False, "state": "ok", "user": "alice", "password": GOOD}) == alice
    assert written({"asked": False, "state": "refused", "user": "alice", "password": GOOD}) == alice
    assert written({"asked": False, "state": "ok", "user": "carol", "password": "yours"}) == alice  # own kept, said
    assert written({"asked": False, "state": "ok", "user": "carol", "password": "yours"}, {"user": "", "password": ""}) is None
    carol = {"state": "ok", "asked": False, "user": "carol", "password": "yours"}
    assert "is not the one the import keeps in group_vars/prod/ (kept)" in render(
        WRITE["vars"]["_cql_todo"], _cql_on=True, _layout={"cluster_group": "prod"}, _cql_from="n1",
        ansible_check_mode=False, _cql_own=own, cassandra_cql_login=carol)[0]
    # an earlier login read back without its password (a file that did not load): not written back as ''
    assert written(carol, {"user": "alice", "password": ""}) is None
    step = next(t for t in WRITE["tasks"] if t.get("name") == "Check the CQL login, or ask for one")
    given = step["vars"]["_cassandra_cql_login_given"]
    assert render(given, _cql_set={"user": "", "password": ""}, _cql_own=own) == own  # -i node, re-import
    mine = {"user": "carol", "password": "y"}
    assert render(given, _cql_set=mine, _cql_own=own) == mine


def test_the_import_reads_back_its_own_login():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_own_values)
    keys = ["cassandra_cql_username", "cassandra_cql_password"]
    read = {"group_vars/prod/main.yml": GENERATED % "prod" + "\ncassandra_cql_username: alice\n",
            "group_vars/prod/secrets.yml": GENERATED % "prod" + "\ncassandra_cql_password: %s\n" % GOOD,
            "group_vars/prod/vault.yml": "cassandra_cql_password: yours\n"}
    assert cassandra_inventory_own_values(read, "prod", keys) == {"cassandra_cql_username": "alice",
                                                                  "cassandra_cql_password": GOOD}
    # another cluster's, or a file of yours at that path: not the import's
    assert cassandra_inventory_own_values(read, "other", keys) == {}
    read["group_vars/prod/secrets.yml"] = "cassandra_cql_password: yours\n"
    assert cassandra_inventory_own_values(read, "prod", keys) == {"cassandra_cql_username": "alice"}


def test_login_written_vaulted_unless_yours():
    login = {"cassandra_cql_username": "alice", "cassandra_cql_password": GOOD}
    nodes = [dict(node("n%d" % i, cassandra_cluster_name="Prod"), dc="dc1", rack="r1") for i in (1, 2)]
    for n in nodes:
        n.update(hand_edits=[], normalized=[], notes=[])
        n["vars"].update(login)
    files = cassandra_inventory_files(cassandra_inventory_layout(nodes, "Prod"))
    by_path = {f["path"]: f for f in files}
    secrets = yaml.safe_load(by_path["group_vars/prod/secrets.yml"]["content"])
    assert secrets == {"cassandra_cql_password": GOOD} and by_path["group_vars/prod/secrets.yml"]["secret"]
    assert GOOD not in by_path["group_vars/prod/main.yml"]["content"]
    assert "cassandra_cql_username: alice" in by_path["group_vars/prod/main.yml"]["content"]
    # the same login in a file of yours (group_vars/prod/vault.yml): not written again
    yours = [{"path": "group_vars/prod/vault.yml",
              "content": "cassandra_cql_username: alice\ncassandra_cql_password: %s\n" % GOOD}]
    files = cassandra_inventory_files(cassandra_inventory_layout_over(nodes, "Prod", yours))
    assert not [f for f in files if "cassandra_cql" in f["content"]]
