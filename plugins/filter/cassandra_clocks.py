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
    it (False while a daemon runs but is not synchronised, is still converging
    - an error bound over CONVERGING_S, a daemon just started - or still
    corrects an offset over the threshold): the wait of a time sync just started.
cassandra_clock_wait_line: a probe not settled, the node, the timeout -> the
    progress line of that wait.
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
cassandra_clock_nodes: the nodes whose clock was read (clocks_read.yml), as
    cassandra_clock_verdicts takes them.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output


# a node no daemon reports on: the dates collected over SSH, only this coarse
COARSE_MS = 1000
# an error bound over this (and over 10 times the threshold): a daemon just started, its skew still estimated
# from a few samples, not a far source; it shrinks with the next samples (chrony: its first polls, ~64 s apart)
CONVERGING_S = 1.0
# chrony's skew still over this (ppm): its frequency still estimated from its first samples (1000000 with no
# drift file yet, its error bound then a sawtooth from ~0 at each update, 1 s more per second since it; then
# down to a few ppm over its next polls); a daemon settled says a few ppm at most
CONVERGING_SKEW_PPM = 100.0
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
    """chronyc -n tracking -> {ref_id, source, stratum, offset, bound, leap, synchronised, skew (ppm)}; {} if
    chronyd did not answer (506 Cannot talk to daemon)."""
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
    skew = re.match(r"([0-9.]+) ppm", f.get("Skew", ""))
    return {"ref_id": ref_id, "source": source if ref_id not in _NO_REFERENCE else "",
            "local": ref_id == "7F7F0101", "stratum": stratum, "offset": offset, "bound": bound, "leap": leap,
            "synchronised": synchronised, "skew": float(skew.group(1)) if skew else None}


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
    bound = _span(f.get("Root distance", "").split("(", 1)[0]) if "Root distance" in f else None
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


# the time sync daemons, in the order a probe picks one (several running: the first)
_ORDER = ["chronyd", "chrony", "systemd-timesyncd", "ntpd", "ntpsec", "ntp"]


def _pick_daemon(units):
    """parse_units' result -> (the unit of the daemon that runs, else of one installed, '' if none; the units
    running, in _ORDER)."""
    running = [u for u in _ORDER if units.get(u, {}).get("active")]
    installed = [u for u in _ORDER if units.get(u, {}).get("installed")]
    return (running or installed or [""])[0], running


def _kernel_synced(s):
    """The probe's sections -> whether the kernel says its clock is synchronised, None when it does not say."""
    show = _fields(s.get("timedatectl", []), "=")
    if "NTPSynchronized" in show:
        return show["NTPSynchronized"] == "yes"
    # systemd before 239: timedatectl status
    status = _fields(s.get("timedatectl_status", []))
    said = status.get("System clock synchronized", status.get("NTP synchronized"))
    return said == "yes" if said is not None else None


def _configured(s):
    """The probe's sections -> the sources configured (chrony.conf's and timesyncd's server/pool lines), sorted."""
    return sorted(set(
        line.split()[1] for line in s.get("chrony_conf", []) + s.get("timesync_servers", [])
        if len(line.split()) > 1 and line.split()[0] != "makestep"))


def _chrony_said(s):
    """The probe's sections, chronyd running -> the fields its tracking and sources give."""
    tracking = parse_chrony_tracking(s.get("tracking", []))
    sources = parse_chrony_sources(s.get("sources", []))
    said = {}
    if tracking:
        said.update(dict((k, tracking[k]) for k in ("source", "offset", "bound", "skew", "leap", "stratum", "local")))
        said["synchronised"] = tracking["synchronised"] and (bool(sources["selected"]) or not s.get("sources"))
        said["source"] = sources["selected"] or tracking["source"] if said["synchronised"] else ""
    said["sources"], said["reachable"] = sources["count"], sources["reachable"]
    return said


def _timesyncd_said(s, kernel_synced, configured):
    """The probe's sections, timesyncd running and telling its status, the kernel's word, the sources configured
    -> the fields its timesync-status gives (its server added to the sources configured)."""
    sync = parse_timesync(s.get("timesync", []))
    said = dict((k, sync[k]) for k in ("source", "offset", "bound", "leap", "stratum"))
    said["synchronised"] = bool(kernel_synced) and bool(sync["source"]) and sync["offset"] is not None
    said["sources"] = 1 if sync["source"] else 0
    said["reachable"] = 1 if sync["offset"] is not None else 0
    if not said["synchronised"]:
        said["source"] = ""
    said["configured"] = sorted(set(configured) | set(
        [sync["source"].rsplit("(", 1)[-1].rstrip(")")] if "(" in sync["source"] else []))
    return said


def _ntp_said(s):
    """The probe's sections, ntpd running -> the fields its selected peer gives."""
    peer = parse_ntpq(s.get("ntpq", []))
    said = {}
    if peer.get("local"):
        said["local"] = True
    elif peer:
        said.update(dict((k, peer[k]) for k in ("source", "offset", "bound", "stratum")))
        said["leap"] = "normal"
    said["synchronised"] = bool(peer) and not peer.get("local")
    said["sources"] = said["reachable"] = 1 if peer else 0
    return said


def _daemon_said(daemon, s, kernel_synced, configured):
    """The daemon running (chrony, timesyncd, ntp), the probe's sections -> the fields its own figures give."""
    if daemon == "chrony":
        return _chrony_said(s)
    if daemon == "timesyncd" and not any(x.strip() for x in s.get("timesync", [])):
        # systemd before 239: no timesync-status, only the kernel's word (compared coarsely)
        return {"kernel_only": True}
    if daemon == "timesyncd":
        return _timesyncd_said(s, kernel_synced, configured)
    if daemon == "ntp":
        return _ntp_said(s)
    return {}


def _makestep(conf):
    """chrony.conf's lines -> its makestep threshold (a start steps an offset over it in its first updates):
    seconds, the last one readable; None if none."""
    threshold = None
    for parts in [line.split() for line in conf if line.split()[:1] == ["makestep"]]:
        try:
            threshold = float(parts[1])
        except (IndexError, ValueError):
            pass
    return threshold


def _query(lines):
    """chronyd -Q's output -> |the offset it measured| (seconds), None if it did not say."""
    query = re.search(r"System clock wrong by (-?[0-9.]+) seconds", "\n".join(lines))
    return abs(float(query.group(1))) if query else None


def cassandra_clock_probe(text):
    """What clock_probe.yml printed on a node -> what its time sync says (see the module's doc).
    e.g. a probe whose ==units== section says "chronyd|active|enabled|loaded" and whose chronyc tracking says
    "0.000012000 seconds fast" -> {daemon: "chrony", service: "chronyd", active: True, synchronised: True, source:
    "192.0.2.1", offset: 1.2e-05, bound: 0.0007, ...}."""
    s = _sections(text)
    units = parse_units(s.get("units", []))
    out = {"daemon": "", "service": "", "active": False, "installed": False, "enabled": False, "masked": False,
           "synchronised": False, "source": "", "sources": 0, "reachable": 0, "configured": [], "offset": None,
           "bound": None, "skew": None, "leap": "", "stratum": None, "local": False, "kernel_synced": None, "container": "",
           "query": None, "window": False, "kernel_only": False}
    name, running = _pick_daemon(units)
    if name:
        unit = units[name]
        out.update({"daemon": _DAEMONS[name], "service": name, "active": unit["active"], "installed": unit["installed"],
                    "enabled": unit["enabled"], "masked": unit["masked"]})
    out["others"] = [u for u in running[1:] if _DAEMONS[u] != out["daemon"]]
    virt = " ".join(s.get("virt", [])).strip()
    out["container"] = "" if virt in ("", "none") else virt
    out["kernel_synced"] = _kernel_synced(s)
    out["configured"] = _configured(s)
    if out["active"]:
        out.update(_daemon_said(out["daemon"], s, out["kernel_synced"], out["configured"]))
    out["window"] = "window" in s
    out["makestep"] = _makestep(s.get("chrony_conf", []))
    out["query"] = _query(s.get("query", []))
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


def _converging(probe, max_s):
    if probe.get("daemon") == "chrony" and (probe.get("skew") or 0.0) > CONVERGING_SKEW_PPM:
        return True
    return (probe.get("bound") or 0.0) > max(CONVERGING_S, 10 * max_s)


def cassandra_clock_settled(probe, max_ms=50, synchronised_only=False):
    """Whether waiting longer can change the verdict of this probe: a daemon running but not synchronised yet,
    still converging (an error bound over CONVERGING_S, or chrony's skew over CONVERGING_SKEW_PPM: just started),
    or still correcting an offset (its own error bound within the threshold), is not settled. synchronised_only:
    settled once synchronised (a step follows).
    Input: a cassandra_clock_probe result; output: bool. e.g. a probe synchronised, 0.012 ms off -> True;
    chronyd synchronised but 200 ms off, its error bound 1 ms (still slewing) -> False."""
    if not isinstance(probe, dict):
        raise AnsibleFilterError("cassandra_clock_settled: a cassandra_clock_probe result is expected")
    why = _bad(probe, float(max_ms) / 1000)
    if why in ("not synchronised", "no offset"):
        return False
    if why == "off" and not synchronised_only:
        if _converging(probe, float(max_ms) / 1000):
            return False
        # a bound over half the threshold (a far source): what a wait could still win is too little
        return (probe.get("bound") or 0.0) * 1000 >= float(max_ms) / 2
    return True


def cassandra_clock_wait_line(probe, node, timeout, max_ms=50):
    """The progress line of a wait for a clock not settled (cassandra_clock_settled false): what it waits for,
    and for how long at most. "node4: chronyd converging (\u00b162.04 s), waiting up to 3m".
    Input: a probe, the node, the timeout (seconds); output: the line. e.g. (a probe not synchronised, "node4",
    180) -> "node4: chronyd not synchronised yet, waiting up to 3m"."""
    if not isinstance(probe, dict):
        raise AnsibleFilterError("cassandra_clock_wait_line: a cassandra_clock_probe result is expected")
    service = probe.get("service") or "its time sync"
    if not probe.get("synchronised") or probe.get("offset") is None:
        what = "%s not synchronised yet" % service
    elif _converging(probe, float(max_ms) / 1000):
        what = "%s converging (\u00b1%s)" % (service, ms(probe.get("bound"), False))
    else:
        what = "%s correcting %s" % (service, ms(probe.get("offset")))
    return "%s: %s, waiting up to %s" % (node, what, cassandra_output.duration(int(timeout), True))


def cassandra_clock_in_sync(probe, max_ms=50):
    """Whether a probe says the node is in sync: synchronised, its offset plus the error bound within max_ms.
    Input: a cassandra_clock_probe result; output: bool. e.g. 0.012 ms off, bound 0.7 ms -> True; 200 ms off -> False."""
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
    source = (probe.get("source") or "").split(" ", 1)[0]
    if source:
        names.add(source)
        if source in addresses:
            names.add("node:" + addresses[source])
    return names


def _reference(nodes):
    """The nodes (probe, skew set) -> (true time as the controller's clock sees it, whether the daemons' offsets
    gave it): the median of the nodes whose daemon gives their offset, else of all of them; None with no dates."""
    known = [n["skew"] - n["probe"]["offset"] for n in nodes
             if n["skew"] is not None and n["probe"].get("synchronised") and n["probe"].get("offset") is not None]
    ref = _median(known) if known else _median([n["skew"] for n in nodes if n["skew"] is not None])
    return ref, bool(known)


def _off(p, running, max_s, step_s):
    """A probe whose offset plus error bound is over max_s, whether Cassandra runs there -> the verdict's fields:
    why (converging, a far source, or how far) and what sync_clocks does (slew, step, window, manual)."""
    v = {"verdict": "off", "ahead": p["offset"] > 0}
    if (p.get("bound") or 0.0) > max_s and _converging(p, max_s):
        v["why"] = ("%s still converging (just started: error bound %s): run again in a few minutes, or"
                    " raise cassandra_clock_sync_timeout"
                    % (p.get("service") or "its time sync", ms(p["bound"], False)))
        v["action"] = "manual"
    elif (p.get("bound") or 0.0) > max_s:
        v["why"] = ("its daemon's error bound alone is %s (root delay and dispersion): a closer or better"
                    " source is needed" % ms(p["bound"], False))
        v["action"] = "manual"
    else:
        side = "ahead of" if v["ahead"] else "behind"
        if abs(p["offset"]) > max_s:
            v["why"] = "%s its source by %s, more than %s" % (side, ms(abs(p["offset"]), False), ms(max_s, False))
        else:  # within the threshold alone, over it with the daemon's error bound
            v["why"] = "%s %s its source, within %s but not with its error bound of %s" % (
                ms(abs(p["offset"]), False), side, ms(max_s, False), ms(p.get("bound"), False))
        v["estimate"] = abs(p["offset"])
        # what the daemon slews by itself (timesyncd steps past 400 ms, ntpd past 128 ms, on their own)
        slews = min(step_s, {"timesyncd": 0.4, "ntp": 0.128}.get(p.get("daemon"), step_s))
        v["action"] = "slew" if abs(p["offset"]) <= slews else ("window" if running else "step")
        if v["action"] in ("window", "step") and p.get("daemon") == "ntp":
            v["action"] = "manual"  # no step of ours for ntpd: its own start (ntpd -g) or chrony
            v["why"] += "; ntpd steps it at its start (-g), or move the node to chrony"
        if v["action"] == "slew" and p.get("daemon") != "chrony":
            v["why"] += (" (%s slews at most 0.5 ms per second: about %d min)"
                         % (p.get("service"), abs(p["offset"]) / 0.0005 // 60 + 1))
    return v


def _unsynchronised(p):
    """A probe whose daemon runs but is not synchronised (or gives no offset) -> the verdict's fields."""
    if p.get("local"):
        why = "%s follows only its own clock (local stratum): no real source" % p.get("service")
    elif p.get("sources", 0) == 0:
        why = "%s has no source configured" % p.get("service")
    elif p.get("reachable", 0) == 0:
        why = "%s reaches none of its %d sources" % (p.get("service"), p["sources"])
    else:
        why = "%s has not synchronised to any of its sources" % p.get("service")
    return {"verdict": "unsynchronised", "why": why, "action": "manual"}


def _other_means(p, coarse, referenced, coarse_s):
    """A node the kernel says synchronised by a daemon the probe does not know (PTP, openntpd, the hypervisor's),
    its gap to the others by the dates (coarse), whether the daemons' offsets gave the reference -> the verdict's
    fields: compared coarsely."""
    v = {"how": "coarse"}
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
    return v


def _host_clock(p, coarse, referenced, dated, coarse_s):
    """A container's probe (the host's clock: the host syncs it), its gap to the others by the dates, whether the
    daemons' offsets gave the reference, how many nodes gave dates -> the verdict's fields."""
    v = {"how": "coarse"}
    if coarse is None or not referenced and dated < 2:
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
    return v


def _no_sync(p, n, coarse, referenced, running, step_s):
    """A node with no time sync running, its gap to the others by the dates, whether the daemons' offsets gave the
    reference, whether Cassandra runs there -> the verdict's fields: how far it is (chronyd -Q, else the dates) and
    what sync_clocks does (start, step, window, manual)."""
    v = {"verdict": "no_sync"}
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
    elif not running:
        v["action"] = "step"  # started, synchronised, stepped if still off: nothing runs to be moved
    elif p.get("query") is not None and p["query"] < 0.9 * _start_limit(p, step_s):
        v["action"] = "start"  # measured small (chronyd -Q): the daemon slews it, Cassandra running
    else:
        v["action"] = "window"
    return v


def _no_daemon(p, n, coarse, referenced, dated, running, coarse_s, step_s):
    """A node no daemon of the probe reports on (none running, or one it can only ask the kernel about) -> the
    verdict's fields: synchronised by other means, the host's clock (a container), or no time sync."""
    # the kernel synchronised and no daemon of ours installed, or one disabled on a node whose time sync is
    # left alone (PTP: chronyd disabled, phc2sys sets the clock); one only stopped leaves the flag set for hours
    other = (not p.get("container") and p.get("kernel_synced") is True
             and (not p.get("installed") or not p.get("enabled") and not n.get("manage", True)))
    other = other or p.get("kernel_only") and p.get("kernel_synced") is True
    if other:
        return _other_means(p, coarse, referenced, coarse_s)
    if p.get("container"):
        return _host_clock(p, coarse, referenced, dated, coarse_s)
    return _no_sync(p, n, coarse, referenced, running, step_s)


def _node_verdict(n, ref, referenced, dated, max_s, step_s, coarse_s):
    """One node (probe, skew set), the reference time, whether the daemons' offsets gave it, how many nodes gave
    dates, the limits (seconds) -> its verdict {node, verdict, ahead, offset, how, text, why, action, ...}."""
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
        v.update(_off(p, v["running"], max_s, step_s))
    elif why in ("not synchronised", "no offset"):
        v.update(_unsynchronised(p))
    else:
        v.update(_no_daemon(p, n, coarse, referenced, dated, v["running"], coarse_s, step_s))
    if p.get("container") and v["action"] in ("step", "window"):
        v["action"] = "manual"
        v["why"] += "; its clock is the host's (%s container): step the host's, never from a container" % (
            p["container"])
    v["text"] = _describe(v, p)
    return v


def _ahead_warnings(out, coarse_s):
    """The verdicts -> the warnings of the nodes where Cassandra runs (it may have stamped writes) ahead by what
    their daemon says, or by more than the dates over SSH can tell."""
    ahead = [v for v in out if v["ahead"] and v["verdict"] != "ok" and v["running"]
             and (v["offset"] is not None and v["how"] == "daemon" and v["active"]
                  or abs(v["coarse"] or v["estimate"] or 0) > coarse_s)]
    warnings = []
    for v in ahead:
        amount = v["offset"] if v["offset"] is not None and v["how"] == "daemon" and v["active"] else v["coarse"] or v["estimate"]
        warnings.append(
            "%s's clock is AHEAD by about %s: the writes it stamped as coordinator (lightweight transactions,"
            " drivers without client-side timestamps) carry future timestamps. An update or a delete of the same data from"
            " a correct clock in the next %s loses against them (the latest timestamp wins), silently; TTLs it"
            " set expire late, its compactions purge tombstones early. Check what was written through it lately."
            % (v["node"], ms(abs(amount or 0), False), ms(abs(amount or 0), False)))
    return warnings


def _source_groups(nodes, out, addresses):
    """The nodes, their verdicts, {address: node} -> the groups of synchronised nodes that share a source (or follow
    each other), [{names, nodes}], in the order they formed."""
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
    return groups


def _cluster_warnings(nodes, out, addresses, coarse_s):
    """The nodes, their verdicts, {address: node} -> the warnings of the cluster: a clock ahead, unrelated sources,
    a leap second pending."""
    warnings = _ahead_warnings(out, coarse_s)
    groups = _source_groups(nodes, out, addresses)
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
    return warnings


def _cluster_notes(out, coarse_s):
    """The verdicts -> the notes: timesyncd's limits, the coarse measure of the nodes without a daemon."""
    notes = []
    timesyncd = [v["node"] for v in out if v["daemon"] == "timesyncd" and v["active"]]
    if timesyncd:
        notes.append("systemd-timesyncd on %s: one server at a time, no error bound of its own, and it steps the clock"
                     " by itself past 0.4 s, Cassandra running or not; chrony is the safer choice (the collection keeps"
                     " chrony where it is installed)." % ", ".join(timesyncd))
    if any(v["how"] == "coarse" for v in out):
        notes.append("a node without its own time sync daemon (a container) is compared by the dates collected over"
                     " SSH: a coarse measure, only a gap over %s counts." % ms(coarse_s, False))
    return notes


def cassandra_clock_verdicts(nodes, max_ms=50, step_ms=400, coarse_ms=COARSE_MS):
    """nodes: [{node, probe (cassandra_clock_probe), samples ([[controller, node] times]), running (Cassandra runs
    there), manage (cassandra_linux manages its time sync), offline (cassandra_offline), addresses (the node's
    own)}] -> {nodes: [{node, verdict, ahead, offset, how, text, why, action, ...}], ok: bool, warnings: [str],
    notes: [str], max_ms}. A node is ok when its daemon is synchronised and its offset plus the daemon's error
    bound is within max_ms; with no daemon to ask (a container: the host's clock), when the dates collected over
    SSH put it within coarse_ms of the others.
    e.g. [{node: node1, probe: <0.012 ms off>, running: True}, {node: node2, probe: <200 ms ahead>, ...}] ->
    {nodes: [{node: node1, verdict: ok, action: none, ...}, {node: node2, verdict: off, action: slew, ...}],
    ok: False, ...}."""
    max_s, step_s, coarse_s = float(max_ms) / 1000, float(step_ms) / 1000, float(coarse_ms) / 1000
    nodes = [dict(n) for n in nodes or []]
    for n in nodes:
        n["probe"] = n.get("probe") or {}
        n["skew"] = _skew(n.get("samples"))
    ref, referenced = _reference(nodes)
    skews = [n["skew"] for n in nodes if n["skew"] is not None]
    addresses = {}
    for n in nodes:
        for a in n.get("addresses") or []:
            addresses[str(a)] = n["node"]
    out, warnings = [], []
    for n in nodes:
        out.append(_node_verdict(n, ref, referenced, len(skews), max_s, step_s, coarse_s))
        p = n["probe"]
        if p.get("others"):
            warnings.append("%s runs several time sync daemons (%s and %s): they fight over the clock, keep one"
                            % (n["node"], p["service"], ", ".join(p["others"])))
    warnings += _cluster_warnings(nodes, out, addresses, coarse_s)
    return {"nodes": out, "ok": all(v["verdict"] == "ok" for v in out), "warnings": warnings,
            "notes": _cluster_notes(out, coarse_s), "max_ms": float(max_ms)}


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
    does about it.
    Input: cassandra_clock_verdicts' result; output: [str]. e.g. -> ["  node1  ok   chronyd, synchronised to
    192.0.2.1, +0.0 ms (\u00b10.7 ms)", "  node2  OFF  chronyd, ...: ahead of its source by 200.0 ms, more than 50.0 ms
    \u2192 being corrected by its daemon (slew)"]."""
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
    """The verdicts of a start (create_cluster, add_node) -> its REFUSED block (under check: what a real run would refuse),
    the nodes not in sync, why, and the way out; [] when every node is. check (--check started no time sync),
    later_ok (a check before cassandra_linux ran: create_cluster's reset): a node not running with no time sync,
    which cassandra_linux would set up, is left to the check after it. unread: {node: why} of the nodes that
    answered but whose clock could not be read: refused.
    Output: [str]. e.g. (verdicts with node2 off, cluster="my_cluster") -> ["REFUSED  clocks  my_cluster  nothing
    was started", "  node2  OFF  chronyd, ...", "", ..., "TO DO", ...]."""
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
    lines += cassandra_output.warning_lines(verdicts.get("warnings", []))
    if later:
        lines += ["NOTE  time sync would be started on %s (cassandra_linux), its clock checked by the real run"
                  % ", ".join(later)]
    lines += ["", "TO DO", "  1. bring the clocks in sync (shows each node's time sync, starts it where missing):",
              "     " + (command or "ansible-playbook community.cassandra.sync_clocks"),
              "  2. or fix the time sync of these nodes (chrony.conf's server/pool lines, reachable from them),"
              " then run %s again" % (operation or "it"),
              "  (-e cassandra_clock_check=false skips this check, for clocks kept in sync by other means)"]
    return lines


def _flag(v, name, default):
    return boolean(v.get(name, default), strict=False)


def cassandra_clock_nodes(hosts, hostvars):
    """hosts: the play's hosts; hostvars. -> [{node, probe, samples, running, manage, unmanaged_by, offline,
    addresses}] for the hosts whose clock was read (_cassandra_clock_read_ok): what clock_probe.yml read
    (_cassandra_clock, _cassandra_clock_samples), whether Cassandra runs there (_cassandra_clock_running), whether
    cassandra_linux manages its time sync (cassandra_linux_manage and cassandra_linux_timesync, else which one is
    off), cassandra_offline, and the names and addresses other nodes may give it."""
    nodes = []
    for name in hosts:
        v = hostvars[name]
        if not _flag(v, "_cassandra_clock_read_ok", False):
            continue
        linux = _flag(v, "cassandra_linux_manage", True)
        nodes.append({
            "node": name, "probe": v["_cassandra_clock"], "samples": v["_cassandra_clock_samples"],
            "running": _flag(v, "_cassandra_clock_running", False),
            "manage": linux and _flag(v, "cassandra_linux_timesync", True),
            "unmanaged_by": "cassandra_linux_timesync" if linux else "cassandra_linux_manage",
            "offline": _flag(v, "cassandra_offline", False),
            "addresses": [name, v.get("ansible_host", name), (v.get("_cassandra_preflight") or {}).get("ring_address", "")]
            + (v.get("ansible_facts") or {}).get("all_ipv4_addresses", []),
        })
    return nodes


class FilterModule(object):
    def filters(self):
        return {"cassandra_clock_probe": cassandra_clock_probe, "cassandra_clock_settled": cassandra_clock_settled,
                "cassandra_clock_wait_line": cassandra_clock_wait_line,
                "cassandra_clock_in_sync": cassandra_clock_in_sync,
                "cassandra_clock_verdicts": cassandra_clock_verdicts, "cassandra_clock_lines": cassandra_clock_lines,
                "cassandra_clock_refusal": cassandra_clock_refusal,
                "cassandra_clock_nodes": cassandra_clock_nodes}
