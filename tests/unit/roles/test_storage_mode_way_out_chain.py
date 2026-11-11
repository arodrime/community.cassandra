from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The storage_compatibility_mode guards' way outs, followed to the letter: each step the WAY OUT names is done (the
# inventory set, the file edited by hand, apply_config or rolling_restart run, Cassandra started, the mode read by hand
# and given with -e, the operator's choice made either way), then the refused command is run again. The checks run in
# the real order (apply_config: cassandra_config's identity check, then the guard, then the check of the nodes it
# starts). The chain must end with that command passing, Cassandra never moved back nor two steps on, never into NONE
# without the plan's point-of-no-return warning, and no state seen twice (a loop).

import hashlib
import itertools
import re

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
    cassandra_storage_mode_restarts_into_none, cassandra_storage_mode_running_none)
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import identity_changes
from ansible_collections.community.cassandra.tests.unit.roles.test_storage_mode_guard import (
    BACK, GIVEN, INTO_NONE, JUMP, KEPT, STOPPED, UNSURE, guard)

ORDER = ("CASSANDRA_4", "UPGRADING", "NONE")
PATH = "/etc/cassandra/conf/cassandra.yaml"
CQL_FAILED = {"failed": True, "rc": 1, "err": "Connection error: ('Unable to connect to any servers')\n"}
IDENTITY = "Fix the inventory to match the node"


def text(mode):
    """A cassandra.yaml with that storage_compatibility_mode, None: without the line."""
    return "num_tokens: 16\n" + ("storage_compatibility_mode: %s\n" % mode if mode else "")


def mode_of(body):
    found = re.findall(r"(?m)^storage_compatibility_mode: (\S+)$", body)
    return found[-1] if found else "CASSANDRA_4"


def sha(body):
    return hashlib.sha1(body.encode()).hexdigest()


class Node(object):
    """One node: the mode it runs (stopped: last ran), its file now and at its (last) start, its inventory, what
    tells the mode: logged (at its start), cql (a CQL answer), start_read (its start and file times read), jvm4x (it
    runs Cassandra 4.x: its restart starts 5.0), stopped (starts: apply_config starts it), record (the roles' record
    of its last start)."""

    def __init__(self, running, file_mode, inventory, logged=True, cql=False, started=None, start_read=True,
                 jvm4x=False, stopped=False, starts=False, record=True):
        self.running, self.inventory, self.logged, self.cql = running, inventory, logged, cql
        self.start_read, self.jvm4x, self.stopped, self.starts, self.record = start_read, jvm4x, stopped, starts, record
        self.file = text(file_mode)
        self.started = text(None if jvm4x else running) if started is None else started

    def host(self):
        line = mode_of(self.file) if "storage_compatibility" in self.file else ""
        lines = ["file=" + PATH, "yaml_read=yes", "yaml=" + line]
        if self.stopped:
            lines += ["running=no", "log=/var/log/cassandra/system.log", "record=/var/lib/cassandra/.record.json"]
            lines += ["last_logged=" + self.running] if self.logged else ["log_state=none"]
            if self.record:
                lines.append("last_same=" + ("yes" if self.file == self.started else "no"))
            return {"cassandra_storage_mode_read": {"stdout_lines": lines}, "cassandra_storage_mode_cql": {"skipped": True}}
        lines.append("running=yes")
        lines.append("jvm_version=" + ("4.1.5" if self.jvm4x else "5.0.4"))
        if not self.start_read:
            lines.append("start_read=no")
        else:
            lines.append("started_sha1=" + sha(self.started))
            if self.logged and not self.jvm4x:
                lines.append("logged=" + self.running)
            elif self.file != self.started:
                lines += ["pending=yes", "changed=record"]
        cql = ({"changed": False, "transformed_output": "value\n----\n %s\n" % self.running}
               if self.cql and not self.jvm4x else CQL_FAILED)
        return {"cassandra_storage_mode_read": {"stdout_lines": lines}, "cassandra_storage_mode_cql": cql}

    def restart(self):
        """Started with its file (a 4.x one restarts 5.0) -> whether it crossed into NONE."""
        new = mode_of(self.file)
        step = ORDER.index(new) - ORDER.index(self.running)
        assert step in (0, 1), "restarted from %s to %s" % (self.running, new)
        crossed = self.running != "NONE" and new == "NONE"
        self.running, self.started, self.logged, self.stopped, self.jvm4x = new, self.file, True, False, False
        self.start_read = True
        return crossed

    def state(self):
        return (self.running, self.file, self.started, self.inventory, self.logged, self.stopped, self.jvm4x,
                self.start_read)


class Cluster(object):
    def __init__(self, nodes):
        self.nodes, self.given, self.crossed, self.warned, self.choices = nodes, "", [], [], 0

    def hosts(self, names):
        out = {}
        for name in names:
            node = self.nodes[name]
            host = node.host()
            host["_cassandra_config_new"] = {"cassandra.yaml": text(node.inventory)}
            out[name] = (host, node.inventory)
        return out

    def hostvars(self, names):
        return dict((name, dict(self.nodes[name].host(), inventory_hostname=name,
                                cassandra_storage_mode_running=self.given)) for name in names)

    def checks(self, names, written, kept=False, starts=(), into_none=False):
        """The guard as the command runs it on names -> the message of the first check that refuses, None."""
        extra = {"_cassandra_storage_mode_written": written}
        if self.given:
            extra["cassandra_storage_mode_running"] = self.given
        order = ([GIVEN] if self.given else []) + [BACK, JUMP] + ([INTO_NONE] if into_none else []) + (
            [KEPT] if kept else []) + [UNSURE]
        for name in order:
            ok, msg = guard(name, self.hosts(names), **extra)
            if not ok:
                return msg
        if starts:
            ok, msg = guard(STOPPED, self.hosts(names), _cassandra_storage_mode_starts=list(starts), **extra)
            if not ok:
                return msg
        return None

    def identity(self, names, tmp_path):
        """cassandra_config's identity check (before the guard) -> its message, None."""
        changed = [n for n in names if identity_changes(tmp_path, self.nodes[n].file, text(self.nodes[n].inventory))]
        if changed:
            return ["IDENTITY " + ", ".join(changed), "WAY OUT  %s: %s." % (IDENTITY, ", ".join(changed))]
        return None

    def restart(self, names):
        for name in names:
            if self.nodes[name].restart():
                self.crossed.append(name)

    def plan_warns(self, names, written):
        """The nodes the plan names in its point-of-no-return warning."""
        hostvars = self.hostvars(names)
        out = list(cassandra_storage_mode_restarts_into_none(names, hostvars))
        if written:  # apply_config: a cassandra.yaml written with NONE
            out += [n for n in names if self.nodes[n].inventory == "NONE" and mode_of(self.nodes[n].file) != "NONE"
                    and n not in cassandra_storage_mode_running_none([n], hostvars)]
        return out

    def run(self, command, names, tmp_path, given=None):
        """apply_config, rolling_restart or update_java on names -> the refusal's message, None when it ran."""
        saved = self.given
        if given is not None:
            self.given = given
        try:
            return self._run(command, names, tmp_path)
        finally:
            self.given = saved

    def _run(self, command, names, tmp_path):
        before = list(self.crossed)
        if command == "rolling_restart":
            msg = self.checks(names, False, into_none=True)
            if msg:
                return msg
            self.warned += self.plan_warns(names, False)
            self.restart([n for n in names if not self.nodes[n].stopped])
            assert set(self.crossed[len(before):]) <= set(self.warned), "into NONE unwarned: %s" % self.crossed
            return None
        if command == "update_java":
            msg = self.checks(names, True, kept=True)
            if msg:
                return msg
            self.warned += self.plan_warns(names, False)
        if command == "apply_config":
            msg = self.identity(names, tmp_path) or self.checks(names, True)
            if msg:
                return msg
            starts = [n for n in names if self.nodes[n].stopped and self.nodes[n].starts]
            msg = self.checks(names, True, starts=starts)
            if msg:
                return msg
            moved = [n for n in names if not self.nodes[n].stopped or self.nodes[n].starts]
            self.warned += self.plan_warns(moved, True)
        for name in names:  # at its turn: the identity check, the guard (cassandra_config), the write, the restart
            node = self.nodes[name]
            assert not self.identity([name], tmp_path), "identity refused at the turn of %s" % name
            assert not self.checks([name], True), "refused at the turn of %s" % name
            node.file = text(node.inventory)
            if (node.stopped and node.starts) or (not node.stopped and node.file != node.started):
                self.restart([name])
        assert set(self.crossed[len(before):]) <= set(self.warned), "into NONE unwarned: %s" % self.crossed
        return None


STEP = re.compile(r"(?<!cassandra_)storage_compatibility_mode: (?P<hand>\w+)(?: back)? in /\S+ by hand"
                  r"|cassandra_storage_compatibility_mode: (?P<inventory>\w+) in the inventory"
                  r"|run (?P<run>apply_config|rolling_restart) --limit (?P<limit>[\w,]+)"
                  r"(?: -e cassandra_storage_mode_running=(?P<e>[^\s()]+))?(?= \(|\.|,|$)"
                  r"|then set (?P<again>NONE) in the inventory again"
                  r"|(?P<start>start Cassandra)"
                  r"|-e cassandra_storage_mode_running=(?P<read>\S+:MODE)"
                  r"|(?P<drop>Fix or drop these items)"
                  r"|Run (?P<first>apply_config) first"
                  r"|(?P<identity>%s): (?P<changed>[\w, ]+)\." % IDENTITY)
NODE = re.compile(r"On (\w+), ")


def way_out(cluster, msg, names, tmp_path, choice):
    """Does what the WAY OUT says, to the letter (choice: the option taken where it is the operator's)."""
    lines = msg[msg.index(next(m for m in msg if m.startswith("WAY OUT"))):]
    assert any(STEP.search(x) or re.search(r"^\s+\w+  cassandra_storage_compatibility_mode", x) for x in lines), lines
    node, option, drop = None, None, False
    for line in lines:
        line = line.replace("WAY OUT  ", "", 1)
        if "without -e cassandra_storage_mode_running" in line:
            drop = True
        head = re.match(r"\s*(?:On (\w+), )?[Cc]hoose one:", line)
        if head:
            node, option = head.group(1), 0
            cluster.choices += 1
            continue
        number = re.match(r"\s*(\d)\. ", line)
        if option is not None and number:
            option = int(number.group(1))
        if option and option != choice and not line.strip().startswith("Then run this again"):
            continue
        kept = re.match(r"\s+(\w+)  cassandra_storage_compatibility_mode: (\w+)$", line)
        if kept:
            cluster.nodes[kept.group(1)].inventory = kept.group(2)
            continue
        for part in re.split(r"(?=On \w+, )", line):
            found = NODE.match(part)
            if found:
                node = found.group(1)
            for step in STEP.finditer(part):
                n = cluster.nodes.get(node)
                if step.group("hand"):
                    n.file = text(step.group("hand"))
                elif step.group("inventory"):
                    n.inventory = step.group("inventory")
                elif step.group("run"):
                    assert cluster.run(step.group("run"), step.group("limit").split(","), tmp_path,
                                       given=step.group("e") or "") is None, lines
                elif step.group("again"):
                    n.inventory = step.group("again")
                elif step.group("start"):
                    cluster.restart([node])
                elif step.group("read"):  # read by hand: the mode it runs (or last ran)
                    cluster.given = ",".join("%s:%s" % (i.split(":")[0], cluster.nodes[i.split(":")[0]].running)
                                             for i in step.group("read").split(","))
                elif step.group("drop"):
                    cluster.given = ""
                elif step.group("first"):  # a run of its own: its own way outs followed
                    given = cluster.given
                    follow("apply_config", cluster, tmp_path, names=names, choice=choice)
                    cluster.given = given
                else:
                    for changed in step.group("changed").split(", "):
                        cluster.nodes[changed].inventory = mode_of(cluster.nodes[changed].file)
    if drop:
        cluster.given = ""


def follow(command, cluster, tmp_path, names=None, given="", choice=1, rounds=10):
    """Runs command, follows each WAY OUT to the letter until it passes -> the stops seen, in order."""
    names = names or sorted(cluster.nodes)
    cluster.given = given
    seen, stops = set(), []
    for _round in range(rounds):
        state = (tuple(cluster.nodes[n].state() for n in sorted(cluster.nodes)), cluster.given)
        assert state not in seen, "a loop: %s" % stops
        seen.add(state)
        msg = cluster.run(command, names, tmp_path)
        if msg is None:
            return stops
        stops.append(msg[0])
        way_out(cluster, msg, names, tmp_path, choice)
    raise AssertionError("no end: %s" % stops)


def one(command, tmp_path, choice=1, given="", **node):
    cluster = Cluster({"n1": Node(**node)})
    return cluster, follow(command, cluster, tmp_path, given=given, choice=choice)


MODES = ("CASSANDRA_4", "UPGRADING", "NONE")


@pytest.mark.parametrize("command, running, file_mode, inventory, logged", list(
    itertools.product(("rolling_restart", "apply_config", "update_java"), MODES, MODES + (None,), MODES, (True, False))))
def test_every_way_out_ends_and_none_is_never_crossed_unwarned(tmp_path, command, running, file_mode, inventory, logged):
    for choice in (1, 2):  # the second option of a choice too, when one was offered
        cluster, stops = one(command, tmp_path, choice=choice, running=running, file_mode=file_mode,
                             inventory=inventory, logged=logged)
        assert len(stops) <= 4, stops
        assert set(cluster.crossed) <= set(cluster.warned)
        if not cluster.choices:
            break


def test_a_restart_into_none_with_the_inventory_set_back_is_refused(tmp_path):
    # apply_config cut after its write of NONE, the inventory set back to UPGRADING: the operator's choice
    cluster, stops = one("rolling_restart", tmp_path, choice=1, running="UPGRADING", file_mode="NONE",
                         inventory="UPGRADING")
    assert cluster.nodes["n1"].running == "UPGRADING" and not cluster.crossed
    assert stops == ["A restart would take n1 into storage_compatibility_mode NONE, the point of no return, which the"
                     " inventory does not have there. Nothing was changed."]
    cluster, stops = one("rolling_restart", tmp_path, choice=2, running="UPGRADING", file_mode="NONE",
                         inventory="UPGRADING")
    assert cluster.nodes["n1"].running == "NONE" and cluster.crossed == ["n1"] and "n1" in cluster.warned
    # update_java writes the inventory's mode: it says the inventory has another one than the file, either way warned
    for choice in (1, 2):
        cluster, stops = one("update_java", tmp_path, choice=choice, running="UPGRADING", file_mode="NONE",
                             inventory="UPGRADING")
        assert stops[0].startswith("This run would change storage_compatibility_mode on n1")
        assert set(cluster.crossed) <= set(cluster.warned)


@pytest.mark.parametrize("command", ["rolling_restart", "update_java"])
def test_a_restart_into_none_with_none_in_the_inventory_is_warned_in_the_plan(tmp_path, command):
    cluster, stops = one(command, tmp_path, running="UPGRADING", file_mode="NONE", inventory="NONE")
    assert stops == [] and cluster.crossed == ["n1"] and cluster.warned == ["n1"]


def test_a_4x_jvm_counts_as_cassandra_4_and_takes_no_mode_given(tmp_path):
    # a 4.x JVM, a 5.0 file at NONE (by hand): two steps; the way out keeps CASSANDRA_4, as the upgrade expects
    cluster, stops = one("rolling_restart", tmp_path, running="CASSANDRA_4", file_mode="NONE", inventory="NONE",
                         jvm4x=True)
    assert stops[0].startswith("A restart would take n1 from CASSANDRA_4 straight to NONE")
    assert cluster.nodes["n1"].running == "CASSANDRA_4" and cluster.nodes["n1"].inventory == "CASSANDRA_4"
    # -e n1:NONE for it: refused (it was taken before, the back way out then led to NONE)
    cluster, stops = one("rolling_restart", tmp_path, given="n1:NONE", running="CASSANDRA_4", file_mode=None,
                         inventory="CASSANDRA_4", jvm4x=True)
    assert stops[0].startswith("-e cassandra_storage_mode_running gives a mode this run cannot take")
    assert cluster.nodes["n1"].running == "CASSANDRA_4" and not cluster.crossed


@pytest.mark.parametrize("logged, record, first", [
    (True, True, "A start would take n2 from CASSANDRA_4 straight to NONE"),
    (False, True, "A start could move n2 back, or two steps on."),
    (False, False, "A start could move n2 back, or two steps on."),
])
def test_a_stopped_node_a_resumed_run_starts_is_checked_against_the_mode_it_last_ran(tmp_path, logged, record, first):
    # apply_config UPGRADING cut after n2 was stopped and written; NONE set, the run resumed: n2 would start in NONE
    cluster = Cluster({"n1": Node("UPGRADING", "UPGRADING", "NONE"),
                       "n2": Node("CASSANDRA_4", "UPGRADING", "NONE", logged=logged, record=record, stopped=True,
                                  starts=True, started=text("CASSANDRA_4"))})
    stops = follow("apply_config", cluster, tmp_path)
    assert stops[0].startswith(first), stops
    assert cluster.nodes["n2"].running == "NONE" and set(cluster.crossed) <= set(cluster.warned)
    # the record of its last start has this file: the mode it last ran is the file's (no stop)
    cluster = Cluster({"n2": Node("UPGRADING", "UPGRADING", "UPGRADING", logged=False, stopped=True, starts=True)})
    assert follow("apply_config", cluster, tmp_path) == []
    # stopped on purpose (not started): nothing to read it from, nothing started, not refused
    cluster = Cluster({"n2": Node("CASSANDRA_4", "UPGRADING", "UPGRADING", logged=False, record=False, stopped=True)})
    assert follow("apply_config", cluster, tmp_path) == []


def test_unread_starts_cql_reads_two_nodes_and_limit(tmp_path):
    # its start not read: unsure, the mode read by hand and given, then the way out
    cluster, stops = one("apply_config", tmp_path, running="CASSANDRA_4", file_mode="UPGRADING", inventory="NONE",
                         logged=False, start_read=False)
    assert stops[0].startswith("A restart could move n1 back, or two steps on.")
    assert set(cluster.crossed) <= set(cluster.warned)
    # read over CQL: known, no stop
    cluster, stops = one("rolling_restart", tmp_path, running="UPGRADING", file_mode="UPGRADING", inventory="UPGRADING",
                         logged=False, cql=True, started=text("CASSANDRA_4"))
    assert stops == []
    # two nodes, one refused: its own way out, the other untouched until the run passes
    cluster = Cluster({"n1": Node("NONE", "NONE", "NONE"), "n2": Node("NONE", "UPGRADING", "NONE")})
    assert len(follow("rolling_restart", cluster, tmp_path)) == 1
    assert [cluster.nodes[n].running for n in ("n1", "n2")] == ["NONE", "NONE"]
    # --limit: only the nodes of the run are checked
    cluster = Cluster({"n1": Node("NONE", "UPGRADING", "NONE"), "n2": Node("UPGRADING", "UPGRADING", "UPGRADING")})
    assert follow("rolling_restart", cluster, tmp_path, names=["n2"]) == []
    assert cluster.nodes["n1"].file == text("UPGRADING")


def test_a_wrong_given_mode_is_refused_and_its_way_out_ends(tmp_path):
    # it runs UPGRADING (logged), the file unchanged: -e n1:NONE contradicts it
    cluster, stops = one("rolling_restart", tmp_path, given="n1:NONE", running="UPGRADING", file_mode="UPGRADING",
                         inventory="UPGRADING")
    assert stops == ["-e cassandra_storage_mode_running gives a mode this run cannot take (node:MODE, the inventory"
                     " name of a node of this run, where the mode it runs is not read). Nothing was changed."]
    assert cluster.nodes["n1"].running == "UPGRADING"


@pytest.mark.parametrize("choice, running", [(1, "NONE"), (2, "UPGRADING")])
def test_update_java_way_out_ends(tmp_path, choice, running):
    # it runs UPGRADING, NONE in the inventory: apply_config first (it shows the change), or keep the file's mode
    cluster, stops = one("update_java", tmp_path, choice=choice, running="UPGRADING", file_mode="UPGRADING",
                         inventory="NONE")
    assert stops[0].startswith("This run would change storage_compatibility_mode on n1")
    assert cluster.nodes["n1"].running == running
