"""Payment-confirmation delay uses durable time and the original proxy deadline."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from backend import post_payment_delay as delay


class PaymentDelayTests(unittest.TestCase):
    def test_default_custom_zero_and_recovery_remaining(self):
        current = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.assertEqual(delay.DEFAULT_SECONDS, 5)
        for seconds, elapsed, expected in [(5,0,5),(17,0,17),(0,0,0),(5,2,3),(5,20,0)]:
            with self.subTest(seconds=seconds, elapsed=elapsed), \
                 patch.object(delay, 'now', return_value=current), \
                 patch.object(delay.time, 'sleep') as sleep, \
                 patch.object(delay.trace, 'record') as trace:
                self.assertTrue(delay.wait_before_refresh('fixture', current-timedelta(seconds=elapsed),
                                                         seconds, current+timedelta(minutes=10)))
                if expected:
                    sleep.assert_called_once_with(expected)
                    self.assertEqual([c.args[2] for c in trace.call_args_list],
                                     ['post_payment_wait','post_payment_wait_completed'])
                    self.assertEqual(trace.call_args.kwargs['diagnostics']['post_payment_wait_ms'],expected*1000)
                else:
                    sleep.assert_not_called()
                    trace.assert_not_called()

    def test_original_proxy_cannot_expire_during_wait(self):
        current = datetime(2026, 11, 4, tzinfo=timezone.utc)
        with patch.object(delay, 'now', return_value=current), patch.object(delay.time, 'sleep') as sleep:
            self.assertFalse(delay.wait_before_refresh('fixture',current,5,current+timedelta(seconds=4)))
            sleep.assert_not_called()
        with patch.object(delay, 'now', side_effect=[current,current+timedelta(seconds=12)]), \
             patch.object(delay.time, 'sleep'), patch.object(delay.trace, 'record') as trace:
            self.assertFalse(delay.wait_before_refresh('fixture',current,5,current+timedelta(seconds=10)))
            self.assertEqual(trace.call_count,1)


if __name__ == '__main__':
    unittest.main()
