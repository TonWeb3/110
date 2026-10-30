"""
Browser-based authenticator for Quotex using undetected_chromedriver.

Why this exists:
Cloudflare's Turnstile / Managed Challenge on Quotex's login endpoint
(https://quotex.com/pt/sign-in/modal/) blocks raw HTTP requests from libraries
like httpx or requests with HTTP 403.

This module launches a real, undetected Chrome browser to complete the login,
solve Cloudflare challenges, handle email 2FA/PIN, and extract the session
cookies, token (SSID), and User-Agent.

Once saved to session.json, the browser closes immediately. The trading bot
then runs 100% headless 24/7 over high-speed WebSocket using cf_transport.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple, Any

logger = logging.getLogger(__name__)

SESSION_FILE = Path("session.json")


def save_session_data(email: str, cookies: str, token: str, user_agent: str) -> bool:
    """Save the extracted session directly to session.json."""
    try:
        data = {}
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[email] = {
            "cookies": cookies,
            "token": token,
            "user_agent": user_agent,
        }
        SESSION_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        logger.info(f"Session successfully saved to {SESSION_FILE} for {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to write session.json: {e}")
        return False


def login_with_browser(
    email: str,
    password: str,
    host: str = "quotex.com",
    proxy: Optional[str] = None,
    otp_callback: Any = None,
    headless: bool = False,
    timeout: int = 120,
) -> Tuple[bool, str]:
    """
    Launch undetected_chromedriver, log in to Quotex, extract session data,
    and save to session.json.
    """
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError as e:
        msg = f"Browser automation dependencies missing: {e}. Run: pip install undetected-chromedriver selenium"
        logger.error(msg)
        return False, msg

    target_host = host or "quotex.com"
    sign_in_url = f"https://{target_host}/en/sign-in"

    logger.info(f"Starting browser authenticator for {email} on {sign_in_url}...")

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,800")
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    driver = None
    try:
        driver = uc.Chrome(options=options, headless=headless)
        driver.set_page_load_timeout(45)
        logger.info(f"Navigating to {sign_in_url}...")
        driver.get(sign_in_url)

        start_time = time.time()
        # Phase 1: Wait for Cloudflare verification to pass
        while time.time() - start_time < timeout:
            title = driver.title.strip()
            # Check for region block
            if "We're sorry" in title or "not available in your region" in driver.page_source.lower():
                err_msg = (
                    "Quotex is geo-blocked for this IP address (e.g. Netherlands/US/EU). "
                    "Please enable a VPN or configure a proxy in Settings."
                )
                logger.error(err_msg)
                return False, err_msg

            # Wait out "Just a moment..." challenge
            if "Just a moment" in title or "Attention Required" in title:
                logger.info("Cloudflare verification in progress... waiting...")
                time.sleep(2)
                continue

            # Check if email input is ready
            email_inputs = driver.find_elements(By.NAME, "email")
            if email_inputs and email_inputs[0].is_displayed():
                logger.info("Sign-in form found. Filling credentials...")
                break

            # If already logged in (e.g., from browser cache/cookies)
            if "trade" in driver.current_url:
                logger.info("Already on trade page!")
                break

            time.sleep(1)

        # Phase 2: Enter credentials if on sign-in page
        if "trade" not in driver.current_url:
            try:
                driver.execute_script(
                    'document.getElementsByName("email")[0].value = arguments[0];', email
                )
                driver.execute_script(
                    'document.getElementsByName("password")[0].value = arguments[0];', password
                )
                # Submit login form
                driver.execute_script(
                    'var f = document.querySelector("#tab-1 form") || document.querySelector("form"); if(f) f.submit();'
                )
                logger.info("Credentials submitted. Waiting for authentication...")
            except Exception as e:
                logger.warning(f"Error submitting form via script: {e}")

        # Phase 3: Check for 2FA / Email PIN
        pin_submitted = False
        login_start = time.time()
        while time.time() - login_start < timeout:
            # Check for PIN input
            code_inputs = (
                driver.find_elements(By.NAME, "code")
                or driver.find_elements(By.NAME, "keep_code")
            )
            if code_inputs and code_inputs[0].is_displayed() and not pin_submitted:
                logger.info("Quotex sent an email PIN verification code.")
                pin_code = None
                if otp_callback:
                    logger.info("Requesting PIN from user via dashboard modal...")
                    try:
                        if asyncio.iscoroutinefunction(otp_callback):
                            # Running in thread, call coroutine threadsafe if event loop available
                            try:
                                from bot.server import run_sync
                                pin_code = run_sync(otp_callback("Enter PIN sent to email"), timeout=180)
                            except Exception:
                                pin_code = None
                        else:
                            pin_code = otp_callback("Enter PIN sent to email")
                    except Exception as e:
                        logger.warning(f"OTP callback failed: {e}")

                if pin_code:
                    code_inputs[0].clear()
                    code_inputs[0].send_keys(str(pin_code).strip())
                    submit_btns = driver.find_elements(By.XPATH, "//button[@type='submit']")
                    if submit_btns:
                        submit_btns[0].click()
                    pin_submitted = True
                    logger.info("Submitted PIN verification code.")

            # Phase 4: Check if reached /trade or token is ready
            token = driver.execute_script(
                "return (window.settings && window.settings.token) ? window.settings.token : '';"
            )
            if token or "trade" in driver.current_url:
                logger.info("Successfully authenticated on Quotex!")
                time.sleep(2)  # Allow cookies to settle
                break

            time.sleep(1)

        # Phase 5: Extract Session Data
        cookies = driver.get_cookies()
        cookie_string = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        user_agent = driver.execute_script("return navigator.userAgent;") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        token = driver.execute_script(
            "return (window.settings && window.settings.token) ? window.settings.token : '';"
        )
        if not token:
            # Fallback regex extraction from page source
            m = re.search(r'window\.settings\s*=\s*(\{.*?\});', driver.page_source)
            if m:
                try:
                    data = json.loads(m.group(1))
                    token = data.get("token")
                except Exception:
                    pass

        if not token:
            # Look in localStorage
            token = driver.execute_script("return window.localStorage.getItem('token') || '';")

        if not cookie_string or not token:
            msg = "Login timed out or credentials invalid — could not obtain session token."
            logger.error(msg)
            return False, msg

        # Save session
        save_session_data(email, cookie_string, token, user_agent)
        logger.info(f"Browser authentication successful! Token length: {len(token)}")
        return True, "Login successful. Session cached for headless trading."

    except Exception as e:
        msg = f"Browser login failed: {e}"
        logger.error(msg)
        return False, msg
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def async_login_with_browser(
    email: str,
    password: str,
    host: str = "quotex.com",
    proxy: Optional[str] = None,
    otp_callback: Any = None,
    headless: bool = False,
    timeout: int = 120,
) -> Tuple[bool, str]:
    """Asynchronous wrapper for login_with_browser."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: login_with_browser(
            email=email,
            password=password,
            host=host,
            proxy=proxy,
            otp_callback=otp_callback,
            headless=headless,
            timeout=timeout,
        ),
    )
