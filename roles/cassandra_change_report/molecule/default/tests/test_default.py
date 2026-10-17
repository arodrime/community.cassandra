import glob
import os

import testinfra.utils.ansible_runner
import yaml

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

REPORTS = os.path.join(os.environ['MOLECULE_EPHEMERAL_DIRECTORY'], 'reports')


def report(run, host, role):
    files = sorted(glob.glob(os.path.join(REPORTS, run, '*', f'{host}-{role}.yml')))
    assert files, f'no {run} report for {host} {role}'
    with open(files[0]) as f:
        return yaml.safe_load(f)


def items(doc):
    return {c['item']: c for c in doc['changes']}


def test_check_on_a_blank_host(host):
    name = host.backend.get_hostname()
    install = report('check', name, 'cassandra_install')

    assert install['mode'] == 'check'
    assert items(install)['package cassandra']['after'] == 'not in configured repos yet'


def test_apply_reports_installed_versions(host):
    name = host.backend.get_hostname()
    install = report('apply', name, 'cassandra_install')

    assert install['mode'] == 'applied'
    assert items(install)['package cassandra']['after'].startswith('5.0.')


def test_rerun_reports_nothing():
    assert not glob.glob(os.path.join(REPORTS, 'rerun', '*', '*.yml'))


def test_reports_private():
    files = glob.glob(os.path.join(REPORTS, '*', '*', '*.yml'))
    assert files
    for path in files:
        assert oct(os.stat(path).st_mode & 0o777) == '0o600', path
        assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == '0o700', path
