from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The seeds: preflight refuses a seed that is not an up member of the ring;
# change_seeds applies cassandra_seeds live (seeds_apply.yml), giving a
# datacenter it would leave without a seed seeds of its own. The expressions
# are read from the playbooks and task files, rendered by Ansible.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

load_collection_filters()  # the collection's lookups and filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def play(playbook, name):
    return next(p for p in load("playbooks", playbook) if p.get("name") == name)


def flat(tasks):
    """The tasks, those of their blocks too, in order."""
    out = []
    for t in tasks:
        out.append(t)
        for key in ("block", "rescue", "always"):
            out.extend(flat(t.get(key) or []))
    return out


def task(tasks, name):
    for t in flat(tasks):
        if t.get("name") == name:
            return t
    raise AssertionError(name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


GROUP = ["n1", "n2", "n3", "n5"]


def node(i, running=True, live="10.0.0.1,10.0.0.2", seed_entry="", dc="dc1", rack="r1"):
    address = "10.0.0.%d" % i
    return {"inventory_hostname": "n%d" % i, "_cassandra_service_seed_entry": seed_entry,
            "_cassandra_service_is_seed": bool(seed_entry), "_cassandra_service_names": ["n%d" % i, address],
            "_cassandra_preflight_running": running,
            "_cassandra_preflight": {"address": address, "ring_address": address, "cassandra_dc": dc,
                                     "cassandra_rack": rack, "live_seeds": live}}


def ring(*numbers, joining=()):
    return {"cluster_status": {"dc1": {"nodes": [{"address": "10.0.0.%d" % i, "status": "U", "rack": "r1",
                                                  "state": "J" if i in joining else "N"} for i in numbers]}}}


# preflight: a seed not up in the ring is refused

PREFLIGHT = play("preflight.yml", "Cassandra preflight checks")
SEED_UP = task(task(PREFLIGHT["tasks"], "Check the running cluster")["block"], "Every seed is up")


def seed_up(item, **extra):
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3),
                "n5": node(5, running=False, live="", seed_entry="10.0.0.5")}
    extra.setdefault("cassandra_preflight_status", ring(1, 2, 3))
    variables = dict(SEED_UP["vars"], item=item, hostvars=hostvars, ansible_play_hosts=GROUP, _from="n1",
                     groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod", **extra)
    return render("{{ %s }}" % SEED_UP["ansible.builtin.assert"]["that"], **variables)


def test_preflight_refuses_a_seed_out_of_the_ring():
    assert seed_up("10.0.0.1") is True
    assert seed_up("10.0.0.5") is False  # e.g. rolling_restart
    assert seed_up("10.0.0.5", _cassandra_preflight_seeds_down_ok=True) is False  # not a member at all
    assert seed_up("10.0.0.9") is False  # no host of the cluster


def test_apply_config_lets_a_seed_of_the_run_not_running_through():
    down = ring(1, 2, 3, 5)
    down["cluster_status"]["dc1"]["nodes"][3]["status"] = "D"
    # a member down that this run finds stopped: apply_config writes it (and starts it if its unit failed)
    assert seed_up("10.0.0.5", cassandra_preflight_status=down, _cassandra_preflight_seeds_down_ok=True) is True
    assert seed_up("10.0.0.5", cassandra_preflight_status=down) is False  # another playbook
    # a member down that runs (Cassandra up there, not seen up by the others): refused all the same
    down["cluster_status"]["dc1"]["nodes"][2]["status"] = "D"
    assert seed_up("10.0.0.3", cassandra_preflight_status=down, _cassandra_preflight_seeds_down_ok=True) is False


# change_seeds applies the list with seeds_apply.yml

def test_change_seeds_uses_the_shared_tasks():
    tasks = play("change_seeds.yml", "Update the seed list")["tasks"]
    assert task(tasks, "Apply the new seed list")["ansible.builtin.include_role"]["tasks_from"] == "seeds_apply.yml"
    apply = load("roles", "cassandra_operation", "tasks", "seeds_apply.yml")
    block = task(apply, "Apply the seed list")["block"]
    reload = task(block, "Reload the seeds on the running node")
    # read when applied: a node this run added was not running at the preflight
    assert "ansible_facts.services['cassandra.service'].state | default('') == 'running'" in reload["when"]
    assert [t["name"] for t in block].index("Check whether Cassandra is running") \
        < [t["name"] for t in block].index("Reload the seeds on the running node")


def test_preflight_names_a_seed_marked_absent():
    groups = {"all": GROUP, "prod": GROUP + ["n2b"]}
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3), "n5": node(5),
                "n2b": {"cassandra_node_state": "absent", "ansible_host": "10.0.0.22"}}
    variables = dict(SEED_UP["vars"], item="10.0.0.22:7000", hostvars=hostvars, ansible_play_hosts=GROUP,
                     cassandra_preflight_status=ring(1, 2, 3), _from="n1", groups=groups, cassandra_hosts="prod")
    assert render("{{ %s }}" % SEED_UP["ansible.builtin.assert"]["that"], **variables) is False
    assert render(SEED_UP["ansible.builtin.assert"]["fail_msg"], **variables).strip().endswith(
        "n2b is marked cassandra_node_state: absent: take it out of cassandra_seeds in the inventory (topology then"
        " drops it from the seed lists before it removes it).")
    # an IPv6 seed: not cut at its last colon, the port in brackets left out
    hostvars["n2b"]["ansible_host"] = "2001:db8::22"
    for item in ("2001:db8::22", "[2001:db8::22]:7000", "[2001:db8::22]"):
        assert render(SEED_UP["vars"]["_absent"], **dict(variables, item=item)) == ["n2b"], item


def _var_names(node):
    """Every name defined under a vars: of a playbook or task file."""
    names = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "vars" and isinstance(value, dict):
                names.update(value)
            names.update(_var_names(value))
    elif isinstance(node, list):
        for item in node:
            names.update(_var_names(item))
    return names


def test_the_shared_seed_tasks_vars_are_their_own():
    # the vars of the include that runs them win over their own task vars: a name used by a caller would
    # replace seeds_apply.yml's own
    elsewhere = set()
    for name in sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml")):
        elsewhere.update(_var_names(load("playbooks", name)))
    for role in ("cassandra_service", "cassandra_operation"):
        for name in os.listdir(os.path.join(TOP, "roles", role, "tasks")):
            if name != "seeds_apply.yml":
                elsewhere.update(_var_names(load("roles", role, "tasks", name)))
    for name in ("seeds_apply.yml",):
        own = _var_names(load("roles", "cassandra_operation", "tasks", name)) - {"cassandra_output"}  # the callback's mark
        assert not own & elsewhere, (name, own & elsewhere)


def test_the_seed_line_read_with_any_quotes():
    facts = task(load("roles", "cassandra_operation", "tasks", "node_facts.yml"), "Record this node's settings")
    template = facts["ansible.builtin.set_fact"]["_cassandra_preflight"]["live_seeds"]
    import base64
    for line in ('- seeds: "10.0.0.1,10.0.0.2"', "- seeds: '10.0.0.1,10.0.0.2'", "- seeds: 10.0.0.1,10.0.0.2"):
        content = base64.b64encode(("seed_provider:\n  - class_name: x\n    parameters:\n      %s\n" % line).encode()).decode()
        assert render(template, cassandra_node_facts_yaml={"content": content}) == "10.0.0.1,10.0.0.2", line


# change_seeds: a list that leaves a datacenter with no seed gets seeds of its own there,
# shown, confirmed, applied, and the line to put in the inventory said at the end

CHANGE = play("change_seeds.yml", "Update the seed list")


def change_seeds(seeds, down=()):
    lives = "10.0.0.1,10.0.0.6"
    hostvars = {"n1": node(1, live=lives, seed_entry="10.0.0.1"), "n2": node(2, live=lives),
                "n6": node(6, live=lives, dc="dc2", seed_entry="10.0.0.6"), "n7": node(7, live=lives, dc="dc2")}
    status = {"cluster_status": {dc: {"nodes": [{"address": "10.0.0.%d" % i, "status": "D" if i in down else "U", "state": "N"}
                                                for i in ids]} for dc, ids in (("dc1", (1, 2)), ("dc2", (6, 7)))}}
    work = task(CHANGE["tasks"], "Work out the seed list")
    variables = dict(work["vars"], ansible_play_hosts_all=["n1", "n2", "n6", "n7"], hostvars=hostvars,
                     cassandra_seeds=seeds, cassandra_preflight_status=status)
    return render(work["ansible.builtin.set_fact"]["_cassandra_change_seeds"], **variables)


def test_change_seeds_gives_a_datacenter_left_with_no_seed_its_own():
    change = change_seeds(["10.0.0.1"])  # n6, dc2's only seed, taken out: kept (it runs as one), with n7
    assert change["problems"] == [] and change["new"] == "10.0.0.1,10.0.0.6,10.0.0.7"
    assert change["auto"][0]["labels"] == ["n6 (dc2/r1)", "n7 (dc2/r1)"]
    assert change["warnings"] == ["cassandra_seeds has no node of dc2: n6 (dc2/r1) kept as its seed; n7 (dc2/r1) made its"
                                  " seed, live only: put them in cassandra_seeds (apply_config would put the inventory's"
                                  " list back)"]
    assert change_seeds(["10.0.0.1", "10.0.0.7"])["auto"] == []  # another node of dc2 instead: as asked
    assert change_seeds(["10.0.0.1"], down=(6,))["auto"][0]["labels"] == ["n7 (dc2/r1)"]  # up nodes only
    refused = change_seeds(["10.0.0.1"], down=(6, 7))
    assert refused["problems"] == ["dc2 would be left with no seed (n6 leaves the seed list) and none of its nodes is up"
                                   " to be one: put a node of dc2 in cassandra_seeds."]
    assert change_seeds([])["problems"][0].startswith("cassandra_seeds is empty")
    names = [t["name"] for t in CHANGE["tasks"]]
    assert names.index("Work out the seed list") < names.index("Refuse a seed list that can't be applied") \
        < names.index("Show the new seed list and confirm") < names.index("Apply the new seed list") \
        < names.index("Say what is left to do")
    refuse = task(CHANGE["tasks"], "Refuse a seed list that can't be applied")
    msg = render(refuse["ansible.builtin.assert"]["fail_msg"], _cassandra_change_seeds=refused, _nl="\n",
                 _cluster="prod cluster")
    assert msg.splitlines()[0] == "REFUSED  change_seeds  prod cluster  nothing was changed"
    # the list applied, and the one the config check compares with: the seeds picked too
    assert CHANGE["vars"]["_new"] == "{{ _cassandra_change_seeds.new }}"
    assert task(CHANGE["tasks"], "Apply the new seed list")["vars"]["cassandra_seeds"] == "{{ _new }}"
    assert task(CHANGE["tasks"], "Look for other config differences")["vars"]["cassandra_seeds"] == "{{ _new }}"
    say = task(CHANGE["tasks"], "Say what is left to do")
    change = change_seeds(["10.0.0.1"])
    text = render(say["ansible.builtin.debug"]["msg"].replace("lookup('community.cassandra.cassandra_hosts')", "'prod'"),
                  _cassandra_change_seeds=change, _nl="\n")
    assert text.splitlines() == [
        "TO DO",
        "  1. put the seeds the nodes now run with in the inventory (group_vars/prod/, or a re-import writes them),"
        " else apply_config puts cassandra_seeds back:",
        '     cassandra_seeds: ["10.0.0.1", "10.0.0.6", "10.0.0.7"]']


# A failed nodetool reloadseeds after the list is written: a WARNING and the run goes on (the list applies at the
# next restart), the TO DO says it; change_seeds, whose job the reload is, still fails. A node whose list did not
# change is not reloaded (a decommission of a non-seed once failed on a seed whose cassandra.yaml no longer loaded).

SEEDS_APPLY = load("roles", "cassandra_operation", "tasks", "seeds_apply.yml")


def test_the_reload_only_where_the_list_changed_or_a_reload_is_owed():
    block = task(SEEDS_APPLY, "Reload the seeds on the running node")
    cond = block["when"][2]
    for written, mark, required, expected in ((True, False, False, True), (False, True, False, True),
                                              (False, False, True, True), (False, False, False, False)):
        variables = {"cassandra_seeds_written": {"changed": written}, "cassandra_seeds_mark": {"stat": {"exists": mark}}}
        if required:
            variables["_cassandra_seeds_reload_required"] = True
        assert render("{{ %s }}" % cond, **variables) is expected, (written, mark, required)
    # the mark before the write (a run stopped right after it still owes the reload), cleared once reloaded
    names = [t["name"] for t in flat(SEEDS_APPLY)]
    assert names.index("Tell whether the seed list changes") < names.index("Mark the reload as owed") \
        < names.index("Write the new seed list") < names.index("Reload the seeds") < names.index("Clear the mark of the owed reload")
    mark = task(SEEDS_APPLY, "Mark the reload as owed")
    probe = task(SEEDS_APPLY, "Tell whether the seed list changes")
    assert probe["check_mode"] is True and "changed_when" not in probe  # its own changed says whether it would write
    would = mark["when"][2]
    assert render("{{ %s }}" % would, cassandra_seeds_change={"changed": True, "msg": "1 replacements made"}) is True
    assert render("{{ %s }}" % would, cassandra_seeds_change={"changed": False, "msg": ""}) is False


def test_a_cassandra_yaml_with_no_seeds_line_to_rewrite_stops_the_node():
    # the seed provider written another way (parameters: [{seeds: "..."}]): nothing matched, the old list would stay
    # with the node "ok". The file itself is read: the preflight's live list is empty for a node added by this run,
    # whose file may hold the new list already (nothing to rewrite)
    names = [t["name"] for t in flat(SEEDS_APPLY)]
    look = task(SEEDS_APPLY, "Look for the seeds line")
    stop = task(SEEDS_APPLY, "Stop when cassandra.yaml has no seeds line to rewrite")
    assert names.index("Write the new seed list") < names.index(look["name"]) < names.index(stop["name"]) < names.index(
        "Reload the seeds on the running node")
    assert look["ansible.builtin.command"] == "grep -qE '^\\s*- seeds:' \"{{ _seeds_file }}\""
    assert look["register"] == "cassandra_seeds_line"
    assert look["changed_when"] is False and look["failed_when"] is False and look["check_mode"] is False  # a read
    assert "ansible.builtin.fail" in stop
    assert stop["vars"]["cassandra_output"] is True  # a verdict: its msg as is (ops callback)
    assert "live_seeds" not in str(stop["when"])
    assert render("{{ %s }}" % stop["when"], cassandra_seeds_line={"rc": 1}) is True  # no line
    assert render("{{ %s }}" % stop["when"], cassandra_seeds_line={"rc": 0}) is False  # a line, rewritten or already right
    msg = render(stop["ansible.builtin.fail"]["msg"], inventory_hostname="n1", _seeds_file="/etc/cassandra/cassandra.yaml",
                 _seeds_new="10.0.0.1,10.0.0.2")
    assert msg == ("n1: /etc/cassandra/cassandra.yaml has no '- seeds:' line the role can rewrite (its seed provider is written"
                   " another way). Write the list there by hand, in the parameters of SimpleSeedProvider"
                   ' (- seeds: "10.0.0.1,10.0.0.2"), then run again.')


def test_a_failed_reload_warns_and_goes_on_except_for_change_seeds():
    block = task(SEEDS_APPLY, "Reload the seeds on the running node")["block"]
    reload = task(block, "Reload the seeds")
    # as registered past failed_when: "failed" is false then, the module's msg says it failed
    failed = {"failed": False, "changed": False, "rc": 0, "error": "Failed to reload the seed node list",
              "msg": "nodetool reloadseeds did not execute successfully (rc 0): Failed to reload the seed node list. ..."}
    ok = {"changed": False, "msg": "nodetool reloadseeds executed successfully"}
    bad = task(SEEDS_APPLY, "Reload the seeds on the running node")["vars"]["_seeds_reload_bad"]
    module_failure = {"failed": False, "rc": 1, "msg": "MODULE FAILURE"}
    for result, expected in ((failed, True), (ok, False), (module_failure, True)):
        assert render(bad, cassandra_seeds_reload=result) is expected, result
    # the module task never fails by itself: the WARNING below says why and what to run, then change_seeds stops
    assert reload["failed_when"] is False
    stop = task(block, "Stop here, change_seeds reloads every running node")
    assert "ansible.builtin.fail" in stop
    names = [t["name"] for t in block]
    assert names.index("Note how the reload went") < names.index("Say the reload failed") < names.index(stop["name"])
    for bad, required, expected in ((True, False, False), (True, True, True), (False, True, False)):
        variables = {"_seeds_reload_bad": bad}
        if required:
            variables["_cassandra_seeds_reload_required"] = True
        assert all(render("{{ %s }}" % w, **variables) for w in stop["when"]) is expected, (bad, required)
    for name in ("Read the reloaded seed list", "Clear the mark of the owed reload", "Read the config the node started with"):
        assert task(block, name)["when"] == "not _seeds_reload_bad | bool", name
    assert task(block, "Say the reload failed")["when"] == [
        "_seeds_reload_bad | bool", "not _cassandra_seeds_reload_required | default(false) | bool"]  # not twice: the stop says it
    # the same text (the cause, the command to run again) as the WARNING
    assert stop["ansible.builtin.fail"]["msg"] == task(block, "Say the reload failed")["ansible.builtin.debug"]["msg"] \
        == "{{ _seeds_reload_warning }}"
    note = task(block, "Note how the reload went")["ansible.builtin.set_fact"]["_cassandra_seeds_reload_failed"]
    assert render(note, cassandra_seeds_reload=failed, _seeds_reload_bad=True) == "Failed to reload the seed node list"
    assert render(note, cassandra_seeds_reload=ok, _seeds_reload_bad=False) == ""  # a later success clears it
    yaml_bad = task(SEEDS_APPLY, "Reload the seeds on the running node")["vars"]["_seeds_yaml_bad"]
    assert render(yaml_bad, cassandra_seeds_reload=dict(failed, stdout="Failed to reload the seed node list.")) is True
    assert render(yaml_bad, cassandra_seeds_reload={"error": "error: Authentication failed", "stdout": ""}) is False
    say = task(SEEDS_APPLY, "Reload the seeds on the running node")["vars"]["_seeds_reload_warning"]
    variables = dict(inventory_hostname="n1", ansible_inventory_sources=["/w/inventories"], cassandra_hosts="prod")
    text = render(say, _seeds_yaml_bad=True, _cassandra_seeds_reload_failed="Failed to reload the seed node list", **variables)
    assert text.startswith("WARNING - n1: the new seed list is written in cassandra.yaml but nodetool reloadseeds failed:"
                           " Failed to reload the seed node list. The running node cannot load its cassandra.yaml again"
                           " (system.log: Error while getting seed node list says why, e.g. a key it does not know): fix"
                           " the file before any restart, or the node will not start; then run: ansible-playbook")
    assert text.endswith("community.cassandra.change_seeds -e cassandra_hosts=prod")
    text = render(say, _seeds_yaml_bad=False, _cassandra_seeds_reload_failed="error: Authentication failed", **variables)
    assert "failed: error: Authentication failed. The list applies at the next restart of the node, or fix the cause and run:" \
        in text
    apply = task(play("change_seeds.yml", "Update the seed list")["tasks"], "Apply the new seed list")
    assert apply["vars"]["_cassandra_seeds_reload_required"] is True
    assert play("change_seeds.yml", "Update the seed list")["any_errors_fatal"] is True  # one node's failed reload stops the run


def test_the_reload_verdict_is_the_modules_success_message():
    # _seeds_reload_bad compares with cassandra_reload's own success msg: a reword there must break this test
    with open(os.path.join(TOP, "plugins", "modules", "cassandra_reload.py"), encoding="utf-8") as f:
        source = f.read()
    assert "result['msg'] = \"nodetool {0} executed successfully\".format(cmd)" in source
    assert 'cmd = "reload{0}".format(module.params[\'reload\'])' in source
    bad = task(SEEDS_APPLY, "Reload the seeds on the running node")["vars"]["_seeds_reload_bad"]
    assert "'nodetool reloadseeds executed successfully'" in bad
