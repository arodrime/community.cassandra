# shellcheck shell=sh
# What the proofs that the files of the roles give the running Cassandra JVM the same arguments may take from it, POSIX
# sh read by the shell tasks of import_cluster and apply_config (reads only).

# jvm_untrusted <pid>: why the arguments and environment of that JVM are not the ones the cassandra unit gives it, one
# reason per line, nothing when they are: a JVM outside the control group of the unit (started by hand, or by something
# else) on a host systemd runs, an environment not readable (not known: not an empty one).
jvm_untrusted() {
  if [ -d /run/systemd/system ]; then
    cg=$(systemctl show cassandra -p ControlGroup --value 2>/dev/null)
    in=no
    if [ -n "$cg" ]; then
      # (/proc/<pid>/cgroup: <id>:<controllers>:<path>, the path under the own one of a container when not in its namespace)
      while IFS= read -r l; do case $l in *"$cg") in=yes ;; esac; done 2>/dev/null < "/proc/$1/cgroup"
    fi
    [ "$in" = yes ] || echo "not started by the cassandra unit (pid $1 is not in its control group)"
  fi
  tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" > /dev/null || echo "its environment (/proc/$1/environ) not readable"
}

# heapdump_before <pid>: the cassandra.in.sh files bin/cassandra may source before cassandra-env.sh, in the same
# shell, that name CASSANDRA_HEAPDUMP_DIR (set there unexported, the environment of the JVM does not show it), one per
# line: CASSANDRA_INCLUDE (the environment of the JVM), else the first one found of the directory of bin/cassandra (next to
# the jars of the classpath of the JVM, ../bin, and /usr/sbin), ~/.cassandra.in.sh of the user it runs as, and the
# shared places. All of them, whichever bin/cassandra picks.
heapdump_before() {
  inc=$(tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" | sed -n 's/^CASSANDRA_INCLUDE=//p' | tail -1)
  home=$(tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" | sed -n 's/^HOME=//p' | tail -1)
  libs=$(tr '\0' '\n' 2>/dev/null < "/proc/$1/cmdline" | tr ':' '\n' | sed -n 's#/[^/]*[.]jar$##p' | sort -u)
  for f in $inc $(for d in $libs; do echo "$d/cassandra.in.sh $d/../bin/cassandra.in.sh"; done) \
      /usr/sbin/cassandra.in.sh ${home:+"$home/.cassandra.in.sh"} /usr/share/cassandra/cassandra.in.sh \
      /usr/local/share/cassandra/cassandra.in.sh /opt/cassandra/cassandra.in.sh; do
    grep -qs CASSANDRA_HEAPDUMP_DIR "$f" && echo "$f"
  done | sort -u
}
