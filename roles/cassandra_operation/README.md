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

Call map
--------

For a reader of the code. File names are those of `tasks/` in this role,
unless another role is named.

### Entry points

The task files the playbooks include (`tasks_from`); the others are only
included from these.

| Task file | Included by |
|---|---|
| `defaults.yml` | most operation playbooks, to read this role's defaults |
| `variables_check.yml`, `hosts_check.yml`, `target_nodes_check.yml` | `preflight` and the read-only playbooks (`health_check`, `status`, `start_rack`, `help`, `import_cluster`) |
| `cluster_banner.yml`, `cluster_name.yml`, `root_check.yml`, `note.yml`, `node_facts.yml` | `preflight` (some also `health_check`, `status`, `start_rack`, `topology`) |
| `cluster_health.yml` | `node_operation.yml` before and after each node, the batches, and the playbooks that check the cluster once |
| `screen.yml`, `confirm.yml` | the plan and its question, before an operation changes anything |
| `node_operation.yml` | the rolling operations, one node at a time (below) |
| `restart_batch.yml` | `rolling_restart` by rack |
| `cleanup_batch.yml` | the cleanups of `add_node`, `move_node` and `cleanup` |
| `progress.yml`, `summary.yml` | the progress file of a rolling operation, the summary at its end |
| `new_node_checks.yml`, `new_node_state.yml`, `reset_node.yml`, `reset_node_plan.yml`, `action_add_prepare.yml`, `prepare_lines.yml`, `prepare_reset.yml` | `add_node`, `replace_node`, `create_cluster`, `reset_node`, `topology` |
| `leaving_node_state.yml`, `keyspaces.yml`, `seeds_apply.yml`, `stream_wait.yml` | the playbooks that change the ring |
| `storage_mode_guard.yml`, `storage_mode_read.yml` | the 5.0 restarts (`storage_mode_guard.yml` also from `cassandra_service` and `cassandra_config`) |
| `clocks_check.yml` | `add_node`, `create_cluster` |
| `cluster_clocks.yml` | `health_check` |
| `clocks_read.yml`, `clock_fix.yml` | `sync_clocks` |
| `cql_login.yml` | `import_cluster` |
| `config_pending.yml` | `apply_config` |
| `exporter_check.yml` | `apply_config`, `update_java` |
| `node_standing.yml` | `create_cluster`, `replace_node` |
| `replace_unit_check.yml` | `replace_node` |

### A rolling operation

`node_operation.yml` runs on one node: the cluster health, the action, the
cluster health again. The action is the file named after
`cassandra_service_node_action`:

| Action | Playbooks | What it runs |
|---|---|---|
| `add` | `add_node`, `add_datacenter` | `action_add_prepare.yml` (the roles), `cassandra_service` `main.yml` (start, no wait), `stream_wait.yml` (the bootstrap) |
| `replace` | `replace_node` | `action_add.yml` with the replace flag in the unit, then the unit without it (`cassandra_service` `main.yml`, `unit_no_restart.yml`) |
| `decommission` | `decommission_node`, `remove_datacenter` | `stream_wait.yml`, then `cassandra_service` `main.yml` (stopped, disabled) |
| `move` | `move_node` | `stream_wait.yml` |
| `restart` | `rolling_restart` | `action_restart.yml`: `cassandra_service` `unit.yml`, `drain.yml`, `os_window.yml`, `cassandra_service` `main.yml` (restarted) |
| `reboot` | `rolling_reboot` | `drain.yml`, the reboot, `cassandra_service` `main.yml` |
| `apply_config` | `apply_config` | `action_apply_config.yml`, through `action_restart.yml` when the change needs a restart |
| `sync_clock` | `sync_clocks` | `action_restart.yml`, the clock set while Cassandra is stopped (`_cassandra_service_while_stopped`) |
| `update_java` | `update_java` | `cassandra_install`, `cassandra_config`, `action_restart.yml` |
| `upgrade` | `upgrade` | `drain.yml`, the packages, the config, `cassandra_service` `main.yml` |
| `upgradesstables` | `upgrade` | `nodetool upgradesstables` |

The actions start and restart Cassandra through `cassandra_service`
`main.yml`, with variables that change what it does:

* `cassandra_service_state` (`restarted`, `stopped`), and
  `cassandra_service_wait_for_normal: false` for `add` (the bootstrap is
  waited for by `stream_wait.yml` instead);
* `_cassandra_service_unit_written: true`: `action_restart.yml` wrote the unit
  (and checked the account Cassandra runs as) before the drain;
* `_cassandra_storage_mode_checked: true`: the playbook checked the 5.0
  `storage_compatibility_mode` before its plan, `main.yml` does not again;
* `_cassandra_change_report_quiet`: the change report left to the summary
  (`-v` shows it).

The deepest chain, `add_node` waiting for a bootstrap, is eight files deep
before the wait loop goes round again:

```
playbooks/add_node.yml
└─ node_operation.yml                       one node: health, action, health
   ├─ cluster_health.yml                    before (and after) the action
   └─ action_add.yml
      ├─ action_add_prepare.yml             the roles on the new node
      ├─ cassandra_service: main.yml        start it, no wait for NORMAL
      └─ stream_wait.yml                    the bootstrap, while it makes progress
         └─ cassandra_service: wait_loop.yml
            └─ wait_group.yml
               └─ wait_step.yml
                  └─ stream_check.yml       one read of netstats, one progress line
```

### The wait loop

`cassandra_service` `wait_loop.yml` runs one check again and again until the
wait is over. The caller sets two variables:

* `_cassandra_wait_check`: the task file of one check (a file of
  `cassandra_service`, or a path such as
  `{{ role_path }}/../cassandra_operation/tasks/stream_check.yml`);
* `_cassandra_wait_going`: an expression, read before each check; the check
  that ends the wait sets what makes it false.

Ansible has no loop over tasks until a condition, so the loop includes
itself. Each include nests one level deeper, and Ansible fails past about 150
levels: the checks go by groups. A round is `_cassandra_wait_size` groups of
`_cassandra_wait_size` checks (about 6 hours of checks,
`cassandra_service/vars/main.yml`), and only the next round adds a level:

```
wait_loop.yml          round 1
├─ wait_group.yml      group 1 of N
│  ├─ wait_step.yml    check 1 of N  ->  _cassandra_wait_check
│  └─ ...              check N
├─ ...                 group N
└─ wait_loop.yml       round 2, only while _cassandra_wait_going
```

Once the wait is over, the rest of the round is skipped: at most N checks and
N groups, one skipped task each. The waits that use it:

| Wait | Check | Over when |
|---|---|---|
| `cassandra_service` `wait_normal.yml` | `wait_normal_check.yml` | the node is `NORMAL`, Cassandra stopped, or the timeout |
| `stream_wait.yml` | `stream_check.yml` | the stream is done, failed or stalled |
| `cleanup_wait.yml` | `cleanup_check.yml` | the cleanups are done, failed or stalled |
| `cluster_health.yml` | `cluster_health_wait.yml` | the nodes down are back, or the timeout |

### What topology passes to add_node and decommission_node

`topology` imports the `add_node` and `decommission_node` playbooks for its
steps, with private variables:

* `_cassandra_preflight_skip: true`: their preflight is skipped (`topology`
  ran one for the whole cluster);
* `_cassandra_screen_asked_by: topology`: their plan is not shown and their
  question not asked again (`screen.yml`);
* `_cassandra_notes_deferred: true`: their notes are kept for the plan of
  `topology` (`note.yml`, `new_node_checks.yml`, `reset_node_plan.yml`,
  `leaving_node_state.yml`);
* `_cassandra_ring_extra` (`add_node`): the nodes still in the ring that
  `topology` removes after the adds, for the ring sizes expected;
* `_cassandra_progress_resume: false` (`decommission_node`): no resume from a
  progress file, the plan is worked out again at each run.

and public ones: `cassandra_target_nodes`, `cassandra_new_node_checks: false`
(`topology` checked them), `cassandra_add_node_cleanup: none` and
`cassandra_service_resume_hint`.

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
* `cassandra_dead_node_assassinate_confirm`: `remove_dead_node` with
  `assassinate` asks a second question after the plan's, the operator types
  the dead node's address; a run without a terminal gives it here
  (`cassandra_operation_confirm: false` does not skip it). Default `""`.
* `cassandra_new_node_min_free_gb`: minimum free space (GiB) on each data
  directory's file system of a new host. Default `0` (none).
* `cassandra_new_node_allow_kept_setup`: the checks refuse a new host with no
  Cassandra installed that still has the host_vars `import_cluster` wrote for
  the node that had its name (`cassandra_imported_host: true` with
  `cassandra_linux_manage`, `cassandra_cqlsh_python_manage`,
  `cassandra_service_unit_manage` or `cassandra_firewall_manage` false, or
  `cassandra_import_kept` and the switches of those parts): the roles would
  leave it half set up. Those switches set by the operator (no marker) are not refused. `true`
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
* `cassandra_run_report_dir` (default: `reports/<cluster group>` next to the
  inventory's dir, where `import_cluster` writes its `report.txt`) and
  `cassandra_run_report_keep` (default 20): `apply_config` writes the report
  of each run there (`apply_config-<YYYY-MM-DD-HHMMSS>.txt`,
  `apply_config-check-...` under `--check`), the last ones kept
  (`run_report.yml`).
* `cassandra_storage_mode_running` (5.0, none by default): the
  `storage_compatibility_mode` each node runs, read there by hand
  (`n1:CASSANDRA_4,n2:UPGRADING`), for a node whose mode could not be read
  (no CQL answer, no line logged since its start) and whose `cassandra.yaml`
  changed since that start: a restart that could move it back is refused
  until its mode is known.
* The `upgrade` playbook's `sstables` phase (`nodetool upgradesstables`, one
  node at a time): `cassandra_upgrade_sstables_jobs` (default 2, nodetool's;
  0: every compaction thread), `cassandra_upgrade_sstables_timeout` (default
  86400 seconds per node).
* The clocks: `cassandra_clock_max_offset_ms` (50), `cassandra_clock_step_ms`
  (400), `cassandra_clock_sync_timeout` (180), `cassandra_clock_check`
  (`true`: checked before `create_cluster` and `add_node` start nodes, shown
  by `health_check`).
* `cassandra_cql_username` / `cassandra_cql_password`: CQL access for the
  checks that read the schema, when authentication is on.

License
-------

BSD
