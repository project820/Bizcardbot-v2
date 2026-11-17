"""Compile real helper comparison code against in-memory Contacts only."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift Contacts required')
class SwiftComparisonTests(unittest.TestCase):
    def test_formatted_split_name_boundary_and_descriptor_without_notes(self):
        source = Path(os.environ.get('BIZCARD_SWIFT_TEST_SOURCE', 'scripts/apple_contacts.swift')).read_text()
        prefix = source[:source.index('func execute(')]
        start = source.index('            let values =')
        end = source.index('            let numbers =', start)
        comparison = source[start:end]
        program = prefix + '''
func checkName(_ contact: CNContact, _ name: String, _ displayName: String) -> Bool {
''' + comparison + '''
    return matchesName
}
let contact = CNMutableContact()
contact.givenName = "길동"
contact.familyName = "홍"
guard checkName(contact, "홍길동", "홍길동") else { exit(11) }
contact.familyName = ""
contact.givenName = "홍길동 담당 Example"
guard checkName(contact, "홍길동", "홍길동") else { exit(12) }
contact.givenName = "홍길동수"
guard !checkName(contact, "홍길동", "홍길동") else { exit(13) }
for keys in [personKeys, findKeys] {
    let descriptions = keys.map { String(describing: $0).lowercased() }
    guard descriptions.contains(where: { $0.contains("formatter") }) else { exit(14) }
    guard !descriptions.contains(where: { $0.contains("note") }) else { exit(15) }
}
print("PASS: split/full names, name boundary, formatter descriptors without note")
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'comparison.swift'
            binary = Path(folder)/'comparison'
            path.write_text(program)
            build = subprocess.run(['swiftc',str(path),'-o',str(binary)], capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            result = subprocess.run([str(binary)],capture_output=True,text=True,timeout=10,env={**os.environ,'LANG':'ko_KR.UTF-8'})
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_fingerprint_requires_fax_email_and_all_phone_values(self):
        source=Path('scripts/apple_contacts.swift').read_text()
        prefix=source[:source.index('func execute(')]
        start=source.index('            let values =')
        end=source.index('            if matchesName && matchesKey',start)
        comparison=source[start:end]
        program=prefix+'''
func matches(_ contact: CNContact, _ phones: Set<String>, _ emails: Set<String>) -> Bool {
    let command = "find-fingerprint"
    let name = "홍길동"
    let displayName = "홍길동"
'''+comparison+'''
    return matchesName && matchesKey
}
let contact = CNMutableContact()
contact.givenName = "홍길동"
contact.phoneNumbers = [CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: "+82 10 0000 0000")), CNLabeledValue(label: CNLabelPhoneNumberWorkFax, value: CNPhoneNumber(stringValue: "02-000-0001"))]
contact.emailAddresses = [CNLabeledValue(label: CNLabelWork, value: "TEST@EXAMPLE.COM" as NSString)]
guard matches(contact, Set(["01000000000", "020000001"]), Set(["test@example.com"])) else { exit(21) }
guard matches(contact, Set(["020000001"]), Set<String>()) else { exit(22) }
guard matches(contact, Set<String>(), Set(["test@example.com"])) else { exit(23) }
guard !matches(contact, Set(["01000000000", "020000002"]), Set(["test@example.com"])) else { exit(24) }
guard !matches(contact, Set(["01000000000"]), Set(["different@example.com"])) else { exit(25) }
guard !matches(contact, Set<String>(), Set<String>()) else { exit(26) }
print("PASS: full fingerprint phones, fax, email")
'''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fingerprint.swift'
            binary=Path(folder)/'fingerprint'
            path.write_text(program)
            build=subprocess.run(['swiftc',str(path),'-o',str(binary)],capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            result=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_native_update_union_preserves_scalars_and_is_idempotent(self):
        source = Path('scripts/apple_contacts.swift').read_text()
        prefix = source[:source.index('func execute(')]
        program = prefix + '''
let c = CNMutableContact()
c.givenName = "Old"
c.familyName = "Name"
c.organizationName = "Keep org"
c.jobTitle = "Keep role"
c.phoneNumbers = [CNLabeledValue(label: "custom", value: CNPhoneNumber(stringValue: "02-000-0000"))]
c.emailAddresses = [CNLabeledValue(label: "custom", value: "old@example.test" as NSString)]
let incoming: [String: Any] = ["names": [["givenName": "Replacement"]], "organizations": [["name": "Replacement", "title": "New", "department": "Added"]], "phoneNumbers": [["value": "01000000000"]], "emailAddresses": [["value": "new@example.test"]]]
apply(incoming, to: c)
guard c.givenName == "Old" && c.familyName == "Name" else { exit(31) }
guard c.organizationName == "Keep org" && c.jobTitle == "Keep role" && c.departmentName == "Added" else { exit(32) }
let first = try! JSONSerialization.data(withJSONObject: personJSON(c, photo: false), options: [.sortedKeys])
apply(incoming, to: c)
let second = try! JSONSerialization.data(withJSONObject: personJSON(c, photo: false), options: [.sortedKeys])
guard first == second && c.phoneNumbers.count == 2 && c.emailAddresses.count == 2 else { exit(33) }
guard c.phoneNumbers.first?.label == "custom" else { exit(34) }
print("PASS: native pure union and idempotent replay")
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'union.swift'
            binary = Path(folder)/'union'
            path.write_text(program)
            build = subprocess.run(['swiftc', str(path), '-o', str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
