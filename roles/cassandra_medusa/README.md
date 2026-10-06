cassandra_medusa
================

Installs [Cassandra Medusa](https://github.com/thelastpickle/cassandra-medusa),
the backup and restore tool, and writes its `/etc/medusa/medusa.ini`.

Medusa is installed with pip, in its own virtualenv (`/opt/cassandra-medusa`),
from PyPI or a mirror of it, and linked as `/usr/local/bin/medusa`. The
virtualenv uses `python3` when Medusa supports it (0.30: Python 3.10 to 3.12),
else `python3.11`, installed on the RedHat family (RHEL 8 and 9). An existing
virtualenv keeps its Python.

`medusa.ini` follows Medusa's `medusa-example.ini`. Each setting has a
variable; a variable set to `""` leaves the setting out, and Medusa's default
applies. The file is only rewritten when its settings change: comments, order
and spacing don't count, so a file written by hand with the same settings stays.
It and the S3 credentials file are readable only by the account Cassandra runs
as (`cassandra_user`, `cassandra_group`; `cassandra_medusa_config_user` and
`_group` set them apart).

The role schedules no backup.

The playbooks of the collection (`create_cluster`, and `add_node`,
`replace_node` and `add_datacenter` for the new nodes) apply the role when
`cassandra_medusa_enabled` is `true` (default `false`). `import_cluster` sets
it when it finds Medusa on the nodes, with the version and the settings of
their `medusa.ini`.

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
  site-packages of the Python (`python3.11` on RHEL 8), `medusa` then being in
  `/usr/local/bin`; refused where the system manages that Python (PEP 668:
  Debian 12, Ubuntu 23.04 and later).
* `cassandra_medusa_pip_index_url`: PyPI mirror, e.g.
  `https://pypi.example.com/simple`. With
  `cassandra_medusa_pip_username` and `cassandra_medusa_pip_password` (vault).
  pip checks its certificate against the system CA bundle
  (`cassandra_medusa_pip_cert`).
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
* CQL and JMX logins: `cassandra_cql_username`/`_password` and
  `cassandra_jmx_username`/`_password_file`/`_password` of the collection, by
  default; `cassandra_medusa_cql_*` and `cassandra_medusa_nodetool_*` to use others.
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

Then, on a node: `medusa backup --backup-name=first`, or
`medusa backup-cluster` from one node (it reaches the others with SSH, see
Medusa's `[ssh]` section).

License
-------

BSD
