from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The shareable copy against a corpus (pre-push audits of rc13, rounds 3 and 4): the self-check differences of a site
# (its names: acme, frob, kwy, quux, zorglub, paris, london...) through every place the report says them (the
# self-check's advice, report.txt, report.details.txt, the screen); none of the site's names may be left, and the
# stock names of a Cassandra install stay readable. Any change of the masking keeps it green.

import re

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    cassandra_import_report, cassandra_import_self_check_advice)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_shareable import cassandra_import_shareable

# the site's names in the corpus
SITE = re.compile(r"(?i)frob|kwy|quux|zorglub|acme|10\.0\.0|10\.1\.2|10\.9\.|fd12|2001:db8|fe80|192\.168"
                  r"|\b(paris|london|payments|billing|orders|ledger|operator|zq7|q7|z9)\b")
# kept in clear by the owner's decisions: a JVM option of a known prefix (JVM_KNOWN), a key of a file keyed by its own
# vocabulary (YAML, INI), an XML tag
KEPT_BY_DECISION = ("-Dcassandra.frobsetting", "-Dcassandra.frobnitz_dir", "frobnitz_custom_key", "[frobsection] frobkey",
                    "frobTag", "@frobattr", "acmeBroker", "table_properties_acme", "-Dio.netty.acmeflag")


def shared(differences, extra=()):
    """The shareable copy of every place the report says these differences of node zorglub-db1."""
    nodes = [{"name": "zorglub-db%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": True,
              "hand_edits": ["cassandra-env.sh, line 3:", "  + x", "logback.xml, line 5:", "  + <x/>"],
              "normalized": [], "notes": [], "vars": {"cassandra_version": "50x"}} for i in (1, 2)]
    layout = cassandra_inventory_layout(nodes, "Zorglub Prod")
    checks = {"zorglub-db1": {"differences": differences}}
    lines = cassandra_import_self_check_advice(layout, checks, report_file="/p/reports/x/report.txt", cwd="/p",
                                               width=200)
    args = (layout, ["/p/inventories/x.yml", "/p/inventories"], "/p/reports/x/report.txt", checks, False)
    for more in ({}, {"details": True}, {"screen": True, "strict": False}):
        lines += [""] + cassandra_import_report(*args, cwd="/p", in_git=True, width=200, **more)
    return cassandra_import_shareable(lines + ["  " + line for line in extra], layout)["text"]


SITE_DIFFERENCES = [
    "logback.xml: /configuration/appender[@name='KWYJIBO_LOG']/file: node has '/srv/kwy/x.log', import"
    ' would write nothing',
    'logback.xml: /configuration/logger[@name="com.frobnitz.ledger"]/@level: node has \'DEBUG\', import'
    ' would write nothing',
    'logback.xml: /configuration/root/appender-ref[@ref="FROBLOG"]: node has <appender-ref'
    ' ref="FROBLOG">, import would write nothing',
    'logback.xml: /configuration/appender[@name="STDOUT"]/encoder/pattern: node has \'%d frobhost %m\','
    ' import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSLOG"]/syslogHost: node has \'loghost.frobcorp.net\','
    ' import would write nothing',
    'logback.xml: /configuration/property[@name="frobDir"]/@value: node has \'/frob/logs\', import would write nothing',
    'logback.xml: /configuration/appender[@class="com.frobnitz.Appender"]: node has <appender'
    ' class="com.frobnitz.Appender">, import would write nothing',
    'logback.xml: /configuration/logger[@name="org.apache.cassandra.frob"]/appender-ref[@ref="QUUXLY"]:'
    ' node has nothing, import would write <appender-ref ref="QUUXLY">',
    'jmxremote.access: quuxuser: node has readwrite, import would write nothing',
    "jmxremote.password: quuxuser: node has '****', import would write nothing",
    'jmxremote.access: monitoring: node has readonly, import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcom.frobnitz.key=quux", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=frobhost.frobcorp.net",'
    ' import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.frobsetting=1", import would write nothing',
    'cassandra-env.sh: node has FROBNITZ_HOME=/opt/frobnitz, import would write nothing',
    'cassandra-env.sh: node has JMX_PORT="${JMX_PORT:-7299}", import would write JMX_PORT=7199',
    'cassandra-env.sh: node has . /opt/frobnitz/env.sh, import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -javaagent:/opt/frobagent/agent.jar=frobhost:9999",'
    ' import would write nothing',
    "jvm17-server.options: -Dcom.frobnitz.x: node has '-Dcom.frobnitz.x=1', import would write nothing",
    'jvm17-server.options: -Djava.security.krb5.realm: node has'
    " '-Djava.security.krb5.realm=FROBCORP.NET', import would write nothing",
    "jvm17-server.options: -XX:HeapDumpPath: node has '-XX:HeapDumpPath=/frobdumps/heap', import would write nothing",
    "jvm17-server.options: -Dcassandra.frobnitz_dir: node has '-Dcassandra.frobnitz_dir=/x', import"
    ' would write nothing',
    "jvm17-server.options: -Xloggc: node has '-Xloggc:/frobgc/gc.log', import would write nothing",
    "jvm17-server.options: -agentpath:/frob/libagent.so: node has '-agentpath:/frob/libagent.so', import"
    ' would write nothing',
    "cassandra.yaml: seed_provider.parameters.seeds: node has '10.0.0.1,frobseed.frobcorp.net', import"
    " would write '10.0.0.1'",
    "cassandra.yaml: cluster_name: node has 'Frobnitz Cluster', import would write 'Zorglub Prod'",
    'cassandra.yaml: data_file_directories: node has \'["/frobdata/d1"]\', import would write nothing',
    "cassandra.yaml: listen_address: node has 'fd12:3456:789a::1', import would write nothing",
    "cassandra.yaml: authenticator: node has 'com.frobnitz.auth.FrobAuth', import would write 'PasswordAuthenticator'",
    "cassandra.yaml: frobnitz_custom_key: node has 'x', import would write nothing",
    "cassandra.yaml: server_encryption_options.keystore: node has '/etc/cassandra/frob-keystore.jks',"
    ' import would write nothing',
    "cassandra-rackdc.properties: dc: node has 'frobdc', import would write 'dc1'",
    "cassandra-rackdc.properties: dc_suffix: node has '_frobsfx', import would write nothing",
    "cassandra-topology.properties: 10.0.0.9: node has 'frobdc:frobrack', import would write nothing",
    "cassandra-topology.properties: fd12::9: node has 'dc1:r1', import would write nothing",
    "cassandra.service: [Service] Environment FROBNITZ_TOKEN: node has 'x', import would write nothing",
    "cassandra.service: [Service] User: node has 'frobuser', import would write 'cassandra'",
    "cassandra.service: [Service] EnvironmentFile: node has '/etc/frob/env', import would write nothing",
    "cassandra.service: [Unit] Description: node has 'Frobnitz Cassandra', import would write nothing",
    'cassandra_linux: sysctl net.ipv4.frob (/etc/sysctl.d/90-frobnitz.conf): node has 1, the roles would set 2',
    'cassandra_linux: read_ahead_kb frobvg-frobdata (live): node has 128, the roles would set 8',
    'cassandra_linux: /etc/security/limits.d/frobnitz.conf: node has x, the roles would set y',
    'cassandra_linux: limits frobuser nofile: node has 1, the roles would set 2',
    'cassandra_linux: lvm frobvg/frobdata: node has x, the roles would set y',
    'cassandra_firewall: port 7000 from frobnet.frobcorp.net: node has closed, the roles would set open',
    'cassandra_firewall: port 7000 from 2001:db8:abcd::/48: node has closed, the roles would set open',
    'cassandra_firewall: zone frobzone: node has x, the roles would set y',
    "medusa.ini: [storage] bucket_name: node has 'frobbucket', import would write nothing",
    "medusa.ini: [storage] host: node has 'minio.frobcorp.net', import would write nothing",
    "medusa.ini: [cassandra] nodetool_username: node has 'frobnt', import would write nothing",
    "medusa.ini: [storage] key_file: node has '/etc/medusa/frob-cred.json', import would write nothing",
    "medusa.ini: [frobsection] frobkey: node has 'x', import would write nothing",
    "heap (cassandra-env.sh, else the unit's Environment): MAX_HEAP_SIZE: node has '8G', import would write '16G'",
    "jvm17-server.options: -Xlog: node has '-Xlog:gc*:file=/frobgc/gc.log:time', import would write nothing",
    "jvm17-server.options: -Xbootclasspath: node has '-Xbootclasspath/a:/frobjar/x.jar', import would write nothing",
    'jvm17-server.options: -Xrunjdwp: node has'
    " '-Xrunjdwp:transport=dt_socket,address=frobhost.frobcorp.net:8000', import would write nothing",
    "jvm17-server.options: -Xshare: node has '-Xshare:auto', import would write nothing",
    "jvm17-server.options: -Xss: node has '-Xss512k', import would write '-Xss256k'",
    'logback.xml: /configuration/logger[@name="Frobnitz Ledger"]/@level: node has \'DEBUG\', import would'
    ' write nothing',
    'logback.xml: /configuration/logger[@name="Frobnitz / Quuxly"]/@level: node has \'DEBUG\', import'
    ' would write nothing',
    'logback.xml: /configuration/appender[@name="frobsink"]/file: node has \'x\', import would write nothing',
    'logback.xml: /configuration/appender[@name="FROBSINK (2)"]/file: node has \'x\', import would write nothing',
    'logback.xml: /configuration/logger[@name="kwyjibo"]: node has <logger level="DEBUG"'
    ' name="kwyjibo">, import would write nothing',
    'logback.xml: /configuration/logger[@name="paris.orders"]: node has <logger level="DEBUG"'
    ' name="paris.orders">, import would write nothing',
    'logback.xml: /configuration/appender[@name="ASYNC"]/appender-ref[@ref="frob sink"]: node has'
    ' nothing, import would write <appender-ref ref="frob sink">',
    "logback.xml: /configuration/frobTag/@frobattr: node has '1', import would write nothing",
    'jmxremote.access: frob user: node has readwrite, import would write nothing',
    'jmxremote.access: london: node has readwrite, import would write nothing',
    "jmxremote.password: frobuser.frobcorp: node has '****', import would write nothing",
    'cassandra_linux: LVM read-ahead vg_frob/lv_kwy (sectors) (lvchange, Cassandra stopped): node has'
    ' 256, the roles would set 16',
    'cassandra_linux: read_ahead_kb vg_q7-lv_z9 (dm-3) (live): node has 128, the roles would set 8',
    'cassandra_linux: IO scheduler frobdisk (sdb) (live): node has mq-deadline, the roles would set none',
    'cassandra_linux: sysctl vm.swappiness (/etc/sysctl.d/99-zq7.conf): node has 10, the roles would set 1',
    'cassandra_linux: /etc/security/limits.d/zq7-limits.conf: node has x, the roles would set y',
    'cassandra_linux: chrony server line: node has server ntp1.frobcorp.net iburst, the roles would set commented out',
    'cassandra_linux: chrony server line: node has server 10.1.2.3 iburst, the roles would set commented out',
    'cassandra_linux: time sync chronyd: node has inactive, the roles would set active',
    'cassandra_linux: tuned profiles: node has frobtune, the roles would set frobtune cassandra-disks'
    ' (applied again, Cassandra stopped)',
    'cassandra_firewall: firewall rule (not carried): node has port 7000: 10.9.0.0/16,'
    ' frobgw.frobcorp.net, the roles would set not written by the roles',
    'cassandra_firewall: port 7199 from fe80::1%eth0: node has closed, the roles would set open',
    'cassandra_firewall: port 7199 from 2001:db8::/32: node has closed, the roles would set open',
    'cassandra_firewall: port 7199 from frobgw: node has closed, the roles would set open',
    'cassandra_linux: /etc/udev/rules.d/60-frobdisk.rules: node has present, the roles would set removed',
    'cassandra_linux: swap in /etc/fstab: node has /dev/mapper/vg_frob-swap none swap sw 0 0, the roles'
    ' would set removed',
    'cassandra_linux: data devices: node has set by cassandra_data_block_device(s), the roles would set x',
    "medusa.ini: [storage] bucket_name: node has 'acme-backups', import would write 'zorglub-bk'",
    'medusa.ini: [storage] bucket_name: node has acmebk, import would write nothing',
    "medusa.ini: [storage] prefix: node has 'acme/prod', import would write nothing",
    "medusa.ini: [storage] fqdn: node has 'n1.acmecorp.example', import would write nothing",
    "medusa.ini: [storage] host: node has 's3.acmecorp.example', import would write nothing",
    "medusa.ini: [storage] region: node has 'acme-region-9', import would write 'us-east-1'",
    "medusa.ini: [storage] base_path: node has '/srv/acmebk', import would write nothing",
    "medusa.ini: [cassandra] cql_username: node has 'acmecql', import would write 'cassandra'",
    'medusa.ini: [cassandra] nodetool_username: node has "acme ops", import would write nothing',
    "medusa.ini: [storage] api_profile: node has 'acmeprof', import would write nothing",
    "medusa.ini: [storage] storage_class: node has 'STANDARD_IA', import would write nothing",
    "medusa.ini: [ssh] username: node has 'acmessh', import would write nothing",
    "medusa.ini: [storage] key_file: node has '/etc/medusa/acme.json', import would write nothing",
    "medusa.ini: [grpc] enabled: node has 'acme', import would write nothing",
    "medusa.ini: [monitoring] monitoring_provider: node has 'acmemon', import would write nothing",
    "medusa.ini: [storage] bucket_name: node has 'acme backups, zorglub', import would write nothing",
    'jmxremote.access: acmeops: node has readwrite, import would write nothing',
    'jmxremote.access: Cassandra: node has readwrite, import would write nothing',
    'jmxremote.access: admin: node has readwrite, import would write nothing',
    'jmxremote.access: data: node has readwrite, import would write nothing',
    "jmxremote.password: acme.ops: node has '****', import would write nothing",
    "jmxremote.access: zorglub-ops: node has 'readwrite \\\n create javax.management.monitor.*', import"
    ' would write nothing',
    "cassandra.yaml: memtable.configurations.acmefast.class_name: node has 'TrieMemtable', import would write nothing",
    "cassandra.yaml: memtable.configurations.acmefast.inherits: node has 'trie', import would write nothing",
    "cassandra.yaml: memtable.configurations.trie.class_name: node has 'TrieMemtable', import would write nothing",
    'cassandra.yaml: memtable.configurations: node has {acmefast: {class_name: TrieMemtable}}, import'
    ' would write nothing',
    "cassandra.yaml: seed_provider[0].parameters[0].acmeseeds: node has 'x', import would write nothing",
    "cassandra.yaml: table_properties_acme: node has 'x', import would write nothing",
    "cassandra.yaml: data_file_directories[0]: node has '/srv/acme/data', import would write '/var/lib/cassandra/data'",
    "cassandra.yaml: client_encryption_options.keystore_password: node has '****', import would write nothing",
    "cassandra.yaml: authenticator.parameters.acme_realm: node has 'ACME.EXAMPLE', import would write nothing",
    "cassandra.yaml: cluster_name: node has 'acmeprod', import would write 'Zorglub Prod'",
    "cassandra.yaml: role_manager: node has 'com.acme.cass.Roles', import would write 'CassandraRoleManager'",
    "jvm17-server.options: -Xmx: node has '-Xmx8G', import would write nothing",
    "jvm17-server.options: -Xlog: node has '-Xlog:gc*:file=/srv/acmegc/gc.log', import would write nothing",
    "jvm17-server.options: -Dorg.apache.cassandra.acme: node has '-Dorg.apache.cassandra.acme=1', import"
    ' would write nothing',
    "jvm17-server.options: -Dsun.rmi.acmething: node has '-Dsun.rmi.acmething=1', import would write nothing",
    'jvm17-server.options: -Djava.security.auth.login.config: node has'
    " '-Djava.security.auth.login.config=/srv/acme/jaas.conf', import would write nothing",
    "jvm17-server.options: -XX:OnOutOfMemoryError: node has '-XX:OnOutOfMemoryError=/srv/acme/oom.sh"
    " acmehost', import would write nothing",
    "jvm17-server.options: -XX:HeapDumpPath: node has '-XX:HeapDumpPath=${ACME_DUMPS}/heap', import"
    ' would write nothing',
    'jvm17-server.options: -Dcassandra.config: node has'
    " '-Dcassandra.config=file:///srv/acme/cassandra.yaml', import would write nothing",
    'jvm17-server.options: -Dcassandra.jmx.remote.login.config: node has'
    " '-Dcassandra.jmx.remote.login.config=AcmeLogin', import would write nothing",
    'jvm17-server.options: -Djava.rmi.server.hostname: node has'
    " '-Djava.rmi.server.hostname=acmehost.acme.example', import would write nothing",
    'jvm17-server.options: -Dlogback.configurationFile: node has'
    " '-Dlogback.configurationFile=/srv/acme/lb.xml', import would write nothing",
    "jvm17-server.options: -Dlogback.acmeprop: node has '-Dlogback.acmeprop=1', import would write nothing",
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dlogback.acme=1", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dsun.acmeprop=zorglub", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -javaagent:${ACME_HOME}/lib/agent.jar", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dx=${ACME_HOME}/acmedir", import would write nothing',
    'cassandra-env.sh: node has . ${ACME_HOME}/acme-env.sh, import would write nothing',
    'cassandra-env.sh: node has export ACMEVAR=1, import would write nothing',
    'logback.xml: /configuration/appender[@name="ACME_AUDIT"]/file: node has'
    " '${cassandra.logdir}/acme.log', import would write nothing",
    'logback.xml: /configuration/logger[@name="com.acme.orders"]/@level: node has \'DEBUG\', import would'
    ' write nothing',
    'logback.xml: /configuration/logger[@name="acme orders"]/@level: node has \'DEBUG\', import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'/srv/acmelogs/system.log\','
    ' import would write nothing',
    'logback.xml: /configuration/appender[@name="ASYNC"]/appender-ref[@ref="ACMESINK"]: node has'
    ' nothing, import would write <appender-ref ref="ACMESINK">',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/filter[@class="com.acme.Filter"]: node has'
    ' nothing, import would write nothing',
    'logback.xml: /configuration/property[@name="acmeDir"]/@value: node has \'/srv/acme\', import would write nothing',
    'logback.xml: /configuration/appender[@name="logback"]/file: node has \'x\', import would write nothing',
    'logback.xml: /configuration/appender[@name="n1"]/file: node has \'x\', import would write nothing',
    'logback.xml: /configuration/appender[@name="zorglub-db1"]/file: node has \'x\', import would write nothing',
    'cassandra_linux: /etc/udev/rules.d/60-acme.rules: node has present, the roles would set removed',
    'cassandra_linux: /etc/udev/rules.d/acme-disks.rules: node has present, the roles would set removed',
    'cassandra_linux: read_ahead_kb acmevg-acmelv (live): node has 128, the roles would set 8',
    'cassandra_linux: IO scheduler acme_disk/x (sdb) (live): node has mq-deadline, the roles would set none',
    "cassandra.service: [Service] Environment ACME_TOKEN: node has 'x', import would write nothing",
    "cassandra.service: [Service] Environment JVM_EXTRA_OPTS: node has '-Dacme.flag=1"
    " -javaagent:/srv/acme/a.jar', import would write nothing",
    "cassandra.service: [Service] User: node has 'acmesvc', import would write 'cassandra'",
    "cassandra.service: [Service] Group: node has 'acmegrp', import would write 'cassandra'",
    "cassandra.service: [Service] ExecStartPre: node has '/usr/local/bin/acme-pre.sh', import would write nothing",
    "cassandra.service: [Service] WorkingDirectory: node has '/home/acmeuser', import would write nothing",
    "cassandra.yaml: seed_provider.parameters.seeds: node has 'cassandra.acmecorp.example', import would write 'IP'",
    "cassandra.yaml: listen_address: node has 'sun.acmecorp.example', import would write nothing",
    "medusa.ini: [storage] host: node has 'java.acmecorp.example', import would write nothing",
    "cassandra.yaml: rpc_address: node has 'community.acmecorp.example', import would write nothing",
    "cassandra.yaml: broadcast_address: node has 'jdk.acmecorp.example', import would write nothing",
    "cassandra.yaml: authenticator: node has 'org.apache.cassandra.auth.AcmeAuthenticator', import would"
    " write 'PasswordAuthenticator'",
    'logback.xml: /configuration/logger[@name="org.apache.cassandra.acmeplugin"]/@level: node has'
    " 'DEBUG', import would write nothing",
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has'
    " '${cassandra.logdir}/payments/prod/system.log', import would write '${cassandra.logdir}/system.log'",
    "cassandra.yaml: hints_directory: node has '${HOME}/billing/hints', import would write nothing",
    "cassandra.yaml: saved_caches_directory: node has '$CASSANDRA_HOME/orders/caches', import would write nothing",
    "jvm17-server.options: -XX:HeapDumpPath: node has '-XX:HeapDumpPath=${DUMPS}/ledger/team', import"
    ' would write nothing',
    'jmxremote.access: operator: node has readwrite, import would write nothing',
    'medusa.ini: [cassandra] cql_username: node has \'operator\', import would write \'cassandra\'',
]

# (round 5) paths of the site after a variable, a default, under a stock directory, relative, Windows'
SITE_DIFFERENCES += [
    "jvm17-server.options: -XX:HeapDumpPath: node has '-XX:HeapDumpPath=${DUMPS:-/srv/orders/billing}', import would"
    " write nothing",
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dx=${ROOT:-/data/payments/metrics}", import would write nothing',
    "cassandra.yaml: hints_directory: node has '~cassandra/orders/hints', import would write nothing",
    "cassandra.yaml: hints_directory: node has '$HOME/../orders', import would write nothing",
    "cassandra.yaml: cdc_raw_directory: node has 'data/orders/cdc', import would write nothing",
    "cassandra.yaml: cdc_raw_directory: node has 'C:\\\\orders\\\\cdc', import would write nothing",
    "cassandra.yaml: saved_caches_directory: node has '/var/lib/cassandra/orders/caches', import would write nothing",
    "cassandra.yaml: saved_caches_directory: node has '/var/lib/cassandra/../../srv/orders', import would write nothing",
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${cassandra.logdir}/system.log.orders\','
    " import would write nothing",
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has'
    " '${cassandra.logdir}/gc.log.billing-events', import would write nothing",
    "jmxremote.access: operator: node has readwrite, import would write nothing",
    "medusa.ini: [storage] bucket_name: node has 'operator', import would write nothing"]


# (round 6) paths with a default, after '-' or '//', relative values, file names, separators in a component
SITE_DIFFERENCES += [
    'cassandra.yaml: cdc_raw_directory: node has data/orders/cdc, import would write nothing',
    "cassandra.yaml: hints_directory: node has 'data/orders/hints extra', import would write nothing",
    "jvm17-server.options: -Xlog:gc: node has '-Xlog:gc:file=logs/orders-gc.log', import would write nothing",
    "jvm17-server.options: -XX:HeapDumpPath: node has '-XX:HeapDumpPath=dumps/billing', import would write nothing",
    'jvm17-server.options: -XX:HeapDumpPath: node has -XX:HeapDumpPath=/var/lib/cassandra/billing/paris, import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=${ROOT:=/srv/orders/billing}", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=${ROOT-/srv/orders/billing}", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=$ROOT/orders/billing", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=${ROOT}orders/billing", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=$(dirname $0)/../orders", import would write nothing',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.x=`pwd`/orders/billing", import would write nothing',
    "cassandra.yaml: data_file_directories: node has ['/var/lib/cassandra/orders', '/var/lib/cassandra/billing'], import would write nothing",
    'cassandra.yaml: data_file_directories: node has ["data/orders", "data/billing"], import would write nothing',
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/commitlog/paris-1', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/commitlog/london', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/prod_orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/team.billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/2024orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/orders,billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/orders:billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/orders=billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/orders#billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/orders~billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/ordérs/billing', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/opt/cassandra/../orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/./orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var//lib/orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '//srv/orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has 'file:/srv/orders', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra-orders/x', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/etc/cassandra/conf.billing/x', import would write nothing",
    "cassandra.yaml: server_encryption_options.keystore: node has 'conf/billing.keystore', import would write nothing",
    'cassandra.yaml: server_encryption_options.keystore: node has conf/billing.keystore, import would write nothing',
    "cassandra.yaml: server_encryption_options.keystore: node has '/etc/cassandra/ssl/billing.jks', import would write nothing",
    "cassandra.yaml: server_encryption_options.keystore: node has '/etc/cassandra/ssl/paris.jks', import would write nothing",
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${cassandra.logdir}/system.log.%d{orders}.zip\', import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${cassandra.logdir}/orders/system.log\', import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${CASSANDRA_LOG_DIR}/billing.log\', import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${cassandra.logdir:-/srv/orders}/system.log\', import would write nothing',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has \'${cassandra.logdir}/../orders/system.log\', import would write nothing',
    "cassandra.service: [Service] EnvironmentFile: node has '-/srv/orders/cassandra.env', import would write nothing",
    "cassandra.service: [Service] ExecStartPre: node has '-/opt/billing/bin/prep.sh', import would write nothing",
]

SITE_LINES = [
    '  see cassandra.acmecorp.example and sun.acmecorp.example, logback.acmecorp.example',
    '  -Dsun.acmecorp=1 -Dio.netty.acmeflag=2 -Dcom.google.acmeprop=3 -Dcom.datastax.acmething=4',
    'check ${cassandra.logdir}/payments/orders.log and /srv/payments/orders.log',
]

STOCK_DIFFERENCES = [
    'logback.xml: /configuration/appender[@name="ASYNCDEBUGLOG"]/queueSize: node has \'1024\', import'
    " would write '2048'",
    'logback.xml: /configuration/appender[@name="DEBUGLOG"]/encoder/pattern: node has \'%-5level'
    " [%thread] %date{ISO8601} %F:%L - %msg%n', import would write nothing",
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/file: node has'
    " '${cassandra.logdir}/system.log', import would write '${cassandra.logdir}/system2.log'",
    'logback.xml: /configuration/appender[@name="STDOUT"]: node has <appender'
    ' class="ch.qos.logback.core.ConsoleAppender" name="STDOUT">, import would write nothing',
    'logback.xml: /configuration/logger[@name="org.apache.cassandra"]/@level: node has \'DEBUG\', import'
    " would write 'INFO'",
    'logback.xml: /configuration/logger[@name="com.thinkaurelius.thrift"]: node has <logger'
    ' level="ERROR" name="com.thinkaurelius.thrift">, import would write nothing',
    'logback.xml: /configuration/root/appender-ref[@ref="ASYNCDEBUGLOG"]: node has nothing, import would'
    ' write <appender-ref ref="ASYNCDEBUGLOG">',
    'logback.xml: /configuration/appender[@name="SYSTEMLOG"]/filter/level: node has \'INFO\', import would'
    " write 'WARN'",
    'logback.xml: /configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern: node has'
    " '${cassandra.logdir}/audit/audit.log.%d{yyyy-MM-dd}.%i.zip', import would write nothing",
    "jvm17-server.options: -XX:+UseG1GC: node has '-XX:+UseG1GC', import would write nothing",
    'jvm17-server.options: G1RSetUpdatingPauseTimePercent: node has'
    " '-XX:G1RSetUpdatingPauseTimePercent=5', import would write nothing",
    'jvm17-server.options: -Dcassandra.max_queued_native_transport_requests: node has'
    " '-Dcassandra.max_queued_native_transport_requests=4096', import would write nothing",
    "jvm17-server.options: -Djdk.attach.allowAttachSelf: node has '-Djdk.attach.allowAttachSelf=true',"
    ' import would write nothing',
    'jvm17-server.options: -Dio.netty.tryReflectionSetAccessible: node has'
    " '-Dio.netty.tryReflectionSetAccessible=true', import would write nothing",
    "jvm17-server.options: -Djava.net.preferIPv4Stack: node has '-Djava.net.preferIPv4Stack=true',"
    ' import would write nothing',
    'jvm17-server.options: (options without a name, in order): node has ["-ea", "-da:net.openhft..."],'
    ' import would write ["-ea"]',
    'jvm17-server.options: -Dcom.datastax.driver.FORCE_NIO: node has'
    " '-Dcom.datastax.driver.FORCE_NIO=true', import would write nothing",
    'jvm17-server.options: -Dnet.openhft.chronicle.something: node has'
    " '-Dnet.openhft.chronicle.something=1', import would write nothing",
    "cassandra.yaml: commitlog_directory: node has '/var/lib/cassandra/commitlog', import would write"
    " '/var/lib/cassandra/cl'",
    "cassandra.yaml: endpoint_snitch: node has 'GossipingPropertyFileSnitch', import would write 'Ec2Snitch'",
    'cassandra.yaml: seed_provider[0].class_name: node has'
    " 'org.apache.cassandra.locator.SimpleSeedProvider', import would write nothing",
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=true",'
    ' import would write nothing',
    'cassandra-env.sh: node has LOCAL_JMX=no, import would write LOCAL_JMX=yes',
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<public name>", import'
    ' would write nothing',
    "cassandra.service: [Service] LimitMEMLOCK: node has 'infinity', import would write nothing",
    'cassandra.service: [Service] Environment CASSANDRA_INCLUDE: node has'
    " '/etc/cassandra/cassandra.in.sh', import would write nothing",
    'cassandra_linux: sysctl vm.max_map_count (/etc/sysctl.d/60-cassandra.conf): node has 65530, the'
    ' roles would set 1048575',
    'cassandra_linux: transparent hugepages: node has always, the roles would set never',
    'cassandra_linux: limits cassandra memlock (/etc/security/limits.d/cassandra.conf): node has 1, the'
    ' roles would set unlimited',
    'cassandra_linux: read_ahead_kb sdb (live): node has 128, the roles would set 8',
    'cassandra_linux: read_ahead_kb nvme0n1 (live): node has 128, the roles would set 8',
    'cassandra_linux: scheduler dm-3 (live): node has mq-deadline, the roles would set none',
    'cassandra_firewall: port 9042/tcp: node has closed, the roles would set open',
    'jmxremote.access: monitorRole: node has readonly, import would write nothing',
    'jmxremote.access: controlRole: node has readwrite, import would write nothing',
    "medusa.ini: [storage] storage_provider: node has 's3', import would write 'local'",
    "medusa.ini: [cassandra] config_file: node has '/etc/cassandra/cassandra.yaml', import would write nothing",
    "medusa.ini: [storage] region: node has 'us-east-1', import would write nothing",
    "cassandra.service: [Service] ExecStartPre: node has '/usr/bin/true', import would write nothing",
    'cassandra_linux: transparent hugepages: node has always, the roles would set never',
]

# what must stay readable of STOCK_DIFFERENCES (and of STOCK_LINES)
STOCK_DIFFERENCES += [
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -javaagent:$CASSANDRA_HOME/lib/jamm-0.3.2.jar", import would write'
    " nothing",
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -XX:CompileCommandFile=$CASSANDRA_CONF/hotspot_compiler", import'
    " would write nothing",
    'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Xloggc:${CASSANDRA_LOG_DIR}/gc.log", import would write nothing',
    "cassandra.yaml: back_pressure_strategy[0].parameters[0].high_ratio: node has '0.90', import would write '0.95'",
    "cassandra.yaml: crypto_provider[0].parameters[0].fail_on_missing_provider: node has 'true', import would write"
    " 'false'",
    "cassandra.yaml: transparent_data_encryption_options.key_provider[0].parameters[0].keystore: node has"
    " 'conf/.keystore', import would write nothing",
    "cassandra.yaml: saved_caches_directory: node has '/var/lib/cassandra/saved_caches', import would write nothing"]
STOCK_LINES = ["-Dsun.rmi.server.x=1 -Dlogback.configurationFile=logback.xml -Djava.rmi.server.hostname=x",
               "${cassandra.logdir}/system.log ${cassandra.logdir}/audit/audit.log.%d{yyyy-MM-dd}.%i.zip",
               "/etc/udev/rules.d/64-cassandra-data-disk.rules"]
READABLE = ["$CASSANDRA_HOME/lib/jamm-0.3.2.jar", "$CASSANDRA_CONF/hotspot_compiler", "${CASSANDRA_LOG_DIR}/gc.log",
            "parameters[0].high_ratio", "parameters[0].fail_on_missing_provider",
            "parameters[0].keystore: node has 'conf/.keystore'", "/var/lib/cassandra/saved_caches",
            "ASYNCDEBUGLOG / queueSize", "DEBUGLOG / encoder / pattern", "org.apache.cassandra / @level",
            "com.thinkaurelius.thrift", "root / appender-ref ASYNCDEBUGLOG", "AUDIT / rollingPolicy / fileNamePattern",
            "-XX:+UseG1GC", "-Dcassandra.max_queued_native_transport_requests", "-Djdk.attach.allowAttachSelf",
            "-Dio.netty.tryReflectionSetAccessible", "-Djava.net.preferIPv4Stack", "-Dcom.datastax.driver.FORCE_NIO",
            "commitlog_directory", "endpoint_snitch", "GossipingPropertyFileSnitch", "seed_provider[0].class_name",
            "org.apache.cassandra.locator.SimpleSeedProvider", "LOCAL_JMX", "[Service] LimitMEMLOCK",
            "[Service] Environment CASSANDRA_INCLUDE", "[Service] ExecStartPre", "transparent hugepages",
            "sysctl vm.max_map_count (/etc/sysctl.d/60-cassandra.conf)", "read_ahead_kb sdb (live)",
            "scheduler dm-3 (live)", "port 9042/tcp", "monitorRole", "controlRole", "[storage] storage_provider",
            "us-east-1", "-Dsun.rmi.server.x=1", "-Dlogback.configurationFile=logback.xml",
            "-Djava.rmi.server.hostname=x", "${cassandra.logdir}/system.log",
            "${cassandra.logdir}/audit/audit.log.%d{yyyy-MM-dd}.%i.zip",
            "/etc/udev/rules.d/64-cassandra-data-disk.rules", "$CASSANDRA_HOME/lib/jamm-0.3.2.jar",
            "$CASSANDRA_CONF/hotspot_compiler", "${CASSANDRA_LOG_DIR}/gc.log", "parameters[0].high_ratio",
            "parameters[0].fail_on_missing_provider", "parameters[0].keystore: node has 'conf/.keystore'",
            "/var/lib/cassandra/saved_caches"]


def test_no_name_of_the_site_left():
    text = shared(SITE_DIFFERENCES, SITE_LINES)
    left = []
    for line in text:
        for kept in KEPT_BY_DECISION:
            line = line.replace(kept, "")
        if SITE.search(line):
            left.append(line)
    assert left == []


@pytest.mark.parametrize("readable", READABLE)
def test_the_stock_names_stay_readable(readable):
    assert [line for line in STOCK_TEXT if readable in line], readable


STOCK_TEXT = shared(STOCK_DIFFERENCES, STOCK_LINES)


@pytest.mark.parametrize("kind", ["cassandra_medusa_storage_prefix", "cassandra_medusa_storage_bucket_name",
                                  "cassandra_medusa_cql_username"])
def test_a_value_named_as_the_cluster_leaves_the_cluster_masked(kind):
    # (round 5) a Medusa prefix, bucket or user named as the cluster (metrics): the cluster's name masked everywhere
    nodes = [{"name": "db%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": True,
              "hand_edits": [], "normalized": [], "notes": [], "vars": {"cassandra_version": "50x", kind: "metrics"}}
             for i in (1, 2)]
    layout = cassandra_inventory_layout(nodes, "metrics")
    checks = {"db1": {"differences": ["medusa.ini: [storage] prefix: node has 'metrics', import would write nothing",
                                      "cassandra.yaml: cluster_name: node has 'Other', import would write 'metrics'"]}}
    args = (layout, ["/p/inventories/x.yml", "/p/inventories"], "/p/reports/x/report.txt", checks, False)
    lines = cassandra_import_report(*args, cwd="/p", in_git=True, width=200)
    lines += cassandra_import_report(*args, cwd="/p", in_git=True, width=200, details=True)
    text = cassandra_import_shareable(lines, layout)["text"]
    assert not [line for line in text if re.search(r"(?i)\bmetrics\b", line)]


# (round 6) a stock OS's and JDK's paths as they are
STOCK_PATHS = [
    '/dev/mapper/vg_data-swap',
    '/usr/lib/jvm/java-11-openjdk-amd64/bin/java',
    '/etc/alternatives/java',
    '/usr/share/cassandra/lib/jamm-0.3.2.jar',
    '/etc/security/limits.d/cassandra.conf',
    '/etc/systemd/system/cassandra.service.d/override.conf',
    '/sys/block/sda/queue/read_ahead_kb',
    '/proc/sys/vm/max_map_count',
    '/proc/sys/vm/swappiness',
    '/etc/default/cassandra',
    '/usr/sbin/cassandra',
    '/opt/cassandra/bin/nodetool',
    '/etc/apt/sources.list.d/cassandra.sources.list',
    '/etc/yum.repos.d/cassandra.repo',
    '/sys/kernel/mm/transparent_hugepage/defrag',
    '/sys/kernel/mm/transparent_hugepage/enabled',
    '/var/run/cassandra/cassandra.pid',
    '/usr/lib/jvm/temurin-17-jdk-amd64',
    '/usr/lib/jvm/java-17-amazon-corretto',
    '/etc/pki/tls/certs/ca-bundle.crt',
    '/etc/ssl/certs/java/cacerts',
    '/usr/lib/jvm/java-17-openjdk-17.0.9.0.9-2.el8.x86_64/bin/java',
    '/usr/lib/systemd/system/cassandra.service',
    '/etc/systemd/system/multi-user.target.wants/cassandra.service',
    '/usr/share/cassandra/cassandra.in.sh',
    '/etc/cassandra/conf/cassandra-env.sh',
    '/etc/cassandra/default.conf/cassandra.yaml',
    '/etc/cassandra/jvm11-server.options',
    '/usr/share/java/jna.jar',
    '/var/lib/cassandra/data/system_auth/roles-5bc52802de2535edaeab188eecebb090',
    '/etc/medusa/medusa.ini',
    '/etc/medusa/credentials',
    '/usr/local/bin/medusa',
    '/etc/udev/rules.d/60-scheduler.rules',
    '/etc/rc.d/rc.local',
    '/sys/block/nvme0n1/queue/scheduler',
    '/sys/class/block/sdb/queue/rotational',
    '/etc/tuned/cassandra/tuned.conf',
    '/usr/lib/tuned/throughput-performance/tuned.conf',
    '/etc/fstab',
    '/etc/hosts',
    '/etc/os-release',
    '/usr/bin/python3',
    '/usr/libexec/platform-python',
    '/etc/environment',
    '/etc/profile.d/java.sh',
    '/etc/sysconfig/cassandra',
    '/etc/cassandra/cassandra-topology.properties',
    '/etc/cassandra/jmxremote.password',
    '/usr/lib/jvm/jre/lib/management/jmxremote.access',
    '/etc/security/limits.conf',
    '/tmp/hsperfdata_cassandra',
    '/var/log/cassandra/gc.log.0.current',
    '/var/log/cassandra/debug.log.1.zip',
    '/usr/share/cassandra/lib/sigar-bin/libsigar-amd64-linux.so',
]


def test_the_stock_paths_stay_as_they_are():
    text = shared([], ["%s and %s" % (a, b) for a, b in zip(STOCK_PATHS[::2], STOCK_PATHS[1::2] + [""])])
    for path in STOCK_PATHS:
        assert [line for line in text if path in line], path


def test_the_way_out_commands_and_the_reports_words_stay_intact():
    # (round 6) a word of a stock path masked there only: the To-pass option, "2 nodes" never touched
    text = "\n".join(shared(SITE_DIFFERENCES, SITE_LINES))
    assert "-e import_keep_hand_edits=" in text
    option = re.search(r"-e import_keep_hand_edits=(\S+)", text).group(1)
    assert set(option.split(",")) <= {"os", "sysctl", "limits", "swap", "timesync", "disks", "unit", "firewall",
                                      "cassandra-env.sh", "logback.xml", "cassandra.yaml", "jvm17-server.options"}, option
    assert not re.search(r"\b\d+ word\d+", text)


# (round 7) the report's own lines, a stock install's: as they are
REPORT_LINES = [
    "    jvm17-server.options: -Xlog: node has '-Xlog:gc=info,heap*=trace,age*=debug,safepoint=info,promotion*=trace"
    ":file=/var/log/cassandra/gc.log:time,uptime,pid,tid,level:filecount=10,filesize=10485760', import would write"
    " nothing",
    "      + -Xlog:gc*:file=/var/log/cassandra/gc.log:time,uptime:filecount=10,filesize=10m",
    "    see /etc/cassandra/jvm.options:12 and /etc/cassandra/cassandra.yaml:44",
    "    /proc/sys/vm/swappiness=1",
    "    keep the JMX password/access files, iptables/nftables rules, true/false, read/write, and/or, start/stop",
    "    -e import_keep_hand_edits=sysctl,limits,swap,timesync,disks,firewall",
    "    the system java (/usr/bin/java); /usr/lib/jvm/java-17-openjdk-amd64/bin/java",
    "    gc.log.0.current  multi-user.target.wants  system.log.1.zip  debug.log",
    "    logs/system.log data/data conf/cassandra.yaml bin/nodetool tools/bin/cassandra-stress lib/jamm-0.3.2.jar",
    # (round 8)
    "    roles/cassandra_config/templates/cassandra.yaml.j2 roles/cassandra_linux/defaults/main.yml",
    "    /proc/sys/fs/file-max /sys/block/sda/queue/nr_requests pylib/cqlshlib doc/cql3",
    "    '/etc/cassandra/cassandra.yaml: line 4'",
    "    cron: '/usr/bin/nodetool repair -pr -full'",
    "    2 nodes in dc1/r1; read 2/2 nodes"]


def test_the_reports_own_lines_as_they_are():
    nodes = [{"name": "zorglub-db%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1", "read": True,
              "hand_edits": [], "normalized": [], "notes": [], "vars": {"cassandra_version": "50x"}} for i in (1, 2)]
    text = cassandra_import_shareable(REPORT_LINES, cassandra_inventory_layout(nodes, "Zorglub Prod"))["text"][2:]
    assert text == REPORT_LINES


def test_the_sites_names_in_relative_paths_and_two_letter_words():
    # (round 7) a node's, a cluster's, a data center's, a rack's name in a relative path: their pseudonyms; a two
    # letter word of the site under a stock directory masked
    nodes = [{"name": "pay-db%d.acme.io" % i, "address": "10.0.0.%d" % i, "dc": "eu-west", "rack": "rack_a1",
              "read": True, "hand_edits": [], "normalized": [], "notes": [], "vars": {"cassandra_version": "50x"}}
             for i in (1, 2)]
    lines = ["    cassandra.yaml: hints_directory: node has 'data/pay-db1/hints', import would write nothing",
             "    cassandra.yaml: hints_directory: node has data/pay-db1.acme.io/hints, import would write nothing",
             "    -XX:HeapDumpPath=dumps/eu-west/rack_a1 and data/pay_prod_eu/cdc",
             "    keystore conf/pay-db1.acme.io.jks, file=logs/pay-db1.log",
             "    commitlog_directory: node has '/var/lib/cassandra/cl', saved_caches: '/var/lib/cassandra/eu/fr'",
             "    commitlog_directory: node has '/var/lib/cassandra/Orders Billing', '/var/lib/cassandra/orders:billing'"]
    text = "\n".join(cassandra_import_shareable(lines, cassandra_inventory_layout(nodes, "Pay Prod EU"))["text"][2:])
    assert not re.search(r"(?i)pay|acme|west|rack_a|/cl'|/eu/|/fr'|orders|billing", text), text
    for name in ("data/n1/hints", "dumps/dc1/r1", "data/c1/cdc", "conf/n1.jks", "logs/n1.log"):
        assert name in text, name


@pytest.mark.parametrize("cluster", ["Acme Payments", "Zorglub Prod", "Frobnitz Ledger EU"])
def test_the_report_and_inventory_paths_carry_the_clusters_pseudonym(cluster):
    # (round 7) reports/<cluster>/report.txt, -i inventories/<cluster>.yml: the cluster's name whole, as c1
    nodes = [{"name": "frob-db%d" % i, "address": "10.0.0.%d" % i, "dc": "paris", "rack": "rackA", "read": True,
              "hand_edits": ["cassandra-env.sh, line 3:", "  + x"], "normalized": [], "notes": [],
              "vars": {"cassandra_version": "50x"}} for i in (1, 2)]
    layout = cassandra_inventory_layout(nodes, cluster)
    slug = re.sub(r"\W+", "_", cluster.lower())
    checks = {"frob-db1": {"differences": ["logback.xml: /configuration/root/@level: node has 'DEBUG', import would"
                                           " write 'INFO'"]}}
    lines = cassandra_import_self_check_advice(layout, checks, report_file="/p/reports/%s/report.txt" % slug, cwd="/p",
                                               width=200, inventory="inventories/%s.yml" % slug, hosts=slug)
    text = "\n".join(cassandra_import_shareable(lines, layout)["text"])
    words = [w for w in re.split(r"\W+", cluster.lower()) if len(w) > 1]
    assert not [w for w in words + ["frob", "paris", "racka"] if re.search(r"(?i)(?<![a-z])%s" % w, text)], text


# (round 8) a password on a command line, quoted or not: never in the copy, never in its key
COMMANDS = ['    cron: "/usr/bin/nodetool -u cassandra -pw s3cret snapshot -t daily zorglub_ks"',
            "    cron: /usr/bin/nodetool -u cassandra -pw s3cret repair",
            "    cron: '/usr/bin/nodetool --password s3cret cleanup'",
            '    cron: "cqlsh -u cassandra -p s3cret -e x"',
            "    cron: '/usr/bin/nodetool -h zorglub-db1.acme.example repair -pr -full'"]


def test_a_password_on_a_command_line_never_shown_its_subcommands_readable():
    nodes = [{"name": "zorglub-db%d.acme.example" % i, "address": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1",
              "read": True, "hand_edits": [], "normalized": [], "notes": [], "vars": {"cassandra_version": "50x"}}
             for i in (1, 2)]
    shared = cassandra_import_shareable(COMMANDS, cassandra_inventory_layout(nodes, "Zorglub Prod"))
    assert "s3cret" not in "\n".join(shared["text"] + shared["map"])
    text = "\n".join(shared["text"])
    for readable in ("-pw **** snapshot -t daily", "-pw **** repair", "--password **** cleanup", "-p **** -e x",
                     "-h n1 repair -pr -full"):
        assert readable in text, readable
    assert "zorglub" not in text
