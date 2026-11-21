from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cluster_health.yml's stop: its way out ends "then resume", "then check again" under --check (no progress file
# was written to resume), as the stop block of the run says it.

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks", "cluster_health.yml")


def said(check, problems, **more):
    with open(PATH, encoding="utf-8") as f:
        stop = next(t for t in yaml.safe_load(f) if t.get("name") == "Stop on an unhealthy cluster")
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in stop["vars"].items() if k != "_absent")
    variables.update(_absent=[], ansible_check_mode=check, cassandra_health_problems=problems,
                     _cassandra_run_command_args={"inventory": ["/inv/hosts.yml"], "default_inventory": "/etc/ansible/hosts",
                                                  "hosts": "prod", "cwd": "/inv"}, **more)
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["ansible.builtin.fail"]["msg"]))


def test_the_way_out_under_check():
    down = ["n4 (10.0.0.4, r1) is DN, seen from n1"]
    assert said(False, down)[1] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then resume"
    assert said(True, down)[1] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then check again"
    assert said(True, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then check again"
    assert said(False, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then resume"


def test_why_a_problem_is_one_said_before_the_way_out():
    # start_rack: why a node the ring puts outside the rack is judged as one of it (_cassandra_health_why)
    why = ["10.0.0.4 (dc1, r3): no inventory host has this address,", "  so it is judged as a node of rack r2"]
    lines = said(False, ["10.0.0.4 (r3) is DN, seen from n1"], _cassandra_health_why=why)
    assert lines[:4] == ["10.0.0.4 (r3) is DN, seen from n1"] + why + [
        "WAY OUT  bring 10.0.0.4 back to UN (its host and/or Cassandra), then resume"]
    assert said(False, ["x"])[1].startswith("WAY OUT")  # (none: as before)


def test_a_node_lost_for_good_gets_full_commands():
    # each way out a command to paste, not a playbook's name
    lines = said(False, ["n4 (10.0.0.4, r1) is DN, seen from n1"])
    # a block of its own, apart from the resume, with the conditions its playbooks state (replace_node.yml:
    # a blank host, the dead one out of the inventory, a dead seed out of cassandra_seeds first; remove_dead_node.yml:
    # the last resort, its data rebuilt from the other replicas, the dead host out of the inventory first)
    assert lines[2:] == [
        "IF n4 IS LOST FOR GOOD",
        "replace it: NEW_NODE (a blank host) in prod, n4 out of the inventory",
        "(a seed: out of cassandra_seeds first, with change_seeds)",
        "  ansible-playbook -i hosts.yml community.cassandra.replace_node -e cassandra_hosts=prod"
        " -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=10.0.0.4",
        "last resort, no replacement: n4 out of the inventory first; the other replicas rebuild its data (removenode)",
        "  ansible-playbook -i hosts.yml community.cassandra.remove_dead_node -e cassandra_hosts=prod"
        " -e cassandra_target_nodes=10.0.0.4"]
    # two nodes down: "their"
    assert said(False, ["n4 (10.0.0.4, r1) is DN, seen from n1", "n5 (10.0.0.5, r1) is DN, seen from n1"])[2] == (
        "WAY OUT  bring n4, n5 back to UN (their host and/or Cassandra), then resume")


def test_a_node_known_by_its_address_only_gets_that_address_not_its_rack():
    # start_rack --limit: a node out of the play is named by its ring address, its rack in the parentheses
    # ("172.18.0.3 (r1) is DN"); the commands took the rack for the address (cassandra_replace_address=r1)
    lines = said(False, ["10.0.0.3 (r1) is DN, seen from n1"])
    assert "  ansible-playbook -i hosts.yml community.cassandra.replace_node -e cassandra_hosts=prod" \
        " -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=10.0.0.3" in lines
    assert "  ansible-playbook -i hosts.yml community.cassandra.remove_dead_node -e cassandra_hosts=prod" \
        " -e cassandra_target_nodes=10.0.0.3" in lines
    assert not [line for line in lines if "=r1" in line]


def test_each_problem_and_the_wait_on_a_line_of_their_own():
    # the stop block says the cause one fact per line under its Why: (80 columns)
    with open(PATH, encoding="utf-8") as f:
        stop = next(t for t in yaml.safe_load(f) if t.get("name") == "Stop on an unhealthy cluster")
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in stop["vars"].items() if k != "_absent")
    variables.update(_absent=[], ansible_check_mode=False, cassandra_service_health_timeout=600,
                     cassandra_health_problems=["n4 (10.0.0.4, r1) is DN, seen from n1", "the schema differs"],
                     _cassandra_health_waited=True, _cassandra_health_wait_start=0,
                     _cassandra_run_command_args={"hosts": "prod"})
    lines = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["ansible.builtin.fail"]["msg"]))
    assert lines[:2] == ["n4 (10.0.0.4, r1) is DN, seen from n1", "the schema differs"]
    assert lines[2].startswith("waited ") and lines[2].endswith(" (cassandra_service_health_timeout: 600s)")
    assert lines[3] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then resume"


def test_a_check_that_only_stops_reads_nothing_and_stops_on_the_problems_given():
    # start_rack: the problems it judged after a report_only check; the reads and the wait skipped
    with open(PATH, encoding="utf-8") as f:
        tasks = yaml.safe_load(f)

    def runs(task, **variables):
        when = task.get("when", [])
        variables = dict(dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in task.get("vars", {}).items()),
                         cassandra_service_health_force=False, cassandra_health_problems=["the ring read before"], **variables)
        return all(Templar(loader=DataLoader(), variables=variables).template(trust_as_template("{{ %s }}" % w))
                   for w in (when if isinstance(when, list) else [when]))
    given = dict(_cassandra_health_stop_on=["n3 (10.0.0.3, r2) is DN, seen from n2"])
    assert [t["name"] for t in tasks if runs(t, **given)] == ["Name the stop for the table of the stop block",
                                                              "Stop on an unhealthy cluster"]
    assert [t["name"] for t in tasks if runs(t, _cassandra_health_stop_on=[])] == []
    # not given: every task as before (the first two read, the stop on what they found)
    assert all(runs(t) for t in tasks[:2])
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    lines = said(False, ["the ring read before"], _cassandra_health_again=start_rack_again(), **given)
    # (start_rack's stop: within 79 columns; "again" was alone on the last line)
    assert lines[:3] == ["n3 (10.0.0.3, r2) is DN, seen from n2",
                         "WAY OUT  bring n3 back to UN (its host and/or Cassandra), then run",
                         "  start_rack again:"]


def test_start_rack_s_stop_gives_its_own_command_again():
    # its way out said "then run start_rack again" with no command, while its rescue gave the run's own one
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    command = "ansible-playbook -i hosts.yml community.cassandra.start_rack --limit 'all:!n3' -e cassandra_target_dc=dc1" \
              " -e cassandra_target_rack=r2"
    given = dict(_cassandra_health_stop_on=["n2 (10.0.0.2, r2) is DN, seen from n1"], _cassandra_health_again=start_rack_again(),
                 _cassandra_health_again_command=command)
    lines = said(False, [], **given)
    assert lines[:5] == ["n2 (10.0.0.2, r2) is DN, seen from n1",
                         "WAY OUT  bring n2 back to UN (its host and/or Cassandra), then run",
                         "  start_rack again:",
                         "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
                         "    --limit 'all:!n3' -e cassandra_target_dc=dc1 -e cassandra_target_rack=r2"]
    assert lines[5] == "IF n2 IS LOST FOR GOOD" and max(len(x) for x in lines) <= 79
    given["_cassandra_health_stop_on"] = ["n2 (r2) is not in the ring at an address it gives: not seen UN"]
    assert said(False, [], **given)[1:4] == [
        "WAY OUT  fix it (the lines above say what), then run start_rack again:",
        "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
        "    --limit 'all:!n3' -e cassandra_target_dc=dc1 -e cassandra_target_rack=r2"]
    # without it, as before
    assert said(False, ["x"])[1:] == ["WAY OUT  fix it (the lines above say what), then resume"]


def start_rack_again():
    """what start_rack's way out ends with (_cassandra_health_again)"""
    with open(os.path.join(os.path.dirname(PATH), "..", "..", "..", "playbooks", "start_rack.yml"), encoding="utf-8") as f:
        return next(t for p in yaml.safe_load(f) for t in p.get("tasks", [])
                    if t.get("name") == "Stop on the rack's nodes not UN")["vars"]["_cassandra_health_again"]


def test_forced_after_a_check_that_only_stops_it_does_not_say_a_node_is_taken_down():
    with open(PATH, encoding="utf-8") as f:
        task = next(t for t in yaml.safe_load(f) if t.get("name") == "Go on despite the problems (cassandra_service_health_force)")

    def msg(**variables):
        variables = dict(dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in task["vars"].items()),
                         cassandra_health_problems=["n3 is DN"], **variables)
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["ansible.builtin.debug"]["msg"]))
    assert msg(_cassandra_health_stop_on=["n3 is DN"]).splitlines() == [
        "WARNING  cassandra_service_health_force is set, going on with an unhealthy cluster:", "  n3 is DN"]
    assert "taking this node down now" in msg()


def test_start_rack_s_stop_fits_79_columns():
    # start_rack prints the stop as it is (no stop block to fit it): its "IF ... IS LOST FOR GOOD" lines were up
    # to 116 columns; each line within 79 now, a command cut between its arguments (" \" at the end), whole
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    problems = ["172.18.0.3 (r1) is DN, seen from h-n1"]
    lines = said(False, problems, _cassandra_health_stop_on=problems, _cassandra_health_again=start_rack_again())
    assert max(len(line) for line in lines) <= 79
    joined = " ".join(line.rstrip("\\").strip() for line in lines)
    assert " ".join(joined.split()).count("ansible-playbook -i hosts.yml community.cassandra.replace_node") == 1
    at = lines.index("IF 172.18.0.3 IS LOST FOR GOOD")
    assert lines[at + 3].endswith(" \\") and "-e cassandra_replace_address=172.18.0.3" in " ".join(lines[at + 3:at + 6])
    # the others' stop: through the stop block of node_operation.yml, which fits it (as it was)
    assert len(said(False, problems)[-1]) > 79


def test_problems_equal_but_for_case_are_said_once_in_the_way_out_and_the_lost_lines():
    # the lost lines moved from Jinja (| unique: ignoring case) to Python (kept the case): "N4 ..." and "n4 ..."
    # gave two IF blocks while the WAY OUT line, still | unique, named one node
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
    load_collection_filters()
    lines = said(False, ["n4 (10.0.0.4, r1) is DN, seen from n1", "N4 (10.0.0.4, r1) is DN, seen from n1",
                         "2001:db8::7 (r1) is DN, seen from n1", "2001:DB8::7 (r1) is DN, seen from n1"])
    assert lines[4] == "WAY OUT  bring n4, 2001:db8::7 back to UN (their host and/or Cassandra), then resume"
    assert [line for line in lines if line.startswith("IF ")] == ["IF n4 IS LOST FOR GOOD", "IF 2001:db8::7 IS LOST FOR GOOD"]
    assert out.unique_ignoring_case(["b", "A", "a", "B", "c"]) == ["b", "A", "c"]


def test_a_node_two_views_see_in_two_states_is_said_once_in_the_lost_lines():
    # n2 sees n4 DN, n1 sees it DL: two problems, one node; the WAY OUT named it once, its IF block came twice
    from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
    load_collection_filters()
    lines = said(False, ["n4 (10.0.0.4, r1) is DN, seen from n2", "N4 (10.0.0.4, r1) is DL, seen from n1"])
    assert lines[2] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then resume"
    assert [line for line in lines if line.startswith("IF ")] == ["IF n4 IS LOST FOR GOOD"]
