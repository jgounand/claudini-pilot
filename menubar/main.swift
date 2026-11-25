// ClaudiniBar — macOS menu bar: what's left on each Claude subscription, and
// one-click switching between them.

import AppKit
import SwiftUI
import WidgetKit

final class Bar: NSObject {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var snapshot: Snapshot?
    var loadedAt = Date.distantPast
    var busy = false { didSet { model.busy = busy } }
    var timer: Timer?
    let model = PanelModel()
    let popover = NSPopover()
    var settingsWindow: NSWindow?
    var dashboardWindow: NSWindow?
    let dashboard = DashboardModel()

    /// `live: false` builds the panel and nothing else — no engine, no timer,
    /// no requests carried out — for drawing it into a picture.
    init(live: Bool = true) {
        super.init()
        // Start on the right, near the clock. On a notched laptop macOS hides
        // whatever does not fit left of the notch, newest items first, so the
        // default spot is the first to disappear. Registered as a default, so
        // if you Cmd-drag the item somewhere else, that position wins.
        UserDefaults.standard.register(defaults: ["NSStatusItem Preferred Position ClaudiniBar": 260])
        item.autosaveName = "ClaudiniBar"
        item.button?.title = "◌"
        wire()
        guard live else { return }
        // A click opens the panel; a right-click, the few things you might want
        // without it.
        item.button?.target = self
        item.button?.action = #selector(clicked(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        popover.behavior = .transient
        popover.animates = false
        let host = NSHostingController(rootView: PanelView(model: model))
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host
        reload()
        schedule(defaultPoll)
        watchRequests()
        watchProfiles()
    }

    /// What the panel's and the settings window's controls do.
    func wire() {
        model.onSwitch = { [weak self] in self?.switchTo($0) }
        model.onRotation = { [weak self] in self?.setInRotation($0, $1) }
        model.onReconnect = { [weak self] in self?.reconnect($0) }
        model.onRefresh = { [weak self] in self?.reload(force: true) }
        model.onAuto = { [weak self] on in self?.command(["--auto", on ? "on" : "off", "--json"]) }
        model.onMode = { [weak self] in self?.command(["--mode", $0, "--json"]) }
        model.onDrain = { [weak self] on in self?.command(["--drain", on ? "on" : "off", "--json"]) }
        model.onSet = { [weak self] key, value in self?.command(["--set", key, String(value), "--json"]) }
        model.onRename = { [weak self] in self?.rename($0, $1) }
        model.onRemove = { [weak self] in self?.remove($0) }
        model.onAdd = { [weak self] in self?.addAccount() }
        model.onHistory = { [weak self] in self?.showDashboard() }
        dashboard.onRefresh = { [weak self] in self?.loadDashboard() }
        model.onSettings = { [weak self] in self?.showSettings() }
        model.onQuit = { NSApp.terminate(nil) }
        model.onMenuBarStyle = { [weak self] in self?.paint() }
    }

    @objc func clicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            showQuickMenu()
        } else {
            togglePanel()
        }
    }

    func togglePanel() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        guard let button = item.button else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        // The panel is drawn from the snapshot we already have; only ask the
        // engine again when that snapshot has actually gone stale — the poll
        // period, not the cache TTL, or every opening would sweep the API.
        let due = TimeInterval(snapshot?.poll_after_sec ?? Int(defaultPoll))
        if Date().timeIntervalSince(loadedAt) > due { reload() }
    }

    func closePanel() {
        if popover.isShown { popover.performClose(nil) }
    }

    func showQuickMenu() {
        let menu = NSMenu()
        for (title, selector, key) in [("Refresh now", #selector(refreshNow), "r"),
                                       ("Dashboard…", #selector(openDashboard), "d"),
                                       ("Settings…", #selector(openSettings), ","),
                                       ("History page (browser)…", #selector(openHistory), "")] {
            let entry = NSMenuItem(title: title, action: selector, keyEquivalent: key)
            entry.target = self
            menu.addItem(entry)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit claudini-pilot", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item.menu = menu
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc func refreshNow() { reload(force: true) }
    @objc func openSettings() { showSettings() }
    @objc func openHistory() { showHistory() }
    @objc func openDashboard() { showDashboard() }

    /// The dashboard window, made once; its figures are read again each time
    /// it is brought forward.
    func showDashboard() {
        closePanel()
        if dashboardWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: DashboardView(model: dashboard)))
            window.title = "claudini-pilot Dashboard"
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.setContentSize(NSSize(width: dashboardWidth, height: 760))
            window.isReleasedWhenClosed = false
            window.center()
            dashboardWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        dashboardWindow?.makeKeyAndOrderFront(nil)
        loadDashboard()
    }

    func loadDashboard() {
        dashboard.loading = true
        engineQueue.async {
            let (_, data) = engine(["--dashboard-json"])
            let decoded = try? JSONDecoder().decode(DashboardData.self, from: data)
            DispatchQueue.main.async {
                self.dashboard.loading = false
                self.dashboard.failed = decoded == nil && self.dashboard.data == nil
                if let decoded { self.dashboard.data = decoded }
            }
        }
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
        model.snapshot = snap
        model.loadedAt = loadedAt
        publish(data, snap)
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
        let style = MenuBarStyle(rawValue: UserDefaults.standard.string(forKey: "menuBar.style") ?? "")
            ?? .percent
        let title = NSMutableAttributedString()
        if style != .ring {
            if let binding = active.binding {
                title.append(digits(" \(binding.label) \(binding.percent)%", 12, shade))
            } else {
                title.append(text(" ?", 12))
            }
        }
        if style == .name {
            title.append(text(" " + active.name, 12, .regular, .secondaryLabelColor))
        }
        // The spent-model marker only means something while the policy is
        // protecting that model; in endurance mode it is just noise.
        if snap.show_saturated, !active.saturated.isEmpty {
            title.append(text(" " + active.saturated.map { String($0.prefix(1)) }.joined(),
                              11, .bold, .systemRed))
        }
        item.button?.attributedTitle = title
    }

    // MARK: actions

    /// The OAuth login needs a real terminal, so open one.
    func reconnect(_ name: String) {
        inTerminal("claudini-usage --reconnect \(name)")
    }

    /// Run a command in a new Terminal window. Only ever given a profile name
    /// that has passed `validName`, so nothing in it means anything to a shell.
    func inTerminal(_ command: String) {
        inBackground {
            run("/usr/bin/osascript", ["-e",
                "tell application \"Terminal\" to do script \"\(command)\"",
                "-e", "tell application \"Terminal\" to activate"])
        }
    }

    /// The engine's rule for a profile name, checked here too so a bad one is
    /// caught in the dialog rather than in a Terminal window.
    func validName(_ name: String) -> Bool {
        name.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", options: .regularExpression) != nil
    }

    /// Name the account, then log in to it in Terminal. The login happens in a
    /// throwaway Claude Code config: the account in use and open sessions are
    /// not touched, and nothing switches.
    func addAccount() {
        closePanel()
        let alert = NSAlert()
        alert.messageText = "Add a Claude account"
        alert.informativeText = "Give it a name, then log in with that account in the Terminal "
            + "window that opens. The account you are using now, and your open sessions, "
            + "are not touched."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.placeholderString = "e.g. team-seat"
        alert.accessoryView = field
        alert.addButton(withTitle: "Continue")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
        let taken = snapshot?.profiles.contains { $0.name == name } ?? false
        guard validName(name), !taken else {
            let problem = NSAlert()
            problem.messageText = taken ? "There is already an account named \(name)"
                                        : "That name can't be used"
            problem.informativeText = "Up to 40 letters, digits, '.', '_' or '-', "
                + "starting with a letter or a digit."
            problem.runModal()
            return
        }
        inTerminal("claudini-usage --add \(name)")
    }

    /// A profile added or removed from a terminal shows up without waiting for
    /// the next poll. The folder changes when a profile's own folder appears,
    /// a moment before its file is written — hence the short wait.
    private var profilesWatch: DispatchSourceFileSystemObject?

    func watchProfiles() {
        let folder = NSString(string: "~/.claudini/profiles").expandingTildeInPath
        let descriptor = open(folder, O_EVTONLY)
        guard descriptor >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor, eventMask: .write, queue: .main)
        source.setEventHandler { [weak self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.reload() }
        }
        source.setCancelHandler { close(descriptor) }
        source.resume()
        profilesWatch = source
    }

    func switchTo(_ name: String) {
        engineQueue.async {
            let code = engine(["--switch", name]).0
            DispatchQueue.main.async {
                if code == 0 {
                    self.model.show("Now using \(name). Open claude sessions keep their account until relaunched.")
                } else {
                    self.model.show("Could not switch to \(name): try claudini-usage --switch \(name)", error: true)
                }
                self.reload()
            }
        }
    }

    /// A setting or an account change: queued behind whatever is running —
    /// unlike a poll, it is something you asked for — and the engine answers
    /// with the snapshot that reflects it.
    func command(_ args: [String]) {
        engineQueue.async {
            let (_, data) = engine(args)
            DispatchQueue.main.async { self.apply(data) }
        }
    }

    func rename(_ old: String, _ new: String) {
        engineQueue.async {
            let code = engine(["--rename", old, new]).0
            DispatchQueue.main.async {
                if code != 0 { self.model.show("Could not rename \(old)", error: true) }
                self.reload()
            }
        }
    }

    func remove(_ name: String) {
        engineQueue.async {
            let code = engine(["--remove", name]).0
            DispatchQueue.main.async {
                self.model.show(code == 0 ? "\(name) removed" : "Could not remove \(name)", error: code != 0)
                self.reload()
            }
        }
    }

    /// The settings window, made once and brought forward after that.
    func showSettings() {
        closePanel()
        if settingsWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: SettingsView(model: model)))
            window.title = "claudini-pilot Settings"
            window.styleMask = [.titled, .closable, .miniaturizable]
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    /// Put an account in or out of the rotation. Queued rather than dropped
    /// while a poll is running — unlike a poll, this is something you asked
    /// for — and the open menu is redrawn from the answer, so "Next session"
    /// and the order reflect the change without closing it.
    func setInRotation(_ name: String, _ on: Bool) {
        engineQueue.async {
            let (_, data) = engine([on ? "--on" : "--off", name, "--json"])
            DispatchQueue.main.async { self.apply(data) }
        }
    }

    // MARK: widget

    private var widgetFingerprint = ""
    private var widgetReloadedAt = Date.distantPast
    private var requestWatch: DispatchSourceFileSystemObject?

    /// Leave the snapshot for the widget, and have it redrawn when that shows.
    ///
    /// WidgetKit rations the reloads of an app that is never in front, and
    /// spending one every five-minute poll would see them refused by the
    /// afternoon. So the widget is reloaded when something it draws has
    /// visibly moved — an account, a switch, a status, or a bar by 3 points —
    /// and at least every half hour. Its own entries keep the countdowns
    /// moving in between.
    func publish(_ data: Data, _ snap: Snapshot) {
        guard SharedStore.folder != nil else { return }
        SharedStore.write(data)
        let fingerprint = snap.profiles.map {
            "\($0.name) \($0.active) \($0.disabled) \($0.status) \(($0.binding?.percent ?? -3) / 3)"
        }.joined(separator: ",") + "|\(snap.next.name ?? "")|\(snap.auto)|\(snap.mode)"
        guard fingerprint != widgetFingerprint
                || Date().timeIntervalSince(widgetReloadedAt) > 30 * 60 else { return }
        widgetFingerprint = fingerprint
        widgetReloadedAt = Date()
        WidgetCenter.shared.reloadAllTimelines()
    }

    /// Carry out what the widget's buttons ask for. The folder is watched
    /// rather than polled, and read once at launch for anything asked while
    /// the app was not running.
    func watchRequests() {
        guard let folder = SharedStore.requestsFolder else { return }
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let descriptor = open(folder.path, O_EVTONLY)
        guard descriptor >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor, eventMask: .write, queue: .main)
        source.setEventHandler { [weak self] in self?.handleRequests() }
        source.setCancelHandler { close(descriptor) }
        source.resume()
        requestWatch = source
        handleRequests()
    }

    func handleRequests() {
        for request in Request.drain() {
            switch request.kind {
            case .refresh:
                reload(force: true)
            case .on, .off:
                // The engine refuses a name it does not know; a leading dash
                // would be read as one of its options instead.
                guard let name = request.name, !name.isEmpty, !name.hasPrefix("-") else { continue }
                setInRotation(name, request.kind == .on)
            }
        }
    }

    /// The engine writes the page and opens it; it knows where it keeps the
    /// history, and the app has no business guessing.
    func showHistory() {
        closePanel()
        inBackground { _ = engine(["--history"]) }
    }

    @objc func quitApp() { NSApp.terminate(nil) }
}

/// Opening the app again from Finder or Launchpad shows its menu, rather
/// than doing nothing visible.
final class Delegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        // After the activation that brought us here has settled: opened in
        // the same turn, the menu is dismissed again as the app comes forward.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            bar.togglePanel()
        }
        return false
    }
}

/// `ClaudiniBar --widget-previews DIR [SNAPSHOT.json]`: draw the widget at
/// every size, light and dark, and exit. From the engine's current answer, or
/// from a saved one — which is how the README's picture shows made-up
/// accounts rather than yours. Layouts can be checked without placing a widget.
func renderWidgetPreviews(into folder: URL, from file: URL?) {
    let data = file.flatMap { try? Data(contentsOf: $0) } ?? engine(["--json"]).1
    guard let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
        FileHandle.standardError.write("the engine gave no snapshot\n".data(using: .utf8)!)
        exit(1)
    }
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let board = Board(snapshot: snapshot, writtenAt: Date(), now: Date(), followed: nil, showOff: true)
    MainActor.assumeIsolated {
        for size in WidgetSize.allCases {
            for scheme in [ColorScheme.light, .dark] {
                let card = WidgetContent(board: board, size: size)
                    .padding(16)
                    .frame(width: size.points.width, height: size.points.height)
                    .background(scheme == .dark ? Color(white: 0.16) : Color(white: 0.97))
                    .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .environment(\.colorScheme, scheme)
                let renderer = ImageRenderer(content: card)
                renderer.scale = 2
                guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
                      let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:])
                else { continue }
                let name = "\(size)-\(scheme == .dark ? "dark" : "light").png"
                try? png.write(to: folder.appendingPathComponent(name))
                print(folder.appendingPathComponent(name).path)
            }
        }
    }
    exit(0)
}

/// `ClaudiniBar --menu-preview DIR [SNAPSHOT.json]`: draw the panel, light and
/// dark, from the same views the real one is built from. For the README, fed a
/// made-up snapshot, so no real account appears in a picture.
func renderMenuPreview(into folder: URL, from file: URL?) {
    let data = file.flatMap { try? Data(contentsOf: $0) } ?? engine(["--json"]).1
    guard let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { exit(1) }
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let model = PanelModel()
    model.preview = true
    model.snapshot = snapshot
    model.loadedAt = Date()
    var both: [NSImage] = []
    MainActor.assumeIsolated {
        for scheme in [ColorScheme.light, .dark] {
            let panel = PanelView(model: model)
                .background(scheme == .dark ? Color(white: 0.17) : Color(white: 0.965))
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .environment(\.colorScheme, scheme)
            let renderer = ImageRenderer(content: panel)
            renderer.scale = 2
            guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
                  let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:])
            else { continue }
            let path = folder.appendingPathComponent("menu-\(scheme == .dark ? "dark" : "light").png")
            try? png.write(to: path)
            print(path.path)
            both.append(image)
        }
    }
    // Light and dark side by side: the README's picture.
    if both.count == 2, let png = sideBySide(both, gap: 24) {
        let path = folder.appendingPathComponent("menu.png")
        try? png.write(to: path)
        print(path.path)
    }
    exit(0)
}

/// Images next to each other on a transparent background, as PNG data.
func sideBySide(_ images: [NSImage], gap: CGFloat) -> Data? {
    let width = images.map(\.size.width).reduce(0, +) + gap * CGFloat(images.count - 1)
    let height = images.map(\.size.height).max() ?? 0
    guard let canvas = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(width * 2),
                                        pixelsHigh: Int(height * 2), bitsPerSample: 8, samplesPerPixel: 4,
                                        hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                        bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
    canvas.size = NSSize(width: width, height: height)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: canvas)
    var x: CGFloat = 0
    for image in images {
        image.draw(in: NSRect(x: x, y: height - image.size.height, width: image.size.width,
                              height: image.size.height))
        x += image.size.width + gap
    }
    NSGraphicsContext.restoreGraphicsState()
    return canvas.representation(using: .png, properties: [:])
}

/// `ClaudiniBar --dashboard-preview DIR [DASHBOARD.json]`: draw the dashboard,
/// light and dark, and exit.
func renderDashboardPreview(into folder: URL, from file: URL?) {
    let data = file.flatMap { try? Data(contentsOf: $0) } ?? engine(["--dashboard-json"]).1
    guard let decoded = try? JSONDecoder().decode(DashboardData.self, from: data) else {
        FileHandle.standardError.write("the engine gave no dashboard\n".data(using: .utf8)!)
        exit(1)
    }
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let model = DashboardModel()
    model.preview = true
    model.data = decoded
    MainActor.assumeIsolated {
        for scheme in [ColorScheme.light, .dark] {
            let view = DashboardView(model: model)
                .background(scheme == .dark ? Color(white: 0.13) : Color(white: 0.98))
                .environment(\.colorScheme, scheme)
            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
                  let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:])
            else { continue }
            let path = folder.appendingPathComponent("dashboard-\(scheme == .dark ? "dark" : "light").png")
            try? png.write(to: path)
            print(path.path)
        }
    }
    exit(0)
}

if let flag = CommandLine.arguments.firstIndex(of: "--dashboard-preview"),
   flag + 1 < CommandLine.arguments.count {
    let arguments = CommandLine.arguments
    renderDashboardPreview(into: URL(fileURLWithPath: arguments[flag + 1]),
                           from: flag + 2 < arguments.count ? URL(fileURLWithPath: arguments[flag + 2]) : nil)
}

if let flag = CommandLine.arguments.firstIndex(of: "--menu-preview"),
   flag + 1 < CommandLine.arguments.count {
    let arguments = CommandLine.arguments
    renderMenuPreview(into: URL(fileURLWithPath: arguments[flag + 1]),
                      from: flag + 2 < arguments.count ? URL(fileURLWithPath: arguments[flag + 2]) : nil)
}

if let flag = CommandLine.arguments.firstIndex(of: "--widget-previews"),
   flag + 1 < CommandLine.arguments.count {
    let arguments = CommandLine.arguments
    renderWidgetPreviews(into: URL(fileURLWithPath: arguments[flag + 1]),
                         from: flag + 2 < arguments.count
                             ? URL(fileURLWithPath: arguments[flag + 2]) : nil)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let bar = Bar()
let delegate = Delegate()
app.delegate = delegate
app.run()
