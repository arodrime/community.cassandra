# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The JVM arguments Cassandra starts with, worked out from its config files:
what import_cluster needs to move a hand edit of cassandra-env.sh (a line
adding options to JVM_OPTS, the heap set elsewhere in the file) into the
variables of cassandra_config, and to prove the JVM gets the same arguments
from the files the role writes.

Nothing of the node runs here: the running JVM's arguments (its
/proc/<pid>/cmdline) are the start, and the difference between the node's
files and the role's is applied to them:
- the options files the running Java reads (jvm-server.options, then
  jvm<N>-server.options) come in as the words of their lines starting with
  '-' (cassandra.in.sh): the node's words, found in the arguments, are
  replaced by the role's;
- cassandra-env.sh may differ only by lines outside any block adding plain
  options to JVM_OPTS (they go to the options files) and by the heap
  assignments (MAX_HEAP_SIZE, HEAP_NEWSIZE, MAX_DIRECT_MEMORY_SIZE). The
  options it adds itself depend on what it finds in JVM_OPTS (grep) and on
  the heap: worked out for both files as its stock code does.
Then the JVM must get every option with the same value: the last one of a
name counts (-Xmx and -XX:MaxHeapSize are one), the ones that add up in the
same order, and the -XX ones still after the option unlocking them.

cassandra_jvm_args_same, cassandra_jvm_args_adopt, cassandra_jvm_args_cleanup:
    used by cassandra_import (the import) and cassandra_import_check (its
    self-check); no filter of their own.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import difflib
import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    JVM_LISTS, JVM_UNLOCK, shell_words)
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

MAIN_CLASS = "org.apache.cassandra.service.CassandraDaemon"
ENV = "cassandra-env.sh"
COMMON = "jvm-server.options"
HEAP_NAMES = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE", "MAX_DIRECT_MEMORY_SIZE")
# the variable that writes each heap assignment, by series (none: the template has no line for it)
HEAP_VARS = {
    "MAX_HEAP_SIZE": {"40x": "cassandra_heap_size", "41x": "cassandra_heap_size", "50x": "cassandra_heap_size"},
    "HEAP_NEWSIZE": {"40x": "cassandra_heap_newsize", "41x": "cassandra_heap_newsize"},
    "MAX_DIRECT_MEMORY_SIZE": {"50x": "cassandra_max_direct_memory_size"},
}
# a line adding plain options to JVM_OPTS (no expansion, no quote, no escape in them)
APPEND = re.compile(r'^(?:export )?JVM_OPTS="\$(?:JVM_OPTS|\{JVM_OPTS\}) ([^"$`\\]*)"$')
SET = re.compile(r"^(?:export )?(%s)=(\S*)$" % "|".join(HEAP_NAMES))
GREP = re.compile(r'^echo "?\$JVM_OPTS"? \| grep -qe? "?([^"\s]+)"?$')
# what cassandra-env.sh finds in JVM_OPTS and decides other options by: the same on both sides
GC_GREPS = ("+UseG1GC", "UseConcMarkSweepGC", "-Xlog:gc", "-Xloggc")
OPENERS = ("if", "case", "for", "while", "until", "select", "{")
CLOSERS = ("fi", "esac", "done", "}")
# what cassandra-env.sh works out on the node itself
CALC, CORES = "(calculated)", "(cpu cores)"
STOCK_TEXT = {"-Xms": "-Xms%s", "-Xmx": "-Xmx%s", "-Xmn": "-Xmn%s", "MaxDirectMemorySize": "-XX:MaxDirectMemorySize=%s",
              "ParallelGCThreads": "-XX:ParallelGCThreads=%s", "ConcGCThreads": "-XX:ConcGCThreads=%s"}
# HotSpot options the running Java ignores or no longer needs: name -> (from Java, why)
_CMS = "ignored since Java 14 (CMS removed)"
OBSOLETE = dict([(name, (14, _CMS)) for name in (
    "UseConcMarkSweepGC", "CMSParallelRemarkEnabled", "CMSInitiatingOccupancyFraction", "UseCMSInitiatingOccupancyOnly",
    "CMSWaitDuration", "CMSParallelInitialMarkEnabled", "CMSEdenChunksRecordAlways", "CMSClassUnloadingEnabled")] + [
    ("UseBiasedLocking", (18, "ignored since Java 18 (biased locking removed)")),
    ("BiasedLockingStartupDelay", (18, "ignored since Java 18 (biased locking removed)")),
    ("AggressiveOpts", (12, "ignored since Java 12")),
])


# HotSpot options that add up when given twice (the others: the last one counts)
LISTS = JVM_LISTS + ("DisableIntrinsic", "ControlIntrinsic", "AOTLibrary")
# options read where they are (a file of options inserted there): never moved, and the arguments must stay as they are
IN_PLACE = re.compile(r"^-XX:(VMOptionsFile|Flags)=|^@")


def _jvm_name(option):
    """The setting an option sets: -XX:[+-]Name -> Name, -Dx=1 -> -Dx, -Xmx4G -> -Xmx (and -XX:MaxHeapSize, the same
    setting), -Xss (and -XX:ThreadStackSize), -Xloggc, -verbose:gc; -Xms and -Xmn are not -XX:InitialHeapSize and -XX:NewSize (they also
    set the minimum heap, the maximum young generation). None: an option without a name."""
    m = re.match(r"^-XX:[+-]?(\w+)", option)
    if m:
        return {"MaxHeapSize": "-Xmx", "ThreadStackSize": "-Xss"}.get(m.group(1), m.group(1))
    m = re.match(r"^(-D[^=]+)", option) or re.match(r"^(-X(?:mx|ms|mn|ss|loggc))", option) \
        or re.match(r"^(-verbose:gc)$", option)
    return m.group(1) if m else None


# options that set one same HotSpot flag under other names (-Xms: InitialHeapSize and MinHeapSize, -Xmn: NewSize and
# MaxNewSize): the order of the last option of each name counts among them
FAMILIES = {
    "(initial heap)": ("-Xms", "InitialHeapSize", "MinHeapSize"),
    "(young generation)": ("-Xmn", "NewSize", "MaxNewSize"),
    # Java 8: -Xloggc turns PrintGC and PrintGCTimeStamps on where it is, -verbose:gc PrintGC
    "(GC log)": ("-Xloggc", "-verbose:gc", "PrintGC", "PrintGCTimeStamps"),
}


class NotSame(Exception):
    """Why the JVM arguments cannot be shown to be the same (no value in it)."""


def options_file(series, java):
    """The jvm<N>-server.options the running Java reads (cassandra.in.sh), None when not known."""
    major = _major(java)
    if not major:
        return None
    if series == "50x":
        return "jvm17-server.options" if major >= 17 else "jvm11-server.options"
    return "jvm11-server.options" if major >= 11 else "jvm8-server.options"


def _major(java):
    """The Java major version: 17 for 17, 8 for 1.8 (or 1.8.0_392); 0 when not one."""
    found = re.match(r"(?:1\.(\d+)(?:\D.*)?|(\d+))$", str(java))
    return int(found.group(1) or found.group(2)) if found else 0


def _arguments(cmdline):
    """/proc/<pid>/cmdline -> its arguments, the main class last but its own."""
    args = str(cmdline or "").split("\0")
    if args and args[-1] == "":
        args.pop()
    if MAIN_CLASS not in args[1:]:
        raise NotSame("the running JVM's arguments could not be read")
    return args


def _option_words(text):
    """The words bin/cassandra takes from an options file: of its lines starting with '-', split on blanks."""
    if "\r" in text:
        raise NotSame("an options file with a \\r")
    return [(word, n + 1) for n, line in enumerate(text.split("\n")) if line.startswith("-")
            for word in re.split(r"[ \t]+", line) if word]


def _files_words(files, series, java):
    """[(word, file, line)] of the options files the running Java reads, in the order it reads them."""
    dep = options_file(series, java)
    if dep is None:
        raise NotSame("the running Java's version is not known")
    out_words = []
    for name in (COMMON, dep):
        if name not in files:
            raise NotSame("%s not read" % name)
        out_words += [(word, name, n) for word, n in _option_words(files[name])]
    return out_words


def _find(args, words, start=1):
    """Where words are, in a row, in args (from start): the index, once only, else NotSame."""
    found = [i for i in range(start, len(args) - len(words) + 1) if args[i:i + len(words)] == words]
    if len(found) != 1 or not words:
        raise NotSame("the options files are not in the running JVM's arguments as they are now (changed since it"
                      " started?)")
    return found[0]


def _found(args, options, at, end):
    """Where options are in a row in args[at:end]: every index."""
    return [i for i in range(at, end - len(options) + 1) if args[i:i + len(options)] == options]


def _command_word(words, i):
    return i == 0 or words[i - 1].endswith(";") or words[i - 1] in ("then", "do", "else", "&&", "||", "|", "{")


def env_lines(text):
    """cassandra-env.sh as bash runs it, line by line: [{n, text (its words), words, depth (blocks around it)}];
    NotSame when it is not read so (a line going on to the next, a heredoc, a \\r, blocks not closed)."""
    if "\r" in text or "<<" in text:
        raise NotSame("cassandra-env.sh is not read line by line")
    lines, depth = [], 0
    for n, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            raise NotSame("cassandra-env.sh has a line going on to the next")
        try:
            words = shell_words(line)
        except ValueError:
            raise NotSame("cassandra-env.sh has a quote going on to the next line")
        if not words:
            continue
        lines.append({"n": n + 1, "text": " ".join(words), "words": words, "depth": depth})
        for i, word in enumerate(words):
            word = word.rstrip(";")
            if word in OPENERS and _command_word(words, i):
                depth += 1
            elif word in CLOSERS and _command_word(words, i):
                depth -= 1
    if depth != 0:
        raise NotSame("cassandra-env.sh: its blocks could not be followed")
    return lines


def env_differences(live, role):
    """The lines of the node's cassandra-env.sh not in the role's -> ([its JVM_OPTS lines: {n, text, words}], [its
    heap assignments]); NotSame when they differ otherwise (only lines outside any block may: a line adding plain
    options to JVM_OPTS on the node's side, a heap assignment on either)."""
    added, heap = [], []
    matcher = difflib.SequenceMatcher(None, [line["text"] for line in role], [line["text"] for line in live],
                                      autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        if (i1 and GREP.match(role[i1 - 1]["text"])) or (j1 and GREP.match(live[j1 - 1]["text"])):
            raise NotSame("cassandra-env.sh differs right after a grep (its $? read on the next line)")
        for line in role[i1:i2]:
            if line["depth"] or not SET.match(line["text"]):
                raise NotSame("cassandra-env.sh differs from the role's otherwise than by JVM_OPTS lines and the heap")
        for line in live[j1:j2]:
            append = APPEND.match(line["text"]) if not line["depth"] else None
            if append and append.group(1).split():
                added.append(dict(line, options=append.group(1).split()))
            elif append:
                continue  # JVM_OPTS="$JVM_OPTS ": adds nothing
            elif not line["depth"] and SET.match(line["text"]):
                heap.append(line)
            else:
                raise NotSame("cassandra-env.sh differs from the role's otherwise than by JVM_OPTS lines and the heap")
    return added, heap


def _plain_value(word):
    return None if re.search(r"[$`\\'\"]", word) else word


def heap_values(lines, environment=None):
    """{name: value} of the heap variables as cassandra-env.sh's heap code gets them: its last assignment outside
    any block before the first line using it, else the environment's (None: not set); a name it does not use is
    left out. -> (values, [dead assignments: after its last use]); NotSame for one between two uses."""
    values, dead = {}, []
    for name in HEAP_NAMES:
        uses = [i for i, line in enumerate(lines) if re.search(r"\b%s\b" % name, line["text"])
                and not (SET.match(line["text"]) and SET.match(line["text"]).group(1) == name)]
        if not uses:
            continue
        value = (environment or {}).get(name) or None
        for i, line in enumerate(lines):
            m = SET.match(line["text"])
            if not m or m.group(1) != name or line["depth"]:
                continue
            if i < uses[0]:
                value = m.group(2) or None
            elif i > uses[-1]:
                dead.append(line)
            else:
                raise NotSame("cassandra-env.sh sets %s between two of its uses" % name)
        values[name] = value
    return values, dead


def _greps(lines):
    """[(index of the line, what cassandra-env.sh looks for in JVM_OPTS)], [X] as X."""
    found = []
    for i, line in enumerate(lines):
        m = GREP.match(line["text"])
        if m:
            # as shell_words writes it: "-[X]log:gc" is -\[X\]log:gc; [X] matches X
            found.append((i, re.sub(r"\[(.)\]", r"\1", re.sub(r"\\(.)", r"\1", m.group(1)))))
    return found


def stock_options(greps, heap):
    """The options cassandra-env.sh adds from what it found in JVM_OPTS (greps: {pattern: found}) and the heap
    ({name: value}): {name: value, CALC or CORES when the node works it out}; None when it stops (exit 1)."""
    def found(pattern):
        return greps.get(pattern, False)
    g1, cms = found("+UseG1GC"), found("UseConcMarkSweepGC")
    heap_size, new_size = heap.get("MAX_HEAP_SIZE"), heap.get("HEAP_NEWSIZE")
    if heap_size is None and (new_size is None or g1):
        heap_size = new_size = CALC
    elif heap_size is None or (new_size is None and not g1):
        return None  # "please set or unset MAX_HEAP_SIZE and HEAP_NEWSIZE in pairs"
    added = {}
    if not found("Xmx") and not found("Xms"):
        added["-Xms"] = added["-Xmx"] = heap_size
    elif not (found("Xmx") and found("Xms")):
        return None
    if "MaxDirectMemorySize" in greps and not found("MaxDirectMemorySize"):
        added["MaxDirectMemorySize"] = heap.get("MAX_DIRECT_MEMORY_SIZE") or CALC
    if found("Xmn") and not found("Xmx"):
        return None
    if not found("Xmn") and cms:
        added["-Xmn"] = new_size
    if found("Xmn") and g1:
        return None
    if "ParallelGCThreads" in greps and g1 and not found("ParallelGCThreads") and not found("ConcGCThreads"):
        added["ParallelGCThreads"] = added["ConcGCThreads"] = CORES
    return added


def _kind(option):
    """The order of the options without a name counts among the ones of a kind: -ea/-da, -javaagent:..., -Xlog
    by output (-Xlog:<what>:<output>: a later one for an output reconfigures it, other outputs are apart)."""
    if re.match(r"^-[ed]s?a\b", option):
        return "(assertions)"
    if re.match(r"^-(javaagent|agentlib|agentpath)[:=]", option):
        return "(agents)"
    xlog = re.match(r"^-Xlog:[^:]+:([^:]+)", option)
    if xlog and not xlog.group(1).startswith("disable"):
        return "-Xlog:%s" % xlog.group(1)
    return re.split(r"[:=]", option.split(" ")[0], maxsplit=1)[0]


def effective(args):
    """What the JVM gets from its arguments (the program name and the main class out): {name: [option]} (the last
    one, every one in order for the ones that add up), {kind: [options in order]} for the others, the unlock
    options before each -XX option that counts, and in each family of settings the order of their last options."""
    options, i = [], 0
    while i < len(args):
        option = args[i]
        if option in ("-cp", "-classpath", "--class-path"):
            i += 2
            continue
        if option.startswith("--") and "=" not in option and i + 1 < len(args) and not args[i + 1].startswith("-"):
            option += " " + args[i + 1]
            i += 1
        options.append(option)
        i += 1
    named, unnamed, unlocked, seen, last = {}, {}, {}, set(), {}
    # an -Xlog without an output (stdout) or -Xlog:disable acts on every output: their order with all -Xlog counts
    xlog_apart = all(_kind(o) != "-Xlog" for o in options if o.startswith("-Xlog"))
    for position, option in enumerate(options):
        name = _jvm_name(option)
        if name is None:
            kind = _kind(option)
            unnamed.setdefault(kind if xlog_apart or not kind.startswith("-Xlog") else "-Xlog", []).append(option)
            continue
        if name in LISTS:
            named.setdefault(name, []).append(option)
        else:
            named[name] = [option]
        last[name] = position
        if option.startswith("-XX:"):
            unlocked[name] = sorted(seen)
        else:
            unlocked.pop(name, None)
        if option.startswith("-XX:+") and name in JVM_UNLOCK:
            seen.add(name)
        elif option.startswith("-XX:-") and name in JVM_UNLOCK:
            seen.discard(name)
    families = dict((family, [n for n in sorted(last, key=last.get) if n in members])
                    for family, members in FAMILIES.items())
    return {"named": named, "unnamed": unnamed, "unlocked": unlocked, "families": families}


def differences(before, after):
    """The names (kinds) of the options the JVM gets otherwise, sorted."""
    a, b = effective(before), effective(after)
    names = set()
    for key in ("named", "unnamed", "unlocked", "families"):
        names |= set(k for k in set(a[key]) | set(b[key]) if a[key].get(k) != b[key].get(k))
    return sorted(names)


def _analyse(args, live, role, series, java, node_env):
    """The node's side (see the module): where its options files' words are in its arguments, its JVM_OPTS lines
    in cassandra-env.sh and where their options are, what cassandra-env.sh finds in JVM_OPTS, the heap it gets."""
    end = len(args) - 1 - args[::-1].index(MAIN_CLASS)
    words = _files_words(live, series, java)
    start = _find(args[:end], [w for w, dummy, dummy2 in words])
    after_files = start + len(words)
    if ENV not in live or ENV not in role:
        raise NotSame("cassandra-env.sh not read")
    live_lines, role_lines = env_lines(live[ENV]), env_lines(role[ENV])
    added, heap_lines = env_differences(live_lines, role_lines)
    # options the unit gives cassandra-env.sh to add (JVM_EXTRA_OPTS, MX4J_*): a line moved to the options files
    # would go before them, and an option of another spelling among them (-Xint and -Xmixed, -Xrs and
    # -XX:-ReduceSignalUsage, -verbose:gc and -Xlog...) would win instead
    if added and any((node_env or {}).get(name) for name in ("JVM_EXTRA_OPTS", "MX4J_ADDRESS", "MX4J_PORT")):
        raise NotSame("the unit gives cassandra-env.sh options to add (JVM_EXTRA_OPTS, MX4J_*)")
    # the node's JVM_OPTS lines in the arguments, in the order cassandra-env.sh adds them
    at = after_files
    for line in added:
        found = _found(args, line["options"], at, end)
        if not found:
            raise NotSame("a JVM_OPTS line of cassandra-env.sh is not in the running JVM's arguments (changed since it"
                          " started?)")
        if len(found) > 1:  # which one is the line's (the other: cassandra-env.sh's own, JVM_EXTRA_OPTS...)?
            raise NotSame("the options of a JVM_OPTS line of cassandra-env.sh are twice in the arguments")
        if any(IN_PLACE.match(o) for o in line["options"]):
            raise NotSame("a JVM_OPTS line of cassandra-env.sh reads a file of options where it is")
        line["at"] = found[0]
        at = line["at"] + len(line["options"])
    # cassandra-env.sh read before bin/cassandra too (an init script exporting JVM_OPTS): its options before the
    # options files' would change with it
    before = args[1:start]
    if any(o.startswith("-XX:CompileCommandFile=") for o in before) or any(
            before[i:i + len(line["options"])] == line["options"] for line in added for i in range(len(before))):
        raise NotSame("cassandra-env.sh's options are also before the options files' (read twice?)")
    # what it finds in JVM_OPTS: the options before it, its JVM_OPTS lines above the grep among them
    greps, index = {}, dict((line["n"], i) for i, line in enumerate(live_lines))
    for i, pattern in _greps(live_lines):
        seen = args[1:after_files] + [o for line in added if index[line["n"]] < i for o in line["options"]]
        greps.setdefault(pattern, pattern in " ".join(seen))
    live_heap, dead = heap_values(live_lines, node_env)
    return {"end": end, "start": start, "after_files": after_files, "words": words, "live_lines": live_lines,
            "role_lines": role_lines, "added": added, "heap_lines": heap_lines, "greps": greps,
            "live_heap": live_heap, "dead": dead}


def _launch(cmdline, live, role, series, java, node_env, role_env):
    """The arguments the JVM would get from the role's files (see the module): (the running JVM's arguments, the
    ones it would get); NotSame when that cannot be shown."""
    args = _arguments(cmdline)
    info = _analyse(args, live, role, series, java, node_env)
    end, start, after_files, before = info["end"], info["start"], info["after_files"], info["greps"]
    role_words = [w for w, dummy, dummy2 in _files_words(role, series, java)]
    hand = set(i for line in info["added"] for i in range(line["at"], line["at"] + len(line["options"])))
    role_greps = _greps(info["role_lines"])  # the node's (env_differences: no grep line differs)
    now = dict((p, p in " ".join(args[1:start] + role_words)) for dummy, p in role_greps)
    if any(before[p] != now[p] for p in GC_GREPS if p in before):
        raise NotSame("cassandra-env.sh would find another GC in the options")
    role_heap, dummy = heap_values(info["role_lines"], role_env)
    live_stock, role_stock = stock_options(before, info["live_heap"]), stock_options(now, role_heap)
    if live_stock is None:
        raise NotSame("cassandra-env.sh's heap could not be followed")
    if role_stock is None:
        raise NotSame("cassandra-env.sh would stop: the heap set without its pair (MAX_HEAP_SIZE and HEAP_NEWSIZE,"
                      " -Xmx and -Xms) or -Xmn with G1")
    rest = dict((i, args[i]) for i in range(after_files, end) if i not in hand)
    first = []  # options cassandra-env.sh would add that it does not add now
    for name in sorted(set(live_stock) | set(role_stock)):
        was, will = live_stock.get(name), role_stock.get(name)
        if was is not None:
            where = [i for i in rest if _jvm_name(rest[i]) == name]
            if len(where) != 1 or was not in (CALC, CORES) and rest[where[0]] != STOCK_TEXT[name] % was:
                raise NotSame("the options cassandra-env.sh adds are not in the running JVM's arguments as worked out")
            if will is None:
                del rest[where[0]]
            elif will != was:
                if will in (CALC, CORES):
                    raise NotSame("the heap would be worked out on the node instead of set")
                rest[where[0]] = STOCK_TEXT[name] % will
        elif will is not None:
            if will in (CALC, CORES):
                raise NotSame("cassandra-env.sh would add %s, worked out on the node" % name)
            first.append(STOCK_TEXT[name] % will)
    return args, args[:start] + role_words + first + [rest[i] for i in sorted(rest)] + args[end:]


def cassandra_jvm_args_same(cmdline, live, role, series, java, node_env=None, role_env=None):
    """Whether the role's files (role: {file: text}) give the JVM the same arguments as the node's (live) did
    (cmdline: its /proc/<pid>/cmdline; node_env, role_env: the environment the unit gives Cassandra now, and with
    the roles) -> (True, '') or (False, why)."""
    for name in ("JVM_OPTS", "JVM_EXTRA_OPTS"):
        if (node_env or {}).get(name) != (role_env or {}).get(name):
            return False, "the unit would give Cassandra another %s" % name
    try:
        args, after = _launch(cmdline, live, role, series, java, node_env, role_env)
    except NotSame as exc:
        return False, str(exc)
    if args != after and any(IN_PLACE.match(o) for o in args + after):
        return False, "an option reads a file of options where it is (-XX:VMOptionsFile, -XX:Flags, @file)"
    names = differences(args[1:], after[1:])
    if names:  # their names only (-Xlog:file=<path>: -Xlog, -Xmaxjitcodesize240M: -Xmaxjitcodesize)
        names = sorted(set(re.sub(r"(?<=^-X[a-z])([a-z]*?)\d.*$", r"\1", re.split(r"[:=]", n)[0]) for n in names))
        return False, "the JVM would get other values of %s" % ", ".join(out.mask(n) for n in names)
    return True, ""


def _later(args, i, end):
    """The last option after args[i] (up to end) that sets the same name, None if none (or the option adds up)."""
    name = _jvm_name(args[i])
    if name is None or name in LISTS:
        return None
    later = [k for k in range(i + 1, end) if _jvm_name(args[k]) == name]
    return later[-1] if later else None


def _lines_of(options):
    """Options -> the lines of an options file (--add-opens and its value on one line), None when one does not
    start with '-' (bin/cassandra would leave it out)."""
    lines = []
    for option in options:
        if option.startswith("-"):
            lines.append(option)
        elif lines and lines[-1].startswith("--") and "=" not in lines[-1] and " " not in lines[-1]:
            lines[-1] += " " + option
        else:
            return None
    return lines


HEAP_OPTIONS = {"-Xmx": "cassandra_heap_size", "-Xms": "cassandra_heap_size", "-Xmn": "cassandra_heap_newsize",
                "MaxDirectMemorySize": "cassandra_max_direct_memory_size"}


def cassandra_jvm_args_adopt(cmdline, live, render, variables, series, java, node_env=None):
    """The variables that move the node's hand edits of cassandra-env.sh (its JVM_OPTS lines, its heap set
    elsewhere) to where the role writes them, when the JVM gets the same arguments from the role's files.
    render(variables): {file: text} the role writes; variables: the ones imported so far; node_env: the
    environment the unit gives Cassandra. -> {vars, moved, dropped: report lines}; {why: the reason, names only}
    when there are such edits but the same arguments cannot be shown; None when there are none."""
    dep = options_file(series, java)
    if not cmdline or dep is None or ENV not in live:
        return None
    role = render(variables)
    try:
        args = _arguments(cmdline)
        info = _analyse(args, live, role, series, java, node_env)
        role_heap, dummy = heap_values(info["role_lines"], node_env)
    except NotSame as exc:
        return {"why": str(exc)} if _candidates(live[ENV], role[ENV]) else None
    if not info["added"] and not info["heap_lines"]:
        return None
    end, dropped, keep = info["end"], [], []
    # the heap cassandra-env.sh gets, set where the role writes it
    heap_vars = {}
    for name, value in info["live_heap"].items():
        if value != role_heap.get(name) and HEAP_VARS[name].get(series):
            if value is not None and _plain_value(value) is None:
                return {"why": "cassandra-env.sh sets %s from what only the shell knows" % name}
            heap_vars[HEAP_VARS[name][series]] = value or ""
    dropped += ["%s line %d: %s: set after the last use of %s (no effect), left out" % (
        ENV, line["n"], out.mask(line["text"]), SET.match(line["text"]).group(1)) for line in info["dead"]]
    for line in info["added"]:
        for k, option in enumerate(line["options"]):
            later = _later(args, line["at"] + k, end)
            if later is None:
                keep.append(option)
            else:
                dropped.append("%s line %d: %s: %s by %s later, left out" % (
                    ENV, line["n"], out.mask(option), "set again" if args[later] == option else "overridden",
                    out.mask(args[later])))
    extras_var = "cassandra_%s_extra_options" % dep.split("-", maxsplit=1)[0]
    # -Xms and -Xmx alike: the heap variables; else (or when that is not the same) every option in the extra options
    last = dict((_jvm_name(o), o) for o in keep if _jvm_name(o) in HEAP_OPTIONS)
    last = dict((k, o) for k, o in last.items() if o.startswith(k) or (k == "MaxDirectMemorySize" and "=" in o))
    by_vars, to_vars = dict(heap_vars), []
    if "-Xmx" in last and "-Xms" in last and last["-Xmx"][4:] == last["-Xms"][4:]:
        by_vars["cassandra_heap_size"] = last["-Xmx"][4:]
        to_vars += [last["-Xms"], last["-Xmx"]]
    if "-Xmn" in last and HEAP_VARS["HEAP_NEWSIZE"].get(series):
        by_vars["cassandra_heap_newsize"] = last["-Xmn"][4:]
        to_vars.append(last["-Xmn"])
    if "MaxDirectMemorySize" in last and HEAP_VARS["MAX_DIRECT_MEMORY_SIZE"].get(series):
        by_vars["cassandra_max_direct_memory_size"] = last["MaxDirectMemorySize"].split("=", 1)[1]
        to_vars.append(last["MaxDirectMemorySize"])
    if by_vars.get("cassandra_heap_size") and HEAP_VARS["HEAP_NEWSIZE"].get(series) \
            and not by_vars.get("cassandra_heap_newsize") and not variables.get("cassandra_heap_newsize") \
            and not info["greps"].get("+UseG1GC"):
        # without G1, MAX_HEAP_SIZE needs HEAP_NEWSIZE: the young generation the JVM runs with
        xmn = [a for a in args[1:end] if a.startswith("-Xmn")]
        if xmn:
            by_vars["cassandra_heap_newsize"] = xmn[-1][4:]
    tries = [(by_vars, [o for o in keep if o not in to_vars])] if to_vars else []
    tries.append((heap_vars, keep))
    why = "an option would not go to an options file"
    for found, options in tries:
        found = dict(found)
        if options:
            lines = _lines_of(options)
            if lines is None:
                continue
            found[extras_var] = list(variables.get(extras_var) or []) + lines
        same, why = cassandra_jvm_args_same(cmdline, live, render(dict(variables, **found)), series, java,
                                            node_env, node_env)
        if same:
            moved = _moved(info, found, options, extras_var)
            # the files are rewritten: their own extra options set again later are left out too
            tidy, gone = _without_dead(args, info, variables, found, live, series, java)
            if gone and cassandra_jvm_args_same(cmdline, live, render(dict(variables, **dict(found, **tidy))), series,
                                                java, node_env, node_env)[0]:
                found.update(tidy)
                dropped += gone
            return {"vars": found, "moved": moved, "dropped": dropped}
    return {"why": why}


def _candidates(live, role):
    """Whether the node's cassandra-env.sh has JVM_OPTS lines of plain options or heap assignments the role's has
    not (wherever they are)."""
    try:
        theirs = set(line["text"] for line in env_lines(role))
        return any((APPEND.match(line["text"]) or SET.match(line["text"])) and line["text"] not in theirs
                   for line in env_lines(live))
    except NotSame:
        return False


def _without_dead(args, info, variables, found, live, series, java):
    """The extra options variables of the options files the running Java reads (variables: as imported, found: the
    move's) without the lines the node had whose options are all set again later in the arguments -> ({variable:
    lines}, [report lines])."""
    words, start, end = info["words"], info["start"], info["end"]
    tidy, gone = {}, []
    for name in (COMMON, options_file(series, java)):
        var = "cassandra_%s_extra_options" % ("jvm" if name == COMMON else name.split("-", maxsplit=1)[0])
        lines = list(found.get(var, variables.get(var)) or [])
        for entry in list(variables.get(var) or []):
            at = [start + i for i, (dummy, f, n) in enumerate(words)
                  if f == name and _line(live[name], n) == entry.strip()]
            later = [_later(args, i, end) for i in at]
            if at and all(k is not None for k in later):
                lines.remove(entry)
                gone.append("%s line %d: %s: %s by %s later, left out" % (
                    name, words[at[0] - start][2], out.mask(entry), "set again" if args[later[-1]] == entry
                    else "overridden", out.mask(args[later[-1]])))
        if lines != list(found.get(var, variables.get(var)) or []):
            tidy[var] = lines
    return tidy, gone


def _line(text, n):
    return text.split("\n")[n - 1].strip()


def _moved(info, found, options, extras_var):
    """The report lines of a move: each line of cassandra-env.sh moved, and where it went."""
    lines = []
    for line in info["heap_lines"]:
        if line in info["dead"]:
            continue
        name, value = SET.match(line["text"]).groups()
        var = [v for v in HEAP_VARS[name].values() if v in found]
        went = "left out, no effect"
        if var and found[var[0]] == value:
            went = "%s: %s" % (var[0], out.mask(found[var[0]]))
        elif var or info["live_heap"].get(name) != value:
            went = "left out, set again later"
        lines.append("%s line %d: %s -> %s" % (ENV, line["n"], out.mask(line["text"]), went))
    for line in info["added"]:
        went = []
        for option in line["options"]:
            var = HEAP_OPTIONS.get(_jvm_name(option))
            where = extras_var if option in options else (
                "%s: %s" % (var, out.mask(found[var])) if var in found else "left out")
            if where not in went:
                went.append(where)
        lines.append("%s line %d: %s -> %s" % (ENV, line["n"], out.mask(" ".join(line["options"])) or "(no option)",
                                               "; ".join(went) or "left out"))
    if found.get("cassandra_heap_newsize") and not any("cassandra_heap_newsize" in line for line in lines):
        lines.append("the young generation the JVM runs with (MAX_HEAP_SIZE needs it without G1) -> "
                     "cassandra_heap_newsize: %s" % out.mask(found["cassandra_heap_newsize"]))
    return lines


def _own_lines(text, stock):
    """[(word, line number)] of an options file's lines starting with '-' not in stock (a text)."""
    lines = set(line.strip() for line in str(stock or "").split("\n"))
    return [(word, n + 1) for n, line in enumerate(text.split("\n"))
            if line.startswith("-") and line.strip() not in lines for word in re.split(r"[ \t]+", line) if word]


def cassandra_jvm_args_cleanup(live, default, role, series, java, cmdline="", moved=False):
    """The node's own options (in its options files, not in the role's with its defaults: default {file: text};
    on its JVM_OPTS lines of cassandra-env.sh, not in the role's: role {file: text}) the running JVM does not use
    -> report lines: in the options file of another Java, set again later (the last one counts: from the running
    JVM's arguments, cmdline), ignored by the running Java. Values masked. moved: cassandra-env.sh's lines were
    moved (what of them is left out said with the move)."""
    lines = []
    dep = options_file(series, java)
    if dep is None:
        return lines
    major = _major(java)
    for name in sorted(live):
        m = re.match(r"^jvm(\d+)-server\.options$", name)
        if m and name != dep and not ("\r" in live[name]):
            lines += ["%s line %d: %s: not read, Java %d reads %s" % (name, n, out.mask(word), major, dep)
                      for word, n in _own_lines(live[name], default.get(name))]
    mine = []  # (option, where) the node's own options the running Java reads
    for name in (COMMON, dep):
        if name in live and "\r" not in live[name]:
            mine += [(word, "%s line %d" % (name, n)) for word, n in _own_lines(live[name], default.get(name))]
    added = []
    if ENV in live and ENV in role:
        try:
            added = env_differences(env_lines(live[ENV]), env_lines(role[ENV]))[0]
        except NotSame:
            added = []
    if not moved:
        mine += [(o, "%s line %d" % (ENV, line["n"])) for line in added for o in line["options"]]
    lines += ["%s: %s: %s" % (where, out.mask(o), OBSOLETE[_jvm_name(o)][1]) for o, where in mine
              if _jvm_name(o) in OBSOLETE and major >= OBSOLETE[_jvm_name(o)][0]]
    # Java 15 to 17: biased locking off by default, -XX:-UseBiasedLocking sets it as it is
    lines += ["%s: %s: Java %d's default since Java 15 (deprecated, ignored from Java 18)" % (where, o, major)
              for o, where in mine if o == "-XX:-UseBiasedLocking" and 15 <= major < 18]
    try:
        args = _arguments(cmdline)
        end = len(args) - 1 - args[::-1].index(MAIN_CLASS)
        words = _files_words(live, series, java)
        start = _find(args[:end], [w for w, dummy, dummy2 in words])
    except NotSame:
        return lines
    source = dict((start + i, "%s line %d" % (f, n)) for i, (dummy, f, n) in enumerate(words))
    at = start + len(words)
    for line in added:
        found = _found(args, line["options"], at, end)
        if len(found) != 1:
            break
        source.update((found[0] + k, "%s line %d" % (ENV, line["n"])) for k in range(len(line["options"])))
        at = found[0] + len(line["options"])
    ours = set(where for dummy, where in mine)
    for i in sorted(source):
        later = _later(args, i, end)
        if source[i] in ours and later is not None:
            lines.append("%s: %s: %s by %s (%s)" % (
                source[i], out.mask(args[i]), "set again" if args[later] == args[i] else "overridden",
                out.mask(args[later]), source.get(later, "added by cassandra-env.sh or bin/cassandra")))
    return lines


class FilterModule(object):
    def filters(self):
        return {}
