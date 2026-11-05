"""Availability of newly offered products; execution stays in its owning flow."""
from .common import require

GO_HTTP_FLOW = 'cslive_go_direct_pay'
FLOW_PRODUCTS = frozenset({'plus', 'promax'})


def available(product_id, flow):
    if flow == GO_HTTP_FLOW and product_id == 'team_premium':
        return False
    return product_id not in FLOW_PRODUCTS or flow == GO_HTTP_FLOW


def visible_products(rows, flow):
    return [display_product(row, flow) for row in rows if available(row['id'], flow)]


def display_product(row, flow):
    if row['id'] == 'team' and flow == GO_HTTP_FLOW:
        row = dict(row, name='Team团队订阅')
        if 'description' in row:row['description']='按需组合普通席位与高级席位'
    return row


def require_product_flow(product_id, flow):
    require(available(product_id, flow), '当前办理流程未开放此套餐', 400)
