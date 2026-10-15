import UIKit
import Capacitor
import WebKit

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        window = UIWindow(windowScene: windowScene)
        window?.rootViewController = DoomsdayGlassBridgeViewController()
        window?.makeKeyAndVisible()

        SceneDelegateProxy.shared.scene(scene, willConnectTo: session, options: connectionOptions)
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        SceneDelegateProxy.shared.scene(scene, openURLContexts: URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        SceneDelegateProxy.shared.scene(scene, continue: userActivity)
    }
}

private final class WeakGlassMessageHandler: NSObject, WKScriptMessageHandler {
    weak var owner: DoomsdayGlassBridgeViewController?

    init(owner: DoomsdayGlassBridgeViewController) {
        self.owner = owner
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        owner?.userContentController(userContentController, didReceive: message)
    }
}

private final class DoomsdayGlassBridgeViewController: CAPBridgeViewController, WKScriptMessageHandler {
    private var glassTabBar: UIView?

    override func webView(with frame: CGRect, configuration: WKWebViewConfiguration) -> WKWebView {
        configuration.userContentController.add(WeakGlassMessageHandler(owner: self), name: "doomsdayGlassNav")
        let glassAvailable: Bool
        if #available(iOS 26.0, *) { glassAvailable = true } else { glassAvailable = false }
        let capabilitySource = "window.DoomsdayGlassNativeAvailable = " + (glassAvailable ? "true" : "false") + ";"
        let capability = WKUserScript(source: capabilitySource, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(capability)
        return super.webView(with: frame, configuration: configuration)
    }

    override func capacitorDidLoad() {
        super.capacitorDidLoad()
        guard #available(iOS 26.0, *) else { return }
        let bar = NativeGlassTabBar { [weak self] tab in
            guard ["home", "tasks", "messages", "profile"].contains(tab) else { return }
            let script = "window.doomsdayNativeSelectTab && window.doomsdayNativeSelectTab('\(tab)');"
            self?.webView?.evaluateJavaScript(script)
        }
        bar.isHidden = true
        view.addSubview(bar)
        glassTabBar = bar
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        if let glassTabBar { view.bringSubviewToFront(glassTabBar) }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard #available(iOS 26.0, *), message.name == "doomsdayGlassNav", let webView,
              let payload = message.body as? [String: Any] else { return }
        guard payload["visible"] as? Bool == true,
              let viewportValue = payload["viewportWidth"] as? NSNumber, viewportValue.doubleValue > 0,
              let frameData = payload["frame"] as? [String: Any],
              let itemFrameData = payload["itemFrames"] as? [[String: Any]],
              let tabs = payload["tabs"] as? [[String: String]],
              let selected = payload["selected"] as? Int,
              itemFrameData.count == tabs.count else {
            glassTabBar?.isHidden = true
            return
        }
        let scale = webView.bounds.width / CGFloat(viewportValue.doubleValue)
        func coordinate(_ key: String) -> CGFloat {
            guard let value = frameData[key] as? NSNumber else { return 0 }
            return CGFloat(value.doubleValue) * scale
        }
        let webViewOrigin = webView.convert(CGPoint(x: coordinate("x"), y: 0), to: view)
        let frameHeight = coordinate("height")
        let frame = CGRect(
            x: webViewOrigin.x,
            y: view.bounds.maxY - view.safeAreaInsets.bottom - 10 - frameHeight,
            width: coordinate("width"),
            height: frameHeight
        )
        let itemFrames = itemFrameData.map { data in
            CGRect(
                x: (data["x"] as? NSNumber).map { CGFloat($0.doubleValue) * scale } ?? 0,
                y: (data["y"] as? NSNumber).map { CGFloat($0.doubleValue) * scale } ?? 0,
                width: (data["width"] as? NSNumber).map { CGFloat($0.doubleValue) * scale } ?? 0,
                height: (data["height"] as? NSNumber).map { CGFloat($0.doubleValue) * scale } ?? 0
            )
        }
        guard let nativeBar = glassTabBar as? NativeGlassTabBar else { return }
        nativeBar.update(frame: frame, tabs: tabs, itemFrames: itemFrames, selectedIndex: selected)
        nativeBar.isHidden = frame.width <= 0 || frame.height <= 0
    }
}

@available(iOS 26.0, *)
private final class NativeGlassTabBar: UIView, UIGestureRecognizerDelegate {
    private let backgroundGlass = UIVisualEffectView(effect: UIGlassEffect(style: .regular))
    private let selectionGlass = UIVisualEffectView(effect: UIGlassEffect(style: .regular))
    private var items: [NativeGlassTabItem] = []
    private var tabs: [[String: String]] = []
    private var itemFrames: [CGRect] = []
    private var selectedIndex = 0
    private var dragStartX: CGFloat = 0
    private var dragStartPosition: CGFloat = 0
    private var dragPosition: CGFloat = 0
    private var dragVelocity: CGFloat = 0
    private let onSelect: (String) -> Void

    init(onSelect: @escaping (String) -> Void) {
        self.onSelect = onSelect
        super.init(frame: .zero)
        isAccessibilityElement = false
        backgroundColor = .clear

        backgroundGlass.isUserInteractionEnabled = false
        backgroundGlass.clipsToBounds = true
        backgroundGlass.layer.cornerCurve = .continuous
        backgroundGlass.layer.borderWidth = 0.8
        backgroundGlass.layer.borderColor = UIColor.white.withAlphaComponent(0.92).cgColor
        backgroundGlass.contentView.backgroundColor = UIColor.white.withAlphaComponent(0.28)
        addSubview(backgroundGlass)

        if let glassEffect = selectionGlass.effect as? UIGlassEffect {
            glassEffect.isInteractive = true
            selectionGlass.effect = glassEffect
        }
        selectionGlass.isUserInteractionEnabled = false
        selectionGlass.clipsToBounds = true
        selectionGlass.layer.cornerCurve = .continuous
        selectionGlass.layer.cornerRadius = 16
        selectionGlass.layer.borderWidth = 0.8
        selectionGlass.layer.borderColor = UIColor.white.withAlphaComponent(0.92).cgColor
        addSubview(selectionGlass)

        let pan = UIPanGestureRecognizer(target: self, action: #selector(handlePan(_:)))
        pan.delegate = self
        pan.cancelsTouchesInView = true
        addGestureRecognizer(pan)
        layer.shadowColor = UIColor.black.cgColor
        layer.shadowOpacity = 0.14
        layer.shadowRadius = 15
        layer.shadowOffset = CGSize(width: 0, height: 7)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func update(frame: CGRect, tabs newTabs: [[String: String]], itemFrames newItemFrames: [CGRect], selectedIndex newSelected: Int) {
        let frameChanged = !self.frame.equalTo(frame)
        let oldIds = tabs.map { $0["id"] ?? "" }
        let newIds = newTabs.map { $0["id"] ?? "" }
        tabs = newTabs
        let clampedSelection = max(0, min(newSelected, max(0, tabs.count - 1)))
        let selectionChanged = clampedSelection != selectedIndex
        selectedIndex = clampedSelection
        itemFrames = newItemFrames
        let itemsChanged = oldIds != newIds || items.count != newTabs.count
        if itemsChanged {
            items.forEach { $0.removeFromSuperview() }
            items = newTabs.map { tab in
                let item = NativeGlassTabItem()
                item.configure(tab: tab)
                item.addTarget(self, action: #selector(handleTap(_:)), for: .touchUpInside)
                addSubview(item)
                return item
            }
        } else {
            for (index, tab) in newTabs.enumerated() { items[index].configure(tab: tab) }
        }
        for (index, item) in items.enumerated() { item.setSelected(index == selectedIndex) }
        isHidden = false
        if !isTracking { self.frame = frame }
        setNeedsLayout()
        if selectionChanged {
            applySelection(selectedIndex, animated: true)
        } else if frameChanged || itemsChanged {
            dragPosition = CGFloat(selectedIndex)
            setNeedsLayout()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        backgroundGlass.frame = bounds
        backgroundGlass.layer.cornerRadius = bounds.height / 2
        for (index, item) in items.enumerated() {
            item.frame = itemFrames.indices.contains(index) ? itemFrames[index] : .zero
            bringSubviewToFront(item)
        }
        positionSelection(dragPosition, animated: false, velocity: isTracking ? dragVelocity : 0)
        selectionGlass.layer.cornerRadius = selectionGlass.bounds.height / 2
    }

    private var isTracking = false

    private func positionSelection(_ position: CGFloat, animated: Bool, velocity: CGFloat = 0) {
        guard !items.isEmpty, itemFrames.count == items.count else { return }
        let clamped = min(CGFloat(items.count - 1), max(0, position))
        let lowerIndex = Int(clamped.rounded(.down))
        let upperIndex = min(lowerIndex + 1, items.count - 1)
        let fraction = clamped - CGFloat(lowerIndex)
        let first = itemFrames[lowerIndex].insetBy(dx: 2, dy: 2)
        let second = itemFrames[upperIndex].insetBy(dx: 2, dy: 2)
        let base = CGRect(
            x: first.minX + (second.minX - first.minX) * fraction,
            y: first.minY + (second.minY - first.minY) * fraction,
            width: first.width + (second.width - first.width) * fraction,
            height: first.height + (second.height - first.height) * fraction
        )
        // Let the glass lag behind the finger and widen as it travels, like a soft lens.
        let travel = min(0.86, abs(velocity) / 1_000 * 0.72)
        let extraWidth = base.width * (0.08 + travel * 0.75)
        let trailingOffset = velocity > 0 ? -base.width * travel * 0.13 : (velocity < 0 ? base.width * travel * 0.13 : 0)
        let target = CGRect(
            x: base.minX - extraWidth / 2 + trailingOffset,
            y: base.minY - 1,
            width: base.width + extraWidth,
            height: base.height + 2
        ).intersection(bounds.insetBy(dx: 2, dy: 2))
        let apply = { self.selectionGlass.frame = target }
        if animated {
            UIView.animate(withDuration: 0.48, delay: 0, usingSpringWithDamping: 0.56, initialSpringVelocity: 0, options: [.beginFromCurrentState, .allowUserInteraction], animations: apply)
        } else {
            apply()
        }
    }

    private func applySelection(_ index: Int, animated: Bool) {
        guard !items.isEmpty else { return }
        selectedIndex = max(0, min(index, items.count - 1))
        for (itemIndex, item) in items.enumerated() { item.setSelected(itemIndex == selectedIndex) }
        dragPosition = CGFloat(selectedIndex)
        positionSelection(dragPosition, animated: animated)
    }

    @objc private func handleTap(_ sender: NativeGlassTabItem) {
        guard let index = items.firstIndex(of: sender), tabs.indices.contains(index) else { return }
        applySelection(index, animated: true)
        if let id = tabs[index]["id"] { onSelect(id) }
    }

    override func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        guard let pan = gestureRecognizer as? UIPanGestureRecognizer else { return true }
        let velocity = pan.velocity(in: self)
        return abs(velocity.x) > abs(velocity.y) * 1.15
    }

    @objc private func handlePan(_ pan: UIPanGestureRecognizer) {
        let point = pan.location(in: self)
        switch pan.state {
        case .began:
            isTracking = true
            dragStartX = point.x
            dragStartPosition = position(for: point.x)
            dragPosition = dragStartPosition
            dragVelocity = 0
            if let glassEffect = selectionGlass.effect as? UIGlassEffect { glassEffect.isInteractive = true }
        case .changed:
            dragPosition = position(for: dragStartX + (point.x - dragStartX))
            dragVelocity = pan.velocity(in: self).x
            positionSelection(dragPosition, animated: false, velocity: dragVelocity)
            let nearest = Int(dragPosition.rounded())
            if tabs.indices.contains(nearest) {
                selectedIndex = nearest
                for (index, item) in items.enumerated() { item.setSelected(index == nearest) }
            }
        case .ended, .cancelled, .failed:
            isTracking = false
            dragVelocity = 0
            let index = min(max(Int(dragPosition.rounded()), 0), max(items.count - 1, 0))
            applySelection(index, animated: true)
            if tabs.indices.contains(index), let id = tabs[index]["id"] { onSelect(id) }
        default:
            break
        }
    }

    private func position(for x: CGFloat) -> CGFloat {
        guard itemFrames.count > 1 else { return 0 }
        let centers = itemFrames.map(\.midX)
        if x <= centers[0] { return 0 }
        if x >= centers[centers.count - 1] { return CGFloat(centers.count - 1) }
        for index in 0..<(centers.count - 1) where x <= centers[index + 1] {
            let span = centers[index + 1] - centers[index]
            let fraction = span > 0 ? (x - centers[index]) / span : 0
            return CGFloat(index) + min(1, max(0, fraction))
        }
        return CGFloat(centers.count - 1)
    }
}

@available(iOS 26.0, *)
private final class NativeGlassTabItem: UIControl {
    private let iconView = UIImageView()
    private let titleLabel = UILabel()

    override init(frame: CGRect) {
        super.init(frame: frame)
        iconView.contentMode = .scaleAspectFit
        iconView.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 20, weight: .semibold)
        titleLabel.font = .systemFont(ofSize: 10, weight: .semibold)
        titleLabel.adjustsFontSizeToFitWidth = true
        titleLabel.minimumScaleFactor = 0.82
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.textAlignment = .center
        titleLabel.isUserInteractionEnabled = false
        iconView.isUserInteractionEnabled = false
        addSubview(iconView)
        addSubview(titleLabel)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        let iconSize: CGFloat = 22
        let textHeight: CGFloat = 13
        let contentHeight = iconSize + 2 + textHeight
        let top = max(4, (bounds.height - contentHeight) / 2)
        iconView.frame = CGRect(x: (bounds.width - iconSize) / 2, y: top, width: iconSize, height: iconSize)
        titleLabel.frame = CGRect(x: 2, y: iconView.frame.maxY + 2, width: max(0, bounds.width - 4), height: textHeight)
    }

    func configure(tab: [String: String]) {
        let symbols = ["home": "safari.fill", "tasks": "checklist", "messages": "bubble.left.and.bubble.right.fill", "profile": "person.crop.circle.fill"]
        iconView.image = UIImage(systemName: symbols[tab["id"] ?? ""] ?? "circle")
        titleLabel.text = tab["label"]
        accessibilityLabel = tab["label"]
        accessibilityTraits = .button
    }

    func setSelected(_ selected: Bool) {
        let color = selected
            ? UIColor(red: 0.79, green: 0.28, blue: 0.19, alpha: 1)
            : UIColor(red: 0.31, green: 0.33, blue: 0.31, alpha: 1)
        iconView.tintColor = color
        titleLabel.textColor = color
        accessibilityTraits = selected ? [.button, .selected] : .button
    }
}
