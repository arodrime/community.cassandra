# shellcheck shell=sh
# What runs on this node when it is not Apache Cassandra: DataStax Enterprise or ScyllaDB (their own
# packages, services and configuration files), refused by import_cluster. POSIX sh, read by the shell
# tasks of the playbook (nothing is started: dse -v and scylla --version are scripts and a binary).
# Prints distribution=cassandra|dse|scylla|<empty> and distribution_version=<version or empty>.
dist=
dver=
for p in $(pgrep -x java); do
  # a running Apache Cassandra wins over the files of a DSE it was migrated from
  tr '\0' '\n' < "/proc/$p/cmdline" | grep -qx org.apache.cassandra.service.CassandraDaemon && { dist=cassandra; break; }
  tr '\0' '\n' < "/proc/$p/cmdline" | grep -qE '^(com\.datastax\.bdp\.DseModule|-Ddse\.)' && { dist=dse; break; }
done
# no process: the distribution is what is installed (its package or its unit), not a directory or a command
# left by a migration (a stopped Apache node keeps its usual handling)
installed() { rpm -q "$1" >/dev/null 2>&1 || dpkg -s "$1" >/dev/null 2>&1 || systemctl cat "$2" >/dev/null 2>&1; }
if [ "$dist" != cassandra ]; then
  if [ "$dist" = dse ] || installed dse-full dse.service || installed dse dse.service; then
    dist=dse
    dver=$(dse -v 2>/dev/null | head -1)
  elif pgrep -x scylla >/dev/null 2>&1 || installed scylla-server scylla-server.service; then
    dist=scylla
    dver=$(scylla --version 2>/dev/null | head -1)
  fi
fi
echo "distribution=$dist"
echo "distribution_version=$dver"
