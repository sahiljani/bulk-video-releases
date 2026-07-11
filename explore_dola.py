#!/usr/bin/env python3
"""Login to Dola, then explore the Create Video UI to find all controls."""
import asyncio
import os
import json
import time

async def explore_video():
    from cloakbrowser import launch_persistent_context_async
    from dola_login import click_by_text
    from autologin import load_accounts, log

    session_dir = "/home/azureuser/bulk-Video-generation/app/sessions/ypatel42011a_gmail_com"
    
    ctx = await launch_persistent_context_async(
        user_data_dir=session_dir,
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        humanize=True,
    )

    page = await ctx.new_page()
    page.set_default_timeout(30000)
    
    await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(5)
    log(f"Page loaded: {page.url}", "OK")

    # Dismiss cookie banner
    try:
        accept_btn = page.locator('button:has-text("Accept All")').first
        if await accept_btn.count() > 0:
            await accept_btn.click()
            log("Dismissed cookie banner", "INFO")
            await asyncio.sleep(1)
    except:
        pass

    # Check if logged in
    text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
    if "Log In" in text and "How can I assist" in text:
        log("Not logged in. Need to login first...", "WAIT")
        
        # Click Log In
        login_btn = page.locator('button:has-text("Log In")').first
        if await login_btn.count() > 0:
            await login_btn.click()
            await asyncio.sleep(2)

        # Click Continue with Google
        google_btn = page.locator(
            'button:has-text("Continue with Google"), '
            '[class*="button-"]:has-text("Continue with Google"), '
            'button:has(img.size-24), '
            '[class*="button-"]:has(img.size-24)'
        ).first
        
        popups = []
        ctx.on("page", lambda pg: popups.append(pg))
        
        try:
            await google_btn.wait_for(state="visible", timeout=10000)
            await google_btn.click()
            await asyncio.sleep(5)
        except:
            log("Google button not found", "ERR")
        
        # Check if Google popup or redirect happened
        def google_page():
            for pg in [page] + popups:
                try:
                    if "accounts.google.com" in (pg.url or ""):
                        return pg
                except:
                    pass
            return None
        
        gpg = None
        for _ in range(10):
            gpg = google_page()
            if gpg:
                break
            await asyncio.sleep(1)
        
        if gpg:
            log(f"Google login page: {gpg.url[:80]}", "OK")
            # Since the session dir has cookies, it might auto-login
            # Wait for it to redirect back
            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    if "dola.com" in page.url:
                        log(f"Redirected back to Dola: {page.url}", "OK")
                        break
                except:
                    break
        else:
            # Maybe already logged in after cookie session
            log("No Google page appeared - checking if already logged in", "INFO")
        
        await asyncio.sleep(3)
    else:
        log("Already logged in!", "OK")

    # Now take screenshot of logged-in state
    await page.screenshot(path="/home/azureuser/dola_logged_in.png")
    log("Screenshot: logged-in state saved", "INFO")

    # Get the visible text after login
    text = await page.evaluate("() => document.body.innerText.slice(0, 3000)")
    log(f"Visible text after login:\n{text}", "INFO")

    # Click "Create Video" button
    log("Looking for Create Video button...", "INFO")
    create_video_btn = page.locator('button:has-text("Create Video")').first
    if await create_video_btn.count() > 0:
        await create_video_btn.click()
        log("Clicked Create Video!", "OK")
        await asyncio.sleep(3)
        
        # Screenshot after clicking Create Video
        await page.screenshot(path="/home/azureuser/dola_create_video.png")
        log("Screenshot: Create Video panel saved", "INFO")
        
        # Get the new visible text
        text2 = await page.evaluate("() => document.body.innerText.slice(0, 5000)")
        log(f"Text after Create Video click:\n{text2}", "INFO")
        
        # Find all new elements that appeared
        all_elements = await page.evaluate("""() => {
            const results = {};
            
            // All buttons
            const btns = document.querySelectorAll('button, [role="button"]');
            results.buttons = Array.from(btns).map(b => ({
                text: (b.textContent || '').trim().slice(0, 80),
                ariaLabel: b.getAttribute('aria-label') || '',
            })).filter(b => b.text || b.ariaLabel);
            
            // All inputs
            const inputs = document.querySelectorAll('input, textarea, select');
            results.inputs = Array.from(inputs).map(e => ({
                tag: e.tagName,
                type: e.type || '',
                placeholder: e.placeholder || '',
                name: e.name || '',
                id: e.id || '',
                accept: e.accept || '',
            }));
            
            // All radio/checkbox groups
            const radios = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
            results.radios = Array.from(radios).map(e => ({
                name: e.name || '',
                value: e.value || '',
                checked: e.checked,
                label: e.closest('label') ? e.closest('label').textContent.trim().slice(0, 50) : '',
            }));
            
            // Any elements with ratio-like text
            const allEls = document.querySelectorAll('*');
            const ratioEls = [];
            for (const el of allEls) {
                const t = (el.textContent || '').trim();
                if (t.match(/\\d+:\\d+/) && t.length < 20) {
                    ratioEls.push({text: t, tag: el.tagName});
                }
            }
            results.ratios = ratioEls.slice(0, 20);
            
            return results;
        }""")
        
        log(f"\nButtons: {json.dumps(all_elements.get('buttons', []), indent=2)}", "INFO")
        log(f"\nInputs: {json.dumps(all_elements.get('inputs', []), indent=2)}", "INFO")
        log(f"\nRadios/Checkboxes: {json.dumps(all_elements.get('radios', []), indent=2)}", "INFO")
        log(f"\nRatio elements: {json.dumps(all_elements.get('ratios', []), indent=2)}", "INFO")
    else:
        log("Create Video button NOT found", "ERR")
        
    # Also try Create Image
    log("\nLooking for Create Image button...", "INFO")
    create_image_btn = page.locator('button:has-text("Create Image")').first
    if await create_image_btn.count() > 0:
        log("Create Image button found", "OK")

    # Save full page HTML for analysis
    html = await page.content()
    with open("/home/azureuser/dola_page.html", "w", encoding="utf-8") as f:
        f.write(html)
    log("Saved full page HTML to /home/azureuser/dola_page.html", "INFO")

    # Keep browser open
    log("\n=== EXPLORATION COMPLETE ===", "OK")
    log("Browser stays open for 10 minutes. Watch in VNC!", "INFO")
    try:
        await asyncio.sleep(600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    
    await ctx.close()

if __name__ == "__main__":
    asyncio.run(explore_video())
