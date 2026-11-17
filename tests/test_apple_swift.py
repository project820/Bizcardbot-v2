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

    def test_native_job_change_opt_in_replaces_scalars_and_preserves_union(self):
        source = Path('scripts/apple_contacts.swift').read_text()
        prefix = source[:source.index('func execute(')]
        program = prefix + '''
let c = CNMutableContact()
c.givenName = "Old Display"
c.familyName = "Old Family"
c.organizationName = "Old Org"
c.jobTitle = "Old Role"
c.departmentName = "Old Dept"
c.phoneNumbers = [CNLabeledValue(label: "custom", value: CNPhoneNumber(stringValue: "010-0000-1111"))]
c.emailAddresses = [CNLabeledValue(label: "custom", value: "old@example.invalid" as NSString)]
c.urlAddresses = [CNLabeledValue(label: "custom", value: "https://old.example.invalid" as NSString)]
let address = CNMutablePostalAddress()
address.street = "Old Street"
c.postalAddresses = [CNLabeledValue(label: "custom", value: address)]
let incoming: [String: Any] = ["names": [["givenName": "New Display"]], "organizations": [["name": "New Org", "title": "New Role"]], "phoneNumbers": [["value": "010-0000-2222"]], "emailAddresses": [["value": "new@example.invalid"]], "urls": [["value": "https://new.example.invalid"]], "addresses": [["formattedValue": "New Street"]]]
apply(incoming, to: c, replaceOrganization: true)
guard c.givenName == "New Display" && c.familyName.isEmpty else { exit(41) }
guard c.organizationName == "New Org" && c.jobTitle == "New Role" && c.departmentName == "Old Dept" else { exit(42) }
guard c.phoneNumbers.count == 2 && c.emailAddresses.count == 2 && c.urlAddresses.count == 2 && c.postalAddresses.count == 2 else { exit(43) }
guard c.phoneNumbers.first?.label == "custom" else { exit(44) }
let first = try! JSONSerialization.data(withJSONObject: personJSON(c, photo: false), options: [.sortedKeys])
apply(incoming, to: c, replaceOrganization: true)
let second = try! JSONSerialization.data(withJSONObject: personJSON(c, photo: false), options: [.sortedKeys])
guard first == second else { exit(45) }
print("PASS: native opt-in scalar replacement, union and replay")
'''
        with tempfile.TemporaryDirectory() as folder:
            path, binary = Path(folder)/'jobchange.swift', Path(folder)/'jobchange'
            path.write_text(program)
            build = subprocess.run(['swiftc', str(path), '-o', str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class SwiftPhoneNormalizationTests(unittest.TestCase):
    def test_matches_python_on_korean_and_cjk_corpus(self):
        import json
        from bizcard_intake.contacts import normalize_phone
        import importlib.util
        spec = importlib.util.spec_from_file_location('cjk_preservation_fixture', Path(__file__).with_name('test_cjk_preservation.py'))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        korean_phone_corpus = fixture.korean_phone_corpus

        # Compile the production function without Contacts or address-book access.
        source = Path('scripts/apple_contacts.swift').read_text()
        functions = source[source.index('func replace('):source.index('func phoneLabel(')]
        program = 'import Foundation\n' + functions + '''
let data = FileHandle.standardInput.readDataToEndOfFile()
let values = try JSONDecoder().decode([String].self, from: data)
FileHandle.standardOutput.write(try JSONEncoder().encode(values.map(normalizedPhone)))
'''
        cjk = [
            '+81 90-0000-0000', '0081 90-0000-0000', '+81 (0)90-0000-0000',
            '+81 090-0000-0000', '+86 138-0000-0000', '+86 010-5555-0100',
            '0086 10-5555-0100', '＋８１ （０）９０－００００－００００',
            '＋８６ １３８－００００－００００',
        ]
        for country in ('81', '86', '852', '886'):
            for prefix in ('+', '00'):
                for national in ('0 5555 0100', '(0) 5555 0100', '5555 0100'):
                    value = f'{prefix}{country} {national}'
                    cjk.extend((value, value.translate(str.maketrans(
                        '+0123456789()', '＋０１２３４５６７８９（）'))))
        values = korean_phone_corpus() + cjk + [
            '03-5555-0100', '06-5555-0100', '090-5555-0100',
            '070-5555-0100', '080-5555-0100', '+82 １０-５５５５-０１００',
        ]
        with tempfile.TemporaryDirectory() as folder:
            path, binary = Path(folder) / 'phone.swift', Path(folder) / 'phone'
            path.write_text(program)
            build = subprocess.run(['swiftc', str(path), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], input=json.dumps(values),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(values))
        for value, normalized in zip(values, actual):
            with self.subTest(phone=value):
                self.assertEqual(normalized, normalize_phone(value))


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift required')
class SwiftNoteTests(unittest.TestCase):
    def test_handler_arguments_and_automation_error_mapping(self):
        source = Path('scripts/apple_contacts.swift').read_text()
        self.assertIn('func callHandler(', source)
        prefix = source[:source.index('func execute(')]
        program = prefix + '''
let literal = "quote \\" and newline\\n한글; do shell script"
let script = "on echoargs(a, b, c)\\nreturn a & b & c\\nend echoargs"
let result = try callHandler(script, "echoargs", ["id", "[marker]", literal])
guard result.stringValue == "id[marker]" + literal else { exit(51) }
guard automationName(0) == "authorized" else { exit(52) }
guard automationName(-1744) == "not_determined" else { exit(53) }
guard automationName(-1743) == "denied" else { exit(54) }
guard scriptErrorKind(-1728) == "not_found" else { exit(55) }
for code in [-600, -1712] { guard scriptErrorKind(code) == "transient" else { exit(56) } }
var error: NSDictionary?
guard NSAppleScript(source: noteScript)?.compileAndReturnError(&error) == true else { exit(57) }
print("PASS: event arguments, permission/error mapping, note script syntax")
'''
        with tempfile.TemporaryDirectory() as folder:
            path, binary = Path(folder)/'note.swift', Path(folder)/'note'
            path.write_text(program)
            build = subprocess.run(['swiftc', str(path), '-o', str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
