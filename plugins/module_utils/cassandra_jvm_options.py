# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""JVM options worth a warning, or a refusal before a Java change, and the
options files each Java reads.

bin/cassandra passes on the lines starting with '-' of jvm-server.options and
of one jvm<N>-server.options, picked by the running Java (cassandra.in.sh sets
JAVA_VERSION, cassandra-env.sh reads the file): 4.0 and 4.1 read
jvm8-server.options on Java 8 and jvm11-server.options on any later one, 5.0
jvm11-server.options on Java 11 and jvm17-server.options on 17 or later. The
others are not read at all.

CHECKS is the table of options looked at: the option (its name as the JVM
takes it: -Xss, or the Foo of -XX:+Foo / -XX:-Foo), when it is worth a warning
(warn) and when a move to a Java is refused with it (refuse), both given the
option and the Java major, and why. A check is one more entry.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
# the -XX spellings of the -X options, and the unit of their value (ThreadStackSize: KiB, a suffix too)
ALIASES = {"ThreadStackSize": ("-Xss", 1024), "MaxHeapSize": ("-Xmx", 1), "InitialHeapSize": ("-Xms", 1),
           "NewSize": ("-Xmn", 1)}
# HotSpot options that add up when given twice (the others: the last one counts)
LISTS = ("OnError", "OnOutOfMemoryError", "CompileCommand", "CompileOnly", "StartFlightRecording")
KIB, MIB = 1024, 1024 ** 2


def option_name(option):
    """The setting an option sets, as the JVM takes it: -Xss256k and
    -XX:ThreadStackSize=256 -> -Xss; -XX:+Foo, -XX:-Foo -> Foo; -Da=1 -> -Da;
    None for another word (-ea, -javaagent:...)."""
    m = re.match(r"^-XX:[+-]?(\w+)", option)
    if m:
        return ALIASES.get(m.group(1), (m.group(1),))[0]
    m = re.match(r"^(-D[^=]+)", option) or re.match(r"^(-X(?:mx|ms|mn|ss))", option)
    return m.group(1) if m else None


def size(option):
    """The bytes a size option gives (-Xss1024M; -XX:ThreadStackSize=1024 and
    =1m count in KiB), None when it gives no number."""
    m = re.match(r"^-XX:(\w+)=(\d+)([kKmMgGtT]?)$", option)
    if m and m.group(1) in ALIASES:
        return int(m.group(2)) * UNITS[m.group(3).lower()] * ALIASES[m.group(1)][1]
    m = re.match(r"^-X(?:mx|ms|mn|ss)(\d+)([kKmMgGtT]?)$", option)
    return int(m.group(1)) * UNITS[m.group(2).lower()] if m else None


def shown_size(value):
    for unit, name in ((1024 ** 4, "TiB"), (1024 ** 3, "GiB"), (MIB, "MiB"), (KIB, "KiB")):
        if value >= unit:
            return "%g %s" % (round(float(value) / unit, 1), name)
    return "%d bytes" % value


def _stack_why(option, dummy):
    stack = size(option)
    if stack > 8 * MIB:
        return ("%s of stack for every thread, far above the usual 256k to 1m (stock -Xss256k; M typed for k?):"
                " each of Cassandra's hundreds to thousands of threads reserves that much, new threads may fail"
                " (unable to create native thread)" % shown_size(stack))
    return ("%s of stack per thread, below the usual 256k to 1m (stock -Xss256k): the JVM may refuse to start, or"
            " threads fail with StackOverflowError" % shown_size(stack))


def _stack_odd(option, dummy):
    # 0: the platform's default
    return size(option) not in (None, 0) and not 160 * KIB <= size(option) <= 8 * MIB


def _biased_why(dummy, java):
    if java > 17:
        return "biased locking is gone after Java 17: the JVM does not start with it"
    return ("biased locking is deprecated on Java 17 (a warning at each start), and gone after it: the JVM does not"
            " start with it on Java 21")


def _from_java(major):
    return lambda dummy, java: java >= major


CHECKS = (
    {"option": "-Xss", "warn": _stack_odd, "refuse": None, "why": _stack_why},
    # deprecated in the JDK since 15, removed after 17: of the Javas Cassandra runs on, a warning at each start
    # on 17, and the JVM does not start with it on 21
    {"option": "UseBiasedLocking", "warn": _from_java(17), "refuse": _from_java(18), "why": _biased_why},
)


def java_major(java):
    """'17', 17, '17.0.2', '1.8.0_402' -> 17; None when not a version."""
    text = str(java).strip()
    if text.startswith("1."):
        text = text[2:]
    m = re.match(r"^(\d+)", text)
    return int(m.group(1)) if m else None


def options_file(series, java):
    """The jvm<N>-server.options a Java reads besides jvm-server.options
    (bin/cassandra's choice), '' when the Java or the series is not known."""
    major = java_major(java)
    if major is None or series not in ("40x", "41x", "50x"):
        return ""
    if series == "50x":
        return "jvm17-server.options" if major >= 17 else "jvm11-server.options"
    return "jvm11-server.options" if major >= 11 else "jvm8-server.options"


def options_variable(name):
    """The cassandra_config variable of a file's extra options."""
    m = re.match(r"^jvm(\d*)-server\.options$", name)
    return "cassandra_jvm%s_extra_options" % m.group(1) if m else ""


def read_files(series, java):
    """The options files a Java reads, in the order bin/cassandra passes them on."""
    dep = options_file(series, java)
    return ["jvm-server.options"] + ([dep] if dep else [])


def words(lines):
    """The options of option lines (several on a line, as bin/cassandra splits
    them); a string is one line."""
    out = []
    for line in [lines] if isinstance(lines, str) else lines or []:
        line = str(line)
        if line.startswith("-"):
            out += [w for w in re.split(r"[ \t]+", line) if w]
    return out


def check(options, java, refusals=False):
    """options: [(option, where, way out)] in the order the JVM gets them:
    where says what sets it, the way out what to do (a sentence); java: the
    Java major they are given to -> [message], each with why and the way out:
    the warnings, or, refusals true, what a move to that Java is refused with.
    Of an option given twice, the one that counts (the last)."""
    major = java_major(java)
    if major is None:
        return []
    last = dict((option_name(o), i) for i, (o, dummy, dummy2) in enumerate(options))
    out = []
    for i, (option, where, way_out) in enumerate(options):
        name = option_name(option)
        if name is None or (name not in LISTS and last[name] != i):
            continue
        for entry in CHECKS:
            test = entry["refuse"] if refusals else entry["warn"]
            if name != entry["option"] or test is None or not test(option, major):
                continue
            why = entry["why"](option, major)
            if refusals:
                out.append("%s (%s) refused for Java %d: %s. %s" % (option, where, major, why, way_out))
            else:
                out.append("%s (%s, Java %d): %s. %s" % (option, where, major, why, way_out))
    return out


def fix_it(variable):
    """The way out of a warning on an option a variable sets."""
    return "Fix or remove it in %s: it is kept as it is until then." % variable


def remove_it(variable):
    """The way out of a refusal on an option a variable sets."""
    return "Remove it from %s first, then run again." % variable
