"""Customer capabilities, enforced at the authenticated request boundary."""
from .common import config, require

FEATURES = ('home', 'subscribe', 'orders', 'wallet', 'links', 'renewal', 'keys', 'invites', 'membership', 'docs')
NAVIGATION = [('home','工作台'),('subscribe','订阅办理'),('orders','订单中心'),('wallet','资金账户'),
              ('links','结账链接'),('renewal','续费管理'),('keys','开发接入'),('invites','伙伴计划'),
              ('membership','会员中心'),('docs','API 文档'),('profile','个人设置')]


def navigation(user=None):
    flags=capabilities(user)
    return [{'id':key,'path':'/'+key,'label':label,'group':'main' if i<4 else 'tools'}
            for i,(key,label) in enumerate(NAVIGATION) if key=='profile' or flags[key]]


def capabilities(user=None):
    values = config('features')
    return {name: True if user and user.get('role') == 'admin' else values.get(name, True) is True for name in FEATURES}


def allowed(name, user=None):
    return capabilities(user).get(name, False)


def require_any(user, *names):
    flags = capabilities(user)
    require(any(flags.get(name) for name in names), '此功能暂未开放', 403)


def enforce(request, user):
    if user['role'] == 'admin':
        return
    path = request.url.path.rstrip('/')
    if path.startswith('/api/v1/'):
        # Disabling developer access also suspends previously issued API keys.
        require_any(user, 'keys')
        path = '/api/' + path[len('/api/v1/'):]
    for prefix in ('/api/cslive-go/', '/api/cslive-full/'):
        if path.startswith(prefix): path = '/api/' + path[len(prefix):]
    route = path.removeprefix('/api/').split('/')[0]
    required = {'overview': ('home',), 'wallet': ('wallet',), 'topups': ('wallet',),
                'keys': ('keys',), 'invites': ('invites',), 'membership': ('membership',),
                'renewals': ('renewal',), 'quote': ('subscribe', 'links'),
                'catalog': ('subscribe', 'links', 'membership')}
    if route in required: require_any(user, *required[route])
    if route == 'orders':
        require_any(user, *('subscribe', 'links') if request.method == 'POST' and path == '/api/orders' else ('orders',))
    if route == 'subscriptions': require_any(user, 'orders')


async def enforce_payload(request, user):
    if user['role'] == 'admin': return
    path = request.url.path.rstrip('/')
    if request.method == 'POST' and (path.endswith('/orders') or path.endswith('/session/inspect')):
        from .common import body
        value = await body(request)
        if path.endswith('/orders'):
            require_any(user, 'subscribe' if value.get('workflow', 'link') == 'subscription' else 'links')
        elif value.get('workflow') in ('subscription', 'link', 'checkout', 'renewal'):
            require_any(user, {'subscription': 'subscribe', 'link': 'links', 'checkout': 'links', 'renewal': 'renewal'}[value['workflow']])
        else:
            require_any(user, 'subscribe', 'links', 'renewal')


def project_user(user):
    result = dict(user)
    flags = capabilities(user)
    if not flags['wallet']:
        for key in ('balance', 'frozen'): result.pop(key, None)
    if not flags['invites']: result.pop('invite_code', None)
    if not flags['membership']: result.pop('fee_override', None)
    return result
