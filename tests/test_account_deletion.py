from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from backend import db
from backend.common import uid, config


class AccountDeletionTests:
    def test_account_delete_revokes_access_keeps_history_and_releases_email(self):
        id,email,user=self.new_user('待删除客户')
        self.ok(self.client.post('/api/admin/users/'+id+'/balance',json={'amount':5000,'reason':'测试原余额'},headers={'Idempotency-Key':uid()}))
        key=self.ok(user.post('/api/keys',json={'name':'旧密钥'}))['key']
        token=TestClient(self.client.app,headers={'Authorization':'Bearer '+key})
        order,original=self.paid_followup_order()
        db.execute('UPDATE orders SET user_id=$2 WHERE id=$1',order,id)
        db.execute('UPDATE users SET frozen=1000 WHERE id=$1',id)
        ledger_before=db.all('SELECT * FROM ledger WHERE user_id=$1 ORDER BY id',id)
        order_before=db.one('SELECT * FROM orders WHERE id=$1',order)
        result=self.ok(self.client.delete('/api/admin/users/'+id))
        self.assertEqual(result['deleted'],1);self.assertEqual(result['skipped'],[])
        self.assertEqual(db.one('SELECT * FROM orders WHERE id=$1',order),order_before)
        self.assertEqual(db.all('SELECT * FROM ledger WHERE user_id=$1 ORDER BY id',id),ledger_before)
        self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',id),{'balance':5000,'frozen':1000})
        self.assertEqual(user.get('/api/me').status_code,401)
        self.assertEqual(token.get('/api/v1/me').status_code,401)
        self.assertEqual(user.post('/api/auth/login',json={'email':email,'password':'Test-password-123'}).status_code,401)
        for path in ('/api/admin/users','/api/admin/membership'):
            self.assertNotIn(id,[row['id'] for row in self.ok(self.client.get(path))['items']])
        self.assertEqual(self.ok(self.client.get('/api/admin/orders/'+order))['order']['user_id'],id)
        self.assertEqual(self.ok(self.client.delete('/api/admin/users/'+id))['deleted'],0)
        replacement=TestClient(self.client.app)
        self.ok(replacement.post('/api/auth/register',json={'email':email,'name':'新账户','password':'Fresh-password-123'}))
        fresh=self.ok(replacement.get('/api/me'));self.assertNotEqual(fresh['id'],id);self.assertEqual(fresh['balance'],0)
        self.assertEqual(self.ok(replacement.get('/api/orders'))['items'],[])
        self.assertEqual(user.get('/api/me').status_code,401)

    def test_account_delete_batch_privilege_and_current_admin_protection(self):
        id1,_,user1=self.new_user();id2,_,_=self.new_user();admin=self.ok(self.client.get('/api/me'))['id']
        self.assertEqual(user1.post('/api/admin/users/batch-delete',json={'ids':[id2]}).status_code,403)
        self.assertEqual(user1.delete('/api/admin/users/'+id2).status_code,403)
        self.assertEqual(TestClient(self.client.app).delete('/api/admin/users/'+id2).status_code,401)
        result=self.ok(self.client.post('/api/admin/users/batch-delete',json={'ids':[id1,id2,id2,admin,'missing']}))
        self.assertEqual(result['deleted'],2);self.assertEqual(result['skipped'][0]['id'],admin)
        self.ok(self.client.get('/api/admin/users'))
        self.assertIsNone(db.scalar('SELECT deleted_at FROM users WHERE id=$1',admin))
        self.assertEqual(db.scalar("SELECT count(*) FROM audit_events WHERE action='user.deleted' AND target=ANY($1)",[id1,id2]),2)
        for ids in ([],None,[{}],[''],['a']*501):
            self.assertEqual(self.client.post('/api/admin/users/batch-delete',json={'ids':ids}).status_code,400)

    def test_account_delete_cannot_resurrect_via_admin_mutations(self):
        id,email,_=self.new_user();self.ok(self.client.delete('/api/admin/users/'+id))
        self.assertNotEqual(self.client.patch('/api/admin/users/'+id,json={'email':email,'name':'回写','role':'member','status':'active','fee_override':None}).status_code,200)
        self.assertEqual(self.client.put('/api/admin/users/'+id+'/membership',json={'code':'L1','reason':'旧界面回写'}).status_code,404)
        self.assertEqual(self.client.post('/api/admin/users/'+id+'/balance',json={'amount':100,'reason':'旧界面回写'},headers={'Idempotency-Key':uid()}).status_code,404)
        self.assertEqual(self.ok(self.client.post('/api/admin/users/pricing',json={'ids':[id],'fee_override':100}))['updated'],0)
        self.assertEqual(db.scalar('SELECT status FROM users WHERE id=$1',id),'disabled')

    def test_account_delete_concurrent_exactly_once(self):
        id,_,_=self.new_user()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.client.delete('/api/admin/users/'+id),range(2)))
        self.assertEqual(sorted(self.ok(r)['deleted'] for r in results),[0,1])
        self.assertEqual(db.scalar("SELECT count(*) FROM audit_events WHERE action='user.deleted' AND target=$1",id),1)

    def test_account_delete_invalidates_old_reset_and_invitation(self):
        with self.site_config():
            id,email,user=self.new_user();invite=db.scalar('SELECT invite_code FROM users WHERE id=$1',id)
            code=self.email_code(user,email,'reset')
            self.ok(self.client.delete('/api/admin/users/'+id))
            self.assertEqual(user.post('/api/auth/reset-password',json={'email':email,'code':code,'password':'Reset-password-123'}).status_code,400)
            self.assertEqual(user.post('/api/auth/register',json={'email':uid()+'@example.test','name':'test','password':'Password-12345','invite':invite}).status_code,400)
            self.ok(user.post('/api/auth/register',json={'email':email,'name':'新账户','password':'New-password-123'}))
            self.assertEqual(user.post('/api/auth/reset-password',json={'email':email,'code':code,'password':'Reset-password-123'}).status_code,400)
            fresh_id=self.ok(user.get('/api/me'))['id'];self.assertNotEqual(fresh_id,id)
            self.assertEqual(db.scalar('SELECT count(*) FROM email_challenges WHERE user_id=$1',id),0)

    def test_account_delete_other_admin_preserves_one_active_admin(self):
        id=self.ok(self.client.post('/api/admin/users',json={'email':uid()+'@example.test','name':'管理员副本','role':'admin','password':'Admin-password-123'}))['id']
        result=self.ok(self.client.delete('/api/admin/users/'+id));self.assertEqual(result['deleted'],1)
        self.assertGreaterEqual(db.scalar("SELECT count(*) FROM users WHERE role='admin' AND status='active' AND deleted_at IS NULL"),1)
