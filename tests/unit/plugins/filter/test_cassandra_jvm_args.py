from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra-env.sh's hand edits moved to cassandra_config's variables: only when the JVM gets the same arguments
# from the files the role writes. What the JVM gets is not taken from the code under test: bash runs the files
# (cassandra.in.sh's reading of the options files, then cassandra-env.sh, as bin/cassandra does), for the node's
# files and for the role's.

import os
import re
import shutil
import subprocess

import pytest

from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    _render as role_render,
    cassandra_import_self_check,
    cassandra_inventory_host_vars,
)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout

from .test_cassandra_import_check import FACTS, check, import_node, seeded, stock

# bin/cassandra up to the JVM: the options files of the running Java, cassandra-env.sh, its arguments. What the
# node works out (memory, cpus, the date) is set so that two runs give the same.
LAUNCH = r"""
CASSANDRA_CONF=$1; JAVA_VERSION=$2; CASSANDRA_HOME=/opt/cassandra; JVM_ARCH=64-Bit; CASSANDRA_LOG_DIR=$PWD/logs
free() { echo "Mem: 16000 1 1"; }
egrep() { echo 8; }
date() { echo 1700000000; }
if [ $JAVA_VERSION -ge 17 ] && [ -f $CASSANDRA_CONF/jvm17-server.options ]; then DEP=jvm17-server.options
elif [ $JAVA_VERSION -ge 11 ]; then DEP=jvm11-server.options; else DEP=jvm8-server.options; fi
for opt in `grep "^-" $CASSANDRA_CONF/jvm-server.options` `grep "^-" $CASSANDRA_CONF/$DEP`; do
  JVM_OPTS="$JVM_OPTS $opt"
done
. $CASSANDRA_CONF/cassandra-env.sh
printf '%s\0' /usr/bin/java $JVM_OPTS "$JVM_ON_OUT_OF_MEMORY_ERROR_OPT" -Dlogback.configurationFile=logback.xml \
  -Dcassandra.logdir=$CASSANDRA_LOG_DIR -Dcassandra.storagedir=/var/lib/cassandra -cp /opt/cassandra/lib/a.jar \
  org.apache.cassandra.service.CassandraDaemon
"""
JAVA = {"40x": "8", "41x": "11", "50x": "17"}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash runs the files")


def launch(files, java, where, environment=None, paths=False):
    """The arguments bin/cassandra gives the JVM with these files (pid and paths of the run made the same, but with
    paths true); environment: the unit's."""
    conf = os.path.join(str(where), "conf")
    if os.path.isdir(conf):
        shutil.rmtree(conf)
    os.makedirs(conf)
    os.makedirs(os.path.join(str(where), "logs"), exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(conf, name), "w") as f:
            f.write(text)
    with open(os.path.join(str(where), "launch.sh"), "w") as f:
        f.write(LAUNCH)
    run = subprocess.run(["bash", "launch.sh", conf, str(java)], cwd=str(where), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False, env=dict(os.environ, **(environment or {})))
    assert run.returncode == 0, run.stderr
    text = run.stdout.decode()
    return text if paths else re.sub(r"pid\d+", "pid0", text.replace(str(where), "/run"))


# the HotSpot flags an option sets (the -X forms set more than one)
X_FLAGS = {"-Xmx": ("MaxHeapSize",), "-Xms": ("InitialHeapSize", "MinHeapSize"), "-Xmn": ("NewSize", "MaxNewSize"),
           "-Xss": ("ThreadStackSize",)}
ADD_UP = ("OnError", "OnOutOfMemoryError", "CompileCommand", "CompileOnly")


def settings(cmdline):
    """What the JVM is set to from its arguments, worked out here apart from the code under test: each HotSpot
    flag's last value (-Xmn sets NewSize and MaxNewSize...), each -D's, the flags that add up in order, the other
    options as a whole."""
    args = cmdline.split("\0")
    args = args[1:args.index("org.apache.cassandra.service.CassandraDaemon")]
    flags, others, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        x = re.match(r"^(-X(?:mx|ms|mn|ss))(.+)$", arg)
        xx = re.match(r"^-XX:(?:([+-])(\w+)|(\w+)=(.*))$", arg)
        if arg == "-cp":
            i += 1
        elif x:
            flags.update((flag, x.group(2)) for flag in X_FLAGS[x.group(1)])
        elif arg.startswith("-Xloggc:"):  # Java 8: the GC log, PrintGC and PrintGCTimeStamps on
            flags.update(Xloggc=arg[8:], PrintGC="+", PrintGCTimeStamps="+")
        elif arg == "-verbose:gc":
            flags["PrintGC"] = "+"
        elif xx and xx.group(2):
            flags[xx.group(2)] = xx.group(1)
        elif xx and xx.group(3) in ADD_UP:
            flags.setdefault(xx.group(3), []).append(xx.group(4))
        elif xx:
            flags[xx.group(3)] = xx.group(4)
        elif arg.startswith("-D"):
            flags[arg.split("=", 1)[0]] = arg.split("=", 1)[1] if "=" in arg else ""
        else:
            others.append(arg)
        i += 1
    return flags, sorted(others)


def node_files(series, edit=None):
    files = seeded(stock(series))
    if edit:
        edit(files)
    return files


def before_extra(line):
    """An edit adding line(s) to cassandra-env.sh above its last line (JVM_EXTRA_OPTS)."""
    def edit(files):
        files["cassandra-env.sh"] = files["cassandra-env.sh"].replace(
            'JVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"', line + '\nJVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"')
    return edit


def after_line(line, lines):
    """An edit adding lines right after a stock line of cassandra-env.sh."""
    def edit(files):
        assert line in files["cassandra-env.sh"], line
        files["cassandra-env.sh"] = files["cassandra-env.sh"].replace(line, line + "\n" + lines, 1)
    return edit


def on_top(lines):
    def edit(files):
        files["cassandra-env.sh"] = lines + "\n" + files["cassandra-env.sh"]
    return edit


def add_to(name, lines):
    def edit(files):
        files[name] = files[name].rstrip("\n") + "\n" + lines + "\n"
    return edit


def both(*edits):
    def edit(files):
        for e in edits:
            e(files)
    return edit


def imported(files, series, java, cmdline, newer=None, environment=None):
    inventory, layout = import_node(files, series, jvm={"cmdline": cmdline, "java": java,
                                                        "environment": environment or {}, "newer": newer or []})
    return inventory, layout, layout["nodes"][0]


def role_files(inventory, layout, files):
    variables = cassandra_inventory_host_vars(inventory, layout["hosts"], "n1")
    rendered, errors, dummy, dummy2 = role_render(variables, FACTS, files)
    assert errors == []
    return rendered, variables


MOVED = [  # (id, series, java, edit, the variables it takes)
    ("options and heap", "50x", "17", before_extra('JVM_OPTS="$JVM_OPTS -XX:+AlwaysPreTouch -Dfoo=1 -Xms8G -Xmx8G -Xss1M"'),
     {"cassandra_heap_size": "8G", "cassandra_jvm17_extra_options": ["-XX:+AlwaysPreTouch", "-Dfoo=1", "-Xss1M"]}),
    ("heap with CMS: its young generation too", "41x", "11", before_extra('JVM_OPTS="$JVM_OPTS -Xms8G -Xmx8G"'),
     {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M"}),
    ("heap set at the top", "41x", "11", on_top('MAX_HEAP_SIZE="6G"\nHEAP_NEWSIZE=600M'),
     {"cassandra_heap_size": "6G", "cassandra_heap_newsize": "600M"}),
    ("heap at the top, Java 17, -Xmx in the options of Java 11", "50x", "17",
     both(on_top("MAX_HEAP_SIZE=12G"), add_to("jvm11-server.options", "-Xmx4G\n-Xms4G")),
     {"cassandra_heap_size": "12G", "cassandra_jvm11_extra_options": ["-Xmx4G", "-Xms4G"]}),
    ("an option set twice: the last one", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -Xss1024M"\nJVM_OPTS="$JVM_OPTS -XX:+PrintFlagsFinal"\nJVM_OPTS="$JVM_OPTS -Xss512k"'),
     {"cassandra_jvm17_extra_options": ["-XX:+PrintFlagsFinal", "-Xss512k"]}),
    ("direct memory and a log of its own", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:MaxDirectMemorySize=2G -Xlog:safepoint:file=/tmp/sp.log"'),
     {"cassandra_max_direct_memory_size": "2G", "cassandra_jvm17_extra_options": ["-Xlog:safepoint:file=/tmp/sp.log"]}),
    ("5.0 on Java 11", "50x", "11", before_extra('export JVM_OPTS="${JVM_OPTS} -XX:+UseNUMA -Dbar=2"  # mine'),
     {"cassandra_jvm11_extra_options": ["-XX:+UseNUMA", "-Dbar=2"]}),
    ("Java 8, heap and options apart", "40x", "8",
     before_extra('JVM_OPTS="$JVM_OPTS -Xmx8G -Xms8G"\nJVM_OPTS="$JVM_OPTS -XX:+UseLargePages"'),
     {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "800M",
      "cassandra_jvm8_extra_options": ["-XX:+UseLargePages"]}),
    ("-Xmx in the options read, -Xmx in cassandra-env.sh: the one the JVM uses", "41x", "11",
     both(add_to("jvm11-server.options", "-Xmx4G\n-Xms4G"), before_extra('JVM_OPTS="$JVM_OPTS -Xms8G -Xmx8G"')),
     {"cassandra_jvm11_extra_options": ["-Xms8G", "-Xmx8G"]}),
    ("an option before the greps", "50x", "17",
     on_top('JVM_OPTS="$JVM_OPTS -Xms10G -Xmx10G -XX:+UseLargePages"'),
     {"cassandra_heap_size": "10G", "cassandra_jvm17_extra_options": ["-XX:+UseLargePages"]}),
]


@pytest.mark.parametrize("case, series, java, edit, wanted", MOVED, ids=[c[0] for c in MOVED])
def test_hand_edits_moved_give_the_jvm_the_same_arguments(tmp_path, case, series, java, edit, wanted):
    files = node_files(series, edit)
    cmdline = launch(files, java, tmp_path)
    inventory, layout, node = imported(files, series, java, cmdline)
    assert node["moved"] and node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    assert dict((k, variables.get(k)) for k in wanted) == wanted
    # bash, on the files the role writes: the JVM gets the same, worked out here and by the code under test
    after = launch(rendered, java, tmp_path)
    assert settings(after) == settings(cmdline)
    assert jvm.differences(cmdline.split("\0")[1:], after.split("\0")[1:]) == []
    out = check(files, series, java=java, cmdline=cmdline)
    assert out["differences"] == []
    assert [n for n in out["notes"] if "the JVM gets the same arguments" in n]
    assert "cassandra-env.sh" in node["rewritten"]


NOT_MOVED = [  # bash would give the JVM other arguments from the role's files, or that cannot be shown
    ("G1 threads: cassandra-env.sh adds ConcGCThreads only without it", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:ParallelGCThreads=4"')),
    ("a GC log: cassandra-env.sh adds its own only without one", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -Xlog:gc:file=/var/log/cassandra/mine.log"')),
    ("-Xmn alone: in the options, cassandra-env.sh would stop", "40x", "8", before_extra('JVM_OPTS="$JVM_OPTS -Xmn1G"')),
    ("in a block", "50x", "17", before_extra('if [ -n "$X" ]; then\n  JVM_OPTS="$JVM_OPTS -Dfoo=1"\nfi')),
    ("with a variable", "50x", "17", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=$CASSANDRA_HOME/x"')),
    # what the audits found
    ("right after a grep: the $? read next is the line's", "50x", "17",
     after_line('echo "$JVM_OPTS" | grep -qe "-[X]log:gc"', 'JVM_OPTS="$JVM_OPTS -Dfoo=1"')),
    ("right after the CMS grep", "50x", "17",
     after_line("echo $JVM_OPTS | grep -q UseConcMarkSweepGC", 'JVM_OPTS="$JVM_OPTS -Dfoo=1"')),
    ("-Xmn is not -XX:NewSize: it sets MaxNewSize too", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -Xmn2G -XX:NewSize=1G"')),
    ("a file of options read where it is", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:VMOptionsFile=/etc/cassandra/heap.opts"')),
    ("an agent: agents load in order", "50x", "17", before_extra('JVM_OPTS="$JVM_OPTS -agentpath:/opt/p/lib.so"')),
    ("-XX:MaxNewSize before cassandra-env.sh's -Xmn, which sets it too", "41x", "11",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:MaxNewSize=2G"')),
    ("-Xloggc turns these on where it is, Java 8", "40x", "8",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:-PrintGCTimeStamps -XX:-PrintGC"')),
    ("-XX:MaxNewSize, Java 8", "40x", "8", before_extra('JVM_OPTS="$JVM_OPTS -XX:MaxNewSize=1G -Dz=1"')),
    ("-XX:MinHeapSize before cassandra-env.sh's -Xms, which sets it too", "50x", "17",
     before_extra('JVM_OPTS="$JVM_OPTS -XX:MinHeapSize=1G"')),
    ("a line in a block of cassandra-env.sh's own", "50x", "17",
     after_line('if [ "$LOCAL_JMX" = "yes" ]; then', '  JVM_OPTS="$JVM_OPTS -Dfoo=1"')),
    ("a \\r in an options file", "50x", "17",
     both(add_to("jvm-server.options", "-Dx=1\r"), before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))),
    ("the heap from what only the shell knows", "41x", "11", on_top('MAX_HEAP_SIZE="${HEAP:-6G}"\nHEAP_NEWSIZE=600M')),
    ("the heap set between two of its uses", "50x", "17",
     after_line("    export MALLOC_ARENA_MAX=4\nfi", "MAX_HEAP_SIZE=2G")),
    ("another hand edit in cassandra-env.sh", "50x", "17",
     both(before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'), before_extra("ulimit -n 100000"))),
]


@pytest.mark.parametrize("case, series, java, edit", NOT_MOVED, ids=[c[0] for c in NOT_MOVED])
def test_hand_edits_not_moved_stay_hand_edits(tmp_path, case, series, java, edit):
    files = node_files(series, edit)
    cmdline = launch(files, java, tmp_path)
    inventory, layout, node = imported(files, series, java, cmdline)
    assert node["moved"] == [] and node["rewritten"] == []
    assert [line for line in node["hand_edits"] if line.startswith("cassandra-env.sh")]
    assert check(files, series, java=java, cmdline=cmdline)["differences"] != []
    if not re.search(r"in a block|another hand edit|with a variable", case):  # (no line of plain options alone)
        assert node["not_moved"]
    if "only the shell knows" in case:
        assert node["not_moved"] == "cassandra-env.sh sets MAX_HEAP_SIZE from what only the shell knows"


def test_not_moved_when_the_files_changed_since_the_jvm_started(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    cmdline = launch(files, "17", tmp_path)
    assert imported(files, "50x", "17", cmdline, newer=["cassandra-env.sh"])[2]["moved"] == []
    # the arguments of another start (here: of the stock files): not the files' either
    other = launch(node_files("50x"), "17", tmp_path)
    assert imported(files, "50x", "17", other)[2]["moved"] == []
    assert imported(files, "50x", "17", "")[2]["moved"] == []  # Cassandra not running


def test_cleanup_says_what_the_jvm_does_not_use(tmp_path):
    files = node_files("50x", both(add_to("jvm11-server.options", "-Xmx4G"),
                                   add_to("jvm17-server.options", "-Xss1024M\n-XX:-UseBiasedLocking"),
                                   add_to("jvm-server.options", "-XX:+UseConcMarkSweepGC")))
    files["jvm17-server.options"] = files["jvm17-server.options"].replace("-Xss1024M", "-Xss1024M\n-Xss768k")
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert node["moved"] == []
    cleanup = [re.sub(r"^(\S+) line \d+", r"\1", line) for line in node["cleanup"]]
    assert cleanup == [
        "jvm11-server.options: -Xmx4G: not read, Java 17 reads jvm17-server.options",
        "jvm-server.options: -XX:+UseConcMarkSweepGC: ignored since Java 14 (CMS removed)",
        "jvm17-server.options: -XX:-UseBiasedLocking: Java 17's default since Java 15 (deprecated, ignored from Java"
        " 18)",
        "jvm17-server.options: -Xss1024M: overridden by -Xss768k (jvm17-server.options line %d)"
        % (files["jvm17-server.options"].split("\n").index("-Xss768k") + 1)]
    # nothing moved: the files stay as they are, the self-check passes
    assert check(files, "50x", cmdline=cmdline)["differences"] == []


def test_cleanup_values_masked(tmp_path):
    files = node_files("50x", add_to("jvm11-server.options", "-Djavax.net.ssl.keyStorePassword=S3cr3t"))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert node["cleanup"] and "S3cr3t" not in "\n".join(node["cleanup"])


def test_moved_and_dropped_lines_said(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Xss1024M"\nJVM_OPTS="$JVM_OPTS -Xms8G -Xmx8G -Xss512k"'))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    at = files["cassandra-env.sh"].split("\n").index('JVM_OPTS="$JVM_OPTS -Xss1024M"') + 1
    assert node["moved"] == [
        "cassandra-env.sh line %d: -Xss1024M -> left out" % at,
        "cassandra-env.sh line %d: -Xms8G -Xmx8G -Xss512k -> cassandra_heap_size: 8G; cassandra_jvm17_extra_options"
        % (at + 1)]
    assert node["cleanup"] == ["cassandra-env.sh line %d: -Xss1024M: overridden by -Xss512k later, left out" % at]
    assert node["rewritten"] == ["cassandra-env.sh", "jvm17-server.options"]


def test_dead_heap_assignment_left_out(tmp_path):
    files = node_files("50x", before_extra("MAX_HEAP_SIZE=2G"))  # after cassandra-env.sh used it: no effect
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert node["moved"] == [] and [c for c in node["cleanup"] if "MAX_HEAP_SIZE=2G: set after the last use" in c]
    assert check(files, "50x", cmdline=cmdline)["differences"] == []


def test_report_moved_cleanup_and_next(tmp_path):
    files = node_files("50x", both(before_extra('JVM_OPTS="$JVM_OPTS -Xms8G -Xmx8G -XX:+AlwaysPreTouch"'),
                                   add_to("jvm11-server.options", "-Xmx4G")))
    inventory, layout, dummy = imported(files, "50x", "17", launch(files, "17", tmp_path))
    nodes = [dict(layout["nodes"][0], name="n%d" % i, address="10.0.0.%d" % i) for i in (1, 2)]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["/p/c.yml", "/p"], "/p/r.txt", {}, True,
                                    cwd="/p", in_git=True)
    at = lines.index(u"MOVED — cassandra-env.sh hand edits now set by variables, the JVM gets the same arguments")
    assert re.match(r"^  cassandra-env.sh line \d+: -Xms8G -Xmx8G -XX:\+AlwaysPreTouch:$", lines[at + 1])
    assert re.match(r"^ +cassandra_heap_size: 8G; cassandra_jvm17_extra_options +all$", lines[at + 2])
    assert lines[at + 3] == ("  apply_config rewrites cassandra-env.sh, jvm17-server.options on all nodes in the"
                             " collection's layout (same JVM arguments; it restarts the nodes it writes)")
    at = lines.index(u"CLEANUP — options the running JVM does not use: left out of the lines moved, else left as"
                     u" they are")
    assert re.match(r"^  jvm11-server.options line \d+: -Xmx4G: +not read, Java 17 reads jvm17-server.options +all$",
                    lines[at + 1])
    assert u"  1. cassandra-env.sh hand edits moved to variables (see MOVED): apply_config rewrites cassandra-env.sh," \
        u" jvm17-server.options and restarts n1, n2 (same JVM arguments)" in lines
    assert [line for line in lines if "apply_config --check --diff" in line][0].endswith(
        "expect: cassandra-env.sh, jvm17-server.options rewritten (same JVM arguments, see MOVED), nothing else")


# --- what the JVM gets from its arguments ----------------------------------------

@pytest.mark.parametrize("before, after, names", [
    (["-Xmx4G", "-Xmx8G"], ["-Xmx8G"], []),  # the last one counts
    (["-Xmx8G"], ["-XX:MaxHeapSize=8G"], ["-Xmx"]),  # one setting, its text differs
    (["-XX:MaxHeapSize=4G", "-Xmx8G"], ["-Xmx8G"], []),
    (["-XX:OnError=a", "-XX:OnError=b"], ["-XX:OnError=b", "-XX:OnError=a"], ["OnError"]),  # they add up, in order
    (["-XX:+UnlockExperimentalVMOptions", "-XX:G1NewSizePercent=50"],
     ["-XX:G1NewSizePercent=50", "-XX:+UnlockExperimentalVMOptions"], ["G1NewSizePercent"]),  # before its unlock
    (["-Xlog:gc:file=a", "-Xlog:safepoint:file=b"], ["-Xlog:safepoint:file=b", "-Xlog:gc:file=a"], []),  # outputs apart
    (["-Xlog:gc:file=a", "-Xlog:gc*:file=a"], ["-Xlog:gc*:file=a", "-Xlog:gc:file=a"], ["-Xlog:file=a"]),
    (["-Xlog:disable", "-Xlog:gc:file=a"], ["-Xlog:gc:file=a", "-Xlog:disable"], ["-Xlog"]),
    (["--add-opens", "a=b", "-ea"], ["-ea", "--add-opens", "a=b"], []),
    (["-ea", "-da:x"], ["-da:x", "-ea"], ["(assertions)"]),
])
def test_differences(before, after, names):
    assert jvm.differences(before, after) == names


@pytest.mark.parametrize("java, series, read", [("1.8", "40x", "jvm8-server.options"), ("11", "41x", "jvm11-server.options"),
                                                ("17", "41x", "jvm11-server.options"), ("17", "50x", "jvm17-server.options"),
                                                ("21", "50x", "jvm17-server.options"), ("11", "50x", "jvm11-server.options"),
                                                ("", "50x", None)])
def test_options_file_the_running_java_reads(java, series, read):
    assert jvm.options_file(series, java) == read


def test_stock_options_as_cassandra_env_adds_them():
    g1 = {"+UseG1GC": True, "UseConcMarkSweepGC": False, "Xmx": False, "Xms": False, "Xmn": False,
          "MaxDirectMemorySize": False, "ParallelGCThreads": False, "ConcGCThreads": False}
    assert jvm.stock_options(g1, {"MAX_HEAP_SIZE": "8G"}) == {
        "-Xms": "8G", "-Xmx": "8G", "MaxDirectMemorySize": jvm.CALC, "ParallelGCThreads": jvm.CORES,
        "ConcGCThreads": jvm.CORES}
    cms = dict(g1, **{"+UseG1GC": False, "UseConcMarkSweepGC": True})
    assert jvm.stock_options(cms, {"MAX_HEAP_SIZE": "8G"}) is None  # without HEAP_NEWSIZE: it stops
    assert jvm.stock_options(cms, {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "1G"})["-Xmn"] == "1G"
    assert jvm.stock_options(dict(g1, Xmx=True), {}) is None  # -Xmx without -Xms
    assert jvm.stock_options(dict(g1, Xmx=True, Xms=True, Xmn=True), {}) is None  # -Xmn with G1


@pytest.mark.parametrize("series, greps", [
    ("40x", ["-Xlog:gc", "-Xloggc", "Xmn", "Xmx", "Xms", "UseConcMarkSweepGC", "+UseG1GC"]),
    ("41x", ["-Xlog:gc", "-Xloggc", "Xmn", "Xmx", "Xms", "UseConcMarkSweepGC", "+UseG1GC"]),
    ("50x", ["-Xlog:gc", "Xmn", "Xmx", "Xms", "MaxDirectMemorySize", "ParallelGCThreads", "ConcGCThreads",
             "UseConcMarkSweepGC", "+UseG1GC"]),
])
def test_what_cassandra_env_looks_for(series, greps):
    """Every grep of the stock cassandra-env.sh is read: one missed would go unseen when an option moves."""
    assert [p for dummy, p in jvm._greps(jvm.env_lines(stock(series)["cassandra-env.sh"]))] == greps


def test_not_the_same_when_the_unit_gives_other_jvm_options(tmp_path):
    files = node_files("50x")
    cmdline = launch(files, "17", tmp_path)
    assert jvm.cassandra_jvm_args_same(cmdline, files, files, "50x", "17", {}, {})[0]
    assert not jvm.cassandra_jvm_args_same(cmdline, files, files, "50x", "17", {}, {"JVM_EXTRA_OPTS": "-Da=1"})[0]


def test_options_the_files_already_have_moved_without_hand_edits(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -XX:MaxGCPauseMillis=300 -Dfoo=1"'))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert node["moved"] and node["hand_edits"] == []


def test_heap_assignment_set_again_said_left_out(tmp_path):
    files = node_files("50x", both(on_top("MAX_HEAP_SIZE=6G"), before_extra('JVM_OPTS="$JVM_OPTS -Xmx7G -Xms7G"')))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert node["moved"][0] == "cassandra-env.sh line 1: MAX_HEAP_SIZE=6G -> left out, set again later"
    assert node["moved"][1].endswith("-> cassandra_heap_size: 7G")


def test_young_generation_said_in_the_report(tmp_path):
    files = node_files("41x", before_extra('JVM_OPTS="$JVM_OPTS -Xms8G -Xmx8G"'))
    inventory, layout, node = imported(files, "41x", "11", launch(files, "11", tmp_path))
    lines = cassandra_import_report(layout, ["/p/c.yml", "/p"], "/p/r.txt", {}, True, cwd="/p", in_git=True)
    assert [line for line in lines if "the young generation the JVM runs with" in line]
    assert [line for line in lines if "cassandra_heap_newsize: 800M" in line]


def test_self_check_not_shown_the_same_when_the_files_changed_since_the_jvm_started(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, dummy = imported(files, "50x", "17", cmdline)
    jvm_now = {"cmdline": cmdline, "newer": []}
    assert cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "17",
                                       "", {}, jvm=jvm_now)["differences"] == []
    out = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "17",
                                      "", {}, jvm=dict(jvm_now, newer=["cassandra-env.sh"]))
    assert [d for d in out["differences"] if d.startswith("cassandra-env.sh")]


@pytest.mark.parametrize("series, java, line, extra", [
    ("50x", "17", "-Dcom.sun.management.jmxremote.authenticate=false", "-Dcom.sun.management.jmxremote.authenticate=true"),
    ("50x", "17", "-Xms8000M", "-XX:InitialHeapSize=2G"),  # cassandra-env.sh's own -Xms, worked out on that node
])
def test_a_line_repeating_what_cassandra_env_adds_is_not_moved(tmp_path, series, java, line, extra):
    """Its options twice in the arguments (cassandra-env.sh's own, the line's, JVM_EXTRA_OPTS between): which one is
    the line's is not known."""
    files = node_files(series, add_to("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS %s"' % line))
    environment = {"JVM_EXTRA_OPTS": extra}
    cmdline = launch(files, java, tmp_path, environment)
    node = imported(files, series, java, cmdline, environment=environment)[2]
    assert node["moved"] == []
    assert not jvm.cassandra_jvm_args_same(cmdline, files, node_files(series), series, java, environment,
                                           environment)[0]


@pytest.mark.parametrize("line, extra", [
    ("-verbose:gc", "-Xlog:gc=off"), ("-Xnoclassgc", "-XX:+ClassUnloading"), ("-Xint", "-Xmixed"),
    ("-Xrs", "-XX:-ReduceSignalUsage"),
])
def test_not_moved_before_what_the_unit_adds(tmp_path, line, extra):
    """Moved to the options files, the line would go before JVM_EXTRA_OPTS: another spelling of the same setting
    there would win."""
    files = node_files("50x", add_to("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS %s"' % line))
    environment = {"JVM_EXTRA_OPTS": extra}
    node = imported(files, "50x", "17", launch(files, "17", tmp_path, environment), environment=environment)[2]
    assert node["moved"] == []


def test_options_files_found_once_only():
    with pytest.raises(jvm.NotSame):
        jvm._find(["java", "-a", "-b", "-a", "-b"], ["-a", "-b"])
    assert jvm._find(["java", "-a", "-b", "-c"], ["-a", "-b"]) == 1


def test_the_role_s_cassandra_env_differing_otherwise_is_not_the_same(tmp_path):
    files = node_files("50x")
    cmdline = launch(files, "17", tmp_path)
    role = dict(files, **{"cassandra-env.sh": files["cassandra-env.sh"] + "\nulimit -n 100000\n"})
    assert jvm.cassandra_jvm_args_same(cmdline, files, role, "50x", "17") == (
        False, "cassandra-env.sh differs from the role's otherwise than by JVM_OPTS lines and the heap")


def test_a_heap_set_on_the_node_is_not_the_same_as_one_worked_out(tmp_path):
    files = node_files("50x", on_top("MAX_HEAP_SIZE=6G"))
    cmdline = launch(files, "17", tmp_path)
    assert jvm.cassandra_jvm_args_same(cmdline, files, node_files("50x"), "50x", "17") == (
        False, "the heap would be worked out on the node instead of set")


def test_an_empty_jvm_opts_line_is_left_out(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS "\nJVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert len(node["moved"]) == 1 and node["moved"][0].endswith("-Dfoo=1 -> cassandra_jvm17_extra_options")
    assert node["hand_edits"] == []


def test_why_not_moved_said_in_the_report(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -XX:ParallelGCThreads=4"'))
    inventory, layout, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert node["not_moved"] == "the JVM would get other values of ConcGCThreads"
    lines = cassandra_import_report(layout, ["/p/c.yml", "/p"], "/p/r.txt", {}, False, cwd="/p", in_git=True)
    assert ("  cassandra-env.sh JVM_OPTS lines, heap: not moved to variables, the JVM would get other values of"
            " ConcGCThreads (all)") in lines


def test_why_not_moved_holds_names_only(tmp_path):
    # another log to cassandra-env.sh's own GC log file (its order with it counts)
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Xlog:safepoint=debug:file=%s/logs/gc.log -Dfoo=1"'
                                           % tmp_path))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path, paths=True))[2]
    assert node["moved"] == [] and "gc.log" not in node["not_moved"] and "-Xlog" in node["not_moved"]
