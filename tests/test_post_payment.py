"""Order-bound validation shared by the Go post-payment entry."""
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.post_payment import FollowupError,canonical_plan,select_return_url,validate_return_url

def checkpoint():
    return {'plan':'pro5x','checkout_id':'cs_test_fixture','account_id':'account-fixture','entity':'openai_llc'}

class PostPaymentTests(unittest.TestCase):
    def test_plan_mapping_rejects_unknown_plan(self):
        self.assertEqual(canonical_plan('pro'),'pro20x')
        self.assertEqual(canonical_plan('team_premium'),'team5x')
        with self.assertRaisesRegex(FollowupError,'unsupported_plan'):canonical_plan('unknown')

    def test_return_url_is_bound_to_order_and_no_external_redirect(self):
        cp = checkpoint()
        valid = select_return_url(cp, [])
        self.assertEqual(validate_return_url(valid, cp), valid)
        for raw in [valid.replace('https:', 'http:'), valid.replace('chatgpt.com', 'chatgpt.com.evil.test'),
                    valid.replace('chatgpt.com', 'user@chatgpt.com'), valid.replace('chatgpt.com', 'chatgpt.com:443'),
                    valid.replace('/verify', '/other'), valid + '#fragment', valid + '&account_id=other',
                    valid + '&stripe_session_id=cs_test_fixture', valid + '&processor_entity=other',
                    valid + '\\', valid + '\n', valid + '&value=%xx']:
            with self.assertRaisesRegex(FollowupError, 'invalid_return_url'):
                validate_return_url(raw, cp)
        chosen = valid + '&account_id=account-fixture'
        self.assertEqual(select_return_url(cp, [{'success_url': 'https://evil.test',
                                                 'after_completion': {'redirect': {'url': chosen}}}]), chosen)
        cp['plan'] = 'team5x'
        self.assertIn('plan_type=team', select_return_url(cp, []))

if __name__=='__main__':unittest.main()
