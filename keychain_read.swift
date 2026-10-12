import Foundation
import Security

// Read only the account and service requested by the Python receiver.
guard CommandLine.arguments.count == 2 else { exit(2) }
let service = CommandLine.arguments[1]
guard service == "personal-crashlytics-bot-oauth-user" ||
      service == "personal-crashlytics-bot-pubsub-user" else { exit(2) }
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: "personal-gmail",
    kSecReturnData as String: true,
    kSecMatchLimit as String: kSecMatchLimitOne
]
var value: CFTypeRef?
let status = SecItemCopyMatching(query as CFDictionary, &value)
guard status == errSecSuccess, let data = value as? Data else {
    fputs("Keychain read status: \(status)\n", stderr)
    exit(1)
}
FileHandle.standardOutput.write(data)
