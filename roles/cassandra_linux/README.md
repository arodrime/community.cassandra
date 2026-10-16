cassandra_linux
===============

Set Cassandra Linux OS customizations.

Requirements
------------

Root on the hosts: the role does not ask for it itself, apply it in a play
with `become: true` (the collection's playbooks do).

Role Variables
--------------

* `cassandra_linux_manage`: `false` leaves this node's OS settings as they
  are (the role changes nothing on it). The `import_cluster` playbook sets it
  on the nodes it finds set up by other means. Default `true`.
* `cassandra_linux_timesync`: install and start time sync (chrony, kept when
  already installed, or systemd-timesyncd on Debian/Ubuntu). `false` leaves the
  host's time sync alone. Default `true`.
* `cassandra_offline`: `true` on air-gapped hosts: the time sync package is
  not installed, only started when present. Default `false`.
* `cassandra_data_disk_tuning`: tune the read-ahead and IO scheduler of the
  devices under the Cassandra directories (see below). Default `true`.
  `false` leaves them as they are: the role then touches neither the devices
  nor what an earlier run set to keep them (its udev rule, the read-ahead in
  the LVM metadata; `lvchange --readahead auto` and removing
  `/etc/udev/rules.d/64-cassandra-data-disk.rules` undo them).
* `cassandra_data_block_device`, `cassandra_data_block_devices`: devices to
  tune instead of finding them from the directories, e.g. `/dev/sdb`,
  `/dev/nvme0n1`, `/dev/vg/lv`, `/dev/md0` (a partition is taken as its
  disk); the devices under them are tuned too. Defaults to `""`
  and `[]`. A
  device set here that can't be tuned fails the role (not in containers,
  where the disks are left alone).
* `cassandra_data_lvchange_running`: run `lvchange --readahead`, which
  reloads the volume, also under a running Cassandra. Default `false`: at a
  run with Cassandra stopped (see below).
* `cassandra_data_lvchange_restart`: the playbook `apply_config` runs that
  `lvchange` in the node's restart window (drained, Cassandra stopped,
  `lvchange`, started), a restart for it alone if need be. Default `true`;
  `false` leaves it for later (the udev rule keeps the read-ahead meanwhile).
  An `lvchange` refused there (a missing PV, a shared VG) does not cause
  another restart before the next boot: a WARNING gives the command instead.
* `cassandra_data_tune_os_volumes`: tune the filesystem Cassandra's data
  directories are on also when it is one of the OS, and a disk under them
  that also holds one (see below). Default `true`; `false` leaves them
  alone, with a note (shown with `-v`).
* `cassandra_data_readahead_kb`: read-ahead in KB for those devices
  (`queue/read_ahead_kb`, not `blockdev --setra` sectors). Defaults to `4`,
  the practical minimum: read-ahead brings nothing to Cassandra's random
  reads and fills the page cache with data it doesn't need. The kernel
  rounds it down to whole memory pages (0 with 64 KB pages).
* `cassandra_linux_tuned_manage`: when the profiles tuned runs would set the
  read-ahead or IO scheduler of the Cassandra devices (RHEL's default ones
  do), have it run a profile of the role's after them that leaves those
  devices alone (see below). `false`: the role only warns. Default `true`.
* `cassandra_linux_tuned_switch_running`: switching tuned to that profile
  applies the profiles again, live. Default `false`: done at a run with
  Cassandra stopped (a new node, before it starts); under a running
  Cassandra the profile is only written, and the role says how to switch
  (`apply_config` switches in the node's restart window).
  `true`: switched under a running Cassandra too.
* `cassandra_linux_tuned_switch_restart`: the playbook `apply_config` does
  that switch (or the reload of the profile once changed) in the node's
  restart window (drained, Cassandra stopped, `tuned-adm profile ...`,
  started), a restart for it alone if need be. Default `true`; `false` leaves
  it for later, with a warning giving the command. A `tuned-adm` that failed
  there does not cause another restart before the next boot (the same
  warning). A profile the role wrote since tuned loaded it (by a run under a
  running Cassandra) counts as a reload to do. Either variable `false` still
  lets the other's restart do it (Cassandra stopped then).
* `cassandra_linux_apply_live`: apply the kernel settings (sysctl, swapoff,
  THP, disk tuning) live, not only persist them (swapoff only when what swap
  holds fits twice in the memory available, else a warning). Defaults to `auto`: live
  everywhere except in containers (Ansible's virtualization facts, and
  `cassandra_linux_container_types`), where `/proc/sys` and `/sys` belong to
  the host; what runs at boot (the THP unit, the disks' udev rule) is
  left out there too. Set `true` to tune the host from a dedicated
  privileged container, `false` to only persist (in containers, only the
  files). Ansible doesn't detect every container (e.g. Kubernetes pods on
  cgroup v2): set `false` there, the THP unit and the disks' udev rule are
  then still set up.
* `cassandra_linux_container_types`: `virtualization_type` values taken as
  containers, besides Ansible's own container detection. Defaults to
  `docker`, `podman`, `container`, `containerd`, `lxc`.
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

Disk tuning
-----------

The devices are found from the filesystems of the data directories
(`cassandra_data_file_directories`, else `cassandra_data_dir`, as
cassandra_config writes them) and `cassandra_commitlog_dir` (`findmnt`;
a directory not created yet through its nearest parent), then followed down
to the disks (`lsblk`): partitions, LVM volumes (also thin ones), md RAID,
dm-crypt, multipath, NVMe, virtio, and any stack of them. A filesystem of
the OS (`/`, `/usr`, `/var`, `/var/lib`, `/var/log`, `/var/tmp`, `/home`,
`/opt`, `/tmp`, `/boot`, `/boot/efi`, also through a bind mount, or a
partition of a disk holding one) Cassandra's data directories are on is
tuned all the same, with a note (not for the commitlog, hints or saved
caches alone, read whole, sequentially): its other files read with the same
read-ahead (at worst slower sequential reads for the OS, where Cassandra
suffers from a large one); an LVM volume of the OS is kept by the udev rule,
never reloaded with `lvchange`. `cassandra_data_tune_os_volumes: false` leaves it alone.
Filesystems not on a block device (overlay, tmpfs, NFS, ZFS), btrfs (it
reads ahead through a bdi of its own) and loop devices are left alone.

The role ends with one summary for the nodes of the run, the nodes with the
same filesystems and devices grouped, e.g.

```
Data disks: read-ahead 4 KB, set now on 2 nodes
  node1, node2:
    /data/c/{data,commitlog} → vg_data-lv_cass → md0 → sdb1, sdc1 → sdb, sdc
  optional: restart Cassandra to read the files it has open with the new read-ahead (node1, node2)
```

then a `WARNING - ...` line for what should be looked at (a filesystem on
NFS, ZFS or btrfs, `lvchange` or `tuned-adm` failing, a tuned profile setting
another read-ahead). Under `--check` it says `would be set`. With `-v`: the
kernel names (`dm-2`), and why something is left alone or how a device is
shared. What changes on each node is listed in its change report. In a
prepare of new nodes (add_node, create_cluster...), the summary comes after
the prepare's step lines (`data disks  read-ahead 4 KB already set`), and only
when something was set, a line to act on (not tuned yet, a restart to take it), a warning, or with `-v`.

What read-ahead is used: the kernel reads ahead for a file (`read()`, and the
read-around of `mmap` faults) with the read-ahead of the device its
filesystem is mounted from: the top one (the LVM volume, the md array, the
dm-crypt device), not the disks under it, which only get the I/O mapped down
to them. That device is tuned, whatever else the filesystem holds (a
partition has no read-ahead of its own: its disk is tuned). A file takes it when it is
opened: the files Cassandra has open keep the one they had until it
restarts.

The devices under the top one get the same value, so that what is set up
again above them takes no larger one from them: LVM's `auto` read-ahead and
cryptsetup take the device under them as a minimum when they activate a
volume. Cassandra is taken as alone on its storage: a device under it that
also holds something else (another volume of the volume group, a disk with
other partitions in use) is tuned all the same; the other volumes keep
their own read-ahead (LVM's `auto` takes the disk's only as a minimum,
never lower). A disk that also holds a filesystem of the OS (a partition
with `/` next to the volume group) is tuned too, said as `sda (also /)`:
`/` on a partition reads with it; it is left alone with
`cassandra_data_tune_os_volumes: false`, or when only the commitlog is
above it. md arrays
get the same value too: md sets twice a stripe (megabytes
on RAID 0/5/10), for sequential reads Cassandra doesn't do.

The IO scheduler is set to `none` on the devices that have one to choose and
are not rotational (`queue/rotational` 0: SSD, NVMe): the disks, and a
multipath device (its paths' schedulers are bypassed). Rotational devices
keep theirs (`mq-deadline` by default), as do virtual disks that report
themselves rotational: Red Hat recommends `mq-deadline` in virtual guests,
and nothing tells which storage is behind a virtual disk.

Both are applied immediately through sysfs, and kept across reboots:

* LVM volumes: in their metadata (`lvchange --readahead`, see
  `lvs -o +lv_read_ahead`). LVM sets it again whenever it activates or
  reloads the volume (at boot, after `lvextend`...), after the kernel, which
  may set it back then. `lvchange` reloads the volume, its I/O suspended a
  moment: it is done at a run with Cassandra stopped (a new node, before it
  starts), once, or by `apply_config` in the node's restart window; with
  Cassandra running, the read-ahead is set live and the udev rule keeps it
  meanwhile (`cassandra_data_lvchange_running: true` runs `lvchange` all the
  same). When LVM refuses (a missing PV, a shared VG),
  when the role does not apply settings live, or with
  `activation/readahead = "none"` in `lvm.conf`, the volume is kept by the
  udev rule too, and LVM may set it back when it activates the volume.
* the other devices: a udev rule
  (`/etc/udev/rules.d/64-cassandra-data-disk.rules`) on `add` and `change`
  events, matching each device by a stable id, never by its kernel name:
  disks by `ID_WWN_WITH_EXTENSION`, `ID_WWN`, `ID_SERIAL` or `ID_PATH`;
  device-mapper devices (dm-crypt, multipath) by their uuid
  (`ATTR{dm/uuid}`, else their name; a dm-crypt uuid ends with its name in
  `/etc/crypttab`); md arrays by `MD_UUID` (else `ATTR{md/uuid}`). A device
  with no stable id is set now but not kept. The rule file comes after
  `63-md-raid-arrays.rules`, which sets `MD_UUID`, and replaces the
  `61-cassandra-data-disk.rules` of earlier versions.

The kernel raises a read-ahead back (to at least 128 KB, or twice the
device's optimal I/O size) whenever a device's queue limits are updated: a
device-mapper reload, an NVMe controller reset, a SCSI rescan. LVM and the
`change` event of a reload set it again; the others stay so until the next
run of the role or the next boot.

tuned: RHEL's default profiles (`throughput-performance`, and
`virtual-guest` which includes it) set `readahead=>4096` (4 MB at least) on
every disk, device-mapper and md device when tuned starts (each boot) and
when it sees a new device, over what the role sets. When the profiles tuned
runs would set a Cassandra device (merged and matched as tuned does: the
first `[disk]` instance by priority whose `devices` or `devices_udev_regex`
takes it sets another read-ahead, or an IO scheduler other than `none` on an
SSD), the role writes a profile of its own, `cassandra-disks` (in
`/etc/tuned/profiles`, or `/etc/tuned` where tuned keeps its profiles
directly under `/usr/lib/tuned`: before 2.23, RHEL 8 and 9), and has tuned run
it after them: `tuned-adm profile <the active ones> cassandra-disks` (tuned
merges the profiles it runs). It only adds a `[disk]` instance with a lower
priority number (`priority=-100`): tuned gives each device to the first
instance that matches it, so it takes the Cassandra devices, matched by the
same stable ids as the udev rule (`devices_udev_regex`), and sets nothing on
them; everything else stays as the active profiles set it. Needs tuned 2.10
or later, running, and the role applying settings live; the role says when
it can't (tuned stopped, no stable id, udevadm failing). Not read: a
profile loaded after the others (`post_loaded_profile`), `readahead_multiply`,
conditional includes. A device that appears after tuned started (a volume
activated later, a disk attached later) may still get tuned's read-ahead,
before udev knows its ids: the udev rule and LVM set it back at its next
`change` event or activation, the next run of the role at once.

Adding the profile applies the profiles again, live: tuned takes back what
they set and applies them again (their scripts run again, services they
manage are restarted), and its profile mode becomes manual (no automatic
recommendation). The role then sets its kernel settings and THP again. It
is done once, and again only when the Cassandra devices change, and only at
a run with Cassandra stopped (a new node, before it starts): under a
running Cassandra, the profile is written but not switched to (the role
says so, with the `tuned-adm profile` command), unless
`cassandra_linux_tuned_switch_running: true`. The playbook `apply_config`
does it in the node's restart window (drained, Cassandra stopped, switched,
started: a restart for it alone if need be, its reason said), unless
`cassandra_linux_tuned_switch_restart: false`. Until it is switched to,
tuned sets its read-ahead and scheduler on the Cassandra devices again when
it starts (at boot); the role sets them back at its next run. A disk
matched by its `ID_WWN` alone (no extension) shares it with the other LUNs
of its array: they leave tuned's `[disk]` too. `tuned-ppd` (power profiles
daemon) switching profiles takes it off: `apply_config` then restarts the
node to switch back (`cassandra_linux_tuned_switch_restart: false` on hosts
where tuned-ppd runs). `cassandra_linux_tuned_manage: false` leaves tuned alone, and
the role only warns; back to the profiles before: that, and
`tuned-adm profile <them>`.

In containers, the devices are not tuned (they are the host's).

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

