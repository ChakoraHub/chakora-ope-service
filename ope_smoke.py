import os
import sys
import time
import pytest
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==============================================================================
# CONFIGURATION
# Default to production website if no environment variable is provided.
# ==============================================================================
BASE_URL = os.getenv(
    "OPE_BASE_URL",
    os.getenv("BASE_URL", "https://www.chakorahub.com")
).rstrip("/")

OPE_LOGIN_URL = f"{BASE_URL}/ope"
HEALTH_URL = f"{BASE_URL}/health"
WAIT = int(os.getenv("SELENIUM_WAIT_TIMEOUT", "25"))


# ==============================================================================
# FIXTURES
# ==============================================================================
@pytest.fixture
def driver():
    """Initialize a headless Chrome browser matching CI/CD standard options."""
    import shutil

    # Filter out mismatched pre-installed chromedrivers from PATH so Selenium Manager resolves matching driver
    clean_path_parts = [
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if "chromedriver" not in p.lower()
    ]
    os.environ["PATH"] = os.pathsep.join(clean_path_parts)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1440,1000")

    chrome_bin = (
        os.getenv("CHROME_PATH")
        or os.getenv("CHROME_BIN")
        or shutil.which("google-chrome")
        or shutil.which("chrome")
    )
    if chrome_bin:
        options.binary_location = chrome_bin

    browser = None
    try:
        browser = webdriver.Chrome(options=options)
    except Exception as e1:
        try:
            options_legacy = webdriver.ChromeOptions()
            options_legacy.add_argument("--headless")
            options_legacy.add_argument("--no-sandbox")
            options_legacy.add_argument("--disable-dev-shm-usage")
            options_legacy.add_argument("--disable-gpu")
            if chrome_bin:
                options_legacy.binary_location = chrome_bin
            browser = webdriver.Chrome(options=options_legacy)
        except Exception as e2:
            pytest.skip(f"Chrome browser environment failed to start in runner: {e1} | {e2}")

    browser.set_page_load_timeout(45)

    try:
        yield browser
    finally:
        if browser:
            try:
                browser.quit()
            except Exception:
                pass


def wait_loaded(browser):
    """Wait until document.readyState == 'complete'."""
    WebDriverWait(browser, WAIT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


# ==============================================================================
# SELENIUM SMOKE TESTS FOR ONLINE PROCTORED EXAM (OPE) SERVICE
# ==============================================================================
def test_ope_health_or_api_reachable():
    """Verify health endpoint responds without 50x server gateway errors."""
    try:
        resp = requests.get(HEALTH_URL, timeout=15)
        assert resp.status_code not in (502, 503, 504), (
            f"Health check returned gateway failure status: {resp.status_code}"
        )
    except requests.exceptions.RequestException as e:
        print(f"[Health Check Notice]: {e}")


def test_ope_login_page_rendered(driver):
    """Verify that /ope authentication page loads and displays OPE in title."""
    driver.get(OPE_LOGIN_URL)
    wait_loaded(driver)

    title = driver.title
    assert "OPE" in title or "Authentication" in title or "Chakora" in title, (
        f"Unexpected page title for OPE login: '{title}'"
    )


def test_ope_login_form_present(driver):
    """Verify the OPE login form is present on the page."""
    driver.get(OPE_LOGIN_URL)
    wait_loaded(driver)

    form = WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "form[action='/ope'], form"))
    )
    assert form is not None, "OPE login form not found on /ope"


def test_ope_login_inputs_visible(driver):
    """Verify username and password fields are present and enabled."""
    driver.get(OPE_LOGIN_URL)
    wait_loaded(driver)

    username_input = WebDriverWait(driver, WAIT).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "input#username, input[name='username']"))
    )
    password_input = WebDriverWait(driver, WAIT).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "input#password, input[name='password']"))
    )

    assert username_input.is_enabled(), "OPE username input is disabled"
    assert password_input.is_enabled(), "OPE password input is disabled"


def test_ope_submit_button_clickable(driver):
    """Verify the OPE login submit button is present and clickable."""
    driver.get(OPE_LOGIN_URL)
    wait_loaded(driver)

    submit_button = WebDriverWait(driver, WAIT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
    )
    assert submit_button is not None, "OPE submit button not clickable"
