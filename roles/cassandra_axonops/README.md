cassandra_axonops
=================

Installs the [AxonOps](https://axonops.com) agent on a Cassandra node:
`axon-agent` and the AxonOps Java agent package built for the node's
Cassandra series and Java, writes `/etc/axonops/axon-agent.yml`, and keeps
`axon-agent` enabled and running.

| Series | Java 8 | Java 11 | Java 17 |
|---|---|---|---|
| 4.0 | axon-cassandra4.0-agent-jdk8 | axon-cassandra4.0-agent | - |
| 4.1 | axon-cassandra4.1-agent-jdk8 | axon-cassandra4.1-agent | - |
| 5.0 | - | axon-cassandra5.0-agent-jdk11 | axon-cassandra5.0-agent-jdk17 |

The package is picked from `cassandra_version` and `cassandra_java_version`
(`cassandra_axonops_java_agent_package` overrides it). A Java agent package
of another series or Java found on the node is removed (on a Java change).

The Java agent runs inside Cassandra's JVM. Every Java agent package from
1.1.0 on ships `/usr/share/axonops/axonops-jvm.options`, which adds the
`-javaagent` option and, on Java 9 and later, the module flags it needs.
With `cassandra_axonops_enabled: true`, `cassandra_config` sources that file
at the end of `cassandra-env.sh` (before your snippet of
`cassandra_config_template_snippets`). It refuses to write that line when the
file is missing (Cassandra would not start), and refuses a second loader in
your snippet or JVM options.

The role never restarts Cassandra. The account Cassandra runs as is added to
the `axonops` group (its Java agent reads `axon-agent.yml` and writes its
queue in `/var/lib/axonops`), and `axonops` to Cassandra's group.
`axon-agent` is restarted when its package or `axon-agent.yml` changes.

The playbooks of the collection apply the role when `cassandra_axonops_enabled`
is `true` (default `false`):

* `create_cluster` on every node, `add_node`, `replace_node` and
  `add_datacenter` on the new nodes;
* `apply_config` on a running cluster: its plan shows what differs (section
  "AxonOps agent", `--check` included) and the restart the new
  `cassandra-env.sh` line needs; at each node's turn, the agent is set up
  first, then the config written, then the node restarted (one at a time).
  A change of the agent alone (versions, `axon-agent.yml`) restarts no
  Cassandra: its Java agent reads them at its next start (said in the plan).
  The repository the role adds is compared too (source, key, login).
* `upgrade`: its preflight lists the change; at each node's turn the
  repository and the target's packages are downloaded before the drain, and
  installed once the target series is (Cassandra stopped), before its config;
* `update_java`: its plan shows the change; at each node's turn the package of
  the new Java replaces the old one once the config is written, before the
  restart.

Turned off (`cassandra_axonops_enabled: false`), the line goes from
`cassandra-env.sh` (a restart); the packages, `axon-agent.yml` and the service
are left as they are.

Requirements
------------

Root on the hosts (`become: true`; the collection's playbooks do). Cassandra
installed (the account it runs as must exist). The AxonOps server is not
managed here: see AxonOps' own collection (`axonops.axonops`).

Role Variables
--------------

The main ones (all of them in `meta/argument_specs.yml`):

* `cassandra_axonops_server_host` (required), `cassandra_axonops_server_port`
  (default `1888`), `cassandra_axonops_org` (required), `cassandra_axonops_key`
  (vault; `""` for none).
* `cassandra_axonops_tls_mode`: `disabled` (default), `TLS` or `mTLS`, with
  `_tls_skip_verify`, `_tls_ca_file`, `_tls_cert_file`, `_tls_key_file`.
* `cassandra_axonops_ntp_hosts`: e.g. `["ntp1.example.com:123"]`.
* `cassandra_axonops_extra_settings`: merged into `axon-agent.yml`.
* `cassandra_axonops_agent_version`, `cassandra_axonops_java_agent_version`:
  `""` installs the newest when absent and never upgrades; a version pins it.
* Where the packages come from:
  * AxonOps' repository by default (`https://packages.axonops.com/apt`, suite
    `axonops-apt`, its signing key; `https://packages.axonops.com/yum`);
  * a mirror: `cassandra_axonops_repository_url`, `_suite`, `_component`,
    `_key_url` (the key the mirror signs with), `_username`, `_password`;
  * a repository the hosts already have:
    `cassandra_axonops_repository_manage: false`;
  * package files on the controller (air-gapped):
    `cassandra_axonops_package_files` (no repository added).

Example
-------

```yaml
# group_vars/orders.yml
cassandra_axonops_enabled: true
cassandra_axonops_server_host: axonops.example.com
cassandra_axonops_org: example
cassandra_axonops_agent_version: "2.0.36"
cassandra_axonops_java_agent_version: "1.1.1"
```

```
ansible-playbook community.cassandra.apply_config --check
ansible-playbook community.cassandra.apply_config
```

Then on a node: `ps -o args= -C java | grep -o 'javaagent:[^ ]*axon[^ ]*'`,
`systemctl is-active axon-agent`, `/var/log/axonops/axon-agent.log`.

License
-------

GPL-3.0-or-later
