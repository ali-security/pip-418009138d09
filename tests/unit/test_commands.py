import pytest
from mock import patch

from pip._internal.cli.req_command import (
    IndexGroupCommand,
    RequirementCommand,
    SessionCommandMixin,
)
from pip._internal.commands import commands_dict, create_command
from pip._internal.commands.install import _arg_refers_to_pip

# These are the expected names of the commands whose classes inherit from
# IndexGroupCommand.
EXPECTED_INDEX_GROUP_COMMANDS = ['download', 'install', 'list', 'wheel']


def check_commands(pred, expected):
    """
    Check the commands satisfying a predicate.
    """
    commands = [create_command(name) for name in sorted(commands_dict)]
    actual = [command.name for command in commands if pred(command)]
    assert actual == expected, 'actual: {}'.format(actual)


def test_commands_dict__order():
    """
    Check the ordering of commands_dict.
    """
    names = list(commands_dict)
    # A spot-check is sufficient to check that commands_dict encodes an
    # ordering.
    assert names[0] == 'install'
    assert names[-1] == 'help'


@pytest.mark.parametrize('name', list(commands_dict))
def test_create_command(name):
    """Test creating an instance of each available command."""
    command = create_command(name)
    assert command.name == name
    assert command.summary == commands_dict[name].summary


def test_session_commands():
    """
    Test which commands inherit from SessionCommandMixin.
    """
    def is_session_command(command):
        return isinstance(command, SessionCommandMixin)

    expected = ['download', 'install', 'list', 'search', 'uninstall', 'wheel']
    check_commands(is_session_command, expected)


def test_index_group_commands():
    """
    Test the commands inheriting from IndexGroupCommand.
    """
    def is_index_group_command(command):
        return isinstance(command, IndexGroupCommand)

    check_commands(is_index_group_command, EXPECTED_INDEX_GROUP_COMMANDS)

    # Also check that the commands inheriting from IndexGroupCommand are
    # exactly the commands with the --no-index option.
    def has_option_no_index(command):
        return command.parser.has_option('--no-index')

    check_commands(has_option_no_index, EXPECTED_INDEX_GROUP_COMMANDS)


@pytest.mark.parametrize('command_name', EXPECTED_INDEX_GROUP_COMMANDS)
@pytest.mark.parametrize(
    'disable_pip_version_check, no_index, expected_called',
    [
        # The fetch phase only runs when both disable_pip_version_check
        # and no_index are False.
        (False, False, True),
        (False, True, False),
        (True, False, False),
        (True, True, False),
    ],
)
@patch('pip._internal.cli.req_command._pip_self_version_check_fetch')
def test_index_group_pip_version_check(
    mock_version_check, command_name, disable_pip_version_check, no_index,
    expected_called,
):
    """
    Test whether the pre-body fetch runs when pip_version_check() is
    entered, for each of the IndexGroupCommand classes.
    """
    command = create_command(command_name)
    options = command.parser.get_default_values()
    options.disable_pip_version_check = disable_pip_version_check
    options.no_index = no_index
    # Return None so the emit branch is a no-op.
    mock_version_check.return_value = None

    with command.pip_version_check(options, []):
        pass
    if expected_called:
        mock_version_check.assert_called_once()
    else:
        mock_version_check.assert_not_called()


@pytest.mark.parametrize('command_name', EXPECTED_INDEX_GROUP_COMMANDS)
@patch('pip._internal.cli.req_command._pip_self_version_check_emit')
@patch('pip._internal.cli.req_command._pip_self_version_check_fetch')
def test_index_group_pip_version_check_fetches_before_the_command_body(
    mock_fetch, mock_emit, command_name,
):
    """
    All of the check's work must happen *before* the command body runs.

    This is the security property behind the self-check code injection fix:
    a command that overwrites pip's own files on disk must not be able to get
    that code loaded and executed by a version check running afterwards.
    """
    calls = []

    def record_fetch(session, options):
        calls.append('fetch')
        return ('1.0', '2.0')

    def record_emit(upgrade_prompt):
        calls.append(('emit', upgrade_prompt))

    mock_fetch.side_effect = record_fetch
    mock_emit.side_effect = record_emit

    command = create_command(command_name)
    options = command.parser.get_default_values()
    options.disable_pip_version_check = False
    options.no_index = False

    with command.pip_version_check(options, []):
        calls.append('body')

    assert calls == ['fetch', 'body', ('emit', ('1.0', '2.0'))]


@patch('pip._internal.cli.req_command._pip_self_version_check_emit')
@patch('pip._internal.cli.req_command._pip_self_version_check_fetch')
def test_index_group_pip_version_check_emits_when_body_raises(
    mock_fetch, mock_emit,
):
    """
    The prompt is still emitted when the command body blows up.
    """
    mock_fetch.return_value = ('1.0', '2.0')

    command = create_command('install')
    options = command.parser.get_default_values()
    options.disable_pip_version_check = False
    options.no_index = False

    with pytest.raises(RuntimeError):
        with command.pip_version_check(options, []):
            raise RuntimeError('boom')

    mock_fetch.assert_called_once()
    mock_emit.assert_called_once_with(('1.0', '2.0'))


@patch('pip._internal.cli.req_command._pip_self_version_check_emit')
@patch('pip._internal.cli.req_command._pip_self_version_check_fetch')
def test_index_group_pip_version_check_survives_fetch_failure(
    mock_fetch, mock_emit,
):
    """
    A broken fetch must not stop the command from running.
    """
    mock_fetch.side_effect = ValueError('no network')

    command = create_command('install')
    options = command.parser.get_default_values()
    options.disable_pip_version_check = False
    options.no_index = False

    ran = []
    with command.pip_version_check(options, []):
        ran.append(True)

    assert ran == [True]
    mock_emit.assert_called_once_with(None)


@pytest.mark.parametrize('args, expected_called', [
    (['pip'], False),
    (['PIP'], False),
    (['pip==20.3.4'], False),
    (['pip>=1.0'], False),
    (['pip[extra]'], False),
    (['some-other-pkg', 'pip'], False),
    (['some-other-pkg'], True),
    ([], True),
])
@patch('pip._internal.cli.req_command._pip_self_version_check_fetch')
def test_install_pip_version_check_skipped_when_pip_is_a_requirement(
    mock_version_check, args, expected_called,
):
    """
    ``pip install pip`` must not run the self-version check at all.

    The running pip is about to be replaced on disk, so neither half of the
    check should touch it.
    """
    mock_version_check.return_value = None

    command = create_command('install')
    options = command.parser.get_default_values()
    options.disable_pip_version_check = False
    options.no_index = False

    with command.pip_version_check(options, args):
        pass

    if expected_called:
        mock_version_check.assert_called_once()
    else:
        mock_version_check.assert_not_called()


@pytest.mark.parametrize('arg, expected', [
    ('pip', True),
    ('PIP', True),
    ('pip==20.3.4', True),
    ('pip[extra]>=1', True),
    ('pip ; python_version >= "3"', True),
    ('not-pip', False),
    ('pipx', False),
    ('.', False),
    ('./local/dir', False),
    ('/abs/path/to/pkg', False),
    ('C:\\Users\\User\\pkg', False),
    ('https://example.com/pip-1.0.tar.gz', False),
    ('git+https://example.com/pip.git#egg=pip', False),
    ('', False),
])
def test_arg_refers_to_pip(arg, expected):
    """
    ``_arg_refers_to_pip()`` must recognise pip requirements and must not
    choke on paths, URLs or nonsense.
    """
    assert _arg_refers_to_pip(arg) is expected


def test_requirement_commands():
    """
    Test which commands inherit from RequirementCommand.
    """
    def is_requirement_command(command):
        return isinstance(command, RequirementCommand)

    check_commands(is_requirement_command, ['download', 'install', 'wheel'])
