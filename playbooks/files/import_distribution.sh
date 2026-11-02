# What runs on this node when it is not Apache Cassandra: DataStax Enterprise or ScyllaDB (their own
# packages, services and configuration files), refused by import_cluster. POSIX sh, read by the shell
# tasks of the playbook (nothing is started: dse -v and scylla --version are scripts and a binary).
# Prints distribution=cassandra|dse|scylla|<empty> and distribution_version=<version or empty>.
dist=
dver=
for p in $(pgrep -x java); do
  # a running Apache Cassandra wins over the files of a DSE it was migrated from
  tr '\0' '\n' < /proc/$p/cmdline | grep -qx org.apache.cassandra.service.CassandraDaemon && { dist=cassandra; break; }
  tr '\0' '\n' < /proc/$p/cmdline | grep -qE '^(com\.datastax\.bdp\.DseModule|-Ddse\.)' && { dist=dse; break; }
done
if [ "$dist" != cassandra ]; then
  if [ "$dist" = dse ] || command -v dse >/dev/null 2>&1 || [ -d /etc/dse ]; then
    dist=dse
    dver=$(dse -v 2>/dev/null | head -1)
  elif pgrep -x scylla >/dev/null 2>&1 || [ -e /etc/scylla/scylla.yaml ] || command -v scylla >/dev/null 2>&1; then
    dist=scylla
    dver=$(scylla --version 2>/dev/null | head -1)
  fi
fi
echo "distribution=$dist"
echo "distribution_version=$dver"
