# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output of the collection's filters, written once: every
function returns text lines (or a string) that a debug task prints, nothing
else. Pure Python, no Ansible.

- sizes, durations, counts: size, parse_size, duration, count, plural;
- node lists: nodes (node1..node5, node7), full_list; by groups on a large
  cluster: node_groups, who, group_lines;
- secrets: mask, shown;
- a setting and its value per node: settings_lines (several, aligned, wrapped
  in a width: wrap_value, wrap_list); lines fit in a terminal's width: fit;
  laid out at the width where they are printed: flow;
- warnings: warning, warning_lines (each once, a blank line between two),
  spaced paragraphs;
- phase lines: phase, check_what;
- the end of a node operation: recap;
- what is left to do: command, extra_var, todo, seeds_todo;
- the seed rule of preflight: seed_layout."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import os
import re
import shlex
import textwrap
ARROW = u"←"  # the marker of a line that needs a look: "<- down", "<- differs"
# A blank line between two paragraphs of one block: a single space (the
# default callback's yaml format strips it). An empty line "" between two
# blocks of a plan, a screen or a recap is a rule under the ops callback;
# this one stays blank there.
PARAGRAPH = " "
GIB = 1024.0 ** 3
_UNITS = (("TiB", 1024.0 ** 4), ("GiB", GIB), ("MiB", 1024.0 ** 2), ("KiB", 1024.0))

# --- sizes, rates, durations ---------------------------------------------------------------


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


def duration(seconds, short=False):
    """"45s", "9m20s", "1h12m", "2d04h"; short: minutes at most under an
    hour ("9m"), for an ETA."""
    seconds = int(max(0, seconds or 0))
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return ("%dm" % (seconds // 60)) if short else "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def count(number):
    """1240 as "1 240"."""
    return "{0:,}".format(int(number)).replace(",", " ")


def plural(number, word, many=None):
    """"1 node", "3 nodes"; many: the plural when it is not word + s."""
    return "%d %s" % (number, word if number == 1 else (many or word + "s"))


# --- node lists ------------------------------------------------------------------------------

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


# --- secrets -------------------------------------------------------------------------------

# One rule for what is a secret: what the output hides (****).
# A variable or setting whose value is a secret (sse_c_key, access_key: Medusa's; kspw, tspw: a keystore's and a
# truststore's in a JVM option; ca_key, auth_token, a name ending in _pw or _pass)
SECRET = re.compile(r"password|passwd|secret|sse_c_key|access_key|private_key|key_material|auth_token|ca_key"
                    r"|_kspw$|_tspw$|_pw$|_pass$|^pw$|^pass$", re.I)
_HIDDEN = SECRET
MASK = "****"
# A secret inside a line of text (a config file line, a JVM option): its name (as above, inside a longer one;
# not a path: a name ending in file or path, e.g. -Dcom.sun.management.jmxremote.password.file), a quote between
# the name and the colon (JSON, Python), then its value
_HIDDEN_NAME = (r"(?<![\w.-])[\w.-]*(?:password|passwd|secret|private_key|sse_c_key|access_key|key_material|auth_token"
                r"|ca_key|_kspw|_tspw|_pw|_pass)[\w.-]*(?<![._-]file)(?<![._-]path)[\"']?")
# (its name and the = or : after it: import_cluster takes the value out of a line)
SECRET_NAME = r"(?i)(" + _HIDDEN_NAME + r"\s*[:=]\s*)"
_HIDDEN_KEY = (SECRET_NAME +
               r"(\"(?:[^\"\\]|\\.)*\"?|'(?:[^']|'')*'?|")
# an unquoted value runs to the end of the line (a comment aside), as a YAML plain scalar or a JVM option may hold
# a comma; inside a flow mapping or JSON ({...} before it) it ends at the next comma or brace
_HIDDEN_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|$))", re.M)
_HIDDEN_FLOW_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|,\s|[,}]|$))", re.M)
# nodetool -pw, --password; -p only after cqlsh (elsewhere a port or mkdir -p)
_HIDDEN_OPTION = re.compile(r"((?:^|\s)(?:-pw|--password)\s+|\bcqlsh\b[^\n]*?\s-p\s+)(\S+)", re.M)
# the text of an XML element of a secret's name (logback.xml: <password>...</password>)
# (its text up to its own closing tag: CDATA sections and comments, closed or not, child elements too)
_ELEMENT_TEXT = r"<!\[CDATA\[(?:.*?\]\]>|.*)|<!--(?:.*?-->|.*)"
_HIDDEN_ELEMENT = re.compile(r"(?i)(<(?P<name>" + _HIDDEN_NAME + r")(?:\s[^<>]*)?>)(\s*(?!</(?P=name)\s*>)(?:" + _ELEMENT_TEXT + r"|\S)"
                             r"(?:" + _ELEMENT_TEXT + r"|(?!</(?P=name)\s*>).)*)")

# a secret's name and what comes before its value, anywhere on a line
_HIDDEN_KEY_ONLY = re.compile(r"(?i)(" + _HIDDEN_NAME + r"\s*[:=]\s*)")
# a line without any of these words holds no secret setting (looked for first: the patterns above are slow)
_HIDDEN_WORD = re.compile(r"(?i)password|passwd|secret|private_key|sse_c_key|access_key|key_material|auth_token"
                          r"|ca_key|_kspw|_tspw|_pw|_pass")


def _in_flow(before):
    """True when text before a key leaves a flow mapping or JSON object open
    (a "{" not closed, Jinja's "{{" aside)."""
    before = before.replace("{{", "").replace("}}", "")
    return before.count("{") > before.count("}")


def _mask_flow(match):
    """Inside an open {...}: the value up to the next comma or brace."""
    return match.group(1) + MASK if _in_flow(match.string[:match.start()]) else match.group(0)


def _mask_line(match):
    """Elsewhere: the value up to the end of the line (a comment aside)."""
    return match.group(0) if _in_flow(match.string[:match.start()]) else match.group(1) + MASK


def _scan(text, depth=0, quote=None, brackets="[{"):
    """(depth, quote) after text: the brackets ([ {; ( too for the shell) left
    open and the quote (" or ') left open, from those before it. A quote opens
    a value only where one starts (first, after a bracket, a comma, a colon, =,
    a space or the shell's $: it's is a word); a comment (space #) ends the text."""
    return _scan_to(text, depth, quote, brackets)[:2]


def _scan_to(text, depth=0, quote=None, brackets="[{"):
    """_scan, and where the text ends: its comment's #, else its length."""
    closing = {"[": "]", "{": "}", "(": ")"}
    closers = "".join(closing[b] for b in brackets)
    i, prev = 0, " "
    while i < len(text):
        c = text[i]
        if quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
        elif quote == "'":
            if c == "'" and text[i + 1:i + 2] == "'":
                i += 1
            elif c == "'":
                quote = None
        elif c in "\"'" and prev in " \t[{(,:=$":
            quote = c
        elif c in brackets:
            depth += 1
        elif c in closers:
            depth -= 1
        elif c == "#" and prev in " \t":
            break
        prev = c
        i += 1
    return depth, quote, i


def _odd_backslashes(text):
    text = text.rstrip("\r")  # a CRLF line
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


def _goes_on(line):
    """How the value of the line's secret (the first one whose value goes on)
    goes on to the next lines, else
    None: ("indent", column, list) for a YAML value (a block value key: |, a
    plain value, or the value under key:, list: a "- " list at the key's column
    too) on the lines indented more than the key; ("scan", depth, quote) for a
    quote or brackets left open (a flow [ {, a flow mapping the value is in, a
    shell array), brackets: the ones counted; ("backslash",) for a line ending with an odd number of
    backslashes (a properties file, the shell). A commented-out key: none; a name in the line's comment
    (key: |  # the password: ...) is not a key, nor one inside a value its line's first secret leaves open."""
    if line.lstrip().startswith("#"):
        return None
    code = line[:_scan_to(line)[2]]  # the line without its comment
    keys = list(_HIDDEN_KEY_ONLY.finditer(code))
    # each key tried costs a scan of the line: the first ones, then the last (a line of many: a JSON file)
    for key in (keys[:16] + keys[-1:]) if len(keys) > 17 else keys:
        state = _goes_on_from(line, code, key)
        if state is not None:
            return state
    return ("backslash",) if _odd_backslashes(line) else None


def _goes_on_from(line, code, key):
    """_goes_on for the secret key matched in code (line without its comment): None when its value ends on the
    line."""
    rest, colon, flow = code[key.end():], key.group(1).rstrip()[-1:] == ":", _in_flow(line[:key.start()])
    # its value, after a YAML tag or anchor (key: !!str "..., key: &a '...)
    value = re.sub(r"^(?:[!&]\S*(?:[ \t]+|$))+", "", rest) if colon else rest
    before = _scan(line[:key.end()])[1]  # a quote open before the value (JVM_OPTS="... -Dx.password=")
    if flow:
        # in a flow mapping: the rest of it when it goes on to the next line with the value not ended on this one
        depth, quote = _scan(line.replace("{{", "").replace("}}", ""))
        opened = _scan(rest, 0, before)[0] > 0  # the value's own [ { left open
        ended = re.search(r"[,}\]]", re.sub(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'", "", rest))
        if quote or (depth > 0 and (opened or not ended)):
            return ("scan", depth, quote, "[{")
    elif value[:1] and (value[:1] in "\"'[{(" or (not (colon and not before) and value[:1] == "$")):  # the shell's $' $( $"
        brackets = "[{" if colon else "[{("
        depth, quote = _scan(value, 0, before, brackets)
        if depth > 0 or quote:
            return ("scan", depth, quote, brackets)
    elif colon and not before:
        return ("indent", key.start(1), not value.strip())
    return None


def _still_on(state, line):
    """The state after line, a line of the secret's value (False: line is not
    one; a line after one ending with a backslash is, see mask)."""
    if state[0] == "indent":
        indent = len(line) - len(line.lstrip())
        if not line.strip() or indent > state[1] or (state[2] and indent == state[1] and
                                                     re.match(r"-(\s|$)", line.lstrip())):
            return state
        return False
    if state[0] == "scan":
        depth, quote = _scan(line, state[1], state[2], state[3])
        return ("scan", depth, quote, state[3]) if depth > 0 or quote else None
    return False


def mask(text):
    """text with the value of every secret setting in it as ****: key: value,
    key=value, "key": "value", -pw value, --password value, and the lines its
    value goes on to: a YAML block value (key: |), a plain value on more
    indented lines, a value under key: (a mapping, a list), a quote or a flow
    [ { left open, a line ending with a backslash."""
    out = []
    state = None  # how the value of the last secret goes on (_goes_on)
    continued = False  # the line before, of the value, ends with a backslash: this one goes on with it
    for line in str(text).split("\n"):
        indent = len(line) - len(line.lstrip())
        if state is not None:
            after = _still_on(state, line)
            if after is False and continued:
                after = state
            if after is None and _odd_backslashes(line):
                after = ("backslash",)  # a quote closed, then a backslash: the next line too
            if after is not False:
                out.append(line[:indent] + MASK if line.strip() else line)
                state, continued = after, _odd_backslashes(line)
                continue
        if not _HIDDEN_WORD.search(line):
            state = None
            out.append(_HIDDEN_OPTION.sub(r"\1" + MASK, line))
            continue
        state, continued = _goes_on(line), _odd_backslashes(line)
        line = _HIDDEN_ELEMENT.sub(r"\1" + MASK, line)
        line = _HIDDEN_VALUE.sub(_mask_line, _HIDDEN_FLOW_VALUE.sub(_mask_flow, line))
        out.append(_HIDDEN_OPTION.sub(r"\1" + MASK, line))
    return "\n".join(out)


def hidden(key, value):
    """True when value of key is (or holds) a secret: the output shows it as ****, import_cluster writes it to
    secrets.yml. A name of SECRET (not a path: cassandra_jmx_password_file), or a secret inside the value (a
    key: value, key=value, -pw value in a string, a dict or list that holds one); never an empty value."""
    if isinstance(value, dict):
        return any(hidden(str(k), v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return bool(_HIDDEN.search(key)) or any(hidden(key, v) for v in value)
    if value == "" or value is None:
        return False  # e.g. a password variable set to "" to leave it out
    if isinstance(value, str) and mask(value) != value:
        return True  # e.g. a unit's JVM_EXTRA_OPTS=-Djavax.net.ssl.keyStorePassword=...
    return bool(_HIDDEN.search(key)) and not key.endswith("_file")


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


def fit(lines, width):
    """Lines no longer than width (a terminal's): a longer one goes on over
    lines indented under its text ("  1. " of a numbered item, else its own
    indent and 2 more), cut between words only (a path or a name longer than
    width stays whole). None or 0: as they are."""
    if not width:
        return list(lines)
    out = []
    for line in lines:
        if len(line) <= width:
            out.append(line)
            continue
        lead = re.match(r"^(\s*)(\d+\.\s+)?", line)
        hang = " " * (len(lead.group(0)) if lead.group(2) else len(lead.group(1)) + 2)
        out += textwrap.wrap(line.lstrip(), width, initial_indent=lead.group(1), subsequent_indent=hang,
                             break_long_words=False, break_on_hyphens=False) or [line]
    return out


# A line laid out where it is printed, at that width: "\x1f<hang>\x1f<text>" a
# paragraph, its next lines from column hang; "\x1f<hang>\x1f<key>\x1f<text>"
# a key (an option, an operation) and its text from column hang. NBSP: a space
# never broken ("(default: false)"), printed as a space.
FLOW = u"\x1f"


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


# --- node groups -------------------------------------------------------------------------

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


def _table(rows, indent=""):
    """Rows of cells, each column as wide as its widest cell, 2 spaces between."""
    rows = [[str(c) for c in row] for row in rows]
    if not rows:
        return []
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(len(r) for r in rows))]
    return [(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r) if c or i < len(r) - 1)).rstrip()
            for r in rows]


# --- warnings --------------------------------------------------------------------------------

# The one mark of a warning, everywhere: "WARNING - what"; the lines of a
# longer one under it, indented
WARNING = "WARNING - "
_MARKED = re.compile(r"^\s*WARNING(?:\s+-\s+|:\s*|\s+)")


def is_warning(line):
    """Whether line is a warning (WARNING - ..., its older spellings too)."""
    return bool(_MARKED.match(str(line)))


def warning_text(line):
    """A warning without its mark ("WARNING - x" -> "x"); another line as is."""
    return _MARKED.sub("", str(line), count=1) if is_warning(line) else str(line)


def warning(text, indent="  ", under=False):
    """A warning's lines: "WARNING - text", its next lines (text a list, or
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
    up to date), a blank line (PARAGRAPH) between two; under: see warning()."""
    seen = set() if seen is None else seen
    blocks = []
    for text in texts or []:
        lines = warning(text, under=under)
        key = "\n".join(lines)
        if lines and key not in seen:
            seen.add(key)
            blocks.append(lines)
    return spaced(blocks)


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


# --- the prepare of new nodes ---------------------------------------------------------------

# --- progress ------------------------------------------------------------------------------

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


def recap(operation, cluster, outcomes, check=False, seconds=None, extra=None, full=False, slow_factor=1.5):
    """The end of a run (Q4). outcomes: [{node, outcome ('restarted',
    'applied, restarted'...), status ('ok', 'failed', 'skipped'; default
    ok), seconds (how long its part took), reason (failed or skipped),
    said (the outcome as shown, when would() can't say it), dc, rack}] in
    the order of the run. Line 1, the verdict: "DONE  op cluster  6
    restarted (11m30s)", "FAILED  op  cluster  ..." when a node failed
    (under check too), "CHECK  op  cluster  would restart 6 nodes  nothing
    was changed" under check, "NOTHING TO DO" when no node did anything. Then the nodes grouped by identical outcome ("node1..node6
    restarted  (1m49s..1m58s each)"), and a line of its own for a node more
    than slow_factor times the median of its group (slow), a failed node (its
    reason on the line under it) and the skipped ones, with the reason. extra: lines added after (the ring
    after, versions...). full: every name (-v)."""
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
    lines = [_recap_verdict(operation, cluster, groups, failed, skipped, check, seconds)]
    rows = []
    for word, members in groups.items():
        rows += _recap_done_rows(word, members, check, full, slow_factor)
    # a failure's reason on its own line under its row: the columns are not sized on it
    under = {}
    for o in failed:
        if o.get("reason"):
            under[len(rows)] = "    %s" % o["reason"]
        rows.append([o["node"], "FAILED", ""])
    reasons = {}
    for o in skipped:
        reasons.setdefault(o.get("reason") or "", []).append(o["node"])
    for reason, names in reasons.items():
        rows.append([nodes(names, keep_order=True, full=full), "not touched" + (" (%s)" % reason if reason else ""), ""])
    for i, line in enumerate(_table(rows, "  ")):
        lines += [line] + ([under[i]] if i in under else [])
    if extra:
        lines += [""] + list(extra)
    return lines


def _recap_verdict(operation, cluster, groups, failed, skipped, check, seconds):
    """The recap's first line: verdict, operation, cluster, the counts by outcome (groups: {outcome as said:
    [outcomes]}), the run's duration; under check, that nothing was changed."""
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
        head += " (%s)" % duration(seconds)
    if check:
        head += "   nothing was changed"
    return head


def _recap_done_rows(word, members, check, full, slow_factor):
    """The rows of the nodes done with one same outcome (word): the usual ones together with their times, then
    each one slower than slow_factor times the group's median on its own."""
    times = [o["seconds"] for o in members if o.get("seconds") is not None]
    middle = _median(times) if times else None
    slow = [o for o in members if middle and len(times) > 1 and o.get("seconds") is not None
            and o["seconds"] > slow_factor * middle]
    usual = [o for o in members if o not in slow]
    rows = []
    if usual:
        spread = sorted(o["seconds"] for o in usual if o.get("seconds") is not None)
        note = ""
        if spread and not check:
            note = "(%s each)" % duration(spread[0]) if spread[0] == spread[-1] or len(usual) == 1 \
                else "(%s..%s each)" % (duration(spread[0]), duration(spread[-1]))
            if len(usual) == 1:
                note = duration(spread[0])
        rows.append([nodes([o["node"] for o in usual], keep_order=True, full=full), word, note])
    for o in slow:
        rows.append([o["node"], word, "%s  %s slow (median %s)" % (duration(o["seconds"]), ARROW, duration(middle))])
    return rows


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


def seeds_todo(change, group=""):
    """The TO DO item once seeds were picked for a datacenter its
    cassandra_seeds left with none (cassandra_seed_change's auto): the line
    to put in the inventory, else apply_config would write the inventory's
    list back. None when nothing was picked."""
    if not (change or {}).get("auto"):
        return None
    entries = [e.strip() for e in str(change["new"]).split(",") if e.strip()]
    return {"text": "put the seeds the nodes now run with in the inventory (group_vars/%s/, or a re-import writes"
                    " them), else apply_config puts cassandra_seeds back" % (group or "<cluster group>"),
            "command": "cassandra_seeds: [%s]" % ", ".join('"%s"' % e for e in entries)}


SEEDS_MIN, SEEDS_MAX = 2, 3  # per datacenter (OUTPUT Q7)


def seed_target(nodes, racks):
    """The seeds a datacenter of that many nodes and racks gets by the seed rule."""
    return min(nodes, SEEDS_MAX, max(SEEDS_MIN, racks))


def pick_seeds(racks, target):
    """target nodes of a datacenter, one rack after the other, its seeds first
    (racks holding a seed first: well placed seeds stay)."""
    order = sorted(racks, key=lambda r: not any(n["seed"] for n in racks[r]))
    queues = dict((r, sorted(racks[r], key=lambda n: not n["seed"])) for r in order)
    picked = []
    while len(picked) < target and any(queues.values()):
        for rack in order:
            if queues[rack] and len(picked) < target:
                picked.append(queues[rack].pop(0))
    return picked


def seed_layout(nodes):
    """The seed rule (Q7), the same for preflight and help: 2 or 3 seeds per
    datacenter, on different racks when it has several; 1 seed (in a
    datacenter of more than one node) or none is a warning, more than 3 a
    note. nodes: [{name, address, dc, rack, seed}] in inventory order.
    Returns {lines: one per datacenter ("Seeds  dc1  node1 (rack_a), node3
    (rack_b)  ok", a warning's "WARNING - Seeds  dc2  ..."), dcs: [{dc, level
    ok|note|warning, text}], problems: the warnings' texts, notes, suggested: a seed list that follows the rule
    (addresses, every datacenter; empty without a warning)}."""
    dcs = {}
    for n in nodes or []:
        dcs.setdefault(n["dc"], {}).setdefault(n["rack"], []).append(n)
    width = max([len(str(dc)) for dc in dcs] or [0])
    result = {"lines": [], "dcs": [], "problems": [], "notes": [], "suggested": []}
    suggested = []
    for dc, racks in dcs.items():
        members = [n for rack in racks.values() for n in rack]
        seeds = [n for n in members if n["seed"]]
        seed_racks = []
        for n in seeds:
            if n["rack"] not in seed_racks:
                seed_racks.append(n["rack"])
        level, text = "ok", ""
        if not seeds:
            level, text = "warning", "no seed"
        elif len(seeds) == 1 and len(members) > 1:
            level, text = "warning", "1 seed: %d to %d per datacenter%s" % (
                SEEDS_MIN, SEEDS_MAX, ", on different racks" if len(racks) > 1 else "")
        elif len(seed_racks) < min(len(seeds), len(racks)):
            empty = [r for r in racks if r not in seed_racks]
            level, text = "warning", "%s on %s, none on %s: put them on different racks" % (
                plural(len(seeds), "seed"), ", ".join(seed_racks), ", ".join(empty))
        elif len(seeds) > SEEDS_MAX:
            level, text = "note", "%d seeds: %d to %d are enough" % (len(seeds), SEEDS_MIN, SEEDS_MAX)
        listed = ", ".join("%s (%s)" % (n["name"], n["rack"]) for n in seeds) or "none"
        line = "Seeds  %s  %s  %s" % (str(dc).ljust(width), listed, {"ok": "ok", "note": "note: " + text}.get(level, text))
        result["lines"].append(WARNING + line if level == "warning" else line)
        result["dcs"].append({"dc": dc, "level": level, "text": text})
        if level == "warning":
            result["problems"].append("%s: %s" % (dc, text))
        elif level == "note":
            result["notes"].append("%s: %s" % (dc, text))
        keep = seeds if level != "warning" else pick_seeds(racks, seed_target(len(members), len(racks)))
        suggested += [n["address"] for n in keep]
    result["suggested"] = suggested if result["problems"] else []
    return result
