// The settings window: everything the policy can be told, the accounts and
// what can be done to each, and how the app itself behaves.
//
// The menu used to hide these in submenus, and two settings — the line an
// account counts as spent at, and the wait between switches — could only be
// changed by editing a JSON file. Every change here goes through the engine
// (`--auto`, `--mode`, `--drain`, `--set`, `--on/--off`, `--rename`,
// `--remove`), which answers with a fresh snapshot.

import AppKit
import ServiceManagement
import SwiftUI

/// What the menu bar item shows next to its ring.
enum MenuBarStyle: String, CaseIterable {
    case percent, ring, name

    var title: String {
        switch self {
        case .percent: return "Ring and usage (5h 29%)"
        case .ring: return "Ring only"
        case .name: return "Ring, usage and account"
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: PanelModel
    @AppStorage("menuBar.style") private var menuBarStyle = MenuBarStyle.percent.rawValue
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?
    @State private var renaming: String?
    @State private var newName = ""
    @State private var removing: String?

    var body: some View {
        Form {
            if let snap = model.snapshot {
                switching(snap)
                accounts(snap)
            } else {
                Text("Reading your accounts…").foregroundStyle(.secondary)
            }
            app
            commandLine
        }
        .formStyle(.grouped)
        .frame(width: 560, height: 640)
        .alert("Rename \(renaming ?? "")", isPresented: SwiftUI.Binding(
            get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("New name", text: $newName)
            Button("Rename") {
                if let old = renaming, validProfileName(newName) { model.onRename(old, newName) }
                renaming = nil
            }
            Button("Cancel", role: .cancel) { renaming = nil }
        } message: {
            Text("Up to 40 letters, digits, '.', '_' or '-'.")
        }
        .confirmationDialog("Remove \(removing ?? "")?", isPresented: SwiftUI.Binding(
            get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("Remove", role: .destructive) {
                if let name = removing { model.onRemove(name) }
                removing = nil
            }
        } message: {
            Text("Its saved login is deleted from this Mac. The Claude account itself is not touched, "
                 + "and you can add it again later.")
        }
    }

    // MARK: switching

    @ViewBuilder
    private func switching(_ snap: Snapshot) -> some View {
        let settings = snap.settings
        Section {
            Toggle("Switch accounts automatically", isOn: SwiftUI.Binding(
                get: { snap.auto }, set: { model.onAuto($0) }))
            Picker("Optimise for", selection: SwiftUI.Binding(
                get: { snap.mode }, set: { model.onMode($0) })) {
                ForEach(snap.modes, id: \.name) { Text($0.title).tag($0.name) }
            }
            .pickerStyle(.radioGroup)
            if let settings {
                LabeledContent {
                    Stepper(value: SwiftUI.Binding(get: { settings.max_usage },
                                           set: { model.onSet("max_usage", $0) }),
                            in: 50...100, step: 1) {
                        Text("\(settings.max_usage)%").monospacedDigit()
                    }
                } label: {
                    Text("Count an account as spent at")
                    Text("Lower it to move off an account before it hits the wall.")
                }
                LabeledContent {
                    Stepper(value: SwiftUI.Binding(get: { settings.cooldown_min },
                                           set: { model.onSet("cooldown_min", $0) }),
                            in: 0...120, step: 5) {
                        Text("\(settings.cooldown_min) min").monospacedDigit()
                    }
                } label: {
                    Text("Wait between switches")
                    Text("After any switch, by hand or automatic.")
                }
                Toggle(isOn: SwiftUI.Binding(get: { settings.drain }, set: { model.onDrain($0) })) {
                    Text("Spend the last few percent")
                    Text("Once every account is past the line, use what is left above it, one at a time.")
                }
            }
        } header: {
            Text("Switching")
        }
    }

    // MARK: accounts

    private func accounts(_ snap: Snapshot) -> some View {
        Section {
            ForEach(snap.profiles, id: \.name) { profile in
                HStack(spacing: 10) {
                    Circle().fill(profile.state(line: model.line).tint).frame(width: 8, height: 8)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 6) {
                            Text(profile.name).fontWeight(.medium)
                            if profile.active {
                                Text("in use").font(.caption2.weight(.semibold))
                                    .foregroundStyle(Color.accentColor)
                            }
                        }
                        Text(subtitle(profile)).font(.caption)
                            .foregroundStyle(profile.binding == nil ? Color.orange : Color.secondary)
                            .lineLimit(1)
                    }
                    Spacer()
                    if profile.needs_login {
                        Button("Log in again") { model.onReconnect(profile.name) }.controlSize(.small)
                    }
                    Menu {
                        if !profile.active && !profile.disabled && !profile.needs_login {
                            Button("Use this account") { model.onSwitch(profile.name) }
                        }
                        Button("Rename…") { newName = profile.name; renaming = profile.name }
                        Button("Remove…", role: .destructive) { removing = profile.name }
                            .disabled(profile.active)
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    Toggle("", isOn: SwiftUI.Binding(get: { !profile.disabled },
                                             set: { model.onRotation(profile.name, $0) }))
                        .toggleStyle(.switch).controlSize(.mini).labelsHidden()
                        .help("In the rotation: auto-switching may choose it")
                }
            }
            Button {
                model.onAdd()
            } label: {
                Label("Add account…", systemImage: "plus")
            }
        } header: {
            Text("Accounts")
        } footer: {
            Text("Switched off: still read and shown, never chosen by auto-switching.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func subtitle(_ profile: Profile) -> String {
        if profile.binding == nil { return profile.detail }
        var parts = [planLine(profile)]
        if let email = profile.email { parts.append(email) }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    // MARK: app

    private var app: some View {
        Section {
            Picker("Menu bar shows", selection: $menuBarStyle) {
                ForEach(MenuBarStyle.allCases, id: \.rawValue) { Text($0.title).tag($0.rawValue) }
            }
            .onChange(of: menuBarStyle) { model.onMenuBarStyle() }
            Toggle("Open at login", isOn: SwiftUI.Binding(get: { openAtLogin }, set: setOpenAtLogin))
            if let loginError {
                Text(loginError).font(.caption).foregroundStyle(.orange)
            }
        } header: {
            Text("App")
        }
    }

    private func setOpenAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginError = nil
        } catch {
            loginError = "macOS refused: \(error.localizedDescription)"
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }

    // MARK: command line

    private var commandLine: some View {
        Section {
            ForEach(model.snapshot?.actions ?? [], id: \.command) { action in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("claudini-usage " + action.command)
                            .font(.system(size: 11, design: .monospaced))
                        Text(action.about).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("claudini-usage " + action.command, forType: .string)
                    } label: {
                        Image(systemName: "doc.on.doc")
                    }
                    .buttonStyle(.borderless)
                    .help("Copy")
                }
            }
        } header: {
            Text("Command line")
        }
    }
}

/// The engine's rule for a profile name.
func validProfileName(_ name: String) -> Bool {
    name.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", options: .regularExpression) != nil
}
