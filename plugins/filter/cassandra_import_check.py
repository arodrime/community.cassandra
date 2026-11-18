# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import_cluster self-check: do the imported variables make the roles
write each node's config as the node has it?

cassandra_import_self_check (the function, for one node; the filter is the next one): the files import_cluster
    writes (group_vars,
    host_vars), the hosts file's data, a host, its facts, {file: what the node has}
    -> the files the roles would write for that host (their templates, by
    Ansible's template lookup, with the variables read back from the files'
    text as Ansible merges them, over the roles' defaults), compared with the
    node's as the program that reads each file does (YAML, properties, INI,
    JVM options, shell words, XML elements, unit keys), not as text: the
    settings that differ, values of secrets masked; and the owner, group and
    mode cassandra_config would give its files, against the node's.
cassandra_import_self_check_nodes (filter): the same for each node read, the
    inventory files read once for all of them -> {node: {differences, notes}}.

Their unexpected errors do not quote the error message, which may show a
value read from the nodes (a password).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import configparser
import difflib
import functools
import hashlib
import json
import os
import re
import shlex
import xml.etree.ElementTree as ET

import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.filter.core import b64decode
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
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    REMOTE_BRANCH_NOTE, REPEATED_NOTE, Unsafe, error_without_message, log_dir_of, logback_as_log_dir, undefined_name,
    values_hidden)
from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_os import (
    NO_EFFECT, NO_EFFECT_NOTE, cassandra_firewall_changes, cassandra_linux_changes)
from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import (
    cassandra_file_permissions, permission_differences)
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    compared, jvm_options_settings, properties_settings, same_settings, same_yaml_value, shell_lines,
    without_later_stock, xml_differences, xml_settings, yaml_settings, yaml_rejected)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as _out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_differences import HEAP_FILE
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_prometheus as prometheus
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import ROLES, role_file

SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# Set in cassandra-env.sh, else taken from the environment (the unit's): the
# import may move them from one to the other, so they are compared on their own
HEAP_ENV = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE")
ASSIGN = re.compile(r"^(\w+)=(.*)$")
# the files cassandra_config writes, left as they are when their settings are the same
CONFIG_FILES = set(f for files in role_file("cassandra_config", "vars")["_cassandra_config_files"].values() for f in files)


@values_hidden
def cassandra_inventory_host_vars(files, hosts, name):
    """The variables `name` gets from the inventory files (the import's, and the
    user's own: group_vars/all...), as Ansible merges them: all, parent groups
    first, then child groups, then the host; in a dir, its files by name."""
    return _host_vars(_read_files(files), hosts, name)


# the templates next to the variables (the import's snippets and templates, .j2: not variables files), by path
TEMPLATES = "(templates)"
# the role's variable each snippet's text is in (cassandra_config's vars)
SNIPPET_VARS = (("logback.xml", "_cassandra_config_snippet_logback"),
                ("logback.xml:appenders", "_cassandra_config_snippet_appenders"),
                ("cassandra-env.sh", "_cassandra_config_snippet_env"))
# the import's path of a template: {{ inventory_dir }}/<path in the inventory>
IN_INVENTORY = re.compile(r"^\{\{\s*inventory_dir\s*\}\}/(.+)$")


def _read_files(files):
    """The inventory files -> {group_vars/<group> or host_vars/<host>: its variables, TEMPLATES: {path: text} of
    the templates among them}."""
    by_where = {}
    templates = {}
    for f in files:
        if f["path"].endswith(".j2"):  # (Ansible reads no .j2 there as variables)
            # a copy as a plain str: before 2.19 the import hands its files over marked unsafe (never templated on
            # the way), which a Templar would leave as it is
            templates[f["path"]] = "".join([f["content"]])
            continue
        parts = f["path"].split("/")
        by_where.setdefault(parts[0] + "/" + (parts[1].rsplit(".", 1)[0] if len(parts) == 2 else parts[1]), []).append(f)
    data = {}
    for where, found in by_where.items():
        # its dir if there is one (<name>.yml next to it is not read), else the first of .yml, .yaml, .json
        in_dir = [f for f in found if f["path"].count("/") == 2]
        found = in_dir or [f for ext in ("yml", "yaml", "json") for f in found if f["path"] == "%s.%s" % (where, ext)][:1]
        for f in sorted(found, key=lambda f: f["path"]):
            data.setdefault(where, {}).update(yaml.load(f["content"], Loader=Loader) or {})
    data[TEMPLATES] = templates
    return data


def _host_vars(data, hosts, name):
    chain = _groups_of(hosts.get("all", {}), name, [])
    if chain is None:
        raise AnsibleFilterError("cassandra_inventory_host_vars: %s is not in the inventory" % name)
    out = {}
    for group in ["all"] + chain:
        out.update(data.get("group_vars/%s" % group, {}))
    out.update(data.get("host_vars/%s" % name, {}))
    return out


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!unsafe", lambda loader, node: Unsafe(loader.construct_scalar(node)))
Loader.add_constructor("!vault", lambda loader, node: None)  # a user's inline vaulted value: not read


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
    if _out.secret(str(key), value):  # the one rule (cassandra_output): what secrets.yml holds, hidden on screen
        return "****"
    return _out.mask(json.dumps(value, default=str) if not isinstance(value, str) else repr(value))


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


def _top(path):
    """A cassandra.yaml setting's top-level key (seed_provider[0].class_name: seed_provider)."""
    return re.split(r"[.\[]", path, maxsplit=1)[0]


def _compare(name, node, role, storage_dir="", left_out=()):
    """The settings that differ; left_out: cassandra.yaml keys left out of the node's (its Cassandra rejects them)."""
    if name.endswith(".yaml"):
        a, b = compared(yaml_settings(node, storage_dir), role), compared(yaml_settings(role, storage_dir), role)
        a = dict((k, v) for k, v in a.items() if _top(k) not in left_out)
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
        stray = []
        found = xml_differences(node, role, stray)
        if found or stray:  # (else the same elements in another order: the lines below)
            return [_stray_text(name, path, text) for path, text in stray] + [
                "%s: %s: node has %s, import would write %s" % (name, path, _xml_shown(path, a), _xml_shown(path, b))
                for path, a, b in found]
        a, b = xml_settings(node), xml_settings(role)
    else:  # shell: cassandra-env.sh
        # the heap is compared on its own (see _heap)
        heap = re.compile(r"^(?:%s)=" % "|".join(HEAP_ENV))
        a = shell_lines("\n".join(line for line in node.split("\n") if not heap.match(line)))
        b = shell_lines("\n".join(line for line in role.split("\n") if not heap.match(line)))
    out = []
    # masked as in the whole file: a line of a secret's value (a quote left open) without its key in the block
    shown_a, shown_b = _out.mask_lines(a), _out.mask_lines(b)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if name.endswith(".xml"):
            out.append("%s: node has %s, import would write %s"
                       % (name, " | ".join(shown_a[i1:i2]) or "nothing", " | ".join(shown_b[j1:j2]) or "nothing"))
            continue
        for x, y in _env_pairs(a[i1:i2], b[j1:j2]):  # a line each: its setting's, the other side's
            out.append("%s: node has %s, import would write %s" % (
                name, "nothing" if x is None else shown_a[i1 + x], "nothing" if y is None else shown_b[j1 + y]))
    return out


def _stray_text(name, path, text):
    """The node's text in an element that holds elements, where it means nothing (the role's has none): said in
    words, with the way out (the operator removes it on the nodes)."""
    appender = re.search(r'/appender\[@name="([^"]*)"\]', path)
    element = re.sub(r"\[.*$", "", path.rsplit("/", 1)[-1])
    where = " of appender %s" % appender.group(1) if appender else ""
    return ("%s: stray text %s inside <%s>%s, an element that takes no text (such as the end of a comment left when"
            " uncommenting a block): remove it on the nodes, then import again (or keep %s as it is: -e"
            " import_keep_hand_edits=%s)" % (name, _out.mask(repr(str(text))), element, where, name, name))


def _xml_shown(path, value):
    """An XML difference's value as the self-check says it: a text or an attribute as a value (a secret by its
    element's or attribute's name), an element alone on one side as its tag and attributes (its text too, when it has
    no element inside), none as nothing."""
    if value is None:
        return "nothing"
    if isinstance(value, str):
        return _shown(re.split(r"[/@]", path)[-1], value)
    text = (value.text or "").strip() if not len(value) else ""
    return _out.mask("<%s%s>%s" % (value.tag, "".join(' %s="%s"' % kv for kv in sorted(value.attrib.items())),
                                   "%s</%s>" % (text, value.tag) if text else ""))


def _env_setting(line):
    """What a line of cassandra-env.sh sets: a variable (MAX_HEAP_SIZE), the option a JVM_OPTS line adds (-Xss,
    -Dx, UseG1GC), else None."""
    m = re.match(r"^(?:export\s+)?(\w+)=(.*)$", line)
    if not m:
        return None
    added = re.match(r'^"\$JVM_OPTS (\S+)"$', m.group(2)) if m.group(1) == "JVM_OPTS" else None
    return (jvm.option_name(added.group(1)) or added.group(1)) if added else m.group(1)


def _env_pairs(a, b):
    """The lines of cassandra-env.sh that differ (a: the node's, b: the role's) -> [(index in a, index in b)], a line
    with the line of the other side that sets the same (else None), the node's order, then the role's other lines."""
    pairs, taken = [], set()
    for i, line in enumerate(a):
        setting = _env_setting(line)
        j = next((j for j, other in enumerate(b) if j not in taken and setting and _env_setting(other) == setting), None)
        if j is not None:
            taken.add(j)
        pairs.append((i, j))
    return pairs + [(None, j) for j in range(len(b)) if j not in taken]


@functools.lru_cache(maxsize=None)
def _defaults():
    ctx = {}
    for role in ("cassandra_config", "cassandra_service", "cassandra_operation", "cassandra_medusa"):
        ctx.update(role_file(role) or {})
    for role in ("cassandra_config", "cassandra_service", "cassandra_operation"):  # their private variables
        ctx.update(role_file(role, "vars") or {})
    return ctx


def _render(variables, facts, live, conf_dir="", templates=None, script=None):
    """{file: text} the roles would write on a node with these variables: the
    cassandra_config files of its series, the JMX users' files, the unit when
    cassandra_service manages it (and the node's could be read), medusa.ini
    when Medusa is on. templates: {path in the inventory: text} of the ones
    about to be written there (read from it, not from the disk). script: (path, its package or modified or '') of the
    init script the node starts Cassandra with, None when a unit."""
    ctx = _trusted(dict(_defaults(), **variables))
    ctx["ansible_facts"] = facts
    # the roles' templates rendered by Ansible itself (its template lookup, its filters and tests), with these
    # variables and not the play's: a Templar of its own (public: ansible.template; 2.19+ trusts only the text
    # marked so, trust_as_template), a DataLoader for the lookup's file reads
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
    # your own templates (cassandra_config_template_overrides: absolute paths), else the role's; what
    # cassandra_config refuses, a difference
    errors = []
    try:
        overrides = value("cassandra_config_template_overrides") or {}
    except Exception as exc:  # pylint: disable=broad-except  # any template error, said by its type (no value)
        overrides = {}
        errors.append("cassandra_config_template_overrides: cannot be read with these variables (%s)" % type(exc).__name__)
    if not isinstance(overrides, dict):
        errors.append("cassandra_config_template_overrides: not a dict, cassandra_config would refuse it")
        overrides = {}
    for f, path in sorted(overrides.items()):
        if f not in ctx["_cassandra_config_files"][series]:
            errors.append("cassandra_config_template_overrides: %s is not a file cassandra_config writes" % f)
        elif not isinstance(path, str) or not path.startswith("/") or not os.path.isfile(path):
            errors.append("cassandra_config_template_overrides: %s: not an absolute path to a file on the controller,"
                          " cassandra_config would refuse it" % f)
        elif f in keep:
            errors.append("cassandra_config_template_overrides: %s is also in cassandra_config_keep_files,"
                          " cassandra_config would refuse it" % f)
    errors += _snippets(variables, ctx, value, templar, keep, overrides, templates or {})
    wanted = [(f, overrides[f] if isinstance(overrides.get(f), str) and overrides[f].startswith("/")
               else "cassandra_config/templates/%s/%s.j2" % (series, f)) for f in ctx["_cassandra_config_files"][series]
              if not (f in keep and f in live)]
    jmx = ("jmxremote.password", "jmxremote.access") if value("cassandra_jmx_users") else ()
    wanted += [(f, "cassandra_config/templates/%s.j2" % f) for f in jmx if not (f in keep and f in live)]
    if conf_dir and str(value("cassandra_conf_dir")).rstrip("/") != conf_dir.rstrip("/"):
        errors.append("config dir: node reads %s, the roles would write to %s" % (conf_dir, value("cassandra_conf_dir")))
    # (cassandra_service_start package_script: the package's init script started, no unit written)
    unit_managed = str(value("cassandra_service_unit_manage | bool and cassandra_service_start | default('unit')"
                             " == 'unit'")) == "True"
    # the package's init script: its environment in a drop-in of its unit (cassandra_service_environment)
    package_script = str(value("cassandra_service_unit_manage | bool and cassandra_service_start | default('unit')"
                               " == 'package_script'")) == "True"
    if unit_managed:
        if "cassandra.service" in live:
            wanted.append(("cassandra.service", "cassandra_service/templates/cassandra.service.j2"))
        elif script:
            errors.append("cassandra.service: the node starts Cassandra with the init script %s (%s), the roles would"
                          " start it with their systemd unit from its next restart" % (
                              script[0], "modified since its package installed it" if script[1] == "modified"
                              else "of the package %s, as installed" % script[1] if script[1] else "of no package"))
        else:
            errors.append("cassandra.service: the roles would replace the unit, which could not be read")
    if str(value("cassandra_medusa_enabled | default(false) | bool")) == "True":
        wanted.append(("medusa.ini", "cassandra_medusa/templates/medusa.ini.j2"))
    rendered = {}
    for name, path in wanted:
        try:
            rendered[name] = lookup.run([os.path.join(ROLES, path)], variables=ctx)[0] or ""  # (2.19+: None when empty)
        except Exception as exc:  # pylint: disable=broad-except  # a template error: said by its type (no value)
            errors.append("%s: the roles could not write it with these variables (%s)" % (name, type(exc).__name__))
    # owner, group and mode cassandra_config gives its files (not the unit's nor medusa.ini's), the ones it keeps
    # as they are too
    kept = [f for f in list(ctx["_cassandra_config_files"][series]) + list(jmx) if f in keep and f in live]
    permissions = {}
    try:
        settings = value("_cassandra_config_perm_settings")
        for name in list(rendered) + kept:
            if name not in ("cassandra.service", "medusa.ini"):
                permissions[name] = cassandra_file_permissions(name, settings)
    except AnsibleFilterError as exc:  # cassandra_file_permissions': a file name or a mode, no secret
        errors.append("owner, group and mode: %s" % exc)
    except Exception as exc:  # pylint: disable=broad-except  # a template error: said by its type (no value)
        errors.append("owner, group and mode: the roles could not work them out with these variables (%s)"
                      % type(exc).__name__)
    # the environment Cassandra would get from the unit: the roles' one, else the node's, kept
    return rendered, errors, value("cassandra_service_environment") if unit_managed or package_script else None, \
        permissions


def _in_inventory(given, name, templates):
    """The text of the snippet that given (cassandra_config_template_snippets, as written) names for
    the file name, when it is one about to be written in the inventory ({{ inventory_dir }}/<path>), else None."""
    path = given.get(name) if isinstance(given, dict) else None
    m = IN_INVENTORY.match(str(path)) if isinstance(path, str) else None
    return templates.get(m.group(1)) if m else None


def _snippets(variables, ctx, value, templar, keep, overrides, templates):
    """Your template pieces (cassandra_config_template_snippets): what cassandra_config refuses, a difference ->
    [error]; their text rendered here (ctx: in their variable), from the inventory about to be written or from the
    controller."""
    try:
        snippets = value("cassandra_config_template_snippets") or {}
    except Exception as exc:  # pylint: disable=broad-except  # any template error, said by its type (no value)
        return ["cassandra_config_template_snippets: cannot be read with these variables (%s)" % type(exc).__name__]
    if not isinstance(snippets, dict):
        return ["cassandra_config_template_snippets: not a dict, cassandra_config would refuse it"]
    errors = []
    for f, path in sorted(snippets.items()):
        mine = _in_inventory(variables.get("cassandra_config_template_snippets"), f, templates)
        if f not in dict(SNIPPET_VARS):
            errors.append("cassandra_config_template_snippets: %s takes no snippet, cassandra_config would refuse it" % f)
        elif mine is None and (not isinstance(path, str) or not path.startswith("/") or not os.path.isfile(path)):
            errors.append("cassandra_config_template_snippets: %s: not an absolute path to a file on the controller,"
                          " cassandra_config would refuse it" % f)
        elif f.split(":")[0] in keep or f.split(":")[0] in overrides:
            errors.append("cassandra_config_template_snippets: %s: its file is also in cassandra_config_%s,"
                          " cassandra_config would refuse it" % (f, "keep_files" if f.split(":")[0] in keep
                                                                 else "template_overrides"))
        else:
            if mine is None:  # yours on the controller: read here (rendered as the role's lookup would)
                with open(path, encoding="utf-8") as text:
                    mine = text.read()
            try:
                ctx[dict(SNIPPET_VARS)[f]] = as_is(re.sub(r"\n+$", "", templar.template(trust_as_template(mine)) or ""))
            except Exception as exc:  # pylint: disable=broad-except  # a template error: said by its type (no value)
                errors.append("%s: its snippet could not be rendered with these variables (%s)" % (f, type(exc).__name__))
    return errors


@values_hidden
def cassandra_import_self_check(files, hosts, name, facts, live, node_environment=None, storage_dir="", java="",
                                conf_dir="", permissions=None, release="", inventory_dir="", jvm=None, os_found=None,
                                os_own=False):
    """files, hosts, name: the inventory files, the hosts file's data and the node's
    name there (the variables it gets: cassandra_inventory_host_vars); facts: its ansible_facts;
    live: {file: text the node has}; node_environment: what the node's unit
    gives Cassandra today (MAX_HEAP_SIZE and HEAP_NEWSIZE count wherever they
    are set); storage_dir: the JVM's -Dcassandra.storagedir, where the
    directories cassandra.yaml leaves out are; java: the running Java's major
    version (the jvm<N>-server.options it reads); conf_dir: where the node
    reads its config; permissions: {file: {owner, group, mode, uid, gid}} the
    node's files have (cassandra_permissions_import's files), None: not read;
    release: its Cassandra version (4.1.5; '': its series'), the cassandra.yaml
    keys it rejects are notes, not differences (the import leaves them out); inventory_dir: where the
    inventory is (a value may follow it: {{ inventory_dir }}/templates/...); jvm: {cmdline: the running
    JVM's /proc/<pid>/cmdline, newer: the config files written since it started, environment: what the JVM got for
    cassandra-env.sh to add from outside its unit (JVM_EXTRA_OPTS, MX4J_*)}: the JVM's files the roles
    would rewrite pass when the JVM gets the same arguments from them (a note).
    os_found: what import_cluster's read of the OS printed on the node ({rc, stdout}; {}: not read), None: the OS
    settings not checked; os_own: its limits file has cassandra_linux's mark.
    -> {'differences', 'notes'}."""
    return _self_check(_read_files(files), hosts, name, facts, live, node_environment, storage_dir, java, conf_dir,
                       permissions, release, inventory_dir, jvm, os_found, os_own)


@values_hidden
def cassandra_import_self_check_nodes(files, hosts, nodes, names, data, inventory_dir=""):
    """cassandra_import_self_check of each node read: files, hosts, inventory_dir as for it;
    nodes: cassandra_import_nodes; names: {address: its name in the hosts
    file}; data: cassandra_import_host_vars of the nodes' hosts (what was read
    on them). The files are read once for all the nodes.
    -> {name: {'differences', 'notes'}}."""
    files_data = _read_files(files)
    out = {}
    for node in nodes:
        error = None
        try:
            checked = _self_check_node(files_data, hosts, node, names, data, inventory_dir)
        except Exception as exc:  # pylint: disable=broad-except
            # ours (their message shows no value) or a missing variable; not another filter's
            # error (2.19+: Ansible's wrapper is an AnsibleFilterError too)
            if undefined_name(exc) or (type(exc) is AnsibleFilterError  # pylint: disable=unidiomatic-typecheck
                                       and exc.__cause__ is None
                                       and str(exc).startswith("cassandra_")):
                raise
            # which node, not the message (it may show a value read from it)
            error = error_without_message("cassandra_import_self_check_nodes", str(node.get("address")))
        if error:
            raise error  # (out of the except: not chained to the message)
        if checked:
            out[checked[0]] = checked[1]
    return out


def _self_check_node(files_data, hosts, node, names, data, inventory_dir=""):
    """One node of cassandra_import_self_check_nodes: (its name, its check), None when not read."""
    if not boolean(node["read"], strict=False):
        return None
    hv = data[node["host"]]
    slurped = list(hv["import_cluster_slurp"]["results"]) + list(hv.get("import_cluster_jmx_slurp", {}).get("results", []))
    for key, item in (("import_cluster_unit_slurp", "cassandra.service"), ("import_cluster_medusa_ini", "medusa.ini")):
        if "content" in (hv.get(key) or {}):
            slurped.append(dict(hv[key], item=item))
    live = dict((r["item"], b64decode(r["content"])) for r in slurped if "content" in r)
    unit = hv.get("import_cluster_unit") or {}
    name = names[node["address"]]
    cmdline = (hv.get("import_cluster_jvm_cmdline") or {}).get("content")
    running = {"cmdline": b64decode(cmdline) if cmdline else "", "newer": hv.get("import_cluster_newer_files") or [],
               "environment": unit.get("jvm_env") or {}}
    return name, _self_check(
        files_data, hosts, name, hv["ansible_facts"], live, unit.get("env", {}),
        hv.get("import_cluster_storage_dir", ""), hv.get("import_cluster_java", ""), hv["import_cluster_conf_dir"],
        (hv.get("import_cluster_perms") or {}).get("files"), (hv.get("import_cluster_config") or {}).get("release", ""),
        inventory_dir, running, hv.get("import_cluster_os_found") or {},
        boolean(hv.get("import_cluster_own_os", False), strict=False),
        (unit["script"], unit.get("pkg_script", "")) if unit.get("script") else None,
        {"repo": hv.get("import_cluster_repo") or {"manage": True}, "repo_files": hv.get("import_cluster_repo_files") or [],
         "series": hv.get("import_cluster_series", ""), "cqlsh_error": hv.get("import_cluster_cqlsh_error", ""),
         "own_cqlsh": boolean(hv.get("import_cluster_own_cqlsh", False), strict=False),
         "cqlsh": hv.get("import_cluster_cqlsh") or {}})


def _env_value(key, value):
    """A value of the environment as said (none: "nothing"; a secret's masked)."""
    if value is None:
        return "nothing"
    return "'%s'" % (_out.MASK if re.search(r"(?i)pass|secret|token|key", key) else value)


def _self_check(files_data, hosts, name, facts, live, node_environment, storage_dir, java, conf_dir, permissions,
                release="", inventory_dir="", running=None, os_found=None, os_own=False, script=None, parts=None):
    # the names Ansible gives the host, which a value of the import may follow ({{ inventory_hostname }}.x)
    variables = dict(_host_vars(files_data, hosts, name), inventory_hostname=name,
                     inventory_hostname_short=name.split(".")[0])
    if inventory_dir:
        variables["inventory_dir"] = inventory_dir
    rendered, errors, role_environment, wanted = _render(variables, facts, live, conf_dir,
                                                         files_data.get(TEMPLATES) or {}, script)
    if role_environment is None:  # the unit stays as it is
        role_environment = node_environment
    elif script and str(variables.get("cassandra_service_start", "unit")) == "package_script" \
            and boolean(variables.get("cassandra_service_unit_manage", True), strict=False):
        # the package's init script, its environment from a drop-in of its unit: the one the roles write against
        # the node's (what the script and Cassandra get)
        node_env, role_env = dict(node_environment or {}), dict(role_environment or {})
        for key in sorted(set(node_env) | set(role_env)):
            if node_env.get(key) != role_env.get(key):
                errors.append("cassandra.service.d (the environment of the init script): %s: node has %s, the roles"
                              " would set %s" % (key, _env_value(key, node_env.get(key)),
                                                 _env_value(key, role_env.get(key))))
    running = dict(running or {}, series=str(dict(_defaults(), **variables).get("cassandra_version")))
    out = _compare_files(rendered, live, node_environment, role_environment, storage_dir, java,
                         release or SERIES.get(str(variables.get("cassandra_version")), ""), running=running)
    if permissions is None:
        out["differences"].append("owner, group and mode: not read on the node, the roles may change them")
    else:
        out["differences"] += permission_differences(wanted, permissions)
    if parts is not None:
        differences, notes = _parts_check(variables, facts, parts)
        out["differences"] += differences
        out["notes"] += notes
    if os_found is not None:
        for check in (_linux_check, _firewall_check):
            differences, notes = check(variables, facts, os_found, os_own)
            out["differences"] += differences
            out["notes"] += notes
    return {"differences": errors + out["differences"], "notes": out["notes"]}


# cassandra_linux's variables the model of what it would change reads (cassandra_os.cassandra_linux_changes), as the
# role reads them
LINUX_VARS = (("manage", "cassandra_linux_manage | bool"), ("apply_live", "cassandra_linux_apply_live"),
              ("container_types", "cassandra_linux_container_types"), ("sysctl", "cassandra_linux_sysctl"),
              ("sysctl_file", "cassandra_linux_sysctl_file"), ("limits", "cassandra_linux_limits"),
              ("limits_file", "cassandra_linux_limits_file"), ("nproc_all", "cassandra_linux_nproc_all | bool"),
              ("swap_off", "cassandra_linux_swap_off | bool"), ("thp_enabled", "cassandra_linux_thp_enabled"),
              ("thp_defrag", "cassandra_linux_thp_defrag"), ("timesync", "cassandra_linux_timesync | bool"),
              ("timesync_servers", "cassandra_linux_timesync_servers"), ("offline", "cassandra_offline | bool"),
              ("disk_tuning", "cassandra_data_disk_tuning | bool"), ("readahead_kb", "cassandra_data_readahead_kb"),
              ("scheduler", "cassandra_data_scheduler"), ("lvm_readahead", "cassandra_data_lvm_readahead"),
              ("block_device", "cassandra_data_block_device"), ("block_devices", "cassandra_data_block_devices"),
              ("tuned_manage", "cassandra_linux_tuned_manage | bool"),
              ("tune_os_volumes", "cassandra_data_tune_os_volumes | bool"),
              ("sysfs_block_root", "cassandra_sysfs_block_root"))


def _role_values(role, exprs, variables, facts):
    """The values of exprs [(key, expression)] as the role reads them with these variables over its defaults ->
    ({key: value}, None), or (None, the difference saying which one cannot be read)."""
    ctx = _trusted(dict(_defaults(), **dict(role_file(role), **variables)))
    ctx["ansible_facts"] = facts
    templar = Templar(loader=DataLoader(), variables=ctx)
    out = {}
    for key, expr in exprs:
        try:
            value = templar.template(trust_as_template("{{ %s }}" % expr))
        except Exception as exc:  # pylint: disable=broad-except  # a template error: said by its type (no value)
            return None, "%s: %s cannot be read with these variables (%s)" % (role, expr.split()[0], type(exc).__name__)
        out[key] = str(value) == "True" if expr.endswith("| bool") else value
    return out, None


def _changes_lines(role, items):
    """The model's items -> (differences, notes): a change is a difference, a file written with no effect a note."""
    differences, notes = [], []
    for item in items:
        if item["effect"] == NO_EFFECT:
            notes.append("%s: %s: %s -> %s%s" % (role, item["what"], item["now"], item["would"], NO_EFFECT_NOTE))
        else:
            differences.append("%s: %s: node has %s, the roles would set %s" % (role, item["what"], item["now"], item["would"]))
    return differences, notes


def _role_expression(role, name):
    """The expression of a variable in a role's vars/main.yml ("{{ expression }}", alone), to template it here."""
    m = re.match(r"^\{\{(.*)\}\}$", str(role_file(role, "vars")[name]).strip(), re.S)
    if not m:
        raise ValueError("%s: %s is not one {{ expression }}" % (role, name))
    return m.group(1).strip()


# cassandra_firewall's (run by create_cluster and add_node with cassandra_manage_firewall: checked whatever it says,
# for the day it is set)
FIREWALL_VARS = (("manage", "cassandra_firewall_manage | bool"), ("enabled", "cassandra_firewall_enabled | bool"),
                 ("open_ports", "open_ports"),
                 # (the role's, the Prometheus exporter's port to its sources added)
                 ("port_sources", _role_expression("cassandra_firewall", "_cassandra_firewall_port_sources")),
                 ("exporter_port", "cassandra_prometheus_exporter_port | default(9404)"
                                   " if cassandra_prometheus_exporter_enabled | default(false) | bool else ''"),
                 ("ports", "[cassandra_storage_port | default(7000), cassandra_native_transport_port | default(9042),"
                           " cassandra_jmx_port | default(7199)]"))


def _firewall_check(variables, facts, found, own):
    """What cassandra_firewall would change on the node with these variables, and the rules it does not write ->
    (differences, notes)."""
    fw, error = _role_values("cassandra_firewall", FIREWALL_VARS, variables, facts)
    if error:
        return [error], []
    if not fw["manage"]:
        return [], []
    if found.get("rc", 1) != 0 or "stdout" not in found:
        return ["cassandra_firewall: the firewall could not be read on the node, the roles may change it"], []
    ports = ["%s/tcp" % p for p in fw["ports"]]
    items = cassandra_firewall_changes(found["stdout"], fw, facts.get("os_family", ""), ports)
    return _changes_lines("cassandra_firewall", items)


REPOSITORY_VARS = (("manage", "cassandra_repository_manage | bool"), ("method", "cassandra_install_method"))
CQLSH_VARS = (("manage", "cassandra_cqlsh_python_manage | bool"), ("source", "cassandra_cqlsh_source"),
              ("python", "cassandra_cqlsh_python"), ("supported", "cassandra_cqlsh_python_supported"),
              ("series", "cassandra_version"), ("offline", "cassandra_offline | bool"),
              ("command", "cassandra_cqlsh_command | default('cqlsh', true)"))
PYTHON311 = "/usr/bin/python3.11"
PACKAGE_CQLSHLIB = "/usr/lib/python3.6/site-packages/cqlshlib"


def _version_in(version, bounds):
    """version (3.9) within bounds ([3.6, 3.11])."""
    try:
        v = tuple(int(x) for x in str(version).split("."))
        return tuple(int(x) for x in str(bounds[0]).split(".")) <= v <= tuple(int(x) for x in str(bounds[1]).split("."))
    except (ValueError, IndexError, TypeError):
        return False


@functools.lru_cache(maxsize=None)
def _cqlsh_pointers(python):
    """cassandra_install's wrapper and profile.d file for this Python as cqlsh_links.yml writes them -> {path:
    "sha1|mode"} (as the read gives them)."""
    with open(os.path.join(ROLES, "cassandra_install", "tasks", "cqlsh_links.yml"), encoding="utf-8") as f:
        task = next(t for t in yaml.safe_load(f) if t.get("name") == "Point cqlsh at its Python")
    templar = Templar(loader=DataLoader(), variables={"_cassandra_cqlsh_python": python})
    out = {}
    for item in task["loop"]:
        content = str(templar.template(trust_as_template(item["content"])))
        out[item["dest"]] = "%s|%s" % (hashlib.sha1(content.encode()).hexdigest(), str(item["mode"]).lstrip("0"))
    return out


def _cqlsh_check(cqlsh, st, family, error):
    """What cassandra_install would change for the package's cqlsh (cqlsh_python.yml, cqlsh_links.yml) on a node,
    from what the import read there (st: import_cluster_cqlsh), and whether the cqlsh it sets up starts ->
    (differences, notes)."""
    out, notes = [], []
    if not st:  # (not read: what the playbooks run, at least)
        if error:
            out.append("cassandra_install: cqlsh: node has %s, the roles would set a Python for the package's cqlsh"
                       " (python3.11, the link of its cqlshlib, a wrapper, as it needs)" % error)
        return out, notes
    bounds = (cqlsh["supported"] or {}).get(str(cqlsh["series"]))
    python = cqlsh["python"] or ("" if not bounds or _version_in(st.get("py3"), bounds) else PYTHON311)
    if python == PYTHON311 and not boolean(st.get("py311", False), strict=False) and not cqlsh["offline"]:
        out.append("cassandra_install: python3.11: node has none, the roles would set installed (python3 %s is outside"
                   " %s, which the cqlsh of Cassandra %s runs on)"
                   % (st.get("py3") or "?", "-".join(bounds or []), cqlsh["series"]))
    # (offline without python3.11 the role leaves them as they are rather than point cqlsh at nothing)
    left = python == PYTHON311 and cqlsh["offline"] and not boolean(st.get("py311", False), strict=False)
    for path, key, what in (("/usr/local/bin/cqlsh", "wrapper", "their wrapper (the package's cqlsh with %s)"),
                            ("/etc/profile.d/cqlsh.sh", "profile", "CQLSH_PYTHON=%s")):
        have = st.get(key, "absent")
        named = st.get(key + "_py", "")
        if python and not left and have != "role":
            out.append("cassandra_install: %s: node has %s, the roles would set %s" % (
                path, "another file" if have == "other" else "none", what % python))
        elif python and not left and named != python:
            out.append("cassandra_install: %s: node has %s, the roles would set %s" % (
                path, what % (named or "?"), what % python))
        elif python and not left and st.get(key + "_sum") and st.get(key + "_sum") != _cqlsh_pointers(python)[path]:
            out.append("cassandra_install: %s: node has %s, edited or of an earlier version (content or mode), the roles"
                       " would set it as they write it" % (path, what % python))
        elif not python and have == "role":
            out.append("cassandra_install: %s: node has the roles' (an earlier run), the roles would set removed" % path)
    # each Python the role reads (cqlsh_links.yml): path|site-packages|cqlshlib|test
    pythons = {}
    for entry in st.get("pythons") or []:
        path, purelib, link, test = (str(entry).split("|", 3) + ["", "", ""])[:4]
        pythons.setdefault(path, {"purelib": purelib, "link": link, "test": test})
    used = python or "python3"
    if family == "RedHat":
        for path in [p for i, p in enumerate(["python3", python]) if p and p not in ["python3", python][:i]]:
            have = pythons.get(path)
            # (not there, or the package's own site-packages: nothing linked)
            if not have or not have["purelib"] or have["purelib"] == os.path.dirname(PACKAGE_CQLSHLIB):
                continue
            if have["link"] in ("", "absent"):
                notes.append("cassandra_install: cqlshlib of %s: none -> a link to the package's%s" % (path, NO_EFFECT_NOTE))
            elif have["link"] != PACKAGE_CQLSHLIB:
                out.append("cassandra_install: cqlshlib of %s: node has %s, the roles would set a link to %s" % (
                    path, "its own" if have["link"] == "other" else "a link to " + have["link"], PACKAGE_CQLSHLIB))
    test = (pythons.get(used) or {}).get("test", "")
    if test and test != "ok":
        out.append("cassandra_install: cqlsh as the roles would set it up (the package's, with %s) does not start: %s"
                   % (used, test))
    ran = st.get("ran", "")
    package = ("/usr/bin/cqlsh", "/usr/local/bin/cqlsh") if st.get("wrapper") in ("role", "absent") else ("/usr/bin/cqlsh",)
    # the same file through a link (/bin/cqlsh on EL, /bin a link to usr/bin): the package's
    real = st.get("real") or {}
    same = [p for p in package if real.get("ran") and real.get("ran") == real.get(p)]
    if ran and ran not in package and not same and cqlsh["command"] == "cqlsh" and not error:
        out.append("cassandra_install: cqlsh: node has %s (not the package's) for the playbooks, the roles would set"
                   " the package's: set cassandra_cqlsh_command: %s in group_vars and import again (it writes"
                   " cassandra_cqlsh_source: external), or -e import_keep_hand_edits=cqlsh" % (ran, ran))
    return out, notes


def _parts_check(variables, facts, parts):
    """What cassandra_repository and cassandra_install (cqlsh) would change on the node with these variables, from what
    the import read there (parts: {repo: cassandra_repository_import's, repo_files, series, cqlsh_error, own_cqlsh,
    cqlsh: import_cluster_cqlsh}) -> (differences, notes)."""
    out = []
    repo, error = _role_values("cassandra_repository", REPOSITORY_VARS, variables, facts)
    if error:
        out.append(error)
    elif repo["manage"]:
        files = ", ".join(parts["repo_files"]) or "none"
        if repo["method"] == "packages" and parts["repo_files"]:
            out.append("cassandra_repository: Cassandra repository files: node has %s, the roles would set removed"
                       " (cassandra_install_method packages)" % files)
        elif repo["method"] == "repository" and not parts["repo_files"]:
            out.append("cassandra_repository: Cassandra repository files: node has none, the roles would set"
                       " cassandra-%s (cassandra_install_method repository)" % parts["series"])
        elif repo["method"] == "repository" and not (parts["repo"] or {}).get("manage", True):
            out.append("cassandra_repository: Cassandra repository files: node has %s (%s), the roles would set"
                       " cassandra-%s as they write it" % (files, parts["repo"].get("why", ""), parts["series"]))
    cqlsh, error = _role_values("cassandra_install", CQLSH_VARS, variables, facts)
    notes = []
    if error:
        out.append(error)
    elif cqlsh["manage"] and cqlsh["source"] == "package" and (parts.get("cqlsh") or not parts["own_cqlsh"]):
        found, notes = _cqlsh_check(cqlsh, parts.get("cqlsh") or {}, facts.get("os_family", ""), parts["cqlsh_error"])
        out += found
    return out, notes


def _linux_check(variables, facts, found, own):
    """What cassandra_linux would change on the node with these variables (apply_config runs it) -> (differences,
    notes): a change is a difference, a file it writes with no effect on the node's values a note."""
    linux, error = _role_values("cassandra_linux", LINUX_VARS, variables, facts)
    if error:
        return [error], []
    if not linux["manage"]:
        return [], []
    if found.get("rc", 1) != 0 or "stdout" not in found:
        return ["cassandra_linux: the OS settings could not be read on the node, the roles may change them"], []
    if str(linux["sysfs_block_root"]) != "/sys/block":
        return ["cassandra_linux: cassandra_sysfs_block_root is not /sys/block, the import does not work out its disks"], []
    return _changes_lines("cassandra_linux", cassandra_linux_changes(found["stdout"], linux, facts, own))


def _rejected(live, rendered, release):
    """The top-level keys of the node's cassandra.yaml its Cassandra rejects, the roles' leaves out."""
    try:
        node = set(_top(k) for k in yaml_settings(live))
        role = set(_top(k) for k in yaml_settings(rendered))
    except yaml.YAMLError:
        return []
    return yaml_rejected(node - role, release)


def _compare_files(rendered, live, node_environment=None, role_environment=None, storage_dir="", java="", release="",
                   series="", running=None):
    """running: {cmdline, newer, series} (cassandra_import_self_check's jvm); series: cassandra_version, else running's."""
    differences, notes, heap, by_file = [], [], [], {}
    series = series or (running or {}).get("series", "")
    # a stock line of a later release the node's file predates: not a difference (the import says it under WORTH A LOOK)
    rendered = dict((name, without_later_stock(name, text, live.get(name))[0] if name in live else text)
                    for name, text in rendered.items())
    # the log dir written as itself in logback.xml: ${cassandra.logdir}, each side's (the node's: as its JVM got it)
    if "logback.xml" in live:
        live = dict(live, **{"logback.xml": logback_as_log_dir(live["logback.xml"], log_dir_of(
            live.get("cassandra-env.sh", ""), (running or {}).get("cmdline"), node_environment,
            (running or {}).get("newer")))})
    if "logback.xml" in rendered:
        rendered = dict(rendered, **{"logback.xml": logback_as_log_dir(rendered["logback.xml"], log_dir_of(
            rendered.get("cassandra-env.sh", ""), "", role_environment))})
    if "cassandra-env.sh" in live:  # the Prometheus exporter's block: the line it runs (the role writes the agent)
        live = dict(live, **{"cassandra-env.sh": prometheus.block_as_line(live["cassandra-env.sh"],
                                                                          (running or {}).get("cmdline"))})
    if "cassandra-env.sh" in live and series in ("40x", "41x"):  # (a plain CASSANDRA_HEAPDUMP_DIR: as the import reads it)
        live = dict(live, **{"cassandra-env.sh": jvm_args.heap_dump_dir_as_role(
            live["cassandra-env.sh"], dict(node_environment or {}, **((running or {}).get("environment") or {})),
            bool((running or {}).get("cmdline")))[0]})
    jvm_live, jvm_rendered = live, rendered  # (the JVM's proof: the files whole)
    if "cassandra-env.sh" in rendered and "cassandra-env.sh" in live:
        # (the node's lines in the remote JMX branch it takes, the roles' outside it: as the import reads them)
        mine, theirs = jvm_args.remote_branch_moved(live["cassandra-env.sh"], rendered["cassandra-env.sh"], dict(
            node_environment or {}, **((running or {}).get("environment") or {})), (running or {}).get("cmdline"))
        if mine:
            notes.append(REMOTE_BRANCH_NOTE % _out.plural(len(mine), "line"))
        # (and a line its block writes again the same below it: the last one counts)
        again = jvm_args.repeated_lines(live["cassandra-env.sh"])
        if again:
            notes.append(REPEATED_NOTE % _out.plural(len(again), "line"))
        live = dict(live, **{"cassandra-env.sh": jvm_args.without_lines(live["cassandra-env.sh"], mine + again)})
        rendered = dict(rendered, **{"cassandra-env.sh": jvm_args.without_lines(
            rendered["cassandra-env.sh"], theirs + jvm_args.repeated_lines(rendered["cassandra-env.sh"]))})
        node_heap = _heap(live["cassandra-env.sh"], node_environment)
        role_heap = _heap(rendered["cassandra-env.sh"], role_environment)
        heap = _dict_diff(HEAP_FILE, node_heap, role_heap)
    read = jvm.options_file(series, java) or "jvm%s-server.options" % java
    for name in sorted(rendered):
        other_java = re.match(r"^jvm(\d+)-server\.options$", name)
        other_java = other_java if other_java and java and name != read else None
        if name not in live:
            if other_java:
                notes.append("%s: not on the node, the roles would create it (Java %s does not read it)" % (name, java))
            else:
                differences.append("%s: not on the node, the roles would create it" % name)
            continue
        left_out = _rejected(live[name], rendered[name], release) if name == "cassandra.yaml" and release else []
        if left_out:
            notes.append("cassandra.yaml: %s, which Cassandra %s rejects: left out by the import (the node would not"
                         " restart with %s)" % (", ".join(left_out), release, "it" if len(left_out) == 1 else "them"))
        try:
            found = _compare(name, live[name], rendered[name], storage_dir, left_out)
        except (yaml.YAMLError, ET.ParseError, configparser.Error, RecursionError):
            found = ["%s: cannot be read as its program reads it (node's or the roles' version)" % name]
        # cassandra_config keeps a file only with the same settings by its own test: else it rewrites it
        # (a key left out: it rewrites it, as it should)
        if not (found or left_out or heap and name == "cassandra-env.sh") and name in CONFIG_FILES \
                and not same_settings(name, live[name], rendered[name], storage_dir):
            found = ["%s: the same settings, written in a way cassandra_config does not keep (it would rewrite the file,"
                     " e.g. the heap moved between the unit and cassandra-env.sh, or a line going on to the next)" % name]
        if other_java and found:  # a file the running Java does not read: no effect today
            notes += ["%s (no effect: Java %s does not read this file; it matters only if the node runs Java %s)"
                      % (line, java, other_java.group(1)) for line in found]
            continue
        by_file[name] = found
        differences += found
    moved = _options_moved(rendered, live, series, java, by_file)
    if moved:
        differences = [d for d in differences if not any(d in by_file[f] for f in moved)
                       or not any(d.startswith("%s: %s: node has " % (f, k)) for f, keys in moved.items() for k in keys)]
        for f in moved:
            by_file[f] = [d for d in by_file[f] if d in differences]
        notes.append("%s: %s set in the other of the two files Java %s reads (bin/cassandra passes them as one list):"
                     " the same for the JVM" % (", ".join(sorted(moved)), ", ".join(sorted(set(
                         k for keys in moved.values() for k in keys))), java))
    rewritten, apart = _jvm_rewritten(jvm_rendered, jvm_live, by_file, heap, node_environment, role_environment, java,
                                      running or {})
    if rewritten:
        # but for the options files' own hand edits, said as they are
        differences = [d for d in differences if not any(d in by_file.get(name, []) for name in rewritten)
                       or any(d.startswith("%s: %s:" % (f, k)) for k, f in apart.items())]
        notes.append("%s: cassandra_config rewrites %s in its own layout; the JVM gets the same arguments from them at"
                     " its next restart (worked out from the running JVM's)%s" % (
                         ", ".join(rewritten), "it" if len(rewritten) == 1 else "them",
                         ", but for %s, above" % ", ".join(sorted(apart)) if apart else ""))
        heap = []
    return {"differences": differences + heap, "notes": notes}


def _options_moved(rendered, live, series, java, by_file):
    """The settings jvm-server.options and the options file of the running Java differ by each on its own, but not
    read together as bin/cassandra reads them (the first, then the other, one list, the last of an option counting):
    an option moved from one to the other (the heap, cassandra_heap_in_jvm_options) -> {file: [its settings]}."""
    pair = [jvm_args.COMMON, jvm.options_file(series, java)]
    if not pair[1] or not all(f in rendered and f in live and by_file.get(f) is not None for f in pair):
        return {}
    a, b = [jvm_options_settings("\n".join(side[f] for f in pair)) for side in (live, rendered)]
    moved = {}
    for f in pair:
        mine, theirs = jvm_options_settings(live[f]), jvm_options_settings(rendered[f])
        keys = [k for k in sorted(set(mine) | set(theirs)) if mine.get(k) != theirs.get(k) and a.get(k) == b.get(k)]
        if keys:
            moved[f] = keys
    return moved


def _jvm_rewritten(rendered, live, by_file, heap, node_environment, role_environment, java, running):
    """The JVM's files (cassandra-env.sh, the options files the running Java reads) that differ when the JVM gets
    the same arguments from the roles' ones (the heap is among them), but for the settings the options files'
    own hand edits change -> (files, {setting: its file}); ([], {}) otherwise."""
    series = running.get("series")
    group = [jvm_args.ENV, jvm_args.COMMON, jvm_args.options_file(series, java)]
    # a file the roles leave as it is (cassandra_config_keep_files): the node's
    rendered = dict(dict((f, live[f]) for f in group if f in live and f not in rendered), **rendered)
    differing = [name for name in group if by_file.get(name)]
    if not running.get("cmdline") or set(running.get("newer") or []) & set(group) or not (differing or heap) \
            or not all(f in rendered and f in live for f in group):
        return [], {}
    apart = jvm_args.options_edits(live, rendered, series, java)
    # (each side's environment, the unit's (the roles': theirs, else the node's kept), with what the JVM got from
    # elsewhere for cassandra-env.sh to add, an EnvironmentFile's JVM_EXTRA_OPTS, as apply_config's proof reads it)
    jvm_env = running.get("environment") or {}
    same, dummy = jvm_args.cassandra_jvm_args_same(
        running["cmdline"], live, rendered, series, java, dict(node_environment or {}, **jvm_env),
        dict(role_environment if role_environment is not None else node_environment or {}, **jvm_env), apart)
    return ((differing or [jvm_args.ENV]), apart) if same else ([], {})


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_import_self_check_nodes": cassandra_import_self_check_nodes,
        }
