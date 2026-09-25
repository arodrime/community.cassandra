cassandra_change_report
=======================

Used by the other roles of the collection to show what they change, as
`item: before -> after` lines, on a normal run and under `--check`.

With `cassandra_change_report_dir` set, the same list is also written on the
controller, one YAML file per host and role:
`<dir>/<UTC timestamp>/<host>-<role>.yml`, with the run mode (`check` or
`applied`). A `--check` run gives the planned changes for review; the applied
run records what was done.
The files are private (`0600`, directory `0700`): they hold config diffs,
where password values are masked (`****`) like in the role output.

    - hosts: cassandra
      vars:
        cassandra_change_report_dir: "{{ playbook_dir }}/changes"
      roles:
        - community.cassandra.cassandra_install
        - community.cassandra.cassandra_config

Role Variables
--------------

* `cassandra_change_report_dir`: controller directory for the reports.
  Default `""` (no files, only the displayed list).

License
-------

BSD
