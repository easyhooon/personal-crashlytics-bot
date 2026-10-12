"""Read-only check of the personal Pub/Sub Keychain credential and subscription."""

import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmail_push import keychain_pubsub_credentials
from google.cloud import pubsub_v1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription', required=True)
    args = parser.parse_args()
    try:
        subscription_name = args.subscription
        if not re.fullmatch(r'projects/[^/]+/subscriptions/[^/]+', subscription_name):
            raise ValueError('invalid subscription name')
        credentials = keychain_pubsub_credentials()
        if set(credentials.scopes or []) != {
            'https://www.googleapis.com/auth/pubsub'
        }:
            raise ValueError('unexpected Pub/Sub scope')
        with pubsub_v1.SubscriberClient(credentials=credentials) as client:
            subscription = client.get_subscription(subscription=subscription_name)
        if subscription.name != subscription_name:
            raise ValueError('unexpected subscription')
    except Exception as error:
        print(f'verification_failed: {type(error).__name__}')
        return 1
    print('pubsub_scope_and_subscription_ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
