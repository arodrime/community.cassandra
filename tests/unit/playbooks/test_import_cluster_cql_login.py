from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster and CQL authentication: the login given (inventory, -e) checked with a real login, not asked;
# none given, asked on the terminal (3 tries), each one checked; no terminal (or --check, or
# cassandra_operation_confirm false): a TO DO. No password in the output, under the ops and the default callback.

import fcntl
import json
import os
import pty
import select
import subprocess
import sys
import termios
import time

import pytest
import yaml

from ansible import template as ansible_template

if hasattr(ansible_template, "trust_as_template"):  # ansible-core 2.19+
    def fact(value):  # 2.19+: data is never templated again
        return value
else:
    from ansible.utils.unsafe_proxy import wrap_var as fact  # a fact (set_fact's): not templated again

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_inventory_files, cassandra_inventory_layout, cassandra_inventory_layout_over)
from ansible_collections.community.cassandra.tests.unit.helpers import (
    path_with, playbook_command, render_as_given, run_playbook)
from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
GOOD, BAD = "S3cr3tGood", "Wr0ngPw"

# a cqlsh that takes alice and the password in its file (GOOD), refuses any other login as Cassandra's driver
# does; it parses its arguments as cqlsh does (--password=value or --password value), no shell in between
CQLSH = """#!%s
import os, sys
args, opts = sys.argv[1:], {}
while args:
    arg = args.pop(0)
    if arg.startswith("--") and "=" in arg:
        opts[arg[:arg.index("=")]] = arg[arg.index("=") + 1:]
    elif arg in ("--username", "--password"):
        opts[arg] = args.pop(0)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "password"), encoding="utf-8") as f:
    good = f.read()
if opts.get("--username") == "alice" and opts.get("--password") == good:
    print(" release_version")
    print(" 4.1.5")
    sys.exit(0)
sys.stderr.write("Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': AuthenticationFailed('Failed to"
                 " authenticate to 127.0.0.1:9042: Error from server: code=0100 [Bad credentials] message=\\"Provided"
                 " username %%s and/or password are incorrect\\"')})\\n" %% opts.get("--username"))
sys.exit(2)
""" % sys.executable

LOGIN = """
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: community.cassandra.cassandra_operation
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


def env_for(tmp_path, callback="community.cassandra.ops", password=GOOD):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "password").write_text(password, encoding="utf-8")
    (bin_dir / "cqlsh").write_text(CQLSH)
    (bin_dir / "cqlsh").chmod(0o755)
    (tmp_path / "login.yml").write_text(LOGIN)
    return dict(ANSIBLE_STDOUT_CALLBACK=callback, ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_DEPRECATION_WARNINGS="0",
                ANSIBLE_PYTHON_INTERPRETER=sys.executable, PATH=path_with(bin_dir))


def run(tmp_path, *args, **kw):  # kw: env_for's
    return run_playbook(["login.yml"] + list(args), env_for(tmp_path, **kw), ["ANSIBLE_CONFIG"], cwd=tmp_path,
                        stdin=subprocess.DEVNULL)


def on_a_terminal(tmp_path, answers, timeout=240, password=GOOD):
    """Runs login.yml on a terminal of its own, typing each answer once its prompt shows."""
    master, slave = pty.openpty()

    def own_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    argv, env = playbook_command(["login.yml"], env_for(tmp_path, password=password), ["ANSIBLE_CONFIG"])
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(tmp_path),
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


@pytest.mark.playbook
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


@pytest.mark.playbook
def test_no_terminal_and_not_asked(tmp_path):
    rc, output = run(tmp_path)
    assert rc == 0, output
    assert state(output).startswith("STATE=no_terminal USER= ASKED=True")
    rc, output = run(tmp_path, "-e", "ask=false")
    assert state(output).startswith("STATE=not_asked") and "CQL user" not in output


@pytest.mark.playbook
def test_asked_on_a_terminal_until_a_login_works(tmp_path):
    rc, output = on_a_terminal(tmp_path, [("CQL user", "alice"), ("Password of the CQL user alice", BAD),
                                          ("refused that login (1 of 3)", "alice"),
                                          ("Password of the CQL user alice", GOOD)])
    assert rc == 0, output
    assert state(output) == "STATE=ok USER=alice ASKED=True WHY= GOOD=True"
    assert GOOD not in output and BAD not in output  # typed without echo, never shown


@pytest.mark.playbook
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


# a password with what a shell or shlex would split, unquote or expand: the login works as is
NASTY = ["two words", "it's", 'say "hi"', "$HOME$(id)`id`", "back\\slash\\", "#not a comment", "a;b&&c|d",
         "-starts-with-a-dash", " spaces around ", "pässwörd-€", "%(x)s %% {x}"]


@pytest.mark.playbook
@pytest.mark.parametrize("password", NASTY)
def test_given_login_with_any_character(tmp_path, password):
    # -e as JSON: the value as is, whatever it holds
    rc, output = run(tmp_path, "-e", json.dumps({"given_user": "alice", "given_password": password}), password=password)
    assert rc == 0, output
    assert state(output).startswith("STATE=ok USER=alice ASKED=False"), output
    assert password.strip() not in output


@pytest.mark.playbook
def test_typed_login_with_any_character(tmp_path):
    password = "it's \"quoted\" $x \\ #1 ;é"
    rc, output = on_a_terminal(tmp_path, [("CQL user", "alice"), ("Password of the CQL user alice", password)],
                               password=password)
    assert rc == 0, output
    assert state(output).startswith("STATE=ok USER=alice ASKED=True"), output
    assert password not in output


# --- the import's side: when it asks, what it writes, what it says ---

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
WRITE = find_play(PLAYS, "Write the inventory")


def render(template, **variables):
    return render_as_given(template, variables)


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
    assert "import again on a terminal, it asks for it" in unset and "group_vars/prod/ (this import wrote it)" in unset
    assert "none typed: import again to type one" in todo({"state": "none"})[0]
    assert "not asked (cassandra_operation_confirm false)" in todo({"state": "not_asked"}, cassandra_operation_confirm=False)[0]
    # not asked: the self-check failed (import_strict), the inventory is not the one to keep: asked at the one that is
    assert todo({"state": "not_asked"}) == [
        "CQL login: asked at the import that passes the self-check (authentication is on), then written to the"
        " inventory (password in secrets.yml)"]
    # --check writes nothing: no group_vars to set the login in yet (a first import), never sent there
    assert todo({"state": "not_asked"}, ansible_check_mode=True) == [
        "CQL login: asked at the real import (authentication is on), then written to the inventory (password in"
        " secrets.yml)"]
    assert "not checked (no cqlsh on n1), written as typed" in todo(
        {"state": "unchecked", "asked": True, "user": "a", "why": "no cqlsh on n1"})[0]
    # the report gets them
    assert WRITE["vars"]["_report_args"]["todo"] == "{{ _cql_todo + _cqlsh_todo }}" and "todo=_report_args.todo" in WRITE["vars"]["_report"]


def test_cql_address_of_the_node():
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()  # cassandra_cql_host, under a plain pytest run too
    step = find_task(WRITE, "Check the CQL login, or ask for one")
    assert step["ansible.builtin.include_role"]["tasks_from"] == "cql_login.yml"
    v = step["vars"]
    hostvars = {"n1": {"ansible_facts": {"hostname": "node1", "fqdn": "node1.example.com"}}}

    def host(**variables):
        nodes = [node("n1", **variables)]
        values = dict(_nodes=fact(nodes), hostvars=hostvars)
        for k in ("_cql_node", "_cql_vars", "_cql_facts"):
            values[k] = render(v[k], **values)
        return render(v["_cassandra_cql_login_host"], **values)

    assert host() == "127.0.0.1"  # rpc_address localhost, the default
    assert host(cassandra_rpc_address="0.0.0.0") == "127.0.0.1"
    assert host(cassandra_rpc_address="10.9.9.9") == "10.9.9.9"
    assert host(cassandra_rpc_address="{{ ansible_facts['hostname'] }}") == "node1"
    assert host(cassandra_rpc_address="{{ ansible_facts['default_ipv4']['address'] }}") == "10.0.0.1"
    # blank: Cassandra binds what the node's name resolves to, not loopback (production: refused on 127.0.0.1)
    assert host(cassandra_rpc_address="") == "node1.example.com"
    assert host(cassandra_rpc_address="::") == "127.0.0.1"


def test_logs_in_on_the_node_the_operator_gave():
    # -i h-n1, and the question said "It is checked on h-n2" (the first node read, in ring order)
    step = find_task(WRITE, "Check the CQL login, or ask for one")
    cql_node = step["vars"]["_cql_node"]
    nodes = fact([node("n2"), node("n3"), node("n1"), node("n4", read=False)])
    assert render(cql_node, _nodes=nodes, _given=["n1"])["host"] == "n1"
    assert render(cql_node, _nodes=nodes, _given=["n4", "n3"])["host"] == "n3"  # (n4 given, not read)
    assert render(cql_node, _nodes=nodes, _given=["n4"])["host"] == "n2"  # none given read: the first read
    assert render(cql_node, _nodes=nodes)["host"] == "n2"
    assert render(cql_node, _nodes=fact([node("n1", read=False)]), _given=["n1"]) == {}


def test_logs_in_as_the_operations_do():
    # no quoting, port or TLS of its own: a login that works here works for keyspaces.yml's cqlsh too
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_operation", "tasks", "cql_login_try.yml")) as f:
        mine = yaml.safe_load(f)[0]["community.cassandra.cassandra_cqlsh"]
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_operation", "tasks", "keyspaces.yml")) as f:
        theirs = yaml.safe_load(f)[0]["community.cassandra.cassandra_cqlsh"]
    assert sorted(set(mine) - {"execute"}) == sorted(set(theirs) - {"execute", "transform"})


def test_which_login_is_written():
    # typed and accepted, else the one the import wrote before (kept, even refused: never lost); yours: not written
    write = task("Write the CQL login with the cluster's variables")
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
    step = find_task(WRITE, "Check the CQL login, or ask for one")
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


# As import_cluster checks it: from its connection: local play, on a node named by a fact of localhost (_nodes).
# The delegated task's connection is templated in that node's variables, where localhost's facts are undefined
# (seen: the login task failed, "'_nodes' is undefined", hidden by no_log).
FROM_A_NODE = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_become: false
  tasks:
    - ansible.builtin.set_fact:
        _nodes: [{host: node1}]
    - ansible.builtin.include_role:
        name: community.cassandra.cassandra_operation
        tasks_from: cql_login.yml
      vars:
        _cql_node: "{{ _nodes | first }}"
        _cassandra_cql_login_from: "{{ _cql_node.host }}"
        _cassandra_cql_login_host: 127.0.0.1
        _cassandra_cql_login_given: {user: alice, password: "%s"}
        _cassandra_cql_login_ask: false
    - ansible.builtin.debug:
        msg: "STATE={{ cassandra_cql_login.state }}"
""" % GOOD


@pytest.mark.playbook
@pytest.mark.parametrize("node, expected", [
    # a node with a connection of its own: the login runs there
    ("node1 ansible_connection=local", "STATE=ok"),
    # none of its own: over ssh (Ansible's default), not the play's local; nothing listens on port 1
    ("node1 ansible_host=127.0.0.1 ansible_port=1 ansible_ssh_common_args='-o ConnectTimeout=5'", "UNREACHABLE"),
])
def test_the_login_runs_on_the_node_the_import_names(tmp_path, node, expected):
    env = env_for(tmp_path, callback="default")
    (tmp_path / "from_a_node.yml").write_text(FROM_A_NODE)
    (tmp_path / "hosts").write_text(node + "\n")
    dummy, output = run_playbook(["-i", "hosts", "from_a_node.yml"], env, ["ANSIBLE_CONFIG"], cwd=tmp_path,
                                 stdin=subprocess.DEVNULL)
    assert expected in output, output
    assert "is undefined" not in output and GOOD not in output, output


# --- asked once, when it is written ---

FILES = find_task(WRITE, "Write the files")


def task(name):
    return find_task(WRITE, name)


def test_asked_after_the_self_check_before_the_files_are_sorted_out():
    # nothing before needs the login (the read runs cqlsh --version, without one): asked at the write, after the
    # self-check; the login in the layout before the stops that look at the files (a secrets.yml in clear over a
    # vaulted one), before anything is written
    order = [t.get("name") for t in WRITE["tasks"]]
    assert order.index("Self-check") < order.index("Check the CQL login, or ask for one") < order.index("Write the files")
    inside = [t.get("name") for t in FILES["block"]]
    assert inside[:3] == ["Write the CQL login with the cluster's variables", "Lay out again with the CQL login",
                          "Sort out the files an earlier import wrote"]
    for name in ("Stop rather than write over another cluster's files", "Stop rather than write in clear over a vaulted file",
                 "Write the hosts file", "Write group_vars and host_vars"):
        assert name in inside
    assert inside[-2:] == ["Write group_vars and host_vars", "Note the CQL login is written"]
    # the read plays never log in over CQL
    with open(PLAYBOOK, encoding="utf-8") as f:
        text = f.read()
    head = text[:text.index("- name: Write the inventory")]
    assert "cql_login" not in head and "cassandra_cql_password" not in head


@pytest.mark.parametrize("check, confirm, ok, strict, asked", [
    (False, True, True, True, True),     # the self-check passed: the inventory is written, asked
    (False, True, False, False, True),   # failed, import_strict false: written all the same (to inspect), asked
    (False, True, False, True, False),   # failed, import_strict: the run stops, not asked
    (True, True, True, True, False),     # --check: never asked
    (False, False, True, True, False),   # cassandra_operation_confirm false: never asked
])
def test_asked_only_when_the_run_goes_on_to_write(check, confirm, ok, strict, asked):
    step = task("Check the CQL login, or ask for one")
    variables = dict(ansible_check_mode=check, cassandra_operation_confirm=confirm, _self_check_ok=ok,
                     import_strict=strict)
    variables["_import_completes"] = render(WRITE["vars"]["_import_completes"], **variables)
    assert render("{{ (%s) | bool }}" % step["vars"]["_cassandra_cql_login_ask"].strip()[2:-2], **variables) is asked


def test_the_login_typed_is_laid_out_again():
    # the self-check ran before it: the files about to be written (secrets.yml) come from a layout with it
    relayout = task("Lay out again with the CQL login")
    assert relayout["ansible.builtin.set_fact"]["_layout"] == "{{ _layout_over }}"
    assert relayout["when"] == "import_cluster_cql_login_set is not skipped"
    write = task("Write the CQL login with the cluster's variables")
    assert write["register"] == "import_cluster_cql_login_set" and write["no_log"] is True
    nodes = [dict(node("n%d" % i, cassandra_cluster_name="Prod"), dc="dc1", rack="r1") for i in (1, 2)]
    for n in nodes:
        n.update(hand_edits=[], normalized=[], notes=[])
    layout = cassandra_inventory_layout(nodes, "Prod")
    assert "group_vars/prod/secrets.yml" not in [f["path"] for f in cassandra_inventory_files(layout)]
    for n in nodes:
        n["vars"].update(cassandra_cql_username="alice", cassandra_cql_password=GOOD)
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    values = dict(_nodes=fact(nodes), _layout=fact(layout), _dir="/opt/x/inventories",
                  import_cluster_existing_vars={"results": []})
    values["_theirs"] = render(WRITE["vars"]["_theirs"], **values)
    again = render(WRITE["vars"]["_layout_over"], **values)
    files = dict((f["path"], f) for f in cassandra_inventory_files(again))
    assert yaml.safe_load(files["group_vars/prod/secrets.yml"]["content"]) == {"cassandra_cql_password": GOOD}


# The import's own tasks, run by ansible-playbook: a login typed (or the import's own), then a stop before the
# inventory is written (another cluster's files there), or the inventory written. In a block with the import's
# always, as the import has them.
def run_the_write(tmp_path, stop, callback="community.cassandra.ops", state="ok", asked=True):
    setup = {"name": "What the import found", "ansible.builtin.set_fact": {
        "_nodes": [node("n1", cassandra_authenticator="PasswordAuthenticator")],
        "_cql_own": {"user": "" if asked else "alice", "password": "" if asked else GOOD},
        "cassandra_cql_login": {"state": state, "user": "alice", "password": GOOD, "asked": asked, "why": ""},
        "_leftovers": {"conflicts": ["group_vars/prod/main.yml: another cluster's"] if stop else []},
        "_dir": "/opt/x/inventories"}}
    block = dict(FILES, block=[task("Write the CQL login with the cluster's variables"),
                               task("Stop rather than write over another cluster's files"),
                               task("Note the CQL login is written")])
    play = {"name": "Write the inventory", "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {"_cql_on": True}, "tasks": [setup, block]}
    (tmp_path / "stop.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    rc, output = run_playbook(["stop.yml"], env_for(tmp_path, callback=callback), ["ANSIBLE_CONFIG"], cwd=tmp_path,
                              stdin=subprocess.DEVNULL)
    return rc, " ".join(output.split())


@pytest.mark.playbook
@pytest.mark.parametrize("callback", ["community.cassandra.ops", "default"])
@pytest.mark.parametrize("state", ["ok", "unchecked"])
def test_a_stop_after_the_login_typed_says_it_was_not_saved(tmp_path, callback, state):
    rc, output = run_the_write(tmp_path, stop=True, callback=callback, state=state)
    assert rc != 0, output
    assert "another cluster's" in output  # the stop's own message
    said = "typed and checked" if state == "ok" else "typed (not checked)"
    assert "CQL login alice: %s, NOT saved: the import stopped before it wrote the inventory." % said in output, output
    assert "The next import asks for it again, when it writes the inventory." in output
    for line in ("To give it without typing the password, in a vaulted file: ansible-vault create cql_login.yml",
                 "cassandra_cql_username: alice", "cassandra_cql_password: <its password>",
                 "Then add to the playbooks, the import too: -e @cql_login.yml --ask-vault-pass"):
        assert line in output, line
    assert GOOD not in output, output  # never the password
    assert output.index("another cluster's") < output.index("NOT saved")


@pytest.mark.playbook
def test_no_note_once_the_login_is_written(tmp_path):
    rc, output = run_the_write(tmp_path, stop=False)
    assert rc == 0, output
    assert "NOT saved" not in output and GOOD not in output, output


@pytest.mark.playbook
def test_no_note_without_a_login_typed(tmp_path):
    # the import's own login (or one given): nothing typed to lose, the stop alone
    rc, output = run_the_write(tmp_path, stop=True, asked=False)
    assert rc != 0 and "another cluster's" in output, output
    assert "NOT saved" not in output and GOOD not in output, output
