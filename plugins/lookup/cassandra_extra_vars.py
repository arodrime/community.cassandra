# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_extra_vars
short_description: The names of the extra variables (-e) of the run
version_added: 2.1.0
description:
  - Returns the names of the variables given with C(-e)/C(--extra-vars) on the command line (C(key=value),
    YAML/JSON and C(@file) alike), sorted. The variables of the inventory and of the playbooks are not in it.
  - Read from what C(ansible-playbook) parsed at start (not read again, unless those C(-e) hold no variable at
    all); empty when the run has no command line (the Python API).
  - Use C(query) (or C(wantlist=true)) for a list.
author: Alain Rodriguez (@arodrime)
"""

EXAMPLES = r"""
- name: Show the -e variables
  ansible.builtin.debug:
    msg: "{{ query('community.cassandra.cassandra_extra_vars') }}"
"""

RETURN = r"""
_raw:
  description: The variable names.
  type: list
  elements: str
"""

from ansible.plugins.lookup import LookupBase
from ansible.utils.vars import load_extra_vars


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        # memoized by ansible-core when the VariableManager was built: the same dict, no file read again
        # (without -e, nothing to memoize: an empty command line parsed again)
        return sorted(str(name) for name in load_extra_vars(self._loader))
