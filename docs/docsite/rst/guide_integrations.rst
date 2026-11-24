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
``cassandra_config`` adds this line at the end of ``cassandra-env.sh``:

.. code-block:: bash

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
    # a mirror (an Artifactory apt repository) instead of packages.axonops.com
    cassandra_axonops_repository_url: https://artifactory.example.com/artifactory/axonops-deb
    cassandra_axonops_repository_suite: trixie
    cassandra_axonops_repository_key_url: https://artifactory.example.com/artifactory/api/gpg/key/public

On a running cluster, ``apply_config`` shows the agent's changes (section "AxonOps agent") and the restart the
``cassandra-env.sh`` line needs, then, one node at a time: sets up the agent, writes the config, restarts the node.

.. code-block:: console

    ansible-playbook community.cassandra.apply_config --check
    ansible-playbook community.cassandra.apply_config

``create_cluster`` sets it up before the first start, ``add_node``, ``replace_node`` and ``add_datacenter`` on the
new nodes. A change of the agent alone (its versions, ``axon-agent.yml``) restarts ``axon-agent``, not Cassandra:
Cassandra's Java agent reads them at its next start. Air-gapped hosts without a mirror: put the ``.deb`` or
``.rpm`` files in ``cassandra_axonops_package_files`` (on the controller).
