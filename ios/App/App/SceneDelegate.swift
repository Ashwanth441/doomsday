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
        guard #available(iOS 26.0, *), let webView else { return }
        let bar = NativeGlassTabBar { [weak self] tab in
            guard ["home", "tasks", "messages", "profile"].contains(tab) else { return }
            self?.webView?.evaluateJavaScript("window.doomsdayNativeSelectTab && window.doomsdayNativeSelectTab('\\(tab)')")
        }
        bar.isHidden = true
        webView.addSubview(bar)
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
              let tabs = payload["tabs"] as? [[String: String]],
              let selected = payload["selected"] as? Int else {
            glassTabBar?.isHidden = true
            return
        }
        let scale = webView.bounds.width / CGFloat(viewportValue.doubleValue)
        func coordinate(_ key: String) -> CGFloat {
            guard let value = frameData[key] as? NSNumber else { return 0 }
            return CGFloat(value.doubleValue) * scale
        }
        let frame = CGRect(
            x: coordinate("x"),
            y: coordinate("y"),
            width: coordinate("width"),
            height: coordinate("height")
        )
        guard let nativeBar = glassTabBar as? NativeGlassTabBar else { return }
        nativeBar.update(frame: frame, tabs: tabs, selectedIndex: selected)
        nativeBar.isHidden = frame.width <= 0 || frame.height <= 0
    }
}

@available(iOS 26.0, *)
private final class NativeGlassTabBar: UIView, UIGestureRecognizerDelegate {
    private let glassContainer = UIVisualEffectView(effect: UIGlassContainerEffect())
    private let backgroundGlass = UIVisualEffectView(effect: UIGlassEffect(style: .regular))
    private let selectionGlass = UIVisualEffectView(effect: UIGlassEffect(style: .regular))
    private var items: [NativeGlassTabItem] = []
    private var tabs: [[String: String]] = []
    private var selectedIndex = 0
    private var dragStartX: CGFloat = 0
    private var dragStartPosition: CGFloat = 0
    private var dragPosition: CGFloat = 0
    private let onSelect: (String) -> Void

    init(onSelect: @escaping (String) -> Void) {
        self.onSelect = onSelect
        super.init(frame: .zero)
        isAccessibilityElement = false
        backgroundColor = .clear

        if let glassEffect = glassContainer.effect as? UIGlassContainerEffect {
            glassEffect.spacing = 18
            glassContainer.effect = glassEffect
        }
        glassContainer.frame = bounds
        glassContainer.backgroundColor = .clear
        addSubview(glassContainer)

        backgroundGlass.isUserInteractionEnabled = false
        backgroundGlass.clipsToBounds = true
        backgroundGlass.layer.cornerCurve = .continuous
        backgroundGlass.layer.cornerRadius = 23
        glassContainer.contentView.addSubview(backgroundGlass)

        if let glassEffect = selectionGlass.effect as? UIGlassEffect {
            glassEffect.isInteractive = true
            selectionGlass.effect = glassEffect
        }
        selectionGlass.isUserInteractionEnabled = false
        selectionGlass.clipsToBounds = true
        selectionGlass.layer.cornerCurve = .continuous
        selectionGlass.layer.cornerRadius = 18
        selectionGlass.layer.borderWidth = 0.75
        selectionGlass.layer.borderColor = UIColor.white.withAlphaComponent(0.8).cgColor
        glassContainer.contentView.addSubview(selectionGlass)

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

    func update(frame: CGRect, tabs newTabs: [[String: String]], selectedIndex newSelected: Int) {
        let frameChanged = !self.frame.equalTo(frame)
        let oldIds = tabs.map { $0["id"] ?? "" }
        let newIds = newTabs.map { $0["id"] ?? "" }
        tabs = newTabs
        let clampedSelection = max(0, min(newSelected, max(0, tabs.count - 1)))
        let selectionChanged = clampedSelection != selectedIndex
        selectedIndex = clampedSelection
        let itemsChanged = oldIds != newIds || items.count != newTabs.count
        if itemsChanged {
            items.forEach { $0.removeFromSuperview() }
            items = newTabs.map { tab in
                let item = NativeGlassTabItem()
                item.translatesAutoresizingMaskIntoConstraints = false
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
        if selectionChanged {
            applySelection(selectedIndex, animated: true)
        } else if frameChanged || itemsChanged {
            dragPosition = CGFloat(selectedIndex)
            setNeedsLayout()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        glassContainer.frame = bounds
        backgroundGlass.frame = bounds
        let count = CGFloat(max(items.count, 1))
        let width = bounds.width / count
        for (index, item) in items.enumerated() {
            item.frame = CGRect(x: CGFloat(index) * width, y: 1, width: width, height: bounds.height - 2)
            bringSubviewToFront(item)
        }
        positionSelection(dragPosition, animated: false)
    }

    private var isTracking = false

    private func positionSelection(_ position: CGFloat, animated: Bool) {
        guard !items.isEmpty else { return }
        let width = bounds.width / CGFloat(items.count)
        let target = CGRect(x: 4 + position * width, y: 4, width: max(0, width - 8), height: max(0, bounds.height - 8))
        let apply = { self.selectionGlass.frame = target }
        if animated {
            UIView.animate(withDuration: 0.52, delay: 0, usingSpringWithDamping: 0.76, initialSpringVelocity: 0, options: [.beginFromCurrentState, .allowUserInteraction], animations: apply)
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
            dragStartPosition = CGFloat(selectedIndex)
            dragPosition = dragStartPosition
            if let glassEffect = selectionGlass.effect as? UIGlassEffect { glassEffect.isInteractive = true }
        case .changed:
            let cellWidth = bounds.width / CGFloat(max(items.count, 1))
            dragPosition = min(CGFloat(max(items.count - 1, 0)), max(0, dragStartPosition + (point.x - dragStartX) / cellWidth))
            positionSelection(dragPosition, animated: false)
            let nearest = Int(dragPosition.rounded())
            if tabs.indices.contains(nearest) {
                selectedIndex = nearest
                for (index, item) in items.enumerated() { item.setSelected(index == nearest) }
            }
        case .ended, .cancelled, .failed:
            isTracking = false
            let index = min(max(Int(dragPosition.rounded()), 0), max(items.count - 1, 0))
            applySelection(index, animated: true)
            if tabs.indices.contains(index), let id = tabs[index]["id"] { onSelect(id) }
        default:
            break
        }
    }
}

@available(iOS 26.0, *)
private final class NativeGlassTabItem: UIControl {
    private let iconView = UIImageView()
    private let titleLabel = UILabel()
    private let stack = UIStackView()

    override init(frame: CGRect) {
        super.init(frame: frame)
        stack.axis = .vertical
        stack.alignment = .center
        stack.distribution = .fill
        stack.spacing = 2
        iconView.contentMode = .scaleAspectFit
        iconView.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 16, weight: .semibold)
        titleLabel.font = .systemFont(ofSize: 9, weight: .semibold)
        titleLabel.adjustsFontSizeToFitWidth = true
        titleLabel.minimumScaleFactor = 0.82
        titleLabel.lineBreakMode = .byTruncatingTail
        stack.addArrangedSubview(iconView)
        stack.addArrangedSubview(titleLabel)
        stack.isUserInteractionEnabled = false
        addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 2),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -2)
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func configure(tab: [String: String]) {
        let symbols = ["home": "safari.fill", "tasks": "checklist", "messages": "bubble.left.and.bubble.right.fill", "profile": "person.crop.circle.fill"]
        iconView.image = UIImage(systemName: symbols[tab["id"] ?? ""] ?? "circle")
        titleLabel.text = tab["label"]
        accessibilityLabel = tab["label"]
        accessibilityTraits = .button
    }

    func setSelected(_ selected: Bool) {
        let color = selected ? UIColor(red: 0.79, green: 0.28, blue: 0.19, alpha: 1) : UIColor.secondaryLabel.withAlphaComponent(0.82)
        iconView.tintColor = color
        titleLabel.textColor = color
        accessibilityTraits = selected ? [.button, .selected] : .button
    }
}
