#!/usr/bin/env python3
"""
OpenCode Telegram Agent
Gemini Flash 2.0 Lite orchestrates → OpenCode CLI executes → streams output to Telegram
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import yaml
from google import genai
from google.genai import types
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    encoding="utf-8",
)
log = logging.getLogger("opencode-agent")

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

cfg = load_config()

TELEGRAM_TOKEN       = cfg["telegram"]["bot_token"]
ALLOWED_USER_IDS     = set(cfg["telegram"]["allowed_user_ids"])
GEMINI_API_KEY       = cfg["gemini"]["api_key"]
OPENCODE_BINARY      = cfg["opencode"]["binary_path"]        # "opencode" or full path
OPENCODE_WORKDIR     = str(Path(cfg["opencode"]["work_dir"]).expanduser().resolve())
OPENCODE_MODEL       = cfg["opencode"].get("model", "opencode/minimax-m2.5-free")

# ── Post-run config ────────────────────────────────────────────────────────────
GIT_ENABLED          = cfg.get("post_run", {}).get("git_push", False)
GIT_COMMIT_MSG       = cfg.get("post_run", {}).get("commit_message", "chore: opencode agent update")
SCREENSHOT_ENABLED   = cfg.get("post_run", {}).get("screenshot", False)
SCREENSHOT_FILE      = cfg.get("post_run", {}).get("html_file", "index.html")
OUTPUT_LOG_DIR       = Path(cfg["opencode"]["output_log_dir"]).expanduser().resolve()
STREAM_CHUNK_LINES   = cfg["opencode"].get("stream_chunk_lines", 15)
MAX_OUTPUT_CHARS     = cfg["opencode"].get("max_output_chars", 3800)

OUTPUT_LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Gemini client ──────────────────────────────────────────────────────────────
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """You are an AI orchestrator for OpenCode CLI, a coding agent.
Your job is to receive the user's natural language request and convert it into a
clear, precise, actionable task description for OpenCode.

Rules:
- Be specific about what files to modify (if mentioned or inferable)
- Include any constraints the user mentioned (language, framework, style)
- Do NOT include shell commands or code yourself — OpenCode will do the coding
- Keep the final task under 500 words
- Output ONLY the task description, no preamble or explanation

Respond with a single block of text: the refined task prompt for OpenCode."""

async def orchestrate_with_gemini(user_message: str) -> str:
    """Use Gemini to turn a casual user request into a structured OpenCode task."""
    log.info("Sending to Gemini: %s", user_message[:80])
    response = gemini_client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        contents=user_message,
    )
    task = response.text.strip()
    log.info("Gemini task: %s", task[:120])
    return task

# ── OpenCode runner ────────────────────────────────────────────────────────────
async def run_opencode_streaming(
    task: str,
    update: Update,
    log_path: Path,
):
    """
    Run OpenCode CLI, stream stdout line-by-line to Telegram,
    and save full output to a log file.
    """
    cmd = [OPENCODE_BINARY, "run", "-m", OPENCODE_MODEL, task]
    log.info("Running: %s", " ".join(cmd))

    status_msg = await update.message.reply_text(
        "⚙️ *OpenCode running…*",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    buffer_lines: list[str] = []
    full_output: list[str] = []
    last_edit_time = asyncio.get_event_loop().time()
    EDIT_INTERVAL = 2.0  # seconds between Telegram message edits

    async def flush_buffer(force: bool = False):
        nonlocal last_edit_time
        now = asyncio.get_event_loop().time()
        if not buffer_lines:
            return
        if not force and (now - last_edit_time) < EDIT_INTERVAL:
            return
        chunk = "\n".join(buffer_lines[-STREAM_CHUNK_LINES:])
        # Strip non-ASCII to avoid encoding issues, hard-truncate to safe limit
        safe = chunk.encode("ascii", errors="replace").decode("ascii")
        preview = safe[:1800]  # leave room for the wrapper text + code fences
        try:
            await status_msg.edit_text(
                f"Running...\n```\n{preview}\n```",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            last_edit_time = now
        except Exception as e:
            log.warning("Edit failed (rate limit?): %s", e)

    with open(log_path, "w", encoding="utf-8") as log_file:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=OPENCODE_WORKDIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            line = line.encode("ascii", errors="replace").decode("ascii")
            log_file.write(line + "\n")
            log_file.flush()
            full_output.append(line)
            buffer_lines.append(line)
            await flush_buffer()

        await proc.wait()
        exit_code = proc.returncode

    # Final flush
    await flush_buffer(force=True)

    return exit_code, "\n".join(full_output)


# ── Post-run actions ──────────────────────────────────────────────────────────
async def decide_screenshot_file(task: str, output: str, workdir: str):
    """Ask Gemini which HTML file to screenshot based on the task."""
    html_files = [
        str(p.relative_to(workdir)).replace("\\", "/")
        for p in Path(workdir).rglob("*.html")
        if "_screenshot" not in p.name
    ]
    if not html_files:
        return None
    if len(html_files) == 1:
        return html_files[0]

    files_list = "\n".join(f"- {f}" for f in html_files)
    prompt = (
        f"A coding agent just completed this task:\n{task}\n\n"
        f"These HTML files exist in the project:\n{files_list}\n\n"
        "Which single HTML file is the most relevant to screenshot to verify the result? "
        "Reply with ONLY the filename exactly as listed, nothing else."
    )
    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
        )
        chosen = response.text.strip().lstrip("- ").strip()
        if chosen in html_files:
            return chosen
        for f in html_files:
            if chosen in f or f in chosen:
                return f
    except Exception as e:
        log.warning("Gemini screenshot decision failed: %s", e)
    for f in html_files:
        if "index" in f.lower():
            return f
    return html_files[0]


async def git_push(workdir: str) -> tuple[bool, str]:
    """Stage all changes, commit, and push."""
    try:
        # Check if there are any changes to commit
        result = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        if not stdout.strip():
            return True, "No changes to commit."

        # Add all
        p1 = await asyncio.create_subprocess_exec(
            "git", "add", ".",
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await p1.communicate()

        # Commit
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"{GIT_COMMIT_MSG} [{ts}]"
        p2 = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", msg,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out2, err2 = await p2.communicate()

        # Push
        p3 = await asyncio.create_subprocess_exec(
            "git", "push",
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out3, err3 = await p3.communicate()

        if p3.returncode == 0:
            return True, f"Pushed: {msg}"
        else:
            return False, err3.decode("utf-8", errors="replace")[:300]

    except Exception as e:
        return False, str(e)


async def take_screenshot(workdir: str, html_file: str) -> Path | None:
    """Use headless Chrome to screenshot an HTML file, return the image path."""
    # Find Chrome/Chromium
    chrome = None
    candidates = [
        "chrome", "google-chrome", "chromium", "chromium-browser",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if shutil.which(c) or os.path.exists(c):
            chrome = c
            break

    if not chrome:
        log.warning("Chrome not found for screenshot")
        return None

    html_path = Path(workdir) / html_file
    if not html_path.exists():
        # Try to find any html file in workdir
        html_files = list(Path(workdir).glob("*.html"))
        if not html_files:
            log.warning("No HTML file found for screenshot")
            return None
        html_path = html_files[0]

    out_path = Path(workdir) / "_screenshot.png"
    file_url = html_path.as_uri()

    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--window-size=1280,800",
        f"--screenshot={out_path}",
        file_url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=20)
        if out_path.exists():
            return out_path
    except asyncio.TimeoutError:
        log.warning("Screenshot timed out")
    except Exception as e:
        log.warning("Screenshot failed: %s", e)
    return None


# ── Telegram handlers ──────────────────────────────────────────────────────────
def auth_required(func):
    """Decorator: only allow whitelisted user IDs."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in ALLOWED_USER_IDS:
            await update.message.reply_text("⛔ Unauthorized.")
            log.warning("Blocked user %s", uid)
            return
        return await func(update, ctx)
    return wrapper


@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *OpenCode Agent ready.*\n\n"
        "Send me a coding task and I'll handle it.\n\n"
        "Commands:\n"
        "/run `<task>` — explicit task\n"
        "/status — last run info\n"
        "/cancel — stop current run\n"
        "/logs — list saved log files",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    last = ctx.bot_data.get("last_run")
    if not last:
        await update.message.reply_text("No runs yet this session.")
        return
    await update.message.reply_text(
        f"📋 *Last run*\n"
        f"• Task: `{last['task'][:80]}…`\n"
        f"• Exit code: `{last['exit_code']}`\n"
        f"• Finished: `{last['finished']}`\n"
        f"• Log: `{last['log_path']}`",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


@auth_required
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    files = sorted(OUTPUT_LOG_DIR.glob("*.log"), reverse=True)[:10]
    if not files:
        await update.message.reply_text("No logs found.")
        return
    names = "\n".join(f"• `{f.name}`" for f in files)
    await update.message.reply_text(
        f"📂 *Recent logs* (last 10):\n{names}",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


@auth_required
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    task: asyncio.Task = ctx.bot_data.get("current_task")
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Run cancelled.")
    else:
        await update.message.reply_text("Nothing running.")


async def handle_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw_input: str):
    """Core pipeline: Gemini → OpenCode → stream → notify."""
    uid = update.effective_user.id
    username = update.effective_user.username or str(uid)

    # 1. Orchestrate with Gemini
    thinking_msg = await update.message.reply_text("🧠 *Thinking with Gemini…*", parse_mode=constants.ParseMode.MARKDOWN)
    try:
        task_prompt = await orchestrate_with_gemini(raw_input)
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Gemini error: `{e}`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    await thinking_msg.edit_text(
        f"✅ *Task ready:*\n```\n{task_prompt[:500]}\n```",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    # 2. Prepare log file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_LOG_DIR / f"run_{ts}_{username}.log"

    # 3. Run OpenCode (streaming)
    async def _run():
        exit_code, full_output = await run_opencode_streaming(task_prompt, update, log_path)

        # 4. Final result notification
        success = exit_code == 0
        icon = "✅" if success else "❌"
        tail = full_output[-MAX_OUTPUT_CHARS:] if len(full_output) > MAX_OUTPUT_CHARS else full_output
        summary = tail[-1000:] if len(tail) > 1000 else tail  # last 1000 chars as summary

        safe_summary = summary.encode("ascii", errors="replace").decode("ascii")[:800]
        await update.message.reply_text(
            f"{icon} Run finished (exit {exit_code})\n\n"
            f"Summary:\n{safe_summary}\n\n"
            f"Log saved: {log_path.name}",
        )

        # 5. Git push
        if success and GIT_ENABLED:
            await update.message.reply_text("Pushing to git...")
            git_ok, git_msg = await git_push(OPENCODE_WORKDIR)
            git_icon = "✅" if git_ok else "❌"
            await update.message.reply_text(f"{git_icon} Git: {git_msg}")

        # 6. Screenshot — Gemini decides which file
        if success and SCREENSHOT_ENABLED:
            await update.message.reply_text("Deciding which file to screenshot...")
            html_file = await decide_screenshot_file(task_prompt, full_output, OPENCODE_WORKDIR)
            if html_file:
                await update.message.reply_text(f"Screenshotting {html_file}...")
                shot = await take_screenshot(OPENCODE_WORKDIR, html_file)
                if shot:
                    with open(shot, "rb") as img:
                        await update.message.reply_photo(
                            photo=img,
                            caption=f"Screenshot: {html_file}",
                        )
                else:
                    await update.message.reply_text(
                        "Could not take screenshot. Is Chrome installed?"
                    )
            else:
                await update.message.reply_text("No HTML file found to screenshot.")

        ctx.bot_data["last_run"] = {
            "task": task_prompt,
            "exit_code": exit_code,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "log_path": str(log_path),
        }

    task = asyncio.create_task(_run())
    ctx.bot_data["current_task"] = task
    try:
        await task
    except asyncio.CancelledError:
        await update.message.reply_text("🛑 Task was cancelled.")


@auth_required
async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(ctx.args)
    if not raw.strip():
        await update.message.reply_text("Usage: /run <your task description>")
        return
    await handle_task(update, ctx, raw)


@auth_required
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Any plain message is treated as a task."""
    await handle_task(update, ctx, update.message.text)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs",   cmd_logs))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("run",    cmd_run))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
