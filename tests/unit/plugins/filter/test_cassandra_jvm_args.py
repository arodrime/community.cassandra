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
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4, cassandra_config_import, cassandra_inventory_layout)

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
    # Java 17 reads no jvm11-server.options: its -Xmx/-Xms are not imported, that file is left as it is on the
    # node; the heap at the top of cassandra-env.sh is moved
    files = node_files("50x", both(on_top("MAX_HEAP_SIZE=12G"), add_to("jvm11-server.options", "-Xmx4G\n-Xms4G")))
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["moved"] and all(e.startswith(("jvm11-server.options", "  + -X")) for e in node["hand_edits"])
    rendered, variables = role_files(inventory, layout, files)
    assert variables.get("cassandra_heap_size") == "12G" and "cassandra_jvm11_extra_options" not in variables
    assert variables.get("cassandra_config_keep_files") == ["jvm11-server.options"]
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
    # (re-audit, 2026-11-14) a stock line of the file removed too: the GC read all the same, that line the only
    # difference (custom was dropped then: 8 differences)
    def edit(files):
        text = files["jvm17-server.options"].replace("-Djdk.attach.allowAttachSelf=true\n", "")
        files["jvm17-server.options"] = "\n".join(line for line in text.split("\n") if line not in removed)
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    assert [line for line in node["hand_edits"] if line.startswith("  ")] == ["  - -Djdk.attach.allowAttachSelf=true"]
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
    assert [line for line in lines if "jvm-server.options, jvm17-server.options rewritten: the heap moves to"
            " jvm-server.options, the same JVM arguments, nothing else" in line]
    assert not [line for line in lines if "see HAND EDITS" in line]
    # (audit r5) the heap already in jvm-server.options, a JVM_OPTS line moved: it does not move
    files = node_files("50x", both(add_to("jvm-server.options", "-Xms16G\n-Xmx16G"),
                                   before_extra('JVM_OPTS="$JVM_OPTS -XX:+AlwaysPreTouch"')))
    nodes = [dict(imported(files, "50x", "17", launch(files, "17", tmp_path))[2], name="n1", address="10.0.0.1")]
    assert nodes[0]["vars"]["cassandra_heap_in_jvm_options"] is True
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "rewritten: the same JVM arguments, nothing else" in line]
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
    (("-Dchronicle.analytics.disable=true\n", ""), False, True),  # the stock comment there: removed by hand
    ((OLDER_CHRONICLE, ""), True, False),
    (("-Dchronicle.analytics.disable=true", "#-Dchronicle.analytics.disable=true"), False, True),
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
    # a stock line removed from jvm-server.options (said as a hand edit, the role puts it back): the JVM_OPTS line
    # of cassandra-env.sh is moved all the same, the self-check says that line only
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "")
    cmdline = launch(files, "17", tmp_path)
    inventory, layout, node = imported(files, "50x", "17", cmdline)
    assert node["vars"]["cassandra_jvm17_extra_options"] == ["-Dfoo=1"]
    assert [line for line in node["hand_edits"] if line.startswith("  ")] == ["  - -XX:+UseNUMA"]
    out = check(files, "50x", java="17", cmdline=cmdline)
    assert out["differences"] == ["jvm-server.options: UseNUMA: node has nothing, import would write '-XX:+UseNUMA'"]
    assert [n for n in out["notes"] if n.endswith("but for UseNUMA, above")]
    # the hand edit in the file the move goes to: not moved
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    files["jvm17-server.options"] = files["jvm17-server.options"].replace("-Djdk.attach.allowAttachSelf=true\n", "")
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
    # the report's way out for a stock line removed: jvm-server.options kept, with cassandra-env.sh (whose lines were
    # moved: a JVM file kept, nothing is moved), and the self-check passes then
    def edit(files):
        older_release(files)
        files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "")
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    node = imported(files, "50x", "17", cmdline)[2]
    nodes = [dict(node, name="n1", address="10.0.0.1")]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "-e import_cluster_keep_hand_edits=cassandra-env.sh,jvm-server.options" in line]
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
        files["jvm-server.options"] = files["jvm-server.options"].replace("-XX:+UseNUMA\n", "")
    files = node_files("50x", edit)
    cmdline = launch(files, "17", tmp_path)
    nodes = [dict(imported(files, "50x", "17", cmdline)[2], name="n1", address="10.0.0.1")]
    lines = cassandra_import_report(cassandra_inventory_layout(nodes, "c"), ["c.yml", "."], "report.txt", {}, True,
                                    width=400, in_git=False)
    assert [line for line in lines if "-e import_cluster_keep_hand_edits=cassandra-env.sh,jvm-server.options" in line]
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
    from .test_cassandra_import_logback import audit_on
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
    out = cassandra_config_import(files, "50x", FACTS, keep_unread_jvm_options=True, jvm={
        "cmdline": cmdline, "java": "17", "environment": {}, "newer": []})
    assert out["hand_edits"] == [] and out["keep_files"] == []
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
    assert u"\u2192 expect cassandra-env.sh, jvm17-server.options rewritten: the same JVM arguments, nothing" \
        u" else" in " ".join(x.strip() for x in lines[next(i for i, line in enumerate(lines) if line.startswith("==== WHAT TO DO NEXT =")):])


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
def test_no_move_in_files_import_cluster_keep_hand_edits_keeps(tmp_path, keep):
    # the files kept as they are: not rewritten, the hand edits left to import_cluster_keep_hand_edits
    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    cmdline = launch(files, "17", tmp_path)
    dummy, dummy2, node = imported(files, "50x", "17", cmdline, keep=keep)
    assert node["moved"] == [] and node["rewritten"] == [] and node["not_moved"] == ""
    if keep != "jvm17-server.options":
        assert [e for e in node["hand_edits"] if e.startswith("cassandra-env.sh")]
    # another file kept: the move goes on
    dummy, dummy2, node = imported(files, "50x", "17", cmdline, keep="logback.xml")
    assert node["moved"] and node["rewritten"]


def test_a_wrong_name_of_import_cluster_keep_hand_edits_said(tmp_path):
    from ansible.errors import AnsibleFilterError
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_import_error

    files = node_files("50x", before_extra('JVM_OPTS="$JVM_OPTS -Dfoo=1"'))
    with pytest.raises(AnsibleFilterError) as raised:
        imported(files, "50x", "17", launch(files, "17", tmp_path), keep="logbak.xml")
    # the import's no_log tasks show it: the user's own value
    assert cassandra_import_error({"failed": True, "msg": "cassandra_config_import: %s" % raised.value}) == (
        "import_cluster_keep_hand_edits: logbak.xml: not a config file of cassandra_config (true, false, or among"
        " cassandra.yaml, cassandra-env.sh, jvm*-server.options, cassandra-rackdc.properties, logback.xml, jmxremote.password,"
        " jmxremote.access)")


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
