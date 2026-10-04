import SwiftUI
import SolvioApprovalsKit

struct TaskPortalFields: View {
    @ObservedObject var model: TaskStartModel
    let client: ApprovalClient?
    @Environment(\.dynamicTypeSize) private var typeSize
    private var loadKey: String {
        [client?.pairedCoreInstanceID ?? "", client?.deviceIdentifier ?? ""].joined(separator: ":")
    }
    private var sessionPicker: some View {
        Picker("Eigene Sitzung", selection: Binding<String?>(
            get: { model.actionForm.portal.selectedSession?.account },
            set: { model.selectPortalSession($0) })) {
            Text("Bitte Sitzung wählen").tag(Optional<String>.none)
            ForEach(model.actionForm.portal.catalogue?.items ?? [], id: \.account) { item in
                Text((model.actionForm.portal.catalogue?.displayLabel(for: item) ?? item.displayLabel) + (item.authenticated ? "" : " · Anmeldung nicht bestätigt"))
                    .tag(Optional(item.account))
                    .disabled(model.actionForm.portal.catalogue?.usable(item) != true)
            }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.portalLoading {
                HStack { ProgressView(); Text("Eigene Portal-Sitzungen laden …").font(.footnote) }
            } else if let catalogue = model.actionForm.portal.catalogue {
                if !catalogue.items.isEmpty {
                    if typeSize.isAccessibilitySize {
                        TaskAccessibleMenu("Eigene Sitzung", value: model.actionForm.portal.selectedSession.map { catalogue.displayLabel(for: $0) } ?? "Bitte Sitzung wählen") { sessionPicker }
                    } else { sessionPicker.pickerStyle(.menu) }
                }
                if let selected = model.actionForm.portal.selectedSession {
                    Text(verbatim: selected.origin).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(catalogue.usable(selected) ? "Beim letzten Lesen als angemeldet bestätigt." : "Die Anmeldung ist nicht mehr aktuell bestätigt. Bitte erneut laden.")
                        .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                } else if model.actionForm.portal.selectedAccount != nil {
                    Text("Die bisher gewählte Sitzung ist nicht mehr verfügbar. Bitte neu auswählen.")
                        .font(.footnote).foregroundStyle(Theme.warn).fixedSize(horizontal: false, vertical: true)
                }
                if catalogue.truncated {
                    Text("Es werden die ersten 50 eigenen Sitzungen angezeigt.")
                        .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            if !model.portalMessage.isEmpty {
                Text(verbatim: model.portalMessage).font(.footnote).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button("Sitzungen erneut laden") { if let client { Task { await model.loadPortalSessions(client: client) } } }
                .disabled(model.portalLoading || client == nil)
            Text("Mit „Auftrag erteilen“ liest SOLVIO den aktuellen Status der gewählten Sitzung. Hier wird keine neue Anmeldung gestartet.")
                .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .task(id: loadKey) { if let client { await model.loadPortalSessions(client: client) } }
    }
}
