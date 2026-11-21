# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output of the playbooks, written once: every function
returns text lines (or a string) that a debug task prints, nothing else.
Pure Python, no Ansible: the filters of plugins/filter/cassandra_output.py
and the other filters call it. Conventions in
docs/docsite/rst/guide_output.rst.

- sizes, rates, durations, clocks: size, amount, sizes, parse_size, rate, duration, clock,
  count, plural;
- node lists: nodes (node1..node5, node7), full_list; by groups on a large
  cluster: node_groups, who, group_lines; texts each once, ignoring case:
  unique_ignoring_case;
- a value as the output shows it: shown, secrets as **** (the rule and the
  masking of a text: plugin_utils/cassandra_secrets.py);
- a setting and its value per node: setting_lines, settings_lines (several,
  aligned, wrapped in a width: wrap_value, wrap_list), setting_rows, by_nodes;
  lines fit in a terminal's width: fit; laid out at the width where they are
  printed: flow, command_flow, laid_out (the help text);
- before a change: plan; during it: phase, progress_line, peer_lines; at the
  end: recap, perm_lines, diff_lines, changed_lines;
- what is left to do: command, extra_var, todo, inventory_steps,
  in_git_work_tree.

The texts of one operation each (the prepare of new nodes, the TO DO after a
removal, the seed rule) are in plugin_utils/cassandra_operation_texts.py.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import os
import re
import shlex
import textwrap
import time

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import MASK, hidden, mask_lines

ARROW = u"←"  # the marker of a line that needs a look: "<- down", "<- differs"
# A blank line between two paragraphs of one block: a single space (the
# default callback's yaml format strips it). An empty line "" between two
# blocks of a plan, a screen or a recap is a rule under the ops callback;
# this one stays blank there.
PARAGRAPH = " "
GIB = 1024.0 ** 3
_UNITS = (("TiB", 1024.0 ** 4), ("GiB", GIB), ("MiB", 1024.0 ** 2), ("KiB", 1024.0))

# --- sizes, rates, durations ---------------------------------------------------------------


def number(value, default=None):
    """A number a template hands over, as a number: before ansible-core 2.19 a variable set by a template (a
    task's vars, set_fact, -e) is text ("12", "12.5"; "" for none, AnsibleUnsafeText). An int, a float with a
    fraction; default for none (None, "", "None"). Text that is no number raises ValueError."""
    if value is None or isinstance(value, (int, float)):
        return default if value is None else value
    text = str(value).strip()
    if text in ("", "None"):
        return default
    figure = float(text)
    return int(figure) if figure.is_integer() else figure


def _unit(count):
    """The unit count reads at least 1 in: (name, factor), ("B", 1) under
    1 KiB; the larger one when the smaller would round up to 1024.0."""
    for unit, factor in _UNITS:
        if count >= factor or float("%.1f" % (count * 1024 / factor)) >= 1024.0:
            return unit, factor
    return "B", 1


def _figure(count, factor):
    """count in a unit of factor bytes, without the unit: "305.1", "0.0",
    "<0.1" for a non-zero count that would read 0.0, "512" in bytes."""
    if factor == 1:
        return "%d" % count
    text = "%.1f" % (count / factor)
    return "<0.1" if count > 0 and text == "0.0" else text


def size(count, scale=None):
    """count bytes, one decimal: "41.2 GiB", "260.6 MiB", "0 B", in the
    unit it reads at least 1.0 in; scale: the bytes whose unit to use
    instead (the larger size next to it). A non-zero count never reads
    0.0: "<0.1 GiB". A done/total pair goes through amount or sizes."""
    count = max(0.0, float(count or 0))
    unit, factor = _unit(float(scale) if scale else count)
    return "%s %s" % (_figure(count, factor), unit)


def sizes(part, whole):
    """part and whole bytes in whole's unit, as two strings: ("40.1 GiB",
    "98.0 GiB"); part is never shown above whole ("12.0 GiB free of 98.0 GiB")."""
    whole = float(whole or 0)
    return size(min(float(part or 0), whole), whole), size(whole)


def amount(done, total, sep="/"):
    """done out of total bytes, the unit once, the one total reads naturally
    in, the same precision for both: "305.1/329.8 MiB", "<0.1/100.0 GiB";
    done is never shown above total (netstats can count a little more)."""
    total = max(0.0, float(total or 0))
    unit, factor = _unit(total)
    return "%s%s%s %s" % (_figure(max(0.0, min(float(done or 0), total)), factor), sep, _figure(total, factor), unit)


_SIZE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(bytes|B|KiB|KB|MiB|MB|GiB|GB|TiB|TB)\s*$")
_FACTORS = {"bytes": 1, "B": 1, "KiB": 1024, "KB": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
            "GiB": 1024 ** 3, "GB": 1024 ** 3, "TiB": 1024 ** 4, "TB": 1024 ** 4}


def parse_size(text):
    """A size as nodetool prints it ("412.3 GiB", "1,5 GiB" in some locales,
    "100 bytes") in bytes (float), None when unknown ("?", "")."""
    match = _SIZE.match(str(text or ""))
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) * _FACTORS[match.group(2)]


def rate(per_second):
    """Bytes per second: "89 MiB/s", "3.2 MiB/s", "1.1 GiB/s"."""
    per_second = float(per_second or 0)
    if per_second >= GIB:
        return "%.1f GiB/s" % (per_second / GIB)
    if per_second >= 1024.0 ** 2:
        mib = per_second / 1024.0 ** 2
        return ("%.1f MiB/s" if mib < 10 else "%d MiB/s") % mib
    if per_second >= 1024:
        return "%d KiB/s" % (per_second / 1024.0)
    return "%d B/s" % per_second


def duration(seconds, short=False):
    """"45s", "9m20s", "1h12m", "2d04h"; short: minutes at most under an
    hour ("9m"), for an ETA."""
    seconds = int(max(0, number(seconds, 0)))
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return ("%dm" % (seconds // 60)) if short else "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def clock(epoch, now=None, date=False):
    """The controller's local time of epoch with its zone, the date too when it
    is not today, or always with date: "19:03 CEST", "2026-10-07 04:26 CEST"."""
    now = time.time() if now is None else now
    when = time.localtime(epoch)
    zone = time.strftime("%Z", when)
    if not zone or zone[0] in "+-":  # no abbreviation for this zone: its offset
        offset = time.strftime("%z", when)
        zone = offset[:3] + ":" + offset[3:]
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] and not date else "%Y-%m-%d %H:%M", when) + " " + zone


def count(number):
    """1240 as "1 240"."""
    return "{0:,}".format(int(number)).replace(",", " ")


def plural(number, word, many=None):
    """"1 node", "3 nodes"; many: the plural when it is not word + s."""
    return "%d %s" % (number, word if number == 1 else (many or word + "s"))


# --- node lists ----------------------------------------------------------------------------

def _natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(name))]


def _follows(before, name):
    """The index of the one number that goes up by 1 from before to name, the
    rest the same (node9 -> node10, node09 -> node10, rack1-n3 -> rack1-n4),
    else None."""
    a, b = re.split(r"(\d+)", before), re.split(r"(\d+)", name)
    if len(a) != len(b):
        return None
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    if len(changed) != 1 or changed[0] % 2 == 0:
        return None
    x, y = a[changed[0]], b[changed[0]]
    padded = x.startswith("0") or y.startswith("0")
    if int(y) != int(x) + 1 or (padded and len(x) != len(y)):
        return None
    return changed[0]


def unique_ignoring_case(texts):
    """texts, each once, the first of those equal but for case kept (as Ansible's | unique does by default)."""
    seen, kept = set(), []
    for text in texts:
        if text.lower() not in seen:
            seen.add(text.lower())
            kept.append(text)
    return kept


def full_list(names, keep_order=False):
    """Every name, ", " between them (an import report, -v)."""
    names = [str(n) for n in names or []]
    return ", ".join(names if keep_order else sorted(names, key=_natural))


def nodes(names, keep_order=False, full=False):
    """Node names, short: 3 or more consecutive numbered names as a range
    (node1..node5), 2 listed (node1, node2), a gap breaks the range (never a
    node that is not there), "6 nodes: " first when there are more than 5.
    Sorted naturally unless keep_order (a run's order: ranges only where the
    order follows the numbers). full: every name (see full_list)."""
    names = [str(n) for n in names or []]
    if full:
        return full_list(names, keep_order)
    names = names if keep_order else sorted(set(names), key=_natural)
    text = _ranges(names)
    return ("%d nodes: %s" % (len(names), text)) if len(names) > 5 else text


def _ranges(names):
    """Names in the order given, 3 or more consecutive numbered ones as a range."""
    parts, run, place = [], [], None

    def flush():
        parts.extend(["%s..%s" % (run[0], run[-1])] if len(run) >= 3 else run)
        del run[:]

    for name in names:
        step = _follows(run[-1], name) if run else None
        if step is not None and (len(run) == 1 or step == place):
            run.append(name)
            place = step
            continue
        flush()
        run.append(name)
        place = None
    flush()
    return ", ".join(parts)


# --- a value as shown ----------------------------------------------------------------------


def shown(key, value):
    """value as the output shows it: **** for a secret, a string as is, the
    rest as compact JSON."""
    if hidden(str(key), value):
        return MASK
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


# --- a setting and its value per node ------------------------------------------------------


def _same(value):
    return json.dumps(value, sort_keys=True, default=str)


def _groups(values, order):
    """{node: value} -> [(shown value, raw value, [nodes])], the most nodes first."""
    groups = {}
    for node in order:
        if node not in values:
            continue
        raw = values[node]
        groups.setdefault(_same(raw), [raw, []])[1].append(node)
    ranked = sorted(groups.values(), key=lambda g: (-len(g[1]), order.index(g[1][0])))
    return [tuple(group) for group in ranked]


def leaves(name, value):
    """A dict value as one setting per sub-key, name.key (a dict in it: name.key.sub),
    in its order: [(name.key, value)]; any other value, an empty dict too: [(name, value)]."""
    if isinstance(value, dict) and value:
        found = []
        for key in value:
            found += leaves("%s.%s" % (name, key), value[key])
        return found
    return [(name, value)]


def setting_rows(setting, values, all_nodes=None, expected=None, notes=None, where=None, full=False):
    """One setting across nodes -> (setting, [(shown value, nodes, note)]) for
    settings_lines. values: {node: value}; all_nodes: the nodes of the run
    (default: the ones of values), "all" when they all have the same value;
    one row per value, the minority ones marked "<- differs" (the ones not
    the expected value, when it is given). expected: the value the
    collection sets, annotated "(collection value)"; notes: {shown value:
    annotation}; where: where the value is written, a string for all or
    {shown value: place}. Secrets as ****."""
    order = list(all_nodes or sorted(values, key=_natural))
    order += [n for n in sorted(values, key=_natural) if n not in order]
    groups = _groups(values, order)
    notes = dict(notes or {})
    rows = []
    for index, (raw, names) in enumerate(groups):
        text = shown(setting, raw)
        who = "all" if len(groups) == 1 and len(names) == len(order) else nodes(names, full=full)
        extra = []
        if expected is not None and _same(raw) == _same(expected):
            extra.append("(collection value)")
        if text in notes:
            extra.append("(%s)" % notes[text])
        place = where.get(text) if isinstance(where, dict) else where
        if place:
            extra.append("in %s" % place)
        if expected is not None:
            differs = _same(raw) != _same(expected)
        else:  # the value most nodes have is the reference (on a tie, the first node's)
            differs = index > 0
        if differs and len(groups) > 1:
            extra.append(ARROW + " differs")
        rows.append((text, who, "  ".join(extra)))
    return (setting, rows)


def settings_lines(settings, indent="  ", max_value=30, max_label=40, max_nodes=30, width=None):
    """Several settings, aligned: one column for their names, one for the
    values, one for the nodes, then the notes ("<- differs"), the same
    columns across all of them (import_cluster's report, apply_config).
    settings: [(setting, [(value, nodes, note)])], a row per value (the
    setting named on its first one), or a str: a heading line printed as is
    between them (a file name). The value column is as wide as the longest
    value, up to max_value (a longer one pushes its nodes only), the nodes
    one up to max_nodes (a longer list pushes its note only); a name longer
    than max_label on a line of its own, its values below. width: the lines
    fit in it (a terminal's): names up to 35% of it, a longer value goes on
    over lines when pushing its nodes does not fit, the nodes too, under
    their column, never a name cut (see wrap_list); a note that does not fit
    after them on a line of its own, under them."""
    rows = [s for s in settings or [] if not isinstance(s, str)]
    if not rows:
        return [s for s in settings or [] if isinstance(s, str)]
    if width:
        max_label = min(56, max(16, int(width * 0.35)))
    labels = [len("%s:" % name) for name, values in rows if values]
    label = max([n for n in labels if n <= max_label] or [0])
    lengths = [len(str(v[0])) for n, values in rows for v in values]
    if width:  # the column of the values that fit in it, a longer one pushes its nodes
        lengths = [n for n in lengths if n + 2 <= max_value] or [max_value - 2]
    value_width = min(max(lengths or [0]) + 2, max_value)
    if width:  # 20 columns at least left for the nodes
        value_width = min(value_width, max(6, width - len(indent) - label - 24))  # (the nodes from width-22 on)
    who = min(max([len(str(v[1])) for n, values in rows for v in values] or [0]), max_nodes)
    out = []
    for entry in settings:
        if isinstance(entry, str):
            out.append(entry)
            continue
        name, values = entry
        head = "%s:" % name
        if values and len(head) > label:
            out.append(indent + head)
            head = ""
        for index, (value, names, note) in enumerate(values):
            value, names = str(value), str(names)
            first = (head if index == 0 else "").ljust(label)
            if not width:
                line = "%s%s %s %s" % (indent, first, value.ljust(value_width) if len(value) < value_width
                                       else value + " ",
                                       (names.ljust(who) if len(names) < who else names) + "   " + note if note
                                       else names)
                out.append(line.rstrip())
                continue
            out += _row_lines(indent + first, indent + " " * label, value, names, note, value_width, who, width)
    return out


def _row_lines(first, blank, value, names, note, value_width, who, width):
    """One row of settings_lines in width: first, the line's start (indent and the setting's name), blank the
    same as spaces; the value in its column, a longer one pushing the nodes as far as they fit, else on over lines;
    the nodes on as many lines as they need, under their column; the note after the nodes, else on lines of its
    own after the row's, under the nodes (further left when there is not room for it)."""
    start = len(first) + 1
    if len(value) >= value_width:  # pushes the nodes, as far as the line allows
        value_width = max(value_width, min(len(value) + 1, width - start - min(len(names), 30) - 1))
    values = wrap_value(value, value_width - 1)
    at = start + value_width + 1  # the nodes' column
    names = wrap_list(names, max(20, width - at))
    lines = []
    for i in range(max(len(values), len(names))):
        lines.append("%s %s %s" % (first if i == 0 else blank, (values[i] if i < len(values) else "").ljust(value_width),
                                   names[i] if i < len(names) else ""))
    if note:
        last = len(names) - 1
        noted = lines[last] + " " * max(0, who - len(names[-1])) + "   " + note
        if len(noted) > width and not names[-1]:  # no nodes (an inventory value no node is on): right after the value
            noted = lines[last].rstrip() + "   " + note
        if len(noted) <= width and last == len(lines) - 1:
            lines[last] = noted
        else:
            hang = " " * min(at, max(4, width - len(note), width - 40))
            lines += textwrap.wrap(note, width, initial_indent=hang, subsequent_indent=hang, break_long_words=False,
                                   break_on_hyphens=False) or [hang + note]
    return [line.rstrip() for line in lines]


def wrap_value(text, room):
    """A value on lines of at most room characters (one line when it fits): cut
    at a space when there is one, else where it must."""
    text = str(text)
    room = max(1, int(room))
    if len(text) <= room:
        return [text]
    return textwrap.wrap(text, room, break_long_words=True, break_on_hyphens=False, drop_whitespace=True) or [text]


def wrap_list(text, room):
    """A list of nodes or groups ("db11..db16, db21, G1 + db074") on lines of at
    most room characters: cut after a ", " (the comma kept), before a " + " or
    after "all except", a range longer than room after its "..", never inside
    a name (one longer than room has a line of its own)."""
    text = str(text)
    if len(text) <= room:
        return [text]
    lead = "all except " if text.startswith("all except ") else ""  # a word of its own
    parts = re.split(r"(, | \+ )", text[len(lead):])
    words = ([lead.strip()] if lead else []) + [parts[0]]
    for sep, word in zip(parts[1::2], parts[2::2]):
        if sep == ", ":
            words[-1] += ","
            words.append(word)
        else:
            words.append("+ " + word)
    pieces = []  # (word, glued: right after the one before it, no space: the end of a range)
    for word in words:
        first, dots, rest = word.partition("..")
        if len(word) > room and dots and rest:
            pieces += [(first + dots, False), (rest, True)]
        else:
            pieces.append((word, False))
    lines, line = [], ""
    for word, glued in pieces:
        joined = (line + ("" if glued else " ") + word) if line else word
        if line and len(joined) > room:
            lines.append(line)
            line = word
        else:
            line = joined
    return lines + [line]


_COMMAND_LINE = re.compile(r"^(\s*)(ansible-playbook|ansible) ")
# a command after a label ("then apply it live: ansible-playbook ..."): its indent, the label, the command (a
# plan's or a note's item, said on a line of its own when too long)
LABELLED_COMMAND = re.compile(r"^(\s*)(.*?: )(ansible-playbook .*)$")


_JOINED = u"\x1e"  # (a space fit never breaks at: put back as a space)


def fit(lines, width):
    """Lines no longer than width (a terminal's): a longer one goes on over
    lines indented under its text ("  1. " of a numbered item, else its own
    indent and 2 more), cut between words only (a path or a name longer than
    width stays whole; NBSP: a space never broken, printed as a space); a
    command (a line of its own starting with ansible-playbook or ansible) cut
    between its arguments only, each line but the last ending with " \\" (it
    pastes as one command); an option and its value (-e name=value) kept
    together. None or 0: as they are."""
    if not width:
        return list(lines)
    out = []
    for line in lines:
        if len(line) <= width:
            out.append(line.replace(NBSP, " "))
            continue
        command = _COMMAND_LINE.match(line)
        if command:
            out += _continued(len(command.group(1)), line.strip().replace(NBSP, " "), width)
            continue
        lead = re.match(r"^(\s*)(\d+\.\s+)?", line)
        hang = " " * (len(lead.group(0)) if lead.group(2) else len(lead.group(1)) + 2)
        text = re.sub(r"(?<!\S)-e (?=\S)", "-e" + _JOINED, line.lstrip())
        out += [part.replace(_JOINED, " ").replace(NBSP, " ") for part in textwrap.wrap(
            text, width, initial_indent=lead.group(1), subsequent_indent=hang, break_long_words=False,
            break_on_hyphens=False)] or [line]
    return out


# A line laid out where it is printed, at that width: "\x1f<hang>\x1f<text>" a
# paragraph, its next lines from column hang; "\x1f<hang>\x1f<key>\x1f<text>"
# a key (an option, an operation) and its text from column hang. NBSP: a space
# never broken ("(default: false)"), printed as a space.
FLOW = u"\x1f"
NBSP = u"\xa0"
BULLET = u"\u25cf"  # before an operation's name (its key)
_FLOWED = re.compile(u"\x1f(\\d+)\x1f([^\x1f]*)(?:\x1f(.*))?$", re.S)
NARROW = 30  # less room than this right of the key's column: its text under it


def flow(hang, text, key=None):
    """A line that laid_out lays out at the width of the screen: text, its
    next lines from column hang; with key (its indent included): the key,
    then text from column hang. A newline in them (an inventory's value) as a
    space: one line."""
    def one(value):
        return re.sub(u"[\x1f\r\n]", " ", value)
    if key is None:
        return u"%s%d%s%s" % (FLOW, hang, FLOW, one(text))
    return u"%s%d%s%s%s%s" % (FLOW, hang, FLOW, one(key), FLOW, one(text))


# a command to paste, "\x1f$<indent>\x1f<command>": "<indent>$ <command>", cut between its arguments
_COMMAND = re.compile(u"\x1f\\$(\\d+)\x1f(.*)$", re.S)
# the options that take the next word as their value: never cut between the two
_VALUED = ("-i", "--inventory", "-e", "--extra-vars", "-u", "--user", "-l", "--limit", "--vault-id",
           "--vault-password-file", "--private-key", "-t", "--tags", "--skip-tags", "-f", "--forks", "-c",
           "--connection", "-T", "--timeout", "--become-user", "--start-at-task", "--ssh-common-args")


def command_flow(indent, text):
    """A command line that laid_out lays out at the width of the screen:
    "<indent>$ <text>" on one line when it fits, else cut between two
    arguments (an option and its value kept together), each line but the last
    ending with " \\" (the shell's continuation: it pastes as one command),
    the next ones 4 columns further in."""
    return u"%s$%d%s%s" % (FLOW, len(indent), FLOW, re.sub(u"[\x1f\r\n]", " ", text))


def command_words(text):
    """The arguments of a command as written (its quotes kept), an option and
    its value as one: "-e x=y", "-i inventory.yml"."""
    words, word, quote, escaped = [], "", "", False
    for char in text:
        if escaped:  # a character after a backslash: as it is
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in " \t":
            if word:
                words.append(word)
            word = ""
            continue
        word += char
    words += [word] if word else []
    out = []
    for word in words:
        if out and out[-1] in _VALUED and not word.startswith("-"):
            out[-1] += " " + word
        else:
            out.append(word)
    return out


def is_command(line):
    """Whether a line is a command of command_flow (each of its lines printed as a command)."""
    return _COMMAND.match(line) is not None


def _continued(indent, text, width):
    """A command from column indent, cut between its arguments (an option and its value together), each line but
    the last ending with " \\", the next ones 2 columns further in."""
    first, rest = " " * indent, " " * (indent + 2)
    words = command_words(text)
    lines, line = [], first
    for i, word in enumerate(words):
        if line.strip() and len(line) + 1 + len(word) + (2 if i < len(words) - 1 else 0) > width:
            lines.append(line + " \\")
            line = rest
        line += (" " if line.strip() else "") + word
    return lines + [line]


def _command_lines(indent, text, width):
    first, rest = " " * indent + "$ ", " " * (indent + 4)
    lines, line = [], first
    words = command_words(text)
    for i, word in enumerate(words):
        # room for " \\" after it, but after the last one
        if line not in (first, rest) and len(line) + 1 + len(word) + (2 if i < len(words) - 1 else 0) > width:
            lines.append(line + " \\")
            line = rest
        line += ("" if line in (first, rest) else " ") + word
    return lines + [line]


def laid_out(line, width):
    """(lines, key): a line of flow wrapped at width, between words only (a
    word longer than the room stays whole); key: the key starting its first
    line ("" for none). A key longer than its column has a line of its own;
    with less than NARROW columns right of the column (or than a word of the
    text), the text goes under the key, 4 columns in. A command of
    command_flow cut between its arguments. Any other line as it is."""
    command = _COMMAND.match(line)
    if command:
        return _command_lines(int(command.group(1)), command.group(2), width), ""
    found = _FLOWED.match(line)
    if not found:
        return [line], ""
    hang, head, text = int(found.group(1)), found.group(2), found.group(3)
    indent = head[:len(head) - len(head.lstrip())]
    key = ""
    if text is None:  # a paragraph
        first, text = indent, head.lstrip()
    else:
        key = head.strip()
        if width - hang < max([NARROW] + [len(word) for word in text.split(" ")]):  # a word: never cut
            hang = len(indent) + 4
        first = head.ljust(hang) if len(head) + 2 <= hang else None  # None: the key on a line of its own
    rest = " " * hang
    lines = textwrap.wrap(text, width, initial_indent=rest if first is None else first, subsequent_indent=rest,
                          break_long_words=False, break_on_hyphens=False) if text.strip() else []
    if first is None:
        lines = [head] + lines
    return [out.replace(NBSP, " ").rstrip() for out in lines or [head]], key


def setting_lines(setting, values, all_nodes=None, expected=None, notes=None, where=None, full=False, indent=""):
    """One setting across nodes, setting-centric: "setting: value  nodes",
    a line per other value (see setting_rows), aligned as settings_lines
    does. Returns the lines."""
    entry = setting_rows(setting, values, all_nodes=all_nodes, expected=expected, notes=notes, where=where, full=full)
    return settings_lines([entry], indent=indent) if entry[1] else []


def by_nodes(pairs, full=False, every="all"):
    """pairs: [(node, [lines])] -> each line once, under the nodes that have
    it: "node1..node4" then its lines indented, the group every node shares
    first (named every)."""
    pairs = list(pairs or [])
    order, where = [], {}
    for name, lines in pairs:
        for line in lines or []:
            if line not in where:
                where[line] = []
                order.append(line)
            if name not in where[line]:
                where[line].append(name)
    groups = []
    for line in order:
        names = tuple(where[line])
        group = next((g for g in groups if g[0] == names), None)
        if group is None:
            groups.append((names, [line]))
        else:
            group[1].append(line)
    out = []
    for names, lines in sorted(groups, key=lambda g: len(g[0]) != len(pairs)):
        out.append(every if len(names) == len(pairs) and len(pairs) > 1 else nodes(names, full=full))
        out += ["  " + line for line in lines]
    return out


# --- node groups ---------------------------------------------------------------------------

# From this many nodes on, a screen names the node lists of its rows by groups:
# "G1 + db074", "all except db111"; a group has GROUP_LEAST nodes at least
GROUPS_FROM = 12
GROUP_LEAST = 3


def node_groups(sets, all_nodes, least=GROUPS_FROM):
    """The node groups of a screen of rows (a value and its nodes): the nodes
    that are in the same lists (sets: the node list of each row) make a group,
    so that each list is some groups and a few nodes (a list of one node is
    its name, e.g. a value per node: not a split). [(label, [nodes])], G1
    the largest; a group of GROUP_LEAST nodes at least, never every node; []
    under least nodes (their names are short enough)."""
    order = []
    for name in all_nodes or []:
        if str(name) not in order:
            order.append(str(name))
    if len(order) < least:
        return []
    every = set(order)
    seen, kinds = set(), []
    for names in sets or []:
        kind = frozenset(str(n) for n in names) & every
        if len(kind) > 1 and kind != every and kind not in seen:  # a node alone: its name
            seen.add(kind)
            kinds.append(kind)
    atoms = {}
    for name in order:
        atoms.setdefault(tuple(name in kind for kind in kinds), []).append(name)
    atoms.pop(tuple(False for kind in kinds), None)  # in no list (a node not read): named by no row
    big = [sorted(a, key=_natural) for a in atoms.values() if GROUP_LEAST <= len(a) < len(order)]
    big.sort(key=lambda a: (-len(a), _natural(a[0])))
    return [("G%d" % (i + 1), a) for i, a in enumerate(big)]


def who(names, all_nodes=None, groups=None, full=False, counted=True):
    """A node list by the groups of node_groups: "all" (every one of
    all_nodes), "G1 + G3 + db074", or "all except db111" when it is shorter;
    the nodes outside a group as nodes() names them (full: every name;
    counted: "6 nodes: " before more than 5)."""
    names = [str(n) for n in names or []]
    every = [str(n) for n in all_nodes or []]
    if every and set(every) <= set(names):
        return "all"

    def union(members):
        left, parts = set(members), []
        for label, group in groups or []:
            if set(group) <= left:
                parts.append(label)
                left -= set(group)
        rest = sorted(left, key=_natural)
        if rest:
            plain = full_list(rest) if full else nodes(rest) if counted and not parts else _ranges(rest)
            parts.append(plain)
        return " + ".join(parts)

    text = union(names)
    if groups and every:
        others = [n for n in every if n not in set(names)]
        rest = union(others)
        alternative = "all except " + ("(%s)" % rest if " + " in rest else rest)
        if len(alternative) < len(text):
            return alternative
    return text


def group_lines(groups, full=False, width=None, indent="  "):
    """The groups who() names, defined once at the top of a screen:
    "NODE GROUPS", then "  G1 = db11..db16, db21..db26 (12 nodes)", a long one
    on over lines under its nodes (full: every name)."""
    if not groups:
        return []
    lines = ["NODE GROUPS"]
    for label, members in groups:
        head = "%s%s = " % (indent, label)
        text = "%s (%s)" % (full_list(members) if full else _ranges(sorted(members, key=_natural)),
                            plural(len(members), "node"))
        chunks = wrap_list(text, max(20, width - len(head))) if width else [text]
        lines += [(head if i == 0 else " " * len(head)) + chunk for i, chunk in enumerate(chunks)]
    return lines


# --- plan ----------------------------------------------------------------------------------


def _where(step):
    if step.get("dc"):
        return "%s/%s" % (step["dc"], step.get("rack") or "?")
    return ""


def table(rows, indent=""):
    """Rows of cells, each column as wide as its widest cell, 2 spaces between."""
    rows = [[str(c) for c in row] for row in rows]
    if not rows:
        return []
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(len(r) for r in rows))]
    return [(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r) if c or i < len(r) - 1)).rstrip()
            for r in rows]


# --- warnings ------------------------------------------------------------------------------

# The one mark of a warning, everywhere: "WARNING  what" (the keyword first,
# two spaces, as the verdict words); the lines of a longer one under it
WARNING = "WARNING  "
_MARKED = re.compile(r"^\s*WARNING(?:\s+-\s+|:\s*|\s+)")


def is_warning(line):
    """Whether line is a warning (WARNING  ..., its older spellings too)."""
    return bool(_MARKED.match(str(line)))


def warning_text(line):
    """A warning without its mark ("WARNING  x" -> "x"); another line as is."""
    return _MARKED.sub("", str(line), count=1) if is_warning(line) else str(line)


def warning(text, indent="  ", under=False):
    """A warning's lines: "WARNING  text", its next lines (text a list, or
    lines in it) indented; under: every next line under its text (a plan's,
    their own indent kept after it)."""
    lines = [str(t) for t in text] if isinstance(text, (list, tuple)) else str(text).split("\n")
    lines = [line.rstrip() for line in lines if line.strip()]
    if not lines:
        return []
    if under:
        rest = [" " * len(WARNING) + line for line in lines[1:]]
    else:
        rest = [line if line[:1] in (" ", "\t") else indent + line for line in lines[1:]]
    return [WARNING + warning_text(lines[0]).strip()] + rest


def warning_lines(texts, seen=None, under=False):
    """Several warnings: each one once (seen: the texts already shown, kept
    up to date), one under the other; under: see warning()."""
    seen = set() if seen is None else seen
    out = []
    for text in texts or []:
        lines = warning(text, under=under)
        key = "\n".join(lines)
        if lines and key not in seen:
            seen.add(key)
            out += lines
    return out


def warnings_above(count, where="the plan", names=None, also=""):
    """The recap's pointer to warnings printed earlier: "2 warnings in the plan
    above (node2, node3)." (none: empty); names: the nodes they are for;
    also: the others ("the refusal")."""
    if not count:
        return []
    said = "; ".join(x for x in (nodes(names) if names else "", also) if x)
    return ["%s in %s above%s." % (plural(count, "warning"), where, " (%s)" % said if said else "")]


def spaced(paragraphs):
    """Paragraphs (each a list of lines) as one block, a PARAGRAPH line
    between two, the empty ones left out."""
    lines = []
    for paragraph in paragraphs:
        if paragraph:
            lines += ([PARAGRAPH] if lines else []) + list(paragraph)
    return lines


def title(operation, summary=""):
    """The first line of a plan: the operation in capitals, then what it does
    to whom, where: "ADD NODE  node4, node5 into dc1 of my_cluster"."""
    return "  ".join(x for x in (str(operation).replace("_", " ").upper(), str(summary or "")) if x)


def plan(operation, cluster="", summary="", steps=None, facts=None, warnings=None, question="", check=False,
         verdict="PLAN"):
    """The screen before a change, the same order everywhere: the title
    ("TOPOLOGY  3 steps of my_cluster, one node at a time": the cluster and
    version are the banner's, not said again), the numbered steps (each
    {node, dc, rack, text} or a string), then the facts ([label, text] or a
    string, NOTE lines among them), the WARNING lines, and the question
    (none under check: a line says nothing will be changed), each block
    after one blank line, an empty block left out. verdict: REFUSED, READY...
    first instead of the operation's capitals ("REFUSED  topology  cluster  summary")."""
    if verdict == "PLAN":
        lines = [title(operation, summary or cluster)]
    else:
        lines = ["  ".join(str(x) for x in (verdict, operation, cluster, summary) if x)]
    rows, numbered = [], 0
    for step in steps or []:
        numbered += 1
        if isinstance(step, dict):
            rows.append(["%d." % numbered, step.get("node", ""), _where(step), step.get("text", "")])
        else:
            rows.append(["%d." % numbered, str(step)])
    lines += table(rows, "  ")
    block = []
    labelled = [f for f in facts or [] if isinstance(f, (list, tuple))]
    width = max([len(str(f[0])) + 1 for f in labelled] or [0])
    for fact in facts or []:  # each fact a paragraph
        if isinstance(fact, (list, tuple)):
            block.append([("%s  %s" % ((str(fact[0]) + ":").ljust(width), fact[1])).rstrip()])
        elif str(fact).strip():
            block.append(str(fact).split("\n"))
    block = spaced(block)
    warned = warning_lines(warnings, under=True)  # a longer one's lines under its text
    asked = ["--check: nothing will be changed"] if check else [question] if question else []
    for section in (block, warned, asked):
        if section:
            lines += [""] + section
    return lines


# --- phases --------------------------------------------------------------------------------

# What a run does now, a line per major phase, ending with "..." (the ops
# callback's phase colour). {nodes}: the node list (nodes()), {count}: "5
# nodes", {node}, {what}: as given.
PHASES = {
    "check": "Checking the cluster ({what})...",  # what: check_what()
    "check_new": "Checking the new nodes ({nodes})...",
    "check_leaving": "Checking the nodes to remove ({nodes})...",
    "ring": "Reading the ring and seeds...",
    "prepare": "Preparing the new nodes ({nodes})...",
    "prepare_cluster": "Preparing the nodes ({nodes})...",
    "join": "Starting {node} and waiting for its bootstrap...",
    "join_again": "Waiting for the bootstrap of {node} (started by an earlier run)...",
    "join_no_stream": "Starting {node}, joining without streaming (auto_bootstrap false)...",
    "replace": "Starting {node} and waiting while it replaces {what}...",
    "decommission": "Decommissioning {node}...",
    "leave_again": "Waiting for the decommission of {node} (started by an earlier run)...",
    "move": "Moving {node}...",
    "reboot": "Rebooting {node}...",
    "update_java": "Updating Java on {node}...",
    "sync_clock": "Setting the clock of {node} (drained, stopped, started again)...",
    "upgrade": "Upgrading {node}...",
    "upgradesstables": "Upgrading the SSTables of {node}...",
    "wait_un": "Waiting for every node to be UN...",
    "seeds": "Applying cassandra_seeds on every node...",
    "compare_config": "Comparing the config with the inventory ({count})...",
    "write_config": "Writing the config ({nodes})...",
    "restart": "Restarting {node}...",
    "restart_rack": "Restarting {what} ({nodes})...",
    "cleanup": "Cleaning up {nodes}...",
    "read_reset": "Reading the data directories of {nodes}...",
    "reset": "Resetting {nodes}...",
    "import_given": "Reading the ring from {nodes}...",
}


def phase(name, names=None, node="", what="", count=None):
    """The phase line PHASES[name], e.g. "Decommissioning node3...". names:
    the nodes (one or a list, shown as nodes() does, in the order given);
    count: how many nodes (the number of names when not given). "" for a
    name PHASES does not have (a step that says nothing)."""
    if name not in PHASES:
        return ""
    names = [names] if isinstance(names, str) else list(names or [])
    number = len(names) if count is None else int(count)
    return PHASES[name].format(nodes=nodes(names, keep_order=True), node=node, what=what, count=plural(number, "node"))


def check_what(present, kind="", targets=None, replacing="", run=None, absent=None):
    """What preflight's check line says of the cluster: its members and the
    change the run makes, "3 nodes; adding node4, node5". present: the
    cluster's hosts not marked absent (lookup cassandra_nodes, the whole
    group, inventory order); kind: what the playbook does with targets
    (_cassandra_preflight_target: add, leave, reset, dead, canary or none;
    topology: plan); targets: the hosts cassandra_target_nodes names (dead: the
    dead node as given); replacing: cassandra_replace_address (replace_node);
    run: the run's hosts, said when --limit leaves some out; absent: the hosts
    marked absent (topology). A host being added or reset is not a member yet,
    one being removed (absent or not) still is; a dead node, out of the
    inventory (or marked absent), is not counted. topology's plan comes from the
    ring, read later: the inventory then."""
    present, targets, absent = list(present or []), list(targets or []), list(absent or [])
    if kind == "plan":
        text = plural(len(present), "node") + " in the inventory"
        if absent:
            text += ", %s marked absent" % nodes(absent, keep_order=True)
        whole = present
    else:
        named = [] if kind == "dead" else targets
        if kind in ("add", "reset"):
            members = [h for h in present if h not in named]
        elif kind == "leave":
            members = present + [h for h in named if h not in present]
        else:
            members = present
        whole = members + [h for h in named if h not in members]
        text = plural(len(members), "node")
        listed = nodes(named, keep_order=True)
        if kind == "add" and named and replacing:
            text += "; replacing %s with %s" % (replacing, listed)
        elif kind == "add" and named:
            text += "; adding " + listed
        elif kind == "leave" and named:
            text += "; removing " + listed
        elif kind == "reset" and named:
            text += "; resetting " + listed
        elif kind == "dead" and targets:
            text += "; removing the dead node %s" % targets[0]
    if run is not None and set(run) != set(whole):
        text += "; --limit: " + nodes(run, keep_order=True)
    return text


# --- progress ------------------------------------------------------------------------------

_BAR = 10


def _hhmm(epoch, now):
    """The controller's local time of epoch, "13:33", the date too when it is not today."""
    when = time.localtime(epoch)
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", when)


def _bar(done, total):
    filled = int(_BAR * min(done, total) / total) if total else 0
    return "[%s%s]" % ("#" * filled, "-" * (_BAR - filled))


# the time without progress a progress line says, from this many seconds (a
# check or two without a byte is no news)
IDLE_SHOWN = 60

# what a progress line says of its operation once it is over
DONE_WORDS = {"bootstrap": "bootstrapped", "decommission": "decommissioned", "move": "moved", "rebuild": "rebuilt",
              "replace": "replaced", "cleanup": "cleaned up", "restart": "restarted", "start": "started",
              "stop": "stopped", "reboot": "rebooted", "upgrade": "upgraded", "write_config": "config written",
              "update_java": "java updated"}


def step_head(index, total_steps, node, operation):
    """The head of a progress line: the node's position in the run, the node,
    two spaces, what is done to it: "[2/5] node2  restart"; "node2  restart"
    outside a rolling operation (total_steps 0)."""
    index, total_steps = number(index, 0), number(total_steps, 0)
    where = ("[%d/%d] %s" % (index, total_steps, node)) if total_steps else str(node)
    return "  ".join(x for x in [where, str(operation or "")] if x)


def wait_line(head, what, waited, timeout):
    """The progress line of a wait without streams: the head (step_head), "waiting", what is waited for, the
    time waited out of the timeout: "[2/5] node2  restart  waiting  node4 is DN, waiting for it to come back
    9m52s/10m"."""
    return "  ".join(x for x in [head, "waiting", what, "%s/%s" % (duration(waited), duration(timeout, short=True))] if x)


def done_word(operation):
    """What the frozen line of a step says of its operation: "bootstrap" ->
    "bootstrapped", "move to 42" -> "moved to 42" (DONE_WORDS, the first
    word); an operation it does not know: "<operation> done"."""
    first, rest = (str(operation).split(" ", 1) + [""])[:2]
    if first in DONE_WORDS:
        return (DONE_WORDS[first] + " " + rest).strip()
    return (str(operation) + " done").strip()


def progress_line(index, total_steps, node, operation, mode="", done=0, total=0, speed=None, now=0, start=None,
                  idle=0, limit=0, peers=None, status="going", warn=None, disk=0, frozen=0, width=None):
    """One check of a long operation (Q2), as its line:
    "[1/2] node5  bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m"
    then, after a blank line, the other ends in columns under it (peer_lines, in width):
    "      from node1  18.0/34.0 GiB  52%   from node3   4.2/17.0 GiB  24%".
    index/total_steps: the position in the run (no [i/n] when total_steps
    is 0); mode: netstats Mode; done/total: bytes; speed: bytes/s (None:
    not known yet); now/start: epoch seconds; idle/limit: the seconds
    without progress and the stall limit (said from IDLE_SHOWN seconds:
    while data is left "no progress for 1m", from warn seconds (the warning
    threshold; None: limit) "STALLED 6m/30m", then only the other ends in
    cause under it (peers_in_cause); "no progress 6m/30m" before the
    streams or once all is sent); disk, frozen: the bytes the receiving
    node's disk grew by since netstats last showed progress, frozen seconds
    ago (a file sent whole counts its bytes once it is over; that growth is
    progress): "receiving (disk +3.2 GiB in 5m)" once frozen for IDLE_SHOWN
    seconds; peers:
    [{name, way ('from'/'to'), done, total, stalled, idle (the seconds it
    moved nothing)}]: in columns (peer_lines) fitting in width (the
    terminal's); status: going, or stalled / failed (said in the line), or
    done: one frozen line, no other ends: "[1/2] node5  bootstrapped  NORMAL
    100.0 GiB in 19m02s  avg 90 MiB/s" (the mode when NORMAL, the average
    speed when bytes moved). Before any data flows: "waiting for streams"
    and the elapsed time."""
    start = now if start is None else start
    head = "  ".join(x for x in [step_head(index, total_steps, node, operation), mode] if x)
    elapsed = duration(now - start, short=True)
    pct = "%d%%" % int(100 * min(done, total) / total) if total else ""
    done_total = amount(done, total) if total else ""
    idle_text, quiet, pause = _idle_words(idle, limit, warn)
    if status == "done":
        return [_done_line(index, total_steps, node, operation, mode, done, total, now - start)]
    stalled = idle_text and not pause and total and done < total
    if status != "going" or stalled:
        line = "  ".join(x for x in [head, _status_word(status, idle_text), pct, done_total, elapsed] if x)
        others = peers_in_cause(peers, width) or peer_lines(peers, width)
    elif not total:  # before any stream: waiting
        line = "  ".join(x for x in [head, "waiting for streams", quiet, elapsed] if x)
        others = peer_lines(peers, width)
    else:
        line = "  ".join(x for x in [head, _bar(done, total), pct, done_total, rate(speed) if speed else "",
                                     _eta(done, total, speed, now), _on_disk(disk, frozen, idle_text, done, total),
                                     quiet if total <= done else pause, elapsed] if x)
        others = peer_lines(peers, width)
    return [line] + ([""] + others if others else [])


def _idle_words(idle, limit, warn):
    """What a progress line says of the time without progress, once it is IDLE_SHOWN seconds long and there is a stall
    limit (else "" each): (the count "6m/30m", "no progress 6m/30m", "no progress for 6m" while under warn, the
    warning threshold, None: limit). The last one: data left, quiet for a while but not that long yet, said as
    progress (a big file on its way)."""
    count = ("%s/%s" % (duration(idle, short=True), duration(limit, short=True))) if idle >= IDLE_SHOWN and limit else ""
    quiet = ("no progress " + count) if count else ""
    warn = limit if warn is None else min(int(warn), int(limit or 0))
    pause = ("no progress for " + duration(idle, short=True)) if count and idle < warn else ""
    return count, quiet, pause


def _status_word(status, idle_text):
    """The word of a progress line that is not going on (_STATUS_WORDS: "FAILED"); a stall with its count:
    "STALLED 6m/30m"."""
    word = _STATUS_WORDS.get(status, "STALLED" if status == "going" else status.upper())
    return ("%s %s" % (word, idle_text)) if idle_text and word == "STALLED" else word


def _on_disk(disk, frozen, idle_text, done, total):
    """A file sent whole shows on the receiving disk before netstats counts it: "receiving (disk +3.2 GiB in 5m)"
    once netstats has shown no progress for IDLE_SHOWN seconds (frozen) with data left, else ""."""
    if disk > 0 and frozen >= IDLE_SHOWN and not idle_text and total > done:
        return "receiving (disk +%s in %s)" % (size(disk), duration(frozen, short=True))
    return ""


def _done_line(index, total_steps, node, operation, mode, done, total, seconds):
    """The frozen line of a step over: "[1/2] node5  bootstrapped  NORMAL  100.0 GiB in 19m02s  avg 90 MiB/s";
    without bytes streamed: "[1/2] node5  bootstrapped  NORMAL  2m10s"."""
    moved = size(max(done, total)) if done or total else ""
    took = duration(seconds)
    average = ("avg " + rate(done / float(seconds))) if done and seconds > 0 else ""
    return "  ".join(x for x in [step_head(index, total_steps, node, done_word(operation)), mode if mode == "NORMAL" else "",
                                 ("%s in %s" % (moved, took)) if moved else took, average] if x)


# progress_line's word for a status other than going
_STATUS_WORDS = {"stalled": "STALLED", "failed": "FAILED", "too_long": "TOO LONG", "stopped": "STOPPED",
                 "jmx_refused": "JMX LOGIN REFUSED"}


def _eta(done, total, speed, now):
    """When the bytes left are through at this speed: "ETA 13:33 (9m)"; all sent: "all sent, finishing"."""
    if speed and total > done:
        left = (total - done) / float(speed)
        return "ETA %s (%s)" % (_hhmm(now + left, now), duration(left, short=True))
    return "all sent, finishing" if total <= done else ""


# above this many other ends, a line sums them up, only the ones behind detailed
PEERS_SUMMED = 30
PEERS_DETAILED = 12  # under it, at most this many of them (stalled first, then the slowest), then "+N more"
PEER_BEHIND = 10  # behind: below half the average of the ones still streaming, once that average is this many %
PEER_WIDTH = 100  # the width peer_lines fits in when none is given (as the ops callback's rules, 100 at most)
_PEER_INDENT = "      "  # the ops callback colours a peer line with STALLED by it
_PEER_GAP = "   "


def _percent(peer):
    total = peer.get("total", 0)
    return int(100 * min(peer.get("done", 0), total) / total) if total else 0


def _slowness(peer):
    return (not peer.get("stalled"), _percent(peer), _natural(peer.get("name")))


def _peer_state(peer):
    """STALLED, else how long it moved nothing (from IDLE_SHOWN seconds, data left), else ''."""
    if peer.get("stalled"):
        return "STALLED"
    if peer.get("idle", 0) >= IDLE_SHOWN and _percent(peer) < 100:
        return "no progress for " + duration(peer["idle"], short=True)
    return ""


def peer_lines(peers, width=None):
    """The other ends of a stream, under its progress line, in aligned columns
    (as many as fit in width, 4 at most), by name, a stalled one in place:
    "      to node1  3.1/7.2 GiB  43%           to node2  0.0/7.2 GiB   0% STALLED".
    peers: [{name, way ('from'/'to'), done, total, stalled, idle}] (idle: "no
    progress for 1m" from IDLE_SHOWN seconds when not stalled). Above
    PEERS_SUMMED of them (a node streaming both ways counts twice), one line
    sums them up, "87 peers: 40 done, 44 ok (avg 44%), 1 STALLED, 2 behind; slowest:
    node5 0% STALLED, node31 12%, node44 12%" (the ok ones' average; the 3
    slowest not done, stalled first, as many as fit in width; a node named
    with its way when it streams both ways), and only the stalled ones and
    the ones behind (below half the average of all the ones not done, once
    that is PEER_BEHIND%) are detailed under it, PEERS_DETAILED at most, then
    "+13 more: 8 STALLED, 5 behind"."""
    peers = sorted(peers or [], key=lambda p: (_natural(p.get("name")), p.get("way") or "with"))
    if not peers:
        return []
    out, left = [], []
    if len(peers) > PEERS_SUMMED:
        out, peers, left = _peers_summed(peers, width)
        if not peers:
            return out
    out += _peer_columns(peers, width)
    if left:
        stalled = len([p for p in left if p.get("stalled")])
        counts = (["%d STALLED" % stalled] if stalled else []) + (["%d behind" % (len(left) - stalled)]
                                                                  if len(left) > stalled else [])
        out.append(_PEER_INDENT + "+%d more: %s" % (len(left), ", ".join(counts)))
    return out


def peers_in_cause(peers, width=None):
    """Under a STALLED progress line, only the other ends in cause, one a line: the stalled ones, else the ones
    that moved nothing for IDLE_SHOWN seconds with data left; the others summed up after the first, the done
    ones together, a moving one with its percent:
    "      from node3  13.4/33.0 GiB  41%  nothing for 6m    (node1, node2 done, node4 52%)".
    [] when no other end is in cause (peer_lines then)."""
    peers = sorted(peers or [], key=lambda p: (_natural(p.get("name")), p.get("way") or "with"))
    cause = [p for p in peers if p.get("stalled")]
    cause = cause or [p for p in peers if p.get("idle", 0) >= IDLE_SHOWN and _percent(p) < 100]
    if not cause:
        return []
    others = [p for p in peers if p not in cause]
    done = [str(p.get("name")) for p in others if _percent(p) >= 100]
    rest = ([nodes(done) + " done"] if done else []) + ["%s %d%%" % (p.get("name"), _percent(p)) for p in others
                                                        if _percent(p) < 100]
    lines = []
    for peer in cause:
        text = "%s %s  %s  %d%%" % (peer.get("way") or "with", peer.get("name"),
                                    amount(peer.get("done", 0), peer.get("total", 0)), _percent(peer))
        if peer.get("idle", 0) >= IDLE_SHOWN:
            text += "  nothing for " + duration(peer["idle"], short=True)
        if rest and not lines:
            text += "    (%s)" % ", ".join(rest)
        lines.append(_PEER_INDENT + text)
    return fit(lines, width or PEER_WIDTH)


def _peers_behind(peers):
    """(the peers not done, the ones behind among them: stalled, or below half the average percent of the ones not
    done, once that average is PEER_BEHIND%)."""
    going = [p for p in peers if p.get("stalled") or _percent(p) < 100]
    average = sum(_percent(p) for p in going) / float(len(going)) if going else 100
    return going, [p for p in going if p.get("stalled") or (average >= PEER_BEHIND and _percent(p) < average / 2.0)]


def _peers_summed(peers, width):
    """Above PEERS_SUMMED peers (sorted by name): (the line that sums them up, the ones behind detailed under it by
    name, the ones behind left out)."""
    going, behind = _peers_behind(peers)
    late = set(id(p) for p in behind)
    fine = [p for p in going if id(p) not in late]
    both = len(set(str(p.get("name")) for p in peers)) < len(peers)
    parts = ["%d done" % (len(peers) - len(going))] if len(going) < len(peers) else []
    if fine:
        parts.append("%d ok (avg %d%%)" % (len(fine), sum(_percent(p) for p in fine) / float(len(fine))))
    stalled = len([p for p in behind if p.get("stalled")])
    parts += ["%d STALLED" % stalled] if stalled else []
    parts += ["%d behind" % (len(behind) - stalled)] if len(behind) > stalled else []
    summary = _PEER_INDENT + "%s: %s" % (plural(len(peers), "peer"), ", ".join(parts))
    slowest = ["%s%s %d%%%s" % ((p.get("way") or "with") + " " if both else "", p.get("name"), _percent(p),
                                " STALLED" if p.get("stalled") else "") for p in sorted(going, key=_slowness)[:3]]
    while slowest and len(summary + "; slowest: " + ", ".join(slowest)) > (width or PEER_WIDTH):
        slowest.pop()  # as many as fit in the width
    lines = fit([summary + ("; slowest: " + ", ".join(slowest) if slowest else "")], width or PEER_WIDTH)
    shown = set(id(p) for p in sorted(behind, key=_slowness)[:PEERS_DETAILED])
    return lines, [p for p in behind if id(p) in shown], [p for p in behind if id(p) not in shown]


def _peer_columns(peers, width):
    """The peers in aligned columns, as many as fit in width (4 at most)."""
    cells = [(p.get("way") or "with", str(p.get("name")), amount(p.get("done", 0), p.get("total", 0)),
              "%d%%" % _percent(p), _peer_state(p)) for p in peers]
    widths = [max(len(c[i]) for c in cells) for i in range(4)]
    texts = ["%s %s  %s  %s%s" % (way.ljust(widths[0]), name.ljust(widths[1]), size_text.rjust(widths[2]),
                                  pct.rjust(widths[3]), " " + state if state else "")
             for way, name, size_text, pct, state in cells]
    cell = max(len(t) for t in texts)
    room = (width or PEER_WIDTH) - len(_PEER_INDENT) + len(_PEER_GAP)
    per_line = max(1, min(4, room // (cell + len(_PEER_GAP))))
    return [(_PEER_INDENT + _PEER_GAP.join(t.ljust(cell) for t in texts[at:at + per_line])).rstrip()
            for at in range(0, len(texts), per_line)]


# --- end of run ----------------------------------------------------------------------------

_WOULD = {"restarted": "restart", "applied": "apply", "started": "start", "stopped": "stop", "removed": "remove",
          "added": "add", "changed": "change", "upgraded": "upgrade", "reset": "reset", "moved": "move",
          "cleaned up": "clean up", "decommissioned": "decommission", "replaced": "replace", "rebooted": "reboot",
          "updated": "update", "written": "write", "drained": "drain", "disabled": "disable", "enabled": "enable",
          "installed": "install", "imported": "import", "repaired": "repair", "created": "create"}


def would(outcome):
    """A done outcome as check mode says it: "applied, restarted" ->
    "would apply, restart"; a word it does not know: "would be <word>"."""
    words = [w.strip() for w in str(outcome).split(",") if w.strip()]
    if not words:
        return "would change nothing"
    known = [_WOULD.get(w) for w in words]
    if all(known):
        return "would " + ", ".join(known)
    return "would be " + ", ".join(words)


def _median(numbers):
    numbers = sorted(numbers)
    middle = len(numbers) // 2
    return numbers[middle] if len(numbers) % 2 else (numbers[middle - 1] + numbers[middle]) / 2.0


def recap(operation, cluster, outcomes, check=False, seconds=None, extra=None, full=False, slow_factor=1.5,
          head=True, causes=True):
    """The end of a run (Q4). outcomes: [{node, outcome ('restarted',
    'applied, restarted'...), status ('ok', 'failed', 'skipped'; default
    ok), seconds (how long its part took), reason (failed or skipped),
    said (the outcome as shown, when would() can't say it), dc, rack}] in
    the order of the run. Line 1, the verdict: "DONE  op  cluster  6
    restarted  11m30s", "FAILED  op  cluster  ..." when a node failed
    (under check too), "CHECK  op  cluster  would restart 6 nodes  nothing
    was changed" under check, "NOTHING TO DO" when no node did anything. Then the nodes grouped by identical outcome ("node1..node6
    restarted  1m49s..1m58s each"), and a line of its own for a node more
    than slow_factor times the median of its group (slow), a failed node (its
    reason on the line under it) and the skipped ones, with the reason. extra: lines added after (the ring
    after, versions...). full: every name (-v). head: False for the table
    alone (the STOPPED block says the verdict); causes: False when the cause
    of a failure is said elsewhere (that block): its row reads what was done
    to the node (row: "restarted; cluster health check failed"), else
    "not done" and the reason as a word on it, no line under."""
    outcomes = list(outcomes or [])
    for item in outcomes:
        item.setdefault("status", "ok")
    done = [o for o in outcomes if o["status"] == "ok"]
    failed = [o for o in outcomes if o["status"] == "failed"]
    skipped = [o for o in outcomes if o["status"] == "skipped"]

    def said(o):
        return o.get("said") or (would(o.get("outcome", "")) if check else str(o.get("outcome", "")))

    groups = {}
    for o in done:
        groups.setdefault(said(o), []).append(o)
    # every failure a refusal (a check before any change) and no node done: REFUSED, nothing was changed
    refused = bool(failed) and not done and all(str(o.get("reason") or "").startswith("refused: ") for o in failed)
    lines = [_recap_verdict(operation, cluster, groups, failed, skipped, check, seconds, refused)] if head else []
    rows = []
    for word, members in groups.items():
        rows += _recap_done_rows(word, members, check, full, slow_factor)
    under, spans = _recap_failed_rows(failed, rows, refused, causes)
    rows += _recap_skipped_rows(skipped, full)
    first = max(len(str(r[0])) for r in rows) if rows else 0
    for i, line in enumerate(table(rows, "  ")):
        line = "  %s  %s" % (str(rows[i][0]).ljust(first), spans[i]) if i in spans else line
        lines += [line] + ([under[i]] if i in under else [])
    if extra:
        lines += [""] + list(extra)
    return lines


def _recap_verdict(operation, cluster, groups, failed, skipped, check, seconds, refused=False):
    """The recap's first line: verdict, operation, cluster, the counts by outcome (groups: {outcome as said:
    [outcomes]}), the run's duration; under check, that nothing was changed. refused: every failure a refusal,
    nothing done: "REFUSED  op  cluster  nothing was changed"."""
    if refused:
        return "  ".join(x for x in ["REFUSED", operation, cluster, "nothing was changed"] if x)
    if check:
        counts = [("%s on %s" if len(word.split()) > 2 and word != "would clean up" else "%s %s") % (word, plural(len(members), "node"))
                  for word, members in groups.items()]
    else:
        counts = ["%d %s" % (len(members), word) for word, members in groups.items()]
    counts += ["%d failed" % len(failed)] if failed else []
    counts += ["%d not touched" % len(skipped)] if skipped else []
    verdict = "FAILED" if failed else ("CHECK" if check else ("DONE" if groups else "NOTHING TO DO"))
    head = "  ".join(x for x in [verdict, operation, cluster, ", ".join(counts) or "nothing to do"] if x)
    if seconds is not None and not check:
        head += "  %s" % duration(seconds)
    if check:
        head += "   nothing was changed"
    return head


def _recap_failed_rows(failed, rows, refused, causes):
    """The failed nodes' rows, added to rows -> (under, spans): {row index: its failure's reason, on a line of its own
    under the row (the columns are not sized on it)}, {row index: what was done to the node, the whole row after its
    name (causes False: the cause is said elsewhere; else "not done" and its reason on the row)}."""
    under, spans = {}, {}
    for o in failed:
        if not causes:
            if o.get("row"):
                spans[len(rows)] = o["row"]
                rows.append([o["node"]])
            else:
                rows.append([o["node"], "not done", o.get("reason") or ""])
            continue
        if o.get("reason"):
            under[len(rows)] = "    %s" % (o["reason"][len("refused: "):] if refused else o["reason"])
        rows.append([o["node"], "REFUSED" if refused else "FAILED", ""])
    return under, spans


def _recap_skipped_rows(skipped, full):
    """The rows of the nodes not touched, one per reason: "node4, node5  not touched (unreachable)"."""
    reasons = {}
    for o in skipped:
        reasons.setdefault(o.get("reason") or "", []).append(o["node"])
    return [[nodes(names, keep_order=True, full=full), "not touched" + (" (%s)" % reason if reason else ""), ""]
            for reason, names in reasons.items()]


def _slow_ones(members, slow_factor):
    """(the median time of the members, the ones slower than slow_factor times it). A node's partial time (the part a
    resumed run did) is no measure of the others, and never slow itself."""
    times = [o["seconds"] for o in members if o.get("seconds") is not None and not o.get("partial")]
    middle = _median(times) if times else None
    slow = [o for o in members if middle and len(times) > 1 and o.get("seconds") is not None and not o.get("partial")
            and o["seconds"] > slow_factor * middle]
    return middle, slow


def _recap_done_rows(word, members, check, full, slow_factor):
    """The rows of the nodes done with one same outcome (word): the usual ones together with their times, then
    each one slower than slow_factor times the group's median on its own."""
    middle, slow = _slow_ones(members, slow_factor)
    usual = [o for o in members if o not in slow]
    rows = []
    if usual:
        spread = sorted(o["seconds"] for o in usual if o.get("seconds") is not None)
        note = ""
        if spread and not check:
            # (labelled: "in 49s", the time of its part of the run)
            note = "in %s each" % duration(spread[0]) if spread[0] == spread[-1] or len(usual) == 1 \
                else "in %s..%s each" % (duration(spread[0]), duration(spread[-1]))
            if len(usual) == 1:
                note = "in " + duration(spread[0])
        rows.append([nodes([o["node"] for o in usual], keep_order=True, full=full), word, note])
    for o in slow:
        rows.append([o["node"], word, "in %s  %s slow (median %s)" % (duration(o["seconds"]), ARROW, duration(middle))])
    return rows


def perm_lines(changes, indent="  "):
    """changes: [{file, before: {owner, group, mode}, after: {...}}] ->
    "cassandra.yaml  owner/group/mode  root/cassandra 0640 -> cassandra/cassandra 0640"."""
    def text(p):
        p = p or {}
        return "%s/%s %s" % (p.get("owner", "?"), p.get("group", "?"), p.get("mode", "?"))
    rows = [[c.get("file", ""), "owner/group/mode", "%s -> %s" % (text(c.get("before")), text(c.get("after")))]
            for c in changes or [] if text(c.get("before")) != text(c.get("after"))]
    return table(rows, indent)


def _flat(value, path=()):
    if isinstance(value, dict) and not value and not path:
        return {}
    if isinstance(value, dict) and value:
        out = {}
        for k in value:
            out.update(_flat(value[k], path + (str(k),)))
        return out
    return {path: value}


def diff_lines(before, after, indent="  "):
    """Two dicts of settings (nested ones as in cassandra.yaml) -> the
    changed ones only, "- key: old" then "+ key: new", a nested key under its
    parents; secrets as ****."""
    old, new = _flat(before or {}), _flat(after or {})
    # a key that held a secret on one side (a dict with a password) hides its value on the other
    parents_of_secrets = set(p[:i] for p in list(old) + list(new) if hidden(p[-1], old.get(p, new.get(p)))
                             for i in range(1, len(p)))
    out, opened = [], ()
    for path in sorted(set(old) | set(new), key=lambda p: [_natural(x) for x in p]):
        a, b = old.get(path, _MISSING), new.get(path, _MISSING)
        if _same(a) == _same(b):
            continue
        parents = path[:-1]
        for depth in range(len(parents)):
            if opened[:depth + 1] != parents[:depth + 1]:
                out.append("%s%s%s:" % (indent, "  " * depth, parents[depth]))
        opened = parents
        masked = any(hidden(p, a) or hidden(p, b) for p in path) or path in parents_of_secrets
        pad = "  " * len(parents)
        for sign, value in (("-", a), ("+", b)):
            if value is not _MISSING:
                out.append("%s%s %s%s: %s" % (indent, sign, pad, path[-1], MASK if masked else shown(path[-1], value)))
    return out


_MISSING = object()


def changed_lines(diff, indent="  "):
    """A unified diff (text or lines) -> its - and + lines only (not the
    ---/+++ headers nor @@), secrets masked."""
    lines = diff.splitlines() if isinstance(diff, str) else list(diff or [])
    shown, sides = {}, ([], [])  # each hunk's lines of the old file and of the new one: (index, text)

    def flush():
        # each side masked as one text, its context lines in: a secret's value spans lines (a YAML block)
        for side in sides:
            for (i, dummy), body in zip(side, mask_lines(text for dummy, text in side)):
                if lines[i][:1] != " ":
                    shown[i] = body
            del side[:]
    for i, line in enumerate(lines):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            flush()
        for side, signs in zip(sides, (" -", " +")):
            if line[:1] and line[:1] in signs:
                side.append((i, line[1:]))
    flush()
    return [indent + lines[i][0] + shown[i] for i in sorted(shown)]


# --- what is left to do --------------------------------------------------------------------


def path_from(path, cwd):
    """path relative to cwd when it is under it, else as given."""
    rel = os.path.relpath(path, cwd) if cwd and os.path.isabs(path) else path
    return path if rel.startswith("..") else rel


def extra_var(name, value):
    """-e name=value as the shell and Ansible's key=value parsing both take it (else as JSON)."""
    value = str(value)
    if re.match(r"^[\w.:/@,+-]+$", value):
        return "-e %s=%s" % (name, value)
    return "-e " + shlex.quote(json.dumps({name: value}))


def command(playbook, inventory=None, hosts=None, limit=None, extra=None, options=None, cwd=None):
    """The full command of a collection playbook, ready to paste:
    "ansible-playbook -i inventory.yml community.cassandra.cleanup -e
    cassandra_hosts=my_cluster --limit node1,node2 -e k=v". inventory: the
    sources (a list or a string; none: the -i is left out, ansible.cfg's
    is used); hosts: the cluster's group (none: left out, the playbooks find
    it); limit: the hosts, every one named (no range); extra: {name: value}
    or a list of ready arguments; options: other arguments as given
    (--vault-id...); cwd: paths shown relative to it."""
    sources = [inventory] if isinstance(inventory, str) else list(inventory or [])
    parts = ["ansible-playbook"] + ["-i %s" % shlex.quote(path_from(s, cwd)) for s in sources if s]
    parts += [str(o) for o in options or []]
    name = playbook if "." in playbook else "community.cassandra." + playbook
    parts.append(name)
    if hosts:
        parts.append(extra_var("cassandra_hosts", hosts))
    if limit:
        names = [limit] if isinstance(limit, str) else list(limit)
        parts.append("--limit " + shlex.quote(",".join(str(n) for n in names)))
    if isinstance(extra, dict):
        parts += [extra_var(k, extra[k]) for k in extra]
    else:
        parts += [str(e) for e in extra or []]
    return " ".join(parts)


def todo(items, title="TO DO"):
    """items: a string, or {text, command} (the command on its own line under
    it) -> "TO DO", then "  1. text" numbered; nothing when no item."""
    entries = []
    for item in items or []:
        if not item:
            continue
        text, cmd = (item.get("text", ""), item.get("command", "")) if isinstance(item, dict) else (str(item), "")
        entry = ["  %d. %s%s" % (len(entries) + 1, text, ":" if cmd and text and not text.endswith(":") else "")]
        entries.append(entry + (["     " + cmd] if cmd else []))
    # an item on several lines: a blank line between two
    lines = spaced(entries) if any(len(e) > 1 for e in entries) else [line for e in entries for line in e]
    return [title] + lines if lines else []


def in_git_work_tree(path):
    """True when path (a file or a dir) is inside a git work tree: a .git
    dir or file in it or above it."""
    path = os.path.abspath(str(path or "."))
    current = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def inventory_steps(inventory_file, in_git=None, message="", cwd=None):
    """The TO DO items about an inventory change: "Review the inventory:
    <file>", and only when it is inside a git work tree (in_git, or found
    out from the file) the commit command."""
    shown_path = path_from(str(inventory_file), cwd)
    if in_git is None:
        in_git = in_git_work_tree(inventory_file)
    steps = ["Review the inventory: %s" % shown_path]
    if in_git:
        steps.append({"text": "commit it", "command": "git add %s && git commit -m %s" % (
            shlex.quote(shown_path), shlex.quote(message or "Inventory: %s" % os.path.basename(shown_path)))})
    return steps
