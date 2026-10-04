import SwiftUI
import SolvioApprovalsKit

struct TaskHomeFields: View {
    @ObservedObject var model: TaskStartModel
    let client: ApprovalClient?
    @Environment(\.dynamicTypeSize) private var typeSize

    private var loadKey: String {
        [model.selectedActionAccount?.account ?? "", client?.pairedCoreInstanceID ?? "",
         client?.deviceIdentifier ?? ""].joined(separator: ":")
    }
    private var devicePicker: some View {
        Picker("Gerät", selection: Binding<String?>(get: {
            model.actionForm.home.selectedDevice?.entityID
        }, set: { model.selectHomeDevice($0) })) {
            Text("Bitte Gerät wählen").tag(Optional<String>.none)
            ForEach(model.actionForm.home.catalogue?.items ?? [], id: \.entityID) { item in
                Text(item.displayLabel).tag(Optional(item.entityID))
            }
        }
    }
    private var operationLabel: String {
        switch model.actionForm.home.desired {
        case .on: return "Einschalten"
        case .off: return "Ausschalten"
        case .brightness: return "Helligkeit einstellen"
        case nil: return "Bitte Aktion wählen"
        }
    }
    private var operationPicker: some View {
        Picker("Aktion", selection: $model.actionForm.home.desired) {
            Text("Bitte Aktion wählen").tag(Optional<TaskHomeForm.Desired>.none)
            Text("Einschalten").tag(Optional(TaskHomeForm.Desired.on))
            Text("Ausschalten").tag(Optional(TaskHomeForm.Desired.off))
            if model.actionForm.home.selectedDevice?.operations.contains("set_brightness") == true {
                Text("Helligkeit einstellen").tag(Optional(TaskHomeForm.Desired.brightness))
            }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.homeLoading {
                HStack { ProgressView(); Text("Freigegebene Geräte laden …").font(.footnote) }
            } else if let catalogue = model.actionForm.home.catalogue {
                if typeSize.isAccessibilitySize {
                    TaskAccessibleMenu("Gerät", value: model.actionForm.home.selectedDevice?.displayLabel ?? "Bitte Gerät wählen") { devicePicker }
                } else { devicePicker.pickerStyle(.menu) }
                if let selected = model.actionForm.home.selectedDevice {
                    Text("Aktuell: \(selected.stateLabel)").font(.footnote).foregroundStyle(.secondary)
                    if typeSize.isAccessibilitySize {
                        TaskAccessibleMenu("Aktion", value: operationLabel) { operationPicker }
                    } else { operationPicker.pickerStyle(.menu) }
                    if model.actionForm.home.desired == .brightness {
                        if typeSize.isAccessibilitySize {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Helligkeit: \(model.actionForm.home.brightnessPercent) %")
                                    .fixedSize(horizontal: false, vertical: true)
                                Stepper("Helligkeit", value: $model.actionForm.home.brightnessPercent, in: 0...100)
                                    .labelsHidden()
                                    .accessibilityValue("\(model.actionForm.home.brightnessPercent) Prozent")
                            }
                        } else {
                            Stepper("Helligkeit: \(model.actionForm.home.brightnessPercent) %",
                                value: $model.actionForm.home.brightnessPercent, in: 0...100)
                        }
                    }
                } else if model.actionForm.home.selectedEntityID != nil {
                    Text("Das bisher gewählte Gerät ist nicht mehr verfügbar. Bitte neu auswählen.")
                        .font(.footnote).foregroundStyle(Theme.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if catalogue.truncated {
                    Text("Es werden die ersten 100 freigegebenen Geräte angezeigt.")
                        .font(.footnote).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !model.homeMessage.isEmpty {
                Text(model.homeMessage).font(.footnote).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button("Geräte erneut laden") {
                if let client { Task { await model.loadHomeResources(client: client) } }
            }
            .disabled(model.homeLoading || model.selectedActionAccount == nil)
            Text("Die Auswahl führt noch nichts aus. Mit „Auftrag erteilen“ übernimmt SOLVIO die gewählte Einstellung.")
                .font(.footnote).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .task(id: loadKey) {
            if let client { await model.loadHomeResources(client: client) }
        }
    }
}
