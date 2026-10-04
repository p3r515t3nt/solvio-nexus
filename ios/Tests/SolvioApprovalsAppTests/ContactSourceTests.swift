import Contacts
import XCTest
@testable import SolvioApprovals

final class ContactSourceTests: XCTestCase {
    func testProjectionCarriesExactAddressesButNoOtherContactFields() throws {
        let contact = CNMutableContact()
        contact.givenName = "Alex"; contact.familyName = "Winter"
        contact.emailAddresses = [CNLabeledValue(label: CNLabelHome, value: "A.Winter@example.test" as NSString),
                                  CNLabeledValue(label: CNLabelWork, value: "office@example.test" as NSString)]
        contact.phoneNumbers = [CNLabeledValue(label: CNLabelHome, value: CNPhoneNumber(stringValue: "+1-202-555-0100"))]
        contact.organizationName = "Private organization"
        let value = try XCTUnwrap(AddressBookReader.project(contact))
        XCTAssertEqual(value.emails, ["A.Winter@example.test", "office@example.test"])
        let encoded = try JSONEncoder().encode(value)
        let data = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(Set(data.keys), Set(["name", "emails"]))
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("Private organization"))
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("555-0100"))
    }

    func testContactsWithoutAnEmailAreNotUploaded() {
        let contact = CNMutableContact(); contact.givenName = "Alex"
        XCTAssertNil(AddressBookReader.project(contact))
    }

    func testImportedCandidateDoesNotDecodeAsAConfirmedBinding() throws {
        let raw = Data("{\"display_name\":\"Alex Winter\",\"handles\":[{\"channel\":\"gmail\",\"value\":\"alex@example.test\"}],\"confirmed\":false}".utf8)
        let candidate = try JSONDecoder().decode(ContactMatch.self, from: raw)
        XCTAssertEqual(candidate.confirmed, false)
        XCTAssertNil(candidate.alias)
    }
}
