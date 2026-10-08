.. _ansible_collections.community.cassandra.docsite.guide_roles:

Running Cassandra clusters with the roles
=========================================

The roles take a Debian, Ubuntu or RedHat family host from a blank system to a
running Cassandra node, for Cassandra 4.0, 4.1 and 5.0.

.. contents::
   :local:
   :depth: 1


The roles
---------

Run them in this order:

1. :ansplugin:`community.cassandra.cassandra_repository#role`: the Apache Cassandra package repository.
2. :ansplugin:`community.cassandra.cassandra_install#role`: Java for the series, Cassandra, and a cqlsh that works.
3. :ansplugin:`community.cassandra.cassandra_linux#role`: kernel settings, swap, transparent huge pages, limits, data disk.
4. :ansplugin:`community.cassandra.cassandra_config#role`: ``cassandra.yaml``, ``cassandra-env.sh``, JVM options,
   ``cassandra-rackdc.properties`` and ``logback.xml``.
5. :ansplugin:`community.cassandra.cassandra_firewall#role`: firewalld or ufw.
6. :ansplugin:`community.cassandra.cassandra_service#role`: systemd unit, start, wait until the node has joined.

Optional: :ansplugin:`community.cassandra.cassandra_medusa#role` installs Cassandra Medusa for backups (see `Backups`_).

Each role handles the host it runs on. Ordering nodes is up to the playbook.

The series is set with ``cassandra_version`` (``40x``, ``41x`` or ``50x``). Set it once for all the roles.

With the default values, the configuration files are the stock ones of the series, apart from the directories the
deb and rpm packages set themselves. Only what you set changes.


Project setup
-------------

Every step is a plain ``ansible-playbook`` call. A project directory with an ``ansible.cfg`` holds what each call
would otherwise repeat:

.. code-block:: text

    project/
      ansible.cfg
      collections/                    # ansible-galaxy collection install -p ./collections <collection tarball or name>
      inventories/                    # one inventory for every cluster
        orders.yml                    # the hosts of each cluster, as import_cluster writes them
        billing.yml
        group_vars/all/local.yml      # your own settings (mirror, ...): kept by a re-import
        group_vars/orders/main.yml    # written by import_cluster, as the groups of each datacenter and rack
        host_vars/node1/main.yml
      reports/orders/report.txt       # the import's report, outside the inventory

.. code-block:: ini

    # ansible.cfg (a sample; relative paths are from this file's dir)
    [defaults]
    collections_path = ./collections
    inventory = ./inventories
    # read by Ansible for every run (which fails if it is missing), and by import_cluster to
    # encrypt the passwords it finds; keep it outside the project, mode 0600
    vault_password_file = ~/.ansible/vault_pass
    # only what an operator reads: plans, progress, recaps, failures (-v: everything)
    stdout_callback = community.cassandra.ops
    callback_result_format = yaml
    interpreter_python = auto_silent

``stdout_callback = community.cassandra.ops`` prints only the collection's operator messages (each verdict, plan,
progress line, recap and TO DO list), the confirmation questions, and every failure in full (the task, the host,
its message and stderr), instead of a header and a line per host for each of the hundreds of tasks of an
operation. With ``-v`` or more, the output is the default callback's. Without it (``stdout_callback =
ansible.builtin.default``), ``callback_result_format = yaml`` shows the same messages as plain text. The
conventions of these messages, and how a playbook writes them: :ref:`ansible_collections.community.cassandra.docsite.guide_output`.

No ``become`` in ``ansible.cfg`` (nor ``-b``): the collection's playbooks ask for root on the nodes themselves, where
they need it (``status`` and ``health_check`` only to read ``cassandra_jmx_password_file``, ``help`` nowhere), and
never on the controller; ad hoc ``ansible`` commands stay unprivileged. Add ``-K`` when sudo asks for a password.
The roles need root too, but do not ask for it: your own playbook that applies them (``site.yml``) sets
``become: true`` on its play. An inventory ``ansible_become: false`` beats the playbooks' ``become``: the operations
then stop before changing anything, unless Ansible already logs in as root there (nodes without sudo:
``ansible_user: root``, or ``ansible_become: false``). An inventory ``ansible_become: true`` makes ``status`` and
``health_check`` run ``nodetool`` as root too, for nodes where it needs root.

Then, naming the cluster on each run (see `Inventory`_):

.. code-block:: console

    $ ansible-playbook -i node1, community.cassandra.import_cluster
    $ ansible-playbook community.cassandra.health_check -e cassandra_hosts=orders
    $ CASSANDRA_CLUSTER=orders ansible-playbook community.cassandra.decommission_node -e cassandra_leaving_nodes=node7

Without ``-i node1,`` (a re-import with the inventory of ``ansible.cfg``), the import is given every host of the
inventory, with their variables (the JMX login, the connection): fine when it holds this cluster alone; with other
clusters there, add ``-e cassandra_hosts=orders`` (``--limit`` is refused: it would leave out the nodes found in the ring
and the controller, where the inventory is written). Given nodes of two rings stop the import before it reads them.

Ansible reads the ``group_vars`` and ``host_vars`` next to an inventory source only: with ``inventory =
./inventories``, the ones of a subdirectory (``inventories/orders/group_vars``) are not read. Hence one flat
directory, the one the import writes (``import_cluster_dir``, default ``inventories``): each cluster's hosts in
``<cluster>.yml``, every group's variables side by side in ``group_vars`` (``orders``, ``orders_dc1``... the import
prefixes each group with the cluster's).

Ansible ignores an ``ansible.cfg`` in a world-writable dir; ``ANSIBLE_CONFIG=<path>`` names one explicitly.

On Ubuntu 26.04, ``sudo`` is sudo-rs, whose password prompt Ansible's ``sudo`` become method does not recognise (the
run stops on "timeout waiting for privilege escalation password prompt"). Passwordless sudo works; with a sudo
password, use the classic sudo (package ``sudo``): ``ansible_become_exe: sudo.ws``.

Inventory
---------

Use one group per cluster and one group per datacenter (the playbooks below take the cluster group as
``cassandra_hosts``). Settings shared by the cluster go in the cluster's
``group_vars``, the datacenter in the datacenter's, and per node settings in ``host_vars``.

.. code-block:: yaml

    # inventory.yml
    all:
      children:
        orders:
          children:
            orders_dc1:
              hosts:
                node1:
                node2:
                node3:
            orders_dc2:
              hosts:
                node4:
                node5:
                node6:

Without ``-e cassandra_hosts=<group>`` (nor ``CASSANDRA_CLUSTER``, below), the playbooks run on the group
``cassandra`` when the inventory has one with hosts, else on the inventory's cluster group when it holds one cluster laid out as the import writes it: the group
whose name starts every other group's and that holds their hosts (``orders`` here, for ``orders_dc1`` and
``orders_dc2``; ``all``, ``ungrouped`` and the groups the playbooks make while they run left out). Any other group
(a second cluster, ``monitoring``, ``linux``, a group of your own not named ``<cluster>_...``) makes them stop with the top groups listed rather
than guess: give ``-e cassandra_hosts=<group>`` then. With an inventory of one cluster only (``-i
inventories/orders.yml``, or a directory of your own per cluster) the playbooks need no ``cassandra_hosts``. The
same rule is the lookup ``community.cassandra.cassandra_hosts``; the groups are read each time, so a group your own
plays add earlier in the same run (``group_by``) makes it stop the same way.

The environment variable ``CASSANDRA_CLUSTER`` names the cluster too, when ``cassandra_hosts`` is not set (which wins
over it): a group of the inventory, or a ``cassandra_cluster_name`` (exact, case included), which stands for the
group whose hosts are the ones with that name (a group laid out as a cluster's: ``cassandra``, or one with its
``<group>_...`` groups). A value that matches nothing, a cluster name several groups have, or one some hosts may have
without the inventory telling (a fact of the node), stops the run before anything is done. The first task of each operation shows the group and where it comes from.

.. code-block:: console

    $ export CASSANDRA_CLUSTER=orders
    $ ansible-playbook community.cassandra.rolling_restart

An exported ``CASSANDRA_CLUSTER`` stays set for every later command of the shell: show it in the prompt, e.g. at the
end of the virtualenv's ``bin/activate``:

.. code-block:: sh

    PS1='${CASSANDRA_CLUSTER:+[$CASSANDRA_CLUSTER] }'"$PS1"

.. code-block:: yaml

    # group_vars/orders.yml
    cassandra_version: 50x
    cassandra_cluster_name: orders
    cassandra_num_tokens: 16
    cassandra_partitioner: org.apache.cassandra.dht.Murmur3Partitioner
    cassandra_allocate_tokens_for_local_replication_factor: 3
    cassandra_storage_compatibility_mode: NONE
    cassandra_seeds: [10.0.1.11, 10.0.2.11]
    cassandra_endpoint_snitch: GossipingPropertyFileSnitch
    cassandra_authenticator: PasswordAuthenticator
    cassandra_authorizer: CassandraAuthorizer
    cassandra_heap_size: 8G

    # group_vars/orders_dc1.yml
    cassandra_dc: dc1

    # host_vars/node1.yml
    cassandra_listen_address: 10.0.1.11
    cassandra_rpc_address: 10.0.1.11
    cassandra_rack: rack1

The seeds are listed explicitly. They are the same list on every node of the cluster, and a good choice of contact
points for clients too. Pick one node per rack, two or three per datacenter.

With the stock ``allocate_tokens_for_local_replication_factor: 3``, a datacenter needs either one rack or at
least three.

A node keeps some settings for life: ``cluster_name``, ``num_tokens``, ``partitioner``, the snitch, its datacenter
and rack, and, on 5.0, ``storage_compatibility_mode`` until an upgrade moves it. Set them explicitly in the inventory,
as above, rather than relying on the role defaults: a default that changes in a later release of the collection
must not change what the next node of your cluster gets. ``create_cluster`` refuses to start without them
(``-e cassandra_accept_default_identity=true`` to go on anyway). ``cassandra_storage_compatibility_mode: NONE`` is
right for a new 5.0 cluster; a cluster upgraded from 4.x keeps ``CASSANDRA_4`` until its upgrade is complete.


Operation playbooks
-------------------

The collection has playbooks for the usual operations on a cluster. Each one works on one inventory group
(the inventory's cluster group, ``-e cassandra_hosts=<group>`` or ``CASSANDRA_CLUSTER``, see `Inventory`_) and starts with ``preflight``, which checks that the settings that must match do
match on every node, that the racks suit the token allocator, and that the seeds are a sensible layout (it suggests
a seed list when they are not). It also checks that the account Cassandra runs as can read the config files, and
warns about the ``cassandra_*`` variables set that no role or playbook knows (a typo, or a name from another version:
they have no effect), with the known one they are close to.

.. code-block:: console

    $ ansible-playbook -i inventory community.cassandra.preflight
    $ ansible-playbook -i inventory community.cassandra.create_cluster
    $ ansible-playbook -i inventory community.cassandra.add_node -e cassandra_new_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.rolling_restart
    $ ansible-playbook -i inventory community.cassandra.apply_config
    $ ansible-playbook -i inventory community.cassandra.health_check
    $ ansible-playbook -i inventory community.cassandra.status
    $ ansible-playbook -i inventory community.cassandra.cleanup
    $ ansible-playbook -i inventory community.cassandra.decommission_node -e cassandra_leaving_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.topology --check
    $ ansible-playbook -i inventory community.cassandra.replace_node -e cassandra_new_nodes=node9 -e cassandra_replace_address=10.0.1.14
    $ ansible-playbook -i inventory community.cassandra.reset_node -e cassandra_reset_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.change_seeds
    $ ansible-playbook -i node1 community.cassandra.import_cluster

Operations that touch running nodes check the whole cluster before and after each node: every node up and normal,
gossip and the native transport running, no streams, schema agreement, and the storage and CQL ports answering. A
node is only touched when the cluster is healthy, and the run stops at the first node that does not come back
healthy (``cassandra_service_health_force: true`` goes on anyway, at your own risk). ``health_check`` runs the same
checks on its own, changing nothing, and fails when there is a problem, so it can be scheduled.
``status`` only shows the ring as one node sees it (the first that answers, or ``cassandra_status_from``), per
datacenter with the nodes up, down, joining, leaving and moving, the total load, and the hosts the inventory and the
ring do not share; a down node is shown, not an error. ``cassandra_status_raw: true`` adds nodetool's own output.

Risky operations show one screen first, the same layout for each: a header (the operation, the cluster and its running
version), one block per node concerned (when the operation works node by node), then the warnings, each one labelled
(``WARNING - replication: ...``) and on its own paragraph. Then they ask for confirmation: ``yes`` (or ``y``) goes on, ``no`` (or ``n``) stops, any other answer
asks again, three times at most. ``cassandra_operation_confirm: false`` skips the question, for runs without a
terminal; without one, a run that would ask fails at once. ``--check`` shows the screen, says that nothing will be
changed and asks nothing (``add_node`` with ``cassandra_token_auto: true`` follows bisect instead of asking); the
warnings that only concern a real run (the SSH session, data deleted for good) are named on one line instead. A
reset (see `Resetting a node`_) shows each node's plan under ``--check``, not the screen.

Rolling operations record each node done in a progress file on the controller, in ``.cassandra_progress`` next to
the inventory (in the current dir when the inventory's dir is not writable and has no ``.cassandra_progress`` yet, or
with ``-i host1,host2``), or in ``cassandra_rolling_progress_dir``. An interrupted run resumes where it stopped with
``-e cassandra_rolling_resume=true``, run with the same inventory from the same dir. A node the interrupted
``rolling_restart``, ``rolling_reboot``, ``apply_config`` or ``update_java`` (one node at a time) left drained or
stopped is restarted first: it may be down then, any other node down still stops the run. The
files are written as the user running Ansible, never as root: add ``.cassandra_progress`` to the inventory's
``.gitignore``.

Help and runbook
----------------

``help`` reads the inventory only (no node is contacted, nothing changes) and prints three sections: each cluster
as the inventory describes it (name, Cassandra series and package version, install method, Java, datacenters, racks
and their nodes, seeds, the nodes marked ``cassandra_node_state: absent``); every operation playbook by theme, with
its command filled for this inventory (the inventory's path, ``-e cassandra_hosts`` when the inventory holds
several clusters, ``CASSANDRA_CLUSTER`` names another group or the dir is shared with other clusters, a datacenter and rack of it, the nodes marked absent, and the vault and
user options the run was given; placeholders such as ``NEW_NODE`` or ``NODE`` are values only you know); and advice from the inventory (nodes
marked absent, authentication on without ``cassandra_cql_username``, a variable close to one the collection reads,
seeds not one per rack, racks against ``allocate_tokens_for_local_replication_factor``, mixed versions).

.. code-block:: console

    $ ansible-playbook -i inventories/orders.yml community.cassandra.help
    $ ansible-playbook -i inventories/orders.yml community.cassandra.help -e help_topic=decommission_node
    $ ansible-playbook -i inventories/orders.yml community.cassandra.help -e help_write=true

``-e help_topic=<operation>`` shows one operation in detail: its command, its options one per line with their
default, an example for this inventory (where one helps), and its documentation (the comment that starts the
playbook). The default callback shows the help as plain text under its yaml result format, as a list of quoted
lines under its json one (the default): set it in ``ansible.cfg`` (or ``ANSIBLE_CALLBACK_RESULT_FORMAT=yaml``):

.. code-block:: ini

    [defaults]
    callback_result_format = yaml

``-e help_write=true`` also writes the overview as ``RUNBOOK.md``, each operation with its options and example, in
the inventory's dir (the first ``-i`` one), or in the existing dir given as ``-e help_runbook_dir=<dir>``, with the commands ready to copy: commit it with the inventory. It is
written only when its content changes (run ``help`` again after changing the inventory); ``--check --diff`` shows the
difference. Its commands are as run from the directory ``help`` was run from (the one with ``ansible.cfg``; the file
says where that is from its own dir), with the ``-i`` path and the vault and connection options ``help`` was given:
run it the same way each time, or the file changes. For one cluster of an inventory dir, next to its import report:

.. code-block:: console

    $ ansible-playbook -i inventories community.cassandra.help -e cassandra_hosts=orders -e help_write=true -e help_runbook_dir=reports/orders

``help`` decrypts nothing, even when given the vault password: the vault-encrypted vars files are skipped and named
in the advice, inline vaulted values are shown as ``(vaulted)``, so no secret reaches its output or ``RUNBOOK.md``.
A value templated from a vaulted one is shown as ``(vaulted)`` too, a template that would run a lookup is shown as
written, and the ``-e`` variables of the ``help`` run are not read. A shown setting that can't be read from the
inventory alone is named in the advice. When the inventory has vaulted values and the run has no vault password
(``--vault-password-file``, ``--vault-id``, ``--ask-vault-pass``, or one in ``ansible.cfg``), the printed commands
carry ``--ask-vault-pass``. ``cassandra_node_state: absent`` marks a host to remove: ``help`` lists it apart and
names it in the ``decommission_node`` command; the other operations still treat it as a node of the cluster.
A vault-encrypted file in ``group_vars/all`` is read by Ansible for every host, ``localhost`` too: ``help`` then
needs the vault password as well (and still shows nothing from it).


Creating a cluster
------------------

``create_cluster`` prepares every node in parallel, then starts the seeds one at a time, then the other nodes, each
one joined before the next. Running it again on a running cluster starts nothing.

On a new node, :ansplugin:`community.cassandra.cassandra_install#role` doesn't let the package start Cassandra
with its stock configuration, so the node first starts with its real configuration.

A node keeps its snitch, datacenter and rack for life once it has joined, so moving a cluster to other ones means
building it again. ``-e cassandra_create_cluster_reset=true`` does that on a cluster that runs already, ALL ITS DATA
LOST: it is refused when ``--limit`` leaves a host of the group out, when no node answers ``nodetool status``, when
the ring has a node the group does not have (never wipe part of a cluster that keeps running), when a running node
is not in that ring or the running nodes see different rings, when a node holding data is not in that ring, when a
Cassandra runs that its unit did not start, or when a node's live ``cassandra.yaml`` names another cluster. With one token per node, the new tokens are shown and confirmed first. Each node's plan is shown (see
`Resetting a node`_), then the operator types the cluster name (for a run without a terminal,
``-e '{"cassandra_create_cluster_reset_confirm": "<cluster name>"}' -e cassandra_operation_confirm=false``); every
node is stopped, kept from starting at boot and emptied, then the cluster is created with the inventory's settings.
``--check`` shows the plan only. If the create fails after the wipe, run ``create_cluster`` again without the option.


Adding a node
-------------

Add the host to the inventory, in its datacenter's group, without adding it to ``cassandra_seeds``, then:

.. code-block:: console

    $ ansible-playbook -i inventory community.cassandra.add_node -e cassandra_new_nodes=node7

The other nodes are not touched. A node that has never started and is listed in ``cassandra_seeds`` is refused while
another seed answers: seeds don't bootstrap, so it would join without its data. Add it, then make it a seed.

Before installing anything, ``add_node`` and ``replace_node`` check the new hosts and stop with every problem found at
once: Ansible runs as root; the data, commitlog, hints and saved_caches directories are empty and on the file system
``/etc/fstab`` or an enabled systemd mount unit expects (not on the root file system because a disk is not mounted),
with their free space shown, a warning when the nodes of the same rack hold more than half of it on average, and
``cassandra_new_node_min_free_gb`` as a minimum; Java and the Pythons of cqlsh and Medusa are installed, or available
from the configured repositories (installed already with ``cassandra_offline``); the Cassandra repository, the Java
tarball and the Medusa pip index answer with their credentials, and when the repositories are set up by other means
(``cassandra_repository_manage: false``, ``cassandra_offline``) Cassandra is available or installed at
``cassandra_package_version``; the storage port of two seeds answers from the new host (their native port too, or a
warning), its own Cassandra ports are free and Cassandra is not running there; a host with no Cassandra installed does
not keep the ``cassandra_linux_manage``, ``cassandra_cqlsh_python_manage`` or ``cassandra_service_unit_manage`` false
that ``import_cluster`` wrote for a node (marked ``cassandra_imported_host: true``), left there when the host is rebuilt
under its name: remove them, or set ``cassandra_new_node_allow_kept_setup: true`` when the host is set up another way.
The same switches set by the operator, e.g. in ``group_vars``, are left to them. A source that does not answer at all
is only a warning: the package managers and pip may go through a proxy of their own. ``-e cassandra_new_node_checks=false`` skips these checks.

Before anything starts, ``add_node`` shows one screen and asks once (``cassandra_operation_confirm: false`` for
non-interactive runs): the cluster and its running version, each new node with its address, datacenter, rack and an
estimate of the data it will receive (from ``nodetool status``) and its Medusa fqdn, the Cassandra, Java and Medusa
(on or off) it gets, the cleanup choice, and warnings: racks of a datacenter left with different node counts (their
nodes then hold different shares of the data), and a run not inside ``tmux`` or ``screen`` on the controller (a lost
SSH session stops the run).

Each new node bootstraps: it streams its share of the data, hours on big nodes. The playbook prints its progress
every ``cassandra_stream_check_interval`` seconds (300 by default; the first checks sooner, after 10 s, 30 s, 1, 2
and 4 minutes, so a short operation ends in seconds): a first line with the node, a bar, the percentage
and the rate over the last 3 checks, then the bytes and files streamed, each node it streams from with its own
progress, and the times on the controller (now, started, expected end); a single line with the total time and average
rate once done. It waits as long as the streams make
progress: it stops only after ``cassandra_stream_stall_checks`` checks in a row (3), a full interval apart, with nothing streamed (4 times as
many while nothing is left to transfer). If the run stops before the node has joined (a stall, a lost SSH session),
the node goes on bootstrapping: run ``add_node`` again with the same nodes, it waits for the bootstrap in progress.
The wait also stops when Cassandra stops or, on 5.0, when the bootstrap fails (``Mode: JOINING_FAILED``). To start a
failed bootstrap over, stop Cassandra on the node, wait until it is gone from ``nodetool status``, and run ``add_node``
again with ``-e cassandra_add_node_reset=true`` (see `Resetting a node`_). ``replace_node``, ``decommission_node``,
``remove_dead_node`` and the rebuild of ``add_datacenter`` wait the same way.

Once the new nodes have joined, the others still hold the data they handed over: ``add_node`` prints the ``cleanup``
command for the nodes concerned (the datacenter's nodes, or only the new nodes' racks when every keyspace has as many
replicas as racks there), or runs it with ``cassandra_add_node_cleanup``: ``sequential`` (a node at a time, ``one`` is
the same), ``rack``, ``dc`` or ``all`` (nodes cleaned together), the cluster checked before each batch. The ``cleanup`` playbook removes that data, with
``cassandra_cleanup_mode`` ``sequential`` (default, one node at a time), ``rack``, ``dc`` or ``all`` (every node at
once, heavy disk I/O everywhere), and ``cassandra_cleanup_jobs`` threads per node.


One token per node
------------------

With ``cassandra_num_tokens: 1`` each node owns one range of the ring, from the token of the node before it to its
own ``initial_token`` (``cassandra_initial_token``). Where the tokens sit decides how much data each node holds, so the
playbooks work them out:

- ``create_cluster`` gives the nodes without ``cassandra_initial_token`` evenly spaced tokens: in each datacenter, node
  *i* of *N* at ``-2^63 + i * 2^64 / N`` (Murmur3Partitioner; ``i * 2^127 / N`` with RandomPartitioner), the racks
  taken in turn so that consecutive tokens are on different racks (racks sorted by name, each rack's nodes in
  inventory order), and each datacenter 100 tokens after the one before it (datacenters sorted by name): tokens never
  collide, and each datacenter, which NetworkTopologyStrategy replicates on its own, is even (with racks of the same
  size; with uneven racks some nodes hold more, and the run warns). ``add_datacenter`` does the same for the new
  datacenter (the next free offset when its own is taken). The ring is shown with
  each node's share before anything starts, and confirmed (``cassandra_operation_confirm``). A datacenter where only
  some nodes have a token is refused, unless ``cassandra_token_allow_partial: true`` (the others then split the
  largest ranges). Copy the tokens shown into the inventory to keep a record: a node that has joined keeps the
  ``initial_token`` its ``cassandra.yaml`` has when the inventory gives none. Run again on a running cluster, it keeps
  the tokens of the running nodes; it only gives tokens to the others when the running nodes have the ones it worked
  out (a create that stopped half way), otherwise add them with ``add_node``.
- ``add_node`` needs a token for each new node: ``cassandra_initial_token``, or ``cassandra_token_auto``: ``bisect``
  (each new node splits the largest range, no node moves), ``balanced`` (an even ring for the new node count: the new
  nodes join at their final tokens, then ``move_node`` moves the others; only when no new node has a token in the
  inventory) or ``true`` (both are shown, with each node's share before and after, and you choose). Going from *N* to *N + 1* even nodes moves nearly every node; going
  to *2N* moves none: every range is split in two (the new nodes' racks are ordered so that racks keep alternating
  when they can). The screen says so when bisect leaves the ring uneven.
- ``move_node`` moves nodes to new tokens, one at a time (``nodetool move``): without ``cassandra_move_tokens``, each
  datacenter is evened out with the fewest moves. The plan comes first (the rings before and after, the order, the
  data each move streams and where), then one confirmation; ``--check`` stops after the plan. Before each move the
  cluster is checked, and the nodes that receive data must keep ``cassandra_move_min_free_percent`` (20) of their data
  disk free (the nodes that give data away keep it until a cleanup). Each move is followed like a bootstrap. Run it
  again to resume: the plan is worked out again from the ring, and a move left going is waited for. The nodes that
  lost ranges are cleaned up afterwards with ``cassandra_move_cleanup`` (``sequential``, ``rack``, ``dc``, ``all``), or the
  command is printed; they stay listed next to the progress files (``<cassandra_hosts>-move.cleanup``) until a ``move_node``
  run cleans them up, so an interrupted run forgets none. A moved node keeps its old ``initial_token`` in
  ``cassandra.yaml`` (it is not read again); the run says which ``cassandra_initial_token`` of the inventory to
  update.

Every token worked out is checked against the tokens of all the datacenters. SimpleStrategy keyspaces are not in the
shares (their replicas follow the whole ring): the plans warn about them, and ``move_node`` then cleans up every node
of the cluster (and every node of the datacenters that move when it can't read the replication). The system
keyspaces are left out (``system_distributed`` and ``system_traces`` are SimpleStrategy by default, and small): a
cleanup of the whole cluster after big moves in a multi-datacenter cluster removes their stale copies too.

The shares shown assume the largest replication factor of each datacenter (``cassandra_token_rf``, 3, when no
keyspace says, e.g. a new cluster). ``allocate_tokens_for_local_replication_factor`` only matters with vnodes: its
default is empty with one token per node (the line stays commented out), and ``preflight`` does not check the racks
for the token allocator then.


Removing a node
---------------

``decommission_node`` removes the nodes in ``cassandra_leaving_nodes``, one at a time: each one streams its data to the
others, then Cassandra is stopped and disabled on it. It refuses a seed (take it out of ``cassandra_seeds`` with
``change_seeds`` first) and a removal that would leave a datacenter with fewer nodes than a keyspace has replicas
there (it reads the replication with CQL: set ``cassandra_cql_username`` and ``cassandra_cql_password`` when
authentication is on). Remove the hosts from the inventory afterwards. Run again after an interruption, a node
still leaving is waited for again, and one already decommissioned is only stopped and disabled. A failed
decommission (``DECOMMISSION_FAILED`` on 5.0, or ``LEAVING`` with no stream for a long time on 4.0 and 4.1) is left to
the operator: ``nodetool decommission`` on the node resumes it, restarting Cassandra on it cancels it.
Its screen shows the order, and for each node its address, datacenter and rack, load and share, the nodes its data
goes to (the other nodes of its rack when the datacenter has as many racks as every keyspace has replicas there and
the rack keeps a node, else the other nodes of its datacenter; SimpleStrategy keyspaces: any node of the cluster),
the node the ring is checked from, and how it ends.


The inventory as the desired state
----------------------------------

``topology`` makes the ring match the inventory, so adding and removing nodes is an edit of the inventory, reviewed
and committed like any other change:

1. Edit the inventory: a new host goes in its datacenter's (or rack's) group, not in ``cassandra_seeds``; a node to
   remove gets ``cassandra_node_state: absent`` (a host var, or a group var for several).
2. ``ansible-playbook -i inventory community.cassandra.topology --check`` shows the plan and changes nothing.
3. ``ansible-playbook -i inventory community.cassandra.topology`` shows the same plan, asks once, then does it.
4. Commit the inventory. Delete the lines of the hosts removed, or leave them marked absent.

A host of the cluster's group that is not in the ring is added as ``add_node`` adds it (its checks,
``cassandra_add_node_reset``, ``cassandra_initial_token`` or ``cassandra_token_auto=bisect|balanced`` with one token
per node). A host marked absent that is still in the ring is decommissioned as ``decommission_node`` does it (refused:
a seed, a datacenter left with fewer nodes than a keyspace has replicas there unless ``cassandra_decommission_force``).
A host marked absent, out of the ring and stopped needs nothing ("already removed"). A node of the ring no host of the
inventory has is never touched: it is reported (a mistyped address, a host missing from the inventory, a dead node to
remove with ``remove_dead_node``), and a plan with something to do is refused while it is there.

One node at a time, the adds first (the cluster never has fewer nodes than it ends with), then the removals, the
cluster checked before and after each node; the run stops at the first problem. One screen lists every step, with
the data each node streams, and asks once; each step then shows its own screen as it starts, without a question.
Refused before anything changes: a ``--limit`` that leaves out a host of the group (the plan needs them all), a plan
removing more nodes than ``cassandra_topology_max_removals`` (2) or more than half of a datacenter
(``cassandra_topology_allow_large_removal: true`` goes on: a group var marking hosts absent by mistake is the case it
catches), an add while a decommission is still running, two hosts with one address, a host marked absent still in the
ring that does not answer or is down (``remove_dead_node`` then), a node of the group that does not answer, one token
per node with adds and removals in one run (add first, then mark the hosts absent, then ``move_node``), and
``cassandra_new_nodes``, ``cassandra_leaving_nodes`` or ``cassandra_reset_nodes`` on the command line (the plan says
which nodes). The cleanup of the nodes that handed data over to the new ones is left to you: its command is printed. An interrupted run is run again: the plan is worked
out again from the ring, a bootstrap or a decommission still running is waited for. Nothing to add or remove: it says so.

``add_node``, ``decommission_node`` and ``remove_dead_node`` stay for explicit use. Every playbook leaves the hosts
marked absent out: preflight, the health checks and the node counts they expect, rolling operations, ``status`` (which
names them while they are still in the ring) and ``import_cluster``. While one is still in the ring, the health checks
count one node too many and stop, naming it: run ``topology``. The lookup ``community.cassandra.cassandra_nodes`` gives
the hosts of the cluster without them.


Replacing a dead node
---------------------

``replace_node`` starts a blank host in place of a dead node: it takes over the dead node's tokens and streams their
data from the other replicas (``replace_address_first_boot``). Put the new host in the cluster's group and take the
dead one out of the inventory (the new host may reuse its address), then run it with the new host in
``cassandra_new_nodes`` and the dead node's address in ``cassandra_replace_address``. Only a node that is down in the
ring can be replaced. A dead seed: take it out of ``cassandra_seeds`` with ``change_seeds`` first, replace it, then
make the new node a seed.

A replacement host that still holds data is refused: a replacement starts blank. Typically a host replacing itself
(the dead node's own address, after a lost disk or a reinstall) with some of its old data left:
``-e cassandra_replace_node_reset=true`` empties it first (see `Resetting a node`_); there, the other nodes may list
its address only as the dead node being replaced, down.


Resetting a node
----------------

A node that never joined the cluster but has data of its own (started once with the package's stock configuration,
or a bootstrap that failed) is refused by ``add_node``. The reset starts it over: Cassandra stopped and kept from
starting at boot, then everything its data, commitlog, saved_caches, hints and cdc_raw directories hold deleted (the
directories stay, they may be mount points). The directories are those of the inventory and those of the live
``cassandra.yaml`` (Cassandra's defaults under ``/var/lib/cassandra`` for the keys it leaves out).

.. code-block:: console

    $ ansible-playbook -i inventory community.cassandra.reset_node -e cassandra_reset_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.add_node -e cassandra_new_nodes=node7 -e cassandra_add_node_reset=true

Nothing is changed, and the run stops with the reason, when an up node of the cluster (every node of the group the
preflight did not find stopped, but the nodes being added or reset, is asked) lists one of the node's addresses
(from its facts, the inventory and its live ``cassandra.yaml``) or its host ID in ``nodetool status``, up or down: a
member leaves with ``decommission_node`` or ``remove_dead_node`` first. Also when no other node answers as up and
normal (nothing to check against, e.g. a single-node cluster), when a node with data finds a down node in the ring
that no inventory host accounts for (it may be this node under an old address), when the node is not in the
``cassandra_hosts`` group or its live ``cassandra.yaml`` names another cluster than the inventory's (or the stock
``Test Cluster``), when Cassandra runs on the node with other nodes in its ring, does not answer, or runs without a
``cassandra`` unit to stop it, when a directory can't be read, and for a path
that looks wrong: empty or relative, ``/``, a system directory, a top-level directory that is not a mount point, a
home, the package's storage root, another program's directory under ``/var/lib``, a directory holding another mount
point, the config or the logs, one directory inside another (links resolved), or a live ``cassandra.yaml`` that can't
be read. The directories are checked again, links resolved, just before the delete.
The run shows what it would stop and delete, directory by directory, then asks once (``cassandra_operation_confirm:
false`` skips the question); ``--check`` shows it and changes nothing. A second run finds nothing to do.
``add_node`` and ``replace_node`` (``cassandra_add_node_reset``, ``cassandra_replace_node_reset``) work out the reset
before their screen, show what it deletes there (a ``data loss`` warning per node), and their one question covers it.


When a node is dead for good and will not be replaced, take it out of the inventory and run ``remove_dead_node`` with
its address in ``cassandra_dead_node_address``: ``removenode`` streams its ranges from the other replicas.
``cassandra_dead_node_method: removenode_force`` finishes a removal of that node that is stuck. It runs on the node
coordinating the removal, or else on one whose ring shows it ``DL`` (dead, being removed), and never while another
node is leaving or being removed, since ``nodetool removenode force`` finishes every removal or decommission that node
knows of; it checks the node is gone afterwards, and does nothing when the node is already out of the ring.
``assassinate`` removes it from gossip without streaming, only when ``removenode`` can't finish: data it held alone is
lost, repair afterwards. Run again after an interruption, ``removenode`` waits for the removal of that node still in
progress (the token the coordinator removes tells whose), whichever node of the run coordinates it, and does nothing
when the node is already out of the ring. A node being removed (its gossip state, or ``DL``) that no node of the run
is removing (a removal coordinated from outside ``cassandra_hosts``, or whose coordinator restarted) is refused rather
than removed a second time: ``-e cassandra_dead_node_new_removal=true`` starts a ``removenode`` once none runs
anywhere.


Datacenters
-----------

``add_datacenter`` adds a datacenter: put its nodes in the cluster's group, all with the new ``cassandra_dc``, then
run it with them in ``cassandra_new_nodes``, the keyspaces that get replicas there in
``cassandra_datacenter_replication`` (``{"orders": 3, "system_auth": 3}``; NetworkTopologyStrategy only) and an
existing datacenter to stream from in ``cassandra_rebuild_source_dc``. The nodes join one at a time without
streaming, the keyspaces are altered, then each node streams those keyspaces (``nodetool rebuild``). Make one node per rack
of the new datacenter a seed afterwards.

``remove_datacenter`` (``-e cassandra_target_dc=dc3``) alters the keyspaces so they keep no replica there, then
removes its nodes one at a time. Move that region's clients first, and take its seeds out of ``cassandra_seeds``.


Rack maintenance
----------------

``stop_rack`` stops every node of one rack at once (``-e cassandra_target_dc=dc1 -e cassandra_target_rack=rack2``),
each one drained with ``nodetool drain`` (a node that does not answer is stopped all the same) then stopped. With at least as many racks as replicas in the datacenter, one rack down is one
replica down: it refuses a keyspace with more replicas in the datacenter than racks, a SimpleStrategy keyspace with
RF above 1 (it ignores racks), and a node already down elsewhere in the datacenter. With RF 2 it warns that
(LOCAL_)QUORUM fails while the rack is down. ``start_rack`` starts the rack again and checks the cluster; repair the
rack's nodes if they were down longer than ``max_hint_window``.


Restarting
----------

``rolling_restart`` drains each node, restarts it and waits until it and the cluster are healthy again before the
next one. ``rolling_reboot`` does the same with a reboot of the host (OS patching). On a big cluster,
``-e cassandra_rolling_mode=rack`` restarts all the nodes of a rack together, rack by rack, when the replication
allows losing a rack (see `Rack maintenance`_).

To move a cluster to another Java, set ``cassandra_java_version`` in the cluster's ``group_vars`` and run
``update_java``: node by node, it installs that Java, makes it the default ``java``, writes the config and restarts.
It refuses a Java the series does not support (see below), and warns about ``cassandra_jvm<N>_*`` settings meant for the old
Java (with the lines to add for the new one) and about CMS, which Java 17 does not have. The systemd unit drains the node on stop as well (``cassandra_service_drain_on_stop``), so a plain
``systemctl stop cassandra`` or a reboot outside Ansible is clean too. A node's own unit kept as found
(``cassandra_service_unit_manage: false``) may not: these playbooks drain it with ``nodetool`` even with
``cassandra_operation_drain: false``.

Where Java comes as a JDK tarball rather than a package, give it in ``cassandra_java_tarball``, a URL or a file on the
controller, with ``cassandra_java_version`` naming its major version:

.. code-block:: yaml

   cassandra_java_tarball: https://mirror.example.com/java/jdk-17.0.12_linux-x64_bin.tar.gz
   cassandra_java_tarball_checksum: "sha256:..."
   cassandra_java_version: "17"

It is unpacked into ``/opt/cassandra-java/<tarball name>`` (``cassandra_java_tarball_dir``) and made the system
``java``. The Cassandra packages depend on Java: on Debian/Ubuntu, a small local package (``cassandra-java-tarball``)
declares the tarball's Java to apt, and the packages install normally. On the RedHat family they are installed
without their dependencies (``rpm --nodeps``), plus the ones Cassandra needs to run: ``dnf check`` then reports the
Java dependency as missing, and a plain ``dnf upgrade`` that finds a newer Cassandra would install a Java package to
satisfy it (the tarball stays the system ``java``): exclude the cassandra packages from routine upgrades
(``excludepkgs``, versionlock).
``update_java`` moves a cluster to a new tarball the same way as to a new package.

With several clusters, the mirror can offer one tarball per Java major in ``cassandra_java_tarballs``, set once for
all of them (here a ``group_vars/all`` file shared by the inventories), and each cluster only names its Java:

.. code-block:: yaml

   # inventories/_common/group_vars/all/mirror.yml
   cassandra_java_tarballs:
     "11":
       url: https://mirror.example.com/java/OpenJDK11U-jre_x64_linux_hotspot_11.0.28_6.tar.gz
       checksum: "sha256:..."
     "17":
       url: https://mirror.example.com/java/OpenJDK17U-jre_x64_linux_hotspot_17.0.16_8.tar.gz
       checksum: "sha256:..."
     "21":
       url: https://mirror.example.com/java/OpenJDK21U-jre_x64_linux_hotspot_21.0.8_9.tar.gz
       checksum: "sha256:..."

   # inventories/<cluster>/group_vars/<cluster>/main.yml
   cassandra_java_version: "17"

``url`` may also be a file on the controller, and ``checksum`` is optional but recommended. Each tarball is unpacked
in a directory named after its file: give each version a file of its own name. An entry may carry its own
``username``/``password``; without them, a tarball on the same host as ``cassandra_install_url`` gets the mirror's
credentials (see the air-gapped section). An explicit ``cassandra_java_tarball`` still wins. With tarballs on offer, a ``cassandra_java_version`` without one is
refused (with the versions on offer), unless the node has ``cassandra_java_home`` or ``cassandra_install_java:
false`` (Java set up by other means, no tarball taken). A cluster on Java packages moves
to the tarball on its next run (the running nodes switch at their next restart; with ``cassandra_offline`` and a URL the
run stops instead): ``cassandra_java_tarballs: {}`` in its
``group_vars`` keeps it on packages; remove that line and run ``update_java`` to move it node by node. Once unpacked, the Java's ``release`` file must name that major version: a
tarball of another Java is refused, and unpacked again on the next run once its entry is fixed. A Java already there
(a tarball unpacked earlier, ``cassandra_java_home``) is checked the same way before anything changes, so
``update_java`` and ``upgrade`` stop before stopping a node whose Java does not match. ``update_java``
follows the same entries: change ``cassandra_java_version`` and run it.

The Java each series runs on is checked by ``cassandra_install``, ``update_java`` and ``upgrade`` alike: 8 or 11 for
4.0 and 4.1, 11 or 17 for 5.0. 5.0 starts on Java 21 (as on 17) but does not support it (that comes with 6.0):
``cassandra_java_allow_unsupported: true`` installs it anyway, at your own risk.

EL 10 (RHEL, Rocky, AlmaLinux 10) has no Java 11 or 17 package, only 21 and 25, which Cassandra 4.x and 5.0 do not
run on: give a Java tarball there (``cassandra_install`` stops and says so otherwise), or ``cassandra_java_home`` for
a Java installed by other means. Its ``python3`` (3.12) suits 5.0's cqlsh but not 4.x's, and EL 10 has no
``python3.11``: with 4.x, install a Python 3.11 by other means and give it in ``cassandra_cqlsh_python``.


Changing the seeds
------------------

Change ``cassandra_seeds`` in the inventory first, then run ``change_seeds``. It writes the new list on every node
and loads it live, no restart needed. Other configuration differences it finds are shown, not applied.

If you replace a seed, update the clients' contact points as well.


Upgrading
---------

Set the target in the cluster's ``group_vars``: ``cassandra_version`` (the series), ``cassandra_package_version``
(the exact version) and ``cassandra_java_version`` (explicitly: keep the current Java, or change it in the same
pass). Then run ``upgrade`` once per phase, with ``-e cassandra_upgrade_phase=``:

``preflight``
    Checks the cluster, the upgrade path (4.0 to 4.1 or 5.0, 4.1 to 5.0, or a newer patch), Java, settings the
    target series no longer has, disk space, and shows the target configuration. Changes nothing.
``prepare``
    After you confirm that backups and repairs are paused and the schema frozen: a snapshot and a copy of the
    configuration on every node.
``canary``
    Upgrades one node (``cassandra_upgrade_canary``, default the first non-seed of the first datacenter). Watch it.
``rolling``
    Upgrades the others, datacenter by datacenter, rack by rack, one node at a time. Re-run it to resume: upgraded
    nodes are skipped.
``sstables``
    Rewrites the sstables in the new format, node by node.
``cleanup``
    Removes the pre-upgrade snapshots.

From 4.x to 5.0, keep ``cassandra_storage_compatibility_mode: CASSANDRA_4`` until every node runs 5.0: 5.0 nodes
then keep writing what 4.x nodes can read. Then set ``UPGRADING`` and run ``apply_config``, then ``NONE`` and run
``apply_config`` again. ``NONE`` is the point of no return.


Changing the configuration
--------------------------

Before writing anything, :ansplugin:`community.cassandra.cassandra_config#role` renders the files into a temporary
directory on the node and shows a diff against the live files. Passwords are shown as ``****``.

On a node that was already initialized, it then asks for confirmation (type ``yes``), once for all the hosts of the
batch. Without a terminal to answer, the run fails instead of applying. ``cassandra_config_confirm: false`` applies
without asking, ``true`` always asks. It refuses outright to change the settings a node keeps for life (see
`Inventory`_) unless ``cassandra_config_force_identity_change: true``.

Run with ``--check`` to only see the diff. With ``cassandra_change_report_dir`` set, every role also writes what it
changed, or would change under ``--check``, to that directory on the controller.

The role never restarts Cassandra. When it changed the files of a running node, it says so.

Whether a running node still has to be restarted for its config is told by content: the roles record the checksums
of the files Cassandra reads (``cassandra.yaml``, ``cassandra-env.sh``, the jvm options, rackdc, logback) and of its
systemd unit at each start, and before the roles first change the config or the unit of a node started another way
(e.g. imported). Other files of the conf dir
(keystores, backups) do not count. A node with no record, whose files were written after Cassandra started by
something else, is not restarted by ``apply_config``, which names those files: run ``rolling_restart`` if they
changed a setting.

To change the configuration of a running cluster, use ``apply_config`` instead of running the role: it shows the
diff of every node, asks once, then goes node by node, writing the files and restarting the node, with the cluster
checked before and after each one. Nodes whose configuration does not change are not touched, except a node still
running with an older configuration than the one on disk (written by the role, or by a run that stopped before the
restart), or with an older systemd unit (e.g. a new ``cassandra_group`` written by ``cassandra_service``): it is
restarted too. A node whose files only need another owner, group or mode gets them without a restart (Cassandra reads
its config when it starts). A node where Cassandra is not running gets its config too: started once written when its
unit failed (e.g. Cassandra could not read its config, or it crashed), left stopped when it was stopped (a stop that
ended in a failed unit too). Each node's health check needs another node up to read the ring from: a cluster with
no node running is not handled.

Cassandra runs as ``cassandra_user`` and ``cassandra_group`` (default ``cassandra``): the unit's ``User=`` and
``Group=``, the group of the config files and the owner of the directories and JMX users' files ``cassandra_config``
creates. Set them once for both roles. ``cassandra_service`` refuses an account that could not read the config files.


Restricted networks (air-gapped)
--------------------------------

The roles download nothing themselves, apart from the signing keys when ``cassandra_repository_key_url`` is set,
a Java tarball given as a URL (``cassandra_java_tarball``) and the package files of ``cassandra_install_method:
packages``: packages come through the hosts' package manager, from the sources below.
Three setups are covered.

**Internal mirror** (a repository manager, reposync...): the hosts reach a mirror of the Cassandra repositories and of
their OS repositories. Point the roles at it:

.. code-block:: yaml

   cassandra_repository_deb_url: https://mirror.example.com/cassandra-debian
   cassandra_repository_rpm_url: "https://mirror.example.com/cassandra-redhat/{{ cassandra_version }}/"
   # Ubuntu 24.04 with Cassandra 4.x, and 26.04: python3.11 for cqlsh (on 26.04 for Medusa's virtualenv too), "" if the OS mirror has it
   cassandra_cqlsh_python_repo_uri: https://mirror.example.com/deadsnakes

``cassandra_install_url`` sets the same, for the hosts' own OS family. A mirror that needs credentials to read (an
account, or a service account and its token) takes them in ``cassandra_install_username`` and
``cassandra_install_password`` (keep the password in a vault; ``cassandra_repository_username``/``_password``, their
older names, still work). A Java tarball on the same host (``cassandra_java_tarball``, see Java above) uses them too,
unless ``cassandra_java_tarball_username``/``_password`` are set; a tarball elsewhere gets no credentials.
``cassandra_repository`` checks the URL is a repository before adding it (``repodata/repomd.xml``, or the suite's
``InRelease``/``Release``): a plain directory of package files is refused, rather than left as a repository that
breaks every later ``dnf``/``apt`` call.

The RPM URL keeps ``{{ cassandra_version }}``: the upgrade playbook moves it to the next series. A mirror that signs
the repository with its own key needs that key's fingerprint added to ``cassandra_repository_key_fingerprints`` and
the key itself in ``cassandra_repository_key_url``.

**Package files in a plain directory** (e.g. a generic folder of a repository manager holding
``cassandra-5.0.7-1.noarch.rpm`` and ``cassandra-tools-5.0.7-1.noarch.rpm``, no ``repodata/``): the hosts download
the files of ``cassandra_package_version`` themselves, no repository is added (``cassandra_repository`` removes the
``cassandra-<series>`` one it added before), and they are installed (on the RedHat family without their Java
dependency, like with a Java tarball):

.. code-block:: yaml

   cassandra_install_method: packages
   cassandra_install_url: https://mirror.example.com/generic/cassandra/rpms/
   cassandra_package_version: "5.0.7"
   cassandra_install_username: reader              # if the mirror needs it
   cassandra_install_password: "{{ vault_cassandra_install_password }}"

The OS packages Cassandra needs (procps-ng, python3, shadow-utils; python3.11 for cqlsh on RHEL 8) still come from the
hosts' OS repositories or their mirror. Files named otherwise than Apache's take ``cassandra_install_package_file``,
and ``cassandra_install_checksums`` checks them (``sha256:...`` by file name): recommended, the package signatures
are not checked with this method. For an upgrade, put the new version's
files in the same directory: the ``upgrade`` playbook checks they are there before stopping any node.
``import_cluster`` writes ``cassandra_install_method: packages`` for nodes whose Cassandra no configured repository
offers (installed from a file); set ``cassandra_install_url`` for the nodes added later.

**No network at all**: the packages are already on the hosts (system image, or installed by other means), and
nothing must be downloaded. One switch:

.. code-block:: yaml

   cassandra_offline: true

The roles then touch no repository, install nothing, and check instead that every package below is installed: a
host missing one fails, with the list of what to add. A Java tarball (``cassandra_java_tarball``) is then given as a
file on the controller, or already unpacked on the hosts. jemalloc and the Python cqlsh may need are optional (a warning), and so is time sync (a
warning, or ``cassandra_linux_timesync: false`` when the hosts' time sync is managed by other means). The Debian
package holds of ``cassandra_package_version`` are not set offline, and the ``upgrade`` playbook refuses to run (it
installs the new packages: use a mirror).

Packages the roles need:

.. list-table::
   :header-rows: 1

   * - What
     - Debian/Ubuntu
     - RedHat family
   * - Cassandra (from its repository or mirror)
     - ``cassandra``, ``cassandra-tools``
     - ``cassandra``, ``cassandra-tools``
   * - Java: 11 for 4.0/4.1, 17 for 5.0 (``cassandra_java_versions``)
     - ``openjdk-<N>-jre-headless``
     - ``java-<N>-openjdk-headless`` (``java-<N>-amazon-corretto-headless`` on Amazon Linux; none on EL 10: a
       Java tarball)
   * - Python for cqlsh, only when the system ``python3`` is outside cqlsh's range (4.x: 3.6-3.11, 5.0: 3.8-3.13),
       e.g. Ubuntu 24.04 with 4.x, Ubuntu 26.04, RHEL 8 with 5.0
     - ``python3.11`` (deadsnakes on Ubuntu 24.04+)
     - ``python3.11`` (none on EL 10, see Java above)
   * - Time sync (``cassandra_linux``, optional in offline mode)
     - ``systemd-timesyncd``, or ``chrony`` (kept when installed)
     - ``chrony``
   * - Firewall (``cassandra_firewall``, when used)
     - ``ufw``
     - ``firewalld``, ``python3-firewall``
   * - Memory allocator (optional)
     - ``libjemalloc2``
     - ``jemalloc`` (EPEL; base repositories on Amazon Linux)
   * - Package facts (read by the roles; installed on demand when online)
     - ``python3-apt``
     - none
   * - Repository setup (``cassandra_repository``, not used offline)
     - ``apt-transport-https``, ``curl``, ``gnupg``, ``python3-debian``
     - ``gnupg2`` with ``cassandra_repository_key_url``, when ``gpg`` is missing (``gnupg2-minimal`` on Amazon Linux
       is enough)
   * - Java tarball with the repository method (downloads the Cassandra packages, not used offline)
     - none
     - ``dnf-plugins-core`` (``dnf download``)

Time sync keeps the servers configured in chrony or systemd-timesyncd: on air-gapped hosts, configure the site's NTP
servers there (or set ``cassandra_linux_timesync: false`` and manage time sync yourself).

On the controller, the collection needs ``community.general`` and ``ansible.posix``: install them from files
(``ansible-galaxy collection install *.tar.gz``) or from a private Galaxy/Automation Hub. The playbooks talk to the
nodes only (JMX on 127.0.0.1, CQL on the nodes' addresses, SSH from the controller). The ``cassandra_keyspace``,
``cassandra_role`` and ``cassandra_table`` modules need the Python ``cassandra-driver`` on the nodes they run on;
the roles and playbooks don't use them.


JMX access
----------

The playbooks and modules reach each node's JMX on ``127.0.0.1``, port ``cassandra_jmx_port``. With JMX
authentication, set ``cassandra_jmx_username`` and, preferably, ``cassandra_jmx_password_file`` (a file on the
nodes); the systemd unit's drain only uses the password file.

To open JMX to remote tools (a repair scheduler, monitoring), set ``cassandra_local_jmx: false`` and list its users
in ``cassandra_jmx_users``: the role writes ``jmxremote.password`` and ``jmxremote.access``, readable by Cassandra
only. For cqlsh on the nodes, ``cassandra_cqlsh_credentials`` writes a ``cqlshrc`` that points at the node, with the
CQL credentials, for the OS users you list. Playbooks that read the schema over CQL take ``cassandra_cql_username``
and ``cassandra_cql_password``.


Backups
-------

:ansplugin:`community.cassandra.cassandra_medusa#role` installs `Cassandra Medusa
<https://github.com/thelastpickle/cassandra-medusa>`_ with pip, in its own virtualenv, and writes
``/etc/medusa/medusa.ini``. With ``cassandra_medusa_enabled: true`` in the inventory, ``create_cluster`` applies it
to every node, and ``add_node``, ``replace_node`` and ``add_datacenter`` to the new nodes, before they start.

.. code-block:: yaml

    # group_vars/orders.yml
    cassandra_medusa_enabled: true
    cassandra_medusa_version: 0.30.1
    cassandra_medusa_pip_index_url: https://pypi.example.com/simple
    cassandra_medusa_pip_username: mirror_user
    cassandra_medusa_storage_provider: s3_compatible
    cassandra_medusa_host: s3.example.com
    cassandra_medusa_port: 443
    cassandra_medusa_bucket_name: cassandra-backups
    cassandra_medusa_prefix: orders

    # group_vars/orders/secrets.yml (vault)
    cassandra_medusa_pip_password: ...
    cassandra_medusa_s3_access_key_id: ...
    cassandra_medusa_s3_secret_access_key: ...

Medusa reaches Cassandra with the collection's logins (``cassandra_cql_username``, ``cassandra_jmx_username`` and
``cassandra_jmx_password_file``...) unless its own are set. Each ``medusa.ini`` setting has a variable, and
``cassandra_medusa_extra_settings`` takes any other. Medusa 0.30 runs on Python 3.10 to 3.12: on RHEL 8 and 9 the
role installs ``python3.11`` for it (``python3.11`` and ``python3.11-pip``, from the OS repositories or their mirror).
On Ubuntu 26.04 (Python 3.14), its virtualenv uses ``python3.11`` from the deadsnakes PPA, which ``cassandra_install``
adds there when cqlsh needs it; without a virtualenv, give ``cassandra_medusa_python``.

The role schedules no backup: run ``medusa backup`` from cron or a systemd timer, or ``medusa backup-cluster`` from
one node.


Taking over an existing cluster
-------------------------------

``import_cluster`` reads a running cluster into an inventory for the roles, without changing anything on the nodes.
Give it any reachable nodes; it finds the others in the ring. It writes ``<cluster group>.yml`` (the hosts),
``group_vars/`` and ``host_vars/`` to ``import_cluster_dir`` (default ``inventories``, the inventory of every cluster,
see `Project setup`_), and a ``report.txt`` to ``import_cluster_report_dir`` (default ``reports/<cluster group>``
next to ``import_cluster_dir``, out of the inventory) listing, per node, the Cassandra and Java versions, drift
between nodes and the hand edits no variable covers (``cassandra_config`` would revert them).

In ``<cluster group>.yml`` every node is named by its hostname, the short one it reports, with ``ansible_host`` set to its
address in the ring. ``import_cluster_host_names`` picks the name: ``hostname`` (default), ``fqdn`` or ``ip``;
``import_cluster_set_ansible_host: false`` leaves ``ansible_host`` out, when the names resolve to the right address.
A node that could not be reached keeps the name it was given, or its address. In the ``group_vars`` and
``host_vars`` files, the variables are grouped by subject (cluster and topology, versions, directories, network,
JMX, JVM, ``cassandra.yaml`` settings, logging, service, Medusa), one commented block each. The report, and the end
of the run, start with the differences between nodes: each setting that differs, with its values and the nodes, DC or
rack that have them. A node's own address or hostname is written as the fact that gives it, not as a difference.

Each node's ``cassandra_dc`` and ``cassandra_rack`` are the ring's. Of Cassandra's snitches, only
``GossipingPropertyFileSnitch`` reads them from ``cassandra-rackdc.properties``: under another snitch (e.g.
``SimpleSnitch``, whose ring says ``datacenter1`` and ``rack1``) the file's ``dc=`` and ``rack=`` lines may say
something else, and the import keeps them as they are with ``cassandra_rackdc_dc`` and ``cassandra_rackdc_rack``
(remove them before switching to ``GossipingPropertyFileSnitch``: ``cassandra_config`` refuses them then). Comment
lines that differ from the role's templates, such as the stock comments of the release a file came from, are listed
apart: they set nothing.

Medusa's ``fqdn`` is the node's folder in the backups: a new one means full backups. When every node has
``<short hostname>.<domain>``, the same domain everywhere, the import keeps ``cassandra_medusa_fqdn_domain`` (new
nodes get the same form); otherwise each node keeps its value in ``host_vars``. The ``cassandra_medusa`` role refuses to
change the ``fqdn`` of an existing ``medusa.ini`` unless ``cassandra_medusa_fqdn_change: true``.

A node whose running Java is not a package (a JDK unpacked by hand, from a tarball) gets ``cassandra_java_home``: the
roles then keep that Java and install no Java package. A node whose Java is a package gets
``cassandra_java_tarballs: {}``: it keeps its package even where a shared ``cassandra_java_tarballs`` offers tarballs
(see Java above); remove that line and run ``update_java`` to move it to the tarball. A node with
``cassandra_java_home`` needs no tarball either; the offer is for the nodes added later.

The account Cassandra runs as (the user and group of its running process) becomes ``cassandra_user`` and
``cassandra_group`` when it is not ``cassandra``. The owner, group and mode of the config files (``cassandra.yaml``,
``cassandra-env.sh``, the JVM options, rackdc, logback) and of the JMX users' files are read too, and kept:
``cassandra_config_user`` and ``cassandra_config_group`` (what most files have), ``cassandra_config_mode``
(``cassandra.yaml`` and the JVM options files) and ``cassandra_config_public_mode`` (the others), each written when it
is not the roles' default, and ``cassandra_config_file_permissions`` for a file that differs from the others. Nodes
that differ from each other get them per datacenter, rack or node, listed with the other differences. So files owned
``cassandra:dbgrp`` mode ``0640`` stay so, rather than going back to the roles' ``root:cassandra``, ``0640`` for
``cassandra.yaml`` and the JVM options, ``0644`` for the others. The report says when ``cassandra-env.sh`` is not
readable by other users (``nodetool`` run by them cannot read the JMX port in it), and lists the data, commitlog,
hints, saved caches and log directories not owned by that account (``cassandra_config`` leaves the directories there
as they are, and creates the missing ones ``cassandra_user:cassandra_group`` mode ``0750``).

What the roles would replace on a node that was set up another way is left as it is there: the package repositories,
the OS settings (kernel, limits, THP, swap, time sync, disks), cqlsh's Python, the systemd unit (or init script)
Cassandra is started by, the system java and the firewall. A part that has no mark of the roles (their repository file or ``Managed by Ansible`` header), and every node that could not be read, gets the matching switch set to false in its
``host_vars`` (``cassandra_repository_manage``, ``cassandra_linux_manage``, ``cassandra_cqlsh_python_manage``,
``cassandra_service_unit_manage``, with ``cassandra_imported_host: true`` for the last three), never in
``group_vars``: nodes added later get the roles' full setup. The same goes for a ``/usr/bin/java`` that is not the
Java Cassandra runs (``cassandra_java_set_default: false``), the firewall (``cassandra_firewall_manage: false`` on
every imported node) and the packages a node lacks: ``cassandra-tools``, jemalloc, dsbulk and, on Debian and Ubuntu,
the hold (``cassandra_install_tools``, ``cassandra_install_jemalloc``, ``cassandra_dsbulk_install``,
``cassandra_package_hold``); a dsbulk the role's way keeps its ``cassandra_dsbulk_version``. Remove a line to let the role take that part over,
after a ``--check --diff``; a host rebuilt under the same name must lose them and ``cassandra_imported_host``
(``add_node`` and ``replace_node`` refuse such a host with no Cassandra installed). The upgrade playbook stops before
touching a node whose repositories are not managed and lack the target version. On RPM nodes the config stays where the node reads it (``cassandra_rpm_conf_alternative: ""`` unless it
already is the role's conf dir), a heap set in a kept unit stays there, a readwrite JMX user without the create and
unregister rights keeps them that way, and ``cassandra_config`` leaves the content of a file (the JMX users' files
too) alone on an initialized node when its settings are the same as the role's (only comments or layout differ). After an import,
the roles change nothing on the imported nodes.

The OS tuning already on the nodes (set by hand, by another tool or in the image) is read too, and listed in the
report under ``OS TUNING FOUND ON THE NODES``, each line with what ``cassandra_linux`` or ``cassandra_service`` would
set instead: the kernel settings of ``/etc/sysctl.conf`` and the ``sysctl.d`` files, and their live values when they
differ; the ``cassandra`` user's limits (``limits.conf``, ``limits.d``), the unit's ``Limit*`` lines and the running
Cassandra's limits; transparent huge pages (live values, kernel command line, a unit or ``rc.local`` that disables
them); the read-ahead and IO scheduler of the data disks and the udev rules that set them; the ``tuned`` profile's
settings for these; swap; the time sync service and its servers; the firewall (firewalld, ufw, or iptables/nftables
rules for the Cassandra ports). Lines every node read has are shown once. What a variable covers is then carried into the
inventory, so that nodes added later get the same tuning as the existing ones (which stay as they are):

* ``cassandra_linux_sysctl``: the values the admin's files set (files under ``/etc`` that no package ships, apart
  from ``/etc/sysctl.conf``), for the role's keys and every key of a file named for Cassandra; and
  ``cassandra_linux_sysctl_file``, the file that sets most of them (not ``/etc/sysctl.conf`` when no link in
  ``/etc/sysctl.d`` has systemd-sysctl read it at boot). The role then writes its keys in that same file,
  line by line, keeping its other lines, rather than in a second file where one of the two would silently
  override the other.
* ``cassandra_linux_limits``: the ``cassandra`` user's own limits (its user or group lines; soft and hard must be the
  same, the role sets both). The role keeps its own file, ``limits.d/cassandra.conf``, which it writes whole.
* ``cassandra_service_limit_*``: the unit's ``Limit*`` lines.
* ``cassandra_data_readahead_kb``: the read-ahead of the udev rules, when they all set one value and the data disks
  have it. The role keeps its own rule file, which it writes whole, for the data disks only.
* ``cassandra_linux_timesync: false`` when the nodes keep their time with a service other than chrony or
  systemd-timesyncd (ntpd): the role would install chrony, whose unit stops it.

THP, swap, ``tuned``, the time servers and the firewall are only reported: the role disables THP and swap the same
way whatever the nodes use, does not write time servers, and only opens the firewall with
``cassandra_manage_firewall: true``; the imported nodes get ``cassandra_firewall_manage: false`` (their firewall, or
none, is left as it is; nodes added later get the role's). A node the role set up keeps the values of the role's own files (its sysctl
file, ``limits.d/cassandra.conf``, the unit it wrote, not its drop-ins), so that running the roles again changes
nothing on it; when those files do not hold them, the values in effect are carried, and a node without time sync gets
``cassandra_linux_timesync: false`` rather than a chrony it does not have.

A node of the ring the import could not read (down, unreachable, nodetool not found, or its running Java removed by
an update since it started: restart it first) makes it fail: the roles would
give it the group variables unchecked, and start it if it is down. ``-e import_cluster_allow_unread=true`` accepts it;
then keep it out of the runs (``--limit``) until an import reads it.

The Cassandra repository files of a node (``cassandra-<series>`` in ``/etc/yum.repos.d`` or
``/etc/apt/sources.list.d``, and ``/etc/apt/auth.conf.d/cassandra.conf``) are taken over only when
``cassandra_repository`` would write them as they are: then their mirror URL, credentials (``secrets.yml``) and key
path are imported. Files written another way (other keys or names, other signing keys, an apt credentials file without
the role's header, another series' file, or any file when the package came from a file) get
``cassandra_repository_manage: false`` on that node, and the report says why.

Hand edits no variable covers (an extra logback appender, a ``-javaagent`` line in ``cassandra-env.sh``) fail the
self-check. With ``-e import_cluster_keep_hand_edits=true`` the files that have them are left as they are on their
node instead (``cassandra_config_keep_files`` in its ``host_vars``: ``cassandra_config`` neither writes nor compares
them); nodes added later get the role's files.

Before writing anything, the import checks itself: for each node read, the files the roles would write with the
imported variables (``cassandra.yaml``, ``cassandra-env.sh``, the JVM options, rackdc, logback, the JMX users' files,
and the unit and ``medusa.ini`` when the roles manage them) are compared with the node's, setting by setting, as
Cassandra, the JVM, bash and systemd read them; and the owner, group and mode ``cassandra_config`` would give the
config and JMX files with the node's (one line per file that differs). The report starts with ``SELF-CHECK PASSED``, or with ``SELF-CHECK
FAILED`` and the differences: the inventory is then marked ``# NOT VALID`` in its hosts file and the playbook fails
(``-e import_cluster_strict=false`` writes the same files without failing). Fix the variables or the nodes before any
run. The OS tuning, ``/etc/default/cassandra``, ``cassandra-topology.properties`` and the owner and mode of the unit
and ``medusa.ini`` are not compared.

When a node has Cassandra Medusa and ``/etc/medusa/medusa.ini``, its version and settings are imported and
``cassandra_medusa_enabled`` is set, so nodes added later get the same Medusa, in the same virtualenv path
(``cassandra_medusa_venv``), or without a virtualenv when the nodes' Medusa is in a system Python
(``cassandra_medusa_venv: ""`` and that Python). Medusa is looked for at ``import_cluster_medusa_path`` when given (its
virtualenv or its ``medusa`` script, a full path on the nodes), else in the PATH, else in ``/opt/cassandra-medusa``,
``/opt/medusa`` or ``/usr/share/cassandra-medusa``, else in the PATH of a login shell of the ``cassandra`` user, then
of root (``bash`` run as that user, without a terminal, stopped after 10 seconds), and last in any virtualenv under
``/opt``. Its version is read by running its Python as ``cassandra`` (as root only when there is no ``cassandra`` user
and root owns both the script and the Python); a script or a Python owned by another user than root or ``cassandra``
is not run. A virtualenv found through a login profile is reported: the roles leave profiles alone, new nodes get
``/usr/local/bin/medusa``, and ``cassandra_medusa_profile_d: true`` adds the virtualenv to every login shell's PATH. A
Medusa installed by a package is not managed (the report says so).

Passwords found in the configuration go to separate ``secrets.yml`` files, encrypted with ansible-vault as a whole
with ``import_cluster_vault_password_file`` (and ``import_cluster_vault_id``) when given, else with Ansible's vault
password file (``vault_password_file`` in ``ansible.cfg``, see `Project setup`_, or ``ANSIBLE_VAULT_PASSWORD_FILE``).
``--vault-password-file`` and ``--vault-id`` on the command line, and ``vault_identity_list``, are not used: set the
file in ``ansible.cfg`` instead. An executable password file is run, as Ansible does (a
``<name>-client`` script with ``--vault-id <import_cluster_vault_id or default>``); an empty password is refused.
Without a password file, they are written in clear with mode ``0600``, and the report and the end of the run give the
``ansible-vault encrypt`` command to run; a vaulted ``secrets.yml`` already there is then never overwritten in clear
(the import stops). A vaulted ``secrets.yml`` whose content has not changed is left as it is on a re-import.

Every file the import writes starts with ``# Written by community.cassandra.import_cluster for <cluster group>``. A
cluster whose ``<cluster group>.yml`` is there already is refused unless ``import_cluster_force=true`` (the directory
itself may exist, with other clusters); then the import writes the files at its own paths (``<cluster group>.yml``,
``group_vars``/``host_vars`` ``main.yml`` and ``secrets.yml``), removes the files with its header for this cluster it
no longer writes (the ``host_vars`` of a node gone from the ring; a vaulted one only when it decrypts with the
password at hand), and keeps every other file there: your ``group_vars/all/*.yml`` (a mirror, a vault), an
``ansible.cfg``, notes, the other clusters' files; dot-dirs (``.git``) are not looked into, and no directory is
removed. The other clusters' files are neither removed nor written over: a host or group name another cluster has
already, a group your own inventory files there give hosts, children or vars, another cluster whose name makes the
same group (``Prod A`` and ``prod-a``), or a hosts file of an earlier import into a directory of its own
(``hosts.yml`` at the top, or any in a subdirectory: Ansible reads those too) stops the import, before anything is written
(``import_cluster_host_names: fqdn`` or ``ip`` for host names). To bring such a directory in, import the cluster again
with the defaults after moving the old directory out of ``inventories``. A cluster whose group would be ``cassandra``,
``all`` or ``ungrouped`` (cluster name ``Cassandra``) cannot be imported: the playbooks or Ansible take those groups
on their own. A file from an
earlier release, whose first line names no cluster, is this cluster's when this cluster's hosts file alone names its
group or host; otherwise it is kept as it is and listed in the report. A ``<cluster group>.yml`` without that first
line that holds this cluster's group alone (an old ``hosts.yml`` moved there by hand, or one edited by hand) is its
hosts file: replaced, the old one kept as a backup. The report lists the files removed, the files
kept (down to ``group_vars/<group>/``), and the files replaced at its paths that did not have its header, each kept
as a ``<file>.<timestamp>~`` backup. A file of the import you edit by hand is replaced by the next import: put your own
settings in files of your own (``group_vars/all/local.yml``, ``group_vars/<cluster>/local.yml``).

Review a re-import before it writes anything: with ``--check --diff`` it shows the changes of every file it would
write or remove (the ``secrets.yml`` files hidden) and the report, and writes nothing (nor ``report.txt``):

.. code-block:: console

    $ ansible-playbook -i node1, community.cassandra.import_cluster -e import_cluster_force=true --check --diff
    $ ansible-playbook -i node1, community.cassandra.import_cluster -e import_cluster_force=true

Then check what the roles would change:

.. code-block:: console

    $ ansible-playbook -i inventories community.cassandra.preflight -e cassandra_hosts=orders
    $ ansible-playbook -i inventories site.yml -e cassandra_hosts=orders --check

Repeat until the diff only shows what you intend to change. The confirmation prompt is a last safety net, not a
replacement for this step.
