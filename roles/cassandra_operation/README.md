cassandra_operation
===================

The task files of the collection's playbooks, with the defaults they read:
the preflight checks (the cluster's group, the variables, root on the nodes,
the cluster name, each node's settings), the cluster health check, the
rolling operations node by node (`node_operation.yml` and its
`action_*.yml`), the confirmation screens and the progress files. Not a
role to run on its own: it has no main tasks, and its task files are not a
stable interface. The playbooks include them with
`include_role: name=community.cassandra.cassandra_operation tasks_from=<file>`.

They run `cassandra_service` (start, restart, the wait for `NORMAL`) through
its own task files, and read its JMX, unit and wait settings: those defaults are
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
  data unavailable). The rolling operations check it before and after each
  node. `health_check` reads once and waits for nothing.
* `cassandra_node_state` (default `present`): `absent` (a host var, or a
  group var) takes the host out of the cluster: the playbooks leave it out,
  and say so when the ring still has it.
* `cassandra_suspicious_ack` (default `[]`): the rows of the preflight's
  sensitive or suspicious settings a node keeps on purpose, their WARNING
  left out (`auto_bootstrap_false`, `replace_address`, `heap_size`,
  `allow_all_authenticator`, `failure_policy_ignore`).
* `cassandra_operation_confirm` (default `true`): the operations that ask
  for a confirmation ask; `false` goes on without asking (`--check` never
  asks). `cassandra_operation_drain` (default `true`): drain a node before
  stopping it (always done on a node whose own unit is kept).
* `cassandra_rolling_resume` (default `false`): resume an interrupted rolling
  operation from its progress file, kept in `cassandra_rolling_progress_dir`
  (default: `.cassandra_progress` next to the inventory) for
  `cassandra_rolling_progress_days` (30).
* The `upgrade` playbook's `sstables` phase (`nodetool upgradesstables`, one
  node at a time): `cassandra_upgrade_sstables_jobs` (default 2, nodetool's;
  0: every compaction thread), `cassandra_upgrade_sstables_timeout` (default
  86400 seconds per node).
* One token per node (`cassandra_num_tokens: 1`), `create_cluster`:
  `cassandra_token_rf` (default 3), the replication factor the shares shown
  assume when no keyspace gives one; `cassandra_token_allow_partial` (default
  `false`), tokens on only some nodes of a datacenter.
* The clocks (`sync_clocks`, and the check before `create_cluster` starts
  nodes): `cassandra_clock_max_offset_ms` (50), `cassandra_clock_step_ms`
  (400), `cassandra_clock_sync_timeout` (180), `cassandra_clock_check`
  (`true`).
* `cassandra_cql_username` / `cassandra_cql_password`: CQL access for the
  checks that read the schema (the keyspaces' replication before a rack goes
  down), when authentication is on.

License
-------

BSD
