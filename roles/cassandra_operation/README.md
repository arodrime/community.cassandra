cassandra_operation
===================

The task files of the collection's playbooks, with the defaults they read:
the cluster health checks, the rolling operations node by node
(`node_operation.yml` and its `action_*.yml`), the checks of new nodes, the
confirmation screens, the progress files, the streaming and cleanup waits.
Not a role to run on its own: it has no main tasks, and its task files are
not a stable interface. The playbooks include them with
`include_role: name=community.cassandra.cassandra_operation tasks_from=<file>`.

They run `cassandra_service` (start, restart, the wait for `NORMAL`) and
`cassandra_config` (the config, the running JVM) through their own task
files. The defaults of `cassandra_service` they read are
repeated in `defaults/main.yml`, with the same values (a unit test keeps them
equal).

Variables
---------

Set in the inventory, as any role variable.

* Streaming operations (the bootstrap of `add_node` and `replace_node`,
  `decommission_node`, `remove_dead_node`, the rebuild of `add_datacenter`,
  `move_node`, `topology`) and cleanups are waited for as long as they make
  progress: `nodetool netstats` (`compactionstats` for a cleanup) is read
  every `cassandra_stream_early_check_interval` seconds (default 10) during
  the first `cassandra_stream_early_time` seconds (default 300), then every
  `cassandra_stream_check_interval` seconds (default 30), and the progress
  printed, one line per check and the other nodes in columns under it, e.g.

  ```
  [1/2] node5 bootstrap  JOINING  [####------]  40%  168.2/420.0 GiB  82 MiB/s  ETA 14:27 (52m)  35m
        from node1  88.1/220.0 GiB  40%           from node2  52.1/130.0 GiB  40%
        from node3   28.0/70.0 GiB  40% STALLED
  ```

  Above 30 other ends (a node streaming both ways counts twice), one line
  sums them up (done, ok and their average, stalled, behind, the 3 slowest)
  and only the stalled ones and the ones behind are detailed, 12 at most.

  The times are the controller's. After a minute without progress the line
  says it, as progress (`no progress for 3m`); after
  `cassandra_stream_stall_warn_time` seconds (default 300) it says
  `STALLED 6m/30m` (the time without progress and the limit); once done, a
  single line with the total time.

  The run fails only after `cassandra_stream_stall_time` seconds (default
  1800) with nothing streamed: no byte or file, no session started or
  ended. Nothing is stopped then. Entire-SSTable streaming (4.0+) counts a
  file only once whole: on a node that receives (bootstrap, replace,
  rebuild), each check while bytes are left also reads the space used on
  its data and commitlog filesystems (`df`), and its growth is progress too
  (`receiving (disk +3.2 GiB in 5m)` while netstats stays still), from 64 MiB
  a check (heavy writes on that node meanwhile can hide a stall:
  `cassandra_stream_max_time` bounds the wait). A
  decommission or a move goes by netstats only (the nodes that receive take
  client load, their disk says little): with very big SSTables, raise
  `cassandra_stream_stall_warn_time` and `cassandra_stream_stall_time`. No overall limit unless
  `cassandra_stream_max_time` (seconds) is set. A JMX login or permission
  refused stops the wait at once, naming the node (on a node being added,
  Cassandra's own JMX authenticator refuses every login until it has joined:
  its bootstrap is then followed from the other nodes). The bootstrap wait also stops
  when Cassandra stops or, on 5.0, when the bootstrap fails
  (`Mode: JOINING_FAILED`). On 5.0.0 to 5.0.4 nodetool does not answer on a
  bootstrapping node (CASSANDRA-19902): its progress is read from the other
  nodes' side (their sending sessions to it). On 4.0 and 4.1 a failed
  bootstrap leaves the node JOINING with no stream: the wait then ends as a
  stall, see `system.log` (`nodetool bootstrap resume` retries it).
* `cassandra_add_node_cleanup` (default `none`): after `add_node`, the
  cleanup of the nodes that handed data over, with the words of the
  `cleanup` playbook's `cassandra_cleanup_mode`. `none` prints the command,
  `sequential` runs it one node at a time (`one` is the same), `rack` a rack
  at a time, `dc` a datacenter at a time, `all` every node at once (heavy I/O).
* One token per node (`cassandra_num_tokens: 1`): `cassandra_token_auto`
  (default `false`), where `add_node` puts new nodes without
  `cassandra_initial_token`: `bisect` (no node moves), `balanced` (even ring,
  `move_node` moves the others afterwards) or `true` (shows both, asks);
  `cassandra_token_rf` (default 3), the replication factor the shares shown
  assume when no keyspace gives one; `cassandra_token_allow_partial` (default
  `false`), `create_cluster` with tokens on only some nodes of a datacenter;
  `move_node`: `cassandra_move_tokens` (default `{}`: even out each
  datacenter), `cassandra_move_cleanup` (as `cassandra_add_node_cleanup`),
  `cassandra_move_min_free_percent` (default 20) and
  `cassandra_move_force_disk` (default `false`).

* `cassandra_new_node_checks`: `add_node` and `replace_node` check the new
  hosts before installing anything (root access, the Cassandra directories
  mounted where fstab or a systemd mount unit expects and empty, free space
  against the load of the other nodes, the Pythons of cqlsh and Medusa, the
  packages or the sources they come from, the seeds' ports, the host's own
  ports free, no Cassandra running) and stop with every problem found.
  Default `true`; `false` skips them.
* `cassandra_add_node_reset`: `add_node` (and `topology`) empty first a new
  node that has data but is not in the ring (started once by mistake, a
  failed bootstrap): `tasks/reset_node.yml`, as the `reset_node` playbook,
  what its directories hold deleted, shown and confirmed. Only when
  Cassandra is down there, no up node sees it in its ring, its cluster is
  this one or the stock `Test Cluster`, and it has no user keyspace but a
  failed bootstrap's of this cluster (one `add_node` started: its mark in
  `/var/lib/cassandra` until the node joined); else refused with what it
  holds. A node of this cluster with its keyspaces and no such mark is a
  former member (removed with `remove_dead_node`, put back in the
  inventory): the writes only it holds would be lost, so `auto` (the
  default) refuses it too, saying so; `true`, given on purpose, resets it.
  `false` refuses every new node with data. Default `auto`.
* `cassandra_replace_node_reset`: the same for a replacement host that still
  holds data in `replace_node`, typically a host replacing itself (its
  address only seen down, as the node being replaced). Default `false`.
* `cassandra_create_cluster_reset`: `create_cluster` rebuilds a cluster that
  runs already, all its data lost (another snitch, datacenters or racks):
  refused unless the group is the whole ring; the operator types the cluster
  name, or gives it in `cassandra_create_cluster_reset_confirm` for a run
  without a terminal. Default `false`.
* `cassandra_new_node_min_free_gb`: minimum free space (GiB) on each data
  directory's file system of a new host. Default `0` (none).
* `cassandra_new_node_allow_kept_setup`: the checks refuse a new host with no
  Cassandra installed that still has the host_vars `import_cluster` wrote for
  the node that had its name (`cassandra_imported_host: true` with
  `cassandra_linux_manage`, `cassandra_cqlsh_python_manage` or
  `cassandra_service_unit_manage` false): the roles would leave it half set
  up. Those switches set by the operator (no marker) are not refused. `true`
  accepts it, when the host is set up another way. Default `false`.
* `cassandra_service_health_timeout` (default 600) and
  `cassandra_service_health_force` (default `false`): the cluster health
  check before and after each node waits that long for down nodes to come
  back; `true` goes on anyway (risky: a second replica down can make data
  unavailable).
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
* The clocks: `cassandra_clock_max_offset_ms` (50), `cassandra_clock_step_ms`
  (400), `cassandra_clock_sync_timeout` (180), `cassandra_clock_check`
  (`true`).
* `cassandra_cql_username` / `cassandra_cql_password`: CQL access for the
  checks that read the schema, when authentication is on.

License
-------

BSD
