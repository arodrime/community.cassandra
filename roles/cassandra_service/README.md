cassandra_service
=================

Runs Cassandra under a native systemd unit, starts and enables it, then
waits until the node has joined (`nodetool netstats` reports `Mode: NORMAL`).

The deb and rpm packages only ship a SysV init script. Where systemd wraps
it (Debian/Ubuntu, RHEL with chkconfig), it reports `active (exited)` even
when the JVM dies right after start; where it does not (e.g. a minimal
RHEL 9), there is no unit at all. The unit installed here
(`/etc/systemd/system/cassandra.service`, same name so it takes precedence
over the generated one) runs `cassandra -f` as `cassandra_user`, with the
limits below. It is not restarted automatically when it dies
(`Restart=no`): Cassandra's `die` disk/commit failure policies and its
OOM handling stop it on purpose, and bringing a failing node back into the
cluster, or looping on the same failure, is worse than an alert. The unit
is for the deb and rpm packages (`/usr/sbin/cassandra`, `/usr/bin/nodetool`).

Role Variables
--------------

* `cassandra_service_state`: `started` (default), `stopped`, `restarted`.
* `cassandra_service_enabled`: start at boot. Default `true`.
* `cassandra_service_restart_on_change`: restart a running node when the
  unit changes. Default `false`: restarting is a cluster operation, do it
  node by node yourself.
* `cassandra_user` / `cassandra_group`: the account and group the service
  runs as, default `cassandra` (created by the packages). The same
  variables as in `cassandra_config`, which gives them the directories and
  files it creates: set them once for both roles.
* `cassandra_service_restart`: systemd `Restart=`. Default `no`; with
  `on-failure`, at most `cassandra_service_start_limit_burst` (3) starts
  per `cassandra_service_start_limit_interval` (1800 s). Manual starts
  count too: past the limit, systemd refuses to start the unit until the
  interval is over (or `systemctl reset-failed cassandra`).
* `cassandra_service_limit_nofile` (1048576), `_nproc` (32768),
  `_memlock` (infinity), `_as` (infinity).
* `cassandra_service_timeout_stop`: seconds systemd waits for the drain,
  then again for the JVM after SIGTERM, before SIGKILL. Default 180.
* `cassandra_service_drain_on_stop`: run `nodetool drain` before the JVM
  gets SIGTERM, so a plain `systemctl stop cassandra` (OS patching, reboot)
  is clean. Default `true`. The drain never blocks the stop: after
  `cassandra_service_drain_timeout` (120 s, kept more than 10 s below
  `cassandra_service_timeout_stop`) or on error, the JVM is stopped anyway.
* `cassandra_service_tasks_max`: systemd `TasksMax=`. Default `infinity`:
  systemd's own default (15% of `pid_max`) can be low enough to make a busy
  node fail with "unable to create native thread".
* `cassandra_service_environment`: extra `Environment=` entries, e.g.
  `{JAVA_HOME: /usr/lib/jvm/java-17-openjdk}`. The unit file is
  world-readable: no secrets there. The unit does not read
  `/etc/default/cassandra`, which the deb init script sources: move what
  is set there to this variable or to `cassandra_config`.
* `cassandra_service_wait_for_normal` (default `true`) and
  `cassandra_service_wait_timeout` (seconds, default 600).
* JMX: the drain and the wait use `cassandra_jmx_port` (from
  `cassandra_config`, default 7199). With JMX authentication, set
  `cassandra_jmx_username` and `cassandra_jmx_password_file`: a file on the
  node, readable by `cassandra_user`, in the `jmxremote.password` format
  (`<user> <password>` lines, nodetool takes the line of the user). The
  `/etc/cassandra/jmxremote.password` that `cassandra_config` writes from
  `cassandra_jmx_users` works as is. An inline `cassandra_jmx_password` is
  only used by the wait, and is visible to `ps` on the node meanwhile: the
  unit file is world-readable, so it is never written there, and the drain
  then fails (the node is stopped without it).

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
