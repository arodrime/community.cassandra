from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster_keep_hand_edits: the config files with hand edits no variable
# covers are left as they are on their node (cassandra_config_keep_files), not
# written nor compared by cassandra_config and the self-check.

import base64
import os
import re

import pytest
import yaml

from ansible.errors import AnsibleFilterError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_config_import, cassandra_inventory_files, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    cassandra_import_self_check, cassandra_import_self_check_nodes)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    cassandra_import_self_check_advice)

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.import_nodes import node

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
STOCK = os.path.join(ROOT, "roles", "cassandra_config", "molecule", "default", "files", "stock-5.0.9")
FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


NOT_IMPORTED = "jmxremote.password (/etc/cassandra/jmxremote.password): its users NOT imported"
HAND = ["logback.xml, line 12:", "  - <x/>", "  + <y/>", "cassandra-env.sh: JMX_PORT", NOT_IMPORTED]


def test_files_with_hand_edits_kept_on_request():
    hv = {"import_cluster_config": {"vars": {}, "hand_edits": HAND}}
    assert "cassandra_config_keep_files" not in node(hv)["keep"]  # by default: the self-check fails on them
    assert node(hv, keep_hand_edits=True)["keep"]["cassandra_config_keep_files"] == ["logback.xml", "cassandra-env.sh"]


@pytest.mark.parametrize("keep, kept", [
    ("logback.xml", ["logback.xml"]),  # -e import_cluster_keep_hand_edits=logback.xml
    ("logback.xml, cassandra-env.sh", ["logback.xml", "cassandra-env.sh"]),
    (["cassandra-env.sh"], ["cassandra-env.sh"]),
    (["cassandra.yaml"], None),  # no hand edit there: nothing to keep
    ("false", None),
    ("yes", ["logback.xml", "cassandra-env.sh"]),
    ('["logback.xml"]', ["logback.xml"]),  # -e 'import_cluster_keep_hand_edits=["logback.xml"]': a string
])
def test_files_with_hand_edits_kept_by_name(keep, kept):
    hv = {"import_cluster_config": {"vars": {}, "hand_edits": HAND}}
    assert node(hv, keep_hand_edits=keep)["keep"].get("cassandra_config_keep_files") == kept


@pytest.mark.parametrize("keep", ["logback", "cassandra.yml", ["jmxremote.secret"], "logback.xml,cassandra.yml"])
def test_keep_by_name_refuses_what_is_not_a_config_file(keep):
    with pytest.raises(AnsibleFilterError, match="not a config file"):
        node({"import_cluster_config": {"vars": {}, "hand_edits": HAND}}, keep_hand_edits=keep)


def test_keep_by_name_takes_the_jmx_users_files():
    # cassandra_config keeps them too (cassandra_config_keep_files): named, they are no error
    node({"import_cluster_config": {"vars": {}, "hand_edits": HAND}}, keep_hand_edits="jmxremote.password,jmxremote.access")


def stock():
    files = {}
    for name in os.listdir(STOCK):
        with open(os.path.join(STOCK, name), encoding="utf-8") as f:
            files[name.replace(".stock", "")] = f.read()
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace('CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"',
                                                                  "CASSANDRA_LOG_DIR=/var/log/cassandra")
    return files


def role_permissions(files):
    # what the roles give them: root:cassandra, cassandra.yaml and the JVM options 0640, the others 0644
    return dict((name, {"owner": "root", "group": "cassandra", "uid": 0, "gid": 990,
                        "mode": "0640" if name == "cassandra.yaml" or name.endswith(".options") else "0644"})
                for name in files)


def check(files, keep, permissions=None):
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    node = {"name": "n1", "address": "10.0.0.1", "hostname": "n1", "dc": "dc1", "rack": "r1", "read": True,
            "vars": dict(imported["vars"], cassandra_version="50x"), "hand_edits": imported["hand_edits"],
            "normalized": imported["normalized"], "comments": imported["comments"], "notes": [],
            "keep": dict({"cassandra_service_unit_manage": False}, **keep)}
    layout = cassandra_inventory_layout([node], "c")
    out = cassandra_import_self_check(cassandra_inventory_files(layout), layout["hosts"], "n1", FACTS, files, {},
                                      "/var/lib/cassandra", "17", "",
                                      role_permissions(files) if permissions is None else permissions)
    return out, layout


def test_report_names_the_edits_left_and_the_others():
    base = {"name": "n1", "address": "10.0.0.1", "dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"},
            "normalized": [], "notes": [], "hand_edits": HAND, "keep": {"cassandra_config_keep_files": ["logback.xml"]}}
    report = cassandra_inventory_layout([base], "c")["report"].split("\n")
    kept = report.index("  HAND EDITS no variable covers, in files LEFT AS THEY ARE on this node"
                        " (cassandra_config_keep_files; nodes added later get the role's):")
    other = report.index("  HAND EDITS no variable covers (cassandra_config would revert them):")
    assert report[kept + 1:kept + 4] == ["    logback.xml, line 12:", "      - <x/>", "      + <y/>"]
    assert report[other + 1:other + 3] == ["    cassandra-env.sh: JMX_PORT", "    " + NOT_IMPORTED]


def test_self_check_leaves_out_the_kept_files():
    files = stock()
    files["logback.xml"] = re.sub(r"<root level=\"INFO\">", '<logger name="com.example" level="WARN"/>\n  <root level="INFO">',
                                  files["logback.xml"], count=1)
    out, layout = check(files, {})
    assert [d for d in out["differences"] if d.startswith("logback.xml")]
    out, layout = check(files, {"cassandra_config_keep_files": ["logback.xml"]})
    assert out["differences"] == []
    assert "in files LEFT AS THEY ARE on this node" in layout["report"]


def test_the_stop_reads_the_self_checks_own_differences():
    # what the self-check really writes, read back by the stop: a logback.xml hand edit and the heap
    files = stock()
    files["logback.xml"] = re.sub(r"<root level=\"INFO\">", '<logger name="com.example" level="WARN"/>\n  <root level="INFO">',
                                  files["logback.xml"], count=1)
    out, layout = check(files, {})
    out["differences"].append(out["differences"][0])  # (the same difference twice: one item)
    heap = [d for d in check_heap() if d.startswith("heap")]
    lines = cassandra_import_self_check_advice(layout, {"n1": {"differences": out["differences"] + heap}}, width=160)
    at = lines.index("  Hand edits no variable covers (cassandra_config would revert them):")
    assert lines[at + 1] == "    - logback.xml, on n1:" and lines[at + 2].startswith("        /configuration")
    at = lines.index("    - heap MAX_HEAP_SIZE, on n1:")
    assert lines[at + 1] == "        node 4G, inventory 8G"
    assert lines[lines.index("    - heap HEAP_NEWSIZE, on n1:") + 1] == "        800M (not in the inventory)"
    assert lines[0].startswith("SELF-CHECK FAILED  c  the inventory would change 3 settings on 1 node")


def check_heap():
    files = stock()
    files["cassandra-env.sh"] += '\nMAX_HEAP_SIZE="4G"\nHEAP_NEWSIZE="800M"\n'
    imported = cassandra_config_import(dict(files), "50x", FACTS, "", "/var/lib/cassandra")
    imported["vars"]["cassandra_heap_size"] = "8G"  # (the inventory's other value)
    node = {"name": "n1", "address": "10.0.0.1", "hostname": "n1", "dc": "dc1", "rack": "r1", "read": True,
            "vars": dict(imported["vars"], cassandra_version="50x"), "hand_edits": [], "normalized": [],
            "comments": [], "notes": [], "keep": {"cassandra_service_unit_manage": False}}
    layout = cassandra_inventory_layout([node], "c")
    return cassandra_import_self_check(cassandra_inventory_files(layout), layout["hosts"], "n1", FACTS, files, {},
                                       "/var/lib/cassandra", "17", "", role_permissions(files))["differences"]


def test_role_leaves_them():
    with open(os.path.join(ROOT, "roles", "cassandra_config", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    block = next(t for t in main if t.get("name") == "Configure")["block"]
    note = next(t for t in block if t.get("name") == "List the files to change and whether to ask first")
    results = [{"item": "logback.xml", "stat": {"exists": True}}, {"item": "cassandra-env.sh", "stat": {"exists": False}}]
    for initialized, kept in [(True, ["logback.xml"]),  # a node without the file gets the role's
                              (False, [])]:  # a new node (e.g. rebuilt under that name): its files are the package's
        variables = {"cassandra_config_preview": {"results": []}, "_cassandra_config_compared": {}, "cassandra_conf_dir": "/c",
                     "cassandra_config_keep_stat": {"results": results},
                     "_cassandra_config_initialized": initialized}
        variables["_keep"] = trust_as_template(note["vars"]["_keep"])
        assert render(note["vars"]["_kept"], **variables) == kept


def test_self_check_compares_the_owner_and_mode_of_the_kept_files():
    # cassandra_config keeps their content, not their owner, group and mode: those are compared
    files = stock()
    files["logback.xml"] = re.sub(r"<root level=\"INFO\">", '<logger name="com.example" level="WARN"/>\n  <root level="INFO">',
                                  files["logback.xml"], count=1)
    permissions = role_permissions(files)
    permissions["logback.xml"] = dict(permissions["logback.xml"], mode="0600")
    out, layout = check(files, {"cassandra_config_keep_files": ["logback.xml"]}, permissions)
    assert out["differences"] == ["logback.xml: owner:group mode: node has root:cassandra 0600,"
                                  " import would write root:cassandra 0644"]


def test_self_check_of_every_node_read():
    # the import's one call: each node read checked as on its own, with its files as read (base64), its unit's
    # environment; a node not read left out
    files = stock()
    out, layout = check(files, {})
    nodes = [{"address": "10.0.0.1", "host": "h1", "read": True}, {"address": "10.0.0.2", "host": "h2", "read": False}]
    slurped = [{"item": name, "content": base64.b64encode(text.encode()).decode()} for name, text in files.items()]
    data = {"h1": {"ansible_facts": FACTS, "import_cluster_slurp": {"results": slurped + [{"item": "x", "failed": True}]},
                   "import_cluster_unit": {"env": {}}, "import_cluster_storage_dir": "/var/lib/cassandra",
                   "import_cluster_java": "17", "import_cluster_conf_dir": "",
                   "import_cluster_perms": {"files": role_permissions(files)}}}
    every = cassandra_import_self_check_nodes(cassandra_inventory_files(layout), layout["hosts"], nodes,
                                              {"10.0.0.1": "n1", "10.0.0.2": "n2"}, data)
    assert every == {"n1": out}


def test_self_check_of_every_node_names_the_one_that_failed():
    nodes = [{"address": "10.0.0.7", "host": "h7", "read": True}]
    with pytest.raises(AnsibleFilterError) as err:
        cassandra_import_self_check_nodes([], {}, nodes, {"10.0.0.7": "n7"}, {"h7": {"import_cluster_slurp": {"results": [
            {"item": "cassandra.yaml", "content": "hunter2 is not base64"}]}}})
    assert str(err.value).startswith("cassandra_import_self_check_nodes: 10.0.0.7, ") and "hunter2" not in str(err.value)


def jmx_files(access):
    """The stock files with remote JMX: cassandra-env.sh reads the password and access files."""
    files = stock()
    env = files["cassandra-env.sh"].replace("    LOCAL_JMX=yes", "    LOCAL_JMX=no")
    files["cassandra-env.sh"] = env.replace("#JVM_OPTS=\"$JVM_OPTS -Dcom.sun.management.jmxremote.access.file",
                                            "JVM_OPTS=\"$JVM_OPTS -Dcom.sun.management.jmxremote.access.file")
    files["jmxremote.password"] = JDK_PASSWORD + "cassandra s3cret\n"
    files["jmxremote.access"] = access
    permissions = role_permissions(files)
    for name in ("jmxremote.password", "jmxremote.access"):
        permissions[name] = {"owner": "cassandra", "group": "cassandra", "uid": 990, "gid": 990, "mode": "0400"}
    return files, permissions


# the JDK's stock comments of jmxremote.password, hashed examples of its roles commented out
JDK_PASSWORD = """# ----------------------------------------------------------------------
#           Template for jmxremote.password
#
# o Copy this template to jmxremote.password
# o Set the user/password entries in jmxremote.password
# ----------------------------------------------------------------------
#
#   monitorRole  QED
#   controlRole   R&D
#
# monitorRole  sNnHasH/hl8bKGlA0Ul8H8WTAbxRIRfuOdrKo3fC7q8= Ml3f/GxjrbiI5IyfbCg9ug== SHA3-512
# controlRole  Wd8oL5JGFfm8VXtIr5Ag2ts8c0twZtAqZDQ1+KFXUGQ= ifTokq7CXZPwW+IPhE5Yfw== SHA3-512
#
"""
JDK_ACCESS = ("#####################################################################\n"
              "#     Default Access Control File for Remote JMX(TM) Monitoring\n"
              "#####################################################################\n#\n"
              "# monitorRole readonly, controlRole readwrite (see the JDK's docs)\n#\n"
              "monitorRole   readonly\ncontrolRole   readwrite \\\n"
              "              create javax.management.monitor.*,javax.management.timer.* \\\n              unregister\n")


@pytest.mark.parametrize("keep_hand_edits", [False, True])
def test_jdk_stock_access_entries_imported(keep_hand_edits):
    # the JDK's stock controlRole and monitorRole (no password entry): carried, nothing kept, the self-check passes
    files, permissions = jmx_files(JDK_ACCESS + "cassandra readwrite\n")
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == []
    assert imported["vars"]["cassandra_jmx_users"] == [
        {"name": "cassandra", "password": "s3cret", "access": "readwrite", "create_unregister": False},
        {"name": "monitorRole", "access": "readonly"}, {"name": "controlRole", "access": "readwrite"}]
    keep = node({"import_cluster_config": imported}, keep_hand_edits=keep_hand_edits)["keep"]
    assert "cassandra_config_keep_files" not in keep
    assert check(files, {}, permissions)[0]["differences"] == []


def test_jmx_entry_no_variable_expresses_is_a_hand_edit_kept_on_request():
    # an access entry the import cannot carry ("role=rights", which the JVM reads as a properties line): a hand
    # edit of jmxremote.access, so the self-check sorts it with the hand edits; kept, the self-check passes
    files, permissions = jmx_files(JDK_ACCESS + "cassandra readwrite\nauditRole=readonly\n")
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == ["jmxremote.access: auditRole=readonly", "  + auditRole=readonly"]
    out, layout = check(files, {}, permissions)
    assert out["differences"] == ["jmxremote.access: auditRole=readonly: node has '', import would write nothing"]
    lines = cassandra_import_self_check_advice(layout, {"n1": out}, width=160)
    at = lines.index("  Hand edits no variable covers (cassandra_config would revert them):")
    assert lines[at + 1] == "    - jmxremote.access auditRole=readonly, on n1:"
    assert "Other differences:" not in "\n".join(lines)
    keep = node({"import_cluster_config": imported}, keep_hand_edits=True)["keep"]
    assert keep["cassandra_config_keep_files"] == ["jmxremote.access"]
    out, layout = check(files, {"cassandra_config_keep_files": ["jmxremote.access"]}, permissions)
    assert out["differences"] == []
    assert "in files LEFT AS THEY ARE on this node" in layout["report"]


def test_three_users_and_the_stock_roles_imported_passwords_in_secrets_only():
    files, permissions = jmx_files(JDK_ACCESS + "cassandra readwrite\nrepair readwrite \\\n  create"
                                   " javax.management.monitor.*,javax.management.timer.* \\\n  unregister\nmonitor readonly\n")
    files["jmxremote.password"] = JDK_PASSWORD + "cassandra Pw-One1\nrepair Pw-Two2\nmonitor Pw-Three3\n"
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == []
    assert [(u["name"], u.get("password"), u["access"]) for u in imported["vars"]["cassandra_jmx_users"]] == [
        ("cassandra", "Pw-One1", "readwrite"), ("repair", "Pw-Two2", "readwrite"), ("monitor", "Pw-Three3", "readonly"),
        ("monitorRole", None, "readonly"), ("controlRole", None, "readwrite")]
    out, layout = check(files, {}, permissions)
    assert out["differences"] == []
    written = dict((f["path"], f["content"]) for f in cassandra_inventory_files(layout))
    for path, content in written.items():
        assert ("Pw-" in content) == path.endswith("secrets.yml"), path
    assert not re.search("Pw-", layout["report"])


def test_hashed_passwords_a_hand_edit_of_the_password_file_kept_on_request():
    # Java 14+ rewrites a password file it can write with hashes: no password to carry, the users' rights carried;
    # the password file a hand edit (never a failure of the whole import), kept as it is on request
    files, permissions = jmx_files(JDK_ACCESS + "cassandra readwrite\n")
    files["jmxremote.password"] = JDK_PASSWORD + "cassandra c2FsdA== aGFzaGVkUGFzcw== SHA3-512\n"
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == ["jmxremote.password: cassandra", "  + cassandra ****"]
    out, layout = check(files, {}, permissions)
    assert out["differences"] == ["jmxremote.password: cassandra: node has ****, import would write nothing"]
    lines = cassandra_import_self_check_advice(layout, {"n1": out}, width=160)
    assert lines[lines.index("  Hand edits no variable covers (cassandra_config would revert them):") + 1] == \
        "    - jmxremote.password cassandra, on n1:"
    assert "aGFzaGVk" not in "\n".join(lines) + layout["report"]
    keep = node({"import_cluster_config": imported}, keep_hand_edits=True)["keep"]
    assert keep["cassandra_config_keep_files"] == ["jmxremote.password"]
    assert check(files, {"cassandra_config_keep_files": ["jmxremote.password"]}, permissions)[0]["differences"] == []


def test_users_left_out_are_hand_edits_of_the_files():
    # a user without rights, rights the JVM refuses: hand edits of their file, the password masked
    files, permissions = jmx_files("cassandra readwrite\nbad readonly create a.*\n")
    files["jmxremote.password"] = "cassandra s3cret\nnone n0ne-secret\n"
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == ["jmxremote.password: none", "  + none ****",
                                      "jmxremote.access: bad", "  + bad readonly create a.*"]
    assert "n0ne-secret" not in str(check(files, {}, permissions))
