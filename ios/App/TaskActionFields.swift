import SwiftUI
import SolvioApprovalsKit

struct TaskActionFields: View {
    @ObservedObject var model: TaskStartModel
    let client: ApprovalClient?
    @Environment(\.dynamicTypeSize) private var typeSize

    private var kindChoices: some View {
        Group {
            Text("Kalender").tag(TaskActionForm.Kind.calendar)
            Text("Mailentwurf").tag(TaskActionForm.Kind.gmail)
            Text("Zuhause").tag(TaskActionForm.Kind.ha)
            Text("Portalstatus").tag(TaskActionForm.Kind.portal)
        }
    }

    var body: some View {
        if typeSize.isAccessibilitySize {
            TaskAccessibleMenu("Was soll SOLVIO erledigen?", value: model.actionForm.kind == .portal ? "Portalstatus" : model.actionForm.kind == .ha ? "Zuhause" : model.actionForm.kind == .gmail ? "Mailentwurf" : "Kalender") {
                Picker("Was soll SOLVIO erledigen?", selection: $model.actionForm.kind) { kindChoices }
            }
        } else {
            Picker("Was soll SOLVIO erledigen?", selection: $model.actionForm.kind) { kindChoices }
                .pickerStyle(.menu)
        }

        if model.actionForm.kind != .portal {
          if model.servicesLoading {
            HStack { ProgressView(); Text("Konten vom Core laden …").font(.footnote) }
        } else {
            Text(model.servicesMessage.isEmpty ? model.actionAccountMessage : model.servicesMessage)
                .font(.footnote).foregroundStyle(.secondary)
            // A stale previous choice also requires an explicit replacement,
            // even when only one different account is now offered.
            if model.actionAccountChoices.count > 1 ||
                (!model.actionAccountChoices.isEmpty && model.actionForm.selectedAccount != nil && model.selectedActionAccount == nil) {
                Picker("Verknüpftes Konto", selection: Binding<ActionServiceAccount?>(
                    get: { model.selectedActionAccount }, set: { model.selectActionAccount($0) })) {
                    Text("Bitte Konto wählen").tag(Optional<ActionServiceAccount>.none)
                    ForEach(model.actionAccountChoices, id: \.self) { row in
                        Text(row.service == "calendar" ? "\(row.displayLabel) · \(row.resource)" : row.displayLabel)
                            .tag(Optional(row))
                    }
                }
                .pickerStyle(.menu)
            }
            if !model.servicesMessage.isEmpty || model.actionServices.filter({ $0.service == model.actionForm.kind.rawValue }).count != 1 {
                Button("Konten erneut laden") {
                    if let client { Task { await model.loadActionServices(client: client) } }
                }
            }
          }
        }

        if model.actionForm.kind == .portal {
            TaskPortalFields(model: model, client: client)
        } else if model.actionForm.kind == .calendar {
            TextField("Titel des Termins", text: $model.actionForm.title)
            TaskDateTimeField(title: "Beginn", selection: $model.actionForm.start)
                .environment(\.timeZone, TimeZone(identifier: model.actionForm.timeZoneIdentifier) ?? .gmt)
            TaskDateTimeField(title: "Ende", selection: $model.actionForm.end)
                .environment(\.timeZone, TimeZone(identifier: model.actionForm.timeZoneIdentifier) ?? .gmt)
            if model.actionForm.end <= model.actionForm.start {
                Text("Das Ende muss nach dem Beginn liegen.").font(.footnote).foregroundStyle(Theme.warn)
            }
            Text("Zeitzone: \(model.actionForm.timeZoneIdentifier)")
                .font(.caption).foregroundStyle(.secondary)
            DisclosureGroup("Ort & Beschreibung") {
                TextField("Ort (optional)", text: $model.actionForm.location)
                TextField("Beschreibung (optional)", text: $model.actionForm.details, axis: .vertical)
                    .lineLimit(3...8)
            }
        } else if model.actionForm.kind == .ha {
            TaskHomeFields(model: model, client: client)
        } else {
            TextField("Empfänger (E-Mail-Adresse)", text: $model.actionForm.recipient)
                .keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
            Picker("Entwurf", selection: $model.actionForm.mailMode) {
                Text("SOLVIO formulieren lassen").tag(TaskActionForm.MailMode.compose)
                Text("Fertigen Text verwenden").tag(TaskActionForm.MailMode.exact)
            }
            .pickerStyle(.menu)
            if model.actionForm.mailMode == .compose {
                TextField("Dein Anliegen", text: $model.actionForm.instruction, axis: .vertical)
                    .lineLimit(5...14)
                Text("Beschreibe, worum es geht und welchen Ton du möchtest. SOLVIO formuliert Betreff und Nachricht.")
                    .font(.footnote).foregroundStyle(.secondary)
            } else {
                TextField("Betreff", text: $model.actionForm.subject)
                TextField("Nachricht", text: $model.actionForm.body, axis: .vertical)
                    .lineLimit(5...14)
            }
            Text("SOLVIO legt den Entwurf in Gmail ab. Es wird keine Nachricht versendet.")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }
}

/// Compact system date chips have an unbreakable minimum width at accessibility
/// sizes. A wrapping value opens the same native date binding at full sheet
/// width; no date parsing, rounding or font-size override is introduced.
private struct TaskDateTimeField: View {
    let title: String
    @Binding var selection: Date
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.timeZone) private var timeZone
    @Environment(\.locale) private var locale
    @Environment(\.calendar) private var calendar
    @State private var showingPicker = false

    private var value: String {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.calendar = calendar
        formatter.timeZone = timeZone
        formatter.dateStyle = .long
        formatter.timeStyle = .short
        return formatter.string(from: selection)
    }

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: 8) {
                    Text(title).fixedSize(horizontal: false, vertical: true)
                    Button { showingPicker = true } label: {
                        Text(value).multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .accessibilityLabel(title).accessibilityValue(value)
                    .accessibilityHint("Datum und Uhrzeit ändern")
                }
            } else {
                DatePicker(title, selection: $selection, displayedComponents: [.date, .hourAndMinute])
            }
        }
        .sheet(isPresented: $showingPicker) {
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        DatePicker("Datum", selection: $selection, displayedComponents: .date)
                            .datePickerStyle(.wheel).labelsHidden()
                            .accessibilityLabel(title + ": Datum")
                        DatePicker("Uhrzeit", selection: $selection, displayedComponents: .hourAndMinute)
                            .datePickerStyle(.wheel).labelsHidden()
                            .accessibilityLabel(title + ": Uhrzeit")
                    }.frame(maxWidth: .infinity)
                }
                .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Fertig") { showingPicker = false }
                    }
                }
            }
            .environment(\.timeZone, timeZone)
            .presentationDragIndicator(.visible)
        }
    }
}

/// SwiftUI's compact menu Picker clips its selected label at large accessibility
/// sizes. Keep its original binding/choices in a native Menu, with a fully
/// wrapping label above it; this view adds no selection or task behavior.
struct TaskAccessibleMenu<Choices: View>: View {
    let title: String
    let value: String
    let choices: Choices
    init(_ title: String, value: String, @ViewBuilder choices: () -> Choices) {
        self.title = title; self.value = value; self.choices = choices()
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).fixedSize(horizontal: false, vertical: true)
            Menu {
                choices
            } label: {
                HStack(alignment: .center, spacing: 8) {
                    Text(value).fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.up.chevron.down").font(.caption)
                        .accessibilityHidden(true)
                }
            }
            .accessibilityLabel(title).accessibilityValue(value)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}
