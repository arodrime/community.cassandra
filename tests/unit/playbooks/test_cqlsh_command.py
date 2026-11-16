from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Every cqlsh the collection runs on a node takes cassandra_cqlsh_command: a cqlsh outside root's PATH (a
# virtualenv's bin/cqlsh) reaches every one of them, none left on the bare "cqlsh".

import glob
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
MODULE = "community.cassandra.cassandra_cqlsh"


def _calls(node):
    """Each cassandra_cqlsh call in a loaded YAML file, blocks and plays included."""
    if isinstance(node, dict):
        if MODULE in node:
            yield node[MODULE]
        for value in node.values():
            yield from _calls(value)
    elif isinstance(node, list):
        for item in node:
            yield from _calls(item)


def test_every_cqlsh_call_takes_the_command():
    files = glob.glob(os.path.join(ROOT, "playbooks", "*.yml")) + glob.glob(
        os.path.join(ROOT, "roles", "*", "tasks", "*.yml"))
    found = []
    for path in files:
        with open(path) as f:
            for call in _calls(yaml.safe_load(f)):
                found.append(os.path.relpath(path, ROOT))
                assert call.get("cqlsh_cmd") == "{{ cassandra_cqlsh_command | default(omit, true) }}", path
    assert len(found) >= 8, found  # (the checks over CQL: keyspaces, the login, the playbooks that read the schema)


def test_the_default_and_its_spec():
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "defaults", "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_cqlsh_command"] == "cqlsh"
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "meta", "argument_specs.yml")) as f:
        spec = yaml.safe_load(f)["argument_specs"]["main"]["options"]["cassandra_cqlsh_command"]
    assert spec["default"] == "cqlsh" and spec["type"] == "str"


def test_no_cqlsh_names_the_variable():
    # the import's TO DO when its login check finds no cqlsh: where to say where it is
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "cql_login_try.yml")) as f:
        outcome = next(t for t in yaml.safe_load(f) if t.get("name") == "Keep the outcome of the CQL login")
    why = outcome["ansible.builtin.set_fact"]["cassandra_cql_login"]["why"]
    # (what was observed only: where it was looked for, not a guess of where it is)
    assert "'no cqlsh in the PATH of root on ' ~ _cassandra_cql_login_from ~ ': cassandra_cqlsh_command gives the' ~ ' full" \
        " path of one'" in " ".join(why.split()) and "virtualenv" not in why


def _why(err="", msg="", rc=1, command=None):
    """The why of the login's outcome for that result of cqlsh, rendered as Ansible does."""
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:  # ansible-core 2.19+ renders trusted templates only
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "cql_login_try.yml")) as f:
        outcome = next(t for t in yaml.safe_load(f) if t.get("name") == "Keep the outcome of the CQL login")
    variables = dict((k, trust_as_template(v)) for k, v in outcome["vars"].items())
    variables.update(_cassandra_cql_try={"rc": rc, "err": err, "msg": msg}, _cassandra_cql_login_from="n1",
                     _cassandra_cql_login_host="10.0.0.1", _cassandra_cql_login_port=9142, _cassandra_cql_try_user="alice")
    if command is not None:
        variables["cassandra_cqlsh_command"] = command
    why = outcome["ansible.builtin.set_fact"]["cassandra_cql_login"]["why"]
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(why)).strip()


def test_not_found_only_when_cqlsh_itself_is_missing():
    no_path = "no cqlsh in the PATH of root on n1: cassandra_cqlsh_command gives the full path of one"
    assert _why(rc=127) == no_path
    assert _why(rc=2, msg="[Errno 2] No such file or directory: b'cqlsh'") == no_path
    assert _why(err="sudo: cqlsh: command not found") == no_path
    venv = "/opt/cqlsh/bin/cqlsh"
    assert _why(rc=2, msg="[Errno 2] No such file or directory: b'%s'" % venv, command=venv) == (
        "%s not found on n1 (cassandra_cqlsh_command): give the full path of a cqlsh there" % venv)
    # cqlsh ran (a virtualenv's, found) and missed another file: not "not found", its text not quoted
    see = "; to see its error, run as root on n1: %s 10.0.0.1 9142 -u alice" % venv
    secret = "Pa55w0rd"
    why = _why(err="Traceback ...\nFileNotFoundError: [Errno 2] No such file or directory: '/root/.cassandra/%s.pem'"
               % secret, command=venv)
    assert why == "cqlsh failed on n1: it could not open a file it needs as root (cqlshrc, certificate...)" + see
    assert secret not in why
    assert _why(err="ModuleNotFoundError: No module named 'cassandra'", command=venv) == (
        "cqlsh failed on n1: its Python could not load a module it needs" + see)
    assert _why(err="something else " + secret, command=venv) == "cqlsh failed on n1" + see
    reach = "cqlsh could not reach 10.0.0.1 port 9142 from n1"
    assert _why(err="Connection error: ('Unable to connect to any servers', ...)") == reach
    # (production: refused at the address, with a file named missing in the same text): the connection says why
    assert _why(rc=1, err="Connection error: ('Unable to connect to any servers', {'10.0.0.1:9142': ConnectionRefusedError("
                          "111, 'Tried connecting to [(10.0.0.1, 9142)]. Last error: Connection refused')}) [Errno 2] No such"
                          " file or directory: '/root/.cassandra/cqlshrc'") == reach
    assert _why(err="AuthenticationFailed('Bad credentials')") == ""
    assert _why(rc=0) == ""


def test_every_cqlsh_call_takes_the_node_s_port():
    # cqlsh's port is the node's native_transport_port, not always 9042: the one of the node it connects to
    files = glob.glob(os.path.join(ROOT, "playbooks", "*.yml")) + glob.glob(
        os.path.join(ROOT, "roles", "*", "tasks", "*.yml"))
    found = []
    for path in files:
        with open(path) as f:
            for call in _calls(yaml.safe_load(f)):
                found.append(os.path.relpath(path, ROOT))
                port = call.get("cqlsh_port", "")
                assert "cassandra_native_transport_port | default(9042)" in port or port == (
                    "{{ _cassandra_cql_login_port | default(9042) }}"), path
    assert len(found) >= 9, found
    with open(os.path.join(ROOT, "playbooks", "import_cluster.yml")) as f:
        step = next(t for play in yaml.safe_load(f) for t in play.get("tasks", [])
                    if t.get("name") == "Check the CQL login, or ask for one")
    assert step["vars"]["_cassandra_cql_login_port"] == "{{ _cql_vars.cassandra_native_transport_port | default(9042) }}"


def test_the_cql_login_runs_on_the_node():
    # The import checks the login from a connection: local play: a node with no ansible_connection of its own
    # would inherit it, and cqlsh would run on the controller (seen: "<cqlsh> not found on <node>").
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "cql_login_try.yml")) as f:
        task = next(t for t in yaml.safe_load(f) if MODULE in t)
    assert task["delegate_to"] == "{{ _cassandra_cql_login_from }}"
    assert task["vars"]["ansible_connection"] == (
        "{{ hostvars[_cassandra_cql_login_from].ansible_connection | default('ssh') }}")
