# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""A copy of the import's report that can be shared (import_cluster writes it with -e import_report_shareable=true).

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

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import group_chains, written_for
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import LATER_STOCK
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import ROLES
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import mask

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")  # (also in a file name: 10.1.2.3.jks, ip_10.1.2.3)
# a host named by its address with dashes (node-10-1-2-3); in an FQDN, the FQDN is masked whole
DASHED_IPV4 = re.compile(r"(?<!\d)(?<!\d-)(?:\d{1,3}-){3}\d{1,3}(?!\d)(?!-\d)(?!\.[A-Za-z])")  # (a host named by it: node-10-1-2-3)
# an IPv6 address: a "::" in it, or 4 groups at least (a time, 13:33:20, has 3); its IPv4 tail masked already
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:IP\b|[0-9A-Fa-f]{0,4}(?![\w:]))")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s,;'\"<>)]+")
# (a component's , = # ~ before a word kept in it: /x/orders,billing is one path; never a colon, the end of a path in
# -Xlog:...:file=/x/gc.log:time,uptime or /x/jvm.options:12)
PATH = re.compile(r"(?<![\w.~/<>*}-])/(?:[\w.@+-]|[,=#~](?=\w))+(?:/(?:[\w.@+*{}<>-]|[,=#~](?=\w))*)*")
# a path after a variable (${HOME}/x, $CASSANDRA_HOME/x): masked, but logback's stock files under the log dir
VAR_PATH = re.compile(r"\$\{[A-Za-z_][\w.]*:?[-=+?][^}]*\}(?:/[\w.@+*{}%-]+)*|\$\{?[A-Za-z_][\w.]*\}?(?:/[\w.@+*{}%-]+)+")
# (the stock ones: the log files under the log dir, their rolling patterns; cassandra-env.sh's own paths)
STOCK_LOG_PATH = re.compile(r"^\$\{?(?:cassandra\.logdir|CASSANDRA_LOG_DIR)\}?/(?:system|debug|gc|audit/audit)\.log"
                            r"(?:\.%d\{[yMdHhms:._ -]*\})?(?:\.%i)?(?:\.(?:zip|gz))?$"
                            r"|^\$\{?CASSANDRA_HOME\}?/lib/(?:jamm-[\d.]+\.jar|sigar-bin)$|^\$\{?CASSANDRA_CONF\}?/hotspot_compiler$")
# a quoted absolute path with a space or a colon in it ('/x/Orders Billing', '/x/orders:billing'): one path
QUOTED_PATH = re.compile(r"""(?<=['"])/[^'"\n]*[\s:][^'"\n]*(?=['"])|^/[^\s'"]*:[^\s'"]+$""")
# a path systemd may do without (EnvironmentFile=-/x, ExecStartPre=-/x), one written //x: masked as any absolute path
OPTIONAL_PATH = re.compile(r"""((?:^|[=\s'"])-?/?)(/[\w.@+-]+(?:/[\w.@+*{}<>-]*)*)""")
# a relative path as a value (data/x/cdc, file=logs/x.log, ${ROOT}x/y): its words of the site masked
# (not one from . or ~, nor the inventory's group_vars/host_vars: their own passes)
RELATIVE_VALUE = re.compile(r"""((?:^|[=:'"\[(,}\s])\s?)(?!(?:group|host)_vars/)"""
                            r"""(\w[\w.-]*/(?:[\w.-]+/)*[\w.@+*-]*)""")
# a relative path in a quoted value (data/x/cdc), a Windows one: masked, but the stock keystore of TDE
QUOTED_RELATIVE = re.compile(r"""(?<=['"])(?!conf/\.keystore['"])[\w.-]+(?:/[\w.@+*-]+)+(?=['"])|[A-Za-z]:\\[^\s'"]+""")
# a path as an XML element's value (<file>/srv/x/a.log</file>): up to the closing tag, never the tag itself
XML_PATH = re.compile(r"(?<![/\w])(<[A-Za-z][\w:.-]*(?:\s[^<>]*)?>)(/[\w.@+-]+(?:/[\w.@+*{}-]*)*)")  # (not <node>/ of a path)
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
              "/dev", "/tmp", "/etc/rc.d", "/etc/rc.local", "/etc/tuned", "/etc/udev/rules.d")
# these alone are kept, what follows them is masked unless a stock directory above
STOCK_TOPS = ("/etc", "/usr", "/opt", "/var", "/var/lib", "/var/log", "/usr/local", "/srv", "/home", "/data")
# what a dotted word that is not a host may be: a file (by its extension), a Java package of a stock part, the
# collection's own names
EXTENSIONS = ("yaml", "yml", "xml", "options", "sh", "properties", "password", "access", "conf", "ini", "txt", "json",
              "cfg", "log", "jar", "py", "service", "j2", "pem", "crt", "key", "jks", "p12", "keystore", "truststore",
              "md", "rst", "repo", "list", "gpg", "asc", "so", "d", "timer", "socket", "target", "env", "orig", "bak",
              "tmp", "map", "gz", "tar", "tgz", "zip", "deb", "rpm", "html", "rules")
STOCK_DOTTED = ("org.apache.", "org.slf4j", "org.eclipse.", "org.yaml.", "org.jctools", "ch.qos.", "io.netty",
                "io.dropwizard", "com.codahale", "com.datastax", "com.sun.", "com.google.", "java.", "javax.", "jdk.",
                "sun.", "net.openhft", "cassandra.", "community.", "ansible.", "ansible_collections.", "rc.local",
                "logback.")
# the kernel's settings (sysctl, tuned): vm.swappiness = 0, net.core.rmem_max=212992; kept as one only when a value
# follows (= or :), a host name may start the same way (vm.lab.example.com)
KERNEL_DOTTED = ("vm.", "net.core.", "net.ipv4.", "net.ipv6.", "kernel.", "fs.")
KERNEL_KEY = re.compile(r"^(?:%s)[a-z0-9_]+(?:\.[a-z0-9_]+)*$" % "|".join(re.escape(k) for k in KERNEL_DOTTED))
# a data center or rack named so says nothing of the site (dc1, r2, rack3, a, b...)
GENERIC = re.compile(r"(?i)^((dc|r|rack|zone|az)?[-_]?\d+|[a-z]{1,2})$")
# common words a name may be: never replaced (the report's own text has them)
STOCK_WORDS = ("cassandra", "data", "main", "default", "all", "local", "node", "nodes", "java", "medusa", "system",
               "root", "config", "logs", "log", "server", "inventories", "reports", "inventory", "host", "hosts",
               "group", "the", "on", "in", "of", "and", "or")
# the values a copy masks as well, by the setting's name: Medusa's bucket, prefix and fqdn, the user names
VALUED = re.compile(r"(?i)\b[\w.]*?(bucket_name|bucket|prefix|fqdn|_username|_user|superuser)\b(?:\s+\([^)]*\))?:\s+")
# (and a secret's variable set empty, '': none, e.g. Medusa's nodetool password when a password file is used)
SECRETS_MARK = re.compile(r"(?:[\w.-]+(?:\s+\([^)]*\))?:\s+)?(?:\(in secrets\.yml(?:, value \d+)?\)|\(in a vars file\)"
                          r"|''(?=\s|$))")
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
        self.clear = set()  # names shown in clear wherever they are (_clear_names)
        self.xml_names = set()  # the names of XML elements (name=, ref=) the report gives

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

    def inside(self, word, kinds=("n", "c", "dc", "r", "domain")):
        """word with the names of nodes, clusters, data centers, racks and domains (kinds) in it replaced, each a
        whole part of it (between -, _, ., / or its ends): db1-old, payments.yml, cass-pay-01.keystore."""
        for real in sorted(self.order, key=len, reverse=True):
            lower = real.lower()
            if (self.kinds.get(lower, "").rstrip("_g") in kinds and lower not in self.quiet
                    and len(real) >= 3 and not ADDRESS.match(real) and lower in word.lower()):
                word = re.sub(r"(?i)(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(real), self.names[lower], word)
        return word

    def lines(self):
        return ["%s = %s" % (self.names[real.lower()], real) for real in self.order]


def _forms(name):
    """A name as the report and the files may write it: as is, with -, _, a space or nothing between its words."""
    words = [w for w in re.split(r"[\s_-]+", str(name)) if w]
    return set([str(name)] + [sep.join(words) for sep in (" ", "_", "-", "")])


@functools.lru_cache(maxsize=1)
def _stock_names():
    """The setting names the copy shows in clear wherever they are: the kernel settings cassandra_linux sets, and the
    JVM properties of the stock options files (-Dcassandra.x, -Dchronicle.analytics.disable: their -D names, with and
    without it), the later stock lines the import reports too -> frozenset."""
    names = set()
    with open(os.path.join(ROLES, "cassandra_linux", "defaults", "main.yml"), encoding="utf-8") as f:
        names.update(str(k) for k in (yaml.safe_load(f).get("cassandra_linux_sysctl") or {}))
    lines = [line for files in LATER_STOCK.values() for line in files]
    templates = os.path.join(ROLES, "cassandra_config", "templates")
    for series in sorted(os.listdir(templates)):
        folder = os.path.join(templates, series)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if re.match(r"^jvm\d*-server\.options\.j2$", name):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    lines += f.read().split("\n")
    for line in lines:
        m = re.match(r"^\s*#?\s*-D([\w.-]+)", line)
        if m:
            names.update((m.group(1), "-D" + m.group(1)))
    return frozenset(names)


def _clear_names(layout):
    """The names the copy shows in clear: the stock ones (_stock_names), and the kernel settings the import read on
    the nodes or carries (cassandra_linux_sysctl, the OS lines read) -> set."""
    names = set(_stock_names())
    for n in layout.get("nodes") or []:
        names.update(str(k) for k in ((n.get("vars") or {}).get("cassandra_linux_sysctl") or {}))
        for line in (n.get("os") or {}).get("lines") or []:
            m = re.match(r"^\s*(?:sysctl\s+)?([a-z0-9_]+(?:\.[a-z0-9_-]+)+)\s*(?:[=:]|\()", str(line))
            if m and KERNEL_KEY.match(m.group(1)):
                names.add(m.group(1))
    return names


def _key(layout, paths=()):
    """The names of the layout -> _Key: the clusters, the nodes (their names, host names, the hosts they were given,
    an address as a name too), the data centers and racks named after the site, the inventory's groups, the dirs of
    paths (the inventory's and the report's as the report shows them, unless the default ones)."""
    key = _Key()
    key.clear = _clear_names(layout)
    nodes = sorted(layout.get("nodes") or [], key=lambda n: _natural(n.get("name", "")))
    places = [str(n.get(k)) for n in nodes for k in ("dc", "rack") if n.get(k)]
    key.taken |= set(p for p in places if GENERIC.match(p))  # (a real dc1 keeps its name: the site's ones after it)
    cluster = layout.get("cluster_group") or ""
    given = key.add(cluster, "c", force=True)
    # (sorted: a set's order changes with the hash seed; the same import writes the same key)
    for form in sorted(_forms(layout.get("cluster_name") or "") | _forms(cluster)):
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
                for form in sorted(_forms(place)):  # (eu-west-1 as eu_west_1 too; sorted: the same key each run)
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
    # the values of the settings that name the site (a bucket, a prefix, an fqdn, a user), read from the nodes: masked
    # in every row of a table, not only in the one that names the setting
    for n in nodes:
        for name, value in _leaves(n.get("vars") or {}):
            kind = VALUED_KINDS.get(VALUED_KEY.search(name).group(1).lower(), "user") if VALUED_KEY.search(name) else ""
            if kind and isinstance(value, str) and value.strip() and "{{" not in value and not re.match(r"^[\d.]+$", value):
                key.add(value.strip(), kind)
    for path in paths:
        for part in str(path).split("/"):
            if part and part not in (".", "..") and not key.name(part):
                key.add(part, "dir")
    return key


VALUED_KEY = re.compile(r"(?i)(bucket_name|bucket|prefix|fqdn|username|_user|superuser)$")
VALUED_KINDS = {"bucket_name": "bucket", "bucket": "bucket", "prefix": "prefix", "fqdn": "host"}


def _leaves(values, path=""):
    """A node's variables -> [(their dotted path, a value that is not a dict nor a list)], list items under their
    list's path."""
    found = []
    items = values.items() if isinstance(values, dict) else ((None, v) for v in values) if isinstance(values, list) \
        else ()
    for k, v in items:
        name = path if k is None else ("%s.%s" % (path, k) if path else str(k))
        found += _leaves(v, name) if isinstance(v, (dict, list)) else [(name, v)]
    return found


def _dotted(token, key):
    """A dotted word -> what the copy shows: a file as it is (its first label replaced only when a cluster's or a
    group's name), a node's FQDN its name and a masked domain, another host or domain masked, a version, a Java
    package of a stock part or a setting's path as it is."""
    labels = token.split(".")
    first = key.get(labels[0])
    if labels[-1].lower() in EXTENSIONS:
        return ".".join([first] + labels[1:]) if first and re.match(r"^c\d+", first) else token
    if first and key.kinds.get(labels[0].lower()) == "n":  # a node's FQDN (its domain kept when a common word)
        rest = ".".join(labels[1:])
        return first + "." + (key.add(rest, "domain") or rest)
    if first:  # (another name of the key first: masked as a whole)
        return key.add(token, "domain") or token
    if (re.sub(r"^-+D?", "", token).startswith(STOCK_DOTTED) or any("_" in label for label in labels)
            or not re.match(r"^[A-Za-z]{2,24}$", labels[-1])):
        return token
    return key.add(token, "domain")


def _path(path, key):
    """An absolute path: kept in a stock directory (by whole components, never one with ..), the names of the site
    in its components replaced (/etc/cassandra/ssl/n1.jks), a short code of the site too (/etc/sysctl.d/01-word1.conf),
    a tuned profile of the site's (/etc/tuned/word2/tuned.conf); else masked."""
    if ".." not in path.split("/") and (path in STOCK_TOPS or any(
            path == stock or path.startswith(stock + "/") for stock in STOCK_DIRS)):
        parts = path.split("/")
        at = 3 if path.startswith("/etc/tuned/") else 4 if path.startswith("/usr/lib/tuned/") else 0
        profile = at if at and len(parts) > at + 1 else None  # (its profile's directory, a file under it)
        if profile:
            parts[at] = _profile(parts[at], key)
        top = max(len(stock.split("/")) for stock in STOCK_DIRS + STOCK_TOPS
                  if path == stock or path.startswith(stock + "/"))
        return "/".join(part if i < top or i == profile else _part(part, key) for i, part in enumerate(parts))
    return "/" + key.add(path, "path")


def _quoted_path(text, key):
    """A quoted absolute path with a space or a colon: a command (/usr/bin/nodetool -h x repair) its path alone, its
    arguments left to the other passes, as what follows ": "; else the whole as one path (/x/Orders Billing)."""
    head, sep, rest = text.partition(": ")  # (a path and what is said of it: '/x/cassandra.yaml: line 4')
    if not sep:
        head, sep, rest = text.partition(" ")
    if sep == ": " or sep and re.search(r"/s?bin/[^/]*$", head):
        return _path(head, key) + sep + rest
    return _path(text, key)


def _part(part, key):
    """A component of a path under a stock directory: the names of the site in it replaced, its short codes masked,
    each word no path of the roles has (orders under /var/lib/cassandra) masked there (_path_words)."""
    shown = _codes(key.inside(part), key)
    return shown if shown in stock_path_parts() else _path_words(shown, key)


# the words of a stock OS's or JDK's paths, of the report's own files: kept in a path, a file name
KEPT_PATH_WORDS = ("mapper", "swap", "amd64", "x86_64", "jamm", "override", "read_ahead_kb", "vm", "swappiness",
                   "defrag", "enabled", "temurin", "amazon", "corretto", "cacerts", "multi", "user", "target", "wants",
                   "jna", "scheduler", "rotational", "hsperfdata", "sigar", "libsigar", "jre", "jdk", "openjdk", "java",
                   "jvm", "bin", "lib", "lib64", "sbin", "share", "queue", "block", "class", "kernel", "mm",
                   "transparent", "hugepage", "max", "map", "count", "pki", "tls", "certs", "ca", "bundle", "ssl",
                   "current", "platform", "python", "python3", "linux", "so", "management", "alternatives", "sources",
                   "list", "repo", "repos", "pid", "run", "tuned", "throughput", "performance", "credentials",
                   "group_vars", "host_vars", "inventories", "inventory", "reports", "report", "details", "shareable",
                   "secrets", "hosts", "main", "vars", "files", "templates", "roles", "playbooks", "all", "dev", "sys",
                   "proc", "etc", "usr", "opt", "var", "tmp", "local", "default", "conf", "data", "keystore", "read",
                   "ahead", "kb", "auth", "topology", "traces", "distributed", "schema", "system", "yum", "repos", "gc",
                   "tools", "stress", "dumps", "iptables", "nftables", "true", "false", "write", "and", "or", "stop",
                   "nodes", "racks", "password", "access", "files", "rules", "fs", "nf", "ns", "defaults", "tasks",
                   "handlers", "meta", "nr", "requests", "doc", "cql3", "pylib", "cqlshlib", "jsr223", "groovy",
                   "init", "check")


def _path_words(text, key):
    """A path or a file name with each of its words that no stock path has masked, there only (a word of the report
    elsewhere stays): its name in the key, reused wherever the key has it already."""
    def word(m):
        found = m.group(0)
        if (found.lower() in stock_path_words() or found.lower() in KEPT_PATH_WORDS or found.lower() in EXTENSIONS
                or found == "IP" or PSEUDONYM.match(found) or OS_CODES.match(found)
                or GENERIC_PLACE.match(found)
                or not re.search(r"[A-Za-z]{2}", found) or re.match(r"^[0-9a-f]{32}$", found)):
            return found
        if key.name(found):
            return key.name(found)
        given = key.add(found, "word", force=True)
        key.quiet.add(found.lower())
        return given
    return re.sub(r"[^-_./=,#~:\s]+", word, text)


@functools.lru_cache(maxsize=1)
def stock_path_words():
    """The words of stock_path_parts (60-cassandra.conf: cassandra, conf), lower case -> frozenset."""
    return frozenset(w.lower() for part in stock_path_parts() for w in re.split(r"[-_.]+", part) if w)


@functools.lru_cache(maxsize=1)
def stock_path_parts():
    """The components of the paths the roles' defaults, variables and templates name (data, commitlog, system.log,
    60-cassandra.conf...), and of a stock install's (hints, saved_caches, cdc_raw, lib, conf) -> frozenset."""
    parts = set(("data", "commitlog", "saved_caches", "hints", "cdc_raw", "lib", "conf", "bin", "logs", "audit",
                 "system.log", "debug.log", "gc.log", "ssl", "backups", "snapshots", "system", "jmx_prometheus",
                 "cqlsh", "nodetool", "sstableloader", "keystore", "truststore", "rc.local") + STOCK_TUNED)
    for role in sorted(os.listdir(ROLES)):
        for kind in ("defaults", "vars", "templates"):
            for top, dirs, files in os.walk(os.path.join(ROLES, role, kind)):
                for name in files:
                    with open(os.path.join(top, name), encoding="utf-8", errors="replace") as f:
                        for path in re.findall(r"(?<![\w.])/[\w.@+-]+(?:/[\w.@+-]+)*", f.read()):
                            parts.update(p for p in path.split("/") if p)
                    parts.add(re.sub(r"\.j2$", "", name))
    return frozenset(parts)


@functools.lru_cache(maxsize=1)
def stock_file_names():
    """The names of the files the roles write (their templates': cassandra-env.sh, jvm11-server.options...) -> frozenset."""
    names = set()
    for role in sorted(os.listdir(ROLES)):
        for top, dirs, files in os.walk(os.path.join(ROLES, role, "templates")):
            names.update(re.sub(r"\.j2$", "", name) for name in files if "." in re.sub(r"\.j2$", "", name))
    return frozenset(names)


def _stock_file(word):
    """Whether word is a file the roles write, alone or with a key of cassandra_config_file_permissions after it."""
    return any(word == name or word in ("%s.%s" % (name, k) for k in ("owner", "group", "mode"))
               for name in stock_file_names())


# a data center or a rack named so (dc2, rack3, az1): says nothing of the site, never a short code of it
GENERIC_PLACE = re.compile(r"(?i)^(dc|r|rack|zone|az)[-_]?\d+$")
# a short code of the site (iv2, zq7, pr01: one or two letters, then digits): what the vocabulary pass leaves (fewer
# than three letters), masked where the site names things (file names, tuned profiles, disk labels); not the OS's
# own (x86_64, el8, sdb1, nvme0n1, dm-3, lz4, s3...) nor the copy's pseudonyms (n1, dc2, r1, c1)
SHORT_CODE = re.compile(r"^[A-Za-z]{1,2}\d+[A-Za-z0-9]*$")
OS_CODES = re.compile(r"(?i)^(x86|x64|el\d+|fc\d+|sd[a-z]+\d*|vd[a-z]+\d*|hd[a-z]+\d*|nvme\d+(n\d+(p\d+)?)?|md\d+|dm\d*|"
                      r"lz4|s3|ec2|gp\d|io\d|v\d+|py\d+|ip\d?|ipv[46]|tls\d*|ssl\d*|sha\d+|md5|utf\d+|k8s|i\d86|j2)$")
STOCK_TUNED = ("balanced", "desktop", "latency-performance", "network-latency", "network-throughput", "powersave",
               "throughput-performance", "virtual-guest", "virtual-host", "hpc-compute", "accelerator-performance",
               "intel-sst", "optimize-serial-console", "aws", "functions", "recommend.d")


def _codes(text, key):
    """text (a file name, a label) with each short code of the site in it masked (01-sysctl-iv2.conf ->
    01-sysctl-word1.conf), its other parts as they are."""
    def code(part):
        if SHORT_CODE.match(part) and not OS_CODES.match(part) and not PSEUDONYM.match(part) and part != "IP" \
                and not GENERIC_PLACE.match(part):
            return key.add(part, "word") or part
        return part
    return "".join(code(p) for p in re.split(r"([-_./]+)", text))


def _profile(name, key):
    """A tuned profile's name: the stock ones as they are, a site's masked."""
    return name if name in STOCK_TUNED or not name else (key.add(name, "word") or name)


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
    return frozenset(words | set(EXTRA_VOCABULARY) | set(KEPT_PATH_WORDS))


# a dotted name, a -D property of one (-Dsun.rmi.dgc.server.gcInterval, org.apache.cassandra.auth.PasswordAuthenticator)
DOTTED_NAME = re.compile(r"(?<![\w.$-])(?:-D)?[A-Za-z_][\w$-]*(?:\.[\w$-]+)+(?![\w$])")
# words of a stock install the collection's files do not write (THP, systemd's directives, a cloud region's parts):
# read as the report's own
EXTRA_VOCABULARY = ("hugepages", "execstartpre", "execstartpost", "execstart", "execstop", "execreload",
                    "workingdirectory", "environmentfile", "limitnofile", "limitmemlock", "limitnproc", "limitas",
                    "timeoutstartsec", "timeoutstopsec", "restartsec", "successexitstatus", "standardoutput",
                    "standarderror", "east", "west", "north", "south", "central", "southeast", "northeast", "yum")


# the -D properties bin/cassandra itself gives (not in the role's files)
STOCK_NAMES = ("logback.configurationFile", "cassandra.logdir", "cassandra.storagedir", "cassandra-foreground",
               "cassandra-pidfile")


@functools.lru_cache(maxsize=1)
def stock_dotted_names():
    """The dotted names of a stock install, in clear wherever they are, each whole (a name of the site under a stock
    prefix is not one): the stock JVM properties and kernel settings (_stock_names), the role's logback names, the
    class names and -D properties its templates write -> frozenset (each also as -D<name>)."""
    names = set(_stock_names()) | set(stock_xml_names()) | set(STOCK_NAMES)
    for top, dirs, files in os.walk(os.path.join(ROLES, "cassandra_config", "templates")):
        for name in files:
            with open(os.path.join(top, name), encoding="utf-8", errors="replace") as f:
                names.update(m.group(0) for m in DOTTED_NAME.finditer(f.read())
                             if re.sub(r"^-D", "", m.group(0)).startswith(STOCK_DOTTED))
    return frozenset(names | set("-D" + n for n in names if not n.startswith("-D")))


def _unknown_words(text, key):
    """Every word (its letters three or more, digits after them allowed: bkp2) that the collection never writes,
    masked (word1): the names the other passes cannot know (a keyspace, acme.jks, a user named in free text). The
    pseudonyms of the copy and IP are kept."""
    known = vocabulary()
    # (a stock dotted name as it is, never a name of the site under a stock prefix: org.apache.cassandra.acme)
    stock = [m.span() for m in DOTTED_NAME.finditer(text) if m.group(0) in stock_dotted_names()]

    def word(m):
        found = m.group(0)
        if any(a <= m.start() < b for a, b in stock):
            return found
        letters = re.match(r"[A-Za-z]+", found).group(0)
        if m.string[m.start() - 1:m.start()] == "-" and m.string[m.start() + 1:].startswith(STOCK_DOTTED):
            return found  # (-Dsun.x, -Dorg.apache.x: a stock prefix's first word; the words after it judged alone)
        if letters.lower() in known or found == "IP" or PSEUDONYM.match(found):
            return found
        return key.add(found, "word") or found
    return UNKNOWN.sub(word, text)


# the setting of a self-check difference ("<file>: <setting>: node has ..."), in its file's own words (setting); a JVM
# option of another prefix than JVM_KNOWN masked as in any line (a site's own: -Dcom.<company>.x)
DIFFERENCE_SETTING = re.compile(r"(?P<file>[^\s:]+\.(?:yaml|xml|ini|options|properties|access|password)): "
                                r"(?P<held>[^\s:][^:]*?): node has ")
# the JVM options whose names are the JVM's, Cassandra's or a stock library's: their names never masked (owner's
# decision, 2026-11-16); any other -D name may be the site's, masked
JVM_KNOWN = ("-XX:", "-Dcassandra.", "-Djava.", "-Djdk.", "-Dcom.sun.", "-Dlog4j2.", "-Dchronicle.", "-Dio.netty.")
JVM_NAME = re.compile(r"(?<![\w.-])(?:%s)[+-]?[\w.-]*[\w]" % "|".join(re.escape(k) for k in JVM_KNOWN))


def _site_option(text):
    """Whether text is a JVM option (-D..., -XX:...) whose name may be the site's (not of JVM_KNOWN)."""
    return bool(re.match(r"^-(D|XX:)", text)) and not text.startswith(JVM_KNOWN)


_HELD = re.compile(u"\ue000(\\d+)\ue001")


def _held(text, key, spans):
    """text with each span of spans (regexes) put aside (a self-check's setting as setting shows it, another with
    its names of nodes and clusters replaced), the others as they are -> (text, [the spans put aside, by their
    number])."""
    held = []

    def hold(m):
        at = m.span("held") if "held" in m.re.groupindex else m.span()
        if _site_option(m.string[at[0]:at[1]]):
            return m.group(0)
        span = m.string[at[0]:at[1]]  # (a self-check's setting in its file's words; a known JVM option's name)
        held.append(setting(span, key, m.group("file")) if "file" in m.re.groupindex
                    else key.inside(span, ("n", "c", "dc", "r")))
        return u"%s\ue000%d\ue001%s" % (m.string[m.start():at[0]], len(held) - 1, m.string[at[1]:m.end()])
    for span in spans:
        text = span.sub(hold, text)
    return text, held


def _back(text, held):
    return _HELD.sub(lambda m: held[int(m.group(1))], text)


VALUES = re.compile(r"^\s+Node {2,}\S")


def _indent(line):
    return len(line) - len(line.lstrip())


def _file_above(lines, i):
    """The file a self-check's setting (lines[i]) is under: the first word of the line above it less indented
    ("    logback.xml    n1")."""
    above = next((line for line in reversed(lines[:i]) if line.strip() and _indent(line) < _indent(lines[i])), "")
    return (above.split() or [""])[0]


# the files whose settings are named by keys of their own vocabulary (a YAML key, an INI one): in clear but for the
# names of the site in them; a JVM option's name, in clear when the JVM's, Cassandra's or a stock library's
KEYED_FILES = (".yaml", ".yml", ".ini")
KNOWN_OPTION = re.compile(r"^(-X(mx|ms|mn|ss)|(%s)[+-]?[\w.]+)$" % "|".join(re.escape(k) for k in JVM_KNOWN))
# an XPath's attribute value (an element named by name= or ref=, another attribute's value): the site's
XPATH_VALUE = re.compile(r"""(\[@([\w.:-]+)=(["']))(.*?)(\3\])""")
# the JMX users of the JDK's own files: the others are the site's
STOCK_JMX_USERS = ("cassandra", "monitorRole", "controlRole")
# the names cassandra.yaml leaves to the site in a key: a memtable configuration's (the stock ones kept), a
# parameter of a class it names (a seed provider's, an authenticator's: seeds kept)
YAML_NAMED = ((re.compile(r"(memtable\.configurations\.)([^.\[\s]+)"), ("skiplist", "trie", "default", "skiplist_sharded")),
              (re.compile(r"(\.parameters(?:\[\d+\])?\.)([^.\[\s]+)"),
               ("seeds", "high_ratio", "low_ratio", "factor", "flow", "fail_on_missing_provider", "keystore",
                "keystore_password", "store_type", "key_password", "truststore", "truststore_password",
                "default_keyspace", "default_table")))


@functools.lru_cache(maxsize=1)
def stock_xml_names():
    """The names (name=, ref=) the role's logback.xml gives its elements (SYSTEMLOG, org.apache.cassandra), and the
    logger of Cassandra's stock file before 4.0 (com.thinkaurelius.thrift): in clear."""
    names = set(["com.thinkaurelius.thrift"])
    templates = os.path.join(ROLES, "cassandra_config", "templates")
    for series in sorted(os.listdir(templates)):
        path = os.path.join(templates, series, "logback.xml.j2")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                names.update(re.findall(r'(?:name|ref)="([^"{]+)"', f.read()))
    return frozenset(names)


def _xml_name(name, key):
    """The name of an XML element (name=, ref=): the role's own and a stock library's package in clear, a package of
    the site's masked as in any line, another name masked whole (word1)."""
    if name in stock_xml_names():
        return name
    if "." in name and " " not in name:
        return _masked_names(name, key)
    return key.add(name, "word") or name


def _xpath(text, key):
    """An XPath's attribute values masked: an element's name (name=, ref=) as _xml_name, another as in any line."""
    return XPATH_VALUE.sub(lambda m: m.group(1) + (_xml_name(m.group(4), key) if m.group(2) in ("name", "ref")
                                                   else _masked_names(m.group(4), key)) + m.group(5), text)


def setting(text, key, file_name):
    """A setting of file_name named in the file's own words (a line of the self-check's: AUDIT / rollingPolicy, -Dx.y,
    client_encryption_options.enabled): a YAML or INI key, an XML element's tags and attribute names, a JVM option of
    JVM_KNOWN or the heap's as they are (but for the names of the site in them, an address); what the site chose
    masked as in any line: a JMX user (but the JDK's), the name= or ref= of an XML element (a logger's package, an
    appender), a memtable configuration, any other setting (an environment variable, a kernel setting's file, a
    device, a JVM option of the site's: -javaagent:/srv/x.jar)."""
    name = os.path.basename(file_name)
    if name.endswith(".xml") and not text.startswith("/"):
        # as xml_where names it (x.y / @level, appender-ref A): the names the XPaths give replaced whole first (a
        # name may have a space, a slash, a node's name)
        for real in sorted(key.xml_names, key=len, reverse=True):
            text = re.sub(r"(?:(?<=^)|(?<= ))%s(?=$| / | \(\d+\))" % re.escape(real),
                          lambda m: u"\ue002%s\ue003" % _xml_name(real, key), text)
    text = IPV4.sub("IP", key.inside(text, ("n", "c", "dc", "r")))
    if name.startswith("jmxremote."):  # (the setting is a user)
        return text if text in STOCK_JMX_USERS or PSEUDONYM.match(text) else _site_value(text, "user", key)
    if name.endswith(".xml"):
        if text.startswith("/"):  # an XPath: /configuration/logger[@name="x"]/@level
            return _xpath(text, key)
        return " / ".join(_step(step, key) for step in text.split(" / ")).replace(u"\ue002", "").replace(u"\ue003", "")
    if name.endswith(KEYED_FILES) or name.endswith(".options") and KNOWN_OPTION.match(text):
        for pattern, stock in YAML_NAMED:
            text = pattern.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) in stock
                                                       else key.add(m.group(2), "word") or m.group(2)), text)
        return key.inside(text)
    return _line(text, key)


def _step(step, key):
    """A step of an XML setting as the self-check names it (xml_where): a tag (rollingPolicy, immediateFlush) or an
    attribute (@level) as it is; an element by its name (a package, or one the XPaths of the report give: replaced
    already, between \ue002 and \ue003), an appender-ref by its ref (appender-ref ASYNC): as _xml_name; its place
    among its siblings (2) kept."""
    m = re.match(r"^(.*?)((?: \(\d+\))?)$", step)
    name, place = m.groups()
    if "[@" in name:  # (an XPath's step left as it is: appender[@name='x'])
        return _xpath(name, key) + place
    if u"\ue002" in name:
        return step
    tag, dummy, value = name.rpartition(" ")
    if not tag and (name.startswith("@") or "." not in name):
        return step
    return (tag + " " if tag else "") + _xml_name(value, key) + place


def _line(text, key):
    """A line of the report (_valued already) with the names of the key replaced, the addresses, the URLs and the
    paths of the site masked (each found a name of the key); the addresses first (a file named by one: IP.jks); the
    setting of a self-check difference kept (DIFFERENCE_SETTING)."""
    text, held = _held(text, key, (DIFFERENCE_SETTING, JVM_NAME))
    return _back(_masked_names(text, key), held)


def _masked_names(text, key):
    """_line's passes."""
    text = re.sub(r"(?i)\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\b(?!\.\w)", "IP", text)  # (a host named by its address)
    for real in sorted((r for r in key.order if ADDRESS.match(r)), key=len, reverse=True):  # (a node named by its IP)
        text = re.sub(r"(?<![\d.:])%s(?!\.?\d|:\w)" % re.escape(real), key.name(real), text)
    text = IPV4.sub("IP", text)
    text = DASHED_IPV4.sub("IP", text)
    text = IPV6.sub(lambda m: "IP" if ("::" in m.group(0) or m.group(0).count(":") >= 3) else m.group(0), text)
    text = URL.sub(lambda m: key.add(m.group(0), "url"), text)
    text = VAR_PATH.sub(lambda m: m.group(0) if STOCK_LOG_PATH.match(m.group(0)) else key.add(m.group(0), "path"), text)
    text = QUOTED_RELATIVE.sub(lambda m: key.add(m.group(0), "path"), text)
    text = QUOTED_PATH.sub(lambda m: _quoted_path(m.group(0), key), text)
    text = OPTIONAL_PATH.sub(lambda m: m.group(1) + _path(m.group(2), key) if m.group(1).endswith(("-", "/"))
                             else m.group(0), text)
    text = RELATIVE_VALUE.sub(lambda m: m.group(1) + _path_words(_codes(key.inside(m.group(2)), key), key), text)
    text = PATH.sub(lambda m: _path(m.group(0), key), text)
    text = XML_PATH.sub(lambda m: m.group(1) + _path(m.group(2), key), text)
    text = re.sub(r"\b(tuned:? \(?profile )([\w.-]+)", lambda m: m.group(1) + _profile(m.group(2), key), text)
    text = re.sub(r"\b((?:device|disk|read_ahead_kb|IO scheduler|scheduler|LVM read-ahead) )([^\s(]+)",
                  lambda m: m.group(1) + _codes(m.group(2), key), text)
    text = re.sub(r"(\b(?:device|disk) \S+ \()(.*?)(, (?:SSD|rotational)\))",  # (its labels: (vg-lv (dm-3), SSD))
                  lambda m: m.group(1) + re.sub(r"[\w.-]+", lambda t: _codes(t.group(0), key), m.group(2)) + m.group(3),
                  text)
    text = RELATIVE_PATH.sub(lambda m: _relative(m.group(0), key), text)
    text = re.sub(r"\b([\w.+-]+)@(?=[\w-]+\.)", lambda m: (key.add(m.group(1), "user") or m.group(1)) + "@", text)
    for real in sorted((r for r in key.order if " " in r), key=len, reverse=True):  # (a cluster name with spaces)
        text = re.sub(re.escape(real), key.name(real), text, flags=re.I)

    def token(m):
        word = m.group(0)
        if _stock_file(word):  # (a file the roles write, a key of its in a dict: jvm-server.options.mode)
            return word
        if key.get(word) or word.lower() in key.quiet:
            return key.get(word) or word
        if KERNEL_KEY.match(word) and (re.match(r"\s*[=:]", m.string[m.end():])  # (vm.swappiness = 0)
                                       or m.string[:m.start()].endswith("sysctl ")):  # (sysctl vm.drop_caches)
            return word
        if word in key.clear or word in stock_dotted_names():  # (a kernel setting the nodes have, a stock name)
            return word
        if "." in word and all(w.lower() in KEPT_PATH_WORDS or w.lower() in EXTENSIONS or w.isdigit()
                               for w in re.split(r"[-_.]+", word) if w):  # (gc.log.0.current, multi-user.target.wants)
            return word
        if word.startswith("cassandra_") or re.sub(r"^-+D?", "", word).startswith(STOCK_DOTTED):
            return _dotted(word, key) if "." in word else word
        labels = word.split(".")
        if "." in word and labels[-1].lower() in EXTENSIONS:  # (a file: the names in it, cass-pay-01.keystore)
            return _path_words(key.inside(word), key)
        if "." in word and key.get(labels[0]):  # (a node's FQDN: its name, the domain masked)
            return _dotted(word, key)
        if "." in word and key.kinds.get(labels[0].lower()) != "n" and any(
                key.kinds.get(".".join(labels[i:]).lower()) == "domain" for i in range(1, len(labels))):
            return key.add(word, "host") or word  # (a host of the site's domain: ntp1.<domain> whole)
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


# a self-check difference's values, after a setting's name ("<file>: <setting>: node has 'v', import would write 'w'")
DIFFERENCE_VALUES = re.compile(r"""((?:node has|import would write|the roles would set) )('[^']*'|"[^"]*"|[^\s,]+)""")


def _valued_word(word, kind, key):
    """A value of a setting that names the site (a bucket, a prefix, an fqdn, a user): masked (bucket1, user1), but
    a number, a word of the report (absent, nothing, ****...) or a name of the copy."""
    if (not word or word.startswith("(") or word.lower() in STOCK_WORDS or re.match(r"^[\d.]+$", word)
            or word in key.taken or word in ("****", "{{", "''", "nothing", "absent", "present")):
        return word
    return _site_value(word, kind, key)


def _site_value(real, kind, key):
    """A value the site chose (a user, a bucket) -> its name in the copy, masked wherever it is (a word the report
    uses too, ansible, readwrite: there as well); a name the key has already (the cluster's) as it is there."""
    if key.name(real):
        return key.name(real)
    given = key.add(real, kind, force=True)
    key.quiet.discard(real.lower())
    return given


def _valued(text, key):
    """Medusa's bucket, prefix and fqdn values and the user names masked (bucket1, user1), for every such setting the
    line names: its value, or each "<value> on <nodes>" after it, or a self-check difference's values (node has 'v',
    import would write 'w'); never a number nor the report's own words."""
    found = list(VALUED.finditer(text))
    if not found:
        return text

    def quoted(v, kind):
        inner = v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v
        return v.replace(inner, _valued_word(inner, kind, key), 1) if inner else v
    shown, at = "", 0
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        kind = VALUED_KINDS.get(m.group(1).lower(), "user")
        rest = text[m.end():end]
        if rest.startswith("node has "):
            shown += text[at:m.end()] + DIFFERENCE_VALUES.sub(lambda v, k=kind: v.group(1) + quoted(v.group(2), k), rest)
        else:
            parts = re.split(r"(; )", rest)
            shown += text[at:m.end()] + "".join(p if p == "; " else re.sub(
                r"^([^\s,;]+)", lambda v, k=kind: _valued_word(v.group(1), k, key), p) for p in parts)
        at = end
    return shown + text[at:]


VALUE_ROW = re.compile(r"^(\s+(?:Node|Ansible) {2,})(\S.*?)(\s*)$")


def _value_rows(lines, named, key):
    """The rows of the values (Node, Ansible) under a self-check's setting that names the site (a bucket, a user:
    VALUED): {index of the row: the row, its value masked}."""
    rows = {}
    for i in named:
        m = VALUED.search(lines[i].strip() + ": ")
        if not m:
            continue
        kind = VALUED_KINDS.get(m.group(1).lower(), "user")
        for k in range(i + 1, len(lines)):
            row = VALUE_ROW.match(lines[k])
            if not row or _indent(lines[k]) <= _indent(lines[i]):
                break
            rows[k] = row.group(1) + _valued_word(row.group(2), kind, key) + row.group(3)
    return rows


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
    """A line masked again (a secret the report would have let through); a file mode, the report's own marks of a
    secret (in secrets.yml) and an empty value ('') left as they are when nothing else in the line would be
    masked."""
    rest = SECRETS_MARK.sub("", MODE_ROW.sub("", line))
    return line if rest != line and mask(rest) == rest else mask(line)


# the first words of the copy and of its key: an earlier import's files known by them
COPY_HEAD = "SHAREABLE COPY of an import_cluster report"
KEY_HEAD = "# The key of report.shareable.txt"

# the files of a shareable copy: (its name, the report it goes with, what it is, its first words); the key's
# .map name is an earlier release candidate's (Ansible read it as inventory), with the same first words
SHAREABLE_FILES = (("report.shareable.txt", "report.txt", "copy", COPY_HEAD),
                   ("report.shareable.key.txt", "report.txt", "key", KEY_HEAD),
                   ("report.shareable.map", "report.txt", "key", KEY_HEAD),
                   ("report.check.shareable.txt", "report.check.txt", "copy", COPY_HEAD))


# a copy and the key that decodes it: when one is moved aside, the other one too (a key that changed, a node
# replaced, while the copy's text stayed the same: the copy left in place would go with the new key)
PAIR = ("report.shareable.txt", "report.shareable.key.txt")


def _first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readline()
    except (IOError, OSError):
        return ""


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (IOError, OSError):
        return None


def _free_name(path, stamp):
    """path.<stamp>~, else path.<stamp>.1~, .2~...: the first name nothing has (a run again within the second)."""
    name, n = "%s.%s~" % (path, stamp), 0
    while os.path.lexists(name):
        n += 1
        name = "%s.%s.%d~" % (path, stamp, n)
    return name


# the stamps of a run in the report's head, left out when an earlier copy is compared with the new one: the run's
# time on the IMPORT lines (cassandra_output.clock: "10:11 CET", "2026-11-20 10:11 +01:00") and the made by line
RUN_TIME = re.compile(r"(\d{4}-\d{2}-\d{2} )?\d{2}:\d{2} ([A-Za-z]+|[+-]\d{2}:\d{2})  ")


def _unstamped(text):
    """A copy's or key's text without the stamps of the run that wrote it (the key has none)."""
    return [RUN_TIME.sub("", line, count=1) if line.startswith("IMPORT ") else line
            for line in str(text).split("\n") if not line.strip().startswith("made by:")]


def cassandra_import_old_shareable(report_dir, cluster, stamp, shareable=False, new=None, check=False):
    """The report dir, the cluster's group, a time stamp ("2026-11-20@10:11:12"), import_report_shareable, new:
    {name: the text this import writes there}, --check -> what to do, before this import writes its report, with
    the shareable copy and key an earlier import wrote there (known by their first line), never removed nor
    overwritten (the key of a copy already shared must not be lost): [{name, path, what (copy, key), action,
    is_link, backup, report, report_for, linked, paired}] in the order of SHAREABLE_FILES.

      the file found                                        action
      shareable: one this import does not write (but the    not listed
        .map key), or a file (not a link) it writes the
        same again (but the run's time and build)
      the same again, the other one of its PAIR moved       move (paired)
      a link nothing is written over (off, the .map key)    link: kept (linked: the file it points to is moved)
      a link this import writes over                        move: the link itself, never the file it points to
      under --check, one --check does not write             would_move
      any other                                             move

    backup: where it is moved, its path with .<stamp>~, else .<stamp>.1~, .2~... (the first name free; "" for a
    link kept). report_for: whose report.txt (report.check.txt) is next to it:

      the report next to it                                 report_for
      an import's, naming the cluster's group               the group
      an import's of an earlier release (no group named)    ""
      not an import's                                       None
      none                                                  False"""
    found = []
    for name, report, what, head in SHAREABLE_FILES:
        path = os.path.join(str(report_dir), name)
        if not os.path.isfile(path) or not _first_line(path).startswith(head):
            continue
        writes = shareable and name in (new or {})
        link = os.path.islink(path)
        if shareable and not name.endswith(".map") and not writes:
            continue
        if link and not writes:
            action = "link"
        elif writes and not link and _unstamped(_read(path)) == _unstamped(new[name]):
            action = "same"  # (left in place, unless the other one of its pair is moved)
        elif check and not writes:
            action = "would_move"
        else:
            action = "move"
        report_path = os.path.join(str(report_dir), report)
        found.append({"name": name, "path": path, "what": what, "action": action, "is_link": link, "report": report,
                      "report_for": written_for(_first_line(report_path)) if os.path.isfile(report_path) else False,
                      "paired": False})
    pair_moved = any(f["name"] in PAIR and f["action"] == "move" for f in found)
    said = []
    for f in found:
        if f["action"] == "same":
            if not (pair_moved and f["name"] in PAIR):
                continue
            f.update(action="move", paired=True)
        f["backup"] = _free_name(f["path"], stamp) if f["action"] != "link" else ""
        said.append(f)
    moved = set(os.path.realpath(f["path"]) for f in said if f["action"] in ("move", "would_move"))
    for f in said:
        f["linked"] = f["action"] == "link" and os.path.realpath(f["path"]) in moved
    return said


def cassandra_import_old_shareable_lines(found, report_dir, cluster, shareable=False):
    """cassandra_import_old_shareable's result, the report dir, the cluster's group, import_report_shareable -> the
    lines that say it: why, a row per file, the name it is moved to below it, and whose report is next to it
    when it is not this cluster's.
    e.g. ([{name: "report.shareable.txt", what: "copy", action: "move", report_for: "c1",
    backup: "/inv/report.shareable.txt.2026-11-20@10:11:12~", ...}], "/inv", "c1") -> ["An earlier import's
    shareable copy (import_report_shareable is off),", "in /inv:", "  report.shareable.txt  the copy  moved aside",
    "    to report.shareable.txt.2026-11-20@10:11:12~"]."""
    outcomes = {"move": "moved aside", "would_move": "would be moved aside", "link": "kept: a link"}
    rows, below = [], {}
    for f in found or []:
        outcome = outcomes[f["action"]] + (" to a file moved aside" if f.get("linked") else "")
        if f["action"] in ("move", "would_move"):
            below[len(rows)] = ["    to %s" % os.path.basename(f["backup"])]
        if f["action"] != "link" and f["report_for"] != cluster:
            below.setdefault(len(rows), []).append("    (%s)" % (
                "no %s next to it" % f["report"] if f["report_for"] is False
                else "the %s next to it is %s's" % (f["report"], f["report_for"]) if f["report_for"]
                else "the %s next to it is not this cluster's" % f["report"]))
        if f.get("paired"):
            below.setdefault(len(rows), []).append("    (the same text, moved with its %s)" % (
                "key" if f["what"] == "copy" else "copy"))
        if f.get("is_link") and f["action"] != "link":
            below.setdefault(len(rows), []).append("    (a link: moved, the file it points to unchanged)")
        rows.append([f["name"], "the " + f["what"], outcome])
    lines = []
    for i, line in enumerate(out.table(rows, indent="  ")):
        lines += [line] + below.get(i, [])
    if not shareable:
        why = "An earlier import's shareable copy (import_report_shareable is off),"
    elif all(f["name"].endswith(".map") for f in found or []):
        why = "An earlier import's shareable key (report.shareable.map, a former name),"
    else:
        why = "An earlier import's shareable copy (this import writes new ones),"
    return out.fit([why, "in %s:" % report_dir] + lines, 79)


def cassandra_import_shareable(lines, layout, written=None, report_file="", cwd=None):
    """lines: the report's (cassandra_import_report's, or its text); layout: cassandra_inventory_layout's; written,
    report_file, cwd: cassandra_import_report's (the names of the inventory and report dirs, as the report shows
    them, masked; inventories and reports kept) -> {text: [the lines of the copy to share], map: [the lines of its
    key, "n1 = db11"...]}."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    shown = [out.path_from(p, cwd) for p in list(written or [])[1:] + ([report_file] if report_file else [])]
    key = _key(layout, [p.rsplit("/", 1)[0] if p.endswith(".txt") else p for p in shown])
    text = [COPY_HEAD + ": nodes n1.., clusters c1.., addresses IP, the site's host"
            " names, domains, URLs, paths, buckets and user names replaced (domain1, url1, path1, bucket1, user1..),"
            " secrets masked. A short host name that is not a node's may remain: check before sharing. Its key"
            " stays with whoever ran the import.", ""]
    lines = [_masked(str(line)) for line in lines]  # (the secrets first: no later pass may hide one from it)
    # the names of XML elements the report's XPaths give (name=, ref=): a self-check's setting names them alone
    key.xml_names = set(m.group(4) for line in lines for m in XPATH_VALUE.finditer(line) if m.group(2) in ("name", "ref"))
    # a self-check's setting, on its own line above its values (Node, Ansible): in the file's own words
    named = set(i for i in range(len(lines) - 1) if lines[i].strip() and VALUES.match(lines[i + 1])
                and _indent(lines[i]) < _indent(lines[i + 1]))

    def rendered():
        rows = _value_rows(lines, named, key)
        return [mask(_indent(line) * " " + setting(line.strip(), key, _file_above(lines, i)))
                if i in named and not _site_option(line.strip()) else _masked(_aligned(rows.get(i, line), key))
                for i, line in enumerate(lines)]
    rendered()  # (a first pass: the names a later line gives, a JMX user, masked on the lines before it too)
    text += rendered()
    head = [KEY_HEAD + ": what each name of it stands for. Keep it to yourself.",
            "# Addresses are all shown as IP: not listed."]
    return {"text": text, "map": head + key.lines()}


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_shareable": cassandra_import_shareable,
                "cassandra_import_old_shareable": cassandra_import_old_shareable,
                "cassandra_import_old_shareable_lines": cassandra_import_old_shareable_lines}
