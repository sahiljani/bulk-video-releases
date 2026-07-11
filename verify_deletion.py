#!/usr/bin/env python3
import asyncio
import os
from cloakbrowser import launch_persistent_context_async
from autologin import automate_google_login, load_accounts, log
from full_lifecycle_video import get_google_auth_url, delete_dola_account, ensure_socks_proxy

SPAIN_SOCKS_PROXY = "socks5://10.200.200.2:1080"

async def test_delete():
    accounts = load_accounts("accounts.txt")
    ensure_socks_proxy()
    
    vpn_proxy = {"server": SPAIN_SOCKS_PROXY}
    acc = accounts[0]
    email, password = acc[0], acc[1]
    totp_secret = acc[2] if len(acc) > 2 else None
    
    # Use a fresh session directory to ensure we log in from scratch
    session_dir = f"/home/azureuser/bulk-Video-generation/app/sessions/test_delete"
    os.system(f"rm -rf {session_dir}")
    
    log(f"\n--- VERIFYING DELETION FOR: {email} ---", "INFO")
    
    ctx = await launch_persistent_context_async(
        user_data_dir=session_dir, headless=True, proxy=vpn_proxy,
        viewport={"width": 1280, "height": 900}, locale="en-US", humanize=True)
    page = await ctx.new_page()

    url = await get_google_auth_url(ctx, page)
    if url:
        state = await automate_google_login(
            url, email, password, headless=True, proxy=vpn_proxy,
            session_dir=session_dir, existing_ctx=ctx, existing_page=page,
            close_on_finish=False, totp_secret=totp_secret)
        
        await asyncio.sleep(4)
        # Delete!
        success = await delete_dola_account(page)
        
        if success:
            log("Saving DOM to verify deletion state...", "INFO")
            with open("/home/azureuser/deletion_proof.html", "w") as f:
                f.write(await page.content())
            await page.screenshot(path="/home/azureuser/deletion_proof.png")
            
            # Read back text from page
            body_text = await page.evaluate("() => document.body.innerText")
            log(f"Text on page after deletion: {body_text[:200]}", "INFO")
            
    await ctx.close()

if __name__ == "__main__":
    asyncio.run(test_delete())
