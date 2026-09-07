// ClaudiniBar — macOS menu bar: what's left on each Claude subscription, and
// one-click switching between them.
//
// All the policy lives in the Python engine: this app decodes what the engine
// decided and draws it. Build: swiftc -O -o ClaudiniBar main.swift -framework AppKit

import AppKit

let helper = NSString(string: "~/.claudini/tools/claudini_usage.py").expandingTildeInPath

/// How long a snapshot stays good enough to skip a reload. Matches the
/// engine's own SUCCESS_TTL, which would serve the same rows from cache.
let snapshotTTL: TimeInterval = 45
let refreshInterval: TimeInterval = 300

// MARK: - the engine's contract

struct Limit: Decodable {
    let kind: String?
    let label: String
    let percent: Int
    let level: String
    let resets_in_sec: Int?
}

struct Profile: Decodable {
    let name: String
    let email: String?
    let active: Bool
    let status: String
    let limits: [Limit]
    let limit_reset: Bool?
    let needs_login: Bool
    let space: String?
    let plan_label: String?
    let headroom: Int?
    let saturated: [String]
}

/// What the next `claude` session gets, and what stops us moving there.
struct NextUp: Decodable {
    let name: String?
    let reason: String
    let blocked_by: String?
}

struct Snapshot: Decodable {
    let profiles: [Profile]
    let auto: Bool
    let throttled_for: Int?
    let next: NextUp
}

// MARK: - helpers

@discardableResult
func run(_ launchPath: String, _ args: [String]) -> (Int32, Data) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: launchPath)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    // A menu bar app doesn't inherit the shell's PATH: widen it for `claudini`.
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = (env["PATH"] ?? "") + ":/usr/local/bin:/opt/homebrew/bin"
    p.environment = env
    do { try p.run() } catch { return (-1, Data()) }
    let out = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, out)
}

func text(_ s: String, _ size: CGFloat,
          _ weight: NSFont.Weight = .regular,
          _ color: NSColor = .labelColor) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
    ])
}

func digits(_ s: String, _ size: CGFloat, _ color: NSColor) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [
        .font: NSFont.monospacedDigitSystemFont(ofSize: size, weight: .regular),
        .foregroundColor: color,
    ])
}

func color(_ level: String) -> NSColor {
    switch level {
    case "critical": return .systemRed
    case "warning": return .systemOrange
    default: return .systemGreen
    }
}

func duration(_ seconds: Int?) -> String {
    guard let seconds else { return "" }
    let mins = seconds / 60
    if mins <= 0 { return "now" }
    if mins < 60 { return "\(mins)min" }
    if mins < 1440 { return String(format: "%dh%02d", mins / 60, mins % 60) }
    return "\(mins / 1440)d"
}

/// Short label for a limit, keeping the menu narrow.
func shortLabel(_ l: Limit) -> String {
    switch l.kind {
    case "session": return "5h"
    case "weekly_all": return "7d"
    default: return l.label
    }
}

// MARK: - app

final class Bar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var snapshot: Snapshot?
    var loadedAt = Date.distantPast
    var loading = false
    var timer: Timer?

    override init() {
        super.init()
        item.autosaveName = "ClaudiniBar"
        item.button?.title = "◌"
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        reload()
        timer = Timer.scheduledTimer(withTimeInterval: refreshInterval, repeats: true) {
            [weak self] _ in self?.reload()
        }
    }

    func reload() {
        guard !loading else { return }
        loading = true
        DispatchQueue.global(qos: .utility).async {
            // --tick lets the engine act on the auto policy in the same pass.
            let (_, data) = run("/usr/bin/env", ["python3", helper, "--json", "--tick"])
            let snap = try? JSONDecoder().decode(Snapshot.self, from: data)
            DispatchQueue.main.async {
                self.loading = false
                if let snap {
                    self.snapshot = snap
                    self.loadedAt = Date()
                }
                self.paint()
            }
        }
    }

    /// Run something slow without freezing the status item.
    func inBackground(_ work: @escaping () -> Void) {
        DispatchQueue.global(qos: .userInitiated).async(execute: work)
    }

    func paint() {
        guard let snap = snapshot, let active = snap.profiles.first(where: { $0.active }) else {
            item.button?.title = "⚠︎"
            return
        }
        let title = NSMutableAttributedString(attributedString:
            text(active.name + " ", 12, .medium))
        title.append(digits(active.headroom.map { "\($0)%" } ?? "?", 12,
                            active.headroom.map { color(level(spent: $0)) } ?? .labelColor))
        // A spent model quota is invisible in the headroom number: flag it.
        if !active.saturated.isEmpty {
            let initials = active.saturated.map { String($0.prefix(1)) }.joined()
            title.append(text(" " + initials, 11, .bold, .systemRed))
        }
        if snap.auto {
            title.append(text(" ⟳", 11, .bold, .controlAccentColor))
        }
        item.button?.attributedTitle = title
    }

    private func level(spent headroom: Int) -> String {
        headroom <= 5 ? "critical" : headroom <= 25 ? "warning" : "ok"
    }

    // MARK: menu

    func menuWillOpen(_ menu: NSMenu) {
        rebuild(menu)
        // The open menu is drawn from the snapshot we already have; only ask
        // the engine again when that snapshot has actually gone stale.
        if Date().timeIntervalSince(loadedAt) > snapshotTTL { reload() }
    }

    func rebuild(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let snap = snapshot else {
            menu.addItem(withTitle: "Loading…", action: nil, keyEquivalent: "")
            return
        }

        menu.addItem(heading("Using now"))
        if let active = snap.profiles.first(where: { $0.active }) {
            menu.addItem(readonly(row(active)))
        }

        menu.addItem(.separator())
        menu.addItem(heading("Next session"))
        menu.addItem(readonly(nextLine(snap)))

        menu.addItem(.separator())
        menu.addItem(heading("Switch to"))
        for p in snap.profiles where !p.active {
            let mi = NSMenuItem(title: p.name,
                                action: p.needs_login ? #selector(reconnect(_:))
                                                      : #selector(switchTo(_:)),
                                keyEquivalent: "")
            mi.target = self
            mi.representedObject = p.name
            mi.attributedTitle = row(p)
            menu.addItem(mi)
        }

        menu.addItem(.separator())
        if let pause = snap.throttled_for, pause > 0 {
            menu.addItem(readonly(text("API paused for \(pause)s — showing cached data",
                                       11, .regular, .secondaryLabelColor)))
        }
        let auto = NSMenuItem(title: snap.auto ? "Auto-switching: ON" : "Auto-switching: off",
                              action: #selector(toggleAuto), keyEquivalent: "a")
        auto.target = self
        auto.state = snap.auto ? .on : .off
        menu.addItem(auto)

        for (title, action, key) in [("Refresh", #selector(refreshNow), "r"),
                                     ("Quit", #selector(quit), "q")] {
            let mi = NSMenuItem(title: title, action: action, keyEquivalent: key)
            mi.target = self
            menu.addItem(mi)
        }
    }

    /// A non-clickable item; `heading` is one styled as a section title.
    func readonly(_ title: NSAttributedString) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.attributedTitle = title
        return item
    }

    func heading(_ title: String) -> NSMenuItem {
        readonly(NSAttributedString(string: title.uppercased(), attributes: [
            .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
            .foregroundColor: NSColor.tertiaryLabelColor,
            .kern: 0.8,
        ]))
    }

    func row(_ p: Profile) -> NSAttributedString {
        let out = NSMutableAttributedString()
        out.append(text("\(p.active ? "●" : "○") \(p.name)", 13, p.active ? .bold : .regular))

        // Two profiles can share one email in different workspaces, so the
        // workspace and plan are what actually tell them apart.
        var who = "  \(p.email ?? "?")"
        if let space = p.space { who += "  ·  \(space)" }
        if let plan = p.plan_label, !plan.isEmpty { who += " (\(plan))" }
        out.append(text(who + "\n", 11, .regular, .secondaryLabelColor))

        guard p.status == "ok" else {
            let hint = p.needs_login ? p.status + " — click to log in" : p.status
            out.append(text("     " + hint, 11, .regular, .systemRed))
            return out
        }

        out.append(text("     ", 11))
        for (i, l) in p.limits.enumerated() {
            if i > 0 { out.append(text(" · ", 11, .regular, .tertiaryLabelColor)) }
            out.append(digits("\(shortLabel(l)) \(l.percent)%", 11, color(l.level)))
        }
        if let session = p.limits.first(where: { $0.kind == "session" }) {
            out.append(text("  ↻ \(duration(session.resets_in_sec))", 11,
                            .regular, .secondaryLabelColor))
        }
        // `/limit-reset` is only open on some accounts: say which.
        if p.limit_reset == true {
            out.append(text("  ⟲", 11, .bold, .systemTeal))
        }
        return out
    }

    /// The engine already decided where the next session goes and why; this
    /// only spells its answer out.
    func nextLine(_ snap: Snapshot) -> NSAttributedString {
        guard let name = snap.next.name else {
            return text("  " + snap.next.reason, 12, .regular, .secondaryLabelColor)
        }
        let staying = snap.profiles.first(where: { $0.active })?.name == name
        let out = NSMutableAttributedString(attributedString:
            text("  \(name)  ", 13, staying ? .medium : .semibold,
                 staying ? .labelColor : .systemGreen))
        out.append(text(snap.next.reason, 11, .regular, .secondaryLabelColor))
        if let blocked = snap.next.blocked_by {
            out.append(text("\n     not switching: \(blocked)", 11,
                            .regular, .tertiaryLabelColor))
        }
        return out
    }

    // MARK: actions

    /// The OAuth login needs a real terminal, so open one.
    @objc func reconnect(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        let cmd = "claudini-usage --reconnect \(name)"
        inBackground {
            run("/usr/bin/osascript", ["-e",
                "tell application \"Terminal\" to do script \"\(cmd)\"",
                "-e", "tell application \"Terminal\" to activate"])
        }
    }

    @objc func switchTo(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        inBackground {
            let (code, _) = run("/usr/bin/env", ["claudini", "use", name])
            DispatchQueue.main.async {
                let alert = NSAlert()
                if code == 0 {
                    alert.messageText = "Now using \(name)"
                    alert.informativeText = "Open `claude` sessions keep the account they "
                        + "started with. Relaunch them to pick up \(name)."
                } else {
                    alert.alertStyle = .critical
                    alert.messageText = "Could not switch"
                    alert.informativeText = "`claudini use \(name)` failed. Try it in a terminal."
                }
                alert.runModal()
                self.reload()
            }
        }
    }

    @objc func toggleAuto() {
        let wanted = !(snapshot?.auto ?? false)
        inBackground {
            run("/usr/bin/env", ["python3", helper, "--auto", wanted ? "on" : "off"])
            DispatchQueue.main.async { self.reload() }
        }
    }

    @objc func refreshNow() { reload() }
    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let bar = Bar()
app.run()
