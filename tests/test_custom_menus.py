import copy
import json
from contextlib import contextmanager
from fastapi.testclient import TestClient
from backend import db
from backend.common import config


class CustomMenuTests:
    @contextmanager
    def menu_config(self):
        saved=config('custom_menus')
        try: yield
        finally: db.execute("UPDATE settings SET value=$1 WHERE key='custom_menus'",saved)

    def menu(self,id='customer-menu',**values):
        return dict(id=id,label='服务页面',url='https://example.test/page?source=menu',icon_svg='',visibility='user',enabled=True,open_new_tab=False,hide_open_button=False,**values)

    def test_custom_menu_roles_are_filtered_and_routes_enforced(self):
        with self.menu_config():
            _,_,user=self.new_user();anonymous=TestClient(self.client.app)
            items=[self.menu('member'),self.menu('admin'),self.menu('both'),self.menu('disabled')]
            items[1].update(visibility='admin',label='专属管理页面',url='https://private.example.test/')
            items[2]['visibility']='all';items[3]['enabled']=False
            self.ok(self.client.put('/api/admin/settings/custom_menus',json={'items':items}))
            member=self.ok(user.get('/api/me'));admin=self.ok(self.client.get('/api/me'))
            self.assertEqual([m['id'] for m in member['custom_menus']],['member','both'])
            self.assertEqual([m['id'] for m in admin['custom_menus']],['admin','both'])
            self.assertNotIn('private.example.test',json.dumps(member))
            self.assertNotIn('custom_menus',self.ok(anonymous.get('/api/public')))
            for id in ('admin','disabled','missing'):
                self.assertEqual(user.get('/api/custom-pages/'+id).status_code,404)
            self.ok(user.get('/api/custom-pages/member'));self.ok(self.client.get('/api/custom-pages/admin'))
            self.assertEqual(anonymous.get('/api/custom-pages/member').status_code,401)
            self.assertEqual(user.put('/api/admin/settings/custom_menus',json={'items':[]}).status_code,403)

    def test_custom_menu_modes_order_removal_and_api_key_boundary(self):
        with self.menu_config():
            _,_,user=self.new_user();items=[self.menu('first'),self.menu('second')]
            items[0].update(open_new_tab=True,hide_open_button=True)
            items[1]['icon_svg']='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M1 1h2" stroke="currentColor"/></svg>'
            self.ok(self.client.put('/api/admin/settings/custom_menus',json={'items':items[::-1]}))
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['custom_menus']['items'],items[::-1])
            menus=self.ok(user.get('/api/me'))['custom_menus'];self.assertEqual([m['id'] for m in menus],['second','first'])
            self.assertTrue(self.ok(user.get('/api/custom-pages/first'))['open_new_tab'])
            key=self.ok(user.post('/api/keys',json={'name':'custom-menu-test'}))['key']
            token=TestClient(self.client.app,headers={'Authorization':'Bearer '+key})
            self.assertEqual(token.get('/api/custom-pages/first').status_code,403)
            self.assertNotIn('custom_menus',self.ok(token.get('/api/v1/me')))
            self.ok(self.client.put('/api/admin/settings/custom_menus',json={'items':[]}))
            self.assertEqual(self.ok(user.get('/api/me'))['custom_menus'],[])
            self.assertEqual(user.get('/api/custom-pages/first').status_code,404)

    def test_custom_menu_rejects_unsafe_urls_and_svg(self):
        with self.menu_config():
            baseline=self.ok(self.client.get('/api/admin/settings'))['custom_menus']
            invalid=[{'url':v} for v in ('javascript:alert(1)','data:text/html,test','//example.test','https://name:password@example.test','https://example.test:bad/','https://example.test/\nnext','https://example.test\\@evil.test')]
            invalid += [{'icon_svg':v} for v in ('<svg><script>alert(1)</script></svg>','<svg onload="alert(1)"/>','<svg><foreignObject/></svg>','<svg><use href="https://example.test/icon.svg"/></svg>','<!DOCTYPE svg [<!ENTITY x "test">]><svg>&x;</svg>','<svg><path fill="url(https://example.test/)"/></svg>','x'*20001)]
            invalid += [{'visibility':'visitor'},{'open_new_tab':'true'},{'id':'../admin'},{'label':' '},{'enabled':1}]
            for change in invalid:
                item=self.menu();item.update(change)
                self.assertEqual(self.client.put('/api/admin/settings/custom_menus',json={'items':[item]}).status_code,400,str(change)[:80])
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['custom_menus'],baseline)

    def test_custom_menu_limits_and_unique_ids(self):
        with self.menu_config():
            for items in ([self.menu(),self.menu()], [self.menu(str(i)) for i in range(31)], ['invalid']):
                self.assertEqual(self.client.put('/api/admin/settings/custom_menus',json={'items':items}).status_code,400)
            valid=self.menu();valid['icon_svg']='<svg><defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs><path fill="url(#g)" d="M0 0"/></svg>'
            self.ok(self.client.put('/api/admin/settings/custom_menus',json={'items':[valid]}))
            headers=self.client.get('/').headers['content-security-policy']
            self.assertIn('frame-src https: http:',headers);self.assertIn("script-src 'self'",headers)
