from playwright.sync_api import sync_playwright, expect
import os
import time

def run(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    # 1. Go to Admin (should redirect to login with next=/admin/)
    page.goto("http://127.0.0.1:8000/admin/")

    # 2. Login
    # Wait for selectors to appear
    page.wait_for_selector('input[name="username"]')
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', "password")

    # Click submit.
    # Unfold might wrap the button, so let's look for a button with type submit
    page.click('button[type="submit"]')

    # Wait for navigation
    page.wait_for_load_state("networkidle")

    # 3. Check Dashboard
    # Just take a screenshot.
    # We might need to wait a bit for JS to render charts/widgets if any
    time.sleep(2)

    os.makedirs("/home/jules/verification", exist_ok=True)
    page.screenshot(path="/home/jules/verification/dashboard.png")
    print("Dashboard screenshot taken.")

    # 4. Navigate to Cases
    # The sidebar link for "Toate Dosarele"
    try:
        # Depending on Unfold, the sidebar text might be "Toate Dosarele" inside a span or div.
        # Let's try to match text.
        page.click("text=Toate Dosarele")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.screenshot(path="/home/jules/verification/cases_list.png")
        print("Cases list screenshot taken.")
    except Exception as e:
        print(f"Could not navigate to Cases: {e}")
        # Take a screenshot of where we are to debug
        page.screenshot(path="/home/jules/verification/debug_nav.png")

    browser.close()

if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
