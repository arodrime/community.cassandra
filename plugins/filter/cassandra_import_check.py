# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import_cluster self-check: do the imported variables make the roles
write each node's config as the node has it?

cassandra_import_self_check: the files import_cluster writes (group_vars,
    host_vars), hosts.yml's data, a host, its facts, {file: what the node has}
    -> the files the roles would write for that host (their templates, by
    Ansible's template lookup, with the variables read back from the files'
    text as Ansible merges them, over the roles' defaults), compared with the
    node's as the program that reads each file does (YAML, properties, INI,
    JVM options, shell words, XML elements, unit keys), not as text: the
    settings that differ, values of secrets masked; and the owner, group and
    mode cassandra_config would give its files, against the node's.

Their unexpected errors do not quote the error message, which may show a
value read from the nodes (a password).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import configparser
import difflib
import json
import os
import re
import shlex
import xml.etree.ElementTree as ET

import yaml

from ansible.errors import AnsibleFilterError
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import lookup_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ templates trusted strings only: the inventory files are
    from ansible.template import trust_as_template

    def as_is(value):
        return str(value)
except ImportError:  # before, any string not marked unsafe
    from ansible.utils.unsafe_proxy import wrap_var as as_is

    def trust_as_template(value):
        return value
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import Unsafe, _mask, _values_hidden
from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import (
    cassandra_file_permissions, permission_differences)
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    jvm_options_settings, properties_settings, same_settings, same_yaml_value, shell_lines, xml_settings, yaml_settings)

ROLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
SECRET = re.compile(r"password|passwd|secret|_kspw$|_tspw$|sse_c_key|access_key|private_key|key_material", re.I)
# Set in cassandra-env.sh, else taken from the environment (the unit's): the
# import may move them from one to the other, so they are compared on their own
HEAP_ENV = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE")
ASSIGN = re.compile(r"^(\w+)=(.*)$")
# the files cassandra_config writes, left as they are when their settings are the same
with open(os.path.join(ROLES, "cassandra_config", "vars", "main.yml")) as _vars:
    CONFIG_FILES = set(f for files in yaml.safe_load(_vars)["_cassandra_config_files"].values() for f in files)


@_values_hidden
def cassandra_inventory_host_vars(files, hosts, name):
    """The variables `name` gets from the inventory files, as Ansible merges
    them: parent groups first, then child groups, then the host."""
    data = {}
    for f in files:
        data.setdefault(f["path"].rsplit("/", 1)[0], {}).update(yaml.load(f["content"], Loader=Loader) or {})
    chain = _groups_of(hosts.get("all", {}), name, [])
    if chain is None:
        raise AnsibleFilterError("cassandra_inventory_host_vars: %s is not in the inventory" % name)
    out = {}
    for group in chain:
        out.update(data.get("group_vars/%s" % group, {}))
    out.update(data.get("host_vars/%s" % name, {}))
    return out


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!unsafe", lambda loader, node: Unsafe(loader.construct_scalar(node)))


def _trusted(value):
    """Templates in the values are templated, as when Ansible reads them from a
    file; not in a value written !unsafe."""
    if isinstance(value, Unsafe):
        return as_is(value)
    if isinstance(value, dict):
        return dict((k, _trusted(v)) for k, v in value.items())
    if isinstance(value, list):
        return [_trusted(v) for v in value]
    if isinstance(value, str) and ("{{" in value or "{%" in value or "{#" in value):
        return trust_as_template(value)
    return value


def _groups_of(group, name, chain):
    """The groups from the top down to the one holding host `name`, None if none does."""
    if name in (group.get("hosts") or {}):
        return chain
    for child, sub in sorted((group.get("children") or {}).items()):
        found = _groups_of(sub or {}, name, chain + [child])
        if found is not None:
            return found
    return None


def _shown(key, value):
    if value is None:
        return "nothing"
    if SECRET.search(str(key)):
        return "****"
    return _mask(json.dumps(value, default=str) if not isinstance(value, str) else repr(value))


def _unit(text):
    """(section, key) -> values of a systemd unit; Environment= split by variable."""
    out, section = {}, ""
    for line in re.sub(r"\\\n", " ", text).split("\n"):
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, dummy, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "Environment":
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            for word in words:
                name, dummy, val = word.partition("=")
                out[(section, "Environment " + name)] = [val]
        else:
            out.setdefault((section, key), []).append(value)
    return out


def _ini(text):
    """medusa.ini as Medusa reads it (configparser: keys case-insensitive), true/false any case."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return {"[%s] %s" % (s, k): (v.strip().lower() if v.strip().lower() in ("true", "false") else v.strip())
            for s in parser.sections() for k, v in parser.items(s, raw=True)}


def _jmx_entries(text):
    """jmxremote.password/.access: user -> its words (rights in any order)."""
    text = re.sub(r"\\\n", " ", text)
    words = [line.split() for line in text.split("\n") if line.strip() and not line.lstrip().startswith(("#", "!"))]
    return dict((w[0], " ".join(sorted(w[1:]))) for w in words)


def _dict_diff(name, node, role, secret=False):
    return ["%s: %s: node has %s, import would write %s"
            % (name, key, _shown("password" if secret else key, node.get(key)), _shown("password" if secret else key, role.get(key)))
            for key in sorted(set(node) | set(role), key=str) if node.get(key) != role.get(key)]


def _heap(text, environment):
    """MAX_HEAP_SIZE / HEAP_NEWSIZE as the JVM gets them: cassandra-env.sh's own
    (the last line setting it outside a block: calculate_heap_sizes' own lines
    are indented), else the environment's (the unit's)."""
    out = {}
    lines = [" ".join(shell_lines(line)) for line in text.split("\n") if line[:1] not in (" ", "\t")]
    for name in HEAP_ENV:
        values = [m.group(2) for m in map(ASSIGN.match, lines) if m and m.group(1) == name]
        out[name] = values[-1] if values else (environment or {}).get(name) or None
    return {k: v for k, v in out.items() if v is not None}


def _compare(name, node, role, storage_dir=""):
    if name.endswith(".yaml"):
        a, b = yaml_settings(node, storage_dir), yaml_settings(role, storage_dir)
        same = [k for k in set(a) & set(b) if same_yaml_value(a[k], b[k], k)]
        return _dict_diff(name, dict((k, v) for k, v in a.items() if k not in same),
                          dict((k, v) for k, v in b.items() if k not in same))
    if name.endswith(".properties"):
        return _dict_diff(name, properties_settings(node), properties_settings(role))
    if name.endswith(".options"):
        a, b = jvm_options_settings(node), jvm_options_settings(role)
        return _dict_diff(name, *[dict((k, v[0] if len(v) == 1 and not k.startswith("(") else v) for k, v in d.items())
                                  for d in (a, b)])
    if name.endswith(".ini"):
        return _dict_diff(name, _ini(node), _ini(role))
    if name.startswith("jmxremote."):
        return _dict_diff(name, _jmx_entries(node), _jmx_entries(role), secret=name.endswith(".password"))
    if name.endswith(".service"):
        node_u = dict((k, v) for k, v in _unit(node).items() if k[1] not in ["Environment " + h for h in HEAP_ENV])
        role_u = dict((k, v) for k, v in _unit(role).items() if k[1] not in ["Environment " + h for h in HEAP_ENV])
        return _dict_diff(name, dict(("[%s] %s" % k, " / ".join(v)) for k, v in node_u.items()),
                          dict(("[%s] %s" % k, " / ".join(v)) for k, v in role_u.items()))
    if name.endswith(".xml"):
        a, b = xml_settings(node), xml_settings(role)
    else:  # shell: cassandra-env.sh
        # the heap is compared on its own (see _heap)
        heap = re.compile(r"^(?:%s)=" % "|".join(HEAP_ENV))
        a = shell_lines("\n".join(line for line in node.split("\n") if not heap.match(line)))
        b = shell_lines("\n".join(line for line in role.split("\n") if not heap.match(line)))
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append("%s: node has %s, import would write %s"
                       % (name, " | ".join(_mask(x) for x in a[i1:i2]) or "nothing",
                          " | ".join(_mask(x) for x in b[j1:j2]) or "nothing"))
    return out


def _defaults():
    ctx = {}
    for role in ("cassandra_config", "cassandra_service", "cassandra_medusa"):
        with open(os.path.join(ROLES, role, "defaults", "main.yml")) as f:
            ctx.update(yaml.safe_load(f) or {})
    with open(os.path.join(ROLES, "cassandra_config", "vars", "main.yml")) as f:
        ctx.update(yaml.safe_load(f) or {})
    return ctx


def _render(variables, facts, live, conf_dir=""):
    """{file: text} the roles would write on a node with these variables: the
    cassandra_config files of its series, the JMX users' files, the unit when
    cassandra_service manages it (and the node's could be read), medusa.ini
    when Medusa is on."""
    ctx = _trusted(dict(_defaults(), **variables))
    ctx["ansible_facts"] = facts
    loader = DataLoader()
    templar = Templar(loader=loader, variables=ctx)
    # outside a play (unit tests) only the short name resolves
    lookup = (lookup_loader.get("ansible.builtin.template", loader=loader, templar=templar)
              or lookup_loader.get("template", loader=loader, templar=templar))

    def value(expr):
        return templar.template(trust_as_template("{{ %s }}" % expr))

    series = SERIES.get(str(ctx.get("cassandra_version")))
    if series is None:
        raise AnsibleFilterError("cassandra_import_self_check: unsupported cassandra_version")
    # the files cassandra_config keeps as the node has them (cassandra_config_keep_files) are not written
    keep = value("cassandra_config_keep_files") or []
    wanted = [(f, "cassandra_config/templates/%s/%s.j2" % (series, f)) for f in ctx["_cassandra_config_files"][series]
              if not (f in keep and f in live)]
    if value("cassandra_jmx_users"):
        wanted += [(f, "cassandra_config/templates/%s.j2" % f) for f in ("jmxremote.password", "jmxremote.access")]
    errors = []
    if conf_dir and str(value("cassandra_conf_dir")).rstrip("/") != conf_dir.rstrip("/"):
        errors.append("config dir: node reads %s, the roles would write to %s" % (conf_dir, value("cassandra_conf_dir")))
    unit_managed = str(value("cassandra_service_unit_manage | bool")) == "True"
    if unit_managed:
        if "cassandra.service" in live:
            wanted.append(("cassandra.service", "cassandra_service/templates/cassandra.service.j2"))
        else:
            errors.append("cassandra.service: the roles would replace the unit, which could not be read")
    if str(value("cassandra_medusa_enabled | default(false) | bool")) == "True":
        wanted.append(("medusa.ini", "cassandra_medusa/templates/medusa.ini.j2"))
    rendered = {}
    for name, path in wanted:
        try:
            rendered[name] = lookup.run([os.path.join(ROLES, path)], variables=ctx)[0]
        except Exception as exc:  # pylint: disable=broad-except
            errors.append("%s: the roles could not write it with these variables (%s)" % (name, type(exc).__name__))
    # owner, group and mode cassandra_config gives its files (not the unit's nor medusa.ini's)
    permissions = {}
    try:
        settings = value("_cassandra_config_perm_settings")
        for name in rendered:
            if name not in ("cassandra.service", "medusa.ini"):
                permissions[name] = cassandra_file_permissions(name, settings)
    except AnsibleFilterError as exc:  # cassandra_file_permissions': a file name or a mode, no secret
        errors.append("owner, group and mode: %s" % exc)
    except Exception as exc:  # pylint: disable=broad-except
        errors.append("owner, group and mode: the roles could not work them out with these variables (%s)"
                      % type(exc).__name__)
    # the environment Cassandra would get from the unit: the roles' one, else the node's, kept
    return rendered, errors, value("cassandra_service_environment") if unit_managed else None, permissions


@_values_hidden
def cassandra_import_self_check(files, hosts, name, facts, live, node_environment=None, storage_dir="", java="",
                                conf_dir="", permissions=None):
    """files, hosts, name: the inventory files, hosts.yml's data and the node's
    name there (the variables it gets: cassandra_inventory_host_vars); facts: its ansible_facts;
    live: {file: text the node has}; node_environment: what the node's unit
    gives Cassandra today (MAX_HEAP_SIZE and HEAP_NEWSIZE count wherever they
    are set); storage_dir: the JVM's -Dcassandra.storagedir, where the
    directories cassandra.yaml leaves out are; java: the running Java's major
    version (the jvm<N>-server.options it reads); conf_dir: where the node
    reads its config; permissions: {file: {owner, group, mode, uid, gid}} the
    node's files have (cassandra_permissions_import's files), None: not read.
    -> {'differences', 'notes'}."""
    variables = cassandra_inventory_host_vars(files, hosts, name)
    rendered, errors, role_environment, wanted = _render(variables, facts, live, conf_dir)
    if role_environment is None:  # the unit stays as it is
        role_environment = node_environment
    out = _compare_files(rendered, live, node_environment, role_environment, storage_dir, java)
    if permissions is None:
        out["differences"].append("owner, group and mode: not read on the node, the roles may change them")
    else:
        out["differences"] += permission_differences(wanted, permissions)
    return {"differences": errors + out["differences"], "notes": out["notes"]}


def _compare_files(rendered, live, node_environment=None, role_environment=None, storage_dir="", java=""):
    differences, notes, heap = [], [], []
    if "cassandra-env.sh" in rendered and "cassandra-env.sh" in live:
        node_heap = _heap(live["cassandra-env.sh"], node_environment)
        role_heap = _heap(rendered["cassandra-env.sh"], role_environment)
        heap = _dict_diff("heap (cassandra-env.sh, else the unit's Environment)", node_heap, role_heap)
    for name in sorted(rendered):
        if name not in live:
            other_java = re.match(r"^jvm(\d+)-server\.options$", name)
            if other_java and java and other_java.group(1) != str(java):
                notes.append("%s: not on the node, the roles would create it (Java %s does not read it)" % (name, java))
            else:
                differences.append("%s: not on the node, the roles would create it" % name)
            continue
        try:
            found = _compare(name, live[name], rendered[name], storage_dir)
        except (yaml.YAMLError, ET.ParseError, configparser.Error):
            found = ["%s: cannot be read as its program reads it (node's or the roles' version)" % name]
        # cassandra_config keeps a file only with the same settings by its own test: else it rewrites it
        if not (found or heap and name == "cassandra-env.sh") and name in CONFIG_FILES \
                and not same_settings(name, live[name], rendered[name], storage_dir):
            found = ["%s: the same settings, written in a way cassandra_config does not keep (it would rewrite the file,"
                     " e.g. the heap moved between the unit and cassandra-env.sh, or a line going on to the next)" % name]
        differences += found
    return {"differences": differences + heap, "notes": notes}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_import_self_check": cassandra_import_self_check,
        }
