#!/usr/bin/env python3
"""
Sequential ESL Video Generator
Reads detailed prompts from esl_prompts.json and drives Dola sequentially to generate
and download multiple clips using the existing lifecycle code, forming a cohesive 2-minute video.
"""

import asyncio
import os
import json
import logging
from full_lifecycle_video import main as run_lifecycle

import full_lifecycle_video

PROMPTS_FILE = "/home/azureuser/bulk-Video-generation/app/esl_prompts.json"

async def esl_main():
    with open(PROMPTS_FILE, "r") as f:
        prompts = json.load(f)
        
    print(f"Loaded {len(prompts)} sequential ESL prompts.")
    
    # We will override the SAMPLE_PROMPT globally for each cycle, 
    # but the current architecture loops inside main() using the same prompt.
    # A quick hack is to patch it for a single pass, but for limitless generation
    # on the same account we actually want to pop a new prompt each cycle.
    
    # To do this cleanly, we can yield prompts dynamically
    global_prompt_idx = 0
    def get_next_prompt():
        nonlocal global_prompt_idx
        if global_prompt_idx >= len(prompts):
            return "A majestic cinematic ending shot, hyperrealistic."
        p = prompts[global_prompt_idx]
        global_prompt_idx += 1
        return p

    # Patch the generate_and_download_video function to use the dynamic prompt
    original_gen = full_lifecycle_video.generate_and_download_video
    async def patched_gen(page, prompt):
        real_prompt = get_next_prompt()
        print(f"\n[ESL] Using prompt ({global_prompt_idx}/{len(prompts)}): '{real_prompt}'\n")
        return await original_gen(page, real_prompt)

    full_lifecycle_video.generate_and_download_video = patched_gen
    
    # Run the cycle limits
    await full_lifecycle_video.main("accounts.txt", headful=True)
    
if __name__ == "__main__":
    asyncio.run(esl_main())
