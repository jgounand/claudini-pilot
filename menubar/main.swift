// ClaudiniBar — macOS menu bar: what's left on each Claude subscription, and
// one-click switching between them.

import AppKit

final class Bar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var snapshot: Snapshot?
    var loadedAt = Date.distantPast
    var busy = false
    var timer: Timer?
    var menuOpen = false

    override init() {
        super.init()
        // Start on the right, near the clock. On a notched laptop macOS hides
        // whatever does not fit left of the notch, newest items first, so the
        // default spot is the first to disappear. Registered as a default, so
        // if you Cmd-drag the item somewhere else, that position wins.
        UserDefaults.standard.register(defaults: ["NSStatusItem Preferred Position ClaudiniBar": 260])
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
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) {
            [weak self] _ in self?.reload()
        }
    }

    func reload(force: Bool = false) {
        ask(["--json", "--tick"] + (force ? ["--force"] : []))
    }

    /// Run one engine call and adopt whatever snapshot comes back.
    func ask(_ args: [String]) {
        guard !busy else { return }
        busy = true
        engineQueue.async {
            let (_, data) = engine(args)
            DispatchQueue.main.async {
                self.busy = false
                self.apply(data)
            }
        }
    }

    /// Adopt a snapshot the engine just produced, whatever produced it.
    func apply(_ data: Data) {
        guard let snap = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            paint()
            return
        }
        snapshot = snap
        loadedAt = Date()
        let wanted = TimeInterval(snap.poll_after_sec)
        if timer?.timeInterval != wanted { schedule(wanted) }
        paint()
    }

    /// Run something slow without freezing the status item, on the same serial
    /// queue as everything else that touches the engine.
    func inBackground(_ work: @escaping () -> Void) {
        engineQueue.async(execute: work)
    }

    func paint() {
        guard let snap = snapshot, let active = snap.profiles.first(where: { $0.active }) else {
            item.button?.image = nil
            item.button?.title = "⚠︎"
            return
        }
        let shade = active.binding.map { color($0.level) } ?? NSColor.labelColor
        item.button?.image = active.binding.map { gauge($0.percent, shade) }
        item.button?.imagePosition = .imageLeading

        // Only the window that binds and how full it is. On a notched laptop the
        // menu bar is half as wide and macOS silently hides whatever does not
        // fit — newest items first — so the account name, which the menu shows
        // anyway, cost the whole item its place. It was the widest one there.
        //
        // The figure is what has been *used*, like every row in the menu and
        // like the ring beside it.
        let title = NSMutableAttributedString()
        if let binding = active.binding {
            title.append(digits(" \(binding.label) \(binding.percent)%", 12, shade))
        } else {
            title.append(text(" ?", 12))
        }
        // The spent-model marker only means something while the policy is
        // protecting that model; in endurance mode it is just noise.
        if snap.show_saturated, !active.saturated.isEmpty {
            title.append(text(" " + active.saturated.map { String($0.prefix(1)) }.joined(),
                              11, .bold, .systemRed))
        }
        item.button?.attributedTitle = title
    }

    // MARK: menu

    func menuDidClose(_ menu: NSMenu) { menuOpen = false }

    func menuWillOpen(_ menu: NSMenu) {
        menuOpen = true
        rebuild(menu)
        // The open menu is drawn from the snapshot we already have; only ask
        // the engine again when that snapshot has actually gone stale.
        // The poll period, not the cache TTL: the snapshot is briefly older
        // than the TTL at the end of every cycle, and refetching then buys
        // seconds of freshness for a whole extra sweep of the API.
        let due = TimeInterval(snapshot?.poll_after_sec ?? Int(defaultPoll))
        if Date().timeIntervalSince(loadedAt) > due { reload() }
    }

    func rebuild(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let snap = snapshot else {
            menu.addItem(withTitle: "Loading…", action: nil, keyEquivalent: "")
            return
        }
        let active = snap.profiles.first { $0.active }

        menu.addItem(row(HeaderView(subtitle: snap.fleet,
                                    status: snap.auto ? "Auto · \(snap.mode)" : "Auto off")))
        menu.addItem(.separator())

        // The account in use, drawn the way Claude draws its own limits.
        if let active {
            menu.addItem(row(SectionView("Using now")))
            let title = AccountTitleView(active)
            title.addSwitch(on: !active.disabled, y: 6) { [weak self] on in
                self?.setInRotation(active.name, on)
            }
            menu.addItem(row(title))
            if active.limits.isEmpty {
                menu.addItem(row(StatusView(active)))
            } else {
                active.limits.forEach { menu.addItem(row(LimitView($0))) }
            }
            menu.addItem(.separator())
        }

        menu.addItem(row(SectionView("Next session")))
        menu.addItem(row(NextView(snap.next)))
        menu.addItem(.separator())

        let others = snap.profiles.filter { !$0.active }
        if !others.isEmpty {
            menu.addItem(row(SectionView("Other accounts · best first")))
            for profile in others {
                let line = AccountRowView(profile)
                line.addSwitch(on: !profile.disabled, y: 6) { [weak self] on in
                    self?.setInRotation(profile.name, on)
                }
                // A switched-off account is not somewhere to go: switch it on
                // first, or the next automatic tick would move you straight
                // back off it.
                if !profile.disabled {
                    line.onClick = { [weak self] in
                        if profile.needs_login { self?.reconnect(profile.name) }
                        else { self?.switchTo(profile.name) }
                    }
                }
                menu.addItem(row(line))
            }
            menu.addItem(.separator())
        }

        if let notice = snap.throttle_notice {
            menu.addItem(row(NoteView(notice)))
        }
        let refresh = ActionRowView(title: "Refresh", badge: updatedAgo(loadedAt))
        refresh.onClick = { [weak self] in self?.reload(force: true) }
        menu.addItem(row(refresh))

        let auto = ActionRowView(title: "Auto-switching", badge: snap.auto ? "On" : "Off")
        auto.onClick = { [weak self] in self?.toggleAuto() }
        menu.addItem(row(auto))

        // Two goals, two rankings: protect the preferred model, or spend the
        // allowance that resets soonest so nothing expires unused.
        let modes = NSMenuItem(title: "Mode", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        for mode in snap.modes {
            let mi = NSMenuItem(title: mode.title, action: #selector(setMode(_:)),
                                keyEquivalent: "")
            mi.target = self
            mi.representedObject = mode.name
            mi.state = mode.name == snap.mode ? .on : .off
            submenu.addItem(mi)
        }
        modes.submenu = submenu
        menu.addItem(modes)

        let history = NSMenuItem(title: "History…", action: #selector(showHistory),
                                 keyEquivalent: "")
        history.target = self
        menu.addItem(history)

        // What the tool can do outside this menu. Clicking copies the command,
        // which is the only thing anyone wants from a list like this.
        let commands = NSMenuItem(title: "Command line", action: nil, keyEquivalent: "")
        let list = NSMenu()
        for action in snap.actions {
            let mi = NSMenuItem(title: action.command, action: #selector(copyCommand(_:)),
                                keyEquivalent: "")
            mi.target = self
            mi.representedObject = action.command
            let line = NSMutableAttributedString(attributedString:
                mono(pad("claudini-usage " + action.command, 30), 12))
            line.append(mono(" " + action.about, 11, .regular, .secondaryLabelColor))
            mi.attributedTitle = line
            list.addItem(mi)
        }
        commands.submenu = list
        menu.addItem(commands)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit claudini-pilot", action: #selector(quitApp),
                              keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    func row(_ view: NSView) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.view = view
        return item
    }

    // MARK: actions

    /// The OAuth login needs a real terminal, so open one.
    func reconnect(_ name: String) {
        let cmd = "claudini-usage --reconnect \(name)"
        inBackground {
            run("/usr/bin/osascript", ["-e",
                "tell application \"Terminal\" to do script \"\(cmd)\"",
                "-e", "tell application \"Terminal\" to activate"])
        }
    }

    func switchTo(_ name: String) {
        inBackground {
            let code = engine(["--switch", name]).0
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

    /// Put an account in or out of the rotation. Queued rather than dropped
    /// while a poll is running — unlike a poll, this is something you asked
    /// for — and the open menu is redrawn from the answer, so "Next session"
    /// and the order reflect the change without closing it.
    func setInRotation(_ name: String, _ on: Bool) {
        engineQueue.async {
            let (_, data) = engine([on ? "--on" : "--off", name, "--json"])
            DispatchQueue.main.async {
                self.apply(data)
                if self.menuOpen, let menu = self.item.menu { self.rebuild(menu) }
            }
        }
    }

    @objc func setMode(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        // The engine answers a setting change with the new snapshot, so one
        // interpreter start does both jobs.
        ask(["--mode", name, "--json"])
    }

    func toggleAuto() {
        let wanted = !(snapshot?.auto ?? false)
        ask(["--auto", wanted ? "on" : "off", "--json"])
    }

    /// The engine writes the page and opens it; it knows where it keeps the
    /// history, and the app has no business guessing.
    @objc func showHistory() {
        inBackground { _ = engine(["--history"]) }
    }

    @objc func copyCommand(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("claudini-usage " + command, forType: .string)
    }

    @objc func quitApp() { NSApp.terminate(nil) }
}

/// Opening the app again from Finder or Launchpad shows its menu, rather
/// than doing nothing visible.
final class Delegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        bar.item.button?.performClick(nil)
        return false
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let bar = Bar()
let delegate = Delegate()
app.delegate = delegate
app.run()
