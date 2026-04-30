"""Shared test utilities for unittest-based tests (rut).

Provides mixins and helper functions equivalent to tests/conftest.py
but without any pytest dependency.
"""
import os
import time
import tempfile
import shutil
import itertools
import unittest
from dbm import whichdb
from io import StringIO

from doit.dependency import Dependency, MD5Checker, TimestampChecker
from doit.dependency import DbmDB, JsonDB, SqliteDB
from doit.task import Task
from doit.cmd_base import get_loader
from doit.runner import Runner
from doit.reporter import ConsoleReporter
from doit.control import TaskDispatcher


# ---------------------------------------------------------------------------
# Plain helpers (copied from tests/conftest.py — already pytest-free)
# ---------------------------------------------------------------------------

def get_abspath(relative_path):
    """Return absolute file path relative to tests/ directory."""
    return os.path.join(os.path.dirname(__file__), relative_path)


# dbm backends use different file extensions
db_ext = {
    'dbm.ndbm': ['.db'],
    'dbm.dump': ['.dat', '.dir', '.bak'],
    'dbm.gnu': [''],
}


def remove_all_db(filename):
    """Remove db file from anydbm (all possible extensions)."""
    for ext in itertools.chain.from_iterable(db_ext.values()):
        if os.path.exists(filename + ext):
            try:
                os.remove(filename + ext)
            except PermissionError:
                pass


backend_map = {
    'dbm': DbmDB,
    'dbm.gnu': DbmDB,
    'dbm.ndbm': DbmDB,
    'dbm.dumb': DbmDB,
    'json': JsonDB,
    'sqlite3': SqliteDB,
}


def tasks_sample(dep1=None):
    """Create a list of sample tasks."""
    file_dep = dep1 if dep1 else 'tests/data/dependency1'
    sample = [
        Task("t1", [""], doc="t1 doc string",
             params=[{'name': 'arg1', 'short': 'a', 'long': 'arg1',
                       'default': 'default_value'}]),
        Task("t2", [""], file_dep=[file_dep], doc="t2 doc string"),
        Task("g1", None, doc="g1 doc string", has_subtask=True),
        Task("g1.a", [""], doc="g1.a doc string", subtask_of='g1'),
        Task("g1.b", [""], doc="g1.b doc string", subtask_of='g1'),
        Task("t3", [""], doc="t3 doc string", task_dep=["t1"]),
    ]
    sample[2].task_dep = ['g1.a', 'g1.b']
    return sample


def tasks_bad_sample():
    """Create list of tasks that cause errors."""
    return [Task("e1", [""], doc='e4 bad file dep', file_dep=['xxxx'])]


def CmdFactory(cls, outstream=None, task_loader=None, dep_file=None,
               backend=None, task_list=None, sel_tasks=None,
               sel_default_tasks=False, dep_manager=None, config=None,
               cmds=None):
    """Helper for test code, so test can call _execute() directly."""
    loader = get_loader(config, task_loader, cmds)
    cmd = cls(task_loader=loader, config=config, cmds=cmds)
    if outstream:
        cmd.outstream = outstream
    if backend:
        dep_class = backend_map[backend]
        cmd.dep_manager = Dependency(dep_class, dep_file, MD5Checker,
                                     module_name=backend)
    elif dep_manager:
        cmd.dep_manager = dep_manager
    cmd.dep_file = dep_file
    cmd.task_list = task_list
    cmd.sel_tasks = sel_tasks
    cmd.sel_default_tasks = sel_default_tasks
    return cmd


# ---------------------------------------------------------------------------
# Mixins (use cooperative super() for composability)
# ---------------------------------------------------------------------------

class DepManagerMixin:
    """Provides self.dep_manager (DbmDB backend) with cleanup."""

    def setUp(self):
        super().setUp()
        self._dep_tmpdir = tempfile.mkdtemp(prefix='doit-test-')
        filename = os.path.join(self._dep_tmpdir, 'testdb')
        self.dep_manager = Dependency(DbmDB, filename)
        if whichdb(self.dep_manager.name):
            self.dep_manager.whichdb = whichdb(self.dep_manager.name)
        else:
            self.dep_manager.whichdb = 'dbm'
        self.dep_manager.name_ext = db_ext.get(
            self.dep_manager.whichdb, [''])

    def tearDown(self):
        if not self.dep_manager._closed:
            self.dep_manager.close()
        remove_all_db(self.dep_manager.name)
        shutil.rmtree(self._dep_tmpdir, ignore_errors=True)
        super().tearDown()


class DepfileNameMixin:
    """Provides self.depfile_name (path string) with cleanup."""

    def setUp(self):
        super().setUp()
        self._depfile_tmpdir = tempfile.mkdtemp(prefix='doit-test-')
        self.depfile_name = os.path.join(self._depfile_tmpdir, 'testdb')

    def tearDown(self):
        remove_all_db(self.depfile_name)
        shutil.rmtree(self._depfile_tmpdir, ignore_errors=True)
        super().tearDown()


class DependencyFileMixin:
    """Provides self.dependency1 and self.dependency2 file paths."""

    def setUp(self):
        super().setUp()
        self.dependency1 = get_abspath("data/dependency1")
        self._write_dep(self.dependency1)
        self.dependency2 = get_abspath("data/dependency2")
        self._write_dep(self.dependency2)

    def _write_dep(self, path):
        if os.path.exists(path):
            os.remove(path)
        with open(path, "w") as f:
            f.write("whatever" + str(time.asctime()))

    def tearDown(self):
        for p in (self.dependency1, self.dependency2):
            if os.path.exists(p):
                os.remove(p)
        super().tearDown()


class RestoreCwdMixin:
    """Restores cwd after each test."""

    def setUp(self):
        super().setUp()
        self._original_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._original_cwd)
        super().tearDown()


# ---------------------------------------------------------------------------
# Unified Dependency Test Harness (for consistent multi-backend testing)
# ---------------------------------------------------------------------------

AVAILABLE_BACKENDS = ['json', 'sqlite3', 'dbm.ndbm', 'dbm.dumb', 'dbm.gnu']


class DependencyHarnessMixin:
    """
    Unified harness for dependency tests across all backends.

    Provides:
    - Consistent setup for JsonDB, DbmDB, SqliteDB backends
    - Helper methods for creating tasks, file_deps, targets
    - Standardized close/reopen workflow
    - Assertion helpers for dependency state
    """
    backend_name = None

    def setUp(self):
        super().setUp()
        self._dep_tmpdir = tempfile.mkdtemp(prefix='doit-dep-harness-')
        self._file_tmpdir = tempfile.mkdtemp(prefix='doit-file-harness-')
        self._dep_name = os.path.join(self._dep_tmpdir, 'testdb')
        self._tasks = {}
        self._files_created = []

        if self.backend_name:
            self._setup_dep_manager()

    def _setup_dep_manager(self):
        """Create dep_manager for the configured backend."""
        dep_class = backend_map[self.backend_name]
        try:
            if self.backend_name.startswith('dbm.'):
                self.dep_manager = Dependency(
                    dep_class, self._dep_name, module_name=self.backend_name)
            else:
                self.dep_manager = Dependency(dep_class, self._dep_name)
        except ImportError:
            raise unittest.SkipTest(f'"{self.backend_name}" not available.')

        if self.backend_name == 'dbm':
            self.dep_manager.whichdb = whichdb(self.dep_manager.name) or 'dbm'
        else:
            self.dep_manager.whichdb = self.backend_name
        self.dep_manager.name_ext = db_ext.get(
            self.dep_manager.whichdb, [''])

    def tearDown(self):
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()
        for f in self._files_created:
            if os.path.exists(f):
                os.remove(f)
        if hasattr(self, '_dep_tmpdir'):
            shutil.rmtree(self._dep_tmpdir, ignore_errors=True)
        if hasattr(self, '_file_tmpdir'):
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

    def create_simple_task(self, name, with_dep=True, with_target=False):
        """Create a task with optional file_dep and target."""
        file_dep = [self.create_file(f'{name}_dep.txt')] if with_dep else []
        targets = [self.create_file(f'{name}_target.txt')] if with_target else []
        return self.create_task(name, file_dep=file_dep, targets=targets)

    def close_dep(self):
        """Close the dependency manager (flush to disk)."""
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()

    def reopen_dep(self):
        """Close and reopen the dependency manager.

        This simulates the real-world scenario where doit is run multiple times.
        Returns the new dep_manager instance.
        """
        self.close_dep()
        self._setup_dep_manager()
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

    def assert_same_dep_value(self, task_name, dep_key, expected_value, msg=None):
        """Assert that a dependency value matches expected."""
        actual = self.dep_manager._get(task_name, dep_key)
        self.assertEqual(
            actual, expected_value,
            msg or f"Dependency '{dep_key}' for task '{task_name}' mismatch"
        )


class RunnerHarnessMixin(DependencyHarnessMixin):
    """
    Harness for testing the full Runner workflow.

    Extends DependencyHarnessMixin with:
    - Creating Runner instances
    - Running tasks through the full run/finish cycle
    - Checking results after close/reopen
    """

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

    def run_task_via_runner(self, task, tasks_dict=None):
        """Run a single task through the full Runner workflow.

        This tests the real execution path: select_task -> execute_task -> process_task_result
        """
        if tasks_dict is None:
            tasks_dict = {task.name: task}

        runner = self.create_runner()
        dispatcher = TaskDispatcher(tasks_dict, [], [task.name])
        result = runner.run_all(dispatcher)
        return result

    def run_tasks_via_runner(self, tasks, selected_names=None):
        """Run multiple tasks through the full Runner workflow."""
        tasks_dict = {t.name: t for t in tasks}
        if selected_names is None:
            selected_names = list(tasks_dict.keys())

        runner = self.create_runner()
        dispatcher = TaskDispatcher(tasks_dict, [], selected_names)
        result = runner.run_all(dispatcher)
        return result


def create_backend_test_classes(base_class, backend_list=None):
    """
    Create parameterized test classes for each backend.

    This is a class decorator factory that creates test classes for each
    available backend, similar to pytest parametrize but for unittest.

    Usage:
        @create_backend_test_classes
        class _MyTests(DependencyHarnessMixin, unittest.TestCase):
            def test_something(self):
                ...

    This will create:
        - TestMyTestsJson
        - TestMyTestsSqlite
        - TestMyTestsDbmNdbm
        - etc.
    """
    if backend_list is None:
        backend_list = AVAILABLE_BACKENDS

    def decorator(base_class):
        module = base_class.__module__
        base_name = base_class.__name__
        if base_name.startswith('_'):
            base_name = base_name[1:]

        for backend in backend_list:
            # Generate a class name from backend name
            backend_parts = backend.replace('.', '_').split('_')
            class_suffix = ''.join(p.capitalize() for p in backend_parts)
            class_name = f'Test{base_name}{class_suffix}'

            # Create the new class
            new_class = type(class_name, (base_class,), {
                'backend_name': backend,
                '__module__': module,
            })

            # Register it in the module
            import sys
            sys.modules[module].__dict__[class_name] = new_class

        return base_class

    return decorator


@create_backend_test_classes
class _BackendSmokeTest(DependencyHarnessMixin, unittest.TestCase):
    """Smoke test to verify all backends work with the harness."""

    def test_harness_creates_dep_manager(self):
        self.assertIsNotNone(self.dep_manager)
        self.assertIsNotNone(self.dep_manager.backend)

    def test_close_and_reopen(self):
        self.dep_manager._set('task1', 'dep1', 'value1')
        self.reopen_dep()
        self.assertEqual('value1', self.dep_manager._get('task1', 'dep1'))


class FixedTaskLoader:
    """A task loader that returns a fixed list of tasks.

    This is useful for testing when you want to bypass the normal
    task loading process (which involves inspect.getsourcelines).

    It directly returns the task_list provided to it, without any
    dependency on source code inspection.
    """
    API = 2
    cmd_options = ()

    def __init__(self, task_list, doit_config=None):
        self.cmd_names = []
        self.config = None
        self.task_opts = None
        self._task_list = task_list
        self._doit_config = doit_config or {}

    def setup(self, opt_values):
        pass

    def load_doit_config(self):
        return self._doit_config

    def load_tasks(self, cmd, pos_args):
        return self._task_list
