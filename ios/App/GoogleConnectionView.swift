import SwiftUI
import UIKit

struct GoogleConnectionSetup: Codable, Sendable {
    let client_id: String
    let server_client_id: String
    let scopes: [String]
    let expected_binding: String
    let currently_active: Bool

    @MainActor var valid: Bool {
        client_id == GoogleSignInClient.clientID && server_client_id == GoogleSignInClient.serverClientID
        && Set(scopes) == Set(GoogleSignInClient.scopes)
        && expected_binding.count == 64 && expected_binding.allSatisfy { "0123456789abcdef".contains($0) }
    }
}

/// Uses the existing Vault mutation and Face-ID flow, with no second authority store.
struct GoogleConnectionView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var mutation: TresorModel
    let onOpenApprovals: () -> Void
    @State private var setup: GoogleConnectionSetup?
    @State private var working = false
    @State private var notice = ""
    @State private var signIn = GoogleSignInClient()
    @State private var presenter: UIViewController?

    var body: some View {
        Form {
            Section("Gmail und Google Kalender") {
                Text("Melde dich bei Google an. Danach bestätigst du mit Face ID, dass SOLVIO dieses Konto mit deinen bestehenden Rechten verwenden darf.")
                Text("Ein bisheriger Google-Zugang wird erst nach erfolgreicher Freigabe ersetzt. Mailversand braucht weiterhin deine Freigabe für den jeweiligen Auftrag.")
                    .font(.footnote).foregroundStyle(.secondary)
                if working { ProgressView("Google-Verbindung wird vorbereitet …") }
                Button("Bei Google anmelden") { Task { await connect() } }
                    .disabled(working || mutation.working || mutation.pending != nil || setup?.valid != true)
                    .accessibilityIdentifier("google.signin")
                if setup == nil && !working {
                    Button("Einrichtung erneut prüfen") { Task { await load() } }
                }
            }
            if !notice.isEmpty { Section { Text(notice) } }
            if !mutation.notice.isEmpty {
                Section("Verbindungsauftrag") {
                    Text(mutation.notice).accessibilityIdentifier("google.result")
                    if mutation.pending != nil { Button("Mit Face ID freigeben", action: onOpenApprovals) }
                    if mutation.ergebnis?.gelungen == true {
                        Text("Die Kontoanbindung ist bestätigt. Eine echte Mail- oder Kalenderabfrage wurde dabei nicht ausgeführt.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .background(GooglePresenter { presenter = $0 }.frame(width: 0, height: 0))
        .navigationTitle("Google verbinden").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        guard !working, let client = app.client else { return }
        working = true; defer { working = false }
        do {
            let value = try await client.googleConnectionSetup()
            guard value.valid else { throw GoogleSignInFailure.unavailable }
            setup = value; notice = ""
        } catch {
            setup = nil
            notice = "Die Anmeldung auf dem iPhone ist gerade nicht verfügbar. Dein bisheriger Zugang bleibt erhalten. Bitte prüfe die Verbindung zum SOLVIO Mac."
        }
    }

    private func connect() async {
        guard !working, mutation.pending == nil, let client = app.client, let presenter else { return }
        working = true; defer { working = false }
        do {
            // Refresh the exact binding immediately before asking for consent.
            let current = try await client.googleConnectionSetup()
            guard current.valid else { throw GoogleSignInFailure.unavailable }
            setup = current
            guard app.client === client, app.googleConnection === mutation else { throw GoogleSignInFailure.expired }
            let authorization = try await signIn.authorize(presenting: presenter)
            defer { authorization.discard() }
            try Task.checkCancellation()
            guard app.client === client, app.googleConnection === mutation else { throw GoogleSignInFailure.expired }
            let result = await mutation.startGoogleConnect(authorization, binding: current.expected_binding)
            notice = ""
            if result == .wartetAufFreigabe { onOpenApprovals() }
        } catch let error as GoogleSignInFailure {
            notice = error.localizedDescription
        } catch {
            notice = "Die Google-Anmeldung wurde nicht abgeschlossen. Bitte prüfe den Verbindungsstand, bevor du es erneut versuchst."
        }
    }
}

private struct GooglePresenter: UIViewControllerRepresentable {
    let ready: (UIViewController) -> Void
    func makeUIViewController(context: Context) -> UIViewController {
        let controller = UIViewController()
        DispatchQueue.main.async { ready(controller) }
        return controller
    }
    func updateUIViewController(_ uiViewController: UIViewController, context: Context) {}
}
