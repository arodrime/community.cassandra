from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# health_check says a secret in the inventory's cassandra_service_environment, which apply_config writes to the
# cassandra unit: the unit file readable by every local user, systemctl show prints it. Said as the inventory's
# value (the file on the node is not read), never as what the file holds.

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render)

TASKS = [t for p in load("playbooks", "health_check.yml") for t in p.get("tasks") or []]
SAY = find(TASKS, "Say a secret in the unit's environment")
SECRET = {"JVM_EXTRA_OPTS": "-Djavax.net.ssl.trustStorePassword=zorglub"}


def said(hostvars):
    variables = dict(SAY["vars"], hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    if not render("{{ %s }}" % SAY["when"], variables):
        return []
    return render(SAY["ansible.builtin.debug"]["msg"], variables)


def test_a_secret_in_the_unit_environment_is_a_warning_of_the_health_check():
    names = [t.get("name") for t in TASKS]
    assert names.index("Say a secret in the unit's environment") < names.index("Report")
    assert SAY["run_once"] is True and SAY["vars"]["cassandra_output"] is True
    lines = said({"node1": {"cassandra_service_environment": SECRET},
                  "node2": {"cassandra_service_environment": SECRET},
                  "node3": {"cassandra_service_environment": {"MAX_HEAP_SIZE": "8G"}},
                  "node4": {}})
    assert max(len(x) for x in lines) <= 79
    text = " ".join(" ".join(lines).split())
    assert lines[0].startswith("WARNING  node1, node2: the inventory's cassandra_service_environment holds a")
    assert text.startswith(lines[0].split()[0] + " node1, node2: the inventory's cassandra_service_environment holds"
                           " a secret: JVM_EXTRA_OPTS ")
    assert "apply_config writes it to /etc/systemd/system/cassandra.service, mode 0644: every local user can read" \
        " it" in text
    assert "once written, systemctl show cassandra prints it too" in text
    assert "written to" not in text  # (not observed: the file on the node is not read)
    assert "way out: keep it out of cassandra_service_environment" in text
    assert "zorglub" not in text and "node3" not in text and "node4" not in text
    assert text.count("WARNING") == 1  # (one block for the nodes that share it)


def test_the_health_check_says_the_file_apply_config_writes():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_unit_secret_warning
    lines = said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_start": "package_script"}})
    text = " ".join(" ".join(lines).split())
    expected = cassandra_unit_secret_warning(SECRET, "package_script", True, inventory=True)
    assert "/etc/systemd/system/cassandra.service.d/zz-ansible-environment.conf" in expected
    assert lines[0].startswith("WARNING  node1: the inventory's")
    assert text.split(" ", 2)[2] == " ".join(expected.split())  # (after the mark and the node)
    # a unit the role does not write (kept as it is, another start): nothing written by it, nothing said
    assert said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_unit_manage": False}}) == []
    assert said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_start": "none"}}) == []
    assert said({"node1": {}}) == []


def test_apply_config_and_the_role_pick_the_file_with_the_same_filter():
    # one place picks the file (cassandra_unit_secret_warning): apply_config, the cassandra_service role, health_check
    apply_config = [t for p in load("playbooks", "apply_config.yml") for t in p.get("tasks") or []]
    text = str(apply_config)
    assert "cassandra_unit_secret_warning(" in text
    assert "zz-ansible-environment.conf" not in text
    assert "zz-ansible-environment.conf" not in str(SAY)
