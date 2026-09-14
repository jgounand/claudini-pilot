// The menu's rows, drawn rather than built from attributed strings.
//
// The layout follows Claude's own usage menu: a label on the left, its value on
// the right, a full-width bar underneath, and status in small pills. The bar is
// the reason these are views at all — an attributed menu title cannot hold one,
// and it is what makes a limit readable before you have read a single number.

import AppKit

let menuWidth: CGFloat = 330
let inset: CGFloat = 16

/// Bars inside the menu follow Claude's palette: blue while there is room, then
/// orange, then red. The menu bar title keeps its own green, which stays legible
/// against a coloured wallpaper where blue would not.
func barColor(_ level: String) -> NSColor {
    switch level {
    case "critical": return .systemRed
    case "warning": return .systemOrange
    default: return .systemBlue
    }
}

/// Shared drawing for every row: top-down layout, text, bars, pills, and — when
/// the row is clickable — a native-looking highlight.
class MenuRow: NSView {
    var onClick: (() -> Void)?
    private var hovering = false

    init(height: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: menuWidth, height: height))
        autoresizingMask = [.width]
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var isFlipped: Bool { true }

    // MARK: interaction

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        guard onClick != nil else { return }
        addTrackingArea(NSTrackingArea(rect: bounds,
                                       options: [.mouseEnteredAndExited, .activeAlways],
                                       owner: self))
    }

    override func mouseEntered(with event: NSEvent) { hovering = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hovering = false; needsDisplay = true }

    override func mouseUp(with event: NSEvent) {
        guard let onClick else { return }
        // Close the menu first: an alert or a new process started while the
        // menu is still tracking would appear behind it.
        enclosingMenuItem?.menu?.cancelTracking()
        DispatchQueue.main.async(execute: onClick)
    }

    var highlighted: Bool { hovering && onClick != nil }

    // MARK: on/off switch

    private var onToggle: ((Bool) -> Void)?

    /// A small switch at the right edge of the first line. A real control
    /// rather than a drawn one: clicking it leaves the menu open, the way
    /// every switch in a Control Centre panel does. Returns where it starts, so
    /// the text on that line can stop short of it.
    @discardableResult
    func addSwitch(on: Bool, y: CGFloat, _ action: @escaping (Bool) -> Void) -> NSSwitch {
        let toggle = NSSwitch()
        toggle.controlSize = .mini
        toggle.state = on ? .on : .off
        toggle.target = self
        toggle.action = #selector(toggled(_:))
        toggle.sizeToFit()
        toggle.setFrameOrigin(NSPoint(x: bounds.width - inset - toggle.frame.width, y: y))
        toggle.autoresizingMask = [.minXMargin]
        addSubview(toggle)
        onToggle = action
        return toggle
    }

    /// Where the right-aligned text of the first line has to end.
    var lineEnd: CGFloat {
        subviews.compactMap { $0 as? NSSwitch }.first.map { $0.frame.minX - 8 }
            ?? bounds.width - inset
    }

    @objc private func toggled(_ sender: NSSwitch) {
        onToggle?(sender.state == .on)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard highlighted else { return }
        NSColor.controlAccentColor.setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 5, dy: 1), xRadius: 6, yRadius: 6).fill()
    }

    // MARK: drawing helpers

    /// On a highlighted row everything turns white, as it does in native items.
    func ink(_ colour: NSColor) -> NSColor { highlighted ? .white : colour }

    func string(_ s: String, _ font: NSFont, _ colour: NSColor,
                wraps: Bool = false) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = wraps ? .byWordWrapping : .byTruncatingTail
        return NSAttributedString(string: s, attributes: [
            .font: font, .foregroundColor: ink(colour), .paragraphStyle: style,
        ])
    }

    /// Left-aligned text clipped to `width`, so a long name can never run into
    /// the value on the right.
    func left(_ s: String, _ font: NSFont, _ colour: NSColor, y: CGFloat, width: CGFloat? = nil) {
        let text = string(s, font, colour)
        let room = width ?? (bounds.width - 2 * inset)
        text.draw(with: NSRect(x: inset, y: y, width: room, height: ceil(text.size().height)),
                  options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine])
    }

    /// Right-aligned text; returns where it starts so the left side can stop short.
    @discardableResult
    func right(_ s: String, _ font: NSFont, _ colour: NSColor, y: CGFloat,
               end: CGFloat? = nil) -> CGFloat {
        let text = string(s, font, colour)
        let x = (end ?? bounds.width - inset) - text.size().width
        text.draw(at: NSPoint(x: x, y: y))
        return x
    }

    func bar(percent: Int, level: String, y: CGFloat, height: CGFloat, muted: Bool = false) {
        let track = NSRect(x: inset, y: y, width: bounds.width - 2 * inset, height: height)
        (highlighted ? NSColor.white.withAlphaComponent(0.28)
                     : NSColor.labelColor.withAlphaComponent(0.10)).setFill()
        NSBezierPath(roundedRect: track, xRadius: height / 2, yRadius: height / 2).fill()

        // Never narrower than its own height, so 0% still shows a dot — which
        // is how Claude draws an untouched limit, and it reads as "measured,
        // nothing used" rather than "missing".
        var fill = track
        fill.size.width = max(height, track.width * CGFloat(min(100, max(0, percent))) / 100)
        (highlighted ? NSColor.white : muted ? NSColor.tertiaryLabelColor : barColor(level)).setFill()
        NSBezierPath(roundedRect: fill, xRadius: height / 2, yRadius: height / 2).fill()
    }

    /// A capsule on the right, like Claude's "Tap to open" and "Updated just now".
    @discardableResult
    func pill(_ s: String, y: CGFloat) -> CGFloat {
        let text = string(s, .systemFont(ofSize: 11, weight: .medium), .secondaryLabelColor)
        let size = text.size()
        let box = NSRect(x: bounds.width - inset - size.width - 18, y: y,
                         width: size.width + 18, height: 20)
        (highlighted ? NSColor.white.withAlphaComponent(0.22)
                     : NSColor.labelColor.withAlphaComponent(0.09)).setFill()
        NSBezierPath(roundedRect: box, xRadius: 10, yRadius: 10).fill()
        text.draw(at: NSPoint(x: box.minX + 9, y: box.minY + (20 - size.height) / 2))
        return box.minX
    }
}

// MARK: - rows

/// The app's name, how the fleet is doing, and whether auto-switching is armed.
final class HeaderView: MenuRow {
    let subtitle: String, status: String
    static let subtitleFont = NSFont.systemFont(ofSize: 12)

    init(subtitle: String, status: String) {
        self.subtitle = subtitle
        self.status = status
        let wrapped = NSAttributedString(string: subtitle, attributes: [.font: Self.subtitleFont])
            .boundingRect(with: NSSize(width: menuWidth - 2 * inset, height: 200),
                          options: [.usesLineFragmentOrigin])
        super.init(height: 34 + ceil(wrapped.height) + 10)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let pillStart = pill(status, y: 9)
        left("claudini-pilot", .systemFont(ofSize: 14, weight: .semibold), .labelColor,
             y: 10, width: pillStart - inset - 8)
        string(subtitle, Self.subtitleFont, .secondaryLabelColor, wraps: true)
            .draw(with: NSRect(x: inset, y: 34, width: bounds.width - 2 * inset, height: 200),
                  options: [.usesLineFragmentOrigin])
    }
}

/// "YOUR USAGE LIMITS · TEAM" in Claude's menu.
final class SectionView: MenuRow {
    let title: String

    init(_ title: String) {
        self.title = title.uppercased()
        super.init(height: 26)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let text = NSAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: 11, weight: .semibold),
            .foregroundColor: NSColor.tertiaryLabelColor,
            .kern: 0.6,
        ])
        text.draw(at: NSPoint(x: inset, y: 8))
    }
}

/// One limit as Claude draws it: title left, "18% · resets 5d" right, bar below.
final class LimitView: MenuRow {
    let limit: Limit

    init(_ limit: Limit) {
        self.limit = limit
        super.init(height: 48)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let value = usage(limit.percent, resets: limit.resets_at_epoch)
        let valueStart = right(value, .systemFont(ofSize: 13), .secondaryLabelColor, y: 4)
        left(limit.title, .systemFont(ofSize: 13), .labelColor, y: 4,
             width: valueStart - inset - 8)
        bar(percent: limit.percent, level: limit.level, y: 27, height: 8)
    }
}

/// Which account is in use, and what kind of subscription it is.
final class AccountTitleView: MenuRow {
    let profile: Profile

    init(_ profile: Profile) {
        self.profile = profile
        super.init(height: 28)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let plan = profile.disabled ? "switched off" : planLine(profile)
        let planStart = right(plan, .systemFont(ofSize: 12),
                              profile.disabled ? .systemOrange : .secondaryLabelColor,
                              y: 5, end: lineEnd)
        left(profile.name, .systemFont(ofSize: 14, weight: .semibold), .labelColor, y: 4,
             width: planStart - inset - 8)
    }
}

/// An account that could not be read: say why, in place of its bars.
final class StatusView: MenuRow {
    let profile: Profile

    init(_ profile: Profile) {
        self.profile = profile
        super.init(height: 26)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let hint = profile.needs_login ? profile.detail + " — click to log in" : profile.detail
        left(hint, .systemFont(ofSize: 12), .systemRed, y: 5)
    }
}

/// Where the next `claude` session goes, and why.
final class NextView: MenuRow {
    let next: NextUp
    let lines: [(String, NSFont, NSColor)]

    init(_ next: NextUp) {
        self.next = next
        let sentence = next.reason.prefix(1).uppercased() + next.reason.dropFirst()
        var lines = [(sentence, NSFont.systemFont(ofSize: 12), NSColor.secondaryLabelColor)]
        if let blocked = next.blocked_by {
            lines.append(("Not switching: " + blocked, .systemFont(ofSize: 12), .tertiaryLabelColor))
        }
        if let after = next.after {
            lines.append(("Then \(after) takes over", .systemFont(ofSize: 12), .tertiaryLabelColor))
        }
        self.lines = lines
        super.init(height: 28 + CGFloat(lines.count) * 17 + 6)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let moving = !(next.staying) && next.name != nil
        pill(moving ? "Switching" : "Staying", y: 4)
        left(next.name ?? "—", .systemFont(ofSize: 14, weight: .semibold),
             moving ? .systemGreen : .labelColor, y: 5, width: menuWidth - 2 * inset - 90)
        for (index, (text, font, colour)) in lines.enumerated() {
            left(text, font, colour, y: 30 + CGFloat(index) * 17)
        }
    }
}

/// Another account: clickable, with the limit that binds it as a slim bar.
final class AccountRowView: MenuRow {
    let profile: Profile

    init(_ profile: Profile) {
        self.profile = profile
        super.init(height: 46)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        // Switched off, the row stays readable but steps back: its figures
        // still help you decide when to switch it on again.
        let off = profile.disabled
        var valueStart = lineEnd
        if let binding = profile.binding {
            valueStart = right(usage(binding.percent, resets: binding.resets_at_epoch,
                                     label: binding.label),
                               .systemFont(ofSize: 12),
                               off ? .tertiaryLabelColor : .secondaryLabelColor,
                               y: 6, end: lineEnd)
        }

        // Name, then the workspace and plan in a quieter voice beside it.
        let nameColour: NSColor = off ? .secondaryLabelColor : .labelColor
        let name = string(profile.name, .systemFont(ofSize: 13, weight: .medium), nameColour)
        let nameWidth = min(name.size().width, valueStart - inset - 8)
        left(profile.name, .systemFont(ofSize: 13, weight: .medium), nameColour, y: 5,
             width: nameWidth)
        let plan = planLine(profile)
        let planX = inset + nameWidth + 8
        if !plan.isEmpty, valueStart - planX > 30 {
            string(plan, .systemFont(ofSize: 11), .tertiaryLabelColor)
                .draw(with: NSRect(x: planX, y: 7, width: valueStart - planX - 8, height: 16),
                      options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine])
        }

        if let binding = profile.binding {
            bar(percent: binding.percent, level: binding.level, y: 29, height: 5, muted: off)
        } else {
            let hint = profile.needs_login ? profile.detail + " — click to log in" : profile.detail
            left(hint, .systemFont(ofSize: 11), .systemRed, y: 26)
        }
    }
}

/// A plain action with a pill on the right — "Refresh · Updated just now".
final class ActionRowView: MenuRow {
    let title: String, badge: String

    init(title: String, badge: String) {
        self.title = title
        self.badge = badge
        super.init(height: 28)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let pillStart = pill(badge, y: 4)
        left(title, .systemFont(ofSize: 13), .labelColor, y: 5, width: pillStart - inset - 8)
    }
}

/// A quiet one-line note, such as the API being paused.
final class NoteView: MenuRow {
    let note: String

    init(_ note: String) {
        self.note = note
        super.init(height: 22)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        left(note, .systemFont(ofSize: 11), .secondaryLabelColor, y: 4)
    }
}
