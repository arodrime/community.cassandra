from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The warnings: one style everywhere ("WARNING - ..."), each warning once,
# two warnings a blank line apart (their continuation lines indented under
# them), the plan's warnings not said again by the recap.

import os
import re

from ansible_collections.community.cassandra.plugins.callback.ops import apart
from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
    cassandra_apply_config_outcomes, cassandra_apply_config_recap, cassandra_apply_config_warnings)
from ansible_collections.community.cassandra.plugins.filter.cassandra_clocks import cassandra_clock_refusal
from ansible_collections.community.cassandra.plugins.filter.cassandra_disks import cassandra_disk_summary
from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def test_one_mark():
    assert out.warning("swap left on") == ["WARNING - swap left on"]
    assert out.warning("WARNING - swap left on") == ["WARNING - swap left on"]  # marked already: kept
    # a longer one: its next lines indented under it, those indented already kept as they are
    assert out.warning("a:\nb\n    c") == ["WARNING - a:", "  b", "    c"]
    assert out.warning(["a:", "b"]) == ["WARNING - a:", "  b"]
    assert out.warning("") == []
    assert out.is_warning("  WARNING - x") and not out.is_warning("NOTE  x") and not out.is_warning("not a WARNING")
    assert out.warning_text("WARNING - data disks: x") == "data disks: x"
    assert out.warning_text("plain") == "plain"


def test_warnings_once_a_blank_line_apart():
    lines = out.warning_lines(["a", "b:\n  b1\n  b2", "a", "c"])
    assert lines == ["WARNING - a", out.PARAGRAPH, "WARNING - b:", "  b1", "  b2", out.PARAGRAPH, "WARNING - c"]
    seen = set()
    assert out.warning_lines(["a"], seen) == ["WARNING - a"]
    assert out.warning_lines(["a", "b"], seen) == ["WARNING - b"]  # said already: left out
    assert out.warnings_above(0) == []
    assert out.warnings_above(1) == ["1 warning in the plan above."]
    assert out.warnings_above(3) == ["3 warnings in the plan above."]
    assert out.warnings_above(2, names=["node3", "node2"]) == ["2 warnings in the plan above (node2, node3)."]
    assert out.warnings_above(1, also="the refusal") == ["1 warning in the plan above (the refusal)."]


def test_the_plan_screens_space_their_warnings():
    lines = out.plan("cleanup", "c1", warnings=["w1", "w2"], question="Go?")
    assert lines[-6:] == ["", "WARNING - w1", out.PARAGRAPH, "WARNING - w2", "", "Go?"]
    # a longer one: its next lines under its text, their own indent kept
    lines = out.plan("topology", warnings=["data loss: node4:\n  /d/data: 1 entries"])
    assert lines[-2:] == ["WARNING - data loss: node4:", " " * len(out.WARNING) + "  /d/data: 1 entries"]


# apply_config: its warnings in its plan, once, the nodes they are for together

NOTES = ["clock 1.500 s ahead of its time source (chrony)",
         "WARNING - clock ahead of its time source: future timestamps",
         "WARNING - swap left on: too little memory"]


def nodes():
    return [{"name": "node1", "todo": True, "then": "restart", "why": "time sync", "notes": NOTES},
            {"name": "node2", "todo": True, "then": "restart", "why": "time sync", "notes": NOTES},
            {"name": "node3", "todo": False, "result": "nothing to apply",
             "notes": ["WARNING - data disks: tuned: not switched"]}]


def test_apply_config_warnings_in_the_plan_only():
    warned = cassandra_apply_config_warnings(nodes())
    assert warned == [
        {"label": "node1, node2", "text": NOTES[1][len(out.WARNING):]},
        {"label": "node1, node2", "text": NOTES[2][len(out.WARNING):]},
        {"label": "node3", "text": "data disks: tuned: not switched"}]
    # under the nodes, their other notes only
    plan = cassandra_apply_config_outcomes(nodes(), plan=True)
    assert "  " + NOTES[0] in plan and not any(out.is_warning(x) for x in plan), plan
    screen = cassandra_screen({"operation": "apply_config", "sections": [[{"pre": plan}]],
                               "warnings": warned + [{"label": "refused", "text": ""}]}, check=True).split("\n")
    shown = [x for x in screen if x.startswith("WARNING")]
    assert shown == ["WARNING - node1, node2: clock ahead of its time source: future timestamps",
                     "WARNING - node1, node2: swap left on: too little memory",
                     "WARNING - node3: data disks: tuned: not switched"]
    # a blank line between two
    for line in shown[1:]:
        assert screen[screen.index(line) - 1].strip() == ""


def test_apply_config_recap_points_at_the_plan():
    every = ["node1", "node2", "node3"]
    for check in (True, False):
        recap = cassandra_apply_config_recap(nodes(), check=check, cluster="c1", refused=["why", "way out"], planned=every)
        assert not any(out.is_warning(x) for x in recap), recap
        assert recap[-2:] == ["", "4 warnings in the plan above (node1..node3; the refusal)." if check
                              else "3 warnings in the plan above (node1..node3)."]
    # a node the plan did not show (out of the play before it): its warnings said here, the others pointed at
    recap = cassandra_apply_config_recap(nodes(), check=True, cluster="c1", planned=["node1", "node2"])
    assert "2 warnings in the plan above (node1, node2)." in recap
    assert [x for x in recap if out.is_warning(x)] == ["WARNING - node3: data disks: tuned: not switched"]
    # no plan (no node to do): the recap says them, once each, a blank line apart, the refusal too
    idle = [dict(n, todo=False, result="nothing to apply") for n in nodes()]
    recap = cassandra_apply_config_recap(idle, check=True, cluster="c1", refused=["why", "way out"])
    assert [x for x in recap if out.is_warning(x)] == [
        "WARNING - node1, node2: " + NOTES[1][len(out.WARNING):], "WARNING - node1, node2: " + NOTES[2][len(out.WARNING):],
        "WARNING - node3: data disks: tuned: not switched",
        "WARNING - a real run is refused, before changing anything: why"]
    assert recap[-1] == "  way out" and recap.count(out.PARAGRAPH) == 3
    assert "in the plan above" not in "\n".join(recap)
    # nothing to warn about: no pointer
    done = [{"name": "n1", "todo": True, "done": True, "then": "restart"}]
    assert "in the plan above" not in "\n".join(cassandra_apply_config_recap(done, cluster="c1", planned=["n1"]))


def test_disk_summary_spaces_its_warnings_and_skips_the_ones_said():
    summary = [{"name": "n1", "kb": "8", "warnings": ["w1", "w2"]}, {"name": "n2", "kb": "8", "warnings": ["w1"]}]
    lines = cassandra_disk_summary(summary)
    assert lines[-3:] == ["WARNING - w1 (n1, n2)", out.PARAGRAPH, "WARNING - w2 (n1)"]
    lines = cassandra_disk_summary(summary, said={"n1": ["w1", "w2"]})
    assert [x for x in lines if out.is_warning(x)] == ["WARNING - w1 (n2)"]


def test_clock_refusal_spaces_its_warnings():
    verdicts = {"max_ms": 50, "nodes": [{"node": "n1", "verdict": "off", "text": "OFF", "why": "", "action": "manual",
                                         "running": True}],
                "warnings": ["a", "b"]}
    lines = cassandra_clock_refusal(verdicts, cluster="c1")
    at = lines.index("WARNING - a")
    assert lines[at:at + 3] == ["WARNING - a", out.PARAGRAPH, "WARNING - b"]


# the ops callback: two warnings in a row of any message, a blank line apart

def test_the_callback_puts_two_warnings_apart():
    assert apart(["WARNING - a", "WARNING - b"]) == ["WARNING - a", out.PARAGRAPH, "WARNING - b"]
    # a warning's indented lines are its own; the next warning after them
    assert apart(["WARNING - a:", "  a1", "WARNING - b"]) == ["WARNING - a:", "  a1", out.PARAGRAPH, "WARNING - b"]
    assert apart(["node1  would apply", "  WARNING - x", "  WARNING - y"]) == [
        "node1  would apply", "  WARNING - x", out.PARAGRAPH, "  WARNING - y"]
    # the rows of a table (preflight's seed layout), a JVM's own "WARNING:" lines: kept together
    rows = ["Seeds  dc1  n1 (r1)  ok", "WARNING - Seeds  dc2  b1 (r1)  1 seed", "WARNING - Seeds  dc3  none  no seed"]
    assert apart(rows) == rows
    assert apart(["WARNING: An illegal reflective access", "WARNING: Please consider"]) == [
        "WARNING: An illegal reflective access", "WARNING: Please consider"]
    # apart already, or after another line: as they are
    for lines in (["WARNING - a", "", "WARNING - b"], ["WARNING - a", "plain", "WARNING - b"], ["a", "WARNING - b"]):
        assert apart(lines) == lines


# one style: no "WARNING:", "WARNING  x" (two spaces), "WARNING x" left as output (prose about the WARNING lines
# stays): marked by a colon, by two spaces or a tab, or one space after a quote, a "msg:" or as a YAML text line

_OLD = re.compile(r"(?<![\[\w])WARNING(:|[ \t]{2,}\S|\t|(?<=['\"]WARNING) (?!- ))")
_OLD_LINE = re.compile(r"^\s*(-\s+|msg:\s+)?WARNING (?!- )\S")
_ALLOWED = ("OpenJDK .*warning|WARNING: )",)  # cassandra_stream.py: a JVM's banner it reads, not ours


def _files():
    for base in ("playbooks", "roles", "plugins", "docs", "tests", "meta"):
        for root, dirs, files in os.walk(os.path.join(TOP, base)):
            if os.path.relpath(root, TOP).startswith(os.path.join("tests", "output")):
                continue  # ansible-test's own (its virtualenvs)
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), TOP)
                # Cassandra's own words (its cassandra.yaml templates)
                if name.endswith((".py", ".yml", ".yaml", ".rst", ".md", ".sh")) and not rel.startswith(
                        os.path.join("roles", "cassandra_config", "templates")):
                    yield rel, os.path.join(root, name)


def test_one_warning_style_everywhere():
    me = os.path.relpath(__file__, TOP)
    found = []
    for rel, path in _files():
        if rel == me:
            continue
        with open(path, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                if any(a in line for a in _ALLOWED):
                    continue
                text = line.replace("[WARNING]", "").replace("[DEPRECATION WARNING]", "")  # Ansible's own
                if _OLD.search(text) or (rel.endswith((".yml", ".yaml")) and _OLD_LINE.search(text)):
                    found.append("%s:%d: %s" % (rel, number, line.strip()))
    assert not found, "\n".join(found)


def test_the_style_check_finds_the_old_marks():
    for old in ('msg: "WARNING: x"', "msg: WARNING x", "'WARNING x'", '"WARNING  x"', "WARNING\tx", "DEPRECATION WARNING: x",
                "      WARNING {{ x }}"):
        assert _OLD.search(old) or _OLD_LINE.search(old), old
    for kept in ('"WARNING - x"', "the WARNING lines", "[WARNING]: x".replace("[WARNING]", ""), "_WARNING = re.compile"):
        assert not (_OLD.search(kept) or _OLD_LINE.search(kept)), kept
