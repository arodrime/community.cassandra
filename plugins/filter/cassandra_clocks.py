# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The clocks of the nodes: what their time sync daemon says, and a verdict.

Cassandra orders the writes to a cell by their timestamps (the latest wins),
and a timestamp is taken from a clock: the client's, or the coordinator's
(a driver without client-side timestamps, the ballots of lightweight
transactions; cqlsh stamps with the clock of its own host); TTLs and tombstones expire by the node's clock. A node whose
clock is ahead stamps writes in the future: an update or a delete of the same
data stamped by a correct clock loses against them until real time catches up.

cassandra_clock_probe: what the probe (clock_probe.yml) printed on a node ->
    {daemon, service, active, installed, enabled, synchronised, source,
    sources, configured, offset (seconds, the node's clock minus its source's:
    positive is ahead), bound (the daemon's error bound, seconds), leap,
    stratum, kernel_synced, container, query (|offset| chronyd -Q measured
    with no daemon running)}. The daemon's own figures are the measure: chrony
    (chronyc tracking and sources), systemd-timesyncd (timedatectl
    timesync-status), ntpd (ntpq -pn).
cassandra_clock_settled: a probe result -> whether waiting longer can change
    it (False while a daemon runs but is not synchronised, or still corrects
    an offset over the threshold): the wait of a time sync just started.
cassandra_clock_verdicts: the nodes' probes, their date samples and context
    -> each node's verdict (ok, off, unsynchronised, no_sync), why, what
    sync_clocks does about it (none, slew, start, step, window, manual), and
    the warnings of the cluster (a clock ahead, unrelated sources, a leap
    second pending, timesyncd). A node no daemon reports on (a container: the
    host's clock) is compared with the others by the dates collected over SSH,
    a coarse measure (tens of milliseconds): only beyond COARSE_MS.
cassandra_clock_lines: the verdicts -> the table, one line per node.
cassandra_clock_refusal: the verdicts -> the REFUSED block of a start
    (create_cluster, add_node), with the way out.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError

# a node no daemon reports on: the dates collected over SSH, only this coarse
COARSE_MS = 1000
# what chronyc tracking says of a clock that follows no source: no reference, or its own (local stratum N)
_NO_REFERENCE = ("00000000", "7F7F0101")
_DAEMONS = {"chronyd": "chrony", "chrony": "chrony", "systemd-timesyncd": "timesyncd", "ntpd": "ntp", "ntp": "ntp",
            "ntpsec": "ntp"}
_SECTION = re.compile(r"^==(\w+)==$")
_UNITS = {"us": 1e-6, "\u00b5s": 1e-6, "\u03bcs": 1e-6, "ms": 1e-3, "s": 1.0, "sec": 1.0, "min": 60.0, "m": 60.0,
          "h": 3600.0, "d": 86400.0}
_SPAN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(us|\u00b5s|\u03bcs|ms|sec|s|min|m|h|d)\b")


def _sections(text):
    out, name = {}, None
    for line in str(text or "").splitlines():
        m = _SECTION.match(line.strip())
        if m:
            name = m.group(1)
            out.setdefault(name, [])
        elif name:
            out[name].append(line.rstrip())
    return out


def _span(text):
    """A systemd time span ("+1.234ms", "-2min 3.5s", "523us") -> seconds, signed; None if none."""
    text = str(text or "").strip()
    sign = -1.0 if text.startswith("-") else 1.0
    parts = _SPAN.findall(text)
    if not parts:
        return None
    return sign * sum(float(n) * _UNITS[u] for n, u in parts)


def _fields(lines, sep=":"):
    out = {}
    for line in lines:
        if sep in line:
            key, value = line.split(sep, 1)
            out[key.strip()] = value.strip()
    return out


def parse_units(lines):
    """'name|active state|enabled state|load state' -> {name: {active, enabled, installed}}."""
    out = {}
    for line in lines:
        parts = (line.split("|") + ["", "", ""])[:4]
        name = parts[0].strip()
        if not name:
            continue
        load = parts[3].strip()
        out[name] = {"active": parts[1].strip() == "active",
                     "enabled": parts[2].strip() in ("enabled", "enabled-runtime", "alias", "static", "indirect"),
                     "installed": load not in ("", "not-found", "masked") or parts[1].strip() == "active",
                     "masked": load == "masked" or parts[2].strip().startswith("masked")}
    return out


def parse_chrony_tracking(lines):
    """chronyc -n tracking -> {ref_id, source, stratum, offset, bound, leap, synchronised}; {} if chronyd did not
    answer (506 Cannot talk to daemon)."""
    f = _fields(lines)
    if "Reference ID" not in f:
        return {}
    m = re.match(r"([0-9A-Fa-f]+)\s*\(([^)]*)\)", f["Reference ID"])
    ref_id, source = (m.group(1).upper(), m.group(2).strip()) if m else (f["Reference ID"].split()[0].upper(), "")
    offset = None
    m = re.match(r"([0-9.]+) seconds (slow|fast)", f.get("System time", ""))
    if m:
        offset = float(m.group(1)) * (1.0 if m.group(2) == "fast" else -1.0)
    delay = re.match(r"([0-9.]+) seconds", f.get("Root delay", ""))
    dispersion = re.match(r"([0-9.]+) seconds", f.get("Root dispersion", ""))
    bound = (float(delay.group(1)) / 2 + float(dispersion.group(1))) if delay and dispersion else None
    leap = f.get("Leap status", "").lower()
    leap = {"normal": "normal", "insert second": "insert", "delete second": "delete"}.get(leap, "unsynchronised")
    stratum = int(f["Stratum"]) if f.get("Stratum", "").isdigit() else None
    synchronised = leap != "unsynchronised" and ref_id not in _NO_REFERENCE and bool(stratum)
    return {"ref_id": ref_id, "source": source if ref_id not in _NO_REFERENCE else "",
            "local": ref_id == "7F7F0101", "stratum": stratum, "offset": offset, "bound": bound, "leap": leap,
            "synchronised": synchronised}


def parse_chrony_sources(lines):
    """chronyc -n sources -> {selected: address or '', count, reachable: how many answered lately}."""
    selected, count, reachable = "", 0, 0
    for line in lines:
        m = re.match(r"^([\^=#])([*+\-?x~ ])\s+(\S+)\s+(\d+)\s+(-?\d+)\s+([0-7]+)\s", line)
        if not m:
            continue
        count += 1
        if int(m.group(6), 8):
            reachable += 1
        if m.group(2) == "*":
            selected = m.group(3)
    return {"selected": selected, "count": count, "reachable": reachable}


def parse_timesync(lines):
    """timedatectl timesync-status -> {source, offset, bound, leap, stratum}; offset None when no server
    answered yet. Its Offset is the server's clock minus the node's (NTP's), the node's is the opposite."""
    f = _fields(lines)
    server = f.get("Server", "")
    source = "" if not server or server.split()[0] in ("n/a", "null", "(null)") else server
    offset = _span(f.get("Offset")) if "Offset" in f else None
    # the server's root distance, plus half the delay to it
    bound = _span(f.get("Root distance", "").split("(")[0]) if "Root distance" in f else None
    if bound is not None and "Delay" in f:
        bound += (_span(f["Delay"]) or 0.0) / 2
    leap = f.get("Leap", "").lower()
    leap = ("normal" if leap == "normal" else "insert" if "extra" in leap or "insert" in leap
            else "delete" if "fewer" in leap or "delete" in leap or "missing" in leap
            else "unsynchronised" if "synchron" in leap or leap == "alarm" else leap)
    stratum = int(f["Stratum"]) if f.get("Stratum", "").isdigit() else None
    return {"source": source, "offset": -float(offset) if offset is not None else None, "bound": bound, "leap": leap,
            "stratum": stratum}


def parse_ntpq(lines):
    """ntpq -pn -> {source, offset, bound, stratum} of the peer selected (*); {} when none is, {local: True}
    when it is ntpd's own clock (127.127.1.x, .LOCL.)."""
    for line in lines:
        cols = line.split()
        if len(cols) >= 10 and cols[0].startswith("*"):
            if cols[0][1:].startswith("127.127.1.") or cols[1] == ".LOCL.":
                return {"local": True}  # its own clock (the local driver): no real source
            try:
                delay, offset = float(cols[7]), float(cols[8])
            except ValueError:
                return {}
            return {"source": cols[0][1:], "offset": -offset / 1000.0, "bound": delay / 2000.0,
                    "stratum": int(cols[2]) if cols[2].isdigit() else None}
    return {}


def cassandra_clock_probe(text):
    """What clock_probe.yml printed on a node -> what its time sync says (see the module's doc)."""
    s = _sections(text)
    units = parse_units(s.get("units", []))
    out = {"daemon": "", "service": "", "active": False, "installed": False, "enabled": False, "masked": False,
           "synchronised": False, "source": "", "sources": 0, "reachable": 0, "configured": [], "offset": None,
           "bound": None, "leap": "", "stratum": None, "local": False, "kernel_synced": None, "container": "",
           "query": None, "window": False, "kernel_only": False}
    # the daemon that runs (several: the first of chrony, timesyncd, ntp), else one installed
    order = ["chronyd", "chrony", "systemd-timesyncd", "ntpd", "ntpsec", "ntp"]
    running = [u for u in order if units.get(u, {}).get("active")]
    installed = [u for u in order if units.get(u, {}).get("installed")]
    name = (running or installed or [""])[0]
    if name:
        unit = units[name]
        out.update({"daemon": _DAEMONS[name], "service": name, "active": unit["active"], "installed": unit["installed"],
                    "enabled": unit["enabled"], "masked": unit["masked"]})
    out["others"] = [u for u in running[1:] if _DAEMONS[u] != out["daemon"]]
    virt = " ".join(s.get("virt", [])).strip()
    out["container"] = "" if virt in ("", "none") else virt
    show = _fields(s.get("timedatectl", []), "=")
    status = _fields(s.get("timedatectl_status", []))
    if "NTPSynchronized" in show:
        out["kernel_synced"] = show["NTPSynchronized"] == "yes"
    else:  # systemd before 239: timedatectl status
        said = status.get("System clock synchronized", status.get("NTP synchronized"))
        if said is not None:
            out["kernel_synced"] = said == "yes"
    out["configured"] = sorted(set(
        line.split()[1] for line in s.get("chrony_conf", []) + s.get("timesync_servers", [])
        if len(line.split()) > 1 and line.split()[0] != "makestep"))
    if out["active"] and out["daemon"] == "chrony":
        tracking = parse_chrony_tracking(s.get("tracking", []))
        sources = parse_chrony_sources(s.get("sources", []))
        if tracking:
            out.update(dict((k, tracking[k]) for k in ("source", "offset", "bound", "leap", "stratum", "local")))
            out["synchronised"] = tracking["synchronised"] and (bool(sources["selected"]) or not s.get("sources"))
            out["source"] = sources["selected"] or tracking["source"] if out["synchronised"] else ""
        out["sources"], out["reachable"] = sources["count"], sources["reachable"]
    elif out["active"] and out["daemon"] == "timesyncd" and not any(x.strip() for x in s.get("timesync", [])):
        # systemd before 239: no timesync-status, only the kernel's word (compared coarsely)
        out["kernel_only"] = True
    elif out["active"] and out["daemon"] == "timesyncd":
        sync = parse_timesync(s.get("timesync", []))
        out.update(dict((k, sync[k]) for k in ("source", "offset", "bound", "leap", "stratum")))
        out["synchronised"] = bool(out["kernel_synced"]) and bool(sync["source"]) and sync["offset"] is not None
        out["sources"] = 1 if sync["source"] else 0
        out["reachable"] = 1 if sync["offset"] is not None else 0
        if not out["synchronised"]:
            out["source"] = ""
        out["configured"] = sorted(set(out["configured"]) | set(
            [sync["source"].split("(")[-1].rstrip(")")] if "(" in sync["source"] else []))
    elif out["active"] and out["daemon"] == "ntp":
        peer = parse_ntpq(s.get("ntpq", []))
        if peer.get("local"):
            out["local"] = True
        elif peer:
            out.update(dict((k, peer[k]) for k in ("source", "offset", "bound", "stratum")))
            out["leap"] = "normal"
        out["synchronised"] = bool(peer) and not peer.get("local")
        out["sources"] = out["reachable"] = 1 if peer else 0
    out["window"] = "window" in s
    # chrony's makestep threshold (a start steps an offset over it in its first updates)
    steps = [line.split() for line in s.get("chrony_conf", []) if line.split()[:1] == ["makestep"]]
    out["makestep"] = None
    for parts in steps:
        try:
            out["makestep"] = float(parts[1])
        except (IndexError, ValueError):
            pass
    query = re.search(r"System clock wrong by (-?[0-9.]+) seconds", "\n".join(s.get("query", [])))
    if query:
        out["query"] = abs(float(query.group(1)))
    return out


def _bad(probe, max_s):
    """Why a probe is not yet as it should be, '' when it is."""
    if not probe.get("active"):
        return "no time sync running"
    if not probe.get("synchronised"):
        return "not synchronised"
    if probe.get("offset") is None:
        return "no offset"
    if abs(probe["offset"]) + (probe.get("bound") or 0.0) > max_s:
        return "off"
    return ""


def cassandra_clock_settled(probe, max_ms=50, synchronised_only=False):
    """Whether waiting longer can change the verdict of this probe: a daemon running but not synchronised yet, or
    still correcting an offset (its own error bound within the threshold), is not settled. synchronised_only: settled
    once synchronised (a step follows)."""
    if not isinstance(probe, dict):
        raise AnsibleFilterError("cassandra_clock_settled: a cassandra_clock_probe result is expected")
    why = _bad(probe, float(max_ms) / 1000)
    if why in ("not synchronised", "no offset"):
        return False
    if why == "off" and not synchronised_only:
        # a bound over half the threshold (a far source): what a wait could still win is too little
        return (probe.get("bound") or 0.0) * 1000 >= float(max_ms) / 2
    return True


def cassandra_clock_in_sync(probe, max_ms=50):
    """Whether a probe says the node is in sync: synchronised, its offset plus the error bound within max_ms."""
    if not isinstance(probe, dict):
        raise AnsibleFilterError("cassandra_clock_in_sync: a cassandra_clock_probe result is expected")
    return not _bad(probe, float(max_ms) / 1000)


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _skew(samples):
    """["<controller time> <node time>", ...] (what clock_probe.yml's raw date printed) -> the node's clock minus
    the controller's, the least delayed sample (the SSH delay only makes a sample later): seconds, or None."""
    diffs = []
    for sample in samples or []:
        times = re.findall(r"[0-9]+\.[0-9]+", str(sample))
        if len(times) >= 2:
            diffs.append(float(times[1]) - float(times[0]))
    return min(diffs) if diffs else None


def ms(seconds, signed=True):
    if seconds is None:
        return "?"
    value = seconds * 1000
    text = ("%+.1f" if signed else "%.1f") % value if abs(value) < 1000 else ("%+.2f s" if signed else "%.2f s") % seconds
    return text if text.endswith(" s") else text + " ms"


def _names(probe, addresses):
    """The source names a node is configured with, its selected source, and the cluster node it follows (a chain)."""
    names = set(probe.get("configured") or [])
    source = (probe.get("source") or "").split(" ")[0]
    if source:
        names.add(source)
        if source in addresses:
            names.add("node:" + addresses[source])
    return names


def cassandra_clock_verdicts(nodes, max_ms=50, step_ms=400, coarse_ms=COARSE_MS):
    """nodes: [{node, probe (cassandra_clock_probe), samples ([[controller, node] times]), running (Cassandra runs
    there), manage (cassandra_linux manages its time sync), offline (cassandra_offline), addresses (the node's
    own)}] -> {nodes: [{node, verdict, ahead, offset, how, text, why, action, ...}], ok: bool, warnings: [str],
    notes: [str], spread}. A node is ok when its daemon is synchronised and its offset plus the daemon's error
    bound is within max_ms; with no daemon to ask (a container: the host's clock), when the dates collected over
    SSH put it within coarse_ms of the others."""
    max_s, step_s, coarse_s = float(max_ms) / 1000, float(step_ms) / 1000, float(coarse_ms) / 1000
    nodes = [dict(n) for n in nodes or []]
    for n in nodes:
        n["probe"] = n.get("probe") or {}
        n["skew"] = _skew(n.get("samples"))
    # true time as the controller's clock sees it: the nodes whose daemon gives their offset, else all of them
    known = [n["skew"] - n["probe"]["offset"] for n in nodes
             if n["skew"] is not None and n["probe"].get("synchronised") and n["probe"].get("offset") is not None]
    ref = _median(known) if known else _median([n["skew"] for n in nodes if n["skew"] is not None])
    referenced = bool(known)
    skews = [n["skew"] for n in nodes if n["skew"] is not None]
    addresses = {}
    for n in nodes:
        for a in n.get("addresses") or []:
            addresses[str(a)] = n["node"]
    out, warnings, notes = [], [], []
    for n in nodes:
        p = n["probe"]
        coarse = (n["skew"] - ref) if n["skew"] is not None and ref is not None else None
        v = {"node": n["node"], "daemon": p.get("daemon", ""), "service": p.get("service", ""),
             "active": bool(p.get("active")), "synchronised": bool(p.get("synchronised")), "source": p.get("source", ""),
             "offset": p.get("offset"), "bound": p.get("bound"), "coarse": coarse, "how": "daemon",
             "running": bool(n.get("running")), "container": p.get("container", ""), "leap": p.get("leap", ""),
             "ahead": False, "why": "", "action": "none", "estimate": None}
        why = _bad(p, max_s) if not p.get("kernel_only") else "kernel only"
        if p.get("kernel_only") and p.get("kernel_synced") is not True:
            why = "not synchronised"
        if not why:
            v["verdict"] = "ok"
        elif why == "off":
            v["verdict"] = "off"
            v["ahead"] = p["offset"] > 0
            if (p.get("bound") or 0.0) > max_s:
                v["why"] = ("its daemon's error bound alone is %s (root delay and dispersion): a closer or better"
                            " source is needed" % ms(p["bound"], False))
                v["action"] = "manual"
            else:
                v["why"] = "%s its source by %s, more than %s" % ("ahead of" if v["ahead"] else "behind",
                                                                  ms(abs(p["offset"]), False), ms(max_s, False))
                v["estimate"] = abs(p["offset"])
                # what the daemon slews by itself (timesyncd steps past 400 ms, ntpd past 128 ms, on their own)
                slews = min(step_s, {"timesyncd": 0.4, "ntp": 0.128}.get(p.get("daemon"), step_s))
                v["action"] = "slew" if abs(p["offset"]) <= slews else ("window" if v["running"] else "step")
                if v["action"] in ("window", "step") and p.get("daemon") == "ntp":
                    v["action"] = "manual"  # no step of ours for ntpd: its own start (ntpd -g) or chrony
                    v["why"] += "; ntpd steps it at its start (-g), or move the node to chrony"
                if v["action"] == "slew" and p.get("daemon") != "chrony":
                    v["why"] += (" (%s slews at most 0.5 ms per second: about %d min)"
                                 % (p.get("service"), abs(p["offset"]) / 0.0005 // 60 + 1))
        elif why in ("not synchronised", "no offset"):
            v["verdict"] = "unsynchronised"
            if p.get("local"):
                v["why"] = "%s follows only its own clock (local stratum): no real source" % p.get("service")
            elif p.get("sources", 0) == 0:
                v["why"] = "%s has no source configured" % p.get("service")
            elif p.get("reachable", 0) == 0:
                v["why"] = "%s reaches none of its %d sources" % (p.get("service"), p["sources"])
            else:
                v["why"] = "%s has not synchronised to any of its sources" % p.get("service")
            v["action"] = "manual"
        else:  # no daemon running (or one the probe can only ask the kernel about)
            # the kernel synchronised and no daemon of ours installed, or one disabled on a node whose time sync is
            # left alone (PTP: chronyd disabled, phc2sys sets the clock); one only stopped leaves the flag set for hours
            other = (not p.get("container") and p.get("kernel_synced") is True
                     and (not p.get("installed") or not p.get("enabled") and not n.get("manage", True)))
            other = other or p.get("kernel_only") and p.get("kernel_synced") is True
            if other:
                # disciplined by a daemon the probe does not know (PTP, openntpd, the hypervisor's): compared coarsely
                v["how"] = "coarse"
                if coarse is not None and referenced and abs(coarse) > coarse_s:
                    v["verdict"], v["ahead"], v["action"] = "off", coarse > 0, "manual"
                    v["why"] = ("synchronised by another means (the kernel says so), %s the others by about %s (dates"
                                " over SSH, coarse)" % ("ahead of" if coarse > 0 else "behind", ms(abs(coarse), False)))
                else:
                    v["verdict"] = "ok"
                    v["why"] = ("%s, synchronised (the kernel says so; this systemd tells no offset)" % p["service"]
                                if p.get("kernel_only") else
                                "synchronised by another means (the kernel says so; %s disabled, its time sync left alone)"
                                % p["service"] if p.get("installed") else
                                "synchronised by another means (the kernel says so; no chrony, timesyncd or ntpd)")
            elif p.get("container"):
                # the host's clock (a container shares its kernel): the host syncs it, the dates say how far it is
                v["how"] = "coarse"
                if coarse is None or not referenced and len(skews) < 2:
                    v["verdict"], v["why"] = "ok", "the host's clock (%s container)" % p["container"]
                elif abs(coarse) > coarse_s:
                    v["verdict"], v["ahead"] = "off", coarse > 0
                    v["why"] = ("the host's clock (%s container), %s the others by about %s (dates over SSH, coarse)"
                                % (p["container"], "ahead of" if coarse > 0 else "behind", ms(abs(coarse), False)))
                    v["action"] = "manual"
                elif p.get("kernel_synced") is False:
                    v["verdict"], v["action"] = "unsynchronised", "manual"
                    v["why"] = "the host's clock (%s container), its kernel says not synchronised" % p["container"]
                else:
                    v["verdict"] = "ok"
                    v["why"] = "the host's clock (%s container), within %s of the others (coarse)" % (
                        p["container"], ms(coarse_s, False))
            else:
                v["verdict"] = "no_sync"
                v["why"] = ("%s %s" % (p["service"], "masked" if p.get("masked") else "installed, not running")
                            if p.get("installed") else "no time sync installed")
                # how far it is: chronyd -Q measured it, else the dates against the nodes that are synchronised
                v["estimate"] = p.get("query") if p.get("query") is not None else (
                    abs(coarse) if coarse is not None and referenced else None)
                if v["estimate"] is not None:
                    v["ahead"] = coarse is not None and referenced and coarse > 0
                if not n.get("manage", True):
                    v["action"] = "manual"
                    v["why"] += " (its time sync is left alone: %s false)" % (n.get("unmanaged_by") or "cassandra_linux_timesync")
                elif p.get("daemon") == "ntp":
                    v["action"] = "manual"  # installing chrony or timesyncd would replace the site's ntpd
                    v["why"] += " (start ntpd, or move the node to chrony)"
                elif p.get("masked"):
                    v["action"] = "manual"
                elif n.get("offline") and not p.get("installed"):
                    v["action"] = "manual"
                    v["why"] += " (cassandra_offline: install chrony from your mirror)"
                elif not v["running"]:
                    v["action"] = "step"  # started, synchronised, stepped if still off: nothing runs to be moved
                elif p.get("query") is not None and p["query"] < 0.9 * _start_limit(p, step_s):
                    v["action"] = "start"  # measured small (chronyd -Q): the daemon slews it, Cassandra running
                else:
                    v["action"] = "window"
        if p.get("container") and v["action"] in ("step", "window"):
            v["action"] = "manual"
            v["why"] += "; its clock is the host's (%s container): step the host's, never from a container" % (
                p["container"])
        v["text"] = _describe(v, p)
        out.append(v)
        if p.get("others"):
            warnings.append("%s runs several time sync daemons (%s and %s): they fight over the clock, keep one"
                            % (n["node"], p["service"], ", ".join(p["others"])))
    # the warnings of the cluster
    # a node where Cassandra runs (it may have stamped writes), ahead by what its daemon says, or by more than the
    # dates over SSH can tell
    ahead = [v for v in out if v["ahead"] and v["verdict"] != "ok" and v["running"]
             and (v["offset"] is not None and v["how"] == "daemon" and v["active"]
                  or abs(v["coarse"] or v["estimate"] or 0) > coarse_s)]
    for v in ahead:
        amount = v["offset"] if v["offset"] is not None and v["how"] == "daemon" and v["active"] else v["coarse"] or v["estimate"]
        warnings.append(
            "%s's clock is AHEAD by about %s: the writes it stamped as coordinator (lightweight transactions,"
            " drivers without client-side timestamps) carry future timestamps. An update or a delete of the same data from"
            " a correct clock in the next %s loses against them (the latest timestamp wins), silently; TTLs it"
            " set expire late, its compactions purge tombstones early. Check what was written through it lately."
            % (v["node"], ms(abs(amount or 0), False), ms(abs(amount or 0), False)))
    groups = []
    for n, v in zip(nodes, out):
        if not v["synchronised"]:
            continue
        names = _names(n["probe"], addresses) | {"node:" + n["node"]}
        joined = [g for g in groups if g["names"] & names]
        merged = {"names": set(names), "nodes": [n["node"]]}
        for g in joined:
            merged["names"] |= g["names"]
            merged["nodes"] = g["nodes"] + merged["nodes"]
            groups.remove(g)
        groups.append(merged)
    if len(groups) > 1:
        said = "; ".join("%s: %s" % (", ".join(g["nodes"]), ", ".join(sorted(x for x in g["names"] if not x.startswith("node:"))) or "?")
                         for g in groups)
        warnings.append(
            "the nodes follow unrelated time sources (%s): each is within its source, but the sources may differ from"
            " each other. Give every node the same servers (chrony.conf's server/pool lines), and never mix sources"
            " that smear leap seconds with sources that step them." % said)
    leaping = [v["node"] for v in out if v["leap"] in ("insert", "delete")]
    if leaping:
        warnings.append(
            "a leap second is pending on %s: nodes whose sources smear it and nodes that step it differ by up to"
            " 1 s for hours around it." % ", ".join(leaping))
    timesyncd = [v["node"] for v in out if v["daemon"] == "timesyncd" and v["active"]]
    if timesyncd:
        notes.append("systemd-timesyncd on %s: one server at a time, no error bound of its own, and it steps the clock"
                     " by itself past 0.4 s, Cassandra running or not; chrony is the safer choice (the collection keeps"
                     " chrony where it is installed)." % ", ".join(timesyncd))
    if any(v["how"] == "coarse" for v in out):
        notes.append("a node without its own time sync daemon (a container) is compared by the dates collected over"
                     " SSH: a coarse measure, only a gap over %s counts." % ms(coarse_s, False))
    spread = (max(skews) - min(skews)) if len(skews) > 1 else None
    return {"nodes": out, "ok": all(v["verdict"] == "ok" for v in out), "warnings": warnings, "notes": notes,
            "spread": spread, "max_ms": float(max_ms)}


def _start_limit(p, step_s):
    """The offset a daemon started on this node slews rather than steps: chrony's makestep threshold (the site's
    chrony.conf), timesyncd's 400 ms, and never over cassandra_clock_step_ms."""
    limit = step_s
    if p.get("makestep") is not None and p.get("service") in ("chronyd", "chrony", ""):
        limit = min(limit, p["makestep"])
    if p.get("service") == "systemd-timesyncd":
        limit = min(limit, 0.4)
    return limit


def _describe(v, p):
    """One node, as the table shows it: daemon, synchronised, source, offset (+/- bound)."""
    if v["how"] == "coarse" and not v["container"]:
        return v["why"]
    if v["how"] == "coarse":
        return "the host's clock (%s container)%s" % (
            v["container"], (", %s vs the others (coarse)" % ms(v["coarse"])) if v["coarse"] is not None else "")
    if not v["active"]:
        if v["estimate"] is None:
            return v["why"]
        how = ", chronyd -Q" if p.get("query") is not None else ", dates over SSH, coarse"
        return v["why"] + " (about %s off%s)" % (ms(v["estimate"], False), how)
    if not v["synchronised"]:
        return "%s, NOT synchronised" % v["service"]
    bound = ms(v["bound"], False) if v["bound"] is not None else "?"
    return "%s, synchronised to %s, %s (\u00b1%s)" % (v["service"], v["source"] or "?", ms(v["offset"]), bound)


VERDICT_WORDS = {"ok": "ok", "off": "OFF", "unsynchronised": "NOT SYNCHRONISED", "no_sync": "NO TIME SYNC",
                 "unread": "NOT READ"}
ACTION_WORDS = {"none": "", "slew": "being corrected by its daemon (slew)", "start": "time sync started live",
                "step": "clock stepped now (Cassandra not running)",
                "window": "clock stepped in a restart window (drain, stop, step, start)",
                "manual": "to fix by hand"}


def cassandra_clock_lines(verdicts, actions=True):
    """The verdicts -> one line per node: name, verdict, what its time sync says, then (actions) what sync_clocks
    does about it."""
    rows = []
    for v in (verdicts or {}).get("nodes", []):
        what = v["text"] + ((": " + v["why"]) if v["verdict"] != "ok" and v["why"] and v["why"] not in v["text"] else "")
        act = ACTION_WORDS.get(v["action"], "") if actions else ""
        rows.append([v["node"], VERDICT_WORDS.get(v["verdict"], v["verdict"]), what + ((" \u2192 " + act) if act else "")])
    if not rows:
        return []
    width = [max(len(r[i]) for r in rows) for i in range(2)]
    return [("  %s  %s  %s" % (r[0].ljust(width[0]), r[1].ljust(width[1]), r[2])).rstrip() for r in rows]


def cassandra_clock_refusal(verdicts, operation="", cluster="", command="", check=False, later_ok=False, unread=None):
    """The verdicts of a start (create_cluster, add_node) -> its REFUSED block (under check: what a real run would
    refuse), the nodes not in sync, why, and the way out; [] when every node is. check, later_ok (a check before
    cassandra_linux ran): a node not running with no time sync, which cassandra_linux would set up, is left to the
    check after it. unread: {node: why} of the nodes that answered but whose clock could not be read: refused."""
    verdicts = verdicts or {}
    bad = [v for v in verdicts.get("nodes", []) if v["verdict"] != "ok"]
    bad += [{"node": n, "verdict": "unread", "text": "its clock could not be read", "why": str(why or ""),
             "action": "manual", "running": False} for n, why in sorted((unread or {}).items())]
    later = [v["node"] for v in bad if (check or later_ok) and v["verdict"] == "no_sync" and v["action"] != "manual"
             and (later_ok or not v["running"])]  # (before a wipe: every node stopped, then set up)
    bad = [v for v in bad if v["node"] not in later]
    if not bad:
        return ["NOTE  clocks: time sync would be started on %s (cassandra_linux), its clock checked %s"
                % (", ".join(later), "by the real run" if check else "before the starts")] if later and check else []
    limit = ms(verdicts.get("max_ms", 50) / 1000, False)
    head = ["REFUSED" if not check else "NOTE", "clocks", cluster,
            "nothing was started" if not check else "a real run would refuse to start the nodes"]
    lines = ["  ".join(x for x in head if x)]
    lines += cassandra_clock_lines({"nodes": bad}, actions=False)
    lines += ["", "Cassandra needs the clocks of its nodes within %s of each other (cassandra_clock_max_offset_ms):"
                  " each write carries a timestamp, the latest wins." % limit]
    lines += ["WARNING  %s" % w for w in verdicts.get("warnings", [])]
    if later:
        lines += ["NOTE  time sync would be started on %s (cassandra_linux), its clock checked by the real run"
                  % ", ".join(later)]
    lines += ["", "TO DO", "  1. bring the clocks in sync (shows each node's time sync, starts it where missing):",
              "     " + (command or "ansible-playbook community.cassandra.sync_clocks"),
              "  2. or fix the time sync of these nodes (chrony.conf's server/pool lines, reachable from them),"
              " then run %s again" % (operation or "it"),
              "  (-e cassandra_clock_check=false skips this check, for clocks kept in sync by other means)"]
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_clock_probe": cassandra_clock_probe, "cassandra_clock_settled": cassandra_clock_settled,
                "cassandra_clock_in_sync": cassandra_clock_in_sync,
                "cassandra_clock_verdicts": cassandra_clock_verdicts, "cassandra_clock_lines": cassandra_clock_lines,
                "cassandra_clock_refusal": cassandra_clock_refusal}
