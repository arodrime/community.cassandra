from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Debian: the apt cache is read again after the repository is written when its credentials or its signing key
# changed alone too (a release check of the same run read a cache refreshed before them, at most hourly,
# and said the release was missing from the repository).

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render)

TASKS = load("roles", "cassandra_repository", "tasks", "apt_source.yml")  # (repository.yml includes it, Debian)


def _updated(**changed):
    update = find(TASKS, "Update the apt cache (Debian & Ubuntu)")
    variables = {"ansible_facts": {"os_family": "Debian"}, "_cassandra_repository_apt": {"changed": False},
                 "_cassandra_repository_old": {"changed": False}}
    variables.update((k, {"changed": v}) for k, v in changed.items())
    return all(render("{{ %s }}" % w, variables) for w in update["when"])


def test_the_apt_cache_is_updated_once_the_repository_changed():
    assert _updated() is False
    assert _updated(_cassandra_repository_apt=True) is True
    assert _updated(_cassandra_repository_old=True) is True
    for name in ("_cassandra_repository_credentials", "_cassandra_repository_credentials_removed",
                 "_cassandra_repository_key_shipped", "_cassandra_repository_key_downloaded"):
        assert _updated(**{name: True}) is True, name
        assert _updated(**{name: False}) is False, name
    # (the order: after the source, the credentials and the keys are written)
    names = [t.get("name") for t in TASKS]
    assert names.index("Update the apt cache (Debian & Ubuntu)") > names.index("Ensure Cassandra apt repository exists (Debian & Ubuntu)")
    names = [t.get("name") for t in load("roles", "cassandra_repository", "tasks", "repository.yml")]
    assert names.index("The apt repository (Debian & Ubuntu)") > max(
        names.index("Credentials and packages of the apt repository (Debian & Ubuntu)"),
        names.index("Install the Cassandra signing keys shipped with the role"),
        names.index("Download the Cassandra signing keys and check their fingerprints"))
