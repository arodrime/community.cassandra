from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The storage_compatibility_mode guards' way outs, followed to the letter: each step the WAY OUT names is done (the
# inventory set, the file edited by hand, apply_config or rolling_restart run, the mode read by hand and given with
# -e), then the refused command is run again. The chain must end with that command passing, Cassandra never moved
# back nor two steps on, cassandra_config's identity check never refusing, and no state seen twice (a loop).

import hashlib
import re

import pytest

from ansible_collections.community.cassandra.tests.unit.roles.config_reader import identity_changes
from ansible_collections.community.cassandra.tests.unit.roles.test_storage_mode_guard import (
    BACK, GIVEN, GUARD as GUARD_FILE, JUMP, KEPT, UNSURE, _load, guard)

ORDER = ("CASSANDRA_4", "UPGRADING", "NONE")
PATH = "/etc/cassandra/conf/cassandra.yaml"
CQL_FAILED = {"failed": True, "rc": 1, "err": "Connection error: ('Unable to connect to any servers')\n"}


def text(mode):
    """A cassandra.yaml with that storage_compatibility_mode, None: without the line."""
    return "num_tokens: 16\n" + ("storage_compatibility_mode: %s\n" % mode if mode else "")


def mode_of(body):
    found = re.findall(r"(?m)^storage_compatibility_mode: (\S+)$", body)
    return found[-1] if found else "CASSANDRA_4"


class Node(object):
    """One 5.0 node: the mode it runs, its file now and at its start, its inventory, what tells the mode."""

    def __init__(self, running, file_mode, inventory, logged=True, cql=False, started=None):
        self.running, self.inventory, self.logged, self.cql = running, inventory, logged, cql
        self.file = text(file_mode)
        self.started = text(running) if started is None else started

    def host(self):
        sha = hashlib.sha1(self.started.encode()).hexdigest()
        line = mode_of(self.file) if "storage_compatibility" in self.file else ""
        lines = ["file=" + PATH, "yaml_read=yes", "yaml=" + line, "running=yes", "jvm_version=5.0.4", "started_sha1=" + sha]
        if self.logged:
            lines.append("logged=" + self.running)
        elif self.file != self.started:
            lines += ["pending=yes", "changed=record"]
        cql = {"changed": False, "transformed_output": "value\n----\n %s\n" % self.running} if self.cql else CQL_FAILED
        return {"cassandra_storage_mode_read": {"stdout_lines": lines}, "cassandra_storage_mode_cql": cql}

    def restart(self):
        new = mode_of(self.file)
        step = ORDER.index(new) - ORDER.index(self.running)
        assert step in (0, 1), "restarted from %s to %s" % (self.running, new)
        self.running, self.started, self.logged = new, self.file, True

    def state(self):
        return (self.running, self.file, self.started, self.inventory, self.logged)


def checks(node, written, given, kept=False):
    """The guard as the command runs it -> the message of the first check that refuses, None when all pass."""
    host = node.host()
    host["_cassandra_config_new"] = {"cassandra.yaml": text(node.inventory)}
    extra = {"_cassandra_storage_mode_written": written}
    if given:
        extra["cassandra_storage_mode_running"] = given
    names = ([GIVEN] if given else []) + ([KEPT] if kept else []) + [BACK, JUMP, UNSURE]
    for name in [n for n in names if n in [t["name"] for t in _load(GUARD_FILE)]]:
        ok, msg = guard(name, {"n1": (host, node.inventory)}, **extra)
        if not ok:
            return msg
    return None


def run(command, node, given, tmp_path):
    """apply_config, rolling_restart or update_java on n1 -> the refusal's message, None when it ran."""
    if command == "rolling_restart":
        msg = checks(node, False, given)
        if msg:
            return msg
        node.restart()
        return None
    msg = checks(node, True, given, kept=command == "update_java")
    if msg:
        return msg
    changes = identity_changes(tmp_path, node.file, text(node.inventory))
    assert not changes, "cassandra_config's identity check refuses: %s" % changes
    node.file = text(node.inventory)
    if node.file != node.started:
        node.restart()
    return None


STEP = re.compile(r"set cassandra_storage_compatibility_mode: (?P<inventory>\w+) in the inventory"
                  r"|set storage_compatibility_mode: (?P<hand>\w+)(?: back)? in \S+ by hand"
                  r"|run (?P<run>apply_config|rolling_restart) --limit n1(?: -e cassandra_storage_mode_running=(?P<e>\S+?))?"
                  r"(?= \(|\.|,|$)"
                  r"|then set (?P<again>NONE) in the inventory again"
                  r"|-e cassandra_storage_mode_running=n1:<its mode>"
                  r"|(?P<drop>Fix or drop these items)"
                  r"|Run (?P<first>apply_config) first")


def follow(command, node, tmp_path, given="", rounds=8):
    """Runs command, follows each WAY OUT to the letter until it passes -> the stops seen, in order."""
    seen, stops = set(), []
    for _round in range(rounds):
        assert (node.state(), given) not in seen, "a loop: %s" % stops
        seen.add((node.state(), given))
        msg = run(command, node, given, tmp_path)
        if msg is None:
            return stops
        stops.append(msg[0])
        way_out = " ".join(msg[msg.index(next(m for m in msg if m.startswith("WAY OUT"))):])
        steps = list(STEP.finditer(way_out))
        assert steps, way_out
        for step in steps:
            if step.group("inventory"):
                node.inventory = step.group("inventory")
            elif step.group("hand"):
                node.file = text(step.group("hand"))
            elif step.group("run"):
                assert run(step.group("run"), node, step.group("e") or "", tmp_path) is None, way_out
            elif step.group("again"):
                node.inventory = step.group("again")
            elif step.group("drop"):
                given = ""
            elif step.group("first"):
                assert run("apply_config", node, "", tmp_path) is None, way_out
            else:  # read by hand: the mode it runs
                given = "n1:" + node.running
    raise AssertionError("no end: %s" % stops)


@pytest.mark.parametrize("command, node, stops", [
    # back: the file edited back by hand, one step (apply_config writes it back)
    ("rolling_restart", dict(running="NONE", file_mode="UPGRADING", inventory="NONE"), 1),
    ("rolling_restart", dict(running="NONE", file_mode="UPGRADING", inventory="UPGRADING"), 1),
    # back: two steps behind, or no line: apply_config cannot write it (identity), by hand
    ("rolling_restart", dict(running="NONE", file_mode="CASSANDRA_4", inventory="NONE"), 1),
    ("rolling_restart", dict(running="UPGRADING", file_mode=None, inventory="UPGRADING"), 1),
    ("rolling_restart", dict(running="NONE", file_mode=None, inventory="CASSANDRA_4"), 1),
    # back, apply_config: the inventory behind the mode it runs
    ("apply_config", dict(running="NONE", file_mode="NONE", inventory="UPGRADING"), 1),
    ("apply_config", dict(running="NONE", file_mode="UPGRADING", inventory="UPGRADING"), 1),
    ("apply_config", dict(running="NONE", file_mode="CASSANDRA_4", inventory="UPGRADING"), 1),
    ("apply_config", dict(running="UPGRADING", file_mode=None, inventory="CASSANDRA_4"), 1),
    # jump: CASSANDRA_4 to NONE, the file at NONE (by hand), at UPGRADING (a restart), at CASSANDRA_4, without the line
    ("rolling_restart", dict(running="CASSANDRA_4", file_mode="NONE", inventory="NONE"), 1),
    ("apply_config", dict(running="CASSANDRA_4", file_mode="UPGRADING", inventory="NONE"), 1),
    ("apply_config", dict(running="CASSANDRA_4", file_mode="CASSANDRA_4", inventory="NONE"), 1),
    ("apply_config", dict(running="CASSANDRA_4", file_mode=None, inventory="NONE"), 1),
    ("apply_config", dict(running="CASSANDRA_4", file_mode="NONE", inventory="NONE"), 1),
])
def test_back_and_jump_way_outs_end(tmp_path, command, node, stops):
    n = Node(**node)
    assert len(follow(command, n, tmp_path)) == stops


@pytest.mark.parametrize("command, node", [
    # the A1 case: it runs CASSANDRA_4, UPGRADING written by a cut apply_config, nothing tells the mode, NONE set
    ("apply_config", dict(running="CASSANDRA_4", file_mode="UPGRADING", inventory="NONE", logged=False)),
    ("rolling_restart", dict(running="CASSANDRA_4", file_mode="NONE", inventory="NONE", logged=False)),
    # it runs a later mode than its file (edited back), nothing tells it
    ("rolling_restart", dict(running="NONE", file_mode="UPGRADING", inventory="NONE", logged=False)),
    ("apply_config", dict(running="NONE", file_mode="UPGRADING", inventory="UPGRADING", logged=False)),
    ("rolling_restart", dict(running="UPGRADING", file_mode=None, inventory="UPGRADING", logged=False)),
    # nothing wrong but nothing tells: read, given, run
    ("apply_config", dict(running="UPGRADING", file_mode="NONE", inventory="NONE", logged=False)),
])
def test_unsure_way_out_ends(tmp_path, command, node):
    n = Node(**node)
    stops = follow(command, n, tmp_path)
    assert stops[0].startswith("A restart could move n1 back, or two steps on.")


def test_a_wrong_given_mode_is_refused_and_its_way_out_ends(tmp_path):
    # it runs UPGRADING (logged), the file unchanged: -e n1:NONE contradicts it
    n = Node("UPGRADING", "UPGRADING", "UPGRADING")
    stops = follow("rolling_restart", n, tmp_path, given="n1:NONE")
    assert stops == ["-e cassandra_storage_mode_running gives a mode this run cannot take (node:MODE, the inventory"
                     " name of a node of this run, where the mode it runs is not read). Nothing was changed."]
    assert n.running == "UPGRADING"


def test_update_java_way_out_ends(tmp_path):
    # it runs UPGRADING, NONE in the inventory: apply_config first (it shows the change), then update_java
    n = Node("UPGRADING", "UPGRADING", "NONE")
    stops = follow("update_java", n, tmp_path)
    assert stops[0].startswith("This run would change storage_compatibility_mode on n1")
    assert n.running == "NONE"
