from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The templates of cassandra_config rendered with the role's defaults and vars,
# outside Ansible (jinja2 with the filters and tests they use), for the tests
# that read what a file says for some variables.

import ast
import os
import re

import jinja2
import yaml

from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.filter.core import regex_replace
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "roles", "cassandra_config")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}


def load_role(series, facts):
    ctx = role_file("cassandra_config")
    role_vars = role_file("cassandra_config", "vars")
    files = role_vars["_cassandra_config_files"][SERIES[series]]
    ctx["cassandra_version"] = series
    ctx["ansible_facts"] = facts
    ctx["_cassandra_config_quote"] = role_vars["_cassandra_config_quote"]
    ctx["_cassandra_config_extra_sections"] = role_vars["_cassandra_config_extra_sections"]
    ctx["_cassandra_config_plain_path"] = role_vars["_cassandra_config_plain_path"]
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True)
    env.filters["regex_replace"] = regex_replace
    # Ansible's, as the templates use them (the JMX users' templates too)
    env.filters["bool"] = lambda value: boolean(value, strict=False)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    env.filters["to_nice_yaml"] = lambda data, indent=2: yaml.safe_dump(data, default_flow_style=False, indent=indent)
    for dummy in range(5):  # defaults referencing other defaults
        for k, v in ctx.items():
            if isinstance(v, str) and "{{" in v:
                ctx[k] = env.from_string(v).render(**ctx)
                if ctx[k].startswith(("[", "{")):  # like Ansible, a rendered list/dict is one again
                    try:
                        ctx[k] = ast.literal_eval(ctx[k])
                    except (ValueError, SyntaxError):
                        pass
    return env, ctx, files


def render_file(env, series, name, ctx):
    with open(os.path.join(ROLE, "templates", SERIES[series], name + ".j2")) as f:
        src = f.read()
    return src.split("\n"), env.from_string(src).render(**ctx).split("\n")
