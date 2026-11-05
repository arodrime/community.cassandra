from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The copy of an import's report to share (import_cluster_report_shareable): no node name, cluster, address, domain
# or path of the site left in it, secrets masked; the sections, hand edits, versions and the import's notes kept;
# the key (n1 = the real name) apart.

import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_shareable import (
    _aligned, _line, _key, cassandra_import_shareable)
from ansible_collections.community.cassandra.tests.unit.plugins.filter import test_cassandra_import_large as large

DOMAIN = "corp.example.org"


def site_node(i, dc):
    """A node named after its site: its FQDN, its address, its DC and rack, a path and a URL of the site."""
    name = "db-%s-%02d.%s" % (dc, i, DOMAIN)
    return {"name": name, "address": "172.20.%d.%d" % (len(dc), i), "ansible_host": "172.20.%d.%d" % (len(dc), i),
            "hostname": "db-%s-%02d" % (dc, i), "dc": dc, "rack": "rack-%s" % ("north" if i % 2 else "south"),
            "read": True, "normalized": [], "notes": [],
            "versions": {"cassandra": "5.0.9", "java": "17.0.12", "os": "Debian 12"},
            "hand_edits": ["cassandra.yaml, line 30:", "  - hints_directory: /var/lib/cassandra/hints",
                           "  + hints_directory: /srv/sitedata-%s/hints" % dc,
                           "logback.xml, line 40:", '  + <logger name="org.example.billing.Ledger" level="DEBUG"/>'],
            "jvm_warnings": ["-Djava.rmi.server.hostname=%s (cassandra_jvm_extra_options, from jvm-server.options,"
                             " Java 17): reached at fd00::%d. Check it." % (name, i)],
            "vars": {"cassandra_version": "50x", "cassandra_num_tokens": 16,
                     "cassandra_seeds": ["db-paris-01.%s" % DOMAIN, "db-lyon-01.%s" % DOMAIN],
                     "cassandra_jmx_password": "s3cret-%s" % dc,
                     "cassandra_install_url": "https://repo.%s/cassandra/debian" % DOMAIN,
                     "cassandra_data_file_directories": ["/srv/sitedata-%s/data" % dc],
                     "cassandra_listen_address": "172.20.%d.%d" % (len(dc), i)}}


def site_report():
    nodes = [site_node(i, dc) for dc in ("paris", "lyon") for i in range(1, 4)]
    layout = cassandra_inventory_layout(nodes, "Orders Prod")
    args = (layout, ["/home/ops/sites/inventories/orders_prod.yml", "/home/ops/sites/inventories"],
            "/home/ops/sites/reports/orders_prod/report.txt", {}, True)
    # (as the playbook: report.txt, then report.details.txt, in one copy)
    lines = cassandra_import_report(*args, cwd="/home/ops/sites", in_git=True, width=160) + [""] + \
        cassandra_import_report(*args, cwd="/home/ops/sites", in_git=True, width=160, details=True)
    return nodes, lines, cassandra_import_shareable(lines, layout)


def test_nothing_of_the_site_left():
    nodes, lines, shared = site_report()
    text = "\n".join(shared["text"])
    real = set()
    for n in nodes:
        real |= {n["name"], n["hostname"], n["address"], n["dc"], n["rack"]}
    real |= {DOMAIN, "Orders Prod", "orders_prod", "sitedata", "fd00::", "org.example.billing", "repo."}
    assert [r for r in real if r in "\n".join(lines)]  # (the report itself has them)
    assert [r for r in real if r in text] == []
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", text)
    assert "s3cret" not in text and "(in secrets.yml)" in text  # (secrets masked)


def test_what_helps_kept():
    nodes, lines, shared = site_report()
    text = shared["text"]
    assert text[0].startswith("SHAREABLE COPY of an import_cluster report") and text[1] == ""
    # the sections, the hand edits with their lines, the versions, the warnings, the import's own notes
    for title in ("WHAT TO DO NEXT", "FOR INFO", "DETAILS"):  # (each in its rule; report.txt, then the details)
        assert [line for line in text if line.startswith("==== %s =" % title)], title
    assert "  What the import read on each node" in text
    joined = "\n".join(text)
    assert "        + hints_directory: /path" in joined and "        - hints_directory: /var/lib/cassandra/hints" in joined
    assert "    Cassandra 5.0.9, Java 17.0.12, Debian 12   all (6 nodes)" in text
    assert "-Djava.rmi.server.hostname=n" in joined and "reached at IP" in joined
    assert "IMPORT c1  6 nodes" in text[2]
    # the node lists by the DCs, renamed too
    assert "dc1 (3 nodes)" in joined or "dc2 (3 nodes)" in joined


def test_the_key_apart():
    nodes, lines, shared = site_report()
    key = "\n".join(shared["map"])
    assert "n1 = db-lyon-01.%s" % DOMAIN in key and "c1 = orders_prod" in key and "c1 = orders prod" in key.lower()
    assert "dc1 = lyon" in key and "c1_dc1_r1 = orders_prod_lyon_rack_north" in key
    assert not [line for line in shared["text"] if " = db-" in line]


def test_columns_stay_aligned():
    nodes, lines, shared = site_report()
    row = [line for line in shared["text"] if line.strip().startswith("cassandra_num_tokens:")][0]
    real = [line for line in lines if line.strip().startswith("cassandra_num_tokens:")][0]
    assert row.index("16") == real.index("16")


def test_88_nodes_no_name_address_or_domain():
    report = large.big_report(width=160) + [""] + large.big_report(width=160, details=True)
    layout = cassandra_inventory_layout(large.fake_cluster(), "Big Cluster")
    text = "\n".join(cassandra_import_shareable(report, layout)["text"])
    assert not re.search(r"\bdb\d+|10\.100\.|example\.net|big_cluster|Big Cluster", text)
    assert '<logger name="org.example.l9" level="WARN"/>' in text  # (no domain: a logger name)


def test_the_rules_word_by_word():
    key = _key({"cluster_group": "c", "nodes": [{"name": "web1", "dc": "dc1", "rack": "r1"}], "hosts": {}})
    assert _line("cassandra.yaml, jvm17-server.options, jmxremote.password: kept", key) == \
        "cassandra.yaml, jvm17-server.options, jmxremote.password: kept"
    assert _line("org.apache.cassandra.db and com.example.app", key) == "org.apache.cassandra.db and domain1"
    assert _line("web1.lan.example and web1", key) == "n1.domain2 and n1"
    assert _line("/etc/cassandra/cassandra.yaml /var/lib/cassandra/data /opt/site/x", key) == \
        "/etc/cassandra/cassandra.yaml /var/lib/cassandra/data /path1"
    assert _line("version 5.0.9, ETA 13:33:20, 10.1.2.3, ::1, 2001:db8::5", key) == \
        "version 5.0.9, ETA 13:33:20, IP, IP, IP"
    assert _line("cassandra_extra_settings.audit_logging_options.audit_logs_dir, community.cassandra.topology", key) \
        == "cassandra_extra_settings.audit_logging_options.audit_logs_dir, community.cassandra.topology"


def layout_of(nodes, cluster="Orders Prod", group="orders_prod"):
    return {"cluster_group": group, "cluster_name": cluster, "nodes": nodes, "hosts": {}}


def test_a_secret_the_report_let_through_masked_again():
    # (audit 2026-11-04) a second layer: every line of the copy masked as the output masks
    lines = ["    -Djavax.net.ssl.keyStorePassword=Planted1 (cassandra_jvm_extra_options)   all (2 nodes)",
             "      ldap_password: Planted2", "  nodetool -u admin -pw Planted3 status",
             "    cassandra_jmx_password:  (in secrets.yml)   all (2 nodes)",
             "    cassandra_config_file_permissions.jmxremote.password:  0400   all (2 nodes)"]
    text = cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))["text"]
    joined = "\n".join(text)
    assert not re.search(r"Planted\d", joined)
    assert "(in secrets.yml)" in text[5] and "0400" in text[6]  # (the report's own marks, a mode: as they are)


def test_an_ipv4_mapped_address_masked_whole():
    key = _key(layout_of([{"name": "a1"}]))
    assert _line("reached at ::ffff:10.1.2.3 and fe80::1:2", key) == "reached at IP and IP"


def test_names_whatever_their_case_and_the_cluster_name_forms():
    key = _key(layout_of([{"name": "DB-Paris-01", "dc": "Paris", "rack": "North"}]))
    assert _line("db-paris-01, DB-PARIS-01, paris, NORTH", key) == "n1, n1, dc1, r1"
    assert _line("orders-prod, Orders Prod, ORDERS_PROD, ordersprod", key) == "c1, c1, c1, c1"


def test_nodes_named_by_their_address():
    key = _key(layout_of([{"name": "10.0.0.7", "hostname": "", "ansible_host": "10.0.0.7"},
                          {"name": "10.0.0.12"}]))
    assert _line("10.0.0.7 and 10.0.0.12 (seed 10.0.0.99)", key) == "n1 and n2 (seed IP)"
    assert "10" not in [real for real in key.order if key.get(real) and key.get(real).startswith("n") and
                        real == "10"]  # (never the first number of an address as a name)


def test_common_words_never_replaced():
    nodes = [{"name": "cassandra", "dc": "main", "rack": "a"}, {"name": "data", "dc": "main", "rack": "b"}]
    key = _key(layout_of(nodes))
    line = "the Cassandra servers, cassandra.yaml, data directory, rack a, dc main"
    assert _line(line, key) == line


def test_site_dcs_numbered_past_the_real_dc1():
    key = _key(layout_of([{"name": "x1", "dc": "dc1"}, {"name": "x2", "dc": "lyon"}]))
    assert _line("dc1 and lyon", key) == "dc1 and dc2"


def test_paths_dirs_and_your_own_groups():
    key = _key(layout_of([{"name": "a1"}]), ["../ops-sitea/inventories", "../ops-sitea/reports/orders_prod"])
    assert _line("/etc/cassandra-sitea/x.yaml /etc/cassandra/cassandra.yaml /var/log/sitea/gc.log", key) == \
        "/path1 /etc/cassandra/cassandra.yaml /path2"
    assert _line("/var/log/cassandra/system.log /usr/local/sitea/bin /usr/local/bin/cqlsh", key) == \
        "/var/log/cassandra/system.log /path3 /usr/local/bin/cqlsh"
    assert _line("written: ../ops-sitea/inventories/orders_prod.yml", key) == "written: ../dir1/inventories/c1.yml"
    assert _line("in group_vars/payments_team/x.yml, group_vars/all/s.yml, group_vars/c1_dc1", key) == \
        "in group_vars/group1/x.yml, group_vars/all/s.yml, group_vars/c1_dc1"


def test_buckets_and_user_names_masked():
    key = _key(layout_of([{"name": "a1"}]))
    assert _aligned("    cassandra_medusa_extra_settings.storage.bucket_name (Medusa): sitea-backups on all (2 nodes);"
                    " sitea-old on a1 (1 node)", key) == \
        "    cassandra_medusa_extra_settings.storage.bucket_name (Medusa): bucket1 on all (2 nodes); bucket2 on n1" \
        " (1 node)"
    assert _aligned("cassandra_jmx_username: opsadmin", key) == "cassandra_jmx_username: user1"
    assert _aligned("cassandra_config_user: root", key) == "cassandra_config_user: root"  # (a common word)


def test_a_cluster_or_node_named_by_a_common_word():
    # (re-audit) a cluster named cassandra: no crash; its groups still renamed, the word left in the text
    for name in ("cassandra", "data", "Main"):
        nodes = [site_node(i, "paris") for i in (1, 2)]
        layout = cassandra_inventory_layout(nodes, name)
        group = layout["cluster_group"]
        shared = cassandra_import_shareable(["IMPORT %s" % group, "group_vars/%s_paris/main.yml" % group], layout)
        assert shared["text"][3] == "group_vars/c1_dc1/main.yml" and "c1 = %s" % group in shared["map"]
    key = _key(layout_of([{"name": "cassandra"}, {"name": "db7"}]))
    assert _line("cassandra.yaml on cassandra and db7", key) == "cassandra.yaml on cassandra and n2"


def test_user_names_in_a_table_row():
    # (re-audit) the FOR INFO table: the setting's name and its value in two columns
    lines = ["    cassandra_jmx_username:          acmejmx   all (6 nodes)   (the user nodetool logs in as)",
             "    cassandra_medusa_cql_username:   acmecql   all (6 nodes)",
             "    cassandra_medusa_extra_settings.storage.prefix:  acmepfx   all (6 nodes)",
             "    a: cassandra_jmx_username: one1, cassandra_cql_username: two2"]
    text = "\n".join(cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))["text"])
    for leak in ("acmejmx", "acmecql", "acmepfx", "one1", "two2"):
        assert leak not in text
    assert "storage.prefix:  prefix1" in text  # (the setting's name kept)


def test_names_inside_file_names_paths_and_compounds():
    key = _key(layout_of([{"name": "db-eu-west-1-01"}, {"name": "cass-pay-02.acme.io"}, {"name": "payments"},
                          {"name": "db1", "dc": "eu_west_1"}]))
    assert _line("/etc/cassandra/ssl/db-eu-west-1-01.jks /etc/cassandra/cass-pay-02.acme.io.pem", key) == \
        "/etc/cassandra/ssl/n3.jks /etc/cassandra/n1.pem"
    assert _line("cass-pay-02.keystore payments.yml /var/log/cassandra/db1-gc.log db1-old db1_backup", key) == \
        "n1.keystore n4.yml /var/log/cassandra/n2-gc.log n2-old n2_backup"
    assert _line("eu-west-1 and eu_west_1", key) == "dc1 and dc1"


def test_never_a_number_as_a_value_and_paths_after_a_mark():
    lines = ["    cassandra_medusa_extra_settings.storage.fqdn: 6 values, one per node, on all (6 nodes)",
             "    host_vars/<node>/main.yml and group_vars/c1*/x, 6 nodes, cassandra_medusa_bucket_name: 7"]
    text = cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))["text"]
    assert text[2].endswith("6 values, one per node, on all (6 nodes)")
    assert text[3] == "    host_vars/<node>/main.yml and group_vars/c1*/x, 6 nodes, cassandra_medusa_bucket_name: 7"


def test_the_nits():
    key = _key(layout_of([{"name": "a1"}]))
    assert _line("ops@acme.io ip-10-2-3-4 /etc/cassandra/../acme/x", key) == "user1@domain1 IP /path1"
    assert _line("ip-10-2-3-4.ec2.internal", key) == "domain2" and "domain2 = ip-10-2-3-4.ec2.internal" in key.lines()
    # a value's key line names the real value, never what replaced it
    shared = cassandra_import_shareable(["    cassandra_medusa_extra_settings.storage.fqdn: old.acme.io on all"],
                                        layout_of([{"name": "a1"}]))
    assert "host1 = old.acme.io" in shared["map"]


def test_home_and_relative_paths_and_a_superuser_masked():
    # (main session's leak probe) paths from the home or the current dir, and a superuser's name
    lines = ["path ~/acmeops/inv and ./acme-ops/x and ../acme-ops/y and ~bob/ops",
             "    cassandra_superuser:   carol   all (6 nodes)"]
    text = "\n".join(cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))["text"])
    for leak in ("acmeops", "acme-ops", "~bob", "carol"):
        assert leak not in text


def test_words_the_collection_never_writes_are_masked():
    # (owner, 2026-11-04) a keyspace, a site word in a file name, a user named in free text: words no rule knows,
    # masked because the collection's own files never use them; the report's words and the pseudonyms kept
    lines = ["keyspace zorbex_payments table zorbex_payments.qlinter",
             "keystore /etc/cassandra/zorbex.jks and zorbex2.keystore",
             "jmx user quibble on all (2 nodes), Cassandra 5.0.9, Java 17"]
    shared = cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))
    text = "\n".join(shared["text"])
    for leak in ("zorbex", "qlinter", "quibble"):
        assert leak not in text
    for kept in ("keyspace", "table", "keystore", "/etc/cassandra/", "jmx user", "Cassandra 5.0.9", "Java 17", "(2 nodes)"):
        assert kept in text
    assert [line for line in shared["map"] if line.endswith("= zorbex")]


def test_kernel_settings_and_xml_tags_kept():
    # (owner's cluster, 2026-11-05) vm.swappiness read as a domain, </file> as a path: both left as they are
    lines = ["sysctl /etc/sysctl.d/99-x.conf: vm.swappiness = 0 (cassandra_linux: 1)",
             "tuned [sysctl]: vm.dirty_ratio=10, net.core.rmem_max=212992",
             "  +     <file>/var/log/cassandra/audit/audit.log</file>"]
    text = "\n".join(cassandra_import_shareable(lines, layout_of([{"name": "a1"}]))["text"][2:])  # (its head said)
    for kept in ("vm.swappiness = 0", "vm.dirty_ratio=10", "net.core.rmem_max", "</file>"):
        assert kept in text
    assert "domain1" not in text
