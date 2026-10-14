import unittest

from repair_policy import assess


FRAME = 'app/src/main/java/com/example/Login.kt'


def evidence(**changes):
    value = dict(event_id='event-1', exception_type='IllegalStateException', app_frames=[FRAME])
    value.update(changes)
    return value


def proposal(**changes):
    value = dict(root_cause_file=FRAME, changed_files=[FRAME], app_fault=True,
                 causal_explanation='Guard the observed null state at Login.kt', head_sha='abc123')
    value.update(changes)
    return value


def verified(**changes):
    value = dict(regression_before_exit=1, regression_after_exit=0,
                 project_check_exit=0, tested_head='abc123')
    value.update(changes)
    return value


class RepairPolicyTest(unittest.TestCase):
    def test_direct_app_defect_with_observed_before_after_and_project_check(self):
        self.assertEqual(assess(evidence(), proposal(), verified()).action, 'draft_pr')

    def test_shell_induced_crash_stops_at_issue_even_with_formal_tests(self):
        crash = evidence(exception_type='CrashedByAdbException')
        decision = assess(crash, proposal(), verified())
        self.assertEqual(decision.action, 'issue_only')
        self.assertIn('Shell-induced test crash has no app code fix', decision.reasons)

    def test_external_or_sparse_manufacturer_report_stops(self):
        for crash in ({'event_id': 'event-1', 'exception_type': 'WebViewException', 'app_frames': []},
                      {'event_id': 'event-1', 'exception_type': 'OEMFailure', 'app_frames': []}):
            self.assertEqual(assess(crash, proposal(), verified()).action, 'issue_only')

    def test_manufacturer_specific_case_can_pass_with_app_evidence(self):
        crash = evidence(device_manufacturer='Example OEM')
        self.assertEqual(assess(crash, proposal(), verified()).action, 'draft_pr')

    def test_model_confidence_and_formal_test_claims_do_not_substitute_for_evidence(self):
        weak = proposal(root_cause_file='unrelated/README.md', changed_files=['unrelated/README.md'],
                        confidence=1.0, tests_passed=True)
        self.assertEqual(assess(evidence(), weak, verified()).action, 'issue_only')
        self.assertEqual(assess(evidence(), proposal(), verified(regression_before_exit=0)).action,
                         'issue_only')
        self.assertEqual(assess(evidence(), proposal(), verified(tested_head='other')).action,
                         'issue_only')

    def test_unsafe_or_unrelated_changes_stop(self):
        for files in (['../secret'], ['/tmp/file'], ['README.md'], []):
            self.assertEqual(assess(evidence(), proposal(changed_files=files), verified()).action,
                             'issue_only')


if __name__ == '__main__':
    unittest.main()
