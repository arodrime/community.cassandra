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
    settings that differ, values of secrets masked.

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

ROLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
SECRET = re.compile(r"password|passwd|secret|_kspw$|_tspw$|sse_c_key|access_key|private_key|key_material", re.I)
# Set in cassandra-env.sh, else taken from the environment (the unit's): the
# import may move them from one to the other, so they are compared on their own
HEAP_ENV = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE")
ASSIGN = re.compile(r"^(\w+)=(.*)$")


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


NULLS = ("", "~", "null", "Null", "NULL")
BOOLS = {"true": "true", "yes": "true", "on": "true", "false": "false", "no": "false", "off": "false"}


def _plain(node):
    """A YAML node as Cassandra's SnakeYAML reads it into its typed settings:
    each scalar its text, quoted or not ('8' and 8); a plain (unquoted) one
    true/false in any YAML 1.1 way, or null; a quoted one text ("yes" is not
    true); not a number PyYAML would make of it (a password 0123 is not 83).
    A key set twice: the last one."""
    if isinstance(node, yaml.MappingNode):
        return dict((_plain(k), _plain(v)) for k, v in node.value)
    if isinstance(node, yaml.SequenceNode):
        return [_plain(v) for v in node.value]
    if node.style is None:
        if node.value in NULLS:
            return None
        return Plain(node.value) if node.value.lower() in BOOLS else node.value
    return node.value


class Plain(str):
    """An unquoted yes/no/on/off/true/false: a boolean for a boolean setting,
    text for a text one (a password Yes is not true)."""


def _same_yaml(a, b):
    """The same setting: the same text, or true/false written two ways where
    one side is the role's own true/false (the role writes a boolean setting so)."""
    if a == b:
        return True
    return (isinstance(a, Plain) and isinstance(b, Plain) and BOOLS[a.lower()] == BOOLS[b.lower()]
            and (a in ("true", "false") or b in ("true", "false")))


def _flatten(value, path, out):
    if isinstance(value, dict):
        if not value:
            out[path] = "{}"
        for k, v in value.items():
            _flatten(v, "%s.%s" % (path, k) if path else str(k), out)
    elif isinstance(value, list):
        if not value:
            out[path] = "[]"
        for i, v in enumerate(value):
            _flatten(v, "%s[%d]" % (path, i), out)
    elif value is not None:  # key: (null) is the same as no key: the default
        out[path] = value
    return out


# cassandra.yaml directories left out: under -Dcassandra.storagedir (bin/cassandra, cassandra.in.sh)
STORAGE_DIRS = {"data_file_directories": "data", "commitlog_directory": "commitlog",
                "saved_caches_directory": "saved_caches", "hints_directory": "hints", "cdc_raw_directory": "cdc_raw"}


def _yaml(text, storage_dir=""):
    node = yaml.compose(text, Loader=yaml.BaseLoader)
    data = _plain(node) if node is not None else {}
    if not isinstance(data, dict):
        raise yaml.YAMLError("not a mapping")
    for key, sub in STORAGE_DIRS.items():
        if storage_dir and data.get(key) is None:
            path = "%s/%s" % (storage_dir.rstrip("/"), sub)
            data[key] = [path] if key == "data_file_directories" else path
    out = dict((k, v) for k, v in _flatten(data, "", {}).items() if k)
    for key, value in out.items():
        if re.match(r"^seed_provider\[\d+\]\.parameters\[\d+\]\.seeds$", key) and isinstance(value, str):
            # SimpleSeedProvider: split on commas, each address trimmed, empty ones skipped
            out[key] = ",".join(s.strip() for s in value.split(",") if s.strip())
    return out


def _properties(text):
    """cassandra-rackdc.properties as Java's Properties reads it (key=value,
    key:value or key value; # and ! comment lines; a backslash goes on to the
    next line; the value keeps its trailing spaces) and the snitch uses it: dc
    and rack trimmed, prefer_local true in any case (false: as when not set)."""
    out = {}
    text = re.sub(r"\r\n?", "\n", text)  # Java ends a line at \r\n, \r or \n
    text = re.sub(r"(?<!\\)\\\n[ \t]*", "", text)
    for line in text.split("\n"):
        line = line.lstrip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"^((?:[^\\=: \t]|\\.)*)[ \t]*[=: \t]?[ \t]*(.*)$", line)
        out[m.group(1)] = m.group(2)
    for key in ("dc", "rack"):
        if key in out:
            out[key] = out[key].strip()
    if "prefer_local" in out:
        out["prefer_local"] = "true" if out["prefer_local"].lower() == "true" else "false"
        if out["prefer_local"] == "false":
            del out["prefer_local"]
    return out


def _jvm_options(text):
    """The options bin/cassandra passes on: the lines starting with '-', each
    word split (a word after it is an option too). The same option given twice:
    the last one counts."""
    out = {}
    for line in text.split("\n"):
        if not line.startswith("-"):
            continue
        words = line.split()
        key = words[0]
        m = re.match(r"^-XX:[+-]?(\w+)", key) or re.match(r"^(-D[^=]+)", key) or re.match(r"^(-X(?:mx|ms|mn|ss))", key)
        if m:
            out[m.group(1)] = " ".join(words)
        else:
            out[" ".join(words)] = "set"
    return out


def _shell_words(line):
    """The words of a shell line as the shell sees them: split on unquoted
    blanks, a comment only at the start of a word; each written back so that
    two words mean the same when equal: quotes around plain text dropped
    ("7199" is 7199), kept around what they change ('$X' is not "$X")."""
    words, word, i, quote, start = [], None, 0, None, 0
    while i < len(line):
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word += ("\\" + c) if not (c.isalnum() or c in "_-./:=,+@%") else c
        elif quote == '"':
            if c == "\\":
                i += 1  # an escaped character: part of the quoted text
            elif c == '"':
                quote = None
                seg = line[start:i]
                word += ('"%s"' % seg) if re.search(r"[$`\\]", seg) else "".join(
                    ("\\" + ch) if not (ch.isalnum() or ch in "_-./:=,+@%") else ch for ch in seg)
        elif c in " \t":
            if word is not None:
                words.append(word)
                word = None
        elif c == "#" and word is None:
            break
        elif c in "'\"":
            word = word or ""
            quote = c
            start = i + 1
        elif c == "\\" and i + 1 < len(line):
            word = (word or "") + line[i:i + 2]
            i += 1
        else:
            word = (word or "") + c
        i += 1
    if quote:
        raise ValueError("unclosed quote")
    if word is not None:
        words.append(word)
    return words


def _shell_lines(text):
    """The lines a shell runs, comments and blank lines out, as their words."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = _shell_words(line)
        except ValueError:  # a quote goes on to the next line: as it is
            words = line.split()
        if words:
            out.append(" ".join(words))
    return out


def _xml(text):
    """Every element, with its attributes and text, comments out."""
    out = []

    def walk(el, path):
        # logback reads a level in any case
        attrs = " ".join('%s="%s"' % (k, v.upper() if k == "level" else v) for k, v in sorted(el.attrib.items()))
        here = "%s/%s%s" % (path, el.tag, ("[%s]" % attrs) if attrs else "")
        out.append(here + ((" = " + el.text.strip()) if (el.text or "").strip() else ""))
        for child in el:
            walk(child, here)
    walk(ET.fromstring(text), "")
    return out


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
    lines = [" ".join(_shell_lines(line)) for line in text.split("\n") if line[:1] not in (" ", "\t")]
    for name in HEAP_ENV:
        values = [m.group(2) for m in map(ASSIGN.match, lines) if m and m.group(1) == name]
        out[name] = values[-1] if values else (environment or {}).get(name) or None
    return {k: v for k, v in out.items() if v is not None}


def _compare(name, node, role, storage_dir=""):
    if name.endswith(".yaml"):
        a, b = _yaml(node, storage_dir), _yaml(role, storage_dir)
        same = [k for k in set(a) & set(b) if _same_yaml(a[k], b[k])]
        return _dict_diff(name, dict((k, v) for k, v in a.items() if k not in same),
                          dict((k, v) for k, v in b.items() if k not in same))
    if name.endswith(".properties"):
        return _dict_diff(name, _properties(node), _properties(role))
    if name.endswith(".options"):
        return _dict_diff(name, _jvm_options(node), _jvm_options(role))
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
        a, b = _xml(node), _xml(role)
    else:  # shell: cassandra-env.sh
        # the heap is compared on its own (see _heap)
        heap = re.compile(r"^(?:%s)=" % "|".join(HEAP_ENV))
        a = _shell_lines("\n".join(line for line in node.split("\n") if not heap.match(line)))
        b = _shell_lines("\n".join(line for line in role.split("\n") if not heap.match(line)))
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
    wanted = [(f, "cassandra_config/templates/%s/%s.j2" % (series, f)) for f in ctx["_cassandra_config_files"][series]]
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
    # the environment Cassandra would get from the unit: the roles' one, else the node's, kept
    return rendered, errors, value("cassandra_service_environment") if unit_managed else None


@_values_hidden
def cassandra_import_self_check(files, hosts, name, facts, live, node_environment=None, storage_dir="", java="",
                                conf_dir=""):
    """files, hosts, name: the inventory files, hosts.yml's data and the node's
    name there (the variables it gets: cassandra_inventory_host_vars); facts: its ansible_facts;
    live: {file: text the node has}; node_environment: what the node's unit
    gives Cassandra today (MAX_HEAP_SIZE and HEAP_NEWSIZE count wherever they
    are set); storage_dir: the JVM's -Dcassandra.storagedir, where the
    directories cassandra.yaml leaves out are; java: the running Java's major
    version (the jvm<N>-server.options it reads); conf_dir: where the node
    reads its config. -> {'differences', 'notes'}."""
    variables = cassandra_inventory_host_vars(files, hosts, name)
    rendered, errors, role_environment = _render(variables, facts, live, conf_dir)
    if role_environment is None:  # the unit stays as it is
        role_environment = node_environment
    out = _compare_files(rendered, live, node_environment, role_environment, storage_dir, java)
    return {"differences": errors + out["differences"], "notes": out["notes"]}


def _compare_files(rendered, live, node_environment=None, role_environment=None, storage_dir="", java=""):
    differences, notes = [], []
    for name in sorted(rendered):
        if name not in live:
            other_java = re.match(r"^jvm(\d+)-server\.options$", name)
            if other_java and java and other_java.group(1) != str(java):
                notes.append("%s: not on the node, the roles would create it (Java %s does not read it)" % (name, java))
            else:
                differences.append("%s: not on the node, the roles would create it" % name)
            continue
        try:
            differences += _compare(name, live[name], rendered[name], storage_dir)
        except (yaml.YAMLError, ET.ParseError, configparser.Error):
            differences.append("%s: cannot be read as its program reads it (node's or the roles' version)" % name)
    if "cassandra-env.sh" in rendered and "cassandra-env.sh" in live:
        node_heap = _heap(live["cassandra-env.sh"], node_environment)
        role_heap = _heap(rendered["cassandra-env.sh"], role_environment)
        differences += _dict_diff("heap (cassandra-env.sh, else the unit's Environment)", node_heap, role_heap)
    return {"differences": differences, "notes": notes}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_import_self_check": cassandra_import_self_check,
        }
