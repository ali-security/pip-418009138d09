import datetime
import json
import logging
import os
import sys

import freezegun
import pretend
import pytest

from pip._internal import self_outdated_check
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.self_outdated_check import (
    SelfCheckState,
    logger,
    pip_self_version_check_emit,
    pip_self_version_check_fetch,
)
from tests.lib.path import Path


class MockBestCandidateResult(object):
    def __init__(self, best):
        self.best_candidate = best


class MockPackageFinder(object):

    BASE_URL = 'https://pypi.org/simple/pip-{0}.tar.gz'
    PIP_PROJECT_NAME = 'pip'
    INSTALLATION_CANDIDATES = [
        InstallationCandidate(PIP_PROJECT_NAME, '6.9.0',
                              BASE_URL.format('6.9.0')),
        InstallationCandidate(PIP_PROJECT_NAME, '3.3.1',
                              BASE_URL.format('3.3.1')),
        InstallationCandidate(PIP_PROJECT_NAME, '1.0',
                              BASE_URL.format('1.0')),
    ]

    @classmethod
    def create(cls, *args, **kwargs):
        return cls()

    def find_best_candidate(self, project_name):
        return MockBestCandidateResult(self.INSTALLATION_CANDIDATES[0])


class MockDistribution(object):
    def __init__(self, installer):
        self.installer = installer

    def has_metadata(self, name):
        return name == 'INSTALLER'

    def get_metadata_lines(self, name):
        if self.has_metadata(name):
            yield self.installer
        else:
            raise NotImplementedError('nope')


def _options():
    ''' Some default options that we pass to
    self_outdated_check.pip_self_version_check_fetch '''
    return pretend.stub(
        find_links=[], index_url='default_url', extra_index_urls=[],
        no_index=False, pre=False, cache_dir='',
    )


@pytest.mark.parametrize(
    [
        'stored_time',
        'installed_ver',
        'new_ver',
        'installer',
        'check_if_upgrade_required',
        'check_warn_logs',
    ],
    [
        # Test we return None when installed version is None
        ('1970-01-01T10:00:00Z', None, '1.0', 'pip', False, False),
        # Need an upgrade - upgrade warning should print
        ('1970-01-01T10:00:00Z', '1.0', '6.9.0', 'pip', True, True),
        # Upgrade available, pip installed via rpm - warning should not print
        ('1970-01-01T10:00:00Z', '1.0', '6.9.0', 'rpm', True, False),
        # No upgrade - upgrade warning should not print
        ('1970-01-9T10:00:00Z', '6.9.0', '6.9.0', 'pip', False, False),
    ]
)
def test_pip_self_version_check(monkeypatch, stored_time, installed_ver,
                                new_ver, installer,
                                check_if_upgrade_required, check_warn_logs):
    monkeypatch.setattr(self_outdated_check, 'get_installed_version',
                        lambda name: installed_ver)
    monkeypatch.setattr(self_outdated_check, 'PackageFinder',
                        MockPackageFinder)
    monkeypatch.setattr(logger, 'warning',
                        pretend.call_recorder(lambda *a, **kw: None))
    monkeypatch.setattr(logger, 'debug',
                        pretend.call_recorder(lambda s, exc_info=None: None))
    monkeypatch.setattr(self_outdated_check, 'get_distribution',
                        lambda name: MockDistribution(installer))

    fake_state = pretend.stub(
        state={"last_check": stored_time, 'pypi_version': installed_ver},
        save=pretend.call_recorder(lambda v, t: None),
    )
    monkeypatch.setattr(
        self_outdated_check, 'SelfCheckState', lambda **kw: fake_state
    )

    with freezegun.freeze_time(
        "1970-01-09 10:00:00",
        ignore=[
            "six.moves",
            "pip._vendor.six.moves",
            "pip._vendor.requests.packages.urllib3.packages.six.moves",
        ]
    ):
        upgrade_prompt = pip_self_version_check_fetch(None, _options())

    # The fetch half never talks to the user.
    assert len(logger.warning.calls) == 0

    # See we return None if not installed_version
    if not installed_ver:
        assert upgrade_prompt is None
    # See that we saved the correct version
    elif check_if_upgrade_required:
        assert fake_state.save.calls == [
            pretend.call(new_ver, datetime.datetime(1970, 1, 9, 10, 00, 00)),
        ]
    else:
        # Make sure no Exceptions
        assert not logger.debug.calls
        # See that save was not called
        assert fake_state.save.calls == []

    # Only the emit half writes anything out, and it only ever formats the
    # values the fetch half already computed.
    if check_warn_logs:
        assert upgrade_prompt == (installed_ver, new_ver)
    else:
        assert upgrade_prompt is None

    pip_self_version_check_emit(upgrade_prompt)

    # Ensure we warn the user or not
    if check_warn_logs:
        assert len(logger.warning.calls) == 1
    else:
        assert len(logger.warning.calls) == 0


def _self_check_records(caplog):
    return [r for r in caplog.records if r.name == logger.name]


def test_pip_self_version_check_emit_no_prompt_is_silent(caplog):
    """``None`` means "nothing to say" -- emit must stay quiet."""
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        pip_self_version_check_emit(None)

    assert _self_check_records(caplog) == []


def test_pip_self_version_check_emit_logs_a_single_warning(caplog):
    """A prompt is rendered with exactly the message pip has always used."""
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        pip_self_version_check_emit(('1.0', '6.9.0'))

    records = _self_check_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "You are using pip version 1.0; however, version 6.9.0 is "
        "available.\nYou should consider upgrading via the "
        "'{} -m pip install --upgrade pip' command.".format(sys.executable)
    )


def test_pip_self_version_check_emit_does_no_work(monkeypatch):
    """The emit half must not re-read anything from the environment.

    Everything the check needs is gathered before the command body runs, so
    that a command which just replaced pip's own files on disk cannot make
    the check load and execute its code.
    """
    def explode(*args, **kwargs):
        raise AssertionError("emit must not touch the environment")

    monkeypatch.setattr(self_outdated_check, 'get_installed_version', explode)
    monkeypatch.setattr(self_outdated_check, 'get_distribution', explode)
    monkeypatch.setattr(self_outdated_check, 'was_installed_by_pip', explode)
    monkeypatch.setattr(self_outdated_check, 'SelfCheckState', explode)
    monkeypatch.setattr(self_outdated_check, 'LinkCollector', explode)
    monkeypatch.setattr(self_outdated_check, 'PackageFinder', explode)

    pip_self_version_check_emit(('1.0', '6.9.0'))
    pip_self_version_check_emit(None)


def test_pip_self_version_check_fetch_swallows_errors(monkeypatch):
    """A broken index must not break the command that triggered the check."""
    monkeypatch.setattr(self_outdated_check, 'get_installed_version',
                        lambda name: '1.0')

    def explode(*args, **kwargs):
        raise ValueError('nope')

    monkeypatch.setattr(self_outdated_check, 'SelfCheckState', explode)

    assert pip_self_version_check_fetch(None, _options()) is None


statefile_name_case_1 = (
    "fcd2d5175dd33d5df759ee7b045264230205ef837bf9f582f7c3ada7"
)

statefile_name_case_2 = (
    "902cecc0745b8ecf2509ba473f3556f0ba222fedc6df433acda24aa5"
)


@pytest.mark.parametrize("key,expected", [
    ("/hello/world/venv", statefile_name_case_1),
    ("C:\\Users\\User\\Desktop\\venv", statefile_name_case_2),
])
def test_get_statefile_name_known_values(key, expected):
    assert expected == self_outdated_check._get_statefile_name(key)


def _get_statefile_path(cache_dir, key):
    return os.path.join(
        cache_dir, "selfcheck", self_outdated_check._get_statefile_name(key)
    )


def test_self_check_state_no_cache_dir():
    state = SelfCheckState(cache_dir=False)
    assert state.state == {}
    assert state.statefile_path is None


def test_self_check_state_key_uses_sys_prefix(monkeypatch):
    key = "helloworld"

    monkeypatch.setattr(sys, "prefix", key)
    state = self_outdated_check.SelfCheckState("")

    assert state.key == key


def test_self_check_state_reads_expected_statefile(monkeypatch, tmpdir):
    cache_dir = tmpdir / "cache_dir"
    cache_dir.mkdir()
    key = "helloworld"
    statefile_path = _get_statefile_path(str(cache_dir), key)

    last_check = "1970-01-02T11:00:00Z"
    pypi_version = "1.0"
    content = {
        "key": key,
        "last_check": last_check,
        "pypi_version": pypi_version,
    }

    Path(statefile_path).parent.mkdir()

    with open(statefile_path, "w") as f:
        json.dump(content, f)

    monkeypatch.setattr(sys, "prefix", key)
    state = self_outdated_check.SelfCheckState(str(cache_dir))

    assert state.state["last_check"] == last_check
    assert state.state["pypi_version"] == pypi_version


def test_self_check_state_writes_expected_statefile(monkeypatch, tmpdir):
    cache_dir = tmpdir / "cache_dir"
    cache_dir.mkdir()
    key = "helloworld"
    statefile_path = _get_statefile_path(str(cache_dir), key)

    last_check = datetime.datetime.strptime(
        "1970-01-02T11:00:00Z", self_outdated_check.SELFCHECK_DATE_FMT
    )
    pypi_version = "1.0"

    monkeypatch.setattr(sys, "prefix", key)
    state = self_outdated_check.SelfCheckState(str(cache_dir))

    state.save(pypi_version, last_check)
    with open(statefile_path) as f:
        saved = json.load(f)

    expected = {
        "key": key,
        "last_check": last_check.strftime(
            self_outdated_check.SELFCHECK_DATE_FMT),
        "pypi_version": pypi_version,
    }
    assert expected == saved
