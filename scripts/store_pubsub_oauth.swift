import Foundation
import Security

// This program receives the OAuth JSON on stdin and writes only to the
// existing user's login Keychain. It never prints the credential.
let service = "personal-crashlytics-bot-pubsub-user"
let account = "personal-gmail"
let keychainPath = NSHomeDirectory() + "/Library/Keychains/login.keychain-db"
let payload = FileHandle.standardInput.readDataToEndOfFile()
guard !payload.isEmpty else { exit(2) }

var keychain: SecKeychain?
guard SecKeychainOpen(keychainPath, &keychain) == errSecSuccess,
      let keychain else { exit(1) }
let status = service.withCString { serviceBytes in
    account.withCString { accountBytes in
        payload.withUnsafeBytes { bytes in
            SecKeychainAddGenericPassword(
                keychain,
                UInt32(service.utf8.count), serviceBytes,
                UInt32(account.utf8.count), accountBytes,
                UInt32(payload.count), bytes.baseAddress!, nil)
        }
    }
}
exit(status == errSecSuccess ? 0 : 1)
