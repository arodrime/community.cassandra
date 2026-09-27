cassandra_repository
====================

Configures a repository for Cassandra on Debian and RedHat based platforms.

Requirements
------------

ansible-core 2.15 or later (the apt repository is written with
`ansible.builtin.deb822_repository`).

Role Variables
--------------

cassandra_version:
  - Which version of Cassandra to install, e.g. "50x", "41x", "40x", "311x".
  - See the distribution names available at:
      - https://debian.cassandra.apache.org (Debian & Ubuntu)
      - https://redhat.cassandra.apache.org/ (RedHat)

cassandra_offline:
  - `true` on air-gapped hosts, nothing is downloaded: sets the default of
    `cassandra_repository_manage` to `false` (see the guide's air-gapped section).

cassandra_repository_username / cassandra_repository_password:
  - Credentials for a mirror that needs them to read: an account and its
    password, or a service account and its token. Used for the repository and
    for `cassandra_repository_key_url`. On RedHat they go in the yum repository
    file (then mode 0600), on Debian/Ubuntu in
    `/etc/apt/auth.conf.d/cassandra.conf` (0600). Keep the password in a vault.

cassandra_repository_manage:
  - `false` when the repositories are configured by other means (a
    Satellite/Foreman, the system image): the role then does nothing.
    Defaults to `true`, `false` with `cassandra_offline`.

cassandra_repository_deb_url / cassandra_repository_rpm_url:
  - Where the packages come from: the Apache repositories by default, or a
    mirror of them for hosts without internet access.

cassandra_repository_key_url:
  - Where the release signing keys come from. Empty (default): the copy of
    https://downloads.apache.org/cassandra/KEYS shipped with the role
    (`files/KEYS`), so nothing is downloaded.
  - A URL (e.g. a local mirror) is downloaded instead, and every key it holds
    must be listed in `cassandra_repository_key_fingerprints` (primary key
    fingerprints, defaults to the keys of the shipped copy).

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
      roles:
         - { role: cassandra_repository, x: 42 }

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a
website (HTML is not allowed).
