import Foundation

/// The existing approval canonicalizer covers flat strings/integers. Task
/// resources add only nested objects, arrays and booleans; string escaping is
/// still the same protocol implementation (including unescaped Unicode).
indirect enum TaskCanonicalValue {
    case string(String), number(Int), bool(Bool), array([TaskCanonicalValue]), object([String: TaskCanonicalValue])
    var encoded: String {
        switch self {
        case let .string(value): return ApprovalProtocol.encodeString(value)
        case let .number(value): return String(value)
        case let .bool(value): return value ? "true" : "false"
        case let .array(value): return "[" + value.map(\.encoded).joined(separator: ",") + "]"
        case let .object(value): return "{" + value.keys.sorted().map {
            ApprovalProtocol.encodeString($0) + ":" + value[$0]!.encoded
        }.joined(separator: ",") + "}"
        }
    }
}

struct TaskWireKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

func requireTaskKeys(_ decoder: Decoder, required: Set<String>, optional: Set<String> = []) throws {
    let keys = Set(try decoder.container(keyedBy: TaskWireKey.self).allKeys.map(\.stringValue))
    guard required.isSubset(of: keys), keys.isSubset(of: required.union(optional)) else { throw TaskActionError.invalidFields }
}

public enum TaskActionError: Error, Equatable, LocalizedError {
    case invalidFields, accountUnavailable, ambiguousAccount, selectedAccountUnavailable, invalidDates
    case devicesUnavailable, deviceSelectionRequired, selectedDeviceUnavailable, deviceOperationRequired
    case portalSessionsUnavailable, portalSelectionRequired, portalSessionUnavailable
    public var errorDescription: String? {
        switch self {
        case .invalidFields: return "Bitte fülle die Angaben vollständig und gültig aus."
        case .accountUnavailable: return "Für diese Aufgabe ist noch kein Konto im Core verfügbar."
        case .ambiguousAccount: return "Bitte wähle das Konto für diesen Auftrag aus."
        case .selectedAccountUnavailable: return "Das gewählte Konto ist nicht mehr verfügbar. Bitte wähle das passende Konto neu."
        case .invalidDates: return "Das Ende muss nach dem Beginn liegen."
        case .devicesUnavailable: return "Lade zuerst die freigegebenen Geräte vom Core."
        case .deviceSelectionRequired: return "Bitte wähle ein Gerät aus."
        case .selectedDeviceUnavailable: return "Das gewählte Gerät ist nicht mehr verfügbar. Bitte wähle es erneut."
        case .deviceOperationRequired: return "Bitte wähle, was mit dem Gerät geschehen soll."
        case .portalSessionsUnavailable: return "Lade zuerst die aktuellen Portal-Sitzungen vom Core."
        case .portalSelectionRequired: return "Bitte wähle eine bestätigte Portal-Sitzung aus."
        case .portalSessionUnavailable: return "Diese Sitzung ist nicht mehr als angemeldet bestätigt. Bitte lade die Sitzungen erneut."
        }
    }
}

public struct ActionServiceAccount: Codable, Hashable, Sendable {
    public let service, account, resource: String
    public let label: String?
    public init(service: String, account: String, resource: String, label: String? = nil) {
        self.service = service; self.account = account; self.resource = resource
        self.label = label
    }
    public static func ==(lhs: Self, rhs: Self) -> Bool {
        lhs.service == rhs.service && lhs.account == rhs.account && lhs.resource == rhs.resource
    }
    public func hash(into hasher: inout Hasher) {
        hasher.combine(service); hasher.combine(account); hasher.combine(resource)
    }
    public var displayLabel: String {
        let supplied = label?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return supplied.isEmpty ? (service == "calendar" ? "Google-Kalender" : service == "ha" ? "Home Assistant" : "Gmail") : String(supplied.prefix(160))
    }
    public static func unique(_ rows: [Self], service: String) throws -> Self {
        let matches = rows.filter { $0.service == service }
        guard !matches.isEmpty else { throw TaskActionError.accountUnavailable }
        guard matches.count == 1 else { throw TaskActionError.ambiguousAccount }
        return try matches[0].validated()
    }
    func validated() throws -> Self {
        guard ["calendar", "gmail", "ha"].contains(service), identifier(account), identifier(resource),
              service != "gmail" || resource == "me",
              service != "ha" || (resource == "configured_home" && TaskHomeResources.validAccount(account))
        else { throw TaskActionError.invalidFields }
        return self
    }
}

private func identifier(_ value: String) -> Bool {
    value.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}\z"#, options: .regularExpression) != nil
}
private func text(_ value: String, max: Int, empty: Bool = false) -> Bool {
    value.unicodeScalars.count <= max && (empty || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        && !value.unicodeScalars.contains { $0.value < 32 && $0.value != 10 && $0.value != 9 }
}

public enum TaskActionValue: Codable, Equatable, Sendable {
    case string(String), bool(Bool), number(Int)
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else { self = .number(try container.decode(Int.self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value): try container.encode(value)
        case let .bool(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        }
    }
    var json: TaskCanonicalValue {
        switch self { case let .string(value): return .string(value); case let .bool(value): return .bool(value)
        case let .number(value): return .number(value) }
    }
}

/// Exact calendar, draft and exposed-home actions; existing signed task scope.
public struct AppTaskAction: Codable, Equatable, Sendable {
    public let action_id, service, operation, account: String
    public let target: [String: String]
    public let payload: [String: TaskActionValue]
    enum CodingKeys: String, CodingKey { case action_id, service, operation, account, target, payload }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["action_id", "service", "operation", "account", "target", "payload"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(actionID: fields.decode(String.self, forKey: .action_id),
            service: fields.decode(String.self, forKey: .service), operation: fields.decode(String.self, forKey: .operation),
            account: fields.decode(String.self, forKey: .account), target: fields.decode([String: String].self, forKey: .target),
            payload: fields.decode([String: TaskActionValue].self, forKey: .payload))
    }

    public init(actionID: String, service: String, operation: String, account: String,
                target: [String: String], payload: [String: TaskActionValue]) throws {
        action_id = actionID; self.service = service; self.operation = operation
        self.account = account; self.target = target; self.payload = payload
        try validate()
    }
    public func validate() throws {
        guard action_id.range(of: #"\A[a-z][a-z0-9_]{0,15}\z"#, options: .regularExpression) != nil,
              identifier(account) else { throw TaskActionError.invalidFields }
        func string(_ key: String) -> String? { if case let .string(value) = payload[key] { return value }; return nil }
        if service == "calendar" && operation == "create" {
            guard Set(target.keys) == ["calendar_id"], target["calendar_id"].map(identifier) == true,
                  Set(payload.keys) == ["summary", "start", "end", "all_day", "description", "location"],
                  payload["all_day"] == .bool(false),
                  let title = string("summary"), text(title, max: 500),
                  let description = string("description"), text(description, max: 4000, empty: true),
                  let location = string("location"), text(location, max: 500, empty: true),
                  let start = string("start"), let end = string("end") else { throw TaskActionError.invalidFields }
            let parser = ISO8601DateFormatter()
            guard start.contains("T"), end.contains("T"), let a = parser.date(from: start),
                  let b = parser.date(from: end), b > a else { throw TaskActionError.invalidDates }
        } else if service == "gmail" && ["create_draft", "compose_draft"].contains(operation) {
            guard target["mailbox"] == "me", let recipient = target["to"], recipient.count <= 254,
                  recipient.range(of: #"\A[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\z"#, options: .regularExpression) != nil else { throw TaskActionError.invalidFields }
            if operation == "compose_draft" {
                guard Set(target.keys) == ["mailbox", "to"], Set(payload.keys) == ["instruction"],
                      let instruction = string("instruction"), text(instruction, max: 4000) else { throw TaskActionError.invalidFields }
                return
            }
            guard Set(target.keys) == ["mailbox", "to", "reply_to_message"], target["reply_to_message"] == "",
                  Set(payload.keys) == ["subject", "body", "thread_id", "in_reply_to"],
                  payload["thread_id"] == .string(""), payload["in_reply_to"] == .string(""),
                  let subject = string("subject"), text(subject, max: 500), !subject.contains("\n"),
                  let body = string("body"), text(body, max: 8000) else { throw TaskActionError.invalidFields }
        } else if service == "ha" && ["set_state", "set_brightness"].contains(operation) {
            guard TaskHomeResources.validAccount(account), Set(target.keys) == ["entity_id"],
                  let entity = target["entity_id"], TaskHomeDevice.validEntity(entity) else { throw TaskActionError.invalidFields }
            if operation == "set_state" {
                guard Set(payload.keys) == ["state"], [.string("on"), .string("off")].contains(payload["state"])
                else { throw TaskActionError.invalidFields }
            } else {
                guard entity.hasPrefix("light."), Set(payload.keys) == ["brightness_pct"],
                      case let .number(value) = payload["brightness_pct"], (0...100).contains(value)
                else { throw TaskActionError.invalidFields }
            }
        } else if service == "portal" && operation == "status" {
            guard TaskPortalSession.validAccount(account), TaskPortalSession.validTarget(target), payload.isEmpty
            else { throw TaskActionError.invalidFields }
        } else { throw TaskActionError.invalidFields }
    }
    var json: TaskCanonicalValue {
        .object(["action_id": .string(action_id), "service": .string(service), "operation": .string(operation),
                 "account": .string(account), "target": .object(target.mapValues(TaskCanonicalValue.string)),
                 "payload": .object(payload.mapValues(\.json))])
    }
}

public struct AppTaskActionRequest: Codable, Equatable, Sendable {
    public let actions: [AppTaskAction]
    enum CodingKeys: String, CodingKey { case actions }
    public init(actions: [AppTaskAction]) throws { self.actions = actions; try validate() }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["actions"])
        try self.init(actions: decoder.container(keyedBy: CodingKeys.self).decode([AppTaskAction].self, forKey: .actions))
    }
    public func validate() throws {
        guard (1...5).contains(actions.count), Set(actions.map(\.action_id)).count == actions.count else {
            throw TaskActionError.invalidFields
        }
        try actions.forEach { try $0.validate() }
        guard json.encoded.unicodeScalars.count <= 32_000 else { throw TaskActionError.invalidFields }
    }
    var json: TaskCanonicalValue { .object(["actions": .array(actions.map(\.json))]) }
}

/// Editable local form. Its values enter the same signed task body and retry digest.
public struct TaskActionForm: Equatable, Sendable {
    public enum Kind: String, Hashable, CaseIterable, Sendable { case calendar, gmail, ha, portal }
    public enum MailMode: String, Hashable, Sendable { case compose, exact }
    public var kind: Kind = .calendar
    public var mailMode: MailMode = .compose
    public var title = "", location = "", details = "", recipient = "", instruction = "", subject = "", body = ""
    public var start: Date, end: Date
    public var home = TaskHomeForm()
    public var portal = TaskPortalForm()
    public let timeZoneIdentifier: String
    // Keep the complete offered identity, including its resource. A reload may
    // make it unavailable, but must not silently substitute a new singleton.
    private var accountSelections: [Kind: ActionServiceAccount] = [:]
    public var selectedAccount: ActionServiceAccount? { accountSelections[kind] }
    /// An offered catalogue is not an edit. Compare every form field while
    /// excluding read metadata and accounts for inactive service forms.
    public func hasSameInput(as other: Self) -> Bool {
        var left = self, right = other
        left.home = TaskHomeForm(); right.home = TaskHomeForm()
        left.portal = TaskPortalForm(); right.portal = TaskPortalForm()
        left.accountSelections = selectedAccount.map { [kind: $0] } ?? [:]
        right.accountSelections = other.selectedAccount.map { [other.kind: $0] } ?? [:]
        return left == right && home.hasSameInput(as: other.home) && portal.hasSameInput(as: other.portal)
    }
    public init(now: Date = Date(), timeZone: TimeZone = .current) {
        let date = Date(timeIntervalSince1970: ceil(now.timeIntervalSince1970 / 3600) * 3600)
        start = date; end = date.addingTimeInterval(3600); timeZoneIdentifier = timeZone.identifier
    }
    public mutating func reconcileAccounts(_ accounts: [ActionServiceAccount]) {
        for kind in Kind.allCases where accountSelections[kind] == nil {
            if let row = try? ActionServiceAccount.unique(Array(Set(accounts)), service: kind.rawValue) {
                accountSelections[kind] = row
            }
        }
    }
    public mutating func selectAccount(_ row: ActionServiceAccount, accounts: [ActionServiceAccount]) throws {
        guard row.service == kind.rawValue, accounts.contains(row) else { throw TaskActionError.accountUnavailable }
        accountSelections[kind] = try row.validated()
    }
    public func account(accounts: [ActionServiceAccount]) throws -> ActionServiceAccount {
        guard let selected = selectedAccount else {
            if accounts.contains(where: { $0.service == kind.rawValue }) { throw TaskActionError.ambiguousAccount }
            throw TaskActionError.accountUnavailable
        }
        guard selected.service == kind.rawValue, let offered = accounts.first(where: { $0 == selected }) else { throw TaskActionError.selectedAccountUnavailable }
        return try offered.validated()
    }
    public func request(accounts: [ActionServiceAccount]) throws -> AppTaskActionRequest {
        if kind == .portal { return try AppTaskActionRequest(actions: [portal.action()]) }
        let row = try account(accounts: accounts)
        let action: AppTaskAction
        if kind == .calendar {
            guard let zone = TimeZone(identifier: timeZoneIdentifier), end > start else { throw TaskActionError.invalidDates }
            let formatter = ISO8601DateFormatter(); formatter.timeZone = zone
            // DatePicker displays minutes; bind those exact visible minutes.
            func absolute(_ date: Date) -> String {
                formatter.string(from: Date(timeIntervalSince1970: floor(date.timeIntervalSince1970 / 60) * 60))
            }
            action = try AppTaskAction(actionID: "calendar1", service: "calendar", operation: "create", account: row.account,
                target: ["calendar_id": row.resource], payload: ["summary": .string(title), "start": .string(absolute(start)),
                    "end": .string(absolute(end)), "all_day": .bool(false), "description": .string(details), "location": .string(location)])
        } else if kind == .ha {
            action = try home.action(account: row)
        } else if mailMode == .compose {
            action = try AppTaskAction(actionID: "draft1", service: "gmail", operation: "compose_draft", account: row.account,
                target: ["mailbox": row.resource, "to": recipient], payload: ["instruction": .string(instruction)])
        } else {
            action = try AppTaskAction(actionID: "draft1", service: "gmail", operation: "create_draft", account: row.account,
                target: ["mailbox": row.resource, "to": recipient, "reply_to_message": ""],
                payload: ["subject": .string(subject), "body": .string(body), "thread_id": .string(""), "in_reply_to": .string("")])
        }
        return try AppTaskActionRequest(actions: [action])
    }
    public var objective: String {
        if kind == .portal { return portal.objective }
        if kind == .calendar { return "Lege den Termin „\(title)“ mit den angegebenen Zeiten und Angaben im Kalender an." }
        if kind == .ha { return home.objective }
        if mailMode == .compose { return "Formuliere einen E-Mail-Entwurf an \(recipient) für das angegebene Anliegen. Lege ihn in Gmail ab." }
        return "Erstelle einen E-Mail-Entwurf an \(recipient) mit dem Betreff „\(subject)“ und dem angegebenen Text."
    }
}
