"""Explicit recovery only: python scripts/recover_post_payment.py ORDER_ID."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import db
from backend.post_payment import resume, warning_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Resume saved post-payment checks using the original order proxy.')
    parser.add_argument('order_id')
    args = parser.parse_args()
    try:
        result = resume(args.order_id)
    except Exception as exc:
        result = {'state': 'review', 'warning_code': warning_code(exc)}
    finally:
        db.close()
    print(json.dumps(result))
    sys.exit(0 if result['state'] == 'completed' else 2)
