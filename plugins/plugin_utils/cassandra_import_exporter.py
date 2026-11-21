# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): the Prometheus exporter of a node (its agent, its config file).

No filter of its own: cassandra_config_import (cassandra_import_config) reads it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re

from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import same_settings
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_prometheus as prometheus
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import (
    role_default, render_template, same_value)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import ROLE


def exporter_import(live_files, cmdline, ctx, read=None, conf_dirs=(), java_files=None):
    """The Prometheus exporter's agent a node's files give its JVM: a line of a JVM options file, a JVM_OPTS line of
    cassandra-env.sh or a block of it (if ... fi) that only adds it -> (the files without it, the JVM's arguments
    without it, {vars: cassandra_prometheus_exporter_*, moved: report lines, notes, found: the agent and its
    file}). The rest of the import reads the node as without it: the role writes it from these variables (at the
    end of jvm-server.options). read: what the import read of it on the node, {sha256: of its jar, config: its
    config's text (None: not read), jar, config_file: the paths they were read at (another agent's: not read),
    carry: its content into cassandra_prometheus_exporter_config (else the path, which a node added later has
    not)}; conf_dirs: the node's conf dir (the role's config there is its own: its
    content, the role's variables again); java_files: the options files the running Java reads (None: not known, all
    of them)."""
    read = dict(read or {})
    files, found, notes = _agent_taken_out(live_files, java_files)
    args = str(cmdline or "").split("\0")
    if found is None:
        running = prometheus.agent(" ".join(args))
        if running and not notes:
            notes.append("the running JVM has the Prometheus exporter's agent (%s) from none of the config files (the"
                         " unit's environment?): NOT imported" % cassandra_secrets.mask(running["option"]))
        return live_files, cmdline, {"vars": {}, "moved": [], "notes": notes, "found": None}
    if found["option"] in args:
        args.remove(found["option"])
    variables = {"cassandra_prometheus_exporter_enabled": True, "cassandra_prometheus_exporter_version": found["version"],
                 "cassandra_prometheus_exporter_dir": found["dir"], "cassandra_prometheus_exporter_port": found["port"],
                 "cassandra_prometheus_exporter_address": found["address"],
                 "cassandra_prometheus_exporter_config_file": found["config"]}
    # what was read on the node: of this agent's jar and config only (read for another agent: not read)
    if read.get("jar") and read["jar"] != found["jar"]:
        read.pop("sha256", None)
    if read.get("config_file") and read["config_file"] != found["config"]:
        read.pop("config", None)
    # the jar the node has (its version from its name), checked by its sha256: never left unchecked
    if read.get("sha256"):
        variables["cassandra_prometheus_exporter_checksum"] = "sha256:" + read["sha256"]
    else:
        notes.append("%s: the Prometheus exporter's jar could not be read: its checksum is not imported. Set"
                     " cassandra_prometheus_exporter_checksum to its sha256 (sha256sum %s), or the role takes it"
                     " for another jar and downloads %s's" % (found["jar"], found["jar"], found["version"]))
    carried = _exporter_config(found, read, conf_dirs, variables, notes)
    variables = dict((k, v) for k, v in variables.items() if not same_value(v, role_default(ctx, k)))
    where = "%s line %d" % (found["file"], found["line"]) if found["lines"] == 1 else "%s lines %d-%d" % (
        found["file"], found["line"], found["line"] + found["lines"] - 1)
    moved = ["%s: %s%s -> cassandra_prometheus_exporter_* (the role writes it in jvm-server.options%s%s)" % (
        where, cassandra_secrets.mask(found["option"]), " (an if ... fi block)" if found["kind"] == "block" else "", carried,
        "; drop it from your own copies of %s: its guards are not kept, the role puts the jar and the config in place"
        " and checks its port before each restart" % found["file"] if found["guarded"] else "")]
    if found["option"] not in str(cmdline or "").split("\0") and cmdline:
        notes.append("%s: the running JVM does not have the Prometheus exporter's agent its config gives (%s; %s):"
                     " with the role it loads at the next restart" % (
                         where, cassandra_secrets.mask(found["option"]), "its guards failed at its start?" if found["guarded"]
                         else "a file its Java does not read, or written since it started?"))
    return files, "\0".join(args), {"vars": variables, "moved": moved, "notes": notes, "found": found}


def _agent_taken_out(live_files, java_files):
    """The exporter's agent out of the JVM's files that give it (the options files the running Java reads, then
    cassandra-env.sh): the first one cassandra_config takes; the others notes, left as hand edits -> (the files, the
    agent and its file (None: none), notes)."""
    files, found, notes = dict(live_files), None, []
    names = sorted(f for f in files if re.match(r"^jvm\d*-server\.options$", f)
                   and (java_files is None or f in java_files)) + [jvm_args.ENV]
    for name in [n for n in names if isinstance(files.get(n), str)]:
        text, got = prometheus.without_agent(name, files[name])
        if got is None:
            continue
        # (a version cassandra_config refuses, e.g. 1.0.1-rc: left as it is)
        why = got["why"] if got["kind"] == "kept" else "the JVM gets it already from %s" % found["file"] if found \
            else "its version %s is not a release number cassandra_config takes, like 1.7.0" % got["version"] \
            if not prometheus.RELEASE.match(got["version"]) else ""
        if why:
            notes.append("%s line %d: the Prometheus exporter's agent (%s): NOT imported (%s), left as a hand edit" % (
                name, got["line"], cassandra_secrets.mask(got["option"]), why))
            continue
        files[name], found = text, dict(got, file=name)
    return files, found, notes


def _exporter_config(found, read, conf_dirs, variables, notes):
    """The exporter's config: the role's own (in the conf dir) written again from the role's variables, its
    content carried when asked (read["carry"]), else its path -> the report's words for a config carried ('' when
    not); variables and notes changed in place."""
    carried = ""
    with open(os.path.join(ROLE, "files", "jmx_prometheus.yml"), encoding="utf-8") as f:
        default_rules = f.read()
    own = found["config"] in ["%s/jmx_prometheus.yml" % str(d).rstrip("/") for d in conf_dirs if d]
    config = read.get("config") or None  # None: not read ('' before ansible-core 2.19, a task's vars)
    if own:  # the role's own config (a node the roles set up): written again from the role's variables
        variables.pop("cassandra_prometheus_exporter_config_file")
        taken, why = prometheus.carried_config(config, default_rules) if config is not None \
            else (None, "it could not be read on the node")
        if taken is None:
            notes.append("%s, the role's exporter config: its content NOT carried into cassandra_prometheus_exporter_config"
                         " (%s): the role writes its own rules there. Set cassandra_prometheus_exporter_config (and its"
                         " HTTPS and authentication variables) to keep the node's" % (found["config"], why))
        else:
            variables.update(taken)
    elif read.get("carry"):
        taken, why = prometheus.carried_config(config, default_rules) if config is not None \
            else (None, "it could not be read on the node (make it readable there, then import again)")
        if taken is None:
            notes.append("%s: its content NOT carried into cassandra_prometheus_exporter_config: %s; its path is"
                         " imported instead" % (found["config"], why))
        else:
            variables.pop("cassandra_prometheus_exporter_config_file")
            variables.update(taken)
            carried = "; its config %s carried into cassandra_prometheus_exporter_config, written to the conf dir" \
                      % found["config"]
    if "cassandra_prometheus_exporter_config_file" in variables and not read.get("carry"):
        notes.append("cassandra_prometheus_exporter_config_file: %s, a file of the node, not copied: a node added later"
                     " has none (cassandra_config stops there). To carry its content into"
                     " cassandra_prometheus_exporter_config (its passwords to secrets.yml), import again with"
                     " -e import_carry_exporter_config=true" % found["config"])
    return carried


def exporter_rewritten(env, series, render, exporter, live):
    """The JVM's files cassandra_config rewrites for the exporter's agent imported (where it was, and
    jvm-server.options, which ends with it): the ones whose settings differ from the node's."""
    found = exporter.get("found")
    if not found:
        return []
    option = found["option"]
    if "cassandra_prometheus_exporter_config_file" not in exporter["vars"]:  # the role's config, in the conf dir
        option = option[:len(option) - len(found["config"])] + "%s/jmx_prometheus.yml" % str(
            render.get("cassandra_conf_dir") or "/etc/cassandra").rstrip("/")
    variables = dict(render, _cassandra_config_jvm_agent=option, **exporter["vars"])
    out_files = []
    for name in [found["file"], jvm_args.COMMON]:
        if name in live and name not in out_files \
                and not same_settings(name, live[name], "\n".join(render_template(env, series, name, variables)[1])):
            out_files.append(name)
    return out_files
