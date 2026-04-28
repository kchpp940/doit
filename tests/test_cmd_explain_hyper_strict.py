"""Hyper-strict tests for doit.cmd_explain -逐项对比验证."""
import os
import time
import tempfile
import shutil
from io import StringIO
import unittest
import json

from doit.task import Task
from doit.cmd_run import Run
from doit.cmd_explain import Explain
from doit.dependency import DbmDB, Dependency, MD5Checker, DependencyStatus
from tests.support import (
    CmdFactory, DepfileNameMixin, DependencyFileMixin,
)


class TestCmdExplainHyperStrict(DepfileNameMixin, DependencyFileMixin, unittest.TestCase):
    """Hyper-strict tests for explain command with item-by-item comparison."""

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

    def _get_full_db_state(self, dep_manager):
        """Get complete DB state as a JSON-serializable dict."""
        state = {}
        
        for attr_name in dir(dep_manager.backend):
            if attr_name.startswith('_'):
                continue
        
        if hasattr(dep_manager.backend, '_db'):
            db_data = dep_manager.backend._db
            if isinstance(db_data, dict):
                for task_id, task_data in db_data.items():
                    state[task_id] = {}
                    if isinstance(task_data, dict):
                        for key, value in task_data.items():
                            try:
                                json.dumps({key: value})
                                state[task_id][key] = value
                            except (TypeError, ValueError):
                                state[task_id][key] = str(value)
        
        if hasattr(dep_manager.backend, '_cache'):
            for task_id, task_data in dep_manager.backend._cache.items():
                if task_id not in state:
                    state[task_id] = {}
                for key, value in task_data.items():
                    try:
                        json.dumps({key: value})
                        state[task_id][key] = value
                    except (TypeError, ValueError):
                        state[task_id][key] = str(value)
        
        return state

    def _get_task_db_state(self, dep_manager, task_name):
        """Get DB state for a specific task with all details."""
        state = {
            'exists': dep_manager._in(task_name),
            'checker': None,
            'deps': None,
            'values': None,
            'result': None,
            'file_deps': {},
        }
        
        if state['exists']:
            state['checker'] = dep_manager._get(task_name, 'checker:')
            state['deps'] = dep_manager._get(task_name, 'deps:')
            state['values'] = dep_manager._get(task_name, '_values_:')
            state['result'] = dep_manager._get(task_name, 'result:')
            
            deps = state['deps']
            if deps:
                for dep in deps:
                    state['file_deps'][dep] = dep_manager._get(task_name, dep)
        
        return state

    def _assert_task_db_state_unchanged(self, state_before, state_after, task_name, context=""):
        """Assert that DB state for a task is completely unchanged."""
        self.assertEqual(
            state_before['exists'],
            state_after['exists'],
            f"{context}: Task '{task_name}' existence changed: "
            f"was={state_before['exists']}, now={state_after['exists']}"
        )
        
        if not state_before['exists']:
            return
        
        self.assertEqual(
            state_before['checker'],
            state_after['checker'],
            f"{context}: Task '{task_name}' checker changed: "
            f"was={state_before['checker']}, now={state_after['checker']}"
        )
        
        self.assertEqual(
            state_before['deps'],
            state_after['deps'],
            f"{context}: Task '{task_name}' deps changed: "
            f"was={state_before['deps']}, now={state_after['deps']}"
        )
        
        self.assertEqual(
            state_before['values'],
            state_after['values'],
            f"{context}: Task '{task_name}' values changed: "
            f"was={state_before['values']}, now={state_after['values']}"
        )
        
        self.assertEqual(
            state_before['result'],
            state_after['result'],
            f"{context}: Task '{task_name}' result changed: "
            f"was={state_before['result']}, now={state_after['result']}"
        )
        
        self.assertEqual(
            state_before['file_deps'],
            state_after['file_deps'],
            f"{context}: Task '{task_name}' file_deps changed: "
            f"was={state_before['file_deps']}, now={state_after['file_deps']}"
        )

    def _assert_reasons_match_output(self, reasons, output, reason_key, expected_phrases):
        """Assert that reasons from get_status match explain output.
        
        Args:
            reasons: dict from DependencyStatus.reasons
            output: string from explain command output
            reason_key: key to check in reasons dict
            expected_phrases: list of phrases that should appear in output
        """
        self.assertIn(
            reason_key,
            reasons,
            f"get_status should have returned reason key '{reason_key}'. "
            f"Available keys: {list(reasons.keys())}"
        )
        
        for phrase in expected_phrases:
            self.assertIn(
                phrase.lower(),
                output.lower(),
                f"explain output should contain phrase '{phrase}'. "
                f"Output:\n{output}"
            )

    def test_db_state_unchanged_after_explain_for_up_to_date_task(self):
        """HYPER-STRICT: DB state must be COMPLETELY unchanged after explain.
        
        Scenario:
        1. Run task to create complete DB state (checker, deps, values, file_dep states)
        2. Get detailed DB state snapshot
        3. Run explain
        4. Get another detailed DB state snapshot
        5. Compare EVERY FIELD: exists, checker, deps, values, result, file_deps
        """
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Test task")
        ]
        
        run_result, _ = self._run_cmd(Run, task_list, sel_tasks=["t1"])
        self.assertEqual(0, run_result)
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_before = self._get_task_db_state(dep_manager1, "t1")
        
        self.assertTrue(state_before['exists'], "Task should exist in DB after run")
        self.assertIsNotNone(state_before['checker'], "checker should be set")
        self.assertIsNotNone(state_before['deps'], "deps should be set")
        self.assertGreater(len(state_before['file_deps']), 0, "file_deps should be present")
        
        dep_manager1.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(0, explain_result)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after = self._get_task_db_state(dep_manager2, "t1")
        dep_manager2.close()
        
        self._assert_task_db_state_unchanged(
            state_before, state_after, "t1",
            context="After explain command on up-to-date task"
        )

    def test_db_state_unchanged_for_never_run_task(self):
        """HYPER-STRICT: Never-run task should NOT be added to DB after explain.
        
        Scenario:
        1. Verify task does NOT exist in DB initially
        2. Run explain
        3. Verify task STILL does NOT exist in DB
        4. Verify no other tasks were created
        """
        task_list = [
            Task("t1", [""], file_dep=[self.dependency1], doc="Test task")
        ]
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_before = self._get_task_db_state(dep_manager1, "t1")
        full_state_before = self._get_full_db_state(dep_manager1)
        dep_manager1.close()
        
        self.assertFalse(state_before['exists'], "Task should NOT exist in DB initially")
        
        explain_result, _ = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, explain_result)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after = self._get_task_db_state(dep_manager2, "t1")
        full_state_after = self._get_full_db_state(dep_manager2)
        dep_manager2.close()
        
        self.assertFalse(
            state_after['exists'],
            "Task should STILL NOT exist in DB after explain. "
            "explain must NOT create new dependency records!"
        )
        
        self.assertEqual(
            full_state_before,
            full_state_after,
            f"Full DB state changed!\n"
            f"Before: {full_state_before}\n"
            f"After: {full_state_after}"
        )

    def test_explain_reasons_match_get_status_uptodate_false(self):
        """HYPER-STRICT: explain 'uptodate_false' reason must match get_status.
        
        Verification:
        1. Directly call get_status(..., get_log=True)
        2. Verify 'uptodate_false' is in reasons dict
        3. Run explain
        4. Verify explain output contains matching information
        5. Verify DB unchanged
        """
        def always_false():
            return False
        
        task_list = [
            Task("t1", [""], uptodate=[always_false], doc="uptodate returns false")
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_before = self._get_task_db_state(dep_manager1, "t1")
        
        status = dep_manager1.get_status(task_list[0], tasks_dict, get_log=True)
        
        self.assertEqual('run', status.status)
        self._assert_reasons_match_output(
            status.reasons, "", 'uptodate_false', []
        )
        
        dep_manager1.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, explain_result)
        
        self._assert_reasons_match_output(
            status.reasons, explain_output, 'uptodate_false',
            ['uptodate', 'false']
        )
        self.assertIn("Status: RUN", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after = self._get_task_db_state(dep_manager2, "t1")
        dep_manager2.close()
        
        self._assert_task_db_state_unchanged(
            state_before, state_after, "t1",
            context="After explain with uptodate_false"
        )

    def test_explain_reasons_match_get_status_file_dep_changed(self):
        """HYPER-STRICT: explain 'changed_file_dep' reason must match get_status.
        
        Verification:
        1. Run task to create DB state
        2. Modify file_dep
        3. Directly call get_status(..., get_log=True)
        4. Verify 'changed_file_dep' is in reasons dict
        5. Run explain
        6. Verify explain output contains matching information
        7. Verify DB unchanged
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
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_before = self._get_task_db_state(dep_manager1, "t1")
        
        status = dep_manager1.get_status(task_list[0], tasks_dict, get_log=True)
        
        self.assertEqual('run', status.status)
        self._assert_reasons_match_output(
            status.reasons, "", 'changed_file_dep', []
        )
        self.assertIn(self.dependency1, status.reasons['changed_file_dep'])
        
        dep_manager1.close()
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, explain_result)
        
        self._assert_reasons_match_output(
            status.reasons, explain_output, 'changed_file_dep',
            ['Changed', 'dependency', self.dependency1]
        )
        self.assertIn("Status: RUN", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after = self._get_task_db_state(dep_manager2, "t1")
        dep_manager2.close()
        
        self._assert_task_db_state_unchanged(
            state_before, state_after, "t1",
            context="After explain with changed_file_dep"
        )

    def test_explain_reasons_match_get_status_missing_target(self):
        """HYPER-STRICT: explain 'missing_target' reason must match get_status.
        
        Verification:
        1. Run task with target to create DB state
        2. Delete target
        3. Directly call get_status(..., get_log=True)
        4. Verify 'missing_target' is in reasons dict
        5. Run explain
        6. Verify explain output contains matching information
        7. Verify DB unchanged
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
            
            dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
            state_before = self._get_task_db_state(dep_manager1, "t1")
            
            status = dep_manager1.get_status(task_list[0], tasks_dict, get_log=True)
            
            self.assertEqual('run', status.status)
            self._assert_reasons_match_output(
                status.reasons, "", 'missing_target', []
            )
            self.assertIn(target_path, status.reasons['missing_target'])
            
            dep_manager1.close()
            
            explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
            self.assertEqual(1, explain_result)
            
            self._assert_reasons_match_output(
                status.reasons, explain_output, 'missing_target',
                ['Missing', 'target', target_path]
            )
            self.assertIn("Status: RUN", explain_output)
            
            dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
            state_after = self._get_task_db_state(dep_manager2, "t1")
            dep_manager2.close()
            
            self._assert_task_db_state_unchanged(
                state_before, state_after, "t1",
                context="After explain with missing_target"
            )

    def test_explain_task_dep_chain_reasons(self):
        """HYPER-STRICT: task_dep chain must be correctly analyzed.
        
        Verification:
        1. Create task chain: t2 depends on t1
        2. Both have no deps, so both would return 'has_no_dependencies'
        3. Directly call get_status for both
        4. Run explain and verify output shows dependency chain
        5. Verify DB unchanged
        """
        task_list = [
            Task("t1", [""], doc="Base task - no dependencies"),
            Task("t2", [""], task_dep=["t1"], doc="Depends on t1")
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        
        state_t1_before = self._get_task_db_state(dep_manager1, "t1")
        state_t2_before = self._get_task_db_state(dep_manager1, "t2")
        
        status_t1 = dep_manager1.get_status(task_list[0], tasks_dict, get_log=True)
        status_t2 = dep_manager1.get_status(task_list[1], tasks_dict, get_log=True)
        
        self.assertEqual('run', status_t1.status)
        self.assertEqual('run', status_t2.status)
        self.assertIn('has_no_dependencies', status_t1.reasons)
        self.assertIn('has_no_dependencies', status_t2.reasons)
        
        dep_manager1.close()
        
        self.assertFalse(state_t1_before['exists'], "t1 should NOT exist in DB initially")
        self.assertFalse(state_t2_before['exists'], "t2 should NOT exist in DB initially")
        
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t2"])
        self.assertEqual(1, explain_result)
        
        self.assertIn("Task: t1", explain_output)
        self.assertIn("Task: t2", explain_output)
        self.assertIn("Task Dependencies", explain_output)
        self.assertIn("t1", explain_output)
        
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_t1_after = self._get_task_db_state(dep_manager2, "t1")
        state_t2_after = self._get_task_db_state(dep_manager2, "t2")
        dep_manager2.close()
        
        self.assertFalse(
            state_t1_after['exists'],
            "t1 should STILL NOT exist in DB after explain. "
            "explain must NOT create new dependency records!"
        )
        self.assertFalse(
            state_t2_after['exists'],
            "t2 should STILL NOT exist in DB after explain. "
            "explain must NOT create new dependency records!"
        )

    def test_full_scenario_uptodate_false_with_db_pollution_check(self):
        """COMPREHENSIVE: uptodate_false with full DB pollution check.
        
        This test verifies:
        1. get_status returns 'uptodate_false' reason
        2. explain output contains matching information
        3. DB state is COMPLETELY unchanged (exists, checker, deps, values, file_deps)
        """
        call_tracker = {'count': 0}
        
        def tracked_false():
            call_tracker['count'] += 1
            return False
        
        task_list = [
            Task("t1", [""], uptodate=[tracked_false], doc="uptodate returns false")
        ]
        tasks_dict = {t.name: t for t in task_list}
        
        dep_manager1 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_before = self._get_task_db_state(dep_manager1, "t1")
        self.assertFalse(state_before['exists'], "Task should NOT exist initially")
        dep_manager1.close()
        
        call_tracker['count'] = 0
        dep_manager2 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        status = dep_manager2.get_status(task_list[0], tasks_dict, get_log=True)
        dep_manager2.close()
        
        self.assertEqual(1, call_tracker['count'], "uptodate should be called once by get_status")
        self.assertEqual('run', status.status)
        self.assertIn('uptodate_false', status.reasons)
        
        dep_manager3 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after_get_status = self._get_task_db_state(dep_manager3, "t1")
        dep_manager3.close()
        
        self._assert_task_db_state_unchanged(
            state_before, state_after_get_status, "t1",
            context="After get_status call (should be read-only)"
        )
        
        call_tracker['count'] = 0
        explain_result, explain_output = self._run_cmd(Explain, task_list, sel_tasks=["t1"])
        self.assertEqual(1, explain_result)
        
        self.assertIn("uptodate", explain_output.lower())
        self.assertIn("false", explain_output.lower())
        self.assertIn("Status: RUN", explain_output)
        
        dep_manager4 = Dependency(DbmDB, self.depfile_name, MD5Checker)
        state_after_explain = self._get_task_db_state(dep_manager4, "t1")
        dep_manager4.close()
        
        self._assert_task_db_state_unchanged(
            state_before, state_after_explain, "t1",
            context="After explain command (MUST be read-only)"
        )
