import asyncio
import argparse
import random
import os
import shutil
import time
from autologin import automate_google_login, load_accounts, dbg, log

async def delete_dola_account(page):
    log("Starting Dola account deletion process...", "INFO")
    try:
        # Click profile menu
        profile_btn = page.locator('div[class*="flex"] > div.cursor-pointer:has(img[alt="avatar"])').first
        if await profile_btn.count() == 0:
            profile_btn = page.locator('img[alt="avatar"]').first
        
        await profile_btn.click(force=True)
        await asyncio.sleep(1)

        # Click Settings
        settings_btn = page.locator('div:has-text("Settings")').last
        await settings_btn.click(force=True)
        await asyncio.sleep(1)

        # Click Account tab
        account_tab = page.locator('div.tab-item:has-text("Account")').first
        if await account_tab.count() > 0:
            await account_tab.click(force=True)
            await asyncio.sleep(1)

        # Click Delete Account
        delete_btn = page.locator('button:has-text("Delete Account")').first
        if await delete_btn.count() > 0:
            await delete_btn.click(force=True)
            await asyncio.sleep(1)
            
            # Confirm deletion
            confirm_btn = page.locator('button:has-text("Delete")').last
            await confirm_btn.click(force=True)
            await asyncio.sleep(3)
            log("Account successfully deleted!", "SUCCESS")
            return True
        else:
            log("Delete button not found.", "WARNING")
            return False
    except Exception as e:
        log(f"Deletion failed: {e}", "ERROR")
        return False

async def run_flow(accounts_file="accounts.txt", headless=True):
    accounts = load_accounts(accounts_file)
    if not accounts:
        log("No accounts loaded.", "ERR")
        return

    for acc in accounts:
        email = acc[0]
        password = acc[1]
        proxy = acc[2] if len(acc) > 2 else None
        
        log(f"--- Starting master flow for {email} ---", "INFO")
        
        # 1. Fresh IP and Fingerprint ( handled by proxy + CloakBrowser )
        # Delete old session to ensure fresh start if we want to test register again
        session_dir = f"./sessions/{email.replace('@', '_').replace('.', '_')}"
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir)
            log("Cleared old session to force fresh login and Captcha.", "INFO")
            
        # 2. Login (This triggers Google Auth + Captcha)
        log("Step 1: First login to delete old account...", "INFO")
        
        # We need to manually launch a context, go to dola, click the button, grab the URL, then use automate_google_login!
        # Because automate_google_login requires the actual Google OAuth URL.
        from cloakbrowser import launch_persistent_context_async
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
            
        page = await ctx.new_page()
        await page.goto("https://www.dola.com/chat/")
        await asyncio.sleep(2)
        
        login_btn = page.locator("text='log in'").first
        if await login_btn.count() > 0:
            await login_btn.click(force=True)
            await asyncio.sleep(2)
            
        google_btn = page.locator(
            'button:has-text("Continue with Google"), '
            '[class*="button-"]:has-text("Continue with Google"), '
            'button:has(img.size-24), '
            '[class*="button-"]:has(img.size-24)'
        ).first
        
        try:
            await google_btn.wait_for(state="visible", timeout=10000)
            async with page.expect_popup() as popup_info:
                await google_btn.click()
            popup = await popup_info.value
            oauth_url = popup.url
            await popup.close()
        except Exception as e:
            log(f"Failed to extract OAuth URL: {e}", "ERR")
            await ctx.close()
            continue
            
        await ctx.close()
        
        state = await automate_google_login(oauth_url, email, password, headless=headless, proxy=proxy, session_dir=session_dir)
        
        if not state.get("login_done", False):
            log(f"First login failed for {email}", "ERROR")
            continue
            
        # Wait for Dola to fully load
        await asyncio.sleep(5)
        
        # We need the browser instance to delete, but automate_google_login closes it.
        # So we should modify or just write a wrapper. 
        # Actually, let's use the persistent session we just created to do the deletion!
        log("Step 2: Re-opening session to delete the account...", "INFO")
        from cloakbrowser import launch_persistent_context_async
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
            
        page = await ctx.new_page()
        await page.goto("https://www.dola.com/chat/")
        await asyncio.sleep(5)
        
        deleted = await delete_dola_account(page)
        await ctx.close()
        
        if not deleted:
            log("Skipping to next account since deletion failed.", "WARNING")
            continue
            
        # 3. Register again (Login -> Birthday prompt)
        log("Step 3: Registering fresh account (Should hit Birthday Prompt)...", "INFO")
        
        ctx2 = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
            
        page2 = await ctx2.new_page()
        await page2.goto("https://www.dola.com/chat/")
        await asyncio.sleep(2)
        
        login_btn2 = page2.locator("text='log in'").first
        if await login_btn2.count() > 0:
            await login_btn2.click(force=True)
            await asyncio.sleep(2)
            
        google_btn2 = page2.locator(
            'button:has-text("Continue with Google"), '
            '[class*="button-"]:has-text("Continue with Google"), '
            'button:has(img.size-24), '
            '[class*="button-"]:has(img.size-24)'
        ).first
        
        try:
            await google_btn2.wait_for(state="visible", timeout=10000)
            async with page2.expect_popup() as popup_info:
                await google_btn2.click()
            popup2 = await popup_info.value
            oauth_url2 = popup2.url
            await popup2.close()
        except Exception as e:
            log(f"Failed to extract OAuth URL for Step 3: {e}", "ERR")
            await ctx2.close()
            continue
            
        await ctx2.close()
        
        state2 = await automate_google_login(oauth_url2, email, password, headless=headless, proxy=proxy, session_dir=session_dir)
        
        if state2.get("login_done", False):
            log(f"--- Flow completely successful for {email} ---", "SUCCESS")
        else:
            log(f"--- Flow failed during final registration for {email} ---", "ERROR")
            
        await asyncio.sleep(2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headful", action="store_true")
    args = parser.add_argument("--accounts", default="accounts.txt")
    args = parser.parse_args()
    asyncio.run(run_flow(args.accounts, headless=not args.headful))
