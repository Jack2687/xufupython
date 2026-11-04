"""Customer-facing identifiers without rewriting historical product references."""

def public_id(value):
    return 'pro20x' if value == 'pro' else value


def internal_id(value):
    return 'pro' if value == 'pro20x' else value


def public_order(row, request):
    if request.url.path.startswith('/api/v1/') and 'product_id' in row:
        row['product_id'] = public_id(row['product_id'])
    return row
