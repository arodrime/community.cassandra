from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A step that failed on several nodes (add_node's prepare): one line per
# cause, the nodes that share it, a repository problem named as such, then
# what to check and the command to run again.

from ansible_collections.community.cassandra.plugins.filter.cassandra_failure import (
    cassandra_failure_cause, cassandra_failure_repos, cassandra_failure_report)

# what the dnf module says when a repository's mirror can't be reached (EL 8)
EPEL = {"msg": "Failed to download metadata for repo 'epel': Cannot download repomd.xml: Cannot download "
               "repodata/repomd.xml: All mirrors were tried", "failures": []}
APPSTREAM = {"msg": "Failed to download metadata for repo 'rhel-8-for-x86_64-appstream-rpms': Cannot download "
                    "repomd.xml: Curl error (28): Timeout was reached for https://deploy:s3cret@mirror.example/rhel8/"
                    "repodata/repomd.xml [Connection timed out after 30000 milliseconds]"}
APT = {"msg": "Failed to update apt cache: W:Failed to fetch http://mirror.example/ubuntu/dists/noble/InRelease  "
              "Could not resolve 'mirror.example', E:Some index files failed to download."}
RERUN = "ansible-playbook -i inventories community.cassandra.add_node -e cassandra_target_nodes=node5,node6"


def test_a_repository_problem_is_named():
    assert cassandra_failure_repos(EPEL) == ["epel"]
    assert cassandra_failure_repos(APT) == ["http://mirror.example/ubuntu/dists/noble/InRelease"]
    assert cassandra_failure_repos({"msg": "No package matching 'python3.11' found available"}) == []
    assert cassandra_failure_cause(EPEL, "Install python3.11") == (
        "Install python3.11: repository 'epel' unreachable (a repository or mirror problem on the node)")


def test_other_causes_short_and_masked():
    long = {"msg": "x" * 300}
    assert cassandra_failure_cause(long, "Task") == "Task: " + "x" * 91 + "..."
    assert len(cassandra_failure_cause(long, "Task")) == 100
    assert cassandra_failure_cause({"msg": "", "results": [{"failed": True, "msg": "item broke"}]}) == "item broke"
    assert "s3cret" not in cassandra_failure_cause({"msg": "mirror https://deploy:s3cret@mirror.example/x down"})


def test_the_same_cause_once_with_its_nodes_then_what_to_do():
    failures = [{"host": "node5", "task": "Install python3.11", "result": EPEL, "family": "RedHat"},
                {"host": "node6", "task": "Install python3.11", "result": EPEL, "family": "RedHat"},
                {"host": "node7", "task": "Install python3.11", "result": APPSTREAM, "family": "RedHat"}]
    lines = cassandra_failure_report(failures, operation="add_node", cluster="my_cluster", rerun=RERUN, python311=True)
    assert lines == [
        "FAILED  add_node  my_cluster  node5..node7 could not be prepared: nothing was started",
        "  node5, node6  Install python3.11: repository 'epel' unreachable (a repository or mirror problem on the node)",
        "  node7  Install python3.11: repository 'rhel-8-for-x86_64-appstream-rpms' unreachable (a repository or mirror"
        " problem on the node)",
        "",
        "TO DO",
        "  1. check the repositories of node5..node7 (a mirror down or unreachable from them):",
        '     ansible -i inventories node5,node6,node7 -b -m ansible.builtin.command -a "dnf -q makecache"',
        "  2. fix the repository or mirror on the node (or its proxy)",
        "  3. or, when cqlsh gets its Python another way, set cassandra_cqlsh_python_manage: false in the inventory:"
        " python3.11 only runs cqlsh on the new nodes (the reset check reads the keyspaces with cqlsh on a node of the"
        " cluster)",
        "  4. run it again once fixed (nothing was started):",
        "     " + RERUN]
    assert all("s3cret" not in line for line in lines)


def test_other_failures_only_the_run_again():
    lines = cassandra_failure_report([{"host": "node5", "task": "Write cassandra.yaml", "result": {"msg": "disk full"}}],
                                     operation="add_node", cluster="c", rerun=RERUN)
    assert lines == ["FAILED  add_node  c  node5 could not be prepared: nothing was started",
                     "  node5  Write cassandra.yaml: disk full", "", "TO DO",
                     "  1. run it again once fixed (nothing was started):", "     " + RERUN]
    # Debian: apt's own check; both families: a command each
    lines = cassandra_failure_report([{"host": "node5", "task": "Install", "result": APT, "family": "Debian"},
                                      {"host": "node6", "task": "Install", "result": EPEL, "family": "RedHat"}])
    assert '     ansible node5 -b -m ansible.builtin.command -a "apt-get update"' in lines
    assert '     ansible node6 -b -m ansible.builtin.command -a "dnf -q makecache"' in lines
    # no node with a failure of its own: still a verdict
    assert cassandra_failure_report([], operation="add_node", cluster="c", rerun=RERUN)[0] == (
        "FAILED  add_node  c  the new nodes could not be prepared: nothing was started")
