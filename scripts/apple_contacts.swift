// JSON stdin/stdout bridge to Contacts. Never fetch CNContactNoteKey: entitlement required.
import Foundation
import Contacts
import Carbon

struct HelperError: Error {
    let kind: String
    let message: String
}

func fail(_ kind: String, _ message: String) throws -> Never {
    throw HelperError(kind: kind, message: message)
}

func emit(_ payload: [String: Any]) {
    if let bytes = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(bytes)
        FileHandle.standardOutput.write(Data([10]))
    }
}

func statusName() -> String {
    switch CNContactStore.authorizationStatus(for: .contacts) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    case .limited: return "limited"
    @unknown default: return "unknown"
    }
}

let authMessage = "시스템 설정 > 개인정보 보호 및 보안 > 연락처에서 봇의 Python 접근을 허용하세요."

func requireAccess() throws {
    guard statusName() == "authorized" else { try fail("auth", authMessage) }
}

func squish(_ text: String) -> String {
    return text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
}

func replace(_ text: String, _ pattern: String, _ value: String = "") -> String {
    text.replacingOccurrences(of: pattern, with: value, options: .regularExpression)
}

func normalizedPhone(_ text: String) -> String {
    var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    let folded = trimmed.precomposedStringWithCompatibilityMapping
    if folded.range(of: #"^(?:\+|00)(?:81|86|852|886)\D*\d"#, options: .regularExpression) != nil {
        trimmed = folded
    }
    let clean = replace(trimmed, #"\(\s*0\s*\)"#)
    var digits = replace(clean, #"\D+"#)
    var international = trimmed.hasPrefix("+")
    if digits.hasPrefix("00") { digits = String(digits.dropFirst(2)); international = true }
    if international {
        for country in ["81", "86", "886"] {
            if digits.hasPrefix(country + "0") {
                digits = country + String(digits.dropFirst(country.count + 1))
                break
            }
        }
    }
    if digits.hasPrefix("82") && (international || digits.count >= 11) {
        digits = String(digits.dropFirst(2))
        if !digits.hasPrefix("0") { digits = "0" + digits }
    }
    return digits
}

func phoneLabel(_ type: String) -> String {
    switch type {
    case "mobile": return CNLabelPhoneNumberMobile
    case "work": return CNLabelWork
    case "workFax", "fax": return CNLabelPhoneNumberWorkFax
    default: return type.isEmpty ? CNLabelWork : type
    }
}

func labelType(_ label: String?) -> String {
    switch label {
    case CNLabelPhoneNumberMobile: return "mobile"
    case CNLabelPhoneNumberWorkFax: return "workFax"
    case CNLabelWork: return "work"
    default: return label ?? "work"
    }
}

let personKeys: [CNKeyDescriptor] = [
    CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey,
    CNContactOrganizationNameKey, CNContactJobTitleKey, CNContactDepartmentNameKey,
    CNContactPhoneNumbersKey, CNContactEmailAddressesKey, CNContactUrlAddressesKey,
    CNContactPostalAddressesKey
].map { $0 as CNKeyDescriptor } + [CNContactFormatter.descriptorForRequiredKeys(for: .fullName)]
let findKeys: [CNKeyDescriptor] = [
    CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey
].map { $0 as CNKeyDescriptor } + [CNContactFormatter.descriptorForRequiredKeys(for: .fullName)]

func selectedContainer(_ store: CNContactStore, _ input: [String: Any]) throws -> String {
    let requested = input["container"] as? String ?? ""
    let identifier = requested.isEmpty ? store.defaultContainerIdentifier() : requested
    guard !identifier.isEmpty,
          try store.containers(matching: nil).contains(where: { $0.identifier == identifier }) else {
        try fail("invalid", "선택한 연락처 계정이 없습니다. setup-contacts를 다시 실행하세요.")
    }
    return identifier
}

func fetch(_ store: CNContactStore, _ identifier: String, photo: Bool = false) throws -> CNMutableContact {
    guard !identifier.isEmpty else { try fail("invalid", "연락처 식별자가 없습니다.") }
    let keys = personKeys + (photo ? [CNContactImageDataKey as CNKeyDescriptor] : [])
    let request = CNContactFetchRequest(keysToFetch: keys)
    request.predicate = CNContact.predicateForContacts(withIdentifiers: [identifier])
    request.unifyResults = false // Never merge data from a linked card in another account.
    request.mutableObjects = true
    var found: CNMutableContact?
    try store.enumerateContacts(with: request) { contact, stop in
        found = contact.mutableCopy() as? CNMutableContact
        stop.pointee = true
    }
    guard let result = found else { try fail("not_found", "연락처를 찾을 수 없습니다.") }
    return result
}

func fetchPinned(_ store: CNContactStore, _ input: [String: Any], photo: Bool = false) throws -> CNMutableContact {
    let identifier = input["identifier"] as? String ?? ""
    if let container = input["container"] as? String {
        let selected = try selectedContainer(store, input)
        let owners = try store.containers(matching: CNContainer.predicateForContainerOfContact(withIdentifier: identifier))
        guard container == selected && owners.contains(where: { $0.identifier == selected }) else {
            try fail("not_found", "선택한 계정에서 연락처를 찾을 수 없습니다.")
        }
    }
    return try fetch(store, identifier, photo: photo)
}

func personJSON(_ contact: CNContact, photo: Bool) -> [String: Any] {
    var body: [String: Any] = [
        "names": [["givenName": contact.givenName, "familyName": contact.familyName]],
        "organizations": [["name": contact.organizationName, "title": contact.jobTitle, "department": contact.departmentName]],
        "phoneNumbers": contact.phoneNumbers.map { ["value": $0.value.stringValue, "type": labelType($0.label)] },
        "emailAddresses": contact.emailAddresses.map { ["value": $0.value as String, "type": labelType($0.label)] },
        "urls": contact.urlAddresses.map { ["value": $0.value as String, "type": labelType($0.label)] },
        "addresses": contact.postalAddresses.map {
            ["formattedValue": CNPostalAddressFormatter.string(from: $0.value, style: .mailingAddress), "type": labelType($0.label)]
        }
    ]
    if photo {
        body["imageDataAvailable"] = !(contact.imageData?.isEmpty ?? true)
        body["imageBytes"] = contact.imageData?.count ?? 0
    }
    return body
}

func array(_ input: [String: Any], _ key: String) -> [[String: String]] {
    input[key] as? [[String: String]] ?? []
}

func apply(_ person: [String: Any], to contact: CNMutableContact, replaceOrganization: Bool = false) {
    if replaceOrganization {
        let name = array(person, "names").first ?? [:]
        let organization = array(person, "organizations").first ?? [:]
        contact.givenName = name["givenName"] ?? ""
        contact.familyName = name["familyName"] ?? ""
        if let value = organization["name"], !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { contact.organizationName = value }
        if let value = organization["title"], !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { contact.jobTitle = value }
        if let value = organization["department"], !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { contact.departmentName = value }
    }
    if contact.givenName.isEmpty && contact.familyName.isEmpty,
       let name = array(person, "names").first?["givenName"] {
        contact.givenName = name
        contact.familyName = ""
    }
    if let organization = array(person, "organizations").first {
        if contact.organizationName.isEmpty, let value = organization["name"], !value.isEmpty { contact.organizationName = value }
        if contact.jobTitle.isEmpty, let value = organization["title"], !value.isEmpty { contact.jobTitle = value }
        if contact.departmentName.isEmpty, let value = organization["department"], !value.isEmpty { contact.departmentName = value }
    }
    var phones = contact.phoneNumbers
    var knownPhones = Set(phones.map { normalizedPhone($0.value.stringValue) })
    for item in array(person, "phoneNumbers") {
        guard let value = item["value"], !value.isEmpty else { continue }
        if knownPhones.insert(normalizedPhone(value)).inserted {
            phones.append(CNLabeledValue(label: phoneLabel(item["type"] ?? "work"), value: CNPhoneNumber(stringValue: value)))
        }
    }
    contact.phoneNumbers = phones
    func textKey(_ value: String, url: Bool) -> String {
        var result = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if url {
            result = replace(result, #"^https?://(www\.)?"#)
            while result.hasSuffix("/") { result.removeLast() }
        }
        return result
    }
    func mergeText(_ old: [CNLabeledValue<NSString>], _ incoming: [[String: String]], url: Bool) -> [CNLabeledValue<NSString>] {
        var result = old
        var seen = Set(old.map { textKey($0.value as String, url: url) })
        for item in incoming {
            guard let value = item["value"], !value.isEmpty else { continue }
            if seen.insert(textKey(value, url: url)).inserted {
                result.append(CNLabeledValue(label: phoneLabel(item["type"] ?? "work"), value: value as NSString))
            }
        }
        return result
    }
    contact.emailAddresses = mergeText(contact.emailAddresses, array(person, "emailAddresses"), url: false)
    contact.urlAddresses = mergeText(contact.urlAddresses, array(person, "urls"), url: true)
    func addressKey(_ value: String) -> String { replace(value, #"\s+"#).lowercased() }
    var addresses = contact.postalAddresses
    var knownAddresses = Set(addresses.map { addressKey(CNPostalAddressFormatter.string(from: $0.value, style: .mailingAddress)) })
    for item in array(person, "addresses") {
        guard let value = item["formattedValue"], !value.isEmpty else { continue }
        if knownAddresses.insert(addressKey(value)).inserted {
            let postal = CNMutablePostalAddress()
            postal.street = value
            addresses.append(CNLabeledValue(label: phoneLabel(item["type"] ?? "work"), value: postal))
        }
    }
    contact.postalAddresses = addresses
}

func automationName(_ code: OSStatus) -> String {
    switch code {
    case 0: return "authorized"
    case -1744: return "not_determined"
    case -1743: return "denied"
    case -600, -1712: return "timeout"
    default: return "error"
    }
}

func scriptErrorKind(_ code: Int) -> String {
    switch code {
    case -1744, -1743: return "auth"
    case -1728: return "not_found"
    case -600, -1712: return "transient"
    default: return "other"
    }
}

func automationPermission(_ ask: Bool) throws -> OSStatus {
    // A stopped target returns -600; open without activating before the probe.
    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    open.arguments = ["-gj", "-b", "com.apple.AddressBook"]
    open.standardOutput = FileHandle.nullDevice
    open.standardError = FileHandle.nullDevice
    try open.run()
    open.waitUntilExit()
    guard open.terminationStatus == 0 else { try fail("transient", "연락처 앱을 열지 못했습니다.") }
    let target = NSAppleEventDescriptor(bundleIdentifier: "com.apple.AddressBook")
    return AEDeterminePermissionToAutomateTarget(target.aeDesc, typeWildCard, typeWildCard, ask)
}

func callHandler(_ source: String, _ handler: String, _ values: [String]) throws -> NSAppleEventDescriptor {
    guard let script = NSAppleScript(source: source) else { try fail("other", "메모 스크립트를 준비하지 못했습니다.") }
    let event = NSAppleEventDescriptor(eventClass: AEEventClass(kASAppleScriptSuite),
                                      eventID: AEEventID(kASSubroutineEvent),
                                      targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
                                      transactionID: AETransactionID(kAnyTransactionID))
    event.setParam(NSAppleEventDescriptor(string: handler), forKeyword: AEKeyword(keyASSubroutineName))
    let arguments = NSAppleEventDescriptor.list()
    for (index, value) in values.enumerated() {
        arguments.insert(NSAppleEventDescriptor(string: value), at: index + 1)
    }
    event.setParam(arguments, forKeyword: AEKeyword(keyDirectObject))
    var error: NSDictionary?
    let result = script.executeAppleEvent(event, error: &error)
    if let failure = error {
        let code = (failure[NSAppleScript.errorNumber] as? NSNumber)?.intValue ?? 0
        try fail(scriptErrorKind(code), "연락처 메모를 추가하지 못했습니다.")
    }
    return result
}

let noteScript = """
on setnote(cid, marker, body)
    with timeout of 15 seconds
        tell application id "com.apple.AddressBook"
            set targetPerson to person id cid
            set oldNote to note of targetPerson
            if oldNote is missing value then set oldNote to ""
            if oldNote contains marker then return false
            if oldNote is "" then
                set note of targetPerson to body
            else
                set note of targetPerson to oldNote & linefeed & linefeed & body
            end if
            save
            return true
        end tell
    end timeout
end setnote
"""

func execute(_ input: [String: Any]) throws -> [String: Any] {
    let command = input["command"] as? String ?? ""
    if command == "status" { return ["status": statusName()] }
    if command == "automation-status" || command == "automation-request" {
        let code = try automationPermission(command == "automation-request")
        return ["status": automationName(code), "code": code]
    }
    let store = CNContactStore()
    if command == "set-note" {
        guard let identifier = input["identifier"] as? String, !identifier.isEmpty,
              let marker = input["marker"] as? String, !marker.isEmpty,
              let body = input["body"] as? String, !body.isEmpty else {
            try fail("invalid", "메모 입력값이 없습니다.")
        }
        // Check and send from the same executable/TCC identity. Never prompt here.
        let code = try automationPermission(false)
        guard code == 0 else { try fail(scriptErrorKind(Int(code)), "연락처 자동화 권한을 확인하세요.") }
        try requireAccess()
        _ = try fetchPinned(store, input)
        let result = try callHandler(noteScript, "setnote", [identifier, marker, body])
        return ["written": result.booleanValue]
    }
    if command == "request" {
        if statusName() == "notDetermined" {
            let semaphore = DispatchSemaphore(value: 0)
            store.requestAccess(for: .contacts) { _, _ in semaphore.signal() }
            // Python bounds the whole helper call at 30s. Pump the run loop for the TCC reply.
            let deadline = Date().addingTimeInterval(28)
            while semaphore.wait(timeout: .now()) != .success {
                if Date() >= deadline { try fail("transient", "연락처 권한 응답 시간이 초과되었습니다.") }
                _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
            }
        }
        return ["status": statusName()]
    }
    try requireAccess()
    switch command {
    case "containers":
        return ["containers": try store.containers(matching: nil).map {
            ["id": $0.identifier, "name": $0.name, "type": $0.type.rawValue] as [String: Any]
        }, "defaultIdentifier": store.defaultContainerIdentifier()]
    case "find", "find-fingerprint", "find-job-change":
        let container = try selectedContainer(store, input)
        let request = CNContactFetchRequest(keysToFetch: findKeys)
        request.predicate = CNContact.predicateForContactsInContainer(withIdentifier: container)
        request.unifyResults = false
        let phones = Set((input["phones"] as? [String] ?? []).map(normalizedPhone))
        let emails = Set((input["emails"] as? [String] ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() })
        let name = squish(input["name"] as? String ?? "")
        let displayName = squish(input["displayName"] as? String ?? "")
        var identifiers = Set<String>()
        try store.enumerateContacts(with: request) { contact, _ in
            // Same exact person-name boundary as the gws adapter; never match a longer person's name.
            let values = [squish(contact.givenName), squish(contact.givenName + " " + contact.familyName),
                          squish(CNContactFormatter.string(from: contact, style: .fullName) ?? "")]
            let matchesName = !name.isEmpty && values.contains {
                $0 == displayName || $0 == name || $0.hasPrefix(name + " ")
            }
            let numbers = Set(contact.phoneNumbers.map { normalizedPhone($0.value.stringValue) })
            let addresses = Set(contact.emailAddresses.map { ($0.value as String).trimmingCharacters(in: .whitespacesAndNewlines).lowercased() })
            let matchesKey = command == "find-fingerprint"
                ? ((!phones.isEmpty || !emails.isEmpty) && phones.isSubset(of: numbers) && emails.isSubset(of: addresses))
                : (!phones.isDisjoint(with: numbers) || (command == "find-job-change" && !emails.isDisjoint(with: addresses)))
            if matchesName && matchesKey { identifiers.insert(contact.identifier) }
        }
        return ["identifiers": identifiers.sorted(), "count": identifiers.count]
    case "get":
        let contact = try fetchPinned(store, input, photo: true)
        return ["identifier": contact.identifier, "person": personJSON(contact, photo: true)]
    case "create", "update":
        guard let person = input["person"] as? [String: Any] else { try fail("invalid", "연락처 입력값이 없습니다.") }
        let contact: CNMutableContact
        let request = CNSaveRequest()
        if command == "create" {
            let container = try selectedContainer(store, input)
            contact = CNMutableContact()
            apply(person, to: contact)
            request.add(contact, toContainerWithIdentifier: container)
        } else {
            contact = try fetchPinned(store, input)
            apply(person, to: contact, replaceOrganization: input["replaceOrganization"] as? Bool ?? false)
            request.update(contact)
        }
        try store.execute(request)
        return ["identifier": contact.identifier]
    case "set-photo":
        let contact = try fetchPinned(store, input, photo: true)
        guard let path = input["imagePath"] as? String,
              let image = try? Data(contentsOf: URL(fileURLWithPath: path)), !image.isEmpty else {
            try fail("invalid", "명함 사진 파일을 읽을 수 없습니다.")
        }
        contact.imageData = image
        let request = CNSaveRequest()
        request.update(contact)
        try store.execute(request)
        return ["identifier": contact.identifier]
    default: try fail("invalid", "지원하지 않는 연락처 명령입니다.")
    }
}

do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard let input = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        try fail("invalid", "JSON 객체가 필요합니다.")
    }
    emit(try execute(input))
} catch let error as HelperError {
    emit(["error": ["kind": error.kind, "message": error.message, "retryable": error.kind == "transient"]])
    exit(1)
} catch {
    let native = error as NSError
    var kind = "other"
    if native.domain == CNErrorDomain {
        switch CNError.Code(rawValue: native.code) {
        case .authorizationDenied: kind = "auth"
        case .recordDoesNotExist: kind = "not_found"
        case .communicationError, .dataAccessError: kind = "transient"
        case .noAccessableWritableContainers, .validationMultipleErrors,
             .validationTypeMismatch, .validationConfigurationError: kind = "invalid"
        default: break
        }
    } else if native.domain == NSCocoaErrorDomain && native.code == NSPropertyListReadCorruptError {
        kind = "invalid"
    }
    // Native localizedDescription can contain contact values: never emit it.
    emit(["error": ["kind": kind, "message": kind == "auth" ? authMessage : "Mac 연락처 작업에 실패했습니다.",
                     "retryable": kind == "transient", "code": native.code]])
    exit(1)
}
