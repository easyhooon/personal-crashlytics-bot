import Foundation
import Security

// This program receives the OAuth JSON on stdin and writes only to the
// existing user's login Keychain. It never prints the credential.
// The Keychain service is the first argument; an existing item is updated in
// place so an expired refresh token can be replaced after re-consent.
let allowed: Set<String> = ["personal-crashlytics-bot-pubsub-user", "personal-crashlytics-bot-oauth-user"]
let service = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "personal-crashlytics-bot-pubsub-user"
guard allowed.contains(service) else { exit(3) }
let account = "personal-gmail"
let keychainPath = NSHomeDirectory() + "/Library/Keychains/login.keychain-db"
let payload = FileHandle.standardInput.readDataToEndOfFile()
guard !payload.isEmpty else { exit(2) }

var keychain: SecKeychain?
guard SecKeychainOpen(keychainPath, &keychain) == errSecSuccess,
      let keychain else { exit(1) }
var item: SecKeychainItem?
let found = service.withCString { serviceBytes in
    account.withCString { accountBytes in
        SecKeychainFindGenericPassword(
            keychain,
            UInt32(service.utf8.count), serviceBytes,
            UInt32(account.utf8.count), accountBytes,
            nil, nil, &item)
    }
}
let status: OSStatus
if found == errSecSuccess, let item {
    status = payload.withUnsafeBytes { bytes in
        SecKeychainItemModifyAttributesAndData(item, nil, UInt32(payload.count), bytes.baseAddress!)
    }
} else {
    status = service.withCString { serviceBytes in
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
}
exit(status == errSecSuccess ? 0 : 1)
