#!/usr/bin/env python3
"""Cleanup active profiles for specified accounts on Dola natively."""
import asyncio
import os
from cloakbrowser import launch_persistent_context_async
from autologin import automate_google_login, load_accounts, log
from full_lifecycle_video import get_google_auth_url, delete_dola_account, ensure_socks_proxy

SPAIN_SOCKS_PROXY = "socks5://10.200.200.2:1080"

async def manual_clean():
    accounts = load_accounts("accounts.txt")
    ensure_socks_proxy()
    
    vpn_proxy = {"server": SPAIN_SOCKS_PROXY}
    
    for acc in accounts:
        email, password = acc[0], acc[1]
        totp_secret = acc[2] if len(acc) > 2 else None
        session_dir = f"/home/azureuser/bulk-Video-generation/app/sessions/{email.replace('@', '_').replace('.', '_')}"
        
        log(f"\n--- PURGE CYCLE FOR: {email} ---", "INFO")
        try:
            ctx = await launch_persistent_context_async(
                user_data_dir=session_dir, headless=False, proxy=vpn_proxy,
                viewport={"width": 1280, "height": 900}, locale="en-US", humanize=True)
            page = await ctx.new_page()

            url = await get_google_auth_url(ctx, page)
            if url:
                state = await automate_google_login(
                    url, email, password, headless=False, proxy=vpn_proxy,
                    session_dir=session_dir, existing_ctx=ctx, existing_page=page,
                    close_on_finish=False, totp_secret=totp_secret)
                
                await asyncio.sleep(4)
                await delete_dola_account(page)
            else:
                log(f"Already logged into Dola. Going straight to delete.", "INFO")
                await delete_dola_account(page)

            await ctx.close()
            log(f"Successfully purged and closed context for {email}.", "SUCCESS")
        except Exception as e:
            log(f"Error purging {email}: {e}", "ERR")

if __name__ == "__main__":
    asyncio.run(manual_clean())
