from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_cqlsh runs cqlsh with an argv list, no shell: each value is one
# argument as is, whatever it holds (a password with a space, a quote, $...).

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_cqlsh

NASTY = "it's \"quoted\" $HOME ~ \\ #1 ;& -x é"


class Exit(Exception):
    pass


def run(monkeypatch, found=None, answer=(0, "out", ""), **params):
    """Runs main() with params; returns (the argv run, run_command's keywords, the exit kwargs). found: what
    get_bin_path finds ({name: path}; none by default) and the dirs it was asked to look in first (found["dirs"])."""
    ran = []
    found = {} if found is None else found
    defaults = dict(cqlsh_host="127.0.0.1", cqlsh_port=9042, username=None, password=None, keyspace=None, file=None,
                    execute=None, encoding="utf-8", cqlshrc=None, cqlversion=None, protocol_version=None,
                    connect_timeout=5, request_timeout=10, tty=False, debug=True, ssl=False, no_compact=False,
                    cqlsh_cmd="cqlsh", transform="raw", split_char=" ", additional_args=None)
    defaults.update(params)

    class Module(object):
        def __init__(self, **kwargs):
            self.params = defaults

        def get_bin_path(self, name, required=False, opt_dirs=None):
            found["dirs"] = opt_dirs
            return found.get(name)

        def run_command(self, args, **kwargs):
            ran.append((args, kwargs))
            return answer

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


def test_the_wrapper_python_option(monkeypatch):
    args = run(monkeypatch, additional_args={"python": "/usr/bin/python3.11"})[0]
    assert args[-2:] == ["--python", "/usr/bin/python3.11"]
    # a password given that way is not shown either
    result = run(monkeypatch, additional_args={"password": NASTY})[2]
    assert NASTY not in result["cmd"]


def test_paths_are_paths():
    # file and cqlshrc: type path (Ansible expands ~ and $VAR there), values elsewhere left as they are
    import inspect
    source = inspect.getsource(cassandra_cqlsh.main)
    assert "file=dict(type='path')" in source and "cqlshrc=dict(type='path')" in source


def test_a_bare_cqlsh_looked_for_in_usr_local_bin_first(monkeypatch):
    # root's PATH under sudo may leave /usr/local/bin out, where cassandra_install's wrapper is
    found = {"cqlsh": "/usr/local/bin/cqlsh"}
    assert run(monkeypatch, found=found)[0][0] == "/usr/local/bin/cqlsh" and found["dirs"] == ["/usr/local/bin"]
    assert run(monkeypatch)[0][0] == "cqlsh"  # (none found: as given, its error said by cqlsh's run)
    # a command given: as it is
    found = {"cqlsh": "/usr/local/bin/cqlsh"}
    assert run(monkeypatch, found=found, cqlsh_cmd="/opt/venv/bin/cqlsh")[0][0] == "/opt/venv/bin/cqlsh"
    assert "dirs" not in found


def test_a_failure_says_why_without_debug(monkeypatch):
    # cqlsh failing (no cqlshlib for this python): its rc and standard error kept, debug off
    err = "Traceback ...\nModuleNotFoundError: No module named 'cqlshlib'\n"
    args, kwargs, result = run(monkeypatch, answer=(1, "", err), debug=False, username="alice", password=NASTY)
    assert result["msg"] == "module execution failed" and result["rc"] == 1 and result["err"] == err
    assert "cmd" not in result and "out" not in result
    # never the password, even if cqlsh printed it
    args, kwargs, result = run(monkeypatch, answer=(2, "", "Bad credentials %s for alice" % NASTY), debug=False,
                               password=NASTY)
    assert NASTY not in str(result) and result["err"] == "Bad credentials ******** for alice"
    # a success: as before, no err without debug
    args, kwargs, result = run(monkeypatch, debug=False)
    assert "err" not in result and "rc" not in result
