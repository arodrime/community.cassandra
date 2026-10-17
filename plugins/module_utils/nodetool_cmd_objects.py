from __future__ import (absolute_import, division, print_function)
__metaclass__ = type
import re
import socket

# Lines the JVM prints before nodetool's own output: a JAVA_TOOL_OPTIONS or
# _JAVA_OPTIONS of the environment ("Picked up JAVA_TOOL_OPTIONS: ..."), a VM
# warning. Usually on stderr, but they end up in stdout when it is merged.
JVM_BANNER_RE = re.compile(r'^(Picked up [A-Za-z_]+: |(OpenJDK|Java HotSpot\(TM\)) .*VM warning: )')


def strip_jvm_banner(text):
    """text without the JVM banner lines (JVM_BANNER_RE)."""
    if not text:
        return text
    return ''.join(line for line in text.splitlines(True) if not JVM_BANNER_RE.match(line))


def nodetool_error(out, err):
    """What nodetool said went wrong, for a failure's message: its error line
    ("error: ..."), else the first line of its output (not a JVM banner),
    without its final dot; "" when it said nothing."""
    lines = [line.strip() for line in strip_jvm_banner((err or "") + "\n" + (out or "")).splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("error:")), lines[0] if lines else "").rstrip(".")


def fail_nodetool(module, result, command, rc, out, err, advice):
    """Fail the module on a nodetool command that did not succeed: result gets the error (nodetool_error's), rc,
    stdout and stderr, msg says which command failed, its error and advice (what to check, a sentence)."""
    error = nodetool_error(out, err)
    result['error'] = error
    result['rc'] = rc
    result['changed'] = False
    result['stdout'] = out
    result['stderr'] = err
    result['msg'] = "nodetool {0} did not execute successfully (rc {1}){2}. {3}".format(
        command, rc, ": " + error if error else "", advice)
    module.fail_json(**result)


def cassandra_version_at_least(version_string, minimum_version):
    """Compare two "MAJOR.MINOR"-style Cassandra version strings, e.g.
    cassandra_version_at_least("5.0", "4.1") -> True.

    version_string is normally auto-detected in NodeToolCmd.__init__ (or
    passed via the cassandra_version module option). Several nodetool
    sub-commands changed output/flags starting in a given version (e.g.
    get*streamthroughput requiring -d since 4.1) and keep that behaviour
    in later versions too - comparing with == against a single version
    string breaks the moment a newer version ships.
    """
    def parts(v):
        return tuple(int(p) for p in v.split(".")[:2])
    return parts(version_string) >= parts(minimum_version)


# a JMX login refused, in nodetool's error (the JDK's file login, Cassandra's own)
_JMX_REFUSED = re.compile(r"Invalid username or password|Authentication failed|Credentials required|"
                          r"FailedLoginException|Authentication error")


# nodetool not reaching JMX: "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'."
_JMX_UNREACHED = re.compile(r"Failed to connect to '(?P<where>[^']*)'(?: - (?P<kind>\w+): '?(?P<why>[^'\n]*?)'?\.?)?\s*$", re.M)
JMX_CAUSES = (
    (re.compile(r"Connection refused", re.I), "Connection refused",
     "Cassandra is not running there, or its JMX listens on another port (cassandra_jmx_port). "
     "Check: systemctl status cassandra, and its system.log"),
    (re.compile(r"timed out|SocketTimeout", re.I), "no answer in time",
     "a firewall, or JMX listening on another address (LOCAL_JMX in cassandra-env.sh)"),
    (re.compile(r"NoRouteToHost|No route to host", re.I), "no route to host",
     "the host is down or out of reach, or a firewall rejects it"),
    (re.compile(r"UnknownHost|Name or service not known", re.I), "unknown host",
     "that host name does not resolve"),
)


def version_error(out, err, host=None, port=None):
    """The message when nodetool version fails, the first nodetool call of a
    module: a JMX login refused said as such (the options to check), JMX not
    reached said with its address, its cause and what to look at, else
    nodetool's own error line. host, port: the JMX address asked (else the
    one nodetool names)."""
    text = "{0}\n{1}".format(out or "", err or "")
    said = [line.strip() for line in text.splitlines() if line.strip().startswith("error:")]
    first = said[0][len("error:"):].strip() if said else ""
    if _JMX_REFUSED.search(text):
        return ("JMX login refused ({0}): check the JMX user and its password or password file "
                "(cassandra_jmx_username, cassandra_jmx_password_file or cassandra_jmx_password)").format(
                    first or "authentication failed")
    unreached = _JMX_UNREACHED.search(text)
    if unreached:
        where = unreached.group("where") or ("{0}:{1}".format(host, port) if host else "")
        why = (unreached.group("why") or unreached.group("kind") or "").strip()
        for pattern, cause, hint in JMX_CAUSES:
            if pattern.search(why or text):
                return "nodetool gets no answer from JMX on {0} ({1}): {2}".format(where, cause, hint)
        return ("nodetool can't reach JMX on {0}{1}: is Cassandra running there (systemctl status cassandra), "
                "on that JMX port (cassandra_jmx_port)?").format(where, " ({0})".format(why) if why else "")
    errors = [line.strip() for line in (err or "").strip().splitlines() if line.strip()]
    found = first or (out or "").strip() or (errors[0] if errors else "")
    return "nodetool version failed{0}: is Cassandra installed and running?{1}".format(
        " on {0}:{1}".format(host, port) if host else "", " ({0})".format(found) if found else " (no error printed)")


def parse_nodetool_get(out, cast=float):
    """Parse the value printed by a nodetool get* command, such as
    "Current stream throughput: 200.0 Mb/s" or "Current trace probability: 0.1".

    Returns (value, unit, line): unit is None when nodetool prints none,
    and "unlimited" (printed for 0 by the throughput commands since 4.1)
    is returned as 0. Returns (None, None, None) when no value is found.
    The number is read as Java prints it, exponent included ("1.0E7").
    """
    match = re.search(r'^[^:\n]+:\s*(unlimited|-?\d+(?:\.\d+)?(?:E-?\d+)?)(?:\s+(\S+))?\s*$',
                      out, re.MULTILINE)
    if match is None:
        return None, None, None
    if match.group(1) == 'unlimited':
        return cast(0), None, match.group(0).strip()
    return cast(float(match.group(1))), match.group(2), match.group(0).strip()


class NodeToolCmd(object):
    """
    This is a generic NodeToolCmd class for building nodetool commands
    """

    def __init__(self, module):
        self.module = module
        self.host = module.params['host']
        self.port = module.params['port']
        self.password = module.params['password']
        self.password_file = module.params['password_file']
        self.username = module.params['username']
        self.nodetool_path = module.params['nodetool_path']
        self.nodetool_flags = module.params['nodetool_flags']
        self.debug = module.params['debug']
        self.cassandra_version = module.params['cassandra_version']
        if self.host is None:
            self.host = socket.getfqdn()
        # nodetool takes a password only with a user: without one it would run
        # without credentials and fail with "Credentials required"
        if self.username is None and (self.password is not None or self.password_file is not None):
            module.fail_json(msg="password or password_file is set without username: nodetool needs the JMX user too")
        if self.cassandra_version is None:
            (rc, out, err) = self.nodetool_cmd("version")
            if rc == 0:
                # ReleaseVersion: 5.0.7
                line = [x for x in out.splitlines() if x.startswith('ReleaseVersion')][-1:] or [out]
                what_is_the_version = ".".join(line[0].split(': ')[1].split(".")[:2]).strip()
                module.params['cassandra_version'] = what_is_the_version
            else:
                module.fail_json(msg=version_error(out, err, self.host, self.port), stderr=err)

    def execute_command(self, cmd):
        rc, out, err = self.module.run_command(cmd)
        return rc, strip_jvm_banner(out), err

    def nodetool_cmd(self, sub_command):
        if self.nodetool_path is not None and len(self.nodetool_path) > 0:
            if not self.nodetool_path.endswith('/'):  # replace with os.path.join
                self.nodetool_path += '/'
        else:
            self.nodetool_path = ""
        cmd = "{0}nodetool {1} --host {2} --port {3}".format(self.nodetool_path,
                                                             self.nodetool_flags,
                                                             self.host,
                                                             self.port)
        if self.username is not None:
            cmd += " --username {0}".format(self.username)
            if self.password_file is not None:
                cmd += " --password-file {0}".format(self.password_file)
            else:
                cmd += " --password '{0}'".format(self.password)
        # The thing we want nodetool to execute
        cmd += " {0}".format(sub_command)
        if self.debug:
            self.module.debug(cmd)
        return self.execute_command(cmd)


class NodeToolCommandSimple(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;
        - run_command
    """

    def __init__(self, module, cmd):
        NodeToolCmd.__init__(self, module)
        self.cmd = cmd

    def run_command(self):
        return self.nodetool_cmd(self.cmd)


class NodeToolCommandKeyspaceTable(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;
        - run_command
    2020.01.10 - Added additonal keyspace and table params
    """

    def __init__(self, module, cmd):
        NodeToolCmd.__init__(self, module)
        self.keyspace = module.params['keyspace']
        self.table = module.params['table']
        if self.keyspace is not None:
            cmd = "{0} {1}".format(cmd, self.keyspace)
        if self.table is not None:
            if isinstance(self.table, str):
                cmd = "{0} {1}".format(cmd, self.table)
            elif isinstance(self.table, list):
                cmd = "{0} {1}".format(cmd, " ".join(self.table))
        self.cmd = cmd

    def run_command(self):
        return self.nodetool_cmd(self.cmd)


class NodeTool2PairCommand(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;

        - enable_command
        - disable_command
    """

    def __init__(self, module, enable_cmd, disable_cmd):
        NodeToolCmd.__init__(self, module)
        self.enable_cmd = enable_cmd
        self.disable_cmd = disable_cmd

    def enable_command(self):
        return self.nodetool_cmd(self.enable_cmd)

    def disable_command(self):
        return self.nodetool_cmd(self.disable_cmd)


class NodeTool3PairCommand(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;

        - status_command
        - enable_command
        - disable_command
    """

    def __init__(self, module, status_cmd, enable_cmd, disable_cmd):
        NodeToolCmd.__init__(self, module)
        self.status_cmd = status_cmd
        self.enable_cmd = enable_cmd
        self.disable_cmd = disable_cmd

    def status_command(self):
        return self.nodetool_cmd(self.status_cmd)

    def enable_command(self):
        return self.nodetool_cmd(self.enable_cmd)

    def disable_command(self):
        return self.nodetool_cmd(self.disable_cmd)


class NodeTool4PairCommand(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;

        - status_command
        - enable_command
        - disable_command
        - reset_command

    Additional args also added to enable command method
    """

    def __init__(self, module, status_cmd, enable_cmd, disable_cmd, reset_cmd, additional_args):
        NodeToolCmd.__init__(self, module)
        self.status_cmd = status_cmd
        self.enable_cmd = enable_cmd
        self.disable_cmd = disable_cmd
        self.reset_cmd = reset_cmd
        self.additional_args = additional_args

    def status_command(self):
        return self.nodetool_cmd(self.status_cmd)

    def enable_command(self):
        cmd = "{0} {1}".format(self.enable_cmd, self.additional_args)
        return self.nodetool_cmd(cmd)

    def disable_command(self):
        return self.nodetool_cmd(self.disable_cmd)

    def reset_command(self):
        return self.nodetool_cmd(self.reset_cmd)


class NodeToolGetSetCommand(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;

        - get_cmd
        - set_cmd
    """

    def __init__(self, module, get_cmd, set_cmd):
        NodeToolCmd.__init__(self, module)
        self.get_cmd = get_cmd
        self.set_cmd = set_cmd

    def get_command(self):
        return self.nodetool_cmd(self.get_cmd)

    def set_command(self):
        return self.nodetool_cmd(self.set_cmd)


class NodeToolCommandKeyspaceTableNumJobs(NodeToolCmd):

    """
    Inherits from the NodeToolCmd class. Adds the following methods;
        - run_command
    2020.01.10 - Added additonal keyspace and table params
    """

    def __init__(self, module, cmd):
        NodeToolCmd.__init__(self, module)
        self.keyspace = module.params['keyspace']
        self.table = module.params['table']
        self.num_jobs = module.params['num_jobs']
        cmd = "{0} -j {1}".format(cmd, self.num_jobs)
        if self.keyspace is not None:
            cmd = "{0} {1}".format(cmd, self.keyspace)
        if self.table is not None:
            if isinstance(self.table, str):
                cmd = "{0} {1}".format(cmd, self.table)
            elif isinstance(self.table, list):
                cmd = "{0} {1}".format(cmd, " ".join(self.table))
        self.cmd = cmd

    def run_command(self):
        return self.nodetool_cmd(self.cmd)
