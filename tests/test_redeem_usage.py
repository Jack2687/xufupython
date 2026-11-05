from fastapi.testclient import TestClient
from backend import db
from backend.common import uid,digest
from backend.report_period import report_period


class RedeemUsageTests:
    def test_redeem_usage_calendar_permissions_and_readonly_totals(self):
        user,email,client=self.new_user()
        marker='report_'+uid()
        stamps=['2026-11-04T15:59:59Z','2026-11-04T16:00:00Z','2026-11-05T15:59:59Z','2026-11-05T16:00:00Z']
        for i,stamp in enumerate(stamps):
            db.execute("INSERT INTO redeem_codes(id,code_hash,code_enc,prefix,amount,batch,status,used_by,used_at,expires_at) VALUES($1,$2,'fixture','XP-test',1000,$3,'used',$4,$5,now()+interval '1 day')",uid(),digest(uid()),marker+(' 100%_' if i==1 else ''),user,stamp)
        db.execute("UPDATE users SET deleted_at=now(),status='disabled' WHERE id=$1",user)
        path='/api/admin/codes/usage'
        params={'date':'2026-11-05','q':marker,'page_size':1}
        report=self.ok(self.client.get(path,params=params))
        self.assertEqual(report['summary'],{'redemptions':2,'codes':2,'accounts':1,'amount':2000})
        self.assertEqual(report['start_date'],'2026-11-05')
        self.assertEqual(report['end_date'],'2026-11-05')
        next_page=self.ok(self.client.get(path,params={**params,'page':2}))
        self.assertNotEqual(next_page['items'][0]['id'],report['items'][0]['id'])
        self.assertEqual(next_page['summary'],report['summary'])
        for query,want in [(marker+' 100%_',1),(email,2),(uid(),0)]:
            result=self.ok(self.client.get(path,params={**params,'q':query}))
            self.assertEqual(result['total'],want)
            for row in result['items']:
                self.assertFalse(set(row)&{'code_hash','code_enc','password_hash'})
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',user),0)
        self.assertEqual(TestClient(self.client.app).get(path).status_code,401)
        _,_,member=self.new_user();self.assertEqual(member.get(path).status_code,403)
        for params in [{'date':'2026-02-30'},{'period':'hour'},{'q':'a'*201},{'page':0},{'page_size':101}]:
            self.assertEqual(self.client.get(path,params=params).status_code,400)
        for period,date,start,end in [('week','2021-01-01','2020-12-28','2021-01-04'),('month','2024-02-29','2024-02-01','2024-03-01'),('year','2024-02-29','2024-01-01','2025-01-01')]:
            a,b=report_period(period,date)
            self.assertEqual((str(a.date()),str(b.date())),(start,end))
            self.assertEqual(a.utcoffset().total_seconds(),28800)
