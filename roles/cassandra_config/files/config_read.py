# -*- coding: utf-8 -*-
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# What cassandra_config reads on a node, a step at a time: one run of this
# script per step instead of a task (a round trip) per file or directory. The
# role runs it with the node's Python (python -c, a JSON request on stdin, a
# JSON answer on stdout), its results shaped as the tasks they replace
# registered them (ansible.builtin.stat, slurp, command), so the role's
# templates read them unchanged. Read only. Python 2.7 and 3.6+, no PyYAML
# needed (used when there, to check the new cassandra.yaml).
#   node:    the files and directories the role looks at, the live cassandra.yaml,
#            the Cassandra package's version, whether its unit runs
#   dirs:    the system keyspace in the candidate data dirs, the dirs tried as
#            Cassandra's account, the data dirs' subdirs another account owns
#   compare: the new files (rendered on the controller) checked and diffed
#            against the live ones (secrets masked), the live ones that differ,
#            the settings a joined node must keep

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import base64
import difflib
import errno
import grp
import io
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import traceback

PY2 = sys.version_info[0] == 2


def fs(path):
    """A path as the stat module takes it (type path), in bytes: a C locale
    (LC_ALL=C) cannot encode a path that is not ASCII under Python 3.6."""
    path = os.path.expanduser(os.path.expandvars(path))
    return path.encode("utf-8") if PY2 else path.encode("utf-8", "surrogateescape")


def text(value):
    return value.decode("utf-8", "replace" if PY2 else "surrogateescape") if isinstance(value, bytes) else value


def stat_result(path, follow=False):
    """What ansible.builtin.stat registers: {'stat': {...}}, {'stat': {'exists': False}} when
    the path is not there, {'failed': True, 'msg': ...} when it fails (its strerror)."""
    b_path = fs(path)
    try:
        st = os.stat(b_path) if follow else os.lstat(b_path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return {"stat": {"exists": False}}
        return {"failed": True, "msg": e.strerror}
    mode = st.st_mode
    out = {"exists": True, "path": os.path.expanduser(os.path.expandvars(path)), "mode": "%04o" % stat.S_IMODE(mode),
           "isdir": stat.S_ISDIR(mode), "ischr": stat.S_ISCHR(mode), "isblk": stat.S_ISBLK(mode), "isreg": stat.S_ISREG(mode),
           "isfifo": stat.S_ISFIFO(mode), "islnk": stat.S_ISLNK(mode), "issock": stat.S_ISSOCK(mode),
           "uid": st.st_uid, "gid": st.st_gid, "size": st.st_size, "inode": st.st_ino, "dev": st.st_dev, "nlink": st.st_nlink,
           "atime": st.st_atime, "mtime": st.st_mtime, "ctime": st.st_ctime,
           "wusr": bool(mode & stat.S_IWUSR), "rusr": bool(mode & stat.S_IRUSR), "xusr": bool(mode & stat.S_IXUSR),
           "wgrp": bool(mode & stat.S_IWGRP), "rgrp": bool(mode & stat.S_IRGRP), "xgrp": bool(mode & stat.S_IXGRP),
           "woth": bool(mode & stat.S_IWOTH), "roth": bool(mode & stat.S_IROTH), "xoth": bool(mode & stat.S_IXOTH),
           "isuid": bool(mode & stat.S_ISUID), "isgid": bool(mode & stat.S_ISGID),
           "readable": os.access(b_path, os.R_OK), "writeable": os.access(b_path, os.W_OK),
           "executable": os.access(b_path, os.X_OK)}
    for attr, key in (("st_blocks", "blocks"), ("st_blksize", "block_size"), ("st_rdev", "device_type"), ("st_flags", "flags"),
                      ("st_gen", "generation"), ("st_birthtime", "birthtime")):
        if hasattr(st, attr):  # platform dependent, as the module adds them
            out[key] = getattr(st, attr)
    if hasattr(st, "st_blocks"):  # as ansible-core 2.21+'s stat
        out["disk_usage_bytes"] = st.st_blocks * 512
    if out["islnk"]:
        out["lnk_source"] = text(os.path.realpath(b_path))
        out["lnk_target"] = text(os.readlink(b_path))
    try:
        out["pw_name"] = pwd.getpwuid(st.st_uid).pw_name
    except (TypeError, KeyError):
        pass
    try:
        out["gr_name"] = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, ValueError, OverflowError):
        pass
    return {"stat": out}


def run(argv, stdin=None):
    """(rc, stdout, stderr) of a command, rc None when it cannot be run."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else None, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        return None, "", str(e)
    out, err = proc.communicate(stdin)
    return proc.returncode, text(out), text(err)


def lines(output):
    """stdout_lines as a command task registers them."""
    return output.rstrip("\r\n").splitlines()


def read_b64(path):
    """The base64 of a file as slurp registers it, None when slurp fails (missing, unreadable)."""
    try:
        with open(fs(path), "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except (IOError, OSError):
        return None


# The fstab mount points not mounted: a dir there would land on the filesystem below
UNMOUNTED = """set -o pipefail
command -v mountpoint >/dev/null || exit 0
findmnt --fstab --noheadings --output TARGET | while read -r t; do
  case "$t" in /?*) mountpoint -q "$t" || echo "$t";; esac
done
"""

# ansible.builtin.service_facts, for the cassandra unit only: its state is running when
# systemd lists it with the SUB state running (BAD_STATES as the module reads them)
BAD_STATES = frozenset(["not-found", "masked", "failed"])


def cassandra_state():
    """The state service_facts gives cassandra.service: 'running', '' when systemd
    does not list it running; None when systemd is not the init (service_facts reads it)."""
    try:
        with open("/proc/1/comm") as f:
            if "systemd" not in f.read():
                return None
    except IOError:
        return None
    rc, out, dummy = run(["systemctl", "list-units", "--no-pager", "--type", "service", "--all", "cassandra.service"])
    state = ""
    for line in out.split("\n") if rc == 0 else []:
        if ".service" not in line:
            continue
        fields = line.split()
        for bad in BAD_STATES:
            if bad in fields:
                fields = fields[1:]
                break
        if fields and fields[0] == "cassandra.service":
            state = "running" if len(fields) > 3 and fields[3] == "running" else "stopped"
    return state


def package_version():
    """The version of the cassandra package installed (rpm, else dpkg), '' when none."""
    rc, out, dummy = run(["rpm", "-q", "--qf", "%{VERSION}\\n", "cassandra"])
    if rc == 0 and out.strip():
        return out.strip().split("\n", 1)[0].strip()
    rc, out, dummy = run(["dpkg-query", "-W", "-f", "${db:Status-Status} ${Version}\\n", "cassandra"])
    if rc == 0:
        for line in out.split("\n"):
            words = line.split()
            if len(words) == 2 and words[0] == "installed":
                return words[1]
    return ""


def step_node(req):
    errors = []

    def checked(path, follow=False):
        res = stat_result(path, follow)
        if res.get("failed"):
            errors.append("%s: %s" % (path, res["msg"]))
        return res

    yaml_stat = checked(req["yaml"])
    try:  # as python -c "import yaml" tells it
        import yaml  # noqa: F401 pylint: disable=unused-import
        pyyaml = True
    except Exception:  # pylint: disable=broad-except
        pyyaml = False
    rc, out, dummy = run(["/bin/bash", "-c", UNMOUNTED])
    answer = {
        "dirs": [dict(checked(p), item=p) for p in req["dirs"]],
        "unmounted": lines(out) if rc is not None else [],
        "yaml_stat": yaml_stat.get("stat", {"exists": False}),
        "yaml": read_b64(req["yaml"]) if yaml_stat.get("stat", {}).get("exists") else None,
        "pyyaml": pyyaml,
        "conf_target": checked(req["conf_target"]) if req.get("conf_target") else None,
        "keep": [dict(checked(p), item=n) for n, p in req["keep"]],
        "files": [dict(checked(p, True), cassandra_config_file=n) for n, p in req["files"]],
        "links": [n for n, p in req["files"] if os.path.islink(fs(p))],  # the template task replaces them
        "dir_stats": [dict(checked(p, True), item=[k, p]) for k, p in req["dir_stats"]],
        "package": package_version(),
        "running": cassandra_state(),
    }
    answer["errors"] = errors
    return answer


FOREIGN = 'find -H "$1" -mindepth 1 -maxdepth 2 -name lost+found -prune -o -type d ! -user "$2" -print 2>/dev/null | head -n 4'


def step_dirs(req):
    user = str(req["user"])  # a uid given as a number too
    tries = []
    for path in req["tries"]:  # the mode bits miss the account's other groups and ACLs
        rc, dummy, dummy2 = run(["runuser", "-u", user, "--", "test", "-r", path, "-a", "-w", path, "-a", "-x", path])
        tries.append(2 if rc is None else rc)  # no runuser: as the command task fails (rc 2)
    return {
        "system": [dict(stat_result(d + "/system", True), item=d) for d in req["system"]],
        "tries": tries,
        "foreign": [lines(run(["sh", "-c", FOREIGN, "sh", d, user])[1]) for d in req["foreign"]],
    }


def validate(new_text):
    """The error of the new cassandra.yaml, None when valid or no PyYAML: its line
    only, never the text (it may hold a password)."""
    try:
        import yaml
    except Exception:  # pylint: disable=broad-except
        return None
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return "the new cassandra.yaml is not valid YAML (line %s)" % (mark.line + 1 if mark else "?")
    except Exception as e:  # pylint: disable=broad-except
        return "the new cassandra.yaml could not be read as YAML (%s)" % type(e).__name__  # e.g. a date out of range
    return None


def universal(value):
    """A text as io.open reads a file (universal newlines)."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


# values of secret keys never reach the output: all of the line after the key, and the lines the value goes on
# to (a port of the collection's cassandra_output.mask: a YAML block key: |, a plain value on more indented lines, the
# value under key:, a quote or a flow [ { left open, a line ending with a backslash)
SECRET_NAME = (r"(?<![\w.-])[\w.-]*(?:password|passwd|secret|private_key|sse_c_key|access_key|key_material"
               r"|auth_token|ca_key|_kspw|_tspw|_pw|_pass)[\w.-]*(?<![._-]file)(?<![._-]path)")
SECRET = re.compile(r"(?i)(" + SECRET_NAME + r"[\"']?[ \t]*[:=][ \t]*)\S[^\r\n]*")
SECRET_KEY = re.compile(r"(?i)(" + SECRET_NAME + r"[\"']?\s*[:=]\s*)")
# the text of an XML element of a secret's name (logback.xml: <password>...</password>)
# (its text up to its own closing tag: CDATA sections and comments, closed or not, child elements too)
ELEMENT_TEXT = r"<!\[CDATA\[(?:.*?\]\]>|.*)|<!--(?:.*?-->|.*)"
SECRET_ELEMENT = re.compile(r"(?i)(<(?P<name>" + SECRET_NAME + r")(?:\s[^<>]*)?>)(\s*(?!</(?P=name)\s*>)(?:" + ELEMENT_TEXT + r"|\S)"
                            r"(?:" + ELEMENT_TEXT + r"|(?!</(?P=name)\s*>).)*)")

SECRET_WORD = re.compile(r"(?i)password|passwd|secret|private_key|sse_c_key|access_key|key_material|auth_token"
                         r"|ca_key|_kspw|_tspw|_pw|_pass")


def scan(text, depth=0, quote=None, brackets="[{"):
    """(depth, quote) after text: the brackets left open and the quote (" or ')
    left open; a quote opens only where a value starts; a comment ends the text."""
    return scan_to(text, depth, quote, brackets)[:2]


def scan_to(text, depth=0, quote=None, brackets="[{"):
    """scan, and where the text ends: its comment's #, else its length."""
    closers = "".join({"[": "]", "{": "}", "(": ")"}[b] for b in brackets)
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


def odd_backslashes(text):
    text = text.rstrip("\r")  # a CRLF line
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


def in_flow(before):
    before = before.replace("{{", "").replace("}}", "")
    return before.count("{") > before.count("}")


def goes_on(line):
    """How the value of the line's secret (its first one whose value goes on) goes on to the next lines, else None."""
    if line.lstrip().startswith("#"):
        return None
    code = line[:scan_to(line)[2]]  # the line without its comment: a name in it is no key
    keys = list(SECRET_KEY.finditer(code))
    # each key tried costs a scan of the line: the first ones, then the last (a line of many: a JSON file)
    for key in (keys[:16] + keys[-1:]) if len(keys) > 17 else keys:
        state = goes_on_from(line, code, key)
        if state is not None:
            return state
    return ("backslash",) if odd_backslashes(line) else None


def goes_on_from(line, code, key):
    """goes_on for one secret key of the line, None when its value ends on it."""
    rest, colon, flow = code[key.end():], key.group(1).rstrip()[-1:] == ":", in_flow(line[:key.start()])
    value = re.sub(r"^(?:[!&]\S*(?:[ \t]+|$))+", "", rest) if colon else rest  # after a YAML tag or anchor
    before = scan(line[:key.end()])[1]
    if flow:
        depth, quote = scan(line.replace("{{", "").replace("}}", ""))
        opened = scan(rest, 0, before)[0] > 0
        ended = re.search(r"[,}\]]", re.sub(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'", "", rest))
        if quote or (depth > 0 and (opened or not ended)):
            return ("scan", depth, quote, "[{")
    elif value[:1] and (value[:1] in "\"'[{(" or (not (colon and not before) and value[:1] == "$")):  # the shell's $' $( $"
        brackets = "[{" if colon else "[{("
        depth, quote = scan(value, 0, before, brackets)
        if depth > 0 or quote:
            return ("scan", depth, quote, brackets)
    elif colon and not before:
        return ("indent", key.start(1), not value.strip())
    return None


def still_on(state, line):
    """The state after line, a line of the secret's value (False: not one)."""
    if state[0] == "indent":
        indent = len(line) - len(line.lstrip())
        if not line.strip() or indent > state[1] or (state[2] and indent == state[1] and
                                                     re.match(r"-(\s|$)", line.lstrip())):
            return state
        return False
    if state[0] == "scan":
        depth, quote = scan(line, state[1], state[2], state[3])
        return ("scan", depth, quote, state[3]) if depth > 0 or quote else None
    return False


def masked(lines):
    """The lines of a file (their line ends kept), secrets masked."""
    out, state, continued = [], None, False
    for line in lines:
        text = line.rstrip("\r\n")
        end = line[len(text):]
        indent = len(text) - len(text.lstrip())
        if state is not None:
            after = still_on(state, text)
            if after is False and continued:
                after = state
            if after is None and odd_backslashes(text):
                after = ("backslash",)
            if after is not False:
                out.append((text[:indent] + "****" if text.strip() else text) + end)
                state, continued = after, odd_backslashes(text)
                continue
        if not SECRET_WORD.search(text):
            state = None
            out.append(line)
            continue
        state, continued = goes_on(text), odd_backslashes(text)
        out.append(SECRET.sub(r"\1****", SECRET_ELEMENT.sub(r"\1****", text)) + end)
    return out


def unified_range(start, stop):
    """A hunk's range as difflib's unified_diff writes it."""
    if stop - start == 1:
        return "%d" % (start + 1)
    return "%d,%d" % (start + 1 if stop > start else start, stop - start)


def diff(live, new_text):
    """(rc, masked unified diff) of the live file against the new text: rc 1 when they differ."""
    # UTF-8 whatever the locale; newline="": a CRLF line differs from the LF one (bash reads X=8G\r as "8G\r")
    a = io.open(fs(live), encoding="utf-8", errors="replace", newline="").read().splitlines(True) \
        if os.path.exists(fs(live)) else []
    b = new_text.splitlines(True)
    # unified_diff's hunks of the files, their lines shown masked as in the whole file (a secret's value spans lines)
    shown_a, shown_b = masked(a), masked(b)
    d = []
    for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(3):
        if not d:
            d += ["--- %s (live)\n" % live, "+++ (new)\n"]
        old, new = unified_range(group[0][1], group[-1][2]), unified_range(group[0][3], group[-1][4])
        d.append("@@ -%s +%s @@\n" % (old, new))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                d += [" " + x for x in shown_a[i1:i2]]
                continue
            d += ["-" + x for x in shown_a[i1:i2]] if tag in ("replace", "delete") else []
            d += ["+" + x for x in shown_b[j1:j2]] if tag in ("replace", "insert") else []
    d = [x if x.endswith(("\n", "\r")) else x + "\n" for x in d]  # a last line without one: not joined to the next
    out = "".join(d)
    return (1 if d else 0), out.encode("utf-8", "replace").decode("utf-8")  # a path not in UTF-8 too


def file_text(path):  # UTF-8 whatever the locale
    return io.open(fs(path), encoding="utf-8", errors="replace").read()


def scalar(v):
    """A YAML scalar's text: quotes undone, trailing comment dropped."""
    m = re.match(r"'((?:[^']|'')*)'|\"((?:[^\"\\]|\\.)*)\"", v)
    if m:
        if m.group(1) is not None:
            return m.group(1).replace("''", "'")
        return re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), m.group(2))
    return re.sub(r"\s+#.*$", "", v)


IDENTITY_YAML = r"^(cluster_name|num_tokens|partitioner|endpoint_snitch|storage_compatibility_mode):[ \t]*(.*?)[ \t]*$"
IDENTITY_RACKDC = r"^[ \t]*(dc|rack|dc_suffix|ec2_naming_scheme)[ \t]*=[ \t]*(.*?)[ \t]*$"


def identity(req):
    """The settings a joined node must keep that would change: ["key: live -> new"].
    Top-level keys and rackdc properties, plain text (no YAML parser). req: live_yaml,
    live_rackdc (paths), new_yaml, new_rackdc ({'text'} or {'path'}: a file kept), snitches."""
    def read(source, pattern):  # properties: no inline comment and no quoting, as the snitch reads them
        get = scalar if pattern == IDENTITY_YAML else lambda v: v.strip()
        body = universal(source["text"]) if "text" in source else file_text(source["path"])
        return dict((k, get(v.strip())) for k, v in re.findall(pattern, body, re.M))

    def reads_rackdc(snitch):  # a name without a dot is in that package
        return re.sub(r"^org\.apache\.cassandra\.locator\.", "", snitch or "") not in req["snitches"]

    live, new = {}, {}
    for live_file, new_file, pattern in ((req["live_yaml"], req["new_yaml"], IDENTITY_YAML),
                                         (req["live_rackdc"], req["new_rackdc"], IDENTITY_RACKDC)):
        if os.path.exists(fs(live_file)):  # no live file: nothing to keep
            live.update(read({"path": live_file}, pattern))
            new.update(read(new_file, pattern))
            if pattern == IDENTITY_YAML:  # absent: Cassandra's default
                live.setdefault("num_tokens", "1")
                new.setdefault("num_tokens", "1")
                if "storage_compatibility_mode" in new:  # 5.0
                    live.setdefault("storage_compatibility_mode", "CASSANDRA_4")
            else:
                # dc= and rack=: the node's only for a snitch that reads them
                if not any(reads_rackdc(s.get("endpoint_snitch")) for s in (live, new)):
                    for d in (live, new):
                        d.pop("dc", None)
                        d.pop("rack", None)
                # cloud snitches: suffix and naming scheme of the dc/rack names
                for d in (live, new):
                    d.setdefault("dc_suffix", "")
                    d.setdefault("ec2_naming_scheme", "standard")
    # 5.0 storage mode: one step forward at a time from what the file says, never back
    steps = [("CASSANDRA_4", "UPGRADING"), ("UPGRADING", "NONE")]
    mode = "storage_compatibility_mode"
    if (live.get(mode), new.get(mode)) in steps and re.search(r"(?m)^" + mode + ":", file_text(req["live_yaml"])):
        live[mode] = new[mode]
    return ["%s: %s -> %s" % (k, live.get(k, "(unset)"), new.get(k, "(unset)"))
            for k in sorted(set(live) | set(new)) if live.get(k) != new.get(k)]


def step_compare(req):
    new = req["files"]  # {name: text}, compared in the order of names
    answer = {"yaml_error": validate(universal(new["cassandra.yaml"])) if "cassandra.yaml" in new else None,
              "preview": [], "live": {}, "identity": None}
    for name in req["names"]:
        new_text = new[name]
        live = req["conf_dir"] + "/" + name
        try:
            rc, out = diff(live, new_text)
        except Exception:  # pylint: disable=broad-except
            rc, out = 1, ""  # as the diff command that fails (rc 1, nothing on stdout): not a change
        answer["preview"].append({"cassandra_config_file": name, "rc": rc, "stdout": out.rstrip("\r\n")})
        if rc == 1 and req["compare"]:  # its settings compared on the controller
            answer["live"][name] = read_b64(live)
    if req.get("identity"):
        try:
            answer["identity"] = {"rc": 0, "changes": identity(req["identity"])}
        except Exception:  # pylint: disable=broad-except
            answer["identity"] = {"rc": 1, "error": traceback.format_exc().strip().rsplit("\n", 1)[-1]}
    return answer


STEPS = {"node": step_node, "dirs": step_dirs, "compare": step_compare}


def main():
    req = json.loads(sys.stdin.read())
    try:
        answer = STEPS[req["step"]](req)
    except Exception:  # pylint: disable=broad-except
        # said by the role (its task is no_log): the last line of the traceback, never a file's content
        answer = {"error": traceback.format_exc().strip().rsplit("\n", 1)[-1]}
    sys.stdout.write(json.dumps(answer))


if __name__ == "__main__":
    main()
