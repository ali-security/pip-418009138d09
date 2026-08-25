# The following comment should be removed at some point in the future.
# mypy: disallow-untyped-defs=False

import os
import shutil
import subprocess
import sys
from glob import glob

VIRTUAL_ENV = os.environ['VIRTUAL_ENV']
TOX_PIP_DIR = os.path.join(VIRTUAL_ENV, 'pip')


def pip(args):
    # First things first, get a recent (stable) version of pip.
    if not os.path.exists(TOX_PIP_DIR):
        # Seal: bound to pip<21 -- a modern pip rejects the
        # `--use-feature=2020-resolver` line that
        # tools/requirements/tests.txt opens with, so no test env can be
        # built.  This cannot be expressed as a constraint instead: tox
        # installs pip from the local sdist *path*, and pip errors with
        # "installation from path or url cannot be constrained to a version".
        # Seal: this one install goes to real PyPI, not to the
        # PIP_INDEX_URL time-machine proxy the rest of the build uses.
        # The bootstrap pip is CI tooling, not a dependency under test, so
        # it is out of scope for the release-date pin -- and the ancient
        # ensurepip pip inside a `python -m venv` env (the Python 3.5 legs)
        # cannot resolve anything from that proxy URL: it finds zero
        # candidates ("from versions: ") and swallows the underlying fetch
        # error at DEBUG level, so the job fails with no diagnostic.
        # `pip<21` resolves to 20.3.4 on 2.7 and 3.5-3.9 alike, so pinning
        # the index here stays deterministic and era-correct.
        subprocess.check_call([sys.executable, '-m', 'pip',
                               '--disable-pip-version-check',
                               'install', '-t', TOX_PIP_DIR,
                               '--index-url', 'https://pypi.org/simple/',
                               'pip<21'])
        shutil.rmtree(glob(os.path.join(TOX_PIP_DIR, 'pip-*.dist-info'))[0])
    # And use that version.
    pypath = os.environ.get('PYTHONPATH')
    pypath = pypath.split(os.pathsep) if pypath is not None else []
    pypath.insert(0, TOX_PIP_DIR)
    os.environ['PYTHONPATH'] = os.pathsep.join(pypath)
    subprocess.check_call([sys.executable, '-m', 'pip'] + args)


if __name__ == '__main__':
    pip(sys.argv[1:])
