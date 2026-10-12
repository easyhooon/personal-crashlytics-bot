"""Verify the personal Gmail Keychain credential without printing private data."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmail_push import keychain_gmail_credentials
from googleapiclient.discovery import build


def main():
    try:
        credentials = keychain_gmail_credentials()
        if set(credentials.scopes or []) != {
            'https://www.googleapis.com/auth/gmail.readonly'
        }:
            raise ValueError('unexpected Gmail scope')
        profile = build(
            'gmail', 'v1', credentials=credentials, cache_discovery=False
        ).users().getProfile(userId='me').execute()
        if not profile.get('emailAddress') or not profile.get('historyId'):
            raise ValueError('incomplete Gmail profile')
    except Exception as error:
        print(f'verification_failed: {type(error).__name__}')
        return 1
    print('gmail_keychain_and_profile_ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
