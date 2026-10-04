import XCTest

/// Taps the real root/menu/list/detail views with no pairing, network or decision.
@MainActor
final class ApprovalNavigationTests: XCTestCase {
    func testAssistantTabsPresenceAndConnectionsKeepExistingDestinations() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--home-preview", "--approval-navigation-preview"]
        app.launch(); defer { app.terminate() }
        XCTAssertTrue(app.tabBars.buttons["Chat"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.tabBars.buttons["Aufgaben"].exists)
        XCTAssertTrue(app.tabBars.buttons["Überblick"].exists)
        XCTAssertTrue(app.tabBars.buttons["Ideen"].exists)
        XCTAssertTrue(app.tabBars.buttons["Bibliothek"].exists)
        let homeImage = XCTAttachment(screenshot: app.screenshot())
        homeImage.name = "assistant-chat"; homeImage.lifetime = .keepAlways; add(homeImage)
        app.buttons["chat.presence"].tap()
        XCTAssertTrue(app.navigationBars["Verlauf"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["chat.voice.end"].exists, "Avatar must not start microphone")
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["home.more"].tap()
        app.buttons["Verbindungen"].tap()
        XCTAssertTrue(app.navigationBars["Verbindungen"].waitForExistence(timeout: 5))
        let connectionsImage = XCTAttachment(screenshot: app.screenshot())
        connectionsImage.name = "assistant-connections"; connectionsImage.lifetime = .keepAlways; add(connectionsImage)
        app.buttons["connections.gmail"].tap()
        XCTAssertTrue(app.navigationBars["Gmail"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Stand nicht bestätigt"].exists)
        XCTAssertFalse(app.buttons["Verbinden"].exists, "No fictitious mobile OAuth action")
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["connections.contacts"].tap()
        XCTAssertTrue(app.navigationBars["Kontakte"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["contacts.sync"].exists)
        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["Aufgaben"].tap()
        XCTAssertTrue(app.navigationBars["Aufgaben"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Regelmäßige Aufgaben"].exists)
        app.tabBars.buttons["Chat"].tap()
        XCTAssertTrue(app.navigationBars["Verbindungen"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["home.speak"].exists)
    }

    func testIdeasPrepareDraftWithoutSendingAndKeepItAcrossAllTabs() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--home-preview", "--approval-navigation-preview"]
        app.launch(); defer { app.terminate() }
        XCTAssertTrue(app.tabBars.buttons["Ideen"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Ideen"].tap()
        app.buttons["ideas.day"].tap()
        XCTAssertTrue(app.buttons["home.speak"].waitForExistence(timeout: 5))
        let draft = app.descendants(matching: .any)["chat.text"].firstMatch
        XCTAssertTrue((draft.value as? String ?? "").contains("heutigen Tag"))
        for title in ["Überblick", "Aufgaben", "Bibliothek", "Ideen"] {
            app.tabBars.buttons[title].tap()
            XCTAssertTrue(app.navigationBars[title].waitForExistence(timeout: 5))
        }
        app.buttons["ideas.research"].tap()
        XCTAssertTrue(app.staticTexts["ideas.notice"].exists)
        app.tabBars.buttons["Chat"].tap()
        XCTAssertTrue((draft.value as? String ?? "").contains("heutigen Tag"), "An idea must never replace another draft")
        XCTAssertFalse(app.buttons["chat.voice.end"].exists)
        let image = XCTAttachment(screenshot: app.screenshot())
        image.name = "five-tabs-draft-preserved"; image.lifetime = .keepAlways; add(image)
    }

    func testPushTapAtColdLaunchOpensInboxAndIsConsumedOnce() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--home-preview", "--approval-navigation-preview", "--push-navigation-preview"]
        app.launch(); defer { app.terminate() }
        XCTAssertTrue(app.navigationBars["Hinweise"].waitForExistence(timeout: 10))
        let notice = app.buttons.containing(.staticText, identifier: "Synthetischer Navigationshinweis").firstMatch
        XCTAssertTrue(notice.waitForExistence(timeout: 5))
        notice.tap()
        XCTAssertTrue(app.navigationBars["Hinweis"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 5))
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Hinweise"].exists)
    }
    func testPendingApprovalOpensAndReturnsWithoutGrantingAuthority() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--home-preview", "--approval-navigation-preview"]
        app.launch()
        defer { app.terminate() }
        for _ in 0..<2 {
            XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 10))
            app.buttons["home.more"].tap()
            app.buttons["Freigaben (1)"].tap()
            XCTAssertTrue(app.navigationBars["Freigaben"].waitForExistence(timeout: 5))
            let row = app.buttons.containing(.staticText, identifier: "Synthetische Mailfreigabe").firstMatch
            XCTAssertTrue(row.waitForExistence(timeout: 5))
            row.tap()
            XCTAssertTrue(app.navigationBars["Freigabe"].waitForExistence(timeout: 5),
                          "A tap on a pending approval must open its actual detail page")
            XCTAssertTrue(app.staticTexts["Nicht verifizierbar"].waitForExistence(timeout: 5),
                          "An unsigned synthetic row must never grant authority")
            XCTAssertFalse(app.buttons["Mit Face ID freigeben"].exists)
            XCTAssertFalse(app.buttons["Ablehnen"].exists)
            app.navigationBars.buttons.firstMatch.tap()
            XCTAssertTrue(app.navigationBars["Freigaben"].waitForExistence(timeout: 5))
            app.navigationBars.buttons.firstMatch.tap()
        }
        app.buttons["home.more"].tap()
        app.buttons["Hinweise"].tap()
        XCTAssertTrue(app.navigationBars["Hinweise"].waitForExistence(timeout: 5))
        let notice = app.buttons.containing(.staticText, identifier: "Synthetischer Navigationshinweis").firstMatch
        XCTAssertTrue(notice.waitForExistence(timeout: 5)); notice.tap()
        XCTAssertTrue(app.navigationBars["Hinweis"].waitForExistence(timeout: 5),
                      "Other existing detail types must share the navigation path")
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Hinweise"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 5))
    }
    func testEverydaySetupIsReachableWithoutCreatingAnOrder() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--home-preview", "--approval-navigation-preview"]
        app.launch(); defer { app.terminate() }
        XCTAssertTrue(app.buttons["home.more"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Aufgaben"].tap()
        XCTAssertTrue(app.buttons["Im Chat beauftragen"].exists)
        XCTAssertTrue(app.navigationBars["Aufgaben"].waitForExistence(timeout: 5))
        app.buttons["Regelmäßige Aufgaben"].tap()
        XCTAssertTrue(app.navigationBars["Geplant"].waitForExistence(timeout: 5))
        app.buttons["Täglichen Überblick einrichten"].tap()
        XCTAssertTrue(app.navigationBars["Tagesüberblick"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Mit Face ID einrichten"].exists)
        XCTAssertFalse(app.buttons["Mit Face ID einrichten"].isEnabled)
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["An Mailantwort erinnern"].tap()
        XCTAssertTrue(app.navigationBars["An Antwort erinnern"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["Empfänger oder Betreff"].exists)
        XCTAssertFalse(app.buttons["Gesendete Mails suchen"].isEnabled)
    }

}
