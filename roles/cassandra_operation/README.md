cassandra_operation
===================

The task files of the collection's playbooks, with the defaults they read:
the preflight checks (the cluster's group, the variables, root on the nodes,
the cluster name, each node's settings) and the cluster health check. Not a
role to run on its own: it has no main tasks, and its task files are not a
stable interface. The playbooks include them with
`include_role: name=community.cassandra.cassandra_operation tasks_from=<file>`.

They read the JMX settings of `cassandra_service`: those defaults are
repeated in `defaults/main.yml`, with the same values (a unit test keeps them
equal).

Variables
---------

Set in the inventory, as any role variable.

* `cassandra_service_health_timeout` (default 600) and
  `cassandra_service_health_force` (default `false`): the cluster health
  check waits that long for down nodes to come back, reading the cluster
  every `cassandra_stream_early_check_interval` seconds (default 10) during
  the first `cassandra_stream_early_time` seconds (default 300), then every
  `cassandra_stream_check_interval` seconds (default 30), a progress line
  each time; `true` goes on anyway (risky: a second replica down can make
  data unavailable). `health_check` reads once and waits for nothing.
* `cassandra_node_state` (default `present`): `absent` (a host var, or a
  group var) takes the host out of the cluster: the playbooks leave it out,
  and say so when the ring still has it.
* `cassandra_suspicious_ack` (default `[]`): the rows of the preflight's
  sensitive or suspicious settings a node keeps on purpose, their WARNING
  left out (`auto_bootstrap_false`, `replace_address`, `heap_size`,
  `allow_all_authenticator`, `failure_policy_ignore`).

License
-------

BSD
