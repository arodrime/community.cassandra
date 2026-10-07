from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The playbooks ask for root on the nodes themselves: no -b, no become = true
# in ansible.cfg. Every play on the nodes declares become: true, but the ones
# that need less (listed with the reason); the plays on the controller never
# become (test_controller_become).

import glob
import os

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
CONTROLLER = ("localhost", "127.0.0.1")
# root only to read the JMX password file (cassandra_jmx_password_file, the service account's 0400)
JMX_FILE_ONLY = "{{ cassandra_jmx_password_file | default('', true) | length > 0 }}"
LESS = {
    ("status.yml", "Cluster status"): JMX_FILE_ONLY,
    ("health_check.yml", "Cluster health"): JMX_FILE_ONLY,
}


def plays():
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            for play in yaml.safe_load(f):
                if "hosts" in play:  # not an import_playbook
                    yield os.path.basename(path), play


NODE_PLAYS = [(name, play) for name, play in plays()
              if play["hosts"] not in CONTROLLER and play.get("connection") != "local"]


def test_there_are_node_plays():
    assert len(NODE_PLAYS) > 50
    assert set(LESS) <= set((name, play["name"]) for name, play in NODE_PLAYS)


@pytest.mark.parametrize("name, play", NODE_PLAYS, ids=["%s: %s" % (n, p["name"]) for n, p in NODE_PLAYS])
def test_every_node_play_asks_for_root(name, play):
    assert play.get("become") == LESS.get((name, play["name"]), True)
    assert "become_user" not in play  # root, not another account


@pytest.mark.parametrize("name", sorted(os.path.basename(p) for p in glob.glob(os.path.join(TOP, "playbooks", "*.yml"))))
def test_every_header_says_what_it_needs(name):
    with open(os.path.join(TOP, "playbooks", name), encoding="utf-8") as f:
        header = " ".join(line.lstrip("#").strip() for line in f.read().split("\n- ")[0].splitlines())
    assert "no -b needed" in header or "No root anywhere" in header, name
    assert " -b community.cassandra." not in header
