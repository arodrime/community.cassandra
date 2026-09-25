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
* `cassandra_service_restart`: systemd `Restart=`. Default `no`; with
  `on-failure`, at most `cassandra_service_start_limit_burst` (3) restarts
  per `cassandra_service_start_limit_interval` (1800 s).
* `cassandra_service_limit_nofile` (1048576), `_nproc` (32768),
  `_memlock` (infinity), `_as` (infinity).
* `cassandra_service_timeout_stop`: seconds before SIGKILL. Default 180.
* `cassandra_service_environment`: extra `Environment=` entries, e.g.
  `{JAVA_HOME: /usr/lib/jvm/java-17-openjdk}`.
* `cassandra_service_wait_for_normal` (default `true`) and
  `cassandra_service_wait_timeout` (seconds, default 600).

Config changes made by `cassandra_config` never restart the node either.

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
