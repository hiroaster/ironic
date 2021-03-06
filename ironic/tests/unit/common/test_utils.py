# Copyright 2011 Justin Santa Barbara
# Copyright 2012 Hewlett-Packard Development Company, L.P.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import errno
import hashlib
import os
import os.path
import shutil
import tempfile

import mock
from oslo_concurrency import processutils
from oslo_config import cfg
import six

from ironic.common import exception
from ironic.common import utils
from ironic.tests import base

CONF = cfg.CONF


class BareMetalUtilsTestCase(base.TestCase):

    def test_create_link(self):
        with mock.patch.object(os, "symlink", autospec=True) as symlink_mock:
            symlink_mock.return_value = None
            utils.create_link_without_raise("/fake/source", "/fake/link")
            symlink_mock.assert_called_once_with("/fake/source", "/fake/link")

    def test_create_link_EEXIST(self):
        with mock.patch.object(os, "symlink", autospec=True) as symlink_mock:
            symlink_mock.side_effect = OSError(errno.EEXIST)
            utils.create_link_without_raise("/fake/source", "/fake/link")
            symlink_mock.assert_called_once_with("/fake/source", "/fake/link")


class ExecuteTestCase(base.TestCase):

    def test_retry_on_failure(self):
        fd, tmpfilename = tempfile.mkstemp()
        _, tmpfilename2 = tempfile.mkstemp()
        try:
            fp = os.fdopen(fd, 'w+')
            fp.write('''#!/bin/sh
# If stdin fails to get passed during one of the runs, make a note.
if ! grep -q foo
then
    echo 'failure' > "$1"
fi
# If stdin has failed to get passed during this or a previous run, exit early.
if grep failure "$1"
then
    exit 1
fi
runs="$(cat $1)"
if [ -z "$runs" ]
then
    runs=0
fi
runs=$(($runs + 1))
echo $runs > "$1"
exit 1
''')
            fp.close()
            os.chmod(tmpfilename, 0o755)
            try:
                self.assertRaises(processutils.ProcessExecutionError,
                                  utils.execute,
                                  tmpfilename, tmpfilename2, attempts=10,
                                  process_input=b'foo',
                                  delay_on_retry=False)
            except OSError as e:
                if e.errno == errno.EACCES:
                    self.skipTest("Permissions error detected. "
                                  "Are you running with a noexec /tmp?")
                else:
                    raise
            fp = open(tmpfilename2, 'r')
            runs = fp.read()
            fp.close()
            self.assertNotEqual(runs.strip(), 'failure', 'stdin did not '
                                'always get passed '
                                'correctly')
            runs = int(runs.strip())
            self.assertEqual(10, runs,
                             'Ran %d times instead of 10.' % (runs,))
        finally:
            os.unlink(tmpfilename)
            os.unlink(tmpfilename2)

    def test_unknown_kwargs_raises_error(self):
        self.assertRaises(processutils.UnknownArgumentError,
                          utils.execute,
                          '/usr/bin/env', 'true',
                          this_is_not_a_valid_kwarg=True)

    def test_check_exit_code_boolean(self):
        utils.execute('/usr/bin/env', 'false', check_exit_code=False)
        self.assertRaises(processutils.ProcessExecutionError,
                          utils.execute,
                          '/usr/bin/env', 'false', check_exit_code=True)

    def test_no_retry_on_success(self):
        fd, tmpfilename = tempfile.mkstemp()
        _, tmpfilename2 = tempfile.mkstemp()
        try:
            fp = os.fdopen(fd, 'w+')
            fp.write('''#!/bin/sh
# If we've already run, bail out.
grep -q foo "$1" && exit 1
# Mark that we've run before.
echo foo > "$1"
# Check that stdin gets passed correctly.
grep foo
''')
            fp.close()
            os.chmod(tmpfilename, 0o755)
            try:
                utils.execute(tmpfilename,
                              tmpfilename2,
                              process_input=b'foo',
                              attempts=2)
            except OSError as e:
                if e.errno == errno.EACCES:
                    self.skipTest("Permissions error detected. "
                                  "Are you running with a noexec /tmp?")
                else:
                    raise
        finally:
            os.unlink(tmpfilename)
            os.unlink(tmpfilename2)

    @mock.patch.object(processutils, 'execute', autospec=True)
    @mock.patch.object(os.environ, 'copy', return_value={}, autospec=True)
    def test_execute_use_standard_locale_no_env_variables(self, env_mock,
                                                          execute_mock):
        utils.execute('foo', use_standard_locale=True)
        execute_mock.assert_called_once_with('foo',
                                             env_variables={'LC_ALL': 'C'})

    @mock.patch.object(processutils, 'execute', autospec=True)
    def test_execute_use_standard_locale_with_env_variables(self,
                                                            execute_mock):
        utils.execute('foo', use_standard_locale=True,
                      env_variables={'foo': 'bar'})
        execute_mock.assert_called_once_with('foo',
                                             env_variables={'LC_ALL': 'C',
                                                            'foo': 'bar'})

    @mock.patch.object(processutils, 'execute', autospec=True)
    def test_execute_not_use_standard_locale(self, execute_mock):
        utils.execute('foo', use_standard_locale=False,
                      env_variables={'foo': 'bar'})
        execute_mock.assert_called_once_with('foo',
                                             env_variables={'foo': 'bar'})

    def test_execute_get_root_helper(self):
        with mock.patch.object(
                processutils, 'execute', autospec=True) as execute_mock:
            helper = utils._get_root_helper()
            utils.execute('foo', run_as_root=True)
            execute_mock.assert_called_once_with('foo', run_as_root=True,
                                                 root_helper=helper)

    def test_execute_without_root_helper(self):
        with mock.patch.object(
                processutils, 'execute', autospec=True) as execute_mock:
            utils.execute('foo', run_as_root=False)
            execute_mock.assert_called_once_with('foo', run_as_root=False)


class GenericUtilsTestCase(base.TestCase):
    @mock.patch.object(utils, 'hashlib', autospec=True)
    def test__get_hash_object(self, hashlib_mock):
        algorithms_available = ('md5', 'sha1', 'sha224',
                                'sha256', 'sha384', 'sha512')
        hashlib_mock.algorithms_guaranteed = algorithms_available
        hashlib_mock.algorithms = algorithms_available
        # | WHEN |
        utils._get_hash_object('md5')
        utils._get_hash_object('sha1')
        utils._get_hash_object('sha224')
        utils._get_hash_object('sha256')
        utils._get_hash_object('sha384')
        utils._get_hash_object('sha512')
        # | THEN |
        calls = [mock.call.md5(), mock.call.sha1(), mock.call.sha224(),
                 mock.call.sha256(), mock.call.sha384(), mock.call.sha512()]
        hashlib_mock.assert_has_calls(calls)

    def test__get_hash_object_throws_for_invalid_or_unsupported_hash_name(
            self):
        # | WHEN | & | THEN |
        self.assertRaises(exception.InvalidParameterValue,
                          utils._get_hash_object,
                          'hickory-dickory-dock')

    def test_hash_file_for_md5(self):
        # | GIVEN |
        data = b'Mary had a little lamb, its fleece as white as snow'
        file_like_object = six.BytesIO(data)
        expected = hashlib.md5(data).hexdigest()
        # | WHEN |
        actual = utils.hash_file(file_like_object)  # using default, 'md5'
        # | THEN |
        self.assertEqual(expected, actual)

    def test_hash_file_for_sha1(self):
        # | GIVEN |
        data = b'Mary had a little lamb, its fleece as white as snow'
        file_like_object = six.BytesIO(data)
        expected = hashlib.sha1(data).hexdigest()
        # | WHEN |
        actual = utils.hash_file(file_like_object, 'sha1')
        # | THEN |
        self.assertEqual(expected, actual)

    def test_hash_file_for_sha512(self):
        # | GIVEN |
        data = b'Mary had a little lamb, its fleece as white as snow'
        file_like_object = six.BytesIO(data)
        expected = hashlib.sha512(data).hexdigest()
        # | WHEN |
        actual = utils.hash_file(file_like_object, 'sha512')
        # | THEN |
        self.assertEqual(expected, actual)

    def test_hash_file_throws_for_invalid_or_unsupported_hash(self):
        # | GIVEN |
        data = b'Mary had a little lamb, its fleece as white as snow'
        file_like_object = six.BytesIO(data)
        # | WHEN | & | THEN |
        self.assertRaises(exception.InvalidParameterValue, utils.hash_file,
                          file_like_object, 'hickory-dickory-dock')

    def test_is_valid_mac(self):
        self.assertTrue(utils.is_valid_mac("52:54:00:cf:2d:31"))
        self.assertTrue(utils.is_valid_mac(u"52:54:00:cf:2d:31"))
        self.assertFalse(utils.is_valid_mac("127.0.0.1"))
        self.assertFalse(utils.is_valid_mac("not:a:mac:address"))
        self.assertFalse(utils.is_valid_mac("52-54-00-cf-2d-31"))
        self.assertFalse(utils.is_valid_mac("aa bb cc dd ee ff"))
        self.assertTrue(utils.is_valid_mac("AA:BB:CC:DD:EE:FF"))
        self.assertFalse(utils.is_valid_mac("AA BB CC DD EE FF"))
        self.assertFalse(utils.is_valid_mac("AA-BB-CC-DD-EE-FF"))

    def test_is_valid_datapath_id(self):
        self.assertTrue(utils.is_valid_datapath_id("525400cf2d319fdf"))
        self.assertTrue(utils.is_valid_datapath_id("525400CF2D319FDF"))
        self.assertFalse(utils.is_valid_datapath_id("52"))
        self.assertFalse(utils.is_valid_datapath_id("52:54:00:cf:2d:31"))
        self.assertFalse(utils.is_valid_datapath_id("notadatapathid00"))
        self.assertFalse(utils.is_valid_datapath_id("5525400CF2D319FDF"))

    def test_is_hostname_safe(self):
        self.assertTrue(utils.is_hostname_safe('spam'))
        self.assertFalse(utils.is_hostname_safe('spAm'))
        self.assertFalse(utils.is_hostname_safe('SPAM'))
        self.assertFalse(utils.is_hostname_safe('-spam'))
        self.assertFalse(utils.is_hostname_safe('spam-'))
        self.assertTrue(utils.is_hostname_safe('spam-eggs'))
        self.assertFalse(utils.is_hostname_safe('spam_eggs'))
        self.assertFalse(utils.is_hostname_safe('spam eggs'))
        self.assertTrue(utils.is_hostname_safe('spam.eggs'))
        self.assertTrue(utils.is_hostname_safe('9spam'))
        self.assertTrue(utils.is_hostname_safe('spam7'))
        self.assertTrue(utils.is_hostname_safe('br34kf4st'))
        self.assertFalse(utils.is_hostname_safe('$pam'))
        self.assertFalse(utils.is_hostname_safe('egg$'))
        self.assertFalse(utils.is_hostname_safe('spam#eggs'))
        self.assertFalse(utils.is_hostname_safe(' eggs'))
        self.assertFalse(utils.is_hostname_safe('spam '))
        self.assertTrue(utils.is_hostname_safe('s'))
        self.assertTrue(utils.is_hostname_safe('s' * 63))
        self.assertFalse(utils.is_hostname_safe('s' * 64))
        self.assertFalse(utils.is_hostname_safe(''))
        self.assertFalse(utils.is_hostname_safe(None))
        # Need to ensure a binary response for success or fail
        self.assertIsNotNone(utils.is_hostname_safe('spam'))
        self.assertIsNotNone(utils.is_hostname_safe('-spam'))
        self.assertTrue(utils.is_hostname_safe('www.rackspace.com'))
        self.assertTrue(utils.is_hostname_safe('www.rackspace.com.'))
        self.assertTrue(utils.is_hostname_safe('http._sctp.www.example.com'))
        self.assertTrue(utils.is_hostname_safe('mail.pets_r_us.net'))
        self.assertTrue(utils.is_hostname_safe('mail-server-15.my_host.org'))
        self.assertFalse(utils.is_hostname_safe('www.nothere.com_'))
        self.assertFalse(utils.is_hostname_safe('www.nothere_.com'))
        self.assertFalse(utils.is_hostname_safe('www..nothere.com'))
        long_str = 'a' * 63 + '.' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 63
        self.assertTrue(utils.is_hostname_safe(long_str))
        self.assertFalse(utils.is_hostname_safe(long_str + '.'))
        self.assertFalse(utils.is_hostname_safe('a' * 255))

    def test_is_valid_logical_name(self):
        valid = (
            'spam', 'spAm', 'SPAM', 'spam-eggs', 'spam.eggs', 'spam_eggs',
            'spam~eggs', '9spam', 'spam7', '~spam', '.spam', '.~-_', '~',
            'br34kf4st', 's', 's' * 63, 's' * 255)
        invalid = (
            ' ', 'spam eggs', '$pam', 'egg$', 'spam#eggs',
            ' eggs', 'spam ', '', None, 'spam%20')

        for hostname in valid:
            result = utils.is_valid_logical_name(hostname)
            # Need to ensure a binary response for success. assertTrue
            # is too generous, and would pass this test if, for
            # instance, a regex Match object were returned.
            self.assertIs(result, True,
                          "%s is unexpectedly invalid" % hostname)

        for hostname in invalid:
            result = utils.is_valid_logical_name(hostname)
            # Need to ensure a binary response for
            # success. assertFalse is too generous and would pass this
            # test if None were returned.
            self.assertIs(result, False,
                          "%s is unexpectedly valid" % hostname)

    def test_validate_and_normalize_mac(self):
        mac = 'AA:BB:CC:DD:EE:FF'
        with mock.patch.object(utils, 'is_valid_mac', autospec=True) as m_mock:
            m_mock.return_value = True
            self.assertEqual(mac.lower(),
                             utils.validate_and_normalize_mac(mac))

    def test_validate_and_normalize_datapath_id(self):
        datapath_id = 'AA:BB:CC:DD:EE:FF'
        with mock.patch.object(utils, 'is_valid_datapath_id',
                               autospec=True) as m_mock:
            m_mock.return_value = True
            self.assertEqual(datapath_id.lower(),
                             utils.validate_and_normalize_datapath_id(
                                 datapath_id))

    def test_validate_and_normalize_mac_invalid_format(self):
        with mock.patch.object(utils, 'is_valid_mac', autospec=True) as m_mock:
            m_mock.return_value = False
            self.assertRaises(exception.InvalidMAC,
                              utils.validate_and_normalize_mac, 'invalid-mac')

    def test_safe_rstrip(self):
        value = '/test/'
        rstripped_value = '/test'
        not_rstripped = '/'

        self.assertEqual(rstripped_value, utils.safe_rstrip(value, '/'))
        self.assertEqual(not_rstripped, utils.safe_rstrip(not_rstripped, '/'))

    def test_safe_rstrip_not_raises_exceptions(self):
        # Supplying an integer should normally raise an exception because it
        # does not save the rstrip() method.
        value = 10

        # In the case of raising an exception safe_rstrip() should return the
        # original value.
        self.assertEqual(value, utils.safe_rstrip(value))

    @mock.patch.object(os.path, 'getmtime', return_value=1439465889.4964755,
                       autospec=True)
    def test_unix_file_modification_datetime(self, mtime_mock):
        expected = datetime.datetime(2015, 8, 13, 11, 38, 9, 496475)
        self.assertEqual(expected,
                         utils.unix_file_modification_datetime('foo'))
        mtime_mock.assert_called_once_with('foo')

    def test_is_valid_no_proxy(self):

        # Valid values for 'no_proxy'
        valid_no_proxy = [
            ('a' * 63 + '.' + '0' * 63 + '.c.' + 'd' * 61 + '.' + 'e' * 61),
            ('A' * 63 + '.' + '0' * 63 + '.C.' + 'D' * 61 + '.' + 'E' * 61),
            ('.' + 'a' * 62 + '.' + '0' * 62 + '.c.' + 'd' * 61 + '.' +
             'e' * 61),
            ',,example.com:3128,',
            '192.168.1.1',  # IP should be valid
        ]
        # Test each one individually, so if failure easier to determine which
        # one failed.
        for no_proxy in valid_no_proxy:
            self.assertTrue(
                utils.is_valid_no_proxy(no_proxy),
                msg="'no_proxy' value should be valid: {}".format(no_proxy))
        # Test valid when joined together
        self.assertTrue(utils.is_valid_no_proxy(','.join(valid_no_proxy)))
        # Test valid when joined together with whitespace
        self.assertTrue(utils.is_valid_no_proxy(' , '.join(valid_no_proxy)))
        # empty string should also be valid
        self.assertTrue(utils.is_valid_no_proxy(''))

        # Invalid values for 'no_proxy'
        invalid_no_proxy = [
            ('A' * 64 + '.' + '0' * 63 + '.C.' + 'D' * 61 + '.' +
             'E' * 61),  # too long (> 253)
            ('a' * 100),
            'a..com',
            ('.' + 'a' * 63 + '.' + '0' * 62 + '.c.' + 'd' * 61 + '.' +
             'e' * 61),  # too long (> 251 after deleting .)
            ('*.' + 'a' * 60 + '.' + '0' * 60 + '.c.' + 'd' * 61 + '.' +
             'e' * 61),  # starts with *.
            'c.-a.com',
            'c.a-.com',
        ]

        for no_proxy in invalid_no_proxy:
            self.assertFalse(
                utils.is_valid_no_proxy(no_proxy),
                msg="'no_proxy' value should be invalid: {}".format(no_proxy))


class TempFilesTestCase(base.TestCase):

    def test_tempdir(self):

        dirname = None
        with utils.tempdir() as tempdir:
            self.assertTrue(os.path.isdir(tempdir))
            dirname = tempdir
        self.assertFalse(os.path.exists(dirname))

    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(tempfile, 'mkdtemp', autospec=True)
    def test_tempdir_mocked(self, mkdtemp_mock, rmtree_mock):

        self.config(tempdir='abc')
        mkdtemp_mock.return_value = 'temp-dir'
        kwargs = {'dir': 'b'}

        with utils.tempdir(**kwargs) as tempdir:
            self.assertEqual('temp-dir', tempdir)
            tempdir_created = tempdir

        mkdtemp_mock.assert_called_once_with(**kwargs)
        rmtree_mock.assert_called_once_with(tempdir_created)

    @mock.patch.object(utils, 'LOG', autospec=True)
    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(tempfile, 'mkdtemp', autospec=True)
    def test_tempdir_mocked_error_on_rmtree(self, mkdtemp_mock, rmtree_mock,
                                            log_mock):

        self.config(tempdir='abc')
        mkdtemp_mock.return_value = 'temp-dir'
        rmtree_mock.side_effect = OSError

        with utils.tempdir() as tempdir:
            self.assertEqual('temp-dir', tempdir)
            tempdir_created = tempdir

        rmtree_mock.assert_called_once_with(tempdir_created)
        self.assertTrue(log_mock.error.called)

    @mock.patch.object(os.path, 'exists', autospec=True)
    @mock.patch.object(utils, '_check_dir_writable', autospec=True)
    @mock.patch.object(utils, '_check_dir_free_space', autospec=True)
    def test_check_dir_with_pass_in(self, mock_free_space, mock_dir_writable,
                                    mock_exists):
        mock_exists.return_value = True
        # test passing in a directory and size
        utils.check_dir(directory_to_check='/fake/path', required_space=5)
        mock_exists.assert_called_once_with('/fake/path')
        mock_dir_writable.assert_called_once_with('/fake/path')
        mock_free_space.assert_called_once_with('/fake/path', 5)

    @mock.patch.object(os.path, 'exists', autospec=True)
    @mock.patch.object(utils, '_check_dir_writable', autospec=True)
    @mock.patch.object(utils, '_check_dir_free_space', autospec=True)
    def test_check_dir_no_dir(self, mock_free_space, mock_dir_writable,
                              mock_exists):
        mock_exists.return_value = False
        self.config(tempdir='/fake/path')
        self.assertRaises(exception.PathNotFound, utils.check_dir)
        mock_exists.assert_called_once_with(CONF.tempdir)
        self.assertFalse(mock_free_space.called)
        self.assertFalse(mock_dir_writable.called)

    @mock.patch.object(os.path, 'exists', autospec=True)
    @mock.patch.object(utils, '_check_dir_writable', autospec=True)
    @mock.patch.object(utils, '_check_dir_free_space', autospec=True)
    def test_check_dir_ok(self, mock_free_space, mock_dir_writable,
                          mock_exists):
        mock_exists.return_value = True
        self.config(tempdir='/fake/path')
        utils.check_dir()
        mock_exists.assert_called_once_with(CONF.tempdir)
        mock_dir_writable.assert_called_once_with(CONF.tempdir)
        mock_free_space.assert_called_once_with(CONF.tempdir, 1)

    @mock.patch.object(os, 'access', autospec=True)
    def test__check_dir_writable_ok(self, mock_access):
        mock_access.return_value = True
        self.assertIsNone(utils._check_dir_writable("/fake/path"))
        mock_access.assert_called_once_with("/fake/path", os.W_OK)

    @mock.patch.object(os, 'access', autospec=True)
    def test__check_dir_writable_not_writable(self, mock_access):
        mock_access.return_value = False

        self.assertRaises(exception.DirectoryNotWritable,
                          utils._check_dir_writable, "/fake/path")
        mock_access.assert_called_once_with("/fake/path", os.W_OK)

    @mock.patch.object(os, 'statvfs', autospec=True)
    def test__check_dir_free_space_ok(self, mock_stat):
        statvfs_mock_return = mock.MagicMock()
        statvfs_mock_return.f_bsize = 5
        statvfs_mock_return.f_frsize = 0
        statvfs_mock_return.f_blocks = 0
        statvfs_mock_return.f_bfree = 0
        statvfs_mock_return.f_bavail = 1024 * 1024
        statvfs_mock_return.f_files = 0
        statvfs_mock_return.f_ffree = 0
        statvfs_mock_return.f_favail = 0
        statvfs_mock_return.f_flag = 0
        statvfs_mock_return.f_namemax = 0
        mock_stat.return_value = statvfs_mock_return
        utils._check_dir_free_space("/fake/path")
        mock_stat.assert_called_once_with("/fake/path")

    @mock.patch.object(os, 'statvfs', autospec=True)
    def test_check_dir_free_space_raises(self, mock_stat):
        statvfs_mock_return = mock.MagicMock()
        statvfs_mock_return.f_bsize = 1
        statvfs_mock_return.f_frsize = 0
        statvfs_mock_return.f_blocks = 0
        statvfs_mock_return.f_bfree = 0
        statvfs_mock_return.f_bavail = 1024
        statvfs_mock_return.f_files = 0
        statvfs_mock_return.f_ffree = 0
        statvfs_mock_return.f_favail = 0
        statvfs_mock_return.f_flag = 0
        statvfs_mock_return.f_namemax = 0
        mock_stat.return_value = statvfs_mock_return

        self.assertRaises(exception.InsufficientDiskSpace,
                          utils._check_dir_free_space, "/fake/path")
        mock_stat.assert_called_once_with("/fake/path")


class GetUpdatedCapabilitiesTestCase(base.TestCase):

    def test_get_updated_capabilities(self):
        capabilities = {'ilo_firmware_version': 'xyz'}
        cap_string = 'ilo_firmware_version:xyz'
        cap_returned = utils.get_updated_capabilities(None, capabilities)
        self.assertEqual(cap_string, cap_returned)
        self.assertIsInstance(cap_returned, str)

    def test_get_updated_capabilities_multiple_keys(self):
        capabilities = {'ilo_firmware_version': 'xyz',
                        'foo': 'bar', 'somekey': 'value'}
        cap_string = 'ilo_firmware_version:xyz,foo:bar,somekey:value'
        cap_returned = utils.get_updated_capabilities(None, capabilities)
        set1 = set(cap_string.split(','))
        set2 = set(cap_returned.split(','))
        self.assertEqual(set1, set2)
        self.assertIsInstance(cap_returned, str)

    def test_get_updated_capabilities_invalid_capabilities(self):
        capabilities = 'ilo_firmware_version'
        self.assertRaises(ValueError,
                          utils.get_updated_capabilities,
                          capabilities, {})

    def test_get_updated_capabilities_capabilities_not_dict(self):
        capabilities = ['ilo_firmware_version:xyz', 'foo:bar']
        self.assertRaises(ValueError,
                          utils.get_updated_capabilities,
                          None, capabilities)

    def test_get_updated_capabilities_add_to_existing_capabilities(self):
        new_capabilities = {'BootMode': 'uefi'}
        expected_capabilities = 'BootMode:uefi,foo:bar'
        cap_returned = utils.get_updated_capabilities('foo:bar',
                                                      new_capabilities)
        set1 = set(expected_capabilities.split(','))
        set2 = set(cap_returned.split(','))
        self.assertEqual(set1, set2)
        self.assertIsInstance(cap_returned, str)

    def test_get_updated_capabilities_replace_to_existing_capabilities(self):
        new_capabilities = {'BootMode': 'bios'}
        expected_capabilities = 'BootMode:bios'
        cap_returned = utils.get_updated_capabilities('BootMode:uefi',
                                                      new_capabilities)
        set1 = set(expected_capabilities.split(','))
        set2 = set(cap_returned.split(','))
        self.assertEqual(set1, set2)
        self.assertIsInstance(cap_returned, str)

    def test_validate_network_port(self):
        port = utils.validate_network_port('1', 'message')
        self.assertEqual(1, port)
        port = utils.validate_network_port('65535')
        self.assertEqual(65535, port)

    def test_validate_network_port_fail(self):
        self.assertRaisesRegex(exception.InvalidParameterValue,
                               'Port "65536" is out of range.',
                               utils.validate_network_port,
                               '65536')
        self.assertRaisesRegex(exception.InvalidParameterValue,
                               'fake_port "-1" is out of range.',
                               utils.validate_network_port,
                               '-1',
                               'fake_port')
        self.assertRaisesRegex(exception.InvalidParameterValue,
                               'Port "invalid" is not a valid integer.',
                               utils.validate_network_port,
                               'invalid')
