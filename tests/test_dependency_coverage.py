"""
Comprehensive dependency tests organized by coverage level.

This module explicitly separates tests into three categories:
1. BACKEND_* - Low-level backend (JsonDB/DbmDB/SqliteDB) behavior tests
2. DEPENDENCY_* - High-level Dependency class behavior tests
3. INTEGRATION_* - Full run/close/reopen integration tests with Runner

Key regression scenarios covered (per requirements):
- task成功执行后close/reopen，依赖状态能重新读取
- task失败后remove_success，再close/reopen
- remove/remove_all后再set，新值不丢、旧task不复活
- up-to-date任务不会修改dependency db
- get_status(get_log=True)收集reasons时不应意外写入新task记录
- checker_changed的remove副作用要有明确测试说明
- 子任务名parent:child和普通task在dependency db中的记录互不污染
"""
import os
import time
import unittest
from unittest.mock import patch
from io import StringIO

from doit.task import Task
from doit.dependency import MD5Checker, TimestampChecker
from doit.runner import Runner
from doit.reporter import ConsoleReporter
from doit.control import TaskDispatcher

from tests.support import (
    DependencyHarnessMixin,
    RunnerHarnessMixin,
    get_abspath,
    backend_map,
)


# ===========================================================================
# Base class for parameterized backend tests
# ===========================================================================

class DependencyCoverageTestBase:
    """Mixin providing self.dep_manager for the backend given by backend_name."""
    backend_name = None

    def setUp(self):
        super().setUp()
        import tempfile
        import shutil
        from dbm import whichdb
        from doit.dependency import Dependency

        self._dep_tmpdir = tempfile.mkdtemp(prefix='doit-test-dep-coverage-')
        self._file_tmpdir = tempfile.mkdtemp(prefix='doit-test-file-coverage-')
        self._dep_name = os.path.join(self._dep_tmpdir, 'testdb')
        self._tasks = {}
        self._files_created = []

        dep_class = backend_map[self.backend_name]
        try:
            if self.backend_name.startswith('dbm.'):
                self.dep_manager = Dependency(
                    dep_class, self._dep_name, module_name=self.backend_name)
            else:
                self.dep_manager = Dependency(dep_class, self._dep_name)
        except ImportError:
            self.skipTest(f'"{self.backend_name}" not available.')

        if self.backend_name == 'dbm':
            self.dep_manager.whichdb = whichdb(self.dep_manager.name) or 'dbm'
        else:
            self.dep_manager.whichdb = self.backend_name

    def tearDown(self):
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()
        for f in self._files_created:
            if os.path.exists(f):
                os.remove(f)
        if hasattr(self, '_dep_tmpdir'):
            import shutil
            shutil.rmtree(self._dep_tmpdir, ignore_errors=True)
        if hasattr(self, '_file_tmpdir'):
            import shutil
            shutil.rmtree(self._file_tmpdir, ignore_errors=True)
        super().tearDown()

    def create_file(self, name, content='content'):
        """Create a temporary file for use as file_dep or target."""
        path = os.path.join(self._file_tmpdir, name)
        with open(path, 'w') as f:
            f.write(content)
        self._files_created.append(path)
        return path

    def create_task(self, name, actions=None, file_dep=None, targets=None,
                    values=None, uptodate=None):
        """Create a Task object with consistent defaults."""
        task = Task(
            name,
            actions or [lambda: None],
            file_dep=file_dep or [],
            targets=targets or [],
            uptodate=uptodate or []
        )
        if values:
            task.values = values
        self._tasks[name] = task
        return task

    def create_runner(self, reporter=None, continue_=False, always_execute=False):
        """Create a Runner instance with the current dep_manager."""
        if reporter is None:
            outstream = StringIO()
            reporter = ConsoleReporter(outstream, {})
        self.runner = Runner(
            self.dep_manager,
            reporter,
            continue_=continue_,
            always_execute=always_execute
        )
        return self.runner

    def run_task_via_runner(self, task, tasks_dict=None, always_execute=False):
        """Run a single task through the full Runner workflow.

        Note: Runner.run_all() calls finish() which closes dep_manager.
        We reopen it here so tests can continue to use self.dep_manager.
        """
        if tasks_dict is None:
            tasks_dict = {task.name: task}

        runner = self.create_runner(always_execute=always_execute)
        dispatcher = TaskDispatcher(tasks_dict, [], [task.name])
        result = runner.run_all(dispatcher)
        self.reopen_dep()
        return result

    def run_tasks_via_runner(self, tasks, selected_names=None, always_execute=False):
        """Run multiple tasks through the full Runner workflow.

        Note: Runner.run_all() calls finish() which closes dep_manager.
        We reopen it here so tests can continue to use self.dep_manager.
        """
        tasks_dict = {t.name: t for t in tasks}
        if selected_names is None:
            selected_names = list(tasks_dict.keys())

        runner = self.create_runner(always_execute=always_execute)
        dispatcher = TaskDispatcher(tasks_dict, [], selected_names)
        result = runner.run_all(dispatcher)
        self.reopen_dep()
        return result

    def close_dep(self):
        """Close the dependency manager (flush to disk)."""
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()

    def reopen_dep(self):
        """Close and reopen the dependency manager."""
        self.close_dep()
        from doit.dependency import Dependency
        dep_class = backend_map[self.backend_name]
        if self.backend_name.startswith('dbm.'):
            self.dep_manager = Dependency(
                dep_class, self._dep_name, module_name=self.backend_name)
        else:
            self.dep_manager = Dependency(dep_class, self._dep_name)
        return self.dep_manager

    def assert_task_in_db(self, task_name, msg=None):
        """Assert that a task has saved state in the DB."""
        self.assertTrue(
            self.dep_manager._in(task_name),
            msg or f"Task '{task_name}' should be in dependency DB"
        )

    def assert_task_not_in_db(self, task_name, msg=None):
        """Assert that a task has NO saved state in the DB."""
        self.assertFalse(
            self.dep_manager._in(task_name),
            msg or f"Task '{task_name}' should NOT be in dependency DB"
        )

    def _normalize_backend_name(self, backend_name):
        """Convert harness backend name to CLI backend name.

        The harness uses 'dbm.gnu', 'dbm.ndbm', 'dbm.dumb' but the CLI
        uses just 'dbm'. This handles the conversion.
        """
        if backend_name.startswith('dbm.'):
            return 'dbm'
        return backend_name

    def _run_cmd_via_parse_execute(self, cmd_cls, task_list, args=None, outstream=None):
        """Run a command via parse_execute path.

        This is a more realistic test path than calling _execute directly.
        It tests:
        - Command line argument parsing
        - Loader setup and task loading
        - dep_manager creation (if not provided)
        - Actual command execution

        Args:
            cmd_cls: Command class (Run, List, Info, Forget, etc.)
            task_list: List of Task objects
            args: Command line arguments (e.g., ['task1'], ['--status'], etc.)
            outstream: Optional output stream (default: StringIO)

        Returns:
            tuple: (result, outstream, cmd)
                - result: Result from parse_execute
                - outstream: The output stream used
                - cmd: The command object created
        """
        from tests.support import FixedTaskLoader, CmdFactory

        if args is None:
            args = []
        if outstream is None:
            outstream = StringIO()

        loader = FixedTaskLoader(task_list)

        cmd = CmdFactory(
            cmd_cls,
            outstream=outstream,
            task_loader=loader,
            dep_file=self._dep_name,
            dep_manager=self.dep_manager,
        )

        normalized_backend = self._normalize_backend_name(self.backend_name)
        full_args = [
            '--db-file', self._dep_name,
            '--backend', normalized_backend,
        ] + args

        result = cmd.parse_execute(full_args)
        return result, outstream, cmd


# ===========================================================================
# CATEGORY 1: BACKEND_* - Low-level backend behavior tests
# ===========================================================================
# These tests directly exercise the backend classes (JsonDB, DbmDB, SqliteDB)
# without going through the Dependency wrapper. They test:
#   - Basic get/set operations
#   - Persistence across close/reopen
#   - remove/remove_all semantics
# ===========================================================================

class _BackendBasicOps(DependencyCoverageTestBase):
    """Low-level backend: basic get/set/in_ operations."""

    def test_backend_get_set_basic(self):
        """BACKEND: Direct set and get on backend."""
        self.dep_manager.backend.set('task1', 'key1', 'value1')
        self.assertEqual('value1', self.dep_manager.backend.get('task1', 'key1'))

    def test_backend_get_set_unicode(self):
        """BACKEND: Unicode task names and keys."""
        self.dep_manager.backend.set('task_\u4e2d\u6587', 'key_\u6d4b\u8bd5', 'value')
        self.assertEqual('value', self.dep_manager.backend.get('task_\u4e2d\u6587', 'key_\u6d4b\u8bd5'))

    def test_backend_get_nonexistent(self):
        """BACKEND: Get returns None for non-existent entries."""
        self.assertIsNone(self.dep_manager.backend.get('nonexistent', 'key'))

    def test_backend_in_(self):
        """BACKEND: in_() checks task existence."""
        self.dep_manager.backend.set('task1', 'key1', 'value1')
        self.assertTrue(self.dep_manager.backend.in_('task1'))
        self.assertFalse(self.dep_manager.backend.in_('task2'))

    def test_backend_task_isolation(self):
        """BACKEND: Different tasks have isolated key spaces."""
        self.dep_manager.backend.set('task1', 'common_key', 'value1')
        self.dep_manager.backend.set('task2', 'common_key', 'value2')
        self.assertEqual('value1', self.dep_manager.backend.get('task1', 'common_key'))
        self.assertEqual('value2', self.dep_manager.backend.get('task2', 'common_key'))


class TestBackendBasicOpsJson(_BackendBasicOps, unittest.TestCase):
    backend_name = 'json'

class TestBackendBasicOpsSqlite(_BackendBasicOps, unittest.TestCase):
    backend_name = 'sqlite3'

class TestBackendBasicOpsDbmGnu(_BackendBasicOps, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestBackendBasicOpsDbmNdbm(_BackendBasicOps, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestBackendBasicOpsDbmDumb(_BackendBasicOps, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _BackendPersistence(DependencyCoverageTestBase):
    """Low-level backend: persistence across close/reopen cycles."""

    def test_backend_persistence_after_close(self):
        """BACKEND: Values persist after close and reopen."""
        self.dep_manager.backend.set('task1', 'key1', 'value1')
        self.dep_manager.backend.set('task2', 'key2', {'nested': 'data'})

        self.reopen_dep()

        self.assertEqual('value1', self.dep_manager.backend.get('task1', 'key1'))
        self.assertEqual({'nested': 'data'}, self.dep_manager.backend.get('task2', 'key2'))

    def test_backend_remove_persistence(self):
        """BACKEND: remove() is persisted after close/reopen."""
        self.dep_manager.backend.set('task1', 'key1', 'value1')
        self.dep_manager.backend.set('task2', 'key2', 'value2')
        self.dep_manager.backend.remove('task1')

        self.reopen_dep()

        self.assertFalse(self.dep_manager.backend.in_('task1'))
        self.assertTrue(self.dep_manager.backend.in_('task2'))

    def test_backend_remove_all_persistence(self):
        """BACKEND: remove_all() is persisted after close/reopen."""
        self.dep_manager.backend.set('task1', 'key1', 'value1')
        self.dep_manager.backend.set('task2', 'key2', 'value2')
        self.dep_manager.backend.remove_all()

        self.reopen_dep()

        self.assertFalse(self.dep_manager.backend.in_('task1'))
        self.assertFalse(self.dep_manager.backend.in_('task2'))


class TestBackendPersistenceJson(_BackendPersistence, unittest.TestCase):
    backend_name = 'json'

class TestBackendPersistenceSqlite(_BackendPersistence, unittest.TestCase):
    backend_name = 'sqlite3'

class TestBackendPersistenceDbmGnu(_BackendPersistence, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestBackendPersistenceDbmNdbm(_BackendPersistence, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestBackendPersistenceDbmDumb(_BackendPersistence, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _BackendRemoveSemantics(DependencyCoverageTestBase):
    """Low-level backend: remove/remove_all edge cases."""

    def test_backend_remove_nonexistent_no_error(self):
        """BACKEND: remove() on non-existent task does not raise."""
        self.dep_manager.backend.remove('nonexistent_task')

    def test_backend_remove_then_set(self):
        """BACKEND: After remove(), can set new values for same task."""
        self.dep_manager.backend.set('task1', 'old_key', 'old_value')
        self.dep_manager.backend.remove('task1')
        self.dep_manager.backend.set('task1', 'new_key', 'new_value')

        self.assertEqual('new_value', self.dep_manager.backend.get('task1', 'new_key'))
        self.assertIsNone(self.dep_manager.backend.get('task1', 'old_key'))

    def test_backend_remove_all_then_set(self):
        """BACKEND: After remove_all(), can set new tasks."""
        self.dep_manager.backend.set('old_task', 'key', 'old')
        self.dep_manager.backend.remove_all()
        self.dep_manager.backend.set('new_task', 'key', 'new')

        self.assertTrue(self.dep_manager.backend.in_('new_task'))
        self.assertFalse(self.dep_manager.backend.in_('old_task'))

    def test_backend_remove_all_persistence_with_new_set(self):
        """REGRESSION: remove/remove_all后再set，新值不丢、旧task不复活."""
        self.dep_manager.backend.set('task_old', 'key', 'old_value')
        self.dep_manager.backend.set('task_both', 'key', 'before')
        self.dep_manager.backend.remove_all()
        self.dep_manager.backend.set('task_new', 'key', 'new_value')
        self.dep_manager.backend.set('task_both', 'key', 'after')

        self.reopen_dep()

        self.assertTrue(self.dep_manager.backend.in_('task_new'),
                        "New task should exist after reopen")
        self.assertEqual('new_value', self.dep_manager.backend.get('task_new', 'key'))

        self.assertTrue(self.dep_manager.backend.in_('task_both'),
                        "Re-added task should exist")
        self.assertEqual('after', self.dep_manager.backend.get('task_both', 'key'))

        self.assertFalse(self.dep_manager.backend.in_('task_old'),
                         "Old task should NOT be resurrected")


class TestBackendRemoveSemanticsJson(_BackendRemoveSemantics, unittest.TestCase):
    backend_name = 'json'

class TestBackendRemoveSemanticsSqlite(_BackendRemoveSemantics, unittest.TestCase):
    backend_name = 'sqlite3'

class TestBackendRemoveSemanticsDbmGnu(_BackendRemoveSemantics, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestBackendRemoveSemanticsDbmNdbm(_BackendRemoveSemantics, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestBackendRemoveSemanticsDbmDumb(_BackendRemoveSemantics, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ===========================================================================
# CATEGORY 2: DEPENDENCY_* - High-level Dependency class behavior tests
# ===========================================================================
# These tests exercise the Dependency class wrapper over backends.
# They test:
#   - save_success / remove_success semantics
#   - get_status behavior
#   - get_log=True side effects (or lack thereof)
#   - checker_changed remove behavior
#   - Task name isolation (parent:child vs regular tasks)
# ===========================================================================

class _DependencySaveSuccess(DependencyCoverageTestBase):
    """High-level Dependency: save_success and persistence."""

    def test_save_success_persistence(self):
        """DEPENDENCY: save_success persists after close/reopen."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])
        task.values = {'x': 1, 'y': 2}
        task.result = 'test_result'

        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.reopen_dep()

        self.assert_task_in_db('task1')
        self.assertEqual({'x': 1, 'y': 2}, self.dep_manager.get_values('task1'))

    def test_save_success_then_remove_success(self):
        """DEPENDENCY: remove_success clears task from DB."""
        task = self.create_task('task1')
        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.dep_manager.remove_success(task)
        self.assert_task_not_in_db('task1')

    def test_remove_success_persistence(self):
        """REGRESSION: task失败后remove_success，再close/reopen."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])
        task.values = {'x': 1}

        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.dep_manager.remove_success(task)
        self.assert_task_not_in_db('task1')

        self.reopen_dep()

        self.assert_task_not_in_db('task1',
            "Task should remain removed after reopen")

    def test_save_success_file_dep_saved(self):
        """DEPENDENCY: save_success stores file_dep states."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.save_success(task)

        self.assertIsNotNone(self.dep_manager._get('task1', dep_file))
        self.assertIn(dep_file, self.dep_manager._get('task1', 'deps:'))


class TestDependencySaveSuccessJson(_DependencySaveSuccess, unittest.TestCase):
    backend_name = 'json'

class TestDependencySaveSuccessSqlite(_DependencySaveSuccess, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencySaveSuccessDbmGnu(_DependencySaveSuccess, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencySaveSuccessDbmNdbm(_DependencySaveSuccess, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencySaveSuccessDbmDumb(_DependencySaveSuccess, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _DependencyGetStatus(DependencyCoverageTestBase):
    """High-level Dependency: get_status behavior."""

    def test_get_status_no_deps_always_run(self):
        """DEPENDENCY: Task without deps is never up-to-date."""
        task = self.create_task('task1')
        self.dep_manager.save_success(task)

        result = self.dep_manager.get_status(task, {})
        self.assertEqual('run', result.status)

    def test_get_status_with_deps_up_to_date(self):
        """DEPENDENCY: Task with saved deps is up-to-date."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.save_success(task)
        result = self.dep_manager.get_status(task, {})
        self.assertEqual('up-to-date', result.status)

    def test_get_status_up_to_date_does_not_modify_db(self):
        """REGRESSION: up-to-date任务不会修改dependency db."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.save_success(task)
        original_deps = self.dep_manager._get('task1', 'deps:')
        original_checker = self.dep_manager._get('task1', 'checker:')

        result = self.dep_manager.get_status(task, {})
        self.assertEqual('up-to-date', result.status)

        self.assertEqual(original_deps, self.dep_manager._get('task1', 'deps:'))
        self.assertEqual(original_checker, self.dep_manager._get('task1', 'checker:'))

    def test_get_status_get_log_does_not_write_new_task(self):
        """REGRESSION: get_status(get_log=True)收集reasons时不应意外写入新task记录.

        Note: SqliteDB.get() caches empty dicts for non-existent tasks,
        which can cause in_() to return True even if nothing was written.
        We use reopen_dep() to clear the cache and verify persistence.
        """
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.assert_task_not_in_db('task1')

        result = self.dep_manager.get_status(task, {}, get_log=True)

        self.assertEqual('run', result.status)

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "get_log=True should NOT create new task records in DB")

    def test_get_status_log_reasons_collected(self):
        """DEPENDENCY: get_log=True collects status change reasons."""
        task = self.create_task('task1', uptodate=[False])
        self.dep_manager.save_success(task)

        result = self.dep_manager.get_status(task, {}, get_log=True)

        self.assertEqual('run', result.status)
        self.assertIn('uptodate_false', result.reasons)


class TestDependencyGetStatusJson(_DependencyGetStatus, unittest.TestCase):
    backend_name = 'json'

class TestDependencyGetStatusSqlite(_DependencyGetStatus, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencyGetStatusDbmGnu(_DependencyGetStatus, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencyGetStatusDbmNdbm(_DependencyGetStatus, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencyGetStatusDbmDumb(_DependencyGetStatus, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _DependencyCheckerChanged(DependencyCoverageTestBase):
    """High-level Dependency: checker_changed behavior and its side effects.

    IMPORTANT: checker_changed has a SIDE EFFECT - it REMOVES the task
    from the dependency DB to ensure old checker state is not reused.
    This is documented behavior that should NOT be changed.
    """

    def test_checker_changed_removes_task(self):
        """REGRESSION: checker_changed的remove副作用要有明确测试说明.

        When checker changes (e.g., from MD5 to Timestamp), the task
        is REMOVED from DB to prevent state reuse. This is intentional.

        Note: We use reopen_dep() to clear SqliteDB cache and verify
        the actual persistence state.
        """
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.checker = MD5Checker()
        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.dep_manager.checker = TimestampChecker()
        result = self.dep_manager.get_status(task, {}, get_log=True)

        self.assertEqual('run', result.status)
        self.assertIn('checker_changed', result.reasons)

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "Task should be REMOVED when checker changes (documented side effect)")

    def test_checker_changed_persistence(self):
        """DEPENDENCY: checker_changed remove persists after close/reopen."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.checker = MD5Checker()
        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.get_status(task, {})

        self.reopen_dep()

        self.assert_task_not_in_db('task1',
            "Task removal from checker_change should persist")

    def test_checker_same_no_remove(self):
        """DEPENDENCY: Same checker does not trigger removal."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.dep_manager.checker = MD5Checker()
        self.dep_manager.save_success(task)
        self.assert_task_in_db('task1')

        self.dep_manager.get_status(task, {})

        self.assert_task_in_db('task1',
            "Task should remain when checker is unchanged")


class TestDependencyCheckerChangedJson(_DependencyCheckerChanged, unittest.TestCase):
    backend_name = 'json'

class TestDependencyCheckerChangedSqlite(_DependencyCheckerChanged, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencyCheckerChangedDbmGnu(_DependencyCheckerChanged, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencyCheckerChangedDbmNdbm(_DependencyCheckerChanged, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencyCheckerChangedDbmDumb(_DependencyCheckerChanged, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _DependencyTaskNameIsolation(DependencyCoverageTestBase):
    """High-level Dependency: Task name isolation.

    REGRESSION: 子任务名parent:child和普通task在dependency db中的记录互不污染.
    """

    def test_subtask_name_isolation(self):
        """DEPENDENCY: parent:child naming does not pollute parent namespace."""
        parent_task = self.create_task('parent')
        child_task = self.create_task('parent:child')
        regular_task = self.create_task('parent_regular')

        parent_task.values = {'type': 'parent'}
        child_task.values = {'type': 'child'}
        regular_task.values = {'type': 'regular'}

        self.dep_manager.save_success(parent_task)
        self.dep_manager.save_success(child_task)
        self.dep_manager.save_success(regular_task)

        self.assertEqual({'type': 'parent'},
                         self.dep_manager.get_values('parent'))
        self.assertEqual({'type': 'child'},
                         self.dep_manager.get_values('parent:child'))
        self.assertEqual({'type': 'regular'},
                         self.dep_manager.get_values('parent_regular'))

    def test_subtask_remove_isolation(self):
        """DEPENDENCY: Removing child does not affect parent."""
        parent_task = self.create_task('parent')
        child_task = self.create_task('parent:child')

        parent_task.values = {'x': 1}
        child_task.values = {'x': 2}

        self.dep_manager.save_success(parent_task)
        self.dep_manager.save_success(child_task)

        self.dep_manager.remove_success(child_task)

        self.assert_task_not_in_db('parent:child')
        self.assert_task_in_db('parent')
        self.assertEqual({'x': 1}, self.dep_manager.get_values('parent'))

    def test_subtask_persistence_isolation(self):
        """REGRESSION: parent:child和普通task记录互不污染 after close/reopen."""
        parent = self.create_task('group')
        child_a = self.create_task('group:a')
        child_b = self.create_task('group:b')
        similar = self.create_task('group_a')

        parent.values = {'level': 'parent'}
        child_a.values = {'level': 'child_a'}
        child_b.values = {'level': 'child_b'}
        similar.values = {'level': 'similar'}

        self.dep_manager.save_success(parent)
        self.dep_manager.save_success(child_a)
        self.dep_manager.save_success(child_b)
        self.dep_manager.save_success(similar)

        self.reopen_dep()

        self.assert_task_in_db('group')
        self.assert_task_in_db('group:a')
        self.assert_task_in_db('group:b')
        self.assert_task_in_db('group_a')

        self.assertEqual({'level': 'parent'},
                         self.dep_manager.get_values('group'))
        self.assertEqual({'level': 'child_a'},
                         self.dep_manager.get_values('group:a'))
        self.assertEqual({'level': 'child_b'},
                         self.dep_manager.get_values('group:b'))
        self.assertEqual({'level': 'similar'},
                         self.dep_manager.get_values('group_a'))


class TestDependencyTaskNameIsolationJson(_DependencyTaskNameIsolation, unittest.TestCase):
    backend_name = 'json'

class TestDependencyTaskNameIsolationSqlite(_DependencyTaskNameIsolation, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencyTaskNameIsolationDbmGnu(_DependencyTaskNameIsolation, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencyTaskNameIsolationDbmNdbm(_DependencyTaskNameIsolation, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencyTaskNameIsolationDbmDumb(_DependencyTaskNameIsolation, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ===========================================================================
# CATEGORY 3: INTEGRATION_* - Full integration tests with Runner
# ===========================================================================
# These tests exercise the full doit workflow:
#   - Create tasks with dependencies
#   - Run through Runner (or command)
#   - Close/reopen dep_manager
#   - Verify state across sessions
#
# These test the REAL execution path that users experience.
# ===========================================================================

class _IntegrationSuccessPersistence(DependencyCoverageTestBase):
    """INTEGRATION: Full run/close/reopen for successful tasks.

    REGRESSION: task成功执行后close/reopen，依赖状态能重新读取.
    """

    def test_runner_save_success_persists(self):
        """INTEGRATION: Task saved by Runner persists across sessions."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        result = self.run_task_via_runner(task)
        self.assertEqual(0, result)

        self.reopen_dep()

        self.assert_task_in_db('task1')
        task_reloaded = self.create_task('task1', file_dep=[dep_file])
        status_result = self.dep_manager.get_status(task_reloaded, {})
        self.assertEqual('up-to-date', status_result.status)

    def test_runner_multiple_tasks_persistence(self):
        """INTEGRATION: Multiple tasks maintain separate state."""
        dep1 = self.create_file('dep1.txt', 'c1')
        dep2 = self.create_file('dep2.txt', 'c2')

        task1 = self.create_task('task1', file_dep=[dep1])
        task2 = self.create_task('task2', file_dep=[dep2])

        result = self.run_tasks_via_runner([task1, task2])
        self.assertEqual(0, result)

        self.reopen_dep()

        self.assert_task_in_db('task1')
        self.assert_task_in_db('task2')

    def test_runner_up_to_date_skipped(self):
        """INTEGRATION: Up-to-date task is skipped on second run."""
        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        result1 = self.run_task_via_runner(task)
        self.assertEqual(0, result1)

        self.reopen_dep()

        task2 = self.create_task('task1', file_dep=[dep_file])
        result2 = self.run_task_via_runner(task2)
        self.assertEqual(0, result2)


class TestIntegrationSuccessPersistenceJson(_IntegrationSuccessPersistence, unittest.TestCase):
    backend_name = 'json'

class TestIntegrationSuccessPersistenceSqlite(_IntegrationSuccessPersistence, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIntegrationSuccessPersistenceDbmGnu(_IntegrationSuccessPersistence, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIntegrationSuccessPersistenceDbmNdbm(_IntegrationSuccessPersistence, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIntegrationSuccessPersistenceDbmDumb(_IntegrationSuccessPersistence, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _IntegrationFailureBehavior(DependencyCoverageTestBase):
    """INTEGRATION: Task failure and remove_success behavior."""

    def test_runner_failure_removes_success(self):
        """INTEGRATION: Failed task removes previous success state.

        Note: We use always_execute=True to force the task to execute even
        if it would otherwise be considered up-to-date. This is necessary
        to test the failure behavior.
        """
        dep_file = self.create_file('dep.txt', 'content')

        task_success = self.create_task('task1', file_dep=[dep_file])
        result1 = self.run_task_via_runner(task_success)
        self.assertEqual(0, result1)
        self.assert_task_in_db('task1')

        def fail_action():
            raise Exception("intentional failure")

        task_fail = self.create_task('task1', [fail_action], file_dep=[dep_file])
        result2 = self.run_task_via_runner(task_fail, always_execute=True)
        self.assertNotEqual(0, result2)

        self.assert_task_not_in_db('task1',
            "Failed task should have remove_success called")

    def test_runner_failure_persistence(self):
        """REGRESSION: task失败后remove_success，再close/reopen.

        This documents the EXISTING behavior: after failure and remove_success,
        the task is gone from DB. Different backends may have different
        persistence timing for remove(), but this test captures the current
        semantic without changing it.

        Note: We use always_execute=True to force the task to execute even
        if it would otherwise be considered up-to-date.
        """
        dep_file = self.create_file('dep.txt', 'content')

        task_success = self.create_task('task1', file_dep=[dep_file])
        self.run_task_via_runner(task_success)
        self.assert_task_in_db('task1')

        def fail_action():
            raise Exception("intentional failure")

        task_fail = self.create_task('task1', [fail_action], file_dep=[dep_file])
        self.run_task_via_runner(task_fail, always_execute=True)

        self.reopen_dep()

        self.assert_task_not_in_db('task1',
            "Failed task state should persist after reopen")


class TestIntegrationFailureBehaviorJson(_IntegrationFailureBehavior, unittest.TestCase):
    backend_name = 'json'

class TestIntegrationFailureBehaviorSqlite(_IntegrationFailureBehavior, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIntegrationFailureBehaviorDbmGnu(_IntegrationFailureBehavior, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIntegrationFailureBehaviorDbmNdbm(_IntegrationFailureBehavior, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIntegrationFailureBehaviorDbmDumb(_IntegrationFailureBehavior, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _IntegrationRemoveAllBehavior(DependencyCoverageTestBase):
    """INTEGRATION: remove_all behavior in real workflow."""

    def test_remove_all_after_run(self):
        """INTEGRATION: remove_all clears all tasks."""
        task1 = self.create_task('task1')
        task2 = self.create_task('task2')

        self.run_tasks_via_runner([task1, task2])
        self.assert_task_in_db('task1')
        self.assert_task_in_db('task2')

        self.dep_manager.remove_all()

        self.assert_task_not_in_db('task1')
        self.assert_task_not_in_db('task2')

    def test_remove_all_persistence(self):
        """INTEGRATION: remove_all persists after close/reopen."""
        task1 = self.create_task('task1')
        task2 = self.create_task('task2')

        self.run_tasks_via_runner([task1, task2])

        self.dep_manager.remove_all()

        self.reopen_dep()

        self.assert_task_not_in_db('task1')
        self.assert_task_not_in_db('task2')

    def test_remove_all_then_new_run(self):
        """INTEGRATION: After remove_all, new runs work normally."""
        old_task = self.create_task('old_task')
        self.run_task_via_runner(old_task)

        self.dep_manager.remove_all()

        new_task = self.create_task('new_task')
        self.run_task_via_runner(new_task)

        self.reopen_dep()

        self.assert_task_not_in_db('old_task')
        self.assert_task_in_db('new_task')


class TestIntegrationRemoveAllBehaviorJson(_IntegrationRemoveAllBehavior, unittest.TestCase):
    backend_name = 'json'

class TestIntegrationRemoveAllBehaviorSqlite(_IntegrationRemoveAllBehavior, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIntegrationRemoveAllBehaviorDbmGnu(_IntegrationRemoveAllBehavior, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIntegrationRemoveAllBehaviorDbmNdbm(_IntegrationRemoveAllBehavior, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIntegrationRemoveAllBehaviorDbmDumb(_IntegrationRemoveAllBehavior, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _IntegrationSubtaskIsolation(DependencyCoverageTestBase):
    """INTEGRATION: Subtask name isolation in real Runner workflow."""

    def test_subtask_run_isolation(self):
        """INTEGRATION: parent and child tasks run and persist separately."""
        parent = self.create_task('group')
        child_a = self.create_task('group:a')
        child_b = self.create_task('group:b')
        similar = self.create_task('group_a')

        self.run_tasks_via_runner([parent, child_a, child_b, similar])

        self.reopen_dep()

        self.assert_task_in_db('group')
        self.assert_task_in_db('group:a')
        self.assert_task_in_db('group:b')
        self.assert_task_in_db('group_a')

    def test_subtask_removal_isolation(self):
        """INTEGRATION: Forgetting child doesn't affect parent."""
        parent = self.create_task('parent')
        child = self.create_task('parent:child')

        self.run_tasks_via_runner([parent, child])

        self.dep_manager.remove_success(child)

        self.reopen_dep()

        self.assert_task_not_in_db('parent:child')
        self.assert_task_in_db('parent')


class TestIntegrationSubtaskIsolationJson(_IntegrationSubtaskIsolation, unittest.TestCase):
    backend_name = 'json'

class TestIntegrationSubtaskIsolationSqlite(_IntegrationSubtaskIsolation, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIntegrationSubtaskIsolationDbmGnu(_IntegrationSubtaskIsolation, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIntegrationSubtaskIsolationDbmNdbm(_IntegrationSubtaskIsolation, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIntegrationSubtaskIsolationDbmDumb(_IntegrationSubtaskIsolation, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ===========================================================================
# CATEGORY 4: COMMAND_* - Real Command Entry Tests
# ===========================================================================
# These tests exercise the REAL command entry points that users use:
#   - Run command (via CmdFactory)
#   - List command (list -s)
#   - Info command
#   - Forget command
#
# These go beyond the Runner direct calls and test the full command
# infrastructure as users would experience it.
# ===========================================================================

class _CommandRunSuccess(DependencyCoverageTestBase):
    """COMMAND: Run command success and persistence.

    REGRESSION: 成功 run 后 reopen 是 up-to-date.
    """

    def test_cmd_run_success_persists(self):
        """COMMAND: Run command success persists after close/reopen.

        This tests the REAL command entry point via parse_execute,
        not just direct _execute calls.
        """
        from doit.cmd_run import Run

        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [task], ['task1']
        )
        self.assertEqual(0, result1)

        self.reopen_dep()

        self.assert_task_in_db('task1')
        task_reloaded = self.create_task('task1', file_dep=[dep_file])
        status_result = self.dep_manager.get_status(task_reloaded, {})
        self.assertEqual('up-to-date', status_result.status)

    def test_cmd_run_success_then_second_run_up_to_date(self):
        """COMMAND: Second run of up-to-date task should be skipped.

        This tests the REAL command entry point via parse_execute.
        """
        from doit.cmd_run import Run

        dep_file = self.create_file('dep.txt', 'content')
        task1 = self.create_task('task1', file_dep=[dep_file])

        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [task1], ['task1']
        )
        self.assertEqual(0, result1)

        self.reopen_dep()

        task2 = self.create_task('task1', file_dep=[dep_file])
        result2, output2, cmd2 = self._run_cmd_via_parse_execute(
            Run, [task2], ['task1']
        )
        self.assertEqual(0, result2)


class TestCommandRunSuccessJson(_CommandRunSuccess, unittest.TestCase):
    backend_name = 'json'

class TestCommandRunSuccessSqlite(_CommandRunSuccess, unittest.TestCase):
    backend_name = 'sqlite3'

class TestCommandRunSuccessDbmGnu(_CommandRunSuccess, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestCommandRunSuccessDbmNdbm(_CommandRunSuccess, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestCommandRunSuccessDbmDumb(_CommandRunSuccess, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _CommandRunFailure(DependencyCoverageTestBase):
    """COMMAND: Run command failure and remove_success behavior.

    REGRESSION: 失败 run 后 remove_success 的 backend 差异被稳定记录.
    """

    def test_cmd_run_failure_removes_success(self):
        """COMMAND: Failed task removes previous success state.

        This tests the REAL command entry point via parse_execute,
        not just direct _execute calls.

        Note: We use --always-execute to force the task to execute even
        if it would otherwise be considered up-to-date. This is necessary
        to test the failure behavior.
        """
        from doit.cmd_run import Run

        dep_file = self.create_file('dep.txt', 'content')

        task_success = self.create_task('task1', file_dep=[dep_file])
        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [task_success], ['task1']
        )
        self.assertEqual(0, result1)

        self.reopen_dep()
        self.assert_task_in_db('task1')

        def fail_action():
            raise Exception("intentional failure")

        task_fail = self.create_task('task1', [fail_action], file_dep=[dep_file])
        result2, output2, cmd2 = self._run_cmd_via_parse_execute(
            Run, [task_fail], ['--always-execute', 'task1']
        )
        self.assertNotEqual(0, result2)

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "Failed task should have remove_success called via command entry")


class TestCommandRunFailureJson(_CommandRunFailure, unittest.TestCase):
    backend_name = 'json'

class TestCommandRunFailureSqlite(_CommandRunFailure, unittest.TestCase):
    backend_name = 'sqlite3'

class TestCommandRunFailureDbmGnu(_CommandRunFailure, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestCommandRunFailureDbmNdbm(_CommandRunFailure, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestCommandRunFailureDbmDumb(_CommandRunFailure, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _CommandReadOnly(DependencyCoverageTestBase):
    """COMMAND: Read-only commands (list -s, info) should not persist.

    REGRESSION: `list -s` / `info` 只读命令不会意外持久化新记录.
    """

    def test_cmd_list_status_does_not_create_new_task(self):
        """COMMAND: list -s should NOT create new task records in DB.

        This tests the REAL command entry point via parse_execute,
        not just direct _execute calls.

        Read-only commands like `list -s` may call get_status to determine
        task status, but they should NOT persist new task records.
        """
        from doit.cmd_list import List

        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.assert_task_not_in_db('task1')

        result, output, cmd = self._run_cmd_via_parse_execute(
            List, [task], ['--status']
        )
        self.assertEqual(0, result)

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "list -s should NOT create new task records in DB")

    def test_cmd_info_does_not_create_new_task(self):
        """COMMAND: info should NOT create new task records in DB.

        This tests the REAL command entry point via parse_execute,
        not just direct _execute calls.

        The info command shows task status but should not persist.
        """
        from doit.cmd_info import Info

        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        self.assert_task_not_in_db('task1')

        result, output, cmd = self._run_cmd_via_parse_execute(
            Info, [task], ['task1']
        )

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "info should NOT create new task records in DB")

    def test_cmd_list_status_on_existing_task(self):
        """COMMAND: list -s on existing task works without modification.

        This tests the REAL command entry point via parse_execute.
        """
        from doit.cmd_list import List
        from doit.cmd_run import Run

        dep_file = self.create_file('dep.txt', 'content')
        task = self.create_task('task1', file_dep=[dep_file])

        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [task], ['task1']
        )
        self.assertEqual(0, result1)

        self.reopen_dep()
        self.assert_task_in_db('task1')

        result2, output2, cmd2 = self._run_cmd_via_parse_execute(
            List, [task], ['--status']
        )
        self.assertEqual(0, result2)

        self.reopen_dep()
        self.assert_task_in_db('task1',
            "Existing task should remain after list -s")


class TestCommandReadOnlyJson(_CommandReadOnly, unittest.TestCase):
    backend_name = 'json'

class TestCommandReadOnlySqlite(_CommandReadOnly, unittest.TestCase):
    backend_name = 'sqlite3'

class TestCommandReadOnlyDbmGnu(_CommandReadOnly, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestCommandReadOnlyDbmNdbm(_CommandReadOnly, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestCommandReadOnlyDbmDumb(_CommandReadOnly, unittest.TestCase):
    backend_name = 'dbm.dumb'


class _CommandForgetBehavior(DependencyCoverageTestBase):
    """COMMAND: Forget command behavior.

    REGRESSION: `forget`/`remove_all` 通过命令入口后旧 task 不复活.
    """

    def test_cmd_forget_all_then_new_run(self):
        """COMMAND: forget --all then new run: new tasks work, old tasks gone.

        This tests the REAL command entry point via parse_execute,
        not just direct _execute calls.
        """
        from doit.cmd_forget import Forget
        from doit.cmd_run import Run

        old_task = self.create_task('old_task')

        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [old_task], ['old_task']
        )
        self.assertEqual(0, result1)

        self.reopen_dep()
        self.assert_task_in_db('old_task')

        result_forget, output_f, cmd_forget = self._run_cmd_via_parse_execute(
            Forget, [old_task], ['--all']
        )

        self.reopen_dep()
        self.assert_task_not_in_db('old_task',
            "Old task should be forgotten")

        new_task = self.create_task('new_task')
        result2, output2, cmd2 = self._run_cmd_via_parse_execute(
            Run, [new_task], ['new_task']
        )
        self.assertEqual(0, result2)

        self.reopen_dep()
        self.assert_task_not_in_db('old_task',
            "Old task should NOT be resurrected")
        self.assert_task_in_db('new_task',
            "New task should exist")

    def test_cmd_forget_single_task(self):
        """COMMAND: forget single task leaves others intact.

        This tests the REAL command entry point via parse_execute.
        """
        from doit.cmd_forget import Forget
        from doit.cmd_run import Run

        task1 = self.create_task('task1')
        task2 = self.create_task('task2')

        result1, output1, cmd1 = self._run_cmd_via_parse_execute(
            Run, [task1, task2], []
        )
        self.assertEqual(0, result1)

        self.reopen_dep()
        self.assert_task_in_db('task1')
        self.assert_task_in_db('task2')

        result_forget, output_f, cmd_forget = self._run_cmd_via_parse_execute(
            Forget, [task1, task2], ['task1']
        )

        self.reopen_dep()
        self.assert_task_not_in_db('task1',
            "task1 should be forgotten")
        self.assert_task_in_db('task2',
            "task2 should remain")


class TestCommandForgetBehaviorJson(_CommandForgetBehavior, unittest.TestCase):
    backend_name = 'json'

class TestCommandForgetBehaviorSqlite(_CommandForgetBehavior, unittest.TestCase):
    backend_name = 'sqlite3'

class TestCommandForgetBehaviorDbmGnu(_CommandForgetBehavior, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestCommandForgetBehaviorDbmNdbm(_CommandForgetBehavior, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestCommandForgetBehaviorDbmDumb(_CommandForgetBehavior, unittest.TestCase):
    backend_name = 'dbm.dumb'
