import XCTest
@testable import KoreAgentNextComputerCore

final class ProtocolTests: XCTestCase {
    func testDecodesStatusRequest() throws {
        let data = #"{"id":1,"method":"status","params":{}}"#.data(using: .utf8)!
        let request = try JSONDecoder().decode(HelperRequest.self, from: data)
        XCTAssertEqual(request.method, "status")
        XCTAssertEqual(request.id, .number(1))
    }
    func testUnknownMethodIsStructuredError() throws {
        let data = #"{"id":"x","method":"missing","params":{}}"#.data(using: .utf8)!
        let request = try JSONDecoder().decode(HelperRequest.self, from: data)
        XCTAssertThrowsError(try ComputerController().handle(request))
    }
}

