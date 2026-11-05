import base64
import binascii
import struct
import zlib
from contextlib import contextmanager
from fastapi.testclient import TestClient
from backend import db
from backend.common import config
from backend.branding import DEFAULT_ICON


def icon_data(data,mime='image/png'):
    return 'data:'+mime+';base64,'+base64.b64encode(data).decode()


def png_icon(width=32,height=32):
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',binascii.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+b'\x14\x31\x3f\xff'*width)*height))+chunk(b'IEND',b'')


class BrandingTests:
    @contextmanager
    def brand_config(self):
        saved=config('site')
        try: yield
        finally: db.execute("UPDATE settings SET value=$1 WHERE key='site'",saved)

    def test_brand_default_icon_public_and_legacy_settings(self):
        with self.brand_config():
            site=config('site');site.pop('icon',None)
            db.execute("UPDATE settings SET value=$1 WHERE key='site'",site)
            anonymous=TestClient(self.client.app)
            for path in ('/site-icon','/favicon.ico','/site-icon/default'):
                response=anonymous.get(path)
                self.assertEqual(response.status_code,200);self.assertEqual(response.content,DEFAULT_ICON)
                self.assertIn('image/svg+xml',response.headers['content-type'])
                self.assertEqual(response.headers['x-content-type-options'],'nosniff')
            public=self.ok(anonymous.get('/api/public'))['site']
            self.assertNotIn('icon',public);self.assertTrue(public['icon_url'].startswith('/site-icon?v='))
            self.assertEqual(config('site'),site)
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['site']['icon'],'')

    def test_brand_upload_png_version_and_old_form_save(self):
        with self.brand_config():
            before=config('site');png=png_icon()
            old_url=self.ok(self.client.get('/api/public'))['site']['icon_url']
            self.ok(self.client.put('/api/admin/settings/site',json=dict(before,icon=icon_data(png))))
            public=self.ok(self.client.get('/api/public'))['site'];self.assertNotEqual(public['icon_url'],old_url)
            response=TestClient(self.client.app).get(public['icon_url'])
            self.assertEqual(response.content,png);self.assertIn('image/png',response.headers['content-type'])
            self.assertNotIn('icon',public)
            legacy=dict(before);legacy.pop('icon',None)
            self.ok(self.client.put('/api/admin/settings/site',json=legacy))
            self.assertEqual(config('site')['icon'],icon_data(png))
            self.assertEqual({k:v for k,v in config('site').items() if k!='icon'},{k:v for k,v in before.items() if k!='icon'})

    def test_brand_svg_upload_reset_and_permissions(self):
        with self.brand_config():
            _,_,member=self.new_user();site=config('site')
            svg=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#17483f"/></svg>'
            value=dict(site,icon=icon_data(svg,'image/svg+xml'))
            self.assertEqual(member.put('/api/admin/settings/site',json=value).status_code,403)
            self.assertEqual(TestClient(self.client.app).put('/api/admin/settings/site',json=value).status_code,401)
            self.ok(self.client.put('/api/admin/settings/site',json=value))
            self.assertEqual(self.client.get('/site-icon').content,svg)
            self.assertEqual(self.client.get('/site-icon/default').content,DEFAULT_ICON)
            self.ok(self.client.put('/api/admin/settings/site',json=dict(site,icon='')))
            self.assertEqual(self.client.get('/site-icon').content,DEFAULT_ICON)

    def test_brand_rejects_unsafe_or_invalid_images_without_replacing_icon(self):
        with self.brand_config():
            site=config('site');invalid=['https://example.test/icon.svg',None,123,'data:text/html;base64,PHNjcmlwdD4=',icon_data(b'not a png'),icon_data(png_icon(1,1)),icon_data(png_icon()[:-1]),icon_data(png_icon()+b'extra'),icon_data(b'x'*(256*1024+1))]
            for svg in (b'<svg onload="alert(1)"/>',b'<svg><script>alert(1)</script></svg>',b'<svg><use href="https://example.test/"/></svg>',b'<!DOCTYPE svg [<!ENTITY x "a">]><svg>&x;</svg>'):
                invalid.append(icon_data(svg,'image/svg+xml'))
            for value in invalid:
                self.assertEqual(self.client.put('/api/admin/settings/site',json=dict(site,icon=value)).status_code,400)
                self.assertEqual(config('site'),site)
