import Foundation

/// Existing owner sessions from the Core. This catalogue has no credential,
/// login or navigation operation; display labels never select an account.
public struct TaskPortalSession: Codable, Equatable, Sendable {
    public let account: String
    public let target: [String: String]
    public let label, origin: String
    public let authenticated: Bool
    public let expires_in_s: Int
    public var sessionID: String { target["session_id"] ?? "" }
    public var displayLabel: String { label }
    enum CodingKeys: String, CodingKey { case account, target, label, origin, authenticated, expires_in_s }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["account", "target", "label", "origin", "authenticated", "expires_in_s"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        account = try c.decode(String.self, forKey: .account); target = try c.decode([String: String].self, forKey: .target)
        label = try c.decode(String.self, forKey: .label); origin = try c.decode(String.self, forKey: .origin)
        authenticated = try c.decode(Bool.self, forKey: .authenticated); expires_in_s = try c.decode(Int.self, forKey: .expires_in_s)
        guard Self.validAccount(account), Self.validTarget(target), (0...3600).contains(expires_in_s),
              !label.isEmpty, label.unicodeScalars.count <= 200,
              !label.unicodeScalars.contains(where: { $0.value < 32 }),
              let url = URLComponents(string: origin), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty, origin.count <= 500 else { throw TaskActionError.invalidFields }
    }
    public static func validAccount(_ value: String) -> Bool {
        value.range(of: #"\Aportal-[a-f0-9]{32}\z"#, options: .regularExpression) != nil
    }
    public static func validTarget(_ value: [String: String]) -> Bool {
        Set(value.keys) == ["portal_id", "session_id"]
            && value["portal_id"]?.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}\z"#, options: .regularExpression) != nil
            && value["session_id"]?.range(of: #"\Aps-[0-9]+-[0-9]+\z"#, options: .regularExpression) != nil
            && (value["session_id"]?.count ?? 129) <= 128
    }
}

public struct TaskPortalSessions: Codable, Equatable, Sendable {
    public let service: String
    public let items: [TaskPortalSession]
    public let truncated: Bool
    public let observed_at: Double
    enum CodingKeys: String, CodingKey { case service, items, truncated, observed_at }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["service", "items", "truncated", "observed_at"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        service = try c.decode(String.self, forKey: .service); items = try c.decode([TaskPortalSession].self, forKey: .items)
        truncated = try c.decode(Bool.self, forKey: .truncated); observed_at = try c.decode(Double.self, forKey: .observed_at)
        guard service == "portal", items.count <= 50, observed_at.isFinite, observed_at > 0,
              Set(items.map(\.sessionID)).count == items.count, Set(items.map(\.account)).count == items.count
        else { throw TaskActionError.invalidFields }
    }
    public func fresh(now: Date = Date()) -> Bool {
        let age = now.timeIntervalSince1970 - observed_at
        return age >= -5 && age <= 60
    }
    public func usable(_ item: TaskPortalSession, now: Date = Date()) -> Bool {
        fresh(now: now) && item.authenticated && item.expires_in_s > 0
            && now.timeIntervalSince1970 < observed_at + Double(item.expires_in_s)
    }
    /// A display-only ordinal distinguishes equally named own sessions. The
    /// selection continues to bind account and target, never this ordinal.
    public func displayLabel(for item: TaskPortalSession) -> String {
        let peers = items.filter { $0.label == item.label }.sorted { $0.sessionID < $1.sessionID }
        guard peers.count > 1, let index = peers.firstIndex(where: { $0.account == item.account && $0.target == item.target })
        else { return item.displayLabel }
        return "\(item.displayLabel) · Verbindung \(index + 1)"
    }
    public static func decode(_ data: Data, now: Date = Date()) throws -> Self {
        guard data.count <= 100_000 else { throw TaskActionError.invalidFields }
        let result = try JSONDecoder().decode(Self.self, from: data)
        guard result.fresh(now: now) else { throw TaskActionError.portalSessionsUnavailable }
        return result
    }
}

public struct TaskPortalForm: Equatable, Sendable {
    public private(set) var catalogue: TaskPortalSessions?
    public private(set) var selectedAccount: String?
    private var selectedTarget: [String: String]?
    private var selectedLabel: String?
    public init() {}
    public mutating func invalidate() { catalogue = nil }
    public mutating func accept(_ value: TaskPortalSessions) throws {
        guard value.fresh() else { throw TaskActionError.portalSessionsUnavailable }
        catalogue = value
    }
    public var selectedSession: TaskPortalSession? {
        catalogue?.items.first { $0.account == selectedAccount && $0.target == selectedTarget }
    }
    public mutating func select(_ account: String?) throws {
        guard let catalogue, catalogue.fresh() else { throw TaskActionError.portalSessionsUnavailable }
        guard let account else { selectedAccount = nil; selectedTarget = nil; selectedLabel = nil; return }
        guard let row = catalogue.items.first(where: { $0.account == account }), catalogue.usable(row)
        else { throw TaskActionError.portalSessionUnavailable }
        if selectedAccount != account || selectedTarget != row.target { selectedLabel = row.label }
        selectedAccount = account; selectedTarget = row.target
    }
    public func hasSameInput(as other: Self) -> Bool {
        selectedAccount == other.selectedAccount && selectedTarget == other.selectedTarget && selectedLabel == other.selectedLabel
    }
    public func action() throws -> AppTaskAction {
        guard let catalogue, catalogue.fresh() else { throw TaskActionError.portalSessionsUnavailable }
        guard selectedAccount != nil else { throw TaskActionError.portalSelectionRequired }
        guard let row = selectedSession, catalogue.usable(row) else { throw TaskActionError.portalSessionUnavailable }
        return try AppTaskAction(actionID: "portal1", service: "portal", operation: "status",
            account: row.account, target: row.target, payload: [:])
    }
    public var objective: String { "Lies den aktuellen Status meiner ausgewählten Sitzung im Portal „\(selectedLabel ?? "gewähltes Portal")“." }
}
