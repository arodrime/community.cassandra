from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's read scripts (playbooks/templates/*.sh.j2) print key=value lines that the playbook reads back as a
# dict (_kv.<key>, _found.<key>): every key the playbook reads is one the script prints, so a key renamed or dropped
# on one side only fails here, not as an empty value on a node.

import os
import re

import pytest

from ansible_collections.community.cassandra.tests.unit.helpers import TOP

# a key printed: echo "key=...", printf 'key=%s', or each key=% of a printf format (stat --printf 'a=%U\nb=%G')
_PRINTED = re.compile(r"""\b(?:echo|printf)\s+["']?([A-Za-z_$][\w${}]*)=|(?:printf\s+'|\\n)([A-Za-z_]\w*)=%""")
_VARIABLE = re.compile(r"\$\{?(\w+)\}?")


def _read(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return f.read()


def _values(script, variable, at):
    """The values a shell variable takes where script prints it (at): the words of the last for loop over it before
    that, else its literal assignments; None when they are not literal (a variable's words, a number)."""
    loops = list(re.finditer(r"\bfor %s in ([^;\n]*); do" % variable, script[:at]))
    if loops:
        words = loops[-1].group(1)
        return None if "$" in words else words.split()
    return re.findall(r"(?<![\w$])%s=([A-Za-z_]\w*)(?=[\s;)]|$)" % variable, script) or None


def printed(script):
    """The keys script prints, a key with a variable in it as a regex of each value that variable takes."""
    keys = set()
    for match in _PRINTED.finditer(script):
        key = match.group(1) or match.group(2)
        pattern = re.escape(key)
        for variable in set(_VARIABLE.findall(key)):
            values = _values(script, variable, match.start())
            alternatives = "|".join(re.escape(v) for v in values) if values else r"\w+"
            pattern = re.sub(r"\\\$(?:\\\{)?%s(?:\\\})?" % variable, lambda m: "(?:%s)" % alternatives, pattern)
        keys.add(pattern)
    return keys


def read(playbook, name):
    """The keys playbook reads from the dict name: name.<key>, name['<key>']."""
    return set(re.findall(r"\b%s(?:\.|\[')(\w+)" % name, playbook))


@pytest.mark.parametrize("template, name", [("import_running.sh.j2", "_kv"), ("import_medusa.sh.j2", "_found")])
def test_every_key_the_playbook_reads_is_printed(template, name):
    keys = printed(_read("playbooks", "templates", template))
    wanted = read(_read("playbooks", "import_cluster.yml"), name)
    assert wanted, name
    assert sorted(k for k in wanted if not any(re.fullmatch(p, k) for p in keys)) == []


def test_a_key_with_a_variable_is_each_value_of_it():
    script = 'for p in $(pgrep -x java); do :; done\nfor p in Restart Environment; do\n  echo "unit_$p=x"\ndone\n' \
             'case $f in *.sh) k=a ;; *) k=b ;; esac\n' \
             'echo "${k}_sum=1"\nfor n in $LIST; do echo "x_$n=$n"; done\nstat --printf \'o=%U\\ng=%G\\n\' f\n'
    keys = printed(script)
    assert [k for k in ["unit_Restart", "unit_Environment", "a_sum", "b_sum", "x_any", "o", "g"]
            if not any(re.fullmatch(p, k) for p in keys)] == []
    assert not any(re.fullmatch(p, k) for p in keys for k in ["unit_Other", "c_sum"])
