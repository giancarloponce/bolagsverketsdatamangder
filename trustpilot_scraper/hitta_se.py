import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.hitta.se/")
    page.get_by_role("button", name="Godkänn alla").click()
    page.locator("[data-test=\"autocomplete-input\"]").click()
    page.get_by_text("✕").click()
    page.locator("[data-test=\"autocomplete-input\"]").fill("svenska markavtal")
    page.locator("[data-test=\"autocomplete-input\"]").press("Enter")
    page.locator("[data-test=\"copy-to-clipboard\"]").nth(3).click()
    page.goto("https://www.hitta.se/verksamhet/svenska-markavtal-ab-hcpprzkcm?vad=svenska%20markavtal")
    page.locator("[data-test=\"company-sidebar\"] div").filter(has_text=re.compile(r"^070-740 16 77$")).click()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
