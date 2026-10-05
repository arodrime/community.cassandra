from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_role

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

ROLE_ROW = {
    'role': 'app_user',
    'can_login': True,
    'is_superuser': False,
    'member_of': None,
    'salted_hash': 'xxx',
}


# BaseException: main() turns any Exception into fail_json, as exit_json's SystemExit is not one
class ExitJson(BaseException):
    pass


class FailJson(BaseException):
    pass


class FakeModule(object):

    def __init__(self, argument_spec=None, supports_check_mode=False):
        self.params = dict(FakeModule.params)
        self.check_mode = self.params.pop('_check_mode', False)

    def exit_json(self, **kwargs):
        raise ExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise FailJson(kwargs)


class FakeResult(list):
    """Enough of the driver's ResultSet: iterable, one() is None when empty."""

    def one(self):
        return self[0] if self else None


class FakeSession(object):

    def __init__(self, roles_rows=()):
        self.roles_rows = list(roles_rows)
        self.executed = []

    def execution_profile_clone_update(self, profile, **kwargs):
        return 'dict_profile'

    def execute(self, cql, execution_profile=None):
        self.executed.append(cql)
        if 'FROM system_auth.roles' in cql:
            return FakeResult(self.roles_rows)
        return FakeResult()


class FakeCluster(object):

    def __init__(self, session):
        self.session = session

    def connect(self):
        return self.session


def params(**kwargs):
    p = dict(
        login_user=None, login_password=None, ssl=False,
        ssl_cert_reqs='CERT_NONE', ssl_ca_certs='', login_host=None,
        login_port=9042, name='app_user', password='secret', state='present',
        super_user=False, login=True, options=None, data_centres=None,
        keyspace_permissions=None, roles=None, update_password=False,
        debug=False, consistency_level='LOCAL_ONE',
    )
    p.update(kwargs)
    return p


@pytest.fixture
def driver(monkeypatch):
    # The unit test environment has no cassandra-driver
    monkeypatch.setattr(cassandra_role, 'HAS_CASSANDRA_DRIVER', True)
    monkeypatch.setattr(cassandra_role, 'EXEC_PROFILE_DEFAULT', object(), raising=False)
    monkeypatch.setattr(cassandra_role, 'dict_factory', object(), raising=False)


def run(roles_rows, **kwargs):
    session_r = FakeSession(roles_rows)
    session_w = FakeSession()
    FakeModule.params = params(**kwargs)
    with patch.object(cassandra_role, 'AnsibleModule', FakeModule), \
            patch.object(cassandra_role, 'get_read_and_write_sessions',
                         return_value=(FakeCluster(session_r), FakeCluster(session_w))):
        with pytest.raises(ExitJson) as e:
            cassandra_role.main()
    return e.value.args[0], session_r, session_w


def role_reads(session):
    return [c for c in session.executed if 'FROM system_auth.roles' in c]


def test_get_role_properties_missing_role_is_none(driver):
    assert cassandra_role.get_role_properties(FakeSession([]), 'app_user') is None


def test_get_role_properties_returns_the_row(driver):
    assert cassandra_role.get_role_properties(FakeSession([ROLE_ROW]), 'app_user') == ROLE_ROW


@pytest.mark.parametrize('check_mode', [True, False])
def test_role_read_once(driver, check_mode):
    result, session_r, session_w = run([ROLE_ROW], _check_mode=check_mode, debug=True)
    assert result['changed'] is False
    assert result['role_exists'] is True
    assert len(role_reads(session_r)) == 1
    assert session_w.executed == []


def test_missing_role_check_mode(driver):
    result, session_r, session_w = run([], _check_mode=True, debug=True)
    assert result['changed'] is True
    assert result['role_exists'] is False
    assert len(role_reads(session_r)) == 1
    assert session_w.executed == []


def test_missing_role_is_created(driver):
    result, session_r, session_w = run([])
    assert result['changed'] is True
    assert session_w.executed[0].startswith("CREATE ROLE 'app_user' ")


def test_changed_role_is_altered(driver):
    result, session_r, session_w = run([ROLE_ROW], super_user=True)
    assert result['changed'] is True
    assert session_w.executed[0].startswith("ALTER ROLE 'app_user' WITH SUPERUSER = True")


def test_missing_role_absent(driver):
    result, session_r, session_w = run([], state='absent')
    assert result['changed'] is False
    assert session_w.executed == []


def test_existing_role_without_login(driver):
    result, session_r, session_w = run([ROLE_ROW], login=False)
    assert result['changed'] is False
    assert len(role_reads(session_r)) == 1
    assert session_w.executed == []


def test_missing_role_without_login_is_created(driver):
    result, session_r, session_w = run([], login=False)
    assert result['changed'] is True
    assert session_w.executed == ["CREATE ROLE 'app_user'"]
