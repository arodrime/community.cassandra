# shellcheck shell=sh
# What the cassandra_axonops role reads on the node, one "<key> <value>" line each: the AxonOps packages
# ("package <name> <state> <version>"), whether the cassandra package is installed, whether Cassandra's account ($CASSANDRA_USER) and axonops exist
# (account_<name> yes|no), their groups, the axon-agent service, whether the Java agent's options file is there,
# the sha1, owner, group and mode of /etc/axonops/axon-agent.yml.
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W -f='package ${Package} ${db:Status-Status} ${Version}\n' 'axon-*' 2>/dev/null
  [ "$(dpkg-query -W -f='${db:Status-Status}' cassandra 2>/dev/null)" = installed ] && c=yes || c=no
else
  rpm -qa --qf 'package %{NAME} installed %{VERSION}-%{RELEASE}\n' 'axon-*' 2>/dev/null
  rpm -q cassandra >/dev/null 2>&1 && c=yes || c=no
fi
echo "cassandra_installed $c"
for u in "$CASSANDRA_USER" axonops; do
  if getent passwd "$u" >/dev/null; then echo "account_$u yes"; else echo "account_$u no"; fi
done
echo "user_groups $(id -nG "$CASSANDRA_USER" 2>/dev/null)"
echo "axonops_groups $(id -nG axonops 2>/dev/null)"
echo "enabled $(systemctl is-enabled axon-agent 2>/dev/null)"
echo "active $(systemctl is-active axon-agent 2>/dev/null)"
if [ -f /usr/share/axonops/axonops-jvm.options ]; then echo "options yes"; else echo "options no"; fi
f=/etc/axonops/axon-agent.yml
if [ -f $f ]; then
  echo "config $(sha1sum < $f | cut -c1-40)"
  echo "config_stat $(stat -c '%U %G %a' $f)"
fi
exit 0
