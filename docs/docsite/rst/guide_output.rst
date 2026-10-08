.. _ansible_collections.community.cassandra.docsite.guide_output:

Operator output
===============

What the playbooks print, and how a playbook prints it. The text comes from
``plugins/module_utils/cassandra_output.py`` (filters in ``plugins/filter/cassandra_output.py``): every block is
written once there, a playbook only fills it.

Conventions
-----------

- **Verdict first.** Line 1 says what happens or happened: ``PLAN``, ``READY``, ``REFUSED``, ``DONE``,
  ``FAILED``, ``CHECK``, ``HEALTHY``, ``NOT HEALTHY``, then the operation and the cluster.
- **One fact per line**, no prose: the documentation explains, the output states.
- **Setting-centric**, grouped: ``setting:  value   nodes``, ``all`` when every node agrees, a line per other
  value, ``← differs`` on the minority ones. Secrets are always shown as ``****``.
- **Node lists** are short: ``node1..node5, node7`` (a range from 3 consecutive names, 2 names listed, a gap breaks
  the range), ``6 nodes:`` first when there are more than 5. Files and ``-v`` give every name; ``--limit`` in a
  command always lists every host.
- **Sizes, rates, durations** in one format: ``41.2 GiB``, ``89 MiB/s``, ``9m20s`` (never ``0.0 GiB`` for a
  non-zero size).
- **Plans**: the header, the numbered steps (node and dc/rack), the facts, then the ``WARNING`` lines just above
  the question.
- **Progress**: one line per check, then always a compact line of the nodes at the other end::

    [1/2] node5 bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m
          from node1 18.0/34.0 GiB ok   from node3 4.2/17.0 GiB stalled

- **Recap**: the verdict with the counts and the duration, the nodes grouped by identical outcome, a line of its
  own for a node more than 50% slower than the median, a failed or a skipped one, with the reason. ``--check``
  says ``would ...`` and ends the verdict with ``nothing was changed``.
- **TO DO**: numbered, each with its full command, ready to paste (``-i`` and ``-e cassandra_hosts`` only when the
  run needed them). An inventory change is followed by its commit only when the inventory is in a git work tree.
- The marker ``←`` points at what needs a look (``← down``, ``← differs``, ``← slow``).

Printing a message
------------------

A task is an operator message when its own ``vars`` set ``cassandra_output: true`` (a literal true, on the task
itself). Its ``msg`` is a list of lines (or one string with newlines). Cluster-wide messages run once:

.. code-block:: yaml

    - name: Show the plan
      ansible.builtin.debug:
        msg: >-
          {{ {'operation': 'decommission_node', 'cluster': cluster, 'steps': steps, 'facts': facts,
              'warnings': warnings} | community.cassandra.cassandra_plan(check=ansible_check_mode) }}
      run_once: true
      vars:
        cassandra_output: true

A verdict that also fails the run (scheduling) is a marked ``assert`` with ``quiet: false``, the same lines as
``success_msg`` and ``fail_msg``:

.. code-block:: yaml

    - name: Report
      ansible.builtin.assert:
        that: report.healthy
        success_msg: "{{ report.lines }}"
        fail_msg: "{{ report.lines }}"
        quiet: false
      run_once: true
      vars:
        cassandra_output: true

The ``community.cassandra.ops`` stdout callback prints:

- the ``msg`` of a marked task that succeeds (each loop item, handlers too); a marked ``assert`` that passes
  prints its ``success_msg``, nothing without one;
- the ``msg`` of a marked ``assert`` or ``fail`` that fails, as it is (no task header);
- every other failure as the default callback does (the task name, the host, the message, stdout and stderr), a
  failed loop item too; a failure the task ignores (``ignore_errors``) is not printed;
- an unreachable host as the default callback does; one line when the play goes on without it
  (``ignore_unreachable``);
- the questions (``pause``), and the diffs under ``--diff``;
- nothing else: no task headers, no ``ok``, ``changed`` or ``skipping`` lines, no retries, no recap.

With ``-v`` or more, it is the default callback. Under the default callback, the same messages show as the
``msg`` of their task (as plain text with ``callback_result_format = yaml``).

Blocks
------

``cassandra_node_list``, ``cassandra_size``, ``cassandra_rate``, ``cassandra_duration``,
``cassandra_setting_lines``, ``cassandra_by_nodes``, ``cassandra_plan``, ``cassandra_progress_line``,
``cassandra_recap``, ``cassandra_perm_lines``, ``cassandra_diff_lines``, ``cassandra_changed_lines``,
``cassandra_command``, ``cassandra_todo``, ``cassandra_inventory_steps``, ``cassandra_in_git_work_tree``,
``cassandra_mask``: their arguments are in ``plugins/filter/cassandra_output.py`` and the module_utils functions
they call. The ``status`` playbook (``cassandra_ring_report``) and ``health_check`` (``cassandra_health_report``)
use them.
