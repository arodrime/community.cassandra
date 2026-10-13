from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The colours of the ops callback: the marks that colour their own line, the
# CHECK and IMPORT verdicts, the plan headers, the WARNING colour option.

import re

import pytest

from ansible import constants as C

from ansible_collections.community.cassandra.plugins.callback.ops import colour, warning_color, _under

from .test_ops import run

ARROW = u"←"


def test_a_mark_colours_its_own_line():
    # a setting that differs: yellow, wherever it is, over the colour of the line it is under
    assert colour(u"                    48   node2   %s differs" % ARROW) == C.COLOR_CHANGED
    assert colour(u"  -Ddemo=1:   present   node1   %s differs (not in the inventory)" % ARROW, above=None) == C.COLOR_CHANGED
    assert colour(u"  concurrent_reads: 8   node1, node3   (inventory)") is None  # the inventory's value: plain
    # a node down: red
    assert colour(u"  node4  dc1/r1  %s down" % ARROW, above=C.COLOR_OK) == C.COLOR_ERROR
    # what a node would get or got: yellow; nothing to do: plain
    assert colour("node1  would apply, then restart") == colour("node3, node6  would apply, no restart") == C.COLOR_CHANGED
    assert colour("node1  applied, restarted") == C.COLOR_CHANGED
    assert colour("node1..node6  nothing to apply") is None and colour("node1  would change nothing") is None
    assert colour("A real run would write each node in turn") is None  # prose, not a node's outcome
    # a reset and what it deletes
    assert colour(u"    node5 (dc1/r1): has data (32.4 MiB) — will be", above=None) == C.COLOR_CHANGED
    assert colour("node5: 6 entries DELETED for good (no snapshot, no backup), in:") == C.COLOR_CHANGED
    assert colour("node5 (dc1/r1): has data, will be reset first") == C.COLOR_CHANGED
    # a verdict at the start of the line wins over a mark
    assert colour(u"REFUSED  x  node2 %s differs" % ARROW) == C.COLOR_ERROR


def test_check_is_green_only_with_nothing_to_do():
    assert colour("CHECK  apply_config  my_cluster  4 would apply (12m32s)") == C.COLOR_CHANGED
    assert colour("CHECK  rolling_restart  my_cluster  would restart 6 nodes   nothing was changed") == C.COLOR_CHANGED
    assert colour("CHECK  apply_config  my_cluster  6 nothing to apply") == C.COLOR_OK
    assert colour("CHECK  add_node  my_cluster  2 not touched   nothing was changed") == C.COLOR_OK
    assert colour("CHECK  apply_config  my_cluster  1 would apply, 1 failed") == C.COLOR_ERROR
    assert colour("CHECKING  x") is None


def test_import_verdict():
    assert colour(u"IMPORT my_cluster (--check, nothing written) — 4 nodes read / 4 — SELF-CHECK PASSED") == C.COLOR_OK
    assert colour(u"IMPORT my_cluster — 4 nodes read / 4 — SELF-CHECK FAILED") == C.COLOR_ERROR


def test_an_indented_line_takes_the_colour_of_the_line_it_is_under():
    shades = [(0, "a"), (2, "b"), (20, "c")]
    assert _under(shades, "  next") == [(0, "a")]  # as indented as "b": under "a"
    assert _under(shades, "      deeper") == [(0, "a"), (2, "b")]
    assert _under(shades, "top") == [] and _under(shades, "") == []


@pytest.mark.parametrize("given, expected", [
    (None, "color208"), ("", "color208"), ("bright yellow", "bright yellow"), ("rgb520", "rgb520"),
    ("color99", "color99"), ("no such colour", "color208")])
def test_warning_color(given, expected):
    assert warning_color(given) == expected


SCREEN = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Plan
      ansible.builtin.debug:
        msg: ["PLAN  op  my_cluster  2 steps", "  1. node1  dc1/rack_a  first", "", "WARNING  careful"]
      vars:
        cassandra_output: true
    - name: Recap
      ansible.builtin.debug:
        msg:
          - "CHECK  apply_config  my_cluster  1 would apply"
          - " "
          - "cassandra.yaml"
          - "  concurrent_reads: 8     node1   (inventory)"
          - "                    48    node2   \\u2190 differs"
          - "  owner/group/mode: 0640  node1   (inventory)"
          - " "
          - "node2  would apply, then restart"
          - "  restart pending"
          - "node1  nothing to apply"
      vars:
        cassandra_output: true
"""


def _codes(output):
    """{line text: its SGR code, None when plain}."""
    found = {}
    for line in output.splitlines():
        m = re.match("^\x1b\\[([0-9;]+)m(.*)\x1b\\[0m$", line)
        found[m.group(2) if m else line] = m.group(1) if m else None
    return found


@pytest.mark.parametrize("env, warning", [
    ({}, "38;5;208"),  # orange
    ({"ANSIBLE_CALLBACK_CASSANDRA_OPS_WARNING_COLOR": "bright yellow"}, "1;33"),
    ({"ANSIBLE_CALLBACK_CASSANDRA_OPS_WARNING_COLOR": "no such colour"}, "38;5;208"),
])
def test_the_screen_colours(tmp_path, env, warning):
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    rc, output = run(tmp_path, SCREEN, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1", **env)
    assert rc == 0, output
    codes = _codes(output)
    assert codes["PLAN  op  my_cluster  2 steps"] == "0;36", repr(output)  # the plan's first line: as progress
    assert codes["  1. node1  dc1/rack_a  first"] is None, repr(output)  # its steps: plain
    assert codes["WARNING  careful"] == warning, repr(output)
    assert codes["CHECK  apply_config  my_cluster  1 would apply"] == "0;33", repr(output)
    assert codes["cassandra.yaml"] is None and codes["  concurrent_reads: 8     node1   (inventory)"] is None
    assert codes[u"                    48    node2   ← differs"] == "0;33", repr(output)
    assert codes["  owner/group/mode: 0640  node1   (inventory)"] is None, repr(output)  # not after the mark
    assert codes["node2  would apply, then restart"] == codes["  restart pending"] == "0;33", repr(output)
    assert codes["node1  nothing to apply"] is None, repr(output)
    assert ("warning_color: no such colour is not a colour Ansible knows" in output) == ("no such" in str(env)), output


def test_no_colour_with_nocolor(tmp_path):
    rc, output = run(tmp_path, SCREEN, ANSIBLE_CALLBACK_CASSANDRA_OPS_WARNING_COLOR="bright yellow")  # ANSIBLE_NOCOLOR
    assert rc == 0 and "\x1b" not in output and "WARNING  careful" in output, repr(output)


def test_a_plan_fact_that_says_would_is_not_an_outcome():
    assert colour("real run:  would also warn about: data loss") is None  # a topology --check fact
    assert colour("node1  would restart") is None  # not apply_config's


FAILED_RECAP = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Recap
      ansible.builtin.debug:
        msg:
          - "FAILED  rolling_restart  my_cluster  2 restarted, 1 failed"
          - "  node1, node2  restarted  (1m49s each)"
          - "  node3  FAILED: not NORMAL after 10m"
          - "  node4  not touched (not reached)"
      vars:
        cassandra_output: true
    - name: Table
      ansible.builtin.debug:
        msg: ["health", "  node5  dc1  \\u2190 down", "    its detail", "  node6  dc1"]
      vars:
        cassandra_output: true
"""


def test_a_failed_recap_shows_which_node_failed(tmp_path):
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    rc, output = run(tmp_path, FAILED_RECAP, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    codes = _codes(output)
    assert codes["FAILED  rolling_restart  my_cluster  2 restarted, 1 failed"] == "0;31", repr(output)
    assert codes["  node1, node2  restarted  (1m49s each)"] is None, repr(output)
    assert codes["  node3  FAILED: not NORMAL after 10m"] == "0;31", repr(output)
    assert codes["  node4  not touched (not reached)"] is None, repr(output)
    # a mark colours its own line, not the lines under it
    assert codes[u"  node5  dc1  ← down"] == "0;31" and codes["    its detail"] is None, repr(output)
    assert codes["  node6  dc1"] is None, repr(output)


def test_a_failed_row_of_apply_config_is_red():
    assert colour("node2  apply_config FAILED after 3s: x") == C.COLOR_ERROR  # not indented (apply_config's recap)
    assert colour("node1  applied, restarted") == C.COLOR_CHANGED


def test_nothing_to_do_is_a_recap_between_rules(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Recap
      ansible.builtin.debug:
        msg: ["NOTHING TO DO  add_node  c  1 not touched", "  node4  not touched (already in the ring, nothing to add)"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines[0].startswith("====") and lines[-1].startswith("===="), output


def test_an_indented_warning_is_a_warning():
    # under a plan, a red row or an informative summary: still the WARNING colour, the configured one
    for above in (None, C.COLOR_ERROR, "cyan"):
        assert colour("  WARNING: x", above=above) == "color208"
    assert colour("    WARNING - lvchange failed", above="cyan", warning="bright yellow") == "bright yellow"
    assert colour("  not a WARNING", above=C.COLOR_ERROR) == C.COLOR_ERROR  # only at the start of the line
