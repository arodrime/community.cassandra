cassandra_install
=================

Installs Apache Cassandra from the repository set up by `cassandra_repository`
(or from package files, `cassandra_install_method: packages`), with the Java
version the series is built for, and the DataStax Bulk Loader (dsbulk) next
to the Cassandra tools.

On Debian/Ubuntu, the package would start Cassandra with its stock config as
soon as it is installed; a temporary `policy-rc.d` prevents that, so the node
only starts once it is configured.

Requirements
------------

Root on the hosts: the role does not ask for it itself, apply it in a play
with `become: true` (the collection's playbooks do).

Role Variables
--------------

* `cassandra_offline`: `true` on air-gapped hosts: nothing is downloaded, Java
  and the Cassandra packages are checked instead of installed, and the role
  stops with the list of what is missing (see the guide's air-gapped section).
  The Python cqlsh may need, jemalloc and dsbulk only give a warning (dsbulk
  is still installed from a copy on the node, `cassandra_dsbulk_url`
  `file://...`). Default `false`.
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
* `cassandra_java_tarball` (default: the `cassandra_java_tarballs` entry of
  `cassandra_java_version`, else `""`): Java from a tarball (a JDK or JRE
  `.tar.gz`) instead of a package: a URL downloaded by the
  nodes (`cassandra_java_tarball_checksum` recommended, credentials in
  `cassandra_java_tarball_username`/`_password`), or a file on the controller.
  It is unpacked into `cassandra_java_tarball_dir/<tarball name>` (default
  `/opt/cassandra-java`) and made the system `java`, which the cassandra script
  and nodetool run. The Cassandra packages are then installed without a Java
  package: on Debian/Ubuntu a local `cassandra-java-tarball` package provides
  the Java they depend on; on the RedHat family they are installed with
  `rpm --nodeps` (plus procps-ng, python3 and shadow-utils).
  `cassandra_java_version` must still name its major version (the `release`
  file of the unpacked Java, or of `cassandra_java_home`, is checked against
  it); `update_java` moves the nodes to a new tarball.
* `cassandra_java_tarballs` (default `{}`): Java tarballs on offer, by major
  version, e.g. `{"17": {url: ..., checksum: "sha256:..."}}` (`url` a URL or a
  file on the controller; `checksum`, `username`, `password` optional), set once
  for every cluster. A cluster then
  only sets `cassandra_java_version`; `cassandra_java_tarball` (and its
  checksum and credentials) default to that entry, unless
  `cassandra_install_java` is false. A version without one is refused, unless
  `cassandra_java_home` is set. Each tarball needs a file name of its own (its
  directory is named after it). A node on a Java package moves to the tarball
  on its next run: `cassandra_java_tarballs: {}` keeps a cluster on packages.
* `cassandra_java_allow_unsupported` (default `false`): install a
  `cassandra_java_version` the series does not support (e.g. 21 with 5.0),
  which is refused otherwise.
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
  Debian 12 and later have no Java 11 package (Ubuntu 22.04 and 24.04 do):
  for 4.x there, use a Java tarball, or add a repository that has one (e.g.
  Adoptium) and set `cassandra_java_package` (e.g. `temurin-11-jdk`). The role
  checks the package is available before installing anything.
* `cassandra_java_package`: package name, derived from the OS and
  `cassandra_java_version`.
* `cassandra_cqlsh_python`: Python used by cqlsh. Empty (default): `python3`,
  unless it is outside the range the series' cqlsh supports
  (`cassandra_cqlsh_python_supported`: 3.6-3.11 for 4.x, 3.8-3.13 for 5.0);
  then `python3.11` is installed next to it and cqlsh is pointed at it
  (`/usr/local/bin/cqlsh` wrapper, `CQLSH_PYTHON` in `/etc/profile.d`).
  The system `python3` is never changed. When no longer needed, these files
  are removed, only if the role wrote them. On the RedHat family the role stops
  when no repository offers `python3.11` (EL 10 with 4.x): set this then.
* `cassandra_cqlsh_python_manage`: `false` leaves cqlsh's Python as it is on
  this node (nothing above is added or removed). The `import_cluster`
  playbook sets it on the nodes whose cqlsh has no wrapper of this role.
  Default `true`.
* `cassandra_cqlsh_python_repo_uri`: where python3.11 comes from on Ubuntu
  releases that don't ship it (default: the deadsnakes PPA, signed by the key
  shipped in `files/deadsnakes.asc`; empty to rely on the configured
  repositories).
* `cassandra_dsbulk_install`: install the DataStax Bulk Loader
  ([dsbulk](https://github.com/datastax/dsbulk)) too. Default `true`
  (`import_cluster` sets `false` for the nodes without one installed this
  way, and keeps the version of the others). It is
  installed like the tools of the `cassandra-tools` package: unpacked in
  `/usr/share/dsbulk-<version>` (owned by root, read-only for others), with a
  `/usr/share/dsbulk` link to it and `/usr/bin/dsbulk`. It runs with
  `$JAVA_HOME` or the `java` on the `PATH` (the Java installed above, unless
  another one is the default), and writes its logs in `./logs`. The two
  links are taken over; a file or directory in their place (a dsbulk
  installed by other means) stops the role. `false` skips it all (a dsbulk
  already installed is left alone).
* `cassandra_dsbulk_version`: dsbulk release, quoted. Default `"1.11.2"`.
  A version already in `/usr/share` is used as is, not downloaded again; on
  a version change the links move to the new one, then the versions this
  role installed before are removed, as a package upgrade would (a dsbulk
  unpacked in `/usr/share` by other means is left alone; one in the way of
  the version to install, without `bin/dsbulk`, stops the role). Changes
  made in its `conf/` go with it: keep settings in a file of your own,
  passed with `-f`.
* `cassandra_dsbulk_url`: where the tarball comes from. Default: the release
  on GitHub. Or a local mirror
  (e.g. `https://mirror.example.com/dsbulk/dsbulk-1.11.2.tar.gz`; with
  `cassandra_install_username`/`_password` when it is on the host of
  `cassandra_install_url`), or a copy of the file on the node
  (`file:///path/to/dsbulk-1.11.2.tar.gz`), which `cassandra_offline` also
  installs.
* `cassandra_dsbulk_checksum`: checksum of the tarball, as `sha256:<hex>`
  (any form `get_url` takes). The default matches the default version only:
  set it whenever you change the version, or the download is refused. `""`
  skips the check.

dsbulk is downloaded from GitHub unless `cassandra_dsbulk_url` points
elsewhere (even `--check` reaches the URL when a download is due): on nodes
without access to it, set that URL or `cassandra_dsbulk_install: false`.

jemalloc is installed when available (Debian/Ubuntu, and RHEL-family with EPEL
or Amazon Linux).

Example Playbook
----------------

    - hosts: cassandra
      become: true
      roles:
        - community.cassandra.cassandra_repository
        - community.cassandra.cassandra_install

License
-------

BSD
