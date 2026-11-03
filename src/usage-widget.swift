// usage-widget — 桌面小组件：用量面板
//
// 开销控制：
//   · 纯 AppKit 绘制，不嵌 WebView，无 JS/CSS 引擎
//   · 不发起网络请求、不启动子进程，只读一个 JSON 文件
//   · 空闲时每 60 秒一次 stat；二进制被重编译过会自行退出交给 launchd 重启
//   · 窗口贴在桌面层级（图标下方、壁纸上方），点击穿透
//   · 双段进度条用固定槽位布局，标签/进度条/百分比各有固定宽度，不会互相重叠

import AppKit
import Foundation

// MARK: - 数据模型

struct WindowInfo: Codable {
    let label: String
    let usedPct: Double?
    let detail: String?
    let resetAt: String?

    enum CodingKeys: String, CodingKey {
        case label
        case usedPct = "used_pct"
        case detail
        case resetAt = "reset_at"
    }
}

struct MetricInfo: Codable {
    let label: String
    let value: String
    let sub: String?
}

struct Provider: Codable {
    let id: String
    let name: String
    let status: String
    let plan: String?
    let account: String?
    let windows: [WindowInfo]
    let metric: MetricInfo?
    let notes: [String]
}

struct SummaryRow: Codable {
    let label: String
    let burned: Int
}

struct Summary: Codable {
    let rows: [SummaryRow]
}

struct Payload: Codable {
    let generatedAt: String?
    let providers: [Provider]
    let summary: Summary?

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case providers
        case summary
    }
}

// MARK: - 固定槽位尺寸（布局与绘制共用同一组常量，杜绝漂移/重叠）

let NAME_SIZE: CGFloat = 10.5
let AUX_SIZE: CGFloat = 9.5
let SEG_LABEL_SIZE: CGFloat = 8
let METRIC_SIZE: CGFloat = 12
let ROW_HEIGHT: CGFloat = 18
let PAD_X: CGFloat = 12
let PAD_TOP: CGFloat = 9

let SEG_LABEL_W: CGFloat = 15     // "5h" / "7d" 固定标签槽
let SEG_INNER: CGFloat = 4        // 标签↔进度条、进度条↔百分比
let SEG_BAR_W: CGFloat = 34       // 每段进度条
let SEG_PCT_W: CGFloat = 27       // 百分比固定槽（右对齐）
let SEG_GAP: CGFloat = 12         // 两段之间

let SEG_W: CGFloat = SEG_LABEL_W + SEG_INNER + SEG_BAR_W + SEG_INNER + SEG_PCT_W
let DUAL_W: CGFloat = SEG_W * 2 + SEG_GAP
let SINGLE_BAR_W: CGFloat = DUAL_W - (SEG_LABEL_W + SEG_INNER * 2 + SEG_PCT_W)
let TAIL_GAP: CGFloat = 8         // 名字列 ↔ 进度区

func statusColor(_ pct: Double) -> NSColor {
    if pct >= 85 { return NSColor(calibratedRed: 0.97, green: 0.32, blue: 0.29, alpha: 1) }
    if pct >= 60 { return NSColor(calibratedRed: 0.82, green: 0.60, blue: 0.13, alpha: 1) }
    return NSColor(calibratedRed: 0.25, green: 0.73, blue: 0.31, alpha: 1)
}

func humanTokens(_ n: Int) -> String {
    let v = Double(n)
    if v >= 1e8 { return String(format: "%.2f 亿", v / 1e8) }
    if v >= 1e4 { return String(format: "%.1f 万", v / 1e4) }
    return "\(n)"
}

func textWidth(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular) -> CGFloat {
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight)]
    return NSAttributedString(string: text, attributes: attrs).size().width
}

// MARK: - 窗口归类

enum WindowKind { case short, long }

func classify(_ label: String) -> WindowKind? {
    let text = label.lowercased()
    if text.contains("5 小时") || text.contains("5h") { return .short }
    if text.contains("周") || text.contains("7 天") || text.contains("7d") { return .long }
    return nil
}

enum RowPlan {
    case dual(short: Double, long: Double)
    case single(label: String, pct: Double)
    case metric(String)
    case plain(String)
}

func rowPlan(_ provider: Provider) -> RowPlan {
    var shortPct: Double?
    var longPct: Double?
    for window in provider.windows {
        guard let pct = window.usedPct, let kind = classify(window.label) else { continue }
        switch kind {
        case .short: shortPct = max(shortPct ?? -1, pct)
        case .long: longPct = max(longPct ?? -1, pct)
        }
    }
    if let s = shortPct, let l = longPct { return .dual(short: s, long: l) }
    if let l = longPct { return .single(label: "7d", pct: l) }
    if let s = shortPct { return .single(label: "5h", pct: s) }
    if let metric = provider.metric { return .metric(metric.value) }
    let hint: String
    switch provider.status {
    case "missing": hint = "缺凭据"
    case "error": hint = "取数失败"
    case "local_only": hint = provider.plan ?? "仅本地"
    default: hint = provider.plan ?? "—"
    }
    return .plain(hint)
}

func pctText(_ pct: Double) -> String { String(format: "%.0f%%", pct) }

// MARK: - 绘制

final class WidgetView: NSView {
    var payload: Payload?
    var dataDate: String = "—"

    private(set) var panelWidth: CGFloat = 260
    private(set) var leftCol: CGFloat = 55
    private var footerText: String = ""
    private var hasFooter: Bool = false

    override var isFlipped: Bool { true }

    func layoutContent() {
        let providers = payload?.providers ?? []
        var widest = textWidth("用量", size: 11.5, weight: .semibold) + 7
            + textWidth(dataDate, size: AUX_SIZE)

        var leftNeed: CGFloat = 0
        for provider in providers {
            leftNeed = max(leftNeed, textWidth(provider.name, size: NAME_SIZE))
        }
        leftCol = ceil(leftNeed) + TAIL_GAP

        for provider in providers {
            var width = leftCol
            switch rowPlan(provider) {
            case .dual:
                width += DUAL_W
            case .single:
                width += DUAL_W
            case let .metric(value):
                width += textWidth(value, size: METRIC_SIZE, weight: .semibold)
            case let .plain(hint):
                width += textWidth(hint, size: AUX_SIZE)
            }
            widest = max(widest, width)
        }

        footerText = ""
        if let summary = payload?.summary, !summary.rows.isEmpty {
            var parts: [String] = []
            for row in summary.rows where row.label.hasPrefix("今日") || row.label.hasPrefix("本月") {
                parts.append("\(row.label) \(humanTokens(row.burned))")
            }
            if !parts.isEmpty {
                footerText = "烧掉 " + parts.joined(separator: " · ")
                widest = max(widest, textWidth(footerText, size: AUX_SIZE))
            }
        }
        hasFooter = !footerText.isEmpty
        panelWidth = ceil(widest) + PAD_X * 2
    }

    func contentHeight() -> CGFloat {
        let rows = CGFloat(payload?.providers.count ?? 0)
        let footer = hasFooter ? 15.0 : 0.0
        return PAD_TOP + 17 + rows * ROW_HEIGHT + footer + 9
    }

    override func draw(_ dirtyRect: NSRect) {
        let bg = NSBezierPath(roundedRect: bounds, xRadius: 12, yRadius: 12)
        NSColor(calibratedWhite: 0.06, alpha: 0.62).setFill()
        bg.fill()
        NSColor(calibratedWhite: 1.0, alpha: 0.08).setStroke()
        bg.lineWidth = 1
        bg.stroke()

        guard let payload = payload else {
            drawText("等待数据…", at: NSPoint(x: PAD_X, y: PAD_TOP), size: 12,
                     color: NSColor(calibratedWhite: 0.75, alpha: 1))
            return
        }

        let titleWidth = textWidth("用量", size: 11.5, weight: .semibold)
        drawText("用量", at: NSPoint(x: PAD_X, y: PAD_TOP + 1),
                 size: 11.5, weight: .semibold,
                 color: NSColor(calibratedWhite: 0.95, alpha: 1))
        drawText(dataDate, at: NSPoint(x: PAD_X + titleWidth + 7, y: PAD_TOP + 2),
                 size: AUX_SIZE, color: NSColor(calibratedWhite: 0.5, alpha: 1))

        var y = PAD_TOP + 17
        for provider in payload.providers {
            drawProvider(provider, y: y)
            y += ROW_HEIGHT
        }

        if hasFooter {
            y += 2
            NSColor(calibratedWhite: 1.0, alpha: 0.08).setFill()
            NSRect(x: PAD_X, y: y - 4, width: panelWidth - PAD_X * 2, height: 1).fill()
            y += 1
            drawText(footerText, at: NSPoint(x: PAD_X, y: y), size: AUX_SIZE,
                     color: NSColor(calibratedWhite: 0.62, alpha: 1))
        }
    }

    private func drawProvider(_ provider: Provider, y: CGFloat) {
        let tailX = PAD_X + leftCol
        let isError = provider.status != "ok" && provider.status != "local_only" && provider.status != "missing"
        drawText(provider.name, at: NSPoint(x: PAD_X, y: y), size: NAME_SIZE,
                 color: isError ? NSColor(calibratedRed: 0.97, green: 0.32, blue: 0.29, alpha: 1)
                                : NSColor(calibratedWhite: 0.88, alpha: 1))

        switch rowPlan(provider) {
        case let .dual(short, long):
            drawSegment("5h", pct: short, at: tailX, y: y, barWidth: SEG_BAR_W)
            drawSegment("7d", pct: long, at: tailX + SEG_W + SEG_GAP, y: y, barWidth: SEG_BAR_W)
        case let .single(label, pct):
            drawSegment(label, pct: pct, at: tailX, y: y, barWidth: SINGLE_BAR_W)
        case let .metric(value):
            drawText(value, at: NSPoint(x: tailX, y: y), size: METRIC_SIZE,
                     weight: .semibold, color: NSColor(calibratedWhite: 0.95, alpha: 1))
        case let .plain(hint):
            drawText(hint, at: NSPoint(x: tailX, y: y + 1), size: AUX_SIZE,
                     color: NSColor(calibratedWhite: 0.55, alpha: 1))
        }
    }

    /// 一段：固定标签槽 + 进度条 + 固定百分比槽（右对齐）。占用宽度恒等于槽位之和。
    private func drawSegment(_ label: String, pct: Double, at x: CGFloat,
                             y: CGFloat, barWidth: CGFloat) {
        drawText(label, at: NSPoint(x: x, y: y + 1), size: SEG_LABEL_SIZE,
                 color: NSColor(calibratedWhite: 0.42, alpha: 1))

        let trackX = x + SEG_LABEL_W + SEG_INNER
        let trackY = y + 6
        let track = NSBezierPath(roundedRect: NSRect(x: trackX, y: trackY, width: barWidth, height: 5),
                                 xRadius: 2.5, yRadius: 2.5)
        NSColor(calibratedWhite: 1.0, alpha: 0.12).setFill()
        track.fill()

        let fillWidth = barWidth * CGFloat(min(max(pct, 0), 100) / 100)
        if fillWidth > 0.5 {
            let fill = NSBezierPath(roundedRect: NSRect(x: trackX, y: trackY, width: fillWidth, height: 5),
                                    xRadius: 2.5, yRadius: 2.5)
            statusColor(pct).setFill()
            fill.fill()
        }

        // 百分比在固定槽内右对齐，字号变化也不会顶到下一段
        let pctX = trackX + barWidth + SEG_INNER
        let width = textWidth(pctText(pct), size: AUX_SIZE)
        drawText(pctText(pct), at: NSPoint(x: pctX + SEG_PCT_W - width, y: y + 1),
                 size: AUX_SIZE, color: statusColor(pct))
    }

    private func drawText(_ text: String, at point: NSPoint, size: CGFloat,
                          weight: NSFont.Weight = .regular, color: NSColor) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
        ]
        NSAttributedString(string: text, attributes: attrs).draw(at: point)
    }
}

// MARK: - 应用

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var view: WidgetView!
    private var timer: Timer?
    private var lastMtime: Date?
    private var binaryMtimeAtLaunch: Date?
    private var executablePath: String { ProcessInfo.processInfo.arguments[0] }
    private let dataPath = NSString(string: "~/.local/share/usage-monitor/widget-data.json")
        .expandingTildeInPath

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 260, height: 200),
                          styleMask: [.borderless],
                          backing: .buffered,
                          defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        let desktopIconLevel = Int(CGWindowLevelForKey(.desktopIconWindow))
        window.level = NSWindow.Level(rawValue: desktopIconLevel - 1)

        view = WidgetView(frame: NSRect(x: 0, y: 0, width: 260, height: 200))
        window.contentView = view

        binaryMtimeAtLaunch = fileMtime(executablePath)
        reload()
        window.orderFrontRegardless()

        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.tick()
        }
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main) { [weak self] _ in
                guard let self = self else { return }
                self.place(width: self.view.panelWidth, height: self.view.contentHeight())
            }
    }

    private func fileMtime(_ path: String) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
    }

    private func tick() {
        if let now = fileMtime(executablePath), now != binaryMtimeAtLaunch {
            exit(0)
        }
        if fileMtime(dataPath) != lastMtime {
            reload()
        }
    }

    private func reload() {
        let url = URL(fileURLWithPath: dataPath)
        guard let data = try? Data(contentsOf: url),
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            view.payload = nil
            view.needsDisplay = true
            return
        }
        lastMtime = fileMtime(dataPath)

        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        if let mtime = lastMtime {
            view.dataDate = formatter.string(from: mtime)
        }

        view.payload = payload
        view.layoutContent()
        let size = NSSize(width: view.panelWidth, height: view.contentHeight())
        view.frame = NSRect(origin: .zero, size: size)
        place(width: size.width, height: size.height)
        window.contentView?.needsDisplay = true
    }

    private func place(width: CGFloat, height: CGFloat) {
        guard let screen = NSScreen.main else { return }
        let margin: CGFloat = 22
        let visible = screen.visibleFrame
        window.setFrame(NSRect(x: visible.maxX - width - margin,
                               y: visible.maxY - height - margin,
                               width: width, height: height),
                        display: true)
    }
}

// MARK: - 离线快照（文档配图用：不建窗口、不联网、不读屏）

func writeSnapshot(jsonPath: String, pngPath: String, scale: CGFloat, padding: CGFloat) -> Int32 {
    _ = NSApplication.shared            // 字体度量与图形上下文要先初始化 AppKit

    guard let data = try? Data(contentsOf: URL(fileURLWithPath: jsonPath)),
          let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
        FileHandle.standardError.write(Data("解析失败：\(jsonPath)\n".utf8))
        return 2
    }

    let view = WidgetView(frame: NSRect(x: 0, y: 0, width: 260, height: 200))
    let formatter = DateFormatter()
    formatter.dateFormat = "HH:mm"
    let mtime = (try? FileManager.default.attributesOfItem(atPath: jsonPath))?[.modificationDate] as? Date
    view.dataDate = formatter.string(from: mtime ?? Date())
    view.payload = payload
    view.layoutContent()

    let size = NSSize(width: view.panelWidth, height: view.contentHeight())
    view.frame = NSRect(origin: .zero, size: size)

    let canvas = NSSize(width: size.width + padding * 2, height: size.height + padding * 2)
    let pxW = Int(ceil(canvas.width * scale))
    let pxH = Int(ceil(canvas.height * scale))
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pxW, pixelsHigh: pxH,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0),
          let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
        FileHandle.standardError.write(Data("无法创建位图上下文\n".utf8))
        return 3
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    ctx.cgContext.scaleBy(x: scale, y: scale)
    ctx.cgContext.setFillColor(NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.10, alpha: 1).cgColor)
    ctx.cgContext.fill(CGRect(origin: .zero, size: canvas))
    ctx.cgContext.translateBy(x: padding, y: padding + size.height)
    ctx.cgContext.scaleBy(x: 1, y: -1)   // WidgetView 是 flipped 坐标
    view.draw(view.bounds)
    ctx.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()

    guard let png = rep.representation(using: .png, properties: [:]) else { return 4 }
    do {
        try png.write(to: URL(fileURLWithPath: pngPath))
    } catch {
        FileHandle.standardError.write(Data("写入失败：\(error)\n".utf8))
        return 5
    }
    print("已生成 \(pngPath)  \(pxW)×\(pxH) @\(Int(scale))x")
    return 0
}

let arguments = CommandLine.arguments
if let flag = arguments.firstIndex(of: "--snapshot") {
    // usage-widget --snapshot [json] [png] [scale] [padding]
    let fallbackJSON = NSString(string: "~/.local/share/usage-monitor/widget-data.json").expandingTildeInPath
    let jsonPath = flag + 1 < arguments.count ? arguments[flag + 1] : fallbackJSON
    let pngPath = flag + 2 < arguments.count ? arguments[flag + 2] : "usage-widget.png"
    let scale = flag + 3 < arguments.count ? CGFloat(Double(arguments[flag + 3]) ?? 2) : 2
    let padding = flag + 4 < arguments.count ? CGFloat(Double(arguments[flag + 4]) ?? 24) : 24
    exit(writeSnapshot(jsonPath: jsonPath, pngPath: pngPath, scale: scale, padding: padding))
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
