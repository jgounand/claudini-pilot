// What the widget draws, in three sizes.
//
// The same look as the menu: a label on the left, its figure on the right, a
// bar underneath — blue while there is room, then orange, then red.
//
// Compiled into the app as well, which renders these views to PNG files
// (`ClaudiniBar --widget-previews DIR`) so a layout can be checked without
// placing a widget on the desktop. The buttons are only live in the widget.

import SwiftUI
#if WIDGET
import AppIntents
#endif

/// The sizes drawn. WidgetKit has its own type for this, but only inside a
/// widget, and the app's previews need to name them too.
enum WidgetSize: CaseIterable {
    case small, medium, large

    /// The desktop sizes, in points.
    var points: CGSize {
        switch self {
        case .small: return CGSize(width: 170, height: 170)
        case .medium: return CGSize(width: 364, height: 170)
        case .large: return CGSize(width: 364, height: 382)
        }
    }
}

/// Everything one widget draws, worked out once.
struct Board {
    let snapshot: Snapshot?
    let writtenAt: Date
    /// The moment this entry is drawn for — later than now for the entries
    /// WidgetKit shows over the next hour.
    let now: Date
    /// The account the widget follows; nil follows whichever is in use.
    let followed: String?
    let showOff: Bool

    var account: Profile? {
        guard let profiles = snapshot?.profiles else { return nil }
        return followed.flatMap { name in profiles.first { $0.name == name } }
            ?? profiles.first { $0.active }
    }

    var others: [Profile] {
        (snapshot?.profiles ?? []).filter { $0.name != account?.name && (showOff || !$0.disabled) }
    }

    /// The app writes a snapshot every few minutes while it runs. Twenty
    /// minutes without one means it is not running, and the figures are old.
    var stale: Bool { now.timeIntervalSince(writtenAt) > 20 * 60 }
}

extension Color {
    static func level(_ level: String) -> Color {
        switch level {
        case "critical": return .red
        case "warning": return .orange
        default: return .blue
        }
    }
}

// MARK: - pieces

/// A ring filled to the binding window's usage, the figure in the middle.
struct UsageRing: View {
    let percent: Int
    let level: String
    var width: CGFloat = 7
    var muted = false

    var body: some View {
        ZStack {
            Circle().stroke(Color.primary.opacity(0.1), lineWidth: width)
            Circle()
                .trim(from: 0, to: CGFloat(min(100, max(0, percent))) / 100)
                .stroke(muted ? Color.secondary : Color.level(level),
                        style: StrokeStyle(lineWidth: width, lineCap: .round))
                .rotationEffect(.degrees(-90))
            Text("\(percent)%")
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .minimumScaleFactor(0.6)
        }
    }
}

/// A capsule filled left to right; never narrower than its own height, so an
/// untouched limit still shows a dot — "measured, nothing used".
struct Gauge: View {
    let percent: Int
    let level: String
    var height: CGFloat = 6
    var muted = false

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule()
                    .fill(muted ? Color.secondary.opacity(0.6) : Color.level(level))
                    .frame(width: max(height, geometry.size.width
                                      * CGFloat(min(100, max(0, percent))) / 100))
            }
        }
        .frame(height: height)
    }
}

/// One limit as Claude draws it: title left, "18% · resets 5d" right, bar below.
struct LimitRow: View {
    let limit: Limit
    let now: Date
    var muted = false

    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 6) {
                Text(limit.title).lineLimit(1)
                Spacer(minLength: 4)
                Text(usage(limit.percent, resets: limit.resets_at_epoch, now: now))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .lineLimit(1)
            }
            .font(.system(size: 11))
            Gauge(percent: limit.percent, level: limit.level, muted: muted)
        }
    }
}

/// The account's name, and its plan or why it cannot be used.
struct AccountHeading: View {
    let profile: Profile
    var nameSize: CGFloat = 13

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(profile.name)
                .font(.system(size: nameSize, weight: .semibold))
                .lineLimit(1)
            Group {
                if profile.disabled {
                    Text("Switched off").foregroundStyle(.orange)
                } else if profile.status != "ok" {
                    Text(profile.detail).foregroundStyle(.red)
                } else {
                    Text(planLine(profile)).foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 10))
            .lineLimit(1)
        }
    }
}

/// The line at the bottom: where the next session goes — or, when the
/// figures are old, that they are.
struct Footer: View {
    let board: Board
    /// The small widget has room for a name, not a sentence.
    var short = false

    var body: some View {
        Group {
            if board.stale {
                Text(board.writtenAt == .distantPast ? "Open ClaudiniBar to start"
                     : "\(updatedAgo(board.writtenAt, now: board.now)) · is ClaudiniBar running?")
                    .foregroundStyle(.orange)
            } else if let next = board.snapshot?.next {
                if next.staying, let after = next.after {
                    Text(short ? "Then \(after)" : "Staying · then \(after)")
                } else {
                    Text("Next: \(next.name ?? "—")")
                }
            }
        }
        .font(.system(size: 10))
        .foregroundStyle(.secondary)
        .lineLimit(1)
    }
}

/// Re-read now. Live in the widget; drawn, inert, in the app's previews.
struct RefreshButton: View {
    var body: some View {
        #if WIDGET
        Button(intent: RefreshUsage()) { icon }.buttonStyle(.plain)
        #else
        icon
        #endif
    }

    private var icon: some View {
        Image(systemName: "arrow.clockwise")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(.secondary)
    }
}

/// An account's switch: off keeps it out of the rotation.
///
/// Drawn, inside a plain button. A widget can only show SwiftUI's own shapes
/// and text: a real switch is an AppKit control, which WidgetKit replaces with
/// a yellow "not supported" square — exactly what the first version showed.
struct RotationSwitch: View {
    let profile: Profile

    var body: some View {
        #if WIDGET
        Button(intent: SetInRotation(name: profile.name, on: profile.disabled)) { knob }
            .buttonStyle(.plain)
        #else
        knob
        #endif
    }

    private var knob: some View {
        let on = !profile.disabled
        return Capsule()
            .fill(on ? Color.blue : Color.primary.opacity(0.18))
            .frame(width: 24, height: 14)
            .overlay(alignment: on ? .trailing : .leading) {
                Circle()
                    .fill(Color.white)
                    .padding(1.5)
                    .shadow(color: .black.opacity(0.15), radius: 0.5, y: 0.5)
            }
    }
}

// MARK: - sizes

struct WidgetContent: View {
    let board: Board
    let size: WidgetSize

    var body: some View {
        if board.snapshot == nil {
            VStack(spacing: 6) {
                Image(systemName: "gauge.with.dots.needle.33percent")
                    .font(.system(size: 22))
                    .foregroundStyle(.secondary)
                Text("Open ClaudiniBar to start")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        } else {
            switch size {
            case .small: SmallWidget(board: board)
            case .medium: MediumWidget(board: board)
            case .large: LargeWidget(board: board)
            }
        }
    }
}

/// One account: the ring for the window that binds, and where it resets.
struct SmallWidget: View {
    let board: Board

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let account = board.account {
                AccountHeading(profile: account)
                Spacer(minLength: 6)
                if let binding = account.binding {
                    HStack(spacing: 10) {
                        UsageRing(percent: binding.percent, level: binding.level,
                                  muted: account.disabled)
                            .frame(width: 58, height: 58)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(binding.label == "5h" ? "5-hour" : "Weekly")
                                .font(.system(size: 11, weight: .medium))
                            Text(resetsIn(binding.resets_at_epoch, now: board.now))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                    }
                } else {
                    Text(account.detail)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                        .lineLimit(3)
                }
                Spacer(minLength: 6)
            }
            Footer(board: board, short: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// One account in full: the ring, then every limit with its bar.
struct MediumWidget: View {
    let board: Board

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let account = board.account {
                HStack(alignment: .top, spacing: 14) {
                    VStack(alignment: .leading, spacing: 8) {
                        AccountHeading(profile: account)
                        if let binding = account.binding {
                            UsageRing(percent: binding.percent, level: binding.level,
                                      muted: account.disabled)
                                .frame(width: 62, height: 62)
                        }
                    }
                    .frame(width: 104, alignment: .leading)

                    VStack(spacing: 7) {
                        HStack {
                            Spacer()
                            RefreshButton()
                        }
                        .frame(height: 10)
                        if account.limits.isEmpty {
                            Text(account.detail)
                                .font(.system(size: 11))
                                .foregroundStyle(.red)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        ForEach(Array(account.limits.prefix(3).enumerated()), id: \.offset) { _, limit in
                            LimitRow(limit: limit, now: board.now, muted: account.disabled)
                        }
                    }
                }
            }
            Spacer(minLength: 4)
            Footer(board: board)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// The whole fleet: the followed account in full, every other one with its
/// bar and its switch.
struct LargeWidget: View {
    let board: Board

    /// Rows that fit under the account block; the rest are counted.
    private let room = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Text("Claude usage").font(.system(size: 13, weight: .semibold))
                Spacer()
                if let snapshot = board.snapshot {
                    Text(snapshot.auto ? "Auto · \(snapshot.mode)" : "Auto off")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }
                RefreshButton()
            }
            Text(board.snapshot?.fleet ?? "")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.top, 2)

            Divider().padding(.vertical, 7)

            if let account = board.account {
                HStack {
                    AccountHeading(profile: account)
                    Spacer()
                    RotationSwitch(profile: account)
                }
                VStack(spacing: 6) {
                    ForEach(Array(account.limits.prefix(3).enumerated()), id: \.offset) { _, limit in
                        LimitRow(limit: limit, now: board.now, muted: account.disabled)
                    }
                }
                .padding(.top, 5)
            }

            Divider().padding(.vertical, 7)

            VStack(spacing: 7) {
                ForEach(board.others.prefix(room), id: \.name) { profile in
                    OtherAccountRow(profile: profile, now: board.now)
                }
            }
            if board.others.count > room {
                Text("+\(board.others.count - room) more")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                    .padding(.top, 3)
            }
            Spacer(minLength: 4)
            Footer(board: board)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct OtherAccountRow: View {
    let profile: Profile
    let now: Date

    var body: some View {
        VStack(spacing: 3) {
            HStack(spacing: 6) {
                Text(profile.name)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(profile.disabled ? .secondary : .primary)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Group {
                    if let binding = profile.binding {
                        Text(usage(binding.percent, resets: binding.resets_at_epoch,
                                   label: binding.label, now: now))
                            .foregroundStyle(profile.disabled ? .tertiary : .secondary)
                    } else {
                        Text(profile.detail).foregroundStyle(.red)
                    }
                }
                .font(.system(size: 10))
                .monospacedDigit()
                .lineLimit(1)
                RotationSwitch(profile: profile)
            }
            if let binding = profile.binding {
                Gauge(percent: binding.percent, level: binding.level, height: 3,
                      muted: profile.disabled)
            }
        }
    }
}
