cassandra_firewall
==================

A simple role to install and configure firewalld with the ports commonly used by Apache Cassandra..

Requirements
------------

Root on the hosts: the role does not ask for it itself, apply it in a play
with `become: true` (the collection's playbooks do).

Role Variables
--------------

* `cassandra_firewall_manage`: `false` leaves the firewall as it is on this
  node (nothing installed, started or opened). Default `true`.
* `cassandra_firewall_enabled`: `false`: no firewall on this node, nothing
  installed, firewalld or ufw stopped and disabled where it runs. Default
  `true`. `import_cluster` sets it for nodes that run none.
* `cassandra_offline`: `true` on air-gapped hosts: firewalld or ufw is
  checked instead of installed. Default `false`.
* `open_ports`: ports open to everyone (SSH, JMX, storage, TLS storage, CQL).
  The node's own Cassandra ports when moved from the stock ones
  (`cassandra_storage_port`, `cassandra_ssl_storage_port`,
  `cassandra_native_transport_port`, a remote JMX's `cassandra_jmx_port`) are
  opened too, unless listed in `cassandra_firewall_port_sources`.

The ports are opened (and those of `cassandra_firewall_port_sources` opened to
their sources, then closed to the others) before the firewall service is
started: a firewall that starts never blocks them, and a port moved to some
sources is never closed to them in between. `apply_config` applies a firewall
change one node at a time, the cluster checked around each.
* `cassandra_firewall_port_sources`: ports open only to some sources instead,
  e.g. JMX for a remote repair scheduler:
  `{"7199/tcp": ["10.0.9.0/24", "10.0.10.5"]}`. A port listed here is closed to
  the others even when it is in `open_ports`.
* `cassandra_prometheus_exporter_sources`: with the Prometheus exporter on
  (`cassandra_prometheus_exporter_enabled`, see cassandra_config), its port
  open to these sources only, e.g. `["10.0.20.0/24"]` for the Prometheus
  servers. Default `[]`: the port stays closed, and a warning says so.

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
         - { role: cassandra_firewall, x: 42 }

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a
website (HTML is not allowed).
