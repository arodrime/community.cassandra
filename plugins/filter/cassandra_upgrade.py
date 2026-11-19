# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_upgrade_copy_refusal: the upgrade's prepare phase refusing a config copy that is not whole."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def cassandra_upgrade_copy_refusal(lines, node, conf_dir, copy, version, command):
    """lines: the comparison of the copy with the config ("missing <file>", "size <file>", "unreadable config ..." or
    "unreadable copy ..."); node; conf_dir: cassandra_conf_dir; copy: the copy's path; version: the one upgraded from;
    command: the command that runs the prepare phase again -> the refusal, its lines joined (ten of them listed at
    most), with its way out."""
    lines = lines or []
    unreadable_config = any(line.startswith("unreadable config ") for line in lines)
    if unreadable_config:
        what = "the config %s could not be read whole, the copy is not checked" % conf_dir
    elif any(line.startswith("unreadable copy ") for line in lines):
        what = "the config copy %s could not be read whole" % copy
    else:
        what = "the config copy %s is not whole (%s has more, or other sizes)" % (copy, conf_dir)
    out = ["%s: %s. The snapshot pre-upgrade-%s is taken; no node was upgraded." % (node, what, version)]
    out += ["  " + line for line in lines[:10]]
    if len(lines) > 10:
        out.append("  ... %d more" % (len(lines) - 10))
    if unreadable_config:
        out.append("WAY OUT  make %s on %s readable (the directory Cassandra reads: cassandra_conf_dir), then run the"
                   " prepare phase again:" % (conf_dir, node))
    else:
        out.append("WAY OUT  remove %s on %s (rm -r), then run the prepare phase again:" % (copy, node))
    return "\n".join(out + ["  " + command])


class FilterModule(object):
    def filters(self):
        return {"cassandra_upgrade_copy_refusal": cassandra_upgrade_copy_refusal}
