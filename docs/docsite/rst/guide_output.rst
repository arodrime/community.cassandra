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
- **Setting-centric**, grouped: ``setting: value   nodes``, ``all`` when every node agrees, a line per other
  value, ``← differs`` on the minority ones. Secrets are always shown as ``****``. Several settings share one
  column each for the names, the values and the nodes (``settings_lines``): the import report and
  ``apply_config``'s settings that differ, where the inventory's value comes first with the nodes already on it::

    cassandra.yaml
      max_hint_window:  3h                            node1..node3, node5   (inventory)
                        6h                            node4                 ← differs
    jvm-server.options
      -Ddemo.drift=1:   present                       node2                 ← differs (not in the inventory)
- **Node lists** are short: ``node1..node5, node7`` (a range from 3 consecutive names, 2 names listed, a gap breaks
  the range), ``6 nodes:`` first when there are more than 5. Files and ``-v`` give every name; ``--limit`` in a
  command always lists every host. From 12 nodes on, the import screen and report and ``apply_config``'s settings
  that differ define ``NODE GROUPS`` first (the nodes that are always in the same lists, 3 or more:
  ``G1 = db11..db16, db21..db26 (12 nodes)``), then a row says ``all``, ``G1``, ``G1 + db074`` or
  ``all except db111``; report.txt's DETAILS name every node. A dict value is a row per sub-key
  (``cassandra_extra_settings.sstable.selected_format``). A long value or node list goes on under its column (a
  name is never cut), the import screen fits the terminal.
- **Sizes, rates, durations** in one format: ``41.2 GiB``, ``89 MiB/s``, ``9m20s`` (never ``0.0 GiB`` for a
  non-zero size).
- **Plans**: the header, the numbered steps (node and dc/rack), the facts, then the ``WARNING`` lines just above
  the question.
- **Phases**: one line per major phase, ending with ``...`` (cyan under the ops callback: its option
  ``progress_color``), e.g. ``Checking the cluster (3 nodes; adding node4, node5)...`` (the members: not the nodes
  being added or reset, nor those marked absent), ``Preparing the new nodes (node5, node6)...``, ``Starting node5 and waiting for its
  bootstrap...``, ``Decommissioning node3...``, ``Waiting for every node to be UN...``, ``Restarting node2...``. Their
  words are in one place (``PHASES`` in ``plugins/module_utils/cassandra_output.py``, filter
  ``community.cassandra.cassandra_phase``); ``--check`` says the reading phases only.
- **Progress**: one line per check, then always a compact line of the nodes at the other end::

    [1/2] node5 bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m
          from node1 18.0/34.0 GiB ok   from node3 4.2/17.0 GiB stalled

  A check every 10 s for the first 5 minutes, then every 30 s; after a minute without progress the line says it
  (``STALLED 6m/15m``: the time without progress and the limit, ``cassandra_stream_stall_time``).

- **Recap**: the verdict with the counts and the duration, the nodes grouped by identical outcome, a line of its
  own for a node more than 50% slower than the median, a failed or a skipped one, with the reason. ``--check``
  says ``would ...`` and ends the verdict with ``nothing was changed``.
- **TO DO**: numbered, each with its full command, ready to paste (``-i`` and ``-e cassandra_hosts`` only when the
  run needed them). An inventory change is followed by its commit only when the inventory is in a git work tree.
- The marker ``←`` points at what needs a look (``← down``, ``← differs``, ``← slow``).
- **Status** (the ``status`` playbook): a view, read top to bottom, so its verdict comes last. A header line (the
  cluster, the node the ring is seen from, whether the ring and the inventory match, the node count), per datacenter the ``nodetool status`` table and a summary table of
  its racks and the datacenter, where the ring and the inventory differ, then the verdict, blank lines between
  the blocks::

    my_cluster  (seen from node3, ring = inventory, 5 nodes)

    Datacenter: dc1

     --  Address   Load      Tokens  Owns   Host ID  Rack   Inventory
     UN  10.0.0.9  6.88 GiB  16      28.0%  1c7a...  rack2  node5
     DN  10.0.0.5  5.25 GiB  16      22.5%  1e96...  rack3  node1      ← down
     ...

      rack1   1 node    1 up           load  5.0 GiB
      rack2   2 nodes   2 up           load  9.1 GiB
      rack3   2 nodes   1 up, 1 down   load  9.1 GiB
      dc1     5 nodes   4 up, 1 down   load 23.2 GiB

    1 NODE DOWN: node1 (dc1/rack3)

  The verdict: ``ALL 5 NODES UP`` (green under the ops callback), or ``1 NODE DOWN: ...`` (red); then, after
  ``—``, the nodes joining, leaving or moving (``ALL 6 NODES UP — 1 JOINING: node6``, yellow) and ``RING AND
  INVENTORY DIFFER`` (red: a host of the run missing from the ring, a ring node unknown to the inventory or marked
  absent; not a ring node that is another host of the inventory, as under ``--limit``). The ops callback puts the report under a heavy rule and the verdict between two, and
  colours the table rows by the node's state: down red, joining, leaving or moving yellow.

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

A play of your own on the nodes with a ``run_once`` task gathers facts first, or gives each node its own facts
before that task, as the collection's playbooks do (first, a cacheable ``set_fact`` without ``run_once``, tagged
``always``): on ansible-core 2.21 (and the 2.22 pre-releases), when a node's first facts come from a ``run_once``
task, the nodes share them from then on, and the facts gathered after on one node go to all of them
(ansible/ansible#87148). The first node play of each playbook stops when nodes share their facts already (a play
before it in the same run). ``--start-at-task`` and ``--skip-tags always`` skip that first task.

The ``community.cassandra.ops`` stdout callback prints:

- the ``msg`` of a marked task that succeeds (each loop item, handlers too); a marked ``assert`` that passes
  prints its ``success_msg``, nothing without one;
- the ``msg`` of a marked ``assert`` or ``fail`` that fails, as it is (no task header);
- every other failure as the default callback does (the task name, the host, the message, stdout and stderr), a
  failed loop item too; a failure the task ignores (``ignore_errors``) is not printed, one the playbook handles (a
  task of a block with a ``rescue``) is one line (nothing when the block sets ``cassandra_output_rescued``);
- an unreachable host as the default callback does; one line when the play goes on without it
  (``ignore_unreachable``);
- the questions (``pause``), and the diffs under ``--diff``;
- nothing else: no task headers, no ``ok``, ``changed`` or ``skipping`` lines, no retries, no recap.

The output goes by blocks, one blank line between two, never two in a row. A task whose own ``vars`` set
``cassandra_output_gap: true`` (a literal, like ``cassandra_output``) starts a block: a blank line before the next
message (on a message itself, before its own lines; on a question, a ``pause``, above it). The plans, the start of
each progress wait, the recaps, the TO DO lists and the questions set it. A blank line also follows each answer,
shown again (``pause`` clears it), and each phase line. A rule has a blank line above and under it, so text never
touches it.

Inside a plan, a screen or a recap, an empty line ``""`` between two blocks is printed as a light rule; the
paragraphs of one block (the intro's sentences, each fact, each warning, the items of a TO DO that span several
lines) are separated by ``PARAGRAPH`` (``plugins/module_utils/cassandra_output.py``, a line with only a space),
printed as a blank line. Under the default callback (``callback_result_format = yaml``) both show as blank lines in
a text message.

The help playbook's message also sets ``cassandra_output_help: true``: its lines (``flow`` in
``plugins/module_utils/cassandra_output.py``) are laid out at the terminal's width (100 columns at most), its
sections drawn as rules, each operation's name in the ok colour, its commands and their options in the progress
colour (coloured on the screen only, plain in a ``log_path`` file). Other callbacks enabled next to it (``tree``,
``junit``...) record that message with its layout marks.

Task variables the callback reads, each a literal ``true`` in the task's own ``vars``:

- ``cassandra_output``: the task is an operator message;
- ``cassandra_output_gap``: a block of its own (above);
- ``cassandra_output_rescued``: on a block, its rescue says what failed (the failure inside is not printed);
- ``cassandra_output_info``: a summary that only informs (``cassandra_linux``'s data disks), its plain lines in
  the progress colour;
- ``cassandra_output_help``: the help playbook's text, laid out as above.

Colours (Ansible's, so ``ANSIBLE_NOCOLOR``, ``ANSIBLE_FORCE_COLOR`` and an output that is not a terminal apply):

- **green** (ok): ``DONE``, ``HEALTHY``, ``NOTHING TO DO``, ``READY``, ``IMPORT ... PASSED``, a ``CHECK`` with
  nothing left to do, ``ALL 5 NODES UP``, the help's operation names;
- **yellow** (change): a ``CHECK`` that says what a real run would do, ``← differs``, ``will be reset``,
  ``DELETED``, what a node would get or got (``node1  would apply, then restart``, ``node1  applied``), the nodes
  joining, leaving or moving;
- **red** (error): ``REFUSED``, ``FAILED``, ``NOT HEALTHY``, ``IMPORT ... FAILED``, a progress line that stalled
  or failed, ``← down``, a recap's failed node, ``RING AND INVENTORY DIFFER``;
- **cyan** (option ``progress_color``): ``NOTE``, the progress and phase lines, the first line of a plan or a
  screen, an informative summary (``cassandra_output_info``), the help's commands and options;
- **purple**, Ansible's own warning colour (``[colors] warn`` in ``ansible.cfg``, bright purple by default; the
  option ``warning_color`` sets another one): every ``WARNING`` line, indented or not, an informative summary's
  too, as Ansible's own ``[WARNING]`` lines; apart from the yellow of the changes.

The lines indented under a coloured one keep its colour, unless they carry a mark of their own or a ``WARNING``.

With ``-v`` or more, it is the default callback. Under the default callback, the same messages show as the
``msg`` of their task (as plain text with ``callback_result_format = yaml``).

Blocks
------

``cassandra_node_list``, ``cassandra_size``, ``cassandra_rate``, ``cassandra_duration``,
``cassandra_setting_lines``, ``cassandra_settings_lines``, ``cassandra_by_nodes``, ``cassandra_plan``, ``cassandra_progress_line``,
``cassandra_recap``, ``cassandra_perm_lines``, ``cassandra_diff_lines``, ``cassandra_changed_lines``,
``cassandra_command``, ``cassandra_todo``, ``cassandra_inventory_steps``, ``cassandra_in_git_work_tree``,
``cassandra_mask``: their arguments are in ``plugins/filter/cassandra_output.py`` and the module_utils functions
they call. The ``status`` playbook (``cassandra_ring_report``) and ``health_check`` (``cassandra_health_report``)
use them.
