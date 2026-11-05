"""Team configuration for the Go checkout + HTTP payment flow."""
import unicodedata
from .common import require


def seat_counts(total, premium):
    require(type(total) is int and 2 <= total <= 100, 'Team 普通与高级席位合计须为 2～100 席')
    require(type(premium) is int and 0 <= premium <= total, '高级席位须为 0～总席位数')
    return total - premium, premium


def promo_code(value, product_id):
    require(isinstance(value, str), '自定义优惠码格式无效')
    require(not any(unicodedata.category(c)=='Cc' for c in value), '自定义优惠码不能包含控制字符')
    value=value.strip()
    require(len(value.encode('utf-8')) <= 256, '自定义优惠码不能超过 256 字节')
    require(not value or product_id=='team', '自定义优惠码仅用于 Team团队订阅')
    return value
