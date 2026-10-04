// SOLVIO — Design-Prototyp.
//
// Reines Anschauungsstueck: Mock-Daten, kein Netz, keine Keychain, kein
// App Attest, keine Face ID, nichts wird gespeichert. Szenario und
// Sprachzustand sind per Startargument schaltbar, damit Screenshots
// deterministisch entstehen:
//
//   -scenario normal|attention|offline
//   -voice ready|listening|thinking|speaking|deepwork|reconnecting|offline|ended
//   -open voice          (Sprachraum sofort oeffnen)
//   -tab approvals|planned|inbox
import SwiftUI

enum AppTab: Hashable {
    case home, approvals, planned, inbox
}

@main
struct SolvioDesignApp: App {
    var body: some Scene {
        WindowGroup { RootView() }
    }
}

struct RootView: View {
    @StateObject private var model = DesignModel()
    @State private var tab: AppTab = .home
    @State private var talking = false
    @State private var approvalPath = NavigationPath()
    @State private var homePath = NavigationPath()

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack(path: $homePath) {
                HomeView(model: model, talking: $talking, tab: $tab)
                    .navigationDestination(for: String.self) { key in
                        if key == "system" { SystemView(model: model) }
                    }
            }
            .tabItem { Label("Start", systemImage: "house.fill") }
            .tag(AppTab.home)

            NavigationStack(path: $approvalPath) { ApprovalsView(model: model) }
                .tabItem { Label("Freigaben", systemImage: "faceid") }
                .tag(AppTab.approvals)
                .badge(model.approvals.count)

            NavigationStack { PlannedView(model: model) }
                .tabItem { Label("Geplant", systemImage: "calendar") }
                .tag(AppTab.planned)

            NavigationStack { InboxView(model: model) }
                .tabItem { Label("Hinweise", systemImage: "tray.fill") }
                .tag(AppTab.inbox)
                .badge(model.unreadCount)
        }
        .tint(Theme.blue)
        .fullScreenCover(isPresented: $talking) {
            VoiceView(model: model)
        }
        .onAppear {
            let args = ProcessInfo.processInfo.arguments
            if let index = args.firstIndex(of: "-open"), args.count > index + 1 {
                switch args[index + 1] {
                case "voice": talking = true
                case "approval":
                    tab = .approvals
                    if let first = model.approvals.first { approvalPath.append(first.id) }
                case "system":
                    homePath.append("system")
                default: break
                }
            }
            if let index = args.firstIndex(of: "-tab"), args.count > index + 1 {
                switch args[index + 1] {
                case "approvals": tab = .approvals
                case "planned": tab = .planned
                case "inbox": tab = .inbox
                default: break
                }
            }
        }
    }
}
