import Contacts
import SwiftUI

/// A view of the existing Core health and device contact permission. Opening
/// this screen never requests a scope, reads contacts or starts a provider job.
struct ConnectionsView: View {
    @ObservedObject var model: AppModel
    let onOpenApprovals: () -> Void
    @State private var components: [ComponentHealth] = []
    @State private var loading = false
    @State private var error = ""
    @State private var search = ""

    static func status(_ component: ComponentHealth?, unavailable: Bool) -> String {
        guard !unavailable else { return "Stand nicht bestätigt" }
        guard let component, component.geprueft_um != nil else { return "Noch nicht geprüft" }
        switch component.zustand {
        case "healthy": return "Zuletzt verfügbar"
        case "auth_required": return "Anmeldung erforderlich"
        case "unknown": return "Noch nicht geprüft"
        default: return "Verbindung prüfen"
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                Text("Deine Dienste für SOLVIO")
                    .font(.display(.title2)).foregroundStyle(Theme.ink)
                Text("Verbinde deine Dienste und bestimme, worauf SOLVIO zugreifen darf.")
                    .font(.subheadline).foregroundStyle(Theme.ink2)
                if loading { ProgressView("Verbindungen werden geprüft …") }
                if !error.isEmpty { Text(error).font(.footnote).foregroundStyle(Theme.warn) }
                VStack(spacing: 12) {
                    if matches("Gmail Google E-Mail") { service("Gmail", icon: "envelope", id: "gmail",
                            description: "Mails suchen und lesen, Antworten und Weiterleitungen vorbereiten. Versand bleibt an deine Face-ID-Freigabe gebunden.") }
                    if matches("Google Kalender Termine Calendar") { service("Google Kalender", icon: "calendar", id: "calendar",
                            description: "Termine lesen und Kalenderaufträge im Gespräch bearbeiten. Veränderungen folgen den bestehenden Freigaben.") }
                    if matches("Kontakte iCloud Google Adressbuch") { NavigationLink {
                        ContactsView(onOpenApprovals: onOpenApprovals)
                    } label: {
                        row("Kontakte", icon: "person.crop.rectangle.stack",
                            subtitle: "iPhone · iCloud · eingebundene Konten", status: contactStatus)
                    }.buttonStyle(.plain).accessibilityIdentifier("connections.contacts") }
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("Weitere Konten").font(.headline)
                    Text("Weitere Mailanbieter sind noch nicht eingerichtet. Vorhandene Konten in Apple Mail werden nicht automatisch für SOLVIO freigegeben.")
                        .font(.subheadline).foregroundStyle(Theme.ink2)
                }.padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
                NavigationLink(value: HomeRoute.system) {
                    Label("Verbindung zum SOLVIO Mac", systemImage: "desktopcomputer")
                        .frame(minHeight: 44)
                }
            }.padding(Theme.Space.margin).frame(maxWidth: 600).frame(maxWidth: .infinity)
        }
        .background(Theme.bg).navigationTitle("Verbindungen")
        .searchable(text: $search, prompt: "Dienst suchen")
        .task { await refresh() }.refreshable { await refresh() }
    }

    private func matches(_ value: String) -> Bool {
        search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || value.localizedCaseInsensitiveContains(search)
    }

    private var contactStatus: String {
        if AddressBookReader.isAllowed() { return "iPhone-Zugriff erlaubt · Abgleich öffnen" }
        if AddressBookReader.isRevoked() { return "iPhone-Zugriff fehlt" }
        return "Noch nicht freigegeben"
    }

    private func service(_ title: String, icon: String, id: String, description: String) -> some View {
        let component = components.first { $0.komponente == id }
        let state = Self.status(component, unavailable: !error.isEmpty || !model.reachable)
        return NavigationLink {
            Form {
                Section(title) {
                    Text(state).accessibilityIdentifier("connections.service-status")
                    if let checked = component?.geprueft_um {
                        Text("Zuletzt geprüft: " + Date(timeIntervalSince1970: checked).formatted())
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    Text(description)
                }
                Section("Anmeldung verwalten") {
                    Text("Gmail und Kalender teilen sich deinen Google-Zugang. Du meldest dich direkt bei Google an und bestätigst die Verbindung einmal mit Face ID.")
                    NavigationLink {
                        GoogleConnectionView(app: model, mutation: model.googleConnection,
                                             onOpenApprovals: onOpenApprovals)
                    } label: { Label("Google verbinden", systemImage: "link") }
                    .accessibilityIdentifier("connections.google-connect")
                    NavigationLink {
                        TresorView(onOpenApprovals: onOpenApprovals)
                    } label: { Label("Zugriff verwalten", systemImage: "lock.shield") }
                    Text("Unter dem Google-Zugang kannst du die bestehenden Rechte prüfen und ihn mit Face ID deaktivieren. Das betrifft Gmail und Kalender gemeinsam; dein Google-Konto wird dabei nicht gelöscht.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("Eine neue Anmeldung erfolgt direkt bei Google. Teile keine Passwörter im Chat.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let component {
                    Section {
                        NavigationLink("Verbindung untersuchen") { DoctorView(model: model, component: component) }
                    }
                }
            }.navigationTitle(title).navigationBarTitleDisplayMode(.inline)
        } label: {
            row(title, icon: icon, subtitle: "Vorhandener Google-Zugang", status: state)
        }.buttonStyle(.plain).accessibilityIdentifier("connections." + id)
    }

    private func row(_ title: String, icon: String, subtitle: String, status: String) -> some View {
        HStack(spacing: 14) {
            Image(systemName: icon).font(.title2).foregroundStyle(Theme.blue)
                .frame(width: 44, height: 44).background(Theme.tintBlue, in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline).foregroundStyle(Theme.ink)
                Text(subtitle).font(.caption).foregroundStyle(Theme.ink2)
                Text(status).font(.caption.weight(.medium)).foregroundStyle(Theme.ink2)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.ink3)
        }.padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
    }

    private func refresh() async {
        guard !loading else { return }
        guard let client = model.client else { error = "Keine bestätigte Verbindung zum SOLVIO Mac."; return }
        loading = true; defer { loading = false }
        do {
            components = try await client.systemHealth().komponenten
            error = ""
        } catch {
            self.error = "Der aktuelle Stand konnte nicht gelesen werden. Bitte versuche es später erneut."
        }
    }
}
