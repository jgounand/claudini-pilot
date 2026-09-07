// ClaudiniBar — macOS menu bar: what's left on each Claude subscription, and
// one-click switching between them.
//
// All the policy lives in the Python engine: this app decodes what the engine
// decided and draws it. Build: swiftc -O -o ClaudiniBar main.swift -framework AppKit

import AppKit

let helper = NSString(string: "~/.claudini/tools/claudini_usage.py").expandingTildeInPath

/// Fallback poll period, used only until the first snapshot arrives: after
/// that the engine tells us its own cadence and staleness window.
let defaultPoll: TimeInterval = 300

// MARK: - the engine's contract

struct Limit: Decodable {
    let kind: String?
    let label: String
    let short_label: String
    let percent: Int
    let level: String
    /// Absolute, so the countdown stays right even drawn from a stale snapshot.
    let resets_at_epoch: Int?
}

struct Profile: Decodable {
    let name: String
    let email: String?
    let active: Bool
    let status: String
    let detail: String?
    let limits: [Limit]
    let limit_reset: Bool?
    let needs_login: Bool
    let space: String?
    let plan_label: String?
    let headroom: Int?
    let headroom_level: String?
    let binding: Binding?
    let saturated: [String]
}

/// The general window that will stop you first — which one it is changes
/// through the day, so the engine names it rather than the app assuming.
struct Binding: Decodable {
    let label: String
    let percent: Int
    let headroom: Int
    let level: String
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
    let mode: String
    let modes: [String]
    let throttled_for: Int?
    let throttle_notice: String?
    let poll_after_sec: Int
    let stale_after_sec: Int
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

/// Monospaced, so every row's columns land in the same place. The dropdown is
/// a table; letting proportional text shuffle the numbers is what made it
/// unreadable at a glance.
func mono(_ s: String, _ size: CGFloat,
          _ weight: NSFont.Weight = .regular,
          _ color: NSColor = .labelColor) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [
        .font: NSFont.monospacedSystemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
    ])
}

func pad(_ s: String, _ width: Int) -> String {
    s.count >= width ? String(s.prefix(width))
                     : s + String(repeating: " ", count: width - s.count)
}

func padLeft(_ s: String, _ width: Int) -> String {
    s.count >= width ? String(s.prefix(width))
                     : String(repeating: " ", count: width - s.count) + s
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

func countdown(to epoch: Int?) -> String {
    guard let epoch else { return "" }
    let mins = max(0, epoch - Int(Date().timeIntervalSince1970)) / 60
    if mins <= 0 { return "now" }
    if mins < 60 { return "\(mins)min" }
    if mins < 1440 { return String(format: "%dh%02d", mins / 60, mins % 60) }
    return "\(mins / 1440)d"
}

/// The mode names come from the engine; these are the human descriptions.
func modeTitle(_ mode: String) -> String {
    switch mode {
    case "model": return "model — keep Fable available"
    case "endurance": return "endurance — spend what resets soonest"
    default: return mode
    }
}

/// A ring filled to `percent`, for the menu bar. Reads at a glance at a size
/// where three of them would be unreadable.
func gauge(_ percent: Int, _ shade: NSColor, size: CGFloat = 13) -> NSImage {
    let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
        let width: CGFloat = 2.5
        let inset = rect.insetBy(dx: width / 2, dy: width / 2)
        let centre = NSPoint(x: rect.midX, y: rect.midY)
        let radius = inset.width / 2

        let track = NSBezierPath(ovalIn: inset)
        track.lineWidth = width
        shade.withAlphaComponent(0.25).setStroke()
        track.stroke()

        let filled = NSBezierPath()
        filled.appendArc(withCenter: centre, radius: radius, startAngle: 90,
                         endAngle: 90 - 3.6 * CGFloat(min(100, max(0, percent))),
                         clockwise: true)
        filled.lineWidth = width
        filled.lineCapStyle = .round
        shade.setStroke()
        filled.stroke()
        return true
    }
    image.isTemplate = false
    return image
}

// MARK: - app

final class Bar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var snapshot: Snapshot?
    var loadedAt = Date.distantPast
    var loading = false
    var poll = defaultPoll
    var timer: Timer?

    override init() {
        super.init()
        item.autosaveName = "ClaudiniBar"
        item.button?.title = "◌"
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        reload()
        schedule(defaultPoll)
    }

    /// The engine owns the cadence; re-arm whenever it says something else.
    func schedule(_ interval: TimeInterval) {
        poll = interval
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) {
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
                    if TimeInterval(snap.poll_after_sec) != self.poll {
                        self.schedule(TimeInterval(snap.poll_after_sec))
                    }
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
            item.button?.image = nil
            item.button?.title = "⚠︎"
            return
        }
        let shade = active.binding.map { color($0.level) } ?? .labelColor
        item.button?.image = active.binding.map { gauge($0.percent, shade) }
        item.button?.imagePosition = .imageLeading

        // Name the window that is actually closest, because which one binds
        // changes through the day: the 5-hour early on, the weekly by Friday.
        let title = NSMutableAttributedString(attributedString:
            text(" " + active.name + " ", 12, .medium))
        if let binding = active.binding {
            title.append(digits("\(binding.label) \(binding.headroom)%", 12, shade))
        } else {
            title.append(text("?", 12))
        }
        // The spent-model marker only means something while the policy is
        // protecting that model; in endurance mode it is just noise.
        if snap.mode == "model", !active.saturated.isEmpty {
            title.append(text(" " + active.saturated.map { String($0.prefix(1)) }.joined(),
                              11, .bold, .systemRed))
        }
        if snap.auto {
            title.append(text(" ⟳", 11, .bold, .controlAccentColor))
        }
        item.button?.attributedTitle = title
    }

    // MARK: menu

    func menuWillOpen(_ menu: NSMenu) {
        rebuild(menu)
        // The open menu is drawn from the snapshot we already have; only ask
        // the engine again when that snapshot has actually gone stale.
        let stale = TimeInterval(snapshot?.stale_after_sec ?? Int(defaultPoll))
        if Date().timeIntervalSince(loadedAt) > stale { reload() }
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
        if let notice = snap.throttle_notice {
            menu.addItem(readonly(text(notice, 11, .regular, .secondaryLabelColor)))
        }
        let auto = NSMenuItem(title: snap.auto ? "Auto-switching: ON" : "Auto-switching: off",
                              action: #selector(toggleAuto), keyEquivalent: "a")
        auto.target = self
        auto.state = snap.auto ? .on : .off
        menu.addItem(auto)

        // Two goals, two rankings: protect the preferred model, or spend the
        // allowance that resets soonest so nothing expires unused.
        let modes = NSMenuItem(title: "Mode: \(snap.mode)", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        for name in snap.modes {
            let mi = NSMenuItem(title: modeTitle(name), action: #selector(setMode(_:)),
                                keyEquivalent: "")
            mi.target = self
            mi.representedObject = name
            mi.state = name == snap.mode ? .on : .off
            submenu.addItem(mi)
        }
        modes.submenu = submenu
        menu.addItem(modes)

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

    /// One profile as two aligned lines: who it is, then its numbers in fixed
    /// columns so the eye can compare rows without reading them.
    func row(_ p: Profile) -> NSAttributedString {
        let out = NSMutableAttributedString()
        out.append(mono("\(p.active ? "●" : "○") \(pad(p.name, 16))", 12,
                        p.active ? .bold : .regular))

        // Two profiles can share one email in different workspaces, so the
        // workspace and plan are what actually tell them apart.
        var who = p.space ?? (p.email ?? "?")
        if let plan = p.plan_label, !plan.isEmpty { who += "  ·  \(plan)" }
        out.append(mono(who + "\n", 11, .regular, .secondaryLabelColor))

        guard !p.limits.isEmpty else {
            var hint = p.detail ?? p.status
            if p.needs_login { hint += " — click to log in" }
            out.append(mono("   " + hint, 11, .regular, .systemRed))
            return out
        }

        out.append(mono("   ", 11))
        for kind in ["session", "weekly_all"] {
            append(slot: p.limits.first { $0.kind == kind }, to: out)
        }
        append(slot: p.limits.first { $0.kind != "session" && $0.kind != "weekly_all" }, to: out)

        let session = p.limits.first { $0.kind == "session" }
        out.append(mono(" ↻ " + pad(countdown(to: session?.resets_at_epoch), 6), 11,
                        .regular, .secondaryLabelColor))
        // `/limit-reset` is only open on some accounts: say which.
        out.append(p.limit_reset == true ? mono("⟲", 11, .bold, .systemTeal)
                                         : mono(" ", 11))
        return out
    }

    /// One fixed-width cell, blank when the account reports no such limit, so
    /// a missing model quota leaves a gap instead of shifting the row.
    private func append(slot limit: Limit?, to out: NSMutableAttributedString) {
        guard let limit else {
            out.append(mono(String(repeating: " ", count: 12), 11))
            return
        }
        out.append(mono(pad(limit.short_label, 6), 11, .regular, .secondaryLabelColor))
        out.append(mono(padLeft("\(limit.percent)%", 5) + " ", 11, .medium, color(limit.level)))
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
            let (code, _) = run("/usr/bin/env", ["python3", helper, "--switch", name])
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

    @objc func setMode(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        inBackground {
            run("/usr/bin/env", ["python3", helper, "--mode", name])
            DispatchQueue.main.async { self.reload() }
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
