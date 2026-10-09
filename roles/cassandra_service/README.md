cassandra_service
=================

Runs Cassandra under a native systemd unit, starts and enables it, then
waits until the node has joined (`nodetool netstats` reports `Mode: NORMAL`).

The deb and rpm packages only ship a SysV init script. Wrapped by systemd,
it reports `active (exited)` even when the JVM dies right after start, and
sets its limits with `ulimit`. The unit installed here
(`/etc/systemd/system/cassandra.service`, same name so it takes precedence
over the generated one) runs `cassandra -f` as the `cassandra` user, with
the limits below. It is not restarted automatically when it dies
(`Restart=no`): Cassandra's disk/commit failure policies and OOM handling
stop it on purpose, and bringing a failing node back into the cluster, or
looping on the same failure, is worse than an alert.

Requirements
------------

Root on the hosts: the role does not ask for it itself, apply it in a play
with `become: true` (the collection's playbooks do).

Role Variables
--------------

* `cassandra_service_state`: `started` (default), `stopped`, `restarted`.
* `cassandra_service_enabled`: start at boot. Default `true`.
* `cassandra_service_restart_on_change`: restart a running node when the
  unit changes. Default `false`: restarting is a cluster operation, do it
  node by node yourself.
* `cassandra_user` / `cassandra_group` (default `cassandra`): the account
  Cassandra runs as (the unit's `User=` and `Group=`). The same variables give
  `cassandra_config`'s files their group and its directories and JMX users'
  files their owner: set them once for both roles.
  `cassandra_service_user` / `cassandra_service_group` override them for the
  unit and `cassandra_config`'s directories and JMX users' files, not for the
  config files' group. Before writing the unit (and in `preflight`, before any
  operation), the role refuses an account that could not read the config
  files `cassandra_config` writes (their owner, group and mode from its
  variables, the account's groups from `id`), or that does not exist. With a
  kept unit (`cassandra_service_unit_manage: false`), its own `User=` and
  `Group=` are checked. A changed `User=`
  or `Group=` takes effect at the next restart (`apply_config` restarts the
  nodes whose unit changed since Cassandra started).
* `cassandra_service_unit_manage`: `false` keeps the node's own unit (or the
  package's init script) instead of writing the role's: the unit variables
  have no effect on it, and the playbooks drain the node with `nodetool`
  before stopping it (such a unit may not drain). The `import_cluster`
  playbook sets it on the nodes it finds started another way. Default `true`.
* `cassandra_service_restart`: systemd `Restart=`. Default `no`; with
  `on-failure`, at most `cassandra_service_start_limit_burst` (3) restarts
  per `cassandra_service_start_limit_interval` (1800 s).
* `cassandra_service_limit_nofile` (1048576), `_nproc` (32768),
  `_memlock` (infinity), `_as` (infinity).
* `cassandra_service_timeout_stop`: seconds before SIGKILL. Default 180.
* `cassandra_service_drain_on_stop`: run `nodetool drain` before the JVM
  gets SIGTERM, so a plain `systemctl stop cassandra` (OS patching, reboot)
  is clean. Default `true`. The drain never blocks the stop: after
  `cassandra_service_drain_timeout` (120 s, kept 10 s below
  `cassandra_service_timeout_stop`) or on error, the JVM is stopped anyway.
  With JMX authentication, it needs `cassandra_jmx_username` and
  `cassandra_jmx_password_file` (the unit file is world-readable, so an inline
  `cassandra_jmx_password` is never written there: the drain then fails and
  the node is stopped without it).
* `cassandra_service_tasks_max`: systemd `TasksMax=`. Default `infinity`:
  systemd's own default (15% of `pid_max`) can be low enough to make a busy
  node fail with "unable to create native thread".
* `cassandra_service_environment`: extra `Environment=` entries, e.g.
  `{JAVA_HOME: /usr/lib/jvm/java-17-openjdk}`.
* `cassandra_service_wait_for_normal` (default `true`) and
  `cassandra_service_wait_timeout` (seconds, default 600).
* Streaming operations (the bootstrap of `add_node` and `replace_node`,
  `decommission_node`, `remove_dead_node`, the rebuild of `add_datacenter`,
  `move_node`, `topology`) and cleanups are waited for as long as they make
  progress: `nodetool netstats` (`compactionstats` for a cleanup) is read
  every `cassandra_stream_early_check_interval` seconds (default 10) during
  the first `cassandra_stream_early_time` seconds (default 300), then every
  `cassandra_stream_check_interval` seconds (default 30), and the progress
  printed, one line per check and one line for the other nodes, e.g.

  ```
  [1/2] node5 bootstrap  JOINING  [####------]  40%  168.2/420.0 GiB  82 MiB/s  ETA 14:27 (52m)  35m
        from node1 88.1/220.0 GiB ok   from node2 52.1/130.0 GiB ok   from node3 28.0/70.0 GiB stalled
  ```

  The times are the controller's. After a minute without progress the line
  says it (`STALLED 6m/15m`: the time without progress and the limit); once
  done, a single line with the total time.

  The run fails only after `cassandra_stream_stall_time` seconds (default
  900) with nothing streamed: no byte or file, no session started or
  ended; 4 times as long while nothing is left to transfer (before the first
  session, index or view builds after the streams). Nothing is stopped then.
  Entire-SSTable streaming (4.0+) counts a file only once whole: with very
  big SSTables, raise `cassandra_stream_stall_time`. No overall limit unless
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

* `cassandra_service_allow_new_seed`: a node that never started and is
  listed in `cassandra_seeds` is refused when another seed already answers
  (seeds don't bootstrap: it would join without its data). Default `false`.
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
  failed bootstrap's of this cluster; else refused with what it holds.
  `false` refuses every new node with data. Default `true`.
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

Config changes made by `cassandra_config` never restart the node either.

A unit the init script left `active (exited)` with no Cassandra running is
reset first, so the node really starts. A Cassandra the init script started
and that still runs is left alone (stopping it would be a restart): the role
says so, and the native unit takes over at the next restart.

One node at a time
------------------

The role manages the node it runs on: start it, wait until it has joined
(`Mode: NORMAL`). Ordering several nodes is the playbook's job: new nodes
must join one at a time, and a new cluster starts its seeds first.

    # new cluster: seeds first, then the others, one at a time
    - hosts: cassandra
      become: true
      serial: 1
      roles:
        - community.cassandra.cassandra_service

    ansible-playbook site.yml --limit dc1-node1,dc2-node1   # the seeds
    ansible-playbook site.yml                               # the others

Example Playbook
----------------

    - hosts: cassandra
      become: true
      serial: 1
      roles:
        - community.cassandra.cassandra_repository
        - community.cassandra.cassandra_install
        - community.cassandra.cassandra_linux
        - community.cassandra.cassandra_config
        - community.cassandra.cassandra_firewall
        - community.cassandra.cassandra_service

License
-------

BSD
