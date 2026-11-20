from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# What apply_config finds besides the config files: a version change (a release of the series in the node's restart,
# another series an upgrade, an older release refused), the ring against the inventory (topology's), a joined node's
# token, what the running Cassandra got from its unit; and the refusal they make before any change.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config_checks import (
    cassandra_apply_config_ring, cassandra_apply_config_stops, cassandra_apply_config_token, cassandra_apply_config_unit,
    cassandra_apply_config_version, cassandra_described_versions)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_patch_notes as notes

COMMANDS = {"upgrade": "ansible-playbook community.cassandra.upgrade -e cassandra_upgrade_phase=preflight",
            "topology_check": "ansible-playbook community.cassandra.topology --check",
            "topology": "ansible-playbook community.cassandra.topology"}


@pytest.mark.parametrize("series, package, running, kind, category", [
    ("41x", "", "4.1.5", "", ""),  # any release of the series: nothing
    ("41x", "4.1.5", "4.1.5", "", ""),
    ("41x", "4.1.5", "4.1.5-1", "", ""),  # (an rpm's release suffix)
    ("41x", "4.1.12", "4.1.11", "release", "B"),
    ("41x", "4.1.4", "4.1.5", "older", "C"),
    ("50x", "", "4.1.5", "series", "C"),
    ("50x", "5.0.6", "4.1.5", "series", "C"),
    ("41x", "", "5.0.2", "older", "C"),
    ("41x", "4.1.12", "5.0.9", "older", "C"),
    ("41x", "5.0.9", "5.0.9", "older", "C"),  # an older series asked with the release it runs: never an upgrade
    ("41x", "4.1.5", "", "", ""),  # not known (a host not set up): nothing said here
])
def test_the_version_change_and_its_category(series, package, running, kind, category):
    found = cassandra_apply_config_version(series, package, running)
    assert found.get("kind", "") == kind and found.get("category", "") == category


def test_a_release_the_notes_cover_with_what_it_crosses():
    found = cassandra_apply_config_version("50x", "5.0.9", "5.0.3")
    assert found["refused"] == [] and found["unknown"] is False
    assert found["notes"] == ["5.0.4: " + dict((r, t) for s, r, t in notes.NOTES)["5.0.4"],
                              "5.0.8: " + dict((r, t) for s, r, t in notes.NOTES)["5.0.8"]]


def test_a_release_newer_than_the_notes_stops_unless_allowed():
    newer = "4.1.%d" % (notes.numbers(notes.KNOWN["41x"])[2] + 1)
    assert cassandra_apply_config_version("41x", newer, "4.1.11")["unknown"] is True
    assert cassandra_apply_config_version("41x", newer, "4.1.11", allow_unknown="true")["unknown"] is False
    lines = cassandra_apply_config_stops([{"name": "n1", "series": "41x",
                                           "version": cassandra_apply_config_version("41x", newer, "4.1.11")}],
                                         commands=COMMANDS)
    assert lines[0].startswith("n1: Cassandra %s is newer than the releases the collection's notes cover (up to %s)"
                               % (newer, notes.KNOWN["41x"]))
    assert "    -e cassandra_allow_unknown_patch=true" in lines


def test_a_package_updated_by_other_means_since_the_start():
    # (dnf update, Cassandra not restarted: installed 4.1.12 or 4.1.13, the JVM 4.1.11)
    same = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", installed="4.1.12")
    assert (same["kind"], same["installed"], same["refused"]) == ("release", "4.1.12", [])
    assert cassandra_apply_config_version("41x", "4.1.12", "4.1.11")["installed"] == "4.1.11"  # (not read: the running)
    assert cassandra_apply_config_version("41x", "4.1.12", "4.1.11", installed="4.1.11-1")["installed"] == "4.1.11"
    # newer than the inventory's: refused before any change (its window would downgrade with Cassandra stopped)
    newer = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", installed="4.1.13")
    lines = cassandra_apply_config_stops([{"name": "n1", "version": newer}], commands=COMMANDS)
    assert lines == ["n1: its package is Cassandra 4.1.13, newer than the inventory's 4.1.12: this run does not go back"
                     " to an older package. Set cassandra_package_version to 4.1.13 (its restart then runs it), or"
                     " install 4.1.12 by hand (now 4.1.11).", "then run apply_config again."]


def test_a_package_left_half_done_stops_a_release_before_any_change():
    # (audit) an interrupted dpkg (half-configured) or rpm transaction (two cassandra headers): the update of the
    # window would start from that state, cassandra_install's version check then failing after the confirmation
    two = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", unfinished="installed 4.1.11, installed 4.1.12")
    deb = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", unfinished="half-configured 4.1.12")
    unpacking = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", unfinished="half-installed 4.1.12")
    lines = cassandra_apply_config_stops([{"name": "n1", "version": two}, {"name": "n2", "version": deb},
                                          {"name": "n3", "version": unpacking}], commands=COMMANDS)
    # (each node its own reason, not its group's: the same change on all; each its own way out)
    assert lines == [
        "n2: its Cassandra package is half-configured 4.1.12 (dpkg), not fully installed: finish it by hand first"
        " (dpkg --configure -a) (now 4.1.11).",
        "n3: its Cassandra package is half-installed 4.1.12 (dpkg), not fully installed: reinstall it by hand first"
        " (apt-get install --reinstall cassandra) (now 4.1.11).",
        "n1: rpm -q cassandra lists 2 packages: 4.1.11, 4.1.12, a transaction left half done: remove the one not"
        " wanted by hand first (dnf remove --duplicates cassandra) (now 4.1.11).", "then run apply_config again."]
    # no release to install: nothing refused for it
    assert cassandra_apply_config_version("41x", "4.1.11", "4.1.11", unfinished="half-configured 4.1.11") == {}
    assert cassandra_apply_config_version("41x", "4.1.12", "4.1.11", unfinished="")["refused"] == []


@pytest.mark.parametrize("options, refused", [
    (["-Dcassandra.auth_bcrypt_gensalt_log2_rounds=31"], True),
    (["-Dcassandra.auth_bcrypt_gensalt_log2_rounds=30"], False),
    ([], False),
])
def test_a_release_that_would_not_start_stops(options, refused):
    found = cassandra_apply_config_version("41x", "4.1.12", "4.1.11", {"cassandra_jvm_options": options})
    assert bool(found["refused"]) is refused
    lines = cassandra_apply_config_stops([{"name": "n1", "version": found}], commands=COMMANDS)
    assert bool(lines) is refused
    if refused:
        assert "refuses to start" in lines[0] and lines[-1] == "then run apply_config again."


def test_the_offheap_refusal_waits_for_its_release():
    settings = {"cassandra_memtable_allocation_type": "offheap_objects", "cassandra_memtable_offheap_space": "0MiB"}
    assert notes.refusals("41x", "4.1.12", settings) == []
    assert notes.refusals("41x", "4.1.13", settings)[0].startswith("memtable_offheap_space 0")
    assert notes.refusals("41x", "4.1.13", dict(settings, cassandra_memtable_offheap_space="")) == []


def test_an_upgrade_and_an_older_release_stop_with_their_way_out():
    nodes = [{"name": "n1", "version": cassandra_apply_config_version("50x", "5.0.6", "4.1.5")},
             {"name": "n2", "version": cassandra_apply_config_version("50x", "5.0.6", "4.1.5")},
             {"name": "n3", "version": cassandra_apply_config_version("41x", "4.1.4", "4.1.5")}]
    lines = cassandra_apply_config_stops(nodes, commands=COMMANDS)
    assert lines[0].startswith("n3: runs Cassandra 4.1.5, the inventory asks for 4.1.4: Cassandra does not go back")
    assert lines[1].startswith("n1, n2: runs Cassandra 4.1.5, the inventory asks for 5.0.6: another series is an upgrade")
    assert "    " + COMMANDS["upgrade"] in lines
    # an older series: the way back names it, never the upgrade (e2e: 41x on a 5.0.9 node, then its release only)
    for package, asked in (("4.1.12", "4.1.12"), ("5.0.9", "5.0.9 (cassandra_version 41x)")):
        lines = cassandra_apply_config_stops([{"name": "n1", "version": cassandra_apply_config_version(
            "41x", package, "5.0.9")}], commands=COMMANDS)
        assert lines[0] == ("n1: runs Cassandra 5.0.9, the inventory asks for %s: Cassandra does not go back to an"
                            " older release by itself (what the newer one wrote may not be read). Set cassandra_version"
                            " to 50x and cassandra_package_version to 5.0.9, the series and release it runs." % asked)
        assert "    " + COMMANDS["upgrade"] not in lines
    # a release of the series, nothing against it: no stop (the node's restart takes it)
    assert cassandra_apply_config_stops([{"name": "n1", "version": cassandra_apply_config_version(
        "41x", "4.1.12", "4.1.11")}], commands=COMMANDS) == []


RING = {"dc1": {"nodes": [{"address": "10.0.0.1"}, {"address": "10.0.0.2"}, {"address": "10.0.0.9"}]}}


def test_the_ring_against_the_inventory():
    found = cassandra_apply_config_ring([{"name": "n1", "address": "10.0.0.1"}, {"name": "n4", "address": "10.0.0.4"}],
                                        [{"name": "n2", "addresses": ["10.0.0.2", "n2"]},
                                         {"name": "n5", "addresses": ["10.0.0.5"]}],
                                        ["10.0.0.1", "10.0.0.4", "10.0.0.2", "10.0.0.5"], RING)
    assert found == {"add": ["n4"], "remove": ["n2"], "unknown": ["10.0.0.9"]}
    lines = cassandra_apply_config_stops([], found, COMMANDS)
    assert lines[0] == "n4: in the inventory, not in the ring: adding a node is topology's (it bootstraps it)."
    assert lines[1].startswith("n2: marked cassandra_node_state: absent, still in the ring")
    assert lines[-4:] == ["the nodes to add or remove, the plan (changing nothing), then the run:",
                          "    " + COMMANDS["topology_check"], "    " + COMMANDS["topology"], "then run apply_config again."]
    # a ring node the inventory lacks alone: said, no stop (as topology, which refuses only with steps to do)
    assert cassandra_apply_config_stops([], dict(found, add=[], remove=[]), COMMANDS) == []
    # no node running: nothing judged
    assert cassandra_apply_config_ring([{"name": "n4", "address": "10.0.0.4"}], [], [], {}) == \
        {"add": [], "remove": [], "unknown": []}


@pytest.mark.parametrize("inventory, live, owned, refused", [
    ("", "-922", ["-922"], False),  # none in the inventory
    ("-922", "-922", ["100"], False),  # nothing written (import: the file's value)
    ("100", "-922", ["100"], False),  # the record of the token it owns (after move_node)
    ("200", "-922", ["100"], True),  # another token: move_node's
    ("200", "-922", [], False),  # not in the ring: nothing judged
    ("200", "-922", None, True),  # the ring read and failed: not known, refused
    ("-922", "-922", None, False),
])
def test_a_joined_node_s_token(inventory, live, owned, refused):
    why = cassandra_apply_config_token(inventory, live, owned)
    assert bool(why) is refused
    if refused:
        assert ("move_node" in why or "could not be read" in why) and "first start only" in why


LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max processes             32768                32768                processes
Max open files            100000               100000               files
Max locked memory         unlimited            unlimited            bytes
Max address space         unlimited            unlimited            bytes
"""
WANTED = {"user": "cassandra", "group": "cassandra",
          "limits": {"nofile": 100000, "nproc": 32768, "memlock": "infinity", "as": "infinity"}, "environment": {}}


def test_the_unit_compared_with_what_the_running_cassandra_got():
    running = {"user": "cassandra", "group": "cassandra", "limits": LIMITS, "environment": ""}
    assert cassandra_apply_config_unit(running, WANTED) == []  # (an import: the same)
    changed = dict(WANTED, limits=dict(WANTED["limits"], nofile=1048576), environment={"JAVA_HOME": "/opt/x"})
    assert cassandra_apply_config_unit(running, changed) == [
        {"item": "unit LimitNOFILE", "before": "100000", "after": "1048576"},
        {"item": "unit Environment JAVA_HOME", "before": "absent", "after": "/opt/x"}]
    # removed from the inventory: the next start does not get it
    assert cassandra_apply_config_unit(dict(running, environment='"A=x y"'), WANTED) == [
        {"item": "unit Environment A", "before": "x y", "after": "absent"}]
    # memlock in a unit's suffix; the init script (no limits): only its environment
    assert cassandra_apply_config_unit(dict(running, limits=LIMITS.replace("unlimited            unlimited            bytes\nMax a",
                                                                           "65536                65536                bytes\nMax a")),
                                       dict(WANTED, limits=dict(WANTED["limits"], memlock="64K"))) == []
    assert cassandra_apply_config_unit(running, dict(WANTED, limits={})) == []
    assert cassandra_apply_config_unit({}, changed) == []  # not read: nothing


def test_the_unit_environment_masks_its_secrets():
    running = {"user": "cassandra", "group": "cassandra", "limits": LIMITS,
               "environment": "AWS_SECRET_ACCESS_KEY=zorglub1 JAVA_HOME=/opt/x"}
    wanted = dict(WANTED, environment={"AWS_SECRET_ACCESS_KEY": "zorglub2", "JAVA_HOME": "/opt/x",
                                       "JVM_EXTRA_OPTS": "-Djavax.net.ssl.keyStorePassword=zorglub3"})
    assert cassandra_apply_config_unit(running, wanted) == [
        {"item": "unit Environment AWS_SECRET_ACCESS_KEY", "before": "****", "after": "**** (another)"},
        {"item": "unit Environment JVM_EXTRA_OPTS", "before": "absent", "after": "****"}]
    # removed: absent after; a value in clear when it holds no secret
    assert cassandra_apply_config_unit(running, dict(WANTED, environment={"JAVA_HOME": "/opt/y"})) == [
        {"item": "unit Environment AWS_SECRET_ACCESS_KEY", "before": "****", "after": "absent"},
        {"item": "unit Environment JAVA_HOME", "before": "/opt/x", "after": "/opt/y"}]


def test_the_versions_described():
    text = ("Cluster Information:\n\tName: zorglub\nDatabase versions:\n\t\t4.1.5: [10.0.0.1:7000, 10.0.0.2:7000]\n\n"
            "\t\t4.1.6: [10.0.0.3:7000]\n\nOther: x\n")
    assert cassandra_described_versions(text) == {"10.0.0.1": "4.1.5", "10.0.0.2": "4.1.5"}
    assert cassandra_described_versions("Database versions:\n\t5.0.2: [[2001:db8::1]:7000]\n") == {"2001:db8::1": "5.0.2"}
    assert cassandra_described_versions("") == {}


def test_a_limit_the_host_caps_plans_no_restart():
    # (a container: the unit's LimitNOFILE above what the host lets systemd give, the JVM gets the host's cap; a
    # restart would give it the same: none planned, every run)
    capped = LIMITS.replace("100000               100000", "524288               524288")
    host = LIMITS.replace("100000               100000", "524288               524288")
    running = {"user": "cassandra", "group": "cassandra", "limits": capped, "environment": "", "ceiling": host}
    wanted = dict(WANTED, limits=dict(WANTED["limits"], nofile=1048576))
    assert cassandra_apply_config_unit(running, wanted) == []
    # under the cap: compared as it is
    assert cassandra_apply_config_unit(dict(running, limits=LIMITS), wanted) == [
        {"item": "unit LimitNOFILE", "before": "100000", "after": "1048576 (524288: the host's cap)"}]
    # a privileged container: its systemd gave the unit's limit past its own: the same, nothing to do
    raised = LIMITS.replace("100000               100000", "1048576              1048576")
    assert cassandra_apply_config_unit(dict(running, limits=raised), wanted) == []
    # no cap read: as before
    assert len(cassandra_apply_config_unit(dict(running, ceiling=""), wanted)) == 1


def test_the_unit_environment_is_the_one_the_jvm_runs_with():
    # (a unit edited, systemctl daemon-reload, no restart: systemctl show has the new environment, the JVM the old)
    running = {"user": "cassandra", "group": "cassandra", "limits": LIMITS, "environment": "JAVA_HOME=/opt/y",
               "environ": "JAVA_HOME=/opt/x\0JVM_OPTS=-Xss256k -Dz=1\0PATH=/usr/bin\0",
               "unit_mtime": "1700000100", "start": "1700000000.25"}
    wanted = dict(WANTED, environment={"JAVA_HOME": "/opt/y"})
    assert cassandra_apply_config_unit(running, wanted) == [
        {"item": "unit Environment JAVA_HOME", "before": "/opt/x", "after": "/opt/y"}]
    # added to the unit since the start: the JVM has none
    assert cassandra_apply_config_unit(dict(running, environ="PATH=/usr/bin\0"), wanted) == [
        {"item": "unit Environment JAVA_HOME", "before": "absent", "after": "/opt/y"}]
    # the same (an import), and what bin/cassandra rewrites (JVM_OPTS) taken from the unit, not the JVM
    same = dict(running, environment="JAVA_HOME=/opt/x JVM_OPTS=-Dz=1")
    assert cassandra_apply_config_unit(same, dict(WANTED, environment={"JAVA_HOME": "/opt/x", "JVM_OPTS": "-Dz=1"})) == []
    # the JVM's environment not read: the unit's, as loaded
    assert cassandra_apply_config_unit(dict(running, environ=""), wanted) == []
    # the unit not written since the start (or its time not read): the unit's, as loaded
    assert cassandra_apply_config_unit(dict(running, unit_mtime="1699999000"), wanted) == []
    assert cassandra_apply_config_unit(dict(running, unit_mtime=""), wanted) == []


def test_an_imported_unit_environment_the_scripts_rewrite_is_a_no_op():
    # (the unit exports names cassandra-env.sh, bin/cassandra or the init script give a value of their own: the JVM
    # keeps the scripts' value, the import carries the unit's; no restart, run after run)
    unit = {"JMX_PORT": "7299", "HEAP_NEWSIZE": "800M", "LD_PRELOAD": "/opt/x/libz.so",
            "CASSANDRA_INCLUDE": "/opt/x/in.sh", "CASSANDRA_OWNR": "zorglub", "MAX_HEAP_SIZE": "8G"}
    environ = ("JMX_PORT=7199\0HEAP_NEWSIZE=2048M\0LD_PRELOAD=/usr/lib64/libjemalloc.so.2\0"
               "CASSANDRA_INCLUDE=/usr/share/cassandra/cassandra.in.sh\0CASSANDRA_OWNR=cassandra\0MAX_HEAP_SIZE=16G\0")
    running = {"user": "cassandra", "group": "cassandra", "limits": LIMITS, "environ": environ, "start": "1700000000.25",
               "unit_mtime": "1690000000", "environment": " ".join("%s=%s" % kv for kv in sorted(unit.items()))}
    assert cassandra_apply_config_unit(running, dict(WANTED, environment=unit)) == []


def test_the_nodes_and_the_ring_sentence_are_built_once():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
        cassandra_apply_config_nodes, cassandra_apply_config_ring_unknown)
    hostvars = {"n1": {"cassandra_apply_config_todo": "True", "cassandra_apply_config_then": "restart",
                       "cassandra_apply_config_os": [{"item": "x"}], "cassandra_op_result": "applied"},
                "n2": {}}
    plan = cassandra_apply_config_nodes(["n1", "n2"], hostvars)
    assert plan[0]["todo"] is True and plan[0]["os"] is True and plan[0]["result"] == "applied"
    assert plan[1] == {"name": "n2", "todo": False, "os": False, "why": "", "steps": [], "then": "",
                       "result": "nothing to apply", "notes": [], "kept_running": [], "unit_failed": False,
                       "os_done": False, "done": False}
    recap = cassandra_apply_config_nodes(["n1", "n2", "n3"], dict(hostvars, n3={}), ran=["n1", "n2"])
    assert [n["result"] for n in recap] == ["applied", "not done (the run stopped before it)", "not in this run (--limit)"]
    assert cassandra_apply_config_ring_unknown({}) == ""
    assert cassandra_apply_config_ring_unknown({"unknown": ["10.0.0.9"]}) == (
        "has 10.0.0.9, which no host of the inventory has: left as it is (topology --check shows the ring against the"
        " inventory)")
    assert "left as they are" in cassandra_apply_config_ring_unknown({"unknown": ["10.0.0.8", "10.0.0.9"]})


def test_one_version_parser():
    assert notes.numbers("4.1.5-1") == (4, 1, 5) and notes.numbers("5.0~beta1") == ()
    assert notes.numbers("5.0~beta1", partial=True) == (5, 0, 0) and notes.numbers("5.0.2", partial=True) == (5, 0, 2)
    assert notes.numbers("x", partial=True) == ()
    # a beta the node runs, the series' release asked: a newer release
    assert cassandra_apply_config_version("50x", "5.0.1", "5.0~beta1")["kind"] == "release"
