"""Tests for doit.dependency — migrated from pytest to unittest."""
import os
import sys
import time
import tempfile
import shutil
from dbm import whichdb
from sys import executable
from unittest.mock import patch
import unittest

from doit.task import Task
from doit.dependency import get_md5, get_file_md5
from doit.dependency import DbmDB, JsonDB, SqliteDB, Dependency
from doit.dependency import DatabaseException, UptodateCalculator
from doit.dependency import FileChangedChecker, MD5Checker, TimestampChecker
from doit.dependency import DependencyStatus
from tests.support import get_abspath, backend_map, db_ext
from tests.support import remove_all_db, DependencyFileMixin, DepManagerMixin, DepfileNameMixin

# path to test folder (the original tests/ dir, where sample files live)
TEST_PATH = os.path.join(os.path.dirname(__file__), '..', 'tests')
PROGRAM = "%s %s/sample_process.py" % (executable, TEST_PATH)


####
# dependencies are files only (not other tasks).
#
# whenever a task has a dependency the runner checks if this dependency
# was modified since last successful run. if not the task is skipped.

# since more than one task might have the same dependency, and the tasks
# might have different results (success/failure). the signature is associated
# not only with the file, but also with the task.
#
# save in db (task - dependency - (timestamp, size, signature))
# taskId_dependency => signature(dependency)
# taskId is md5(CmdTask.task)


# ---------------------------------------------------------------------------
# Bare test functions wrapped in a TestCase
# ---------------------------------------------------------------------------

class TestUtilFunctions(unittest.TestCase):

    def test_unicode_md5(self):
        data = "\u6211"
        # no exception is raised
        self.assertTrue(get_md5(data))

    def test_md5(self):
        filePath = os.path.join(TEST_PATH, "sample_md5.txt")
        # result got using command line md5sum, with different line-endings
        # to deal with different GIT configurations:
        expected_lf = "45d1503cb985898ab5bd8e58973007dd"
        expected_crlf = "cf7b48b2fec3b581b135f7c9a1f7ae04"
        self.assertIn(get_file_md5(filePath), {expected_lf, expected_crlf})

    def test_sqlite_import(self):
        """Checks that SQLite module is not imported until the SQLite class is instantiated"""
        from doit import dependency
        self.assertFalse(hasattr(dependency, 'sqlite3'))


# ---------------------------------------------------------------------------
# DependencyTestBase mixin: parametrized dep_manager by backend_name
# ---------------------------------------------------------------------------

class DependencyTestBase:
    """Mixin providing self.dep_manager for the backend given by backend_name."""
    backend_name = None

    def setUp(self):
        super().setUp()
        self._dep_tmpdir = tempfile.mkdtemp(prefix='doit-test-dep-')
        filename = os.path.join(self._dep_tmpdir, 'testdb')
        dep_class = backend_map[self.backend_name]
        try:
            if self.backend_name.startswith('dbm.'):
                self.dep_manager = Dependency(
                    dep_class, filename, module_name=self.backend_name)
            else:
                self.dep_manager = Dependency(dep_class, filename)
        except ImportError:
            self.skipTest(f'"{self.backend_name}" not available.')
        if self.backend_name == 'dbm':
            self.dep_manager.whichdb = whichdb(self.dep_manager.name)
        else:
            self.dep_manager.whichdb = self.backend_name
        self.dep_manager.name_ext = db_ext.get(
            self.dep_manager.whichdb, [''])

    def tearDown(self):
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()
        if hasattr(self, '_dep_tmpdir'):
            shutil.rmtree(self._dep_tmpdir, ignore_errors=True)
        super().tearDown()


# FIXME there was major refactor breaking classes from dependency,
# unit-tests could be more specific to base classes.

# ---------------------------------------------------------------------------
# TestDependencyDb
# ---------------------------------------------------------------------------

class _DependencyDbTests:
    """Tests for basic DB operations (get/set/dump/remove etc.)."""

    # adding a new value to the DB
    def test_get_set(self):
        self.dep_manager._set("taskId_X", "dependency_A", "da_md5")
        value = self.dep_manager._get("taskId_X", "dependency_A")
        self.assertEqual("da_md5", value)

    def test_get_set_unicode_name(self):
        self.dep_manager._set("taskId_\u6211", "dependency_A", "da_md5")
        value = self.dep_manager._get("taskId_\u6211", "dependency_A")
        self.assertEqual("da_md5", value)

    #
    def test_dump(self):
        # save and close db
        self.dep_manager._set("taskId_X", "dependency_A", "da_md5")
        self.dep_manager.close()

        # open it again and check the value
        d2 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        value = d2._get("taskId_X", "dependency_A")
        self.assertEqual("da_md5", value)
        d2.close()

    def test_corrupted_file(self):
        if self.dep_manager.whichdb == 'sqlite3':
            self.skipTest('close() does not release fp on windows')
        if self.dep_manager.whichdb == 'dbm.ndbm':
            # TODO: ndbm raises no Exception, but it writes an error on STDERR.
            self.skipTest('dbm.ndbm does not raise Exception')
        if self.dep_manager.whichdb == 'dbm.dumb' and sys.version_info >= (3, 13):
            # Python 3.13 dbm.dumb silently recovers from corrupted files
            self.skipTest('dbm.dumb does not raise Exception on 3.13+')

        # create some corrupted files
        for name_ext in self.dep_manager.name_ext:
            full_name = self.dep_manager.name + name_ext
            fd = open(full_name, 'w')
            fd.seek(0)
            fd.write("""{"x": y}""")
            fd.close()
        self.assertRaises(
            DatabaseException, Dependency,
            self.dep_manager.db_class, self.dep_manager.name)

    def test_corrupted_file_unrecognized_excep(self):
        if self.dep_manager.whichdb == 'sqlite3':
            self.skipTest('close() does not release fp on windows')
        if self.dep_manager.whichdb == 'dbm.ndbm':
            # TODO: ndbm raises no Exception, but it writes an error on STDERR.
            self.skipTest('dbm.ndbm does not raise Exception')
        if self.dep_manager.whichdb == 'dbm.dumb' and sys.version_info >= (3, 13):
            self.skipTest('dbm.dumb does not raise Exception on 3.13+')

        # create some corrupted files
        for name_ext in self.dep_manager.name_ext:
            full_name = self.dep_manager.name + name_ext
            fd = open(full_name, 'w')
            fd.seek(0)
            fd.write("""{"x": y}""")
            fd.close()
        with patch.object(DbmDB, 'DBM_CONTENT_ERROR_MSG', 'xxx'):
            self.assertRaises(
                DatabaseException, Dependency,
                self.dep_manager.db_class, self.dep_manager.name)

    # _get must return None if entry doesnt exist.
    def test_getNonExistent(self):
        self.assertIsNone(self.dep_manager._get("taskId_X", "dependency_A"))

    def test_in(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.assertTrue(self.dep_manager._in("taskId_ZZZ"))
        self.assertFalse(self.dep_manager._in("taskId_hohoho"))

    def test_remove(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.dep_manager._set("taskId_ZZZ", "dep_2", "13")
        self.dep_manager._set("taskId_YYY", "dep_1", "14")
        self.dep_manager.remove("taskId_ZZZ")
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_1"))
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_2"))
        self.assertEqual("14", self.dep_manager._get("taskId_YYY", "dep_1"))

    # special test for DBM backend and "dirty"/caching mechanism
    def test_remove_from_non_empty_file(self):
        # 1 - put 2 tasks of file
        self.dep_manager._set("taskId_XXX", "dep_1", "x")
        self.dep_manager._set("taskId_YYY", "dep_1", "x")
        self.dep_manager.close()
        # 2 - re-open and remove one task
        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        reopened.remove("taskId_YYY")
        reopened.close()
        # 3 - re-open again and check task was really removed
        reopened2 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertTrue(reopened2._in("taskId_XXX"))
        self.assertFalse(reopened2._in("taskId_YYY"))
        reopened2.close()

    def test_remove_all(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.dep_manager._set("taskId_ZZZ", "dep_2", "13")
        self.dep_manager._set("taskId_YYY", "dep_1", "14")
        self.dep_manager.remove_all()
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_1"))
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_2"))
        self.assertIsNone(self.dep_manager._get("taskId_YYY", "dep_1"))


class TestDependencyDbJson(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'json'

class TestDependencyDbSqlite(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencyDbDbmGnu(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencyDbDbmNdbm(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencyDbDbmDumb(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestSaveSuccess
# ---------------------------------------------------------------------------

class _SaveSuccessTests:
    """Tests for Dependency.save_success."""

    def test_save_result(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1)
        self.assertEqual(get_md5("result"), self.dep_manager._get(t1.name, "result:"))
        self.assertEqual(get_md5("result"), self.dep_manager.get_result(t1.name))

    def test_save_result_hash(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1, result_hash='abc')
        self.assertEqual('abc', self.dep_manager._get(t1.name, "result:"))

    def test_save_resultNone(self):
        t1 = Task('t_name', None)
        self.dep_manager.save_success(t1)
        self.assertIsNone(self.dep_manager._get(t1.name, "result:"))

    def test_save_result_dict(self):
        t1 = Task('t_name', None)
        t1.result = {'d': "result"}
        self.dep_manager.save_success(t1)
        self.assertEqual({'d': "result"}, self.dep_manager._get(t1.name, "result:"))

    def test_save_file_md5(self):
        # create a test dependency file
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("i am the first dependency ever for doit")
        ff.close()

        # save it
        t1 = Task("taskId_X", None, [filePath])
        self.dep_manager.save_success(t1)
        expected = "a1bb792202ce163b4f0d17cb264c04e1"
        value = self.dep_manager._get("taskId_X", filePath)
        self.assertEqual(os.path.getmtime(filePath), value[0])  # timestamp
        self.assertEqual(39, value[1])  # size
        self.assertEqual(expected, value[2])  # MD5

    def test_save_skip(self):
        filePath = get_abspath("data/dependency1")
        # ensure file exists
        ff = open(filePath, "w")
        ff.write("content")
        ff.close()
        t1 = Task("taskId_X", None, [filePath])
        self.dep_manager._set(t1.name, filePath, (345, 0, "fake"))
        with patch.object(os.path, 'getmtime', lambda x: 345):
            # save but md5 is not modified
            self.dep_manager.save_success(t1)
        got = self.dep_manager._get("taskId_X", filePath)
        self.assertEqual("fake", got[2])

    def test_save_files(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()
        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part2")
        ff.close()
        self.assertIsNone(self.dep_manager._get("taskId_X", filePath))
        self.assertIsNone(self.dep_manager._get("taskId_X", filePath2))

        t1 = Task("taskId_X", None, [filePath, filePath2])
        self.dep_manager.save_success(t1)
        self.assertIsNotNone(self.dep_manager._get("taskId_X", filePath))
        self.assertIsNotNone(self.dep_manager._get("taskId_X", filePath2))
        self.assertEqual(
            set(self.dep_manager._get("taskId_X", 'deps:')), t1.file_dep)

    def test_save_values(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual({'x': 5, 'y': 10}, self.dep_manager._get("t1", "_values_:"))


class TestSaveSuccessJson(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'json'

class TestSaveSuccessSqlite(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestSaveSuccessDbmGnu(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestSaveSuccessDbmNdbm(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestSaveSuccessDbmDumb(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestGetValue
# ---------------------------------------------------------------------------

class _GetValueTests:
    """Tests for Dependency.get_value / get_values."""

    def test_all_values(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual({'x': 5, 'y': 10}, self.dep_manager.get_values('t1'))

    def test_ok(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual(5, self.dep_manager.get_value('t1', 'x'))

    def test_ok_dot_on_task_name(self):
        t1 = Task('t1:a.ext', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual(5, self.dep_manager.get_value('t1:a.ext', 'x'))

    def test_invalid_taskid(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertRaises(Exception, self.dep_manager.get_value, 'nonono', 'x')

    def test_invalid_key(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertRaises(Exception, self.dep_manager.get_value, 't1', 'z')


class TestGetValueJson(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'json'

class TestGetValueSqlite(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestGetValueDbmGnu(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestGetValueDbmNdbm(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestGetValueDbmDumb(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestRemoveSuccess
# ---------------------------------------------------------------------------

class _RemoveSuccessTests:

    def test_save_result(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1)
        self.assertEqual(get_md5("result"), self.dep_manager._get(t1.name, "result:"))
        self.dep_manager.remove_success(t1)
        self.assertIsNone(self.dep_manager._get(t1.name, "result:"))


class TestRemoveSuccessJson(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'json'

class TestRemoveSuccessSqlite(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestRemoveSuccessDbmGnu(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestRemoveSuccessDbmNdbm(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestRemoveSuccessDbmDumb(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestIgnore
# ---------------------------------------------------------------------------

class _IgnoreTests:

    def test_save_result(self):
        t1 = Task('t_name', None)
        self.dep_manager.ignore(t1)
        self.assertEqual('1', self.dep_manager._get(t1.name, "ignore:"))


class TestIgnoreJson(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'json'

class TestIgnoreSqlite(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIgnoreDbmGnu(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIgnoreDbmNdbm(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIgnoreDbmDumb(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestMD5Checker
# ---------------------------------------------------------------------------

class TestMD5Checker(DependencyFileMixin, unittest.TestCase):

    def test_timestamp(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0], state[1] + 1, '')
        file_stat = os.stat(self.dependency1)
        # dep considered the same as long as timestamp is unchanged
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))

    def test_size(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0] + 1, state[1] + 1, state[2])
        file_stat = os.stat(self.dependency1)
        # if size changed for sure modified (md5 is not checked)
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state2))

    def test_md5(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        # same size and md5
        state2 = (state[0] + 1, state[1], state[2])
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))
        # same size, different md5
        state3 = (state[0] + 1, state[1], 'not me')
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state3))


# ---------------------------------------------------------------------------
# TestCustomChecker
# ---------------------------------------------------------------------------

class TestCustomChecker(DependencyFileMixin, unittest.TestCase):

    def test_not_implemented(self):
        class MyChecker(FileChangedChecker):
            pass

        checker = MyChecker()
        self.assertRaises(NotImplementedError, checker.get_state, None, None)
        self.assertRaises(NotImplementedError, checker.check_modified,
                          None, None, None)


# ---------------------------------------------------------------------------
# TestTimestampChecker
# ---------------------------------------------------------------------------

class TestTimestampChecker(DependencyFileMixin, unittest.TestCase):

    def test_timestamp(self):
        checker = TimestampChecker()
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state))
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state + 1))


# ---------------------------------------------------------------------------
# TestDependencyStatus
# ---------------------------------------------------------------------------

class TestDependencyStatus(unittest.TestCase):

    def test_add_reason(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.add_reason('changed_file_dep', 'f1'))
        self.assertEqual('run', result.status)
        self.assertFalse(result.add_reason('changed_file_dep', 'f2'))
        self.assertEqual(['f1', 'f2'], result.reasons['changed_file_dep'])

    def test_add_reason_error(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.add_reason('missing_file_dep', 'f1', 'error'))
        self.assertEqual('error', result.status)
        self.assertEqual(['f1'], result.reasons['missing_file_dep'])

    def test_set_reason(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.set_reason('has_no_dependencies', True))
        self.assertEqual('run', result.status)
        self.assertTrue(result.reasons['has_no_dependencies'])

    def test_no_log(self):
        result = DependencyStatus(False)
        self.assertEqual('up-to-date', result.status)
        self.assertTrue(result.set_reason('has_no_dependencies', True))
        self.assertEqual('run', result.status)

    def test_get_error_message(self):
        result = DependencyStatus(False)
        self.assertIsNone(result.get_error_message())
        result.error_reason = 'foo xxx'
        self.assertEqual('foo xxx', result.get_error_message())


# ---------------------------------------------------------------------------
# TestGetStatus
# ---------------------------------------------------------------------------

class _GetStatusTests:
    """Tests for Dependency.get_status."""

    def test_ignore(self):
        t1 = Task("t1", None)
        # before ignore
        self.assertFalse(self.dep_manager.status_is_ignore(t1))
        # after ignote
        self.dep_manager.ignore(t1)
        self.assertTrue(self.dep_manager.status_is_ignore(t1))

    def test_fileDependencies(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        dependencies = [filePath]
        t1 = Task("t1", None, dependencies)

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(dependencies, t1.dep_changed)

        # second time no
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # FIXME - mock timestamp
        time.sleep(1)  # required otherwise timestamp is not modified!
        # a small change on the file
        ff = open(filePath, "a")
        ff.write(" part2")
        ff.close()

        # execute again
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(dependencies, t1.dep_changed)

    def test_fileDependencies_changed(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part1")
        ff.close()

        dependencies = [filePath, filePath2]
        t1 = Task("t1", None, dependencies)

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(sorted(dependencies), sorted(t1.dep_changed))

        # second time no
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # remove dependency filePath2
        t1 = Task("t1", None, [filePath])
        # execute again
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_fileDependencies_changed_get_log(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, [filePath])

        # first time execute
        result = self.dep_manager.get_status(t1, {}, get_log=True)
        self.assertEqual('run', result.status)
        self.assertEqual([filePath], t1.dep_changed)
        self.dep_manager.save_success(t1)

        # second time
        t1b = Task("t1", None, [filePath2])
        result = self.dep_manager.get_status(t1b, {}, get_log=True)
        self.assertEqual('run', result.status)
        self.assertEqual([filePath2], t1b.dep_changed)
        self.assertEqual([filePath], result.reasons['removed_file_dep'])
        self.assertEqual([filePath2], result.reasons['added_file_dep'])

    def test_file_dependency_not_exist(self):
        filePath = get_abspath("data/dependency_not_exist")
        t1 = Task("t1", None, [filePath])
        self.assertEqual('error', self.dep_manager.get_status(t1, {}).status)

    def test_change_checker(self):
        # need a real dependency file
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("checker test content")
        ff.close()

        t1 = Task("taskId_X", None, [dep_path])
        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        # change of checker force `run` again
        self.dep_manager.checker = MD5Checker()
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    # if there is no dependency the task is always executed
    def test_noDependency(self):
        t1 = Task("t1", None)
        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)
        # second too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateFalse(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[False])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateTrue(self):
        t1 = Task("t1", None, uptodate=[True])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateNone(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[None])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([filePath], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateFunction_True(self):
        test_case = self

        def check(task, values):
            test_case.assertEqual('t1', task.name)
            return True

        t1 = Task("t1", None, uptodate=[check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateFunction_False(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        def check(task, values):
            return False

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[check])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateFunction_without_args_True(self):
        def check():
            return True

        t1 = Task("t1", None, uptodate=[check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_uptodate_call_all_even_if_some_False(self):
        checks = []

        def check():
            checks.append(1)
            return False

        t1 = Task("t1", None, uptodate=[check, check])
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(2, len(checks))

    def test_UptodateFunction_extra_args_True(self):
        test_case = self

        def check(task, values, control):
            test_case.assertEqual('t1', task.name)
            return control > 30

        t1 = Task("t1", None, uptodate=[(check, [34])])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCallable_True(self):
        test_case = self

        class MyChecker(object):
            def __call__(self, task, values):
                test_case.assertEqual('t1', task.name)
                return True

        t1 = Task("t1", None, uptodate=[MyChecker()])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateMethod_True(self):
        test_case = self

        class MyChecker(object):
            def check(self, task, values):
                test_case.assertEqual('t1', task.name)
                return True

        t1 = Task("t1", None, uptodate=[MyChecker().check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCallable_added_attributes(self):
        test_case = self
        task_dict = "fake dict"

        class My_uptodate(UptodateCalculator):
            def __call__(self, task, values):
                # attributes were added to object before call'ing it
                test_case.assertEqual(task_dict, self.tasks_dict)
                test_case.assertIsNone(self.get_val('t1', None))
                return True

        check = My_uptodate()
        t1 = Task("t1", None, uptodate=[check])
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, task_dict).status)

    def test_UptodateCommand_True(self):
        t1 = Task("t1", None, uptodate=[PROGRAM])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCommand_False(self):
        t1 = Task("t1", None, uptodate=[PROGRAM + ' please fail'])
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)

    # if target file does not exist, task is outdated.
    def test_targets_notThere(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        target = get_abspath("data/target")
        if os.path.exists(target):
            os.remove(target)

        t1 = Task("task x", None, [dep_path], [target])
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([dep_path], t1.dep_changed)

    def test_targets(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        filePath = get_abspath("data/target")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        deps = [dep_path]
        targets = [filePath]
        t1 = Task("task X", None, deps, targets)

        self.dep_manager.save_success(t1)
        # up-to-date because target exist
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_targetFolder(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        # folder not there. task is not up-to-date
        deps = [dep_path]
        folderPath = get_abspath("data/target-folder")
        if os.path.exists(folderPath):
            os.rmdir(folderPath)
        t1 = Task("task x", None, deps, [folderPath])
        self.dep_manager.save_success(t1)

        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(deps, t1.dep_changed)
        # create folder. task is up-to-date
        os.mkdir(folderPath)
        try:
            self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
            self.assertEqual([], t1.dep_changed)
        finally:
            if os.path.exists(folderPath):
                os.rmdir(folderPath)


class TestGetStatusJson(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'json'

class TestGetStatusSqlite(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestGetStatusDbmGnu(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestGetStatusDbmNdbm(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestGetStatusDbmDumb(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# Boundary Regression Tests for Dependency/Runner/Commands Integration
# ---------------------------------------------------------------------------

class _UptodateExceptionPropagationTests:
    """Scenario 1: uptodate callable 抛异常时的异常传播测试"""

    def test_uptodate_callable_raises_get_status(self):
        """get_status 直接调用时 uptodate callable 抛异常的行为"""

        def raise_exception(task, values):
            raise RuntimeError("uptodate check failed intentionally")

        t1 = Task("t1", None, uptodate=[raise_exception])

        with self.assertRaises(RuntimeError) as ctx:
            self.dep_manager.get_status(t1, {})
        self.assertIn("uptodate check failed intentionally", str(ctx.exception))

    def test_uptodate_callable_raises_via_selector_with_args(self):
        """uptodate callable 带参数时抛异常"""

        def raise_with_arg(task, values, msg):
            raise ValueError(msg)

        t1 = Task("t1", None, uptodate=[(raise_with_arg, ["custom error"])])

        with self.assertRaises(ValueError) as ctx:
            self.dep_manager.get_status(t1, {})
        self.assertIn("custom error", str(ctx.exception))

    def test_uptodate_callable_no_args_raises(self):
        """uptodate callable 不带参数时抛异常"""

        def raise_simple():
            raise ZeroDivisionError("division by zero in uptodate")

        t1 = Task("t1", None, uptodate=[raise_simple])

        with self.assertRaises(ZeroDivisionError) as ctx:
            self.dep_manager.get_status(t1, {})
        self.assertIn("division by zero", str(ctx.exception))


class TestUptodateExceptionJson(DependencyTestBase, _UptodateExceptionPropagationTests, unittest.TestCase):
    backend_name = 'json'

class TestUptodateExceptionSqlite(DependencyTestBase, _UptodateExceptionPropagationTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestUptodateExceptionDbmGnu(DependencyTestBase, _UptodateExceptionPropagationTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestUptodateExceptionDbmNdbm(DependencyTestBase, _UptodateExceptionPropagationTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestUptodateExceptionDbmDumb(DependencyTestBase, _UptodateExceptionPropagationTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _CheckerChangedSideEffectTests:
    """Scenario 2: get_status(get_log=True) 时 checker_changed 的 remove 副作用测试"""

    def test_checker_changed_removes_task_on_get_log_true(self):
        """get_log=True 时 checker 变化会触发 remove 副作用"""
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("checker test content")

        t1 = Task("t1", None, [dep_path])

        from doit.dependency import TimestampChecker
        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(t1)

        self.assertTrue(self.dep_manager._in("t1"))
        original_dep_state = self.dep_manager._get("t1", dep_path)
        original_checker = self.dep_manager._get("t1", "checker:")
        self.assertIsNotNone(original_dep_state)
        self.assertEqual("TimestampChecker", original_checker)

        from doit.dependency import MD5Checker
        self.dep_manager.checker = MD5Checker()

        result = self.dep_manager.get_status(t1, {}, get_log=True)
        self.assertEqual('run', result.status)
        self.assertIn('checker_changed', result.reasons)

        dep_state_after = self.dep_manager._get("t1", dep_path)
        checker_after = self.dep_manager._get("t1", "checker:")
        self.assertIsNone(dep_state_after)
        self.assertIsNone(checker_after)

    def test_checker_changed_removes_task_on_get_log_false(self):
        """get_log=False 时 checker 变化也会触发 remove 副作用"""
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("checker test content")

        t1 = Task("t1", None, [dep_path])

        from doit.dependency import TimestampChecker
        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(t1)

        self.assertTrue(self.dep_manager._in("t1"))
        original_dep_state = self.dep_manager._get("t1", dep_path)
        original_checker = self.dep_manager._get("t1", "checker:")
        self.assertIsNotNone(original_dep_state)
        self.assertEqual("TimestampChecker", original_checker)

        from doit.dependency import MD5Checker
        self.dep_manager.checker = MD5Checker()

        result = self.dep_manager.get_status(t1, {}, get_log=False)
        self.assertEqual('run', result.status)

        dep_state_after = self.dep_manager._get("t1", dep_path)
        checker_after = self.dep_manager._get("t1", "checker:")
        self.assertIsNone(dep_state_after)
        self.assertIsNone(checker_after)


class TestCheckerChangedJson(DependencyTestBase, _CheckerChangedSideEffectTests, unittest.TestCase):
    backend_name = 'json'

class TestCheckerChangedSqlite(DependencyTestBase, _CheckerChangedSideEffectTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestCheckerChangedDbmGnu(DependencyTestBase, _CheckerChangedSideEffectTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestCheckerChangedDbmNdbm(DependencyTestBase, _CheckerChangedSideEffectTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestCheckerChangedDbmDumb(DependencyTestBase, _CheckerChangedSideEffectTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _TaskStateTransitionTests:
    """Scenario 3: 任务成功->失败->成功的 db 状态测试"""

    def test_success_fail_success_db_state(self):
        """任务成功运行，再失败，再重新成功，close/reopen 后的状态"""
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("initial content")

        t1 = Task("t1", None, [dep_path])
        t1.result = "first_success"

        self.dep_manager.save_success(t1)
        self.assertTrue(self.dep_manager._in("t1"))
        first_result_hash = self.dep_manager._get("t1", "result:")
        self.assertIsNotNone(first_result_hash)

        self.dep_manager.close()

        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertTrue(reopened._in("t1"))
        self.assertEqual(first_result_hash, reopened._get("t1", "result:"))

        reopened.close()

        reopened2 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        t1_modified = Task("t1", None, [dep_path])
        t1_modified.result = "second_success_after_failure"

        reopened2.save_success(t1_modified)
        second_result_hash = reopened2._get("t1", "result:")
        self.assertNotEqual(first_result_hash, second_result_hash)

        reopened2.close()

        reopened3 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertTrue(reopened3._in("t1"))
        self.assertEqual(second_result_hash, reopened3._get("t1", "result:"))
        reopened3.close()

    def test_failure_does_not_overwrite_success_state(self):
        """失败的任务执行不会覆盖之前成功的状态"""
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("content")

        t1 = Task("t1", None, [dep_path])
        t1.result = "success_result"

        self.dep_manager.save_success(t1)
        original_result = self.dep_manager._get("t1", "result:")
        original_dep_state = self.dep_manager._get("t1", dep_path)

        self.dep_manager.close()

        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertEqual(original_result, reopened._get("t1", "result:"))
        self.assertEqual(list(original_dep_state), list(reopened._get("t1", dep_path)))
        reopened.close()


class TestTaskStateTransitionJson(DependencyTestBase, _TaskStateTransitionTests, unittest.TestCase):
    backend_name = 'json'

class TestTaskStateTransitionSqlite(DependencyTestBase, _TaskStateTransitionTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestTaskStateTransitionDbmGnu(DependencyTestBase, _TaskStateTransitionTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestTaskStateTransitionDbmNdbm(DependencyTestBase, _TaskStateTransitionTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestTaskStateTransitionDbmDumb(DependencyTestBase, _TaskStateTransitionTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _TaskNameIsolationTests:
    """Scenario 4: 子任务、普通任务、父任务的 dependency 记录隔离测试"""

    def test_subtask_parent_child_isolation(self):
        """'parent:child'、'parent_child'、'parent' 互不污染"""
        dep1 = get_abspath("data/dependency1")
        dep2 = get_abspath("data/dependency2")

        with open(dep1, "w") as f:
            f.write("dep1 content")
        with open(dep2, "w") as f:
            f.write("dep2 content")

        parent = Task("parent", None, [dep1])
        parent.result = "parent_result"
        parent_sub = Task("parent:child", None, [dep2], subtask_of='parent')
        parent_sub.result = "subtask_result"
        parent_child = Task("parent_child", None, [dep1, dep2])
        parent_child.result = "flat_result"

        self.dep_manager.save_success(parent)
        self.dep_manager.save_success(parent_sub)
        self.dep_manager.save_success(parent_child)

        self.assertTrue(self.dep_manager._in("parent"))
        self.assertTrue(self.dep_manager._in("parent:child"))
        self.assertTrue(self.dep_manager._in("parent_child"))

        parent_result = self.dep_manager._get("parent", "result:")
        sub_result = self.dep_manager._get("parent:child", "result:")
        flat_result = self.dep_manager._get("parent_child", "result:")

        self.assertIsNotNone(parent_result)
        self.assertIsNotNone(sub_result)
        self.assertIsNotNone(flat_result)
        self.assertNotEqual(parent_result, sub_result)
        self.assertNotEqual(parent_result, flat_result)
        self.assertNotEqual(sub_result, flat_result)

        parent_dep = self.dep_manager._get("parent", dep1)
        sub_dep = self.dep_manager._get("parent:child", dep2)
        flat_dep1 = self.dep_manager._get("parent_child", dep1)
        flat_dep2 = self.dep_manager._get("parent_child", dep2)

        self.assertIsNotNone(parent_dep)
        self.assertIsNotNone(sub_dep)
        self.assertIsNotNone(flat_dep1)
        self.assertIsNotNone(flat_dep2)

    def test_remove_one_does_not_affect_others(self):
        """删除一个任务不影响其他同名模式的任务"""
        parent = Task("parent", None)
        parent_sub = Task("parent:child", None, subtask_of='parent')
        parent_child = Task("parent_child", None)

        self.dep_manager.save_success(parent)
        self.dep_manager.save_success(parent_sub)
        self.dep_manager.save_success(parent_child)

        self.dep_manager.remove("parent")

        self.assertFalse(self.dep_manager._in("parent"))
        self.assertTrue(self.dep_manager._in("parent:child"))
        self.assertTrue(self.dep_manager._in("parent_child"))

        self.dep_manager.remove("parent:child")

        self.assertFalse(self.dep_manager._in("parent:child"))
        self.assertTrue(self.dep_manager._in("parent_child"))


class TestTaskNameIsolationJson(DependencyTestBase, _TaskNameIsolationTests, unittest.TestCase):
    backend_name = 'json'

class TestTaskNameIsolationSqlite(DependencyTestBase, _TaskNameIsolationTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestTaskNameIsolationDbmGnu(DependencyTestBase, _TaskNameIsolationTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestTaskNameIsolationDbmNdbm(DependencyTestBase, _TaskNameIsolationTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestTaskNameIsolationDbmDumb(DependencyTestBase, _TaskNameIsolationTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _FileDepRemovalDuringExecutionTests:
    """Scenario 5: file_dep 在执行中被删除的行为测试"""

    def test_save_success_after_file_dep_deleted_raises(self):
        """file_dep 在 save_success 时不存在会抛出 FileNotFoundError

        当前实现行为：
        - save_success 先写入 _values_:、result:、checker:
        - 然后遍历 file_dep 获取状态，此时可能抛出 FileNotFoundError
        - 部分数据可能已写入，所以 _in 可能返回 True
        """
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("temporary content")

        t1 = Task("t1", None, [dep_path])
        t1.result = "test_result"

        status = self.dep_manager.get_status(t1, {})
        self.assertEqual('run', status.status)

        os.remove(dep_path)
        self.assertFalse(os.path.exists(dep_path))

        with self.assertRaises(FileNotFoundError):
            self.dep_manager.save_success(t1)

        result_val = self.dep_manager._get("t1", "result:")
        checker_val = self.dep_manager._get("t1", "checker:")
        dep_val = self.dep_manager._get("t1", dep_path)

        self.assertIsNotNone(result_val)
        self.assertEqual("MD5Checker", checker_val)
        self.assertIsNone(dep_val)

    def test_get_status_before_file_removal(self):
        """get_status 时文件存在，但 save_success 时文件不存在"""
        dep_path = get_abspath("data/dependency1")
        with open(dep_path, "w") as f:
            f.write("content")

        t1 = Task("t1", None, [dep_path])

        status = self.dep_manager.get_status(t1, {})
        self.assertEqual('run', status.status)
        self.assertEqual([dep_path], t1.dep_changed)

        os.remove(dep_path)

        with self.assertRaises(FileNotFoundError):
            self.dep_manager.save_success(t1)


class TestFileDepRemovalJson(DependencyTestBase, _FileDepRemovalDuringExecutionTests, unittest.TestCase):
    backend_name = 'json'

class TestFileDepRemovalSqlite(DependencyTestBase, _FileDepRemovalDuringExecutionTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestFileDepRemovalDbmGnu(DependencyTestBase, _FileDepRemovalDuringExecutionTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestFileDepRemovalDbmNdbm(DependencyTestBase, _FileDepRemovalDuringExecutionTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestFileDepRemovalDbmDumb(DependencyTestBase, _FileDepRemovalDuringExecutionTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _RemoveAllContinuationTests:
    """Scenario 6: remove_all() 后继续写入新任务的测试"""

    def test_remove_all_then_write_new_tasks(self):
        """remove_all() 后同一个进程继续写入新任务"""
        t1 = Task("old_task_1", None)
        t2 = Task("old_task_2", None)

        self.dep_manager.save_success(t1)
        self.dep_manager.save_success(t2)

        self.assertTrue(self.dep_manager._in("old_task_1"))
        self.assertTrue(self.dep_manager._in("old_task_2"))

        self.dep_manager.remove_all()

        self.assertFalse(self.dep_manager._in("old_task_1"))
        self.assertFalse(self.dep_manager._in("old_task_2"))

        new_t1 = Task("new_task_1", None)
        new_t2 = Task("new_task_2", None)
        self.dep_manager.save_success(new_t1)
        self.dep_manager.save_success(new_t2)

        self.assertTrue(self.dep_manager._in("new_task_1"))
        self.assertTrue(self.dep_manager._in("new_task_2"))
        self.assertFalse(self.dep_manager._in("old_task_1"))
        self.assertFalse(self.dep_manager._in("old_task_2"))

        self.dep_manager.close()

        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)

        self.assertTrue(reopened._in("new_task_1"))
        self.assertTrue(reopened._in("new_task_2"))
        self.assertFalse(reopened._in("old_task_1"))
        self.assertFalse(reopened._in("old_task_2"))

        reopened.close()

    def test_remove_all_mixed_operations(self):
        """remove_all 前后的混合操作"""
        for i in range(3):
            t = Task(f"pre_task_{i}", None)
            self.dep_manager.save_success(t)

        self.dep_manager.remove_all()

        for i in range(2):
            t = Task(f"mid_task_{i}", None)
            self.dep_manager.save_success(t)

        self.dep_manager.remove_all()

        final_t = Task("final_task", None)
        self.dep_manager.save_success(final_t)

        self.dep_manager.close()

        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertTrue(reopened._in("final_task"))
        self.assertFalse(reopened._in("pre_task_0"))
        self.assertFalse(reopened._in("mid_task_0"))
        reopened.close()


class TestRemoveAllContinuationJson(DependencyTestBase, _RemoveAllContinuationTests, unittest.TestCase):
    backend_name = 'json'

class TestRemoveAllContinuationSqlite(DependencyTestBase, _RemoveAllContinuationTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestRemoveAllContinuationDbmGnu(DependencyTestBase, _RemoveAllContinuationTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestRemoveAllContinuationDbmNdbm(DependencyTestBase, _RemoveAllContinuationTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestRemoveAllContinuationDbmDumb(DependencyTestBase, _RemoveAllContinuationTests, unittest.TestCase):
    backend_name = 'dbm.dumb'
