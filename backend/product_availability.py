"""Availability of newly offered products; execution stays in its owning flow."""
from .common import require

GO_HTTP_FLOW = 'cslive_go_direct_pay'
FLOW_PRODUCTS = frozenset({'plus', 'promax'})


def available(product_id, flow):
    return product_id not in FLOW_PRODUCTS or flow == GO_HTTP_FLOW


def visible_products(rows, flow):
    return [row for row in rows if available(row['id'], flow)]


def require_product_flow(product_id, flow):
    require(available(product_id, flow), '当前办理流程未开放此套餐', 400)
