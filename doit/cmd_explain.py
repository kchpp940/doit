"""command doit explain - explain why tasks would run or be skipped"""

import sys
import codecs
from collections import deque
from .cmd_base import DoitCmdBase, check_tasks_exist, tasks_and_deps_iter
from .control import TaskControl
from .exceptions import InvalidCommand


opt_outfile = {
    'name': 'outfile',
    'short': 'o',
    'long': 'output-file',
    'type': str,
    'default': sys.stdout,
    'help': "write output into file [default: stdout]"
}


class Explain(DoitCmdBase):
    """command doit explain"""

    doc_purpose = "explain why tasks would run or be skipped"
    doc_usage = "[TASK/TARGET...]"
    doc_description = """
Show why each task would be executed or skipped, without actually running
any actions or modifying the dependency database.

Output includes:
- Task status: 'run', 'up-to-date', or 'error'
- Detailed reasons for each status
- Full dependency chain analysis
"""

    cmd_options = (opt_outfile,)

    def _execute(self, outfile, pos_args):
        """
        Explain task execution decisions.
        
        @param outfile: output file or file-like object
        @param pos_args: list of task names or targets from command line
        @return: 0 if all tasks are up-to-date, 1 if any would run, 3 on error
        """
        if isinstance(outfile, str):
            outstream = codecs.open(outfile, 'w', encoding='utf-8')
        else:
            outstream = outfile
        self.outstream = outstream

        try:
            tasks_dict = dict((t.name, t) for t in self.task_list)
            
            if not self.task_list:
                self.outstream.write("No tasks found.\n")
                return 0
            
            try:
                control = TaskControl(self.task_list)
                control.process(self.sel_tasks)
                selected = control.selected_tasks
            except InvalidCommand:
                check_tasks_exist(tasks_dict, self.sel_tasks)
                selected = self.sel_tasks if self.sel_tasks else list(tasks_dict.keys())
            
            all_tasks_to_explain = list(tasks_and_deps_iter(
                tasks_dict, selected, yield_duplicates=False
            ))
            
            task_names_in_order = [t.name for t in all_tasks_to_explain]
            
            explanations = self._explain_tasks(task_names_in_order, tasks_dict)
            
            self._output_explanations(explanations)
            
            any_would_run = any(
                exp['status'] == 'run' for exp in explanations.values()
            )
            
            return 1 if any_would_run else 0
        finally:
            if isinstance(outfile, str):
                outstream.close()

    def _explain_tasks(self, task_names, tasks_dict):
        """
        Explain multiple tasks, including task_dep chain analysis.
        
        @param task_names: list of task names to explain
        @param tasks_dict: dict of all tasks
        @return: dict of task_name -> explanation dict
        """
        explanations = {}
        
        for task_name in task_names:
            if task_name not in tasks_dict:
                continue
            
            task = tasks_dict[task_name]
            status = self.dep_manager.get_status(task, tasks_dict, get_log=True)
            
            explanation = {
                'name': task_name,
                'status': status.status,
                'reasons': dict(status.reasons),
                'task_dep_statuses': {},
                'would_run_due_to_dep': False,
            }
            
            for dep_name in task.task_dep:
                if dep_name in tasks_dict:
                    dep_task = tasks_dict[dep_name]
                    dep_status = self.dep_manager.get_status(
                        dep_task, tasks_dict, get_log=True
                    )
                    explanation['task_dep_statuses'][dep_name] = {
                        'status': dep_status.status,
                        'reasons': dict(dep_status.reasons),
                    }
                    
                    if dep_status.status == 'run':
                        explanation['would_run_due_to_dep'] = True
                        if 'task_dep_changed' not in explanation['reasons']:
                            explanation['reasons']['task_dep_changed'] = []
                        explanation['reasons']['task_dep_changed'].append(dep_name)
            
            if explanation['would_run_due_to_dep'] and explanation['status'] == 'up-to-date':
                explanation['status'] = 'run'
            
            if status.status == 'error':
                explanation['error_reason'] = status.get_error_message()
            
            explanations[task_name] = explanation
        
        return explanations

    def _output_explanations(self, explanations):
        """
        Output explanations in a human-readable, testable format.
        
        @param explanations: dict of task_name -> explanation dict
        """
        for task_name, exp in explanations.items():
            self.outstream.write("=" * 60 + "\n")
            self.outstream.write(f"Task: {task_name}\n")
            self.outstream.write("=" * 60 + "\n")
            
            status = exp['status']
            self.outstream.write(f"\n  Status: {status.upper()}\n")
            
            if status == 'error':
                self.outstream.write(f"  Error: {exp.get('error_reason', 'Unknown error')}\n")
            
            self._output_reasons(exp['reasons'])
            
            task_dep_statuses = exp.get('task_dep_statuses', {})
            if task_dep_statuses:
                self.outstream.write("\n  Task Dependencies:\n")
                for dep_name, dep_info in task_dep_statuses.items():
                    dep_status = dep_info['status']
                    marker = "!" if dep_status == 'run' else " "
                    self.outstream.write(f"    {marker} {dep_name}: {dep_status}\n")
                    
                    if dep_status == 'run':
                        self._output_indented_reasons(dep_info['reasons'], indent=6)
            
            self.outstream.write("\n")

    def _output_reasons(self, reasons):
        """
        Output reasons in a structured format.
        
        @param reasons: dict of reason_type -> list of items
        """
        if not reasons:
            self.outstream.write("\n  No specific reasons - task is up-to-date.\n")
            return
        
        self.outstream.write("\n  Reasons:\n")
        
        if 'has_no_dependencies' in reasons:
            self.outstream.write("    - Task has no dependencies (always runs)\n")
        
        if 'uptodate_false' in reasons:
            for utd, utd_args, utd_kwargs in reasons['uptodate_false']:
                self.outstream.write(
                    f"    - uptodate check returned false: {utd} "
                    f"(args={utd_args}, kwargs={utd_kwargs})\n"
                )
        
        if 'checker_changed' in reasons:
            old, new = reasons['checker_changed']
            self.outstream.write(
                f"    - Dependency checker changed: {old} -> {new}\n"
            )
        
        if 'missing_target' in reasons:
            self.outstream.write("    - Missing target files:\n")
            for target in reasons['missing_target']:
                self.outstream.write(f"        {target}\n")
        
        if 'missing_file_dep' in reasons:
            self.outstream.write("    - Missing dependency files:\n")
            for dep in reasons['missing_file_dep']:
                self.outstream.write(f"        {dep}\n")
        
        if 'changed_file_dep' in reasons:
            self.outstream.write("    - Changed dependency files:\n")
            for dep in reasons['changed_file_dep']:
                self.outstream.write(f"        {dep}\n")
        
        if 'added_file_dep' in reasons:
            self.outstream.write("    - New dependency files:\n")
            for dep in reasons['added_file_dep']:
                self.outstream.write(f"        {dep}\n")
        
        if 'removed_file_dep' in reasons:
            self.outstream.write("    - Removed dependency files:\n")
            for dep in reasons['removed_file_dep']:
                self.outstream.write(f"        {dep}\n")
        
        if 'task_dep_changed' in reasons:
            self.outstream.write("    - Task dependencies need to run:\n")
            for dep_name in reasons['task_dep_changed']:
                self.outstream.write(f"        {dep_name}\n")

    def _output_indented_reasons(self, reasons, indent):
        """
        Output reasons with extra indentation (for nested dependencies).
        
        @param reasons: dict of reason_type -> list of items
        @param indent: number of spaces to indent
        """
        prefix = " " * indent
        
        if not reasons:
            self.outstream.write(prefix + "No specific reasons\n")
            return
        
        if 'has_no_dependencies' in reasons:
            self.outstream.write(prefix + "- No dependencies\n")
        
        if 'uptodate_false' in reasons:
            for utd, _, _ in reasons['uptodate_false']:
                self.outstream.write(prefix + f"- uptodate: {utd}\n")
        
        if 'missing_target' in reasons:
            self.outstream.write(prefix + "- Missing targets:\n")
            for target in reasons['missing_target']:
                self.outstream.write(prefix + f"    {target}\n")
        
        if 'missing_file_dep' in reasons:
            self.outstream.write(prefix + "- Missing files:\n")
            for dep in reasons['missing_file_dep']:
                self.outstream.write(prefix + f"    {dep}\n")
        
        if 'changed_file_dep' in reasons:
            self.outstream.write(prefix + "- Changed files:\n")
            for dep in reasons['changed_file_dep']:
                self.outstream.write(prefix + f"    {dep}\n")
        
        if 'added_file_dep' in reasons:
            self.outstream.write(prefix + "- New files:\n")
            for dep in reasons['added_file_dep']:
                self.outstream.write(prefix + f"    {dep}\n")
        
        if 'removed_file_dep' in reasons:
            self.outstream.write(prefix + "- Removed files:\n")
            for dep in reasons['removed_file_dep']:
                self.outstream.write(prefix + f"    {dep}\n")
        
        if 'task_dep_changed' in reasons:
            self.outstream.write(prefix + "- Task deps changed:\n")
            for dep_name in reasons['task_dep_changed']:
                self.outstream.write(prefix + f"    {dep_name}\n")
