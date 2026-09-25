cassandra_config
================

Templates Cassandra's own config files from real, verified stock defaults,
with every active setting exposed as an Ansible variable.

Covered: `cassandra.yaml`, `cassandra-env.sh`, `jvm-server.options`, the
per-Java `jvm<N>-server.options` (`jvm11`/`jvm17` for 5.0, `jvm8`/`jvm11`
for 4.x), `cassandra-rackdc.properties` and `logback.xml`, from stock Apache
Cassandra 4.0.21, 4.1.12 and 5.0.9 (`conf/` at the matching tags). Templates
live in `templates/<series>/`, picked from `cassandra_version` (`40x`, `41x`,
`50x`).

Why
---

Hand-patching a handful of "known-stable" keys via `lineinfile`/`blockinfile`
(as e.g. `cassandra_install`-adjacent deploy playbooks tend to do) leaves
everything else at whatever the tarball/package happens to ship, and never
covers `jvm*-server.options` or `cassandra-env.sh` at all - usually because
there was no verified-correct copy of those files to template blindly
against.

This role starts from the actual shipped config files for the target
Cassandra version (not reconstructed from memory or docs), keeps every
upstream comment intact, and turns settings into `{{ cassandra_... }}`
variables whose default equals the exact stock value: every active key of
`cassandra.yaml`, and the commonly tuned lines of the other files.
Each template is verified by rendering it with only its defaults and
diffing the result byte-for-byte against the real stock file.

Role Variables
--------------

* `cassandra_version`: Cassandra series, same values as
  `cassandra_repository`. Defaults to `50x`.
* `cassandra_conf_dir`: destination directory for the templated files.
  Defaults to where the package makes Cassandra read its config:
  `/etc/cassandra/conf` on RedHat, `/etc/cassandra` on Debian. Keep the
  default with a package install: the package's `cassandra.in.sh`
  hardcodes that path for Cassandra and its tools, anything else is not read.
* `cassandra_data_dir`: Cassandra's data directory. Defaults to
  `/var/lib/cassandra/data`. This is the canonical definition - the
  `cassandra_linux` role reads the same variable (with no default of its
  own) to auto-detect the block device for read-ahead/IO scheduler tuning.
* `cassandra.yaml`: one variable per active key, named
  `cassandra_<setting name>` (e.g. `cassandra_num_tokens`), see
  `defaults/main.yml`. Keys that only exist in 4.x get their own variables
  (4.0 keeps its pre-4.1 names, e.g. `cassandra_read_request_timeout_in_ms`).
  Where a key's stock value differs by series (the 4.0 cache save periods),
  the default follows `cassandra_version`.
  Four directory settings (`data_file_directories`, `commitlog_directory`,
  `saved_caches_directory`, `hints_directory`) are commented out in stock
  but are deliberately uncommented here and wired to `cassandra_data_dir`,
  `cassandra_commitlog_dir`, `cassandra_saved_caches_dir`,
  `cassandra_hints_dir` - everything else stays byte-for-byte identical to
  stock when only defaults are used.
* `cassandra-env.sh`: `cassandra_log_dir` (defaults to `/var/log/cassandra`,
  as the deb/rpm packages patch it; the tarball's stock value is
  `$CASSANDRA_HOME/logs`), `cassandra_heap_newsize` (4.x only: CMS needs it
  whenever `cassandra_heap_size` is set, the role asserts it),
  `cassandra_heap_size` (empty = stock auto-sizing),
  `cassandra_max_direct_memory_size` and `cassandra_heap_dump_dir` (5.0
  only; on 4.x set `CASSANDRA_HEAPDUMP_DIR` in the service environment),
  `cassandra_local_jmx`, `cassandra_jmx_port`, `cassandra_jmx_rmi_hostname`.
* `jvm<N>-server.options`: 5.0 G1 settings as
  `cassandra_jvm_<option>` (`max_gc_pause_millis`,
  `initiating_heap_occupancy_percent`, `g1_heap_region_size`,
  `g1_new_size_percent`, `max_tenuring_threshold`, `parallel_gc_threads`,
  `conc_gc_threads`), applied to both files; `cassandra_jvm11_<option>` or
  `cassandra_jvm17_<option>` overrides one file. On 4.x (CMS by default)
  only `parallel_gc_threads` and `conc_gc_threads` apply.
  `cassandra_jvm_extra_options` (jvm-server.options),
  `cassandra_jvm8_extra_options`, `cassandra_jvm11_extra_options` and
  `cassandra_jvm17_extra_options` append
  extra lines.
* `cassandra-rackdc.properties`: `cassandra_dc`, `cassandra_rack`,
  `cassandra_prefer_local`.
* `logback.xml`: `cassandra_log_level` (root logger),
  `cassandra_log_level_cassandra` (`org.apache.cassandra` logger),
  `cassandra_debug_log_enabled`.

Testing
-------

The molecule scenario renders every file of every series with defaults
only and checks it line for line against the stock files in
`molecule/default/files/stock-<version>/`, then renders them again with
overrides and checks the changed lines.

Adding a Cassandra version
--------------------------

Templates are generated from the stock files by `tools/gen_templates.py`
(repo root), which fails if a line it replaces is missing or duplicated in
the new version. The 5.0 `cassandra.yaml.j2` is the hand-checked reference;
other series derive theirs from it and print the defaults to add:

    python3 tools/gen_templates.py 5.0 <cassandra-5.0.x>/conf roles/cassandra_config/templates/5.0
    python3 tools/gen_templates.py 4.1 <cassandra-4.1.x>/conf roles/cassandra_config/templates/4.1 <cassandra-5.0.x>/conf/cassandra.yaml

A new series also needs its file list in `vars/main.yml`.

Dependencies
------------

None required. Pairs naturally with `cassandra_install` (package install)
and `cassandra_linux` (OS tuning, shares `cassandra_data_dir`).

Example Playbook
----------------

    - hosts: cassandra
      roles:
        - community.cassandra.cassandra_install
        - community.cassandra.cassandra_linux
        - community.cassandra.cassandra_config

Multi-node, with authentication; per-node values go in host_vars:

    # group_vars/cassandra.yml
    cassandra_cluster_name: "Prod Cluster"
    cassandra_seeds: "10.0.0.1:7000,10.0.0.2:7000"
    cassandra_endpoint_snitch: GossipingPropertyFileSnitch
    cassandra_authenticator: PasswordAuthenticator
    cassandra_authorizer: CassandraAuthorizer
    cassandra_heap_size: 8G

    # host_vars/10.0.0.1.yml
    cassandra_listen_address: 10.0.0.1
    cassandra_rpc_address: 10.0.0.1
    cassandra_dc: dc1
    cassandra_rack: rack1

License
-------

BSD
