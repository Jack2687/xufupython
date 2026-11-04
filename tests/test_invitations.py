from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from backend import db,invitations
from backend.common import uid


class InvitationTests:
    def invite_order(self, invitee, active=True, fee=10000, workflow='subscription'):
        oid=uid()
        db.execute("INSERT INTO orders(id,user_id,product_id,product_name,email,fee,status,activation,workflow,idempotency_key,request_hash) VALUES($1,$2,'pro','Pro','fixture@example.test',$3,'succeeded',$4,$5,$1,'fixture')",oid,invitee,fee,'active' if active else 'pending',workflow)
        db.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,order_id,reference,description) VALUES($1,'charge',$2,0,0,$3,$3,'fixture')",invitee,-fee,oid)
        return oid

    def invite_pair(self):
        inviter,_,client=self.new_user();invitee,_,guest=self.new_user()
        db.execute('UPDATE users SET invited_by=$2 WHERE id=$1',invitee,inviter)
        return inviter,invitee,client,guest

    def test_invite_reward_once_with_concurrency_and_actual_fee(self):
        inviter,invitee,client,guest=self.invite_pair()
        order=self.invite_order(invitee,active=False,fee=12345)
        invitations.settle();self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',inviter),0)
        db.execute("UPDATE orders SET activation='active' WHERE id=$1",order)
        with ThreadPoolExecutor(4) as pool:list(pool.map(lambda _:invitations.settle(),range(8)))
        self.invite_order(invitee,fee=50000);invitations.settle()
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',inviter),617)
        self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE user_id=$1 AND kind='invite_reward'",inviter),1)
        result=self.ok(client.get('/api/invites'))
        self.assertEqual((result['stats']['earned'],result['stats']['qualified']),(617,1))
        self.assertIsNone(result['items'][0]['inviter']);self.assertIn('***@',result['items'][0]['email'])
        self.assertEqual(self.ok(guest.get('/api/invites'))['items'],[])
        self.assertEqual(self.ok(client.get('/api/wallet'))['ledger'][0]['kind'],'invite_reward')
        self.assertEqual(self.ok(client.get('/api/wallet'))['recharges'],[])

    def test_invite_rules_snapshot_no_historic_backfill_or_link_reward(self):
        original=db.scalar("SELECT value FROM settings WHERE key='invites'")
        try:
            a,b,_,_=self.invite_pair()
            self.ok(self.client.put('/api/admin/settings/invites',json={'enabled':False,'rate_percent':5}))
            self.invite_order(b)
            self.ok(self.client.put('/api/admin/settings/invites',json={'enabled':True,'rate_percent':7}))
            self.invite_order(b);invitations.settle()
            self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',a),0)
            a,b,_,_=self.invite_pair();self.invite_order(b,workflow='link');self.assertEqual(db.scalar('SELECT count(*) FROM invitation_candidates WHERE invitee_id=$1',b),0)
            self.invite_order(b);invitations.settle();self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',a),700)
            a,b,_,_=self.invite_pair();order=self.invite_order(b)
            self.ok(self.client.put('/api/admin/settings/invites',json={'enabled':False,'rate_percent':20}))
            invitations.settle();self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',a),700)
            db.execute('UPDATE orders SET deleted_at=now() WHERE id=$1',order);invitations.settle()
            self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',a),700)
        finally:db.execute("UPDATE settings SET value=$1 WHERE key='invites'",original)

    def test_invite_refund_reclaims_only_available_and_retries_debt(self):
        inviter,invitee,_,_=self.invite_pair();order=self.invite_order(invitee);invitations.settle()
        db.execute('UPDATE users SET frozen=400 WHERE id=$1',inviter)
        db.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,order_id,reference,description) VALUES($1,'refund',10000,10000,0,$2,$3,'fixture')",invitee,order,uid())
        invitations.settle();invitations.settle()
        row=db.one('SELECT * FROM invitation_rewards WHERE invitee_id=$1',invitee)
        self.assertEqual((row['granted'],row['entitled'],row['recovered']),(500,0,100))
        self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',inviter),{'balance':400,'frozen':400})
        db.execute('UPDATE users SET frozen=0 WHERE id=$1',inviter)
        invitations.settle();invitations.settle()
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',inviter),0)
        self.assertEqual(db.scalar("SELECT sum(amount) FROM ledger WHERE user_id=$1 AND kind LIKE 'invite_%'",inviter),0)

    def test_invite_partial_refund_rounding_and_config_validation(self):
        a,b,_,_=self.invite_pair();order=self.invite_order(b,fee=101);invitations.settle()
        db.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,order_id,reference,description) VALUES($1,'refund',51,51,0,$2,$3,'fixture')",b,order,uid())
        invitations.settle();self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',a),2)
        for value in ({'enabled':True,'rate_percent':31},{'enabled':'true','rate_percent':5},{'enabled':True,'rate_percent':1.5}):
            self.assertEqual(self.client.put('/api/admin/settings/invites',json=value).status_code,400)

    def test_invite_registration_binding_and_admin_search(self):
        a,email,client=self.new_user();code=db.scalar('SELECT invite_code FROM users WHERE id=$1',a)
        from backend.main import app
        guest=TestClient(app);guest_email=uid()+'@example.test'
        self.ok(guest.post('/api/auth/register',json={'email':guest_email,'name':'伙伴','password':'Test-password-123','invite':code}))
        self.assertEqual(db.scalar('SELECT invited_by FROM users WHERE email=$1',guest_email),a)
        result=self.ok(self.client.get('/api/admin/invites',params={'q':email}))
        self.assertEqual(result['items'][0]['email'],guest_email)
        self.assertEqual(client.get('/api/admin/invites').status_code,403)

    def test_codes_copy_is_ordered_privileged_and_read_only(self):
        created=self.ok(self.client.post('/api/admin/codes',json={'count':3,'amount':100,'days':1}))
        rows=db.all('SELECT id,status FROM redeem_codes WHERE batch=$1 ORDER BY id',created['batch']);ids=[r['id'] for r in rows][::-1]
        db.execute("UPDATE redeem_codes SET status='disabled' WHERE id=$1",ids[0])
        before=db.all('SELECT * FROM redeem_codes WHERE batch=$1 ORDER BY id',created['batch'])
        result=self.ok(self.client.post('/api/admin/codes/batch',json={'ids':ids+[ids[0]],'action':'copy'}))
        from backend.common import decrypt
        expected=[decrypt(next(r for r in before if r['id']==id)['code_enc']) for id in ids]
        self.assertEqual(result['codes'],expected)
        self.assertEqual(db.all('SELECT * FROM redeem_codes WHERE batch=$1 ORDER BY id',created['batch']),before)
        _,_,member=self.new_user();self.assertEqual(member.post('/api/admin/codes/batch',json={'ids':ids,'action':'copy'}).status_code,403)
        masked=self.ok(self.client.get('/api/admin/codes'))
        self.assertTrue(all(code not in str(masked) for code in expected))
