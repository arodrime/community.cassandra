# shellcheck shell=sh
# What the cassandra_axonops role reads on the node, one "<key> <value>" line each: the AxonOps packages
# ("package <name> <state> <version>"), the groups of Cassandra's account ($CASSANDRA_USER) and of axonops, the
# axon-agent service, whether the Java agent's options file is there, the sha1, owner, group and mode of
# /etc/axonops/axon-agent.yml.
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W -f='package ${Package} ${db:Status-Status} ${Version}\n' 'axon-*' 2>/dev/null
else
  rpm -qa --qf 'package %{NAME} installed %{VERSION}\n' 'axon-*' 2>/dev/null
fi
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
