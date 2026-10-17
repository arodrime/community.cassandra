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
  node (nothing installed, started or opened). `import_cluster` sets it for
  the nodes it imports. Default `true`.
* `cassandra_offline`: `true` on air-gapped hosts: firewalld or ufw is
  checked instead of installed. Default `false`.
* `open_ports`: ports open to everyone (SSH, JMX, storage, TLS storage, CQL).
* `cassandra_firewall_port_sources`: ports open only to some sources instead,
  e.g. JMX for a remote repair scheduler:
  `{"7199/tcp": ["10.0.9.0/24", "10.0.10.5"]}`. A port listed here is closed to
  the others even when it is in `open_ports`.

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
