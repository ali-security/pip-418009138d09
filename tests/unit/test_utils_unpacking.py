import io
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import zipfile

import pytest

from pip._internal.exceptions import InstallationError
from pip._internal.utils.unpacking import is_within_directory, untar_file, unzip_file

# tarfile cannot always materialise a symlink member on Windows: os.symlink
# does not exist there under Python 2 and requires a privilege under Python 3,
# so tarfile silently falls back to copying the link target instead. Only the
# platforms below can be asserted to end up with an actual symlink.
SYMLINKS_SUPPORTED = sys.platform != 'win32'


class TestUnpackArchives(object):
    """
    test_tar.tgz/test_tar.zip have content as follows engineered to confirm 3
    things:
     1) confirm that reg files, dirs, and symlinks get unpacked
     2) permissions are not preserved (and go by the 022 umask)
     3) reg files with *any* execute perms, get chmod +x

       file.txt         600 regular file
       symlink.txt      777 symlink to file.txt
       script_owner.sh  700 script where owner can execute
       script_group.sh  610 script where group can execute
       script_world.sh  601 script where world can execute
       dir              744 directory
       dir/dirfile      622 regular file
     4) the file contents are extracted correctly (though the content of
        each file isn't currently unique)

    """

    def setup(self):
        self.tempdir = tempfile.mkdtemp()
        self.old_mask = os.umask(0o022)
        self.symlink_expected_mode = None

    def teardown(self):
        os.umask(self.old_mask)
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def confirm_files(self):
        # expectations based on 022 umask set above and the unpack logic that
        # sets execute permissions, not preservation
        for fname, expected_mode, test, expected_contents in [
            ('file.txt', 0o644, os.path.isfile, b'file\n'),
            # We don't test the "symlink.txt" contents for now.
            ('symlink.txt', 0o644, os.path.isfile, None),
            ('script_owner.sh', 0o755, os.path.isfile, b'file\n'),
            ('script_group.sh', 0o755, os.path.isfile, b'file\n'),
            ('script_world.sh', 0o755, os.path.isfile, b'file\n'),
            ('dir', 0o755, os.path.isdir, None),
            (os.path.join('dir', 'dirfile'), 0o644, os.path.isfile, b''),
        ]:
            path = os.path.join(self.tempdir, fname)
            if path.endswith('symlink.txt') and sys.platform == 'win32':
                # no symlinks created on windows
                continue
            assert test(path), path
            if expected_contents is not None:
                with open(path, mode='rb') as f:
                    contents = f.read()
                assert contents == expected_contents, 'fname: {}'.format(fname)
            if sys.platform == 'win32':
                # the permissions tests below don't apply in windows
                # due to os.chmod being a noop
                continue
            mode = self.mode(path)
            assert mode == expected_mode, (
                "mode: {}, expected mode: {}".format(mode, expected_mode)
            )

    def make_zip_file(self, filename, file_list):
        """
        Create a zip file for test case
        """
        test_zip = os.path.join(self.tempdir, filename)
        with zipfile.ZipFile(test_zip, 'w') as myzip:
            for item in file_list:
                myzip.writestr(item, 'file content')
        return test_zip

    def make_tar_file(self, filename, file_list):
        """
        Create a tar file for test case
        """
        test_tar = os.path.join(self.tempdir, filename)
        with tarfile.open(test_tar, 'w') as mytar:
            for item in file_list:
                file_tarinfo = tarfile.TarInfo(item)
                mytar.addfile(file_tarinfo, 'file content')
        return test_tar

    def test_unpack_tgz(self, data):
        """
        Test unpacking a *.tgz, and setting execute permissions
        """
        test_file = data.packages.joinpath("test_tar.tgz")
        untar_file(test_file, self.tempdir)
        self.confirm_files()
        # Check the timestamp of an extracted file
        file_txt_path = os.path.join(self.tempdir, 'file.txt')
        mtime = time.gmtime(os.stat(file_txt_path).st_mtime)
        assert mtime[0:6] == (2013, 8, 16, 5, 13, 37), mtime

    def test_unpack_zip(self, data):
        """
        Test unpacking a *.zip, and setting execute permissions
        """
        test_file = data.packages.joinpath("test_zip.zip")
        unzip_file(test_file, self.tempdir)
        self.confirm_files()

    def test_unpack_zip_failure(self):
        """
        Test unpacking a *.zip with file containing .. path
        and expect exception
        """
        files = ['regular_file.txt', os.path.join('..', 'outside_file.txt')]
        test_zip = self.make_zip_file('test_zip.zip', files)
        with pytest.raises(InstallationError) as e:
            unzip_file(test_zip, self.tempdir)
        assert 'trying to install outside target directory' in str(e.value)

    def test_unpack_zip_success(self):
        """
        Test unpacking a *.zip with regular files,
        no file will be installed outside target directory after unpack
        so no exception raised
        """
        files = [
            'regular_file1.txt',
            os.path.join('dir', 'dir_file1.txt'),
            os.path.join('dir', '..', 'dir_file2.txt'),
        ]
        test_zip = self.make_zip_file('test_zip.zip', files)
        unzip_file(test_zip, self.tempdir)

    def test_unpack_tar_failure(self):
        """
        Test unpacking a *.tar with file containing .. path
        and expect exception
        """
        files = ['regular_file.txt', os.path.join('..', 'outside_file.txt')]
        test_tar = self.make_tar_file('test_tar.tar', files)
        with pytest.raises(InstallationError) as e:
            untar_file(test_tar, self.tempdir)
        assert 'trying to install outside target directory' in str(e.value)

    def test_unpack_tar_success(self):
        """
        Test unpacking a *.tar with regular files,
        no file will be installed outside target directory after unpack
        so no exception raised
        """
        files = [
            'regular_file1.txt',
            os.path.join('dir', 'dir_file1.txt'),
            os.path.join('dir', '..', 'dir_file2.txt'),
        ]
        test_tar = self.make_tar_file('test_tar.tar', files)
        untar_file(test_tar, self.tempdir)

    def test_unpack_normal_tar_link1_no_data_filter(self, monkeypatch):
        """
        Test unpacking a normal tar with file containing soft links, but no
        data_filter
        """
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filename = 'test_tar_links_no_data_filter.tar'
        tar_filepath = os.path.join(self.tempdir, tar_filename)

        extract_path = os.path.join(self.tempdir, 'extract_path')

        with tarfile.open(tar_filepath, 'w') as tar:
            file_content = b'normal\n'
            file_data = io.BytesIO(file_content)
            normal_file_tarinfo = tarfile.TarInfo(name='normal_file')
            normal_file_tarinfo.size = len(file_content)
            tar.addfile(normal_file_tarinfo, fileobj=file_data)

            info = tarfile.TarInfo('normal_symlink')
            info.type = tarfile.SYMTYPE
            info.linkname = 'normal_file'
            tar.addfile(info)

        # A symlink whose target is itself a member of the archive is legit
        # and must not be rejected.
        untar_file(tar_filepath, extract_path)

        symlink_path = os.path.join(extract_path, 'normal_symlink')
        if SYMLINKS_SUPPORTED:
            assert os.path.islink(symlink_path)
            assert os.readlink(symlink_path) == 'normal_file'

            with open(symlink_path, 'rb') as f:
                assert f.read() == b'normal\n'

    def test_unpack_normal_tar_link2_no_data_filter(self, monkeypatch):
        """
        Test unpacking a normal tar with file containing soft links, but no
        data_filter
        """
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filename = 'test_tar_links_no_data_filter.tar'
        tar_filepath = os.path.join(self.tempdir, tar_filename)

        extract_path = os.path.join(self.tempdir, 'extract_path')

        link_path = '..' + os.sep + 'normal_file'

        with tarfile.open(tar_filepath, 'w') as tar:
            file_content = b'normal\n'
            file_data = io.BytesIO(file_content)
            normal_file_tarinfo = tarfile.TarInfo(name='normal_file')
            normal_file_tarinfo.size = len(file_content)
            tar.addfile(normal_file_tarinfo, fileobj=file_data)

            info = tarfile.TarInfo('sub/normal_symlink')
            info.type = tarfile.SYMTYPE
            info.linkname = link_path
            tar.addfile(info)

        # The link points up out of its own directory but stays inside the
        # destination, so it must not be rejected.
        untar_file(tar_filepath, extract_path)

        symlink_path = os.path.join(extract_path, 'sub', 'normal_symlink')
        if SYMLINKS_SUPPORTED:
            assert os.path.islink(symlink_path)
            assert os.readlink(symlink_path) == link_path

            with open(symlink_path, 'rb') as f:
                assert f.read() == b'normal\n'

    def test_unpack_evil_tar_link1_no_data_filter(self, monkeypatch):
        """
        Test unpacking an evil tar with file containing soft links, but no
        data_filter
        """
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filename = 'test_tar_links_no_data_filter.tar'
        tar_filepath = os.path.join(self.tempdir, tar_filename)

        import_filename = 'import_file'
        import_filepath = os.path.join(self.tempdir, import_filename)
        open(import_filepath, 'w').close()

        extract_path = os.path.join(self.tempdir, 'extract_path')

        with tarfile.open(tar_filepath, 'w') as tar:
            info = tarfile.TarInfo('evil_symlink')
            info.type = tarfile.SYMTYPE
            info.linkname = import_filepath
            tar.addfile(info)

        with pytest.raises(InstallationError) as e:
            untar_file(tar_filepath, extract_path)

        msg = (
            'The tar file ({}) has a file ({}) trying to install outside '
            'target directory ({})'
        )
        assert msg.format(
            tar_filepath, 'evil_symlink', import_filepath
        ) in str(e.value)

        assert not os.path.exists(os.path.join(extract_path, 'evil_symlink'))

    def test_unpack_evil_tar_link2_no_data_filter(self, monkeypatch):
        """
        Test unpacking an evil tar with file containing soft links, but no
        data_filter
        """
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filename = 'test_tar_links_no_data_filter.tar'
        tar_filepath = os.path.join(self.tempdir, tar_filename)

        import_filename = 'import_file'
        import_filepath = os.path.join(self.tempdir, import_filename)
        open(import_filepath, 'w').close()

        extract_path = os.path.join(self.tempdir, 'extract_path')

        link_path = '..' + os.sep + import_filename

        with tarfile.open(tar_filepath, 'w') as tar:
            info = tarfile.TarInfo('evil_symlink')
            info.type = tarfile.SYMTYPE
            info.linkname = link_path
            tar.addfile(info)

        with pytest.raises(InstallationError) as e:
            untar_file(tar_filepath, extract_path)

        msg = (
            'The tar file ({}) has a file ({}) trying to install outside '
            'target directory ({})'
        )
        assert msg.format(
            tar_filepath, 'evil_symlink', link_path
        ) in str(e.value)

        assert not os.path.exists(os.path.join(extract_path, 'evil_symlink'))

    def test_unpack_tar_symlink_then_member_no_data_filter(self, monkeypatch):
        """Reject a symlink to outside before a member is written through it"""
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filepath = os.path.join(self.tempdir, 'symlink_then_member.tar')
        extract_path = os.path.join(self.tempdir, 'extract_path')
        outside_path = os.path.join(self.tempdir, 'outside.txt')

        with tarfile.open(tar_filepath, 'w') as tar:
            info = tarfile.TarInfo('outside_link')
            info.type = tarfile.SYMTYPE
            info.linkname = '..'
            tar.addfile(info)

            file_content = b'data\n'
            data = io.BytesIO(file_content)
            info = tarfile.TarInfo('outside_link/outside.txt')
            info.size = len(file_content)
            tar.addfile(info, fileobj=data)

            # Present so that the link target counts as a member of the tar.
            info = tarfile.TarInfo('..')
            info.type = tarfile.DIRTYPE
            tar.addfile(info)

        with pytest.raises(InstallationError) as e:
            untar_file(tar_filepath, extract_path)

        assert 'trying to install outside target directory' in str(e.value)

        assert not os.path.exists(outside_path)
        assert not os.path.exists(os.path.join(extract_path, 'outside_link'))

    @pytest.mark.skipif(
        not SYMLINKS_SUPPORTED,
        reason='tarfile does not create symlinks on Windows and realpath '
               'does not resolve them there, so the traversal is unreachable',
    )
    def test_unpack_tar_nested_symlink_traversal_no_data_filter(
        self, monkeypatch
    ):
        """Reject a member that escapes through a chain of in-bounds symlinks"""
        if hasattr(tarfile, 'data_filter'):
            monkeypatch.delattr('tarfile.data_filter')

        tar_filepath = os.path.join(self.tempdir, 'nested_traversal.tar')
        extract_path = os.path.join(self.tempdir, 'extract_path')
        outside_path = os.path.join(self.tempdir, 'outside.txt')

        with tarfile.open(tar_filepath, 'w') as tar:
            info = tarfile.TarInfo('.')
            info.type = tarfile.DIRTYPE
            tar.addfile(info)

            info = tarfile.TarInfo('redir')
            info.type = tarfile.SYMTYPE
            info.linkname = '.'
            tar.addfile(info)

            info = tarfile.TarInfo('redir/up')
            info.type = tarfile.SYMTYPE
            info.linkname = '..'
            tar.addfile(info)

            file_content = b'data\n'
            data = io.BytesIO(file_content)
            info = tarfile.TarInfo('redir/up/outside.txt')
            info.size = len(file_content)
            tar.addfile(info, fileobj=data)

        with pytest.raises(InstallationError) as e:
            untar_file(tar_filepath, extract_path)

        assert 'trying to install outside target directory' in str(e.value)

        assert not os.path.exists(outside_path)


@pytest.mark.parametrize('args, expected', [
    # Test the second containing the first.
    (('parent/sub', 'parent/'), False),
    # Test the first not ending in a trailing slash.
    (('parent', 'parent/foo'), True),
    # Test target containing `..` but still inside the parent.
    (('parent/', 'parent/foo/../bar'), True),
    # Test target within the parent
    (('parent/', 'parent/sub'), True),
    # Test target outside parent
    (('parent/', 'parent/../sub'), False),
    # Test a sibling whose name merely starts with the parent's name: it is
    # outside the parent, and only a separator-aware comparison catches it.
    (('parent', 'parent-evil'), False),
])
def test_is_within_directory(args, expected):
    result = is_within_directory(*args)
    assert result == expected
