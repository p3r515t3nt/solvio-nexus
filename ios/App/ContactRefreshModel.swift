import Contacts
import Foundation
import SwiftUI

/// Refreshes only while the app is in use. The Core remains the contact store.
@MainActor
final class ContactRefreshModel: ObservableObject {
    static let shared = ContactRefreshModel()
    @Published var enabled: Bool {
        didSet { defaults.set(enabled, forKey: "contacts.refresh.enabled") }
    }
    @Published private(set) var working = false
    @Published private(set) var notice = ""
    @Published private(set) var accounts: [String] = []
    @Published private(set) var lastSuccess: Date?
    private let defaults: UserDefaults
    private var removalPending: Bool {
        get { defaults.bool(forKey: "contacts.refresh.removalPending") }
        set { defaults.set(newValue, forKey: "contacts.refresh.removalPending") }
    }
    private var revocationCleared: Bool {
        get { defaults.bool(forKey: "contacts.refresh.revocationCleared") }
        set { defaults.set(newValue, forKey: "contacts.refresh.revocationCleared") }
    }
    private var lastAttempt: Date?
    private var dirty = false
    private var generation = 0
    private var changes = 0
    private let now: () -> Date

    init(defaults: UserDefaults = .standard, now: @escaping () -> Date = Date.init) {
        self.defaults = defaults; self.now = now
        enabled = defaults.bool(forKey: "contacts.refresh.enabled")
        lastSuccess = defaults.object(forKey: "contacts.refresh.lastSuccess") as? Date
    }

    func changed() { dirty = true; changes += 1 }

    func reset() {
        generation += 1
        enabled = false; removalPending = false; revocationCleared = false; lastSuccess = nil
        defaults.removeObject(forKey: "contacts.refresh.lastSuccess")
        accounts = []; notice = ""; lastAttempt = nil; dirty = false
    }

    func sync(client: ApprovalClient, manual: Bool = false, remove: Bool = false) async {
        await refresh(manual: manual, remove: remove,
                      allowed: AddressBookReader.isAllowed, revoked: AddressBookReader.isRevoked,
                      read: { try await AddressBookReader.read(requestPermission: manual) },
                      upload: { raw in
            try await client.contactMutate("contacts_import", arguments: ["contacts_json": raw])
        })
    }

    // Injectable IO keeps tests away from personal contacts and the paired Core.
    func refresh(manual: Bool = false, remove: Bool = false,
                 allowed: () -> Bool, revoked: () -> Bool = { false },
                 read: () async throws -> AddressBookSnapshot,
                 upload: (String) async throws -> ContactMutationResult) async {
        guard !working else { return }
        if remove { enabled = false; removalPending = true }
        let newRevocation = revoked() && !revocationCleared && !removalPending
        if newRevocation { removalPending = true }
        guard manual || enabled || removalPending else { return }
        let stamp = now()
        if !manual && !remove && !newRevocation {
            if let lastAttempt, stamp.timeIntervalSince(lastAttempt) >= 0,
               stamp.timeIntervalSince(lastAttempt) < 60 { return }
            if allowed(), !removalPending, !dirty, let lastSuccess,
               stamp.timeIntervalSince(lastSuccess) >= 0,
               stamp.timeIntervalSince(lastSuccess) < 3600 { return }
        }
        let currentGeneration = generation
        let currentChanges = changes
        working = true; lastAttempt = stamp
        defer { working = false }
        do {
            if removalPending || (!manual && !allowed()) {
                try await clearSource(upload: upload, generation: currentGeneration)
                return
            }
            let snapshot = try await read()
            guard currentGeneration == generation else { return }
            // Permission may have been revoked while the reader was awaiting IO.
            guard allowed() else { throw ContactReadError.permission }
            let data = try JSONEncoder().encode(snapshot.contacts)
            guard data.count <= 750_000, let raw = String(data: data, encoding: .utf8) else {
                throw ContactReadError.tooMany
            }
            let result = try await upload(raw)
            guard currentGeneration == generation else { return }
            guard result.ok else { notice = "Der Kontaktabgleich wurde nicht bestätigt. SOLVIO versucht es beim nächsten Öffnen erneut."; return }
            lastSuccess = now(); defaults.set(lastSuccess, forKey: "contacts.refresh.lastSuccess")
            accounts = snapshot.accounts; dirty = changes != currentChanges; revocationCleared = false
            if manual { enabled = true }
            notice = "\(result.imported ?? snapshot.contacts.count) Kontakte mit Mailadressen abgeglichen."
        } catch ContactReadError.permission {
            guard currentGeneration == generation else { return }
            do { try await clearSource(upload: upload, generation: currentGeneration) }
            catch { notice = "Kein Kontaktzugriff. Frühere Suchdaten konnten noch nicht entfernt werden; SOLVIO versucht es erneut." }
        } catch {
            guard currentGeneration == generation else { return }
            // No provider errors/contact values in the user-facing diagnostic.
            notice = "Der Kontaktabgleich ist noch nicht bestätigt. Bitte prüfe die Verbindung; SOLVIO versucht es beim nächsten Öffnen erneut."
        }
    }
    private func clearSource(upload: (String) async throws -> ContactMutationResult, generation expected: Int) async throws {
        removalPending = true
        let result = try await upload("[]")
        guard expected == generation else { return }
        guard result.ok else { notice = "Frühere Suchdaten konnten noch nicht entfernt werden. SOLVIO versucht es erneut."; return }
        removalPending = false; revocationCleared = true; lastSuccess = nil; accounts = []
        defaults.removeObject(forKey: "contacts.refresh.lastSuccess")
        notice = "Die Suchdaten dieses iPhones wurden entfernt. Bestätigte Kontakte bleiben erhalten."
    }

}
