cassandra_install
=================

Installs Apache Cassandra from the repository set up by `cassandra_repository`
(or from package files, `cassandra_install_method: packages`), with the Java
version the series is built for.

On Debian/Ubuntu, the package would start Cassandra with its stock config as
soon as it is installed; a temporary `policy-rc.d` prevents that, so the node
only starts once it is configured.

Role Variables
--------------

* `cassandra_offline`: `true` on air-gapped hosts: nothing is downloaded, Java
  and the Cassandra packages are checked instead of installed, and the role
  stops with the list of what is missing (see the guide's air-gapped section).
  The Python cqlsh may need and jemalloc only give a warning. Default `false`.
* `cassandra_version`: Cassandra series, same values as `cassandra_repository`
  (`40x`, `41x`, `50x`). Default `50x`.
* `cassandra_install_method` (default `repository`): `packages` downloads the
  package files from `cassandra_install_url`, a plain directory of files (no
  repository metadata, e.g. a generic folder of a repository manager), and
  installs them without their Java dependency (`rpm -U --nodeps` with
  procps-ng, python3 and shadow-utils on the RedHat family; `apt-get install`
  of the files on Debian/Ubuntu, Java being a package or the local package of
  a tarball). Only what is not installed in `cassandra_package_version`,
  which is then required, is downloaded; the files are removed after.
  `cassandra_install_package_file` names the files (Apache's names by
  default: `{name}-{version}-1.noarch.rpm`, `{name}_{version}_all.deb`), and
  `cassandra_install_checksums` can give their checksums by file name
  (`{"cassandra-5.0.7-1.noarch.rpm": "sha256:..."}`). Same variable as in
  `cassandra_repository`; `tarball` is not supported yet.
* `cassandra_install_url`, `cassandra_install_username`,
  `cassandra_install_password`: the repository or the directory of the files,
  and its credentials (default to `cassandra_repository_rpm_url` /
  `cassandra_repository_deb_url`, `cassandra_repository_username`,
  `cassandra_repository_password`).
* `cassandra_package_version`: exact Cassandra version (e.g. `5.0.4`), so
  every node, including the ones added later, runs the same one. Empty
  (default) installs the repository's latest. An installed node is never moved
  to another version by the role (that is an upgrade); on Debian and Ubuntu the
  pinned packages are held (`apt-mark hold`), unless `cassandra_package_hold`
  is `false` (a hold already there is never removed).
* `cassandra_packages` (default `cassandra`): package or list of packages;
  `cassandra-tools` is added unless `cassandra_install_tools` is `false`.
* `cassandra_install_jemalloc` (default `true`): install jemalloc when
  available, which `bin/cassandra` preloads.
  `import_cluster` sets `cassandra_install_tools`, `cassandra_install_jemalloc`
  and `cassandra_package_hold` to `false` for the nodes without them.
* `cassandra_java_tarball` (default `""`): Java from a tarball (a JDK or JRE
  `.tar.gz`) instead of a package: a URL downloaded by the
  nodes (`cassandra_java_tarball_checksum` recommended, credentials in
  `cassandra_java_tarball_username`/`_password`), or a file on the controller.
  It is unpacked into `cassandra_java_tarball_dir/<tarball name>` (default
  `/opt/cassandra-java`) and made the system `java`, which the cassandra script
  and nodetool run. The Cassandra packages are then installed without a Java
  package: on Debian/Ubuntu a local `cassandra-java-tarball` package provides
  the Java they depend on; on the RedHat family they are installed with
  `rpm --nodeps` (plus procps-ng, python3 and shadow-utils).
  `cassandra_java_version` must still name its major version; `update_java`
  moves the nodes to a new tarball.
* `cassandra_java_home` (default `""`): Java already unpacked in this
  directory by other means, not a package: made the system `java`, and the
  Cassandra packages installed without a Java package, as with a tarball.
  `import_cluster` sets it for nodes whose running Java is not a package.
* `cassandra_install_java` (default `true`): `false` when Java is installed
  by other means (an internal package, the system image); the Cassandra
  package still needs a Java package that satisfies its dependency.
* `cassandra_java_set_default` (default `true`): make `cassandra_java_version`
  the default `java` when several JDKs are installed, or the tarball /
  `cassandra_java_home` Java the system `java`. `false` leaves `/usr/bin/java`
  as it is (Cassandra then needs `JAVA_HOME`, e.g. in
  `cassandra_service_environment`); `import_cluster` sets it for the nodes
  whose `/usr/bin/java` is not the running Java.
* `cassandra_java_version`: Java installed before Cassandra. Defaults to the
  series' version from `cassandra_java_versions` (11 for 4.x, 17 for 5.0).
* `cassandra_java_package`: package name, derived from the OS and
  `cassandra_java_version`.
* `cassandra_cqlsh_python`: Python used by cqlsh. Empty (default): `python3`,
  unless it is outside the range the series' cqlsh supports
  (`cassandra_cqlsh_python_supported`: 3.6-3.11 for 4.x, 3.8-3.13 for 5.0);
  then `python3.11` is installed next to it and cqlsh is pointed at it
  (`/usr/local/bin/cqlsh` wrapper, `CQLSH_PYTHON` in `/etc/profile.d`).
  The system `python3` is never changed. On the RedHat family the role stops
  when no repository offers `python3.11` (EL 10 with 4.x): set this then.
* `cassandra_cqlsh_python_manage`: `false` leaves cqlsh's Python as it is on
  this node (nothing above is added or removed). The `import_cluster`
  playbook sets it on the nodes whose cqlsh has no wrapper of this role.
  Default `true`.
* `cassandra_cqlsh_python_repo_uri`: where python3.11 comes from on Ubuntu
  releases that don't ship it (default: the deadsnakes PPA, signed by the key
  shipped in `files/deadsnakes.asc`; empty to rely on the configured
  repositories).

jemalloc is installed when available (Debian/Ubuntu, and RHEL-family with EPEL
or Amazon Linux).

Example Playbook
----------------

    - hosts: cassandra
      roles:
        - community.cassandra.cassandra_repository
        - community.cassandra.cassandra_install

License
-------

BSD
