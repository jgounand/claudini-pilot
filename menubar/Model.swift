// ClaudiniBar — the engine's contract and the helpers every part of the app
// shares. All the policy lives in the Python engine; this side only decodes
// what it decided and draws it.
//
// Build: swiftc -O -o ClaudiniBar menubar/*.swift -framework AppKit

import AppKit

let helper = NSString(string: "~/.claudini/tools/claudini_usage.py").expandingTildeInPath

/// Fallback poll period, used only until the first snapshot arrives: after
/// that the engine tells us its own cadence and staleness window.
let defaultPoll: TimeInterval = 300

// MARK: - the engine's contract

struct Limit: Decodable {
    let short_label: String
    /// "5-hour limit", "Weekly · all models" — worded like Claude's own menu.
    let title: String
    let percent: Int
    let level: String
    /// Absolute, so the countdown stays right even drawn from a stale snapshot.
    let resets_at_epoch: Int?
    /// Which limits are the general windows, and which one is the preferred
    /// model, is the engine's call — the app only lays them out.
    let general: Bool
    let preferred: Bool
}

struct Profile: Decodable {
    let name: String
    let email: String?
    let active: Bool
    let status: String
    let detail: String
    let limits: [Limit]
    let limit_reset: Bool?
    let needs_login: Bool
    let space: String?
    let plan_label: String?
    let binding: Binding?
    let saturated: [String]
}

/// The general window that will stop you first — which one it is changes
/// through the day, so the engine names it rather than the app assuming.
struct Binding: Decodable {
    let label: String
    let percent: Int
    let level: String
    let resets_at_epoch: Int?
}

/// What the next `claude` session gets, and what stops us moving there.
struct NextUp: Decodable {
    let name: String?
    let reason: String
    let blocked_by: String?
    let staying: Bool
    /// Where you would go if this account ran out — so the line says something
    /// even when nothing is about to change.
    let after: String?
}

struct Mode: Decodable {
    let name: String
    let title: String
}

/// The command line, described by the engine rather than retyped here — the
/// list would otherwise drift the first time a command changed.
struct Action: Decodable {
    let command: String
    let about: String
}

struct Snapshot: Decodable {
    let profiles: [Profile]
    let auto: Bool
    /// One line on whether the fleet as a whole is heading for an outage.
    let fleet: String
    let actions: [Action]
    let mode: String
    let modes: [Mode]
    /// A spent model quota only means something while the policy protects it.
    let show_saturated: Bool
    let throttle_notice: String?
    let poll_after_sec: Int
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

func styled(_ s: String, _ font: NSFont, _ color: NSColor) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [.font: font, .foregroundColor: color])
}

func text(_ s: String, _ size: CGFloat,
          _ weight: NSFont.Weight = .regular,
          _ color: NSColor = .labelColor) -> NSAttributedString {
    styled(s, .systemFont(ofSize: size, weight: weight), color)
}

/// Monospaced, so every row's columns land in the same place. The dropdown is
/// a table; letting proportional text shuffle the numbers is what made it
/// unreadable at a glance.
func mono(_ s: String, _ size: CGFloat,
          _ weight: NSFont.Weight = .regular,
          _ color: NSColor = .labelColor) -> NSAttributedString {
    styled(s, .monospacedSystemFont(ofSize: size, weight: weight), color)
}

func pad(_ s: String, _ width: Int) -> String {
    s.count >= width ? String(s.prefix(width))
                     : s + String(repeating: " ", count: width - s.count)
}

func digits(_ s: String, _ size: CGFloat, _ color: NSColor) -> NSAttributedString {
    styled(s, .monospacedDigitSystemFont(ofSize: size, weight: .regular), color)
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

/// A ring filled to `percent`, for the menu bar. Reads at a glance at a size
/// where three of them would be unreadable.
func gauge(_ percent: Int, _ shade: NSColor) -> NSImage {
    let size: CGFloat = 13
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

/// Every call into the engine goes through here, so the interpreter and the
/// helper path are spelled once.
func engine(_ args: [String]) -> (Int32, Data) {
    run("/usr/bin/env", ["python3", helper] + args)
}

/// One engine process at a time, for the whole app.
///
/// Each of these can switch accounts or rewrite settings, and letting two run
/// at once is how a click lands in the middle of an automatic tick — the
/// interleaving that can overwrite one account's stored credentials with
/// another's. A serial queue costs nothing here: none of it is fast enough to
/// want overlapping anyway.
let engineQueue = DispatchQueue(label: "claudini-pilot.engine")
