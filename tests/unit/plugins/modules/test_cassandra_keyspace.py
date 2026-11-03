from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_keyspace

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

SIMPLE = ("CREATE KEYSPACE myks WITH replication = "
          "{{'class': 'SimpleStrategy', 'replication_factor': '{0}'}}  AND durable_writes = true;")
NTS = ("CREATE KEYSPACE myks WITH replication = "
       "{{'class': 'NetworkTopologyStrategy', {0}}}  AND durable_writes = true;")


def simple(rf):
    return SIMPLE.format(rf)


def nts(**dcs):
    return NTS.format(", ".join("'{0}': '{1}'".format(dc, rf) for dc, rf in sorted(dcs.items())))


# exit_json and fail_json raise SystemExit, which main() does not catch
class ExitJson(SystemExit):
    pass


class FailJson(SystemExit):
    pass


class FakeModule(object):

    def __init__(self, argument_spec=None, supports_check_mode=False):
        self.params = dict(FakeModule.params)
        self.check_mode = self.params.pop('_check_mode', False)

    def exit_json(self, **kwargs):
        raise ExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise FailJson(kwargs)


class FakeKeyspace(object):

    def __init__(self, cql):
        self.cql = cql

    def export_as_string(self):
        return self.cql


class FakeCluster(object):

    def __init__(self, cql):
        self.metadata = type('Metadata', (), {'keyspaces': {'myks': FakeKeyspace(cql)}})()
        self.executed = []

    def connect(self):
        return self

    def execute(self, cql):
        self.executed.append(cql)


def is_changed(existing, replication_factor=1, durable_writes=True, data_centres=None):
    FakeModule.params = {}
    return cassandra_keyspace.keyspace_is_changed(FakeModule(), FakeCluster(existing), 'myks',
                                                  replication_factor, durable_writes, data_centres)


def run_main(existing, check_mode=False, exists=True, **params):
    """Run main() on keyspace myks (existing unless exists=False); return (exception, CQL executed)."""
    FakeModule.params = dict(login_user=None, login_password=None, login_host=['127.0.0.1'],
                             login_port=9042, ssl=False, ssl_cert_reqs='CERT_NONE',
                             ssl_ca_certs='', name='myks', state='present',
                             replication_factor=1, durable_writes=True, data_centres=None,
                             consistency_level='LOCAL_ONE', _check_mode=check_mode)
    FakeModule.params.update(params)
    cluster = FakeCluster(existing)
    with patch.object(cassandra_keyspace, 'AnsibleModule', FakeModule), \
            patch.object(cassandra_keyspace, 'HAS_CASSANDRA_DRIVER', True), \
            patch.object(cassandra_keyspace, 'get_read_and_write_sessions',
                         return_value=(cluster, cluster)), \
            patch.object(cassandra_keyspace, 'keyspace_exists', return_value=exists):
        with pytest.raises((ExitJson, FailJson)) as exc:
            cassandra_keyspace.main()
    return exc.value, cluster.executed


class TestKeyspaceIsChanged:

    def test_simple_unchanged(self):
        assert is_changed(simple(1)) is False

    def test_simple_rf_change(self):
        assert is_changed(simple(1), replication_factor=3) is True

    @pytest.mark.parametrize("rf", [1, 3])
    def test_simple_to_nts(self, rf):
        assert is_changed(simple(1), data_centres={'dc1': rf}) is True

    def test_nts_unchanged(self):
        assert is_changed(nts(dc1=3, dc2=1), data_centres={'dc1': 3, 'dc2': 1}) is False

    def test_nts_rf_change(self):
        assert is_changed(nts(dc1=3), data_centres={'dc1': 1}) is True

    def test_nts_dc_added(self):
        assert is_changed(nts(dc1=3), data_centres={'dc1': 3, 'dc2': 3}) is True

    def test_nts_dc_removed(self):
        assert is_changed(nts(dc1=3, dc2=3), data_centres={'dc1': 3}) is True

    def test_nts_to_simple_is_refused(self):
        with pytest.raises(FailJson) as exc:
            is_changed(nts(dc1=3), replication_factor=3)
        assert "not supported" in exc.value.args[0]['msg']

    def test_empty_data_centres_is_unset(self):
        assert is_changed(simple(1), data_centres={}) is False
        assert is_changed(simple(1), replication_factor=3, data_centres={}) is True
        with pytest.raises(FailJson):
            is_changed(nts(dc1=3), data_centres={})


def test_keyspace_cql_empty_data_centres():
    assert cassandra_keyspace.keyspace_cql('myks', 3, True, {}, True) == (
        "ALTER KEYSPACE myks WITH REPLICATION = { 'class' : 'SimpleStrategy', "
        "'replication_factor': 3 } AND DURABLE_WRITES = True")


class TestMain:

    @pytest.mark.parametrize("rf", [1, 3])
    def test_simple_to_nts(self, rf):
        res, executed = run_main(simple(1), data_centres={'dc1': rf})
        assert isinstance(res, ExitJson)
        assert res.args[0]['changed'] is True
        assert executed == [res.args[0]['cql']]
        assert executed[0] == ("ALTER KEYSPACE myks WITH REPLICATION = {{ 'class' : "
                               "'NetworkTopologyStrategy',  'dc1' : {0} }} AND DURABLE_WRITES = True"
                               .format(rf))

    def test_simple_rf_change(self):
        res, executed = run_main(simple(1), replication_factor=3)
        assert res.args[0]['changed'] is True
        assert executed == ["ALTER KEYSPACE myks WITH REPLICATION = { 'class' : 'SimpleStrategy', "
                            "'replication_factor': 3 } AND DURABLE_WRITES = True"]

    @pytest.mark.parametrize("existing, params", [
        (simple(1), {}),
        (nts(dc1=3), {'data_centres': {'dc1': 3}}),
    ])
    def test_unchanged(self, existing, params):
        res, executed = run_main(existing, **params)
        assert isinstance(res, ExitJson)
        assert res.args[0]['changed'] is False
        assert 'cql' not in res.args[0]
        assert executed == []

    @pytest.mark.parametrize("check_mode", [False, True])
    def test_nts_to_simple_is_refused(self, check_mode):
        res, executed = run_main(nts(dc1=3), check_mode=check_mode, replication_factor=3)
        assert isinstance(res, FailJson)
        assert res.args[0]['msg'] == ("Keyspace myks uses NetworkTopologyStrategy and moving it "
                                      "to SimpleStrategy is not supported. Set data_centres "
                                      "to manage its replication.")
        assert executed == []

    def test_check_mode(self):
        res, executed = run_main(simple(1), check_mode=True, data_centres={'dc1': 3})
        assert isinstance(res, ExitJson)
        assert res.args[0]['changed'] is True
        assert "'NetworkTopologyStrategy',  'dc1' : 3 }" in res.args[0]['cql']
        assert res.args[0]['cql'].startswith("ALTER KEYSPACE myks ")
        assert executed == []

    def test_check_mode_new_keyspace(self):
        res, executed = run_main(None, check_mode=True, exists=False, data_centres={'dc1': 3})
        assert isinstance(res, ExitJson)
        assert res.args[0]['changed'] is True
        assert res.args[0]['cql'] == ("CREATE KEYSPACE myks WITH REPLICATION = { 'class' : "
                                      "'NetworkTopologyStrategy',  'dc1' : 3 } AND DURABLE_WRITES = True")
        assert executed == []
