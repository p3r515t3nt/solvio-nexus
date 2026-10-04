import Foundation

/// Display data from the authenticated, fresh Core HA catalogue. Names never
/// become entity identifiers or authority; no arbitrary attributes are decoded.
public struct TaskHomeDevice: Codable, Equatable, Hashable, Sendable {
    public let target: [String: String]
    public let name, area, domain, state: String
    public let operations: [String]
    public var entityID: String { target["entity_id"] ?? "" }
    public var displayLabel: String { area.isEmpty ? name : "\(name) · \(area)" }
    public var stateLabel: String {
        switch state { case "on": return "An"; case "off": return "Aus"
        case "unavailable": return "Nicht erreichbar"; default: return "Zustand unbekannt" }
    }
    enum CodingKeys: String, CodingKey { case target, name, area, domain, state, operations }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["target", "name", "area", "domain", "state", "operations"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        target = try c.decode([String: String].self, forKey: .target)
        name = try c.decode(String.self, forKey: .name); area = try c.decode(String.self, forKey: .area)
        domain = try c.decode(String.self, forKey: .domain); state = try c.decode(String.self, forKey: .state)
        operations = try c.decode([String].self, forKey: .operations)
        try validate()
    }
    public static func validEntity(_ value: String) -> Bool {
        value.count <= 256 && value.range(of: #"\A(?:light|switch|input_boolean)\.[a-z0-9_]+\z"#, options: .regularExpression) != nil
    }
    private func validate() throws {
        guard Set(target.keys) == ["entity_id"], Self.validEntity(entityID),
              entityID.split(separator: ".").first.map(String.init) == domain,
              !name.isEmpty, name.unicodeScalars.count <= 200, area.unicodeScalars.count <= 200,
              state.unicodeScalars.count <= 64,
              [name, area, state].allSatisfy({ !$0.unicodeScalars.contains(where: { $0.value < 32 }) }),
              operations.contains("set_state"), Set(operations).count == operations.count,
              operations.allSatisfy({ $0 == "set_state" || ($0 == "set_brightness" && domain == "light") })
        else { throw TaskActionError.invalidFields }
    }
}

public struct TaskHomeResources: Codable, Equatable, Sendable {
    public let service, account: String
    public let items: [TaskHomeDevice]
    public let truncated: Bool
    enum CodingKeys: String, CodingKey { case service, account, items, truncated }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["service", "account", "items", "truncated"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        service = try c.decode(String.self, forKey: .service); account = try c.decode(String.self, forKey: .account)
        items = try c.decode([TaskHomeDevice].self, forKey: .items); truncated = try c.decode(Bool.self, forKey: .truncated)
        guard service == "ha", Self.validAccount(account), items.count <= 100,
              Set(items.map(\.entityID)).count == items.count else { throw TaskActionError.invalidFields }
    }
    public static func validAccount(_ value: String) -> Bool {
        value.range(of: #"\Aha-[a-f0-9]{32}\z"#, options: .regularExpression) != nil
    }
    public static func decode(_ data: Data, account: ActionServiceAccount) throws -> Self {
        guard data.count <= 160_000, account.service == "ha" else { throw TaskActionError.invalidFields }
        _ = try account.validated()
        let result = try JSONDecoder().decode(Self.self, from: data)
        guard result.account == account.account else { throw TaskActionError.selectedAccountUnavailable }
        return result
    }
}

/// No device or operation is preselected. Failed/changed reads invalidate the
/// actionable catalogue, while a matching explicit choice may survive refresh.
public struct TaskHomeForm: Equatable, Sendable {
    public enum Desired: String, CaseIterable, Sendable { case on, off, brightness }
    public private(set) var catalogue: TaskHomeResources?
    public private(set) var selectedEntityID: String?
    private var selectedAccount: String?
    // Capture the chosen label once: a later catalogue rename is display data,
    // not a user edit to the already signed objective or its retry digest.
    private var selectedName: String?
    public var desired: Desired?
    public var brightnessPercent = 50
    public init() {}
    public mutating func invalidate() { catalogue = nil }
    public mutating func accept(_ value: TaskHomeResources, account: ActionServiceAccount) throws {
        guard account.service == "ha", value.account == account.account else { throw TaskActionError.selectedAccountUnavailable }
        _ = try account.validated()
        if selectedAccount != account.account { selectedEntityID = nil; selectedName = nil; desired = nil }
        selectedAccount = account.account; catalogue = value
    }
    public mutating func select(_ entityID: String?) throws {
        guard let catalogue else { throw TaskActionError.devicesUnavailable }
        guard let entityID else { selectedEntityID = nil; selectedName = nil; desired = nil; return }
        guard let device = catalogue.items.first(where: { $0.entityID == entityID }) else { throw TaskActionError.selectedDeviceUnavailable }
        if selectedEntityID != entityID { desired = nil; selectedName = device.name }
        selectedEntityID = entityID
    }
    /// Catalogue reads can change availability without changing the user's task.
    /// The current catalogue remains mandatory in action(account:).
    public func hasSameInput(as other: Self) -> Bool {
        selectedAccount == other.selectedAccount && selectedEntityID == other.selectedEntityID
            && selectedName == other.selectedName && desired == other.desired
            && brightnessPercent == other.brightnessPercent
    }
    public var selectedDevice: TaskHomeDevice? {
        catalogue?.items.first { $0.entityID == selectedEntityID }
    }
    public func action(account: ActionServiceAccount) throws -> AppTaskAction {
        guard let catalogue, catalogue.account == account.account, selectedAccount == account.account,
              account.service == "ha", account.resource == "configured_home" else { throw TaskActionError.devicesUnavailable }
        guard selectedEntityID != nil else { throw TaskActionError.deviceSelectionRequired }
        guard let device = selectedDevice else { throw TaskActionError.selectedDeviceUnavailable }
        guard let desired else { throw TaskActionError.deviceOperationRequired }
        let operation = desired == .brightness ? "set_brightness" : "set_state"
        guard device.operations.contains(operation) else { throw TaskActionError.deviceOperationRequired }
        let payload: [String: TaskActionValue] = desired == .brightness ? ["brightness_pct": .number(brightnessPercent)] : ["state": .string(desired.rawValue)]
        return try AppTaskAction(actionID: "home1", service: "ha", operation: operation,
            account: account.account, target: device.target, payload: payload)
    }
    public var objective: String {
        let name = selectedName ?? "gewähltes Gerät"
        switch desired {
        case .on: return "Schalte das Gerät „\(name)“ ein."
        case .off: return "Schalte das Gerät „\(name)“ aus."
        case .brightness: return "Stelle das Licht „\(name)“ auf \(brightnessPercent) Prozent Helligkeit."
        case nil: return "Steuere das ausgewählte Gerät mit der angegebenen Einstellung."
        }
    }
}
