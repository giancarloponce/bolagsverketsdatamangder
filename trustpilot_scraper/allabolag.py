import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.allabolag.se/")
    page.get_by_role("button", name="GODKÄNN").click()
    page.get_by_role("combobox", name="Sök efter företag,").click()
    page.get_by_role("combobox", name="Sök efter företag,").fill("fredrik tillkvist")
    page.get_by_role("combobox", name="Sök efter företag,").press("Enter")
    page.get_by_role("tab", name="Befattningshavare").click()
    page.get_by_text("Fredrik A D Tillkvist EkmanMan, född 19865 befattningar i svenska företag").click()
    page.get_by_text("Fredrik A D Tillkvist EkmanMan, född 19865 befattningar i svenska företag").click()
    page.get_by_role("link", name="Fredrik A D Tillkvist Ekman").click()
    page.get_by_role("link", name="Visa alla befattningar").click()
    page.get_by_role("row", name="Verkställande direktör").get_by_role("link").click()
    page.get_by_role("link", name="Se alla nyckeltal").click()
    page.get_by_role("rowheader", name="Avkastning totalt kapital i %").hover()
    page.get_by_role("cell", name="54,7").hover()
    page.get_by_role("tab", name="Händelser").click()
    page.get_by_role("combobox", name="Sök efter företag,").click()
    page.get_by_role("combobox", name="Sök efter företag,").dblclick()
    page.get_by_role("combobox", name="Sök efter företag,").click()
    page.get_by_role("combobox", name="Sök efter företag,").dblclick()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
