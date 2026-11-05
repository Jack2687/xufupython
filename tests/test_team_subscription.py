import json
from pathlib import Path
from unittest.mock import Mock,patch
from backend import db
from backend.common import uid,decrypt


class TeamSubscriptionTests:
    def test_team_catalog_combines_entries_without_rewriting_history_or_prices(self):
        with self.api_fixture() as (_,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='cslive_go_direct_pay'))
            before=db.all('SELECT * FROM products ORDER BY id')
            for path,field in (('/api/public','products'),('/api/v1/catalog','items'),('/api/v1/membership','products'),('/api/admin/products','items'),('/api/admin/users','products')):
                response=self.client.get(path) if '/admin/' in path else client.get(path)
                rows=self.ok(response)[field]
                self.assertNotIn('team_premium',{r['id'] for r in rows})
                self.assertEqual(next(r for r in rows if r['id']=='team')['name'],'Team团队订阅')
            self.assertEqual(client.get('/api/v1/quote?product_id=team_premium').status_code,400)
            self.assertEqual(self.ok(client.get('/api/v1/quote?product_id=team'))['fee'],10000)
            self.assertEqual(db.all('SELECT * FROM products ORDER BY id'),before)

    def test_team_combinations_and_custom_promo_are_saved_encrypted_and_replayed_once(self):
        with self.api_fixture() as (owner,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='cslive_go_direct_pay'))
            for total,premium in ((2,0),(2,1),(2,2),(6,4)):
                for workflow in ('link','subscription'):
                    payload=dict(self.api_payload(workflow),product_id='team',seats=total,premium_seats=premium,promo_code='  OWN-TEAM-2026  ')
                    headers={'Idempotency-Key':uid()};first=self.ok(client.post('/api/v1/orders',json=payload,headers=headers));oid=first['id']
                    row=db.one('SELECT o.product_name,o.seats,o.fee,s.premium_seats,s.credentials_enc,s.workspace_name FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1',oid)
                    self.assertEqual((row['product_name'],row['seats'],row['premium_seats']),('Team团队订阅',total,premium))
                    self.assertEqual(row['workspace_name'],'Team')
                    self.assertEqual(row['fee'],10000 if workflow=='subscription' else 0)
                    self.assertNotIn('OWN-TEAM',row['credentials_enc'])
                    self.assertEqual(json.loads(decrypt(row['credentials_enc']))['team_promo_code'],'OWN-TEAM-2026')
                    before=db.scalar('SELECT frozen FROM users WHERE id=$1',owner)
                    self.assertTrue(self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['replayed'])
                    self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',owner),before)
                    self.assertEqual(client.post('/api/v1/orders',json=dict(payload,promo_code='DIFFERENT'),headers=headers).status_code,409)
                    self.assertEqual(client.post('/api/v1/orders',json=dict(payload,premium_seats=(premium+1)%3),headers=headers).status_code,409)
                    detail=client.get('/api/v1/orders/'+oid)
                    self.assertNotIn('OWN-TEAM',detail.text)
                    db.execute("UPDATE orders SET status='failed' WHERE id=$1",oid)
            payload=dict(self.api_payload(),product_id='team');first=self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))
            row=db.one('SELECT o.seats,s.premium_seats FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1',first['id'])
            self.assertEqual(row,dict(seats=2,premium_seats=0))

    def test_team_new_defaults_do_not_replay_different_seats_but_old_hashes_still_replay(self):
        import hashlib,hmac,os
        from backend.customer_contract import OrderRequest
        with self.api_fixture() as (_,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='cslive_go_direct_pay'))
            payload=dict(self.api_payload(),product_id='team',seats=2,premium_seats=1)
            headers={'Idempotency-Key':uid()}
            first=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))
            implicit=dict(payload);implicit.pop('premium_seats')
            self.assertEqual(client.post('/api/v1/orders',json=implicit,headers=headers).status_code,409)
            # Prior deployments used premium=1 when omitted. Replaying those orders is read-only.
            old=OrderRequest.model_validate(implicit).model_dump();old.pop('promo_code')
            canonical=json.dumps(old,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            old_hash='api2:'+hmac.new(os.environ['MASTER_KEY'].encode(),canonical.encode(),hashlib.sha256).hexdigest()
            db.execute('UPDATE orders SET request_hash=$2 WHERE id=$1',first['id'],old_hash)
            replay=self.ok(client.post('/api/v1/orders',json=implicit,headers=headers))
            self.assertEqual(replay['id'],first['id']);self.assertTrue(replay['replayed'])

    def test_team_user_price_edit_keeps_hidden_historical_premium_price(self):
        with self.api_fixture() as (owner,_,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='cslive_go_direct_pay'))
            db.execute('INSERT INTO user_product_prices(user_id,product_id,fee) VALUES($1,$2,$3)',owner,'team_premium',6500)
            self.ok(self.set_account_fixture(owner,product_prices={'team':7000}))
            self.assertEqual(db.scalar("SELECT fee FROM user_product_prices WHERE user_id=$1 AND product_id='team_premium'",owner),6500)
            self.assertEqual(db.scalar("SELECT fee FROM user_product_prices WHERE user_id=$1 AND product_id='team'",owner),7000)
            self.ok(self.set_account_fixture(owner,product_prices={}))
            self.assertEqual(db.scalar("SELECT fee FROM user_product_prices WHERE user_id=$1 AND product_id='team_premium'",owner),6500)

    def test_team_rejects_under_two_invalid_seats_and_invalid_promo_before_charge(self):
        with self.api_fixture() as (owner,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='cslive_go_direct_pay'))
            payload=dict(self.api_payload('subscription'),product_id='team',seats=2,premium_seats=0)
            before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            for delta in ({'seats':0},{'seats':1},{'seats':101},{'premium_seats':-1},{'premium_seats':3},{'seats':True},{'seats':2.5},{'promo_code':'x\ny'},{'promo_code':'汉'*100},{'promo_code':23},{'product_id':'plus','promo_code':'OWN'},{'product_id':'team_premium'}):
                self.assertEqual(client.post('/api/v1/orders',json=dict(payload,**delta),headers={'Idempotency-Key':uid()}).status_code,400,delta)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),before)
            # Direct webpage requests must enforce the same minimum, including admins.
            for total,premium in ((1,0),(0,0),(2,3),(2,False),(2.1,0)):
                self.assertEqual(self.client.post('/api/cslive-go/orders',json=dict(payload,seats=total,premium_seats=premium),headers={'Idempotency-Key':uid()}).status_code,400)

    def test_team_checkout_passes_exact_counts_and_promo_outside_command_line(self):
        from backend import cslive_go_checkout as checkout
        result=Mock(returncode=0,stdout=b'{"checkout_url":"https://pay.openai.com/c/pay/cs_fixture","checkout_id":"cs_fixture"}',stderr=b'')
        for total,premium in ((2,0),(2,1),(2,2),(6,4)):
            for promo in ('','MY-OWN-CODE'):
                with patch.object(Path,'is_file',return_value=True),patch.object(checkout.subprocess,'run',return_value=result) as run:
                    checkout.create_checkout(session={'access_token':'fixture'},plan='team',country='US',proxy_url='http://fixture-proxy:8080',seats=total,seats_5x=premium,promo_code=promo)
                    args=run.call_args.args[0]
                    self.assertEqual(args[args.index('--seats')+1],str(total-premium))
                    self.assertEqual(args[args.index('--seats-5x')+1],str(premium))
                    self.assertEqual(args[args.index('--plan')+1],'team')
                    self.assertNotIn('--workspace-name',args)
                    if promo:
                        self.assertNotIn(promo,' '.join(args))
                        self.assertEqual(run.call_args.kwargs['env']['XUFU_TEAM_PROMO_CODE'],promo)
                    else:self.assertNotIn('--promo-code-env',args)
