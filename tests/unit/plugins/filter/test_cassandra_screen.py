from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import PARAGRAPH

SPEC = {
    "operation": "stop_rack", "cluster": "Orders", "version": "4.1.5", "summary": "stop dc1 / rack2",
    "intro": ["Stops Cassandra on every node of the rack at once.", ""],
    "blocks": [{"title": "node2  10.0.0.2", "lines": ["drained, then stopped", {"pre": ["  a   b", "  c   d"]}]},
               {"title": "node5  10.0.0.5"}],
    "after": ["They stay down until start_rack."],
    "warnings": [
        {"label": "replication", "each": ["orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails",
                                          "orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails"]},
        {"label": "replicas", "text": ["forced although:", "orders loses 2 replicas", "events loses 2 replicas"]},
        {"label": "empty", "text": ""},
        {"label": "data loss", "text": "everything is deleted.", "real_run": True},
    ],
}
SESSION = "this run is not inside tmux or screen: if the SSH session to this machine drops, the run stops."

# "" between two blocks (a rule under the ops callback), PARAGRAPH (shown as ~ here) between two
# paragraphs of a block: each warning one
REAL = """\
stop_rack on cluster 'Orders' (Cassandra 4.1.5): stop dc1 / rack2

Stops Cassandra on every node of the rack at once.

  node2  10.0.0.2
    drained, then stopped
      a   b
      c   d

  node5  10.0.0.5

They stay down until start_rack.

WARNING - replication: orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails
~
WARNING - replicas: forced although:
  - orders loses 2 replicas
  - events loses 2 replicas
~
WARNING - data loss: everything is deleted.
~
WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,
  the run stops.""".replace("\n~\n", "\n" + PARAGRAPH + "\n")


def test_layout():
    assert cassandra_screen(SPEC, session=SESSION) == REAL


def test_check_names_the_real_run_warnings_on_one_line():
    # the warnings together, then in the question's place what --check means, one blank line apart
    expected = REAL.split("\nWARNING - data loss:", maxsplit=1)[0] + "\n(A real run would also warn about: data loss, session.)" + \
        "\n\n--check: nothing will be changed (the plan only, no question)."
    assert cassandra_screen(SPEC, check=True, session=SESSION) == expected


def test_no_question():
    lines = cassandra_screen(SPEC, asks=False).split("\n")
    assert lines[-2:] == ["", "cassandra_operation_confirm is false: no question, the run goes on."]
    assert "WARNING - session" not in "\n".join(lines)  # no session given: short operation


def test_wrapping_keeps_words_and_paths_whole():
    path = "/var/lib/cassandra/" + "x" * 120
    text = cassandra_screen({"operation": "op", "intro": ["word " * 30 + path]})
    lines = text.split("\n")
    assert all(len(line) <= 100 for line in lines if path not in line)
    assert lines[-1] == path
    assert lines[2].startswith("word word")


def test_paragraphs_a_blank_line_apart():
    # the intro, the sections and what comes after: each item a paragraph; a node's lines stay together
    text = cassandra_screen({"operation": "op", "intro": ["one", "two"], "blocks": [{"title": "node1", "lines": ["a", "b"]}],
                             "after": ["three", "", "four"]})
    assert text.split("\n") == ["op", "", "one", PARAGRAPH, "two", "", "  node1", "    a", "    b", "", "three",
                                PARAGRAPH, "four"]


def test_a_lead_in_keeps_what_it_introduces():
    text = cassandra_screen({"operation": "op", "intro": ["Check that:", {"pre": []}, {"pre": ["  - a", "  - b"]}, "next"]})
    assert text.split("\n") == ["op", "", "Check that:", "  - a", "  - b", PARAGRAPH, "next"]


def test_minimal_spec():
    assert cassandra_screen({"operation": "apply_config"}) == "apply_config"
    assert cassandra_screen({}) == ""


NODES = [{"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1" if i < 7 else "dc2",
          "rack": "rack%d" % (i % 2 + 1), "seed": i == 1} for i in range(1, 9)]


def test_a_step_of_another_playbook_asks_nothing():
    text = cassandra_screen({"operation": "add_node", "summary": "add node7"}, asked_by="topology")
    assert text == "add_node: add node7\n\nA step of topology, confirmed on its screen: no question here."
    # --check says so in its place: nothing changes
    assert cassandra_screen({"operation": "add_node"}, check=True, asked_by="topology").endswith(
        "--check: nothing will be changed (the plan only, no question).")
