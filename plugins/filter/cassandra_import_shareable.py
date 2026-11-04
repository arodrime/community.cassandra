# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""A copy of the import's report that can be shared (import_cluster -e import_cluster_report_shareable=true).

cassandra_import_shareable: the report's lines and the import's layout -> {text: the lines of the copy, map: the
    lines of its key}. In the copy the node names become n1, n2.. (in the order of their names), the clusters c1,
    the data centers and racks named after the site dc1, r1.. (numbered past the dc1, r1 already in use), the
    inventory's groups and directories of the site, the addresses IP, the host names and domains that are not a
    node's domain1.., the paths outside the stock Cassandra and OS ones path1.., the URLs url1.., Medusa's bucket,
    prefix and fqdn values and the user names bucket1, user1..; secrets masked (by the report, and again line by
    line). Names that are common words (cassandra, data, a rack "a") stay. Everything else stays: the sections, the
    hand edits and their lines, the versions, the import's own notes and errors, so that someone else can see the
    problems and help fix them. The key (n1 = the real name...) is for the operator only: written next to the
    copy, mode 0600, never in it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import group_chains
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\w)")
# an IPv6 address: a "::" in it, or 4 groups at least (a time, 13:33:20, has 3); its IPv4 tail masked already
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:IP\b|[0-9A-Fa-f]{0,4}(?![\w:]))")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s,;'\"<>)]+")
PATH = re.compile(r"(?<![\w.~/>*-])/[\w.@+-]+(?:/[\w.@+*{}<>-]*)*")
# a path from the home or the current directory (~/ops/x, ./ops, ../ops): the site's own, always masked
RELATIVE_PATH = re.compile(r"(?<![\w/])(?:~[\w.-]*|\.\.?)/[\w.@+*-]+(?:/[\w.@+*-]*)*")
TOKEN = re.compile(r"[\w-]+(?:\.[\w-]+)*")
ADDRESS = re.compile(r"^[\d.:]+$")
# the directories of a stock install (Cassandra's packages and tarball layout, the OS's), matched by whole
# components: a path in one of them is kept
STOCK_DIRS = ("/etc/cassandra", "/etc/default", "/etc/sysconfig", "/etc/security", "/etc/systemd", "/etc/sysctl.d",
              "/etc/sysctl.conf", "/etc/apt", "/etc/yum.repos.d", "/etc/alternatives", "/etc/medusa",
              "/etc/cassandra-medusa", "/etc/os-release", "/etc/hosts", "/etc/fstab", "/etc/environment",
              "/etc/profile.d", "/etc/ssl", "/etc/pki", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/share",
              "/usr/libexec", "/usr/local/bin", "/usr/local/sbin", "/lib", "/lib64", "/bin", "/sbin", "/opt/cassandra",
              "/opt/medusa", "/var/lib/cassandra", "/var/log/cassandra", "/var/log/medusa", "/var/log/audit",
              "/var/log/messages", "/var/log/syslog", "/var/run", "/var/tmp", "/var/cache", "/run", "/proc", "/sys",
              "/dev", "/tmp")
# these alone are kept, what follows them is masked unless a stock directory above
STOCK_TOPS = ("/etc", "/usr", "/opt", "/var", "/var/lib", "/var/log", "/usr/local", "/srv", "/home", "/data")
# what a dotted word that is not a host may be: a file (by its extension), a Java package of a stock part, the
# collection's own names
EXTENSIONS = ("yaml", "yml", "xml", "options", "sh", "properties", "password", "access", "conf", "ini", "txt", "json",
              "cfg", "log", "jar", "py", "service", "j2", "pem", "crt", "key", "jks", "p12", "keystore", "truststore",
              "md", "rst", "repo", "list", "gpg", "asc", "so", "d", "timer", "socket", "target", "env", "orig", "bak",
              "tmp", "map", "gz", "tar", "tgz", "zip", "deb", "rpm", "html")
STOCK_DOTTED = ("org.apache.", "org.slf4j", "org.eclipse.", "org.yaml.", "org.jctools", "ch.qos.", "io.netty",
                "io.dropwizard", "com.codahale", "com.datastax", "com.sun.", "com.google.", "java.", "javax.", "jdk.",
                "sun.", "net.openhft", "cassandra.", "community.", "ansible.", "ansible_collections.")
# a data center or rack named so says nothing of the site (dc1, r2, rack3, a, b...)
GENERIC = re.compile(r"(?i)^((dc|r|rack|zone|az)?[-_]?\d+|[a-z]{1,2})$")
# common words a name may be: never replaced (the report's own text has them)
STOCK_WORDS = ("cassandra", "data", "main", "default", "all", "local", "node", "nodes", "java", "medusa", "system",
               "root", "config", "logs", "log", "server", "inventories", "reports", "inventory", "host", "hosts",
               "group", "the", "on", "in", "of", "and", "or")
# the values a copy masks as well, by the setting's name: Medusa's bucket, prefix and fqdn, the user names
VALUED = re.compile(r"(?i)\b[\w.]*?(bucket_name|bucket|prefix|fqdn|_username|_user|superuser)\b(?:\s+\([^)]*\))?:\s+")
SECRETS_MARK = re.compile(r"(?:[\w.-]+(?:\s+\([^)]*\))?:\s+)?(?:\(in secrets\.yml(?:, value \d+)?\)|\(in a vars file\))")
# a file mode under cassandra_config_file_permissions: never a secret (the masking below would take it for one)
MODE_ROW = re.compile(r"cassandra_config_file_permissions\.[\w.-]+:\s+0?[0-7]{3,4}\b")


def _natural(name):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(name))]


class _Key(object):
    """The names replaced and what replaces them, in the order found (the key of the copy); looked up whatever
    their case. A cluster or a node named by a common word gets a name in the key too, but the word stays in the
    text (quiet): it says nothing of the site, and the report's own text has it."""

    def __init__(self):
        self.names, self.order, self.counts, self.taken, self.kinds, self.quiet = {}, [], {}, set(), {}, set()

    def get(self, real):
        """What replaces real in the text, None when nothing does."""
        return None if str(real).lower() in self.quiet else self.names.get(str(real).lower())

    def name(self, real):
        """The name real has in the key."""
        return self.names.get(str(real).lower())

    def add(self, real, kind, given=None, force=False):
        """real's name in the key (given, else kind and the next free number); a common word only when force (the
        cluster, a node: quiet in the text), else None."""
        real = str(real)
        lower = real.lower()
        if not real or lower in STOCK_WORDS and not force:
            return None
        if lower in self.names:
            return self.names[lower]
        while given is None:  # (numbered past the names in use: a real dc1)
            self.counts[kind] = self.counts.get(kind, 0) + 1
            given = "%s%d" % (kind, self.counts[kind])
            given = None if given in self.taken else given
        if lower in STOCK_WORDS:
            self.quiet.add(lower)
        self.names[lower] = given
        self.kinds[lower] = kind
        self.taken.add(given)
        self.order.append(real)
        return given

    def inside(self, word):
        """word with the names of nodes, clusters, data centers, racks and domains in it replaced, each a whole part
        of it (between -, _, ., / or its ends): db1-old, payments.yml, cass-pay-01.keystore."""
        for real in sorted(self.order, key=len, reverse=True):
            lower = real.lower()
            if (self.kinds.get(lower, "").rstrip("_g") in ("n", "c", "dc", "r", "domain") and lower not in self.quiet
                    and len(real) >= 3 and not ADDRESS.match(real) and lower in word.lower()):
                word = re.sub(r"(?i)(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(real), self.names[lower], word)
        return word

    def lines(self):
        return ["%s = %s" % (self.names[real.lower()], real) for real in self.order]


def _forms(name):
    """A name as the report and the files may write it: as is, with -, _, a space or nothing between its words."""
    words = [w for w in re.split(r"[\s_-]+", str(name)) if w]
    return set([str(name)] + [sep.join(words) for sep in (" ", "_", "-", "")])


def _key(layout, paths=()):
    """The names of the layout -> _Key: the clusters, the nodes (their names, host names, the hosts they were given,
    an address as a name too), the data centers and racks named after the site, the inventory's groups, the dirs of
    paths (the inventory's and the report's as the report shows them, unless the default ones)."""
    key = _Key()
    nodes = sorted(layout.get("nodes") or [], key=lambda n: _natural(n.get("name", "")))
    places = [str(n.get(k)) for n in nodes for k in ("dc", "rack") if n.get(k)]
    key.taken |= set(p for p in places if GENERIC.match(p))  # (a real dc1 keeps its name: the site's ones after it)
    cluster = layout.get("cluster_group") or ""
    given = key.add(cluster, "c", force=True)
    for form in _forms(layout.get("cluster_name") or "") | _forms(cluster):
        if len(form) > 2:
            key.add(form, "c", given)
    for n in nodes:
        given = key.add(n["name"], "n", force=True)
        for alias in (n.get("hostname"), n.get("host"), n.get("ansible_host"), str(n["name"]).split(".", maxsplit=1)[0]):
            if alias and not ADDRESS.match(str(alias)):  # (never a number: an address is masked anyway)
                key.add(alias, "n", given)
    for n in nodes:
        for place, kind in ((n.get("dc"), "dc"), (n.get("rack"), "r")):
            if place and not GENERIC.match(str(place)):
                given = key.add(place, kind)
                for form in _forms(place):  # (eu-west-1 as eu_west_1 too)
                    key.add(form, kind, given)
    # the inventory's groups: <cluster>_<dc>_<rack>, their names as written (lower case, _ for the rest)
    written = sorted(((re.sub(r"[^a-z0-9]+", "_", real.lower()), key.name(real)) for real in places
                      if key.name(real)), key=lambda p: -len(p[0]))
    for chain in group_chains(layout.get("hosts") or {}).values():
        for group in chain:
            if group.startswith(cluster + "_") and not key.name(group):
                rest = "_%s_" % group[len(cluster) + 1:]
                for real, given in written:
                    rest = rest.replace("_%s_" % real, "_%s_" % given)
                rest = rest.strip("_")
                key.add(group, key.name(cluster) + "_g", "%s_%s" % (key.name(cluster), rest) if all(
                    GENERIC.match(p) for p in rest.split("_")) else None)
    for path in paths:
        for part in str(path).split("/"):
            if part and part not in (".", "..") and not key.name(part):
                key.add(part, "dir")
    return key


def _dotted(token, key):
    """A dotted word -> what the copy shows: a file as it is (its first label replaced only when a cluster's or a
    group's name), a node's FQDN its name and a masked domain, another host or domain masked, a version, a Java
    package of a stock part or a setting's path as it is."""
    labels = token.split(".")
    first = key.get(labels[0])
    if labels[-1].lower() in EXTENSIONS:
        return ".".join([first] + labels[1:]) if first and re.match(r"^c\d+", first) else token
    if first:  # a node's FQDN
        return first + "." + key.add(".".join(labels[1:]), "domain")
    if (re.sub(r"^-+D?", "", token).startswith(STOCK_DOTTED) or any("_" in label for label in labels)
            or not re.match(r"^[A-Za-z]{2,24}$", labels[-1])):
        return token
    return key.add(token, "domain")


def _path(path, key):
    """An absolute path: kept in a stock directory (by whole components, never one with ..), the names of the site
    in its components replaced (/etc/cassandra/ssl/n1.jks); else masked."""
    if ".." not in path.split("/") and (path in STOCK_TOPS or any(
            path == stock or path.startswith(stock + "/") for stock in STOCK_DIRS)):
        return "/".join(key.inside(part) for part in path.split("/"))
    return "/" + key.add(path, "path")


def _relative(path, key):
    """A path from the home or the current directory: left to the name pass when it starts with a directory the key
    knows (the report's own ../<inventory dir>/...), else masked whole (~/ops/x, ./site-ops)."""
    first = next((part for part in path.split("/")[1:] if part not in ("", ".", "..")), "")
    if not path.startswith("~") and first.lower() in key.names:
        return path
    return key.add(path, "path")


# the pseudonyms of the copy (n1, c1, dc1, r1, domain1, path1, url1, bucket1, user1, prefix1, host1, group1, word1)
PSEUDONYM = re.compile(r"^(n|c|dc|r|domain|path|url|bucket|user|prefix|host|group|word)\d+$")
WORD = re.compile(r"[A-Za-z]{3,}")
UNKNOWN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3,}[A-Za-z0-9]*")
VOCABULARY_FILES = (".py", ".yml", ".yaml", ".j2", ".md", ".rst", ".txt", ".cfg", ".json", ".ini", ".xml", ".sh")


@functools.lru_cache(maxsize=1)
def vocabulary():
    """The words of the collection's own files (its code, roles, templates, playbooks, docs; not its tests): a word of
    the copy that is none of them comes from the site (a keyspace, a file name, a user) and is masked."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    words = set()
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", ".git", "output")]
        for name in files:
            if name.endswith(VOCABULARY_FILES) and name != os.path.basename(__file__):  # (its examples are site words)
                try:
                    with open(os.path.join(top, name), errors="replace") as f:
                        words.update(w.lower() for w in WORD.findall(f.read()))
                except OSError:
                    continue
    return frozenset(words)


def _unknown_words(text, key):
    """Every word (its letters three or more, digits after them allowed: bkp2) that the collection never writes,
    masked (word1): the names the other passes cannot know (a keyspace, acme.jks, a user named in free text). The
    pseudonyms of the copy and IP are kept."""
    known = vocabulary()

    def word(m):
        found = m.group(0)
        letters = re.match(r"[A-Za-z]+", found).group(0)
        if letters.lower() in known or found == "IP" or PSEUDONYM.match(found):
            return found
        return key.add(found, "word") or found
    return UNKNOWN.sub(word, text)


def _line(text, key):
    """A line of the report (_valued already) with the names of the key replaced, the addresses, the URLs and the
    paths of the site masked (each found a name of the key)."""
    text = URL.sub(lambda m: key.add(m.group(0), "url"), text)
    text = PATH.sub(lambda m: _path(m.group(0), key), text)
    text = RELATIVE_PATH.sub(lambda m: _relative(m.group(0), key), text)
    text = re.sub(r"\b([\w.+-]+)@(?=[\w-]+\.)", lambda m: (key.add(m.group(1), "user") or m.group(1)) + "@", text)
    text = re.sub(r"(?i)\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\b(?!\.\w)", "IP", text)  # (a host named by its address)
    for real in sorted((r for r in key.order if ADDRESS.match(r)), key=len, reverse=True):  # (a node named by its IP)
        text = re.sub(r"(?<![\w.:])%s(?![\w.:]*\w)" % re.escape(real), key.name(real), text)
    text = IPV4.sub("IP", text)
    text = IPV6.sub(lambda m: "IP" if ("::" in m.group(0) or m.group(0).count(":") >= 3) else m.group(0), text)
    for real in sorted((r for r in key.order if " " in r), key=len, reverse=True):  # (a cluster name with spaces)
        text = re.sub(re.escape(real), key.name(real), text, flags=re.I)

    def token(m):
        word = m.group(0)
        if key.get(word) or word.lower() in key.quiet:
            return key.get(word) or word
        if word.startswith("cassandra_") or re.sub(r"^-+D?", "", word).startswith(STOCK_DOTTED):
            return _dotted(word, key) if "." in word else word
        labels = word.split(".")
        if "." in word and labels[-1].lower() in EXTENSIONS:  # (a file: the names in it, cass-pay-01.keystore)
            return key.inside(word)
        if "." in word and key.get(labels[0]):  # (a node's FQDN: its name, the domain masked)
            return _dotted(word, key)
        inside = key.inside(word)  # (a compound of a name: db1-old, db1_backup)
        if inside != word:
            return inside
        if re.match(r":(\s|$)", m.string[m.end():m.end() + 2]):  # (a setting's name, storage.prefix: never a host)
            return word
        return _dotted(word, key) if "." in word else word
    text = TOKEN.sub(token, text)

    def own(m):  # (a group or host of your own inventory, not one the import wrote)
        name = m.group(2)
        if name == "all" or name.startswith("<") or name in key.taken or "_" in name and name.split("_")[0] in key.taken:
            return m.group(0)
        return "%s/%s" % (m.group(1), key.add(name, "group" if m.group(1) == "group_vars" else "host") or name)
    text = re.sub(r"\b(group_vars|host_vars)/([\w.<>-]+?)(?=[/*\s,;)]|$)", own, text)
    return _unknown_words(text, key)


def _valued(text, key):
    """Medusa's bucket, prefix and fqdn values and the user names masked (bucket1, user1), for every such setting the
    line names: its value, or each "<value> on <nodes>" after it; never a number nor the report's own words."""
    found = list(VALUED.finditer(text))
    if not found:
        return text

    def value(word, kind):
        if (word.startswith("(") or word.lower() in STOCK_WORDS or re.match(r"^[\d.]+$", word) or word in key.taken
                or word in ("****", "{{", "''")):
            return word
        return key.add(word, kind) or word
    shown, at = "", 0
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        kind = {"bucket_name": "bucket", "bucket": "bucket", "prefix": "prefix", "fqdn": "host"}.get(
            m.group(1).lower(), "user")
        parts = re.split(r"(; )", text[m.end():end])
        shown += text[at:m.end()] + "".join(p if p == "; " else re.sub(
            r"^([^\s,;]+)", lambda v, k=kind: value(v.group(1), k), p) for p in parts)
        at = end
    return shown + text[at:]


def _aligned(line, key):
    """_line; in an indented line (a table's row), its columns (the texts after two spaces or more) kept where they
    were when the names got shorter."""
    line = _valued(line, key)  # (the whole row: a setting's name and its value in two columns)
    if not line.startswith(" "):
        return _line(line, key)
    shown = ""
    for part in re.finditer(r"\S+(?: \S+)*", line):
        shown += " " * (part.start() if not shown else max(2, part.start() - len(shown))) + _line(part.group(0), key)
    return shown


def _masked(line):
    """A line masked again (a secret the report would have let through); a file mode and the report's own marks of a
    secret (in secrets.yml) left as they are when nothing else in the line would be masked."""
    rest = SECRETS_MARK.sub("", MODE_ROW.sub("", line))
    return line if rest != line and out.mask(rest) == rest else out.mask(line)


def cassandra_import_shareable(lines, layout, written=None, report_file="", cwd=None):
    """lines: the report's (cassandra_import_report's, or its text); layout: cassandra_inventory_layout's; written,
    report_file, cwd: cassandra_import_report's (the names of the inventory and report dirs, as the report shows
    them, masked; inventories and reports kept) -> {text: [the lines of the copy to share], map: [the lines of its
    key, "n1 = db11"...]}."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    shown = [out.path_from(p, cwd) for p in list(written or [])[1:] + ([report_file] if report_file else [])]
    key = _key(layout, [p.rsplit("/", 1)[0] if p.endswith(".txt") else p for p in shown])
    text = ["SHAREABLE COPY of an import_cluster report: nodes n1.., clusters c1.., addresses IP, the site's host"
            " names, domains, URLs, paths, buckets and user names replaced (domain1, url1, path1, bucket1, user1..),"
            " secrets masked. A short host name that is not a node's may remain: check before sharing. Its key"
            " stays with whoever ran the import.", ""]
    text += [_masked(_aligned(str(line), key)) for line in lines]
    head = ["# The key of report.shareable.txt: what each name of it stands for. Keep it to yourself.",
            "# Addresses are all shown as IP: not listed."]
    return {"text": text, "map": head + key.lines()}


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_shareable": cassandra_import_shareable}
