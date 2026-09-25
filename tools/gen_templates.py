#!/usr/bin/env python3
"""Turn stock Cassandra conf files into cassandra_config templates.

Usage: gen_templates.py <stock_dir> <templates_out_dir>
  e.g. gen_templates.py ~/cassandra-5.0.9/conf roles/cassandra_config/templates/5.0

cassandra.yaml.j2 is not generated here (one variable per active key,
maintained by hand); all other files are.

Each rule replaces one exact stock line (asserted unique, so an upstream
version that moved/changed it fails loudly). Replacements are inline {{ }}
only, so rendering with the role defaults gives back the stock file.
"""
import os
import sys


def opt(var, commented, active, example):
    # Commented in stock; uncommented with the var's value once it is set,
    # otherwise left as is (stock example value included).
    value = "{{ %s or '%s' }}" % (var, example)
    return (commented, "{{ '' if %s else '#' }}%s" % (var, active.replace("@", value)))


def jvm_rules(n):
    v = lambda name: "(cassandra_jvm%s_%s | default(cassandra_jvm_%s))" % (n, name, name)
    return [
        ("-XX:MaxTenuringThreshold=2", "-XX:MaxTenuringThreshold={{ %s }}" % v("max_tenuring_threshold")),
        ("-XX:G1HeapRegionSize=16m", "-XX:G1HeapRegionSize={{ %s }}" % v("g1_heap_region_size")),
        ("-XX:G1NewSizePercent=50", "-XX:G1NewSizePercent={{ %s }}" % v("g1_new_size_percent")),
        ("-XX:MaxGCPauseMillis=300", "-XX:MaxGCPauseMillis={{ %s }}" % v("max_gc_pause_millis")),
        ("-XX:InitiatingHeapOccupancyPercent=70",
         "-XX:InitiatingHeapOccupancyPercent={{ %s }}" % v("initiating_heap_occupancy_percent")),
        opt(v("parallel_gc_threads"), "#-XX:ParallelGCThreads=16", "-XX:ParallelGCThreads=@", "16"),
        opt(v("conc_gc_threads"), "#-XX:ConcGCThreads=16", "-XX:ConcGCThreads=@", "16"),
    ]


def append_list(var):
    # Suffix for a file's last line: one extra line per list item.
    return "{{ ('\\n' ~ (%s | join('\\n'))) if %s else '' }}" % (var, var)


RULES = {
    "cassandra-env.sh": [
        # deb and rpm packages ship /var/log/cassandra here, not the tarball's value
        ('    CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"', '    CASSANDRA_LOG_DIR={{ cassandra_log_dir }}'),
        opt("cassandra_heap_size", '#MAX_HEAP_SIZE="20G"', 'MAX_HEAP_SIZE="@"', "20G"),
        opt("cassandra_max_direct_memory_size", '#MAX_DIRECT_MEMORY_SIZE="10G"', 'MAX_DIRECT_MEMORY_SIZE="@"', "10G"),
        ('    CASSANDRA_HEAPDUMP_DIR="$CASSANDRA_LOG_DIR"',
         '    CASSANDRA_HEAPDUMP_DIR="{{ cassandra_heap_dump_dir }}"'),
        opt("cassandra_jmx_rmi_hostname",
            '# JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<public name>"',
            ' JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=@"', "<public name>"),
        ("    LOCAL_JMX=yes", "    LOCAL_JMX={{ 'yes' if cassandra_local_jmx else 'no' }}"),
        ('JMX_PORT="7199"', 'JMX_PORT="{{ cassandra_jmx_port }}"'),
    ],
    "jvm-server.options": [
        ("#-Dcassandra.expiration_date_overflow_policy=REJECT",
         "#-Dcassandra.expiration_date_overflow_policy=REJECT" + append_list("cassandra_jvm_extra_options")),
    ],
    "jvm11-server.options": jvm_rules(11) + [
        ("# The newline in the end of file is intentional",
         "# The newline in the end of file is intentional" + append_list("cassandra_jvm11_extra_options")),
    ],
    "jvm17-server.options": jvm_rules(17) + [
        ("# The newline in the end of file is intentional",
         "# The newline in the end of file is intentional" + append_list("cassandra_jvm17_extra_options")),
    ],
    "cassandra-rackdc.properties": [
        ("dc=dc1", "dc={{ cassandra_dc }}"),
        ("rack=rack1", "rack={{ cassandra_rack }}"),
        ("# prefer_local=true", "{{ '' if cassandra_prefer_local else '# ' }}prefer_local=true"),
    ],
    "logback.xml": [
        ('  <root level="INFO">', '  <root level="{{ cassandra_log_level }}">'),
        ('    <appender-ref ref="ASYNCDEBUGLOG" /> <!-- Comment this line to disable debug.log -->',
         "    {{ '' if cassandra_debug_log_enabled else '<!-- ' }}"
         '<appender-ref ref="ASYNCDEBUGLOG" />'
         "{{ '' if cassandra_debug_log_enabled else ' -->' }} <!-- Comment this line to disable debug.log -->"),
        ('  <logger name="org.apache.cassandra" level="DEBUG"/>',
         '  <logger name="org.apache.cassandra" level="{{ cassandra_log_level_cassandra }}"/>'),
    ],
}


def main(stock_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, rules in RULES.items():
        with open(os.path.join(stock_dir, name)) as f:
            lines = f.read().split("\n")
        for old, new in rules:
            hits = [i for i, line in enumerate(lines) if line == old]
            assert len(hits) == 1, "%s: %r found %d times" % (name, old, len(hits))
            lines[hits[0]] = new
        with open(os.path.join(out_dir, name + ".j2"), "w") as f:
            f.write("\n".join(lines))


if __name__ == "__main__":
    main(*sys.argv[1:])
