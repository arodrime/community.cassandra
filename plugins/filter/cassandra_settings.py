# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The settings of the files cassandra_config writes, read as the program
that reads each one does: one "same settings" test for cassandra_config (a
file with the same settings as the role's is left as it is on a running
node) and the import_cluster self-check (which must not pass a file the role
would rewrite).

cassandra_same_settings: the text of a live file, the role's, the file name
    (its type: .yaml, .sh, .options, .properties, .xml) and the directory of
    -Dcassandra.storagedir (where the directories cassandra.yaml leaves out
    are) -> true when Cassandra, bash, the JVM and logback get the same
    settings from both.
cassandra_yaml_rejected: top-level cassandra.yaml keys, a Cassandra version
    -> the ones it refuses to start with (not one of its properties).
cassandra_config_header: the text of a live file left as it is (same
    settings), the role's, the file name -> the role's header line to add to
    it ("Managed by Ansible..."), "" when it has one, the role's file has none,
    or the file with it would not have the same settings.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import importlib.util
import os
import re
import xml.etree.ElementTree as ET

import yaml

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_yaml_properties as known
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

NULLS = ("", "~", "null", "Null", "NULL")
BOOLS = {"true": "true", "yes": "true", "on": "true", "false": "false", "no": "false", "off": "false"}
# The units Cassandra 4.1+ reads a size, a rate or a duration in (DataStorageSpec, DataRateSpec, DurationSpec),
# in the smallest of each
UNITS = {"B": ("size", 1), "KiB": ("size", 1024), "MiB": ("size", 1024 ** 2), "GiB": ("size", 1024 ** 3),
         "B/s": ("rate", 1), "KiB/s": ("rate", 1024), "MiB/s": ("rate", 1024 ** 2),
         "ns": ("duration", 1), "us": ("duration", 10 ** 3), "\u00b5s": ("duration", 10 ** 3),
         "ms": ("duration", 10 ** 6), "s": ("duration", 10 ** 9), "m": ("duration", 60 * 10 ** 9),
         "h": ("duration", 3600 * 10 ** 9), "d": ("duration", 86400 * 10 ** 9)}
QUANTITY = re.compile(r"^(\d+)(%s)$" % "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True)))
# The cassandra.yaml settings (4.1+) of cassandra_config's variables that Cassandra reads in any of these units: the
# same amount is the same setting, and import_cluster writes it in the unit of the stock comment (else in the
# largest one that keeps it whole)
QUANTITY_KEYS = {
    "file_cache_size": "MiB", "memtable_heap_space": "MiB", "memtable_offheap_space": "MiB", "column_index_size": "KiB",
    "stream_throughput_outbound": "MiB/s", "inter_dc_stream_throughput_outbound": "MiB/s",
    "entire_sstable_stream_throughput_outbound": "MiB/s", "entire_sstable_inter_dc_stream_throughput_outbound": "MiB/s",
    "internode_streaming_tcp_user_timeout": "ms", "roles_update_interval": "ms", "permissions_update_interval": "ms",
    "credentials_update_interval": "ms", "partition_size_warn_threshold": "MiB",
    "partition_size_fail_threshold": "MiB", "gc_log_threshold": "ms", "gc_warn_threshold": "ms",
    "native_transport_max_frame_size": "MiB", "auto_snapshot_ttl": "d",
    # (and the ones 4.0 wrote under a former name, _cassandra_config_old_names)
    "permissions_validity": "ms", "roles_validity": "ms", "credentials_validity": "ms", "max_hint_window": "h",
    "request_timeout": "ms", "read_request_timeout": "ms", "range_request_timeout": "ms", "write_request_timeout": "ms",
    "counter_write_request_timeout": "ms", "cas_contention_timeout": "ms", "truncate_request_timeout": "ms",
    "slow_query_log_timeout": "ms", "column_index_cache_size": "KiB", "batch_size_warn_threshold": "KiB",
    "batch_size_fail_threshold": "KiB", "compaction_throughput": "MiB/s", "commitlog_sync_group_window": "ms",
    "commitlog_sync_period": "ms", "commitlog_segment_size": "MiB", "dynamic_snitch_update_interval": "ms",
    "dynamic_snitch_reset_interval": "ms", "hinted_handoff_throttle": "KiB", "batchlog_replay_throttle": "KiB",
    "hints_flush_period": "ms", "max_hints_file_size": "MiB", "trickle_fsync_interval": "KiB",
    "sstable_preemptive_open_interval": "MiB", "key_cache_size": "MiB", "row_cache_size": "MiB",
    "counter_cache_size": "MiB", "index_summary_capacity": "MiB", "index_summary_resize_interval": "m",
    "trace_type_query_ttl": "d", "trace_type_repair_ttl": "d", "prepared_statements_cache_size": "MiB",
    "compaction_large_partition_warning_threshold": "MiB"}
# Top-level settings the role leaves out at Cassandra's default, which a node may write: {key: that value}
DEFAULT_WHEN_ABSENT = {"auto_bootstrap": "true"}
# Keys a later series' stock cassandra.yaml added, which the role writes, that Cassandra reads the same when absent (its
# Config.java default, or the class it takes when null): {key: its settings, as yaml_settings reads them, at that
# value}. A node's file without the key (one of an older series) and the role's with these settings: the same settings.
# Not crypto_provider: 5.0 takes JREProvider when absent, its stock file DefaultCryptoProvider (Amazon Corretto)
STOCK_WHEN_ABSENT = {
    # 4.1 (Config.java's defaults)
    "auto_hints_cleanup_enabled": {"auto_hints_cleanup_enabled": "false"},
    "uuid_sstable_identifiers_enabled": {"uuid_sstable_identifiers_enabled": "false"},
    "compaction_tombstone_warning_threshold": {"compaction_tombstone_warning_threshold": "100000"},  # (4.1's role only)
    # 5.0: AuthConfig's AllowAllCIDRAuthorizer when null; Config.java's default; the default memtable when null
    # (SkipListMemtable: the stock "default" inherits it); DatabaseDescriptor's CASSANDRA_4 when null
    "cidr_authorizer": {"cidr_authorizer.class_name": "AllowAllCIDRAuthorizer"},
    "commitlog_disk_access_mode": {"commitlog_disk_access_mode": "legacy"},
    "memtable": {"memtable.configurations.skiplist.class_name": "SkipListMemtable",
                 "memtable.configurations.trie.class_name": "TrieMemtable",
                 "memtable.configurations.default.inherits": "skiplist"},
    "storage_compatibility_mode": {"storage_compatibility_mode": "CASSANDRA_4"},
}
# Settings that set nothing on the nodes compared (with the role's cassandra-env.sh), left out of the comparisons
# when the role's file has a line for them (5.0's, not 4.x's, which Cassandra refuses): {key: why}
NOT_COMPARED = {"heap_dump_path": "the -XX:HeapDumpPath cassandra-env.sh gives wins"}


# The unit of the number a former name holds (Converters of Config.java's @Replaces), by the end of the name
# (tracetype_query_ttl, tracetype_repair_ttl: seconds); a name without one (enable_x for x_enabled): its value as it is
FORMER_UNITS = (("_megabits_per_sec", "megabits"), ("_mb_per_sec", "MiB/s"), ("_in_mb", "MiB"), ("_mb", "MiB"),
                ("_in_kb", "KiB"), ("_in_ms", "ms"), ("_in_minutes", "m"), ("_ttl", "s"))
# The former names Cassandra reads a negative number of as no value (null; Converters' MILLIS_CUSTOM_DURATION and
# MINUTES_CUSTOM_DURATION: -1; NEGATIVE_MEBIBYTES_DATA_STORAGE_INT: any): {name: the negative numbers, None for any}
FORMER_NULL_WHEN_NEGATIVE = {"permissions_update_interval_in_ms": ("-1",), "roles_update_interval_in_ms": ("-1",),
                             "credentials_update_interval_in_ms": ("-1",),
                             "index_summary_resize_interval_in_minutes": ("-1",),
                             "sstable_preemptive_open_interval_in_mb": None}


# 4.1+: the former names (4.0's) of a section's sub-keys the role writes (EncryptionOptions' @Replaces, its only ones):
# {section: {former name: its 4.1+ key}}; Cassandra reads either, and refuses both
FORMER_SUB_KEYS = {"server_encryption_options": {"enable_legacy_ssl_storage_port": "legacy_ssl_storage_port_enabled"}}


@functools.lru_cache(maxsize=None)
def former_names(series):
    """4.1+: {former (4.0) name: the variable of its key} of the cassandra.yaml keys cassandra_config's variables
    write in that series (4.1, 5.0; the role's _cassandra_config_old_names): Cassandra reads either name, and
    refuses both. 4.0: none."""
    if series == "4.0":
        return {}
    role_vars = role_file("cassandra_config", "vars")
    return dict(role_vars["_cassandra_config_old_names"], **role_vars["_cassandra_config_old_names_of"].get(series, {}))


@functools.lru_cache(maxsize=None)
def former_keys():
    """(former name, its 4.1+ key) of every series."""
    return tuple(sorted(set((old, var[len("cassandra_"):]) for series in ("4.1", "5.0")
                            for old, var in former_names(series).items())))


def former_value(name, value):
    """The value of the former name `name` written for its 4.1+ key, as Cassandra converts it (512 for
    file_cache_size_in_mb: 512MiB; megabits per second: bytes per second), else None (not a whole number, or
    negative: none, or -1 for "not set"). No value (null), or a negative number Cassandra reads as null
    (FORMER_NULL_WHEN_NEGATIVE): "" (the variable unset: no value either). A former name without a unit (5.0's
    compaction_large_partition_warning_threshold, enable_user_defined_functions): its value as it is."""
    if value is None or value == "":
        return ""  # (Cassandra converts null to null)
    if name in FORMER_NULL_WHEN_NEGATIVE and re.match(r"^-\d+$", str(value)) and \
            str(value) in (FORMER_NULL_WHEN_NEGATIVE[name] or (str(value),)):
        return ""
    unit = next((u for end, u in FORMER_UNITS if str(name).endswith(end)), None)
    if unit is None:
        return value
    if isinstance(value, bool) or not re.match(r"^\d+$", str(value)):
        return None
    if unit == "megabits":
        return "%dB/s" % (int(value) * 125000)
    return "%d%s" % (int(value), unit)


def stock_when_absent(key, settings):
    """True when settings (yaml_settings') set the key of STOCK_WHEN_ABSENT at the value Cassandra takes without it
    (a boolean in any case)."""
    want = STOCK_WHEN_ABSENT[key]
    mine = dict((k, v) for k, v in settings.items() if re.split(r"[.\[]", k, maxsplit=1)[0] == key)
    return set(mine) == set(want) and all(
        str(v) == want[k] or isinstance(v, Plain) and BOOLS.get(v.lower(), v) == want[k] for k, v in mine.items())


def compared(settings, role_text):
    """cassandra.yaml's settings (yaml_settings') without the NOT_COMPARED ones the role's text has a line for, nor the
    keys of STOCK_WHEN_ABSENT the role's text writes when at the value Cassandra takes without them, and with a former
    name the role's text writes under its 4.1+ name (Cassandra reads either) under that name (none when Cassandra
    reads it as null: as no key)."""
    gone = [k for k in NOT_COMPARED if re.search(r"(?m)^#?[ \t]*%s:" % k, role_text)]
    gone += [k for k in STOCK_WHEN_ABSENT if re.search(r"(?m)^%s:" % k, role_text) and stock_when_absent(k, settings)]
    out = dict((k, v) for k, v in settings.items() if re.split(r"[.\[]", k, maxsplit=1)[0] not in gone)
    for section, names in FORMER_SUB_KEYS.items():
        for old, new in names.items():
            old, new = "%s.%s" % (section, old), "%s.%s" % (section, new)
            if old in out and new not in out and re.search(r"(?m)^\s+%s:" % new.split(".")[1], role_text):
                out[new] = out.pop(old)
    for old, new in former_keys():
        if old in out and new not in out and re.search(r"(?m)^%s:" % new, role_text) \
                and former_value(old, out[old]) is not None:
            value = former_value(old, out.pop(old))
            if value != "":
                out[new] = value
    return out


def quantity(text):
    """(kind, amount in the smallest unit) of a size, rate or duration as Cassandra 4.1+ reads it, else None."""
    m = QUANTITY.match(str(text)) if isinstance(text, str) else None
    return (UNITS[m.group(2)][0], int(m.group(1)) * UNITS[m.group(2)][1]) if m else None


def canonical_quantity(key, text):
    """text of the setting key in its canonical unit (QUANTITY_KEYS), or as it is (another key, no unit)."""
    found = quantity(text)
    if key not in QUANTITY_KEYS or found is None:
        return text
    kind, amount = found
    wanted = QUANTITY_KEYS[key]
    if amount % UNITS[wanted][1] == 0:
        return "%d%s" % (amount // UNITS[wanted][1], wanted)
    unit = max((u for u, (k, f) in UNITS.items() if k == kind and amount % f == 0 and u != "\u00b5s"),
               key=lambda u: UNITS[u][1])
    return "%d%s" % (amount // UNITS[unit][1], unit)


class Plain(str):
    """An unquoted yes/no/on/off/true/false: a boolean for a boolean setting,
    text for a text one (a password Yes is not true)."""


def _plain(node):
    """A YAML node as Cassandra's SnakeYAML reads it into its typed settings:
    each scalar its text, quoted or not ('8' and 8); a plain (unquoted) one
    true/false in any YAML 1.1 way, or null; a quoted one text ("yes" is not
    true); not a number PyYAML would make of it (a password 0123 is not 83).
    A key set twice: the last one."""
    if isinstance(node, yaml.MappingNode):
        return dict((_plain(k), _plain(v)) for k, v in node.value)
    if isinstance(node, yaml.SequenceNode):
        return [_plain(v) for v in node.value]
    if node.style is None:
        if node.value in NULLS:
            return None
        return Plain(node.value) if node.value.lower() in BOOLS else node.value
    return node.value


def same_yaml_value(a, b, path=""):
    """The same setting at that path: the same text, or a boolean written two
    ways, unquoted, one of them the role's own true/false (the role writes a
    boolean setting so): true/false in any case (Boolean.parseBoolean reads
    them alike), and yes/no/on/off, which SnakeYAML makes a boolean for a
    boolean setting, but not in a map of parameters (text, which
    parseBoolean reads false)."""
    if a == b:
        return True
    if path in QUANTITY_KEYS and quantity(a) is not None and quantity(a) == quantity(b):
        return True  # 200MiB/s, 209715200B/s
    if not (isinstance(a, Plain) and isinstance(b, Plain) and (a in ("true", "false") or b in ("true", "false"))):
        return False
    if re.search(r"(^|\.)parameters(\[|\.|$)", path):
        return a.lower() == b.lower()
    return BOOLS[a.lower()] == BOOLS[b.lower()]


def _flatten(value, path, out):
    if isinstance(value, dict):
        if not value:
            out[path] = "{}"
        for k, v in value.items():
            _flatten(v, "%s.%s" % (path, k) if path else str(k), out)
    elif isinstance(value, list):
        if not value:
            out[path] = "[]"
        for i, v in enumerate(value):
            _flatten(v, "%s[%d]" % (path, i), out)
    elif value is not None:  # key: (null) is the same as no key: the default
        out[path] = value
    return out


# cassandra.yaml directories left out: under -Dcassandra.storagedir (bin/cassandra, cassandra.in.sh)
STORAGE_DIRS = {"data_file_directories": "data", "commitlog_directory": "commitlog",
                "saved_caches_directory": "saved_caches", "hints_directory": "hints", "cdc_raw_directory": "cdc_raw"}


@functools.lru_cache(maxsize=8)
def _compose(text):
    """The YAML node of text, of the last ones read: the import's self-check reads each cassandra.yaml twice (its
    settings, then whether they are the same), about a second each in pure-Python YAML. Read only (_plain makes
    new values)."""
    return yaml.compose(text, Loader=yaml.BaseLoader)


def yaml_settings(text, storage_dir=""):
    """cassandra.yaml: {dotted path: value}."""
    node = _compose(text)
    data = _plain(node) if node is not None else {}
    if not isinstance(data, dict):
        raise yaml.YAMLError("not a mapping")
    for key, sub in STORAGE_DIRS.items():
        if storage_dir and data.get(key) is None:
            path = "%s/%s" % (storage_dir.rstrip("/"), sub)
            data[key] = [path] if key == "data_file_directories" else path
    for key, value in DEFAULT_WHEN_ABSENT.items():
        if isinstance(data.get(key), str) and BOOLS.get(data[key].lower()) == value:
            del data[key]  # (auto_bootstrap: true, the default, quoted or not; false stays: a node it would join without data)
    out = dict((k, v) for k, v in _flatten(data, "", {}).items() if k)
    for key, value in out.items():
        if re.match(r"^seed_provider\[\d+\]\.parameters\[\d+\]\.seeds$", key) and isinstance(value, str):
            # SimpleSeedProvider: split on commas, each address trimmed, empty ones skipped
            out[key] = ",".join(s.strip() for s in value.split(",") if s.strip())
    return out


def properties_settings(text):
    """cassandra-rackdc.properties as Java's Properties reads it (key=value,
    key:value or key value; # and ! comment lines, which never go on to the
    next line; another line ending with an odd number of backslashes does; the
    value keeps its trailing spaces) and the snitch uses it: dc and rack
    trimmed, prefer_local true in any case (false: as when not set)."""
    lines, current = [], None
    for line in re.sub(r"\r\n?", "\n", text).split("\n"):  # Java ends a line at \r\n, \r or \n
        line = line.lstrip(" \t\f")
        if current is None:
            if not line or line[0] in "#!":
                continue
            current = line
        else:
            current += line
        if (len(current) - len(current.rstrip("\\"))) % 2:
            current = current[:-1]
            continue
        lines.append(current)
        current = None
    if current is not None:
        lines.append(current)
    out = {}
    for line in lines:
        m = re.match(r"^((?:[^\\=: \t]|\\.)*)[ \t]*[=: \t]?[ \t]*(.*)$", line)
        out[m.group(1)] = m.group(2)
    for key in ("dc", "rack"):
        if key in out:
            out[key] = out[key].strip()
    if "prefer_local" in out:
        out["prefer_local"] = "true" if out["prefer_local"].lower() == "true" else "false"
        if out["prefer_local"] == "false":
            del out["prefer_local"]
    return out


# HotSpot options that add up when given twice (the others: the last one counts)
JVM_LISTS = jvm.LISTS
# options the ones they unlock must come after
JVM_UNLOCK = ("UnlockDiagnosticVMOptions", "UnlockExperimentalVMOptions", "UnlockCommercialFeatures")
# Stock lines a later release added (file: {line: (why, the stock comment line above it, None when it has none of
# its own)}): a node's file without one predates it, not a hand edit; the role adds it (said under WORTH A LOOK)
# (the stock comment line above it too: a file with it is of that release, its line removed or changed by hand)
NETTY = "# Revert changes in defaults introduced in https://netty.io/news/2022/03/10/4-1-75-Final.html"
LATER_STOCK = {
    "jvm-server.options": {"-Dchronicle.analytics.disable=true": (
        "Cassandra's default since CASSANDRA-19656", "# Disable chronicle analytics. See CASSANDRA-19656")},
    # 5.0's (4.x has -XX:-UseBiasedLocking in jvm-server.options, which a node upgraded keeps)
    "jvm11-server.options": {
        "-XX:-UseBiasedLocking": ("5.0's stock line for Java 11 (4.x's in jvm-server.options)",
                                  "# Disable biased locking as it does not benefit Cassandra."),
        "--add-exports java.base/jdk.internal.util=ALL-UNNAMED": ("5.0's stock line for Java 11", None),
        "-Dio.netty.allocator.useCacheForAllThreads=true": ("5.0's stock line for Java 11 (Netty's former default)",
                                                            NETTY),
        "-Dio.netty.allocator.maxOrder=11": ("5.0's stock line for Java 11 (Netty's former default)", NETTY)},
}


def _later_stock_named(line, text):
    """True when text names the option of a LATER_STOCK line: -XX:+Name or -XX:-Name for -XX:-Name, the part before
    = of any other."""
    m = re.match(r"^-XX:[+-]?(\w+)$", line)
    if m:
        return re.search(r"-XX:[+-]?%s\b" % m.group(1), text) is not None
    return line.split("=", 1)[0] in text


def without_later_stock(name, rendered, live):
    """rendered (a file's text) without the LATER_STOCK lines of name the node's text (live) has nothing of: no
    line naming its option, on or commented out, nor its stock comment -> (text, [those lines])."""
    lines = rendered.split("\n")
    text = str(live or "")
    missing = [line for line, (dummy, comment) in LATER_STOCK.get(name, {}).items() if line in lines
               and not _later_stock_named(line, text) and (comment is None or comment not in text)]
    return "\n".join(line for line in lines if line not in missing), missing


# what cassandra-env.sh looks for in the options (grep), whatever comes after
JVM_ENV_GREPS = ("Xmn", "Xmx", "Xms", "UseConcMarkSweepGC", "+UseG1GC", "-Xlog:gc", "-Xloggc", "MaxDirectMemorySize",
                 "ParallelGCThreads", "ConcGCThreads")


_jvm_name = jvm.option_name


def jvm_options_settings(text):
    """The options bin/cassandra passes on: every word of the lines starting
    with '-' (split on blanks and tabs, as the shell does; an option starting
    with -- and the word after it are one option), as the JVM takes them: by
    name, in any order, the last one counting (-X and -XX spellings of one
    setting under one name), but for the ones that add up; the options
    without a name (an agent, -ea) in their order, but the ones the JVM takes
    in any order (--add-opens...: jvm.unnamed_in_order); the -XX ones set before an
    unlock option; and what cassandra-env.sh looks for in them. A \\r (the JVM
    would get it): the text itself."""
    if "\r" in text:
        return {"(text)": [text]}
    options = []
    for line in text.split("\n"):
        if line.startswith("-"):
            for word in [w for w in re.split(r"[ \t]+", line) if w]:
                if options and not word.startswith("-") and options[-1].startswith("--") and "=" not in options[-1] \
                        and " " not in options[-1]:
                    options[-1] += " " + word
                else:
                    options.append(word)
    out, unnamed = {}, []
    for option in options:
        name = _jvm_name(option)
        if name is None:
            unnamed.append(option)
        elif name in JVM_LISTS:
            out.setdefault(name, []).append(option)
        else:
            out[name] = [option]
    if unnamed:
        out["(options without a name, in order)"] = jvm.unnamed_in_order(unnamed)
    for unlock in JVM_UNLOCK:
        if ("-XX:+%s" % unlock) in options:
            before = options[:options.index("-XX:+%s" % unlock)]
            out["(set before -XX:+%s)" % unlock] = sorted(set(
                _jvm_name(o) for o in before if o.startswith("-XX:") and _jvm_name(o)))
    greps = [g for g in JVM_ENV_GREPS if any(g in o for o in options)]
    if greps:
        out["(cassandra-env.sh finds)"] = greps
    return out


def shell_words(line):
    """The words of a shell line as the shell sees them: split on unquoted
    blanks, a comment only at the start of a word; each written back so that
    two words mean the same when equal: quotes around plain text dropped
    ("7199" is 7199), kept around what they change ('$X' is not "$X")."""
    words, word, i, quote, start = [], None, 0, None, 0
    while i < len(line):
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word += ("\\" + c) if not (c.isalnum() or c in "_-./:=,+@%") else c
        elif quote == '"':
            if c == "\\":
                i += 1  # an escaped character: part of the quoted text
            elif c == '"':
                quote = None
                seg = line[start:i]
                word += ('"%s"' % seg) if re.search(r"[$`\\]", seg) else "".join(
                    ("\\" + ch) if not (ch.isalnum() or ch in "_-./:=,+@%") else ch for ch in seg)
        elif c in " \t":
            if word is not None:
                words.append(word)
                word = None
        elif c == "#" and word is None:
            break
        elif c in "'\"":
            word = word or ""
            quote = c
            start = i + 1
        elif c == "\\" and i + 1 < len(line):
            word = (word or "") + line[i:i + 2]
            i += 1
        else:
            word = (word or "") + c
        i += 1
    if quote:
        raise ValueError("unclosed quote")
    if word is not None:
        words.append(word)
    return words


def shell_lines(text, strict=False):
    """The lines a shell runs, comments and blank lines out, as their words (a
    quote going on to the next line: that line's words as they are). strict:
    a file bash reads otherwise than line by line (a quote, a heredoc or a
    backslash going on to the next line) or with a \\r (bash keeps it in the
    value): its text, as one line."""
    if strict and ("\r" in text or "<<" in text):
        return [text]
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if strict and line.endswith("\\"):
            return [text]
        try:
            words = shell_words(line)
        except ValueError:
            if strict:
                return [text]
            words = line.split()
        if words:
            out.append(" ".join(words))
    return out


# appenders that do nothing unless an event reaches them, created and started or not (logback 1.2 creates and
# starts an appender no appender-ref names, 1.3+ skips it): Cassandra 5.0's system_views.system_logs one (the
# stock CQLLOG), its start() its base class's
INERT_APPENDERS = ("org.apache.cassandra.utils.logging.VirtualTableAppender",)


def _unreferenced_inert(conf):
    """The <appender> elements under <configuration> of an INERT_APPENDERS class that no appender-ref of the file
    names: no effect, whatever logback reads it."""
    if _logback_tag(conf) != "configuration":
        return []
    # (a file included, or a name or ref of a variable: what names what is not known from this file)
    if any(_logback_tag(e) == "include" or "${" in str(e.get("name", "")) + str(e.get("ref", "")) for e in conf.iter()):
        return []
    refs = set(e.get("ref") for e in conf.iter() if _logback_tag(e) == "appender-ref")
    return [c for c in conf if _logback_tag(c) == "appender" and c.get("name") not in refs
            and str(c.get("class", "")).strip() in INERT_APPENDERS]


def xml_settings(text):
    """Every element, with its attributes and text, comments out; logback.xml's in logback_order, without its
    appenders of no effect (_unreferenced_inert)."""
    out = []

    def walk(el, path):
        # logback reads a level in any case
        attrs = " ".join('%s="%s"' % (k, v.upper() if k == "level" else v) for k, v in sorted(el.attrib.items()))
        here = "%s/%s%s" % (path, el.tag, ("[%s]" % attrs) if attrs else "")
        out.append(here + ((" = " + el.text.strip()) if (el.text or "").strip() else ""))
        for child in logback_order(el, path == ""):
            walk(child, here)
    conf = ET.fromstring(text)
    for appender in _unreferenced_inert(conf):
        conf.remove(appender)
    walk(conf, "")
    return out


def _xml_key(el):
    """What tells an element from its siblings: its tag and name= (an appender, a logger), else its ref= (an
    appender-ref), else its tag alone (then its place among the siblings of that tag)."""
    for attr in ("name", "ref"):
        if el.get(attr) is not None:
            return (el.tag, attr, el.get(attr))
    return (el.tag, None, None)


def _xml_step(key, index, many):
    """An element's step in an XPath: appender[@name="AUDIT"], appender-ref[@ref="ASYNC"], filter, pattern[2] (its
    place among the siblings with the same key, when there are several)."""
    tag, attr, value = key
    return ('%s[@%s="%s"]' % (tag, attr, value) if attr else tag) + ("[%d]" % (index + 1) if many else "")


def xml_differences(node, role, stray=None):
    """Where two XML texts differ, element by element as xml_settings reads them (comments out, a level in any case),
    the siblings matched by name=, else ref=, else their place among those of their tag -> [(XPath, the node's,
    the role's)]: an attribute (/configuration/root/@level) or an element's text (.../rollingPolicy/fileNamePattern)
    as a str, an element one side has alone as that element (the other side None). [] when the elements are the
    same, in another order or not. stray, a list: the node's text in an element that takes none (it holds elements,
    the role's has no text there) goes in it instead, (XPath, the text)."""
    out = []

    def attrs(el):
        return dict((k, v.upper() if k == "level" else v) for k, v in el.attrib.items())

    def walk(a, b, path):
        own = _xml_key(a)[1]
        mine, theirs = attrs(a), attrs(b)
        out.extend(("%s/@%s" % (path, k), mine.get(k), theirs.get(k)) for k in sorted(set(mine) | set(theirs))
                   if k != own and mine.get(k) != theirs.get(k))
        if (a.text or "").strip() != (b.text or "").strip():
            if stray is not None and len(a) and len(b) and not (b.text or "").strip():
                stray.append((path, (a.text or "").strip()))
            else:
                out.append((path, (a.text or "").strip(), (b.text or "").strip()))
        keys = []
        for child in list(a) + list(b):
            if _xml_key(child) not in keys:
                keys.append(_xml_key(child))
        for key in keys:
            mine, theirs = [c for c in a if _xml_key(c) == key], [c for c in b if _xml_key(c) == key]
            count = max(len(mine), len(theirs))
            for i in range(count):
                here = "%s/%s" % (path, _xml_step(key, i, count > 1))
                if i < len(mine) and i < len(theirs):
                    walk(mine[i], theirs[i], here)
                else:
                    out.append((here, mine[i] if i < len(mine) else None, theirs[i] if i < len(theirs) else None))

    a, b = ET.fromstring(node), ET.fromstring(role)
    for conf in (a, b):  # (an appender of no effect, as xml_settings reads it: not a difference)
        for appender in _unreferenced_inert(conf):
            conf.remove(appender)
    if a.tag != b.tag:
        return [("/" + a.tag, a, None), ("/" + b.tag, None, b)]
    walk(a, b, "/" + a.tag)
    return out


def _logback_tag(el):
    """An element's name as logback reads it: any case, without its namespace."""
    return str(el.tag).rsplit("}", 1)[-1].lower()


def logback_order(el, top=False):
    """The children of an element in the order that matters to logback (1.2, Cassandra 4.x and 5.0): the
    <appender-ref> of a logger or of <root> by name (each adds its appender, whatever its place); under
    <configuration> (top), its <appender> by name, then its <logger> by name after the others, kept in order: logback
    reads an appender before the appender-refs naming it, a logger once; so when every appender-ref of the file
    comes after its appender, no appender nor logger is named twice and nothing is read in order (a property, an
    include). Else as they are."""
    children = list(el)
    tag = _logback_tag(el)
    if tag in ("root", "logger"):
        refs = sorted((c for c in children if _logback_tag(c) == "appender-ref"), key=lambda c: str(c.get("ref")))
        return [c for c in children if _logback_tag(c) != "appender-ref"] + refs
    if not (top and tag == "configuration"):
        return children
    kinds = [_logback_tag(c) for c in children]
    names = {"appender": set(), "logger": set()}
    for child, kind in zip(children, kinds):
        if kind not in ("appender", "logger", "root", "jmxconfigurator"):
            return children
        if kind == "logger" and str(child.get("name")).upper() == "ROOT":  # (the root again: the last one counts)
            return children
        refs = [e.get("ref") for e in child.iter() if _logback_tag(e) == "appender-ref"]
        if any(ref not in names["appender"] for ref in refs):
            return children
        if kind in names:
            if child.get("name") in names[kind]:
                return children
            names[kind].add(child.get("name"))
    others = [c for c, k in zip(children, kinds) if k not in ("appender", "logger")]
    return (sorted((c for c, k in zip(children, kinds) if k == "appender"), key=lambda c: str(c.get("name")))
            + others + sorted((c for c, k in zip(children, kinds) if k == "logger"), key=lambda c: str(c.get("name"))))


def same_settings(name, live, new, storage_dir=""):
    """The same settings in both texts of the file `name` (see the module)."""
    if live == new:
        return True
    try:
        if name.endswith(".yaml"):
            a, b = compared(yaml_settings(live, storage_dir), new), compared(yaml_settings(new, storage_dir), new)
            return set(a) == set(b) and all(same_yaml_value(a[k], b[k], k) for k in a)
        if name.endswith(".properties"):
            return properties_settings(live) == properties_settings(new)
        if name.endswith(".options"):
            return jvm_options_settings(live) == jvm_options_settings(new)
        if name.endswith(".xml"):
            return xml_settings(live) == xml_settings(new)
        if name.endswith(".sh"):
            return shell_lines(live, strict=True) == shell_lines(new, strict=True)
    except (yaml.YAMLError, ET.ParseError, RecursionError):
        return False
    return False


def cassandra_same_settings(live, new, name, storage_dir=""):
    """Filter: see the module. No live file (None or ''): False. "a: 1\n", "a: 1  # x\n",
    "cassandra.yaml" -> true."""
    if not live:
        return False
    return same_settings(str(name), str(live), str(new), str(storage_dir or ""))


def yaml_rejected(keys, version):
    """The keys among `keys` (top-level cassandra.yaml keys) Cassandra `version` refuses to start with ("Invalid
    yaml. Please remove properties [...]"), sorted. version: a release (4.1.5), else a series (4.1, 41x: the keys
    no release of it accepts). Nothing for a series not known, nor for a release newer than the lists (the
    properties it added are not known). A dotted key (sstable.selected_format: 4.1+ sets a nested property that
    way) is judged by its first part."""
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(version)) or re.match(r"^(\d)(\d)x$", str(version))
    series = "%s.%s" % m.group(1, 2) if m else None
    if series not in known.LATEST:
        return []
    # a pre-release (5.0.0-rc1): the series'
    patch = int(m.group(3)) if m.lastindex == 3 and not re.search(r"(?i)-(alpha|beta|rc)", str(version)) else None
    if patch is not None and patch > known.LATEST[series]:
        return []
    some = known.SOME.get(series, {})
    accepted = set(known.ALL[series]) | set(
        k for k, ranges in some.items() if patch is None or any(a <= patch <= b for a, b in ranges))
    return sorted(k for k in set(str(k) for k in keys) if k.split(".")[0] not in accepted)


def cassandra_yaml_rejected(keys, version):
    """Filter: see yaml_rejected; keys: a list, or a dict (its keys). ["fake_key", "num_tokens"], "4.1.5" -> ["fake_key"]."""
    return yaml_rejected(list(keys or []), version)


# cassandra_config's header line, and the node-side writer that adds it (files/config_header.py: where it goes)
HEADER_MARK = "Managed by Ansible (community.cassandra.cassandra_config)"
CONFIG_HEADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config", "files",
                             "config_header.py")


@functools.lru_cache(maxsize=None)
def _config_header():
    spec = importlib.util.spec_from_file_location("cassandra_config_header", CONFIG_HEADER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def header_line(new):
    """The role's header line in its rendered file new (its comment syntax: # or <!-- -->), "" when it has none
    (e.g. a template of your own)."""
    for line in str(new).splitlines():
        if HEADER_MARK in line:
            return line.rstrip("\r")
    return ""


def cassandra_config_header(live, new, name, storage_dir=""):
    """Filter: see the module. The header line goes where the node puts it (config_header.py); the file with it
    must have the same settings as the role's (its proof that the header changes nothing: no restart).
    "num_tokens: 16\n", "# Managed by Ansible (community.cassandra.cassandra_config): ...\nnum_tokens: 16\n",
    "cassandra.yaml" -> "# Managed by Ansible (community.cassandra.cassandra_config): ..."."""
    live, new = str(live or ""), str(new or "")
    line = header_line(new)
    if not live or not line or HEADER_MARK in live:
        return ""
    added = _config_header().with_header(live.encode("utf-8", "surrogateescape"), line.encode("utf-8"))
    if not same_settings(str(name), added.decode("utf-8", "surrogateescape"), new, str(storage_dir or "")):
        return ""
    return line


class FilterModule(object):
    def filters(self):
        return {"cassandra_same_settings": cassandra_same_settings,
                "cassandra_yaml_rejected": cassandra_yaml_rejected,
                "cassandra_config_header": cassandra_config_header}
