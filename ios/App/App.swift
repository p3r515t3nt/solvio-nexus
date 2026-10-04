// SOLVIO Approvals — app entry, model, and views.
//
// Two keys: the Face-ID Secure-Enclave APPROVAL key signs the decision (human authority);
// the Apple App Attest key proves a legitimate app instance and adds a second proof for every
// APPROVE. A device is a trusted approver only with BOTH Secure Enclave AND App Attest; on the
// Simulator (neither) approval is disabled — a Simulator is never proof.
import Contacts
import GoogleSignIn
import CryptoKit
import SwiftUI
import SolvioApprovalsKit

@main
struct SolvioApprovalsApp: App {
    @UIApplicationDelegateAdaptor(PushDelegate.self) private var pushDelegate
    var body: some Scene { WindowGroup { RootView().onOpenURL { url in
        if url.scheme == GoogleSignInClient.callbackScheme {
            _ = GIDSignIn.sharedInstance.handle(url)
        }
    } } }
}

private let kEnrollTag = "de.solvio.approvals.enrollment"

struct Enrollment: Codable {
    let pairing: PairingPayload
    let deviceID: String
    let transportCred: String
    let appAttestKeyId: String
}

@MainActor
final class AppModel: ObservableObject {
    @Published var paired = false
    @Published var attested = true
    @Published var pending: [PendingApproval] = []
    @Published var status = ""
    @Published var busy = false

    // MARK: - Kontrollzentrum
    //
    // Eigene Felder, damit die Freigabeliste unberuehrt bleibt: der Freigabeweg
    // ist der Teil, an dem eine Entscheidung haengt, und der soll nicht
    // mitleiden, wenn eine Uebersicht nicht laedt.
    @Published var overview: Overview?
    @Published var tasks: [TaskRow] = []
    @Published var inbox: [InboxItem] = []
    @Published var activity: [ActivityEvent] = []
    @Published var system: [ComponentHealth] = []
    /// Ob der Mac beim letzten Versuch geantwortet hat.
    @Published var reachable = true
    /// Wann der angezeigte Stand entstanden ist. Ohne das wuerde ein alter
    /// Stand wie der aktuelle aussehen — die gefaehrlichste Art von Anzeige.
    @Published var snapshotAt: Double?
    @Published var controlError = ""

    /// Fuer die Ansichten. Der Client bleibt sonst intern.
    private(set) var client: ApprovalClient? {
        didSet {
            googleConnection.invalidate()
            googleConnection = TresorModel(client: client, restoreEnrollment: false)
        }
    }
    /// One in-memory draft survives navigation and network retries; the Core owns tasks.
    let taskStart = TaskStartModel()
    @Published private(set) var googleConnection = TresorModel(restoreEnrollment: false)
    var taskStartKeyID: String? { enrollment?.appAttestKeyId }

    /// Was der Sprachweg braucht — und mehr nicht. Es ist dieselbe
    /// Registrierung wie fuer Freigaben: derselbe gepinnte Endpunkt, dieselbe
    /// Transportkennung. Es entsteht kein zweites Geheimnis auf dem Telefon.
    ///
    /// Die Transportkennung darf lesen und sprechen. Freigeben kann sie nicht —
    /// dafuer signiert der Secure Enclave nach Face ID, und dieser Weg fasst
    /// das nicht an.
    var pairing: PairingPayload? { enrollment?.pairing }
    var enrollmentForVoice: (deviceID: String, transportCred: String)? {
        guard let e = enrollment else { return nil }
        return (e.deviceID, e.transportCred)
    }
    private var signer: DeviceSigner?
    private var enrollment: Enrollment?

    /// Die laufende Nachfrageschleife. Nil heisst: die App fragt gerade nicht —
    /// weil sie nicht im Vordergrund ist. iOS suspendiert eine Hintergrund-App
    /// ohnehin; so zu tun, als liefe dort ein Zeitgeber weiter, waere eine Luege
    /// an den Nutzer und eine Verschwendung an die Batterie.
    private var pollTask: Task<Void, Never>?

    /// Verhindert ueberlappende Abrufe. Ohne das koennte ein haengender Abruf im
    /// Drei-Sekunden-Takt Nachfolger stapeln, bis nichts mehr geht.
    private var refreshing = false

    /// Derselbe Schutz fuer das Kontrollzentrum, eigener Riegel: sonst wuerde
    /// ein haengender Uebersichtsabruf die Freigabeliste mit blockieren.
    private var controlRefreshing = false

    private var policy = RefreshPolicy()

    init() { restore() }

    #if DEBUG
    /// Beispieldaten fuer die visuelle Schleife — niemals echte Wahrheit.
    static func visualPreview() -> AppModel {
        let m = AppModel()
        m.paired = true
        m.reachable = true
        m.pending = []
        // Unpaired, synthetic navigation fixture; never a verified challenge.
        if ProcessInfo.processInfo.arguments.contains("--approval-navigation-preview") {
            precondition(m.client == nil, "Navigation preview requires an unpaired simulator")
            m.attested = false
            m.pending = [PendingApproval(
                approval_id: "ap-navigation-fixture", tool: "gmail_send_draft",
                mode: "NON_IDEMPOTENT_WRITE", task: "Synthetic navigation only",
                workspace: "Navigation fixture", human_summary: "Synthetische Mailfreigabe",
                action_digest: String(repeating: "0", count: 64),
                expires_at: Date().timeIntervalSince1970 + 600)]
            m.inbox = [InboxItem(id: "notice-navigation-fixture",
                zusammenfassung: "Synthetischer Navigationshinweis", dringlichkeit: "normal",
                zeit: nil, aufgabe: "", gelesen: true, quelle: "Navigation fixture")]
        }
        m.tasks = [TaskRow(id: "t1", titel: "Wohnzimmerlicht abends einschalten",
                           was: "Licht", wann: "täglich 20:00", zustand: "active",
                           aktiv: true, naechster_lauf: nil, letzter_lauf: nil,
                           fehlschlaege: 0, letzter_fehler: "",
                           wartet_auf_freigabe: false, auftrag: "")]
        return m
    }
    #endif

    private func makeSigner(create: Bool) throws -> DeviceSigner {
        if SecureEnclave.isAvailable { return try SecureEnclaveSigner(create: create) }
        return DevSoftwareSigner()   // Simulator/dev only — never the real gate
    }

    /// A real trusted approver needs BOTH a Secure-Enclave approval key and App Attest.
    private func computeAttested(_ s: DeviceSigner) -> Bool {
        s.attested && AppAttestManager.isSupported
    }

    private func restore() {
        guard let data = Keychain.load(tag: kEnrollTag),
              let e = try? JSONDecoder().decode(Enrollment.self, from: data) else { return }
        do {
            let s = try makeSigner(create: false)
            signer = s; enrollment = e; attested = computeAttested(s)
            client = ApprovalClient(pairing: e.pairing, deviceID: e.deviceID, transportCred: e.transportCred)
            paired = true
        } catch {
            paired = false   // biometric set changed -> key invalid -> must re-pair
            status = "Gerätefreigabe muss neu gekoppelt werden."
        }
    }

    func pair(qr: String) async {
        busy = true; defer { busy = false }
        guard let data = qr.data(using: .utf8),
              let pairing = try? JSONDecoder().decode(PairingPayload.self, from: data),
              pairing.type == "pairing" else { status = "Ungültiger QR-Code."; return }
        guard AppAttestManager.isSupported else {
            status = "Dieses Gerät unterstützt App Attest nicht — es kann kein SOLVIO-Approver sein."
            return
        }
        do {
            let s = try makeSigner(create: true)                 // Secure-Enclave approval key
            let appAttestKeyId = try await AppAttestManager.ensureKeyId()
            let deviceID = "dev-" + UUID().uuidString.lowercased()
            let transportCred = UUID().uuidString + UUID().uuidString
            let begin = try await ApprovalClient.beginEnroll(
                pairing: pairing, deviceID: deviceID, approvalPublicKeyX963: s.publicKeyX963,
                appAttestKeyId: appAttestKeyId, transportCred: transportCred)
            guard let bindingBytes = Data(base64Encoded: begin.binding_b64) else {
                status = "Ungültiges Enrollment-Binding."; return
            }
            let cdh = AppAttestBinding.enrollmentClientDataHash(bindingBytes: bindingBytes)
            let attestation = try await AppAttestManager.attest(keyId: appAttestKeyId, clientDataHash: cdh)
            try await ApprovalClient.completeEnroll(
                pairing: pairing, enrollmentId: begin.enrollment_id,
                attestationB64: attestation.base64EncodedString())
            let e = Enrollment(pairing: pairing, deviceID: deviceID, transportCred: transportCred,
                               appAttestKeyId: appAttestKeyId)
            Keychain.save(try JSONEncoder().encode(e), tag: kEnrollTag)
            signer = s; enrollment = e; attested = computeAttested(s)
            client = ApprovalClient(pairing: pairing, deviceID: deviceID, transportCred: transportCred)
            paired = true; status = "Gekoppelt und attestiert."
            await refresh()
        } catch {
            status = "Kopplung fehlgeschlagen: \(error.localizedDescription)"
        }
    }

    func refresh() async {
        guard let client, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            pending = try await client.listPending()
            if policy.isBackingOff { status = "" }   // Erholung sichtbar machen
            policy.succeeded()
        } catch {
            policy.failed()
            status = error.localizedDescription
        }
    }

    /// Faengt im Vordergrund an, von selbst nachzufragen.
    ///
    /// Ausdruecklich nur Entdeckung: dass eine Freigabe erscheint, gibt ihr keine
    /// Befugnis. Freigegeben wird weiterhin nur mit Face ID ueber die signierte
    /// Anfrage — an `decide` aendert sich hier nichts.
    /// Holt die Uebersicht — und nur so viel, wie der Bildschirm braucht.
    ///
    /// Bewusst getrennt von `refresh()`: die Freigabeliste ist der wichtigere
    /// Weg und darf nicht langsamer werden, weil eine Chronik laedt. Der
    /// Ueberlappungsschutz gilt trotzdem, aus demselben Grund wie dort.
    func refreshControl() async {
        guard let client, !controlRefreshing else { return }
        controlRefreshing = true
        defer { controlRefreshing = false }
        do {
            async let over = client.overview()
            async let taskList = client.tasks()
            async let inboxList = client.inbox()
            overview = try await over
            tasks = try await taskList
            inbox = try await inboxList
            snapshotAt = overview?.stand
            reachable = true
            controlError = ""
        } catch {
            // Der alte Stand bleibt stehen — aber als ALT gekennzeichnet. Ihn
            // zu loeschen waere unfreundlich, ihn als aktuell auszugeben waere
            // unehrlich.
            reachable = false
        }
    }

    /// Die selteneren Ansichten. Sie laden beim Oeffnen, nicht im Takt — eine
    /// Chronik alle drei Sekunden neu zu holen waere Verschwendung.
    func loadActivity() async {
        guard let client else { return }
        if let events = try? await client.activity() { activity = events }
    }

    func loadSystem() async {
        guard let client else { return }
        if let health = try? await client.systemHealth() {
            system = health.komponenten
        }
    }

    func startAutoRefresh() {
        guard pollTask == nil, client != nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                await self.refreshControl()
                await self.refreshContacts()
                if let client = self.client { await PushModel.shared.refresh(client: client) }
                let wait = await self.policy.interval
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
        }
    }

    func stopAutoRefresh() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - Was der Bildschirm daraus macht
    //
    // Alles hier ist ABGELEITET aus dem, was der Core geliefert hat. Es gibt
    // keinen zweiten Zustand und keine Zahl, die die App sich ausdenkt.

    var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<11: return "Guten Morgen."
        case 11..<18: return "Guten Tag."
        default: return "Guten Abend."
        }
    }

    /// Der eine Satz oben. Produktsprache, keine Komponentenliste.
    var statusSentence: String {
        if !reachable { return "SOLVIO ist gerade nicht erreichbar." }
        guard let overview else { return "Ich sehe nach …" }
        if !(overview.aufmerksamkeit ?? []).isEmpty || !pending.isEmpty {
            return "SOLVIO läuft — eine Sache braucht dich."
        }
        return overview.gesundheit.satz
    }

    var statusTone: Color {
        if !reachable { return Theme.warn }
        if !(overview?.aufmerksamkeit ?? []).isEmpty || !pending.isEmpty { return Theme.warn }
        return toneColor(overview?.gesundheit.zustand ?? "unknown")
    }

    var unread: Int { overview?.ungelesen ?? inbox.filter { !$0.gelesen }.count }

    /// Ein Abruf fuer alles, was ein Bildschirm zeigen kann.
    func refreshAll() async {
        await refresh()
        await refreshControl()
    }

    /// Nur fuer Tests und die Anzeige: der aktuelle Abstand zweier Nachfragen.
    var refreshInterval: TimeInterval { policy.interval }
    var isPolling: Bool { pollTask != nil }

    /// Fetch + verify the Mac-signed challenge. This is the ONLY authoritative source for
    /// the approval screen; until it succeeds no decision button may be enabled.
    func verifyChallenge(_ approvalID: String) async -> VerifiedChallenge? {
        guard let client else { return nil }
        do {
            return try await client.challenge(approvalID: approvalID)
        } catch {
            status = "Anfrage nicht verifizierbar: \(error.localizedDescription)"
            return nil
        }
    }

    /// Decide on an ALREADY-VERIFIED challenge. Everything signed comes from `vc.challenge`,
    /// which is exactly what was displayed — the unsigned list is never an input here.
    func decide(_ vc: VerifiedChallenge, approve: Bool) async {
        guard let client, let signer, let e = enrollment else { return }
        busy = true; defer { busy = false }
        let ch = vc.challenge
        do {
            guard Date().timeIntervalSince1970 < ch.expiresAt else {
                status = "Anzeige abgelaufen – bitte neu laden."; return
            }
            let decision = ApprovalDecision(
                coreInstanceID: ch.coreInstanceID, approvalID: ch.approvalID,
                actionDigest: ch.actionDigest, principalID: ch.principalID, deviceID: e.deviceID,
                keyID: signer.keyID, challengeNonce: ch.challengeNonce,
                challengePayloadSHA256: vc.payloadSHA256,
                decision: approve ? DECISION_APPROVE : DECISION_DENY,
                issuedAt: Int64(Date().timeIntervalSince1970), challengeExpiresAt: Int64(ch.expiresAt))
            let bytes = decision.canonicalBytes()
            let sig = try signer.sign(bytes)  // PROOF A: Secure Enclave -> Face ID prompt here
            var assertionB64: String? = nil
            if approve {                      // PROOF B: fresh App Attest assertion over the decision
                let db = AppAttestDecisionBinding(
                    coreInstanceID: ch.coreInstanceID, approvalID: ch.approvalID, deviceID: e.deviceID,
                    decisionSHA256: ApprovalCrypto.sha256Hex(bytes), challengeNonce: ch.challengeNonce,
                    approvalPublicKeySHA256: ApprovalCrypto.sha256Hex(signer.publicKeyX963))
                let assertion = try await AppAttestManager.assert(
                    keyId: e.appAttestKeyId, clientDataHash: db.clientDataHash())
                assertionB64 = assertion.base64EncodedString()
            }
            _ = try await client.submitDecision(
                approvalID: ch.approvalID, payloadB64: bytes.base64EncodedString(),
                signatureB64: sig.base64EncodedString(), keyID: signer.keyID, assertionB64: assertionB64)
            status = approve ? "Freigegeben." : "Abgelehnt."
            await refresh()
        } catch {
            status = "Nicht ausgeführt: \(error.localizedDescription)"
        }
    }

    func refreshContacts() async {
        guard let client else { return }
        await ContactRefreshModel.shared.sync(client: client)
    }

    func unpair() async {
        guard await PushModel.shared.disable(client: client) else {
            status = "Die Push-Abmeldung ist noch offen. Die Kopplung bleibt erhalten, damit SOLVIO sie bei Verbindung abschließen kann. Danach bitte erneut entkoppeln."
            return
        }
        ContactRefreshModel.shared.reset()
        taskStart.reset()
        Keychain.delete(tag: kEnrollTag)
        SecureEnclaveSigner.reset()
        AppAttestManager.reset()
        stopAutoRefresh()
        paired = false; pending = []; signer = nil; client = nil; enrollment = nil
    }
}


// MARK: - Die App, wie der Mensch sie sieht
//
// Ein Chat als Hauptansicht. Auftraege, Wissen, Freigaben und Hinweise sind
// ueber Mehr erreichbar; ihre vorhandenen Detailwege bleiben bestehen.
// Sprache bleibt im Chat. Mikrofon und Abschlussbestaetigung gehoeren weiterhin
// der vorhandenen VoiceSession, nicht einem zweiten Sprachsystem.

struct RootView: View {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--cost-preview") { TaskCostApprovalPreview() }
            else if PresenceGallery.requested { PresenceGallery() }
            else if ProcessInfo.processInfo.arguments.contains("--home-preview") {
                // Nur fuer die visuelle Schleife: der Start mit Beispieldaten,
                // ohne Kopplung. Existiert im Release nicht.
                SolvioTabs(model: AppModel.visualPreview())
            }
            else if model.paired { SolvioTabs(model: model) }
            else { NavigationStack { PairingView(model: model) } }
            #else
            if model.paired { SolvioTabs(model: model) }
            else { NavigationStack { PairingView(model: model) } }
            #endif
        }
        .tint(Theme.blue)
        .task { await model.refreshContacts() }
        .onReceive(NotificationCenter.default.publisher(for: .CNContactStoreDidChange)) { _ in
            ContactRefreshModel.shared.changed()
            if scenePhase == .active { Task { await model.refreshContacts() } }
        }
        .onChange(of: scenePhase) { phase in
            // Aktiv: sofort einmal nachsehen und dann im Takt bleiben. Alles
            // andere: aufhoeren. Wer aus einer anderen App zurueckkommt, soll
            // nicht ziehen muessen — das war der ganze Punkt.
            if phase == .active {
                Task { await model.refreshAll() }
                Task { await model.refreshContacts() }
                model.startAutoRefresh()
            } else {
                model.stopAutoRefresh()
            }
        }
    }
}

enum AssistantTab: String, CaseIterable, Hashable {
    case chat, overview, ideas, tasks, library
    var title: String {
        switch self {
        case .chat: return "Chat"
        case .overview: return "Überblick"
        case .ideas: return "Ideen"
        case .tasks: return "Aufgaben"
        case .library: return "Bibliothek"
        }
    }
    var icon: String {
        switch self {
        case .chat: return "bubble.left.and.bubble.right"
        case .overview: return "newspaper"
        case .ideas: return "lightbulb"
        case .tasks: return "checklist"
        case .library: return "square.grid.2x2"
        }
    }
}

struct SolvioTabs: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var pushNavigation = PushNavigation.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var selected: AssistantTab = .chat
    @State private var paths: [AssistantTab: NavigationPath] = [:]
    @StateObject private var library = AgentLibraryModel()
    @StateObject private var chats = ConversationModel()
    @StateObject private var composer = MessageComposerModel()
    @State private var ideaNotice = ""

    var body: some View {
        TabView(selection: $selected) {
            ForEach(AssistantTab.allCases, id: \.self) { tab in
                NavigationStack(path: path(for: tab)) {
                    section(tab)
                        .navigationDestination(for: HomeRoute.self) { destination($0) }
                }
                .tabItem { Label(tab.title, systemImage: tab.icon) }.tag(tab)
            }
        }
        .task(id: "\(scenePhase):\(pushNavigation.pendingInbox)") {
            await Task.yield()
            guard !Task.isCancelled else { return }
            openPendingPush()
        }
    }

    private func path(for tab: AssistantTab) -> Binding<NavigationPath> {
        Binding(get: { paths[tab] ?? NavigationPath() }, set: { paths[tab] = $0 })
    }
    private func open(_ route: HomeRoute) { paths[selected, default: NavigationPath()].append(route) }

    @ViewBuilder private func section(_ tab: AssistantTab) -> some View {
        if tab == .chat {
            HomeView(model: model, open: open, chats: chats, composer: composer)
        } else {
            Group {
                switch tab {
                case .overview: AssistantOverviewView(model: model)
                case .ideas: AssistantIdeasView(app: model, notice: ideaNotice, prepare: prepareIdea)
                case .tasks: AgentTasksView(app: model, openChat: { paths[.chat] = NavigationPath(); selected = .chat })
                case .library: AssistantLibraryView(app: model, results: library)
                case .chat: EmptyView()
                }
            }
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    AssistantMenu(model: model, open: open)
                }
                ToolbarItem(placement: .principal) {
                    Button { open(.activity) } label: {
                        HStack(spacing: 6) { Mark(size: 24); Text("SOLVIO").font(.headline) }
                    }.accessibilityLabel("SOLVIO Aktivitäten öffnen")
                }
            }
        }
    }

    private func prepareIdea(_ prompt: String) {
        guard !chats.creating, composer.stageSuggestion(prompt) else {
            ideaNotice = "Im Chat liegt noch eine Nachricht oder ein Anhang. Sende oder leere sie zuerst; dein Entwurf bleibt erhalten."
            return
        }
        chats.select(nil)
        paths[.chat] = NavigationPath()
        ideaNotice = ""
        selected = .chat
    }

    @ViewBuilder private func destination(_ route: HomeRoute) -> some View {
        Group {
            switch route {
            case .tasks: AgentTasksView(app: model, openChat: { paths[.chat] = NavigationPath(); selected = .chat })
            case .connections: ConnectionsView(model: model, onOpenApprovals: { open(.approvals) })
            case .knowledge: WissenView(onOpenApprovals: { open(.approvals) })
            case .approvals: ApprovalsView(model: model)
            case .inbox: InboxView(model: model)
            case .system: SystemView(model: model)
            case .activity: ActivityView(model: model)
            case .tresor: TresorView(onOpenApprovals: { open(.approvals) })
            case .zahlungen: PaymentView(onOpenApprovals: { open(.approvals) })
            }
        }.toolbar(.visible, for: .navigationBar)
    }

    private func openPendingPush() {
        guard pushNavigation.consume(isActive: scenePhase == .active) else { return }
        selected = .chat
        var destination = NavigationPath()
        destination.append(HomeRoute.inbox)
        paths[.chat] = destination
    }
}

struct PairingView: View {
    @ObservedObject var model: AppModel
    @State private var scanning = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Mark(size: 88)
            Wordmark(size: .largeTitle)
            Text("Koppeln: scanne den QR-Code, den der Mac anzeigt.")
                .font(.subheadline)
                .multilineTextAlignment(.center)
                .foregroundStyle(Theme.ink2)
                .padding(.horizontal, 32)
            Spacer()
            Button { Haptics.tap(); scanning = true } label: {
                Label("Gerät koppeln", systemImage: "qrcode.viewfinder")
                    .font(.display(.title3, weight: .semibold))
                    .foregroundStyle(Theme.onBlue)
                    .frame(maxWidth: .infinity).frame(height: 68)
                    .background(Theme.blue,
                                in: RoundedRectangle(cornerRadius: Theme.Radius.hero,
                                                     style: .continuous))
            }
            .buttonStyle(PressScale())
            if !model.status.isEmpty {
                Text(model.status).font(.footnote).foregroundStyle(Theme.ink2)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal, Theme.Space.margin)
        .padding(.bottom, 32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .sheet(isPresented: $scanning) {
            QRScannerView { code in scanning = false; Task { await model.pair(qr: code) } }
        }
    }
}
