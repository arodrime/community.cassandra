cassandra_firewall
==================

A simple role to install and configure firewalld with the ports commonly used by Apache Cassandra..

Requirements
------------

Any pre-requisites that may not be covered by Ansible itself or the role should
be mentioned here. For instance, if the role uses the EC2 module, it may be a
good idea to mention in this section that the boto package is required.

Role Variables
--------------

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
      roles:
         - { role: cassandra_firewall, x: 42 }

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a
website (HTML is not allowed).
