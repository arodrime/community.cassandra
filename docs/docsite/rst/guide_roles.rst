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

Each role handles the host it runs on. Ordering nodes is up to the playbook.

The series is set with ``cassandra_version`` (``40x``, ``41x`` or ``50x``). Set it once for all the roles.

With the default values, the configuration files are the stock ones of the series, apart from the directories the
deb and rpm packages set themselves. Only what you set changes.


Inventory
---------

Use one group per cluster and one group per datacenter (the playbooks below take the cluster group as
``cassandra_hosts``). Settings shared by the cluster go in the cluster's
``group_vars``, the datacenter in the datacenter's, and per node settings in ``host_vars``.

.. code-block:: yaml

    # inventory.yml
    cassandra:
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
(``-e cassandra_hosts=<group>``) and starts with ``preflight``, which checks that the settings that must match do
match on every node, that the racks suit the token allocator, and that the seeds are a sensible layout (it suggests
a seed list when they are not).

.. code-block:: console

    $ ansible-playbook -i inventory community.cassandra.preflight -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.create_cluster -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.add_node -e cassandra_hosts=orders -e cassandra_new_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.rolling_restart -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.apply_config -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.health_check -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.cleanup -e cassandra_hosts=orders
    $ ansible-playbook -i inventory community.cassandra.decommission_node -e cassandra_hosts=orders -e cassandra_leaving_nodes=node7
    $ ansible-playbook -i inventory community.cassandra.change_seeds -e cassandra_hosts=orders
    $ ansible-playbook -i node1 community.cassandra.import_cluster

Operations that touch running nodes check the whole cluster before and after each node: every node up and normal,
gossip and the native transport running, no streams, schema agreement, and the storage and CQL ports answering. A
node is only touched when the cluster is healthy, and the run stops at the first node that does not come back
healthy (``cassandra_service_health_force: true`` goes on anyway, at your own risk). ``health_check`` runs the same
checks on its own, changing nothing, and fails when there is a problem, so it can be scheduled.

Risky operations ask for confirmation first (type ``yes``). ``cassandra_operation_confirm: false`` skips the
question, for runs without a terminal.

Rolling operations record each node done in a progress file on the controller. An interrupted run resumes where it
stopped with ``-e cassandra_rolling_resume=true``.


Creating a cluster
------------------

``create_cluster`` prepares every node in parallel, then starts the seeds one at a time, then the other nodes, each
one joined before the next. Running it again on a running cluster starts nothing.

On a new node, :ansplugin:`community.cassandra.cassandra_install#role` doesn't let the package start Cassandra
with its stock configuration, so the node first starts with its real configuration.


Adding a node
-------------

Add the host to the inventory, in its datacenter's group, without adding it to ``cassandra_seeds``, then:

.. code-block:: console

    $ ansible-playbook -i inventory community.cassandra.add_node -e cassandra_hosts=orders -e cassandra_new_nodes=node7

The other nodes are not touched. A node that has never started and is listed in ``cassandra_seeds`` is refused while
another seed answers: seeds don't bootstrap, so it would join without its data. Add it, then make it a seed.

Once the new nodes have joined, the others still hold the data they handed over. ``cleanup`` removes it, with
``cassandra_cleanup_mode`` ``sequential`` (default, one node at a time), ``rack``, ``dc`` or ``all`` (every node at
once, heavy disk I/O everywhere), and ``cassandra_cleanup_jobs`` threads per node.


Removing a node
---------------

``decommission_node`` removes the nodes in ``cassandra_leaving_nodes``, one at a time: each one streams its data to the
others, then Cassandra is stopped and disabled on it. It refuses a seed (take it out of ``cassandra_seeds`` with
``change_seeds`` first) and a removal that would leave a datacenter with fewer nodes than a keyspace has replicas
there (it reads the replication with CQL: set ``cassandra_cql_username`` and ``cassandra_cql_password`` when
authentication is on). Remove the hosts from the inventory afterwards.


Restarting
----------

``rolling_restart`` drains each node, restarts it and waits until it and the cluster are healthy again before the
next one. The systemd unit drains the node on stop as well (``cassandra_service_drain_on_stop``), so a plain
``systemctl stop cassandra`` or a reboot outside Ansible is clean too.


Changing the seeds
------------------

Change ``cassandra_seeds`` in the inventory first, then run ``change_seeds``. It writes the new list on every node
and loads it live, no restart needed. Other configuration differences it finds are shown, not applied.

If you replace a seed, update the clients' contact points as well.


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

To change the configuration of a running cluster, use ``apply_config`` instead of running the role: it shows the
diff of every node, asks once, then goes node by node, writing the files and restarting the node, with the cluster
checked before and after each one. Nodes whose configuration does not change are not touched, except a node still
running with an older configuration than the one on disk (written by the role, or by a run that stopped before the
restart): it is restarted too.


JMX access
----------

The playbooks and modules reach each node's JMX on ``127.0.0.1``, port ``cassandra_jmx_port``. With JMX
authentication, set ``cassandra_jmx_username`` and, preferably, ``cassandra_jmx_password_file`` (a file on the
nodes); the systemd unit's drain only uses the password file.


Taking over an existing cluster
-------------------------------

``import_cluster`` reads a running cluster into an inventory for the roles, without changing anything on the nodes.
Give it any reachable nodes; it finds the others in the ring. It writes ``hosts.yml``, ``group_vars/``,
``host_vars/`` and a ``report.txt`` listing, per node, the Cassandra and Java versions, drift between nodes and the
hand edits no variable covers (``cassandra_config`` would revert them).

Passwords found in the configuration go to separate ``secrets.yml`` files: encrypted with ansible-vault when
``import_cluster_vault_password_file`` is given, otherwise written with mode ``0600`` and the report gives the
``ansible-vault encrypt`` command to run.

Then check what the roles would change:

.. code-block:: console

    $ ansible-playbook -i orders/hosts.yml community.cassandra.preflight -e cassandra_hosts=orders
    $ ansible-playbook -i orders/hosts.yml site.yml --check

Repeat until the diff only shows what you intend to change. The confirmation prompt is a last safety net, not a
replacement for this step.
