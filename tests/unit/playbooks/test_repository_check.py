from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_repository: the URL is refused as a repository only on a clear
# answer (404 for all its metadata); the conditions are booleans (2.19+).

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_repository", "tasks", "repository.yml")

with open(TASKS, encoding="utf-8") as f:
    BY_NAME = {t["name"]: t for t in yaml.safe_load(f)}


def condition(expr, codes):
    return Templar(loader=DataLoader(), variables={"_codes": codes}).template(trust_as_template("{{ %s }}" % expr))


@pytest.mark.parametrize("codes, accepted, warned", [
    ([200], True, False),
    ([404, 200], True, False),  # deb: Release only
    ([404], False, False),  # a plain directory
    ([404, 404], False, False),
    ([-1], True, True),  # no answer: a proxy only dnf/apt know
    ([403], True, True),
])
def test_refused_only_on_a_clear_404(codes, accepted, warned):
    assert condition(BY_NAME["The URL must be a repository"]["ansible.builtin.assert"]["that"], codes) is accepted
    assert condition(BY_NAME["Say the repository could not be checked"]["when"][1], codes) is warned
