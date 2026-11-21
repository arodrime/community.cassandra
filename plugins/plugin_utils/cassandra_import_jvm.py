# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): the JVM options files, JMX and logback.xml of a node.

The JVM options files read against the role's, the heap, the JMX users (password and access files), the log
directory, the lines moved between cassandra-env.sh and the options files, and logback.xml (its loggers, its
appenders, the hand edits carried).

Filter (plugins/filter/cassandra_import.py names the filters):
cassandra_jmx_files: cassandra-env.sh, the JVM options files, the JVM's
    arguments -> the remote JMX password and access files the JVM reads.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat

from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import without_later_stock, same_settings
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import (
    APPENDERS, EXPR, HEAP_OPTIONS, LOGBACK_END, REMOVED_TOGGLE, render_template)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import ROLE
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import keep_files_option


# The rights the role gives a readwrite JMX user besides readwrite
JMX_CREATE_UNREGISTER = ["create", "javax.management.monitor.*,javax.management.timer.*", "unregister"]
# The rights of an access file line the role writes, as the JVM takes them (readonly alone; the role's own check)
_JMX_CLASS = r"[A-Za-z0-9_$*]+([.][A-Za-z0-9_$*]+)*"
JMX_RIGHTS = re.compile(r"^(readonly|readwrite([ \t]+(unregister|create[ \t]+%s([ \t]*,[ \t]*%s)*))*)[ \t]*$"
                        % (_JMX_CLASS, _JMX_CLASS))


def _template_options(env, series, ctx, name):
    """A jvm options template -> (the option lines it always writes as they
    are, the names of the options its other lines set)."""
    tpl = render_template(env, series, name, ctx)[0]
    # (a stock option line on unless switched off, cassandra_jvm_removed_options: as it is while on)
    tpl = [m.group(3) if m and m.group(1) not in (ctx.get(m.group(2)) or []) else line
           for line, m in ((line, REMOVED_TOGGLE.match(line)) for line in tpl)]
    static = set(line for line in tpl if "{{" not in line and line.startswith("-") and len(line.split()) == 1)
    templated = set(jvm.option_name(EXPR.sub("", line).lstrip("#").strip()) for line in tpl if "{{" in line)
    return static, templated - {None}


def jvm_options(env, series, ctx, found, java):
    """The extra options of the files the running Java reads, found changed in place: those the file's template
    writes as they are not carried again (the options of a jvm<N> file it does not read are imported as they are).
    -> (notes, warnings)."""
    if not jvm.options_file(series, java):  # its Java not known: as read
        return [], []
    read = jvm.read_files(series, java)
    notes = []
    # an option the file's template always writes as it is, set again at another place in the file (the last
    # one counts, the same): not carried. Not one without a name (-ea, an agent) nor one that adds up: the JVM
    # gets each of them
    for name in read:
        var = jvm.options_variable(name)
        always, set_elsewhere = _template_options(env, series, ctx, name)
        names = [jvm.option_name(o.strip()) for o in found.get(var) or []]
        for line in list(found.get(var) or []):
            option = jvm.option_name(line.strip())
            if (line.strip() in always and option is not None and option not in jvm.LISTS
                    and option not in set_elsewhere and names.count(option) == 1):
                found[var] = [o for o in found[var] if o != line]
                notes.append("%s: %s, which its template writes already: not carried in %s"
                             % (name, line.strip(), var))
        if var in found and not found[var]:
            found.pop(var)
    options = [(o, "%s, from %s" % (var, f), jvm.fix_it(var)) for f, var in [(f, jvm.options_variable(f)) for f in read]
               for o in jvm.words(found.get(var))]
    return notes, jvm.set_twice(options, java) + jvm.check(options, java)


def heap_in_options(found, series, java, live_files):
    """The heap set in the JVM options files the running Java reads, an -Xms and an -Xmx line of the same size among
    their extra options (an -Xmn line too without G1; none else of the heap there): cassandra_heap_size
    (cassandra_heap_newsize) with cassandra_heap_in_jvm_options, out of the extra options -> found changed in place;
    {file: its heap options (-Xms, -Xmx, -Xmn)}, {} when nothing is moved. The caller makes sure the JVM gets the
    same where the role writes them (jvm-server.options) and cassandra-env.sh sets no heap of its own."""
    if not jvm.options_file(series, java) or found.get("cassandra_heap_size") or found.get("cassandra_heap_newsize"):
        return {}
    read = jvm.read_files(series, java)
    lines = [(jvm.options_variable(f), line) for f in read for line in found.get(jvm.options_variable(f)) or []]
    names = HEAP_OPTIONS + ("MaxHeapSize", "InitialHeapSize", "NewSize", "MaxNewSize")
    heap = [(var, line) for var, line in lines if any(jvm.option_name(o) in names for o in jvm.words(line))]
    sizes = dict((line.strip()[:4], line.strip()[4:]) for dummy, line in heap if re.match(r"^-Xm[sxn]\S+$", line.strip()))
    g1 = [o for f in read for o in jvm.words(live_files.get(f, "").split("\n")) if jvm.option_name(o) == "UseG1GC"]
    if len(heap) != len(sizes) or set(sizes) - {"-Xmn"} != {"-Xms", "-Xmx"} or sizes["-Xms"] != sizes["-Xmx"] \
            or ("-Xmn" in sizes and g1[-1:] == ["-XX:+UseG1GC"]):  # (-Xmn with G1: cassandra-env.sh stops)
        return {}
    for var, line in heap:
        found[var] = [o for o in found[var] if o != line]
        if not found[var]:
            found.pop(var)
    found["cassandra_heap_size"] = sizes["-Xmx"]
    if "-Xmn" in sizes:
        found["cassandra_heap_newsize"] = sizes["-Xmn"]
    found["cassandra_heap_in_jvm_options"] = True
    moved = {}
    for f in read:
        options = [line.strip()[:4] for var, line in heap if var == jvm.options_variable(f)]
        if options:
            moved[f] = options
    return moved


def _jmx_lines(text):
    """The lines of a JMX password/access file, without comment lines: # or !
    only at the start of a line (a # further on belongs to the password)."""
    text = re.sub(r"\\\n", " ", text or "")  # access lines go on after a backslash
    return [line.split() for line in text.split("\n") if line.strip() and not line.lstrip().startswith(("#", "!"))]


def jmx_users(password_file, access_file):
    """cassandra_jmx_users from jmxremote.password and jmxremote.access (None: no user with rights): the users of
    the password file with a plain password, then the roles with rights only (no password the role can write back:
    none, e.g. the JDK's stock controlRole, or hashed); rights other than the role's readwrite or readonly lines
    kept whole in access. What is left out (a user without rights, rights the JVM refuses): hand edits of the
    files (jmx_leftovers)."""
    rights = dict((words[0], words[1:]) for words in _jmx_lines(access_file) if len(words) >= 2)
    rights = dict((k, v) for k, v in rights.items() if JMX_RIGHTS.match(" ".join(v)))  # (a key's last line: the JVM's)
    passwords = dict((words[0], words[1:]) for words in _jmx_lines(password_file))
    users = [{"name": name, "password": words[0]} for name, words in passwords.items()
             if len(words) == 1 and name in rights]
    users += [{"name": name} for name in rights if name not in [u["name"] for u in users]]
    for user in users:
        spec = rights[user["name"]]
        if spec in (["readonly"], ["readwrite"] + JMX_CREATE_UNREGISTER):
            user["access"] = spec[0]
        elif spec == ["readwrite"]:
            user.update(access="readwrite", create_unregister=False)  # the role's readwrite line adds them
        else:
            user["access"] = " ".join(spec)  # written as it is (e.g. other create classes)
    return users or None


def jmx_leftovers(env, users, live_files):
    """What the role's JMX files would change on the node with these users (entries as the JVM reads them:
    comments, spacing, line continuations and the order of the rights aside), as hand edits of the file:
    "jmxremote.access: <role>", its line "  - " (the role's) and "  + " (the node's); passwords masked."""
    hand = []
    for name in ("jmxremote.password", "jmxremote.access"):
        with open(os.path.join(ROLE, "templates", name + ".j2")) as f:
            role = env.from_string(f.read()).render(cassandra_jmx_users=users)
        mine, node = [dict((w[0], w[1:]) for w in _jmx_lines(text)) for text in (role, live_files.get(name, ""))]
        for key in [k for k in list(node) + [k for k in mine if k not in node]
                    if sorted(mine.get(k, ["?"])) != sorted(node.get(k, ["?"]))]:
            hand.append("%s: %s" % (name, cassandra_secrets.mask(key)))
            for sign, entry in (("-", mine), ("+", node)):
                if key in entry:
                    words = [cassandra_secrets.MASK] if name.endswith(".password") else entry[key]
                    hand.append("  %s %s" % (sign, cassandra_secrets.mask(" ".join([key] + words))))
    return hand


# the JVM options naming the remote JMX files, and the ones of a JMX authentication by JAAS
_JMX_FILE_OPTION = r"-Dcom\.sun\.management\.jmxremote\.%s\.file=(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
_JAAS = re.compile(r"-D(cassandra\.jmx\.remote\.login\.config|java\.security\.auth\.login\.config"
                   r"|cassandra\.jmx\.authorizer)=")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|\S*)\s*(?:#.*)?$")
JMX_FILE_DEFAULTS = {"password": "/etc/cassandra/jmxremote.password", "access": "/etc/cassandra/jmxremote.access"}


def _shell_value(text, names):
    """A shell word with its quotes off and $NAME, ${NAME} replaced from names (not inside single quotes); None
    when one is not known."""
    if text[:1] == "'":
        return text[1:-1] if text.endswith("'") and len(text) > 1 else text[1:]
    if text[:1] == '"':
        text = text[1:-1] if text.endswith('"') and len(text) > 1 else text[1:]

    def known(match):
        name = match.group(1) or match.group(2)
        if name not in names:
            raise KeyError(name)
        return names[name]
    try:
        text = re.sub(r"\$\{(\w+)\}|\$(\w+)", known, text)
    except KeyError:
        return None
    # what only the shell knows (${X:-d}, $(..), `..`, ~): not a path
    return None if re.search(r"[$`]", text) or text.startswith("~") else text


# the stock guard of cassandra-env.sh's own CASSANDRA_LOG_DIR: the unit's wins over it
LOG_DIR_GUARD = re.compile(r'^if \[ "x\$CASSANDRA_LOG_DIR" = x \] ?; then$')  # (as env_lines writes it)


def log_dir_of(env_sh, cmdline="", environment=None, newer=()):
    """The dir the JVM gets as cassandra.logdir -> '' when not sure (then nothing is read as it).
    cmdline: the running JVM's /proc/<pid>/cmdline: its last -Dcassandra.logdir (bin/cassandra adds its own after
    JVM_OPTS), none there: not known; not used when cassandra-env.sh changed since it started (newer). Else
    cassandra-env.sh's: its last plain path set outside any block, else the unit's (environment), else the one of
    the stock guard (CASSANDRA_LOG_DIR unset)."""
    if cmdline and jvm_args.ENV not in (newer or []):
        found = [arg for arg in str(cmdline).split("\0") if arg.startswith("-Dcassandra.logdir=")]
        return found[-1][len("-Dcassandra.logdir="):] if found else ""
    try:
        lines = jvm_args.env_lines(str(env_sh or ""))
    except jvm_args.NotSame:
        return ""
    top, guarded, inside = [], [], False
    for line in lines:
        m = re.match(r"^(?:export )?CASSANDRA_LOG_DIR=(\S+)$", line["text"])
        if LOG_DIR_GUARD.match(line["text"]) and not line["depth"]:
            inside = True
            continue
        if inside and line["depth"] == 1 and re.match(r"^(else|elif)\b", line["text"]):
            return ""  # the guard with another branch: not sure
        if inside and line["depth"] == 1 and m:
            guarded.append(m.group(1))
            continue
        if not line["depth"]:
            inside = False
        if m and not line["depth"]:
            top.append(m.group(1))
        elif m or re.search(r"\bCASSANDRA_LOG_DIR=|\bunset\b.*\bCASSANDRA_LOG_DIR\b", line["text"]):
            return ""  # set in another block, set empty, unset: not sure
    value = top[-1] if top else (environment or {}).get("CASSANDRA_LOG_DIR") or (guarded[-1] if guarded else "")
    value = _shell_value(value, {}) if value else ""
    return value if value and value.startswith("/") else ""


def logback_as_log_dir(text, log_dir):
    """logback.xml with the log dir written as itself at the start of a <file> or <fileNamePattern> as
    ${cassandra.logdir} (bin/cassandra gives the JVM -Dcassandra.logdir=<it>): the same files for logback."""
    if not log_dir.startswith("/") or not log_dir.rstrip("/"):
        return text
    return re.sub(r"(<(file|fileNamePattern)>\s*)%s/" % re.escape(log_dir.rstrip("/")), r"\1${cassandra.logdir}/", text)


def _shell_code(line):
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


def cassandra_jmx_files(env_sh, options=None, cmdline="", conf_dir="", cassandra_yaml=""):
    """Where the running JVM reads its remote JMX password and access files: its own arguments (cmdline:
    /proc/<pid>/cmdline), else cassandra-env.sh (its last line that sets them, comments aside, $CASSANDRA_CONF
    and the variables it sets expanded), else the JVM options files (options: {file name: text}). A path
    cassandra-env.sh gives through what only the shell knows (a variable it does not set, ${X:-d}, $(..)) is
    looked for in the next ones.
    -> {password, access: the paths ('' when not found), from: where, unknown: what cassandra-env.sh gives that
    no source resolves ('' when nothing), jaas: what says JMX authenticates by JAAS ('' when nothing does)}.
    e.g. a cassandra-env.sh with -Dcom.sun.management.jmxremote.password.file=/etc/cassandra/jmxremote.password
    -> {"password": "/etc/cassandra/jmxremote.password", "access": "", "from": "cassandra-env.sh", "unknown": "",
    "jaas": ""}."""
    names = {"CASSANDRA_CONF": conf_dir} if conf_dir else {}
    lines = [_shell_code(line) for line in str(env_sh or "").split("\n")]
    seen = {}
    for line in lines:
        assigned = _ASSIGN.match(line)
        if assigned and assigned.group(1) != "JVM_OPTS":
            value = _shell_value(assigned.group(2), names)
            seen.setdefault(assigned.group(1), set()).add(value)
            # set twice to different values (in branches of an if): not known
            if value is not None and len(seen[assigned.group(1)]) == 1:
                names[assigned.group(1)] = value
            else:
                names.pop(assigned.group(1), None)
    # the running JVM's own arguments first: what it reads (cassandra-env.sh may set them in branches)
    sources = [("the running JVM's arguments", re.split(r"[\0\n]", str(cmdline or "")), False),
               ("cassandra-env.sh", lines, True)]
    for name, text in sorted((options or {}).items()):
        sources.append((name, [line for line in str(text or "").split("\n") if not line.lstrip().startswith("#")],
                        False))
    out = {"password": "", "access": "", "from": "", "unknown": "", "jaas": ""}
    unknown = {}
    for name, source_lines, shell in sources:
        for kind in ("password", "access"):
            value = raw = None
            for line in source_lines:
                for match in re.finditer(_JMX_FILE_OPTION % kind, line):  # the last one wins, as for the JVM
                    raw = match.group(1)
                    value = _shell_value(raw, names) if shell else raw.strip("\"'")
                    # (what a shell would run or expand, from any source: never a path the role writes)
                    value = None if value is None or re.search(r"[$`]", value) else value
            if raw is None or out[kind]:
                continue
            if value:  # each file from the first source that gives it
                out[kind] = value
                out["from"] = out["from"] or name
                unknown.pop(kind, None)
            else:
                unknown[kind] = "%s.file=%s" % (kind, raw)
        if not out["jaas"]:
            jaas = next((m.group(1) for line in source_lines for m in [_JAAS.search(line)] if m), "")
            out["jaas"] = ("-D%s in %s" % (jaas, name)) if jaas else ""
    out["unknown"] = ", ".join(unknown[k] for k in ("password", "access") if k in unknown)
    if not out["jaas"] and re.search(r"(?m)^jmx_server_options:\s*$", str(cassandra_yaml or "")) and re.search(
            r"(?m)^\s+login_config_(name|file):\s*\S", str(cassandra_yaml or "")):
        out["jaas"] = "jmx_server_options in cassandra.yaml"
    return out


def audit_file_default(found):
    """cassandra_log_audit_file's default for the values found: on with FileAuditLogger."""
    return boolean(found.get("cassandra_audit_logging_enabled", False), strict=False) and \
        str(found.get("cassandra_audit_logger_class_name", "")).rsplit(".", maxsplit=1)[-1] == "FileAuditLogger"


def jvm_moves(env, series, files, ctx, render, live_files, running, earlier=None):
    """cassandra-env.sh's hand edits moved to variables when the JVM gets the same arguments from the role's files
    (cassandra_jvm_args), and the options it does not use -> {vars, moved, rewritten: the JVM's files the role
    rewrites then, cleanup, shown: the JVM's files shown to give the same arguments (left out of the hand edits),
    not_moved: why not, '' if moved or nothing to say}; running: cassandra_config_import's, jaas: what says JMX
    authenticates by JAAS; earlier: the earlier series' stock cassandra-env.sh the node's is (earlier_env's text)."""
    dep = jvm_args.options_file(series, running.get("java"))
    if dep is None:  # the running Java not known (Cassandra not running): nothing worked out
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [], "not_moved": ""}
    jvm_files = [f for f in (jvm_args.ENV, jvm_args.COMMON, dep) if f in files]
    keep = keep_files_option(running.get("keep", False))
    kept = jvm_files if keep is True else [f for f in jvm_files if f in keep]
    if kept:  # import_keep_hand_edits: those files left as they are (said with the files kept)
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [], "not_moved": ""}

    def rendered(variables):  # (a stock line of a later release the node's file predates: left out, said apart)
        return dict((name, without_later_stock(name, "\n".join(render_template(env, series, name, variables)[1]),
                                               live_files.get(name))[0]) for name in jvm_files)
    if running.get("jaas") and jvm_args.ENV in live_files and jvm_args.ENV in jvm_files \
            and jvm_args.env_has_candidates(live_files[jvm_args.ENV], rendered(render)[jvm_args.ENV]):
        # JMX authenticated by JAAS, not imported: cassandra-env.sh to keep as it is (its note), its lines not moved
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [],
                "not_moved": "JMX authenticates by JAAS (%s), not imported" % running["jaas"]}
    cmdline = str(running.get("cmdline") or "")
    if set(running.get("newer") or []) & set(jvm_files):
        cmdline = ""  # the files changed since the JVM started: its arguments are not theirs
    adopted = None
    if cmdline and dep in files:
        adopted = jvm_args.cassandra_jvm_args_adopt(
            cmdline, live_files, rendered, render, series, running.get("java"), running.get("environment") or {},
            earlier)
    why = (adopted or {}).get("why", "")
    adopted = adopted if adopted and "vars" in adopted else None
    found = adopted["vars"] if adopted else {}
    role = rendered(dict(render, **found))
    rewritten = [f for f in jvm_files if f in live_files and not same_settings(f, live_files[f], role[f])] \
        if adopted else []
    others = dict((name, "\n".join(render_template(env, series, name, ctx)[1])) for name in files
                  if re.match(r"^jvm\d*-server\.options$", name))
    cleanup = adopted["dropped"] if adopted else []
    said = set(": ".join(line.split(": ", 2)[:2]) for line in cleanup)
    cleanup += [line for line in jvm_args.cassandra_jvm_args_cleanup(
        live_files, others, role, series, running.get("java"), cmdline, moved=bool(adopted))
        if ": ".join(line.split(": ", 2)[:2]) not in said]
    return {"vars": found, "moved": adopted["moved"] if adopted else [], "rewritten": rewritten, "cleanup": cleanup,
            "shown": [f for f in jvm_files if f not in adopted["apart"]] if adopted else [], "not_moved": why}


def _xml_text(text):
    """A value read from logback.xml (its entities decoded) as the template writes it in the XML: & and < escaped
    (/data/R&amp;D read as /data/R&D, written back as /data/R&amp;D)."""
    return text.replace("&", "&amp;").replace("<", "&lt;")


def logback(text):
    """cassandra_config's logback.xml variables as logback reads the node's file, whatever its quoting, spacing,
    attribute order or comments (a line written another way is not read by the template's): the root logger's
    level and appenders (debug.log, the console), the org.apache.cassandra logger's level. {} when not XML."""
    try:
        conf = ET.fromstring(text)
    except ET.ParseError:
        return {}
    found = {}
    roots = conf.findall("root")
    _logback_levels(conf, roots, found)
    _logback_rotation(conf, found)
    _logback_audit(conf, found)
    _logback_dirs(conf, found)
    # logback's JMX MBean, under <configuration>
    found["cassandra_log_jmx_configurator"] = conf.find("jmxConfigurator") is not None
    _logback_appenders(conf, roots, found)
    return found


def _appender(conf, name):
    """logback.xml's appender of that name (the first), None when none."""
    return next((a for a in conf.findall("appender") if a.get("name") == name), None)


def _logback_levels(conf, roots, found):
    """found with the root logger's level and appenders, the org.apache.cassandra logger's level and the lowest
    level system.log takes."""
    if len(roots) == 1:
        if roots[0].get("level"):
            found["cassandra_log_level"] = roots[0].get("level")
        refs = [ref.get("ref") for ref in roots[0].findall("appender-ref")]
        found["cassandra_debug_log_enabled"] = "ASYNCDEBUGLOG" in refs
        found["cassandra_log_console"] = "STDOUT" in refs
    loggers = [logger for logger in conf.findall("logger") if logger.get("name") == "org.apache.cassandra"]
    if len(loggers) == 1 and loggers[0].get("level"):
        found["cassandra_log_level_cassandra"] = loggers[0].get("level")
    # the lowest level system.log takes: SYSTEMLOG's one ThresholdFilter (None: not read, whatever its line said)
    systemlog = [a for a in conf.findall("appender") if a.get("name") == "SYSTEMLOG"]
    filters = [f for f in systemlog[0].findall("filter") if f.get("class") == "ch.qos.logback.classic.filter.ThresholdFilter"] \
        if len(systemlog) == 1 else []
    level = (filters[0].findtext("level") or "").strip() if len(filters) == 1 and len(list(filters[0])) == 1 else ""
    found["cassandra_log_system_level"] = level if level.upper() in LOG_LEVELS else None


def _logback_rotation(conf, found):
    """found with the rotation of each file, by its appender (live: a commented-out AUDIT's values are not its own;
    None: not read, whatever a line said)."""
    for name, kind in (("SYSTEMLOG", "system"), ("DEBUGLOG", "debug"), ("AUDIT", "audit")):
        policy = next((a.find("rollingPolicy") for a in conf.findall("appender") if a.get("name") == name), None)
        for tag, var in (("maxFileSize", "max_file_size"), ("maxHistory", "max_history"), ("totalSizeCap", "total_size_cap")):
            key = "cassandra_log_%s_%s" % (kind, var)
            text = (policy.findtext(tag) or "").strip() if policy is not None else ""
            found[key] = (int(text) if text.isdigit() and var == "max_history" else text) if text else None


def _logback_audit(conf, found):
    """found with the audit file: the stock AUDIT appender and the audit logger naming it, both live; the dir of its
    file, the same for audit.log and its rolled files (the stock names), zipped or not."""
    audit = [lg for lg in conf.findall("logger") if lg.get("name") == "org.apache.cassandra.audit"]
    found["cassandra_log_audit_file"] = bool(
        [a for a in conf.findall("appender") if a.get("name") == "AUDIT"]
        and audit and "AUDIT" in [r.get("ref") for r in audit[0].findall("appender-ref")])
    appender = _appender(conf, "AUDIT")
    if appender is not None:
        path = (appender.findtext("file") or "").strip()
        pattern = (appender.findtext("rollingPolicy/fileNamePattern") or "").strip()
        if path.endswith("/audit.log") and pattern in (path + ROLLED, path + ROLLED + ".zip"):
            found["cassandra_log_audit_dir"] = _xml_text(path[:-len("/audit.log")])
            found["cassandra_log_audit_compress"] = pattern.endswith(".zip")


def _logback_dirs(conf, found):
    """found with system.log's and debug.log's dir, the same for the file and its rolled files (else a hand edit);
    their rolled files (their stock names), zipped or not: one variable for both, read when they agree (else the line
    that differs: a hand edit)."""
    zipped = set()
    for name, kind in (("SYSTEMLOG", "system"), ("DEBUGLOG", "debug")):
        appender = _appender(conf, name)
        if appender is None:
            continue
        path = (appender.findtext("file") or "").strip()
        pattern = (appender.findtext("rollingPolicy/fileNamePattern") or "").strip()
        if path.endswith("/%s.log" % kind) and pattern in (path + ROLLED, path + ROLLED + ".zip"):
            found["cassandra_log_%s_dir" % kind] = _xml_text(path[:-len("/%s.log" % kind)])
            zipped.add(pattern.endswith(".zip"))
        else:  # (None: not read, whatever its <file> line said)
            found["cassandra_log_%s_dir" % kind] = None
    if len(zipped) == 1:
        found["cassandra_log_compress"] = zipped.pop()


def _logback_appenders(conf, roots, found):
    """found with 5.0's system_views.system_logs table, the appenders <root> names besides the stock ones, and the
    simple loggers' levels."""
    refs = [r.get("ref") for r in roots[0].findall("appender-ref")] if len(roots) == 1 else []
    # 5.0's system_views.system_logs table: the stock CQLLOG appender live, <root> naming it
    cql = "CQLLOG" in refs and [a for a in conf.findall("appender") if a.get("name") == "CQLLOG"]
    # (the appender alone gets no event: not the table, though the line reader took its line on for it; its level
    # then has no effect)
    found["cassandra_log_cql_table"] = True if cql else None
    if not cql:
        found["cassandra_log_cql_table_level"] = None
    if len(roots) == 1:  # the appenders <root> names besides the stock ones, in their order
        extra = [ref for ref in refs if ref not in ("SYSTEMLOG", "STDOUT", "ASYNCDEBUGLOG") + (("CQLLOG",) if cql else ())]
        if extra and len(set(extra)) == len(extra):
            found["cassandra_logback_root_extra_refs"] = extra
    # the loggers that set a level only (no other attribute, nothing in them), each named once, not one a variable of
    # its own sets; a level of logback's (not a property: ${LVL:-INFO}), a name the role writes as it is
    simple = [lg for lg in conf.findall("logger") if set(lg.attrib) == {"name", "level"} and not list(lg)
              and lg.get("name") not in LOGBACK_OWN_LOGGERS and str(lg.get("name")).upper() != "ROOT"
              and str(lg.get("level")).upper() in LOG_LEVELS and not re.search(r"[\"'<>&]", lg.get("name"))]
    names = [lg.get("name") for lg in conf.findall("logger")]
    levels = dict((lg.get("name"), lg.get("level")) for lg in simple if names.count(lg.get("name")) == 1)
    if levels:
        found["cassandra_log_levels"] = levels


# the end of a rolled log file's stock name (fileNamePattern), before its .zip
ROLLED = ".%d{yyyy-MM-dd}.%i"
# logback's levels (cassandra_log_levels takes only these)
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "OFF", "ALL")
# the loggers of logback.xml a variable of its own sets (cassandra_log_levels may not take them)
LOGBACK_OWN_LOGGERS = ("ROOT", "org.apache.cassandra", "org.apache.cassandra.audit")
SIMPLE_LOGGER = re.compile(r'^\s*<logger\s+name="([^"]+)"\s+level="([^"]+)"\s*/>\s*$')


def logback_carried(env, series, ctx, found, text):
    """logback.xml's variables found (logback) taken from the lines the import carries too: the simple loggers of
    cassandra_log_levels out of the end lines, the node's whole <appender> elements before <root> (not a stock one)
    in APPENDERS (its snippet logback.xml:appenders) when nothing else keeps them -> (found (a copy), notes for
    the report)."""
    found = dict(found)
    levels = found.get("cassandra_log_levels") or {}
    end = [line for line in found.get(LOGBACK_END) or []
           if not (SIMPLE_LOGGER.match(line) and SIMPLE_LOGGER.match(line).group(1) in levels)]
    if end:
        found[LOGBACK_END] = end
    else:
        found.pop(LOGBACK_END, None)
    blocks = _appender_blocks(text)
    if blocks:  # (a secret in them: to secrets.yml, as for the end lines)
        found[APPENDERS] = blocks
    # <root>'s refs only to appenders the inventory then defines (else a hand edit, cassandra_config would refuse them)
    defined = list(ROOT_APPENDERS) + (["AUDIT"] if found.get("cassandra_log_audit_file") else []) + re.findall(
        r'<appender\s[^>]*?\bname\s*=\s*["\']([^"\']+)["\']', "\n".join(found.get(APPENDERS) or []))
    if [ref for ref in found.get("cassandra_logback_root_extra_refs") or [] if ref not in defined]:
        found.pop("cassandra_logback_root_extra_refs")
    # one pattern for the stock appenders' encoders: read only when the live ones agree (else the lines that
    # differ: hand edits)
    try:
        patterns = set((e.text or "").strip() for a in ET.fromstring(text) if _tag(a) == "appender"
                       and a.get("name") in ("SYSTEMLOG", "DEBUGLOG", "STDOUT", "AUDIT")
                       for e in a.iter() if _tag(e) == "pattern")
    except ET.ParseError:
        patterns = set()
    if len(patterns) > 1:
        found.pop("cassandra_log_pattern", None)
    # a pattern with -- while the audit appender is commented out: the template writes it in that comment, where
    # XML does not take it (cassandra_config refuses it), so not carried (the lines that differ: hand edits)
    notes = []
    audit = found["cassandra_log_audit_file"] if "cassandra_log_audit_file" in found else audit_file_default(found)
    if "--" in str(found.get("cassandra_log_pattern", "")) and not audit:
        notes.append("logback.xml: its log pattern (%s) has a --, which the role's template would write in the"
                     " audit appender it leaves commented out (cassandra_log_audit_file: false), where XML does not"
                     " take it: not carried in cassandra_log_pattern. Write the pattern without -- on the nodes and"
                     " import again, or keep logback.xml as it is (-e import_keep_hand_edits=logback.xml)"
                     % cassandra_secrets.mask(found.pop("cassandra_log_pattern")))
    return found, notes


def _tag(el):
    return str(el.tag).rsplit("}", 1)[-1].lower()


# the appenders of the stock logback.xml <root> may name (their stock definitions, written by the role)
ROOT_APPENDERS = ("SYSTEMLOG", "DEBUGLOG", "ASYNCDEBUGLOG", "STDOUT")
# the stock ones a variable writes (the others, LogbackMetrics: as the node's own appenders)
STOCK_APPENDERS = ("SYSTEMLOG", "DEBUGLOG", "ASYNCDEBUGLOG", "STDOUT", "AUDIT", "CQLLOG")


def _appender_blocks(text):
    """The lines of the node's whole <appender> elements before <root>, other than the stock ones, each on lines of
    its own, in their order -> [line]; [] when there are none, or one shares a line with another element."""
    lines = text.split("\n")
    parser = expat.ParserCreate()
    depth, opened, spans, root = [0], [], [], []

    def start(tag, attrs):
        depth[0] += 1
        if depth[0] == 2:
            opened.append((tag, attrs.get("name"), parser.CurrentLineNumber - 1, parser.CurrentColumnNumber))
            if tag.lower() == "root" and not root:
                root.append(parser.CurrentLineNumber - 1)

    def end(tag):
        if depth[0] == 2:
            line, col = parser.CurrentLineNumber - 1, parser.CurrentColumnNumber
            close = re.match(r"</\s*%s\s*>" % re.escape(tag), lines[line][col:])  # (an empty one: after it already)
            spans.append(opened.pop() + (line, col + close.end() if close else col))
        depth[0] -= 1

    parser.StartElementHandler, parser.EndElementHandler = start, end
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        return []
    out = []
    for tag, name, first, col, last, after in spans:
        if tag.lower() != "appender" or name in STOCK_APPENDERS or not root or first >= root[0]:
            continue
        if lines[first][:col].strip() or lines[last][after:].strip():
            return []
        out += lines[first:last + 1]
    return out
