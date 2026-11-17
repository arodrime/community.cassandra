from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config: remote JMX over TLS (cassandra_jmx_ssl_*): the checks, the stock lines of the remote branch of
# cassandra-env.sh switched on, nodetool-ssl.properties.

import os

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _load_role, _render
from ansible_collections.community.cassandra.tests.unit.roles.test_config_declared import run

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}
CHECK = "Refuse JMX TLS settings the JVM would not start with"
ON = {"cassandra_local_jmx": False, "cassandra_jmx_ssl_enabled": True, "cassandra_jmx_ssl_keystore": "/srv/zorglub/ks.jks",
      "cassandra_jmx_ssl_keystore_password": "s3cret"}


@pytest.mark.parametrize("changes, wrong", [
    ({}, None),
    (ON, None),
    (dict(ON, cassandra_jmx_ssl_need_client_auth=True, cassandra_jmx_ssl_truststore="/srv/zorglub/ts.jks"), None),
    (dict(ON, cassandra_local_jmx=True), "cassandra_jmx_ssl_enabled needs remote JMX (cassandra_local_jmx: false)"),
    (dict(ON, cassandra_jmx_ssl_keystore=""), "cassandra_jmx_ssl_enabled needs cassandra_jmx_ssl_keystore and"),
    (dict(ON, cassandra_jmx_ssl_keystore_password=""), "cassandra_jmx_ssl_enabled needs cassandra_jmx_ssl_keystore and"),
    (dict(ON, cassandra_jmx_ssl_need_client_auth=True), "cassandra_jmx_ssl_need_client_auth needs"),
    ({"cassandra_jmx_ssl_need_client_auth": True, "cassandra_jmx_ssl_truststore": "/t"}, "need_client_auth needs"),
    (dict(ON, cassandra_jmx_ssl_keystore_password="a b"), "cassandra_jmx_ssl_keystore_password: no blank, quote"),
    (dict(ON, cassandra_jmx_ssl_keystore_password='a"b'), "cassandra_jmx_ssl_keystore_password: no blank, quote"),
    (dict(ON, cassandra_jmx_ssl_keystore_password="a$b"), "cassandra_jmx_ssl_keystore_password: no blank, quote"),
    (dict(ON, cassandra_jmx_ssl_keystore_password="a`b"), "cassandra_jmx_ssl_keystore_password: no blank, quote"),
    (dict(ON, cassandra_jmx_ssl_keystore_password="a\\b"), "cassandra_jmx_ssl_keystore_password: no blank, quote"),
    (dict(ON, cassandra_jmx_ssl_protocols="TLSv1.3, TLSv1.2"), "cassandra_jmx_ssl_protocols: no blank"),
    (dict(ON, cassandra_jmx_ssl_keystore_password="p@ss/w=rd+1#x'y"), None),
])
def test_jmx_tls_checks(changes, wrong):
    passed, msg = run(CHECK, "50x", **changes)
    assert passed is (wrong is None)
    if wrong:
        assert wrong in msg and msg.endswith(". Nothing is changed.")


def env_lines(series, **changes):
    env, ctx, dummy = _load_role(series, FACTS)
    ctx.update(changes)
    return _render(env, series, "cassandra-env.sh", ctx)[1]


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_the_stock_tls_lines_switched_on(series):
    lines = env_lines(series, **dict(ON, cassandra_jmx_ssl_truststore="/srv/zorglub/ts.jks",
                                     cassandra_jmx_ssl_truststore_password="t0p", cassandra_jmx_ssl_need_client_auth=True,
                                     cassandra_jmx_ssl_protocols="TLSv1.3,TLSv1.2"))
    remote = lines[lines.index("  # jmx ssl options") + 1:lines.index("  # jmx ssl options") + 9]
    assert remote == [
        '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.ssl=true"',
        '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.ssl.need.client.auth=true"',
        '  JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.ssl.enabled.protocols=TLSv1.3,TLSv1.2"',
        '  #JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.ssl.enabled.cipher.suites=<enabled-cipher-suites>"',
        '  JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStore=/srv/zorglub/ks.jks"',
        '  JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStorePassword=s3cret"',
        '  JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.trustStore=/srv/zorglub/ts.jks"',
        '  JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.trustStorePassword=t0p"']
    # off: the stock lines
    stock = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "molecule",
                         "default", "files", {"40x": "stock-4.0.21", "41x": "stock-4.1.12", "50x": "stock-5.0.9"}[series],
                         "cassandra-env.sh.stock")
    with open(stock) as f:
        wanted = [line for line in f.read().split("\n") if "jmxremote.ssl" in line or "javax.net.ssl" in line]
    assert [line for line in env_lines(series) if "jmxremote.ssl" in line or "javax.net.ssl" in line] == wanted


# --- the client side ---------------------------------------------------------------------------------------------

import yaml  # noqa: E402

from ansible_collections.community.cassandra.tests.unit.roles.test_config_declared import render  # noqa: E402

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def role_vars(role, kind="vars"):
    with open(os.path.join(TOP, "roles", role, kind, "main.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def properties(**changes):
    with open(os.path.join(TOP, "roles", "cassandra_config", "templates", "nodetool-ssl.properties.j2")) as f:
        variables = dict(role_vars("cassandra_config", "defaults"), **dict(ON, **changes))
        return render(f.read(), **variables).split("\n")


def test_nodetool_trusts_the_node_with_its_keystore_or_the_truststore():
    assert [line for line in properties() if line] == [
        "-Dcom.sun.management.jmxremote.registry.ssl=true", "-Djavax.net.ssl.trustStore=/srv/zorglub/ks.jks",
        "-Djavax.net.ssl.trustStorePassword=s3cret"]
    lines = properties(cassandra_jmx_ssl_truststore="/srv/zorglub/ts.jks", cassandra_jmx_ssl_truststore_password="t0p",
                       cassandra_jmx_ssl_need_client_auth=True, cassandra_jmx_ssl_protocols="TLSv1.3")
    assert [line for line in lines if line] == [
        "-Dcom.sun.management.jmxremote.registry.ssl=true", "-Djavax.net.ssl.trustStore=/srv/zorglub/ts.jks",
        "-Djavax.net.ssl.trustStorePassword=t0p", "-Djavax.net.ssl.keyStore=/srv/zorglub/ks.jks",
        "-Djavax.net.ssl.keyStorePassword=s3cret", "-Djavax.rmi.ssl.client.enabledProtocols=TLSv1.3"]


@pytest.mark.parametrize("role", ["cassandra_operation", "cassandra_service"])
@pytest.mark.parametrize("enabled, ssl", [(None, False), (False, False), (True, True), ("true", True)])
def test_the_collections_nodetool_calls_use_ssl(role, enabled, ssl):
    variables = dict(role_vars(role), cassandra_jmx_username="", cassandra_jmx_password="", cassandra_jmx_password_file="")
    if enabled is not None:
        variables["cassandra_jmx_ssl_enabled"] = enabled
    assert render(variables["_cassandra_service_jmx"], **variables).get("ssl", False) is ssl
    if role == "cassandra_operation":
        assert render(variables["_cassandra_service_nodetool"], **variables).endswith(" --ssl") is ssl


@pytest.mark.parametrize("enabled, written", [(None, ""), (False, ""), (True, True)])
def test_medusas_nodetool_ssl_follows_jmx_over_tls(enabled, written):
    defaults = role_vars("cassandra_medusa", "defaults")
    variables = {} if enabled is None else {"cassandra_jmx_ssl_enabled": enabled}
    assert render(defaults["cassandra_medusa_nodetool_ssl"], **variables) == written
