# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The ends of the node operations, as the other recaps (plugin_utils cassandra_output recap).

cassandra_op_recap: [{node, result}] (each node's cassandra_op_result) ->
    "DONE  add_node  my_cluster  2 added", the nodes grouped by outcome.
cassandra_reset_recap: [{node, delete, stop, disable}] (reset_node's plan of
    each node) -> "DONE  reset_node  my_cluster  node4 emptied ..." and what
    is left to do.
cassandra_stop_block: the end of a run that stopped at a node: "STOPPED
    rolling_restart  prod  at node2" (FAILED when a task failed, not one of
    our stops), the cause under it, the nodes' table, then WAY OUT with the
    command to copy, each said once, 79 columns at most.
cassandra_differs: {node: value} of a setting that must be the same on every
    node -> why the run stops, and where to fix it.
cassandra_limits_changed: the nodes whose limits file a package update changed,
    left as is (cassandra_linux_manage: false) -> the recap's warnings.
cassandra_warnings: [{label, text}] -> their WARNING lines, 79 columns.
cassandra_op_results: the nodes of the cluster -> [{node, result, stage}] (each node's cassandra_op_result, else
    why it has none).
cassandra_limit_patterns: a --limit (ansible_limit) -> its host patterns, its @files read.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import shlex
import textwrap

from ansible.inventory.manager import split_host_pattern

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_command
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

WAY_OUT = "WAY OUT  "  # the mark of the way out of a stop, once, last

# cassandra_service_node_action (the first word of a result) -> its playbook
PLAYBOOKS = {"add": "add_node", "decommission": "decommission_node", "restart": "rolling_restart",
             "reboot": "rolling_reboot", "replace": "replace_node", "move": "move_node", "update_java": "update_java",
             "upgrade": "upgrade", "upgradesstables": "upgrade", "cleanup": "cleanup", "clean": "cleanup",
             "apply_config": "apply_config", "update": "update_java", "apply": "apply_config",  # (the last two: would ...)
             "sync_clock": "sync_clocks", "clock_start": "sync_clocks", "clock_step": "sync_clocks",
             "clock_start_step": "sync_clocks", "clock_slew": "sync_clocks", "clock_slewing": "sync_clocks",
             "clock_start_slewing": "sync_clocks", "clock_step_slewing": "sync_clocks", "clock_start_step_slewing": "sync_clocks"}
# the same action, done
DONE = {"add": "added", "decommission": "decommissioned", "restart": "restarted", "reboot": "rebooted",
        "replace": "replaced", "move": "moved", "update_java": "Java updated", "upgrade": "upgraded",
        "upgradesstables": "SSTables upgraded", "cleanup": "cleaned up", "apply_config": "applied",
        "sync_clock": "clock set in a restart window", "clock_start": "time sync started", "clock_step": "clock stepped",
        "clock_start_step": "time sync started, clock stepped", "clock_slew": "clock corrected by its daemon",
        "clock_slewing": "clock being corrected by its daemon (minutes)",
        "clock_start_slewing": "time sync started, clock being corrected by it (minutes)",
        "clock_step_slewing": "clock stepped, being corrected by its daemon (minutes)",
        "clock_start_step_slewing": "time sync started, clock stepped, being corrected (minutes)"}

# the texts node_operation.yml, restart_batch.yml, cleanup_batch.yml and the playbooks record
_DONE = re.compile(r"^(?P<action>\w+) done(?P<how> with its rack)?(?P<resumed> \(resumed\))? in (?P<seconds>\d+)s$")
_WOULD = re.compile(r"^would (?P<what>(?P<action>\w+).*?)(?: \(--check: nothing was changed\))?$")
_FAILED = re.compile(r"^(?P<action>\w+) FAILED(?: after (?P<seconds>\d+)s)?:?\s*(?P<reason>.*)$", re.S)
_REFUSED = re.compile(r"^(?P<action>\w+) refused: (?P<reason>.*)$", re.S)
_SKIPPED = re.compile(r"^(?P<action>\w+) (?:skipped, |(?P<unfinished>not finished): )(?P<reason>.*)$")


# what was started on a node, when its action failed midway ("restart started, not finished")
STARTED = {"add": "bootstrap", "decommission": "decommission", "replace": "replacement", "update_java": "Java update",
           "upgradesstables": "SSTables upgrade", "apply_config": "config write"}

# an action that notes how far it went itself (_cassandra_op_stage): what each stage observed. apply_config
# (action_apply_config.yml, apply_config_window.yml): its config not written yet, its write started, its config written
OWN_STAGES = {"apply_config": {"during": "config not written", "writing": "config write started, not finished",
                               "written": "config written"}}
# the stages of a node touched by its action (a resume does it again)
TOUCHED = ("during", "writing", "written", "after", "unconfirmed")


def _failed_row(action, stage, reason, check=False):
    """What a failed node's row says was done to it (stage: node_operation's _cassandra_op_stage), its reason after;
    "" when the stage is not known. "after": its action done and its end confirmed (a restart: the node NORMAL
    again); "unconfirmed": its action ran, its end not waited for (cassandra_service_wait_for_normal false). Under
    check nothing was changed, whatever the stage."""
    reason = reason or "a check after it failed"
    if check and stage:
        return "nothing changed (--check); %s" % reason
    if action in OWN_STAGES and stage in OWN_STAGES[action]:
        return "%s; %s" % (OWN_STAGES[action][stage], reason)
    if stage == "before":
        return "not touched; %s" % reason
    if stage == "during":
        return "%s started, not finished; %s" % (STARTED.get(action, action.replace("_", " ")), reason)
    if stage == "unconfirmed":
        return "%s run, not waited for; %s" % (STARTED.get(action, action.replace("_", " ")), reason)
    if stage == "after":
        return "%s; %s" % (DONE.get(action, action + " done"), reason)
    return ""


def outcome(node, result, stage="", check=False):
    """A node's cassandra_op_result (stage: how far its action went, node_operation's; check: a --check run) -> (the
    recap's outcome, its action or "")."""
    text = " ".join(str(result or "").split())
    m = _DONE.match(text)
    if m:
        # (resumed: its time is only this run's part of it, never compared with the others')
        return (dict({"node": node, "outcome": DONE.get(m.group("action"), m.group("action") + " done") + (m.group("how") or ""),
                      "seconds": int(m.group("seconds"))}, **({"partial": True} if m.group("resumed") else {})),
                m.group("action"))
    m = _WOULD.match(text)
    if m:
        action = "clean" if m.group("what").startswith("clean up") else m.group("action")
        return {"node": node, "said": "would " + m.group("what")}, action
    m = _FAILED.match(text)
    if m:
        return ({"node": node, "status": "failed", "reason": m.group("reason").strip(),
                 "row": _failed_row(m.group("action"), stage, m.group("reason").strip(), check),
                 "touched": not check and stage in TOUCHED,
                 "seconds": int(m.group("seconds")) if m.group("seconds") else None}, m.group("action"))
    m = _REFUSED.match(text)
    if m:
        return {"node": node, "status": "failed", "reason": "refused: " + m.group("reason").strip()}, m.group("action")
    m = _SKIPPED.match(text)
    if m and m.group("action") in PLAYBOOKS:  # done by the run before; a node of a batch stopped midway: said as such
        said = ("not finished (%s)" % m.group("reason")) if m.group("unfinished") else m.group("reason")
        touched = {"touched": True} if m.group("unfinished") and not check else {}  # (a resume does it again)
        return dict({"node": node, "said": said}, **touched), m.group("action")
    # not reached, not in this run (--limit), already in the ring / on that version, nothing to apply...
    # (not run yet: the run stopped before it, plain "not touched")
    reason = {"not in this run (--limit)": "left out by --limit", "not run yet": "",
              "not done (the run stopped before it)": ""}.get(text, text)
    return {"node": node, "status": "skipped", "reason": reason}, ""


def _outcomes(results, operation, action, check=False):
    """(the recap's outcomes, the playbook's name) of the results."""
    outcomes, actions = [], []
    for item in results or []:
        said, done = outcome(item.get("node"), item.get("result"), item.get("stage") or "", check)
        outcomes.append(said)
        if done:
            actions.append(done)
    actions = ([action] if action else []) + actions
    return outcomes, operation or (PLAYBOOKS.get(actions[0], actions[0]) if actions else "")


def _limits_text(node, changed, cwd=None):
    """One node's warning of its limits file changed by the package update (changed: package_update.yml's
    cassandra_package_limits_changed, {path, saved, created, removed, link, dangling, error}; a path alone as well)
    -> its lines. link: the target of the link the file was (its saved content read through it), the link to put
    back; dangling: that link pointed to no file (no former content). cwd: the copy's path said from it when under
    it (a path is never cut: within 79 columns then)."""
    if not isinstance(changed, dict):
        changed = {"path": changed}
    path = str(changed.get("path") or "")
    manage = "set cassandra_linux_manage: true (cassandra_linux writes it)"
    if changed.get("created"):
        return ["%s: the Cassandra package update created %s (there was none)" % (node, path),
                "left as the package wrote it (cassandra_linux_manage: false)",
                "check it holds the limits you want for the cassandra user, or %s" % manage]
    if changed.get("removed"):
        lines = ["%s: the Cassandra package update removed %s" % (node, path),
                 "not written back (cassandra_linux_manage: false)"]
    else:
        lines = ["%s: the Cassandra package update replaced %s" % (node, path),
                 "left as the package wrote it (cassandra_linux_manage: false)"]
    link = str(changed.get("link") or "")
    dangling = bool(link and changed.get("dangling"))  # (no former content: nothing to save)
    if link:
        lines.append("it was a link to %s%s" % (link, ", which pointed to no file" if dangling else ""))
    if changed.get("saved"):
        saved = out.path_from(str(changed["saved"]), cwd)
        lines.append("its former content%s: %s" % (" (read through the link)" if link else "", saved))
    elif not dangling:
        lines.append("its former content was not saved (%s)" % (changed.get("error") or "not read"))
    if link:
        return lines + ["put the link back (ln -sfn %s %s) if it held the limits of the cassandra user, or %s"
                        % (shlex.quote(link), shlex.quote(path), manage)]
    if changed.get("saved"):
        return lines + ["copy it back to %s if it held the limits of the cassandra user, or %s" % (path, manage)]
    return lines + ["check it holds the limits you want for the cassandra user, or %s" % manage]


def cassandra_limits_changed(hosts, hostvars, cwd=None):
    """The nodes (in their order) and their hostvars -> the warnings of the nodes whose limits file the Cassandra
    package update replaced or created (package_update.yml's cassandra_package_limits_changed) while
    cassandra_linux_manage is false, which leaves it as the package wrote it: [{label: "", text: its lines}], one
    per node, [] for none."""
    said = []
    for h in hosts or []:
        changed = (hostvars.get(h) or {}).get("cassandra_package_limits_changed")
        if changed:
            said.append({"label": "", "text": "\n".join(_limits_text(str(h), changed, cwd))})
    return said


def cassandra_warnings(warnings, width=None):
    """[{label, text}] -> their WARNING lines, each once, width columns at most (default 79, a stop block's; text:
    lines in it, the next ones indented under it)."""
    texts = ["%s: %s" % (w["label"], w["text"]) if w.get("label") else w["text"] for w in warnings or [] if w.get("text")]
    return out.fit(out.warning_lines(texts), width or STOP_WIDTH) if texts else []


def cassandra_notes_lines(notes, width=None):
    """The checks' notes said on their own (note.yml's _cassandra_notes: no plan to hold them), width columns at most
    (default 79): a WARNING note's next lines under its text, as in a plan, a note of several lines set apart by a
    blank line (a block of its own); a command after a label ("then apply it live: ansible-playbook ...") on a line
    of its own when too long, cut between its arguments; another note as it is, wrapped."""
    width = width or STOP_WIDTH
    lines, spaced = [], False
    for note in notes or []:
        if out.is_warning(note):
            said = out.warning(str(note).split("\n"), under=True)
            first = textwrap.wrap(said[0], width, subsequent_indent=" " * len(out.WARNING), break_long_words=False,
                                  break_on_hyphens=False) or [said[0]]
            rest = []
            for line in said[1:]:
                labelled = out.LABELLED_COMMAND.match(line)
                if labelled and len(line) > width:
                    rest += [labelled.group(1) + labelled.group(2).rstrip(), labelled.group(1) + "  " + labelled.group(3)]
                else:
                    rest.append(line)
            several = "\n" in str(note).strip()
            if lines and (several or spaced):
                lines.append("")
            lines, spaced = lines + first + out.fit(rest, width), several
        else:
            lines += out.fit(str(note).split("\n"), width)
    return lines


def cassandra_op_recap(results, check=False, cluster="", operation="", action="", seconds=None, warnings=None):
    """results: [{node, result}] in the order of the run, result each node's
    cassandra_op_result. The recap of plugin_utils cassandra_output: the verdict (DONE,
    FAILED, CHECK), then the nodes grouped by outcome, their times.
    operation: the playbook's name, else from action (the
    cassandra_service_node_action of the run), else from the actions
    recorded; seconds: the run's, on the verdict line.
    -> lines: "rolling_restart done in 100s" on
    node4, 110s on node5 -> ["DONE  rolling_restart  my_cluster  2 restarted", "  node4, node5  restarted  1m40s..1m50s
    each"]. warnings: [{label, text}] said last, WARNING lines of 79 columns."""
    outcomes, operation = _outcomes(results, operation, action)
    # seconds: text before ansible-core 2.19 (a task's vars), "" for none
    return out.recap(operation, str(cluster or ""), outcomes, check=check, seconds=out.number(seconds),
                     extra=cassandra_warnings(warnings) or None)


def cassandra_op_results(hosts, hostvars, play_hosts, play_hosts_all, limited=None):
    """The nodes of the cluster (or the run's: cassandra_service_summary_hosts), their hostvars, the play's hosts
    (ansible_play_hosts, ansible_play_hosts_all), the hosts a --limit keeps (none: no --limit, or one not read)
    -> [{node, result, stage}] in their order: its cassandra_op_result; none, still in the play: "not run yet"
    (the run stopped before it); out of the play (unreachable, failed earlier): "not reached"; not in it at all:
    "not in this run (--limit)" when the --limit leaves it out, else "not in this run" (a node the playbook does
    not run on: replace_node's other nodes).
    e.g. (["n1", "n3"], {"n1": {"cassandra_op_result": "add done in 5s"}, "n3": {}}, [], ["n1"], ["n1"]) ->
    [{node: "n1", result: "add done in 5s", stage: ""}, {node: "n3", result: "not in this run (--limit)", stage: ""}]."""
    results = []
    for h in hosts or []:
        variables = (hostvars or {}).get(h) or {}
        result = variables.get("cassandra_op_result")
        if result is None:
            result = ("not run yet" if h in (play_hosts or []) else "not reached" if h in (play_hosts_all or [])
                      else "not in this run (--limit)" if limited and h not in limited
                      else "not in this run")
        results.append({"node": h, "result": result, "stage": variables.get("_cassandra_op_stage", "")})
    return results


def cassandra_limit_patterns(limit):
    """A --limit as Ansible takes it (ansible_limit) -> its host patterns, comma separated, each @file's lines in
    its place (as Ansible reads it: from the run's directory, its path as written, no ~; each line one pattern, so
    a line the lookup would split, "n1:n2", matches nothing, left out); "" without a --limit, or when a file is
    not read (the inventory_hostnames lookup would match nothing, with a warning in the run).
    e.g. "n1,@retry.txt" (retry.txt: "n2\nn3\n") -> "n1,n2,n3"."""
    patterns = []
    for pattern in split_host_pattern(str(limit or "")):
        if not pattern.startswith("@"):
            patterns.append(pattern)
            continue
        try:
            with open(pattern[1:], encoding="utf-8") as f:
                lines = [line.strip() for line in f.read().split("\n") if line.strip()]
        except (IOError, OSError, UnicodeDecodeError):
            return ""
        patterns += [line for line in lines if split_host_pattern(line) == [line]]
    return ",".join(patterns)


# Ansible's own words for a command that failed: the task's stderr says more
_GENERIC = re.compile(r"^(non-zero return code|MODULE FAILURE)")


def _cause_and_way_out(failure):
    """A failed task's msg (a string or lines) -> (the cause's lines, each once (the same check failed from two
    nodes), the way out's lines: from its WAY OUT line on, the mark removed from the first; none: [])."""
    lines = []
    for item in failure if isinstance(failure, (list, tuple)) else [failure]:
        lines += [line.rstrip() for line in str(item or "").split("\n") if line.strip()]
    for i, line in enumerate(lines):
        if line.startswith(WAY_OUT.strip()):
            return _once(lines[:i]), [line[len(WAY_OUT.strip()):].strip()] + lines[i + 1:]
    return _once(lines), []


def _once(lines):
    return [line for i, line in enumerate(lines) if line not in lines[:i]]


# what a resume does again to a node its run touched and did not record done: (one node, several)
AGAIN = {"restart": ("restarts", "restart"), "reboot": ("reboots", "reboot")}
# the width of a stop block: 79 columns (an 80-column terminal), whatever prints it (a terminal, a pipe, a log file)
STOP_WIDTH = 79
_COMMAND_LINE = re.compile(r"^(\s*)(ansible-playbook|ansible) ")
_THEN = re.compile(r"^(?P<first>.+?),? then (?P<again>resume|check again):?$")


def _wrapped(text, indent, hang, width=STOP_WIDTH):
    """text from column indent, its next lines from column hang, cut between words (a word longer stays whole); a
    command (ansible-playbook ...) cut between its arguments, an option and its value together, each line but the
    last ending with " \\" (it pastes as one command), the next ones 2 columns further in."""
    if not _COMMAND_LINE.match(text):
        return textwrap.wrap(text, width, initial_indent=" " * indent, subsequent_indent=" " * hang,
                             break_long_words=False, break_on_hyphens=False) or [" " * indent]
    first, rest = " " * indent, " " * (indent + 2)
    words = out.command_words(text)
    lines, line = [], first
    for i, word in enumerate(words):
        if line.strip() and len(line) + 1 + len(word) + (2 if i < len(words) - 1 else 0) > width:
            lines.append(line + " \\")
            line = rest
        line += (" " if line.strip() else "") + word
    return lines + [line]


_LOST = re.compile(r"^IF .+ IS LOST FOR GOOD$")  # the block of the ways out of a node lost for good (cluster_health)


def _way_out_lines(lines, indent):
    """Lines of a way out (their own indent kept, from indent on), each wrapped at the stop block's width; a
    heading "IF node4 IS LOST FOR GOOD" starts a block of its own (a blank line above it, not indented): its lines
    are no next step of the way out."""
    said = []
    for line in lines:
        text = line.lstrip()
        if _LOST.match(text):
            said += ["", text]
            continue
        said += _wrapped(text, indent + len(line) - len(text), indent + len(line) - len(text))
    return said


def cassandra_stop_block(results, node, failure, operation="", action="", cluster="", check=False, hint="",
                         resume=True, progress_file="", inventory=None, default_inventory=None, hosts="", cwd=None,
                         batch=False, task="", stderr=""):
    """The end of a run that stopped at node: results, the nodes' results (cassandra_op_result, as cassandra_op_recap,
    with stage: how far each node's action went); failure, the failed task's msg: its cause, and its way out from its
    WAY OUT line on when it has one (one of our stops: the block says STOPPED), else FAILED, and the way out is hint
    (cassandra_service_resume_hint) or, with resume, the run's command plus -e cassandra_rolling_resume=true
    (inventory, default_inventory, hosts, cwd: see cassandra_command), the progress file's path under it; batch: the
    nodes done at the same time; task, stderr: the failed task's name and stderr, said in a FAILED cause when it is
    Ansible's generic one ("non-zero return code"); a hint stays under the failure's own WAY OUT (add_node has no
    resume: "run add_node again"). The verdict first, the cause on its own lines under it (Why:), the nodes' table,
    then WAY OUT, its steps numbered when the failure's way out ends "then resume"; 79 columns at most (a command cut
    between its arguments, " \\" at the end of each line but its last).
    -> lines: ["STOPPED  rolling_restart  prod  at node2", "  Why:  node4 (10.0.0.4, r1) is DN, seen from node1 and
    node2", "        waited 10m29s (cassandra_service_health_timeout: 600s)", "  node1         restarted  1m50s",
    "  node2         restarted; cluster health check failed", "  node3..node5  not touched", "WAY OUT", "  1. start
    Cassandra on node4", "  2. resume:", "     ansible-playbook community.cassandra.rolling_restart \\",
    "       -e cassandra_rolling_resume=true", "     progress: .cassandra_progress/..."]."""
    outcomes, operation = _outcomes(results, operation, action, check)
    cause, way_out = _cause_and_way_out(failure)
    head = _stop_head("STOPPED" if way_out else "FAILED", operation, cluster, node, cause, task if not way_out else "",
                      stderr)
    table = out.recap(operation, str(cluster or ""), outcomes, check=check, head=False, causes=False)
    if not table:  # (the table says which nodes were touched)
        head.append("  " + ("The nodes done at the same time were not finished." if batch else "No node after it was touched."))
    command = ""
    if resume and not hint:  # --check wrote no progress file: the same check again, not a resume
        command = cassandra_command(operation, inventory=inventory, hosts=hosts, cwd=cwd, default_inventory=default_inventory,
                                    extra=["--check"] if check else ["-e cassandra_rolling_resume=true"])
    way_out = _stop_way_out(way_out, hint, command, check)
    # the nodes a resume does again (not in the progress file: node_operation.yml adds a node once the cluster
    # check after its action passed, restart_batch.yml a rack once back NORMAL): said when that is a restart
    again_nodes = [o["node"] for o in outcomes if o.get("touched")] if not check and action in AGAIN else []
    again_said = " (%s %s again)" % (out.nodes(again_nodes), AGAIN[action][len(again_nodes) > 1]) if again_nodes else ""
    lines = _way_out_head(way_out[0], command, again_said, "" if check else progress_file, cwd)
    return head + table + [""] + lines + _way_out_lines(way_out[1:], 2)


def _stop_head(verdict, operation, cluster, node, cause, task, stderr):
    """The verdict line, then the cause on its own lines under it (Why:); task, stderr: the failed task's name and
    stderr when not one of our stops, said in its cause (stderr when Ansible's generic cause)."""
    first_cause = cause[0] if cause else "a task failed (see above)"
    if task:
        detail = [line.strip() for line in str(stderr or "").split("\n") if line.strip()]
        first_cause = "%s: %s" % (task, first_cause) + (" (%s)" % detail[-1] if detail and _GENERIC.match(first_cause) else "")
    head = ["  ".join(x for x in [verdict, operation, str(cluster or ""), "at %s" % node] if x)]
    for i, line in enumerate([first_cause] + cause[1:]):
        head += _wrapped(("Why:  " if i == 0 else "") + line.strip(), 2 if i == 0 else 8, 8)
    return head


def _stop_way_out(way_out, hint, command, check):
    """The failure's way out (its lines), with the hint, the resume or the check again."""
    again = "check again" if check else "resume"
    if not way_out:
        return [hint.strip() if hint else ("fix it, then %s" % again if command else
                "fix it, then check where the cluster stands (the status playbook) before running it again")]
    if hint and not command:  # the failure's own way out ends "then resume": the hint says how
        return [re.sub(r",? then resume:?$", ", then:", way_out[0]), hint.strip()] + way_out[1:]
    if check:
        return [re.sub(r"then resume(:?)$", r"then check again\1", way_out[0])] + way_out[1:]
    return way_out


def _way_out_head(first, command, again_said, progress_file, cwd):
    """WAY OUT and its first step: with a command, 1. what to fix, 2. the command that goes on when first ends with
    "then resume", else first then the command; progress_file under the command."""
    lines = ["WAY OUT"]
    steps = _THEN.match(first) if command else None
    if steps:  # 1. what to fix, 2. the command that goes on (the progress file under it)
        lines += _wrapped("1. " + steps.group("first"), 2, 5) + ["  2. %s%s:" % (steps.group("again"), again_said)]
        lines += _wrapped(command, 5, 7)
        indent = 5
    else:
        lines += _wrapped((first.rstrip(".") + ":") if command and not first.endswith(":") else first, 2, 4)
        lines += _wrapped(command, 4, 6) if command else []
        indent = 4
    if command and progress_file:  # (under the command it is for, before the other ways out)
        lines += _wrapped("progress: %s" % (out.path_from(str(progress_file), cwd) if cwd else progress_file),
                          indent, indent + 2)
    return lines


def cassandra_reset_recap(nodes, check=False, cluster="", add_command=""):
    """nodes: [{node, delete (paths), stop, disable}] (reset_node_plan.yml's
    plan of each node). The end of reset_node: "DONE  reset_node  cluster
    node4, node5 emptied (8 entries deleted), Cassandra stopped and
    disabled on node4, node5", each part for the nodes it is for, then TO
    DO: add_command (the add_node command for them). Under check: "CHECK
    reset_node  cluster  would empty node4, node5 (8 entries), stop and
    disable Cassandra on node4, node5   nothing was changed". The nodes with
    nothing to do said as such."""
    nodes = list(nodes or [])
    done = [n for n in nodes if n.get("delete") or n.get("stop") or n.get("disable")]
    idle = out.nodes([n["node"] for n in nodes if n not in done], keep_order=True)
    if not done:
        return ["  ".join(x for x in ("NOTHING TO DO", "reset_node", str(cluster or ""),
                                      "%s: empty, Cassandra stopped and disabled" % idle) if x)]

    said = ("would " if check else "") + ", ".join(_reset_acts(done, check))
    head = [("CHECK" if check else "DONE"), "reset_node", str(cluster or ""), said]
    lines = ["  ".join(x for x in head if x) + ("   nothing was changed" if check else "")]
    if idle:
        lines.append("  %s  nothing to do (empty, Cassandra stopped and disabled)" % idle)
    if not check and add_command:
        lines += [""] + out.todo([{"text": "add %s back to the cluster" % ("it" if len(done) == 1 else "them"),
                                   "command": add_command}])
    return lines


def _reset_acts(done, check):
    """What reset_node did (check: would do) on the nodes done, each part for the nodes it is for."""
    def named(key):
        return out.nodes([n["node"] for n in done if n.get(key)], keep_order=True)

    emptied, stopped, disabled = named("delete"), named("stop"), named("disable")
    if stopped and stopped == disabled:
        acts = [("stop and disable Cassandra on %s" if check else "Cassandra stopped and disabled on %s") % stopped]
    else:
        acts = ([("stop Cassandra on %s" if check else "Cassandra stopped on %s") % stopped] if stopped else []) + (
            [("disable Cassandra on %s" if check else "Cassandra disabled on %s") % disabled] if disabled else [])
    if emptied:
        entries = out.plural(sum(len(n.get("delete") or []) for n in done), "entry", "entries")
        acts.insert(0, ("empty %s (%s)" if check else "%s emptied (%s deleted)") % (emptied, entries))
    return acts


def cassandra_differs(values, setting, group=""):
    """values: {node: value} of a setting that must be the same on every node
    (preflight). Why the run stops, the values by nodes, where to fix it.
    -> the lines that stop the run: why, then each value with its nodes."""
    by_value, shown_as = {}, {}
    for node, value in (values or {}).items():
        by_value.setdefault(str(value), []).append(node)  # as the assert compares them
        shown_as[str(value)] = (", ".join(str(v) for v in value) if isinstance(value, (list, tuple))
                                else str(value) if str(value) else '""')
    ranked = sorted(by_value.items(), key=lambda kv: (-len(kv[1]), out.nodes(kv[1])))
    shown = "; ".join("%s: %s" % (out.nodes(names), shown_as[value] if value != "None" else "(none)")
                      for value, names in ranked)
    where = "group_vars/%s" % group if group else "the cluster's group_vars"
    others = sorted((n for value, names in ranked[1:] for n in names),
                    key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    hosts = ", ".join("host_vars/%s" % n for n in others[:3]) + (", ..." if len(others) > 3 else "")
    if setting == "_cassandra_service_seeds":
        what = "The seed list (cassandra_seeds) is not the same on every node%s (%s)" % (
            " of %s" % group if group else "", shown)
        fix = ("Set cassandra_seeds once in %s (or leave it out: the collection picks the seeds), and remove it "
               "from %s." % (where, hosts or "the host_vars"))
    else:
        what = "%s is not the same on every node%s (%s)" % (setting, " of %s" % group if group else "", shown)
        fix = "Set it once in %s, and remove it from %s." % (where, hosts or "the host_vars")
    return "%s: every node needs the same. %s Nothing was changed." % (what, fix)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_op_recap": cassandra_op_recap,
            "cassandra_op_results": cassandra_op_results,
            "cassandra_limit_patterns": cassandra_limit_patterns,
            "cassandra_stop_block": cassandra_stop_block,
            "cassandra_reset_recap": cassandra_reset_recap,
            "cassandra_differs": cassandra_differs,
            "cassandra_limits_changed": cassandra_limits_changed,
            "cassandra_warnings": cassandra_warnings,
            "cassandra_notes_lines": cassandra_notes_lines,
        }
