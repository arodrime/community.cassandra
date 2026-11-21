# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The JVM options checks of preflight and of a Java change (module_utils
cassandra_jvm_options: the table of checks).

cassandra_jvm_option_checks: a node's extra options variables, its series and
    Java -> the warnings on the options that Java gets from the files the role
    writes (refusals=true: the options a move to that Java is refused with).
cassandra_jvm_options_file: series, Java -> the jvm<N>-server.options that
    Java reads besides jvm-server.options.
cassandra_jvm_options_variable: an options file -> its extra options variable.
cassandra_jvm_switch_refusals: a node's variables, the series and Java it
    moves to and the ones it runs -> why the move is refused (cassandra-env.sh
    would stop, as its stock_options says; a second exporter agent).
cassandra_jvm_switch_heap: the same -> the heap after the move when the new
    Java's options file gives another one (a line of fact for the plan)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import functools
import os
import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_java import cassandra_java_supported
from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_args import (
    STOPS_HEAP_PAIR, STOPS_XMN_XMX, STOPS_XMX_PAIR, stock_options)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import NBSP
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import mask

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config", "templates")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# cassandra_install's cassandra_java_versions: the Java when none is given
DEFAULT_JAVA = {"40x": "11", "41x": "11", "50x": "17"}


# a stock option line the role writes unless its file's cassandra_jvm<N>_removed_options lists it
REMOVABLE = re.compile(r"^\{\{ '#' if '([^']*)' in cassandra_jvm\d*_removed_options else '' \}\}(.*)$")


@functools.lru_cache(maxsize=None)
def _template_options(series, name):
    """The option lines the role's template of a file always writes, but the ones a variable switches off:
    ((option, its stock line), ...)."""
    path = os.path.join(TEMPLATES, SERIES.get(series, ""), name + ".j2")
    if series not in SERIES or not os.path.exists(path):
        return ()
    out = []
    with open(path) as f:
        for line in f.read().split("\n"):
            m = REMOVABLE.match(line)
            if m or "{{" not in line:
                out += [(o, m.group(1) if m else None) for o in jvm.words([m.group(2) if m else line])]
    return tuple(out)


def _removed_variable(name):
    """jvm11-server.options -> cassandra_jvm11_removed_options."""
    return re.sub(r"_extra_options$", "_removed_options", jvm.options_variable(name))


def cassandra_jvm_option_checks(variables, series, java="", refusals=False, kept=None):
    """variables: {cassandra_jvm_extra_options: [...], cassandra_jvm<N>_extra_options: [...]}; java: the Java
    major the node runs or moves to ('': the series' default); kept: cassandra_config_keep_files, the files
    the role leaves as they are (neither its lines nor the variable's in them) -> [message]."""
    series = str(series)
    java = str(java or DEFAULT_JAVA.get(series, ""))
    options = []
    for name in jvm.read_files(series, java):  # each file's own lines, then its extra options
        var = jvm.options_variable(name)
        if name in (kept or []):  # the node's own file, which the role leaves as it is: not known here
            continue
        removed = list((variables or {}).get(_removed_variable(name)) or [])
        stock = ("The role writes it as Cassandra %s's stock file does, for Java %s: set cassandra_java_version to one"
                 " of them, or switch it off (%s)" % (SERIES.get(series, series),
                                                      " or ".join(cassandra_java_supported(series)), _removed_variable(name)))
        options += [(o, "the role's %s" % name, stock) for o, line in _template_options(series, name) if line not in removed]
        options += [(o, var, jvm.remove_it(var) if refusals else jvm.fix_it(var))
                    for o in jvm.words((variables or {}).get(var) or [])]
    return jvm.check(options, java, refusals=bool(refusals))


def cassandra_jvm_options_file(series, java):
    """The jvm<N>-server.options a Java reads besides jvm-server.options, '' when not known: "50x", "17" ->
    "jvm17-server.options"."""
    return jvm.options_file(str(series), java)


def cassandra_jvm_options_variable(name):
    """jvm11-server.options -> cassandra_jvm11_extra_options."""
    return jvm.options_variable(str(name))


# what cassandra-env.sh greps for in JVM_OPTS that decides whether it starts (its heap and GC checks)
SWITCH_GREPS = ("Xmx", "Xms", "Xmn", "+UseG1GC", "UseConcMarkSweepGC")
# the heap options, as the JVM takes them (jvm.option_name): the heap, the young generation
MAX_HEAP, YOUNG = ("-Xmx", "-Xms"), ("-Xmn", "MaxNewSize")
# the files of the node cassandra-env.sh's checks read that cassandra_config may keep as they are (the role's model of
# them is then the node's own text): jvm-server.options, the jvm<N>-server.options of the Java, cassandra-env.sh
KEPT = "%s (kept as it is: cassandra_config_keep_files)"


def _gc(variables, series, number):
    """The GC the role writes in jvm<number>-server.options (cassandra_jvm<N>_gc, cassandra_jvm_gc, the series')."""
    return str(variables.get("cassandra_jvm%s_gc" % number) or variables.get("cassandra_jvm_gc")
               or ("CMS" if series in ("40x", "41x") else "G1"))


def _kept_texts(kept):
    """slurp's results of the kept files ({item, content} each; skipped: none) -> ({file: text, None when it could not
    be read}, {file: slurp's message when it could not be read}). A file not on the node is not kept: the role writes
    its own there (cassandra_config_keep_files keeps the files a node has)."""
    texts, errors = {}, {}
    for result in kept or []:
        if (isinstance(result, dict) and re.match(r"^(cassandra-env\.sh|jvm\d*-server\.options)$", str(result.get("item")))
                and not result.get("skipped")):
            if "content" not in result and re.search(r"(?i)not found|no such file|does not exist", str(result.get("msg"))):
                continue
            if "content" in result:
                texts[result["item"]] = base64.b64decode(result["content"]).decode("utf-8", "replace")
            else:
                texts[result["item"]] = None
                errors[result["item"]] = str(result.get("msg") or "slurp gave no content")
    return texts, errors


ENV_ASSIGN = re.compile(r"^(?:export\s+)?(MAX_HEAP_SIZE|HEAP_NEWSIZE|JVM_OPTS)=(.*)$")
# one of them named as a word (not read: $NAME, ${NAME}), or set by a ${NAME:=...} expansion
ENV_NAMED = re.compile(r"(?<![\w$])(?<!\$\{)(MAX_HEAP_SIZE|HEAP_NEWSIZE|JVM_OPTS)(?!\w)|\$\{(MAX_HEAP_SIZE|HEAP_NEWSIZE|JVM_OPTS):?=")
ENV_OPENS = ("if", "case", "for", "while", "until", "select")
ENV_CLOSES = ("fi", "esac", "done")
# commands that read them only
ENV_READERS = ("echo", "printf", "[", "test", "export")


def _env_scan(line):
    """A line of cassandra-env.sh as the shell reads it: (its commands outside quotes and comments [(the command,
    conditional: after && or ||)], the keywords then/do/else/elif and ; | & as separators; the quote left open at its
    end, or None). A backslash escapes the next character outside single quotes."""
    out, word, quote, conditional = [], "", None, False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'" and i + 1 < len(line):
            word += line[i:i + 2]
            i += 2
            continue
        if quote:
            quote = None if ch == quote else quote
            word += ch
        elif ch in "\"'":
            quote, word = ch, word + ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;"):
            break
        elif line.startswith(("&&", "||"), i) or ch in ";|&":
            out.append((word.strip(), conditional))
            conditional = line.startswith(("&&", "||"), i)
            word = ""
            i += 1 if line.startswith(("&&", "||"), i) else 0
        else:
            word += ch
        i += 1
    out.append((word.strip(), conditional))
    commands = []
    for command, cond in out:
        while True:  # (then MAX_HEAP_SIZE=8G: the keyword, then the command)
            m = re.match(r"^(then|do|else|elif|\{)\s+(.*)$", command)
            if not m:
                break
            commands.append((m.group(1), cond))
            command = m.group(2)
        if command:
            commands.append((command, cond))
    return commands, quote


def _env_unknown(name, value):
    """Whether an assignment's value is one the check cannot know: a variable or a command in it (JVM_OPTS: one
    besides its own $JVM_OPTS, or in an option's name), a word after it (a command it prefixes)."""
    if name != "JVM_OPTS":
        return bool(re.search(r"[$`]", value)) or bool(re.search(r"\s", value.strip("\"'"))) and value[:1] not in "\"'"
    if value[:1] not in "\"'" and re.search(r"\s", value):
        return True
    for word in re.split(r"[\s\"']+", value):
        if word and word not in ("$JVM_OPTS", "${JVM_OPTS}") and (
                word.startswith(("$", "`")) or "$" in re.split(r"[:=]", word)[0] or "`" in word):
            return True
    return False


def _env_heap(text):
    """A cassandra-env.sh as it runs before its heap checks, followed only where it can be: {heap: the MAX_HEAP_SIZE
    and HEAP_NEWSIZE it sets before it tests them ("x$MAX_HEAP_SIZE"), words: the options its JVM_OPTS assignments add
    before its first grep of Xmn, later: the ones it adds after (the JVM takes the last -Xmx...; not ones made of its
    variables), unfollowed: [(what, line number)] of what it cannot follow there}.
    Followed: plain assignments of them at the top level (export too, several commands on a line with ; then do...),
    their reads ($NAME in tests, echo, the greps), the assignments in a function body that is not called before the
    checks, a condition's JVM_OPTS assignment without the letters it greps (Xmx, Xms, Xmn, the GCs). Not followed
    (unfollowed): an assignment of them inside a condition (if, case, a loop, a brace group, after && or ||), one
    whose value holds a variable or a command (JVM_OPTS: other than its own), a mention of them in any other form
    (readonly, eval, unset, +=, ${NAME:=...}, a case arm...), a line continued with a backslash or an open quote that
    names them, a here-document that names them, a file it sources (. or source), a call of a function that sets them,
    the function keyword (dash, the sh of Debian, stops on it), a block left open at the end of the file."""
    heap = {"MAX_HEAP_SIZE": None, "HEAP_NEWSIZE": None}
    words, later, unfollowed = [], [], []
    greps = tested = False
    stack, functions, setters, heredoc = [], [], set(), None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        number, line = i + 1, lines[i]
        i += 1
        # (the line of a check is the check's: from it on, what follows is not before the checks)
        greps = greps or bool(re.search(r"grep -q\w* +[\"']?Xmn", line))
        tested = tested or '"x$MAX_HEAP_SIZE"' in line
        region = {"JVM_OPTS": not greps, "MAX_HEAP_SIZE": not tested, "HEAP_NEWSIZE": not tested}

        def named(code):
            return [m.group(1) or m.group(2) for m in ENV_NAMED.finditer(code)
                    if region[m.group(1) or m.group(2)]]
        if heredoc is not None:  # (its body: text, not commands)
            if line.strip() == heredoc:
                heredoc = None
            elif named(line):
                unfollowed.append(("a here-document", number))
            continue
        commands, quote = _env_scan(line)
        joined = False
        while (quote or re.search(r"(?<!\\)(\\\\)*\\$", line)) and i < len(lines):  # (one logical line)
            line = (line[:-1] if not quote else line + "\n") + lines[i]
            i += 1
            joined = True
            commands, quote = _env_scan(line)
        code = " ; ".join(c for c, dummy in commands)
        if (joined or "\r" in line) and named(code):
            unfollowed.append((named(code)[0], number))
            continue
        doc = re.search(r"<<-?\s*[\"']?(\w+)", code)
        if doc:
            heredoc = doc.group(1)
        for command, conditional in commands:
            parts = command.split()
            first = parts[0] if parts else ""
            header = re.match(r"^(?:function\s+)?([\w-]+)\s*\(\)\s*(\{)?\s*(.*)$", command) or \
                re.match(r"^function\s+([\w-]+)\s*(\{)?\s*(.*)$", command)
            if header and first == "function" and any(region.values()):  # (bash and ksh only: dash stops on it)
                unfollowed.append(("the function keyword (not every sh has it)", number))
            if header:
                functions.append(header.group(1))
                stack.append("func" if header.group(2) else "func-pending")
                command = header.group(3)
                parts = command.split()
                first = parts[0] if parts else ""
                if not command:
                    continue
            if first == "{":
                if stack and stack[-1] == "func-pending":
                    stack[-1] = "func"
                else:
                    stack.append("brace")
                continue
            if first == "}" or command.startswith("}"):
                if stack:
                    if stack.pop().startswith("func") and functions:
                        functions.pop()
                continue
            if first in ENV_OPENS:
                stack.append("ctl")
            elif first in ENV_CLOSES and stack and stack[-1] == "ctl":
                stack.pop()
            if "func" in stack:  # (its body runs when it is called: who sets them)
                if ENV_NAMED.search(command):
                    setters.add(functions[-1] if functions else "")
                continue
            in_region = any(region.values())
            if in_region and (first in (".", "source", "eval") or first in setters):
                unfollowed.append(({".": "a file it sources", "source": "a file it sources", "eval": "eval"}.get(
                    first, "%s (a function that sets them)" % first), number))
                continue
            m = ENV_ASSIGN.match(command)
            if m:
                name, value = m.group(1), m.group(2).strip()
                inside = bool(stack) or conditional
                if name == "JVM_OPTS":
                    options = [w for w in re.split(r"[\s\"']+", value) if w.startswith("-")]
                    if not region[name]:
                        later += [w for w in options if "$" not in w]
                    elif _env_unknown(name, value):
                        unfollowed.append((name, number))
                    elif inside and any(p in w for w in options for p in SWITCH_GREPS):
                        unfollowed.append((name, number))
                    elif not inside:
                        words += options
                elif region[name]:
                    if inside or _env_unknown(name, value):
                        unfollowed.append((name, number))
                    else:
                        heap[name] = value.strip("\"'") or None
                continue
            if first not in ENV_READERS and named(command):
                unfollowed.append((named(command)[0], number))
            elif first == "export" and re.search(r"\$\{\w+:?=", command) and named(command):
                unfollowed.append((named(command)[0], number))
    if stack:
        unfollowed.append(("a block left open (if, case, a loop, a function)", len(lines)))
    return {"heap": heap, "words": words, "later": later, "unfollowed": unfollowed}


def _reads(variables, series, java, kept=None):
    """What cassandra-env.sh greps in JVM_OPTS when the node runs that Java with the role's files, and its heap:
    the options file's name, its variable, its extra options, the GC, the sources of the options ([(what holds
    them: a variable, a kept file, or "" for the role's own lines, [option])]), {MAX_HEAP_SIZE, HEAP_NEWSIZE};
    None when the file is not known. kept: _kept_texts' texts."""
    name = jvm.options_file(series, java)
    if not name:
        return None
    kept = kept or {}
    var = jvm.options_variable(name)
    extra = jvm.words(variables.get(var) or [])
    heap_size, newsize = str(variables.get("cassandra_heap_size") or ""), str(variables.get("cassandra_heap_newsize") or "")
    in_options = boolean(variables.get("cassandra_heap_in_jvm_options", False), strict=False)
    gc = _gc(variables, series, name[len("jvm"):-len("-server.options")])
    sources = []
    if "jvm-server.options" in kept:
        sources.append((KEPT % "jvm-server.options", jvm.words((kept["jvm-server.options"] or "").split("\n"))))
    else:
        sources.append(("", list(_template_options(series, "jvm-server.options"))))
        if in_options and heap_size:  # (the role writes them in jvm-server.options, before the extra options)
            sources.append(("cassandra_heap_size", ["-Xms" + heap_size, "-Xmx" + heap_size]))
            sources.append(("cassandra_heap_newsize", ["-Xmn" + newsize] if newsize else []))
        sources.append(("cassandra_jvm_extra_options", jvm.words(variables.get("cassandra_jvm_extra_options") or [])))
    if name in kept:  # (the node's own file of that Java: its options, its GC lines)
        extra = jvm.words((kept[name] or "").split("\n"))
        sources.append((KEPT % name, extra))
    else:
        sources.append(("", list(_template_options(series, name))))
        number = name[len("jvm"):-len("-server.options")]
        gc_var = "cassandra_jvm%s_gc" % number
        if not variables.get(gc_var) and not variables.get("cassandra_jvm_gc"):
            gc_var += " (default %s)" % gc
        sources.append((gc_var, {"G1": ["-XX:+UseG1GC"], "CMS": ["-XX:+UseConcMarkSweepGC"]}.get(gc, [])))  # (custom: extras)
        sources.append((var, extra))
    later, unfollowed = [], []
    if "cassandra-env.sh" in kept:
        env = _env_heap(kept["cassandra-env.sh"] or "")
        heap, later, unfollowed = env["heap"], [(KEPT % "cassandra-env.sh", env["later"])], env["unfollowed"]
        sources.append((KEPT % "cassandra-env.sh", env["words"]))
    else:
        heap = {"MAX_HEAP_SIZE": heap_size if heap_size and not in_options else None,
                "HEAP_NEWSIZE": newsize if newsize and not in_options else None}
    return {"file": name, "var": KEPT % name if name in kept else var, "extra": extra, "sources": sources,
            "heap": heap, "gc": gc, "later": later, "unfollowed": unfollowed}


def _same(variables, series, java, now_series, now_java):
    """Whether the node keeps its options file and its GC (now_java '': not known, then not)."""
    after, before = _reads(variables, series, java), _reads(variables, now_series or series, now_java) if now_java else None
    return before is not None and after is not None and (before["file"], before["gc"]) == (after["file"], after["gc"])


def _holders(sources, pattern):
    """What holds the options cassandra-env.sh finds pattern in (its grep: the letters anywhere in them)."""
    return [what for what, words in sources if what and any(pattern in w for w in words)]


def _stop_text(reason, reads):
    """Why cassandra-env.sh stops, naming what holds the options, and the way out."""
    def held(*patterns):
        return " and ".join(sorted(set(h for p in patterns for h in _holders(reads["sources"], p)))) or "the options"
    if reason == STOPS_HEAP_PAIR:
        kept_env = KEPT % "cassandra-env.sh" in [w for w, dummy in reads["sources"]]
        one, other = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE") if reads["heap"]["MAX_HEAP_SIZE"] else ("HEAP_NEWSIZE", "MAX_HEAP_SIZE")
        names = {"MAX_HEAP_SIZE": "cassandra_heap_size", "HEAP_NEWSIZE": "cassandra_heap_newsize"}
        where = "in %s" % (KEPT % "cassandra-env.sh") if kept_env else "%s without %s" % (names[one], names[other])
        return ("cassandra-env.sh stops: %s without %s, the GC not G1 (%s). Set both, or neither" % (one, other, where))
    if reason == STOPS_XMX_PAIR:
        return ("cassandra-env.sh stops: it finds one of Xmx and Xms in the options and not the other (in %s; it reads"
                " the letters: -XX:MaxHeapSize is no Xmx). Give both -Xms and -Xmx there, or neither" % held("Xmx", "Xms"))
    if reason == STOPS_XMN_XMX:
        return ("cassandra-env.sh stops: -Xmn without -Xmx (in %s). Give -Xms and -Xmx with it there, or remove -Xmn"
                % held("Xmn"))
    return ("cassandra-env.sh stops: -Xmn under G1 (-Xmn in %s; G1 from %s). Remove -Xmn there (G1 sizes the young"
            " generation itself; -XX:MaxNewSize is not refused)" % (held("Xmn"), held("+UseG1GC")))


def cassandra_jvm_switch_refusals(variables, series, java, now_series="", now_java="", kept=None):
    """variables: the node's (cassandra_jvm*_extra_options, the GC ones, the heap ones, cassandra_config_keep_files,
    cassandra_prometheus_exporter_enabled); series, java: what the node moves to; now_series, now_java: what it runs
    (now_java '': not known); kept: slurp's results of its kept jvm-server.options and cassandra-env.sh -> [message]
    when cassandra-env.sh would stop with what the new Java reads (its stock_options, fed with every option of the
    files the node then has), or the options file of that Java holds an exporter agent the role writes already
    (loaded twice); [] when the node keeps its file and its GC.
    Example: {"cassandra_jvm11_extra_options": ["-Xmn800M"]}, "41x", "11", "41x", "8" -> ["Java 11 reads
    jvm11-server.options: cassandra-env.sh stops: -Xmn without -Xmx (in cassandra_jvm11_extra_options). ..."]."""
    variables, series = variables or {}, str(series)
    texts, errors = _kept_texts(kept)
    reads = _reads(variables, series, java, texts)
    if reads is None or _same(variables, series, java, str(now_series or series), now_java):
        return []
    where = "Java %s reads %s: " % (jvm.java_major(java), reads["file"])
    if errors:
        return [where + "%s: not read (%s): what cassandra-env.sh finds is not checked. Make it readable by root, then"
                " run again." % (KEPT % f, errors[f]) for f in sorted(errors)]
    if reads["unfollowed"]:
        return [where + "%s, line %d: %s in a form this check cannot follow (a condition, a function called, another"
                " file...). Check that line by hand; for the check to follow it, set it with a plain assignment at the"
                " start of a line (or take cassandra-env.sh out of cassandra_config_keep_files: the role writes its"
                " own), then run again." % (KEPT % "cassandra-env.sh", number,
                                            "sets " + what if re.match(r"^[A-Z_]+$", what) else what)
                for what, number in reads["unfollowed"]]
    words = [w for dummy, ws in reads["sources"] for w in ws]
    why = []
    out = []
    if stock_options(dict((p, any(p in w for w in words)) for p in SWITCH_GREPS), reads["heap"], why) is None:
        out.append(where + _stop_text(why[0], reads) + ", then run again.")
    if boolean(variables.get("cassandra_prometheus_exporter_enabled", False), strict=False):
        for option in reads["extra"]:
            if "jmx_prometheus_javaagent" in option:
                out.append(where + "%s is a Prometheus exporter agent, which the role writes already: the JVM would"
                           " load two (the second exits on its port). Remove it from %s, then run again."
                           % (mask(option), reads["var"]))
    return out


def _named(what):
    """A source as a heap line names it: a kept file "<file>, kept as it is" (no parentheses in parentheses)."""
    m = re.match(r"^(.*) \(kept as it is: cassandra_config_keep_files\)$", what)
    return "%s, kept as it is" % m.group(1) if m else what


def _size_text(value, fallback):
    """A size as the line shows it, its number never apart from its unit (a no-break space)."""
    return jvm.shown_size(value).replace(" ", NBSP) if value else fallback


def _heap_of(sources, size_value, size_name, names):
    """The heap (names: MAX_HEAP or YOUNG) a node gets: the last of these options in the sources, as the JVM takes
    them (cassandra-env.sh adds its own only without them), else size_value (cassandra_heap_size,
    cassandra_heap_newsize, or the kept cassandra-env.sh's) -> (bytes or None, what it shows: the size and where it
    comes from, from a kept file or not)."""
    found = [(what, o) for what, words in sources for o in words if what and jvm.option_name(o) in names]
    if found:
        what, last = found[-1]
        value = jvm.size(last) if jvm.option_name(last) != "MaxNewSize" else jvm.size("-Xmn" + last.split("=", 1)[1])
        options = " ".join(o for w, o in found if w == what)
        return value, "%s (%s: %s)" % (_size_text(value, last), _named(what), options), "kept as it is" in what
    if size_value:
        value = jvm.size("-Xmx" + size_value)
        return value, "%s (%s)" % (_size_text(value, size_value), _named(size_name)), "kept as it is" in size_name
    return None, "cassandra-env.sh's own sizing", False


def cassandra_jvm_switch_heap(variables, series, java, now_series="", now_java="", kept=None, move="the move"):
    """The heap of a node after the move when the options file it then reads gives another one than the file it reads
    now (one there, or none there while the old one had one) -> the lines of fact for the plan, the heap and the
    young generation apart; [] for none (the same file, the same heap). Sizes from the inventory's variables and the
    files the role keeps as they are (kept: slurp's results, as cassandra_jvm_switch_refusals'; "node's file now"
    when the size now comes from one); the options cassandra-env.sh adds after its checks too (the last one wins).
    move: what the line calls it (the upgrade).
    Example: {"cassandra_heap_size": "31G", "cassandra_jvm11_extra_options": ["-Xms24G", "-Xmx24G"]}, "50x", "11",
    "50x", "17" -> ["heap after the move: 24 GiB (cassandra_jvm11_extra_options: -Xms24G -Xmx24G); inventory now:
    31 GiB (cassandra_heap_size)"] (no-break spaces in the sizes)."""
    variables, series = variables or {}, str(series)
    texts = _kept_texts(kept)[0]
    after = _reads(variables, series, java, texts)
    before = _reads(variables, str(now_series or series), now_java, texts) if now_java else None
    if after is None or before is not None and before["file"] == after["file"]:
        return []
    lines = []
    for names, size_name, what, env_name in ((MAX_HEAP, "cassandra_heap_size", "heap", "MAX_HEAP_SIZE"),
                                             (YOUNG, "cassandra_heap_newsize", "young generation", "HEAP_NEWSIZE")):
        # cassandra-env.sh's size: the inventory's (written there unless cassandra_heap_in_jvm_options), or the kept
        # file's own
        size_value = str(after["heap"][env_name] or "")
        if "cassandra-env.sh" in texts:
            size_name = "%s in %s" % (env_name, KEPT % "cassandra-env.sh")
        if not any(jvm.option_name(o) in names for o in after["extra"] + (before["extra"] if before else [])):
            continue  # (neither file sets it: the move does not change it)
        will = _heap_of(after["sources"] + after["later"], size_value, size_name, names)
        if before is None:  # (the Java it runs not read: the file it reads now neither)
            lines.append("%s after %s: %s; now: not known (the Java the node runs was not read)" % (what, move, will[1]))
            continue
        was = _heap_of(before["sources"] + before["later"], size_value, size_name, names)
        if was[0] != will[0] or was[0] is None and was[1] != will[1]:
            lines.append("%s after %s: %s; %s now: %s" % (what, move, will[1],
                                                          "node's file" if was[2] else "inventory", was[1]))
    return lines


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_jvm_option_checks": cassandra_jvm_option_checks,
            "cassandra_jvm_options_file": cassandra_jvm_options_file,
            "cassandra_jvm_options_variable": cassandra_jvm_options_variable,
            "cassandra_jvm_switch_heap": cassandra_jvm_switch_heap,
            "cassandra_jvm_switch_refusals": cassandra_jvm_switch_refusals,
        }
