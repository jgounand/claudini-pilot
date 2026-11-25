// The dashboard window: whether the accounts will last, when capacity comes
// back, and how much of what is paid for goes unused.
//
// Drawn with Swift Charts from `claudini-usage --dashboard-json`; the figures
// are worked out by the engine (claudini_history.dashboard), which knows the
// history and the plans. This side only lays them out.
//
// Like the panel, every control is drawn, so `ClaudiniBar --dashboard-preview`
// can render the window to a picture exactly as it looks.

import Charts
import SwiftUI

// MARK: - contract

struct DashboardData: Decodable {
    let generated_at: Int
    /// Calendar weeks, oldest first — from the daily summary, kept for good.
    let weeks: [WeekRow]?
    let span: Span
    let capacity: Capacity
    let resets: [ResetRow]
    let seats: [SeatRow]
    let totals: Totals

    struct Span: Decodable {
        let from: Int?
        let to: Int?
        let samples: Int
    }

    struct Point: Decodable {
        let at: Int
        let reserve: Double
        var date: Date { Date(timeIntervalSince1970: TimeInterval(at)) }
    }

    struct Capacity: Decodable {
        let points: [Point]
        let forecast: [Point]
        /// Accounts' worth of weekly allowance spent per day, at the recent pace.
        let burn_per_day: Double
        let run_out_epoch: Int?
        let line: Int
    }

    struct ResetRow: Decodable {
        let name: String
        let plan: String
        let weekly_pct: Int?
        let weekly_reset_epoch: Int?
        let session_pct: Int?
        let session_reset_epoch: Int?
    }

    struct Cycle: Decodable {
        let end_epoch: Int?
        let used_pct: Int
    }

    struct SeatRow: Decodable {
        let name: String
        let plan: String
        let monthly_price: Double?
        let off: Bool
        let cycles: [Cycle]
        let current_used_pct: Int?
        let avg_used_pct: Double?
        let unused_value_month: Double?
    }

    struct WeekRow: Decodable {
        /// The Monday that starts it, "YYYY-MM-DD".
        let start: String
        /// The week under way.
        let partial: Bool
        /// Accounts' worth spent, and brought (one per account read all week).
        let used: Double
        let capacity: Double
        /// Weekly points each account spent that week.
        let accounts: [String: Int]
        let unused_value: Double
    }

    struct Totals: Decodable {
        let monthly_price: Double
        let unused_value_month: Double
        let measured: Int
    }
}

final class DashboardModel: ObservableObject {
    @Published var data: DashboardData?
    @Published var loading = false
    @Published var failed = false
    var preview = false
    var onRefresh: () -> Void = {}
}

enum ChartSpan: String, CaseIterable {
    case day, week, month

    var title: String {
        switch self {
        case .day: return "24 h"
        case .week: return "7 days"
        case .month: return "30 days"
        }
    }

    var seconds: TimeInterval {
        switch self {
        case .day: return 86400
        case .week: return 7 * 86400
        case .month: return 30 * 86400
        }
    }
}

let dashboardWidth: CGFloat = 900

// MARK: - the window

struct DashboardView: View {
    @ObservedObject var model: DashboardModel
    @AppStorage("dashboard.range") private var rangeName = ChartSpan.week.rawValue

    var body: some View {
        Group {
            if model.preview {
                content
            } else {
                ScrollView { content }
            }
        }
        .frame(width: dashboardWidth)
        .frame(minHeight: model.preview ? 0 : 640)
    }

    private var range: ChartSpan { ChartSpan(rawValue: rangeName) ?? .week }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            if let data = model.data {
                kpis(data)
                card("Capacity", subtitle: "Weekly allowance left across the accounts in rotation, "
                     + "in accounts' worth. Dashed: the coming week if the last three days' pace holds.") {
                    capacityChart(data)
                }
                card("When capacity comes back", subtitle: "Each account until its weekly reset. "
                     + "The colour is how much of the week it has used.") {
                    resetChart(data)
                }
                if let weeks = data.weeks, !weeks.isEmpty {
                    card("Week by week", subtitle: "What was spent against what the accounts brought, "
                         + "per calendar week, and each account's share. Kept for good, a day at a time.") {
                        weekChart(weeks)
                    }
                }
                card("How much of each seat is used", subtitle: "Share of the weekly allowance used before "
                     + "it reset, last four weeks. Unused value = monthly price × the share left unused.") {
                    seatChart(data)
                }
            } else {
                Text(model.failed ? "The engine gave no dashboard. Try claudini-usage --dashboard-json."
                                  : "Reading the history…")
                    .foregroundStyle(model.failed ? Color.orange : Color.secondary)
                    .frame(maxWidth: .infinity, minHeight: 300)
            }
        }
        .padding(20)
    }

    // MARK: header and figures

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("Dashboard").font(.system(size: 20, weight: .semibold))
            if let span = model.data?.span, let from = span.from, let to = span.to {
                Text("\(span.samples.formatted()) readings · \(days(from, to)) of history")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Spacer()
            ForEach(ChartSpan.allCases, id: \.self) { option in
                Chip(title: option.title, selected: option == range) { rangeName = option.rawValue }
            }
            Chip(icon: "arrow.clockwise") { model.onRefresh() }
                .opacity(model.loading ? 0.4 : 1)
                .help("Read the history again")
        }
    }

    private func kpis(_ data: DashboardData) -> some View {
        let now = data.capacity.points.last?.reserve ?? data.capacity.forecast.first?.reserve ?? 0
        let runOut = data.capacity.run_out_epoch.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        let totals = data.totals
        let share = totals.monthly_price > 0 ? totals.unused_value_month / totals.monthly_price : 0
        return HStack(spacing: 10) {
            Tile(title: "Weekly left now", value: String(format: "%.1f", now), unit: "",
                 detail: "accounts' worth", tint: now < 1 ? .orange : .accentColor)
            Tile(title: "Pace", value: String(format: "%.1f", data.capacity.burn_per_day), unit: " /day",
                 detail: "accounts' worth, last 3 days", tint: .primary)
            Tile(title: "Runs out", value: runOut.map { $0.formatted(.dateTime.weekday(.abbreviated).hour().minute()) }
                 ?? "Not this week", unit: "",
                 detail: runOut == nil ? "at this pace, within 7 days" : "at this pace",
                 tint: runOut == nil ? .green : .red)
            Tile(title: "Paid, unused", value: money(totals.unused_value_month), unit: " /mo",
                 detail: "\(Int((share * 100).rounded()))% of \(money(totals.monthly_price)) "
                    + "· \(totals.measured) seats measured",
                 tint: share > 0.25 ? .orange : .primary)
        }
    }

    // MARK: charts

    private func capacityChart(_ data: DashboardData) -> some View {
        let start = Date(timeIntervalSince1970: TimeInterval(data.generated_at)) - range.seconds
        let history = data.capacity.points.filter { $0.date >= start }
        let ahead = data.capacity.forecast
        let now = Date(timeIntervalSince1970: TimeInterval(data.generated_at))
        let top = max(1, (history.map(\.reserve) + ahead.map(\.reserve)).max() ?? 1)
        return Chart {
            ForEach(history, id: \.at) { point in
                AreaMark(x: .value("Time", point.date), y: .value("Left", point.reserve))
                    .foregroundStyle(LinearGradient(colors: [Color.accentColor.opacity(0.35),
                                                             Color.accentColor.opacity(0.05)],
                                                    startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("Time", point.date), y: .value("Left", point.reserve),
                         series: .value("Series", "measured"))
                    .foregroundStyle(Color.accentColor)
                    .lineStyle(StrokeStyle(lineWidth: 1.8))
                    .interpolationMethod(.monotone)
            }
            ForEach(ahead, id: \.at) { point in
                LineMark(x: .value("Time", point.date), y: .value("Left", point.reserve),
                         series: .value("Series", "forecast"))
                    .foregroundStyle(Color.secondary)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
            }
            RuleMark(x: .value("Now", now))
                .foregroundStyle(Color.secondary.opacity(0.5))
                .annotation(position: .top, alignment: .center) {
                    Text("now").font(.system(size: 9)).foregroundStyle(.secondary)
                }
            if let out = data.capacity.run_out_epoch {
                RuleMark(x: .value("Runs out", Date(timeIntervalSince1970: TimeInterval(out))))
                    .foregroundStyle(Color.red)
                    .annotation(position: .top) {
                        Text("runs out").font(.system(size: 9, weight: .semibold)).foregroundStyle(.red)
                    }
            }
        }
        .chartYScale(domain: 0...(top * 1.1))
        .chartYAxisLabel("accounts' worth")
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 8)) { _ in
                AxisGridLine()
                AxisValueLabel(format: range == .day ? .dateTime.hour() : .dateTime.weekday(.abbreviated).day())
            }
        }
        .frame(height: 220)
    }

    /// Each account as a bar from now to its weekly reset, on a week-long axis.
    /// Rows rather than a Swift Charts plot: a category axis with twenty names
    /// put each name beside the wrong bar.
    private func resetChart(_ data: DashboardData) -> some View {
        let now = Date(timeIntervalSince1970: TimeInterval(data.generated_at))
        let end = now + 7 * 86400
        let rows = data.resets.filter { $0.weekly_reset_epoch != nil }
        return VStack(spacing: 3) {
            BarRow(name: "") { width in
                DayScale(from: now, to: end, width: width)
            }
            .frame(height: 16)
            ForEach(rows, id: \.name) { row in
                let reset = Date(timeIntervalSince1970: TimeInterval(row.weekly_reset_epoch ?? data.generated_at))
                let used = row.weekly_pct ?? 0
                BarRow(name: row.name) { width in
                    let scale = width / CGFloat(end.timeIntervalSince(now))
                    let length = max(8, CGFloat(min(reset, end).timeIntervalSince(now)) * scale)
                    ZStack(alignment: .leading) {
                        DayGrid(from: now, to: end, width: width)
                        Capsule().fill(usageColour(used, line: data.capacity.line).opacity(0.85))
                            .frame(width: length, height: 10)
                        Text("\(used)% · \(reset.formatted(.dateTime.weekday(.abbreviated).hour()))")
                            .font(.system(size: 9.5)).foregroundStyle(.secondary)
                            .fixedSize()
                            .offset(x: length + 5)
                    }
                }
            }
        }
    }

    /// How much of each week every seat used, with what the unused part costs.
    private func seatChart(_ data: DashboardData) -> some View {
        VStack(spacing: 3) {
            BarRow(name: "") { width in
                ZStack(alignment: .leading) {
                    ForEach([0, 25, 50, 75, 100], id: \.self) { mark in
                        Text("\(mark)%").font(.system(size: 9)).foregroundStyle(.tertiary)
                            .fixedSize()
                            .offset(x: seatScale(width) * CGFloat(mark) / 100 - (mark == 0 ? 0 : 10))
                    }
                }
            }
            .frame(height: 16)
            ForEach(data.seats, id: \.name) { seat in
                let measured = seat.avg_used_pct
                let value = measured ?? Double(seat.current_used_pct ?? 0)
                BarRow(name: seat.name) { width in
                    let full = seatScale(width)
                    let length = max(8, full * CGFloat(min(value, 100)) / 100)
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.06)).frame(width: full, height: 10)
                        Capsule().fill(measured == nil ? Color.secondary.opacity(0.45)
                                                       : usageColour(Int(value), line: 101, invert: true))
                            .frame(width: length, height: 10)
                        Text(seatNote(seat)).font(.system(size: 9.5)).foregroundStyle(.secondary)
                            .fixedSize()
                            .offset(x: full + 8)
                    }
                }
            }
        }
    }

    /// Columns of spent against brought, then a grid of every account by week.
    private func weekChart(_ weeks: [DashboardData.WeekRow]) -> some View {
        let top = max(1, weeks.map { max($0.used, $0.capacity) }.max() ?? 1)
        let names = Set(weeks.flatMap { $0.accounts.keys }).sorted { a, b in
            let total = { (name: String) in weeks.reduce(0) { $0 + ($1.accounts[name] ?? 0) } }
            return total(a) == total(b) ? a < b : total(a) > total(b)
        }
        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .bottom, spacing: 10) {
                ForEach(weeks, id: \.start) { week in
                    VStack(spacing: 4) {
                        Text(String(format: "%.1f", week.used))
                            .font(.system(size: 11, weight: .semibold).monospacedDigit())
                        ZStack(alignment: .bottom) {
                            RoundedRectangle(cornerRadius: 4).fill(Color.primary.opacity(0.08))
                                .frame(height: 130 * CGFloat(week.capacity / top))
                            RoundedRectangle(cornerRadius: 4)
                                .fill(week.partial ? Color.accentColor.opacity(0.55) : Color.accentColor)
                                .frame(height: max(2, 130 * CGFloat(week.used / top)))
                        }
                        .frame(height: 130, alignment: .bottom)
                        Text(weekLabel(week.start)).font(.system(size: 10, weight: .medium))
                        Text(String(format: "of %.1f brought", week.capacity))
                            .font(.system(size: 9).monospacedDigit()).foregroundStyle(.secondary)
                        Text(week.partial ? "so far" : "\(money(week.unused_value)) unused")
                            .font(.system(size: 9)).foregroundStyle(week.partial ? Color.secondary : Color.orange)
                    }
                    .frame(maxWidth: 90)
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 14) {
                legend(Color.accentColor, "spent, in accounts' worth")
                legend(Color.primary.opacity(0.12), "brought: one week per account read all week")
                Text("A week that resets mid-week can give one account up to two weeks' worth.")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Divider()
            VStack(spacing: 2) {
                BarRow(name: "") { width in
                    HStack(spacing: 2) {
                        ForEach(weeks, id: \.start) { week in
                            Text(weekLabel(week.start)).font(.system(size: 9)).foregroundStyle(.tertiary)
                                .frame(width: cellWidth(width, weeks.count))
                        }
                    }
                }
                ForEach(names, id: \.self) { name in
                    BarRow(name: name) { width in
                        HStack(spacing: 2) {
                            ForEach(weeks, id: \.start) { week in
                                let used = week.accounts[name]
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(heat(used))
                                    .frame(width: cellWidth(width, weeks.count), height: 14)
                                    .overlay {
                                        if let used {
                                            Text("\(used)%").font(.system(size: 8.5, weight: .medium).monospacedDigit())
                                                .foregroundStyle(used >= 60 ? Color.white : Color.primary.opacity(0.7))
                                        }
                                    }
                            }
                        }
                    }
                }
            }
        }
    }

    private func cellWidth(_ width: CGFloat, _ count: Int) -> CGFloat {
        min(64, (width - CGFloat(max(0, count - 1)) * 2) / CGFloat(max(1, count)))
    }

    /// Nothing read: blank. Read: from faint (unused) to full blue (a whole week spent).
    private func heat(_ used: Int?) -> Color {
        guard let used else { return Color.clear }
        return Color.accentColor.opacity(0.10 + 0.85 * min(1, Double(used) / 100))
    }

    private func legend(_ colour: Color, _ text: String) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 2).fill(colour).frame(width: 10, height: 10)
            Text(text).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    /// "16 Nov" from "2026-11-16".
    private func weekLabel(_ start: String) -> String {
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: start) else { return start }
        return date.formatted(.dateTime.day().month(.abbreviated))
    }

    /// The seat bars stop short of the full width, leaving room for the note.
    private func seatScale(_ width: CGFloat) -> CGFloat { width * 0.58 }

    // MARK: pieces

    private func card<Content: View>(_ title: String, subtitle: String,
                                     @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 13, weight: .semibold))
            Text(subtitle).font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 10)
            content()
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(Color.primary.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(Color.primary.opacity(0.07)))
    }

    private func seatNote(_ seat: DashboardData.SeatRow) -> String {
        guard let average = seat.avg_used_pct else {
            return "first week · \(seat.current_used_pct ?? 0)% so far · \(seat.plan)"
        }
        var note = "\(Int(average.rounded()))% used · \(seat.plan)"
        if let idle = seat.unused_value_month, idle >= 1 { note += " · \(money(idle))/mo unused" }
        if seat.off { note += " · off" }
        return note
    }
}

/// Blue while there is room, orange then red as a week fills — or, for seats,
/// the other way round: a seat left mostly unused is the one to look at.
func usageColour(_ percent: Int, line: Int, invert: Bool = false) -> Color {
    if invert {
        return percent < 50 ? .orange : percent < 75 ? .yellow : .green
    }
    return percent >= min(line, 95) ? .red : percent >= 75 ? .orange : .accentColor
}

func money(_ value: Double) -> String {
    "$" + Int(value.rounded()).formatted()
}

func days(_ from: Int, _ to: Int) -> String {
    let count = max(1, (to - from) / 86400)
    return count == 1 ? "1 day" : "\(count) days"
}


/// A row of the bar charts: the account's name in a fixed column, then the
/// drawing, which is handed the width it has.
struct BarRow<Content: View>: View {
    let name: String
    @ViewBuilder let content: (CGFloat) -> Content

    var body: some View {
        HStack(spacing: 10) {
            Text(name).font(.system(size: 11)).lineLimit(1).truncationMode(.middle)
                .frame(width: 150, alignment: .leading)
            GeometryReader { geometry in
                content(geometry.size.width)
                    .frame(width: geometry.size.width, height: geometry.size.height, alignment: .leading)
            }
        }
        .frame(height: 17)
    }
}

/// A faint line at each midnight between two dates.
struct DayGrid: View {
    let from: Date
    let to: Date
    let width: CGFloat

    var body: some View {
        ZStack(alignment: .leading) {
            ForEach(midnights(from, to), id: \.self) { day in
                Rectangle().fill(Color.primary.opacity(0.07)).frame(width: 1)
                    .offset(x: CGFloat(day.timeIntervalSince(from) / to.timeIntervalSince(from)) * width)
            }
        }
    }
}

/// The day names above the reset chart, each over its midnight.
struct DayScale: View {
    let from: Date
    let to: Date
    let width: CGFloat

    var body: some View {
        ZStack(alignment: .leading) {
            ForEach(midnights(from, to), id: \.self) { day in
                Text(day.formatted(.dateTime.weekday(.abbreviated).day()))
                    .font(.system(size: 9)).foregroundStyle(.tertiary)
                    .fixedSize()
                    .offset(x: CGFloat(day.timeIntervalSince(from) / to.timeIntervalSince(from)) * width + 3)
            }
        }
    }
}

func midnights(_ from: Date, _ to: Date) -> [Date] {
    var days: [Date] = []
    var day = Calendar.current.startOfDay(for: from) + 86400
    while day < to {
        days.append(day)
        day += 86400
    }
    return days
}
