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
from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_no_effect
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    _render as role_render,
    cassandra_import_self_check,
    cassandra_inventory_host_vars,
)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4, cassandra_config_import, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import cassandra_same_settings

from .test_cassandra_import_check import FACTS, check, edit, import_node, seeded, stock
from .test_cassandra_import_logback import audit_on, file_auditing

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


def imported(files, series, java, cmdline, newer=None, environment=None, keep=False):
    inventory, layout = import_node(files, series, jvm={"cmdline": cmdline, "java": java, "keep": keep,
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


def test_heap_moved_with_a_heap_in_the_options_of_another_java(tmp_path):
    # Java 17 reads no jvm11-server.options: its -Xmx/-Xms are imported as they are (the role writes the file the
    # same, no file kept); the heap at the top of cassandra-env.sh is moved
    files = node_files("50x", both(on_top("MAX_HEAP_SIZE=12G"), add_to("jvm11-server.options", "-Xmx4G\n-Xms4G")))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["moved"] and node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    assert variables.get("cassandra_heap_size") == "12G"
    assert variables.get("cassandra_jvm11_extra_options") == ["-Xmx4G", "-Xms4G"]
    assert "cassandra_config_keep_files" not in variables
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


G1_TUNING = ("-XX:MaxTenuringThreshold=2", "-XX:G1HeapRegionSize=16m", "-XX:+UnlockExperimentalVMOptions",
             "-XX:G1NewSizePercent=50", "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:InitiatingHeapOccupancyPercent=70")


TUNED = dict(("cassandra_jvm17_%s" % k, "") for k in (
    "max_tenuring_threshold", "g1_heap_region_size", "g1_new_size_percent", "g1_rset_updating_pause_time_percent",
    "initiating_heap_occupancy_percent"))


@pytest.mark.parametrize("removed", [G1_TUNING, ("-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled") + G1_TUNING],
                         ids=["G1 without its tuning", "the JVM's default GC"])
def test_gc_lines_removed_are_carried(tmp_path, removed):
    # 5.0's G1 block without some of its lines (an older release's file), the heap the node wrote in their place:
    # G1 with those lines' variables "" (commented out); without G1 at all: cassandra_jvm17_gc custom, the node's GC
    # lines in cassandra_jvm17_extra_options, in their order
    def edit(files):
        text = files["jvm17-server.options"].replace("-XX:MaxTenuringThreshold=2", "-Xms31G\n-Xmx31G")
        files["jvm17-server.options"] = "\n".join(line for line in text.split("\n") if line not in removed)
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    # (the heap: cassandra_heap_size in jvm-server.options)
    assert variables["cassandra_heap_size"] == "31G" and variables["cassandra_heap_in_jvm_options"] is True
    if "-XX:+UseG1GC" in removed:
        assert variables["cassandra_jvm17_gc"] == "custom"
        assert variables["cassandra_jvm17_extra_options"] == ["-XX:MaxGCPauseMillis=300"]
    else:
        assert "cassandra_jvm17_gc" not in variables and "cassandra_jvm17_extra_options" not in variables
        assert dict((k, variables.get(k)) for k in TUNED) == TUNED
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


@pytest.mark.parametrize("removed", [G1_TUNING, ("-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled") + G1_TUNING],
                         ids=["G1 without its tuning", "the JVM's default GC"])
def test_a_gc_reading_kept_beside_another_hand_edit(tmp_path, removed):
    # (re-audit, 2026-11-14) a stock line of the file switched off too (otherwise than the role does: ##): the GC
    # read all the same, that line the only difference (custom was dropped then: 8 differences)
    def edit(files):
        text = files["jvm17-server.options"].replace("-Djdk.attach.allowAttachSelf=true\n",
                                                     "##-Djdk.attach.allowAttachSelf=true\n")
        files["jvm17-server.options"] = "\n".join(line for line in text.split("\n") if line not in removed)
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert [line for line in node["hand_edits"] if line.startswith("  ")] == [
        "  - -Djdk.attach.allowAttachSelf=true", "  + ##-Djdk.attach.allowAttachSelf=true"]
    assert [d.split(":")[1].strip() for d in check(files, "50x", java="17", cmdline=cmdline)["differences"]] == [
        "-Djdk.attach.allowAttachSelf"]


def test_the_heap_in_an_options_file_is_cassandra_heap_size(tmp_path):
    # an -Xms/-Xmx pair in jvm17-server.options: cassandra_heap_size, written in jvm-server.options
    # (cassandra_heap_in_jvm_options), shown the same from the running JVM's arguments
    files = node_files("50x", add_to("jvm17-server.options", "-Xms16G\n-Xmx16G"))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_heap_size"] == "16G" and variables["cassandra_heap_in_jvm_options"] is True
    assert "cassandra_jvm17_extra_options" not in variables
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    # Cassandra not running: as read (nothing shows the same arguments), sizes not alike either
    assert "cassandra_heap_size" not in imported(files, "50x", "17", "")[2]["vars"]
    files = node_files("50x", add_to("jvm17-server.options", "-Xms8G\n-Xmx16G"))
    assert "cassandra_heap_size" not in imported(files, "50x", "17", launch(files, "17", tmp_path))[2]["vars"]


def heap_lines_on(xmn=""):
    def edit(files):
        text = files["jvm-server.options"].replace("#-Xms4G\n#-Xmx4G", "-Xms8G\n-Xmx8G")
        files["jvm-server.options"] = text.replace("#-Xmn800M", xmn) if xmn else text
    return edit


@pytest.mark.parametrize("xmn", ["", "-Xmn2G"], ids=["without -Xmn", "with -Xmn"])
def test_a_cms_heap_in_the_jvm_options_the_role_takes(tmp_path, xmn):
    # (audit r4) 4.1 on CMS, its stock heap lines uncommented: the heap in the JVM options, which the role's heap
    # assert takes without cassandra_heap_newsize (cassandra-env.sh works -Xmn out); -Xmn there: the young generation
    from ansible_collections.community.cassandra.tests.unit.roles.test_heap_newsize_g1 import _assert, _task
    files = node_files("41x", heap_lines_on(xmn))
    cmdline = launch(files, "11", tmp_path)
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    assert node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_heap_in_jvm_options"] is True and variables["cassandra_heap_size"] == "8G"
    assert variables.get("cassandra_heap_newsize") == (xmn[4:] or None)
    assert rendered["jvm-server.options"].split("\n")[1:] == files["jvm-server.options"].split("\n")  # (as the node has it)
    assert check(files, "41x", java="11", cmdline=cmdline)["differences"] == []
    simple = dict((k, v) for k, v in variables.items() if not str(v).startswith("{{"))
    for name in ("Assert heap settings are consistent",
                 "Assert no young generation size with G1 when the heap is in the JVM options"):
        assert _assert(_task("roles/cassandra_config/tasks/checks.yml", name), **simple) is True


@pytest.mark.parametrize("edit", [
    both(before_extra('JVM_OPTS="$JVM_OPTS -Xmx$MYHEAP"'), on_top("MYHEAP=16G")),
    on_top(": ${MAX_HEAP_SIZE:=16G}")], ids=["-Xmx through a variable", "MAX_HEAP_SIZE given a default"])
def test_a_heap_of_cassandra_env_however_written_keeps_the_heap_as_it_is(tmp_path, edit):
    # (audit r6) -Xmx through a variable in cassandra-env.sh, (audit r7) MAX_HEAP_SIZE given a default (:=): the pair
    # left in the extra options
    files = node_files("50x", both(edit, add_to("jvm-server.options", "-Xms16G\n-Xmx16G")))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert "cassandra_heap_in_jvm_options" not in node["vars"]
    assert node["vars"]["cassandra_jvm_extra_options"] == ["-Xms16G", "-Xmx16G"]


@pytest.mark.parametrize("series, java, edit, said", [
    ("50x", "17", both(add_to("jvm-server.options", "-Xms16G"), add_to("jvm17-server.options", "-Xmx16G")),
     ["jvm17-server.options: -Xmx "]),
    ("41x", "11", add_to("jvm11-server.options", "-Xms8G\n-Xmx8G\n-Xmn2G"), ["jvm11-server.options: -Xms/-Xmx/-Xmn "])],
    ids=["split pair", "with -Xmn"])
def test_the_heap_moved_names_what_each_file_had(tmp_path, series, java, edit, said):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import HEAP_MOVED
    files = node_files(series, edit)
    node = imported(files, series, java, launch(files, java, tmp_path))[2]
    assert [n for n in node["normalized"] if n.endswith(HEAP_MOVED)] == [s + HEAP_MOVED for s in said]


def test_a_heap_size_in_cassandra_env_keeps_the_heap_as_it_is(tmp_path):
    # (audit r5) MAX_HEAP_SIZE set at the top of cassandra-env.sh, the heap in jvm-server.options: not moved
    files = node_files("50x", both(on_top("MAX_HEAP_SIZE=16G"), add_to("jvm-server.options", "-Xms16G\n-Xmx16G")))
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert "cassandra_heap_in_jvm_options" not in node["vars"]
    assert node["vars"]["cassandra_jvm_extra_options"] == ["-Xms16G", "-Xmx16G"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def test_g1_with_a_young_generation_in_cassandra_env_keeps_the_heap_as_it_is(tmp_path):
    # (audit r4) 5.0 on G1, HEAP_NEWSIZE in cassandra-env.sh (G1 ignores it), the heap in jvm-server.options: not
    # moved to cassandra_heap_in_jvm_options (it would write -Xmn there, with which cassandra-env.sh stops)
    files = node_files("50x", both(on_top('HEAP_NEWSIZE="10G"'), add_to("jvm-server.options", "-Xms16G\n-Xmx16G")))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == [] and "cassandra_heap_in_jvm_options" not in node["vars"]
    assert node["vars"]["cassandra_jvm_extra_options"] == ["-Xms16G", "-Xmx16G"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def test_the_report_says_the_heap_moves_to_jvm_server_options(tmp_path):
    # (audit r4) the heap pair at the end of jvm17-server.options: the role writes it in jvm-server.options
    files = node_files("50x", add_to("jvm17-server.options", "-Xms16G\n-Xmx16G"))
    nodes = [dict(imported(files, "50x", "17", launch(files, "17", tmp_path))[2], name="n1", address="10.0.0.1")]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "jvm-server.options, jvm17-server.options rewritten, no restart (the same JVM"
            " arguments; the heap moves to jvm-server.options), nothing else" in line]
    assert not [line for line in lines if "see HAND EDITS" in line]
    # (audit r5) the heap already in jvm-server.options, a JVM_OPTS line moved: it does not move
    files = node_files("50x", both(add_to("jvm-server.options", "-Xms16G\n-Xmx16G"),
                                   before_extra('JVM_OPTS="$JVM_OPTS -XX:+AlwaysPreTouch"')))
    nodes = [dict(imported(files, "50x", "17", launch(files, "17", tmp_path))[2], name="n1", address="10.0.0.1")]
    assert nodes[0]["vars"]["cassandra_heap_in_jvm_options"] is True
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "rewritten, no restart (the same JVM arguments), nothing else" in line]
    assert not [line for line in lines if "the heap moves" in line]


@pytest.mark.parametrize("series, java, where, heap, keep", [
    (s, j, f, h, k) for s, j, f, h in (("50x", "17", "jvm17-server.options", "-Xms16G\n-Xmx16G"),
                                       ("41x", "11", "jvm11-server.options", "-Xms8G\n-Xmx8G\n-Xmn2G"))
    for k in (f, "cassandra-env.sh", "jvm-server.options")])
def test_the_heap_of_an_options_file_stays_with_a_jvm_file_kept(tmp_path, series, java, where, heap, keep):
    # (audit r5) a JVM file kept as it is: the heap left in the extra options where the node has it (moved to
    # jvm-server.options, it was set twice, the self-check passing while apply_config changed the file)
    files = node_files(series, add_to(where, heap))
    cmdline = launch(files, java, tmp_path)
    jvm = {"cmdline": cmdline, "java": java, "keep": keep, "environment": {}, "newer": []}
    kept = {"cassandra_service_unit_manage": False, "cassandra_config_keep_files": [keep]}
    inventory, layout = import_node(files, series, jvm=jvm, keep=kept)
    node = layout["nodes"][0]
    assert "cassandra_heap_in_jvm_options" not in node["vars"] and "cassandra_heap_size" not in node["vars"]
    assert node["vars"]["cassandra_jvm%s_extra_options" % where[3:5]] == heap.split("\n")
    assert node["rewritten"] == []
    out = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", java,
                                      "", {}, jvm=jvm)
    assert out["differences"] == [] and not [n for n in out["notes"] if "rewrites" in n]


def jvm17(*pairs, drop=()):
    def edit(files):
        text = files["jvm17-server.options"]
        for old, new in pairs:
            assert old in text, old
            text = text.replace(old, new, 1)
        files["jvm17-server.options"] = "\n".join(line for line in text.split("\n") if line not in drop)
    return edit


GC_SHAPES = [  # (id, edit, custom): clean at 9356e934 (the first two) or a hand edit then, never worse
    ("two G1 lines swapped", jvm17(("-XX:MaxTenuringThreshold=2\n-XX:G1HeapRegionSize=16m",
                                    "-XX:G1HeapRegionSize=16m\n-XX:MaxTenuringThreshold=2")), False),
    ("two G1 options on one line", jvm17(("-XX:+UseG1GC\n-XX:+ParallelRefProcEnabled",
                                          "-XX:+UseG1GC -XX:+ParallelRefProcEnabled")), False),
    ("ZGC in the place of G1", jvm17(("-XX:+UseG1GC\n", "-XX:+UseZGC\n")), True),
    ("the parallel GC in the place of G1", jvm17(("-XX:+UseG1GC\n", "-XX:+UseParallelGC\n")), True),
    # GC threads with a tuning line removed: G1, that line's variable ""
    ("G1 with GC threads", jvm17(("#-XX:ParallelGCThreads=16", "-XX:ParallelGCThreads=8"),
                                 drop=("-XX:InitiatingHeapOccupancyPercent=70",)), False),
    # custom with GC threads (no variable switches ParallelRefProcEnabled): they stay after the unlock option
    ("custom with GC threads", jvm17(("#-XX:ParallelGCThreads=16", "-XX:ParallelGCThreads=8"),
                                     drop=("-XX:+ParallelRefProcEnabled",)), True),
]


@pytest.mark.parametrize("case, edit, custom", GC_SHAPES, ids=[c[0] for c in GC_SHAPES])
def test_gc_lines_otherwise_never_worse_than_before_the_custom_gc(tmp_path, case, edit, custom):
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == [] and (node["vars"].get("cassandra_jvm17_gc") == "custom") == custom
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    rendered = role_files(inventory, layout, files)[0]
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)


def test_a_custom_gc_inventory_the_role_takes(tmp_path):
    # MAX_HEAP_SIZE alone (G1), ParallelRefProcEnabled not there: custom, which the role's heap assert takes (G1 on)
    from ansible_collections.community.cassandra.tests.unit.roles.test_heap_newsize_g1 import _assert, _task
    files = node_files("50x", both(on_top("MAX_HEAP_SIZE=31G"), jvm17(drop=("-XX:+ParallelRefProcEnabled",))))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    variables = role_files(inventory, layout, files)[1]
    assert variables["cassandra_jvm17_gc"] == "custom" and variables["cassandra_heap_size"] == "31G"
    assert "cassandra_heap_newsize" not in variables
    heap = _task("roles/cassandra_config/tasks/checks.yml", "Assert heap settings are consistent")
    assert _assert(heap, **dict((k, v) for k, v in variables.items() if not str(v).startswith("{{"))) is True
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


# a file of a release before it: neither the line nor its stock comment
OLDER_CHRONICLE = "# Disable chronicle analytics. See CASSANDRA-19656\n-Dchronicle.analytics.disable=true\n"
CHRONICLE = ("-Dchronicle.analytics.disable=true (jvm-server.options): added by apply_config: Cassandra's default since"
             " CASSANDRA-19656; missing because this file predates it")


@pytest.mark.parametrize("edit, said, differs", [
    # the stock comment there: removed by hand, switched off (cassandra_jvm_removed_options)
    (("-Dchronicle.analytics.disable=true\n", ""), False, False),
    ((OLDER_CHRONICLE, ""), True, False),
    (("-Dchronicle.analytics.disable=true", "#-Dchronicle.analytics.disable=true"), False, False),
    (("-Dchronicle.analytics.disable=true", "-Dchronicle.analytics.disable=false"), False, False)],  # (carried)
    ids=["line removed", "older file", "commented out", "set false"])
def test_a_later_stock_line_only_when_the_file_predates_it(tmp_path, edit, said, differs):
    # (audit r4) only a file with nothing of the line (its option, its stock comment) predates it
    files = node_files("50x", lambda f: f.update({"jvm-server.options": f["jvm-server.options"].replace(*edit)}))
    cmdline = launch(files, "17", tmp_path)
    assert (CHRONICLE in cassandra_config_import(files, "50x", FACTS, java="17", cmdline=cmdline)["jvm_warnings"]) is said
    assert bool(check(files, "50x", java="17", cmdline=cmdline)["differences"]) is differs


def dev_node(chronicle=True):
    """(2026-11-13, a 5.0.9 DEV node) the heap and the stack in G1's block, its tuning commented out with a marker of
    its own, a longer pause; -Xss1024M in jvm-server.options (jvm17-server.options is read after it: its -Xss256k
    wins), biased locking off there."""
    def edit(files):
        text = files["jvm17-server.options"].replace("-XX:+ParallelRefProcEnabled\n",
                                                     "-XX:+ParallelRefProcEnabled\n-Xms31G\n-Xmx31G\n-Xss256k\n")
        for line in G1_TUNING:
            text = text.replace("\n%s\n" % line, "\n#XX %s\n" % line)
        files["jvm17-server.options"] = text.replace("-XX:MaxGCPauseMillis=300", "-XX:MaxGCPauseMillis=500").replace(
            "#-Xlog:gc=info,heap*=trace,age*=debug,safepoint=info,promotion*=trace:file=/var/log/cassandra/gc.log",
            "#-Xlog:gc=info,heap*=trace,age*=debug,safepoint=info,promotion*=trace:file=/data/cassandra/log/gc.log")
        text = files["jvm-server.options"].replace("-Xss256k", "-Xss1024M") + "-XX:-UseBiasedLocking\n"
        files["jvm-server.options"] = text if chronicle else text.replace(OLDER_CHRONICLE, "")
    return edit


def test_the_dev_node_jvm_files(tmp_path):
    files = node_files("50x", dev_node())
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    assert dict((k, v) for k, v in variables.items() if k.startswith(("cassandra_jvm", "cassandra_heap"))) == {
        "cassandra_heap_size": "31G", "cassandra_heap_in_jvm_options": True,
        "cassandra_jvm17_extra_options": ["-Xss256k"], "cassandra_jvm17_max_gc_pause_millis": 500,
        "cassandra_jvm_extra_options": ["-Xss1024M", "-XX:-UseBiasedLocking"], **TUNED}
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    # without the chronicle line (an older file): no difference, said under WORTH A LOOK
    files = node_files("50x", dev_node(chronicle=False))
    cmdline = launch(files, "17", tmp_path)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    assert CHRONICLE in cassandra_config_import(files, "50x", FACTS, java="17", cmdline=cmdline)["jvm_warnings"]


def test_the_stock_gc_block_stays_g1(tmp_path):
    files = node_files("50x", lambda f: f.update({"jvm17-server.options": f["jvm17-server.options"].replace(
        "-XX:MaxGCPauseMillis=300", "-XX:MaxGCPauseMillis=500")}))
    inventory, layout, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    variables = role_files(inventory, layout, files)[1]
    assert "cassandra_jvm17_gc" not in node["vars"] and variables["cassandra_jvm17_max_gc_pause_millis"] == 500


RMI = 'JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.1"'


@pytest.mark.parametrize("edit", [after_line('# JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<public name>"', RMI),
                                  add_to("cassandra-env.sh", RMI)], ids=["below the stock comment", "at the end"])
def test_the_rmi_hostname_line_elsewhere_is_its_variable(tmp_path, edit):
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == [] and node["vars"]["cassandra_jmx_rmi_hostname"] == IPV4  # (each node its own)
    rendered = role_files(inventory, layout, files)[0]
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    # inside a block: not read there
    files = node_files("50x", add_to("cassandra-env.sh", 'if [ -n "$CASSANDRA_CONF" ]; then\n%s\nfi' % RMI))
    assert "cassandra_jmx_rmi_hostname" not in imported(files, "50x", "17", launch(files, "17", tmp_path))[2]["vars"]


def test_a_line_at_another_place_after_the_unit_s_extra_options_is_not_the_same(tmp_path):
    # the node's rmi line after JVM_EXTRA_OPTS wins over the unit's; the role's, before it, would not
    env = {"JVM_EXTRA_OPTS": "-Djava.rmi.server.hostname=10.9.9.9"}
    files = node_files("50x", add_to("cassandra-env.sh", RMI))
    cmdline = launch(files, "17", tmp_path, environment=env)
    assert check(files, "50x", java="17", cmdline=cmdline, env=env)["differences"]


def remote_jmx(local_jmx="no"):
    """(2026-11-13, a 5.0.9 RPM node) remote JMX: the JMX files and the rmi hostname in its branch, below the
    authentication line, the stock password file line commented out (its users readable)."""
    auth = '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=true"'
    password = 'JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file=/etc/cassandra/jmxremote.password"'
    access = 'JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.access.file=/etc/cassandra/jmxremote.access"'

    def edit(files):
        text = files["cassandra-env.sh"].replace("    LOCAL_JMX=yes", "    LOCAL_JMX=" + local_jmx)
        text = text.replace(auth, "\n  ".join((auth, password, access, RMI)))
        files["cassandra-env.sh"] = text.replace("\n%s\n" % password, "\n# %s\n" % password)
        files["jmxremote.password"] = "monitor m0nitor\n"
        files["jmxremote.access"] = "monitor readonly\n"
    return edit


def test_remote_jmx_lines_in_its_branch_are_carried(tmp_path):
    files = node_files("50x", remote_jmx())
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == [] and node["moved"] == []
    assert [n for n in node["normalized"] if n.startswith("cassandra-env.sh: lines at another place")]  # (no effect)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_jmx_rmi_hostname"] == IPV4 and variables["cassandra_local_jmx"] is False
    assert [u["name"] for u in variables["cassandra_jmx_users"]] == ["monitor"]
    assert settings(launch(rendered, "17", tmp_path)) == settings(cmdline)
    # (the self-check said "not on the node: fi": the block's end paired with another line)
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    # local JMX: that branch does not run, its rmi line is not read
    files = node_files("50x", remote_jmx("yes"))
    inventory, layout, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert "cassandra_jmx_rmi_hostname" not in node["vars"] and node["hand_edits"]


def test_a_hand_edit_of_an_options_file_does_not_stop_a_move(tmp_path):
    # a stock line of jvm-server.options switched off otherwise than the role does (said as a hand edit, the role
    # puts it back): the JVM_OPTS line of cassandra-env.sh is moved all the same, the self-check says that line only
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "##-XX:+UseNUMA\n")
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["vars"]["cassandra_jvm17_extra_options"] == ["-Dfoo=1"]
    assert [line for line in node["hand_edits"] if line.startswith("  ")] == ["  - -XX:+UseNUMA", "  + ##-XX:+UseNUMA"]
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == ["jvm-server.options: UseNUMA: node has nothing, import would write '-XX:+UseNUMA'"]
    assert [n for n in out["notes"] if n.endswith("but for UseNUMA, above")]
    # the hand edit in the file the move goes to: not moved
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    files["jvm17-server.options"] = files["jvm17-server.options"].replace("-Djdk.attach.allowAttachSelf=true\n",
                                                                          "##-Djdk.attach.allowAttachSelf=true\n")
    assert imported(files, "50x", "17", launch(files, "17", tmp_path))[2]["moved"] == []


AGENT = "-javaagent:/opt/jmx_exporter/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/cassandra/jmx_exporter.yml"


def older_release(files):
    """(2026-11-13, a 5.0.9 cluster on RPM) its jvm17 G1 block without the tuning lines, the heap in their place,
    -Xss1024M for -Xss256k, the rmi hostname at the end of cassandra-env.sh, the exporter's agent in an if ... fi
    block, the chronicle line not there."""
    files["cassandra-env.sh"] += '\n%s\n\nif [ -n "$CASSANDRA_CONF" ]; then\n    JVM_OPTS="$JVM_OPTS %s"\nfi\n' % (
        RMI, AGENT)
    files["jvm-server.options"] = files["jvm-server.options"].replace("-Xss256k", "-Xss1024M").replace(
        OLDER_CHRONICLE, "") + "-XX:-UseBiasedLocking\n"
    text = files["jvm17-server.options"].replace("-XX:MaxTenuringThreshold=2", "-Xms31G\n-Xmx31G")
    files["jvm17-server.options"] = "\n".join(line for line in text.split("\n") if line not in G1_TUNING)


def test_a_node_with_the_files_of_an_older_release(tmp_path):
    # everything carried; the chronicle line, a stock line of a later release, said under WORTH A LOOK
    files = node_files("50x", older_release)
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["hand_edits"] == []
    assert CHRONICLE in cassandra_config_import(files, "50x", FACTS, java="17", cmdline=cmdline)["jvm_warnings"]
    variables = role_files(inventory, layout, files)[1]
    assert variables["cassandra_jmx_rmi_hostname"] == IPV4 and "cassandra_jvm17_gc" not in variables
    assert "cassandra_jvm17_extra_options" not in variables and variables["cassandra_heap_size"] == "31G"
    assert dict((k, variables.get(k)) for k in TUNED) == TUNED
    assert variables["cassandra_jvm_extra_options"] == ["-Xss1024M", "-XX:-UseBiasedLocking"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def test_the_way_out_of_an_options_file_hand_edit_keeps_it(tmp_path):
    # the report's way out for a stock line switched off otherwise than the role does: jvm-server.options kept, with
    # cassandra-env.sh (whose lines were moved: a JVM file kept, nothing is moved), and the self-check passes then
    def edit(files):
        older_release(files)
        files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "##-XX:+UseNUMA\n")
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    nodes = [dict(node, name="n1", address="10.0.0.1")]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "-e import_keep_hand_edits=cassandra-env.sh,jvm-server.options" in line]
    keep = ["cassandra-env.sh", "jvm-server.options"]
    jvm = {"cmdline": cmdline, "java": "17", "keep": ",".join(keep), "environment": {}, "newer": []}
    inventory, layout = import_node(files, "50x", jvm=jvm, keep={"cassandra_service_unit_manage": False,
                                                                 "cassandra_config_keep_files": keep})
    out = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "17",
                                      "", {}, jvm=jvm)
    assert out["differences"] == []


@pytest.mark.parametrize("keep", [["cassandra-env.sh", "jvm-server.options"], ["cassandra-env.sh"]])
def test_a_kept_jvm_file_keeps_the_exporter_s_agent(tmp_path, keep):
    # (re-audit, 2026-11-14) the exporter's block the only move, a hand edit in jvm-server.options: the report's way
    # out keeps both files; the agent is then not imported (cassandra_config refuses to write it in a kept
    # jvm-server.options), cassandra-env.sh gives it as it does
    def edit(files):
        files["cassandra-env.sh"] += '\nif [ -n "$CASSANDRA_CONF" ]; then\n    JVM_OPTS="$JVM_OPTS %s"\nfi\n' % AGENT
        files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "##-XX:+UseNUMA\n")
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    nodes = [dict(imported(files, "50x", "17", cmdline)[2], name="n1", address="10.0.0.1")]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "-e import_keep_hand_edits=cassandra-env.sh,jvm-server.options" in line]
    jvm = {"cmdline": cmdline, "java": "17", "keep": ",".join(keep), "environment": {}, "newer": []}
    inventory, layout = import_node(files, "50x", jvm=jvm, keep={"cassandra_service_unit_manage": False,
                                                                 "cassandra_config_keep_files": keep})
    assert not [k for k in layout["nodes"][0]["vars"] if k.startswith("cassandra_prometheus_exporter")]
    out = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "17",
                                      "", {}, jvm=jvm)
    assert out["differences"] == ([] if "jvm-server.options" in keep else
                                  ["jvm-server.options: UseNUMA: node has nothing, import would write '-XX:+UseNUMA'"])


def owner_dev_node(files):
    """(2026-11-14) a 5.0.9 RPM DEV node, its four files: cassandra-env.sh (its log dir, remote JMX with the JMX files
    and the rmi hostname in its branch), logback.xml (the log dir written as itself, the audit file in a dir of its
    own), jvm-server.options of 4.x (-Xss1024M, biased locking off, no chronicle line), jvm17-server.options (the
    heap and the stack in G1's block, its tuning commented out)."""
    remote_jmx()(files)
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace("CASSANDRA_LOG_DIR=/var/log/cassandra",
                                                                  "CASSANDRA_LOG_DIR=/data/cassandra/log")
    text = audit_on(files["logback.xml"]).replace("${cassandra.logdir}/audit/", "/data/audit/").replace(
        "    <file>/data/audit/audit.log", "          <file>/data/audit/audit.log")
    for name in ("system.log", "debug.log"):
        text = text.replace("${cassandra.logdir}/" + name, "/data/cassandra/log/" + name)
    files["logback.xml"] = text
    jvm17 = files["jvm17-server.options"]
    dev_node()(files)
    files["jvm-server.options"] = stock("41x")["jvm-server.options"].replace("-Xss256k", "-Xss1024M")
    assert files["jvm17-server.options"] != jvm17


def test_the_owner_dev_node_in_full_sync(tmp_path):
    files = node_files("50x", owner_dev_node)
    # the JVM's log dir: the node's (the test's launch gives its own)
    cmdline = launch(files, "17", tmp_path).replace("-Dcassandra.logdir=/run/logs", "-Dcassandra.logdir=/data/cassandra/log")
    out = cassandra_config_import(files, "50x", FACTS, jvm={
        "cmdline": cmdline, "java": "17", "environment": {}, "newer": []})
    assert out["hand_edits"] == [] and out["jvm_notes"] == []
    assert CHRONICLE in out["jvm_warnings"]
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    # bash: the same JVM arguments, but for the chronicle line apply_config adds
    flags, others = settings(launch(rendered, "17", tmp_path))
    assert flags.pop("-Dchronicle.analytics.disable") == "true"
    assert (flags, others) == settings(launch(files, "17", tmp_path))
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []
    prefixes = ("cassandra_jvm", "cassandra_heap", "cassandra_log", "cassandra_jmx", "cassandra_local")
    assert dict((k, v) for k, v in node["vars"].items() if k.startswith(prefixes)) == {
        "cassandra_log_dir": "/data/cassandra/log", "cassandra_local_jmx": False, "cassandra_jmx_rmi_hostname": IPV4,
        "cassandra_jmx_users": [{"name": "monitor", "password": "m0nitor", "access": "readonly"}],
        "cassandra_log_audit_file": True, "cassandra_log_audit_dir": "/data/audit", "cassandra_log_console": True,
        "cassandra_heap_size": "31G", "cassandra_heap_in_jvm_options": True,
        "cassandra_jvm_extra_options": ["-Xss1024M", "-XX:-UseBiasedLocking"],
        "cassandra_jvm17_extra_options": ["-Xss256k"], "cassandra_jvm17_max_gc_pause_millis": 500, **TUNED}


@pytest.mark.parametrize("where, keep, imported_too", [
    ("jvm-server.options", ["cassandra-env.sh"], True), ("jvm17-server.options", ["cassandra-env.sh"], True),
    ("jvm17-server.options", ["jvm17-server.options"], False), ("jvm17-server.options", ["jvm-server.options"], False)])
def test_the_exporter_s_agent_imported_unless_its_file_is_kept(tmp_path, where, keep, imported_too):
    # (audit r4) the agent in an options file, another file kept: imported (it was carried as an extra option, the
    # exporter off, and the role's cleanup removed its jar); its own file kept, or jvm-server.options where the role
    # writes it: not imported, the file gives it as it does
    files = node_files("50x", add_to(where, AGENT))
    cmdline = launch(files, "17", tmp_path)
    jvm = {"cmdline": cmdline, "java": "17", "keep": ",".join(keep), "environment": {}, "newer": []}
    inventory, layout = import_node(files, "50x", jvm=jvm, keep={"cassandra_service_unit_manage": False,
                                                                 "cassandra_config_keep_files": keep})
    variables = layout["nodes"][0]["vars"]
    assert bool(variables.get("cassandra_prometheus_exporter_enabled")) is imported_too
    assert cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "17",
                                       "", {}, jvm=jvm)["differences"] == []


def test_the_exporter_s_agent_in_a_file_its_java_does_not_read(tmp_path):
    # (audit r5) the agent in jvm11-server.options on Java 17: the JVM does not load it, no exporter imported; the
    # line imported as it is, the file's other options (the role writes it the same, the JVM keeps not reading it)
    files = node_files("50x", add_to("jvm11-server.options", AGENT))
    cmdline = launch(files, "17", tmp_path)
    jvm = {"cmdline": cmdline, "java": "17", "environment": {}, "newer": []}
    out = cassandra_config_import(files, "50x", FACTS, jvm=jvm)
    assert not [k for k in out["vars"] if k.startswith("cassandra_prometheus_exporter")]
    assert out["vars"]["cassandra_jvm11_extra_options"] == [AGENT]
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert "cassandra_config_keep_files" not in node["keep"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


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
        "jvm17-server.options: -XX:-UseBiasedLocking: Java 17's default anyway; biased locking is deprecated on Java 17 (a"
        " warning at each start), and gone after it: the JVM does not start with it on Java 21",
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
    args = (cassandra_inventory_layout(nodes, "c"), ["/p/c.yml", "/p"], "/p/r.txt", {}, True)
    lines = cassandra_import_report(*args, cwd="/p", in_git=True) + \
        cassandra_import_report(*args, cwd="/p", in_git=True, details=True)  # (report.txt, then report.details.txt)
    at = next(i for i, line in enumerate(lines) if line.startswith("  Hand edits moved to variables: "))
    assert re.match(r"^    cassandra-env.sh line \d+: -Xms8G -Xmx8G -XX:\+AlwaysPreTouch -> cassandra_heap_size: 8G;"
                    r" cassandra_jvm17_extra_options   all \(2 nodes\)$", lines[at + 2])
    assert " ".join(x.strip() for x in lines[at + 3:at + 5]) == (
        "nothing to do now; the next apply_config rewrites cassandra-env.sh, jvm17-server.options in the collection's"
        " layout and restarts these nodes (same Java options)")
    assert lines[at + 5:at + 7] == [  # (a blank line between entries)
        "", "    cassandra_jvm17_extra_options: read by that Java only; after update_java, move them to the new Java's"]
    at = lines.index("  Java options the running Java does not use (left out of the lines moved, else left as they are)")
    assert re.match(r"^    jvm11-server.options line \d+: -Xmx4G   all \(2 nodes\)$", lines[at + 1])
    assert lines[at + 2] == "      not read, Java 17 reads jvm17-server.options"
    assert "1 hand edit moved" in lines[0]
    after = lines[next(i for i, line in enumerate(lines) if line.startswith("==== WHAT TO DO NEXT =")):]
    assert "expected: cassandra-env.sh, jvm17-server.options rewritten, no restart (the same JVM arguments), nothing" \
        " else" in \
        " ".join(" ".join(x.split()) for x in after)


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


@pytest.mark.parametrize("java, series, read", [("17.0.2", "50x", "jvm17-server.options"), ("11.0.20", "41x", "jvm11-server.options"),
                                                ("1.8.0_402", "40x", "jvm8-server.options"), ("17", "311x", None)])
def test_options_file_of_a_full_java_version_and_of_an_unknown_series(java, series, read):
    # module_utils' rule (cassandra_jvm_options.options_file): a full version read as its major, a series the
    # collection does not know has no options file known
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
    at = lines.index("    cassandra-env.sh JVM_OPTS lines and heap, not moved to variables   all (1 node)")
    assert lines[at + 1] == "      the JVM would get other values of ConcGCThreads"


def test_why_not_moved_holds_names_only(tmp_path):
    # another log to cassandra-env.sh's own GC log file (its order with it counts)
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Xlog:safepoint=debug:file=%s/logs/gc.log -Dfoo=1"'
                                           % tmp_path))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path, paths=True))[2]
    assert node["moved"] == [] and "gc.log" not in node["not_moved"] and "-Xlog" in node["not_moved"]


@pytest.mark.parametrize("keep", [True, "true", "cassandra-env.sh", ["logback.xml", "cassandra-env.sh"],
                                  "jvm17-server.options"])
def test_no_move_in_files_import_keep_hand_edits_keeps(tmp_path, keep):
    # the files kept as they are: not rewritten, the hand edits left to import_keep_hand_edits
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    cmdline = launch(files, "17", tmp_path)
    dummy, dummy2, node = imported(files, "50x", "17", cmdline, keep=keep)
    assert node["moved"] == [] and node["rewritten"] == [] and node["not_moved"] == ""
    if keep != "jvm17-server.options":
        assert [e for e in node["hand_edits"] if e.startswith("cassandra-env.sh")]
    # another file kept: the move goes on
    dummy, dummy2, node = imported(files, "50x", "17", cmdline, keep="logback.xml")
    assert node["moved"] and node["rewritten"]


def test_a_wrong_name_of_import_keep_hand_edits_said(tmp_path):
    from ansible.errors import AnsibleFilterError
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_import_error

    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    with pytest.raises(AnsibleFilterError) as raised:
        imported(files, "50x", "17", launch(files, "17", tmp_path), keep="logbak.xml")
    # the import's no_log tasks show it: the user's own value
    assert cassandra_import_error({"failed": True, "msg": "cassandra_config_import: %s" % raised.value}) == (
        "import_keep_hand_edits: logbak.xml: neither a config file of cassandra_config (cassandra.yaml, cassandra-env.sh,"
        " jvm*-server.options, cassandra-rackdc.properties, logback.xml, jmxremote.password, jmxremote.access) nor a"
        " part (os, sysctl, limits, thp, swap, timesync, disks, unit, repo, cqlsh, firewall); or true (every"
        " hand-edited file), false")


def test_no_move_when_jmx_authenticates_by_jaas(tmp_path):
    # the JAAS note says to keep cassandra-env.sh as it is: its JAAS lines not moved
    files = node_files("50x", before_extra(
        'JVM_OPTS="$JVM_OPTS -Dcassandra.jmx.remote.login.config=CassandraLogin"\n'
        'JVM_OPTS="$JVM_OPTS -Djava.security.auth.login.config=/etc/cassandra/cassandra-jaas.config"'))
    dummy, dummy2, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert node["moved"] == [] and node["rewritten"] == []
    assert node["not_moved"].startswith("JMX authenticates by JAAS")
    assert [e for e in node["hand_edits"] if e.startswith("JMX authentication by JAAS")]
    # nothing to move (the stock line uncommented, with a variable: not a line to move): no reason either
    files = node_files("50x", before_extra(
        'JVM_OPTS="$JVM_OPTS -Djava.security.auth.login.config=$CASSANDRA_CONF/cassandra-jaas.config"'))
    dummy, dummy2, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert [e for e in node["hand_edits"] if e.startswith("JMX authentication by JAAS")]
    assert node["moved"] == [] and node["not_moved"] == ""


def test_a_max_direct_memory_size_without_value_not_moved_no_error(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -XX:+MaxDirectMemorySize"'))
    dummy, dummy2, node = imported(files, "50x", "17", launch(files, "17", tmp_path))
    assert "cassandra_max_direct_memory_size" not in node["vars"]


def test_a_logback_xml_nested_too_deep_compared_by_its_lines():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_config_import

    files = node_files("50x")
    files["logback.xml"] = "<configuration>" + "<a>" * 3000 + "</a>" * 3000 + "<root level='INFO'/></configuration>"
    imported_files = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert [e for e in imported_files["hand_edits"] if e.startswith("logback.xml")]
    # cassandra_config's own test and the self-check: not the same, no failure
    from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import same_settings

    assert not same_settings("logback.xml", files["logback.xml"], node_files("50x")["logback.xml"])
    assert [d for d in check(files, "50x")["differences"] if d.startswith("logback.xml")]


@pytest.mark.parametrize("mine, other", [("-Xint", "-Xmixed"), ("-Xrs", "-XX:-ReduceSignalUsage"),
                                         ("-Xshare:off", "-XX:+UseSharedSpaces"), ("-Xnoclassgc", "-XX:+ClassUnloading")])
def test_no_move_before_another_spelling_the_import_does_not_see(tmp_path, mine, other):
    # JVM_EXTRA_OPTS from a file the import does not read (/etc/default/cassandra), a hand line after it: moved
    # before it, the other spelling would win
    files = node_files("50x", add_to("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS %s"' % mine))
    env = {"JVM_EXTRA_OPTS": other}
    cmdline = launch(files, "17", tmp_path, env)
    inventory, layout, node = imported(files, "50x", "17", cmdline, environment={})
    if node["moved"]:
        rendered, dummy = role_files(inventory, layout, files)
        assert settings(launch(rendered, "17", tmp_path, env)) == settings(cmdline)
    assert node["moved"] == []


def test_not_moved_without_the_options_file_read(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    cmdline = launch(files, "17", tmp_path)
    for name in ("jvm-server.options", "jvm17-server.options"):
        node = imported(dict((k, v) for k, v in files.items() if k != name), "50x", "17", cmdline)[2]
        assert node["moved"] == [] and node["not_moved"] == "%s not read" % name


def test_not_moved_when_the_unit_gives_mx4j(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    environment = {"MX4J_PORT": "8081"}
    cmdline = launch(files, "17", tmp_path, environment)
    node = imported(files, "50x", "17", cmdline, environment=environment)[2]
    assert node["moved"] == [] and "MX4J" in node["not_moved"]


def test_no_reason_without_a_line_to_move(tmp_path):
    files = node_files("50x", add_to("jvm17-server.options", "-XX:+UseNUMA"))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert node["moved"] == [] and node["not_moved"] == ""


def test_options_that_add_up_counted():
    assert jvm.differences(["-XX:OnError=a", "-XX:OnError=b"], ["-XX:OnError=b"]) == ["OnError"]


def test_a_moved_line_with_a_secret_shows_its_other_options(tmp_path):
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dcom.example.password=S3cr3t -Dz=1"'))
    node = imported(files, "50x", "17", launch(files, "17", tmp_path))[2]
    assert node["moved"] and "S3cr3t" not in str(node["moved"]) and "-Dz=1" in node["moved"][0]


@pytest.mark.parametrize("java", ["11", "17"])
def test_a_5_0_heap_newsize_with_g1_kept_as_found(tmp_path, java):
    # a 5.0 node of the e2e: its stock MAX_HEAP_SIZE and HEAP_NEWSIZE lines uncommented (G1 ignores HEAP_NEWSIZE):
    # the import carries both, the role writes them back as they are, so apply_config after the import changes
    # nothing (it rewrote cassandra-env.sh and restarted the node)
    def edit(files):
        text = files["cassandra-env.sh"]
        assert '#MAX_HEAP_SIZE="20G"\n#HEAP_NEWSIZE="10G"\n' in text
        files["cassandra-env.sh"] = text.replace('#MAX_HEAP_SIZE="20G"\n#HEAP_NEWSIZE="10G"\n',
                                                 'MAX_HEAP_SIZE="512M"\nHEAP_NEWSIZE="128M"\n')
    files = node_files("50x", edit)
    cmdline = launch(files, java, tmp_path)
    inventory, layout, node = imported(files, "50x", java, cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables.get("cassandra_heap_size") == "512M" and variables.get("cassandra_heap_newsize") == "128M"
    import difflib
    assert [d for d in difflib.unified_diff(files["cassandra-env.sh"].split("\n"), rendered["cassandra-env.sh"].split("\n"),
                                            lineterm="", n=0) if d[:1] in "+-" and d[:3] not in ("+++", "---")
            and "Managed by Ansible" not in d] == []  # the role's mark aside, line for line
    assert node["hand_edits"] == [] and "cassandra-env.sh" not in node.get("rewritten", [])
    assert settings(launch(rendered, java, tmp_path)) == settings(cmdline)


# (2026-11-17) a stock 5.0 RPM node with remote JMX set by hand: LOCAL_JMX=no below the stock line commented out, the
# password file, access file and rmi hostname in the remote branch, the stock password file line still on after fi, the heap
# in jvm17-server.options, the log dir written as itself in logback.xml, jvm11-server.options edited (Java 17)
def remote_jmx_by_hand(files):
    env = edit(files["cassandra-env.sh"], "    LOCAL_JMX=yes", "    #LOCAL_JMX=yes\n    LOCAL_JMX=no")
    authenticate = '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=true"\n'
    files["cassandra-env.sh"] = edit(env, authenticate, authenticate + "\n"
                                     '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file='
                                     '/etc/cassandra/jmxremote.password"\n'
                                     '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.access.file='
                                     '/etc/cassandra/jmxremote.access"\n'
                                     '  JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.1"\n')
    files["jmxremote.password"] = "ops s3cret\n"
    files["jmxremote.access"] = "ops readwrite\n"


def heap_in_jvm17(files):
    files["jvm17-server.options"] = files["jvm17-server.options"].rstrip("\n") + "\n-Xms23552m\n-Xmx23552m\n"


def log_dir_as_itself(files):
    for tag in ("<file>", "<fileNamePattern>"):
        for log in ("system", "debug"):
            files["logback.xml"] = edit(files["logback.xml"], "%s${cassandra.logdir}/%s" % (tag, log),
                                        "%s/var/log/cassandra/%s" % (tag, log))


def jvm11_edited(files):
    # (switched off otherwise than the role does, ##: hand edits; with # or removed: cassandra_jvm11_removed_options)
    text = edit(files["jvm11-server.options"], "-XX:-UseBiasedLocking\n", "##-XX:-UseBiasedLocking\n")
    text = edit(text, "-Dio.netty.tryReflectionSetAccessible=true\n", "##-Dio.netty.tryReflectionSetAccessible=true\n")
    files["jvm11-server.options"] = edit(text, "--add-exports java.base/jdk.internal.util=ALL-UNNAMED\n",
                                         "##--add-exports java.base/jdk.internal.util=ALL-UNNAMED\n")


def running(files, tmp_path):
    """The running JVM's arguments, its log dir the packages' (bin/cassandra gives -Dcassandra.logdir)."""
    return re.sub(r"-Dcassandra\.logdir=[^\0]*", "-Dcassandra.logdir=/var/log/cassandra", launch(files, "17", tmp_path))


def test_remote_jmx_set_by_hand_is_imported(tmp_path):
    # LOCAL_JMX=no was not read beside the stock line commented out (paired with it): LOCAL_JMX=yes written
    files = node_files("50x", remote_jmx_by_hand)
    cmdline = running(files, tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_local_jmx"] is False
    assert variables["cassandra_jmx_rmi_hostname"] == IPV4  # the node's address
    assert [u["name"] for u in variables["cassandra_jmx_users"]] == ["ops"]
    assert 'JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.access.file=/etc/cassandra/jmxremote.access"' \
        in rendered["cassandra-env.sh"].split("\n")
    assert settings(launch(rendered, "17", tmp_path)) == settings(launch(files, "17", tmp_path))
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == []
    assert [n for n in out["notes"] if n.startswith("cassandra-env.sh") and "the same arguments" in n]
    # Cassandra not running: LOCAL_JMX=no read all the same
    assert cassandra_config_import(files, "50x", FACTS)["vars"]["cassandra_local_jmx"] is False


def test_remote_jmx_by_hand_with_the_heap_moved_and_the_log_dir_as_itself(tmp_path):
    # the heap moved to jvm-server.options failed the check once cassandra-env.sh differed (5 items)
    files = node_files("50x", both(remote_jmx_by_hand, heap_in_jvm17, log_dir_as_itself))
    cmdline = running(files, tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    variables = role_files(inventory, layout, files)[1]
    assert variables["cassandra_heap_size"] == "23552m" and variables["cassandra_heap_in_jvm_options"] is True
    assert variables.get("cassandra_log_system_dir", "${cassandra.logdir}") == "${cassandra.logdir}"  # (as itself)
    assert [n for n in node["normalized"] if "/var/log/cassandra written as itself" in n]
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == []


def test_a_jvm_options_file_the_running_java_does_not_read_is_a_note(tmp_path):
    files = node_files("50x", both(remote_jmx_by_hand, heap_in_jvm17, log_dir_as_itself, jvm11_edited))
    cmdline = running(files, tmp_path)
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == []
    unread = [n for n in out["notes"] if n.startswith("jvm11-server.options")]
    assert unread and all(n.endswith("(no effect: Java 17 does not read this file; it matters only if the node runs"
                                     " Java 11)") for n in unread)
    assert any("UseBiasedLocking" in n for n in unread) and any("tryReflectionSetAccessible" in n for n in unread)
    # the Java that reads it: a difference still
    files = node_files("50x", jvm11_edited)
    assert [d for d in check(files, "50x", java="11")["differences"] if d.startswith("jvm11-server.options")]


@pytest.mark.parametrize("later, again", [
    ([{"depth": 0, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}], True),
    ([{"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}], True),  # again below in the same block
    ([{"depth": 1, "text": "else"}, {"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}], False),  # another branch
    ([{"depth": 1, "text": "fi"}, {"depth": 0, "text": "if [ -n x ]; then"},
      {"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}], False),  # another block: maybe not run
    ([{"depth": 2, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}], False),  # in a block inside it: maybe not run
    ([{"depth": 0, "text": 'JVM_OPTS="$JVM_OPTS -Dx=2"'}], False),
])
def test_a_line_in_a_block_set_again_below(later, again):
    assert jvm._set_again({"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}, later) is again
    in_block = again and later[-1]["depth"] == 1  # (in_block: the same block only, not outside any)
    assert jvm._set_again({"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}, later, in_block=True) is in_block
    assert jvm._set_again({"depth": 0, "text": 'JVM_OPTS="$JVM_OPTS -Dx=1"'}, later) is False


@pytest.mark.parametrize("text", [
    'JVM_OPTS="$JVM_OPTS -XX:CompileCommand=exclude,a/B.c"',  # a list: each one adds up
    'JVM_OPTS="$JVM_OPTS -javaagent:x.jar"',  # no name
    'JVM_OPTS="$JVM_OPTS -Xmx4G"',  # what cassandra-env.sh greps for
    'JVM_OPTS="$JVM_OPTS -XX:+UnlockDiagnosticVMOptions"',  # unlocks the options after it
    'JVM_OPTS="$JVM_OPTS -XX:VMOptionsFile=/etc/x.options"',  # read where it is
    'JVM_OPTS="$JVM_OPTS -XX:Flags=/etc/x.flags"',
])
def test_a_line_whose_place_counts_is_never_taken_as_set_again(text):
    assert jvm._set_again({"depth": 1, "text": text}, [{"depth": 0, "text": text}]) is False


def test_a_line_reading_jvm_opts_in_between_keeps_the_block_s_line():
    line = {"depth": 1, "text": 'JVM_OPTS="$JVM_OPTS -Dfoo=1"'}
    grep = {"depth": 0, "text": 'if echo "$JVM_OPTS" | grep -q -- -Dfoo=1; then JVM_OPTS="$JVM_OPTS -XX:+X"; fi'}
    again = {"depth": 0, "text": line["text"]}
    assert jvm._set_again(line, [grep, again]) is False
    # lines that only add to JVM_OPTS, or do not mention it: as before
    assert jvm._set_again(line, [{"depth": 0, "text": 'JVM_OPTS="$JVM_OPTS -Dbar=2"'}, {"depth": 0, "text": "x=1"},
                                 again]) is True


def test_the_heap_in_a_block_and_again_after_fi_stays_a_difference():
    # -Xmx is what cassandra-env.sh greps for: its line in a block is never dropped as set again
    def heap_twice(files):
        files["cassandra-env.sh"] = files["cassandra-env.sh"] + (
            '\nif [ -n "$X" ]; then\n    JVM_OPTS="$JVM_OPTS -Xmx4G"\nfi\nJVM_OPTS="$JVM_OPTS -Xmx4G"\n')
    files = node_files("50x", heap_twice)
    assert [d for d in check(files, "50x", java="17")["differences"] if d.startswith("cassandra-env.sh")]


def own_log_dirs(files):
    """system.log and debug.log in a dir of their own, the audit file's dir the log dir written as itself."""
    text = files["logback.xml"]
    for tag in ("<file>", "<fileNamePattern>"):
        for log in ("system", "debug"):
            text = edit(text, "%s${cassandra.logdir}/%s" % (tag, log), "%s/srv/zorglub/logs/%s" % (tag, log))
        text = text.replace("%s${cassandra.logdir}/audit/" % tag, "%s/var/log/cassandra/audit/" % tag)
    files["logback.xml"] = text


@pytest.mark.parametrize("on", [False, True], ids=["without the earlier edits", "with them"])
def test_system_and_debug_log_in_a_dir_of_their_own(tmp_path, on):
    files = node_files("50x", both(remote_jmx_by_hand, heap_in_jvm17, jvm11_edited, own_log_dirs) if on else own_log_dirs)
    cmdline = running(files, tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_log_system_dir"] == variables["cassandra_log_debug_dir"] == "/srv/zorglub/logs"
    assert "cassandra_log_audit_dir" not in variables  # (the log dir written as itself: the default)
    assert "<file>/srv/zorglub/logs/system.log</file>" in rendered["logback.xml"]
    assert "<fileNamePattern>/srv/zorglub/logs/debug.log.%d{yyyy-MM-dd}.%i.zip</fileNamePattern>" in rendered["logback.xml"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def log_dirs_apart(system, debug, audit):
    """system.log, debug.log and audit.log each in a dir of its own (as written in the XML: & as &amp;)."""
    def edit_dirs(files):
        text = files["logback.xml"]
        for tag in ("<file>", "<fileNamePattern>"):
            for log, where in (("system", system), ("debug", debug)):
                text = edit(text, "%s${cassandra.logdir}/%s" % (tag, log), "%s%s/%s" % (tag, where, log))
            text = text.replace("%s${cassandra.logdir}/audit/" % tag, "%s%s/" % (tag, audit))
        files["logback.xml"] = text
    return edit_dirs


def test_system_debug_and_audit_dirs_apart_pass_the_self_check(tmp_path):
    files = node_files("50x", log_dirs_apart("/srv/sys/logs", "/srv/debug/logs", "/srv/audit/logs"))
    cmdline = running(files, tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_log_system_dir"] == "/srv/sys/logs"
    assert variables["cassandra_log_debug_dir"] == "/srv/debug/logs"
    assert variables["cassandra_log_audit_dir"] == "/srv/audit/logs"
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def test_a_log_dir_with_an_ampersand_is_read_as_the_xml_writes_it(tmp_path):
    # findtext decodes &amp;: read back escaped, the role writes the same line (never /data/R&D, invalid XML)
    files = node_files("50x", log_dirs_apart("/data/R&amp;D/sys", "/data/R&amp;D/debug", "/data/R&amp;D/audit"))
    cmdline = running(files, tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert [variables[k] for k in ("cassandra_log_system_dir", "cassandra_log_debug_dir", "cassandra_log_audit_dir")] \
        == ["/data/R&amp;D/sys", "/data/R&amp;D/debug", "/data/R&amp;D/audit"]
    assert "<file>/data/R&amp;D/sys/system.log</file>" in rendered["logback.xml"]
    assert check(files, "50x", java="17", cmdline=cmdline)["differences"] == []


def test_a_log_dir_its_rolled_files_do_not_share_stays_a_hand_edit(tmp_path):
    def edit_logs(files):
        files["logback.xml"] = edit(files["logback.xml"], "<file>${cassandra.logdir}/system.log",
                                    "<file>/srv/zorglub/logs/system.log")
    files = node_files("50x", edit_logs)
    cmdline = running(files, tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert "cassandra_log_system_dir" not in node["vars"]
    assert [d for d in check(files, "50x", java="17", cmdline=cmdline)["differences"] if d.startswith("logback.xml")]


@pytest.mark.parametrize("series", ["4.0", "4.1", "5.0"])
def test_the_log_dirs_by_default_render_the_stock_lines(series):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _load_role, _render
    key = {"4.0": "40x", "4.1": "41x", "5.0": "50x"}[series]
    env, ctx, dummy = _load_role(key, FACTS)
    text = "\n".join(_render(env, key, "logback.xml", ctx)[1])
    for log in ("system", "debug"):
        assert "<file>${cassandra.logdir}/%s.log</file>" % log in text
        assert "<fileNamePattern>${cassandra.logdir}/%s.log.%%d{yyyy-MM-dd}.%%i.zip</fileNamePattern>" % log in text


# apply_config's proof that the files it writes change nothing for the running Cassandra (no restart for them),
# the self-check's own: (2026-11-17) the owner's case after an import with rc12, 12 nodes on Java 17: cassandra-env.sh
# laid out otherwise, the heap moved from jvm17-server.options to jvm-server.options, jvm11-server.options' stock
# lines put back (Java 17 does not read it), logback.xml's audit file under ${cassandra.logdir} where the node writes
# /var/log/cassandra (the log dir), a stray text of it removed
def audit_dir_written_out(files):
    file_auditing(files, "50x")
    files["logback.xml"] = audit_on(files["logback.xml"])
    for tag in ("<file>", "<fileNamePattern>"):
        files["logback.xml"] = files["logback.xml"].replace("%s${cassandra.logdir}/audit/" % tag,
                                                            "%s/var/log/cassandra/audit/" % tag)
    files["logback.xml"] = edit(files["logback.xml"], "</root>", "</root> >")


OWNERS_CASE = both(remote_jmx_by_hand, heap_in_jvm17, jvm11_edited, audit_dir_written_out)
STORAGE = "/var/lib/cassandra"


def written(tmp_path, node_edit=OWNERS_CASE, rc12=True):
    """(the node's files, the files apply_config writes, the ones it changes, the running JVM's arguments)."""
    files = node_files("50x", node_edit)
    cmdline = running(files, tmp_path)
    inventory, layout, dummy = imported(files, "50x", "17", cmdline)
    variables = cassandra_inventory_host_vars(inventory, layout["hosts"], "n1")
    rendered, errors, dummy, dummy2 = role_render(variables, FACTS, files)
    assert errors == []
    if rc12:  # (an inventory an rc12 import wrote: the audit dir not read, the role's own, the stray text gone)
        rendered["logback.xml"] = files["logback.xml"].replace("/var/log/cassandra/audit/", "${cassandra.logdir}/audit/") \
            .replace("</root> >", "</root>")
    changed = [f for f in rendered if f in files and rendered[f] != files[f]
               and not cassandra_same_settings(files[f], rendered[f], f, STORAGE)]
    return files, rendered, changed, cmdline


def no_effect(changed, files, rendered, cmdline, java="17", environ=None, newer=None):
    return cassandra_apply_config_no_effect(changed, files, rendered, cmdline, "50x", java, environ or {}, newer or [])


def test_the_owners_case_is_proven_no_restart(tmp_path):
    files, rendered, changed, cmdline = written(tmp_path)
    assert sorted(changed) == ["cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm17-server.options",
                               "logback.xml"]
    assert no_effect(changed, files, rendered, cmdline) == [
        "the same JVM arguments", "jvm11-server.options not read by Java 17", "logback.xml read again by logback (scan)"]


def test_a_real_heap_change_restarts(tmp_path):
    files, rendered, changed, cmdline = written(tmp_path)
    rendered = dict(rendered, **{"jvm-server.options": rendered["jvm-server.options"].replace("-Xmx23552m", "-Xmx16G")})
    assert "-Xmx16G" in rendered["jvm-server.options"]
    assert no_effect(changed, files, rendered, cmdline) == []


def test_logback_without_scan_restarts(tmp_path):
    files, rendered, changed, cmdline = written(tmp_path)
    unscanned = dict(files, **{"logback.xml": files["logback.xml"].replace(' scan="true"', "")})
    assert no_effect(changed, unscanned, rendered, cmdline) == []
    # the new file without it: the same
    assert no_effect(changed, files, dict(rendered, **{"logback.xml": rendered["logback.xml"].replace(' scan="true"', "")}),
                     cmdline) == []


def test_logback_written_since_the_start_restarts(tmp_path):
    # (pre-push audit, rc13) scan="true" added after the JVM started (or no record of what it read): logback scans
    # only if it read scan="true" at its start
    files, rendered, changed, cmdline = written(tmp_path)
    assert no_effect(changed, files, rendered, cmdline, newer=["logback.xml"]) == []


@pytest.mark.parametrize("head", [
    '<!-- <configuration scan="true"> -->\n<configuration>',  # in a comment
    '<configuration name=" scan=\'true\'">',  # in another attribute's value
    '<configuration scan="yes">',  # (logback: true only)
    '<configuration scan="true">\n<unclosed>',  # a file that does not parse
])
def test_logback_scan_read_as_logback_reads_it(tmp_path, head):
    # (pre-push audit, rc13) the root's scan attribute, parsed; anything else proves nothing
    files, rendered, changed, cmdline = written(tmp_path)
    live = dict(files, **{"logback.xml": re.sub(r"<configuration[^>]*>", lambda m: head, files["logback.xml"], count=1)})
    assert "scan" in live["logback.xml"]
    assert no_effect(changed, live, rendered, cmdline) == []


@pytest.mark.parametrize("release, proven", [("5.0.2", True), ("4.1.7", False)])
def test_a_jvm_of_another_series_than_the_inventorys_restarts(tmp_path, release, proven):
    # (pre-push audit, rc13) the package upgraded under the running JVM (cassandra_version, the inventory's series,
    # is the installed one): its arguments are not emulated with the new series' files
    files, rendered, changed, cmdline = written(tmp_path)
    java, rest = cmdline.split("\0", 1)
    cmdline = "\0".join([java, "-cp", "/usr/share/cassandra/apache-cassandra-%s.jar:/usr/share/cassandra/lib/x.jar" % release,
                         rest])
    assert bool(no_effect(changed, files, rendered, cmdline)) is proven


def test_the_import_and_apply_config_judge_the_environment_alike(tmp_path):
    # (pre-push audit, rc13, rounds 1-3) one rule (env_differs) for the import's "no restart (the same JVM arguments)"
    # and apply_config's proof: what the environment gives cassandra-env.sh to add, the same on both sides, is carried
    # as it is (rc12's rule); another value on one side gives up; a value as text (8081, "8081"), None as unset
    files, rendered, changed, cmdline = written(tmp_path)
    group = ["cassandra-env.sh", "jvm-server.options", "jvm17-server.options"]
    live, role = [dict((f, side[f]) for f in group) for side in (files, rendered)]
    same = jvm.cassandra_jvm_args_same
    assert same(cmdline, live, role, "50x", "17", {}, {})[0] is True
    assert jvm.env_differs({"MX4J_PORT": 8081, "JVM_OPTS": None}, {"MX4J_PORT": "8081", "JVM_OPTS": ""}) == []
    assert jvm.env_differs({"MX4J_PORT": 0}, {}) == ["MX4J_PORT"]
    for node_env, role_env in (({"JVM_OPTS": "-Dy=2"}, {}), ({}, {"MX4J_PORT": "8081"}),
                               ({"JVM_EXTRA_OPTS": "-Dx=1"}, {"JVM_EXTRA_OPTS": "-Dx=2"})):
        assert same(cmdline, live, role, "50x", "17", node_env, role_env) == (
            False, "the roles would give cassandra-env.sh other options to add (%s)" % list(dict(node_env, **role_env))[0])


def test_jvm_opts_of_the_environment_reach_the_proof_through_the_command_line(tmp_path):
    # (pre-push audit, rc13, round 3) bin/cassandra puts the environment's JVM_OPTS before the options files' words:
    # the same environment on both sides, the running JVM's arguments with them, proven
    files, rendered, changed, cmdline = written(tmp_path)
    java, rest = cmdline.split("\0", 1)
    cmdline = "\0".join([java, "-Dy=2", rest])
    assert no_effect(changed, files, rendered, cmdline, environ={"JVM_OPTS": "-Dy=2"})


def test_an_environment_file_value_gives_up_on_both_sides_alike(tmp_path):
    # (pre-push audit, rc13, round 3) JVM_EXTRA_OPTS set outside the unit's Environment (an EnvironmentFile,
    # /etc/default/cassandra), as the JVM has it: with cassandra-env.sh's lines moved, the import no longer says
    # "no restart" and apply_config restarts (both read it from the JVM; whatever the unit's mode)
    files, rendered, changed, cmdline = written(tmp_path)
    environment = {"JVM_EXTRA_OPTS": "-Dx=1"}
    assert imported(files, "50x", "17", cmdline)[2]["rewritten"]
    assert imported(files, "50x", "17", cmdline, environment=environment)[2]["rewritten"] == []
    assert no_effect(changed, files, rendered, cmdline, environ=environment) == []


@pytest.mark.parametrize("java, environ, newer, more", [
    ("", None, None, []),  # the running Java not known: which options file it reads is not
    ("17", {"JVM_EXTRA_OPTS": "-Dx=1"}, None, []),  # the environment adds options
    ("17", None, ["jvm-server.options"], []),  # written since that JVM started: its arguments not theirs
    ("17", None, None, ["cassandra.yaml"]),  # a file no proof covers
])
def test_anything_unproven_restarts(tmp_path, java, environ, newer, more):
    files, rendered, changed, cmdline = written(tmp_path)
    assert no_effect(changed + more, files, rendered, cmdline, java, environ, newer) == []
    assert no_effect(changed, files, rendered, "", java="17") == []  # no arguments read


def test_the_self_check_reads_the_environment_as_apply_config(tmp_path):
    # (pre-push audit, rc13, round 4) JVM_EXTRA_OPTS the JVM got from outside its unit (an EnvironmentFile,
    # /etc/default/cassandra): the self-check no longer passes the JVM's files rewritten as "the same arguments"
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import cassandra_import_self_check
    files = node_files("50x", OWNERS_CASE)
    cmdline = running(files, tmp_path)
    jvm_read = {"cmdline": cmdline, "java": "17", "environment": {}, "newer": []}
    inventory, layout = import_node(files, "50x", jvm=jvm_read)
    found = dict((name, cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {},
                                                    "/var/lib/cassandra", "17", "", {}, jvm=dict(jvm_read, environment=env)))
                 for name, env in (("unit only", {}), ("environment file", {"JVM_EXTRA_OPTS": "-Dx=1"})))
    assert found["unit only"]["differences"] == []
    assert [n for n in found["unit only"]["notes"] if "the JVM gets the same arguments" in n]
    assert not [n for n in found["environment file"]["notes"] if "the JVM gets the same arguments" in n]
    assert found["environment file"]["differences"]


def test_the_self_check_compares_the_roles_environment_with_the_nodes(tmp_path):
    # (pre-push audit, rc13, round 5) the heap the unit gives cassandra-env.sh: the roles' (16G) against the node's
    # (8G), no "same arguments"; the same environment on both sides, as before
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import _jvm_rewritten
    files = node_files("50x")
    heap = {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "800M"}
    cmdline = launch(files, "17", tmp_path, heap)
    jvm_read = {"cmdline": cmdline, "newer": [], "series": "50x"}

    def rewritten(role_env):
        return _jvm_rewritten(files, files, {"cassandra-env.sh": ["x"]}, False, heap, role_env, "17", jvm_read)[0]
    assert rewritten(heap) == ["cassandra-env.sh"]
    assert rewritten(None) == ["cassandra-env.sh"]  # (the unit kept: the node's)
    assert rewritten(dict(heap, MAX_HEAP_SIZE="16G")) == []


# --- 4.x: CASSANDRA_HEAPDUMP_DIR set plainly ---------------------------------------------------------------------

HEAPDUMP = after_line("# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR", "CASSANDRA_HEAPDUMP_DIR=/srv/zorglub/dumps")


@pytest.mark.parametrize("series", ["40x", "41x"])
def test_a_plain_heap_dump_dir_is_the_roles_when_the_environment_has_none(tmp_path, series):
    """CASSANDRA_HEAPDUMP_DIR=<dir> above the stock if: the role's "${CASSANDRA_HEAPDUMP_DIR:-<dir>}" gives the JVM
    the same dir when its environment has none (read from the running JVM)."""
    files = node_files(series, HEAPDUMP)
    cmdline = launch(files, JAVA[series], tmp_path)
    inventory, layout, node = imported(files, series, JAVA[series], cmdline)
    assert node["vars"]["cassandra_heap_dump_dir"] == "/srv/zorglub/dumps" and node["hand_edits"] == []
    assert any("CASSANDRA_HEAPDUMP_DIR=/srv/zorglub/dumps read as cassandra_heap_dump_dir" in line
               for line in node["normalized"])
    assert check(files, series, java=JAVA[series], cmdline=cmdline)["differences"] == []


def test_a_plain_heap_dump_dir_is_not_the_roles_when_the_environment_sets_it(tmp_path):
    """The environment gives CASSANDRA_HEAPDUMP_DIR: the node's line wins over it, the role's would not: not the same;
    the JVM not read (its environment not known): not proven either."""
    files = node_files("41x", HEAPDUMP)
    env = {"CASSANDRA_HEAPDUMP_DIR": "/srv/other"}
    cmdline = launch(files, "11", tmp_path, environment=env)
    inventory, layout, node = imported(files, "41x", "11", cmdline, environment=env)
    assert "cassandra_heap_dump_dir" not in node["vars"]
    assert check(files, "41x", java="11", cmdline=cmdline, env=env)["differences"] != []
    inventory, layout, node = imported(files, "41x", "11", "")
    assert "cassandra_heap_dump_dir" not in node["vars"]
    assert check(files, "41x", java="11")["differences"] != []


# --- the JMX lines moved into the remote branch ----------------------------------------------------------------

def jmx_in_remote_branch(files):
    """Remote JMX by hand (2026-11-18): LOCAL_JMX=no, the password file, access file and rmi hostname lines indented
    in the remote branch, their stock places commented out."""
    env = edit(files["cassandra-env.sh"], "    LOCAL_JMX=yes", "    LOCAL_JMX=no")
    env = edit(env, '\nJVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file=',
               '\n#JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file=')
    authenticate = '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=true"\n'
    files["cassandra-env.sh"] = edit(env, authenticate, authenticate + "\n"
                                     '    JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.password.file='
                                     '/srv/zorglub/jmx/jmxremote.password"\n'
                                     '    JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.access.file='
                                     '/srv/zorglub/jmx/jmxremote.access"\n'
                                     '    JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.1"\n')
    files["jmxremote.password"] = "ops s3cret\n"
    files["jmxremote.access"] = "ops readwrite\n"


@pytest.mark.parametrize("series", sorted(JAVA))
def test_jmx_lines_in_the_remote_branch_it_takes_are_the_roles(tmp_path, series):
    files = node_files(series, jmx_in_remote_branch)
    cmdline = launch(files, JAVA[series], tmp_path)
    inventory, layout, node = imported(files, series, JAVA[series], cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_jmx_remote_password_file"] == "/srv/zorglub/jmx/jmxremote.password"
    assert variables["cassandra_jmx_remote_access_file"] == "/srv/zorglub/jmx/jmxremote.access"
    assert variables["cassandra_jmx_rmi_hostname"] == IPV4
    assert node["hand_edits"] == []
    # (the branch the running JVM took, from its remote JMX port)
    assert [len(lines) for lines in jvm.remote_branch_moved(files["cassandra-env.sh"], rendered["cassandra-env.sh"], {},
                                                            cmdline)] == [3, 3]
    out = check(files, series, java=JAVA[series], cmdline=cmdline)
    assert out["differences"] == []
    assert any(line.startswith("cassandra-env.sh: 3 lines adding options to JVM_OPTS in the remote JMX branch")
               for line in out["notes"])
    # (and so they are: bash gives the JVM the same from the role's file)
    assert settings(launch(rendered, JAVA[series], tmp_path)) == settings(cmdline)


def jmx_lines_are_hand_edits(files, cmdline, env=None):
    """The node's JMX lines in the remote branch: hand edits for the import, differences for its self-check."""
    inventory, layout, node = imported(files, "41x", "11", cmdline, environment=env)
    assert any("-Djava.rmi.server.hostname=10.0.0.1" in line for line in node["hand_edits"])
    out = check(files, "41x", java="11", env=env, cmdline=cmdline)
    assert any("-Djava.rmi.server.hostname=10.0.0.1" in d for d in out["differences"])


def test_jmx_lines_in_the_remote_branch_not_taken_stay_hand_edits(tmp_path):
    """LOCAL_JMX=yes from the environment: the local branch runs, the node's lines in the remote one do not."""
    files = node_files("41x", jmx_in_remote_branch)
    env = {"LOCAL_JMX": "yes"}
    jmx_lines_are_hand_edits(files, launch(files, "11", tmp_path, environment=env), env)


def test_jmx_lines_in_the_remote_branch_unseen_local_jmx_stay_hand_edits(tmp_path):
    """(pre-push audit, rc13, round 9) LOCAL_JMX=yes from an EnvironmentFile or /etc/default/cassandra, in no
    environment the import reads: the running JVM's arguments (its local JMX port) say the local branch ran, not the
    file's default (no)."""
    files = node_files("41x", jmx_in_remote_branch)
    cmdline = launch(files, "11", tmp_path, environment={"LOCAL_JMX": "yes"})
    assert "-Djava.rmi.server.hostname=10.0.0.1" not in cmdline
    jmx_lines_are_hand_edits(files, cmdline)


def test_jmx_lines_in_the_remote_branch_not_proven_without_the_jvm_or_its_remote_port_alone(tmp_path):
    """Cassandra not running (the branch bash takes not known: LOCAL_JMX may come from an EnvironmentFile): hand
    edits; its arguments with both JMX ports, or neither: not proven either."""
    files = node_files("41x", jmx_in_remote_branch)
    jmx_lines_are_hand_edits(files, "")
    cmdline = launch(files, "11", tmp_path)
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    role = role_files(inventory, layout, files)[0]["cassandra-env.sh"]
    assert jvm.remote_branch_moved(files["cassandra-env.sh"], role, {}, cmdline) != ([], [])
    remote = "-Dcassandra.jmx.remote.port=7199"
    assert remote in cmdline.split("\0")
    for other in (cmdline + "\0-Dcassandra.jmx.local.port=7199", cmdline.replace(remote, "-Dx=1"), ""):
        assert jvm.remote_branch_moved(files["cassandra-env.sh"], role, {}, other) == ([], [])
    # (LOCAL_JMX yes in the unit's environment: the next start takes the local branch)
    assert jvm.remote_branch_moved(files["cassandra-env.sh"], role, {"LOCAL_JMX": "yes"}, cmdline) == ([], [])


def test_jmx_lines_in_the_remote_branch_with_jvm_opts_read_between_stay_hand_edits(tmp_path):
    """A line below them in the branch reads JVM_OPTS (rewrites it): the place of the lines the role writes after the
    block counts (the rmi hostname, above it in the role's file, does not cross that line)."""
    files = node_files("41x", jmx_in_remote_branch)
    files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], '-Djava.rmi.server.hostname=10.0.0.1"\n',
                                     '-Djava.rmi.server.hostname=10.0.0.1"\n'
                                     '    JVM_OPTS="$(echo "$JVM_OPTS" | sed s/password/x/)"\n')
    cmdline = launch(files, "11", tmp_path)
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    assert any("jmxremote.password.file=/srv/zorglub" in line for line in node["hand_edits"])
    assert not any("-Djava.rmi.server.hostname=10.0.0.1" in line for line in node["hand_edits"])
    out = check(files, "41x", java="11", cmdline=cmdline)
    assert out["differences"] != [] and any(n.startswith("cassandra-env.sh: 1 line adding") for n in out["notes"])


def heap_in_jvm11_dump_off(files):
    """The heap in jvm11-server.options (jvm-server.options' own lines commented out at another size), the heap dump
    on OutOfMemoryError switched off."""
    text = edit(files["jvm-server.options"], "\n-XX:+HeapDumpOnOutOfMemoryError\n", "\n# -XX:+HeapDumpOnOutOfMemoryError\n")
    files["jvm-server.options"] = edit(edit(text, "\n#-Xms4G\n", "\n#-Xms8G\n"), "\n#-Xmx4G\n", "\n#-Xmx8G\n")
    files["jvm11-server.options"] += "-Xms10G\n-Xmx10G\n"


@pytest.mark.parametrize("running_jvm", [True, False])
def test_a_41_rpm_node_with_remote_jmx_by_hand_imports_clean(tmp_path, running_jvm):
    """(2026-11-18) 4.1, Java 11: the heap in jvm11-server.options, a plain CASSANDRA_HEAPDUMP_DIR, the heap dump
    switched off, the JMX lines in the remote branch: imported, the self-check clean, whether Cassandra runs or not
    (not running: CASSANDRA_HEAPDUMP_DIR and the JMX lines not proven, the rest is)."""
    files = node_files("41x", both(jmx_in_remote_branch, HEAPDUMP, heap_in_jvm11_dump_off))
    cmdline = launch(files, "11", tmp_path) if running_jvm else ""
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    rendered, variables = role_files(inventory, layout, files)
    assert variables["cassandra_jvm_removed_options"] == ["-XX:+HeapDumpOnOutOfMemoryError"]
    out = check(files, "41x", java="11", cmdline=cmdline)
    if running_jvm:
        assert node["hand_edits"] == [] and out["differences"] == []
        assert variables["cassandra_heap_dump_dir"] == "/srv/zorglub/dumps"
        assert settings(launch(rendered, "11", tmp_path)) == settings(cmdline)
    else:
        jmx = r"HEAPDUMP|rmi.server.hostname|would write fi|has fi,"
        assert [d for d in out["differences"] if not re.search(jmx, d)] == []


# --- apply_config after an import: a plain CASSANDRA_HEAPDUMP_DIR read as the import reads it --------------------

def heap_dump_dir_of_role(series, folder):
    """The stock cassandra-env.sh with the role's line for cassandra_heap_dump_dir: folder."""
    text = node_files(series)["cassandra-env.sh"]
    return edit(text, "# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR\n",
                'CASSANDRA_HEAPDUMP_DIR="${CASSANDRA_HEAPDUMP_DIR:-%s}"  # set jvm HeapDumpPath with it\n' % folder)


def dump_path(cmdline):
    return [a for a in cmdline.split("\0") if a.startswith("-XX:HeapDumpPath")]


@pytest.mark.parametrize("which", ["heapdump", "all"])
def test_apply_config_after_import_proves_a_plain_heap_dump_dir(tmp_path, which):
    """(pre-push audit, rc13, round 9) a plain CASSANDRA_HEAPDUMP_DIR imported clean: the next apply_config proves the
    role's files change nothing for the running JVM (no restart), as the import and its self-check do."""
    files = node_files("41x", HEAPDUMP if which == "heapdump"
                       else both(jmx_in_remote_branch, HEAPDUMP, heap_in_jvm11_dump_off))
    cmdline = launch(files, "11", tmp_path)
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    assert node["hand_edits"] == []
    rendered, variables = role_files(inventory, layout, files)
    changed = [f for f in ("cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm8-server.options")
               if f in rendered and rendered[f] != files[f]]
    assert "cassandra-env.sh" in changed
    assert cassandra_apply_config_no_effect(changed, files, rendered, cmdline, "41x", "11", {})[0] == "the same JVM arguments"
    assert dump_path(launch(rendered, "11", tmp_path)) == dump_path(cmdline)


def test_apply_config_restarts_for_a_heap_dump_dir_the_environment_gives(tmp_path):
    """The unit exports another dir: the node's plain line overrides it (the JVM's environment shows the line's dir),
    the role's would not: a restart."""
    files = node_files("41x", HEAPDUMP)
    unit = {"CASSANDRA_HEAPDUMP_DIR": "/srv/other"}
    cmdline = launch(files, "11", tmp_path, environment=unit)
    new = dict(files, **{"cassandra-env.sh": heap_dump_dir_of_role("41x", "/srv/zorglub/dumps")})
    assert dump_path(launch(new, "11", tmp_path, environment=unit)) != dump_path(cmdline)
    environ = dict(unit, CASSANDRA_HEAPDUMP_DIR="/srv/zorglub/dumps")  # (unit's, then the JVM's: after the line)
    assert cassandra_apply_config_no_effect(["cassandra-env.sh"], files, new, cmdline, "41x", "11", environ) == []


def test_apply_config_restarts_for_an_exported_heap_dump_dir_against_another(tmp_path):
    """The node exports its dir, the role writes another: a restart (as when the role's dir differs, nothing exported)."""
    exported = node_files("41x", after_line("# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR",
                                            "export CASSANDRA_HEAPDUMP_DIR=/srv/zorglub/dumps"))
    plain = node_files("41x", HEAPDUMP)
    for files, environ in ((exported, {"CASSANDRA_HEAPDUMP_DIR": "/srv/zorglub/dumps"}), (plain, {})):
        cmdline = launch(files, "11", tmp_path)
        new = dict(files, **{"cassandra-env.sh": heap_dump_dir_of_role("41x", "/srv/zorglub/other")})
        assert cassandra_apply_config_no_effect(["cassandra-env.sh"], files, new, cmdline, "41x", "11", environ) == []


# --- an option set again identically: the last one counts ---------------------------------------------------------

AUTHENTICATE = '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.authenticate=true"\n'


def authenticate_twice(again):
    """(2026-11-18) the JMX lines in the remote branch, its authenticate line written twice in a row (again: the
    second one's text)."""
    def change(files):
        jmx_in_remote_branch(files)
        files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], AUTHENTICATE, AUTHENTICATE + again)
    return change


@pytest.mark.parametrize("series", sorted(JAVA))
def test_an_option_repeated_identically_in_the_remote_branch_is_the_roles(tmp_path, series):
    files = node_files(series, authenticate_twice(AUTHENTICATE))
    cmdline = launch(files, JAVA[series], tmp_path)
    inventory, layout, node = imported(files, series, JAVA[series], cmdline)
    rendered = role_files(inventory, layout, files)[0]
    assert node["hand_edits"] == []
    assert check(files, series, java=JAVA[series], cmdline=cmdline)["differences"] == []
    assert settings(launch(rendered, JAVA[series], tmp_path)) == settings(cmdline)


@pytest.mark.parametrize("again", [
    AUTHENTICATE.replace("=true", "=false"),  # (another value: the last one, false, counts)
    '  JVM_OPTS="$JVM_OPTS -javaagent:/srv/zorglub/agent.jar"\n' * 2,  # (agents add up)
    '  JVM_OPTS="$JVM_OPTS --add-opens java.base/java.io=ALL-UNNAMED"\n' * 2,
])
def test_an_option_repeated_otherwise_or_adding_up_stays_reported(tmp_path, again):
    files = node_files("41x", authenticate_twice(again))
    cmdline = launch(files, "11", tmp_path)
    inventory, layout, node = imported(files, "41x", "11", cmdline)
    assert node["hand_edits"] != []
    assert check(files, "41x", java="11", cmdline=cmdline)["differences"] != []
