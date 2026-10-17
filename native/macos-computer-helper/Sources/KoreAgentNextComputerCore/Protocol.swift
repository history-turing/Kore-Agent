import Foundation

public struct HelperRequest: Decodable {
    public let id: JSONValue
    public let method: String
    public let params: [String: JSONValue]
}

public indirect enum JSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        // Try Bool before Double to preserve the boolean case when decoding
        // JSON values that pass through Foundation's number representation.
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    public var string: String? { if case .string(let v) = self { return v }; return nil }
    public var number: Double? { if case .number(let v) = self { return v }; return nil }
    public var array: [JSONValue]? { if case .array(let v) = self { return v }; return nil }
}

public struct HelperResponse: Encodable {
    public let id: JSONValue
    public let result: JSONValue?
    public let error: HelperError?
    public init(id: JSONValue, result: JSONValue) { self.id = id; self.result = result; self.error = nil }
    public init(id: JSONValue, error: HelperError) { self.id = id; self.result = nil; self.error = error }
}

public struct HelperError: Encodable, Error {
    public let code: String
    public let message: String
    public init(_ code: String, _ message: String) { self.code = code; self.message = message }
}

