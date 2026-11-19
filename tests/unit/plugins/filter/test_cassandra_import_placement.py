from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Where import_cluster puts each value: the most common one in the cluster's group_vars, each other node's in its
# host_vars (a dict whole), nothing in a DC's or rack's group_vars but cassandra_dc and cassandra_rack. Whatever
# the layout, every node gets exactly the values it has (as Ansible merges group_vars and host_vars).

import functools
import random

import pytest

from ansible_collections.community.cassandra.plugins.filter import cassandra_import
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4, LOGBACK_END, MISSING, RACKDC, _cluster_group, _dc_group, _place_highest, _rack_group, _roles_defaults,
    cassandra_inventory_files, cassandra_inventory_layout, cassandra_inventory_layout_over,
    cassandra_inventory_leftovers, GENERATED)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    cassandra_inventory_host_vars)

DEFAULTS = _roles_defaults()


def node(name, dc, rack, **variables):
    return {"name": name, "dc": dc, "rack": rack, "read": True, "vars": variables,
            "hand_edits": [], "normalized": [], "notes": []}


def effective(layout, name, extra=()):
    """What host name gets from the files the import writes (and extra files of yours), as Ansible merges them."""
    files = cassandra_inventory_files(layout) + list(extra)
    return cassandra_inventory_host_vars(files, layout["hosts"], name)


def assert_same_values(layout, nodes, keys, extra=()):
    """Every node read gets its own value of each key: the one read, else the collection's default (or the key is
    not set at all)."""
    for n in nodes:
        got = effective(layout, n["name"], extra)
        for key in keys:
            if key in n["vars"]:
                assert key in got and got[key] == n["vars"][key], (n["name"], key)
            elif key in got:
                assert got[key] == DEFAULTS[key], (n["name"], key)


# The values the random layouts draw from: plain defaults, a default made of another variable, no default at all,
# a dict, a list, a secret, values per node by nature, rackdc's dc=
POOLS = {
    "cassandra_heap_size": ["8G", "16G", "", MISSING],
    "cassandra_concurrent_reads": [32, 64, 128, MISSING],
    "cassandra_config_group": ["cassandraops", "cassandra", MISSING],  # default: {{ cassandra_group }}
    "cassandra_jvm11_gc": ["G1", "CMS", MISSING],  # no default: cassandra_jvm_gc's
    "cassandra_service_environment": [{"A": "1"}, {"A": "1", "B": "2"}, {}, MISSING],
    "cassandra_jvm_extra_options": [["-Dx=1"], ["-Dx=1", "-Dy=2"], [], MISSING],
    "cassandra_jmx_password": ["s3cret", "other", "{{ not a template }}", MISSING],
    "cassandra_listen_address": [IPV4, "UNIQUE", MISSING],
    "cassandra_initial_token": ["UNIQUE", MISSING],
    "cassandra_rackdc_dc": ["dcX", "dcY", MISSING],
}


def random_nodes(seed):
    rnd = random.Random(seed)
    nodes = []
    for i in range(rnd.randint(1, 9)):
        dc, rack = "dc%d" % rnd.randint(1, 2), "r%d" % rnd.randint(1, 3)
        variables = {}
        for key, pool in POOLS.items():
            value = rnd.choice(pool[:rnd.randint(1, len(pool))])  # (fewer values more often: majorities)
            if value == "UNIQUE":
                value = "10.0.%d.%d" % (seed % 250, i + 1)
            if value is not MISSING:
                variables[key] = value
        nodes.append(node("n%d" % (i + 1), dc, rack, **variables))
    return nodes


def old_layout(nodes, monkeypatch):
    """The layout of the releases before: each value at the highest level (cluster, DC, rack, node) where all the
    nodes there agree."""
    def highest(key, read, cluster, group_vars, host_vars, given=None, said=None):
        levels = [functools.partial(_cluster_group, cluster), functools.partial(_dc_group, cluster),
                  functools.partial(_rack_group, cluster)]
        return _place_highest(key, read, levels, group_vars, host_vars)
    with monkeypatch.context() as m:
        m.setattr(cassandra_import, "_place", highest)
        return cassandra_inventory_layout([dict(n, vars=dict(n["vars"])) for n in nodes], "c")


@pytest.mark.parametrize("seed", range(120))
def test_random_layouts_give_every_node_its_values(seed, monkeypatch):
    nodes = random_nodes(seed)
    layout = cassandra_inventory_layout([dict(n, vars=dict(n["vars"])) for n in nodes], "c")
    assert_same_values(layout, nodes, POOLS)
    # the same values as the layout of the releases before, node by node
    old = old_layout(nodes, monkeypatch)
    for n in nodes:
        mine, before = effective(layout, n["name"]), effective(old, n["name"])
        for key in POOLS:
            assert mine.get(key, DEFAULTS.get(key, MISSING)) == before.get(key, DEFAULTS.get(key, MISSING)), (
                n["name"], key)
    # a DC's or a rack's group_vars: its dc or rack, rackdc's dc= (a DC's by nature), nothing else
    for group, variables in layout["group_vars"].items():
        if group != "c":
            assert set(variables) <= {"cassandra_dc", "cassandra_rack"} | set(RACKDC), group
    # host_vars hold only a node's difference from the cluster's group_vars
    cluster = layout["group_vars"]["c"]
    for name, variables in layout["host_vars"].items():
        for key, value in variables.items():
            if key in cluster:
                assert value != cluster[key], (name, key)
    # the most common value in the cluster's group_vars (no node of another value outnumbers it)
    for key in [k for k in POOLS if k in cluster and k not in RACKDC]:
        holding = [n for n in nodes if n["vars"].get(key, MISSING) == cluster[key]]
        for value in [v for v in POOLS[key] if v is not MISSING]:
            assert len([n for n in nodes if n["vars"].get(key, MISSING) == value]) <= len(holding), (key, value)


def six_nodes(**odd):
    """6 nodes over 3 racks, n1 (rack r1) with other values."""
    nodes = [node("n%d" % i, "dc1", "r%d" % ((i - 1) // 2 + 1), cassandra_concurrent_reads=64,
                  cassandra_service_environment={"A": "1", "B": "2"}) for i in range(1, 7)]
    nodes[0]["vars"].update(odd)
    return nodes


def test_one_odd_node_its_differences_alone_in_its_host_vars():
    # before: n1 in r1 sent the others' values to r1's other node (host_vars) and to r2's and r3's group_vars
    layout = cassandra_inventory_layout(six_nodes(cassandra_concurrent_reads=32,
                                                  cassandra_service_environment={"A": "1"}), "c")
    assert layout["group_vars"]["c"] == {"cassandra_concurrent_reads": 64,
                                         "cassandra_service_environment": {"A": "1", "B": "2"}}
    # the dict whole (Ansible replaces a dict, no merge)
    assert layout["host_vars"] == {"n1": {"cassandra_concurrent_reads": 32,
                                          "cassandra_service_environment": {"A": "1"}}}
    assert layout["group_vars"]["c_dc1"] == {"cassandra_dc": "dc1"}
    assert all(layout["group_vars"]["c_dc1_r%d" % r] == {"cassandra_rack": "r%d" % r} for r in (1, 2, 3))
    assert "A TIE" not in layout["report"]


def test_a_node_on_the_default_gets_it_written_in_its_host_vars():
    nodes = six_nodes()
    del nodes[0]["vars"]["cassandra_concurrent_reads"]
    del nodes[0]["vars"]["cassandra_service_environment"]
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["host_vars"] == {"n1": {"cassandra_concurrent_reads": 32, "cassandra_service_environment": {}}}
    assert_same_values(layout, nodes, ["cassandra_concurrent_reads", "cassandra_service_environment"])


def test_a_tie_takes_the_default_when_it_is_one_of_the_values_and_says_so():
    nodes = [node("n1", "dc1", "r1", cassandra_concurrent_reads=64), node("n2", "dc1", "r1"),
             node("n3", "dc1", "r2"), node("n4", "dc1", "r2", cassandra_concurrent_reads=64)]
    layout = cassandra_inventory_layout(nodes, "c")
    # 64 on 2 nodes, the default on 2 (not set): nothing in the cluster's group_vars
    assert "cassandra_concurrent_reads" not in layout["group_vars"]["c"]
    assert layout["host_vars"] == {"n1": {"cassandra_concurrent_reads": 64}, "n4": {"cassandra_concurrent_reads": 64}}
    assert "  cassandra_concurrent_reads: the collection's default" in layout["report"].split("\n")
    # the default's value read on a node (32): that one
    nodes = [node("n1", "dc1", "r1", cassandra_concurrent_reads=64), node("n2", "dc1", "r1", cassandra_concurrent_reads=32)]
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"] == {"cassandra_concurrent_reads": 32}
    assert layout["host_vars"] == {"n1": {"cassandra_concurrent_reads": 64}}
    assert "  cassandra_concurrent_reads: the collection's default" in layout["report"].split("\n")


def test_a_tie_without_the_default_takes_the_first_node_of_the_hosts_file():
    # (the hosts file's order: by DC, rack, name; not the order the nodes were read in)
    nodes = [node("n9", "dc2", "r1", cassandra_concurrent_reads=128), node("n1", "dc1", "r2", cassandra_concurrent_reads=64)]
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"] == {"cassandra_concurrent_reads": 64}
    assert layout["host_vars"] == {"n9": {"cassandra_concurrent_reads": 128}}
    lines = layout["report"].split("\n")
    head = lines.index("A TIE, as many nodes on each value: the one in group_vars/c/ (the others in their nodes'"
                       " host_vars):")
    assert lines[head + 1:head + 3] == ["  cassandra_concurrent_reads: the one of n1, the first node in the hosts file", ""]


def test_a_secret_follows_the_same_rule_in_secrets_yml():
    nodes = six_nodes(cassandra_jmx_password="odd")
    for n in nodes[1:]:
        n["vars"]["cassandra_jmx_password"] = "s3cret"
    files = dict((f["path"], f) for f in cassandra_inventory_files(cassandra_inventory_layout(nodes, "c")))
    assert files["group_vars/c/secrets.yml"]["secret"] and "s3cret" in files["group_vars/c/secrets.yml"]["content"]
    assert files["host_vars/n1/secrets.yml"]["secret"] and "odd" in files["host_vars/n1/secrets.yml"]["content"]
    assert not any("s3cret" in f["content"] or "odd" in f["content"] for f in files.values() if not f["secret"])


def test_a_value_per_node_by_nature_never_goes_to_the_cluster_from_one_node():
    nodes = [node("n%d" % i, "dc1", "r1", cassandra_initial_token=str(i * 100), cassandra_listen_address="10.0.0.%d" % i)
             for i in range(1, 4)]
    nodes[2]["vars"]["cassandra_listen_address"] = IPV4
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"] == {}
    assert all(set(v) == {"cassandra_initial_token", "cassandra_listen_address"} for v in layout["host_vars"].values())
    assert "A TIE" not in layout["report"]
    # shared by most: in the cluster's group_vars
    nodes[1]["vars"]["cassandra_listen_address"] = IPV4
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"] == {"cassandra_listen_address": IPV4}


def test_a_default_made_of_other_variables_keeps_the_value_per_node_and_says_so():
    nodes = six_nodes(cassandra_config_group="cassandra")  # (n1: written, its default is {{ cassandra_group }})
    for n in nodes[2:]:
        n["vars"]["cassandra_config_group"] = "cassandraops"
    layout = cassandra_inventory_layout(nodes, "c")  # n2 on the default: not writable as a value
    assert "cassandra_config_group" not in layout["group_vars"]["c"]
    assert sorted(h for h, v in layout["host_vars"].items() if "cassandra_config_group" in v) == \
        ["n1", "n3", "n4", "n5", "n6"]
    assert "\n  cassandra_config_group\n" in layout["report"]
    assert_same_values(layout, nodes, ["cassandra_config_group"])


def test_no_default_keeps_the_value_per_node():
    nodes = six_nodes()
    for n in nodes[1:]:
        n["vars"]["cassandra_jvm11_gc"] = "G1"  # n1 without it: cassandra_jvm_gc's applies
    layout = cassandra_inventory_layout(nodes, "c")
    assert "cassandra_jvm11_gc" not in layout["group_vars"]["c"]
    assert "cassandra_jvm11_gc" not in layout["host_vars"].get("n1", {})


def test_the_most_common_snippet_is_written_once_for_the_cluster():
    lines = ["<!-- the site's -->", "<logger name=\"x\" level=\"WARN\"/>"]
    nodes = [node("n%d" % i, "dc1", "r1", **{LOGBACK_END: list(lines)}) for i in range(1, 4)]
    nodes[0]["vars"][LOGBACK_END] = ["<logger name=\"y\" level=\"INFO\"/>"]
    layout = cassandra_inventory_layout(nodes, "c")
    assert sorted(layout["snippets"]) == ["group_vars/c/logback.xml.snippet.j2", "host_vars/n1/logback.xml.snippet.j2"]
    assert layout["group_vars"]["c"]["cassandra_config_template_snippets"] == {
        "logback.xml": "{{ inventory_dir }}/group_vars/c/logback.xml.snippet.j2"}
    assert layout["host_vars"]["n1"]["cassandra_config_template_snippets"] == {
        "logback.xml": "{{ inventory_dir }}/host_vars/n1/logback.xml.snippet.j2"}
    assert "n2" not in layout["host_vars"]


def test_a_snippet_on_half_the_nodes_is_not_the_clusters():
    # 2 nodes with the snippet, 2 without: a tie goes to the collection's default (no snippet for the cluster)
    lines = ["<logger name=\"x\" level=\"WARN\"/>"]
    nodes = [node("n%d" % i, "dc1", "r1", **({LOGBACK_END: list(lines)} if i < 3 else {})) for i in range(1, 5)]
    layout = cassandra_inventory_layout(nodes, "c")
    assert sorted(layout["snippets"]) == ["host_vars/n1/logback.xml.snippet.j2", "host_vars/n2/logback.xml.snippet.j2"]
    assert "cassandra_config_template_snippets" not in layout["group_vars"]["c"]


def test_a_snippet_on_a_minority_of_the_nodes_is_written_for_them():
    lines = ["<logger name=\"x\" level=\"WARN\"/>"]
    nodes = [node("n%d" % i, "dc1", "r1", **({LOGBACK_END: list(lines)} if i == 1 else {})) for i in range(1, 4)]
    layout = cassandra_inventory_layout(nodes, "c")
    assert sorted(layout["snippets"]) == ["host_vars/n1/logback.xml.snippet.j2"]
    assert "cassandra_config_template_snippets" not in layout["group_vars"]["c"]


def test_two_snippets_as_common_the_first_node_in_the_hosts_file_gives_the_clusters():
    # 2 nodes with one text, 2 with another, none without: the first node's text (hosts order) for the cluster
    nodes = [node("n%d" % i, "dc1", "r1", **{LOGBACK_END: ["<logger name=\"%s\" level=\"WARN\"/>" % (
        "x" if i in (2, 4) else "y")]}) for i in range(1, 5)]
    layout = cassandra_inventory_layout(nodes, "c")
    assert sorted(layout["snippets"]) == ["group_vars/c/logback.xml.snippet.j2", "host_vars/n2/logback.xml.snippet.j2",
                                          "host_vars/n4/logback.xml.snippet.j2"]
    assert 'name="y"' in layout["snippets"]["group_vars/c/logback.xml.snippet.j2"]


def test_your_value_a_node_keeps_is_written_for_it_when_the_cluster_gets_another():
    """group_vars/all of yours sets 8G, which n1 and n2 have; the 3 others 16G: 16G in the cluster's group_vars
    (which wins over group_vars/all), so n1 and n2 get theirs in their host_vars."""
    yours = [{"path": "group_vars/all/standard.yml", "content": "cassandra_heap_size: 8G\n"}]
    nodes = [node("n%d" % i, "dc1", "r1", cassandra_heap_size="8G" if i < 3 else "16G") for i in range(1, 6)]
    for n in nodes:
        n["address"] = "10.0.0.%s" % n["name"][1:]
    layout = cassandra_inventory_layout_over([dict(n, vars=dict(n["vars"])) for n in nodes], "c", yours)
    assert layout["group_vars"]["c"] == {"cassandra_heap_size": "16G"}
    assert layout["host_vars"] == {"n1": {"cassandra_heap_size": "8G"}, "n2": {"cassandra_heap_size": "8G"}}
    # (their value is yours: written in their host_vars all the same, the cluster's group_vars win over yours)
    assert layout["from_yours"]["n1"]["cassandra_heap_size"]["path"] == "group_vars/all/standard.yml"
    for n in nodes:
        assert effective(layout, n["name"], yours)["cassandra_heap_size"] == n["vars"]["cassandra_heap_size"]
    # most nodes on yours: nothing written for them, theirs applies
    for n in nodes[2:4]:
        n["vars"]["cassandra_heap_size"] = "8G"
    layout = cassandra_inventory_layout_over([dict(n, vars=dict(n["vars"])) for n in nodes], "c", yours)
    assert "cassandra_heap_size" not in layout["group_vars"]["c"]
    assert layout["host_vars"] == {"n5": {"cassandra_heap_size": "16G"}}


def test_import_again_over_the_old_layout_moves_values_and_changes_none(monkeypatch):
    """An inventory the releases before wrote (each value where all nodes agree: r2's and r3's group_vars, n2's
    host_vars), imported again: the cluster's group_vars get the most common values, the rack groups only their
    rack, n2's host_vars go (an earlier import's file this one does not write: removed); every node keeps its
    values."""
    nodes = six_nodes(cassandra_concurrent_reads=32, cassandra_service_environment={"A": "1"})
    old = old_layout(nodes, monkeypatch)
    assert old["group_vars"]["c_dc1_r2"]["cassandra_concurrent_reads"] == 64
    assert old["host_vars"]["n2"]["cassandra_concurrent_reads"] == 64
    new = cassandra_inventory_layout([dict(n, vars=dict(n["vars"])) for n in nodes], "c")
    for n in nodes:
        assert effective(new, n["name"]) == effective(old, n["name"]), n["name"]
    before = dict((f["path"], f["content"]) for f in cassandra_inventory_files(old))
    after = dict((f["path"], f["content"]) for f in cassandra_inventory_files(new))
    assert sorted(set(before) - set(after)) == ["host_vars/n2/main.yml"]
    changed = sorted(p for p in after if before.get(p) != after[p])
    assert changed == ["group_vars/c/main.yml", "group_vars/c_dc1_r2/main.yml", "group_vars/c_dc1_r3/main.yml"]
    # the import's own files: the one it no longer writes is removed, the others written again
    found = dict((p, GENERATED % "c" + "\n" + text) for p, text in before.items())
    found["c.yml"] = GENERATED % "c" + "\n"
    leftovers = cassandra_inventory_leftovers(sorted(found), dict((p, t.split("\n")[0]) for p, t in found.items()),
                                              sorted(after) + ["c.yml"], "c", inventory=new["hosts"])
    assert leftovers["stale"] == ["host_vars/n2/main.yml"] and leftovers["conflicts"] == []


def test_emptying_a_nodes_host_vars_plans_exactly_its_differences(tmp_path):
    """The way to align a node: empty its host_vars. apply_config --check then shows exactly that node's settings
    going to the cluster's values, nothing on the others."""
    import copy
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _render
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_import_cluster_end_lines import (
        FACTS, imported, self_check, stock)
    from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_apply_config import (
        Node, view)
    live = {}
    for name in ("n1", "n2", "n3"):
        live[name] = stock()
        yaml_text = live[name]["cassandra.yaml"]
        assert "\nconcurrent_reads: 32\n" in yaml_text and "\nconcurrent_writes: 32\n" in yaml_text
        if name == "n3":  # the odd one: concurrent_writes 48, concurrent_reads the default
            live[name]["cassandra.yaml"] = yaml_text.replace("\nconcurrent_writes: 32\n", "\nconcurrent_writes: 48\n")
        else:
            live[name]["cassandra.yaml"] = yaml_text.replace("\nconcurrent_reads: 32\n", "\nconcurrent_reads: 64\n")
    nodes = [imported(live[name], name, "10.0.0.%s" % name[1:])[1] for name in ("n1", "n2", "n3")]
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"]["cassandra_concurrent_reads"] == 64
    kept = {"cassandra_service_unit_manage": False, "cassandra_imported_host": True}  # (the unit left as it is)
    assert layout["host_vars"]["n3"] == dict(kept, cassandra_concurrent_reads=32, cassandra_concurrent_writes=48)
    assert all(self_check(live[n], layout, n)["differences"] == [] for n in live)  # import, then no-op
    # n3's host_vars emptied (its settings; the unit left as it is stays)
    aligned = copy.deepcopy(layout)
    aligned["host_vars"]["n3"] = dict(kept)

    def written(inventory, name):  # the files the roles write with the inventory's values for name
        variables = dict(effective(inventory, name), inventory_hostname=name, inventory_hostname_short=name)
        facts = dict(FACTS, hostname=name, default_ipv4={"address": "10.0.0.%s" % name[1:]})
        rendered = _render(variables, facts, live[name])[0]
        return dict((f, rendered[f]) for f in ("cassandra.yaml", "cassandra-env.sh", "jvm-server.options"))
    # (the nodes as apply_config wrote them from the import, against the files of the inventory aligned)
    plan = [Node(tmp_path, name, files=written(layout, name), targets=written(aligned, name)) for name in live]
    rows = [" ".join(line.split()) for line in view(*plan)]
    # n3 alone: its 2 settings from its value (Node) to the cluster's (Inventory); n1, n2: nothing
    assert rows == ["Item Node Inventory", "cassandra.yaml on n3", "concurrent_reads 32 64", "concurrent_writes 48 32"]
