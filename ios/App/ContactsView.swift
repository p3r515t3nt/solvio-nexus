import Contacts
import Foundation
import SolvioApprovalsKit
import SwiftUI

struct AddressBookContact: Codable, Sendable {
    let name: String
    let emails: [String]
}

struct AddressBookSnapshot: Sendable {
    let contacts: [AddressBookContact]
    let accounts: [String]
}

enum AddressBookReader {
    static func project(_ contact: CNContact) -> AddressBookContact? {
        let emails = Array(Set(contact.emailAddresses.map { String($0.value) }
            .filter { !$0.isEmpty })).sorted()
        let name = CNContactFormatter.string(from: contact, style: .fullName) ?? ""
        return name.isEmpty || emails.isEmpty ? nil : AddressBookContact(name: name, emails: emails)
    }
    static func isRevoked() -> Bool {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        return status == .denied || status == .restricted
    }
    static func isAllowed() -> Bool {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        if #available(iOS 18.0, *), status == .limited { return true }
        return status == .authorized
    }
    static func read(requestPermission: Bool = true) async throws -> AddressBookSnapshot {
        let store = CNContactStore()
        if requestPermission && CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            _ = try await store.requestAccess(for: .contacts)
        }
        let status = CNContactStore.authorizationStatus(for: .contacts)
        var allowed = status == .authorized
        if #available(iOS 18.0, *) { allowed = allowed || status == .limited }
        guard allowed else { throw ContactReadError.permission }
        return try await Task.detached(priority: .userInitiated) {
            let reader = CNContactStore()
            let request = CNContactFetchRequest(keysToFetch: [
                CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
                CNContactEmailAddressesKey as CNKeyDescriptor
            ])
            request.unifyResults = true
            var contacts: [AddressBookContact] = []
            try reader.enumerateContacts(with: request) { contact, _ in
                if let projected = project(contact) { contacts.append(projected) }
            }
            guard contacts.count <= 3000 else { throw ContactReadError.tooMany }
            let accounts = try reader.containers(matching: nil).map(\.name).sorted()
            return AddressBookSnapshot(contacts: contacts, accounts: accounts)
        }.value
    }
}

enum ContactReadError: LocalizedError {
    case permission, tooMany
    var errorDescription: String? {
        switch self {
        case .permission: return "Der Kontaktzugriff fehlt. Du kannst ihn in den iPhone-Einstellungen für SOLVIO erlauben."
        case .tooMany: return "Die Kontaktliste ist zu groß. Bitte gib SOLVIO eine kleinere Auswahl frei."
        }
    }
}

struct ContactHandle: Codable, Hashable { let channel, value: String }
struct ContactMatch: Codable, Identifiable {
    let display_name: String
    let handles: [ContactHandle]
    var alias: String? = nil
    var confirmed: Bool? = nil
    var id: String { (alias ?? "source") + "|" + display_name + handles.map(\.value).joined(separator: "|") }
}
struct ContactLookup: Codable { let bindings, candidates: [ContactMatch] }
struct ContactMutationResult: Codable {
    let ok: Bool
    var outcome, reason, human_message, request_id: String?
    var imported: Int?
}


struct ContactsView: View {
    let onOpenApprovals: () -> Void
    @StateObject private var knowledge = WissenModel()
    @State private var working = false
    @State private var notice = ""
    @ObservedObject private var refresh = ContactRefreshModel.shared
    @State private var query = ""
    @State private var matches: [ContactMatch] = []
    @State private var editing = false
    @State private var alias = ""
    @State private var name = ""
    @State private var email = ""

    var body: some View {
        Form {
            Section("Adressbücher") {
                Text("SOLVIO gleicht Namen und Mailadressen aus den freigegebenen iPhone-Kontakten ab. Dazu gehören auch iCloud- und Google-Kontakte, die in deiner Kontakte-App eingebunden sind.")
                Button("Kontakte abgleichen") { Task { await sync() } }
                    .disabled(working || refresh.working).accessibilityIdentifier("contacts.sync")
                if !refresh.accounts.isEmpty {
                    Text("Konten auf diesem iPhone: " + refresh.accounts.joined(separator: ", "))
                        .font(.footnote)
                }
                Toggle("Beim Öffnen aktuell halten", isOn: $refresh.enabled)
                    .disabled(working || refresh.working)
                if let date = refresh.lastSuccess {
                    Text("Zuletzt abgeglichen: " + date.formatted(date: .abbreviated, time: .shortened))
                        .font(.footnote)
                }
                if !refresh.notice.isEmpty { Text(refresh.notice).font(.footnote) }
                Text("Der Abgleich liest deine Kontakte und ändert sie nicht. Fotos, Notizen und Geburtstage werden nicht übertragen. Bei aktivem Abgleich aktualisiert SOLVIO sie beim Öffnen der App, spätestens nach einer Stunde. Bei geschlossener App läuft kein Abgleich. Suchdaten bleiben 24 Stunden verwendbar; bestätigte Kontakte bleiben gespeichert.")
                    .font(.footnote).foregroundStyle(.secondary)
                Button("Übertragene Suchdaten entfernen", role: .destructive) {
                    Task { await removeSource() }
                }.disabled(working || refresh.working)
            }
            Section("Kontakt finden") {
                TextField("Name oder gespeicherte Bezeichnung", text: $query)
                    .submitLabel(.search).onSubmit { Task { await search() } }
                Button("Suchen") { Task { await search() } }.disabled(working || query.count < 2)
                ForEach(matches) { contact in
                    ForEach(contact.handles.filter { $0.channel == "gmail" }, id: \.self) { handle in
                        Button {
                            name = contact.display_name; alias = contact.alias ?? contact.display_name
                            email = handle.value; editing = true
                        } label: {
                            VStack(alignment: .leading) {
                                Text(contact.display_name)
                                Text(handle.value).font(.footnote)
                            }
                        }
                    }
                }
                Button("Kontakt oder eigene Adresse eintragen") {
                    name = ""; alias = ""; email = ""; editing = true
                }
            }
            if !notice.isEmpty { Section { Text(notice).accessibilityIdentifier("contacts.notice") } }
            Section { Button("Freigaben öffnen", action: onOpenApprovals) }
        }
        .navigationTitle("Kontakte")
        .sheet(isPresented: $editing) {
            NavigationStack {
                Form {
                    TextField("Name", text: $name)
                    TextField("Merken als, z. B. mich", text: $alias)
                    TextField("Mailadresse", text: $email)
                        .keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Text("Prüfe die genaue Adresse. Die Zuordnung wird erst nach deiner Face-ID-Freigabe gespeichert. Ein späterer Mailversand braucht weiterhin eine eigene Freigabe.")
                        .font(.footnote)
                    Button("Zur Face-ID-Freigabe vorlegen") { Task { await confirm() } }
                        .disabled(working || name.isEmpty || alias.isEmpty || email.isEmpty)
                }
                .navigationTitle("Kontakt merken")
                .toolbar { Button("Schließen") { editing = false } }
            }
        }
    }

    private func sync() async {
        guard !working else { return }
        guard let client = knowledge.client else { notice = "Die Verbindung zum Core fehlt. Bitte prüfe die Kopplung unter Mehr."; return }
        working = true; defer { working = false }
        await refresh.sync(client: client, manual: true)
    }

    private func removeSource() async {
        guard !working else { return }
        guard let client = knowledge.client else { notice = "Die Verbindung zum Core fehlt. Bitte prüfe die Kopplung unter Mehr."; return }
        working = true; defer { working = false }
        await refresh.sync(client: client, remove: true)
        matches = []
    }

    private func search() async {
        guard !working else { return }
        guard let client = knowledge.client else { notice = "Die Verbindung zum Core fehlt. Bitte prüfe die Kopplung unter Mehr."; return }
        working = true; defer { working = false }
        do {
            let result = try await client.contactLookup(query)
            matches = result.bindings + result.candidates
            notice = matches.isEmpty ? "Kein passender Kontakt gefunden. Du kannst eine Adresse eintragen oder die Kontakte erneut abgleichen." : "Wähle die passende Person und Adresse."
        } catch { matches = []; notice = error.localizedDescription }
    }

    private func confirm() async {
        guard !working else { return }
        guard let client = knowledge.client else { notice = "Die Verbindung zum Core fehlt. Bitte prüfe die Kopplung unter Mehr."; return }
        working = true; defer { working = false }
        do {
            let result = try await client.contactMutate("contacts_confirm",
                arguments: ["alias": alias, "name": name, "email": email])
            notice = result.request_id != nil ? "Die Kontaktbestätigung liegt unter Freigaben. Gespeichert wird sie erst nach Face ID." :
                result.human_message ?? (result.ok ? "Kontakt gespeichert." : "Die Kontaktbestätigung wurde nicht gestartet.")
            editing = false
        } catch { notice = error.localizedDescription; editing = false }
    }
}
