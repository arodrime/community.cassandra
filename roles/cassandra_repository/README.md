cassandra_repository
====================

Configures a repository for Cassandra on Debian and RedHat based platforms.
With `cassandra_install_method: packages`, configures none (and removes the
ones it configured before): `cassandra_install` then downloads the package
files themselves.

Requirements
------------

ansible-core 2.15 or later (the apt repository is written with
`ansible.builtin.deb822_repository`).

Root on the hosts: the role does not ask for it itself, apply it in a play
with `become: true` (the collection's playbooks do).

Role Variables
--------------

cassandra_version:
  - Which version of Cassandra to install, e.g. "50x", "41x", "40x".
  - Default "50x". cassandra_install reads it too: set it for both (e.g. in
    group_vars), not as a parameter of this role only.
  - See the distribution names available at:
      - https://debian.cassandra.apache.org (Debian & Ubuntu)
      - https://redhat.cassandra.apache.org/ (RedHat)

cassandra_offline:
  - `true` on air-gapped hosts, nothing is downloaded: sets the default of
    `cassandra_repository_manage` to `false`.

cassandra_install_method:
  - `repository` (default): adds the yum/apt repository at
    `cassandra_install_url`. The URL is checked first (`repodata/repomd.xml`,
    or the suite's `InRelease`/`Release`): a plain directory of package files
    is refused with a message, and no repository is added.
  - `packages`: the package files are downloaded by `cassandra_install` from
    `cassandra_install_url`, a plain directory (e.g. a generic folder of a
    repository manager). This role adds no repository, and removes the
    `cassandra-<series>` repositories it added before.
  - Same variable as in `cassandra_install`.

cassandra_install_url:
  - The repository, or the directory of the package files. Defaults to the
    Apache repositories (or point it at a mirror of them).

cassandra_install_username / cassandra_install_password:
  - Credentials for a mirror that needs them to read: an account and its
    password, or a service account and its token. Used for the repository, and
    for `cassandra_repository_key_url` when it is on the same server (scheme,
    host and port). On RedHat they go in the yum repository file (then mode
    0600), on Debian/Ubuntu in
    `/etc/apt/auth.conf.d/cassandra.conf` (0600). Keep the password in a vault.
    Without credentials that file is removed, only when this role wrote it
    (its header).

cassandra_repository_manage:
  - `false` when the repositories are configured by other means (a
    Satellite/Foreman, the system image): the role then does nothing.
    Defaults to `true`, `false` with `cassandra_offline`.

cassandra_repository_key_url:
  - Where the release signing keys are downloaded from. Default
    https://downloads.apache.org/cassandra/KEYS.
  - Every key the file holds must be listed in
    `cassandra_repository_key_fingerprints` (primary key fingerprints; the
    default lists the keys in the Apache file), or the role fails: when Apache
    adds a release manager's key, check it, then add it to the list. Reading
    them needs GnuPG 2.2.8 or later on the node (`gpg --show-keys`).
  - Offline nodes: a local mirror URL, or a copy of the file on the node
    (`file:///path/to/KEYS`).

cassandra_apt_keyring_path / cassandra_rpm_key_path:
  - Where the keys are installed (Debian & Ubuntu / RedHat).

Dependencies
------------

A list of other roles hosted on Galaxy should go here, plus any details in
regards to parameters that may need to be set for other roles, or variables that
are used from other roles.

Example Playbook
----------------

Including an example of how to use your role (for instance, with variables
passed in as parameters) is always nice for users too:

    - hosts: servers
      become: true
      roles:
         - { role: cassandra_repository, x: 42 }

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a
website (HTML is not allowed).
