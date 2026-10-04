import Foundation

/// Resolve the owner's unchanged objective within the existing task. This
/// marker contains no proposed action, account, target or additional grant.
public struct AppTaskActionIntent: Codable, Equatable, Sendable {
    public let version: Int
    enum CodingKeys: String, CodingKey { case version }

    public init() { version = 1 }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["version"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        guard try fields.decode(Int.self, forKey: .version) == 1 else {
            throw TaskStartError.invalidBody
        }
        self.init()
    }

    var json: TaskCanonicalValue { .object(["version": .number(version)]) }
}
