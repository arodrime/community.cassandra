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

* `cassandra_linux_timesync`: install and start time sync (chrony, kept when
  already installed, or systemd-timesyncd on Debian/Ubuntu). `false` leaves the
  host's time sync alone. Default `true`.
* `cassandra_offline`: `true` on air-gapped hosts: the time sync package is
  not installed, only started when present. Default `false`.
* `cassandra_data_block_device`: the disk of the Cassandra data, to tune
  its read-ahead and IO scheduler, e.g. `/dev/sdb` or `/dev/nvme0n1` (a
  partition is taken as its disk). Defaults to `""`.
* `cassandra_data_block_devices`: several disks to tune (JBOD data
  directories, a commitlog disk). Defaults to `[]`. With neither variable
  set, the disks are found from the data directories (`cassandra_data_dir`,
  `cassandra_data_file_directories`) and `cassandra_commitlog_dir` with
  `findmnt` and `lsblk`; without those either, nothing is tuned. LVM, md
  RAID and dm-crypt devices are not guessed: the role says so, set the
  disks. A disk set here that can't be tuned fails the role.
* `cassandra_data_readahead_kb`: read-ahead in KB for those disks
  (`queue/read_ahead_kb`, not `blockdev --setra` sectors). Defaults to `4`,
  the practical minimum: read-ahead brings nothing to Cassandra's random
  reads and fills the page cache with data it doesn't need.
* `cassandra_linux_apply_live`: apply the kernel settings (sysctl, swapoff,
  THP, disk tuning) live, not only persist them. Defaults to `auto`: live
  everywhere except in containers (Ansible's virtualization facts, and
  `cassandra_linux_container_types`), where `/proc/sys` and `/sys` belong to
  the host. Set `true` to tune the host from a dedicated privileged
  container, `false` to only persist.
* `cassandra_linux_sysctl`: kernel settings (swappiness, max_map_count, TCP
  keepalive and buffers...), written to `cassandra_linux_sysctl_file`
  (default `/etc/sysctl.d/60-cassandra.conf`). The same keys are removed from
  `/etc/sysctl.conf`, which is read last and would win.
* `cassandra_linux_limits`: limits of the cassandra user, in
  `/etc/security/limits.d/cassandra.conf`, for tools run by hand as
  cassandra (the service gets its own from its systemd unit). Same values as
  the unit by default.
* `cassandra_sysfs_block_root`: sysfs directory the disk tuning reads and
  writes. Defaults to `/sys/block`; only meant to be overridden by tests,
  to point at a fake tree instead of the host's real disks.

The read-ahead is set on the disks, and the IO scheduler set to `none` on
SSD/NVMe disks only (`queue/rotational` 0): spinning disks keep theirs. Both
are applied immediately through sysfs, then kept across reboots with a udev
rule (`/etc/udev/rules.d/61-cassandra-data-disk.rules`) matching each disk by
its serial number (`ID_SERIAL`), or by its name when udev doesn't know one.
In containers, the disks are not tuned (they are the host's).

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

