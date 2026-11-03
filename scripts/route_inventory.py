"""Generate reviewable route coverage from decorators and the source route inventory."""
import ast
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def implemented():
    routes=set()
    for f in (ROOT/'backend').glob('*.py'):
        tree=ast.parse(f.read_text(encoding='utf-8-sig'));prefixes={'app':''}
        for n in tree.body:
            if isinstance(n,ast.Assign) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=='APIRouter':
                prefixes[n.targets[0].id]=next((k.value.value for k in n.value.keywords if k.arg=='prefix'),'')
        for n in tree.body:
            if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
            for d in n.decorator_list:
                if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and isinstance(d.func.value,ast.Name) and d.func.value.id in prefixes and d.func.attr in ('get','post','put','patch','delete'):
                    routes.add((d.func.attr.upper(),prefixes[d.func.value.id]+d.args[0].value))
    return routes


if __name__=='__main__':
    norm=lambda p:re.sub(r'\{[^}]+\}','{}',p)
    actual=implemented();normalized={(m,norm(p)) for m,p in actual}
    source=json.loads((ROOT/'docs/source-routes.json').read_text(encoding='utf-8-sig'))
    rows=[]
    for method,path in source:
        done=(method,norm(path)) in normalized
        excluded=path in ('/api/orders','/api/v1/orders') or '/orders/{id}/' in path
        if not done and not excluded:raise RuntimeError('Missing route: '+method+' '+path)
        rows.append({'method':method,'path':path,'status':'implemented' if done else 'excluded','reason':'' if done else '用户要求订阅办理、结账链接及关联执行后端留空'})
    (ROOT/'docs/route-coverage.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'source_routes':len(rows),'implemented':sum(r['status']=='implemented' for r in rows),'intentionally_excluded':sum(r['status']=='excluded' for r in rows)}))
