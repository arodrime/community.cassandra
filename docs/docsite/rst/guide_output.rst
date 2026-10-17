.. _ansible_collections.community.cassandra.docsite.guide_output:

Operator output
===============

What the playbooks print, and how a playbook prints it. The text comes from
``plugins/plugin_utils/cassandra_output.py`` (filters in ``plugins/filter/cassandra_output.py``): every block is
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
  non-zero size). A done/total pair says the unit once, the one the total reads in: ``305.1/329.8 MiB``,
  ``<0.1/100.0 GiB``; done is never shown above the total.
- **Plans**: the header, the numbered steps (node and dc/rack), the facts, then the ``WARNING`` lines just above
  the question.
- **Warnings**: one style, ``WARNING - what``, the lines of a longer one indented under it; two warnings a blank
  line apart; each one said once per run, where it is decided (a plan's warnings not again in its recap, which
  says how many are above: ``3 warnings in the plan above (node2, node3).``).
- **Phases**: one line per major phase, ending with ``...`` (cyan under the ops callback: its option
  ``progress_color``), e.g. ``Checking the cluster (3 nodes; adding node4, node5)...`` (the members: not the nodes
  being added or reset, nor those marked absent), ``Preparing the new nodes (node5, node6)...``, ``Starting node5 and waiting for its
  bootstrap...``, ``Decommissioning node3...``, ``Waiting for every node to be UN...``, ``Restarting node2...``. Their
  words are in one place (``PHASES`` in ``plugins/plugin_utils/cassandra_output.py``, filter
  ``community.cassandra.cassandra_phase``); ``--check`` says the reading phases only.
- **Prepare of new nodes** (add_node, replace_node, add_datacenter, create_cluster): after its phase line, one line
  per step, the nodes with the same outcome together, in the progress colour::

    node4, node5  repository  ok (already set)
    node4         install     cassandra 5.0.9 (installed now)
    node5         install     cassandra 5.0.9 (already installed)
    node4, node5  os tuning   2 changes
    node4, node5  data disks  read-ahead 4 KB already set
    node4, node5  config      3 files written
    node4, node5  firewall    off (cassandra_manage_firewall: false)

  ``would ...`` under ``--check`` (``prepare_lines``, filter ``community.cassandra.cassandra_prepare_lines``,
  from each role's change report: the lists per role and node with ``-v`` or in ``cassandra_change_report_dir``).
  The data disks' summary follows only when something was set, a line to act on (not tuned yet, a restart to take it), a warning, or with ``-v``.
- **Progress**: one line per check, then always the nodes at the other end, in aligned columns (as many as fit
  in the terminal, 4 at most; here 100 columns), by name, a stalled one in place, its line in red::

    [1/2] node5 bootstrap  JOINING  [####------]  46%  25.2/54.0 GiB  89 MiB/s  ETA 13:33 (5m)  10m
          from node1  18.0/34.0 GiB   52%           from node2    3.0/3.0 GiB  100%
          from node3   4.2/17.0 GiB   24% STALLED

  Above 30 of them (a node streaming both ways counts twice), one line sums them up: the ones done, the ones
  keeping up and their average, the stalled ones, the ones behind (below half the average of all the ones not
  done, once that is 10%) and the 3 slowest (as many as fit); only the stalled ones and the ones behind are
  detailed under it, 12 at most (the others counted, ``+13 more: 8 STALLED, 5 behind``)::

    [1/1] node3 decommission  LEAVING  [####------]  43%  282.2/648.0 GiB  410 MiB/s  ETA 16:01 (15m)  2h10m
          90 peers: 87 ok (avg 44%), 1 STALLED, 2 behind; slowest: node5 0% STALLED, node31 12%
          to node5   0.0/7.2 GiB   0% STALLED   to node31  0.9/7.2 GiB  12%
          to node44  0.9/7.2 GiB  12%

  A check every 10 s for the first 5 minutes, then every 30 s; after a minute without progress the line says it,
  as progress (``no progress for 3m``), and after ``cassandra_stream_stall_warn_time`` (5 min) as an error
  (``STALLED 6m/30m``: the time without progress and the limit, ``cassandra_stream_stall_time``). On a node that
  receives (bootstrap, replace, rebuild), the growth of its used disk space counts as progress
  (``receiving (disk +3.2 GiB in 5m)``: a whole SSTable is counted in netstats once sent).

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
  ``—``, the nodes joining, leaving or moving (``ALL 6 NODES UP — 1 JOINING: node6``, yellow). Under it, a second
  line ``WARNING - ring and inventory differ`` (the warning colour) when a host of the run is missing from the ring,
  or a ring node is unknown to the inventory or marked absent (not a ring node that is another host of the inventory, as under
  ``--limit``); the lines above it say which. The ops callback puts the report under a heavy rule and the verdict
  (both lines) between two, and colours the table rows by the node's state: down red, joining, leaving or moving yellow.

Where a node stands
-------------------

Every operation that adds, empties or replaces a node decides from one reading of where the node stands
(``roles/cassandra_service/tasks/node_standing.yml``, the filter ``community.cassandra.cassandra_node_standing``),
read once per step (each deciding play reads the node again): whether Cassandra runs there (its JVM, or the
``cassandra`` unit active), the ring the other nodes of the cluster see (each running node reads its own, all at
once, in the preflight; for a node holding data, a node alone in its ring does not stand for the cluster while
others did not answer or were stopped), what its Cassandra directories hold (the data directories its config uses are the inventory's and the live ``cassandra.yaml``'s; a
system keyspace left in the package's default directory that its config does not use is not the node's: said,
left as it is), the cluster its live ``cassandra.yaml`` names, and the mark ``add_node`` leaves on a node while it
bootstraps. The output gives the state in its words, with the reasons::

    node7 (dc1/rack_b): holds 12.0 GiB of cluster 'my_cluster' (keyspaces shop), not in the ring, stopped
      — a former member of this cluster: the writes only it holds would be lost if wiped

.. list-table::
   :header-rows: 1
   :widths: 14 34 24 14 14

   * - state
     - its words
     - ``add_node``, ``topology``
     - ``reset_node``
     - ``replace_node``
   * - ``blank``
     - blank: nothing in its Cassandra directories, Cassandra stopped, not in the ring
     - added
     - nothing to delete (kept from starting at boot); refused when no node of the cluster answers UN
     - replaces
   * - ``failed_bootstrap``
     - never joined: data of this cluster or of the package's stock config, no user keyspace but those of a bootstrap add_node started there; not in the ring, Cassandra stopped
     - emptied, then added (``-e cassandra_add_node_reset=false``: refused)
     - emptied
     - emptied first with ``-e cassandra_replace_node_reset=true``, else refused
   * - ``former_member``
     - former member: this cluster's keyspaces on disk, not in the ring, Cassandra stopped
     - refused; ``-e cassandra_add_node_reset=true``: emptied, then added
     - emptied
     - emptied first with ``-e cassandra_replace_node_reset=true``, else refused
   * - ``unidentified_data``
     - data not known to be this cluster's (no readable cassandra.yaml, user keyspaces or other nodes met under the stock cluster name, keyspaces this cluster does not have or could not be read); not in the ring, Cassandra stopped
     - refused, what it holds said
     - emptied
     - emptied first with ``-e cassandra_replace_node_reset=true``, else refused
   * - ``other_cluster``
     - another cluster's node: its cassandra.yaml names another cluster, or Cassandra runs there in another ring
     - refused
     - refused
     - refused
   * - ``running_outside_ring``
     - Cassandra running alone, outside the cluster's ring
     - refused: stop Cassandra first
     - stopped, then emptied
     - emptied first with ``-e cassandra_replace_node_reset=true``, else refused
   * - ``member``
     - in the ring, up (UN), under its own host ID
     - not added again (joined)
     - refused
     - refused
   * - ``member_joining``
     - joining: UJ in the ring under its own host ID, a bootstrap running
     - its bootstrap waited for again
     - refused
     - refused
   * - ``member_down``
     - in the ring, down (DN): Cassandra stopped there
     - refused: start it
     - refused
     - refused
   * - ``member_busy``
     - in the ring, leaving or moving (UL, UM), or joining (UJ) without its host ID or system keyspace read here
     - refused: wait until UN
     - refused
     - refused
   * - ``dead_address``
     - a dead node (DN) of the ring has its address, and it holds no system keyspace
     - refused: run replace_node
     - refused
     - refused
   * - ``address_conflict``
     - the ring has another node at its address (another host ID, or this one does not answer)
     - refused
     - refused
     - refused
   * - ``unknown``
     - can't be told: something needed could not be read or checked
     - refused, what could not be read said
     - refused
     - refused

``cassandra_config`` (``cassandra_config_confirm: auto``) asks before changing a file of a node that is not blank,
and keeps the identity (cluster name, tokens, dc and rack) such a node started with. Run on its own (``apply_config``,
a role in your play), it reads the node itself and, as a precaution, also counts a system keyspace left in a usual
place its config does not use (the package's default directory, a tarball's ``data/data``, a path its live
``cassandra.yaml`` names); on a node the operation checked and asked about itself (``add_node``, ``topology``,
``replace_node``, ``create_cluster`` after its reset) it asks nothing, only its own data directories and a running
Cassandra count there. ``create_cluster`` makes a
new cluster (``cassandra_storage_compatibility_mode`` may be left out of a 5.0 one) only when every node of the
group is blank, with no system keyspace left in the package's default directory either. ``import_cluster``
reports a ring node it could not read in the same words (``in the ring, down (DN): Cassandra stopped there``).

One word per meaning, in the output:

- **blank**: nothing in its Cassandra directories, Cassandra stopped (and, as a state, not in the ring); **not
  blank** says why (``/var/lib/cassandra/data/system``, ``Cassandra is running``, what could not be read).
- **in the ring**: a node of the cluster that is up and normal (UN) lists it (``node1 sees 10.0.0.7 in its ring
  (DN, host ID ...)``); **member**: in the ring under its own host ID; **in the group**: in the cluster's
  inventory group, said as such.
- **never joined** (``failed_bootstrap``), **former member** (``former_member``): its data on disk, not in the
  ring.
- **joining**: UJ, a bootstrap running; **joined**: ``add_node`` found it in the ring already (a bootstrap that
  went through). A node back after a restart is up (Mode NORMAL), never "joined".
- **Cassandra running** / **Cassandra stopped**: on that node (its JVM, or its unit); **down** (DN): as the ring
  sees it.

Printing a message
------------------

A task is an operator message when its own ``vars`` set ``cassandra_output: true`` (a literal true, on the task
itself). Its ``msg`` is a list of lines (or one string with newlines). Cluster-wide messages run once:

.. code-block:: yaml

    - name: Say what is checked
      ansible.builtin.debug:
        msg: "{{ 'check_leaving' | community.cassandra.cassandra_phase(ansible_play_hosts_all) }}"
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
lines) are separated by ``PARAGRAPH`` (``plugins/plugin_utils/cassandra_output.py``, a line with only a space),
printed as a blank line. Under the default callback (``callback_result_format = yaml``) both show as blank lines in
a text message.

The help playbook's message also sets ``cassandra_output_help: true``: its lines (``flow`` in
``plugins/plugin_utils/cassandra_output.py``) are laid out at the terminal's width (100 columns at most), its
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
  ``DELETED``, what a node would get, will get or got (``node1  would apply, then restart``, ``node1  will apply``,
  ``node1  applied``), the nodes
  joining, leaving or moving;
- **red** (error): ``REFUSED``, ``FAILED``, ``NOT HEALTHY``, ``IMPORT ... FAILED``, a progress line that stalled
  or failed, a line of the other ends with a ``STALLED`` one, ``← down``, a recap's failed node,
  ``1 NODE DOWN: ...``, ``1 NODE NOT REACHED: ...``;
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

``cassandra_node_list``, ``cassandra_size``, ``cassandra_duration``, ``cassandra_progress_line``,
``cassandra_phase``, ``cassandra_check_what``, ``cassandra_prepare_lines``, ``cassandra_left_system_warning``,
``cassandra_command``, ``cassandra_todo``, ``cassandra_removed_todo``, ``cassandra_seeds_todo``,
``cassandra_seeds_reload_todo``, ``cassandra_reimport_hosts``: their arguments are in
``plugins/filter/cassandra_output.py``, with the plugin_utils functions they call. The other blocks (the plan, the
recap, the settings and diff lines, masking) are the plugin_utils functions themselves, which the screens of the
operations call: the ``status`` playbook (``cassandra_ring_report``) and ``health_check``
(``cassandra_health_report``), for example.
