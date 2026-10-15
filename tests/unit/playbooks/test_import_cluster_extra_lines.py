from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the lines a node has at the end of cassandra-env.sh (and of logback.xml's <configuration>)
# carried as cassandra_env_extra_lines (cassandra_logback_extra_lines), so that the self-check passes without
# keeping the file as it is; a secret in such a line (or in a JVM option) moved to secrets.yml in a variable of
# its own, the line referring to it, its value never written in clear; your own template for a file
# (cassandra_config_template_overrides) rendered by the self-check.

import os
import re

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_config_import, cassandra_inventory_files, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import cassandra_import_self_check
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    cassandra_import_self_check_advice)
from ansible_collections.community.cassandra.tests.unit.import_nodes import node
from ansible_collections.community.cassandra.tests.unit.playbooks.test_import_cluster_keep_files import (
    FACTS, role_permissions, stock)

SECRETS = ["s3cr3t-A", "tsP4ss", "jvmP4ss"]
ROLE_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "templates", "5.0")


def imported(files, name="n1", address="10.0.0.1"):
    out = cassandra_config_import(files, "50x", dict(FACTS, hostname=name, default_ipv4={"address": address}), "",
                                  "/var/lib/cassandra")
    return out, {"name": name, "address": address, "hostname": name, "dc": "dc1", "rack": "r1", "read": True,
                 "vars": dict(out["vars"], cassandra_version="50x"), "hand_edits": out["hand_edits"],
                 "normalized": out["normalized"], "comments": out["comments"], "notes": [], "trusted": out["trusted"],
                 "keep": {"cassandra_service_unit_manage": False}}


def self_check(files, layout, name="n1", extra_files=(), inventory_dir=""):
    return cassandra_import_self_check(cassandra_inventory_files(layout) + list(extra_files), layout["hosts"], name,
                                       dict(FACTS, hostname=name), files, {}, "/var/lib/cassandra", "17", "",
                                       role_permissions(files), inventory_dir=inventory_dir)


def with_env(*lines):
    files = stock()
    files["cassandra-env.sh"] += "".join(line + "\n" for line in lines)
    return files


def test_lines_at_the_end_of_cassandra_env_carried():
    files = with_env("export MY_AGENT_OPTS=-Dx=1", 'JVM_OPTS="$JVM_OPTS -javaagent:/opt/agent.jar"')
    out, n = imported(files)
    assert out["vars"]["cassandra_env_extra_lines"] == ["export MY_AGENT_OPTS=-Dx=1",
                                                        'JVM_OPTS="$JVM_OPTS -javaagent:/opt/agent.jar"']
    assert out["hand_edits"] == []
    assert "cassandra-env.sh: 2 line(s) at its end kept with cassandra_env_extra_lines" in out["carried"]
    assert "hand edits carried into the inventory: cassandra-env.sh: 2 line(s) at its end kept with" \
           " cassandra_env_extra_lines" in node({"import_cluster_config": out})["notes"]
    layout = cassandra_inventory_layout([n], "c")
    assert self_check(files, layout)["differences"] == []
    # no hand edit left: nothing kept as it is, even when asked to keep them
    hv = {"import_cluster_config": out}
    assert "cassandra_config_keep_files" not in node(hv, keep_hand_edits=True)["keep"]


def test_without_the_blank_line_at_the_end():
    files = stock()
    files["cassandra-env.sh"] = files["cassandra-env.sh"].rstrip("\n") + "\nexport FOO=1\n"
    out, n = imported(files)
    assert out["vars"]["cassandra_env_extra_lines"] == ["export FOO=1"] and out["hand_edits"] == []
    assert self_check(files, cassandra_inventory_layout([n], "c"))["differences"] == []


@pytest.mark.parametrize("line", [
    'JMX_PORT="7299"', 'export MAX_HEAP_SIZE="16G"', 'JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=h1"',
    'JVM_OPTS="$JVM_OPTS -Xms16G -Xmx16G"', 'JVM_OPTS="$JVM_OPTS -Dcassandra.jmx.local.port=7299"',
    'JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=false"', 'JVM_OPTS="$JVM_OPTS -XX:+UseG1GC"',
    'JVM_OPTS="$JVM_OPTS -XX:HeapDumpPath=/data/dumps"',
])
def test_lines_a_variable_covers_stay_hand_edits(line):
    # set at the end, they would win over their variable (or over the role's JVM options): left as hand edits,
    # with the lines around them (all of the end lines or none)
    files = with_env("export FOO=1", line)
    out, n = imported(files)
    assert "cassandra_env_extra_lines" not in out["vars"]
    assert [x for x in out["hand_edits"] if x.startswith("  +") and x.strip() != "+"] == ["  + export FOO=1", "  + " + line]


@pytest.mark.parametrize("lines", [
    ["if [ -f /etc/big ]; then", "  MAX_HEAP_SIZE=16G", "fi"],  # cut in two, it would not run
    ["if [ -f /etc/big ]; then", "  export FOO=1"],  # not closed
    ['export FOO="a', 'b"'],  # a quote going on to the next line
    ["export FOO=a \\", "b"],
])
def test_end_lines_that_do_not_make_a_whole_stay_hand_edits(lines):
    out, n = imported(with_env(*lines))
    assert "cassandra_env_extra_lines" not in out["vars"] and out["hand_edits"]


def test_a_whole_block_at_the_end_carried():
    lines = ["if [ -f /etc/agent ]; then", '    JVM_OPTS="$JVM_OPTS -javaagent:/opt/agent.jar"', "fi"]
    files = with_env(*lines)
    out, n = imported(files)
    assert out["vars"]["cassandra_env_extra_lines"] == lines and out["hand_edits"] == []
    assert self_check(files, cassandra_inventory_layout([n], "c"))["differences"] == []


def test_a_line_in_the_middle_stays_a_hand_edit_and_the_hint_says_how():
    files = stock()
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace("\nJMX_PORT=", '\nexport FOO=1\nJMX_PORT=', 1)
    out, n = imported(files)
    assert "cassandra_env_extra_lines" not in out["vars"]
    assert "  + export FOO=1" in out["hand_edits"]
    layout = cassandra_inventory_layout([n], "c")
    check = self_check(files, layout)
    assert check["differences"]
    advice = " ".join(cassandra_import_self_check_advice(layout, {"n1": check}, width=400))
    assert "cassandra_env_extra_lines" in advice and "cassandra_config_template_overrides" in advice
    assert "cassandra_env_extra_lines and cassandra_logback_extra_lines for lines at the end of the file" in advice


def logback(text):
    files = stock()
    files["logback.xml"] = files["logback.xml"].replace("</configuration>", text)
    return files


def test_loggers_at_the_end_of_logback_carried():
    files = logback('  <logger name="com.example" level="WARN"/>\n</configuration>')
    out, n = imported(files)
    assert out["vars"]["cassandra_logback_extra_lines"] == ['  <logger name="com.example" level="WARN"/>']
    assert out["hand_edits"] == []
    assert self_check(files, cassandra_inventory_layout([n], "c"))["differences"] == []


@pytest.mark.parametrize("text", [
    "</configuration>\n<!-- after the root element -->",  # outside <configuration>: not where the role writes them
    '  <logger name="org.apache.cassandra" level="INFO"/>\n</configuration>',  # cassandra_log_level_cassandra's
    "  <logger level='INFO'  name='org.apache.cassandra'/>\n</configuration>",  # (written another way)
    '  <root level="WARN">\n    <appender-ref ref="SYSTEMLOG"/>\n  </root>\n</configuration>',
    '  <logger name="com.example">\n</configuration>',  # not closed
    '  <LOGGER xmlns="urn:x" name="org.apache.cassandra" level="INFO"/>\n</configuration>',
])
def test_logback_lines_not_carried(text):
    out, dummy = imported(logback(text))
    assert "cassandra_logback_extra_lines" not in out["vars"]
    assert [line for line in out["hand_edits"] if line.startswith("logback.xml")]


def secret_files():
    files = with_env('export KEYSTORE_PASSWORD="s3cr3t-A"',
                     'JVM_OPTS="$JVM_OPTS -Dcom.example.flag=1 -Djavax.net.ssl.trustStorePassword=tsP4ss"')
    files["jvm-server.options"] += "-Djavax.net.ssl.keyStorePassword=jvmP4ss\n-Dfoo=bar\n"
    return files


def written(layout):
    return dict((f["path"], f) for f in cassandra_inventory_files(layout))


def test_secrets_moved_to_secrets_yml():
    files = secret_files()
    out, n = imported(files)
    assert out["vars"]["cassandra_env_extra_lines"] == [
        'export KEYSTORE_PASSWORD="{{ cassandra_secret_env_keystore_password }}"',
        'JVM_OPTS="$JVM_OPTS -Dcom.example.flag=1'
        ' -Djavax.net.ssl.trustStorePassword={{ cassandra_secret_env_javax_net_ssl_truststorepassword }}"']
    assert out["vars"]["cassandra_jvm_extra_options"] == [
        "-Djavax.net.ssl.keyStorePassword={{ cassandra_secret_jvm_javax_net_ssl_keystorepassword }}", "-Dfoo=bar"]
    layout = cassandra_inventory_layout([n], "c")
    files_out = written(layout)
    main, secrets = files_out["group_vars/c/main.yml"], files_out["group_vars/c/secrets.yml"]
    assert secrets["secret"] and not main["secret"]  # (the playbook vaults the secret ones)
    assert yaml.safe_load(secrets["content"]) == {
        "cassandra_secret_env_keystore_password": "s3cr3t-A", "cassandra_secret_env_javax_net_ssl_truststorepassword": "tsP4ss",
        "cassandra_secret_jvm_javax_net_ssl_keystorepassword": "jvmP4ss"}
    # the lines in main.yml, as templates (not !unsafe): Ansible gives them the secrets
    data = yaml.safe_load(main["content"])
    assert data["cassandra_env_extra_lines"] == out["vars"]["cassandra_env_extra_lines"]
    assert "!unsafe" not in main["content"]
    # nowhere else in clear: not in main.yml, the report, the notes
    hv = {"import_cluster_config": out}
    notes = node(hv)["notes"]
    for text in [main["content"], layout["report"], "\n".join(notes),
                 "\n".join(out["normalized"] + out["hand_edits"] + out["carried"])]:
        assert not [s for s in SECRETS if s in text]
    assert "hand edits carried into the inventory: cassandra-env.sh: KEYSTORE_PASSWORD=**** moved to secrets.yml as" \
           " cassandra_secret_env_keystore_password, which cassandra_env_extra_lines refers to" in notes
    # the self-check renders them with the secrets: the node's files
    assert self_check(files, layout)["differences"] == []


@pytest.mark.parametrize("line, kind", [
    ('export TRUSTSTORE_PASSWORD="$(cat /etc/pw)"', "sh"),  # an expansion: its value is not in the line
    ("export TRUSTSTORE_PASSWORD=a'b'", "sh"),  # a word made of parts
    ("export TRUSTSTORE_PASSWORD= x", "sh"),  # an empty value, then a command
])
def test_a_secret_not_told_whole_keeps_its_list_in_secrets_yml(line, kind):
    files = with_env("export FOO=1", line)
    out, n = imported(files)
    assert out["vars"]["cassandra_env_extra_lines"] == ["export FOO=1", line]
    files_out = written(cassandra_inventory_layout([n], "c"))
    assert "cassandra_env_extra_lines" not in yaml.safe_load(files_out["group_vars/c/main.yml"]["content"])
    assert "cassandra_env_extra_lines" in yaml.safe_load(files_out["group_vars/c/secrets.yml"]["content"])
    assert "cassandra-env.sh: cassandra_env_extra_lines kept whole in secrets.yml (a secret in it could not be taken" \
           " out alone)" in out["carried"]


def test_each_node_its_own_secret():
    a, na = imported(with_env('export KEYSTORE_PASSWORD="pw-one"'))
    b, nb = imported(with_env('export KEYSTORE_PASSWORD="pw-two"'), "n2", "10.0.0.2")
    layout = cassandra_inventory_layout([na, nb], "c")
    files_out = written(layout)
    assert yaml.safe_load(files_out["group_vars/c/main.yml"]["content"])["cassandra_env_extra_lines"] == [
        'export KEYSTORE_PASSWORD="{{ cassandra_secret_env_keystore_password }}"']
    for name, pw in (("n1", "pw-one"), ("n2", "pw-two")):
        assert yaml.safe_load(files_out["host_vars/%s/secrets.yml" % name]["content"]) == {
            "cassandra_secret_env_keystore_password": pw}
    assert "pw-one" not in layout["report"] and "pw-two" not in layout["report"]
    assert "SECRETS OF HAND EDITS PER NODE: cassandra_secret_env_keystore_password are set per node" in layout["report"]
    assert self_check(with_env('export KEYSTORE_PASSWORD="pw-two"'), layout, "n2")["differences"] == []


def test_two_secrets_of_the_same_name():
    files = with_env('JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStorePassword=one"', "export X_PASSWORD=two",
                     'JVM_OPTS="$JVM_OPTS -DX_PASSWORD=four"')
    files["jvm-server.options"] += "-Djavax.net.ssl.keyStorePassword=three\n"
    out, n = imported(files)
    assert out["vars"]["cassandra_secret_env_javax_net_ssl_keystorepassword"] == "one"
    assert out["vars"]["cassandra_secret_env_x_password"] == "two"
    assert out["vars"]["cassandra_secret_env_x_password_2"] == "four"
    assert out["vars"]["cassandra_secret_jvm_javax_net_ssl_keystorepassword"] == "three"
    assert self_check(files, cassandra_inventory_layout([n], "c"))["differences"] == []


def test_the_same_lines_the_same_variables_on_every_node():
    # named per file: a node with a secret in another file too gets the same names for the lines both have
    a_files = with_env('JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStorePassword=A"')
    a_files["jvm-server.options"] += "-Djavax.net.ssl.keyStorePassword=B\n"
    b_files = stock()
    b_files["jvm-server.options"] += "-Djavax.net.ssl.keyStorePassword=B\n"
    a, na = imported(a_files)
    b, nb = imported(b_files, "n2", "10.0.0.2")
    layout = cassandra_inventory_layout([na, nb], "c")
    group = yaml.safe_load(written(layout)["group_vars/c/main.yml"]["content"])
    assert group["cassandra_jvm_extra_options"] == [
        "-Djavax.net.ssl.keyStorePassword={{ cassandra_secret_jvm_javax_net_ssl_keystorepassword }}"]
    assert "cassandra_jvm_extra_options" not in layout["differences"]


def test_a_nodes_own_template_text_is_never_templated():
    # a line read from a node with {{ in it: written !unsafe, also when another node's line made by the import is
    # the same text
    own = 'JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.trustStorePassword={{ cassandra_secret_env_javax_net_ssl_truststorepassword }}"'
    a, na = imported(with_env('JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.trustStorePassword=x"'))
    b, nb = imported(with_env(own), "n2", "10.0.0.2")
    assert b["trusted"] == [] and b["vars"]["cassandra_env_extra_lines"] == [own]
    layout = cassandra_inventory_layout([na, nb], "c")
    assert layout["trusted"] == []
    text = "\n".join(f["content"] for f in cassandra_inventory_files(layout))
    assert re.search(r"!unsafe '?JVM_OPTS=", text)


def test_self_check_renders_your_own_template(tmp_path):
    # a logback.xml no variable makes: your template for it (cassandra_config_template_overrides, in a file of
    # yours), the self-check renders it as cassandra_config would
    files = stock()
    files["logback.xml"] = files["logback.xml"].replace("<root level=\"INFO\">",
                                                        '<logger name="com.example" level="WARN"/>\n  <root level="INFO">')
    out, n = imported(files)
    layout = cassandra_inventory_layout([n], "c")
    assert self_check(files, layout)["differences"]
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "logback.xml.j2").write_text(files["logback.xml"].replace(
        "<root level=\"INFO\">", '<root level="{{ cassandra_log_level }}">'))
    yours = {"path": "group_vars/all/templates.yml", "secret": False,
             "content": 'cassandra_config_template_overrides:\n  logback.xml: "{{ inventory_dir }}/templates/logback.xml.j2"\n'}
    assert self_check(files, layout, extra_files=[yours], inventory_dir=str(tmp_path))["differences"] == []


@pytest.mark.parametrize("env, jvm, logback_line", [
    # the masking of the output reads more of the value than the import would take: the list whole in secrets.yml
    ("# it's KEYSTORE_PASSWORD=LEAK1 LEAK2", None, None),
    ("export KEYSTORE_PASSWORD=LEAK1 LEAK2", None, None),
    ('JVM_OPTS="$JVM_OPTS -Dx.password=\'LEAK1 LEAK2\'"', None, None),
    (None, "#-Djavax.net.ssl.keyStorePassword=LEAK1 LEAK2", None),
    (None, '-Dx.password="LEAK1 LEAK2"', None),
    (None, None, "  <!-- password=LEAK1 LEAK2 -->"),
    # a secret the masking does not name as one
    (None, None, '  <property name="dbPassword" value="LEAK1"/>'),
    ('export X="--password LEAK1"', None, None),
    # a node's own reference, then its secret
    ("export KEYSTORE_PASSWORD={{ cassandra_secret_x }}LEAK1", None, None),
])
def test_no_part_of_a_secret_in_main_yml(env, jvm, logback_line):
    files = with_env("export FOO=1", env) if env else stock()
    if jvm:
        files["jvm-server.options"] += jvm + "\n"
    if logback_line:
        files = dict(files, **{"logback.xml": files["logback.xml"].replace("</configuration>",
                                                                           logback_line + "\n</configuration>")})
    out, n = imported(files)
    layout = cassandra_inventory_layout([n], "c")
    main = written(layout)["group_vars/c/main.yml"]["content"]
    assert "LEAK" not in main and "LEAK" not in layout["report"]


def test_a_line_made_like_one_of_the_node_is_left_as_it_is():
    # the node has the very line the import would make of its other line: neither is written as a template
    own = 'export A_PASSWORD="{{ cassandra_secret_env_a_password }}"'
    out, n = imported(with_env('export A_PASSWORD="realpw"', own))
    assert out["trusted"] == [] and out["vars"]["cassandra_env_extra_lines"][0] == 'export A_PASSWORD="realpw"'
    layout = cassandra_inventory_layout([n], "c")
    secrets = written(layout)["group_vars/c/secrets.yml"]["content"]
    assert "cassandra_env_extra_lines:" in secrets and "!unsafe 'export A_PASSWORD=" in secrets


def test_self_check_says_what_cassandra_config_would_refuse(tmp_path):
    files = stock()
    out, n = imported(files)
    layout = cassandra_inventory_layout([n], "c")
    (tmp_path / "l.j2").write_text("x")
    for value, said in (("templates/logback.xml.j2", "logback.xml: not an absolute path to a file on the controller"),
                        (str(tmp_path / "nowhere.j2"), "logback.xml: not an absolute path to a file on the controller"),
                        ("[1]", "not a dict")):
        content = ("cassandra_config_template_overrides: %s\n" % value if value == "[1]"
                   else "cassandra_config_template_overrides:\n  logback.xml: %s\n" % value)
        yours = {"path": "group_vars/all/t.yml", "secret": False, "content": content}
        assert [d for d in self_check(files, layout, extra_files=[yours])["differences"] if said in d], value


def test_reimport_leaves_your_templates_files_to_them(tmp_path):
    # a file your own template renders: not a hand edit of the import, nor kept as it is (cassandra_config
    # refuses a file both kept and overridden)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    files = stock()
    files["logback.xml"] = files["logback.xml"].replace("<root level=\"INFO\">",
                                                        '<logger name="com.example" level="WARN"/>\n  <root level="INFO">')
    out, n = imported(files)
    n = dict(n, keep=dict(n["keep"], cassandra_config_keep_files=["logback.xml"]))
    yours = {"path": "group_vars/all/t.yml",
             "content": "cassandra_config_template_overrides:\n  logback.xml: /i/templates/logback.xml.j2\n"}
    layout = cassandra_inventory_layout_over([n], "c", [yours])
    assert "cassandra_config_keep_files" not in layout["host_vars"]["n1"]
    assert "HAND EDITS" not in layout["report"]
    assert "logback.xml: your own template (cassandra_config_template_overrides)" in layout["report"]


@pytest.mark.parametrize("name, text", [
    ("logback.xml", '  <logger name="com.example" level="WARN"/>'),
    ("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS -javaagent:/opt/x.jar"'),
    ("cassandra-env.sh", 'export KEYSTORE_PASSWORD="s3cr3t-A"'),
])
@pytest.mark.parametrize("has_it", [True, False])
def test_your_template_and_the_end_lines(tmp_path, name, text, has_it):
    # your copy of the role's template, with the node's line in it (not carried again: it would be written twice)
    # or without (carried, your copy renders the list as the role's does); its path under inventory_dir
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    files = stock()
    if name == "logback.xml":
        files[name] = files[name].replace("</configuration>", text + "\n</configuration>")
    else:
        files[name] += text + "\n"
    out, n = imported(files)
    with open(os.path.join(ROLE_TEMPLATES, name + ".j2"), encoding="utf-8") as f:
        mine = f.read()
    if has_it:
        mine = mine.replace("</configuration>", text + "\n</configuration>") if name == "logback.xml" \
            else mine.rstrip("\n") + "\n" + text + "\n"
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.j2").write_text(mine)
    yours = {"path": "group_vars/all/t.yml", "secret": False,
             "content": 'cassandra_config_template_overrides:\n  %s: "{{ inventory_dir }}/templates/t.j2"\n' % name}
    layout = cassandra_inventory_layout_over([n], "c", [yours], str(tmp_path))
    text_out = "\n".join(f["content"] for f in cassandra_inventory_files(layout))
    assert ("extra_lines" in text_out) is not has_it
    assert ("cassandra_secret_env_keystore_password" in text_out) is (not has_it and "PASSWORD" in text)
    assert ("are in your own template (cassandra_config_template_overrides): not carried" in layout["report"]) is has_it
    assert self_check(files, layout, extra_files=[yours], inventory_dir=str(tmp_path))["differences"] == []


@pytest.mark.parametrize("name, lines, in_yours", [
    # a line your copy of the role's template has elsewhere (fi): the block still carried whole
    ("cassandra-env.sh", ["if [ -f /etc/x ]; then", "  . /etc/x", "fi"], []),
    ("logback.xml", ['  <appender name="X" class="ch.qos.logback.core.ConsoleAppender">', "  </appender>"], []),
    # your template has one of them only: all of them carried, in their order
    ("cassandra-env.sh", ['export KEYSTORE_PASSWORD="s3cr3t-A"', 'export TRUSTSTORE_PASSWORD="t5"'],
     ['export KEYSTORE_PASSWORD="s3cr3t-A"']),
])
def test_end_lines_left_to_your_template_all_or_none(tmp_path, name, lines, in_yours):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    files = stock()
    if name == "logback.xml":
        files[name] = files[name].replace("</configuration>", "\n".join(lines) + "\n</configuration>")
    else:
        files[name] += "\n".join(lines) + "\n"
    out, n = imported(files)
    with open(os.path.join(ROLE_TEMPLATES, name + ".j2"), encoding="utf-8") as f:
        mine = f.read()
    mine = mine.rstrip("\n") + "\n" + "".join(line + "\n" for line in in_yours)
    (tmp_path / "t.j2").write_text(mine)
    yours = {"path": "group_vars/all/t.yml", "secret": False,
             "content": "cassandra_config_template_overrides:\n  %s: %s\n" % (name, tmp_path / "t.j2")}
    layout = cassandra_inventory_layout_over([n], "c", [yours])
    assert "not carried" not in layout["report"]
    if not in_yours:
        assert self_check(files, layout, extra_files=[yours])["differences"] == []


@pytest.mark.parametrize("name, tail, yours_tail", [
    # blocks the node has with a blank line between them: still your template's
    ("cassandra-env.sh", 'export KEYSTORE_PASSWORD="s3cr3t-A"\n\nif [ -f /etc/x ]; then\n  . /etc/x\nfi\n', None),
    # JVM options: in your template in their order, other lines between them
    ("jvm-server.options", "-Dmy.first=1\n-Dmy.trustStorePassword=jvmP4ss\n",
     "-Dmy.first=1\n# (in between)\n-Dmy.trustStorePassword=jvmP4ss\n"),
])
def test_your_copy_of_the_nodes_file(tmp_path, name, tail, yours_tail):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    files = stock()
    files[name] += tail
    out, n = imported(files)
    with open(os.path.join(ROLE_TEMPLATES, name + ".j2"), encoding="utf-8") as f:
        mine = f.read().rstrip("\n") + "\n" + (yours_tail or tail)
    (tmp_path / "t.j2").write_text(mine)
    yours = {"path": "group_vars/all/t.yml", "secret": False,
             "content": "cassandra_config_template_overrides:\n  %s: %s\n" % (name, tmp_path / "t.j2")}
    layout = cassandra_inventory_layout_over([n], "c", [yours])
    assert "not carried" in layout["report"]
    assert "cassandra_secret_" not in "\n".join(f["content"] for f in cassandra_inventory_files(layout))
    assert self_check(files, layout, extra_files=[yours])["differences"] == []
