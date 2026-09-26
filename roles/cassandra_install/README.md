cassandra_install
=================

Installs Apache Cassandra from the repository set up by `cassandra_repository`,
with the Java version the series is built for.

On Debian/Ubuntu, the package would start Cassandra with its stock config as
soon as it is installed; a temporary `policy-rc.d` prevents that, so the node
only starts once it is configured.

Role Variables
--------------

* `cassandra_version`: Cassandra series, same values as `cassandra_repository`
  (`40x`, `41x`, `50x`). Default `50x`.
* `cassandra_package_version`: exact Cassandra version (e.g. `5.0.4`), so
  every node, including the ones added later, runs the same one. Empty
  (default) installs the repository's latest. An installed node is never moved
  to another version by the role (that is an upgrade); on Debian and Ubuntu the
  pinned packages are held (`apt-mark hold`).
* `cassandra_install_java` (default `true`): `false` when Java is installed
  by other means (an internal package, the system image); the Cassandra
  package still needs a Java package that satisfies its dependency.
* `cassandra_java_set_default` (default `true`): make `cassandra_java_version`
  the default `java` when several JDKs are installed.
* `cassandra_java_version`: Java installed before Cassandra. Defaults to the
  series' version from `cassandra_java_versions` (11 for 4.x, 17 for 5.0).
* `cassandra_java_package`: package name, derived from the OS and
  `cassandra_java_version`.
* `cassandra_cqlsh_python`: Python used by cqlsh. Empty (default): `python3`,
  unless it is outside the range the series' cqlsh supports
  (`cassandra_cqlsh_python_supported`: 3.6-3.11 for 4.x, 3.8-3.13 for 5.0);
  then `python3.11` is installed next to it and cqlsh is pointed at it
  (`/usr/local/bin/cqlsh` wrapper, `CQLSH_PYTHON` in `/etc/profile.d`).
  The system `python3` is never changed.
* `cassandra_cqlsh_python_repo_uri`: where python3.11 comes from on Ubuntu
  releases that don't ship it (default: the deadsnakes PPA, signed by the key
  shipped in `files/deadsnakes.asc`; empty to rely on the configured
  repositories).

jemalloc is installed when available (Debian/Ubuntu, and RHEL-family with EPEL
or Amazon Linux), and `cassandra-tools` on RHEL-family systems.

Example Playbook
----------------

    - hosts: cassandra
      roles:
        - community.cassandra.cassandra_repository
        - community.cassandra.cassandra_install

License
-------

BSD
