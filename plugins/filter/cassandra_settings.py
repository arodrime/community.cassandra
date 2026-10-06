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
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import xml.etree.ElementTree as ET

import yaml

NULLS = ("", "~", "null", "Null", "NULL")
BOOLS = {"true": "true", "yes": "true", "on": "true", "false": "false", "no": "false", "off": "false"}


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


def yaml_settings(text, storage_dir=""):
    """cassandra.yaml: {dotted path: value}."""
    node = yaml.compose(text, Loader=yaml.BaseLoader)
    data = _plain(node) if node is not None else {}
    if not isinstance(data, dict):
        raise yaml.YAMLError("not a mapping")
    for key, sub in STORAGE_DIRS.items():
        if storage_dir and data.get(key) is None:
            path = "%s/%s" % (storage_dir.rstrip("/"), sub)
            data[key] = [path] if key == "data_file_directories" else path
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


# the -XX options that are -X ones under another name
JVM_ALIASES = {"MaxHeapSize": "-Xmx", "InitialHeapSize": "-Xms", "NewSize": "-Xmn", "ThreadStackSize": "-Xss"}
# HotSpot options that add up when given twice (the others: the last one counts)
JVM_LISTS = ("OnError", "OnOutOfMemoryError", "CompileCommand", "CompileOnly", "StartFlightRecording")
# options the ones they unlock must come after
JVM_UNLOCK = ("UnlockDiagnosticVMOptions", "UnlockExperimentalVMOptions", "UnlockCommercialFeatures")
# what cassandra-env.sh looks for in the options (grep), whatever comes after
JVM_ENV_GREPS = ("Xmn", "Xmx", "Xms", "UseConcMarkSweepGC", "+UseG1GC", "-Xlog:gc", "-Xloggc", "MaxDirectMemorySize",
                 "ParallelGCThreads", "ConcGCThreads")


def _jvm_name(option):
    m = re.match(r"^-XX:[+-]?(\w+)", option)
    if m:
        return JVM_ALIASES.get(m.group(1), m.group(1))
    m = re.match(r"^(-D[^=]+)", option) or re.match(r"^(-X(?:mx|ms|mn|ss))", option)
    return m.group(1) if m else None


def jvm_options_settings(text):
    """The options bin/cassandra passes on: every word of the lines starting
    with '-' (split on blanks and tabs, as the shell does; an option starting
    with -- and the word after it are one option), as the JVM takes them: by
    name, in any order, the last one counting (-X and -XX spellings of one
    setting under one name), but for the ones that add up; the options
    without a name (an agent, -ea) in their order; the -XX ones set before an
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
        out["(options without a name, in order)"] = unnamed
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


def xml_settings(text):
    """Every element, with its attributes and text, comments out."""
    out = []

    def walk(el, path):
        # logback reads a level in any case
        attrs = " ".join('%s="%s"' % (k, v.upper() if k == "level" else v) for k, v in sorted(el.attrib.items()))
        here = "%s/%s%s" % (path, el.tag, ("[%s]" % attrs) if attrs else "")
        out.append(here + ((" = " + el.text.strip()) if (el.text or "").strip() else ""))
        for child in el:
            walk(child, here)
    walk(ET.fromstring(text), "")
    return out


def same_settings(name, live, new, storage_dir=""):
    """The same settings in both texts of the file `name` (see the module)."""
    if live == new:
        return True
    try:
        if name.endswith(".yaml"):
            a, b = yaml_settings(live, storage_dir), yaml_settings(new, storage_dir)
            return set(a) == set(b) and all(same_yaml_value(a[k], b[k], k) for k in a)
        if name.endswith(".properties"):
            return properties_settings(live) == properties_settings(new)
        if name.endswith(".options"):
            return jvm_options_settings(live) == jvm_options_settings(new)
        if name.endswith(".xml"):
            return xml_settings(live) == xml_settings(new)
        if name.endswith(".sh"):
            return shell_lines(live, strict=True) == shell_lines(new, strict=True)
    except (yaml.YAMLError, ET.ParseError):
        return False
    return False


def cassandra_same_settings(live, new, name, storage_dir=""):
    """Filter: see the module. No live file (None or ''): False."""
    if not live:
        return False
    return same_settings(str(name), str(live), str(new), str(storage_dir or ""))


class FilterModule(object):
    def filters(self):
        return {"cassandra_same_settings": cassandra_same_settings}
