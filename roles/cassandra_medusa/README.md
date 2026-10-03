cassandra_medusa
================

Installs [Cassandra Medusa](https://github.com/thelastpickle/cassandra-medusa),
the backup and restore tool, and writes its `/etc/medusa/medusa.ini`.

Medusa is installed with pip, in its own virtualenv (`/opt/cassandra-medusa`),
from PyPI or a mirror of it, and linked as `/usr/local/bin/medusa`. The
virtualenv uses `python3` when Medusa supports it (0.30: Python 3.10 to 3.12),
else `python3.11`, installed on the RedHat family (RHEL 8 and 9). An existing
virtualenv keeps its Python.

`medusa.ini` follows Medusa's `medusa-example.ini`. Most settings of its
`[cassandra]`, `[storage]`, `[monitoring]` and `[logging]` sections have a
variable (`cassandra_medusa_extra_settings` for any other); a variable set
to `""` leaves the setting out, and Medusa's default applies. The file is
only rewritten when its settings change: comments, order and spacing don't
count, so a file written by hand with the same settings stays.
It and the S3 credentials file are readable only by the account Cassandra runs
as (`cassandra_user`, default `cassandra`), which is the one to run Medusa with:
it reads the data files and the config.

Apply it after Cassandra is installed (the account must exist). The role
schedules no backup.

Role Variables
--------------

The main ones (all of them in `meta/argument_specs.yml`):

* `cassandra_medusa_version`: default `0.30.1`.
* `cassandra_medusa_venv`: the virtualenv, default `/opt/cassandra-medusa`;
  `cassandra_medusa_link_dir`: where `medusa` is linked, default `/usr/local/bin`
  (`""` for no link); `cassandra_medusa_profile_d`: `true` writes
  `/etc/profile.d/cassandra-medusa.sh`, which puts the virtualenv's `bin` in
  the PATH of login shells (default `false`; users' profiles are never edited).
  `cassandra_medusa_venv: ""` installs without a virtualenv, into the system
  site-packages of the Python (`python3.11` on RHEL 8 and 9), `medusa` then being in
  `/usr/local/bin`; refused where the system manages that Python (PEP 668:
  Debian 12, Ubuntu 23.04 and later).
* `cassandra_medusa_pip_index_url`: PyPI mirror, e.g.
  `https://pypi.example.com/simple`. With
  `cassandra_medusa_pip_username` (e.g. `mirror_user`) and `cassandra_medusa_pip_password` (vault).
  pip checks its certificate against the system CA bundle
  (`cassandra_medusa_pip_cert`). The login goes to pip in a temporary file
  only root reads, not on the command line.
* `cassandra_medusa_storage_provider` (required): `s3`, `s3_compatible`,
  `local`, `google_storage`, `azure_blobs`...
* `cassandra_medusa_bucket_name`, `cassandra_medusa_prefix`.
* `cassandra_medusa_fqdn`: the node's name in the backups, their path in the
  bucket (default: Medusa works it out); `cassandra_medusa_fqdn_domain` makes it
  `<short hostname>.<domain>` on every node. The role refuses to change the
  fqdn of an existing `medusa.ini` (a new folder in the bucket, full backups)
  unless `cassandra_medusa_fqdn_change: true`.
* `cassandra_medusa_host`, `cassandra_medusa_port`, `cassandra_medusa_secure`,
  `cassandra_medusa_region`: the endpoint of an `s3_compatible` storage.
* `cassandra_medusa_s3_access_key_id`, `cassandra_medusa_s3_secret_access_key`
  (vault): written to `/etc/medusa/credentials`. Without them,
  `cassandra_medusa_key_file` can point at a file of your own (not written).
* `cassandra_medusa_base_path`: the directory of the `local` provider.
* `cassandra_medusa_transfer_max_bandwidth`, `cassandra_medusa_concurrent_transfers`,
  `cassandra_medusa_multi_part_upload_threshold`, `cassandra_medusa_max_backup_age`,
  `cassandra_medusa_max_backup_count`.
* CQL and JMX logins, when authentication is on: `cassandra_medusa_cql_username`/`_password`,
  `cassandra_medusa_nodetool_username`/`_password_file`/`_password` (the file wins).
  `cassandra_medusa_nodetool_port` defaults to `cassandra_jmx_port`, else 7199, and
  the `cassandra.yaml` Medusa reads is in `cassandra_conf_dir`, when they are set.
* `cassandra_medusa_config_owner`/`_group`: `cassandra_user`/`cassandra_group`,
  else `cassandra`.
* `cassandra_medusa_extra_settings`: any other setting, per section, e.g.
  `{checks: {enable_md5_checks: "true"}}`.
* `cassandra_offline`: nothing is downloaded; Medusa must already be in the
  virtualenv, unless `cassandra_medusa_pip_index_url` is a mirror the hosts reach.

Example Playbook
----------------

    - hosts: cassandra
      vars:
        cassandra_medusa_storage_provider: s3_compatible
        cassandra_medusa_host: s3.example.com
        cassandra_medusa_port: 443
        cassandra_medusa_bucket_name: cassandra-backups
        cassandra_medusa_prefix: orders
        cassandra_medusa_s3_access_key_id: "{{ vault_s3_access_key_id }}"
        cassandra_medusa_s3_secret_access_key: "{{ vault_s3_secret_access_key }}"
      roles:
        - community.cassandra.cassandra_medusa

Then, on a node, as the Cassandra account: `sudo -u cassandra medusa backup --backup-name=first`,
or `medusa backup-cluster` from one node (it reaches the others with SSH, see
Medusa's `[ssh]` section). By Medusa's defaults, `backup-cluster` and the
restores run commands with `sudo` (`cassandra_medusa_use_sudo`,
`cassandra_medusa_use_sudo_for_restore`) and restores stop and start Cassandra
with `sudo service cassandra stop/start`: give the account these sudo rights,
or set those variables.

License
-------

BSD
