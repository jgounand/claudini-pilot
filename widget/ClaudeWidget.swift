// The widget: what is left on your Claude subscriptions, on the desktop or in
// Notification Center.

import SwiftUI
import WidgetKit

struct Entry: TimelineEntry {
    let date: Date
    let board: Board
}

struct Provider: AppIntentTimelineProvider {
    private func board(_ settings: FollowAccount, at date: Date) -> Board {
        let stored = SharedStore.read()
        return Board(snapshot: stored?.snapshot, writtenAt: stored?.writtenAt ?? .distantPast,
                     now: date, followed: settings.followed, showOff: settings.showOff)
    }

    func placeholder(in context: Context) -> Entry {
        Entry(date: .now, board: board(FollowAccount(), at: .now))
    }

    func snapshot(for settings: FollowAccount, in context: Context) async -> Entry {
        Entry(date: .now, board: board(settings, at: .now))
    }

    /// One entry every five minutes for the next hour, all drawn from the same
    /// snapshot, so "resets in" keeps counting down between reloads without
    /// spending any. ClaudiniBar reloads the widget when the figures change;
    /// the hour is only the fallback for when it is not running.
    func timeline(for settings: FollowAccount, in context: Context) async -> Timeline<Entry> {
        let start = Date.now
        let entries = (0...12).map { step in
            let date = start.addingTimeInterval(TimeInterval(step * 5 * 60))
            return Entry(date: date, board: board(settings, at: date))
        }
        return Timeline(entries: entries, policy: .atEnd)
    }
}

struct ClaudeUsageWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: "claude-usage", intent: FollowAccount.self,
                               provider: Provider()) { entry in
            SizedContent(board: entry.board)
                .containerBackground(.background, for: .widget)
        }
        .configurationDisplayName("Claude usage")
        .description("What is left on your Claude subscriptions, and which one the next session gets.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

/// Picks the layout for the size the widget was placed at.
struct SizedContent: View {
    let board: Board
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall: WidgetContent(board: board, size: .small)
        case .systemMedium: WidgetContent(board: board, size: .medium)
        default: WidgetContent(board: board, size: .large)
        }
    }
}

@main
struct ClaudiniWidgets: WidgetBundle {
    var body: some Widget {
        ClaudeUsageWidget()
    }
}
