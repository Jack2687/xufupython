import time
from concurrent.futures import ThreadPoolExecutor

from backend import db
from backend.common import digest, encrypt, uid


class RedeemCodeTests:
    def issue_code(self):
        code = self.ok(self.client.post('/api/admin/codes', json={'count': 1, 'amount': 1000, 'days': 1}))['codes'][0]
        return code, db.scalar('SELECT id FROM redeem_codes WHERE code_hash=$1', digest(code))

    def wait_redemption_lock(self):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            if db.scalar("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'SELECT balance,frozen,status,deleted_at FROM users%'"):
                return
            time.sleep(.02)
        self.fail('redemption did not reach the held user lock')

    def test_redemption_rechecks_disabled_or_deleted_account_without_blocking_health(self):
        for deleted in [False, True]:
            with self.subTest(deleted=deleted):
                user, _, client = self.new_user()
                code, code_id = self.issue_code()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    with db.transaction() as lock:
                        lock.one('SELECT id FROM users WHERE id=$1 FOR UPDATE', user)
                        pending = pool.submit(self.client.post, '/api/wallet/redeem', json={'code': code}, headers={'Cookie': 'xupay_session='+client.cookies.get('xupay_session')})
                        self.wait_redemption_lock()
                        health = pool.submit(self.client.get, '/health/live')
                        self.ok(health.result(timeout=2))
                        self.assertFalse(pending.done())
                        if deleted:
                            lock.execute('UPDATE users SET deleted_at=now() WHERE id=$1', user)
                        else:
                            lock.execute("UPDATE users SET status='disabled' WHERE id=$1", user)
                    self.assertEqual(pending.result(timeout=5).status_code, 403)
                self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1', user), 0)
                self.assertEqual(db.scalar('SELECT status FROM redeem_codes WHERE id=$1', code_id), 'active')
                self.assertEqual(db.scalar('SELECT count(*) FROM ledger WHERE reference=$1', 'redeem:'+code_id), 0)

    def test_redemption_lock_wait_is_bounded_and_retry_is_single_credit(self):
        user, _, client = self.new_user()
        code, code_id = self.issue_code()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.transaction() as lock:
                lock.one('SELECT id FROM users WHERE id=$1 FOR UPDATE', user)
                pending = pool.submit(client.post, '/api/wallet/redeem', json={'code': code})
                self.wait_redemption_lock()
                self.assertEqual(pending.result(timeout=6).status_code, 503)
                self.assertEqual(db.scalar('SELECT status FROM redeem_codes WHERE id=$1', code_id), 'active')
        self.ok(client.post('/api/wallet/redeem', json={'code': code}))
        self.assertEqual(client.post('/api/wallet/redeem', json={'code': code}).status_code, 400)
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1', user), 1000)
        self.assertEqual(db.scalar('SELECT count(*) FROM ledger WHERE reference=$1', 'redeem:'+code_id), 1)

    def test_redemption_ledger_failure_preserves_unused_code(self):
        user, _, client = self.new_user()
        code, code_id = self.issue_code()
        db.execute("CREATE FUNCTION reject_code_credit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind='redeem' THEN RAISE EXCEPTION 'fixture'; END IF; RETURN NEW; END $$")
        db.execute('CREATE TRIGGER reject_code_credit BEFORE INSERT ON ledger FOR EACH ROW EXECUTE FUNCTION reject_code_credit()')
        try:
            self.assertEqual(client.post('/api/wallet/redeem', json={'code': code}).status_code, 500)
            self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1', user), 0)
            self.assertEqual(db.scalar('SELECT status FROM redeem_codes WHERE id=$1', code_id), 'active')
        finally:
            db.execute('DROP TRIGGER reject_code_credit ON ledger')
            db.execute('DROP FUNCTION reject_code_credit()')
        self.ok(client.post('/api/wallet/redeem', json={'code': code}))

    def test_codes_server_pagination_search_expiry_and_batch_counts(self):
        code, code_id = self.issue_code()
        prefix = 'page-'+uid()
        db.execute("UPDATE redeem_codes SET batch=$2,created_at=now()-interval '1 day' WHERE id=$1", code_id, prefix)
        with db.transaction() as q:
            q.execute("INSERT INTO redeem_codes(id,code_hash,code_enc,prefix,amount,batch,expires_at) SELECT $1||i,$1||i,$2,'page',100,$1,now()+interval '1 day' FROM generate_series(1,1001) AS i", prefix, encrypt('fixture'))
        first = self.ok(self.client.get('/api/admin/codes', params={'q': prefix, 'page_size': 100}))
        self.assertEqual(first['total'], 1002); self.assertEqual(len(first['items']), 100)
        self.assertTrue(first['has_more'])
        last = self.ok(self.client.get('/api/admin/codes', params={'q': prefix, 'page': 11, 'page_size': 100}))
        self.assertEqual(len(last['items']), 2); self.assertFalse(last['has_more'])
        self.assertIn(code_id, [row['id'] for row in last['items']])
        marker = db.scalar('SELECT prefix FROM redeem_codes WHERE id=$1', code_id)
        self.assertEqual(self.ok(self.client.get('/api/admin/codes', params={'q': marker}))['total'], 1)
        db.execute("UPDATE redeem_codes SET expires_at=now()-interval '1 second' WHERE id=$1", code_id)
        expired = self.ok(self.client.get('/api/admin/codes', params={'q': prefix, 'status': 'expired'}))
        self.assertEqual([row['id'] for row in expired['items']], [code_id])
        self.assertEqual(expired['items'][0]['status'], 'expired')
        result = self.ok(self.client.post('/api/admin/codes/batch', json={'ids': [code_id, code_id, 'missing-fixture'], 'action': 'export'}))
        self.assertEqual(result['codes'], []); self.assertEqual(result['changed'], 0); self.assertEqual(result['skipped'], 2)
        self.assertEqual(self.ok(self.client.post('/api/admin/codes/batch', json={'ids': [code_id], 'action': 'copy'}))['codes'], [code])
        for params in [{'page': 0}, {'page_size': 101}, {'status': 'invalid'}]:
            self.assertEqual(self.client.get('/api/admin/codes', params=params).status_code, 400)
        # Remove only this fixture's rows, leaving the shared suite untouched.
        db.execute('DELETE FROM redeem_codes WHERE batch=$1', prefix)
