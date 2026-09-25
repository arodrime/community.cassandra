cassandra_linux
===============

Set Cassandra Linux OS customizations.

Requirements
------------

Any pre-requisites that may not be covered by Ansible itself or the role should
be mentioned here. For instance, if the role uses the EC2 module, it may be a
good idea to mention in this section that the boto package is required.

Role Variables
--------------

* `cassandra_data_block_device`: block device to apply read-ahead/IO
  scheduler tuning to, e.g. `/dev/nvme0n1`. Defaults to `""`, which
  auto-detects it from `cassandra_data_dir` (owned by the cassandra_config
  role) via `findmnt` + `lsblk`. Skipped, not guessed, if
  `cassandra_data_dir` isn't defined or detection is inconclusive - set
  this explicitly to force a specific device.
* `cassandra_data_readahead_kb`: read-ahead in KB applied to
  `cassandra_data_block_device`'s `queue/read_ahead_kb`. Defaults to `4`
  (the practical minimum, not `blockdev --setra` sectors) - read-ahead
  offers no benefit for Cassandra's random-access read path, especially
  on 5.0+.
* `cassandra_linux_apply_live`: apply kernel settings (sysctl, swapoff,
  THP) live, not only persist them. Defaults to `auto`: live everywhere
  except in containers (`cassandra_linux_container_types`), where `/proc/sys`
  and `/sys` belong to the host. Set `true` to tune the host from a
  dedicated privileged container, `false` to only persist.
* `cassandra_sysfs_block_root`: sysfs directory the disk tuning reads and
  writes. Defaults to `/sys/block`; only meant to be overridden by tests,
  to point at a fake tree instead of the host's real disks.

The tuning is applied immediately through sysfs, then persisted across
reboots with a udev rule (`/etc/udev/rules.d/60-cassandra-data-disk.rules`).

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
         - { role: cassandra_linux, x: 42 }

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a
website (HTML is not allowed).

References
__________

The following sources of information were used extensively for this role:

* https://docs.datastax.com/en/docker/doc/docker/dockerRecommendedSettings.html
* https://docs.datastax.com/en/cassandra/3.0/cassandra/install/installRecommendSettings.html
* https://docs.datastax.com/en/dse/5.1/dse-admin/datastax_enterprise/config/configRecommendedSettings.html

