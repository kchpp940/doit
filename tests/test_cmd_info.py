import unittest
from io import StringIO

from doit.exceptions import InvalidCommand
from doit.task import Task
from doit.cmd_info import Info
from tests.support import CmdFactory, DepManagerMixin, DependencyFileMixin


class TestCmdInfo(DepManagerMixin, unittest.TestCase):

    def test_info_basic_attrs(self):
        output = StringIO()
        task = Task("t1", [], file_dep=['tests/data/dependency1'],
                    doc="task doc", getargs={'a': ('x', 'y')}, verbosity=2,
                    meta={'a': ['b', 'c']})
        cmd = CmdFactory(Info, outstream=output,
                         dep_file=self.dep_manager.name, task_list=[task])
        cmd._execute(['t1'], hide_status=True)
        self.assertIn("t1", output.getvalue())
        self.assertIn("task doc", output.getvalue())
        self.assertIn("- tests/data/dependency1", output.getvalue())
        self.assertIn("verbosity  : 2", output.getvalue())
        self.assertIn("getargs    : {'a': ('x', 'y')}", output.getvalue())
        self.assertIn("meta       : {'a': ['b', 'c']}", output.getvalue())

    def test_invalid_command_args(self):
        output = StringIO()
        task = Task("t1", [], file_dep=['tests/data/dependency1'])
        cmd = CmdFactory(Info, outstream=output,
                         dep_file=self.dep_manager.name, task_list=[task])
        self.assertRaises(InvalidCommand, cmd._execute, [])
        self.assertRaises(InvalidCommand, cmd._execute, ['t1', 't2'])


class TestCmdInfoStatus(DependencyFileMixin, DepManagerMixin, unittest.TestCase):

    def test_execute_status_run(self):
        output = StringIO()
        task = Task("t1", [], file_dep=['tests/data/dependency1'])
        cmd = CmdFactory(Info, outstream=output,
                         dep_file=self.dep_manager.name, task_list=[task],
                         dep_manager=self.dep_manager)
        return_val = cmd._execute(['t1'])
        self.assertIn("t1", output.getvalue())
        self.assertEqual(1, return_val)
        self.assertIn("status", output.getvalue())
        self.assertIn(": run", output.getvalue())
        self.assertIn(" - tests/data/dependency1", output.getvalue())

    def test_hide_execute_status(self):
        output = StringIO()
        task = Task("t1", [], file_dep=['tests/data/dependency1'])
        cmd = CmdFactory(Info, outstream=output,
                         dep_manager=self.dep_manager, task_list=[task])
        return_val = cmd._execute(['t1'], hide_status=True)
        self.assertIn("t1", output.getvalue())
        self.assertEqual(0, return_val)
        self.assertNotIn("status", output.getvalue())
        self.assertNotIn(": run", output.getvalue())

    def test_execute_status_uptodate(self):
        output = StringIO()
        task = Task("t1", [], file_dep=['tests/data/dependency1'])
        cmd = CmdFactory(Info, outstream=output,
                         dep_manager=self.dep_manager, task_list=[task])
        cmd.dep_manager.save_success(task)
        return_val = cmd._execute(['t1'])
        self.assertIn("t1", output.getvalue())
        self.assertEqual(0, return_val)
        self.assertIn(": up-to-date", output.getvalue())


class TestGetReasons(unittest.TestCase):

    def test_get_reasons_str(self):
        reasons = {
            'has_no_dependencies': True,
            'uptodate_false': [('func', 'arg', 'kwarg')],
            'checker_changed': ['foo', 'bar'],
            'missing_target': ['f1', 'f2'],
        }
        got = Info.get_reasons(reasons).splitlines()
        self.assertEqual(7, len(got))
        self.assertEqual(' * The task has no dependencies.', got[0])
        self.assertEqual(' * The following uptodate objects evaluate to false:', got[1])
        self.assertEqual('    - func (args=arg, kwargs=kwarg)', got[2])
        self.assertEqual(' * The file_dep checker changed from foo to bar.', got[3])
        self.assertEqual(' * The following targets do not exist:', got[4])
        self.assertEqual('    - f1', got[5])
        self.assertEqual('    - f2', got[6])


class TestCmdInfoCheckerChangedSideEffect(DependencyFileMixin, DepManagerMixin, unittest.TestCase):
    """cmd_info 中 checker_changed 的副作用测试"""

    def test_info_command_persists_checker_changed_remove(self):
        """info 命令使用 get_log=True，checker_changed 会触发 remove 并持久化"""
        from io import StringIO
        from doit.cmd_info import Info
        from doit.dependency import TimestampChecker, MD5Checker
        from tests.support import CmdFactory

        dep_path = self.dependency1
        task = Task("t1", None, [dep_path])

        cmd = CmdFactory(Info, outstream=StringIO(),
                         dep_manager=self.dep_manager, task_list=[task])

        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(task)

        self.assertTrue(self.dep_manager._in("t1"))

        self.dep_manager.checker = MD5Checker()

        return_val = cmd._execute(['t1'])

        self.assertEqual(1, return_val)
        self.assertFalse(self.dep_manager._in("t1"))

    def test_info_command_after_close_reopen(self):
        """info 命令的副作用在 close/reopen 后仍然生效"""
        from io import StringIO
        from doit.cmd_info import Info
        from doit.dependency import TimestampChecker, MD5Checker, Dependency, DbmDB
        from tests.support import CmdFactory

        dep_path = self.dependency1
        task = Task("t1", None, [dep_path])

        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(task)
        self.dep_manager.close()

        reopened = Dependency(DbmDB, self.dep_manager.name)
        self.assertTrue(reopened._in("t1"))

        output = StringIO()
        cmd = CmdFactory(Info, outstream=output,
                         dep_manager=reopened, task_list=[task])

        reopened.checker = MD5Checker()
        cmd._execute(['t1'])

        self.assertFalse(reopened._in("t1"))
        reopened.close()

        reopened2 = Dependency(DbmDB, self.dep_manager.name)
        self.assertFalse(reopened2._in("t1"))
        reopened2.close()
