import asyncio
import argparse
import random
import os
import shutil
import time
from autologin import automate_google_login, load_accounts, dbg, log
from dola_login import run as dola_run, click_by_text

async def delete_dola_account(page):
    log("Starting Dola account deletion process...", "INFO")
    try:
        profile_btn = page.locator('div[class*="flex"] > div.cursor-pointer:has(img[alt="avatar"])').first
        if await profile_btn.count() == 0:
            profile_btn = page.locator('img[alt="avatar"]').first
        
        await profile_btn.click(force=True)
        await asyncio.sleep(1)

        settings_btn = page.locator('div:has-text("Settings")').last
        await settings_btn.click(force=True)
        await asyncio.sleep(1)

        account_tab = page.locator('div.tab-item:has-text("Account")').first
        if await account_tab.count() > 0:
            await account_tab.click(force=True)
            await asyncio.sleep(1)

        delete_btn = page.locator('button:has-text("Delete Account")').first
        if await delete_btn.count() > 0:
            await delete_btn.click(force=True)
            await asyncio.sleep(1)
            
            confirm_btn = page.locator('button:has-text("Delete")').last
            await confirm_btn.click(force=True)
            await asyncio.sleep(3)
            log("Account successfully deleted!", "SUCCESS")
            return True
        return False
    except Exception as e:
        return False

async def get_google_auth_url(ctx, page):
    popups = []
    ctx.on("page", lambda pg: popups.append(pg))

    await page.goto("https://dola.com", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(5)

    await page.evaluate(
        "() => { for (const el of document.querySelectorAll('button'))"
        " { if ((el.textContent||'').trim().toLowerCase()==='ok') { el.click(); return; } } }")
    await asyncio.sleep(1)

    async def modal_open():
        return await page.evaluate("() => document.body.innerText.includes('Unlock More Features')")

    for attempt in range(8):
        if await modal_open():
            break
        await click_by_text(page, ["log in", "login", "sign in"])
        await asyncio.sleep(2)

    google = page.locator(
        'button:has-text("Continue with Google"), '
        '[class*="button-"]:has-text("Continue with Google"), '
        'button:has(img.size-24), '
        '[class*="button-"]:has(img.size-24)'
    ).first
    
    try:
        await google.wait_for(state="visible", timeout=10000)
        await google.click()
    except Exception:
        pass

    def google_page():
        for pg in [page] + popups:
            try:
                if "accounts.google.com" in (pg.url or ""):
                    return pg
            except Exception:
                pass
        return None

    gpg = None
    for _ in range(25):
        gpg = google_page()
        if gpg:
            break
        await asyncio.sleep(1)
    
    if gpg:
        return gpg.url
    return None

async def run_flow(accounts_file="accounts.txt", headless=True):
    accounts = load_accounts(accounts_file)
    if not accounts: return

    for acc in accounts:
        email = acc[0]
        password = acc[1]
        proxy = acc[2] if len(acc) > 2 else None
        
        session_dir = f"/home/azureuser/bulk-Video-generation/app/sessions/{email.replace('@', '_').replace('.', '_')}"
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir)
            log("Cleared old session for fresh start.", "INFO")
            
        from cloakbrowser import launch_persistent_context_async
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
            
        page = await ctx.new_page()
        oauth_url = await get_google_auth_url(ctx, page)
        
        if not oauth_url:
            log("Failed to get OAuth URL.", "ERR")
            await ctx.close()
            continue
            
        await ctx.close()
        
        log("Step 1: First login...", "INFO")
        state = await automate_google_login(oauth_url, email, password, headless=headless, proxy=proxy, session_dir=session_dir)
        
        if not state.get("login_done", False):
            continue
            
        log("Step 2: Deleting account...", "INFO")
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
            
        page = await ctx.new_page()
        await page.goto("https://www.dola.com/chat/")
        await asyncio.sleep(5)
        
        deleted = await delete_dola_account(page)
        await ctx.close()
        
        if not deleted: continue
            
        log("Step 3: Registering fresh account...", "INFO")
        ctx = await launch_persistent_context_async(
            user_data_dir=session_dir, headless=headless, proxy=proxy,
            viewport={"width": 1280, "height": 720}, locale="en-US", humanize=True)
        page = await ctx.new_page()
        oauth_url2 = await get_google_auth_url(ctx, page)
        await ctx.close()
        
        if not oauth_url2: continue
        
        state2 = await automate_google_login(oauth_url2, email, password, headless=headless, proxy=proxy, session_dir=session_dir)
        if state2.get("login_done", False):
            log(f"--- Flow completely successful for {email} ---", "SUCCESS")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headful", action="store_true")
    args = parser.add_argument("--accounts", default="accounts.txt")
    args = parser.parse_args()
    asyncio.run(run_flow(args.accounts, headless=not args.headful))
