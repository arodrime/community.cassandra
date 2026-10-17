# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output of the collection's filters, written once: every
function returns text lines (or a string) that a debug task prints, nothing
else. Pure Python, no Ansible.

- counts and times: plural, duration;
- node lists: nodes (node1..node5, node7), full_list;
- warnings: warning, warning_lines (each once, a blank line between two),
  spaced paragraphs.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

# A blank line between two paragraphs of one block: a single space (the
# default callback's yaml format strips it). An empty line "" between two
# blocks of a plan, a screen or a recap is a rule under the ops callback;
# this one stays blank there.
PARAGRAPH = " "


def plural(number, word, many=None):
    """"1 node", "3 nodes"; many: the plural when it is not word + s."""
    return "%d %s" % (number, word if number == 1 else (many or word + "s"))


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


def spaced(paragraphs):
    """Paragraphs (each a list of lines) as one block, a PARAGRAPH line
    between two, the empty ones left out."""
    lines = []
    for paragraph in paragraphs:
        if paragraph:
            lines += ([PARAGRAPH] if lines else []) + list(paragraph)
    return lines
