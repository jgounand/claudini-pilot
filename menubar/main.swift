// ClaudiniBar — menu bar macOS : conso de chaque abonnement Claude + bascule en 1 clic.
// Compile : swiftc -O -o ClaudiniBar main.swift -framework AppKit

import AppKit

let helper = NSString(string: "~/.claudini/tools/claudini_usage.py").expandingTildeInPath

struct Limit: Decodable {
    let kind: String?
    let label: String
    let percent: Int
    let severity: String?
    let resets_at: String?
}

struct Profile: Decodable {
    let name: String
    let email: String?
    let org: String?
    let active: Bool
    let status: String
    let limits: [Limit]
}

struct Snapshot: Decodable {
    let active: String?
    let profiles: [Profile]
    let auto: Bool
    let suggestion: String?
}

// MARK: - shell

@discardableResult
func run(_ launchPath: String, _ args: [String]) -> (Int32, Data) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: launchPath)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    // L'app n'hérite pas forcément du PATH du shell : on l'étoffe pour `claudini`.
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = (env["PATH"] ?? "") + ":/usr/local/bin:/opt/homebrew/bin"
    p.environment = env
    do { try p.run() } catch { return (-1, Data()) }
    let out = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, out)
}

// MARK: - formatage

func countdown(_ iso: String?) -> String {
    guard let iso else { return "" }
    // Python écrit 6 décimales de secondes, ISO8601DateFormatter n'en accepte que 3 :
    // on retire la partie fractionnaire avant de parser.
    let trimmed = iso.replacingOccurrences(of: "\\.[0-9]+", with: "",
                                           options: .regularExpression)
    guard let date = ISO8601DateFormatter().date(from: trimmed) else { return "" }
    let mins = Int(date.timeIntervalSinceNow / 60)
    if mins <= 0 { return "maintenant" }
    if mins < 60 { return "\(mins)min" }
    if mins < 1440 { return String(format: "%dh%02d", mins / 60, mins % 60) }
    return "\(mins / 1440)j"
}

func color(_ pct: Int) -> NSColor {
    if pct >= 95 { return .systemRed }
    if pct >= 75 { return .systemOrange }
    return .systemGreen
}

/// Marge sur les limites générales (session 5h + semaine), hors quotas par modèle.
func generalHeadroom(_ p: Profile) -> Int {
    let general = p.limits.filter { $0.kind == "session" || $0.kind == "weekly_all" }
    guard p.status == "ok", !general.isEmpty else { return -1 }
    return 100 - (general.map(\.percent).max() ?? 100)
}

/// Marge réelle toutes limites confondues — c'est elle qui décide du meilleur profil.
func headroom(_ p: Profile) -> Int {
    guard p.status == "ok", !p.limits.isEmpty else { return -1 }
    return 100 - (p.limits.map(\.percent).max() ?? 100)
}

/// Modèles dont le quota hebdo dédié est épuisé (Fable, Opus…).
func saturatedModels(_ p: Profile) -> [String] {
    p.limits.filter { $0.kind != "session" && $0.kind != "weekly_all" && $0.percent >= 100 }
            .map(\.label)
}

// MARK: - app

final class Bar: NSObject, NSMenuDelegate {
    let item = NSStatusItem.autosaveName("ClaudiniBar")
    var snapshot: Snapshot?
    var loading = false

    override init() {
        super.init()
        item.button?.title = "◌"
        let menu = NSMenu()
        menu.delegate = self
        // On gère nous-mêmes l'état des lignes : le profil actif reste inerte.
        menu.autoenablesItems = false
        item.menu = menu
        reload()
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in self.reload() }
    }

    func reload() {
        guard !loading else { return }
        loading = true
        DispatchQueue.global(qos: .utility).async {
            let (_, data) = run("/usr/bin/env", ["python3", helper, "--json", "--tick"])
            let snap = try? JSONDecoder().decode(Snapshot.self, from: data)
            DispatchQueue.main.async {
                self.loading = false
                if let snap { self.snapshot = snap }
                self.paint()
            }
        }
    }

    func paint() {
        guard let snap = snapshot, let active = snap.profiles.first(where: { $0.active }) else {
            item.button?.title = "⚠︎"
            return
        }
        let left = generalHeadroom(active)
        let attr = NSMutableAttributedString(string: active.name + " ", attributes: [
            .font: NSFont.systemFont(ofSize: 12, weight: .medium),
        ])
        attr.append(NSAttributedString(string: left < 0 ? "?" : "\(left)%", attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
            .foregroundColor: left < 0 ? NSColor.labelColor : color(100 - left),
        ]))
        // Quota modèle épuisé : on le signale par son initiale, sinon l'info se perd.
        let dead = saturatedModels(active).map { String($0.prefix(1)) }
        if !dead.isEmpty {
            attr.append(NSAttributedString(string: " " + dead.joined(), attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .bold),
                .foregroundColor: NSColor.systemRed,
            ]))
        }
        if snap.auto {
            attr.append(NSAttributedString(string: " ⟳", attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .bold),
                .foregroundColor: NSColor.controlAccentColor,
            ]))
        }
        item.button?.attributedTitle = attr
    }

    // MARK: menu

    func menuWillOpen(_ menu: NSMenu) {
        rebuild(menu)
        reload()
    }

    func rebuild(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let snap = snapshot else {
            menu.addItem(withTitle: "Chargement…", action: nil, keyEquivalent: "")
            return
        }

        // Recommandation : le profil sain qui a le plus de marge.
        let best = snap.profiles.filter { !$0.active && headroom($0) >= 0 }
                                .max { headroom($0) < headroom($1) }
        if let best, headroom(best) > 0 {
            let head = NSMenuItem(title: "Basculer sur \(best.name) — \(headroom(best))% de marge",
                                  action: #selector(switchTo(_:)), keyEquivalent: "")
            head.target = self
            head.representedObject = best.name
            head.isEnabled = true
            head.attributedTitle = NSAttributedString(string: head.title, attributes: [
                .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
                .foregroundColor: NSColor.systemGreen,
            ])
            menu.addItem(head)
            menu.addItem(.separator())
        }

        for p in snap.profiles {
            let mi = NSMenuItem(title: p.name, action: #selector(switchTo(_:)), keyEquivalent: "")
            mi.target = self
            mi.representedObject = p.name
            mi.attributedTitle = row(p)
            mi.isEnabled = !p.active
            menu.addItem(mi)
        }

        menu.addItem(.separator())
        let auto = NSMenuItem(title: snap.auto ? "Bascule auto : ACTIVE" : "Bascule auto : inactive",
                              action: #selector(toggleAuto), keyEquivalent: "a")
        auto.target = self
        auto.isEnabled = true
        auto.state = snap.auto ? .on : .off
        menu.addItem(auto)

        let refresh = NSMenuItem(title: "Rafraîchir", action: #selector(refreshNow), keyEquivalent: "r")
        refresh.isEnabled = true
        refresh.target = self
        menu.addItem(refresh)
        let quit = NSMenuItem(title: "Quitter", action: #selector(quit), keyEquivalent: "q")
        quit.isEnabled = true
        quit.target = self
        menu.addItem(quit)
    }

    func row(_ p: Profile) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let mark = p.active ? "●" : "○"
        out.append(NSAttributedString(string: "\(mark) \(p.name)", attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: p.active ? .bold : .regular),
        ]))
        out.append(NSAttributedString(string: "  \(p.email ?? "?")\n", attributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]))

        if p.status != "ok" {
            out.append(NSAttributedString(string: "     \(p.status)", attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.systemRed,
            ]))
            return out
        }

        out.append(NSAttributedString(string: "     ", attributes: [.font: NSFont.systemFont(ofSize: 11)]))
        for (i, l) in p.limits.enumerated() {
            if i > 0 {
                out.append(NSAttributedString(string: " · ", attributes: [
                    .font: NSFont.systemFont(ofSize: 11),
                    .foregroundColor: NSColor.tertiaryLabelColor,
                ]))
            }
            let short = l.kind == "session" ? "5h" : (l.kind == "weekly_all" ? "7j" : l.label)
            out.append(NSAttributedString(string: "\(short) \(l.percent)%", attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular),
                .foregroundColor: color(l.percent),
            ]))
        }
        if let reset = p.limits.first(where: { $0.kind == "session" })?.resets_at {
            out.append(NSAttributedString(string: "  ↻ \(countdown(reset))", attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }
        return out
    }

    // MARK: actions

    @objc func switchTo(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        let (code, _) = run("/usr/bin/env", ["claudini", "use", name])

        let alert = NSAlert()
        if code == 0 {
            alert.messageText = "Profil actif : \(name)"
            alert.informativeText = "Les sessions Claude Code déjà ouvertes gardent l'ancien compte. "
                + "Relance `claude` dans tes terminaux pour utiliser \(name)."
        } else {
            alert.alertStyle = .critical
            alert.messageText = "Bascule impossible"
            alert.informativeText = "`claudini use \(name)` a échoué. Vérifie dans un terminal."
        }
        alert.runModal()
        reload()
    }

    @objc func toggleAuto() {
        let wanted = !(snapshot?.auto ?? false)
        run("/usr/bin/env", ["python3", helper, "--auto", wanted ? "on" : "off"])
        reload()
    }

    @objc func refreshNow() { reload() }
    @objc func quit() { NSApp.terminate(nil) }
}

extension NSStatusItem {
    static func autosaveName(_ name: String) -> NSStatusItem {
        let i = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        i.autosaveName = name
        return i
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let bar = Bar()
app.run()
