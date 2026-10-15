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
  cluster: node_groups, who, group_lines;
- secrets: secret, mask, shown;
- a setting and its value per node: setting_lines, settings_lines (several,
  aligned, wrapped in a width: wrap_value, wrap_list), setting_rows, by_nodes;
  lines fit in a terminal's width: fit; laid out at the width where they are
  printed: flow, laid_out (the help text);
- before a change: plan; while new nodes are prepared: prepare_lines,
  left_system_warning; during it: progress_line; at the end: recap,
  perm_lines, diff_lines, changed_lines;
- what is left to do: command, extra_var, todo, inventory_steps,
  in_git_work_tree;
- the seed rule of preflight and help: seed_layout.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import os
import re
import shlex
import textwrap
import time

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
    seconds = int(max(0, seconds or 0))
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return ("%dm" % (seconds // 60)) if short else "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def clock(epoch, now=None):
    """The controller's local time of epoch with its zone, the date too when it
    is not today: "19:03 CEST", "2026-10-07 04:26 CEST"."""
    now = time.time() if now is None else now
    when = time.localtime(epoch)
    zone = time.strftime("%Z", when)
    if not zone or zone[0] in "+-":  # no abbreviation for this zone: its offset
        offset = time.strftime("%z", when)
        zone = offset[:3] + ":" + offset[3:]
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", when) + " " + zone


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

# One rule for what is a secret: what the output hides (****) and what import_cluster files in secrets.yml.
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
_HIDDEN_KEY = (r"(?i)(" + _HIDDEN_NAME + r"\s*[:=]\s*)"
               r"(\"(?:[^\"\\]|\\.)*\"?|'(?:[^']|'')*'?|")
# an unquoted value runs to the end of the line (a comment aside), as a YAML plain scalar or a JVM option may hold
# a comma; inside a flow mapping or JSON ({...} before it) it ends at the next comma or brace
_HIDDEN_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|$))", re.M)
_HIDDEN_FLOW_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|,\s|[,}]|$))", re.M)
SECRET_VALUE = _HIDDEN_VALUE
# nodetool -pw, --password; -p only after cqlsh (elsewhere a port or mkdir -p)
_HIDDEN_OPTION = re.compile(r"((?:^|\s)(?:-pw|--password)\s+|\bcqlsh\b[^\n]*?\s-p\s+)(\S+)", re.M)
_BLOCK = re.compile(r"^\s*[|>][-+]?\d*\s*$")
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


def mask_lines(lines):
    """The lines of a file (or one block of them), each masked as mask() masks
    the whole text: a line of a secret's value is masked even without its key
    in the lines shown (the key is in lines)."""
    lines = list(lines)
    if any("\n" in line for line in lines):  # entries of several lines each (an XML element's text): one at a time
        return [mask(line) for line in lines]
    return mask("\n".join(lines)).split("\n") if lines else []


def mask_hunks(lines):
    """Hand-edit lines (a head, "<file>, line N:", "<file>: <key>" or a note,
    then its "  - " and "  + " lines), each head's - lines masked as one text
    and its + lines as another (a secret's value on the lines under its key),
    every other line on its own."""
    lines, shown, hunk, runs = list(lines), list(lines), 0, {}
    for i, line in enumerate(lines):
        if line[:4] in ("  - ", "  + "):
            runs.setdefault((hunk, line[2]), []).append(i)
        else:
            hunk += 1
            shown[i] = mask(line)
    for at in runs.values():
        for i, body in zip(at, mask_lines(lines[i][4:] for i in at)):
            shown[i] = lines[i][:4] + body
    return shown


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


# what import_cluster files in secrets.yml: the same rule, nothing hidden on screen in clear in group_vars
secret = hidden


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


def laid_out(line, width):
    """(lines, key): a line of flow wrapped at width, between words only (a
    word longer than the room stays whole); key: the key starting its first
    line ("" for none). A key longer than its column has a line of its own;
    with less than NARROW columns right of the column (or than a word of the
    text), the text goes under the key, 4 columns in. Any other line as it
    is."""
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


def _where(step):
    if step.get("dc"):
        return "%s/%s" % (step["dc"], step.get("rack") or "?")
    return ""


def _table(rows, indent=""):
    """Rows of cells, each column as wide as its widest cell, 2 spaces between."""
    rows = [[str(c) for c in row] for row in rows]
    if not rows:
        return []
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(len(r) for r in rows))]
    return [(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r) if c or i < len(r) - 1)).rstrip()
            for r in rows]


def spaced(paragraphs):
    """Paragraphs (each a list of lines) as one block, a PARAGRAPH line
    between two, the empty ones left out."""
    lines = []
    for paragraph in paragraphs:
        if paragraph:
            lines += ([PARAGRAPH] if lines else []) + list(paragraph)
    return lines


def plan(operation, cluster="", summary="", steps=None, facts=None, warnings=None, question="", version="",
         check=False, verdict="PLAN"):
    """The screen before a change, the same order everywhere:
    "PLAN  operation  cluster (Cassandra x)  summary", the numbered steps
    (each {node, dc, rack, text} or a string), then the facts ([label, text]
    or a string, NOTE lines among them), the WARNING lines, and the question
    (none under check: a line says nothing will be changed), each block
    after one blank line, an empty block left out. verdict: REFUSED, READY...
    for the same layout."""
    head = [verdict, operation, ("%s (Cassandra %s)" % (cluster, version)) if version and cluster else cluster,
            summary]
    lines = ["  ".join(str(x) for x in head if x)]
    rows, numbered = [], 0
    for step in steps or []:
        numbered += 1
        if isinstance(step, dict):
            rows.append(["%d." % numbered, step.get("node", ""), _where(step), step.get("text", "")])
        else:
            rows.append(["%d." % numbered, str(step)])
    lines += _table(rows, "  ")
    block = []
    labelled = [f for f in facts or [] if isinstance(f, (list, tuple))]
    width = max([len(str(f[0])) + 1 for f in labelled] or [0])
    for fact in facts or []:  # each fact a paragraph
        if isinstance(fact, (list, tuple)):
            block.append([("%s  %s" % ((str(fact[0]) + ":").ljust(width), fact[1])).rstrip()])
        elif str(fact).strip():
            block.append(str(fact).split("\n"))
    block = spaced(block)
    warned = spaced([["WARNING  %s" % w] for w in warnings or [] if str(w).strip()])
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


# --- the prepare of new nodes ---------------------------------------------------------------

# The steps of a prepare (cassandra_service action_add_prepare.yml, create_cluster), in their order
PREPARE_STEPS = ("repository", "install", "java", "os tuning", "data disks", "config", "firewall", "medusa", "seeds")
_FILE_CHANGED = re.compile(r"\d+ line\(s\) changed$")  # cassandra_config's item for a file it writes
_VERSION = re.compile(r"(?:\d+:)?(\d+(?:\.\d+)*)")  # "1:17.0.12+7-1~deb12u1" -> 17.0.12


def _on(value):
    """A flag as Ansible may template it: True, "True", "yes", "on"..."""
    return value is True or str(value).strip().lower() in ("true", "yes", "on", "y", "1")


def _version(text):
    found = _VERSION.match(str(text or ""))
    return found.group(1) if found else str(text or "")


def _changes(count, check):
    if not count:
        return "ok (already set)"
    return ("would make %s" if check else "%s") % plural(count, "change")


def _grouped(pairs):
    """[(key, name)] -> [(key, [names])], the keys in the order first seen."""
    groups = []
    for key, name in pairs:
        group = next((g for g in groups if g[0] == key), None)
        if group is None:
            groups.append((key, [name]))
        else:
            group[1].append(name)
    return groups


def _package(item):
    """The package a change item of cassandra_install names, without a version pin ("pkg=17.0.12*"), else ""."""
    text = str(item.get("item", ""))
    return re.sub(r"[=<>].*$", "", text[len("package "):]) if text.startswith("package ") else ""


def _package_step(name, version, items, check, also=(), label=None):
    """A package's text: "cassandra 5.0.9 (installed now)", "(already installed)", "(would be
    installed)"; label: what names it ("" for its version alone), also: other packages installed with it."""
    new = next((i for i in items if _package(i) == name), None)
    if new is None:
        state = "already installed"
    else:
        state = "would be installed" if check else "installed now"
        after = new.get("after") or ""
        # (a pinned name the role looked up as is: "not in configured repos yet", the version read is better)
        version = after if _VERSION.match(str(after)) or not _VERSION.match(str(version or "")) else version
    extra = "; also " + ", ".join(also) if also else ""
    label = name if label is None else label
    shown = _version(version)
    if shown and not _VERSION.match(str(version or "")):  # a candidate not found ("not in configured repos yet")
        return ("%s (%s: %s%s)" % (label, state, version, extra)).strip()
    return ("%s %s (%s%s)" % (label, shown, state, extra) if shown else "%s (%s%s)" % (label, state, extra)).strip()


def _prepare_texts(node, check):
    """{step: text} for one node of prepare_lines (a step it has nothing for: left out)."""
    texts = {}
    items = list(node.get("packages") or [])
    repository = node.get("repository")
    if repository:
        texts["repository"] = {
            "off": "not managed (cassandra_repository_manage: false)",
            "packages": "none (cassandra_install_method: packages)",
            "set": "would be set" if check else "set now",
        }.get(repository, "ok (already set)")
    ran = list(node.get("ran") or [])
    if "cassandra_install" in ran:
        java = node.get("java") or {}
        others = [_package(i) for i in items if _package(i) not in ("", "cassandra", str(java.get("package", "")))]
        texts["install"] = _package_step("cassandra", node.get("cassandra"), items, check, also=others)
        if not _on(java.get("managed", True)):
            texts["java"] = "not managed (cassandra_install_java: false)"
        elif java.get("home"):
            texts["java"] = "%s from %s" % (java.get("version") or java.get("series", ""), java["home"])
        elif java.get("package"):
            texts["java"] = _package_step(java["package"], java.get("version"), items, check, label="")
    if ran:  # no report from cassandra_linux: it left the OS as it is
        if "cassandra_linux" in ran:
            texts["os tuning"] = _changes(int(node.get("os") or 0), check)
        else:
            texts["os tuning"] = "not managed (cassandra_linux_manage: false)"
    disks = node.get("disks") or {}
    if disks:
        kb = "read-ahead %s KB " % disks["kb"] if str(disks.get("kb", "")) not in ("", "none") else ""
        if _on(disks.get("changed")):
            text = kb + ("would be set" if check else "set now")
        elif disks.get("brief"):
            text = kb + "already set"
        else:
            text = "nothing to tune"
        if disks.get("warnings"):
            text += " (%s below)" % plural(len(disks["warnings"]), "warning")
        elif disks.get("info"):
            text += " (see below)"
        texts["data disks"] = text.strip()
    if "cassandra_config" in ran:
        config = list(node.get("config") or [])
        templated = next((i for i in config if i.get("item") == "config files"), None)
        files = [i for i in config if _FILE_CHANGED.search(str(i.get("after", "")))]
        other = [i for i in config if i is not templated and i not in files]
        if templated is not None:
            text = "would write the files (%s)" % str(templated.get("before", "")).lower()
        elif files:
            text = ("would write %s" if check else "%s written") % plural(len(files), "file")
        else:
            text = "" if other else "ok (unchanged)"
        if other:  # owners, modes, the RedHat conf link
            text += (", " if text else "") + ("would make %s" if check else "%s") % plural(len(other), "other change")
        texts["config"] = text
    for step, switch in (("firewall", "cassandra_manage_firewall"), ("medusa", "cassandra_medusa_enabled")):
        value = node.get(step)
        if value is None or not ran:
            continue
        if value == "off":
            texts[step] = "off (%s: false)" % switch
        elif value == "not managed":  # turned on, the role's own switch off (the firewall's)
            texts[step] = "not managed" + (" (cassandra_firewall_manage: false)" if step == "firewall" else "")
        elif not isinstance(value, str):
            changes = list(value)
            medusa = next((i for i in changes if i.get("item") == "Medusa"), None)
            if medusa is not None:
                text = ("would install Medusa %s" if check else "Medusa %s installed now") % medusa.get("after")
                texts[step] = text + ("" if len(changes) == 1 else " (+%s)" % plural(len(changes) - 1, "change"))
            else:
                texts[step] = _changes(len(changes), check)
    seeds = node.get("seeds")
    if seeds:
        seeds = [s.strip() for s in seeds.split(",")] if isinstance(seeds, str) else list(seeds)
        texts["seeds"] = ("would join with " if check else "joins with ") + ", ".join(seeds)
    return texts


def prepare_lines(hosts, check=False):
    """What the prepare of new nodes did (check: would do), a line per step
    and per group of nodes with the same outcome, the steps in their order:
    "node4, node5  repository  ok (already set)", "node4  install  cassandra
    5.0.9 (installed now)". hosts: [{name, ran (the roles whose change
    report ran), repository (off, packages, set or ok), packages
    (cassandra_install's change items), cassandra (its version installed),
    java ({managed, package, version, home, series}), os (the count of OS
    changes), disks (cassandra_linux's _cassandra_disk_summary), config
    (cassandra_config's change items), firewall (its change items, off or
    not managed), medusa (its change items or off), seeds (the seeds it joins
    with)}]."""
    texts = [(h["name"], _prepare_texts(h, check)) for h in hosts or [] if isinstance(h, dict) and h.get("name")]
    rows = []
    for step in PREPARE_STEPS:
        groups = _grouped((said[step], name) for name, said in texts if step in said)
        rows += [[who(names, counted=False), step, text] for text, names in groups]
    return _table(rows)


def _wrapped(text, width=110, indent="  "):
    return textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False,
                         break_long_words=False)


def left_system_warning(hosts):
    """The WARNING about an old system keyspace a new node has outside the
    data directories its config uses, each directory once with its nodes:
    why the run goes on, and what to delete by hand. hosts: [{name, left:
    [directories holding a system keyspace], dirs: [the data directories its
    config uses]}]."""
    said = _grouped(((tuple(h["left"]), tuple(h.get("dirs") or [])), h["name"])
                    for h in hosts or [] if isinstance(h, dict) and h.get("left"))
    lines = []
    for (left, dirs), names in said:
        where, shown = ", ".join(d.rstrip("/") or "/" for d in left), who(names, counted=False)
        inside = [d for d in dirs if any(d.startswith(x.rstrip("/") + "/") for x in left)]
        lines.append("WARNING: %s: an old system keyspace in %s, not one of the data directories the config uses (%s)"
                     % (shown, where, ", ".join(dirs) or "?"))
        lines += _wrapped(
            "Cassandra does not read it: the node starts empty in its own data directories, the run goes on. It is"
            " what an earlier install or config left there: disk space taken, and a config pointing there again"
            " would start the node on that old system keyspace.")
        lines += _wrapped(
            "The way out, once the run is over and if no other Cassandra on %s uses it: delete by hand the keyspace"
            " directories in %s (system, system_schema, system_auth, system_distributed, system_traces and any"
            " other there)%s. reset_node does not: it empties only the directories the inventory and"
            " cassandra.yaml name." % (shown, where, ", keeping " + ", ".join(inside) if inside else ""))
    return lines


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


def progress_line(index, total_steps, node, operation, mode="", done=0, total=0, speed=None, now=0, start=None,
                  idle=0, limit=0, peers=None, status="going"):
    """One check of a long operation (Q2), as 2 lines:
    "[1/2] node5 bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m"
    then always the other ends: "      from node1 18.0/34.0 GiB ok   from node3 4.2/17.0 GiB stalled".
    index/total_steps: the position in the run (no [i/n] when total_steps
    is 0); mode: netstats Mode; done/total: bytes; speed: bytes/s (None:
    not known yet); now/start: epoch seconds; idle/limit: the seconds
    without progress and the stall limit (said from IDLE_SHOWN seconds:
    "STALLED 6m/15m" while data is left, "no progress 6m/1h" before the
    streams or once all is sent); peers: [{name, way ('from'/'to'), done,
    total, stalled}]; status: going, or stalled / failed / done (said in the
    line). Before any data flows: "waiting for streams" and the elapsed time."""
    start = now if start is None else start
    head = "  ".join(x for x in [("[%d/%d] %s %s" % (index, total_steps, node, operation)) if total_steps
                                 else "%s %s" % (node, operation), mode] if x)
    elapsed = duration(now - start, short=True)
    pct = "%d%%" % int(100 * min(done, total) / total) if total else ""
    done_total = amount(done, total) if total else ""
    idle_text = ("%s/%s" % (duration(idle, short=True), duration(limit, short=True))) if idle >= IDLE_SHOWN and limit else ""
    quiet = ("no progress " + idle_text) if idle_text else ""
    if status == "done":
        line = "  ".join(x for x in [head, "done", done_total, elapsed] if x)
    elif status != "going" or (idle_text and total and done < total):  # before any stream: still waiting for them
        word = {"stalled": "STALLED", "failed": "FAILED", "too_long": "TOO LONG", "stopped": "STOPPED",
                "jmx_refused": "JMX LOGIN REFUSED"}.get(
            status, "STALLED" if status == "going" else status.upper())
        count_text = ("%s %s" % (word, idle_text)) if idle_text and word == "STALLED" else word
        line = "  ".join(x for x in [head, count_text, pct, done_total, elapsed] if x)
    elif not total:
        line = "  ".join(x for x in [head, "waiting for streams", quiet, elapsed] if x)
    else:
        eta = ""
        if speed and total > done:
            left = (total - done) / float(speed)
            eta = "ETA %s (%s)" % (_hhmm(now + left, now), duration(left, short=True))
        elif total <= done:
            eta = "all sent, finishing"
        line = "  ".join(x for x in [head, _bar(done, total), pct, done_total, rate(speed) if speed else "", eta,
                                     quiet if total <= done else "", elapsed] if x)
    out = [line]
    if peers:
        parts = []
        for peer in sorted(peers, key=lambda p: (-p.get("total", 0), str(p.get("name")))):
            state = "done" if peer.get("total") and peer.get("done", 0) >= peer["total"] else (
                "stalled" if peer.get("stalled") else "ok")
            parts.append("%s %s %s %s" % (peer.get("way") or "with", peer.get("name"),
                                          amount(peer.get("done", 0), peer.get("total", 0)), state))
        out.append("      " + "   ".join(parts))
    return out


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

    tally = {}
    for o in done:
        tally.setdefault(said(o), []).append(o["node"])
    if check:
        counts = [("%s on %s" if len(word.split()) > 2 and word != "would clean up" else "%s %s") % (word, plural(len(names), "node"))
                  for word, names in tally.items()]
    else:
        counts = ["%d %s" % (len(names), word) for word, names in tally.items()]
    counts += ["%d failed" % len(failed)] if failed else []
    counts += ["%d not touched" % len(skipped)] if skipped else []
    verdict = "FAILED" if failed else ("CHECK" if check else ("DONE" if done else "NOTHING TO DO"))
    head = "  ".join(x for x in [verdict, operation, cluster, ", ".join(counts) or "nothing to do"] if x)
    if seconds is not None and not check:
        head += " (%s)" % duration(seconds)
    if check:
        head += "   nothing was changed"
    lines = [head]

    rows = []
    groups = {}
    for o in done:
        groups.setdefault(said(o), []).append(o)
    for word, members in groups.items():
        times = [o["seconds"] for o in members if o.get("seconds") is not None]
        middle = _median(times) if times else None
        slow = [o for o in members if middle and len(times) > 1 and o.get("seconds") is not None
                and o["seconds"] > slow_factor * middle]
        usual = [o for o in members if o not in slow]
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


def perm_lines(changes, indent="  "):
    """changes: [{file, before: {owner, group, mode}, after: {...}}] ->
    "cassandra.yaml  owner/group/mode  root/cassandra 0640 -> cassandra/cassandra 0640"."""
    def text(p):
        p = p or {}
        return "%s/%s %s" % (p.get("owner", "?"), p.get("group", "?"), p.get("mode", "?"))
    rows = [[c.get("file", ""), "owner/group/mode", "%s -> %s" % (text(c.get("before")), text(c.get("after")))]
            for c in changes or [] if text(c.get("before")) != text(c.get("after"))]
    return _table(rows, indent)


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


def seeds_reload_todo(failed, command_line):
    """The TO DO item when nodetool reloadseeds failed on nodes after their
    seed list was written (seeds_apply.yml): command_line (change_seeds)
    reloads it once the cause is fixed. None when none failed."""
    failed = list(failed or [])
    if not failed:
        return None
    return {"text": "reload the seed list on %s: nodetool reloadseeds failed (the WARNING above says why; a node that"
                    " cannot load its cassandra.yaml again will not start either: fix the file before any restart),"
                    " then" % nodes(failed, keep_order=True),
            "command": command_line}


def removed_todo(removed, reimport_from, inventory=None, hosts=None, inventory_dir=None, cwd=None, in_git=None,
                 unreachable=None, imported=True, reimport_hosts=None, first=None):
    """The TO DO once nodes left the ring (decommission_node, topology):
    empty them (reset_node), then drop them from the inventory by a
    re-import of the cluster from the inventory itself (its connection
    variables and its logins, vaulted ones too), --check --diff first, then
    the commit, only when the inventory dir is in a git work tree.
    reimport_from: a node that stays (none: no re-import); inventory, hosts:
    the run's (none: left out of the commands); reimport_hosts: the
    cluster's group for the re-import's -e cassandra_hosts ('': the import
    takes the cluster without it, CASSANDRA_CLUSTER or the inventory's only
    cluster; None: hosts); inventory_dir: where the cluster's inventory is
    (import_cluster_dir, given when it is not the default inventories);
    unreachable: the ones not answering, left out of the reset (the
    re-import drops them too); imported: the inventory is import_cluster's
    (else: remove them from it by hand, no re-import); first: items said
    before (the seeds to put in the inventory)."""
    removed = list(removed or [])
    first = [i for i in first or [] if i]
    if not removed:
        return todo(first)
    if reimport_hosts is None:
        reimport_hosts = hosts
    names = nodes(removed, keep_order=True)
    reset = [n for n in removed if n not in (unreachable or [])]
    items = first + [{"text": "empty %s before reusing %s (%s data is left in place)" % (
        nodes(reset, keep_order=True), "the host" if len(reset) == 1 else "the hosts", "its" if len(reset) == 1 else "their"),
        "command": command("reset_node", inventory=inventory, hosts=hosts, cwd=cwd,
                           extra={"cassandra_target_nodes": ",".join(reset)})}] if reset else first
    shown_dir = path_from(str(inventory_dir), cwd) if inventory_dir else ""
    reimport = [extra_var("import_cluster_force", "true")]
    if shown_dir and os.path.normpath(shown_dir) != "inventories":
        reimport.append(extra_var("import_cluster_dir", shown_dir))
    if not imported or not reimport_from:
        items.append("remove %s from the inventory (or leave %s marked cassandra_node_state: absent)" % (
            names, "it" if len(removed) == 1 else "them"))
    else:
        # read with the inventory: its connection variables and logins (secrets.yml) come with it
        items.append({"text": "drop %s from the inventory: re-import the cluster with its inventory, the changes shown"
                              " first" % names,
                      "command": command("import_cluster", inventory=inventory, hosts=reimport_hosts, cwd=cwd,
                                         extra=reimport + ["--check", "--diff"])})
        items.append({"text": "then write them",
                      "command": command("import_cluster", inventory=inventory, hosts=reimport_hosts, cwd=cwd,
                                         extra=reimport)})
    if shown_dir and (in_git if in_git is not None else in_git_work_tree(str(inventory_dir))):
        # only the inventory, whatever else is staged
        items.append({"text": "commit it", "command": "git add %s && git commit -m %s -- %s" % (
            shlex.quote(shown_dir), shlex.quote("Inventory: %s removed" % ", ".join(removed)), shlex.quote(shown_dir))})
    return todo(items)


SEEDS_MIN, SEEDS_MAX = 2, 3  # per datacenter (OUTPUT Q7)


def _seed_pick(racks, target):
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
    (rack_b)  ok", a warning's "WARNING  Seeds  dc2  ..."), dcs: [{dc, level
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
        result["lines"].append("WARNING  " + line if level == "warning" else line)
        result["dcs"].append({"dc": dc, "level": level, "text": text})
        if level == "warning":
            result["problems"].append("%s: %s" % (dc, text))
        elif level == "note":
            result["notes"].append("%s: %s" % (dc, text))
        target = min(len(members), SEEDS_MAX, max(SEEDS_MIN, len(racks)))
        keep = seeds if level != "warning" else _seed_pick(racks, target)
        suggested += [n["address"] for n in keep]
    result["suggested"] = suggested if result["problems"] else []
    return result
