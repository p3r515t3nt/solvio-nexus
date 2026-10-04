import Foundation
import SwiftUI
import UIKit
import UserNotifications

/// Keep a tap until the paired, active navigation stack can actually handle it.
/// No message content or authority is carried across this handoff.
@MainActor
final class PushNavigation: ObservableObject {
    static let shared = PushNavigation()
    @Published private(set) var pendingInbox = false

    func receive(route: String?) {
        guard route == "inbox" else { return }
        pendingInbox = true
    }

    func consume(isActive: Bool) -> Bool {
        guard isActive, pendingInbox else { return false }
        pendingInbox = false
        return true
    }
}

struct PushSetupResponse: Codable {
    var ok: Bool?
    var configured: Bool?
    var nonce: String?
    var core_instance_id: String?
}

@MainActor
protocol PushClient {
    func pushChallenge() async throws -> PushSetupResponse
    func pushRegister(token: String, environment: String) async throws -> PushSetupResponse
}
extension ApprovalClient: PushClient {}

@MainActor
final class PushModel: ObservableObject {
    static let shared = PushModel()
    @Published private(set) var enabled: Bool
    @Published private(set) var status = "Mitteilungen sind noch nicht eingeschaltet."
    @Published private(set) var working = false
    private let defaults: UserDefaults
    private let now: () -> Date
    private let registerOS: () -> Void
    private let unregisterOS: () -> Void
    private var client: (any PushClient)?
    private var token: String?
    private var registered: String?
    private var lastAttempt: Date?
    private var lastOSAttempt: Date?
    private var generation = 0
    private var pendingRemoval: Bool {
        get { defaults.bool(forKey: "push.pendingRemoval") }
        set { defaults.set(newValue, forKey: "push.pendingRemoval") }
    }
    private var registrationPossible: Bool {
        get { defaults.bool(forKey: "push.registrationPossible") }
        set { defaults.set(newValue, forKey: "push.registrationPossible") }
    }
    init(defaults: UserDefaults = .standard, now: @escaping () -> Date = Date.init,
         registerOS: @escaping () -> Void = { UIApplication.shared.registerForRemoteNotifications() },
         unregisterOS: @escaping () -> Void = { UIApplication.shared.unregisterForRemoteNotifications() }) {
        self.defaults = defaults; self.now = now
        self.registerOS = registerOS; self.unregisterOS = unregisterOS
        enabled = defaults.bool(forKey: "push.enabled")
    }
    func enable(client: any PushClient, permission: () async throws -> Bool = {
        try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
    }) async {
        guard !working, !pendingRemoval else { return }
        working = true; defer { working = false }
        self.client = client; let current = generation
        do {
            let setup = try await client.pushChallenge()
            guard current == generation else { return }
            guard setup.configured == true else {
                status = "Der Apple-Push-Zugang muss noch am Mac eingerichtet werden. Hinweise bleiben hier in der App verfügbar."
                return
            }
            let allowed = try await permission()
            guard current == generation else { return }
            guard allowed else { status = "Mitteilungen sind in den iPhone-Einstellungen nicht erlaubt."; return }
            enabled = true; defaults.set(true, forKey: "push.enabled")
            requestOSToken()
            status = "Apple-Mitteilungen werden verbunden …"
        } catch {
            if current == generation { status = "Die Einrichtung ist noch nicht bestätigt. Bitte prüfe die Verbindung." }
        }
    }
    private func requestOSToken() {
        if let lastOSAttempt, now().timeIntervalSince(lastOSAttempt) < 60 { return }
        lastOSAttempt = now(); registerOS()
    }
    func received(_ data: Data) {
        guard enabled else { return }
        token = data.map { String(format: "%02x", $0) }.joined()
        Task { await registerToken() }
    }
    func registrationFailed() {
        guard enabled else { return }
        status = "Apple-Mitteilungen konnten nicht verbunden werden. Die App-Freischaltung und Verbindung müssen geprüft werden."
    }
    func refresh(client: any PushClient, denied: () async -> Bool = {
        await UNUserNotificationCenter.current().notificationSettings().authorizationStatus == .denied
    }) async {
        self.client = client
        if pendingRemoval {
            if let lastAttempt, now().timeIntervalSince(lastAttempt) < 60 { return }
            _ = await clearRegistration(client: client); return
        }
        guard enabled else { return }
        let current = generation
        let isDenied = await denied()
        guard current == generation else { return }
        if isDenied { _ = await disable(client: client); return }
        if token == nil { requestOSToken() }
        await registerToken()
    }
    private func registerToken() async {
        guard enabled, !working, !pendingRemoval, let client, let token, registered != token else { return }
        if let lastAttempt, now().timeIntervalSince(lastAttempt) < 60 { return }
        working = true; lastAttempt = now(); let current = generation
        // Persist before IO: an interrupted/unknown request may have registered.
        registrationPossible = true
        defer { working = false }
        do {
            let environment = Bundle.main.object(forInfoDictionaryKey: "SolvioPushEnvironment") as? String ?? "development"
            let response = try await client.pushRegister(token: token, environment: environment)
            guard current == generation, enabled else { return }
            guard response.ok == true else { registrationFailed(); return }
            registered = token
            status = response.configured == true ? "Apple-Mitteilungen sind angemeldet. Eine tatsächliche Zustellung ist noch nicht geprüft." :
                "Das iPhone ist angemeldet; der Apple-Push-Zugang am Mac fehlt noch."
        } catch {
            if current == generation { status = "Mitteilungen sind noch nicht verbunden. SOLVIO versucht es erneut, solange die App geöffnet ist." }
        }
    }
    @discardableResult
    func disable(client: (any PushClient)?) async -> Bool {
        generation += 1; enabled = false; defaults.set(false, forKey: "push.enabled")
        unregisterOS(); token = nil; registered = nil; lastOSAttempt = nil
        pendingRemoval = pendingRemoval || registrationPossible
        guard pendingRemoval else { status = "Mitteilungen sind ausgeschaltet."; return true }
        status = "Auf dem iPhone ausgeschaltet; die Abmeldung am Mac wird noch bestätigt."
        guard let client else { return false }
        self.client = client
        return await clearRegistration(client: client)
    }
    private func clearRegistration(client: any PushClient) async -> Bool {
        // Never race deletion against an in-flight register. Foreground polling
        // retries the persisted deletion, even though local notifications are off.
        guard !working else { return false }
        working = true; lastAttempt = now(); defer { working = false }
        do {
            let response = try await client.pushRegister(token: "", environment: "development")
            guard response.ok == true else { return false }
            pendingRemoval = false; registrationPossible = false
            status = "Mitteilungen sind ausgeschaltet."; return true
        } catch {
            status = "Auf dem iPhone ausgeschaltet; SOLVIO holt die Abmeldung am Mac bei Verbindung nach."
            return false
        }
    }
}

final class PushDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--push-navigation-preview") {
            PushNavigation.shared.receive(route: "inbox")
        }
        #endif
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushModel.shared.received(deviceToken)
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushModel.shared.registrationFailed()
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let route = response.notification.request.content.userInfo["solvio_route"] as? String
        await MainActor.run { PushNavigation.shared.receive(route: route) }
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        []
    }
}

struct PushSettingsView: View {
    let client: ApprovalClient?
    @ObservedObject private var model = PushModel.shared
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.status).font(.footnote)
            Button(model.enabled ? "Mitteilungen ausschalten" : "Mitteilungen einschalten") {
                Task {
                    if model.enabled { await model.disable(client: client) }
                    else if let client { await model.enable(client: client) }
                }
            }.disabled(model.working || client == nil)
            Text("Auf dem Sperrbildschirm erscheinen nur allgemeine Hinweise. Inhalte und Freigaben öffnest du in SOLVIO.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding().card()
    }
}
