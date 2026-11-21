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
- or the node's is an earlier series' stock file (4.x's on 5.0: earlier, its
  rendering), which it may differ from as above: what each file's stock code
  adds is worked out from its own greps (5.0's adds MaxDirectMemorySize and,
  with G1, the GC threads; 4.x's neither), never a heap worked out on the node
  (their formulas differ), the GC log the same on Java 11+; 5.0's
  -XX:HeapDumpPath, which 4.x's adds only with CASSANDRA_HEAPDUMP_DIR, is not
  compared (EARLIER_APART: the import says it).
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
from collections import namedtuple

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    JVM_ENV_GREPS, JVM_LISTS, JVM_UNLOCK, jvm_options_settings, shell_words)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import mask
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_prometheus as prometheus

MAIN_CLASS = "org.apache.cassandra.service.CassandraDaemon"
ENV = "cassandra-env.sh"
COMMON = "jvm-server.options"
HEAP_NAMES = ("MAX_HEAP_SIZE", "HEAP_NEWSIZE", "MAX_DIRECT_MEMORY_SIZE")
# what the environment gives cassandra-env.sh to add to the JVM's options (bin/cassandra puts JVM_OPTS before the
# options files' words, cassandra-env.sh JVM_EXTRA_OPTS and MX4J_* after them): the same on both sides, carried as they
# are; otherwise the arguments are not shown to be the files' (env_differs, cassandra_jvm_args_same)
JVM_ENV = ("JVM_OPTS", "JVM_EXTRA_OPTS", "MX4J_ADDRESS", "MX4J_PORT")
# the variable that writes each heap assignment, by series (none: the template has no line for it)
HEAP_VARS = {
    "MAX_HEAP_SIZE": {"40x": "cassandra_heap_size", "41x": "cassandra_heap_size", "50x": "cassandra_heap_size"},
    "HEAP_NEWSIZE": {"40x": "cassandra_heap_newsize", "41x": "cassandra_heap_newsize", "50x": "cassandra_heap_newsize"},
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
# what an earlier series' stock cassandra-env.sh gives otherwise than the later one's, by design: not compared
EARLIER_APART = ("HeapDumpPath",)
STOCK_TEXT = {"-Xms": "-Xms%s", "-Xmx": "-Xmx%s", "-Xmn": "-Xmn%s", "MaxDirectMemorySize": "-XX:MaxDirectMemorySize=%s",
              "ParallelGCThreads": "-XX:ParallelGCThreads=%s", "ConcGCThreads": "-XX:ConcGCThreads=%s"}
# HotSpot options the running Java ignores or no longer needs: name -> (from Java, why)
_CMS = "ignored since Java 14 (CMS removed)"
OBSOLETE = dict([(name, (14, _CMS)) for name in (
    "UseConcMarkSweepGC", "CMSParallelRemarkEnabled", "CMSInitiatingOccupancyFraction", "UseCMSInitiatingOccupancyOnly",
    "CMSWaitDuration", "CMSParallelInitialMarkEnabled", "CMSEdenChunksRecordAlways", "CMSClassUnloadingEnabled")] + [
    ("UseBiasedLocking", (18, jvm.BIASED_GONE)),
    ("BiasedLockingStartupDelay", (18, jvm.BIASED_GONE)),
    ("AggressiveOpts", (12, "ignored since Java 12")),
])


# HotSpot options that add up when given twice (the others: the last one counts)
LISTS = JVM_LISTS + ("DisableIntrinsic", "ControlIntrinsic", "AOTLibrary")
# options read where they are (a file of options inserted there): never moved, and the arguments must stay as they are
IN_PLACE = re.compile(r"^-XX:(VMOptionsFile|Flags)=|^@")


def _jvm_name(option):
    """The setting an option sets: -XX:[+-]Name -> Name, -Dx=1 -> -Dx, -Xmx4G -> -Xmx (and -XX:MaxHeapSize, the same
    setting), -Xss (and -XX:ThreadStackSize), -Xloggc, -verbose:gc, the -X options of a flag (-Xint, -Xrs, -Xshare:off...,
    their families); -Xms and -Xmn are not -XX:InitialHeapSize and -XX:NewSize (they also
    set the minimum heap, the maximum young generation). None: an option without a name."""
    m = re.match(r"^-XX:[+-]?(\w+)", option)
    if m:
        return {"MaxHeapSize": "-Xmx", "ThreadStackSize": "-Xss"}.get(m.group(1), m.group(1))
    m = re.match(r"^(-D[^=]+)", option) or re.match(r"^(-X(?:mx|ms|mn|ss|loggc|maxjitcodesize))", option) \
        or re.match(r"^(-verbose:gc|-X(?:int|mixed|comp|rs|noclassgc|batch|concgc|incgc))$", option) \
        or re.match(r"^(-Xshare)(?::\w+)?$", option)
    return m.group(1) if m else None


# options that set one same HotSpot flag under other names (-Xms: InitialHeapSize and MinHeapSize, -Xmn: NewSize and
# MaxNewSize): the order of the last option of each name counts among them
FAMILIES = {
    "(initial heap)": ("-Xms", "InitialHeapSize", "MinHeapSize"),
    "(young generation)": ("-Xmn", "NewSize", "MaxNewSize"),
    # Java 8: -Xloggc turns PrintGC and PrintGCTimeStamps on where it is, -verbose:gc PrintGC
    "(GC log)": ("-Xloggc", "-verbose:gc", "PrintGC", "PrintGCTimeStamps"),
    # the -X options of a -XX flag, or of one another (the last one wins)
    "(execution mode)": ("-Xint", "-Xmixed", "-Xcomp", "UseInterpreter", "UseCompiler"),
    "(signals)": ("-Xrs", "ReduceSignalUsage"),
    "(class unloading)": ("-Xnoclassgc", "ClassUnloading"),
    "(background compilation)": ("-Xbatch", "BackgroundCompilation"),
    "(code cache)": ("-Xmaxjitcodesize", "ReservedCodeCacheSize"),
    "(class data sharing)": ("-Xshare", "UseSharedSpaces", "RequireSharedSpaces"),
    "(CMS)": ("-Xconcgc", "-Xincgc", "UseConcMarkSweepGC"),
}


class NotSame(Exception):
    """Why the JVM arguments cannot be shown to be the same (no value in it)."""


def options_file(series, java):
    """The jvm<N>-server.options the running Java reads (cassandra.in.sh), None when not known."""
    return jvm.options_file(series, java) or None


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


def _moved_lines(live, role, ops):
    """The texts of the lines adding plain options to JVM_OPTS that both files have once, at another place (the
    role's line of a variable, outside any block; the node's own below the stock comment, or in a block that ran,
    e.g. the remote JMX branch): bash adds the same, as no other line of either file sets one of their options."""
    def once(lines, side, top):
        texts = [line["text"] for line in lines]
        return set(line["text"] for op in ops for line in lines[op[side]:op[side + 1]]
                   if not (top and line["depth"]) and APPEND.match(line["text"]) and texts.count(line["text"]) == 1)
    moved = once(role, 1, True) & once(live, 3, False)
    for text in list(moved):
        names = set(jvm.option_name(o) for o in APPEND.match(text).group(1).split())
        others = [line for line in role + live if line["text"] != text]
        set_elsewhere = any(jvm.option_name(w) in names for line in others
                            for w in re.split(r"[\s\"'=]+", line["text"]) if w.startswith("-"))
        if None in names or any(g in text for g in JVM_ENV_GREPS) or set_elsewhere:
            moved.discard(text)
    return moved


# a variable set to a value, no command run (no command substitution, no other command on the line)
PLAIN_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=[^`;&|<>()]*$")


def _plain_line(line):
    """Whether bash runs line without changing where it goes next nor what JVM_OPTS has but plain options added: a
    line adding plain options to JVM_OPTS, or NAME=value (export NAME=value) not naming JVM_OPTS (JVM_OPTS=, nor a
    value that may set it: X=${JVM_OPTS:=...})."""
    if APPEND.match(line["text"]):
        return True
    words = line.get("words") or line["text"].split()
    words = words[1:] if words[:1] == ["export"] else words
    return len(words) == 1 and bool(PLAIN_ASSIGNMENT.match(words[0])) and "JVM_OPTS" not in words[0]


def _leaves_if(line):
    """Whether line only ends an if (fi) or goes to its else: bash, having run a line of that if, goes on below."""
    parts = [p.strip() for p in line["text"].split(";") if p.strip()]
    return len(parts) == 1 and parts[0] in ("fi", "else")


def _plain_options(line):
    """The options a line adds to JVM_OPTS when each one counts wherever it is set -> [option]; None when the line
    adds none, a grep of cassandra-env.sh reads it, or one of them adds up, has no name, unlocks others or is read
    where it is (its place counts)."""
    append = APPEND.match(line["text"])
    if not append or not append.group(1).split() or any(g in line["text"] for g in JVM_ENV_GREPS):
        return None
    options = append.group(1).split()
    if any(_jvm_name(o) is None or _jvm_name(o) in LISTS or _jvm_name(o) in JVM_UNLOCK or IN_PLACE.match(o)
           for o in options):
        return None
    return options


def _set_again(line, later, in_block):
    """Whether a line in a block adding plain options to JVM_OPTS (each set by its last occurrence) is written again
    the same below it, bash going from one to the other through plain lines only (_plain_line: no command, no loop,
    no return, no case, no other block): with in_block, in the same block (a line repeated in the remote JMX
    branch); without, outside any block, the lines in between leaving its ifs (fi, else) too (the remote JMX
    branch's password file line, the stock one after fi). The later one sets the same values last, the JVM gets the
    same whether the first one is there or not."""
    if not line["depth"] or _plain_options(line) is None:
        return False
    for other in later:
        if other["text"] == line["text"] and (other["depth"] == line["depth"] if in_block else not other["depth"]):
            return True
        if not (_plain_line(other) or (not in_block and _leaves_if(other))):
            return False
    return False


def repeated_lines(text, other):
    """The numbers of cassandra-env.sh's lines adding plain options to JVM_OPTS in a block that the same block writes
    again the same below them (_set_again: the last one counts), when the other side's cassandra-env.sh (other) has
    that line too: the JVM gets the same without them, the rest is compared with the other side's; a line the other
    side does not have stays, each copy of it shown. [] when either is not read line by line."""
    try:
        lines, theirs = env_lines(text), set(line["text"] for line in env_lines(other))
    except NotSame:
        return []
    return [line["n"] for i, line in enumerate(lines)
            if line["text"] in theirs and _set_again(line, lines[i + 1:], in_block=True)]


LOCAL_JMX_TEST = 'if [ "$LOCAL_JMX" = yes ]; then'  # (as env_lines writes it)


# a line adding to JVM_OPTS, whatever it adds (but JVM_OPTS itself)
APPEND_ANY = re.compile(r'^(?:export )?JVM_OPTS="\$(?:JVM_OPTS|\{JVM_OPTS\}) ([^"]*)"$')


def _reads_jvm_opts(lines):
    """Whether one of lines reads or sets JVM_OPTS otherwise than adding options to it (a grep deciding on it)."""
    return any("JVM_OPTS" in line["text"] and not (APPEND_ANY.match(line["text"]) and "JVM_OPTS" not in
                                                   APPEND_ANY.match(line["text"]).group(1)) for line in lines)


def _block_end(lines, at):
    """The index of the line after the block lines[at] opens (after its fi)."""
    return next(i for i in range(at + 1, len(lines)) if lines[i]["depth"] == lines[at]["depth"])


REMOTE_PORT, LOCAL_PORT = "-Dcassandra.jmx.remote.port=", "-Dcassandra.jmx.local.port="


def _remote_branch_taken(node, at_else, end, environment, cmdline):
    """Whether bash takes the remote JMX branch (node[at_else + 1:end]): the running JVM got the remote port (that
    branch's own line) and not the local one, LOCAL_JMX not yes in the environment. Not from the file's default: an
    EnvironmentFile or /etc/default/cassandra may give LOCAL_JMX, unseen; Cassandra not running: not known."""
    args = re.split(r"[\0\n]", str(cmdline or ""))
    remote = [a for a in args if a.startswith(REMOTE_PORT)]
    local = [a for a in args if a.startswith(LOCAL_PORT)]
    own = any(line["depth"] == 1 and APPEND_ANY.match(line["text"]) and REMOTE_PORT in line["text"]
              for line in node[at_else + 1:end])
    return bool(remote) and not local and own and (environment or {}).get("LOCAL_JMX") != "yes"


# The LOCAL_JMX test's block in a file: the index of its if, of its else (None: not looked for), of its end
Block = namedtuple("Block", "start at_else end")


def remote_branch_moved(live, role, environment=None, cmdline=""):
    """The node's lines adding plain options to JVM_OPTS in the remote JMX branch (the else of its LOCAL_JMX test),
    which bash takes (_remote_branch_taken: the running JVM's arguments, cmdline, say so), that the role's file has
    once outside any block (the rmi hostname above the test, the password and access files below it): the JVM gets
    the same, when no other line of either file sets their options and none between the two places reads or resets
    JVM_OPTS (a grep deciding on it) -> ([their line numbers in live], [in role]); ([], []) when not proven (the
    local branch taken, or not known)."""
    try:
        node, role_lines = env_lines(live), env_lines(role)
    except NotSame:
        return [], []
    node = [line for i, line in enumerate(node) if not _set_again(line, node[i + 1:], in_block=True)]
    tests = [[i for i, line in enumerate(lines) if line["text"] == LOCAL_JMX_TEST and not line["depth"]]
             for lines in (node, role_lines)]
    if any(len(found) != 1 for found in tests):
        return [], []
    start, end = tests[0][0], _block_end(node, tests[0][0])
    at_else = next((i for i in range(start + 1, end) if node[i]["depth"] == 1 and node[i]["text"] == "else"), None)
    if at_else is None or not _remote_branch_taken(node, at_else, end, environment, cmdline):
        return [], []
    node_block = Block(start, at_else, end)
    role_block = Block(tests[1][0], None, _block_end(role_lines, tests[1][0]))
    branch = set(line["text"] for line in node[at_else + 1:end] if line["depth"] == 1 and APPEND.match(line["text"]))
    moved_live, moved_role = [], []
    for i in range(at_else + 1, end):
        j = _moved_to(node, role_lines, i, node_block, role_block, branch)
        if j is not None:
            moved_live.append(node[i]["n"])
            moved_role.append(role_lines[j]["n"])
    return moved_live, moved_role


def _moved_to(node, role_lines, i, node_block, role_block, branch):
    """The index in role_lines of the remote branch's line node[i] when the JVM gets the same from it there (see
    remote_branch_moved), else None. node_block, role_block: the LOCAL_JMX test's Block in each file; branch: the
    texts of the lines adding to JVM_OPTS in the node's remote branch."""
    line = node[i]
    options = _plain_options(line) if line["depth"] == 1 else None
    if options is None:
        return None
    in_role = [j for j, other in enumerate(role_lines) if other["text"] == line["text"]]
    if len(in_role) != 1 or role_lines[in_role[0]]["depth"] or [other["text"] for other in node].count(line["text"]) != 1:
        return None
    j = in_role[0]
    if _set_elsewhere(set(_jvm_name(o) for o in options), (node, i), (role_lines, j)):
        return None  # (set elsewhere too: its place counts)
    between = _lines_between(node, role_lines, i, j, node_block, role_block, branch)
    if between is None:
        return None
    between = [other for other in between if other["text"] not in branch or other["depth"]]
    return None if _reads_jvm_opts(between) else j


def _set_elsewhere(names, *places):
    """Whether a line of either file, but the one at each place ((lines, index)), names an option of names."""
    return any(_jvm_name(w) in names for lines, k in places for n, other in enumerate(lines) if n != k
               for w in re.split(r"[\s\"'=]+", other["text"]) if w.startswith("-"))


def _lines_between(node, role_lines, i, j, node_block, role_block, branch):
    """The lines bash runs between the two places of a moved line (node[i] in the remote branch, role_lines[j] above
    or below the role's test): the role's lines from its line to its test's block, the node's own branch lines from
    this one to the block's end on that side, and the node's lines from its block to the same anchor (the anchor: the
    role's nearest line on the far side of its own, not one of the branch's, found in the node as the same occurrence
    of its text counted from the block). None when not found (the anchor's occurrence missing in the node,
    role_lines[j] inside the role's test)."""
    if j < role_block.start:
        return _lines_above(node, role_lines, i, j, node_block, role_block, branch)
    if j >= role_block.end:
        return _lines_below(node, role_lines, i, j, node_block, role_block, branch)
    return None


def _lines_above(node, role_lines, i, j, node_block, role_block, branch):
    """_lines_between, the role's line above its test."""
    anchor = next((n for n in range(j - 1, -1, -1) if role_lines[n]["text"] not in branch), None)
    occurrence = 0 if anchor is None else sum(1 for n in range(anchor, role_block.start)
                                              if role_lines[n]["text"] == role_lines[anchor]["text"])
    in_node = [] if anchor is None else [n for n in range(node_block.start)
                                         if node[n]["text"] == role_lines[anchor]["text"]]
    if occurrence > len(in_node):
        return None
    return role_lines[(anchor if anchor is not None else -1) + 1:role_block.start + 1] + \
        node[in_node[-occurrence] + 1 if occurrence else 0:node_block.start + 1] + node[node_block.at_else:i]


def _lines_below(node, role_lines, i, j, node_block, role_block, branch):
    """_lines_between, the role's line below its test."""
    anchor = next((n for n in range(j + 1, len(role_lines)) if role_lines[n]["text"] not in branch), None)
    occurrence = 0 if anchor is None else sum(1 for n in range(role_block.end, anchor + 1)
                                              if role_lines[n]["text"] == role_lines[anchor]["text"])
    in_node = [] if anchor is None else [n for n in range(node_block.end, len(node))
                                         if node[n]["text"] == role_lines[anchor]["text"]]
    if occurrence > len(in_node):
        return None
    return role_lines[role_block.end:anchor if anchor is not None else len(role_lines)] + \
        node[node_block.end:in_node[occurrence - 1] if occurrence else len(node)] + node[i + 1:node_block.end]


def _local_jmx_branches(lines):
    """(then lines, else lines) of the one LOCAL_JMX test outside any block; None when not one."""
    tests = [i for i, line in enumerate(lines) if line["text"] == LOCAL_JMX_TEST and not line["depth"]]
    if len(tests) != 1:
        return None
    end = _block_end(lines, tests[0])
    at_else = next((i for i in range(tests[0] + 1, end) if lines[i]["depth"] == 1 and lines[i]["text"] == "else"), end)
    return lines[tests[0] + 1:at_else], lines[at_else + 1:end]


def jmx_branch_run(live, environment=None, cmdline=""):
    """The branch of the node's LOCAL_JMX test (live: its cassandra-env.sh) the running JVM took, which the next start
    takes too. Its arguments (cmdline) tell it: its local JMX port alone, the local one (the then); its remote port
    alone, the remote one (the else); LOCAL_JMX not set otherwise in the unit's environment -> "local" or "remote";
    None when not known (no running JVM, both ports or none, not one test, a branch without its own port line at
    depth 1: the port may come from elsewhere)."""
    args = re.split(r"[\0\n]", str(cmdline or ""))
    local = any(a.startswith(LOCAL_PORT) for a in args)
    if local == any(a.startswith(REMOTE_PORT) for a in args) \
            or (environment or {}).get("LOCAL_JMX") not in (None, "", "yes" if local else "no"):
        return None
    try:
        branches = _local_jmx_branches(env_lines(live))
    except NotSame:
        return None
    # (each branch with its own port line, as stock: the port the JVM has then tells the branch it ran)
    if branches is None or not all(
            any(line["depth"] == 1 and APPEND_ANY.match(line["text"]) and port in line["text"] for line in branch)
            for branch, port in zip(branches, (LOCAL_PORT, REMOTE_PORT))):
        return None
    return "local" if local else "remote"


def jmx_branch_not_run(live, role, environment=None, cmdline=""):
    """The node's lines adding to JVM_OPTS in the branch of its LOCAL_JMX test the running JVM did not take
    (jmx_branch_run) that the role's file does not have in that branch. bash does not run them: no effect, with them
    or without -> (the branch run: "local" or "remote", [their line numbers in live]); (None, []) when none or not
    known."""
    run = jmx_branch_run(live, environment, cmdline)
    if run is None:
        return None, []
    try:
        mine, theirs = _local_jmx_branches(env_lines(live)), _local_jmx_branches(env_lines(role))
    except NotSame:
        return None, []
    if theirs is None:
        return None, []
    side = 1 if run == "local" else 0
    known = set(line["text"] for line in theirs[side])
    found = [line["n"] for line in mine[side]
             if line["depth"] == 1 and line["text"] not in known and APPEND_ANY.match(line["text"])]
    return run if found else None, found


HEAPDUMP_SET = re.compile(r'^CASSANDRA_HEAPDUMP_DIR=("?)(/[^\s"\'$`\\#;]*)\1\s*$')
HEAPDUMP_COMMENT = "# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR"
HEAPDUMP_IF = 'if [ "x$CASSANDRA_HEAPDUMP_DIR" != "x" ]; then'


def heap_dump_dir_as_role(text, environment, cmdline, heapdump_before=""):
    """cassandra-env.sh (4.x) with its plain CASSANDRA_HEAPDUMP_DIR=<dir> above the stock if that reads it (its stock
    comment above it, or none) written as the role writes cassandra_heap_dump_dir, "${CASSANDRA_HEAPDUMP_DIR:-<dir>}":
    the same dir for the JVM when its environment gives no CASSANDRA_HEAPDUMP_DIR (a running JVM, cmdline: its
    environment read, environment holding its CASSANDRA_HEAPDUMP_DIR; and no cassandra.in.sh bin/cassandra may source
    names it, heapdump_before: the ones that do; else as it is: not proven) -> (text, the dir or ""). The import,
    its self-check and apply_config's proof of no restart read it so, one rule.
    (The JVM's environment is what bin/cassandra exported after cassandra-env.sh ran: the plain line shows there only
    when something exported the variable before, the unit, an EnvironmentFile, /etc/default/cassandra; absent, nothing
    did, and the role's line falls back to the same dir. A cassandra.in.sh, sourced by bin/cassandra in the same shell
    before cassandra-env.sh, may set it unexported, unseen there: playbooks/files/jvm_proof.sh, heapdump_before.)"""
    lines = str(text or "").split("\n")
    if not cmdline or str(heapdump_before or "").strip() or (environment or {}).get("CASSANDRA_HEAPDUMP_DIR") \
            or sum(1 for line in lines if "CASSANDRA_HEAPDUMP_DIR=" in line) != 1:
        return text, ""
    at = next((i for i, line in enumerate(lines) if HEAPDUMP_SET.match(line)), None)
    after = next((line for line in lines[at + 1:] if line.strip()), None) if at is not None else None
    if after is None or after.strip() != HEAPDUMP_IF:
        return text, ""
    folder = HEAPDUMP_SET.match(lines[at]).group(2)
    start = at - 1 if at and lines[at - 1].strip() == HEAPDUMP_COMMENT else at
    lines[start:at + 1] = ['CASSANDRA_HEAPDUMP_DIR="${CASSANDRA_HEAPDUMP_DIR:-%s}"  # set jvm HeapDumpPath with it'
                           % folder]
    return "\n".join(lines), folder


def without_lines(text, numbers):
    """text without its lines of those numbers (1 the first)."""
    return "\n".join(line for n, line in enumerate(str(text).split("\n")) if n + 1 not in set(numbers))


def _ops_of(role, live):
    """The differing opcodes of env_lines role -> live (a line in a block differs from the same line outside any)."""
    matcher = difflib.SequenceMatcher(None, [(line["depth"] > 0, line["text"]) for line in role],
                                      [(line["depth"] > 0, line["text"]) for line in live], autojunk=False)
    return [op for op in matcher.get_opcodes() if op[0] != "equal"]


def named_lines_moved(live, role, names, environment=None):
    """The node's lines adding to JVM_OPTS, outside any block, options each starting with one of names (the ones a
    node may switch on at another place than the stock one: cassandra_import's ELSEWHERE), that the role's file has
    once outside any block at another place (_moved_lines, the JVM-args proof's own test: plain options set by no
    other line of either file): bash adds the same, wherever the line sits, when no line of either file reads
    JVM_OPTS but to add options to it or to grep for others (a reset, a grep finding them) and the environment
    gives cassandra-env.sh nothing to add (JVM_EXTRA_OPTS, MX4J_*: added at the end, before the node's line there
    and after the role's) -> ([their line numbers in live], [in role]). No running JVM needed: the same for the
    import, its self-check and apply_config, Cassandra running or not."""
    try:
        node, ours = env_lines(live), env_lines(role)
    except NotSame:
        return [], []
    if any((environment or {}).get(name) for name in JVM_ENV if name != "JVM_OPTS"):
        return [], []
    greps = [p for lines in (node, ours) for dummy, p in _greps(lines)]
    others = [line for line in node + ours if not APPEND_ANY.match(line["text"]) and not GREP.match(line["text"])]
    if _reads_jvm_opts(others):
        return [], []
    moved = _moved_lines(node, ours, _ops_of(ours, node))
    moved = [t for t in moved if all(any(o.startswith(n) for n in names) for o in APPEND.match(t).group(1).split())
             and not any(p in APPEND.match(t).group(1) for p in greps)
             and not any(line["depth"] for line in node if line["text"] == t)]
    return ([line["n"] for line in node if line["text"] in moved], [line["n"] for line in ours if line["text"] in moved])


PASSWORD_FILE = "-Dcom.sun.management.jmxremote.password.file"
# what makes a JVM read the JMX password file: JMX authentication (the last -D...authenticate=true: Cassandra's
# JMXServerUtils reads the file only then), the JDK's own JMX agent (its port, or a management.properties that may
# give it one), a JAAS login (Cassandra's own, or the JDK's) or Cassandra's JMX authorizer. Not the JDK's default
# management.properties (<java home>/conf/management/), which could give the agent a port too: commented out in the
# JDKs' own, not read here
PASSWORD_FILE_READERS = ("-Dcom.sun.management.jmxremote.port=", "-Dcom.sun.management.config.file=",
                         "-Dcassandra.jmx.remote.login.config=", "-Djava.security.auth.login.config=",
                         "-Dcassandra.jmx.authorizer=")


# the variables the Java launcher adds options from, which /proc/<pid>/cmdline does not show
LAUNCHER_ENV = ("_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "JAVA_TOOL_OPTIONS")


def _shown_args(cmdline, environment=None):
    """A Cassandra JVM's arguments (cmdline: its /proc/<pid>/cmdline) when they are all it gets -> the list; None
    when not known to be: not Cassandra's (no MAIN_CLASS), a file of options read where it is (IN_PLACE), or a
    LAUNCHER_ENV variable set in its environment (options its command line does not show)."""
    args = [a for a in re.split(r"[\0\n]", str(cmdline or "")) if a]
    if MAIN_CLASS not in args or any(IN_PLACE.match(a) for a in args) \
            or any((environment or {}).get(name) for name in LAUNCHER_ENV):
        return None
    return args


def password_file_inert(cmdline, environment=None):
    """Whether the running JVM (cmdline: its /proc/<pid>/cmdline; environment: its own, LAUNCHER_ENV read there)
    never reads -Dcom.sun.management.jmxremote.password.file, so that a JVM with the same arguments but that one gets
    nothing else: JMX authentication off (the last -Dcom.sun.management.jmxremote.authenticate not true, as
    Boolean.getBoolean reads it; none: off), none of PASSWORD_FILE_READERS, and no option its command line does not
    show (a LAUNCHER_ENV variable, a file of options read where it is: IN_PLACE). Not the access file: Cassandra hands
    it to the JMX server whatever the authentication, which reads it at start."""
    args = _shown_args(cmdline, environment)
    if args is None:
        return False
    auth = [a.split("=", 1)[1] if "=" in a else "" for a in args
            if a.split("=", 1)[0] == "-Dcom.sun.management.jmxremote.authenticate"]
    return not (auth and auth[-1].strip().lower() == "true") \
        and not any(a.startswith(PASSWORD_FILE_READERS) for a in args)


# what names the JMX password and access files to a JVM: those options, or a management.properties that may
JMX_FILE_OPTIONS = ("-Dcom.sun.management.jmxremote.password.file=", "-Dcom.sun.management.jmxremote.access.file=",
                    "-Dcom.sun.management.config.file=")


def jmx_files_unread(live, cmdline, environment=None):
    """Whether the running JVM reads neither jmxremote.password nor jmxremote.access: the local JMX branch taken
    (jmx_branch_run, live: the node's cassandra-env.sh; environment: the JVM's own, LAUNCHER_ENV read there), its
    arguments naming neither file (JMX_FILE_OPTIONS), and no option its command line does not show (a LAUNCHER_ENV
    variable, IN_PLACE)."""
    args = _shown_args(cmdline, environment)
    if args is None:
        return False
    return jmx_branch_run(live, environment, cmdline) == "local" \
        and not any(a.startswith(JMX_FILE_OPTIONS) for a in args)


def password_file_line_inert(live, role, cmdline, environment=None):
    """The role's line giving the JVM -Dcom.sun.management.jmxremote.password.file (its stock one, outside any block)
    on a node whose cassandra-env.sh runs none (its stock line commented out) and whose JVM never reads that file
    (password_file_inert, environment: the JVM's): the same for that JVM -> [its line number in role]; [] when not."""
    if not password_file_inert(cmdline, environment):
        return []
    try:
        node, ours = env_lines(live), env_lines(role)
    except NotSame:
        return []
    if any(PASSWORD_FILE in line["text"] for line in node):
        return []
    found = [line for line in ours if PASSWORD_FILE in line["text"]]
    if len(found) != 1 or found[0]["depth"] or not APPEND.match(found[0]["text"]) \
            or len(APPEND.match(found[0]["text"]).group(1).split()) != 1:
        return []
    return [found[0]["n"]]


def without_jmx_branch_not_run(live, role, environment=None, cmdline=""):
    """The node's cassandra-env.sh (live) without its lines of jmx_branch_not_run: what bash runs the same -> (text,
    how many lines left out, the branch run)."""
    runs, lines = jmx_branch_not_run(live, role, environment, cmdline)
    return (without_lines(live, lines) if lines else live), len(lines), runs


def env_differences(live, role):
    """The lines of the node's cassandra-env.sh not in the role's -> ([its JVM_OPTS lines: {n, text, words}], [its
    heap assignments], [its lines the role's has at another place (_moved_lines)]);
    NotSame when they differ otherwise (only lines outside any block may: a line adding plain options to JVM_OPTS
    on the node's side, a heap assignment on either; and a line adding plain options at another place on both)."""
    added, heap = [], []
    live = [line for i, line in enumerate(live)  # (a line set again outside any block, or in its own block)
            if not (_set_again(line, live[i + 1:], in_block=False) or _set_again(line, live[i + 1:], in_block=True))]
    moved = _moved_lines(live, role, _ops_of(role, live))
    moved_lines = [line for line in live if line["text"] in moved]
    if any(line["text"] in moved and GREP.match(lines[i - 1]["text"]) for lines in (role, live)
           for i, line in enumerate(lines) if i):
        raise NotSame("cassandra-env.sh differs right after a grep (its $? read on the next line)")
    # the rest compared without them (a line moved into a block would else pair the block's end with another)
    role, live = [line for line in role if line["text"] not in moved], [line for line in live if line["text"] not in moved]
    for op, i1, i2, j1, j2 in _ops_of(role, live):
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
    return added, heap, moved_lines


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


# why cassandra-env.sh stops (stock_options' why), in its order
STOPS_HEAP_PAIR, STOPS_XMX_PAIR, STOPS_XMN_XMX, STOPS_XMN_G1 = "heap pair", "xmx pair", "xmn xmx", "xmn g1"


def stock_options(greps, heap, why=None):
    """The options cassandra-env.sh adds from what it found in JVM_OPTS (greps: {pattern: found}) and the heap
    ({name: value}): {name: value, CALC or CORES when the node works it out}; None when it stops (exit 1), why (a
    list) then gets the STOPS_* it stops on."""
    def found(pattern):
        return greps.get(pattern, False)

    def stops(reason):
        if why is not None:
            why.append(reason)
    g1, cms = found("+UseG1GC"), found("UseConcMarkSweepGC")
    heap_size, new_size = heap.get("MAX_HEAP_SIZE"), heap.get("HEAP_NEWSIZE")
    if heap_size is None and (new_size is None or g1):
        heap_size = new_size = CALC
    elif heap_size is None or (new_size is None and not g1):
        return stops(STOPS_HEAP_PAIR)  # "please set or unset MAX_HEAP_SIZE and HEAP_NEWSIZE in pairs"
    added = {}
    if not found("Xmx") and not found("Xms"):
        added["-Xms"] = added["-Xmx"] = heap_size
    elif not (found("Xmx") and found("Xms")):
        return stops(STOPS_XMX_PAIR)
    if "MaxDirectMemorySize" in greps and not found("MaxDirectMemorySize"):
        added["MaxDirectMemorySize"] = heap.get("MAX_DIRECT_MEMORY_SIZE") or CALC
    if found("Xmn") and not found("Xmx"):
        return stops(STOPS_XMN_XMX)
    if not found("Xmn") and cms:
        added["-Xmn"] = new_size
    if found("Xmn") and g1:
        return stops(STOPS_XMN_G1)
    if "ParallelGCThreads" in greps and g1 and not found("ParallelGCThreads") and not found("ConcGCThreads"):
        added["ParallelGCThreads"] = added["ConcGCThreads"] = CORES
    return added


UNNAMED_ORDER = "(the order of the options without a name)"


def _xlog_output(option):
    """The output of an -Xlog:<what>:<output>[:...] as the JVM names it: stdout, stderr, else file=<name> (a bare
    name is a file, its double quotes off, a leading ./ off: the same file); None without an output."""
    m = re.match(r'^-Xlog:[^:"]+:((?:"[^"]*"|[^:"])+)', option)
    if not m:
        return None
    output = m.group(1)
    if output in ("stdout", "stderr", "#0", "#1"):
        return {"#0": "stdout", "#1": "stderr"}.get(output, output)
    if output.startswith("disable"):
        return output
    name = output[len("file="):] if output.startswith("file=") else output
    name = name.replace('"', "")
    while name.startswith("./"):
        name = name[2:]
    return "file=%s" % name


def _kind(option):
    """The kind of an option without a name, its name in a difference: -ea/-da, -esa/-dsa, agents (the Prometheus
    exporter's apart), -Xlog by output (-Xlog:<what>:<output>), the others by their first word."""
    if re.match(r"^-(ea|da|enableassertions|disableassertions)(:|$)", option):
        return "(assertions)"
    if re.match(r"^-(esa|dsa|enablesystemassertions|disablesystemassertions)$", option):
        return "(system assertions)"
    if prometheus.AGENT.match(option):
        return "(the Prometheus exporter's agent)"
    if re.match(r"^-(javaagent|agentlib|agentpath)[:=]", option):
        return "(agents)"
    output = _xlog_output(option)
    if output and not output.startswith("disable"):
        return "-Xlog:%s" % output
    return re.split(r"[:=]", option.split(" ")[0], maxsplit=1)[0]


def _order_counts(kind, options):
    """Whether the place of an option of that kind among the other options without a name counts (UNNAMED_ORDER):
    but for the ones the JVM takes in any order (jvm.in_any_order), the Prometheus exporter's agent (it transforms
    no class) and an -Xlog to a file of its own (another output is apart, unless an -Xloggc may write that file)."""
    if kind == "(the Prometheus exporter's agent)":
        return False
    if kind.startswith("-Xlog:file=") and not any(o.startswith("-Xloggc") for o in options):
        return False
    return True


def effective(args):
    """What the JVM gets from its arguments (the program name and the main class out): {name: [option]} (the last
    one, every one in order for the ones that add up), {kind: [options in order]} for the others (in any order for
    the ones the JVM takes so, jvm.in_any_order) and under UNNAMED_ORDER their kinds in order (_order_counts), the
    unlock options before each -XX option that counts, and in each family of settings the order of their last
    options."""
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
    # -verbose:gc and -Xloggc are -Xlog's too: with an -Xlog, their place among the options without a name counts
    xlog_used = any(re.match(r"^-Xlog(:|$)", o) for o in options)
    for position, option in enumerate(options):
        name = _jvm_name(option)
        if name is None:
            if jvm.in_any_order(option):  # (each kind in any order, sorted below)
                unnamed.setdefault(_kind(option), []).append(option)
                continue
            kind = _kind(option)
            kind = kind if xlog_apart or not kind.startswith("-Xlog") else "-Xlog"
            unnamed.setdefault(kind, []).append(option)
            if _order_counts(kind, options):
                unnamed.setdefault(UNNAMED_ORDER, []).append(kind)
            continue
        if name in LISTS:
            named.setdefault(name, []).append(option)
        else:
            named[name] = [option]
        last[name] = position
        if name in ("-verbose:gc", "-Xloggc") and xlog_used:
            unnamed.setdefault(UNNAMED_ORDER, []).append(name)
        if option.startswith("-XX:"):
            unlocked[name] = sorted(seen)
        else:
            unlocked.pop(name, None)
        if option.startswith("-XX:+") and name in JVM_UNLOCK:
            seen.add(name)
        elif option.startswith("-XX:-") and name in JVM_UNLOCK:
            seen.discard(name)
    for kind, given in unnamed.items():
        if kind != UNNAMED_ORDER:
            unnamed[kind] = jvm.unnamed_in_order(given)
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


def _analyse(args, live, role, series, java, node_env, earlier=None):
    """The node's side (see the module): where its options files' words are in its arguments, its JVM_OPTS lines
    in cassandra-env.sh and where their options are, what cassandra-env.sh finds in JVM_OPTS, the heap it gets.
    earlier: the stock cassandra-env.sh of an earlier series the node's is (rendered with its values), its lines
    added and heap told from that one's."""
    end = len(args) - 1 - args[::-1].index(MAIN_CLASS)
    words = _files_words(live, series, java)
    start = _find(args[:end], [w for w, dummy, dummy2 in words])
    after_files = start + len(words)
    if ENV not in live or ENV not in role:
        raise NotSame("cassandra-env.sh not read")
    live_lines, role_lines = env_lines(live[ENV]), env_lines(role[ENV])
    added, heap_lines, moved = env_differences(live_lines, role_lines if earlier is None else env_lines(earlier))
    # options the unit gives cassandra-env.sh to add (JVM_EXTRA_OPTS, MX4J_*): a line moved to the options files
    # would go before them, and an option of another spelling among them (-Xint and -Xmixed, -Xrs and
    # -XX:-ReduceSignalUsage, -verbose:gc and -Xlog...) would win instead
    if (added or moved) and any((node_env or {}).get(name) for name in ("JVM_EXTRA_OPTS", "MX4J_ADDRESS", "MX4J_PORT")):
        raise NotSame("the unit gives cassandra-env.sh options to add (JVM_EXTRA_OPTS, MX4J_*)")
    # a line the node has at another place than the role (in a block: the same only when its block ran)
    for line in moved:
        if len(_found(args, APPEND.match(line["text"]).group(1).split(), after_files, end)) != 1:
            raise NotSame("a JVM_OPTS line of cassandra-env.sh at another place is not once in the running JVM's"
                          " arguments")
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


def _launch(cmdline, live, role, series, java, node_env, role_env, earlier=None):
    """The arguments the JVM would get from the role's files (see the module): (the running JVM's arguments, the
    ones it would get); NotSame when that cannot be shown."""
    args = _arguments(cmdline)
    info = _analyse(args, live, role, series, java, node_env, earlier)
    end, start, after_files, before = info["end"], info["start"], info["after_files"], info["greps"]
    role_words = [w for w, dummy, dummy2 in _files_words(role, series, java)]
    hand = set(i for line in info["added"] for i in range(line["at"], line["at"] + len(line["options"])))
    # the node's (env_differences: no grep line differs), but for an earlier series' file (its own, some others)
    patterns = set(p for dummy, p in _greps(info["role_lines"])) | set(before)
    now = dict((p, p in " ".join(args[1:start] + role_words)) for p in patterns)
    if any(before[p] != now[p] for p in GC_GREPS if p in before):
        raise NotSame("cassandra-env.sh would find another GC in the options")
    role_heap, dummy = heap_values(info["role_lines"], role_env)
    live_stock, role_stock = stock_options(before, info["live_heap"]), stock_options(now, role_heap)
    if live_stock is None:
        raise NotSame("cassandra-env.sh's heap could not be followed")
    if role_stock is None:
        raise NotSame("cassandra-env.sh would stop: the heap set without its pair (MAX_HEAP_SIZE and HEAP_NEWSIZE,"
                      " -Xmx and -Xms) or -Xmn with G1")
    if earlier is not None and CALC in [s.get(k) for s in (live_stock, role_stock) for k in ("-Xms", "-Xmx", "-Xmn")]:
        raise NotSame("the heap is worked out on the node, by another formula in the earlier series' cassandra-env.sh")
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


def options_edits(live, role, series, java):
    """The settings the options files the running Java reads set otherwise on the node (live: {file: text}) than in
    the role's (role), none of them in either cassandra-env.sh: hand edits of those files, said on their own (the
    roles put them back) -> {name: file}."""
    in_env = set(jvm.option_name(w) for text in (live.get(ENV, ""), role.get(ENV, ""))
                 for w in re.split(r"[\s\"'=]+", text) if w.startswith("-"))
    found = {}
    for name in (COMMON, options_file(series, java)):
        if name in live and name in role:
            a, b = jvm_options_settings(live[name]), jvm_options_settings(role[name])
            # (the heap: in one file or the other, cassandra-env.sh greps for it; never apart)
            found.update((k, name) for k in set(a) | set(b)
                         if a.get(k) != b.get(k) and not k.startswith("(") and k not in in_env
                         and k not in ("-Xms", "-Xmx", "-Xmn"))
    return found


def env_differs(node_env, role_env):
    """The variables that give cassandra-env.sh options to add (JVM_ENV) the node's environment and the roles' set
    otherwise ({name: value} each, a value as text: 8081 and "8081" alike; unset, None and empty alike) -> [name]."""
    def text(env, name):
        value = (env or {}).get(name)
        return "" if value is None else str(value)
    return [name for name in JVM_ENV if text(node_env, name) != text(role_env, name)]


def cassandra_jvm_args_same(cmdline, live, role, series, java, node_env=None, role_env=None, apart=(), earlier=None):
    """Whether the role's files (role: {file: text}) give the JVM the same arguments as the node's (live) did
    (cmdline: its /proc/<pid>/cmdline; node_env, role_env: the environment the unit gives Cassandra now, and with
    the roles; apart: the names of the settings that differ by hand edits of the options files, not counted;
    earlier: the earlier series' stock cassandra-env.sh the node's is, rendered, EARLIER_APART not counted)
    -> (True, '') or (False, why)."""
    return jvm_args_compared(cmdline, live, role, series, java, node_env, role_env, apart, earlier)[:2]


def jvm_args_compared(cmdline, live, role, series, java, node_env=None, role_env=None, apart=(), earlier=None):
    """cassandra_jvm_args_same, and what the role's files give the JVM at its next start that the comparison leaves
    out as no effect now: the -XX:HeapDumpPath an earlier series' file did not add (earlier), the role's JMX password
    file line the JVM never reads (password_file_line_inert) -> (same, why, ["<option> added"])."""
    differ = env_differs(node_env, role_env)
    if differ:
        return False, "the roles would give cassandra-env.sh other options to add (%s)" % ", ".join(differ), []
    inert = []
    if ENV in live and ENV in role:  # (the node's lines in the JMX branch it does not run: no effect)
        live = dict(live, **{ENV: without_jmx_branch_not_run(live[ENV], role[ENV], node_env, cmdline)[0]})
        # (and the role's password file line where the node has it commented out, the JVM never reading the file)
        numbers = password_file_line_inert(live[ENV], role[ENV], cmdline, node_env)
        inert = [APPEND.match(line["text"]).group(1) for line in env_lines(role[ENV]) if line["n"] in numbers] \
            if numbers else []
        role = dict(role, **{ENV: without_lines(role[ENV], numbers)})
    try:
        args, after = _launch(cmdline, live, role, series, java, node_env, role_env, earlier)
    except NotSame as exc:
        return False, str(exc), []
    apart = tuple(apart) + (EARLIER_APART if earlier is not None else ())
    if any(IN_PLACE.match(o) for o in args + after):  # (read at each start: the same words may give other options)
        return False, "an option reads a file of options where it is (-XX:VMOptionsFile, -XX:Flags, @file)", []
    names = [n for n in differences(args[1:], after[1:]) if n not in apart]
    if names:  # their names only (-Xlog:file=<path>: -Xlog, -Xmaxjitcodesize240M: -Xmaxjitcodesize)
        names = sorted(set(re.sub(r"(?<=^-X[a-z])([a-z]*?)\d.*$", r"\1", re.split(r"[:=]", n)[0]) for n in names))
        return False, "the JVM would get other values of %s" % ", ".join(mask(n) for n in names), []
    later = ["%s added (the JVM never reads that file: JMX authentication off)" % o for o in inert]
    if earlier is not None and not any(_jvm_name(a) == "HeapDumpPath" for a in args):
        # (the later series' file sets it always, in CASSANDRA_HEAPDUMP_DIR: the environment's, else its default)
        folder = (node_env or {}).get("CASSANDRA_HEAPDUMP_DIR") or next(
            (m.group(1) for m in re.finditer(r'^\s*CASSANDRA_HEAPDUMP_DIR="([^"]*)"', role[ENV], re.M)), "")
        logdir = next((a.split("=", 1)[1] for a in args if a.startswith("-Dcassandra.logdir=")), "")
        if logdir:  # (bin/cassandra's log dir, as the JVM got it)
            folder = re.sub(r"\$(CASSANDRA_LOG_DIR\b|\{CASSANDRA_LOG_DIR\})", lambda m: logdir, folder)
        later.insert(0, "-XX:HeapDumpPath=%s/cassandra-<start>-pid<pid>.hprof added (no effect until a heap dump)" % folder)
    return True, "", [mask(change) for change in later]


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


def cassandra_jvm_args_adopt(cmdline, live, render, variables, series, java, node_env=None, earlier=None):
    """The variables that move the node's hand edits of cassandra-env.sh (its JVM_OPTS lines, its heap set
    elsewhere) to where the role writes them, when the JVM gets the same arguments from the role's files.
    render(variables): {file: text} the role writes; variables: the ones imported so far; node_env: the
    environment the unit gives Cassandra. -> {vars, moved, dropped: report lines, apart: the options files with hand
    edits of their own (said as such, not counted)}; {why: the reason, names only} when there are such edits but the
    same arguments cannot be shown; None when there are none. earlier: as for cassandra_jvm_args_same."""
    dep = options_file(series, java)
    if not cmdline or dep is None or ENV not in live:
        return None
    role = render(variables)
    try:
        args = _arguments(cmdline)
        info = _analyse(args, live, role, series, java, node_env, earlier)
        role_heap, dummy = heap_values(info["role_lines"], node_env)
    except NotSame as exc:
        return {"why": str(exc)} if env_has_candidates(live[ENV], role[ENV]) else None
    env_same = [line["text"] for line in info["live_lines"]] == [line["text"] for line in info["role_lines"]]
    heap_moved = any(jvm_options_settings(live[f]).get(k) != jvm_options_settings(role[f]).get(k)
                     for f in (COMMON, dep) if f in live and f in role for k in ("-Xms", "-Xmx"))
    if not info["added"] and not info["heap_lines"] and env_same and not heap_moved:
        return None
    # the options files' own hand edits: said on their own, not in the way of the move (unless the file the lines go
    # to has some: its hand edits would mix with them)
    apart = options_edits(live, role, series, java)
    if dep in apart.values():
        apart = {}
    end = info["end"]
    # the heap cassandra-env.sh gets, set where the role writes it
    heap_vars = {}
    for name, value in info["live_heap"].items():
        if value != role_heap.get(name) and HEAP_VARS[name].get(series):
            if value is not None and _plain_value(value) is None:
                return {"why": "cassandra-env.sh sets %s from what only the shell knows" % name}
            heap_vars[HEAP_VARS[name][series]] = value or ""
    keep, dropped = _kept_options(info, args)
    extras_var = "cassandra_%s_extra_options" % dep.split("-", maxsplit=1)[0]
    by_vars, to_vars = _heap_options_vars(keep, heap_vars, series, variables, info, args[1:end])
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
                                            node_env, node_env, apart, earlier)
        if same:
            moved = _moved(info, found, options, extras_var)
            # the files are rewritten for cassandra-env.sh's lines: their own extra options set again later are left
            # out too (not for lines at another place, nor the heap of an options file: those files stay as they are)
            tidy, gone = _without_dead(args, info, variables, found, live, series, java, set(apart.values())) \
                if info["added"] or info["heap_lines"] else ({}, [])
            if gone and cassandra_jvm_args_same(cmdline, live, render(dict(variables, **dict(found, **tidy))), series,
                                                java, node_env, node_env, apart, earlier)[0]:
                found.update(tidy)
                dropped += gone
            return {"vars": found, "moved": moved, "dropped": dropped, "apart": sorted(set(apart.values()))}
    return {"why": why}


def _kept_options(info, args):
    """The options of cassandra-env.sh's added lines the JVM keeps (none set again later), and the report lines
    of the ones left out (set again or overridden later, a heap assignment after its last use)."""
    end, keep = info["end"], []
    dropped = ["%s line %d: %s: set after the last use of %s (no effect), left out" % (
        ENV, line["n"], mask(line["text"]), SET.match(line["text"]).group(1)) for line in info["dead"]]
    for line in info["added"]:
        for k, option in enumerate(line["options"]):
            later = _later(args, line["at"] + k, end)
            if later is None:
                keep.append(option)
            else:
                dropped.append("%s line %d: %s: %s by %s later, left out" % (
                    ENV, line["n"], mask(option), "set again" if args[later] == option else "overridden",
                    mask(args[later])))
    return keep, dropped


def _heap_options_vars(keep, heap_vars, series, variables, info, before_main):
    """The heap options kept (-Xms and -Xmx alike, -Xmn, MaxDirectMemorySize) as the heap variables, over
    heap_vars -> (the variables, the options they replace); before_main: the JVM's arguments up to its main class.
    Else (or when that is not the same) every option goes to the extra options."""
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
        xmn = [a for a in before_main if a.startswith("-Xmn")]
        if xmn:
            by_vars["cassandra_heap_newsize"] = xmn[-1][4:]
    return by_vars, to_vars


def env_has_candidates(live, role):
    """Whether the node's cassandra-env.sh has JVM_OPTS lines of plain options or heap assignments the role's has
    not (wherever they are)."""
    try:
        theirs = set(line["text"] for line in env_lines(role))
        return any((APPEND.match(line["text"]) or SET.match(line["text"])) and line["text"] not in theirs
                   for line in env_lines(live))
    except NotSame:
        return False


def _without_dead(args, info, variables, found, live, series, java, apart=()):
    """The extra options variables of the options files the running Java reads (variables: as imported, found: the
    move's) without the lines the node had whose options are all set again later in the arguments -> ({variable:
    lines}, [report lines])."""
    words, start, end = info["words"], info["start"], info["end"]
    tidy, gone = {}, []
    for name in [f for f in (COMMON, options_file(series, java)) if f not in apart]:  # (apart: compared as they are)
        var = "cassandra_%s_extra_options" % ("jvm" if name == COMMON else name.split("-", maxsplit=1)[0])
        lines = list(found.get(var, variables.get(var)) or [])
        for entry in list(variables.get(var) or []):
            at = [start + i for i, (dummy, f, n) in enumerate(words)
                  if f == name and _line(live[name], n) == entry.strip()]
            later = [_later(args, i, end) for i in at]
            if at and all(k is not None for k in later):
                lines.remove(entry)
                gone.append("%s line %d: %s: %s by %s later, left out" % (
                    name, words[at[0] - start][2], mask(entry), "set again" if args[later[-1]] == entry
                    else "overridden", mask(args[later[-1]])))
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
            went = "%s: %s" % (var[0], mask(found[var[0]]))
        elif var or info["live_heap"].get(name) != value:
            went = "left out, set again later"
        lines.append("%s line %d: %s -> %s" % (ENV, line["n"], mask(line["text"]), went))
    for line in info["added"]:
        went = []
        for option in line["options"]:
            var = HEAP_OPTIONS.get(_jvm_name(option))
            where = extras_var if option in options else (
                "%s: %s" % (var, mask(found[var])) if var in found else "left out")
            if where not in went:
                went.append(where)
        lines.append("%s line %d: %s -> %s" % (ENV, line["n"], " ".join(mask(o) for o in line["options"]) or "(no option)",
                                               "; ".join(went) or "left out"))
    if found.get("cassandra_heap_newsize") and not any("cassandra_heap_newsize" in line for line in lines):
        lines.append("the young generation the JVM runs with (MAX_HEAP_SIZE needs it without G1) -> "
                     "cassandra_heap_newsize: %s" % mask(found["cassandra_heap_newsize"]))
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
    major = jvm.java_major(java)
    for name in sorted(live):
        m = re.match(r"^jvm(\d+)-server\.options$", name)
        if m and name != dep and not ("\r" in live[name]):
            lines += ["%s line %d: %s: not read, Java %d reads %s" % (name, n, mask(word), major, dep)
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
    lines += ["%s: %s: %s" % (where, mask(o), OBSOLETE[_jvm_name(o)][1]) for o, where in mine
              if _jvm_name(o) in OBSOLETE and major >= OBSOLETE[_jvm_name(o)][0]]
    # Java 15 to 17: biased locking off by default, -XX:-UseBiasedLocking sets it as it is
    lines += ["%s: %s: Java %d's default anyway; %s" % (where, o, major, jvm.BIASED_DEPRECATED)
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
                source[i], mask(args[i]), "set again" if args[later] == args[i] else "overridden",
                mask(args[later]), source.get(later, "added by cassandra-env.sh or bin/cassandra")))
    return lines


class FilterModule(object):
    def filters(self):
        return {}
