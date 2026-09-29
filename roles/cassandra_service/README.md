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

Role Variables
--------------

* `cassandra_service_state`: `started` (default), `stopped`, `restarted`.
* `cassandra_service_enabled`: start at boot. Default `true`.
* `cassandra_service_restart_on_change`: restart a running node when the
  unit changes. Default `false`: restarting is a cluster operation, do it
  node by node yourself.
* `cassandra_service_user` / `cassandra_service_group`: default `cassandra`.
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
  `decommission_node`, `remove_dead_node`, the rebuild of `add_datacenter`)
  and cleanups are waited for as long as they make progress: every
  `cassandra_stream_check_interval` seconds (default 300) `nodetool netstats`
  (`compactionstats` for a cleanup) is read and one line printed, e.g.
  `14:05 [########------------]  41%  290.4/710.2 GiB  38 MiB/s  ETA 3h08 (ends ~17:13)  tables: 12 done, 2 streaming  2 sessions  now: orders.items (from 10.0.0.3)`.
  The run fails only after `cassandra_stream_stall_checks` checks in a row
  (default 3) with nothing streamed: no byte or file, no session started or
  ended; 4 times as many while nothing is left to transfer (before the first
  session, index or view builds after the streams). Nothing is stopped then.
  Entire-SSTable streaming (4.0+) counts a file only once whole: with very
  big SSTables, raise `cassandra_stream_stall_checks`. No overall limit unless
  `cassandra_stream_max_time` (seconds) is set. The bootstrap wait also stops
  when Cassandra stops or, on 5.0, when the bootstrap fails
  (`Mode: JOINING_FAILED`). On 5.0.0 to 5.0.4 nodetool does not answer on a
  bootstrapping node (CASSANDRA-19902): its progress is read from the other
  nodes' side (their sending sessions to it). On 4.0 and 4.1 a failed
  bootstrap leaves the node JOINING with no stream: the wait then ends as a
  stall, see `system.log` (`nodetool bootstrap resume` retries it).
* `cassandra_add_node_cleanup` (default `none`): after `add_node`, the
  cleanup of the nodes that handed data over. `none` prints the command,
  `one` runs it one node at a time, `rack` and `dc` the nodes of a rack, of a
  datacenter together, batch after batch, `all` every node at once.

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
* `cassandra_new_node_min_free_gb`: minimum free space (GiB) on each data
  directory's file system of a new host. Default `0` (none).
* `cassandra_new_node_allow_kept_setup`: the checks refuse a new host with no
  Cassandra installed where `cassandra_linux_manage`,
  `cassandra_cqlsh_python_manage` or `cassandra_service_unit_manage` is
  `false` (e.g. the host_vars `import_cluster` wrote for the node a rebuilt
  host had): the roles would leave it half set up. `true` accepts it, when
  the host is set up another way. Default `false`.

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
      serial: 1
      roles:
        - community.cassandra.cassandra_service

    ansible-playbook site.yml --limit dc1-node1,dc2-node1   # the seeds
    ansible-playbook site.yml                               # the others

Example Playbook
----------------

    - hosts: cassandra
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
