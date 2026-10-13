from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_cqlsh runs cqlsh with an argv list, no shell: each value is one
# argument as is, whatever it holds (a password with a space, a quote, $...).

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_cqlsh

NASTY = "it's \"quoted\" $HOME ~ \\ #1 ;& -x"


class Exit(Exception):
    pass


def run(monkeypatch, **params):
    """Runs main() with params; returns (the argv run, run_command's keywords, the exit kwargs)."""
    ran = []
    defaults = dict(cqlsh_host="127.0.0.1", cqlsh_port=9042, username=None, password=None, keyspace=None, file=None,
                    execute=None, encoding="utf-8", cqlshrc=None, cqlversion=None, protocol_version=None,
                    connect_timeout=5, request_timeout=10, tty=False, debug=True, ssl=False, no_compact=False,
                    cqlsh_cmd="cqlsh", transform="raw", split_char=" ", additional_args=None)
    defaults.update(params)

    class Module(object):
        def __init__(self, **kwargs):
            self.params = defaults

        def run_command(self, args, **kwargs):
            ran.append((args, kwargs))
            return 0, "out", ""

        def exit_json(self, **kwargs):
            raise Exit(kwargs)

        fail_json = exit_json

    monkeypatch.setattr(cassandra_cqlsh, "AnsibleModule", Module)
    with pytest.raises(Exit) as e:
        cassandra_cqlsh.main()
    return ran[0][0], ran[0][1], e.value.args[0]


def test_values_are_single_arguments_as_is(monkeypatch):
    execute = "SELECT * FROM t WHERE k = 'a' AND j = \"b\";"
    args, kwargs, result = run(monkeypatch, username="alice", password=NASTY, execute=execute,
                               additional_args={"no-color": True, "request-timeout": 31})
    assert isinstance(args, list)
    assert args[:3] == ["cqlsh", "127.0.0.1", "9042"]
    assert "--username=alice" in args and "--password=" + NASTY in args and "--execute=" + execute in args
    assert "--no-color" in args and "--debug" in args
    # additional arguments: two arguments each (the bin/cqlsh wrapper reads --python <path> only that way)
    assert args[args.index("--request-timeout"):][:2] == ["--request-timeout", "31"]
    assert kwargs.get("expand_user_and_vars") is False and not kwargs.get("use_unsafe_shell")
    # the debug output never shows the password
    assert NASTY not in result["cmd"] and "--password=********" in result["cmd"]


def test_a_password_starting_with_a_dash_is_not_an_option(monkeypatch):
    args = run(monkeypatch, username="alice", password="-p")[0]
    assert "--password=-p" in args


def test_the_cqlsh_command_may_hold_words(monkeypatch):
    args = run(monkeypatch, cqlsh_cmd="/usr/bin/python3 /opt/cassandra/bin/cqlsh.py")[0]
    assert args[:2] == ["/usr/bin/python3", "/opt/cassandra/bin/cqlsh.py"]
    monkeypatch.setenv("CASSANDRA_HOME", "/opt/c")
    assert run(monkeypatch, cqlsh_cmd="$CASSANDRA_HOME/bin/cqlsh")[0][0] == "/opt/c/bin/cqlsh"
    monkeypatch.setenv("HOME", "/home/c")
    assert run(monkeypatch, cqlsh_cmd="$HOME/bin/cqlsh")[0][0] == "/home/c/bin/cqlsh"


def test_the_wrapper_python_option(monkeypatch):
    args = run(monkeypatch, additional_args={"python": "/usr/bin/python3.11"})[0]
    assert args[-2:] == ["--python", "/usr/bin/python3.11"]
    # a password given that way is not shown either
    result = run(monkeypatch, additional_args={"password": NASTY})[2]
    assert NASTY not in result["cmd"]


def test_paths_are_paths(monkeypatch):
    # file and cqlshrc: type path (Ansible expands ~ and $VAR there), values elsewhere left as they are
    specs = []

    class Module(object):
        def __init__(self, argument_spec, **kwargs):
            specs.append(argument_spec)
            raise Exit()

    monkeypatch.setattr(cassandra_cqlsh, "AnsibleModule", Module)
    with pytest.raises(Exit):
        cassandra_cqlsh.main()
    assert specs[0]["file"]["type"] == "path" and specs[0]["cqlshrc"]["type"] == "path"
    assert specs[0]["password"]["type"] == "str" and specs[0]["execute"]["type"] == "str"
