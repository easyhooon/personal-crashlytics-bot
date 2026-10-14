import os
import time
import unittest
from unittest.mock import patch

import bot


DEBUG = {'project_id': 'yeobeeios', 'app_id': bot.DEBUG_APPS['yeobee'][1], 'issue_id': 'a' * 32}


class DebugAppTest(unittest.TestCase):
    def test_debug_app_is_rejected_without_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PERSONAL_CRASHLYTICS_TEST_UNTIL', None)
            with self.assertRaisesRegex(ValueError, 'Unknown personal app'):
                bot.validate(DEBUG)

    def test_expired_or_too_long_opt_in_is_rejected(self):
        for until in (int(time.time()) - 1, int(time.time()) + 5 * 60 * 60):
            with patch.dict(os.environ, {'PERSONAL_CRASHLYTICS_TEST_UNTIL': str(until)}):
                with self.assertRaises(ValueError):
                    bot.validate(DEBUG)

    def test_opt_in_maps_debug_app_to_yeobee_repository(self):
        until = str(int(time.time()) + 60 * 60)
        with patch.dict(os.environ, {'PERSONAL_CRASHLYTICS_TEST_UNTIL': until}):
            name, target, _, _ = bot.validate(DEBUG)
        self.assertEqual(name, 'yeobee')
        self.assertEqual(target[1], bot.DEBUG_APPS['yeobee'][1])
        self.assertEqual(target[2:], bot.TARGETS['yeobee'][2:])
        self.assertEqual(bot.console_package(name, target), 'com.yeobee.dev')
        self.assertEqual(bot.console_package(name, bot.TARGETS['yeobee']), 'com.yeobee')


if __name__ == '__main__':
    unittest.main()
