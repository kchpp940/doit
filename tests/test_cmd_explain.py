"""Tests for doit.cmd_explain - explain why tasks would run or be skipped."""
import os
import time
import tempfile
import shutil
from io import StringIO
import unittest

from doit.task import Task
from doit.cmd_run import Run
from doit.cmd_explain import Explain
from tests.support import (
    CmdFactory, DepfileNameMixin, DependencyFileMixin,
    get_abspath, remove_all_db
)


class TestCmdExplain(DepfileNameMixin, DependencyFileMixin, unittest.TestCase):
    """Test explain command."""

    def _run_cmd(self, cmd_cls, task_list, sel_tasks=None, **kwargs):
        """Helper to run a command and return output."""
        output = StringIO()
        cmd = CmdFactory(
            cmd_cls,
            backend='dbm',
            dep_file=self.depfile_name,
            task_list=task_list,
            sel_tasks=sel_tasks,
        )
        pos_args = sel_tasks if sel_tasks else []
        result = cmd._execute(output, pos_args, **kwargs)
        return result, output.getvalue()

    def test_explain_first_run_no_deps(self):
        """Test: explain before first run - task has no dependencies."""
        task_list = [
            Task("t1", [""], doc="Task with no dependencies")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: t1", output)
        self.assertIn("Status: RUN", output)
        self.assertIn("no dependencies", output.lower())

    def test_explain_first_run_with_file_dep(self):
        """Test: explain before first run - task has file_dep but no saved state."""
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Task with file_dep")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: t1", output)
        self.assertIn("Status: RUN", output)

    def test_explain_after_run(self):
        """Test: explain after successful run - task should be up-to-date."""
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Task with file_dep")
        ]
        
        run_result, run_output = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(0, explain_result)
        self.assertIn("Task: t1", explain_output)
        self.assertIn("Status: UP-TO-DATE", explain_output)
        self.assertIn("up-to-date", explain_output.lower())

    def test_explain_after_modifying_file_dep(self):
        """Test: explain after modifying file_dep - task should run."""
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Task with file_dep")
        ]
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        time.sleep(0.1)
        with open(self.dependency1, "w") as f:
            f.write("modified content" + str(time.time()))
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(1, explain_result)
        self.assertIn("Task: t1", explain_output)
        self.assertIn("Status: RUN", explain_output)
        self.assertIn("Changed dependency files", explain_output)
        self.assertIn(self.dependency1, explain_output)

    def test_explain_missing_target(self):
        """Test: explain when target is missing - task should run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "output.txt")
            
            task_list = [
                Task(
                    "t1", 
                    ["echo hello > " + target_path], 
                    targets=[target_path],
                    doc="Task with target"
                )
            ]
            
            run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
            self.assertEqual(0, run_result)
            self.assertTrue(os.path.exists(target_path))
            
            os.remove(target_path)
            self.assertFalse(os.path.exists(target_path))
            
            explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
            
            self.assertEqual(1, explain_result)
            self.assertIn("Task: t1", explain_output)
            self.assertIn("Status: RUN", explain_output)
            self.assertIn("Missing target", explain_output)
            self.assertIn(target_path, explain_output)

    def test_explain_missing_file_dep(self):
        """Test: explain when file_dep is missing - should show error."""
        missing_path = "/nonexistent/file_that_does_not_exist.txt"
        task_list = [
            Task("t1", [""], file_dep=[missing_path], doc="Task with missing file_dep")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertIn("Task: t1", output)
        self.assertIn("missing", output.lower())

    def test_explain_uptodate_false(self):
        """Test: explain when uptodate callable returns false."""
        def always_false():
            return False
        
        def always_true():
            return True
        
        task_list = [
            Task(
                "t1", 
                [""], 
                uptodate=[always_false],
                doc="Task with uptodate returning false"
            ),
            Task(
                "t2", 
                [""], 
                uptodate=[always_true],
                doc="Task with uptodate returning true (should be up-to-date)"
            )
        ]
        
        result1, output1 = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, result1)
        self.assertIn("uptodate", output1.lower())
        self.assertIn("false", output1.lower())
        
        result2, output2 = self._run_cmd(Explain, task_list, sel_tasks=["t2"])
        self.assertEqual(0, result2)
        self.assertIn("Status: UP-TO-DATE", output2)

    def test_explain_task_dep(self):
        """Test: explain task_dep chain - if dep needs to run, parent should too."""
        task_list = [
            Task("t1", [""], doc="Base task - no dependencies"),
            Task("t2", [""], task_dep=["t1"], doc="Depends on t1")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["t2"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: t1", output)
        self.assertIn("Task: t2", output)
        
        self.assertIn("no dependencies", output.lower())
        self.assertIn("Task Dependencies", output)
        self.assertIn("t1", output)
        self.assertIn("run", output.lower())

    def test_explain_task_dep_up_to_date(self):
        """Test: explain when task_dep is up-to-date."""
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Base task"),
            Task("t2", [""], task_dep=["t1"], doc="Depends on t1")
        ]
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(0, explain_result)
        self.assertIn("Status: UP-TO-DATE", explain_output)

    def test_explain_multiple_tasks(self):
        """Test: explain multiple tasks at once."""
        task_list = [
            Task("t1", [""], doc="Task 1"),
            Task("t2", [""], doc="Task 2"),
            Task("t3", [""], doc="Task 3")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["t1", "t2"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: t1", output)
        self.assertIn("Task: t2", output)
        self.assertIn("Status: RUN", output)

    def test_explain_wildcard_tasks(self):
        """Test: explain using wildcard task selection."""
        task_list = [
            Task("build:debug", [""], doc="Debug build"),
            Task("build:release", [""], doc="Release build"),
            Task("test", [""], doc="Test task")
        ]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["build:*"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: build:debug", output)
        self.assertIn("Task: build:release", output)
        self.assertNotIn("Task: test", output)

    def test_explain_no_tasks(self):
        """Test: explain with no tasks defined."""
        result, output = self._run_cmd(Explain, [], sel_tasks=[])
        
        self.assertEqual(0, result)
        self.assertIn("No tasks", output)

    def test_explain_does_not_pollute_dependency_db(self):
        """Test: explain should NOT modify the dependency database."""
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Test task")
        ]
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        from doit.dependency import DbmDB, Dependency, MD5Checker
        dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
        self.assertTrue(dep_manager._in("t1"))
        original_checker = dep_manager._get("t1", "checker:")
        original_deps = dep_manager._get("t1", "deps:")
        dep_manager.close()
        
        explain_result, _ = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(0, explain_result)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        self.assertTrue(dep_manager2._in("t1"))
        self.assertEqual(original_checker, dep_manager2._get("t1", "checker:"))
        self.assertEqual(original_deps, dep_manager2._get("t1", "deps:"))
        dep_manager2.close()

    def test_explain_subtasks(self):
        """Test: explain with group tasks and subtasks."""
        task_list = [
            Task("group", None, doc="Group task", has_subtask=True),
            Task("group:a", [""], doc="Subtask A", subtask_of="group"),
            Task("group:b", [""], doc="Subtask B", subtask_of="group")
        ]
        task_list[0].task_dep = ["group:a", "group:b"]
        
        result, output = self._run_cmd(Explain, task_list, sel_tasks=["group"])
        
        self.assertEqual(1, result)
        self.assertIn("Task: group:a", output)
        self.assertIn("Task: group:b", output)
        self.assertIn("Task: group", output)

    def test_explain_nonexistent_task(self):
        """Test: explain with nonexistent task - should raise error."""
        from doit.exceptions import InvalidCommand
        
        task_list = [
            Task("t1", [""], doc="Existing task")
        ]
        
        output = StringIO()
        cmd = CmdFactory(
            Explain,
            backend='dbm',
            dep_file=self.depfile_name,
            task_list=task_list,
            sel_tasks=["nonexistent_task"],
        )
        
        self.assertRaises(InvalidCommand, cmd._execute, output, ["nonexistent_task"])

    def test_explain_with_targets(self):
        """Test: explain by specifying target instead of task name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "output.txt")
            
            task_list = [
                Task(
                    "t1", 
                    ["echo hello"], 
                    targets=[target_path],
                    doc="Task with target"
                )
            ]
            
            result, output = self._run_cmd(Explain, task_list, sel_tasks=[target_path])
            
            self.assertEqual(1, result)
            self.assertIn("Task: t1", output)
