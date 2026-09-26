import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.trustpilot.com/")
    page.get_by_role("button", name="Accept all").click()
    page.get_by_role("searchbox", name="search input field").click()
    page.get_by_role("searchbox", name="search input field").fill("prioritet finans")
    page.get_by_role("searchbox", name="search input field").press("Enter")
    page.get_by_role("button", name="Search company or category").click()
    page.get_by_role("link", name="Prioritet Finans AB prioritet").click()
    page.get_by_role("button", name="See all 592 reviews").click()
    page.get_by_role("link", name="Next page").click()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
