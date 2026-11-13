# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_prometheus_config_error: the exporter config text a user gives
(cassandra_prometheus_exporter_config) -> why the exporter would refuse it
(not YAML, not a mapping), '' when it would not (line and column only: the
text may hold passwords).
cassandra_prometheus_config_change: the Prometheus exporter's config file on
a node (None or '': not there) and the one cassandra_config writes ->
{same, restart, diff}: the same YAML (comments and layout aside); a restart
when its httpServer or openTelemetry part differs, which the exporter reads at
start only (the rest it reloads at the next scrape); the masked diff.
cassandra_prometheus_agent_paths: a node's config files -> the jar and the
config of the exporter's agent they give the JVM (what import_cluster reads).
cassandra_prometheus_at_start: a config -> the content of its record of the
parts read at start (jmx_prometheus.at_start).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import importlib.util
import os

import yaml

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_prometheus import (
    at_start_digest, config_change, without_agent)

CONFIG_READ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config", "files",
                           "config_read.py")


def _config_read():
    """cassandra_config's files/config_read.py (its masked diff, as the role's other config files get it)."""
    spec = importlib.util.spec_from_file_location("cassandra_config_read", CONFIG_READ)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cassandra_prometheus_config_change(live, new, path=""):
    """+ diff: the unified diff of the node's file (path) to the role's, secrets masked as in the other config
    files' diffs ('' when the same).
    e.g. ("rules: []\\n", "rules: [{pattern: x}]\\n") -> {same: False, restart: False, diff: "--- ..."}."""
    try:
        out = config_change(live, str(new or ""))
    except (ValueError, yaml.YAMLError):  # the role's is not a YAML mapping: cassandra_config's checks refuse it before
        raise AnsibleFilterError("cassandra_prometheus_config_change: the new config is not a YAML mapping")
    dummy, text = _config_read().diff_lines(path, str(live or "").splitlines(True), str(new or "").splitlines(True))
    return dict(out, diff=text.rstrip("\r\n"))


def cassandra_prometheus_at_start(text):
    """The content of the record of the exporter config's parts read at start (jmx_prometheus.at_start, next to
    the config files: the record of the config Cassandra started with keeps it, a change is a restart pending)."""
    return ("# Managed by Ansible (community.cassandra.cassandra_config): the sha256 of the parts of the Prometheus"
            " exporter's config read at start\n%s\n" % at_start_digest(str(text or "")))


def cassandra_prometheus_config_error(text):
    """e.g. "rules: [" -> "cassandra_prometheus_exporter_config is not valid YAML (line 1, column 9)"."""
    text = str(text or "")
    if not text.strip():
        return ""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark else ""
        return "cassandra_prometheus_exporter_config is not valid YAML%s" % where
    if not isinstance(data, dict):
        return "cassandra_prometheus_exporter_config must be the YAML of a mapping (the exporter config)"
    return ""


def cassandra_prometheus_agent_paths(files, series="", java=""):
    """A node's config files ({name: text}) -> {jar, config} of the exporter's agent they give the JVM, as
    import_cluster imports it (the first JVM options file the running Java reads, series and java given: those of
    its series, else any; then cassandra-env.sh), {} when none: what the import reads of it (its jar's sha256, its
    config)."""
    read = jvm.read_files(series, str(java or "")) if jvm.options_file(series, str(java or "")) else None
    names = sorted(f for f in files or {} if str(f).startswith("jvm") and str(f).endswith("-server.options")
                   and (read is None or f in read))
    for name in names + ["cassandra-env.sh"]:
        if isinstance((files or {}).get(name), str):
            dummy, found = without_agent(name, files[name])
            if found and found["kind"] != "kept":
                return {"jar": found["jar"], "config": found["config"]}
    return {}


class FilterModule(object):
    def filters(self):
        return {"cassandra_prometheus_agent_paths": cassandra_prometheus_agent_paths,
                "cassandra_prometheus_at_start": cassandra_prometheus_at_start,
                "cassandra_prometheus_config_change": cassandra_prometheus_config_change,
                "cassandra_prometheus_config_error": cassandra_prometheus_config_error}
