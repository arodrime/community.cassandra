# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The Prometheus JMX exporter's Java agent, as cassandra_config writes it and
as import_cluster finds it on a node.

agent(text): the first -javaagent of jmx_prometheus_javaagent-<version>.jar
    in a text (a JVM options line, a shell line, the JVM's arguments) ->
    {option, jar, version, dir, address, port, config}, None when none.
without_agent(name, text): a JVM options file or cassandra-env.sh -> the
    text without the line (or the cassandra-env.sh block: if ... fi) that
    gives the agent, and what was taken out.
block_as_line(text, cmdline): cassandra-env.sh with the agent's block read
    as the line it runs (the running JVM has the agent), for the self-check.
config_change(live, new): the exporter config on the node and the role's ->
    {same, restart}: the rules are reloaded at the next scrape, the
    httpServer (TLS, authentication) and openTelemetry parts are read at start
    only.
at_start_digest(text): the digest of those parts, kept in the record of the
    config Cassandra started with (a change there: a restart pending).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import hashlib
import json
import re

import yaml

# -javaagent:<dir>/jmx_prometheus_javaagent-<version>.jar=[<host>:]<port>:<config> (the agent's own parsing:
# a host name, an IPv4 address or an IPv6 one in brackets; a port of 1 to 5 digits; the config file)
AGENT = re.compile(r"-javaagent:(?P<jar>(?P<dir>/[^\s\"'=]*)/jmx_prometheus_javaagent-(?P<version>[0-9][0-9A-Za-z.-]*)\.jar)"
                   r"=(?:(?P<host>[\w.-]+|\[[0-9A-Fa-f:.]+\]):)?(?P<port>\d{1,5}):(?P<config>/[^\s\"';]+)")
# a shell assignment, outside quotes or not (the block's guards may set nothing else than JVM_OPTS)
# a version cassandra_config takes (cassandra_prometheus_exporter_version), e.g. 1.7.0, not 1.0.1-rc
RELEASE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
ASSIGNMENT = re.compile(r"(?:^|[\s;&|({])(?:export\s+)?([A-Za-z_]\w*)=")
OPENERS = ("if", "case", "for", "while", "until", "{")
CLOSERS = ("fi", "esac", "done", "}")


def agent(text):
    """The agent in text, or None. e.g. "-javaagent:/opt/x/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/x.yml" ->
    {option: that, jar: "/opt/x/jmx_prometheus_javaagent-1.7.0.jar", version: "1.7.0", dir: "/opt/x",
    address: "", port: 9404, config: "/etc/x.yml"}."""
    m = AGENT.search(str(text or ""))
    if not m:
        return None
    return {"option": m.group(0), "jar": m.group("jar"), "version": m.group("version"), "dir": m.group("dir"),
            "address": (m.group("host") or "").strip("[]"), "port": int(m.group("port")), "config": m.group("config")}


def _code(line):
    """A shell line without its comment (a # starting a word, outside quotes)."""
    quote = None
    for i, char in enumerate(line):
        if quote:
            quote = None if char == quote else quote
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _depth_change(code):
    """How many blocks a shell line opens less the ones it closes (its words, quotes aside)."""
    words = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", " ", code).replace(";", " ; ").split()
    change = 0
    for i, word in enumerate(words):
        command = i == 0 or words[i - 1] in (";", "then", "do", "else", "elif", "&&", "||", "|", "!", "{")
        if command and word in OPENERS:
            change += 1
        elif command and word in CLOSERS:
            change -= 1
    return change


def _only_adds_agent(lines, found):
    """Whether a cassandra-env.sh block (its lines) sets nothing but JVM_OPTS="$JVM_OPTS <the agent>" (its guards
    and messages aside)."""
    for line in lines:
        code = _code(line)
        for m in ASSIGNMENT.finditer(code):
            if m.group(1) != "JVM_OPTS":
                return False
            rest = code[m.end():]
            value = re.match(r"\"\$(?:JVM_OPTS|\{JVM_OPTS\}) ([^\"]*)\"", rest)
            if not value or value.group(1).strip() != found["option"]:
                return False
    return True


def without_agent(name, text):
    """name: a JVM options file (jvm*-server.options) or cassandra-env.sh; text: its content -> (the text without
    the agent's line, or block, {agent fields, line: its first line number, lines: how many, kind: "line" or
    "block"}), or (text, None) when it has no agent, (text, {..., "kind": "kept", "why": ...}) when the agent's
    lines do more than give it (left as they are)."""
    lines = str(text or "").split("\n")
    shell = name.endswith(".sh")
    for n, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        found = agent(_code(line) if shell else line)
        if not found:
            continue
        if not shell:  # bin/cassandra passes on each line starting with '-': the line is the option
            if line.strip() != found["option"]:
                return text, dict(found, line=n + 1, lines=1, kind="kept",
                                  why="other options on the agent's line of %s" % name)
            return "\n".join(lines[:n] + lines[n + 1:]), dict(found, line=n + 1, lines=1, kind="line", guarded=False)
        # cassandra-env.sh: the line, or the block around it (from the line that opens it, at depth 0, to the
        # one that closes it)
        depth, start = 0, None
        for i in range(n + 1):
            if depth == 0:
                start = i
            depth += _depth_change(_code(lines[i]))
        end = n
        while depth > 0 and end + 1 < len(lines):
            end += 1
            depth += _depth_change(_code(lines[end]))
        if depth != 0:
            return text, dict(found, line=n + 1, lines=1, kind="kept", why="its block in %s is not closed" % name)
        block = lines[start:end + 1]
        if not _only_adds_agent(block, found):
            return text, dict(found, line=start + 1, lines=len(block), kind="kept",
                              why="its %s in %s sets more than the agent" % (
                                  "block" if end > start or _depth_change(_code(lines[start])) else "line", name))
        kind = "block" if end > start or re.match(r"\s*(if|case|for|while|until|\{)\b", _code(lines[start])) else "line"
        # what else the lines run than the assignment: guards (if, [ ... ] &&, ||), messages
        rest = re.sub(r"(?:export\s+)?JVM_OPTS=\"[^\"]*\"", "", " ".join(_code(line) for line in block))
        guarded = kind == "block" or bool(re.search(r"\S", rest.replace(";", "")))
        return "\n".join(lines[:start] + lines[end + 1:]), dict(found, line=start + 1, lines=len(block), kind=kind,
                                                                guarded=guarded)
    return text, None


def block_as_line(text, cmdline):
    """cassandra-env.sh with the block (if ... fi) that only adds the agent read as the JVM_OPTS line it runs when
    its guards pass, as they did for the running JVM (cmdline: its /proc/<pid>/cmdline, which has the agent);
    else as it is."""
    stripped, found = without_agent("cassandra-env.sh", text)
    if not found or found["kind"] != "block" or found["option"] not in str(cmdline or "").split("\0"):
        return text
    lines = str(text).split("\n")
    start = found["line"] - 1
    return "\n".join(lines[:start] + ['JVM_OPTS="$JVM_OPTS %s"' % found["option"]] + lines[start + found["lines"]:])


# the parts of the exporter's config read at start only (the rest it reloads when the file changes)
AT_START = ("httpServer", "openTelemetry")


def _parts(text):
    """(the YAML of the config as data, its parts read at start); ValueError when not a mapping."""
    data = yaml.safe_load(text) if text else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("not a mapping")
    return data, dict((k, data[k]) for k in AT_START if k in data)


def at_start_digest(text):
    """The sha256 of the parts of the config read at start (their JSON), what the record of the config Cassandra
    started with keeps for it; a config that is not YAML: the sha256 of its text."""
    try:
        dummy, parts = _parts(text)
        blob = json.dumps(parts, sort_keys=True, default=str)
    except (yaml.YAMLError, ValueError):
        blob = str(text)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def config_change(live, new):
    """live: the config file on the node (None or '': not there); new: the role's -> {same: the same YAML (comments
    and layout aside), restart: its parts read at start (httpServer, openTelemetry) differ; the rest is reloaded at
    the next scrape}. A live file that is not YAML: not the same, a restart (what the running agent read is unknown).
    e.g. ("rules: []\\n", "rules: [{pattern: x}]\\n") -> {same: False, restart: False}."""
    new_data, new_parts = _parts(new)
    if live is None or live == "":
        return {"same": False, "restart": bool(new_parts)}
    try:
        live_data, live_parts = _parts(live)
    except (yaml.YAMLError, ValueError):
        return {"same": False, "restart": True}
    return {"same": live_data == new_data, "restart": live_parts != new_parts}


# httpServer settings with a variable of cassandra_config (the rest of httpServer is not carried)
HTTP_VARS = {("ssl", "keyStore", "filename"): "cassandra_prometheus_exporter_tls_keystore",
             ("ssl", "keyStore", "password"): "cassandra_prometheus_exporter_tls_keystore_password",
             ("ssl", "certificate", "alias"): "cassandra_prometheus_exporter_tls_certificate_alias",
             ("authentication", "basic", "username"): "cassandra_prometheus_exporter_username",
             ("authentication", "basic", "password"): "cassandra_prometheus_exporter_password"}


def _leaves(data, path=()):
    if isinstance(data, dict) and data:
        for key, value in data.items():
            yield from _leaves(value, path + (str(key),))
    else:
        yield path, data


# the first line of the config cassandra_config writes
ROLE_HEADER = "# Managed by Ansible (community.cassandra.cassandra_config)"


def carried_config(text, default=None):
    """A node's exporter config -> (cassandra_config variables that write it: cassandra_prometheus_exporter_config,
    its httpServer as the HTTPS and basic authentication variables; None when it cannot be: why). The text kept as
    it is when it has no httpServer (its comments too, not the role's header line); else the rest of its YAML.
    default: the role's own rules (files/jmx_prometheus.yml): the same YAML is "" (the role's default)."""
    text = "\n".join(line for line in str(text).split("\n") if not line.startswith(ROLE_HEADER))
    try:
        data, dummy = _parts(text)
        default_data = _parts(default)[0] if default else None
    except (yaml.YAMLError, ValueError):
        return None, "not a YAML mapping"
    rest = dict((k, v) for k, v in data.items() if k != "httpServer")
    rules = "" if rest == default_data else None
    if "httpServer" not in data:
        return {"cassandra_prometheus_exporter_config": text.strip() + "\n" if rules is None else rules}, ""
    leaves = dict(_leaves(data["httpServer"]))
    other = sorted("httpServer." + ".".join(k) for k in leaves if k not in HTTP_VARS)
    if other:
        return None, "its httpServer sets what no variable covers (%s)" % ", ".join(other)
    out = dict((HTTP_VARS[k], str(v)) for k, v in leaves.items())
    if len([k for k in out if "_tls_" in k]) not in (0, 3) or len([k for k in out if "_tls_" not in k]) not in (0, 2):
        return None, "its httpServer is not whole (a keystore with its password and alias, a username with its password)"
    out["cassandra_prometheus_exporter_config"] = yaml.safe_dump(rest, default_flow_style=False, sort_keys=False) \
        if rules is None else rules
    return out, ""
