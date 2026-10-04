#if DEBUG
import Foundation
import SwiftUI
import SolvioApprovalsKit

/// Visual test input only. No AppModel, credentials, URLSession or provider.
/// The confirmation remains disabled even on a signed Debug app.
struct TaskCostApprovalPreview: View {
    @StateObject private var model = TaskCostApprovalModel()
    @State private var showing = false
    private let run: AgentRun = try! JSONDecoder().decode(AgentRun.self, from: Data(#"{"id":"ar-1111111111111111","aufgabe":"at-1111111111111111","auftrag":"Synthetischer Test: Erstelle eine lokale Tabelle.","zustand":"Wartet auf Kostenfreigabe","zustand_code":"WAITING_USER","offen":true,"anbietergrenze":{"grund":"cost_approval_required","fortsetzbar":true},"kosten":{"configured":true,"task_id":"at-1111111111111111","currency":"EUR","ask_threshold_cents":1000,"approved_ai_cap_cents":1000,"ai_tool":{"spent_cents":125,"reserved_cents":75},"counts":{"unknown":0}}}"#.utf8))
    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Text("Vorschau mit erfundenem Auftrag").font(.headline)
                Text("Keine Verbindung, keine echte Freigabe.").foregroundStyle(Theme.ink2)
                Button("Kostenrahmen freigeben") { showing = true }
                    .buttonStyle(.borderedProminent).accessibilityIdentifier("task.cost.open")
            }.padding().navigationTitle("Kostenfenster-Vorschau")
        }
        .task { model.configure(taskID: run.aufgabe, coreID: "preview", deviceID: "preview", retry: nil) }
        .sheet(isPresented: $showing) {
            TaskCostApprovalSheet(run: run, model: model, allowed: false,
                onConfirm: {}, onClose: { showing = false })
        }
    }
}
#endif
