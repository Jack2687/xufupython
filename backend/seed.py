import os
from . import db
from .common import password_hash, uid

TIERS = [
    {'code':'L1','name':'启程会员','threshold':0,'discount':100,'description':'从第一笔成功订单开始积累'},
    {'code':'L2','name':'银阶会员','threshold':200000,'discount':95,'description':'服务费减免 5%'},
    {'code':'L3','name':'金阶会员','threshold':500000,'discount':90,'description':'服务费减免 10%'},
    {'code':'L4','name':'铂阶会员','threshold':1000000,'discount':85,'description':'服务费减免 15%'},
    {'code':'L5','name':'钻阶会员','threshold':2000000,'discount':80,'description':'服务费减免 20%'},
    {'code':'L9','name':'合作伙伴','threshold':10000000,'discount':20,'description':'服务费减免 80%'},
]
DEFAULTS = {
    'features': {name: True for name in ('home','subscribe','orders','wallet','links','renewal','keys','invites','membership','docs')},
    'security': {'registration_open':True,'email_verify_enabled':False,'email_domain_whitelist':[]},
    'mail': {'enabled':False,'host':'','port':465,'username':'','password_enc':'','from_email':'','from_name':'','encryption':'ssl'},
    'invites': {'enabled':True,'rate_percent':5},
    'site': {'name':'序付','english_name':'XUPAY','tagline':'让每一笔订阅，清晰有序。','registration_open':True,'support_url':'','terms':'请仅提交本人或已获授权的账号与银行卡。服务费与上游订阅费用分别结算，订单处理结果以订单记录为准。','billing_country':'US','billing_mode':'customer_review','invite_rate':0},
    'risk': {'login_per_minute':10,'api_per_minute':120,'order_per_minute':5,'max_open_orders':10,'max_topup':10000000,'session_hours':24,'event_retention_days':90},
    'scheduler': {'paused':False,'max_parallel':2,'per_user_parallel':1,'retry_limit':2,'retry_delay_seconds':30,'task_timeout_seconds':180,'queue_limit':500,'default_country':'PH','link_mode':'long','engine':'not_connected'},
    'billing': {'country':'US','allow_customer_edit':True,'address_templates':[]},
    'proxy': {'default_id':'','global_id':'','match_country':True,'request_timeout_seconds':60},
    'payment': {'enabled':True,'subscription_flow':'','checkout_type':'hosted','checkout_types':{'pro5x':'hosted','pro':'hosted','team':'oaics','team_premium':'oaics','plus':'oaics','go':'oaics'},'max_parallel':2,'per_user_parallel':1,'prepare_timeout_seconds':120,'quote_timeout_seconds':180,'result_timeout_seconds':120,'requests_per_minute':3,'subscription_wait_seconds':600,'renewal_check_interval_seconds':30,'renewal_check_limit':10,'network_restart_limit':2,'activation_check_interval_seconds':5,'activation_check_limit':12,'post_payment_delay_seconds':5,'cslive_go_visit_return':True,'cslive_go_refresh_before_renewal':True,'post_refresh_retries':4,'post_refresh_interval_seconds':3},
    'tiers': {'items':TIERS},
}


def seed():
    with db.transaction() as q:
        for key, value in DEFAULTS.items():
            if key == 'security':
                value = dict(value, registration_open=q.scalar("SELECT value->'registration_open' FROM settings WHERE key='site'", default=True))
            q.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT DO NOTHING',key,value)
        products = [('pro5x','Pro 5x','灵活开启，更适合轻量使用','轻量进阶'),('pro','Pro 20x','为高频工作提供充足空间','热门选择'),('team','Team 标准','将团队协作放到同一个空间','团队协作'),('team_premium','Team 高级','为团队成员配置更高使用额度','弹性席位')]
        for i, (pid,name,description,tag) in enumerate(products):
            q.execute('INSERT INTO products(id,name,description,fee,sort,tag,features) VALUES($1,$2,$3,10000,$4,$5,$6::jsonb) ON CONFLICT DO NOTHING',pid,name,description,i,tag,'["独立订单记录","费用明细可追溯","在线查看办理进度"]')
        if q.scalar('SELECT count(*) FROM users') == 0:
            q.execute("INSERT INTO users(id,email,name,password_hash,role,invite_code) VALUES($1,$2,'运营管理员',$3,'admin',$4)",uid(),os.getenv('BOOTSTRAP_EMAIL','admin'),password_hash(os.getenv('BOOTSTRAP_PASSWORD','admin')),uid()[:10])
