# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What is a secret for the controller: what the output hides (as ****) and what
import_cluster files in secrets.yml. Pure Python, no Ansible.

Two other rules exist, with fewer words (tests/unit/plugins/plugin_utils/
test_secret_rules_compared.py pins where the three differ):
- plugin_utils/cassandra_config_read.py (SECRET_NAME, SECRET_WORD, masked): its
  port that runs on the node (the config diff; apply_config's preview too);
- plugins/filter/cassandra_medusa.py (SECRET_KEY): medusa.ini's keys.

Its names:
- the rule: which names are a secret's (SECRET_WORDS and the lists after it);
- a value: hidden (is it, or does it hold, a secret?), file_mode;
- a text: mask (every secret's value in it as ****, the lines it goes on to
  too), mask_lines, mask_hunks; USERINFO (a URL's password);
- for import_cluster, which takes values out of lines: SECRET_NAME,
  SECRET_VALUE, SECRET_WORD.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

MASK = "****"

# --- the rule: which names are a secret's --------------------------------------------------

# A word that makes a name a secret's wherever it is in it: db_password, sse_c_key and access_key (Medusa's),
# my_api_token, a passphrase, an encryption_key
SECRET_WORDS = ("password", "passwd", "secret", "private_key", "sse_c_key", "access_key", "key_material", "auth_token",
                "api_?token", "api_?key", "access_token", "bearer_token", "refresh_token", "session_token",
                "private_token", "passphrase", "encryption_key", "ca_key")
# A short one: at the end of a variable's or setting's name (cassandra_pw; _kspw, _tspw: a keystore's and a
# truststore's in a JVM option); inside a line, anywhere in a name
SHORT_WORDS = ("_kspw", "_tspw", "_pw", "_pass")
# A name that is a secret's on its own only, never inside a longer one (the HTTP headers, an authorization or a
# token: the Prometheus exporter's openTelemetry headers; not num_tokens, not AuthorizationProxy)
OWN_NAMES = ("authorization", "headers", "token")
# The endings of a variable's or setting's name that make it a secret's (not initial_token, not PWD: the shell's);
# its whole name pw or pass too
KEY_ENDINGS = ("credentials?", "(?<!initial)_token", "_pwd")
KEY_OWN_NAMES = ("pw", "pass")
# The same endings for a name inside a line, after a _ or a . (not credentials_validity, not $PWD, not a path's
# /credentials; with its = or : only)
LINE_ENDINGS = ("(?<!initial)[._]token", "[._]pwd", "[._]pass", "[._]credentials?")

# a variable's or setting's name (re.search, any case)
_KEY_NAME = re.compile("|".join(SECRET_WORDS + tuple(w + "$" for w in SHORT_WORDS + KEY_ENDINGS))
                       + "|^(?:" + "|".join(KEY_OWN_NAMES + OWN_NAMES) + ")$", re.I)

# --- a secret inside a line of text --------------------------------------------------------

# A secret inside a line of text (a config file line, a JVM option): its name (as above, inside a longer one;
# not a path: a name ending in file or path, e.g. -Dcom.sun.management.jmxremote.password.file; not a YAML file's
# name: group_vars/prod/secrets.yml:), a quote between the name and the colon (JSON, Python), then its value
_ANYWHERE = "|".join(SECRET_WORDS + SHORT_WORDS)
_OWN_NAME = r"(?<![\w.-])(?:" + "|".join(OWN_NAMES) + r")(?![\w.-])"
_LINE_END = r"(?:" + "|".join(LINE_ENDINGS) + r")(?![\w.-])"
_NAME_IN_LINE = (r"(?<![\w.-])[\w.-]*(?:" + _ANYWHERE + "|" + _LINE_END + "|" + _OWN_NAME
                 + r")[\w.-]*(?<![._-]file)(?<![._-]path)(?<!\.yml)(?<!\.yaml)[\"']?")
# its name and the = or : after it (import_cluster takes the value out of a line)
SECRET_NAME = r"(?i)(" + _NAME_IN_LINE + r"\s*[:=]\s*)"
_SECRET_KEY = re.compile(SECRET_NAME)
_NAME_AND_VALUE = (SECRET_NAME +
                   r"(\"(?:[^\"\\]|\\.)*\"?|'(?:[^']|'')*'?|")
# an unquoted value runs to the end of the line (a comment aside), as a YAML plain scalar or a JVM option may hold
# a comma; inside a flow mapping or JSON ({...} before it) it ends at the next comma or brace
SECRET_VALUE = re.compile(_NAME_AND_VALUE + r"\S.*?(?=\s+#|$))", re.M)
_FLOW_VALUE = re.compile(_NAME_AND_VALUE + r"\S.*?(?=\s+#|,\s|[,}]|$))", re.M)
# nodetool -pw, --password; -p only after cqlsh (elsewhere a port or mkdir -p)
_OPTION = re.compile(r"((?:^|\s)(?:-pw|--password)\s+|\bcqlsh\b[^\n]*?\s-p\s+)(\S+)", re.M)
# the text of an XML element of a secret's name (logback.xml: <password>...</password>)
# (its text up to its own closing tag: CDATA sections and comments, closed or not, child elements too)
_ELEMENT_TEXT = r"<!\[CDATA\[(?:.*?\]\]>|.*)|<!--(?:.*?-->|.*)"
_ELEMENT = re.compile(r"(?i)(<(?P<name>" + _NAME_IN_LINE + r")(?:\s[^<>]*)?>)(\s*(?!</(?P=name)\s*>)(?:" + _ELEMENT_TEXT
                      + r"|\S)(?:" + _ELEMENT_TEXT + r"|(?!</(?P=name)\s*>).)*)")

# the password of a URL's userinfo (scheme://user:password@host: an HTTP proxy, an appender's URL), up to:
# - the last @ before a blank, when the URL opens with a quote and its word goes on after the closing quote
#   (neither a blank nor one of ; & | < > ( ) after it: the shell's 'http://bob:pw@'rd@host' has pw@rd)
# - else the last @ before a blank or that quote, when a / comes before it (a password may hold a /; a few
#   scheme://host:port/path?x=a@b are masked too)
# - else the last @ before a blank, a / or that quote (a password may hold an @, a ? or a #)
USERINFO = re.compile(r"(?P<head>(?P<q>['\"])?[A-Za-z][\w+.-]*://(?P<user>[^\s/:@'\"<>{}]+):)(?P<password>"
                      r"(?(q)(?=(?:(?!(?P=q))\S)*(?P=q)[^\s;&|<>()])\S+|(?!))"
                      r"|(?=(?:(?!(?P=q))\S)*/(?:(?!(?P=q))\S)*@)(?:(?!(?P=q))\S)+"
                      r"|(?:(?!(?P=q))[^\s/])+)@")

# a line without any of these words holds no secret setting (looked for first: the patterns above are slow)
SECRET_WORD = re.compile(r"(?i)" + _ANYWHERE + r"|(?:" + _LINE_END + "|" + _OWN_NAME + r")[\"']?\s*[:=]")


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
    keys = list(_SECRET_KEY.finditer(code))
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
        line = USERINFO.sub(r"\g<head>" + MASK + "@", line)
        if not SECRET_WORD.search(line):
            state = None
            out.append(_OPTION.sub(r"\1" + MASK, line))
            continue
        state, continued = _goes_on(line), _odd_backslashes(line)
        line = _ELEMENT.sub(r"\1" + MASK, line)
        line = SECRET_VALUE.sub(_mask_line, _FLOW_VALUE.sub(_mask_flow, line))
        out.append(_OPTION.sub(r"\1" + MASK, line))
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


# --- a value -------------------------------------------------------------------------------

# a dict key that is a file's name with a file mode as its value (cassandra_config_file_permissions'
# jmxremote.password: "0400"): not a secret's name; ssl.password: "x" still is
FILE_NAME = re.compile(r"^[\w-]+(\.[\w-]+)*\.[A-Za-z]{2,12}$")
MODE = re.compile(r"^0?[0-7]{3,4}$")


def file_mode(key, value):
    """True when key is a file's name and value a file mode (0400, 640): never a secret."""
    return bool(FILE_NAME.match(str(key))) and not isinstance(value, bool) and bool(MODE.match(str(value)))


def hidden(key, value):
    """True when value of key is (or holds) a secret: the output shows it as ****, import_cluster writes it to
    secrets.yml. A secret's name (the rule above; not a path: cassandra_jmx_password_file; not a file's name with a
    mode as its value, file_mode), or a secret inside the value (a key: value, key=value, -pw value in a string, a
    dict or list that holds one); never an empty value."""
    if isinstance(value, dict):
        return any(hidden("" if file_mode(k, v) else str(k), v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return bool(_KEY_NAME.search(key)) or any(hidden(key, v) for v in value)
    if value == "" or value is None:
        return False  # e.g. a password variable set to "" to leave it out
    if isinstance(value, str) and mask(value) != value:
        return True  # e.g. a unit's JVM_EXTRA_OPTS=-Djavax.net.ssl.keyStorePassword=...
    return bool(_KEY_NAME.search(key)) and not key.endswith("_file")
