"""Strict tests for doit.cmd_explain - verify DB is not polluted and reasons come from get_status."""
import os
import time
import tempfile
import shutil
from io import StringIO
import unittest
import copy

from doit.task import Task
from doit.cmd_run import Run
from doit.cmd_explain import Explain
from doit.dependency import DbmDB, Dependency, MD5Checker, DependencyStatus
from tests.support import (
    CmdFactory, DepfileNameMixin, DependencyFileMixin,
    get_abspath, remove_all_db
)


class TestCmdExplainStrict(DepfileNameMixin, DependencyFileMixin, unittest.TestCase):
    """Strict tests for explain command."""

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

    def _get_db_snapshot(self, dep_manager, task_names):
        """Get a snapshot of DB state for given tasks."""
        snapshot = {}
        for task_name in task_names:
            if dep_manager._in(task_name):
                snapshot[task_name] = {
                    'checker': dep_manager._get(task_name, 'checker:'),
                    'deps': dep_manager._get(task_name, 'deps:'),
                    'values': dep_manager._get(task_name, '_values_:'),
                    'file_deps': {},
                }
                deps = dep_manager._get(task_name, 'deps:')
                if deps:
                    for dep in deps:
                        snapshot[task_name]['file_deps'][dep] = dep_manager._get(task_name, dep)
        return snapshot

    def _compare_snapshots(self, snapshot1, snapshot2, task_names):
        """Compare two DB snapshots and return differences."""
        differences = []
        for task_name in task_names:
            in1 = task_name in snapshot1
            in2 = task_name in snapshot2
            if in1 != in2:
                differences.append(
                    f"Task '{task_name}' existence changed: was={in1}, now={in2}"
                )
                continue
            if not in1:
                continue
            
            s1 = snapshot1[task_name]
            s2 = snapshot2[task_name]
            
            if s1['checker'] != s2['checker']:
                differences.append(
                    f"Task '{task_name}' checker changed: {s1['checker']} -> {s2['checker']}"
                )
            if s1['deps'] != s2['deps']:
                differences.append(
                    f"Task '{task_name}' deps changed: {s1['deps']} -> {s2['deps']}"
                )
            if s1['values'] != s2['values']:
                differences.append(
                    f"Task '{task_name}' values changed: {s1['values']} -> {s2['values']}"
                )
            
            all_deps = set(s1['file_deps'].keys()) | set(s2['file_deps'].keys())
            for dep in all_deps:
                v1 = s1['file_deps'].get(dep)
                v2 = s2['file_deps'].get(dep)
                if v1 != v2:
                    differences.append(
                        f"Task '{task_name}' file_dep '{dep}' changed: {v1} -> {v2}"
                    )
        return differences

    def test_explain_does_not_pollute_db_strict(self):
        """STRICT TEST: explain must NOT add, remove, or update ANY dependency records.
        
        Steps:
        1. Run task successfully to create dependency records
        2. Take DB snapshot (including all file_dep states)
        3. Run explain
        4. Take another DB snapshot
        5. Compare snapshots - they MUST be identical
        """
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Test task")
        ]
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        task_in_db_before = dep_manager1._in("t1")
        self.assertTrue(task_in_db_before, "Task t1 should be in DB after run")
        
        snapshot_before = self._get_db_snapshot(dep_manager1, ["t1"])
        dep_manager1.close()
        
        explain_result, _ = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(0, explain_result)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        task_in_db_after = dep_manager2._in("t1")
        snapshot_after = self._get_db_snapshot(dep_manager2, ["t1"])
        dep_manager2.close()
        
        self.assertEqual(
            task_in_db_before, task_in_db_after,
            f"Task existence changed: was={task_in_db_before}, now={task_in_db_after}"
        )
        
        differences = self._compare_snapshots(snapshot_before, snapshot_after, ["t1"])
        
        if differences:
            self.fail(f"DB was polluted by explain:\n" + "\n".join(differences))
        
        self.assertEqual(
            snapshot_before["t1"]["checker"],
            snapshot_after["t1"]["checker"],
            "Checker should not change"
        )
        self.assertEqual(
            snapshot_before["t1"]["deps"],
            snapshot_after["t1"]["deps"],
            "Deps list should not change"
        )

    def test_explain_does_not_create_new_records(self):
        """STRICT TEST: explain must NOT create new dependency records for un-run tasks.
        
        Steps:
        1. Verify task is NOT in DB initially
        2. Run explain
        3. Verify task is STILL NOT in DB
        """
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Test task")
        ]
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        self.assertFalse(dep_manager1._in("t1"), "Task t1 should NOT be in DB initially")
        dep_manager1.close()
        
        explain_result, _ = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, explain_result)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        task_in_db_after = dep_manager2._in("t1")
        dep_manager2.close()
        
        self.assertFalse(
            task_in_db_after,
            "Task t1 should STILL NOT be in DB after explain - "
            "explain must not create new dependency records!"
        )

    def test_explain_reasons_come_from_get_status_log_uptodate_false(self):
        """VERIFY: explain's 'uptodate_false' reason MUST come from Dependency.get_status(..., get_log=True).
        
        Steps:
        1. Create task with uptodate callable that returns False
        2. Directly call Dependency.get_status(..., get_log=True) and capture reasons
        3. Run explain and capture output
        4. Verify explain output contains the same reasons from get_status
        """
        call_count = [0]
        def always_false():
            call_count[0] += 1
            return False
        
        task_list = [
            Task(
                "t1", 
                [""], 
                uptodate=[always_false],
                doc="Task with uptodate returning false"
            )
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
        
        call_count[0] = 0
        status = dep_manager.get_status(task_list[0], tasks_dict, get_log=True)
        
        self.assertEqual('run', status.status)
        self.assertIn('uptodate_false', status.reasons)
        self.assertEqual(1, len(status.reasons['uptodate_false']))
        
        expected_callable = status.reasons['uptodate_false'][0][0]
        self.assertIs(expected_callable, always_false)
        
        snapshot_before = self._get_db_snapshot(dep_manager, ["t1"])
        dep_manager.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(1, explain_result)
        self.assertIn("uptodate", explain_output.lower())
        self.assertIn("false", explain_output.lower())
        self.assertIn("Status: RUN", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        snapshot_after = self._get_db_snapshot(dep_manager2, ["t1"])
        differences = self._compare_snapshots(snapshot_before, snapshot_after, ["t1"])
        dep_manager2.close()
        
        if differences:
            self.fail(f"DB was polluted during explain:\n" + "\n".join(differences))

    def test_explain_reasons_come_from_get_status_log_file_dep_changed(self):
        """VERIFY: explain's 'changed_file_dep' reason MUST come from Dependency.get_status(..., get_log=True).
        
        Steps:
        1. Run task to create dependency records
        2. Modify file_dep
        3. Directly call Dependency.get_status(..., get_log=True) and capture reasons
        4. Run explain and capture output
        5. Verify explain output contains the same reasons from get_status
        """
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Task with file_dep")
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        time.sleep(0.1)
        with open(self.dependency1, "w") as f:
            f.write("modified content " + str(time.time()))
        
        dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
        status = dep_manager.get_status(task_list[0], tasks_dict, get_log=True)
        
        self.assertEqual('run', status.status)
        self.assertIn('changed_file_dep', status.reasons)
        self.assertIn(self.dependency1, status.reasons['changed_file_dep'])
        
        snapshot_before = self._get_db_snapshot(dep_manager, ["t1"])
        dep_manager.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        
        self.assertEqual(1, explain_result)
        self.assertIn("Changed dependency files", explain_output)
        self.assertIn(self.dependency1, explain_output)
        self.assertIn("Status: RUN", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        snapshot_after = self._get_db_snapshot(dep_manager2, ["t1"])
        differences = self._compare_snapshots(snapshot_before, snapshot_after, ["t1"])
        dep_manager2.close()
        
        if differences:
            self.fail(f"DB was polluted during explain:\n" + "\n".join(differences))

    def test_explain_reasons_come_from_get_status_log_missing_target(self):
        """VERIFY: explain's 'missing_target' reason MUST come from Dependency.get_status(..., get_log=True).
        
        Steps:
        1. Run task with target to create dependency records
        2. Delete target
        3. Directly call Dependency.get_status(..., get_log=True) and capture reasons
        4. Run explain and capture output
        5. Verify explain output contains the same reasons from get_status
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "output.txt")
            
            task_list = [
                Task(
                    "t1", 
                    ["echo hello > " + target_path], 
                    targets=[target_path],
                    file_dep=[self.dependency1],
                    doc="Task with target"
                )
            ]
            tasks_dict = {t.name: t for t in task_list}
            
            run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
            self.assertEqual(0, run_result)
            self.assertTrue(os.path.exists(target_path))
            
            os.remove(target_path)
            self.assertFalse(os.path.exists(target_path))
            
            dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
            status = dep_manager.get_status(task_list[0], tasks_dict, get_log=True)
            
            self.assertEqual('run', status.status)
            self.assertIn('missing_target', status.reasons)
            self.assertIn(target_path, status.reasons['missing_target'])
            
            snapshot_before = self._get_db_snapshot(dep_manager, ["t1"])
            dep_manager.close()
            
            explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
            
            self.assertEqual(1, explain_result)
            self.assertIn("Missing target", explain_output)
            self.assertIn(target_path, explain_output)
            self.assertIn("Status: RUN", explain_output)
            
            dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
            snapshot_after = self._get_db_snapshot(dep_manager2, ["t1"])
            differences = self._compare_snapshots(snapshot_before, snapshot_after, ["t1"])
            dep_manager2.close()
            
            if differences:
                self.fail(f"DB was polluted during explain:\n" + "\n".join(differences))

    def test_explain_task_dep_logic_uses_get_status(self):
        """VERIFY: explain's task_dep logic MUST use Dependency.get_status for each dependency.
        
        Steps:
        1. Create task chain: t2 depends on t1
        2. Both have no deps, so both would run
        3. Verify that explain correctly identifies that t2 needs to run 
           because t1 needs to run
        4. Verify DB is not polluted
        """
        task_list = [
            Task("t1", [""], doc="Base task - no dependencies"),
            Task("t2", [""], task_dep=["t1"], doc="Depends on t1")
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
        
        status_t1 = dep_manager.get_status(task_list[0], tasks_dict, get_log=True)
        self.assertEqual('run', status_t1.status)
        self.assertIn('has_no_dependencies', status_t1.reasons)
        
        status_t2 = dep_manager.get_status(task_list[1], tasks_dict, get_log=True)
        self.assertEqual('run', status_t2.status)
        self.assertIn('has_no_dependencies', status_t2.reasons)
        
        snapshot_before = self._get_db_snapshot(dep_manager, ["t1", "t2"])
        dep_manager.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t2"])
        
        self.assertEqual(1, explain_result)
        self.assertIn("Task: t1", explain_output)
        self.assertIn("Task: t2", explain_output)
        self.assertIn("Task Dependencies", explain_output)
        self.assertIn("t1", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        snapshot_after = self._get_db_snapshot(dep_manager2, ["t1", "t2"])
        
        for task_name in ["t1", "t2"]:
            in_before = task_name in snapshot_before
            in_after = task_name in snapshot_after
            self.assertEqual(
                in_before, in_after,
                f"Task '{task_name}' existence changed in DB after explain: "
                f"was={in_before}, now={in_after}. "
                f"explain must not create/remove dependency records!"
            )
        
        dep_manager2.close()

    def test_explain_compare_direct_get_status_vs_explain_output(self):
        """COMPREHENSIVE: Directly compare get_status reasons with explain output.
        
        This test verifies that explain's output for each reason type 
        matches what Dependency.get_status returns.
        """
        test_cases = [
            {
                'name': 'uptodate_false',
                'task': Task(
                    "t1", 
                    [""], 
                    uptodate=[lambda: False],
                    doc="uptodate returns false"
                ),
                'expected_reason_key': 'uptodate_false',
                'expected_in_output': ['uptodate', 'false'],
            },
            {
                'name': 'has_no_dependencies',
                'task': Task(
                    "t1", 
                    [""], 
                    doc="no dependencies"
                ),
                'expected_reason_key': 'has_no_dependencies',
                'expected_in_output': ['no dependencies'],
            },
        ]
        
        for tc in test_cases:
            with self.subTest(test_case=tc['name']):
                task_list = [tc['task']]
                tasks_dict = {t.name: t for t in task_list}
                
                dep_manager = Dependency(DbmDB, self.depfile_name, MD5Checker)
                status = dep_manager.get_status(task_list[0], tasks_dict, get_log=True)
                
                self.assertIn(
                    tc['expected_reason_key'], 
                    status.reasons,
                    f"get_status should return reason key '{tc['expected_reason_key']}'"
                )
                
                snapshot_before = self._get_db_snapshot(dep_manager, ["t1"])
                dep_manager.close()
                
                explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
                
                for expected_text in tc['expected_in_output']:
                    self.assertIn(
                        expected_text, 
                        explain_output.lower(),
                        f"explain output should contain '{expected_text}'"
                    )
                
                dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
                snapshot_after = self._get_db_snapshot(dep_manager2, ["t1"])
                differences = self._compare_snapshots(snapshot_before, snapshot_after, ["t1"])
                dep_manager2.close()
                
                if differences:
                    self.fail(
                        f"Test case '{tc['name']}': DB was polluted:\n" 
                        + "\n".join(differences)
                    )
