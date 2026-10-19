from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# sync_clocks' plays: what ties a window an interrupted run left open to the next run (preflight lets that seed be
# down, its window first, only it tolerated down), and the reads that stop the run when a node is not read.

import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def plays():
    with open(os.path.join(TOP, "playbooks", "sync_clocks.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def play(name):
    return [p for p in plays() if p.get("name") == name][0]


def test_preflight_lets_a_member_left_down_through():
    assert play("Preflight")["vars"] == {"_cassandra_preflight_members_down_ok": True}
    with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
        text = f.read()
    assert "or (_cassandra_preflight_members_down_ok | default(false) | bool and _addr in _all)" in text


def test_a_window_left_open_comes_first_and_alone_may_be_down():
    window = play("Set the clocks in a restart window, one node at a time")
    hosts = " ".join(window["hosts"].split())
    first, rest = hosts.split(" + ", 1)
    assert "groups['cassandra_sync_clock_window_left_True']" in first
    assert "reject('in', groups['cassandra_sync_clock_window_left_True']" in rest
    assert window["serial"] == 1 and window["any_errors_fatal"] is True
    assert window["vars"]["cassandra_rolling_resume"] == (
        "{{ inventory_hostname in groups['cassandra_sync_clock_window_left_True'] | default([]) }}")
    assert "run sync_clocks again" in window["vars"]["cassandra_service_resume_hint"]
    progress = play("Set up the progress file")
    assert progress["vars"]["_cassandra_progress_resume"] is False  # a fresh file: the clocks read again say the rest


def test_a_node_unreachable_or_not_read_stops_the_run_before_the_screen():
    read = play("Read the clocks")
    assert read["any_errors_fatal"] is True  # unreachable: jvm_started.yml fails there
    names = [t["name"] for t in read["tasks"]]
    refuse = read["tasks"][names.index("Refuse to go on when a clock could not be read")]
    assert names.index("Refuse to go on when a clock could not be read") < names.index("Show the clocks and confirm the run")
    assert refuse["run_once"] is True and refuse["ansible.builtin.assert"]["that"] == "_unread | length == 0"
    assert "selectattr('_cassandra_clock_action', 'equalto', 'unread')" in refuse["vars"]["_unread"]
    # the result words of the live play, as the recap knows them (test_cassandra_clocks)
    live = " ".join(str(play("Set the clocks live")["tasks"]).split())
    assert "'clock_' ~ (_cassandra_clock_did | join('_') or 'slew')" in live


def render(template, **variables):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:  # ansible-core 2.19+ renders trusted templates only
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_a_stale_window_mark_is_removed_not_a_window():
    # the mark of a run interrupted after the start (or a node brought back by another playbook): the node runs,
    # its clock is in sync, no drain and restart for it
    read = play("Read the clocks")
    tasks = dict((t["name"], t) for t in read["tasks"])
    note = tasks["Note what this node needs"]

    def decided(window, running, verdict):
        verdicts = {"nodes": [{"node": "n2", "verdict": verdict, "action": "none" if verdict == "ok" else "window"}]}
        variables = dict(note["vars"], _cassandra_clock={"window": window, "container": ""}, _cassandra_clock_running=running,
                         _cassandra_clock_read_ok=True, _cassandra_clock_verdicts=verdicts, inventory_hostname="n2")
        facts = note["ansible.builtin.set_fact"]
        return render(facts["_cassandra_clock_action"], **variables).strip(), render(facts["_cassandra_clock_stale_mark"], **variables)

    assert decided(True, True, "ok") == ("none", True)
    assert decided(True, False, "ok") == ("window", False)  # left stopped: the window again, which starts it
    assert decided(True, True, "off") == ("window", False)  # the step it was stopped for is still owed
    assert decided(False, True, "off") == ("window", False)  # the verdict's own window
    assert decided(False, True, "ok") == ("none", False)
    # removed after the screen and its question, by the play of the live corrections; said on the screen and in the recap
    assert "Remove the window mark of an interrupted run" not in tasks
    assert "cassandra_sync_clock_stale_mark_{{ _cassandra_clock_stale_mark | bool }}" in str(tasks["Group the nodes with a stale window mark"])
    screen = tasks["Show the clocks and confirm the run"]["vars"]
    groups = {"cassandra_sync_clock_stale_mark_True": ["n2"]}
    variables = dict(screen, groups=groups, ansible_check_mode=False, _cassandra_clock_verdicts={"notes": [], "warnings": [], "ok": True},
                     cassandra_clock_max_offset_ms=50)
    assert render(screen["cassandra_screen_question"], **variables) == "Remove the window mark of n2?"
    assert render(screen["cassandra_screen"]["after"], **variables)[-1] == (
        "A window mark of an interrupted run (the node runs, its clock in sync), removed: n2.")
    assert render(screen["cassandra_screen"]["after"], **dict(variables, ansible_check_mode=True))[-1].startswith(
        "A window mark of an interrupted run (the node runs, its clock in sync), would be removed (--check): n2.")
    assert render(screen["cassandra_screen_question"], **dict(variables, groups=dict(groups, cassandra_sync_clock_live_True=["n1"]))) == (
        "Set the clocks of n1?")
    live = play("Set the clocks live")
    assert "groups['cassandra_sync_clock_stale_mark_True']" in live["hosts"]
    names = [t["name"] for t in live["tasks"]]
    remove = live["tasks"][names.index("Remove the window mark of an interrupted run")]
    assert remove["ansible.builtin.file"] == {"path": "{{ _cassandra_clock_window_mark }}", "state": "absent"}
    note = live["tasks"][names.index("Note the mark is gone")]
    assert remove["when"] == note["when"] == "_cassandra_clock_stale_mark | bool"
    assert live["tasks"][-1]["when"] == ["not ansible_check_mode", "_cassandra_clock_action in ['start', 'step', 'slew']"]  # not its clock
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import outcome
    result = render(note["ansible.builtin.set_fact"]["cassandra_op_result"], ansible_check_mode=False)
    assert outcome("n2", result)[0] == {"node": "n2", "said": "already in sync: a window mark of an interrupted run removed"}
    result = render(note["ansible.builtin.set_fact"]["cassandra_op_result"], ansible_check_mode=True)
    assert outcome("n2", result)[0] == {"node": "n2", "said": "would remove the window mark of an interrupted run (already in sync)"}


def test_a_slew_is_in_progress_not_failed():
    # chrony spreads a correction over minutes: not waited for, said in progress, the recap says it is being corrected
    import warnings
    from ansible.plugins.loader import init_plugin_loader
    with warnings.catch_warnings():  # already done under ansible-test
        warnings.simplefilter("ignore")
        init_plugin_loader()
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "clock_fix.yml"), encoding="utf-8") as f:
        fix = yaml.safe_load(f)
    block = next(t for t in fix if t["name"] == "Set the clock")
    wait = next(t for t in block["block"] if t["name"] == "Wait for its daemon to synchronise")
    assert int(render(wait["vars"]["_cassandra_clock_wait"], _cassandra_clock_action="slew", cassandra_clock_sync_timeout=180)) == 0  # str on 2.16
    assert int(render(wait["vars"]["_cassandra_clock_wait"], _cassandra_clock_action="start", cassandra_clock_sync_timeout=180)) == 180
    clock = {"daemon": "chrony", "service": "chronyd", "active": True, "synchronised": True, "offset": -0.025, "bound": 0.04}
    slewing = render(block["vars"]["_slewing"], _cassandra_clock=clock, cassandra_clock_step_ms=400, cassandra_clock_max_offset_ms=50)
    assert slewing is True
    assert render(block["vars"]["_slewing"], _cassandra_clock=dict(clock, offset=-0.001, bound=0.001), cassandra_clock_step_ms=400,
                  cassandra_clock_max_offset_ms=50) is False  # in sync: nothing to wait for
    assert render(block["vars"]["_slewing"], _cassandra_clock=dict(clock, offset=-0.9), cassandra_clock_step_ms=400,
                  cassandra_clock_max_offset_ms=50) is False  # a step, not a slew
    live = dict((t["name"], t) for t in play("Set the clocks live")["tasks"][-1]["block"])
    say = live["Say a correction its daemon finishes"]
    assert say["when"] == "'slewing' in _cassandra_clock_did"
    text = " ".join(render(say["ansible.builtin.debug"]["msg"], _cassandra_clock=clock, inventory_hostname="n2").split())
    assert text == "n2: correction in progress (slewing 25.0 ms, chronyd finishes it in a few minutes)."
    result = render(live["Record the result"]["ansible.builtin.set_fact"]["cassandra_op_result"], _cassandra_clock_fix_error="",
                    _cassandra_clock_did=["slewing"], _cassandra_clock_start=100, now=lambda: __import__("datetime").datetime.fromtimestamp(103))
    assert " ".join(result.split()) == "clock_slewing done in 3s"
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import outcome
    assert outcome("n2", result)[0]["outcome"] == "clock being corrected by its daemon (minutes)"
