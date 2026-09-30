from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config shows what it would change (secrets masked), refuses to change
# the identity of a joined node, and writes the remote JMX users.

import json
import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks")
HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file.\n"


def task(name):
    with open(os.path.join(TASKS, "main.yml"), encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def render(template, escape_backslashes=True, **variables):
    # escape_backslashes=False: a line of a .j2 file, where Jinja unescapes its string literals itself
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template),
                                                                      escape_backslashes=escape_backslashes)


SCRIPT = task("Diff them against the live files")["ansible.builtin.command"]["argv"][2]


def preview(tmp_path, name, live, new):
    """The preview's output for a live file (None: none yet) and the role's."""
    (tmp_path / "live").mkdir()
    (tmp_path / "new").mkdir()
    live_path, new_path = tmp_path / "live" / name, tmp_path / "new" / name
    if live is not None:
        live_path.write_bytes(live.encode())
    new_path.write_bytes(new.encode())
    return subprocess.run([sys.executable, "-c", SCRIPT, str(live_path), str(new_path)],
                          stdout=subprocess.PIPE, check=True).stdout.decode()


def changed_lines(diff):
    return [line for line in diff.split("\n") if line[:1] in ("+", "-") and line[:3] not in ("+++", "---")]


def test_same_file_no_diff(tmp_path):
    assert preview(tmp_path, "jvm-server.options", HEADER + "-Xss256k\n", HEADER + "-Xss256k\n") == ""


def test_new_file_shown_whole(tmp_path):
    assert changed_lines(preview(tmp_path, "jvm-server.options", None, HEADER + "-Xss256k\n")) == [
        "+" + HEADER.rstrip("\n"), "+-Xss256k"]


def test_crlf_is_a_change(tmp_path):
    # bash reads X=8G\r as "8G\r": the live file must show as different
    assert changed_lines(preview(tmp_path, "cassandra-env.sh", "X=8G\r\n", "X=8G\n")) == ["-X=8G\r", "+X=8G"]


@pytest.mark.parametrize("live, new, shown", [
    ("  keystore_password: old1\n", "  keystore_password: new2\n",
     ["-  keystore_password: ****", "+  keystore_password: ****"]),
    ('  truststore_password: "old1"\n', "  truststore_password: new2\n",
     ["-  truststore_password: ****", "+  truststore_password: ****"]),
    # quotes, a space or a # in the value: all of it masked
    ("  keystore_password: 'a b'\n", '  keystore_password: "c#d" # new\n',
     ["-  keystore_password: ****", "+  keystore_password: ****"]),
    ("keystore_password: x\r\n", "keystore_password: y\n", ["-keystore_password: ****\r", "+keystore_password: ****"]),
    ("tde_key_password=old1\n", "tde_key_password=new2\n", ["-tde_key_password=****", "+tde_key_password=****"]),
    ("secret: a\n", "secret: b\n", ["-secret: ****", "+secret: ****"]),
    ("num_tokens: 16\n", "num_tokens: 8\n", ["-num_tokens: 16", "+num_tokens: 8"]),
])
def test_secrets_masked_in_the_diff(tmp_path, live, new, shown):
    assert changed_lines(preview(tmp_path, "cassandra.yaml", live, new)) == shown


TEMPLATES = os.path.join(TASKS, "..", "templates")
with open(os.path.join(TEMPLATES, "jmxremote.access.j2"), encoding="utf-8") as f:
    ACCESS = f.read()
with open(os.path.join(TEMPLATES, "jmxremote.password.j2"), encoding="utf-8") as f:
    PASSWORD = f.read()


@pytest.mark.parametrize("create_unregister, access", [
    (True, "ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n    unregister\nmon readonly\n"),
    (False, "ops readwrite\nmon readonly\n"),
])
def test_jmx_access_rights(create_unregister, access):
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite", "create_unregister": create_unregister},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    assert render(ACCESS, cassandra_jmx_users=users) == access


IDENTITY = task("Compare the settings a joined node must keep")["ansible.builtin.command"]["argv"][2]


@pytest.mark.parametrize("live, new, changes", [
    ("cluster_name: \"it's\"\nnum_tokens: 16\n", "cluster_name: 'it''s'\nnum_tokens: 16\n", []),
    ("cluster_name: 'X' # prod\nnum_tokens: 16 # fixed\n", "cluster_name: 'X'\nnum_tokens: 16\n", []),
    ("cluster_name: 'X'\ninitial_token: 0\n", "cluster_name: 'X'\nnum_tokens: 1\ninitial_token: 0\n", []),
    ("cluster_name: \"say \\\"hi\\\" a\\\\b\"\n", "cluster_name: 'say \"hi\" a\\b'\n", []),
    ("cluster_name: 'X'\nnum_tokens: 16\n", "cluster_name: 'Y'\nnum_tokens: 256\n",
     ["cluster_name: X -> Y", "num_tokens: 16 -> 256"]),
])
def test_identity_compared_as_yaml_reads_it(tmp_path, live, new, changes):
    assert identity_changes(tmp_path, live, new) == changes


def identity_changes(tmp_path, live, new, live_rackdc="dc=d\n", new_rackdc="dc=d\n"):
    for name, text in (("live.yaml", live), ("new.yaml", new), ("live.p", live_rackdc), ("new.p", new_rackdc)):
        (tmp_path / name).write_text(text)
    out = subprocess.run([sys.executable, "-c", IDENTITY] + [str(tmp_path / n) for n in ("live.yaml", "new.yaml", "live.p", "new.p")],
                         stdout=subprocess.PIPE, universal_newlines=True, check=True)
    return json.loads(out.stdout)


def test_rack_comment_is_part_of_the_value(tmp_path):
    # properties have no inline comment: the snitch's rack is "r1 # old"
    assert identity_changes(tmp_path, "num_tokens: 16\n", "num_tokens: 16\n", "rack=r1 # old\n", "rack=r1\n") == [
        "rack: r1 # old -> r1"]


@pytest.mark.parametrize("live, changes", [
    # a comment after the value, other quotes: the same identity
    ("cluster_name: Prod Cluster   # prod\nnum_tokens: 16  # vnodes\n", []),
    ('cluster_name: "Prod Cluster"\nnum_tokens: 16\n', []),
    ("cluster_name: 'Prod Cluster'\nnum_tokens: 8\nnum_tokens: 16\n", []),  # set twice: the last one
    ("cluster_name: Other\nnum_tokens: 16\n", ["cluster_name: Other -> Prod Cluster"]),
])
def test_identity_of_a_joined_node_as_cassandra_reads_it(tmp_path, live, changes):
    (tmp_path / "live.yaml").write_text(live)
    (tmp_path / "new.yaml").write_text("cluster_name: 'Prod Cluster'\nnum_tokens: 16\n")
    for name in ("live.properties", "new.properties"):
        (tmp_path / name).write_text("dc=dc1\nrack=r1\n")
    out = subprocess.run([sys.executable, "-c", IDENTITY] + [str(tmp_path / n) for n in (
        "live.yaml", "new.yaml", "live.properties", "new.properties")], stdout=subprocess.PIPE, check=True).stdout
    assert json.loads(out) == changes


@pytest.mark.parametrize("live, new, changes", [
    # one step forward at a time, from what the live file says
    ("storage_compatibility_mode: CASSANDRA_4\n", "storage_compatibility_mode: UPGRADING\n", []),
    ("storage_compatibility_mode: UPGRADING\n", "storage_compatibility_mode: NONE\n", []),
    ("storage_compatibility_mode: NONE\n", "storage_compatibility_mode: NONE\n", []),
    ("storage_compatibility_mode: CASSANDRA_4\n", "storage_compatibility_mode: NONE\n",
     ["storage_compatibility_mode: CASSANDRA_4 -> NONE"]),
    ("storage_compatibility_mode: NONE\n", "storage_compatibility_mode: UPGRADING\n",
     ["storage_compatibility_mode: NONE -> UPGRADING"]),
    # absent: CASSANDRA_4 for 5.0, to be written as such first
    ("num_tokens: 16\n", "storage_compatibility_mode: CASSANDRA_4\n", []),
    ("num_tokens: 16\n", "storage_compatibility_mode: UPGRADING\n", ["storage_compatibility_mode: CASSANDRA_4 -> UPGRADING"]),
    ("num_tokens: 16\n", "storage_compatibility_mode: NONE\n", ["storage_compatibility_mode: CASSANDRA_4 -> NONE"]),
    ("num_tokens: 16\n", "num_tokens: 16\n", []),  # 4.x: no such key
])
def test_storage_compatibility_mode_steps(tmp_path, live, new, changes):
    num_tokens = "" if "num_tokens" in live else "num_tokens: 16\n"
    assert identity_changes(tmp_path, live + num_tokens, new + ("" if "num_tokens" in new else "num_tokens: 16\n")) == changes


with open(os.path.join(TASKS, "..", "vars", "main.yml"), encoding="utf-8") as f:
    ROLE_VARS = yaml.safe_load(f)
with open(os.path.join(TASKS, "..", "templates", "5.0", "cassandra.yaml.j2"), encoding="utf-8") as f:
    TDE_LINE = [line for line in f.read().split("\n") if line.strip().startswith("keystore_password: {{ (cassandra_tde")][0]


@pytest.mark.parametrize("password", [
    "cassandra", "abc #def", "a: b", "@x", "%x", "*x", "!x", "'q'", '"d"', "it's", "yes", "Off", "123", "", "a\\b", "p@ss/w=rd+1",
])
def test_passwords_read_back_as_written(password):
    rendered = render(TDE_LINE, escape_backslashes=False, cassandra_tde_keystore_password=password,
                      _cassandra_config_quote=ROLE_VARS["_cassandra_config_quote"])
    assert yaml.safe_load(rendered) == {"keystore_password": password}


def test_plain_password_stays_as_in_stock():
    assert render(TDE_LINE, escape_backslashes=False, cassandra_tde_keystore_password="cassandra",
                  _cassandra_config_quote=ROLE_VARS["_cassandra_config_quote"]).strip() == "keystore_password: cassandra"


def task_that(name):
    return task(name)["ansible.builtin.assert"]["that"]


@pytest.mark.parametrize("version, gcs, size, newsize, ok", [
    ("41x", ["CMS", "CMS"], "", "", True),
    ("41x", ["CMS", "CMS"], "8G", "800M", True),
    ("41x", ["CMS", "CMS"], "8G", "", False),
    ("41x", ["CMS", "CMS"], "", "800M", False),
    ("41x", ["G1", "custom"], "8G", "", False),  # custom is not G1 for cassandra-env.sh
    ("41x", ["G1", "G1"], "8G", "", True),
    ("50x", ["G1", "G1"], "8G", "", True),
    ("50x", ["CMS", "G1"], "8G", "", False),  # no HEAP_NEWSIZE in 5.0's cassandra-env.sh
    ("50x", ["G1", "custom"], "", "", True),
])
def test_heap_pairs(version, gcs, size, newsize, ok):
    that = "{{ %s }}" % task_that("Assert heap settings are consistent")
    assert render(that, cassandra_version=version, _cassandra_config_gcs=gcs, cassandra_heap_size=size,
                  cassandra_heap_newsize=newsize) is ok


@pytest.mark.parametrize("confirm, initialized, ask", [
    ("auto", True, True), ("auto", False, False), (True, False, True), ("true", False, True), ("yes", False, True),
    (False, True, False), ("no", True, False),
])
def test_confirm_values(confirm, initialized, ask):
    template = task("List the files to change and whether to ask first")["ansible.builtin.set_fact"]["_cassandra_config_ask"]
    assert render(template, cassandra_config_confirm=confirm, cassandra_config_initialized={"stat": {"exists": initialized}},
                  cassandra_config_preview={"results": [{"stdout": "diff"}]}) is ask


@pytest.mark.parametrize("confirm, valid", [("auto", True), (True, True), ("false", True), ("yes", True), ("always", False),
                                            ("1", False)])
def test_confirm_value_checked(confirm, valid):
    that = task_that("Assert required cassandra_config variables are set")[2]
    assert render("{{ %s }}" % that, cassandra_config_confirm=confirm) is valid


@pytest.mark.parametrize("installed, series, ok", [
    ("", "4.1", True), ("4.1.12", "4.1", True), ("4.1", "4.1", True), ("5.0.9", "4.1", False), ("4.10.1", "4.1", False),
    ("4.0.21", "4.1", False),
])
def test_installed_series(installed, series, ok):
    that = "{{ %s }}" % task_that("Refuse the templates of another series than the one installed")
    assert render(that, _installed=installed, _cassandra_config_series=series) is ok
