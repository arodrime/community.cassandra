#!/usr/bin/env python3
"""Turn stock Cassandra conf files into cassandra_config templates.

Usage: gen_templates.py <series> <stock_dir> <templates_out_dir> [<5.0 stock cassandra.yaml>]
  e.g. gen_templates.py 4.1 ~/cassandra-4.1.12/conf roles/cassandra_config/templates/4.1 ~/cassandra-5.0.9/conf/cassandra.yaml

Each rule replaces one exact stock line (asserted unique, so an upstream
version that moved/changed it fails loudly). Replacements are inline {{ }}
only, so rendering with the role defaults gives back the stock file.

cassandra.yaml: 5.0's template is the hand-checked reference. Other series
are derived from it by YAML path: same line -> same variable; same key with
another stock value -> same variable, version-dependent default; key unknown
to 5.0 -> new cassandra_<key> variable. Defaults to add are printed.
"""
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REF_SERIES = "5.0"
REF_TEMPLATE = os.path.join(HERE, "..", "roles", "cassandra_config", "templates", REF_SERIES, "cassandra.yaml.j2")


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
    ] + gc_threads_rules(n)


def append_list(var):
    # Suffix for a file's last line: one extra line per list item.
    return "{{ ('\\n' ~ (%s | join('\\n'))) if %s else '' }}" % (var, var)


def last_line(var):
    line = "# The newline in the end of file is intentional"
    return (line, line + append_list(var))


COMMON = {
    "jvm-server.options": [
        ("#-Dcassandra.expiration_date_overflow_policy=REJECT",
         "#-Dcassandra.expiration_date_overflow_policy=REJECT" + append_list("cassandra_jvm_extra_options")),
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

ENV_COMMON = [
    # deb and rpm packages ship /var/log/cassandra here, not the tarball's value
    ('    CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"', '    CASSANDRA_LOG_DIR={{ cassandra_log_dir }}'),
    opt("cassandra_jmx_rmi_hostname",
        '# JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=<public name>"',
        ' JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=@"', "<public name>"),
    ("    LOCAL_JMX=yes", "    LOCAL_JMX={{ 'yes' if cassandra_local_jmx else 'no' }}"),
    ('JMX_PORT="7199"', 'JMX_PORT="{{ cassandra_jmx_port }}"'),
]


def gc_threads_rules(n):
    v = lambda name: "(cassandra_jvm%s_%s | default(cassandra_jvm_%s))" % (n, name, name)
    return [
        opt(v("parallel_gc_threads"), "#-XX:ParallelGCThreads=16", "-XX:ParallelGCThreads=@", "16"),
        opt(v("conc_gc_threads"), "#-XX:ConcGCThreads=16", "-XX:ConcGCThreads=@", "16"),
    ]


# 4.x: CMS by default (G1 commented out), heap and young gen set in pairs
RULES_4X = dict(COMMON, **{
    "cassandra-env.sh": ENV_COMMON + [
        opt("cassandra_heap_size", '#MAX_HEAP_SIZE="4G"', 'MAX_HEAP_SIZE="@"', "4G"),
        opt("cassandra_heap_newsize", '#HEAP_NEWSIZE="800M"', 'HEAP_NEWSIZE="@"', "800M"),
    ],
    "jvm8-server.options": gc_threads_rules(8) + [last_line("cassandra_jvm8_extra_options")],
    "jvm11-server.options": gc_threads_rules(11) + [last_line("cassandra_jvm11_extra_options")],
})

RULES = {
    "5.0": dict(COMMON, **{
        "cassandra-env.sh": ENV_COMMON + [
            opt("cassandra_heap_size", '#MAX_HEAP_SIZE="20G"', 'MAX_HEAP_SIZE="@"', "20G"),
            opt("cassandra_max_direct_memory_size", '#MAX_DIRECT_MEMORY_SIZE="10G"', 'MAX_DIRECT_MEMORY_SIZE="@"', "10G"),
            ('    CASSANDRA_HEAPDUMP_DIR="$CASSANDRA_LOG_DIR"',
             '    CASSANDRA_HEAPDUMP_DIR="{{ cassandra_heap_dump_dir }}"'),
        ],
        "jvm11-server.options": jvm_rules(11) + [last_line("cassandra_jvm11_extra_options")],
        "jvm17-server.options": jvm_rules(17) + [last_line("cassandra_jvm17_extra_options")],
    }),
    "4.1": RULES_4X,
    "4.0": RULES_4X,
}


def yaml_paths(lines):
    # One key per line: raw text for comments/blank, YAML path for settings.
    stack, out = [], []  # stack: [indent, key, child item count]
    for line in lines:
        body = line.lstrip()
        if not body or body.startswith("#"):
            out.append(line)
            continue
        indent = len(line) - len(body)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if body.startswith("- "):
            parent = stack[-1] if stack else [None, None, 0]
            parent[2] += 1
            stack.append([indent, "[%d]" % parent[2], 0])
            body, indent = body[2:], indent + 2
        stack.append([indent, body.split(":", 1)[0].strip(), 0])
        out.append(tuple(k for _, k, _ in stack))
    return out


def split_value(line):
    # "key: value  # comment" -> ("key:", "value", "  # comment")
    key, _, rest = line.partition(":")
    m = re.match(r"\s*(.*?)(\s+#.*|\s*)$", rest)
    return key + ":", m.group(1), m.group(2)


def value_expr(value, var):
    for q in ("'", '"'):
        if len(value) > 1 and value[0] == q == value[-1]:
            return "%s{{ %s }}%s" % (q, var, q), value[1:-1]
    parsed = yaml.safe_load(value)
    if isinstance(parsed, bool):
        return "{{ %s | lower }}" % var, parsed
    return "{{ %s }}" % var, parsed


# Nested keys named like their 5.0 siblings (cassandra_server_keystore, ...)
RENAMES = {
    "server_encryption_options_keystore_password": "server_keystore_password",
    "server_encryption_options_truststore_password": "truststore_password",
    "client_encryption_options_keystore_password": "client_keystore_password",
    "server_encryption_options_enable_legacy_ssl_storage_port": "enable_legacy_ssl_storage_port",
}


def derive_yaml(ref_stock, ref_tpl, stock):
    ref = {}
    for key, a, b in zip(yaml_paths(ref_stock), ref_stock, ref_tpl):
        ref[key] = (a, b)
    known = set(yaml_paths(ref_stock))
    out, new_vars, conflicts = [], {}, {}
    for key, line in zip(yaml_paths(stock), stock):
        if key in ref and ref[key][0] != ref[key][1]:
            a, b = ref[key]
            if not isinstance(key, tuple):
                out.append(b)
                continue
            # Same setting, indentation may differ: keep the variable
            prefix, value, suffix = split_value(line)
            if value == split_value(a)[1]:
                out.append(line[:len(line) - len(line.lstrip())] + b.lstrip())
                continue
            var = re.search(r"cassandra_\w+", b).group(0)
            expr, default = value_expr(value, var)
            out.append("%s %s%s" % (prefix, expr, suffix))
            conflicts[var] = default
        elif isinstance(key, tuple) and key not in known and split_value(line)[1]:
            prefix, value, suffix = split_value(line)
            name = "_".join(k for k in key if not k.startswith("["))
            var = "cassandra_" + RENAMES.get(name, name)
            expr, default = value_expr(value, var)
            out.append("%s %s%s" % (prefix, expr, suffix))
            new_vars[var] = default
        else:
            out.append(line)
    return out, new_vars, conflicts


def main(series, stock_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, rules in RULES[series].items():
        with open(os.path.join(stock_dir, name)) as f:
            lines = f.read().split("\n")
        for old, new in rules:
            hits = [i for i, line in enumerate(lines) if line == old]
            assert len(hits) == 1, "%s: %r found %d times" % (name, old, len(hits))
            lines[hits[0]] = new
        with open(os.path.join(out_dir, name + ".j2"), "w") as f:
            f.write("\n".join(lines))

    if series != REF_SERIES:
        ref_stock = sys.argv[4] if len(sys.argv) > 4 else None
        assert ref_stock, "deriving cassandra.yaml needs the %s stock cassandra.yaml as 4th argument" % REF_SERIES
        with open(ref_stock) as a, open(REF_TEMPLATE) as b, open(os.path.join(stock_dir, "cassandra.yaml")) as c:
            lines, new_vars, conflicts = derive_yaml(a.read().split("\n"), b.read().split("\n"), c.read().split("\n"))
        with open(os.path.join(out_dir, "cassandra.yaml.j2"), "w") as f:
            f.write("\n".join(lines))
        print("# %s: new variables" % series)
        print(yaml.safe_dump(new_vars, sort_keys=False) if new_vars else "{}")
        print("# %s: stock value differs from %s for" % (series, REF_SERIES))
        print(yaml.safe_dump(conflicts, sort_keys=False) if conflicts else "{}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
