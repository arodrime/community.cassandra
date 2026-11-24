.. _ansible_collections.community.cassandra.docsite.guide_integrations:

Third-party tools on the nodes
==============================

What the usual tools around Cassandra need on the nodes, and how the collection
sets it up.

.. contents::
   :local:
   :depth: 1


What is already there
---------------------

* Any JVM option, a ``-javaagent:`` included: ``cassandra_jvm_extra_options`` (or
  ``cassandra_jvm<N>_extra_options`` for one Java).
* Lines of your own at the end of ``cassandra-env.sh``: ``cassandra_config_template_snippets``.
* Remote JMX (e.g. for Cassandra Reaper): ``cassandra_local_jmx: false``, ``cassandra_jmx_users``,
  ``cassandra_jmx_ssl_*``, ``cassandra_jmx_port``; the firewall with ``cassandra_firewall_port_sources``.
* Prometheus: the JMX exporter as a Java agent, ``cassandra_prometheus_exporter_*`` (see the
  :ansplugin:`cassandra_config role <community.cassandra.cassandra_config#role>`).
* Backups: :ansplugin:`community.cassandra.cassandra_medusa#role` (``cassandra_medusa_enabled``).


AxonOps
-------

:ansplugin:`community.cassandra.cassandra_axonops#role` installs the AxonOps agent (``axon-agent``) and the
AxonOps Java agent package built for the node's Cassandra series and Java, writes
``/etc/axonops/axon-agent.yml``, and keeps ``axon-agent`` running. With ``cassandra_axonops_enabled: true``,
``cassandra_config`` adds this line at the end of ``cassandra-env.sh``, under the ``# Custom settings`` marker that
heads the collection's additions there (your snippet of ``cassandra_config_template_snippets`` comes after it):

.. code-block:: bash

    JVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"
    # Custom settings
    # AxonOps agent (cassandra_axonops_enabled)
    . /usr/share/axonops/axonops-jvm.options

That file comes with the Java agent package (1.1.0 and later): it adds the ``-javaagent`` option and, on Java 9
and later, the module flags the agent needs. The AxonOps server itself is not set up by this collection.

.. code-block:: yaml

    # group_vars/orders.yml
    cassandra_axonops_enabled: true
    cassandra_axonops_server_host: axonops.example.com
    cassandra_axonops_org: example
    cassandra_axonops_agent_version: "2.0.36"
    cassandra_axonops_java_agent_version: "1.1.1"
    # a mirror (an apt repository of a repository manager) instead of packages.axonops.com
    cassandra_axonops_repository_url: https://mirror.example.com/axonops-deb
    cassandra_axonops_repository_suite: trixie
    cassandra_axonops_repository_key_url: https://mirror.example.com/keys/axonops-deb.asc

On a running cluster, ``apply_config`` shows the agent's changes (section "AxonOps agent") and the restart the
``cassandra-env.sh`` line needs, then, one node at a time: sets up the agent, writes the config, restarts the node.

.. code-block:: console

    ansible-playbook community.cassandra.apply_config --check
    ansible-playbook community.cassandra.apply_config

``create_cluster`` sets it up before the first start, ``add_node``, ``replace_node`` and ``add_datacenter`` on the
new nodes. A change of the agent alone (its versions, ``axon-agent.yml``) restarts ``axon-agent``, not Cassandra:
Cassandra's Java agent reads them at its next start. ``apply_config`` also compares the repository the role adds
(its source, signing key and credentials). With ``cassandra_axonops_repository_manage`` turned off later, the
repository files the role wrote are left on the nodes (``/etc/apt/sources.list.d/axonops.sources``,
``/etc/apt/keyrings/axonops.*``, ``/etc/apt/auth.conf.d/axonops.conf``, ``/etc/yum.repos.d/axonops.repo``): remove
them by hand if another source replaces them. Air-gapped hosts without a mirror: put the ``.deb`` or
``.rpm`` files in ``cassandra_axonops_package_files`` (on the controller; a relative path is read from the
inventory's directory; named as AxonOps' repositories serve them or ``name_version_arch.deb``,
``name-version-release.arch.rpm``). A package the host's repositories lack, or a file missing on the controller, is
refused before any change, ``--check`` too.

On a node with ``cassandra_axonops_enabled: true``, ``rolling_restart``, ``rolling_reboot`` and ``start_rack`` stop
before restarting or starting it when ``/usr/share/axonops/axonops-jvm.options`` is missing (Cassandra would not
start): ``apply_config`` installs it again.

``upgrade``, ``update_java`` and an ``apply_config`` that moves nodes to another ``cassandra_java_version`` refuse,
for now, the nodes with ``cassandra_axonops_enabled: true`` (not supported yet in this version), before any change
and under ``--check`` too: upgrade those nodes, or move them to another Java,
by hand, following AxonOps' agent docs for the Java agent package of the new series or Java. A Java agent package older
than 1.1.0 (without ``axonops-jvm.options``) is upgraded to the newest, unless ``cassandra_axonops_java_agent_version``
pins another one.

``import_cluster`` does not detect AxonOps yet: on a node already loading the agent from ``cassandra-env.sh``, the
line stays in the snippet it imports. Remove it from the snippet, then set the ``cassandra_axonops_*`` variables.
