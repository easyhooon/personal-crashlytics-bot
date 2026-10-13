import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bot
import codex_repair


ALERT = {'project_id': bot.TARGETS['yeobee'][0], 'app_id': bot.TARGETS['yeobee'][1],
         'issue_id': 'a' * 32}


class CodexProposalTest(unittest.TestCase):
    def test_read_only_structured_no_code_proposal(self):
        with tempfile.TemporaryDirectory() as checkout:
            def fake_run(command, **_):
                self.assertIn('read-only', command)
                self.assertIn('--ignore-user-config', command)
                self.assertTrue(any('crashlytics_get_issue,crashlytics_list_events' in arg
                                    for arg in command))
                output = Path(command[command.index('-o') + 1])
                output.write_text(json.dumps({
                    'decision': 'no_code_fix', 'facts': ['shell crash'], 'hypotheses': [],
                    'missing_information': [], 'root_cause_file': '',
                    'causal_explanation': '', 'test_file': '',
                    'regression_patch': '', 'fix_patch': '',
                }))
                return subprocess.CompletedProcess(command, 0)
            with patch.object(codex_repair.subprocess, 'run', side_effect=fake_run):
                self.assertEqual(codex_repair.propose(ALERT, checkout)['decision'], 'no_code_fix')

    def test_model_no_code_patch_is_rejected(self):
        with tempfile.TemporaryDirectory() as checkout:
            def fake_run(command, **_):
                Path(command[command.index('-o') + 1]).write_text(json.dumps({
                    'decision': 'no_code_fix', 'facts': [], 'hypotheses': [],
                    'missing_information': [], 'root_cause_file': '',
                    'causal_explanation': '', 'test_file': '',
                    'regression_patch': 'unexpected', 'fix_patch': '',
                }))
                return subprocess.CompletedProcess(command, 0)
            with patch.object(codex_repair.subprocess, 'run', side_effect=fake_run):
                with self.assertRaises(ValueError):
                    codex_repair.propose(ALERT, checkout)


if __name__ == '__main__':
    unittest.main()
