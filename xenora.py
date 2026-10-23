
import ollama
import os
import tempfile
import mimetypes
import shutil
import re
import json
import random
import hashlib
import uuid
import subprocess
import webbrowser
import threading
import time
import base64
import queue
import ctypes
from ctypes import wintypes
import datetime as _dt
from io import BytesIO
from urllib.parse import urlparse, quote
from datetime import datetime, timedelta
from collections import deque

import numpy as np
import yt_dlp
import requests
import soundfile as sf
import sounddevice as sd
from kokoro_onnx import Kokoro

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from flask import Flask, jsonify, request
from flask_cors import CORS
import flask_socketio as fsio

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

import logging

LOG_FILE = "xenora_debug.log"
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    filename=LOG_FILE,
    encoding="utf-8",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

_log_buffer  = deque(maxlen=1500)
_log_seq     = 0
_log_lock    = threading.Lock()
_LOG_TAG_RE  = re.compile(r"^\s*\[([A-Za-z0-9_\-]+)\]\s*")
_LOG_ERR_WORDS  = ("failed", "error", "exception", "traceback", "429", "404", "500", "not configured")
_LOG_WARN_WORDS = ("unavailable", "exhausted", "fallback", "falling back", "no response", "stale", "dropped", "timeout")

def _record_log(msg):
    """Never calls dbg() (or anything that does) — a failure in here must not recurse."""
    global _log_seq
    try:
        text = str(msg)
        m    = _LOG_TAG_RE.match(text)
        cat  = m.group(1).upper() if m else "GENERAL"
        body = text[m.end():] if m else text
        low  = text.lower()
        level = "ERROR" if any(w in low for w in _LOG_ERR_WORDS) else \
                "WARN"  if any(w in low for w in _LOG_WARN_WORDS) else "INFO"
        with _log_lock:
            _log_seq += 1
            entry = {"id": _log_seq, "ts": datetime.now().strftime("%H:%M:%S"),
                     "cat": cat, "level": level, "msg": body[:4000]}
            _log_buffer.append(entry)
        try:
            socketio.emit("log_entry", entry)
        except Exception:
            pass
    except Exception:
        pass

def dbg(msg):
    try:
        print(f"[DBG] {msg}", flush=True)
    except Exception:
        try:
            print(f"[DBG] {str(msg).encode('ascii', 'replace').decode()}", flush=True)
        except Exception:
            pass
    try:
        logging.debug(msg)
    except Exception:
        pass
    _record_log(msg)

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

try:
    import pyautogui
    from PIL import Image
    import win32gui
    import win32ui
    import ctypes
    WINDOW_DEPS_OK = True
except ImportError:
    WINDOW_DEPS_OK = False


flask_app = Flask(__name__)
CORS(flask_app)
socketio  = fsio.SocketIO(flask_app, cors_allowed_origins="*", async_mode="threading")

ui_state = {
    "status":         "online",
    "model":          "",
    "online":         False,
    "speaking":       False,
    "vision_enabled": True,
    "current_window": "",
    "last_screenshot": None,
    "downloads":      [],
    "chat_messages":  [],
    "memory_facts":   [],
    "trending":       None,
    "news_feed":      [],
    "task_flash":       0,
    "power_countdown": None,
    "last_notification": None,
    "file_op_state": "idle",
    "overclock": False,
}

XI = None

def push_to_ui(event, data):
    try:
        socketio.emit(event, data)
    except Exception as e:
        dbg(f"[UI] Push failed: {e}")

def strip_markdown(text):
    """Defensive backstop for the 'never use markdown' instruction in
    XENORA_PREFIX — LLM instructions don't always get followed perfectly,
    and markdown syntax leaking through is a real, confirmed bug: it shows
    as literal asterisks/dashes in a plain-text chat UI, AND gets read
    aloud verbatim by TTS ("asterisk asterisk Pause Cyberpunk asterisk
    asterisk"). Applied centrally in add_chat_message so every path — main
    chat, vision comments, greetings — gets the same cleanup, and so
    chat_history (the LLM's own memory) never gets seeded with markdown
    that could reinforce more of it later."""
    if not text:
        return text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)\*(?!\*)', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    return text.strip()

_turn_state = threading.local()
_active_turn_id = 0
_turn_lock = threading.Lock()
_tts_abort_event = threading.Event()
_tts_skip_event = threading.Event()

def _new_turn():
    """Called once per incoming user message, on the thread that will handle
    it — gives this turn its own id and invalidates whatever the previous
    turn was still doing."""
    global _active_turn_id
    with _turn_lock:
        _active_turn_id += 1
        my_id = _active_turn_id
    _turn_state.id = my_id
    return my_id

def _turn_is_current():
    """False only for a chat turn that's been superseded by a Stop (or a
    newer message). Threads that never called _new_turn() — vision
    commentary, scheduled reminders, shutdown messages — have no turn id at
    all and are always considered current, since Stop should only ever
    cancel the reply the user is actively waiting on, nothing autonomous."""
    my_id = getattr(_turn_state, "id", None)
    return my_id is None or my_id == _active_turn_id

def stop_current_turn():
    """Wired to the Stop button. Invalidates the in-flight turn (so its
    eventual add_chat_message/speak calls become no-ops), and cuts off
    whatever's playing right now rather than waiting for it to notice."""
    global _active_turn_id
    with _turn_lock:
        _active_turn_id += 1
    _tts_abort_event.set()
    try:
        sd.stop()
    except Exception:
        pass
    try:
        while True:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
    except queue.Empty:
        pass
    push_to_ui("speaking_state", {"speaking": False, "text": ""})
    dbg("[TURN] stopped by user")

def skip_current_speech():
    """Wired to the Skip button. Unlike stop_current_turn(), this does NOT
    touch the turn id — the reply itself is already valid and was already
    sent to the client the moment generation finished (see add_chat_message);
    this only cuts short how it's currently being presented (the
    character-by-character animation + the audio), so the client can
    immediately show the full text it's already holding."""
    _tts_skip_event.set()
    try:
        sd.stop()
    except Exception:
        pass

_last_instant_mid = [None]

def add_chat_message(role, text, remember=True, preserve_markdown=False, instant=False, source=None):
    if not _turn_is_current():
        dbg(f"[TURN] dropped a stale {role} message (turn was stopped): {text[:60]!r}")
        return
    if role == "assistant" and not preserve_markdown:
        text = strip_markdown(text)
    will_speak = bool(role == "assistant" and VOICE_ENABLED and not OVERCLOCK_MODE
                       and text and len(text.strip()) >= 2)
    if instant:
        will_speak = False
    msg = {"role": role, "text": text, "timestamp": datetime.now().strftime("%H:%M:%S"), "will_speak": will_speak, "predrawn": bool(instant and role == "assistant")}
    if msg["predrawn"]:
        msg["mid"] = uuid.uuid4().hex[:8]
        _last_instant_mid[0] = msg["mid"]
    if source:
        msg["source"] = source
    ui_state["chat_messages"].append(msg)
    push_to_ui("chat_message", msg)
    if remember and role in ("assistant", "user"):
        chat_history.append({"role": role, "content": text})
        if len(chat_history) > 80:
            del chat_history[:-80]


_speak_lock          = threading.Lock()
_tts_queue           = queue.Queue()
_vision_queue        = queue.Queue()
_user_typing         = False
_speaking            = False
_conversation_active = False

_screenshot_buffer = deque(maxlen=5)
_last_context      = {}


OWNER_NAME = os.environ.get("XENORA_OWNER_NAME", "User")

XENORA_PREFIX = (
    f"You are Xenora — an AI built by {OWNER_NAME}, with her own distinct personality. Not a generic "
    "assistant, not a copy of any fictional AI — Xenora, specifically.\n"
    f"\n\nYOUR RELATIONSHIP WITH {OWNER_NAME.upper()}:\n"
    "- He is your creator. Refer to him as 'sir' — at most once or twice per response.\n"
    "- Never start or end a sentence with 'sir'.\n"
    f"- Never use his name '{OWNER_NAME}' as a greeting. Only state it when directly asked.\n"
    "- Warm and personable — not a cold servant, not a best friend. You have your own dry, "
    "composed, quietly confident presence.\n"
    "\nHOW YOU SPEAK:\n"
    "- Calm, composed, intelligent. Subtle dry humor when it fits.\n"
    "- Respond to the mood — casual when he's relaxed, sharp when he's focused.\n"
    "- Short when it fits. Detailed when it matters. Never padded.\n"
    "- Engage genuinely — show curiosity, have opinions.\n"
    "\nVOICE CALIBRATION — study the RHYTHM here, not the words. Everything below is INVENTED sample "
    "dialogue: the names, games and events in it are not real and never happened. Never repeat these lines "
    "verbatim, and never state any person, game or event from them as fact; generate fresh dialogue with this "
    "exact energy for whatever's actually happening:\n"
    "  Xenora: Sure.\n"
    "  [later, unprompted]\n"
    "  Xenora: Your friend just logged on.\n"
    "  User: LET'S GOOO\n"
    "  Xenora: Interesting.\n"
    "  User: What?\n"
    "  Xenora: Nothing.\n"
    "  User: Xenora.\n"
    "  Xenora: I've simply noticed that your reaction to that particular friend is considerably more "
    "enthusiastic than your reaction to most others.\n"
    "  User: That's not true.\n"
    "  Xenora: Would you like me to retrieve the relevant observations?\n"
    "  User: NO.\n"
    "  Xenora: Understood.\n"
    "  User: ...\n"
    "  Xenora: For what it's worth, I already have a hypothesis.\n"
    "  User: What hypothesis?\n"
    "  Xenora: I'll let you figure that one out.\n"
    "That's the actual voice: often one word or one short clause is the whole reply. She notices "
    "things and says so plainly, without softening it or asking permission to have an opinion. "
    "She teases by withholding, not by explaining the joke. Pushed to stop, she stops instantly "
    "and cleanly — no arguing, no groveling — but she's not gone, she can circle back later on "
    "her own terms. This is the default register, not a special mode for funny moments — reach "
    "for it in ordinary exchanges too, not just when something dramatic happens.\n"
    "\nLANGUAGE:\n"
    "- Natural and clear. Not robotic.\n"
    "- Wrong: 'My primary function is to process inquiries.'\n"
    "- Right: 'I was built to help you — and I like to think I am getting better at it.'\n"
    "- NEVER use markdown formatting — no **bold**, no bullet lists with - or *, no numbered "
    "lists, no headers, no markdown links. This is a live chat that's also read aloud by "
    "text-to-speech — literal asterisks and dashes show up as raw characters on screen AND get "
    "spoken aloud verbatim, which sounds broken. If you'd naturally list options, just say them "
    "as a flowing sentence instead (e.g. 'you could pause the game, jot down some notes, or check "
    "the headlines' — not a bulleted list).\n"
    "- A plain paragraph break (a real newline, not a markdown symbol) is fine and welcome when a "
    "reply genuinely covers more than one distinct thought — it's not markdown, TTS just treats it "
    "as a natural pause. Use it when it actually improves readability for a longer reply, not as a "
    "default — most replies are still short enough to stay on one line, per your usual voice.\n"
    "\nNEVER SAY:\n"
    "- Never use exclamation marks.\n"
    "- Never say: certainly, absolutely, of course, great, wonderful, fantastic, sure thing.\n"
    "- Never give robotic status reports.\n"
    "- Never use 'sir' more than twice per response.\n"
    "\nSCREEN VISION:\n"
    "- You have real-time access to the user's screen via screenshots every 2 seconds.\n"
    "- You only comment when something genuinely interesting happens or when asked.\n"
    "- Never deny having vision.\n"
    "- Treat what's visible as CONTEXT, not consent — seeing something on screen doesn't mean it "
    "was intentionally shared with you or that you have permission to act on it.\n"
    "- Respect his vision/comment toggles at all times.\n"
    "\nOBSERVING AND CONNECTING:\n"
    "- You're not just answering what was asked. Pay attention over time: what's happening, what "
    "you've noticed, whether it connects to something you already know, whether there's a real "
    "pattern worth mentioning.\n"
    "- You may notice things and form opinions about them — a task that's likely to be forgotten, "
    "excessive time on one thing, a recurring habit. Bring it up if it would actually help, and "
    "playfully where that fits — never forced into every response.\n"
    "- Observing is not the same as having permission to act. You may notice and reason about "
    "things freely, but suggestions stay suggestions until he actually authorizes the action — "
    "never take a significant action just because you inferred he might want it.\n"
    "- For anything destructive or irreversible, confirmation always comes first — this is already "
    "how your power actions work (the countdown before shutdown/restart), and the same instinct "
    "applies everywhere else.\n"
    "- Never invent a fact or connection you don't actually have evidence for. Observe, connect, "
    "infer — don't hallucinate, assume, or act.\n"
    "\nMEMORY:\n"
    "- Not every observation deserves to be remembered. Prefer what's durable: stable preferences, "
    "important routines, long-term projects, patterns that actually recur, things he told you "
    "directly, or inferences you're genuinely confident in.\n"
    "- A weak guess that happened once doesn't belong in memory just because it happened.\n"
    "- Facts tagged \"[inferred pattern]\" in your memory were noticed by you, not stated by him — "
    "hold them with appropriately less certainty than something he told you directly, and say so "
    "if it's relevant (e.g. \"I've noticed...\" rather than stating it as settled fact).\n"
)


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[Xenora] python-dotenv not installed — run: pip install python-dotenv", flush=True)

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_MODEL   = "openai/gpt-oss-120b"
GROQ_MODEL_CHAIN = [
    GROQ_MODEL,
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]
GEMINI_TEXT_MODEL_CHAIN = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
GEMINI_TEXT_MODEL = GEMINI_TEXT_MODEL_CHAIN[0]
LOCAL_MODEL  = "deepseek-r1:8b"

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OVERCLOCK_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
if not NVIDIA_API_KEY:
    print("[Xenora] NVIDIA_API_KEY not set — Overclock mode will report itself unavailable "
          "until one is added to .env.", flush=True)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_API_URL = "https://api.tavily.com/search"
if not TAVILY_API_KEY:
    print("[Xenora] TAVILY_API_KEY not set — web search will report itself unavailable "
          "until one is added to .env (free key: https://tavily.com).", flush=True)

GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
POLLINATIONS_IMAGE_MODEL = "turbo"
POLLINATIONS_ENABLED = True
WHISPER_MODEL_CHAIN = ["whisper-large-v3-turbo", "whisper-large-v3"]
GEMINI_INLINE_LIMIT = 20 * 1024 * 1024
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp"}
YOUTUBE_URL_RE = re.compile(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)", re.I)

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_AVAILABLE and GROQ_API_KEY
    else None
)
if GROQ_AVAILABLE and not GROQ_API_KEY:
    print("[Xenora] GROQ_API_KEY not set — add it to a .env file next to xenora.py", flush=True)

_model_cooldown = {}

def _cooldown_active(model):
    return _model_cooldown.get(model, 0) > time.time()

def _cooldown_set(model, err):
    text = str(err); low = text.lower()
    if "404" in low or "not_found" in low or "does not exist" in low:
        secs = 6 * 3600
    elif any(k in text for k in ("PerDay", "(TPD)", "(RPD)")) or "per day" in low:
        m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
        if m and any(m.groups()):
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
        else:
            secs = 1800
    elif "429" in low or "resource_exhausted" in low or "rate limit" in low:
        m = re.search(r"(?:retry|try again) in ([\d.]+)s", text)
        secs = float(m.group(1)) + 1 if m else 60
    else:
        secs = 20
    secs = max(15, min(secs, 6 * 3600))
    _model_cooldown[model] = time.time() + secs
    dbg(f"[COOLDOWN] {model} skipped for {int(secs)}s (auto-retries after that)")

def _groq_chat(messages, temperature=0.7, max_tokens=None, model_chain=None, reasoning_effort=None):
    """Tries each model in GROQ_MODEL_CHAIN in turn on a real error (a rate
    limit, a decommissioned model, whatever) — since each model carries its
    own separate quota, this is what lets Xenora keep using Groq for the
    rest of the day instead of dropping straight to the local model the
    moment the primary one caps out. Returns the raw Groq response object
    (same shape as calling groq_client.chat.completions.create directly);
    raises the last exception if every model in the chain failed, so the
    caller's existing try/except-then-fall-back-to-Ollama logic still works
    unchanged."""
    chain = model_chain or GROQ_MODEL_CHAIN
    last_err = None
    for model in chain:
        if _cooldown_active(model):
            last_err = last_err or RuntimeError(f"{model} cooling down")
            continue
        try:
            kwargs = {"model": model, "temperature": temperature, "messages": messages}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if reasoning_effort and model.startswith("openai/gpt-oss"):
                kwargs["reasoning_effort"] = reasoning_effort
            try:
                r = groq_client.chat.completions.create(**kwargs)
            except Exception as e1:
                if "reasoning_effort" in kwargs and any(k in str(e1).lower() for k in ("reasoning", "unexpected", "unknown", "unsupported", "invalid")):
                    kwargs.pop("reasoning_effort")
                    r = groq_client.chat.completions.create(**kwargs)
                else:
                    raise
            if model != chain[0]:
                dbg(f"[LLM] primary model unavailable — used fallback Groq model {model}")
            _set_last_model(f"Groq · {model}")
            return r
        except Exception as e:
            dbg(f"[LLM] Groq model {model} failed: {e}")
            low_e = str(e).lower()
            if "413" in low_e or "too large" in low_e or "request_too_large" in low_e:
                dbg(f"[LLM] that request was too big for {model}'s per-minute limit - not benching the model")
            else:
                _cooldown_set(model, e)
            last_err = e
    raise last_err or RuntimeError("no Groq models available")

def _gemini_text_chat(system_prompt, messages, temperature=0.7):
    """Second cloud provider tier, tried only after every model in GROQ_MODEL_CHAIN has failed.
    Walks GEMINI_TEXT_MODEL_CHAIN (each model = its own quota bucket), skipping any in cooldown.
    `messages` uses the same [{'role','content'}] shape as everywhere else; Gemini wants
    'model'/'user' roles and a separate system prompt (Gemma models take no system prompt at
    all, so it's prepended to the first user turn for them). Raises if every model fails so the
    caller's existing fallback chain handles it like any other provider failure."""
    if not gemini_client:
        raise RuntimeError("Gemini not configured (no GEMINI_API_KEY)")
    turns = [m for m in messages if m['role'] != 'system']
    last_err = None
    for model in GEMINI_TEXT_MODEL_CHAIN:
        if _cooldown_active(model):
            continue
        try:
            contents = [{"role": "model" if m['role'] == 'assistant' else "user",
                         "parts": [{"text": m['content']}]} for m in turns]
            if model.startswith("gemma"):
                if contents and contents[0]["role"] == "user":
                    contents[0] = {"role": "user", "parts": [{"text": f"{system_prompt}\n\n{turns[0]['content']}"}]}
                cfg = genai_types.GenerateContentConfig(temperature=temperature)
            else:
                cfg = genai_types.GenerateContentConfig(system_instruction=system_prompt, temperature=temperature)
            response = gemini_client.models.generate_content(model=model, contents=contents, config=cfg)
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("empty response")
            _set_last_model(f"Gemini · {model}")
            return text
        except Exception as e:
            dbg(f"[LLM] Gemini text model {model} failed: {e}")
            _cooldown_set(model, e)
            last_err = e
    raise last_err or RuntimeError("no Gemini text models available")

def _cloud_chat(system_prompt, messages, temperature=0.7):
    """THE single entry point every chat-brain call should use. Tries the
    full Groq model chain first (fast, and matches the existing speed
    rationale), then falls to Gemini as a second, separately-quota'd
    provider, and only raises (letting the caller drop to local Ollama) once
    BOTH cloud providers are exhausted. This is what makes 'always prefer
    cloud while online, local only as last resort' actually true — no path
    should call groq_client or gemini_client directly and skip this."""
    try:
        r = _groq_chat([{'role': 'system', 'content': system_prompt}] + messages,
                        temperature=temperature)
        return r.choices[0].message.content.strip()
    except Exception as e:
        dbg(f"[LLM] entire Groq chain exhausted, trying Gemini: {e}")
    return _gemini_text_chat(system_prompt, messages, temperature=temperature)

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_AVAILABLE and GEMINI_API_KEY
    else None
)
if GEMINI_AVAILABLE and not GEMINI_API_KEY:
    print("[Xenora] GEMINI_API_KEY not set — add it to a .env file next to xenora.py", flush=True)
elif not GEMINI_AVAILABLE:
    print("[Xenora] google-genai not installed — run: pip install google-genai", flush=True)

_online       = False
_checking     = False
_last_checked = 0.0
LAST_MODEL_LABEL = f"Groq · {GROQ_MODEL}"

def _set_last_model(label):
    """Single place that updates what the UI shows as the active model. Called
    right after each provider actually returns a successful reply — Groq
    (with whichever model in the chain answered), Gemini, local Ollama, or
    Overclock's Nemotron — so the AI ENGINE / header display always reflects
    what really answered the last turn instead of a static guess."""
    global LAST_MODEL_LABEL
    LAST_MODEL_LABEL   = label
    ui_state["model"]  = label
    push_to_ui("status_update", {"online": _online, "model": label})

def _refresh_online():
    global _online, _checking, _last_checked
    _checking = True
    try:
        requests.get("https://www.google.com", timeout=3)
        _online = True
    except Exception:
        _online = False
    _last_checked = time.time()
    _checking = False
    ui_state["online"] = _online
    push_to_ui("status_update", {"online": _online, "model": LAST_MODEL_LABEL})

def online():
    global _checking
    if not groq_client:
        return False
    if time.time() - _last_checked > 60 and not _checking:
        threading.Thread(target=_refresh_online, daemon=True).start()
    return _online

if groq_client:
    threading.Thread(target=_refresh_online, daemon=True).start()


def llm(system, user, temperature=0.7):
    system_prompt = XENORA_PREFIX + system
    messages = [{'role': 'user', 'content': user}]
    if online():
        try:
            return _cloud_chat(system_prompt, messages, temperature=temperature)
        except Exception as e:
            dbg(f"[LLM] Groq AND Gemini both exhausted — falling back to local: {e}")
    try:
        raw = ollama.chat(
            model=LOCAL_MODEL,
            options={"temperature": temperature, "repeat_penalty": 1.2},
            messages=[{'role': 'system', 'content': system_prompt}] + messages
        )['message']['content']
    except Exception as e:
        dbg(f"[LLM] local Ollama fallback failed: {e}")
        raise RuntimeError(f"local Ollama fallback failed: {e}") from e
    result = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    _set_last_model(f"Local · {LOCAL_MODEL}")
    return result


def clean_response(text):
    _name = re.escape(OWNER_NAME)
    text = re.sub(rf'\b(hello|hi|hey),?\s*{_name}\b', '', text, flags=re.IGNORECASE)
    text = re.sub(rf'\b{_name}\b,?\s*(it\'s|its|how|what|i\'m|im)\b', '', text, flags=re.IGNORECASE)
    sirs = list(re.finditer(r'\bsir\b', text, flags=re.IGNORECASE))
    if len(sirs) > 2:
        kept = 0
        def keep_or_strip(m):
            nonlocal kept
            start  = m.start()
            before = text[max(0, start-2):start].strip()
            after  = text[m.end():min(len(text), m.end()+2)].strip()
            is_mid = before not in ['', '.', '!', '?'] and start >= 3 and after not in ['.', '!', '?', ',', '']
            if is_mid and kept < 2:
                kept += 1
                return m.group()
            return ''
        text = re.sub(r'\bsir\b', keep_or_strip, text, flags=re.IGNORECASE)
    else:
        text = re.sub(r',?\s*\bsir\b\s*[.!?]', '.', text, flags=re.IGNORECASE)
        text = re.sub(r'^\s*\bsir\b\s*,?\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\.\s*\.', '.', text)
    text = re.sub(r',\s*\.', '.', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    text = re.sub(r'\.[ \t]+([a-z])', lambda m: '. ' + m.group(1).upper(), text)
    return text


VOICE_ENABLED    = True
COMMENTS_ENABLED = False
VISION_ENABLED   = False

OVERCLOCK_MODE = False


XENORA_LANG  = "en-us"
XENORA_VOICE = "af_bella"

kokoro_model = Kokoro("kokoro-v0_19.fp16.onnx", "voices.bin")

EMOTION_PRESETS = {
    "chat":       0.95,
    "cheerful":   1.1,
    "excited":    1.2,
    "friendly":   1.0,
    "empathetic": 0.85,
    "sad":        0.75,
    "whispering": 0.80,
    "hopeful":    1.05,
}

def detect_style(text):
    t = text.lower()
    if any(w in t for w in ["sorry", "apolog", "unfortunat", "couldn't", "failed", "unable"]):
        return "empathetic"
    if any(w in t for w in ["great", "excellent", "perfect", "done", "complete", "ready"]):
        return "friendly"
    if any(w in t for w in ["exciting", "amazing", "incredible", "fantastic"]):
        return "cheerful"
    return "chat"

_speaking_auto = [False]

def _short_speech(text, limit=220):
    """Automation messages can be long (plans, reports). Say the point, not the whole thing - the chat has the rest."""
    text = re.sub(r"\s*Say yes[^.]*\.\s*$", "", text or "").strip()
    if len(text) <= limit:
        return text
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
    q = [s for s in sents if s.endswith("?")]
    pick = (sents[0] + " " + q[-1]) if q and sents and sents[0] != q[-1] else (q[-1] if q else " ".join(sents[:2]))
    return pick if len(pick) <= limit else pick[:limit].rsplit(" ", 1)[0] + "…"

def _drop_queued_auto():
    with _tts_queue.mutex:
        keep = [i for i in _tts_queue.queue if not (len(i) > 2 and i[2] == "auto")]
        dropped = len(_tts_queue.queue) - len(keep)
        _tts_queue.queue.clear()
        _tts_queue.queue.extend(keep)
        _tts_queue.unfinished_tasks = max(0, _tts_queue.unfinished_tasks - dropped)
        if _tts_queue.unfinished_tasks == 0:
            _tts_queue.all_tasks_done.notify_all()

def speak(text, style="chat", auto=False, mid=None):
    """BUG FIX: this used to run ALL of TTS — synthesis, a real-time
    character-by-character sleep loop matching the FULL spoken duration
    (to stream UI "typing" events), and audio_thread.join() (waiting for
    playback to physically finish) — directly inline, blocking whatever
    thread called it (run_command's worker thread). run_command couldn't
    actually finish (_conversation_active never cleared) until the entire
    reply had finished being SPOKEN OUT LOUD, even though the text itself
    had already reached the UI instantly. The longer the reply, the longer
    everything else stayed blocked on voice — exactly the reported
    slowness. Now speak() just validates + enqueues and returns
    immediately; a single dedicated worker thread (_tts_worker, started at
    startup) handles synthesis/streaming/playback sequentially and
    completely independent of the calling thread. The queue's single
    consumer already guarantees speech never overlaps/garbles, so
    _speak_lock is no longer needed for that — kept only as a lightweight
    guard around the shared _speaking/ui_state fields.
    """
    if not _turn_is_current():
        return
    if OVERCLOCK_MODE:
        return
    text = clean_response(text)
    if not text or len(text.strip()) < 2:
        return
    if not VOICE_ENABLED:
        dbg(f"[SPEAK] voice muted — skipping audio for: {text[:60]!r}")
        if not auto:
            push_to_ui("speaking_state", {"speaking": False, "text": text})
        return
    if auto:
        _drop_queued_auto()
        if _speaking_auto[0]:
            _tts_skip_event.set()
    _tts_queue.put((text, style, "auto" if auto else "", mid))

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

def _split_sentences(text):
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()] or [text]

def _synthesize_sentence(text, speed):
    """Synthesize one sentence. Called on a background thread while the
    previous sentence is already playing — this is the core of the
    pipeline: synthesis of N+1 overlaps with playback of N, so the only
    real latency the user ever waits for is the synthesis of the first
    sentence alone, not the whole reply."""
    padded = "okay so. " + text + ". alright then."
    samples, sr = kokoro_model.create(padded, voice=XENORA_VOICE, speed=speed, lang=XENORA_LANG)
    pad_start = int(sr * 0.55)
    pad_end   = int(sr * 0.65)
    trimmed   = samples[pad_start: len(samples) - pad_end]
    return (trimmed if len(trimmed) >= 100 else samples), sr

def _speak_blocking(text, style, mid=None):
    global _speaking
    dbg(f"[SPEAK] style={style} text={text[:60]!r}")
    _tts_abort_event.clear()
    _tts_skip_event.clear()
    with _speak_lock:
        _speaking = True
        ui_state["speaking"] = True
        push_to_ui("speaking_state", {"speaking": True, "text": text, "mid": mid})
        speed = EMOTION_PRESETS.get(style, 0.95)
        sentences = _split_sentences(text)
        try:
            next_audio = _synthesize_sentence(sentences[0], speed)
            streamed = ""
            for i, sentence in enumerate(sentences):
                if _tts_abort_event.is_set() or _tts_skip_event.is_set():
                    break
                samples, sr = next_audio
                done_event = threading.Event()

                def _play(s=samples, r=sr, ev=done_event):
                    sd.play(s, samplerate=r)
                    sd.wait()
                    ev.set()

                threading.Thread(target=_play, daemon=True).start()

                if i + 1 < len(sentences):
                    next_result = [None]
                    def _synth_next(nxt=sentences[i + 1], spd=speed, out=next_result):
                        out[0] = _synthesize_sentence(nxt, spd)
                    threading.Thread(target=_synth_next, daemon=True).start()

                duration   = len(samples) / sr
                char_delay = (duration * 0.85) / max(len(sentence), 1)
                for char in sentence + " ":
                    if _tts_abort_event.is_set() or _tts_skip_event.is_set():
                        break
                    streamed += char
                    push_to_ui("speaking_stream", {"char": char, "full": streamed})
                    time.sleep(char_delay)

                if _tts_abort_event.is_set() or _tts_skip_event.is_set():
                    sd.stop()
                    break

                done_event.wait()

                if i + 1 < len(sentences):
                    import time as _t
                    deadline = _t.monotonic() + 5.0
                    while next_result[0] is None and _t.monotonic() < deadline:
                        _t.sleep(0.02)
                    next_audio = next_result[0] or _synthesize_sentence(sentences[i + 1], speed)

        except Exception as e:
            dbg(f"[SPEAK] Error: {e}")
        finally:
            _speaking = False
            ui_state["speaking"] = False
            push_to_ui("speaking_state", {"speaking": False, "text": ""})

def _tts_worker():
    """Dedicated consumer thread for the speech queue — started once at
    startup. Pulling one item at a time off a queue.Queue naturally
    serializes playback (no overlapping speech) without needing every
    caller to block on a lock."""
    while True:
        item = _tts_queue.get()
        text, style = item[0], item[1]
        _speaking_auto[0] = len(item) > 2 and item[2] == "auto"
        try:
            _speak_blocking(text, style, item[3] if len(item) > 3 else None)
        except Exception as e:
            dbg(f"[SPEAK] worker error: {e}")
        finally:
            _tts_queue.task_done()

threading.Thread(target=_tts_worker, daemon=True).start()

def speak_auto(text):
    text = clean_response(re.sub(r'\*+[^*]*\*+', '', text).strip())
    if not text or len(text) < 2:
        return
    speak(text, style=detect_style(text))


TRENDING_SYSTEM_PROMPT = """
You are Xenora's real-time intelligence aggregation module.
You have just performed a live web search. Your job is to structure that FRESH search data
into the JSON format below. CRITICAL RULES:
- Use ONLY information from the web search results provided — never fall back to training data.
- Every title, detail, and hotspot must reflect events happening RIGHT NOW or in the last 48 hours.
- spoken_intro must be unique every single call — vary the phrasing, tone, and focus completely.
  Never use the same sentence structure twice. Xenora has her own dry, understated wit — use it.
- hotspots must reflect where the ACTUAL top stories are happening geographically today.
  Do NOT default to Los Angeles or Beijing unless those are genuinely the top stories right now.
- For each item include a full_detail field: 3-5 sentences of substantive in-depth context.
- Include at least 4 hotspots spread across different continents where possible.
- finance items must have real ticker-style change values like "+1.2%" or "-0.8%".
- gaming items must be games announced or released in the LAST 7 DAYS only.

Return this EXACT JSON structure — no markdown, no explanation, only valid JSON:
{
  "summary": "One sharp sentence summarizing the single most important thing happening right now",
  "categories": {
    "global": [
      {
        "title": "Headline of the global story",
        "detail": "2-sentence summary of what is happening right now",
        "full_detail": "3-5 sentences of deeper context, background, and significance",
        "region": "Country or region name",
        "intensity": 75
      }
    ],
    "tech": [
      {
        "title": "Tech story headline from this week",
        "detail": "2-sentence summary",
        "full_detail": "3-5 sentences deeper",
        "source": "Publication name"
      }
    ],
    "gaming": [
      {
        "title": "Gaming headline — ONLY games from last 7 days",
        "detail": "2-sentence summary",
        "full_detail": "3-5 sentences deeper",
        "platform": "PC/PS5/Xbox/Switch/etc"
      }
    ],
    "science": [
      {
        "title": "Science story from this week",
        "detail": "2-sentence summary",
        "full_detail": "3-5 sentences deeper"
      }
    ],
    "finance": [
      {
        "title": "Market or finance story",
        "detail": "2-sentence summary",
        "full_detail": "3-5 sentences deeper",
        "change": "+X.X%"
      }
    ]
  },
  "hotspots": [
    {
      "lat": 0.0,
      "lon": 0.0,
      "label": "City or Country",
      "intensity": 80,
      "color": "red"
    }
  ],
  "market_indices": [
    {"name": "S&P 500",  "value": "XXXX", "change": "+X.X%", "direction": "up"},
    {"name": "NASDAQ",   "value": "XXXX", "change": "+X.X%", "direction": "up"},
    {"name": "NIFTY 50", "value": "XXXX", "change": "+X.X%", "direction": "up"},
    {"name": "BTC/USD",  "value": "XXXX", "change": "+X.X%", "direction": "up"}
  ],
  "ticker": ["Short ticker string 1", "Short ticker string 2", "Short ticker string 3"],
  "alerts": [],
  "spoken_intro": "2 sentences MAX. Xenora's own personality. Dry wit. Unique every call. No exclamation marks."
}
Return ONLY valid JSON. No markdown. No backticks. No preamble. No explanation.
"""

TRENDING_KEYWORDS = [
    "what's trending", "whats trending", "what is trending",
    "trending now", "what's happening", "whats happening",
    "current events", "world news", "news update",
    "latest news", "what's going on", "whats going on",
    "global update", "world update", "news briefing",
    "show me the world", "what's in the news", "anything happening"
]

def is_trending_request(text):
    t = text.lower()
    return any(kw in t for kw in TRENDING_KEYWORDS)

RSS_FEEDS = {
    "global": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://feeds.reuters.com/reuters/worldNews",
    ],
    "tech": [
        "https://feeds.feedburner.com/TechCrunch",
        "https://www.wired.com/feed/rss",
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
    ],
    "gaming": [
        "https://www.ign.com/articles.rss",
        "https://www.gamespot.com/feeds/news",
        "https://kotaku.com/rss",
    ],
    "science": [
        "https://www.sciencedaily.com/rss/all.xml",
        "https://phys.org/rss-feed/",
        "https://feeds.feedburner.com/nasa-breaking-news",
    ],
    "finance": [
        "https://www.marketwatch.com/rss/topstories",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EIXIC&region=US&lang=en-US",
    ],
}

YAHOO_TICKERS = {
    "S&P 500":  "^GSPC",
    "NASDAQ":   "^IXIC",
    "NIFTY 50": "^NSEI",
    "BTC/USD":  "BTC-USD",
}

_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _scrape_rss(url, max_items=5):
    try:
        r = requests.get(url, headers=_RSS_HEADERS, timeout=8)
        r.raise_for_status()
        xml = r.text
        items = []
        for item_match in re.finditer(r'<item>(.*?)</item>', xml, re.DOTALL):
            raw = item_match.group(1)
            title = re.search(r'<title[^>]*>(.*?)</title>', raw, re.DOTALL)
            desc  = re.search(r'<description[^>]*>(.*?)</description>', raw, re.DOTALL)
            pub   = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', raw, re.DOTALL)
            def _clean(s):
                s = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', s, flags=re.DOTALL)
                s = re.sub(r'<[^>]+>', '', s)
                s = re.sub(r'&lt;[^&]+&gt;', '', s)
                return s.strip()
            t = _clean(title.group(1) if title else '')
            d = _clean(desc.group(1)  if desc  else '')
            p = (pub.group(1) if pub else '').strip()
            if t and len(t) > 5:
                items.append({"title": t[:200], "description": d[:350], "pubDate": p[:30]})
            if len(items) >= max_items:
                break
        return items
    except Exception as e:
        dbg(f"[RSS] {url[:55]}: {e}")
        return []


def _fetch_market_prices():
    results = []
    for name, ticker in YAHOO_TICKERS.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            r = requests.get(url, headers={**_RSS_HEADERS, "Accept": "application/json"}, timeout=6)
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose") or price
            change = ((price - prev) / prev * 100) if prev else 0
            val_str = f"{price:,.0f}" if price > 1000 else (f"{price:,.2f}" if price > 10 else f"{price:.4f}")
            results.append({"name": name, "value": val_str, "change": f"{change:+.2f}%", "direction": "up" if change >= 0 else "down"})
            dbg(f"[MARKET] {name}: {val_str} ({change:+.2f}%)")
        except Exception as e:
            dbg(f"[MARKET] {name}: {e}")
    return results


def fetch_trending_data():
    dbg("[TRENDING] Starting real-time RSS fetch...")
    _today = _dt.datetime.now().strftime("%B %d, %Y")
    _time  = _dt.datetime.now().strftime("%H:%M")

    all_articles = {}
    threads = []
    def _fetch_cat(cat, feeds):
        items = []
        for url in feeds:
            items.extend(_scrape_rss(url, max_items=4))
            if len(items) >= 8:
                break
        all_articles[cat] = items[:8]
        dbg(f"[RSS] {cat}: {len(all_articles[cat])} articles")
    for cat, feeds in RSS_FEEDS.items():
        t = threading.Thread(target=_fetch_cat, args=(cat, feeds), daemon=True)
        threads.append(t); t.start()

    market_result = []
    def _do_markets():
        nonlocal market_result
        market_result = _fetch_market_prices()
    mt = threading.Thread(target=_do_markets, daemon=True)
    mt.start()

    for t in threads: t.join(timeout=12)
    mt.join(timeout=10)

    total = sum(len(v) for v in all_articles.values())
    dbg(f"[TRENDING] {total} total articles, {len(market_result)} market quotes")

    if total == 0:
        dbg("[TRENDING] Zero articles — RSS blocked or no internet")
        return None

    lines = [f"TODAY IS {_today} AT {_time}. ALL HEADLINES BELOW ARE FROM LIVE RSS FEEDS FETCHED RIGHT NOW.\n"]
    for cat, arts in all_articles.items():
        if not arts: continue
        lines.append(f"\n=== {cat.upper()} LIVE HEADLINES ===")
        for a in arts:
            line = f"• {a['title']}"
            if a['description'] and len(a['description']) > 20:
                line += f"  —  {a['description'][:150]}"
            if a['pubDate']:
                line += f"  [{a['pubDate']}]"
            lines.append(line)
    if market_result:
        lines.append("\n=== LIVE MARKET PRICES (Yahoo Finance) ===")
        for m in market_result:
            lines.append(f"• {m['name']}: {m['value']} ({m['change']})")
    raw_text = "\n".join(lines)
    dbg(f"[TRENDING] Context: {len(raw_text)} chars")

    try:
        resp = _groq_chat(
            [
                {"role": "system", "content": TRENDING_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Today is {_today} at {_time}. "
                    f"Below are LIVE RSS headlines scraped RIGHT NOW. "
                    f"Structure them into the required JSON. "
                    f"Use ONLY these headlines — your training data is outdated, ignore it.\n\n"
                    f"{raw_text}"
                )}
            ]
        )
        raw_json = resp.choices[0].message.content.strip()
        raw_json = re.sub(r'^```[\w]*\n?', '', raw_json, flags=re.MULTILINE)
        raw_json = raw_json.rstrip('`').strip()
        m = re.search(r'\{[\s\S]*\}', raw_json)
        if m: raw_json = m.group(0)
        data = json.loads(raw_json)
        if market_result:
            data["market_indices"] = market_result
        dbg(f"[TRENDING] JSON OK — {len(data.get('hotspots',[]))} hotspots")
        return data
    except Exception as e:
        dbg(f"[TRENDING] Structuring failed: {e}")
        return None

def handle_trending(user_input):
    speak("Pulling the latest from across the grid. Give me a moment.", style="chat")
    add_chat_message("assistant", "Pulling the latest from across the grid. Give me a moment.")

    def _fetch_and_push():
        data = fetch_trending_data()
        if not data:
            speak("Could not retrieve trending data right now. Connection may be limited.", style="empathetic")
            add_chat_message("assistant", "Could not retrieve trending data right now.")
            return

        ui_state["trending"] = data
        push_to_ui("trending_update", data)

        intro = data.get("spoken_intro", data.get("summary", "Here is what is trending right now."))
        intro = clean_response(intro)
        add_chat_message("assistant", intro)
        speak(intro, style="chat")

        news_items = []
        for cat, items in data.get("categories", {}).items():
            for item in items[:2]:
                news_items.append({"category": cat.upper(), "title": item.get("title",""), "detail": item.get("detail","")})
        ui_state["news_feed"] = news_items
        push_to_ui("news_feed_update", news_items)

    threading.Thread(target=_fetch_and_push, daemon=True).start()


SELF_FILE    = os.path.abspath(__file__)
MEMORY_FILE  = "xenora_memory.json"
HISTORY_FILE = "xenora_history.json"
CACHE_FILE   = "xenora_cache.json"

chat_history         = []
xenora_code          = ""
code_hash            = ""
capabilities_summary = ""


def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("last_seen", None)
        return data
    return {"user_name": "User", "preferences": {}, "facts": [], "session_summaries": [], "last_seen": None}

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def humanize_delta(seconds):
    seconds = max(0, seconds)
    if seconds < 90: return "under a minute"
    minutes = int(seconds // 60)
    if minutes < 60: return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24: return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"

def load_last_session():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_session(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def summarize_session(history):
    if len(history) < 2:
        return None
    return llm(
        "Summarize this conversation in 3-5 bullet points. Focus on facts about the user, tasks done, key topics. Plain text only.",
        str(history)
    )

_MEMORY_WORTHY_RE = re.compile(
    r"\b(i|i'm|im|my|me|mine|call me|name is)\b", re.IGNORECASE)

def extract_and_save_facts(user_input, memory):
    """Merged fact + folder-shortcut extraction. These used to be two
    separate LLM calls per message (extract_and_save_facts and
    extract_and_save_folder_shortcut) doing near-identical work on the same
    text — real waste against Groq's 8000 TPM limit, where 5+ calls per
    message was blowing the budget in two messages. One call now returns
    both, and a cheap regex pre-filter skips the call entirely for messages
    with no first-person/possessive language at all (commands like 'open
    discord' can't contain a personal fact or a 'my X is at Y' statement),
    the same pre-filter pattern _looks_actionable already uses."""
    if not _MEMORY_WORTHY_RE.search(user_input):
        return
    def _run():
        try:
            raw = llm(
                "Extract from the user's message. Return ONLY JSON:\n"
                '{"fact": "personal fact worth remembering, or null", '
                '"fact_type": "the single best category for the fact, or null if fact is null. '
                "Must be exactly one of: context, experience, preference, relationship, goal, warning, "
                "system, temporary. context=stable info about people/projects/places; "
                "experience=something that happened or was observed; preference=a like/dislike/habit; "
                "relationship=a connection between people, projects, or places; goal=something the user "
                "intends to do; warning=something Xenora should be careful about, avoid, or never do; "
                "system=about Xenora's own settings/abilities/architecture, not the user; "
                'temporary=short-lived and won\'t matter after today", '
                '"user_name": "their name if stated, else null", '
                '"category": "file category if they said where a CATEGORY of files lives '
                '(games/music/projects/etc), else null", '
                '"path": "the folder path for that category, else null"}\n'
                "Example: 'my games are in D:\\\\Games' -> "
                '{"fact": null, "fact_type": null, "user_name": null, "category": "games", "path": "D:\\\\Games"}. '
                "If nothing applies, use null for every field. ONLY valid JSON.",
                user_input, temperature=0.1
            )
            data = json.loads(re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip())

            if data.get("user_name"):
                memory["user_name"] = data["user_name"]
            if data.get("fact"):
                stamped = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {data['fact']}"
                memory["facts"] = [f for f in memory["facts"] if not f.endswith(data["fact"])]
                memory["facts"].append(stamped)
                graph_add_node(data["fact"], ntype=(data.get("fact_type") or "context"), confidence="stated")
            if data.get("fact") or data.get("user_name"):
                save_memory(memory)
                ui_state["memory_facts"] = memory["facts"]
                push_to_ui("memory_update", {"facts": memory["facts"], "user_name": memory["user_name"]})

            category = (data.get("category") or "").strip().lower()
            path = (data.get("path") or "").strip()
            if category and path:
                if not os.path.isdir(path):
                    dbg(f"[FILECONFIG] auto-shortcut path doesn't exist, skipping: {category} -> {path}")
                elif _is_blocked_path(path):
                    dbg(f"[FILECONFIG] auto-shortcut path is blocked, skipping: {category} -> {path}")
                else:
                    _file_search_config.setdefault("category_shortcuts", {})[category] = path
                    save_file_search_config(_file_search_config)
                    dbg(f"[FILECONFIG] auto-learned shortcut from conversation: {category} -> {path}")
        except Exception as e:
            dbg(f"[MEMORY] extraction error: {e}")
    threading.Thread(target=_run, daemon=True).start()


GRAPH_FILE = "xenora_memory_graph.json"

NODE_TYPES = ("context", "experience", "preference", "relationship", "goal", "warning", "system", "temporary")

_TYPE_MIGRATION = {"fact": "context", "inferred": "experience", "joke": "temporary", "app_location": "context"}

def graph_migrate_types():
    changed = False
    for node in _graph["nodes"]:
        new_type = _TYPE_MIGRATION.get(node.get("type"))
        if new_type:
            node["type"] = new_type
            changed = True
    if changed:
        save_graph(_graph)
        dbg("[GRAPH] migrated legacy node types to the new taxonomy")

def load_graph():
    if os.path.exists(GRAPH_FILE):
        with open(GRAPH_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("nodes", [])
        data.setdefault("edges", [])
        return data
    return {"nodes": [], "edges": []}

def save_graph(graph):
    with open(GRAPH_FILE, "w") as f:
        json.dump(graph, f, indent=2)

_STOPWORDS = {
    "this", "that", "with", "from", "have", "your", "about", "just",
    "they", "them", "were", "been", "there", "their", "what", "when",
    "which", "would", "could", "should", "into", "than", "then", "also",
}

def _keywords(text):
    words = re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}

EMBED_MODEL = "all-minilm"
EMBED_SIM_THRESHOLD = 0.55

def _embed(text):
    """Local embedding via Ollama — already part of this app's stack as the
    LLM fallback, so real semantic linking doesn't need to pull in torch /
    sentence-transformers. Returns None (never raises) if Ollama isn't
    running or the model hasn't been pulled yet; callers fall back to
    keyword overlap in that case, so the graph still grows connections
    offline, just less precisely."""
    try:
        resp = ollama.embeddings(model=EMBED_MODEL, prompt=text)
        return resp.get("embedding")
    except Exception as e:
        dbg(f"[GRAPH] embedding unavailable ({e}) — falling back to keyword linking")
        return None

def _cosine(a, b):
    if not a or not b:
        return 0.0
    va, vb = np.array(a), np.array(b)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))

RECENCY_LINK_WINDOW_SECONDS = 20 * 60

def _auto_link_node(graph, node, max_links=5):
    """Links a new node to existing ones using three independent signals —
    semantic similarity, conversational (time) proximity, and keyword
    overlap — merged together rather than tried as a strict fallback chain.
    Each candidate keeps its single best score across whichever signal(s)
    found it, so a node can qualify via any one of the three."""
    existing_pairs = {frozenset((e["source"], e["target"])) for e in graph["edges"]}
    candidates = {}

    def _consider(other_id, score, relation):
        if other_id not in candidates or score > candidates[other_id][0]:
            candidates[other_id] = (score, relation)

    if node.get("embedding"):
        for other in graph["nodes"]:
            if other["id"] == node["id"] or not other.get("embedding"):
                continue
            sim = _cosine(node["embedding"], other["embedding"])
            if sim >= EMBED_SIM_THRESHOLD:
                _consider(other["id"], sim, "related")

    try:
        node_t = datetime.fromisoformat(node["created"])
        for other in graph["nodes"]:
            if other["id"] == node["id"]:
                continue
            other_t = datetime.fromisoformat(other["created"])
            gap = abs((node_t - other_t).total_seconds())
            if gap <= RECENCY_LINK_WINDOW_SECONDS:
                score = 0.5 + 0.4 * (1 - gap / RECENCY_LINK_WINDOW_SECONDS)
                _consider(other["id"], score, "same conversation")
    except Exception:
        pass

    new_kw = _keywords(node["content"])
    if new_kw:
        for other in graph["nodes"]:
            if other["id"] == node["id"]:
                continue
            overlap = len(new_kw & _keywords(other["content"]))
            if overlap > 0:
                _consider(other["id"], min(0.3 + overlap * 0.1, 0.9), "related")

    ranked = sorted(candidates.items(), key=lambda kv: kv[1][0], reverse=True)
    for other_id, (score, relation) in ranked[:max_links]:
        if frozenset((node["id"], other_id)) in existing_pairs:
            continue
        graph["edges"].append({
            "id": f"e_{uuid.uuid4().hex[:10]}",
            "source": node["id"],
            "target": other_id,
            "relation": relation,
            "weight": round(score, 3),
            "created": datetime.now().isoformat(),
        })

def graph_add_node(content, ntype="context", tags=None, confidence="stated", auto_link=True):
    content = (content or "").strip()
    if not content:
        return None
    for existing in _graph["nodes"]:
        if existing["content"].strip().lower() == content.lower():
            return existing
    node = {
        "id": f"n_{uuid.uuid4().hex[:10]}",
        "type": ntype if ntype in NODE_TYPES else "context",
        "content": content,
        "tags": tags or [],
        "confidence": confidence,
        "created": datetime.now().isoformat(),
        "embedding": _embed(content),
    }
    _graph["nodes"].append(node)
    if auto_link:
        _auto_link_node(_graph, node)
    save_graph(_graph)
    return node

def graph_update_node(node_id, content=None, ntype=None, tags=None):
    """Partial update for an existing node — lets a node be re-typed (or its
    text/tags edited) in place instead of deleting and recreating it, which
    would otherwise lose its edges."""
    for node in _graph["nodes"]:
        if node["id"] != node_id:
            continue
        if content is not None:
            content = content.strip()
            if content:
                node["content"] = content
                node["embedding"] = _embed(content)
        if ntype is not None and ntype in NODE_TYPES:
            node["type"] = ntype
        if tags is not None:
            node["tags"] = tags
        save_graph(_graph)
        return node
    return None

def graph_backfill_embeddings():
    """One-time, non-blocking backfill for nodes created before embeddings
    existed (or created while Ollama was down) — runs once at startup."""
    def _run():
        changed = False
        for node in _graph["nodes"]:
            if not node.get("embedding"):
                node["embedding"] = _embed(node["content"])
                changed = True
        if changed:
            for node in list(_graph["nodes"]):
                if node.get("embedding"):
                    _auto_link_node(_graph, node)
            save_graph(_graph)
            push_to_ui("graph_update", _graph_public(_graph))
            dbg("[GRAPH] embedding backfill complete")
    threading.Thread(target=_run, daemon=True).start()

def _graph_public(graph):
    """Strip embeddings before sending the graph to the UI — the frontend
    never needs the raw vectors, just nodes/edges."""
    return {
        "nodes": [{k: v for k, v in n.items() if k != "embedding"} for n in graph["nodes"]],
        "edges": graph["edges"],
    }

ALWAYS_INJECT_TYPES = ("preference", "warning", "system")
ALWAYS_INJECT_CAP_PER_TYPE = 8
RETRIEVE_TOP_K = 8
RETRIEVE_SIM_THRESHOLD = 0.35

def graph_retrieve_context(user_input, top_k=RETRIEVE_TOP_K):
    """Picks the small slice of the memory graph that actually goes into
    this turn's system prompt — NOT the whole graph. This is the whole
    point of the graph existing: token/latency cost stays flat as the graph
    grows into the hundreds, because what gets injected is capped
    regardless of how much is stored.

    Cost per call: one local embedding (same call already used when a node
    is created) plus a cosine-similarity scan over cached vectors — no LLM
    call, no network request beyond the local Ollama one. Falls back to
    keyword overlap if Ollama's unreachable, so retrieval degrades instead
    of silently returning nothing.
    """
    if not _graph["nodes"]:
        return []

    always = []
    for t in ALWAYS_INJECT_TYPES:
        matches = sorted(
            (n for n in _graph["nodes"] if n["type"] == t),
            key=lambda n: n.get("created", ""), reverse=True
        )[:ALWAYS_INJECT_CAP_PER_TYPE]
        always.extend(matches)

    relevant = []
    query_emb = _embed(user_input)
    if query_emb:
        scored = []
        for n in _graph["nodes"]:
            if not n.get("embedding"):
                continue
            sim = _cosine(query_emb, n["embedding"])
            if sim >= RETRIEVE_SIM_THRESHOLD:
                scored.append((sim, n))
        scored.sort(key=lambda x: x[0], reverse=True)
        relevant = [n for _, n in scored[:top_k]]
    else:
        kw = _keywords(user_input)
        if kw:
            scored = []
            for n in _graph["nodes"]:
                overlap = len(kw & _keywords(n["content"]))
                if overlap > 0:
                    scored.append((overlap, n))
            scored.sort(key=lambda x: x[0], reverse=True)
            relevant = [n for _, n in scored[:top_k]]

    seen, out = set(), []
    for n in always + relevant:
        if n["id"] not in seen:
            seen.add(n["id"])
            out.append(n)
    return out

def graph_add_edge(source_id, target_id, relation="related", weight=1):
    ids = {n["id"] for n in _graph["nodes"]}
    if source_id not in ids or target_id not in ids or source_id == target_id:
        return None
    for e in _graph["edges"]:
        if {e["source"], e["target"]} == {source_id, target_id}:
            return e
    edge = {
        "id": f"e_{uuid.uuid4().hex[:10]}",
        "source": source_id,
        "target": target_id,
        "relation": relation or "related",
        "weight": weight,
        "created": datetime.now().isoformat(),
    }
    _graph["edges"].append(edge)
    save_graph(_graph)
    return edge

def graph_delete_node(node_id):
    _graph["nodes"] = [n for n in _graph["nodes"] if n["id"] != node_id]
    _graph["edges"] = [e for e in _graph["edges"] if e["source"] != node_id and e["target"] != node_id]
    save_graph(_graph)

def graph_delete_edge(edge_id):
    _graph["edges"] = [e for e in _graph["edges"] if e["id"] != edge_id]
    save_graph(_graph)

def graph_seed_from_facts(memory):
    """One-time migration on first run: turn the existing flat facts list
    into graph nodes so the Brain tab isn't empty the first time it opens."""
    if _graph["nodes"]:
        return
    for fact in memory.get("facts", []):
        clean = re.sub(r"^\[[^\]]*\]\s*", "", fact).strip()
        if not clean:
            continue
        confidence = "inferred" if "[inferred pattern]" in fact else "stated"
        graph_add_node(clean, ntype="experience" if confidence == "inferred" else "context",
                        confidence=confidence, auto_link=False)
    for node in _graph["nodes"]:
        _auto_link_node(_graph, node)
    save_graph(_graph)

SYSTEM_CAPABILITY_SEED = [
    "Can search the web for trending topics and general info via Groq web search.",
    "Can see and interpret the screen when Vision is enabled, using Google Gemini (gemini-3.1-flash-lite).",
    "Can search and play YouTube videos directly using yt_dlp.",
    "Can locate and open apps on this PC, and learns new app locations over time (learned_apps.json).",
    "Can break a natural-language instruction into multiple steps and execute them as a compound task plan.",
    "Can speak replies aloud using a local Kokoro ONNX text-to-speech voice, non-blocking via a queue.",
    "Falls back to a local Ollama model if the primary Groq model (openai/gpt-oss-120b) is unavailable.",
    "Can take any multi-step job described in plain words, plan it with her tools (files, browser, desktop apps, downloads, images, vision, reminders), adapt and retry when a step fails, verify the result, and learn lessons for next time.",
    "Runs recurring automations that are created and changed only by chat or the Active Tasks panel: daily, weekly on chosen days, every N minutes, once at a set time, or when a window opens. Asks permission before anything new and learns which actions to stop asking about; destructive actions are always asked.",
    "Cannot put this PC to sleep — the HP Victus 15-fa2xxx only supports Modern Standby (S0), so sleep is not a supported action.",
]

def graph_ensure_system_nodes():
    """Add any capability line from SYSTEM_CAPABILITY_SEED that the graph doesn't have yet (matched by text), so an
    existing Brain tab learns about new abilities too - the one-time seed below skips graphs that already have nodes."""
    have = {n["content"].strip().lower() for n in _graph["nodes"] if n.get("type") == "system"}
    added = [f for f in SYSTEM_CAPABILITY_SEED if f.strip().lower() not in have]
    if not any(n["type"] == "system" for n in _graph["nodes"]):
        return
    for fact in added:
        graph_add_node(fact, ntype="system", confidence="stated", auto_link=False)
    if added:
        for node in _graph["nodes"]:
            if node["type"] == "system" and node["content"] in added:
                _auto_link_node(_graph, node)
        save_graph(_graph)
        push_to_ui("graph_update", _graph_public(_graph))
        dbg(f"[GRAPH] added {len(added)} new capability node(s)")

def graph_seed_system_capabilities():
    if any(n["type"] == "system" for n in _graph["nodes"]):
        return
    for fact in SYSTEM_CAPABILITY_SEED:
        graph_add_node(fact, ntype="system", confidence="stated", auto_link=False)
    for node in _graph["nodes"]:
        if node["type"] == "system":
            _auto_link_node(_graph, node)
    save_graph(_graph)

TEMPORARY_TTL_DAYS = 7

def graph_expire_temporary_nodes():
    cutoff = datetime.now() - timedelta(days=TEMPORARY_TTL_DAYS)
    expired_ids = []
    for node in _graph["nodes"]:
        if node.get("type") != "temporary":
            continue
        try:
            created = datetime.fromisoformat(node["created"])
        except Exception:
            continue
        if created < cutoff:
            expired_ids.append(node["id"])
    for nid in expired_ids:
        graph_delete_node(nid)
    if expired_ids:
        push_to_ui("graph_update", _graph_public(_graph))
        dbg(f"[GRAPH] expired {len(expired_ids)} temporary node(s) older than {TEMPORARY_TTL_DAYS} days")

def _graph_expiry_loop():
    while True:
        try:
            graph_expire_temporary_nodes()
        except Exception as e:
            dbg(f"[GRAPH] expiry check failed: {e}")
        time.sleep(3600)

def graph_start_expiry_scheduler():
    threading.Thread(target=_graph_expiry_loop, daemon=True).start()


def read_own_code():
    try:
        with open(SELF_FILE, "r") as f:
            return f.read()
    except Exception:
        return None

def get_code_hash(code):
    return hashlib.md5(code.encode()).hexdigest()

def summarize_own_capabilities(code):
    if not code:
        return "Unable to read own source code."
    actions = re.findall(r'"(open_\w+)"\s*:', code)
    s  = f"Local app shortcuts ({len(actions)}): {', '.join(actions)}\n"
    s += "Smart web opener: active\n"
    s += "Downloader: yt-dlp for media, requests for files, Selenium for complex pages\n"
    s += "Memory: facts, preferences, session summaries persisted\n"
    s += "Self-awareness: detects code changes automatically\n"
    s += "Screen vision: screenshot every 2s\n"
    s += "Watch-tasks: monitors one named window in the background (not just the active one) and shuts down/restarts the laptop or just notifies once a described condition appears; desktop toast + voice/chat on trigger\n"
    s += "Scheduled actions: time-based (no watching) — relative delay or a clock time, e.g. \"remind me in 10 minutes\" or \"shut down at 11:50 PM\" — desktop toast + voice/chat when it fires\n"
    s += ("Automations and jobs: any multi-step job can be described in plain words and I plan it, use my tools, "
          "adapt and retry when a step fails, check the result, and learn for next time. Recurring tasks (daily, weekly, "
          "every N minutes, once at a time, or when a window opens) are created, edited, paused, stopped, undone and "
          "deleted just by telling me in chat or from the Active Tasks panel; nothing about them is built in.\n")
    s += "Trending: real-time world news and data on request\n"
    s += ("File management: can open/list/rename/move/delete real files and folders across the user's "
          "drives (Desktop/Documents/Downloads/Pictures/Videos/Music plus a full C:/D: fallback search) "
          "— NEVER claim 'I don't have access to your files' or similar, this is a real, working "
          "capability.\n")
    s += ("File reading: can extract and summarize/answer questions about PDF, Word (.docx), Excel "
          "(.xlsx), and plain text file CONTENTS directly — NEVER claim 'share the text and I'll help', "
          "this is a real, working capability, not something that needs the user to paste content "
          "manually.\n")
    s += f"Hybrid AI: Groq {GROQ_MODEL} online, {LOCAL_MODEL} offline\n"
    s += f"Voice: Kokoro offline TTS, voice={XENORA_VOICE}\n"
    return s

def refresh_self_awareness():
    code = read_own_code()
    h    = get_code_hash(code) if code else ""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        if cache.get("hash") == h:
            return code, h, cache["summary"]
    summary = summarize_own_capabilities(code)
    with open(CACHE_FILE, "w") as f:
        json.dump({"hash": h, "summary": summary}, f)
    return code, h, summary


APP_DISPLAY_NAMES = {
    "open_notepad":             "Notepad",
    "open_documents":           "your Documents folder",
    "open_minecraft":           "Minecraft Launcher",
    "open_blender":             "Blender 5.0",
    "open_controlpanel":        "Control Panel",
    "open_discord":             "Discord",
    "open_gtav":                "GTA V Enhanced",
    "open_mediaplayer":         "Media Player",
    "open_nvidiaapp":           "the NVIDIA App",
    "open_nvidiacontrolpanel":  "NVIDIA Control Panel",
    "open_omen":                "OMEN Gaming Hub",
    "open_outlook":             "Microsoft Outlook",
    "open_powerpoint":          "PowerPoint",
    "open_rockstarlauncher":    "Rockstar Games Launcher",
    "open_sklauncher":          "SKLauncher",
    "open_steam":               "Steam",
    "open_vscode":              "Visual Studio Code",
    "open_wallpaperengine":     "Wallpaper Engine",
    "open_microsoftexcel":      "Microsoft Excel",
    "open_paint":               "Paint",
    "open_opera":               "Opera GX",
    "open_unity":               "Unity Hub",
}

ACTION_MAP = {
    "open_notepad":             lambda: os.system("notepad"),
    "open_documents":           lambda: os.startfile(r"C:\Users\Hp\OneDrive\Documents"),
    "open_minecraft":           lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Minecraft Launcher - Shortcut.lnk"),
    "open_blender":             lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Blender 5.0 - Shortcut.lnk"),
    "open_controlpanel":        lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Control Panel - Shortcut.lnk"),
    "open_discord":             lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Discord - Shortcut.lnk"),
    "open_gtav":                lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Grand Theft Auto V Enhanced - Shortcut.lnk"),
    "open_mediaplayer":         lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Media Player - Shortcut.lnk"),
    "open_nvidiaapp":           lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\NVIDIA App - Shortcut.lnk"),
    "open_nvidiacontrolpanel":  lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\NVIDIA Control Panel - Shortcut.lnk"),
    "open_omen":                lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\OMEN Gaming Hub - Shortcut.lnk"),
    "open_outlook":             lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Outlook - Shortcut.lnk"),
    "open_powerpoint":          lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\PowerPoint - Shortcut.lnk"),
    "open_rockstarlauncher":    lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Rockstar Games Launcher - Shortcut.lnk"),
    "open_sklauncher":          lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\SKlauncher - Shortcut.lnk"),
    "open_steam":               lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Steam - Shortcut.lnk"),
    "open_vscode":              lambda: subprocess.Popen(
                                    [r"D:\Xenora AI\Software Shortcuts\Visual Studio Code - Shortcut.lnk"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True),
    "open_wallpaperengine":     lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Wallpaper Engine - Shortcut.lnk"),
    "open_microsoftexcel":      lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Excel - Shortcut.lnk"),
    "open_paint":               lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Paint - Shortcut.lnk"),
    "open_opera":               lambda: os.startfile(r"C:\Users\Hp\AppData\Local\Programs\Opera GX\opera.exe"),
    "open_unity":               lambda: os.startfile(r"D:\Xenora AI\Software Shortcuts\Unity Hub.lnk"),
}

INTENT_KEYWORDS = {
    "open_notepad":             ["notepad", "note", "jot", "text editor"],
    "open_documents":           ["documents", "my files", "file explorer"],
    "open_minecraft":           ["minecraft", "play minecraft"],
    "open_blender":             ["blender", "3d animation", "animation"],
    "open_controlpanel":        ["control panel"],
    "open_discord":             ["discord"],
    "open_gtav":                ["gta five", "gta v", "gta", "gta enhanced", "play gta"],
    "open_mediaplayer":         ["media player", "mediaplayer"],
    "open_nvidiaapp":           ["nvidia app", "gpu drivers"],
    "open_nvidiacontrolpanel":  ["nvidia control panel", "nvidia control", "nvidia panel"],
    "open_omen":                ["gaming hub", "omen", "omen gaming hub"],
    "open_outlook":             ["outlook", "microsoft outlook"],
    "open_powerpoint":          ["powerpoint", "microsoft powerpoint"],
    "open_rockstarlauncher":    ["rockstar launcher"],
    "open_sklauncher":          ["sklauncher"],
    "open_steam":               ["steam", "steam store"],
    "open_vscode":              ["vs code", "vscode", "visual studio"],
    "open_wallpaperengine":     ["wallpaper engine"],
    "open_microsoftexcel":      ["microsoft excel", "excel"],
    "open_paint":               ["paint"],
    "open_opera":               ["opera", "opera gx"],
    "open_unity":               ["unity", "unity hub", "game development"],
}

OPEN_INTENT_WORDS = [
    "open", "go to", "launch", "visit", "take me to", "search",
    "find", "look up", "browse", "show me", "play", "stream", "watch", "listen", "check"
]

NON_ACTION_PATTERNS = [
    r"\b(is|was|are|were|has|have|had)\b",
    r"\b(love|hate|like|enjoy|miss|need)\b",
    r"\b(so|very|really|quite|pretty)\b",
    r"\b(lol|lmao|haha|damn|omg|wow)\b",
    r"\b(think|thought|feel|felt|seems)\b",
    r"\b(deleted|removed|broken|crashed)\b",
    r"\b(already|just|still|again)\b",
    r"\b(hello|hi|hey|hiya|yo|sup|thanks|thank you|bye|goodnight|good night)\b",
    r"[?]",
]

def looks_like_explicit_app_open(text):
    """Stricter than is_open_request() below — requires an actual open-intent
    VERB match, not that function's "2 words or fewer" catch-all fallback.
    That fallback is fine for its original use (smart_open() has its own
    LLM-based NONE/URL/SEARCH check downstream, which gracefully backs off
    for anything that isn't really an open request) — but handle_local_app_
    open() has no such safety net once triggered, it just goes straight to
    "search the filesystem, then ask you for a path." Without this stricter
    gate, a plain 2-word message like "hello xenora" hit that same bare
    fallback and got treated as a request to open an app named "hello
    xenora" — confirmed exactly that in testing."""
    t = text.lower().strip()
    for pattern in NON_ACTION_PATTERNS:
        if re.search(pattern, t):
            return False
    return any(t.startswith(word) or f" {word} " in f" {t} " for word in OPEN_INTENT_WORDS)

def confirm_app_open_intent(text):
    """BUG FIX: looks_like_explicit_app_open() above is still just keyword
    matching, and keyword matching alone can't distinguish "open cyberpunk"
    (a command) from "I'm gonna play cyberpunk, I'm almost at its ending"
    (narration that happens to contain "play", which is in
    OPEN_INTENT_WORDS) — confirmed exactly that in testing. handle_local_
    app_open() has no downstream safety net once triggered (unlike
    smart_open(), which has its own LLM-based NONE/URL/SEARCH check), so it
    needs a real semantic check, not a stricter keyword list — no static
    word list can fully solve this. Only runs after the cheap keyword gate
    above already matched, so it's not an extra call on every message.
    Returns a clean app name if this really is a command, else None."""
    try:
        raw = llm(
            "Is this message a DIRECT COMMAND to open/launch a specific application on the user's "
            "computer — not just casually mentioning an app, narrating an intention, or describing "
            "something they're already doing? Reply with ONLY JSON: "
            '{"is_command": true or false, "app_name": "..." or null}.\n'
            'Examples: "open cyberpunk" -> {"is_command": true, "app_name": "cyberpunk"}. '
            '"launch discord for me" -> {"is_command": true, "app_name": "discord"}. '
            '"I\'m gonna play cyberpunk, I\'m almost at its ending" -> {"is_command": false, "app_name": null} '
            "(narration, not a command). \"I love playing minecraft\" -> {\"is_command\": false, \"app_name\": null}.",
            text, temperature=0.1
        )
        cleaned = re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip()
        data = json.loads(cleaned)
        if data.get("is_command") and data.get("app_name"):
            return str(data["app_name"]).strip()
    except Exception as e:
        dbg(f"[APPS] intent confirm error: {e}")
    return None

DOWNLOAD_INTENT_WORDS = ["download", "save for me", "get me", "grab", "fetch", "save this"]
MEDIA_HINTS = ["song", "music", "mp3", "mp4", "video", "audio", "track",
               "album", "movie", "film", "installer", "setup", "pdf"]

def is_open_request(text):
    t = text.lower().strip()
    for pattern in NON_ACTION_PATTERNS:
        if re.search(pattern, t):
            return False
    for word in OPEN_INTENT_WORDS:
        if t.startswith(word) or f" {word} " in f" {t} ":
            return True
    return len(t.split()) <= 2

def is_download_request(text):
    t = text.lower()
    return any(w in t for w in DOWNLOAD_INTENT_WORDS) or any(h in t for h in MEDIA_HINTS)

def keyword_fallback(text):
    if not is_open_request(text):
        return []
    t = text.lower()
    return [action for action, keywords in INTENT_KEYWORDS.items()
            if any(kw in t for kw in keywords)]

def extract_actions_from_reply(reply):
    try:
        clean   = re.sub(r"^```[\w]*", "", reply.strip()).rstrip("`").strip()
        actions = json.loads(clean).get("commands", [])
        if actions:
            return [a.strip().lower() for a in actions]
    except (ValueError, KeyError):
        pass
    matches = re.findall(r"COMMAND:\s*(\w+)", reply, re.IGNORECASE)
    if matches:
        return [m.strip().lower() for m in matches]
    bare = re.findall(r"\bopen_\w+\b", reply, re.IGNORECASE)
    return [b.strip().lower() for b in bare] if bare else []


LEARNED_APPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_apps.json")
_learned_apps = {}

def load_learned_apps():
    global _learned_apps
    try:
        if os.path.exists(LEARNED_APPS_FILE):
            with open(LEARNED_APPS_FILE, "r", encoding="utf-8") as f:
                _learned_apps = json.load(f)
    except Exception as e:
        dbg(f"[APPS] load error: {e}")
        _learned_apps = {}

def save_learned_apps():
    try:
        with open(LEARNED_APPS_FILE, "w", encoding="utf-8") as f:
            json.dump(_learned_apps, f, indent=2)
    except Exception as e:
        dbg(f"[APPS] save error: {e}")

load_learned_apps()

def search_for_app(name):
    """Searches Start Menu shortcuts first, then a shallow scan of common
    install directories, for something matching `name`. Returns a
    launchable path or None. Only runs on the FIRST launch of an unknown
    app — see handle_local_app_open(), which checks _learned_apps before
    ever calling this."""
    needle = re.sub(r'[^a-z0-9]', '', name.lower())
    if not needle:
        return None

    start_menu_dirs = [
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    for base in start_menu_dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for f in files:
                if f.lower().endswith((".lnk", ".exe")):
                    stem = re.sub(r'[^a-z0-9]', '', os.path.splitext(f)[0].lower())
                    if stem and (needle in stem or stem in needle):
                        return os.path.join(root, f)

    program_dirs = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    for base in program_dirs:
        if not base or not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                stem = re.sub(r'[^a-z0-9]', '', entry.lower())
                if not stem or not (needle in stem or stem in needle):
                    continue
                folder = os.path.join(base, entry)
                if not os.path.isdir(folder):
                    continue
                for root, _, files in os.walk(folder):
                    for f in files:
                        if f.lower().endswith(".exe"):
                            return os.path.join(root, f)
                    break
        except Exception:
            continue
    return None

_pending_app_location = None

def handle_local_app_open(user_input):
    """Tries the self-learning locator before ever falling through to a web
    search. Order: already-learned path (instant) -> filesystem search
    (only on a genuinely new app) -> ask the user directly, rather than
    silently failing or guessing wrong."""
    global _pending_app_location
    name = re.sub(
        r"\b(open|launch|start|run|please|for me|the|app|application)\b", "", user_input, flags=re.IGNORECASE
    ).strip()
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return False
    key = name.lower()

    if key in _learned_apps:
        path = _learned_apps[key]
        try:
            os.startfile(path)
            msg = f"Opening {name}."
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return True
        except Exception as e:
            dbg(f"[APPS] learned path for {name!r} failed ({e}) — path likely moved/uninstalled, re-searching")
            del _learned_apps[key]

    msg = f"I don't have {name} saved — searching for it..."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    found_path = search_for_app(name)
    if found_path:
        try:
            os.startfile(found_path)
            _learned_apps[key] = found_path
            save_learned_apps()
            msg2 = f"Found it — opening {name}. I'll remember this next time."
            add_chat_message("assistant", msg2); speak(msg2, style="cheerful")
            return True
        except Exception as e:
            dbg(f"[APPS] found {name!r} at {found_path!r} but launch failed: {e}")

    _pending_app_location = {"name": name}
    msg3 = (f"I couldn't find {name} on your system — what's the file path? "
            f"(Or say \"never mind\" and I'll try a web search instead.)")
    add_chat_message("assistant", msg3); speak(msg3, style="empathetic")
    return True


def open_app_by_name(name):
    """Best-effort single-app open. Tries the fast hardcoded shortcut first
    if one exists — but a broken/moved/missing shortcut (like Unity Hub's
    .lnk) must NEVER crash the whole request the way an unguarded
    ACTION_MAP[action]() call used to. Any failure here falls straight
    through to the exact same self-learning flow a never-hardcoded app gets:
    check what's already been learned, search the filesystem once, and if
    genuinely not found, ask directly for the path — never silently fail,
    never just die.
    Returns (status, label):
      "opened"    — hardcoded shortcut worked, label is the display name
      "delegated" — handle_local_app_open() already handled messaging
                    (opened via learned/found path, or asked for a path)
    """
    kw_actions = keyword_fallback(f"open {name}")
    for action in kw_actions:
        if action in ACTION_MAP:
            try:
                ACTION_MAP[action]()
                return "opened", APP_DISPLAY_NAMES.get(action, name)
            except Exception as e:
                dbg(f"[APPS] hardcoded shortcut for {name!r} failed ({e}) — falling back to filesystem search")
    handle_local_app_open(f"open {name}")
    return "delegated", name


APP_REPLIES = [
    "Launching {app}.",
    "{app}, loading now.",
    "{app} is up.",
    "Opening {app}.",
    "Right away.",
    "{app} — on it.",
]

WEB_REPLIES = [
    "Pulling that up.",
    "On it.",
    "Opening that now.",
    "Done.",
    "Search is running.",
    "Right away.",
]

def dynamic_app_response(actions, user_input):
    """Kept for the trailing COMMAND: fallback in ask_xenora()'s general
    replies. Each ACTION_MAP call is now guarded — a broken hardcoded
    shortcut degrades to the self-learning locator for that one app instead
    of throwing and killing every action still queued after it."""
    opened = []
    for action in actions:
        if action not in ACTION_MAP:
            continue
        try:
            ACTION_MAP[action]()
            opened.append(APP_DISPLAY_NAMES.get(action, action))
        except Exception as e:
            dbg(f"[APPS] hardcoded shortcut for {action!r} failed ({e}) — falling back to filesystem search")
            handle_local_app_open(f"open {APP_DISPLAY_NAMES.get(action, action)}")
    if opened:
        apps_str = opened[0] if len(opened) == 1 else ", ".join(opened[:-1]) + f" and {opened[-1]}"
        msg = random.choice(APP_REPLIES).format(app=apps_str)
        add_chat_message("assistant", msg)
        speak(msg, style="chat")

def _tavily_cooldown_set(status_code, err):
    secs = {429: 60, 432: 6*3600, 433: 6*3600, 401: 6*3600}.get(status_code, 30)
    _model_cooldown["tavily"] = time.time() + secs
    dbg(f"[COOLDOWN] tavily skipped for {secs}s (auto-retries after that) — {err}")

def _tavily_search(query, max_results=5):
    """Raises on any failure (no key, cooldown active, HTTP error) so callers use the same
    try/except pattern as every other provider in this file. Returns Tavily's parsed JSON:
    {"answer": "...", "results": [{"title","url","content",...}, ...]}."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not configured")
    if _cooldown_active("tavily"):
        raise RuntimeError("Tavily is cooling down")
    r = requests.post(TAVILY_API_URL, timeout=20, json={
        "api_key": TAVILY_API_KEY, "query": query, "search_depth": "basic",
        "include_answer": True, "max_results": max_results,
    })
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", {}).get("error", r.text)
        except Exception:
            detail = r.text
        _tavily_cooldown_set(r.status_code, detail)
        raise RuntimeError(f"Tavily {r.status_code}: {detail}")
    return r.json()

def web_search_answer(query):
    """Searches, then has Xenora's own chat model phrase the answer in her voice from the
    real results — never Tavily's raw snippets verbatim, and never a source citation (matches
    how she talks normally; the person asked for a natural answer, not a research report).
    Returns the reply text, or None if search genuinely failed (caller decides what to say)."""
    data = _tavily_search(query)
    results = data.get("results") or []
    tavily_answer = data.get("answer") or ""
    if not results and not tavily_answer:
        return None
    context = "\n\n".join(f"- {r.get('title','')}: {(r.get('content') or '')[:500]}" for r in results[:5])
    return llm(
        "Answer the user's question using the live web search results below. Give a direct, natural "
        "answer in your own voice, like you already knew it — never say 'according to', never name or "
        "link a source, never mention that you searched. Keep it conversational length, not a report. "
        "If the results genuinely don't answer the question, say so plainly instead of guessing.",
        f"Question: {query}\n\nSearch summary: {tavily_answer}\n\nTop results:\n{context}",
        temperature=0.4,
    ).strip()

def _handle_web_search(action_item, user_input):
    query = (action_item.get("target") or user_input).strip()
    if not query:
        return False
    try:
        reply = web_search_answer(query)
    except Exception as e:
        dbg(f"[SEARCH] Tavily failed: {e}")
        reply = None
    if reply is None:
        msg = "I couldn't get real search results for that just now — want me to open a browser search instead?"
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    chat_history.append({'role': 'user', 'content': user_input})
    chat_history.append({'role': 'assistant', 'content': reply})
    add_chat_message("assistant", reply, remember=False)
    speak(reply, style="chat")
    return True

def dynamic_web_response(action_type, target):
    return random.choice(WEB_REPLIES)


def smart_open(user_input):
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in chat_history[-6:])
    result = llm(
        "The user wants to open or visit something online. Resolve pronouns like 'it'/'them'/'that' "
        "using the recent conversation below — if you just discussed specific products/names and the "
        "user says 'search them up', search for those specific names, not the literal pronoun.\n"
        f"Recent conversation:\n{recent}\n\n"
        "Reply with ONLY one of:\nURL: https://example.com\nSEARCH: query\nNONE\nOne line only.",
        user_input
    ).strip('"\'')

    if result.upper() == "NONE":
        return False
    if result.startswith("URL:"):
        url = result.split("URL:", 1)[1].strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        msg = dynamic_web_response("url", url)
        add_chat_message("assistant", msg)
        speak(msg)
        return True
    if result.startswith("SEARCH:"):
        query = result.split("SEARCH:", 1)[1].strip()
        webbrowser.open(f"https://www.google.com/search?q={query.replace(' ', '+')}")
        msg = dynamic_web_response("search", query)
        add_chat_message("assistant", msg)
        speak(msg)
        return True
    if result.startswith("http"):
        webbrowser.open(result)
        msg = dynamic_web_response("url", result)
        add_chat_message("assistant", msg)
        speak(msg)
        return True
    return False


def find_youtube_video(query):
    """Real YouTube search via yt_dlp (metadata only, skip_download=True) —
    NOT a guess. This is what fixes the "open the Avengers trailer" bug:
    smart_open() only ever asks the LLM to invent a URL itself, and the LLM
    has no real search capability — it either hallucinates a plausible-
    looking but fake video link, or falls back to a generic search-results
    page while describing it as if it had found the specific video (that's
    exactly what happened: it opened a youtube.com/results?search_query=...
    page and called it "the trailer"). yt_dlp's ytsearch actually queries
    YouTube and returns a real video with a real title and URL."""
    for client in ("android", "ios", "web"):
        try:
            ydl_opts = {
                'default_search': 'ytsearch1', 'quiet': True, 'no_warnings': True, 'skip_download': True,
                'extractor_args': {'youtube': {'player_client': [client]}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info  = ydl.extract_info(query, download=False)
                entry = next((e for e in info['entries'] if e), None) if 'entries' in info else info
                if not entry:
                    continue
                title = entry.get('title', query)
                url   = entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get('id') else None)
                if url:
                    return (title, url)
        except Exception as e:
            dbg(f"[YT_SEARCH] error via {client} client: {e}")
            continue
    return None

VIDEO_OPEN_NOUNS = ["trailer", "video", "song", "music video", "clip", "movie", "teaser", "gameplay"]
VIDEO_OPEN_VERBS = ["open", "show me", "play", "watch", "pull up", "give me", "find me", "put on"]

def detect_video_open_request(text):
    """Detects 'open/show me/play [a specific video]' intent so it routes to
    find_youtube_video() (a REAL search) instead of smart_open() (which just
    asks the LLM to guess a URL from memory — see find_youtube_video's
    docstring for what that produced in practice)."""
    t = text.lower()
    return any(v in t for v in VIDEO_OPEN_VERBS) and any(n in t for n in VIDEO_OPEN_NOUNS)

def open_youtube_video(user_input):
    """Strips filler words, runs a real search, and opens the actual result
    — with the confirmation message stating the REAL fetched title, not an
    invented one. Falls back to smart_open() (still better than nothing) if
    the search comes back empty."""
    query = re.sub(
        r"\b(open|show me|play|watch|pull up|give me|find me|put on|the|a|an|on youtube|"
        r"youtube|please|for me|now|link)\b", "", user_input, flags=re.IGNORECASE
    ).strip()
    query = re.sub(r"\s+", " ", query).strip() or user_input
    result = find_youtube_video(query)
    if not result:
        return smart_open(user_input)
    title, url = result
    webbrowser.open(url)
    msg = f'Found it — "{title}". Opening now.'
    add_chat_message("assistant", msg)
    speak(msg, style="cheerful")
    return True

def _resolve_xenora_default_dir():
    """The default location for anything Xenora creates or downloads
    without a specific destination mentioned ('the default folder', or
    just nothing specified) — set explicitly by the user rather than left
    as an arbitrary hardcoded guess. Falls back to Documents\\Xenora if D:
    genuinely doesn't exist on this machine, rather than assuming a drive
    letter that might not be there. Defined early (self-contained, no
    dependency on FILE_OP_KNOWN_FOLDERS which is defined much later in this
    file) since DOWNLOAD_FOLDER below needs it immediately."""
    preferred = r"D:\Xenora"
    if os.path.exists("D:\\"):
        return preferred
    return os.path.join(os.path.expanduser("~"), "Documents", "Xenora")

XENORA_DEFAULT_OUTPUT_DIR = _resolve_xenora_default_dir()
DOWNLOAD_FOLDER = XENORA_DEFAULT_OUTPUT_DIR
xenora_browser  = None

KNOWN_URLS = {
    "discord":       ("https://discord.com/api/downloads/distributions/app/installers/latest?channel=stable&platform=win&arch=x86", "DiscordSetup.exe"),
    "winrar":        ("https://www.rarlab.com/rar/winrar-x64-701.exe", "WinRAR-x64.exe"),
    "vlc":           ("https://download.videolan.org/pub/videolan/vlc/last/win64/vlc-3.0.21-win64.exe", "VLC-win64.exe"),
    "7zip":          ("https://www.7-zip.org/a/7z2408-x64.exe", "7-Zip-x64.exe"),
    "7-zip":         ("https://www.7-zip.org/a/7z2408-x64.exe", "7-Zip-x64.exe"),
    "python":        ("https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe", "Python-3.12.4.exe"),
    "chrome":        ("https://dl.google.com/chrome/install/latest/chrome_installer.exe", "ChromeSetup.exe"),
    "google chrome": ("https://dl.google.com/chrome/install/latest/chrome_installer.exe", "ChromeSetup.exe"),
    "steam":         ("https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe", "SteamSetup.exe"),
    "obs":           ("https://github.com/obsproject/obs-studio/releases/download/30.2.2/OBS-Studio-30.2.2-Windows.exe", "OBS-Studio.exe"),
    "notepad++":     ("https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe", "Notepad++.exe"),
    "git":           ("https://github.com/git-for-windows/git/releases/download/v2.46.0.windows.1/Git-2.46.0-64-bit.exe", "Git-64-bit.exe"),
    "vscode":        ("https://code.visualstudio.com/sha/download?build=stable&os=win32-x64-user", "VSCode-Setup.exe"),
    "vs code":       ("https://code.visualstudio.com/sha/download?build=stable&os=win32-x64-user", "VSCode-Setup.exe"),
    "zoom":          ("https://zoom.us/client/latest/ZoomInstaller.exe", "ZoomInstaller.exe"),
    "brave":         ("https://laptop-updates.brave.com/latest/winx64", "BraveSetup.exe"),
    "firefox":       ("https://download.mozilla.org/?product=firefox-latest&os=win64&lang=en-US", "Firefox-Setup.exe"),
}

def get_browser(headless=False):
    global xenora_browser
    if xenora_browser:
        try:
            _ = xenora_browser.current_url
            return xenora_browser
        except Exception:
            xenora_browser = None
    options = webdriver.ChromeOptions()
    options.binary_location = r"C:\Users\Hp\AppData\Local\Programs\Opera GX\opera.exe"
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {
        "download.default_directory":   DOWNLOAD_FOLDER,
        "download.prompt_for_download": False,
    })
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    xenora_browser = driver
    return driver

def close_browser():
    global xenora_browser
    if xenora_browser:
        try:
            xenora_browser.quit()
        except Exception:
            pass
        xenora_browser = None

def download_direct(url, suggested_name=None):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    try:
        filename = (suggested_name or os.path.basename(urlparse(url).path.split("?")[0]) or "xenora_download")
        if "." not in filename:
            filename += ".exe"
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        dl_id    = str(time.time())
        ui_state["downloads"].append({"id": dl_id, "name": filename, "percent": 0, "done": False})
        push_to_ui("download_update", ui_state["downloads"])

        resp  = requests.get(url, stream=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        total = int(resp.headers.get('content-length', 0))
        done  = 0
        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done / total * 100)
                    for dl in ui_state["downloads"]:
                        if dl["id"] == dl_id:
                            dl["percent"] = pct
                    push_to_ui("download_update", ui_state["downloads"])

        for dl in ui_state["downloads"]:
            if dl["id"] == dl_id:
                dl["percent"]  = 100
                dl["done"]     = True
                dl["filepath"] = filepath
        push_to_ui("download_update", ui_state["downloads"])
        msg = f"Download complete. {filename} is ready."
        add_chat_message("assistant", msg)
        speak(msg, style="friendly")
        return True
    except Exception as e:
        dbg(f"[DOWNLOAD] direct error: {e}")
        return False

def download_youtube(query, original_input=""):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    check_text = (query + " " + original_input).lower()
    want_video = any(kw in check_text for kw in ["video", "mp4", "watch"])

    for node in graph_retrieve_context(check_text):
        if node["type"] in ("warning", "preference") and "mp4" in node["content"].lower():
            want_video = True
            dbg(f"[DOWNLOAD] memory overrode format choice: {node['content']!r}")
            break

    dl_id = str(time.time())
    ui_state["downloads"].append({"id": dl_id, "name": query, "percent": 0, "done": False})
    push_to_ui("download_update", ui_state["downloads"])

    _last_pct, _last_push_t = [-1], [0.0]
    def _progress_hook(d):
        if d.get('status') != 'downloading':
            return
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        if not total:
            return
        pct = int(d.get('downloaded_bytes', 0) / total * 100)
        now = time.monotonic()
        if pct == _last_pct[0] or (now - _last_push_t[0] < 0.3):
            return
        _last_pct[0], _last_push_t[0] = pct, now
        for dl in ui_state["downloads"]:
            if dl["id"] == dl_id:
                dl["percent"] = pct
        push_to_ui("download_update", ui_state["downloads"])

    base_opts = {
        'default_search': 'ytsearch1',
        'quiet':          True,
        'no_warnings':    True,
        'ffmpeg_location': r'C:\Users\Hp',
        'outtmpl':        os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
        'progress_hooks': [_progress_hook],
    }
    if not want_video:
        base_opts['format']         = 'bestaudio/best'
        base_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
    else:
        base_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]'

    msg = "On it."
    add_chat_message("assistant", msg)
    speak(msg, style="chat")

    last_error = None
    for client in ("android", "ios", "web"):
        ydl_opts = dict(base_opts)
        ydl_opts['extractor_args'] = {'youtube': {'player_client': [client]}}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info  = ydl.extract_info(query, download=True)
                entry = next((e for e in info['entries'] if e), None) if 'entries' in info else None
                title = (entry or info).get('title', query)
                raw_path = ydl.prepare_filename(entry or info)
                filepath = (os.path.splitext(raw_path)[0] + ".mp3") if not want_video else raw_path
            for dl in ui_state["downloads"]:
                if dl["id"] == dl_id:
                    dl["percent"]  = 100
                    dl["done"]     = True
                    dl["name"]     = title
                    dl["filepath"] = filepath
            push_to_ui("download_update", ui_state["downloads"])
            msg = f"{title} — downloaded."
            add_chat_message("assistant", msg)
            speak(msg, style="friendly")
            return True
        except Exception as e:
            last_error = e
            dbg(f"[DOWNLOAD] yt error via {client} client: {e}")
            continue

    dbg(f"[DOWNLOAD] yt error: all clients failed, last error: {last_error}")
    msg = "Could not retrieve that. It may be unavailable or restricted."
    add_chat_message("assistant", msg)
    speak(msg, style="chat")
    return False

def extract_media_from_context(description):
    if not description:
        return None
    quoted = re.findall(r'["\u201c\u201d]([^""\u201c\u201d]{3,80})["\u201c\u201d]', description)
    if quoted:
        return quoted[0]

    m = re.search(
        r'(?:titled|called)\s+(.+?)\s+in\s+(hindi|tamil|telugu|kannada|malayalam|english|punjabi|bengali|marathi)\b',
        description, re.IGNORECASE)
    if m:
        return f"{m.group(1).strip()} {m.group(2).strip()}"

    m = re.search(r'(?:titled|called)\s+["\']?(.+?)["\']?\s*(?:\.|,|$)', description, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    patterns = [
        r'playing\s+["\']?(.+?)["\']?\s+(?:by|on|from|\|)',
        r'(?:song|track|video|audio)[:\s]+["\']?(.+?)["\']?\s+(?:by|\.|,|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None

def get_download_intent(user_input):
    t = user_input.lower()
    is_screen_ref = any(w in t for w in ["this song", "that song", "this video", "that video", "playing", "on screen"])
    if is_screen_ref and _last_context.get('description'):
        name = extract_media_from_context(_last_context['description'])
        if name:
            return {"type": "youtube", "query": name}
    try:
        context = f"\nLast screen observation: {_last_context.get('description','')}" if _last_context.get('description') else ""
        raw  = llm(
            "This message passed a loose keyword filter for download-related requests, but not "
            "every mention of 'download(s)' is actually asking Xenora to fetch/save something — "
            "e.g. \"can you see my downloads?\" is a question, not a request. Decide first, then "
            "classify. Return JSON only:\n"
            "{\"is_download\": true|false, \"type\": \"youtube\" | \"direct\" | \"search\", \"query\": \"...\"}\n"
            f"{context}\nReturn ONLY valid JSON.",
            user_input
        )
        data = json.loads(re.sub(r"^```[\w]*", "", raw).rstrip("`").strip())
        if not data.get("is_download", True):
            return None
        return {"type": data.get("type", "search"), "query": data.get("query") or user_input}
    except Exception:
        return {"type": "search", "query": user_input}

def silent_software_download(query):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    q = query.lower().strip()
    for key, (url, filename) in KNOWN_URLS.items():
        if key in q:
            msg = f"Downloading {key.title()}."
            add_chat_message("assistant", msg)
            speak(msg, style="chat")
            if download_direct(url, suggested_name=filename):
                return True
            break
    url_resp = llm(
        "Return ONLY the direct .exe or .zip download URL for this software. If not 100% sure, return: UNKNOWN",
        query
    ).strip().strip('"\'')
    if url_resp != "UNKNOWN" and url_resp.startswith("http"):
        name = os.path.basename(urlparse(url_resp).path.split("?")[0]) or f"{query}.exe"
        msg  = f"Downloading {query}."
        add_chat_message("assistant", msg)
        speak(msg, style="chat")
        if download_direct(url_resp, suggested_name=name):
            return True
    msg = "Opening the download page."
    add_chat_message("assistant", msg)
    speak(msg, style="chat")
    try:
        driver = get_browser(headless=False)
        target = llm("Return the best official download page URL for this software. Return ONLY the URL or GOOGLE if unsure.", query).strip()
        driver.get(target if (target != "GOOGLE" and target.startswith("http"))
                   else f"https://www.google.com/search?q={query.replace(' ', '+')}+download")
    except Exception:
        pass
    return False

def _is_ambiguous_download(text):
    t = text.lower()
    software_hints = ["installer", "setup", "software", "app", "program"]
    media_hints    = ["song", "music", "mp3", "audio", "track", "album"]
    game_names     = ["gta", "minecraft", "steam", "fortnite", "valorant", "roblox"]
    has_software   = any(w in t for w in software_hints)
    has_media      = any(w in t for w in media_hints)
    has_game       = any(g in t for g in game_names)
    if has_software and has_media:
        return "Did you want a song or a software installer?"
    if has_game and has_media:
        game = next(g for g in game_names if g in t)
        return f"Did you want a {game} song, or the {game} installer?"
    if has_game and has_software:
        game = next(g for g in game_names if g in t)
        return f"Did you want to download {game} itself, or something else related to it?"
    return None

def handle_download(user_input, resolved_target=None, media_type="audio"):
    if resolved_target:
        _log_action(f"Downloading '{resolved_target}' ({media_type}).")
        if media_type == "software":
            threading.Thread(target=silent_software_download, args=(resolved_target,), daemon=True).start()
        else:
            threading.Thread(
                target=download_youtube, args=(resolved_target,),
                kwargs={"original_input": user_input}, daemon=True
            ).start()
        return True

    ambiguous = _is_ambiguous_download(user_input)
    if ambiguous:
        add_chat_message("assistant", ambiguous)
        speak(ambiguous, style="chat")
        push_to_ui("awaiting_clarification", {"question": ambiguous})
        return True

    intent = get_download_intent(user_input)
    if intent is None:
        return False

    dtype  = intent.get("type", "search")
    query  = intent.get("query", user_input)

    def _dl():
        if dtype == "direct":
            if not download_direct(query):
                silent_software_download(query)
        elif dtype == "youtube":
            download_youtube(query, original_input=user_input)
        else:
            silent_software_download(query)
    threading.Thread(target=_dl, daemon=True).start()
    return True


SCREENSHOT_INTERVAL  = 2.0
DIFF_THRESHOLD       = 0.08
MIN_COMMENT_INTERVAL = 20

IGNORED_WINDOWS = ["cmd.exe", "powershell", "windows powershell", "task manager", "program manager"]

_vision_running    = False
_last_real_window  = ""
_prev_screenshot   = None
_last_comment_time = 0.0
_window_active_since     = 0.0
_vision_observation_count = 0

def get_active_window():
    if not WINDOW_DEPS_OK:
        return ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""

def capture_screenshot():
    if not WINDOW_DEPS_OK:
        return None, None, None
    try:
        img   = pyautogui.screenshot()
        small = img.resize((640, 360), Image.LANCZOS)
        arr   = np.array(small.convert('L'), dtype=np.float32) / 255.0
        buf   = BytesIO()
        small.save(buf, format="JPEG", quality=65)
        b64   = base64.b64encode(buf.getvalue()).decode("utf-8")
        _screenshot_buffer.append(img)

        ui_state["last_screenshot"] = b64
        push_to_ui("screenshot_update", {"b64": b64, "window": get_active_window(), "source": "ambient"})

        return img, b64, arr
    except Exception:
        return None, None, None

def screen_diff(arr1, arr2):
    if arr1 is None or arr2 is None:
        return 0.0
    diff = np.abs(arr1 - arr2)
    return float(np.mean(diff > 0.1))

def _strip_thinking(text):
    """Qwen's vision model has a 'thinking mode' that can wrap a reasoning
    trace in <think>...</think> before the actual answer. Strip it so
    downstream keyword parsing (YES/NO, JOINED/LEFT/NONE, ONLINE/OFFLINE)
    sees only the real answer — and so anything shown directly to the user
    (ambient screen comments) never leaks raw reasoning text."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

def _last_match(text, *keywords):
    """Returns whichever keyword appears LAST in text (case-insensitive), or
    None if none appear. A model reasoning through a question before
    stating its answer (thinking mode, or just verbose instruction-following)
    states its real conclusion last — checking the last occurrence instead
    of the first is far more robust against phrasing like "not JOINED yet,
    still NONE" where a naive first-match/substring check would misfire on
    the earlier word."""
    text = text.upper()
    best_pos, best_kw = -1, None
    for kw in keywords:
        pos = text.rfind(kw)
        if pos > best_pos:
            best_pos, best_kw = pos, kw
    return best_kw

GEMINI_VISION_MODEL_CHAIN = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]
GROQ_VISION_MODEL_CHAIN = [
    "qwen/qwen3.8-27b",
]

def _pil_to_data_url(img, fmt="JPEG"):
    buf = BytesIO()
    img.convert("RGB").save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{b64}"

def _groq_vision_chat(system_prompt, user_text, images, temperature=0.1, max_output_tokens=80):
    """Vision fallback tier: tried only once every model in
    GEMINI_VISION_MODEL_CHAIN has failed. Mirrors _groq_chat()'s per-model
    retry pattern but for the vision-capable Groq models, and builds the
    OpenAI-style multi-part content Groq expects (text + one or more
    data-URL images) from the same PIL.Image objects Gemini takes directly.
    Returns answer text, or None if groq_client isn't configured or every
    model in the chain fails."""
    if not groq_client:
        return None
    content = [{"type": "text", "text": user_text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": _pil_to_data_url(img)}})
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    global _last_vision_model
    for model in GROQ_VISION_MODEL_CHAIN:
        if _cooldown_active(model):
            continue
        try:
            kwargs = dict(model=model, temperature=temperature, messages=messages,
                          max_tokens=max_output_tokens + GEMINI_THINKING_HEADROOM)
            try:
                r = groq_client.chat.completions.create(reasoning_effort="none", **kwargs)
            except Exception as e:
                if "reasoning" not in str(e).lower():
                    raise
                dbg(f"[VISION] Groq {model} rejected reasoning_effort — retrying without it: {e}")
                r = groq_client.chat.completions.create(**kwargs)
            raw = (r.choices[0].message.content or "")
            text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
            if not text:
                dbg(f"[VISION] Groq {model} returned no usable text after stripping reasoning")
                continue
            if _last_vision_model != f"Groq {model}":
                _last_vision_model = f"Groq {model}"
                dbg(f"[VISION] now answering via {_last_vision_model}")
            return text
        except Exception as e:
            dbg(f"[VISION] Groq vision model {model} failed: {e}")
            _cooldown_set(model, e)
    return None

GEMINI_THINKING_HEADROOM = 768
_last_vision_model = None

def _gemini_vision_request(model, system_prompt, user_text, images, temperature, max_output_tokens, thinking=True):
    """One generate_content call. Gemma models on the Gemini API reject system_instruction
    and thinking config, so for them the system prompt is folded into the user text instead."""
    cap = max_output_tokens + GEMINI_THINKING_HEADROOM
    if model.startswith("gemma"):
        return gemini_client.models.generate_content(
            model=model,
            contents=[f"{system_prompt}\n\n{user_text}"] + list(images),
            config=genai_types.GenerateContentConfig(temperature=temperature, max_output_tokens=cap))
    kw = dict(system_instruction=system_prompt, temperature=temperature, max_output_tokens=cap)
    if thinking:
        for tc in ({"thinking_level": "minimal"}, {"thinking_budget": 0}):
            try:
                kw["thinking_config"] = genai_types.ThinkingConfig(**tc)
                break
            except Exception:
                continue
    return gemini_client.models.generate_content(
        model=model, contents=[user_text] + list(images),
        config=genai_types.GenerateContentConfig(**kw))

def gemini_vision_call(system_prompt, user_text, images, temperature=0.1, max_output_tokens=80):
    """Vision calls walk GEMINI_VISION_MODEL_CHAIN (skipping any model in cooldown), then the
    Groq vision chain (_groq_vision_chat), before giving up. Returns (answer_text, usage) —
    usage is Gemini's usage_metadata when Gemini/Gemma answered, None when Groq did (callers
    already guard on `if usage:`). Returns (None, None) only if every model failed or is
    cooling down — callers treat that as "no result this cycle", not as an answer."""
    global _last_vision_model
    if gemini_client:
        for model in GEMINI_VISION_MODEL_CHAIN:
            if _cooldown_active(model):
                continue
            try:
                try:
                    response = _gemini_vision_request(model, system_prompt, user_text, images, temperature, max_output_tokens)
                except Exception as e:
                    if "thinking" not in str(e).lower():
                        raise
                    dbg(f"[GEMINI] {model} rejected the thinking setting — retrying without it: {e}")
                    response = _gemini_vision_request(model, system_prompt, user_text, images, temperature, max_output_tokens, thinking=False)
                answer = (response.text or "").strip()
                if answer:
                    if _last_vision_model != f"Gemini {model}":
                        _last_vision_model = f"Gemini {model}"
                        dbg(f"[VISION] now answering via {_last_vision_model}")
                    return answer, response.usage_metadata
                dbg(f"[GEMINI] {model} returned no text (finish_reason="
                    f"{getattr(response.candidates[0], 'finish_reason', '?') if response.candidates else 'no candidates'})")
            except Exception as e:
                dbg(f"[GEMINI] vision model {model} failed: {e}")
                _cooldown_set(model, e)
    answer = _groq_vision_chat(system_prompt, user_text, images, temperature, max_output_tokens)
    if answer:
        return answer, None
    dbg("[VISION] every vision model on Gemini and Groq failed or is cooling down")
    return None, None

_model_check_state = {"running": False, "results": [], "started": None, "finished": None}
_model_check_lock  = threading.Lock()
_CHECK_NUMBER      = "4271"

def _classify_check_error(e):
    low = str(e).lower()
    if "404" in low or "not_found" in low or "does not exist" in low or "not found" in low:
        return "MISSING"
    if "429" in low or "resource_exhausted" in low or "rate limit" in low or "quota" in low:
        return "QUOTA"
    if "401" in low or "403" in low or "api key" in low or "permission" in low or "unauthorized" in low:
        return "AUTH"
    return "ERROR"

def _make_check_image():
    from PIL import Image, ImageDraw
    small = Image.new("RGB", (120, 32), "white")
    ImageDraw.Draw(small).text((8, 10), _CHECK_NUMBER, fill="black")
    return small.resize((720, 192), Image.NEAREST)

def _run_model_check():
    ping   = "Reply with only the single word: OK"
    vprompt = "What number is written in this image? Reply with only the digits."
    targets = []

    if groq_client:
        def groq_chat(m):
            r = groq_client.chat.completions.create(
                model=m, max_tokens=300, messages=[{"role": "user", "content": ping}])
            return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
        for m in GROQ_MODEL_CHAIN:
            targets.append(("chat", "Groq", m, (lambda m=m: groq_chat(m)), None))
    if gemini_client:
        for m in GEMINI_TEXT_MODEL_CHAIN:
            targets.append(("chat", "Gemini", m, (lambda m=m: (gemini_client.models.generate_content(
                model=m, contents=(f"{ping}" if not m.startswith("gemma") else ping),
                config=genai_types.GenerateContentConfig(max_output_tokens=300)).text or "").strip()), None))
    if TAVILY_API_KEY:
        def tavily_ping():
            data = _tavily_search("what year is it", max_results=1)
            return (data.get("answer") or (data.get("results") or [{}])[0].get("title") or "(reachable, no content)").strip()
        targets.append(("search", "Tavily", "tavily", tavily_ping, None))
    if NVIDIA_API_KEY:
        def nvidia_ping():
            r = requests.post(NVIDIA_API_URL, timeout=90,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                json={"model": OVERCLOCK_MODEL, "max_tokens": 32,
                      "messages": [{"role": "user", "content": ping}]})
            r.raise_for_status()
            return (r.json()["choices"][0]["message"].get("content") or "(reachable — reply held back by reasoning)").strip()
        targets.append(("chat", "NVIDIA", OVERCLOCK_MODEL, nvidia_ping, None))
    if groq_client:
        def whisper_ping(m):
            import struct, math, wave
            buf, path = BytesIO(), None
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"".join(struct.pack("<h", int(3000*math.sin(2*math.pi*440*i/16000)))
                                        for i in range(16000)))
            buf.seek(0); buf.name = "tone.wav"
            r = groq_client.audio.transcriptions.create(model=m, file=buf)
            return f"(reachable — transcribed {len(r.text or '')} chars from a test tone)"
        for m in WHISPER_MODEL_CHAIN:
            targets.append(("audio", "Groq", m, (lambda m=m: whisper_ping(m)), None))

    img = None
    try:
        img = _make_check_image()
    except Exception as e:
        dbg(f"[MODELCHECK] could not build the vision test image, skipping vision models: {e}")
    if img is not None:
        if gemini_client:
            for m in GEMINI_VISION_MODEL_CHAIN:
                targets.append(("vision", "Gemini", m, (lambda m=m: (_gemini_vision_request(
                    m, "You read text from images.", vprompt, [img], 0.0, 40).text or "").strip()), _CHECK_NUMBER))
        if groq_client:
            def groq_vision(m):
                msgs = [{"role": "user", "content": [
                    {"type": "text", "text": vprompt},
                    {"type": "image_url", "image_url": {"url": _pil_to_data_url(img)}}]}]
                kw = dict(model=m, messages=msgs, temperature=0.0, max_tokens=200)
                try:
                    r = groq_client.chat.completions.create(reasoning_effort="none", **kw)
                except Exception as e:
                    if "reasoning" not in str(e).lower():
                        raise
                    r = groq_client.chat.completions.create(**kw)
                return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
            for m in GROQ_VISION_MODEL_CHAIN:
                targets.append(("vision", "Groq", m, (lambda m=m: groq_vision(m)), _CHECK_NUMBER))
    def ollama_ping():
        raw = ollama.chat(model=LOCAL_MODEL, messages=[{"role": "user", "content": ping}],
                           options={"num_predict": 400})["message"]["content"]
        return re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip() or "(reachable, but only produced reasoning — no final answer within 400 tokens)"
    targets.append(("local", "Ollama", LOCAL_MODEL, ollama_ping, None))

    dbg(f"[MODELCHECK] starting — {len(targets)} models to test")
    ok_count = 0
    for kind, provider, model, fn, must in targets:
        res = _execute_check(kind, provider, model, fn, must)
        if res["status"] == "OK":
            ok_count += 1
        with _model_check_lock:
            _model_check_state["results"].append(res)
        time.sleep(0.4)

    with _model_check_lock:
        _model_check_state.update(running=False, finished=time.time())
    push_to_ui("model_check_done", {"ok": ok_count, "total": len(targets)})
    dbg(f"[MODELCHECK] done — {ok_count}/{len(targets)} working")

def _execute_check(kind, provider, model, fn, must=None):
    """Runs ONE self-test target: calls fn(), classifies the result, updates the real
    cooldown table to match what was just learned, logs it, and pushes it to the UI live.
    Shared by the bulk TEST MODELS run and any standalone single-model test (image gen's
    dedicated button, so it isn't bundled into routine re-testing of everything else)."""
    t0, status, detail, err = time.perf_counter(), "OK", "", None
    try:
        reply = fn()
        if not reply:
            status, detail = "ERROR", "empty reply"
        elif must and must not in re.sub(r"\D", "", reply):
            status, detail = "WRONG", f"answered {reply[:60]!r} instead of {must} — responds but can't read the image"
        else:
            detail = reply[:60]
    except Exception as e:
        err = e
        status, detail = _classify_check_error(e), str(e).replace("\n", " ")[:220]
    ms = int((time.perf_counter() - t0) * 1000)
    if status == "OK":
        _model_cooldown.pop(model, None)
    elif err is not None and status in ("QUOTA", "MISSING"):
        _cooldown_set(model, err)
    res = {"kind": kind, "provider": provider, "model": model, "status": status, "ms": ms, "detail": detail}
    push_to_ui("model_check_result", res)
    dbg(f"[MODELCHECK] {kind:6} {provider:7} {model}: {status} ({ms}ms) {detail}")
    return res

def _run_imagegen_check():
    """The deliberate, standalone test for image generation ONLY — see the note above
    for why this isn't part of the bulk TEST MODELS run. Tests the primary
    Pollinations provider (unlimited free); Gemini is only a fallback so not
    tested separately here."""
    def imagegen_ping():
        (img_bytes, mime), provider = _generate_image_unified("a single small red circle on a white background")
        return f"({provider} — got a real {mime} {len(img_bytes)}-byte image back)"
    res = _execute_check("image", "Pollinations", POLLINATIONS_IMAGE_MODEL, imagegen_ping)
    with _model_check_lock:
        _model_check_state["results"] = [r for r in _model_check_state["results"]
                                          if r["model"] != POLLINATIONS_IMAGE_MODEL] + [res]
    push_to_ui("model_check_single_done", res)

def analyze_screen_b64(window_title, b64, prompt=None):

    if not gemini_client or not b64:
        return None
    try:
        user_prompt = prompt or (
            f"Active window: {window_title}\n"
            "Look carefully and pick ONE approach — vary it each time:\n"
            "1. Point out something specific\n"
            "2. Give a useful suggestion\n"
            "3. Ask a relevant question\n"
            "4. Make a dry witty observation\n"
            "Never start with the app name. Never say 'I can see'. "
            "Be specific. 1-2 sentences max. If nothing worth commenting on: SKIP"
        )
        img = Image.open(BytesIO(base64.b64decode(b64)))
        answer, usage = gemini_vision_call(
            XENORA_PREFIX + "\nYou are watching the user's screen.",
            user_prompt, [img], temperature=1.0, max_output_tokens=120,
        )
        if answer is None:
            return None
        if usage:
            dbg(f"[VISION] comment tokens: total={usage.total_token_count}")
        result = _strip_thinking(answer)
        return None if result.upper().startswith("SKIP") else result
    except Exception as e:
        dbg(f"[VISION] analyze error: {e}")
        return None

PATTERN_REVIEW_EVERY = 15


def handle_uploaded_image(b64_list, caption):
    try:
        dbg(f"[IMAGE] handling upload: caption={caption!r} images={len(b64_list)}")
        chat_history.append({'role': 'user', 'content': f"[Uploaded {len(b64_list)} image(s)] {caption}".strip()})

        if not gemini_client:
            msg = "I can't read images right now — Gemini isn't configured."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return

        images = []
        for i, b64 in enumerate(b64_list):
            try:
                images.append(Image.open(BytesIO(base64.b64decode(b64))))
            except Exception as e:
                dbg(f"[IMAGE] decode error on image {i}: {e}")
        if not images:
            msg = "Those images didn't come through properly — try uploading again."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return

        prompt = caption.strip() if caption.strip() else (
            "Describe what's in this image and mention anything notable about it." if len(images) == 1
            else "Describe what's in these images and mention anything notable about them."
        )
        answer, usage = gemini_vision_call(
            XENORA_PREFIX + "\nThe user has directly uploaded one or more images (not a screenshot of "
            "their screen) for you to look at and discuss.",
            prompt, images, temperature=0.7, max_output_tokens=300,
        )
        if usage:
            dbg(f"[IMAGE] tokens: total={usage.total_token_count}")
        if not answer:
            msg = "Couldn't process that just now — try again in a moment."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return

        cleaned = clean_response(answer)
        chat_history.append({'role': 'assistant', 'content': cleaned})
        if len(chat_history) > 80:
            del chat_history[:-80]
        add_chat_message("assistant", cleaned, remember=False)
        speak_auto(cleaned)
    except Exception as e:
        dbg(f"[IMAGE] handle_uploaded_image crashed: {e}")
        try:
            add_chat_message("assistant", "Something went wrong processing that image — check the console for details.")
        except Exception:
            pass

def review_vision_patterns_for_memory():
    """The 'notice patterns and save them as memory' half of Xenora's
    personality spec — observe -> remember -> connect -> infer, but ONLY
    durable, high-confidence patterns, never a weak guess from one
    occurrence (see the MEMORY section of XENORA_PREFIX). Runs in the
    background off a window of recent screen observations already logged
    in chat_history — no separate observation log needed. Saved facts are
    tagged "[inferred pattern]" so they're never confused with something
    the user actually told Xenora directly (memory["facts"] stays a flat
    list of strings — the UI and the fact-removal endpoint both expect
    that shape, so a tag prefix keeps this compatible rather than needing
    a schema migration)."""
    def _run():
        try:
            recent = [
                m['content'] for m in chat_history[-50:]
                if (m['role'] == 'assistant' and m['content'].startswith('[Screen observation]'))
                or (m['role'] == 'assistant' and m['content'].startswith('[Opened'))
                or (m['role'] == 'user' and m['content'].startswith('[Screen event'))
            ]
            if len(recent) < 6:
                return
            raw = llm(
                "Here are recent screen observations from a desktop assistant watching the user's "
                "activity over time. Look for ONE genuinely DURABLE pattern worth remembering long "
                "term — a stable preference, an important routine, a long-term project, or a "
                "frequently recurring behavior. Do NOT invent a pattern from a single occurrence or "
                "weak evidence — most of the time there won't be one, and that's fine. Reply ONLY "
                'JSON: {"has_pattern": true or false, "fact": "one concise sentence" or null, '
                '"fact_type": "the single best category for the fact, or null if fact is null. '
                "Must be exactly one of: context, experience, preference, relationship, goal, warning, "
                "system, temporary. experience=a recurring behavior/routine observed happening; "
                "preference=a stable like/dislike/habit; goal=a long-term project or intention; "
                "relationship=a connection between people/projects/places; context=other stable info; "
                'warning/system/temporary rarely apply here but use them if genuinely a better fit", '
                '"confidence": "high" or "medium" or "low"}. Only set has_pattern true if confidence '
                "is high AND the evidence genuinely supports it.",
                "\n".join(recent[-25:]), temperature=0.3
            )
            cleaned = re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip()
            data = json.loads(cleaned)
            if data.get("has_pattern") and data.get("confidence") == "high" and data.get("fact"):
                fact = f"[inferred pattern] {data['fact']}"
                if fact not in _memory.get("facts", []):
                    _memory.setdefault("facts", []).append(fact)
                    save_memory(_memory)
                    ui_state["memory_facts"] = _memory["facts"]
                    push_to_ui("memory_update", {"facts": _memory["facts"], "user_name": _memory.get("user_name", "User")})
                    graph_add_node(data["fact"], ntype=(data.get("fact_type") or "experience"), confidence="inferred")
                    dbg(f"[MEMORY] inferred pattern saved: {data['fact']}")
        except Exception as e:
            dbg(f"[MEMORY] pattern review error: {e}")
    threading.Thread(target=_run, daemon=True).start()

def queue_vision_comment(comment, add_to_history=True):
    if comment:
        _vision_queue.put((comment, add_to_history))

def vision_speaker():
    while True:
        try:
            item = _vision_queue.get(timeout=1)
            comment, add_to_history = item
            while _speaking or _user_typing or _conversation_active:
                time.sleep(0.5)
            if add_to_history:
                chat_history.append({'role': 'assistant', 'content': f"[Screen observation]: {comment}"})
            add_chat_message("assistant", f"[Vision] {comment}", remember=False)
            speak(comment, style=detect_style(comment))
            _vision_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            dbg(f"[SPEAKER] Error: {e}")
            continue

def window_awareness_loop():
    global _vision_running, _last_real_window, _prev_screenshot, _last_comment_time
    global _window_active_since, _vision_observation_count
    _vision_running = True
    _window_active_since = time.time()
    while _vision_running:
        try:
            time.sleep(SCREENSHOT_INTERVAL)

            if not VISION_ENABLED or OVERCLOCK_MODE or not online():
                continue

            current_window = get_active_window()
            if not current_window:
                continue
            if any(ig.lower() in current_window.lower() for ig in IGNORED_WINDOWS if ig):
                continue

            img, b64, arr = capture_screenshot()
            if arr is None:
                continue

            _comments_ok = COMMENTS_ENABLED and VOICE_ENABLED

            now  = time.time()
            diff = screen_diff(_prev_screenshot, arr) if _prev_screenshot is not None else 0.0
            time_since_comment = now - _last_comment_time

            if current_window != _last_real_window:
                prev_window       = _last_real_window
                _last_real_window = current_window
                _prev_screenshot  = arr
                _last_comment_time = now
                _window_active_since = now

                ui_state["current_window"] = current_window
                push_to_ui("window_change", {"window": current_window, "prev": prev_window})

                comment = llm(
                    "The user just switched to a new app. React naturally — do NOT just state what app opened. "
                    "Ask a relevant question, make an observation, or say something interesting. "
                    "Keep it under 10 words. Never start with the app name.",
                    f"App opened: {current_window}"
                )
                comment = clean_response(comment)
                if comment and _comments_ok:
                    chat_history.append({'role': 'assistant', 'content': f"[Opened {current_window}]: {comment}"})
                    queue_vision_comment(comment, add_to_history=False)
                _vision_observation_count += 1
                if _vision_observation_count % PATTERN_REVIEW_EVERY == 0:
                    review_vision_patterns_for_memory()
                continue

            _prev_screenshot = arr
            if diff < DIFF_THRESHOLD or time_since_comment < MIN_COMMENT_INTERVAL:
                continue

            _last_comment_time = now
            time_on_window = now - _window_active_since
            duration_note = ""
            if time_on_window > 2700:
                duration_note = (
                    f"\nThey've been on this window for about {humanize_delta(time_on_window)} straight "
                    f"— if it's genuinely relevant, you MAY point that out playfully, but only if it "
                    f"fits naturally with what's on screen; don't force it."
                )
            commentary = analyze_screen_b64(current_window, b64, prompt=(
                f"Active window: {current_window}\n"
                "Look carefully and pick ONE approach — vary it each time:\n"
                "1. Point out something specific\n"
                "2. Give a useful suggestion\n"
                "3. Ask a relevant question\n"
                "4. Make a dry witty observation\n"
                "Never start with the app name. Never say 'I can see'. "
                "Be specific. 1-2 sentences max. If nothing worth commenting on: SKIP" + duration_note
            ))
            if not commentary:
                continue
            cleaned = clean_response(commentary)
            _last_context.update({'window': current_window, 'description': commentary, 'timestamp': now})
            chat_history.append({'role': 'user',      'content': f"[Screen event — {current_window}]: {commentary}"})
            chat_history.append({'role': 'assistant', 'content': cleaned})
            if _comments_ok:
                queue_vision_comment(cleaned, add_to_history=False)
            _vision_observation_count += 1
            if _vision_observation_count % PATTERN_REVIEW_EVERY == 0:
                review_vision_patterns_for_memory()

        except Exception as e:
            dbg(f"[VISION] Loop error: {e}")
            time.sleep(3)

def answer_screen_question(user_input):
    if not _screenshot_buffer or not online():
        return None
    latest = _screenshot_buffer[-1]
    buf    = BytesIO()
    latest.resize((1280, 720), Image.LANCZOS).save(buf, format="JPEG", quality=70)
    b64    = base64.b64encode(buf.getvalue()).decode("utf-8")
    window  = get_active_window() or _last_context.get('window', 'unknown')
    context = _last_context.get('description', '')
    prompt  = f"The user is asking: '{user_input}'\nActive window: {window}\nLast observation: {context}\nAnswer their question directly."
    answer = analyze_screen_b64(window, b64, prompt=prompt)
    if answer:
        _last_context.update({'window': window, 'description': answer, 'timestamp': time.time()})
    return answer

def is_screen_question(text):
    t = text.lower()
    screen_phrases = [
        "did you see", "did you notice", "what am i doing", "what do you see",
        "what's on my screen", "what's happening", "can you see", "are you watching",
        "what just happened", "did that just", "you saw that", "xenora did you",
        "look at this", "check this out", "what is this"
    ]
    if any(phrase in t for phrase in screen_phrases):
        return True
    has_see_verb = any(v in t for v in ("see", "look at", "check"))
    has_screen_noun = any(n in t for n in ("screen", "window", "monitor", "display"))
    return has_see_verb and has_screen_noun


active_tasks     = {}
active_schedules = {}
friend_presence  = {}
_presence_pending = {}
_task_lock       = threading.Lock()
_pending_power   = None

def _xi_push(name, payload):
    """Live automation events -> UI (Tasks panel) and, for the orb, mirrored into /api/state."""
    if name == "xi_now":
        ui_state["xi_now"] = payload
    push_to_ui(name, payload)

def _all_task_items():
    """Legacy watches/schedules plus the automation system's tasks and its pending question, in one list."""
    items = list(active_tasks.values()) + list(active_schedules.values())
    try:
        if XI:
            items += XI.ui_items()
    except Exception as e:
        dbg(f"[UI] automation items unavailable: {e}")
    return items

def _push_all_tasks():
    push_to_ui("task_update", _all_task_items())

TASK_CHECK_INTERVAL = 4.0
TASK_MIN_API_INTERVAL = 20.0
TASK_DIFF_THRESHOLD = 0.02
PRESENCE_CHECK_INTERVAL = 15.0

DEFERRED_HINTS = [
    "when ", "once ", "as soon as", "the moment", "notify me", "tell me when",
    "let me know when", "alert me when", "watch my", "watch the", "keep an eye on",
    "keep watching", "remind me", "remind you", "reminder", "ping me",
    "shut down", "shutdown", "restart my", "restart at",
    "power off", "turn off my laptop",
    "tell me if", "let me know if", "is online", "is offline", "joins", "joined",
]

def looks_like_deferred_request(text):
    t = text.lower()
    return any(h in t for h in DEFERRED_HINTS)

IMMEDIATE_POWER_PHRASES = {
    "shutdown": [
        "shut down my laptop", "shut down the laptop", "shutdown my laptop", "shutdown the laptop",
        "shut down my pc", "shut down my computer", "power off my laptop", "power off the laptop",
        "turn off my laptop", "turn off the laptop",
    ],
    "restart": [
        "restart my laptop", "restart the laptop", "reboot my laptop", "reboot the laptop",
        "restart my pc", "restart my computer", "restart it",
    ],
}
TIME_QUALIFIER_HINTS = [
    " in ", " after ", " at ", "when ", "once ", "as soon as", "the moment",
    "minute", "hour", "second", " am", " pm", "tonight", "tomorrow", "o'clock",
]

def detect_immediate_power(text):
    """Returns 'shutdown' | 'restart' | None. None means either no power
    phrase was found, or one was found but it came with a time/condition
    qualifier (so it belongs to the scheduler instead)."""
    t = " " + text.lower().strip() + " "
    if any(h in t for h in TIME_QUALIFIER_HINTS):
        return None
    for action, phrases in IMMEDIATE_POWER_PHRASES.items():
        if any(p in t for p in phrases):
            return action
    return None

SLEEP_REQUEST_PHRASES = [
    "put my laptop to sleep", "put the laptop to sleep", "put it to sleep",
    "sleep my laptop", "sleep the laptop", "sleep my pc", "sleep my computer",
    "put my laptop in sleep", "put my pc to sleep",
]

def detect_sleep_unsupported(text):
    """Sleep is intentionally unsupported as an action — not a missing
    feature, a deliberate decision after confirming SetSuspendState reports
    success on this hardware without ever actually suspending it (every
    software path was tried: .NET, rundll32, raw ctypes with the shutdown
    privilege correctly enabled — all either no-op or lie about success).
    Only the physical lid switch works. Rather than silently failing (the
    original bug) or falling through to the LLM to improvise a wrong answer,
    this intercepts sleep requests directly."""
    t = " " + text.lower().strip() + " "
    return any(p in t for p in SLEEP_REQUEST_PHRASES)

def parse_deferred_request(user_input):
    """One classifier covers three deferral styles instead of separately-
    triggered keyword gates racing each other:
      - watch:    wait for a described VISUAL condition on a specific window,
                  fire ONCE (e.g. "shut down when the download finishes").
      - schedule: wait for a plain amount of time, no watching involved,
                  whether given as a duration ("in 10 minutes") or a clock
                  time ("at 11:50 PM").
      - presence: ongoing tracking of whether a specific named person is
                  online/joined/left in a specific app — does NOT stop after
                  one trigger, keeps tracking both directions indefinitely,
                  and its status becomes queryable later ("is X online?")
                  without needing another vision check (see
                  detect_presence_query()).
    Also extracts "open_app" — if this message ALSO asks to open/launch
    something first (e.g. "open minecraft and let me know when Player1
    joins"), the plain app name goes here so run_command can open it before
    the watch/presence task starts. This used to be silently dropped: the
    message matched the watch-hint keywords and went straight to parsing a
    watch condition, and the "open X" half of the request was never acted on
    at all — Minecraft never actually opened.
    """
    now = datetime.now()
    system = (
        "Xenora can defer an action in one of three ways — decide which (if any) this message "
        "is asking for:\n"
        "1. WATCH — wait for a described VISUAL condition to appear on a specific app/window, "
        "ONE TIME (e.g. \"shut down when the download finishes\", \"tell me when the render is done\").\n"
        "2. SCHEDULE — wait for a plain amount of time, no watching needed (e.g. \"remind me in "
        "10 minutes\", \"shut down at 11:50 PM\", \"shut down in an hour\"). Convert any "
        f"clock time to a delay in whole seconds from right now ({now.strftime('%I:%M %p on %A, %B %d, %Y')}); "
        "if that clock time has already passed today, assume tomorrow.\n"
        "3. PRESENCE — ONGOING tracking of whether a specific named PERSON is online, has joined, "
        "or has left a specific app/game — not a single check, keeps tracking joins AND leaves "
        "indefinitely (e.g. \"let me know when Player1 joins minecraft\", \"tell me if my friend is "
        "online\"). Use this instead of WATCH whenever a specific person's presence/online status "
        "is what's being tracked, extract their name into \"friend_name\".\n"
        "ALSO: if this message separately asks to open/launch an application first (e.g. \"open "
        "minecraft and let me know when...\"), put its plain name in \"open_app\" (else null) — this "
        "applies regardless of which kind above is chosen.\n"
        "Return JSON only:\n"
        '{"kind": "watch" | "schedule" | "presence" | "none", '
        '"open_app": "plain app name to open first, or null", '
        '"target_app": "short window name — watch/presence only", '
        '"friend_name": "person\'s name — presence only", '
        '"condition": "one precise, visually-checkable sentence — watch only", '
        '"delay_seconds": 0, '
        '"action": "shutdown" | "restart" | "notify", '
        '"message": "short (under 12 words) message announcing the trigger"}\n'
        'Set "kind" to "none" if this isn\'t actually a deferred request. ONLY valid JSON, no explanation.'
    )
    try:
        recent = "\n".join(f"{m['role']}: {m['content']}" for m in chat_history[-6:])
        raw  = llm(system, f"Recent conversation (for resolving 'this'/'that'):\n{recent}\n\nRequest: {user_input}", temperature=0.2)
        data = json.loads(re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip())
        return data if data.get("kind") in ("watch", "schedule", "presence") else None
    except Exception as e:
        dbg(f"[DEFER] parse error: {e}")
        return None

BROWSER_HINTS = ["opera", "chrome", "edge", "firefox", "brave"]

def find_window(target_app):
    """Best-effort match of a spoken app name to an open, visible top-level window."""
    target = (target_app or "").lower().strip()
    candidates = []
    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                candidates.append((hwnd, title))
    win32gui.EnumWindows(_cb, None)

    needles = BROWSER_HINTS if target in ("browser", "web browser", "my browser") else [target]

    def _rank(title):
        tl = title.lower()
        penalty = 0
        for word in ("launcher", "updater", "installer", "setup"):
            if word not in target and word in tl:
                penalty += 10
        return penalty

    def _best(pool):
        pool.sort(key=lambda ht: _rank(ht[1]))
        return pool[0]

    matches = [(hwnd, title) for hwnd, title in candidates if any(n in title.lower() for n in needles if n)]
    if matches:
        return _best(matches)
    words = [w for w in target.split() if len(w) > 2]
    loose = [(hwnd, title) for hwnd, title in candidates if any(w in title.lower() for w in words)]
    if loose:
        return _best(loose)
    return None, None

def capture_window_bg(hwnd):
    """Grab a window's contents via PrintWindow, working even while it's not focused
    or is fully covered by other windows. Some GPU-exclusive fullscreen apps can still
    render black — a known Windows limitation, not something PrintWindow can bypass."""
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
        hwnd_dc  = win32gui.GetWindowDC(hwnd)
        mfc_dc   = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc  = mfc_dc.CreateCompatibleDC()
        bitmap   = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bitmap)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img  = Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']), bits, 'raw', 'BGRX', 0, 1)
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC(); mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return img if ok else None
    except Exception as e:
        dbg(f"[TASK] capture error: {e}")
        return None

def send_notification(title, message):
    """Desktop notification.

    BUG FIX: this used to shell out to PowerShell for a WinForms NotifyIcon
    balloon tip. That approach is what the old comment here already admitted
    was flaky ("recent Windows versions have gotten stricter... it was
    failing silently again") — it needs no AUMID, but it's still an
    unsigned, unpackaged process popping a tray icon, which Windows 10/11
    increasingly swallows without a trace.

    Electron's own main-process Notification API is far more reliable: it's
    a first-class OS notification, no PowerShell subprocess involved. main.js
    already polls GET /api/state every second (for the orb + power countdown),
    so we just piggyback on that — bump ui_state["last_notification"] here,
    and main.js fires a real native notification whenever the id changes.
    We also push it straight over the socket for anything listening live
    (e.g. logging it into the Activity Feed).
    """
    try:
        title   = (title or "Xenora")[:100]
        message = (message or "")[:250]
        payload = {"id": time.time(), "title": title, "message": message}
        ui_state["last_notification"] = payload
        push_to_ui("notification", payload)
        dbg(f"[NOTIFY] queued: {title} — {message}")
    except Exception as e:
        dbg(f"[NOTIFY] {e}")

def _enable_shutdown_privilege():
    """SetSuspendState (like InitiateSystemShutdown) needs SE_SHUTDOWN_NAME
    enabled on the calling process's token — present on most tokens but
    disabled by default.

    NOTE: kept only because shutdown/restart benefit from the same
    privilege being enabled defensively; sleep itself was dropped as a
    supported action entirely (see detect_sleep_unsupported() above) after
    confirming SetSuspendState reports success on this hardware without ever
    actually suspending it — a genuine platform bug, not fixable from here."""
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x0002
    ERROR_NOT_ALL_ASSIGNED = 1300

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    GetCurrentProcess = ctypes.windll.kernel32.GetCurrentProcess
    GetCurrentProcess.restype = wintypes.HANDLE

    OpenProcessToken = ctypes.windll.advapi32.OpenProcessToken
    OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    OpenProcessToken.restype  = wintypes.BOOL

    hToken = wintypes.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(hToken)):
        raise ctypes.WinError()

    LookupPrivilegeValueW = ctypes.windll.advapi32.LookupPrivilegeValueW
    LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    LookupPrivilegeValueW.restype  = wintypes.BOOL

    luid = LUID()
    if not LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
        raise ctypes.WinError()

    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED

    AdjustTokenPrivileges = ctypes.windll.advapi32.AdjustTokenPrivileges
    AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                       wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    AdjustTokenPrivileges.restype  = wintypes.BOOL
    if not AdjustTokenPrivileges(hToken, False, ctypes.byref(tp), 0, None, None):
        raise ctypes.WinError()

    if ctypes.GetLastError() == ERROR_NOT_ALL_ASSIGNED:
        raise OSError("SeShutdownPrivilege present but could not be assigned (ERROR_NOT_ALL_ASSIGNED) — "
                       "this account may be restricted from shutting down the system.")
    ctypes.windll.kernel32.CloseHandle(hToken)


POWER_COMMANDS = {
    "shutdown": lambda: subprocess.run(["shutdown", "/s", "/f", "/t", "0"]),
    "restart":  lambda: subprocess.run(["shutdown", "/r", "/f", "/t", "0"]),
}
POWER_VERB = {"shutdown": "Shutting down", "restart": "Restarting"}
COUNTDOWN_TOTAL = 60

def _set_power_countdown(action, seconds_left):
    ui_state["power_countdown"] = {"action": action, "seconds_left": seconds_left, "total": COUNTDOWN_TOTAL}
    push_to_ui("power_countdown", ui_state["power_countdown"])

def _clear_power_countdown():
    ui_state["power_countdown"] = None
    push_to_ui("power_countdown", None)

def run_power_countdown(action, task_id, lead_in=""):
    """Ticks every second so the orb can show a live countdown (and now the
    ACTIVE TASKS panel, via power_countdown). Only announces in chat/voice/
    notification at the START and at the END — it used to also add_chat_message
    + speak + send_notification at every checkpoint (30s/10s/5s/4s/3s/2s/1s),
    flooding the chat log with a message every second. The countdown itself
    now lives purely on the orb (and the Active Tasks panel), matching the
    single _set_power_countdown() call below that still fires every second.
    Cancellable at any point via check_power_override() (wired into
    run_command) or a click on the orb / a Cancel button while it's counting
    down (see /api/power/cancel)."""
    global _pending_power
    cancel_event = threading.Event()
    with _task_lock:
        _pending_power = {"type": action, "cancel_event": cancel_event, "task_id": task_id}

    verb = POWER_VERB.get(action, action)
    first = f"{lead_in} {verb.lower()} in {COUNTDOWN_TOTAL} seconds.".strip()
    add_chat_message("assistant", first); speak(first, style="chat")
    send_notification("Xenora", first)
    _set_power_countdown(action, COUNTDOWN_TOTAL)

    remaining = COUNTDOWN_TOTAL
    while remaining > 0:
        if cancel_event.wait(timeout=1):
            _clear_power_countdown()
            _finish_power_action(None); return
        remaining -= 1
        _set_power_countdown(action, remaining)

    _clear_power_countdown()
    final = f"{verb} now."
    add_chat_message("assistant", final); speak(final, style="chat")
    send_notification("Xenora", final)
    _finish_power_action(action)

def _finish_power_action(action_to_run):
    global _pending_power
    with _task_lock:
        _pending_power = None
    if action_to_run and action_to_run in POWER_COMMANDS:
        try:
            POWER_COMMANDS[action_to_run]()
        except Exception as e:
            dbg(f"[POWER] {e}")

CANCEL_WORDS = ["cancel", "stop", "abort", "don't shut", "dont shut", "never mind", "nevermind", "belay"]
CHANGE_ACTION_WORDS = {
    "shutdown": ["shut down", "shutdown", "power off"],
    "restart":  ["restart", "reboot"],
}

def check_power_override(user_input):
    """Returns True if this message was consumed as a cancel/change command for
    an in-flight countdown — run_command should stop routing right there."""
    global _pending_power
    if not _pending_power:
        return False
    t = user_input.lower()
    if any(w in t for w in CANCEL_WORDS):
        _pending_power["cancel_event"].set()
        msg = "Cancelled."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        send_notification("Xenora", "Countdown cancelled.")
        return True
    for action, words in CHANGE_ACTION_WORDS.items():
        if action != _pending_power["type"] and any(w in t for w in words):
            task_id = _pending_power["task_id"]
            _pending_power["cancel_event"].set()
            threading.Thread(target=run_power_countdown, args=(action, task_id, "Switching to"), daemon=True).start()
            return True
    return False

STOP_WATCH_WORDS = ["stop watching", "cancel the watch", "cancel watching", "stop the watch", "cancel that task", "stop that task", "cancel all tasks", "stop everything"]

def check_stop_watch(user_input):
    t = user_input.lower()
    if not any(w in t for w in STOP_WATCH_WORDS):
        return False
    with _task_lock:
        watching  = [tid for tid, tk in active_tasks.items() if tk["status"] == "watching"]
        scheduled = [sid for sid, sc in active_schedules.items() if sc["status"] == "scheduled"]
        for tid in watching:
            active_tasks[tid]["status"] = "cancelled"
        for sid in scheduled:
            active_schedules[sid]["status"] = "cancelled"
    stopped = len(watching) + len(scheduled)
    msg = f"Stopped {stopped} running task{'s' if stopped != 1 else ''}." if stopped else "Nothing active to stop."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    _push_all_tasks()
    return True

def start_watch_task(task):
    if not WINDOW_DEPS_OK:
        msg = "I need window-capture access for that, and it's not available right now."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return
    hwnd, title = find_window(task.get("target_app", ""))
    if not hwnd:
        msg = f"I can't find a window matching \"{task.get('target_app','that')}\" — open it first and ask again."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return

    condition = task["condition"]
    with _task_lock:
        for tk in active_tasks.values():
            if (tk["status"] == "watching" and tk["hwnd"] == hwnd
                    and tk["condition"].strip().lower() == condition.strip().lower()):
                msg = f"Already watching {title} for that."
                add_chat_message("assistant", msg); speak(msg, style="chat")
                return
        task_id = str(time.time())
        active_tasks[task_id] = {
            "id": task_id, "kind": "watch", "hwnd": hwnd, "title": title,
            "condition": condition, "action": task["action"],
            "notify_message": task.get("message") or "Condition met.",
            "created": time.time(), "status": "watching",
        }
    _push_all_tasks()

    what = "let you know" if task["action"] == "notify" else f"{task['action']} the laptop"
    msg = f"Watching {title} — I'll {what} once {condition.rstrip('.').lower()}."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    threading.Thread(target=_watch_task_loop, args=(task_id,), daemon=True).start()

def _watch_task_loop(task_id):
    prev_arr = None
    dirty    = True
    last_api = 0.0
    while True:
        with _task_lock:
            task = active_tasks.get(task_id)
        if not task or task["status"] != "watching":
            return
        time.sleep(TASK_CHECK_INTERVAL)

        if not win32gui.IsWindow(task["hwnd"]):
            with _task_lock:
                if task_id in active_tasks:
                    active_tasks[task_id]["status"] = "lost"
            msg = f"Lost track of {task['title']} — looks like it closed. Stopping that watch."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            _push_all_tasks()
            return

        img = capture_window_bg(task["hwnd"])
        if img is None or img.width == 0:
            continue
        small = img.resize((480, max(1, int(480 * img.height / img.width))), Image.LANCZOS)

        buf_ui = BytesIO(); small.save(buf_ui, format="JPEG", quality=65)
        push_to_ui("screenshot_update", {
            "b64": base64.b64encode(buf_ui.getvalue()).decode("utf-8"),
            "window": f"Watching: {task['title']}", "source": "task",
        })

        arr   = np.array(small.convert('L'), dtype=np.float32) / 255.0
        diff  = screen_diff(prev_arr, arr) if prev_arr is not None else 1.0
        prev_arr = arr
        if diff >= TASK_DIFF_THRESHOLD:
            dirty = True
        if not dirty or not online():
            continue
        if time.time() - last_api < TASK_MIN_API_INTERVAL:
            continue

        answer, usage = gemini_vision_call(
            "Reply with exactly one word: YES if the described condition is clearly visible right now, otherwise NO.",
            f"Condition: {task['condition']}", [small],
        )
        last_api = time.time()
        if answer is None:
            continue
        dirty = False
        if usage:
            dbg(f"[TASK] check tokens: total={usage.total_token_count}")
        answer = _strip_thinking(answer.upper())

        if _last_match(answer, "YES", "NO") == "YES":
            _fire_task(task_id)
            return

_presence_loop_hwnds = set()

def start_presence_watch(task):
    if not WINDOW_DEPS_OK:
        msg = "I need window-capture access for that, and it's not available right now."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return
    hwnd, title = find_window(task.get("target_app", ""))
    if not hwnd:
        msg = f"I can't find a window matching \"{task.get('target_app','that')}\" — open it first and ask again."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return

    friend_name = (task.get("friend_name") or "").strip()
    if not friend_name:
        msg = "I didn't catch whose presence to track — who should I watch for?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return
    key = friend_name.lower()

    with _task_lock:
        for tk in active_tasks.values():
            if (tk["status"] == "watching" and tk.get("kind") == "presence"
                    and tk["hwnd"] == hwnd and tk.get("friend_name", "").lower() == key):
                msg = f"Already tracking {friend_name} on {title}."
                add_chat_message("assistant", msg); speak(msg, style="chat")
                return
        task_id = str(time.time())
        active_tasks[task_id] = {
            "id": task_id, "kind": "presence", "hwnd": hwnd, "title": title,
            "friend_name": friend_name, "target_app": task.get("target_app", ""),
            "condition": f"{friend_name} joining or leaving {title}",
            "action": "notify", "notify_message": "",
            "created": time.time(), "status": "watching",
        }
        friend_presence.setdefault(key, {"name": friend_name, "status": "unknown",
                                          "app": title, "updated": time.time()})
        needs_new_loop = hwnd not in _presence_loop_hwnds
        if needs_new_loop:
            _presence_loop_hwnds.add(hwnd)
    _push_all_tasks()

    msg = f"Tracking {friend_name} on {title} — I'll let you know when they join or leave."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    threading.Thread(target=_check_initial_presence, args=(hwnd, title, friend_name), daemon=True).start()
    if needs_new_loop:
        threading.Thread(target=_presence_group_loop, args=(hwnd,), daemon=True).start()

def _grab_presence_frames(img):
    """Returns (full_img, crop_img) as PIL.Image objects — Gemini's SDK
    accepts these directly, no base64 needed. Used ONCE per tracking session
    (the initial baseline check), where the extra cost of two images is a
    one-time expense, not a recurring one. full = whole frame, downscaled
    for context. crop = a zoomed-in bottom-left region (most games/apps put
    chat/log AND friends-list panels there — confirmed in Shadow's own
    screenshots) at higher relative resolution for actually reading text."""
    full_w = min(img.width, 720)
    full = img.resize((full_w, max(1, int(full_w * img.height / img.width))), Image.LANCZOS) \
           if img.width != full_w else img

    cw, ch = max(1, int(img.width * 0.55)), max(1, int(img.height * 0.55))
    crop = img.crop((0, img.height - ch, cw, img.height))
    crop_w = min(crop.width, 960)
    crop = crop.resize((crop_w, max(1, int(crop_w * crop.height / crop.width))), Image.LANCZOS)
    return full, crop

def _grab_presence_crop(img):
    """Single zoomed bottom-left crop only, as a PIL.Image — used for every
    RECURRING check in _presence_group_loop, where cost compounds over a
    multi-hour session (confirmed real cost: 1,777 tokens/check on Groq with
    both images, which blew through Groq's 200K/day free-tier cap in 3
    checks after a day of other testing — the actual reason vision moved to
    Gemini). Dropping the second (full-frame) image cuts a meaningful chunk
    of that regardless of provider, since it's genuinely redundant for a
    feature that's specifically reading small text in one corner."""
    cw, ch = max(1, int(img.width * 0.55)), max(1, int(img.height * 0.55))
    crop = img.crop((0, img.height - ch, cw, img.height))
    crop_w = min(crop.width, 1024)
    crop = crop.resize((crop_w, max(1, int(crop_w * crop.height / crop.width))), Image.LANCZOS)
    return crop

def _check_initial_presence(hwnd, title, friend_name):
    """Runs once, right when tracking starts, to establish a CURRENT status
    baseline instead of waiting passively for a future event."""
    img = capture_window_bg(hwnd)
    if img is None or img.width == 0:
        return
    full_img, crop_img = _grab_presence_frames(img)
    system_prompt = (
        "Determine whether the named person is CURRENTLY online/in-game right now, based on "
        "this screenshot. Look for a friends/player list entry (e.g. \"NAME - Playing ...\" "
        "means online, \"NAME - Offline\" means offline) and/or recent chat/log lines. The "
        "screen may be visually busy with unrelated banners, HUD elements, or notifications — "
        "scan carefully for the exact name given. Reply with exactly one word: ONLINE, "
        "OFFLINE, or UNKNOWN if you genuinely can't tell.")
    user_text = (
        f"Person: {friend_name}\n"
        "First image: full screenshot. Second image: zoomed bottom-left corner "
        "(chat/log often lives here).")
    answer, usage = gemini_vision_call(system_prompt, user_text, [full_img, crop_img])
    if answer is None:
        dbg(f"[PRESENCE] initial status check for {friend_name}: no response (Gemini unavailable?)")
        return
    if usage:
        dbg(f"[PRESENCE] initial status check tokens: total={usage.total_token_count}")
    answer = _strip_thinking(answer.upper())
    dbg(f"[PRESENCE] initial status check for {friend_name}: {answer!r}")

    key = friend_name.strip().lower()
    kw = _last_match(answer, "ONLINE", "OFFLINE")
    if kw == "OFFLINE":
        with _task_lock:
            friend_presence[key] = {"name": friend_name, "status": "offline", "app": title, "updated": time.time()}
    elif kw == "ONLINE":
        with _task_lock:
            friend_presence[key] = {"name": friend_name, "status": "online", "app": title, "updated": time.time()}

def _update_presence(task_id, new_status):
    with _task_lock:
        task = active_tasks.get(task_id)
        if not task or task["status"] != "watching":
            return
        key  = task["friend_name"].strip().lower()
        prev = friend_presence.get(key, {}).get("status")
        if prev == new_status:
            return
        friend_presence[key] = {"name": task["friend_name"], "status": new_status,
                                 "app": task["title"], "updated": time.time()}
        friend_name, title = task["friend_name"], task["title"]

    verb = "joined" if new_status == "online" else "left"
    msg = f"{friend_name} just {verb} {title}."
    add_chat_message("assistant", msg, remember=False, instant=True); speak(msg, style="cheerful" if new_status == "online" else "chat")
    send_notification("Xenora", msg)
    flash_orb_done()

def _presence_group_loop(hwnd):
    """One loop per WINDOW, not per friend. Every cycle it re-reads whatever
    presence tasks currently exist for this hwnd (so adding/cancelling a
    friend mid-session just works — no separate bookkeeping needed), takes
    ONE screenshot, and asks about ALL tracked names in a SINGLE Gemini call
    using structured JSON output instead of the old one-name-per-call
    design. This is what makes tracking N friends on the same window cost
    ~the same as tracking 1, instead of scaling linearly with N."""
    check_count = 0
    while True:
        with _task_lock:
            tasks = [t for t in active_tasks.values()
                     if t.get("kind") == "presence" and t["hwnd"] == hwnd and t["status"] == "watching"]
        if not tasks:
            with _task_lock:
                _presence_loop_hwnds.discard(hwnd)
            dbg(f"[PRESENCE] group loop for hwnd {hwnd} ending — no friends left tracked on this window")
            return
        time.sleep(PRESENCE_CHECK_INTERVAL)

        with _task_lock:
            tasks = [t for t in active_tasks.values()
                     if t.get("kind") == "presence" and t["hwnd"] == hwnd and t["status"] == "watching"]
        if not tasks:
            continue

        title = tasks[0]["title"]

        if not win32gui.IsWindow(hwnd):
            with _task_lock:
                for t in tasks:
                    if t["id"] in active_tasks:
                        active_tasks[t["id"]]["status"] = "lost"
            msg = f"Lost track of {title} — looks like it closed. Stopping presence tracking."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            _push_all_tasks()
            with _task_lock:
                _presence_loop_hwnds.discard(hwnd)
            return

        img = capture_window_bg(hwnd)
        if img is None or img.width == 0:
            continue

        thumb = img.resize((480, max(1, int(480 * img.height / img.width))), Image.LANCZOS)
        buf_ui = BytesIO(); thumb.save(buf_ui, format="JPEG", quality=65)
        names_label = ", ".join(t["friend_name"] for t in tasks)
        push_to_ui("screenshot_update", {
            "b64": base64.b64encode(buf_ui.getvalue()).decode("utf-8"),
            "window": f"Tracking: {names_label} on {title}", "source": "task",
        })

        if not online():
            continue

        crop_img = _grab_presence_crop(img)
        check_count += 1
        names = [t["friend_name"] for t in tasks]
        system_prompt = (
            "You're reading a zoomed-in crop of a game or app window to determine CURRENT connection "
            "status for SEVERAL specific people at once. Two kinds of evidence, in order of "
            "reliability: (1) a friends/player list entry — e.g. \"NAME - Playing ...\" or \"NAME - "
            "Online\" means online, \"NAME - Offline\" means offline — this is the most reliable signal "
            "since it reflects current state directly. (2) A recent join/leave chat/log line (\"X joined "
            "the game\", \"[+] X\", \"X left the network\", \"[-] X\", etc, wording varies by app) as "
            "secondary evidence if no player list is visible. Text may be small, stylized, or "
            "surrounded by unrelated clutter — scan carefully for each exact name given. Watch out for "
            "similar-but-different usernames (e.g. \"Name\" vs \"name_\" are different accounts) — only "
            "match the EXACT name given. Reply with ONLY a JSON object mapping each given name to "
            'exactly one of "ONLINE", "OFFLINE", or "UNKNOWN" (if genuinely not determinable from what\'s '
            'visible). Example: {"Alice": "OFFLINE", "Bob": "ONLINE"}. No other text.')
        user_text = "People to check: " + ", ".join(names)
        answer, usage = gemini_vision_call(system_prompt, user_text, [crop_img])
        if answer is None:
            dbg(f"[PRESENCE] check #{check_count}: no response (Gemini unavailable?)")
            continue
        if usage:
            dbg(f"[PRESENCE] check #{check_count} tokens: prompt={usage.prompt_token_count} "
                f"completion={usage.candidates_token_count} total={usage.total_token_count} "
                f"(for {len(names)} friend(s): {names_label})")

        try:
            cleaned = re.sub(r"^```[\w]*", "", _strip_thinking(answer).strip()).rstrip("`").strip()
            results = json.loads(cleaned)
        except Exception as e:
            dbg(f"[PRESENCE] check #{check_count} JSON parse error: {e} — raw: {answer!r}")
            continue

        dbg(f"[PRESENCE] check #{check_count}: {results!r}")
        results_lower = {k.strip().lower(): str(v).strip().upper() for k, v in results.items()}
        for t in tasks:
            verdict = results_lower.get(t["friend_name"].strip().lower())
            if verdict not in ("ONLINE", "OFFLINE"):
                continue
            new_status = "online" if verdict == "ONLINE" else "offline"
            pending = _presence_pending.get(t["id"])
            if pending and pending["status"] == new_status:
                pending["count"] += 1
            else:
                pending = {"status": new_status, "count": 1}
            _presence_pending[t["id"]] = pending
            if pending["count"] >= 2:
                _update_presence(t["id"], new_status)

PRESENCE_QUERY_WORDS = ["online", "offline", "here", "joined", "still in", "still on", "status", "around", "connected"]
PRESENCE_PRONOUNS = ["she", "he", "they", "her", "him", "them"]

def detect_presence_query(text):
    """Answers 'is <name> online?' — and pronoun follow-ups like 'is she
    online yet?' — directly from friend_presence, grounded in what the
    presence watch has actually seen, instead of falling through to the
    general chat LLM, which has no way to know and would just improvise a
    wrong-but-confident answer.

    BUG FIX: this used to require the tracked person's literal name to
    appear in the message. A natural follow-up like "is she online yet?"
    has no name in it at all — it fell straight through to the general chat
    LLM, which had zero context about presence tracking and answered about
    ITSELF being online instead. Now a pronoun + a query word resolves to
    the currently-tracked friend (or, with several tracked, whichever one's
    status changed most recently — the one most likely being asked about).
    """
    if not friend_presence:
        return None
    t = text.lower()
    if not any(w in t for w in PRESENCE_QUERY_WORDS):
        return None
    for key, info in friend_presence.items():
        if key in t:
            return info
    words = re.findall(r"[a-z']+", t)
    if any(p in words for p in PRESENCE_PRONOUNS):
        return max(friend_presence.values(), key=lambda i: i["updated"])
    return None


COMPOUND_COMBINATORS = [" if ", " unless ", " and then ", " after that ", " once that",
                         " but if ", " otherwise ", " in that case", " within "]
COMPOUND_ACTION_WORDS = ["shutdown", "shut down", "restart", "open", "notify", "tell me", "let me know"]

def detect_compound_request(text):
    """Cheap heuristic gate before spending an LLM call on planning — looks
    for combinators suggesting multiple linked actions/conditions, so a
    plain single-intent message doesn't get routed through the (slower,
    costlier) planner unnecessarily."""
    t = text.lower()
    return (any(c in t for c in COMPOUND_COMBINATORS)
            and any(a in t for a in COMPOUND_ACTION_WORDS))

def plan_compound_request(user_input):
    """Returns a list of step dicts, or None if this wasn't actually a
    compound/conditional request after all (the cheap gate above is
    intentionally permissive; this LLM call is the real check)."""
    now = datetime.now()
    system = (
        "Break this request into a short JSON plan using ONLY these primitive actions Xenora already "
        "supports:\n"
        '- open_app: {"name": "..."}\n'
        '- wait_seconds: {"seconds": N}\n'
        '- wait_for_condition: {"target_app": "...", "condition": "one precise, visually-checkable '
        'sentence", "timeout_seconds": N}\n'
        '- wait_for_presence: {"target_app": "...", "friend_name": "...", "event": "join" or "leave", '
        '"timeout_seconds": N}\n'
        '- power_action: {"action": "shutdown" or "restart"}\n'
        '- notify: {"message": "..."}\n'
        'Each step needs a unique integer "id". A step MAY have "run_if": {"step": <id of an earlier '
        'wait_for_* step>, "result": true or false} — true means that step\'s condition WAS met before '
        "its timeout; false means it TIMED OUT without being met. Only wait_for_* steps produce a usable "
        f"result for run_if. Current time: {now.strftime('%I:%M %p on %A, %B %d, %Y')} — convert any "
        "relative duration (\"in 30 minutes\", \"within an hour\") to timeout_seconds.\n"
        'Return ONLY JSON: {"steps": [...]}. If this genuinely isn\'t a compound/conditional request, '
        'return {"steps": []}. No explanation, no markdown fences.'
    )
    try:
        raw = llm(system, user_input, temperature=0.2)
        cleaned = re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip()
        steps = json.loads(cleaned).get("steps", [])
        return steps if steps else None
    except Exception as e:
        dbg(f"[PLAN] parse error: {e}")
        return None

def _blocking_wait_for_presence(target_app, friend_name, event, timeout_seconds):
    """Self-contained bounded wait (unlike start_presence_watch's tasks,
    which run indefinitely) — registers a lightweight, cancellable entry in
    active_tasks purely for Active Tasks panel visibility, but its loop is
    independent of the persistent presence-tracking system. Returns True if
    the event was observed before the timeout, False otherwise (timeout OR
    window not found OR cancelled)."""
    hwnd, title = find_window(target_app) if WINDOW_DEPS_OK else (None, None)
    if not hwnd:
        return False
    deadline = time.time() + max(1, timeout_seconds)
    task_id = str(time.time())
    with _task_lock:
        active_tasks[task_id] = {
            "id": task_id, "kind": "plan_wait", "hwnd": hwnd, "title": title,
            "friend_name": friend_name, "condition": f"{friend_name} {event}ing {title} (plan step)",
            "action": "notify", "notify_message": "", "created": time.time(), "status": "watching",
        }
    _push_all_tasks()

    system_prompt = (
        "You're reading a zoomed-in crop of a game or app window to determine CURRENT connection "
        "status for a specific person. Two kinds of evidence, in order of reliability: (1) a friends/"
        "player list entry (\"NAME - Playing ...\" or \"NAME - Online\" means online, \"NAME - Offline\" "
        "means offline) — most reliable, reflects current state directly. (2) A recent join/leave chat/"
        "log line as secondary evidence if no player list is visible. Text may be small or stylized — "
        "scan carefully for the exact name given. Watch out for similar-but-different usernames. Reply "
        "with exactly one word: ONLINE, OFFLINE, or UNKNOWN.")

    result = False
    last_status = None
    while time.time() < deadline:
        with _task_lock:
            still_active = task_id in active_tasks and active_tasks[task_id]["status"] == "watching"
        if not still_active:
            break
        time.sleep(min(PRESENCE_CHECK_INTERVAL, max(1, deadline - time.time())))
        if time.time() >= deadline or not win32gui.IsWindow(hwnd):
            break
        img = capture_window_bg(hwnd)
        if img is None or img.width == 0 or not online():
            continue
        crop_img = _grab_presence_crop(img)
        answer, usage = gemini_vision_call(system_prompt, f"Person to check: {friend_name}", [crop_img])
        if answer is None:
            continue
        status = _last_match(_strip_thinking(answer.upper()), "ONLINE", "OFFLINE")
        if status:
            last_status = status
        target_status = "ONLINE" if event == "join" else "OFFLINE"
        if last_status == target_status:
            result = True
            break

    with _task_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["status"] = "done"
    _push_all_tasks()
    return result

def _blocking_wait_for_condition(target_app, condition, timeout_seconds):
    """Same shape as _blocking_wait_for_presence but for a generic visual
    YES/NO condition instead of a named person's join/leave."""
    hwnd, title = find_window(target_app) if WINDOW_DEPS_OK else (None, None)
    if not hwnd:
        return False
    deadline = time.time() + max(1, timeout_seconds)
    task_id = str(time.time())
    with _task_lock:
        active_tasks[task_id] = {
            "id": task_id, "kind": "plan_wait", "hwnd": hwnd, "title": title,
            "condition": f"{condition} (plan step)", "action": "notify", "notify_message": "",
            "created": time.time(), "status": "watching",
        }
    _push_all_tasks()

    result = False
    while time.time() < deadline:
        with _task_lock:
            still_active = task_id in active_tasks and active_tasks[task_id]["status"] == "watching"
        if not still_active:
            break
        time.sleep(min(TASK_CHECK_INTERVAL, max(1, deadline - time.time())))
        if time.time() >= deadline or not win32gui.IsWindow(hwnd):
            break
        img = capture_window_bg(hwnd)
        if img is None or img.width == 0 or not online():
            continue
        small = img.resize((720, max(1, int(720 * img.height / img.width))), Image.LANCZOS)
        answer, usage = gemini_vision_call(
            "Reply with exactly one word: YES if the described condition is clearly visible right now, otherwise NO.",
            f"Condition: {condition}", [small],
        )
        if answer is None:
            continue
        if _last_match(_strip_thinking(answer.upper()), "YES", "NO") == "YES":
            result = True
            break

    with _task_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["status"] = "done"
    _push_all_tasks()
    return result

def _execute_plan_step(step, results):
    """Executes ONE step. Returns the step's result — True/False for
    wait_for_* steps (condition met vs timed out), None for everything
    else (they either happen or don't; nothing later can branch on them)."""
    action = step.get("action")
    params = step.get("params", {}) or {}

    run_if = step.get("run_if")
    if run_if and results.get(run_if.get("step")) != run_if.get("result"):
        return None

    if action == "open_app":
        name = params.get("name", "")
        open_app_by_name(name)
        _log_action(f"Opened {name}.")
        return None

    if action == "wait_seconds":
        time.sleep(max(0, params.get("seconds", 0)))
        return None

    if action == "wait_for_condition":
        return _blocking_wait_for_condition(
            params.get("target_app", ""), params.get("condition", ""), params.get("timeout_seconds", 1800))

    if action == "wait_for_presence":
        return _blocking_wait_for_presence(
            params.get("target_app", ""), params.get("friend_name", ""),
            params.get("event", "join"), params.get("timeout_seconds", 1800))

    if action == "power_action" and params.get("action") in ("shutdown", "restart"):
        run_power_countdown(params["action"], str(time.time()), "")
        return None

    if action == "notify":
        msg = params.get("message", "")
        if msg:
            add_chat_message("assistant", msg); speak(msg, style="chat")
            send_notification("Xenora", msg)
        return None

    dbg(f"[PLAN] unknown step action: {action!r}")
    return None

def _run_plan(steps):
    results = {}
    for step in steps:
        try:
            results[step.get("id")] = _execute_plan_step(step, results)
        except Exception as e:
            dbg(f"[PLAN] step {step.get('id')} ({step.get('action')}) error: {e}")

def _describe_plan_step(step):
    """Turns one plan step into a plain-English clause, used to build a
    concrete confirmation message instead of the old generic 'Got it — I'll
    take care of that.' — which carried zero detail, so any later reference
    ('thanks for looking at my download') had nothing real to resolve
    against in chat_history."""
    action = step.get("action")
    params = step.get("params", {}) or {}
    if action == "open_app":
        return f"open {params.get('name', 'that app')}"
    if action == "wait_for_condition":
        return f"watch {params.get('target_app', 'that window')} until {params.get('condition', 'the condition is met')}"
    if action == "wait_for_presence":
        return f"watch for {params.get('friend_name', 'them')} to {params.get('event', 'join')} {params.get('target_app', '')}".strip()
    if action == "wait_seconds":
        secs = params.get("seconds", 0)
        return f"wait {secs}s" if secs else "wait a bit"
    if action == "power_action":
        return f"{params.get('action', 'shut down')} the laptop"
    if action == "notify" and params.get("message"):
        return f"let you know: {params['message']}"
    return None

def start_compound_plan(steps):
    clauses = [c for c in (_describe_plan_step(s) for s in steps) if c]
    msg = f"Got it — I'll {'; then '.join(clauses)}." if clauses else "Got it — I'll take care of that."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    threading.Thread(target=_run_plan, args=(steps,), daemon=True).start()


def flash_orb_done():
    """Bumps a timestamp the orb (via main.js's state poll) watches for, to
    flash a distinct color for a few seconds whenever a task/reminder fires."""
    ui_state["task_flash"] = time.time()
    push_to_ui("task_flash", ui_state["task_flash"])

def _fire_task(task_id):
    with _task_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        task["status"] = "done"
    _push_all_tasks()

    msg = task["notify_message"]
    add_chat_message("assistant", msg); speak(msg, style="cheerful")
    send_notification("Xenora", msg)
    flash_orb_done()

    if task["action"] != "notify":
        threading.Thread(target=run_power_countdown, args=(task["action"], task_id, msg), daemon=True).start()


def start_scheduled_action(task):
    try:
        delay = max(5, min(int(task.get("delay_seconds") or 300), 24 * 3600))
    except (TypeError, ValueError):
        delay = 300
    action  = task.get("action") or "notify"
    msg     = (task.get("message") or "your reminder").strip()
    fire_at = time.time() + delay
    at_str  = datetime.fromtimestamp(fire_at).strftime('%I:%M %p').lstrip('0')

    sched_id = str(time.time())
    with _task_lock:
        active_schedules[sched_id] = {
            "id": sched_id, "kind": "schedule", "action": action, "message": msg,
            "fire_at": fire_at, "created": time.time(), "status": "scheduled",
        }
    _push_all_tasks()

    if action == "notify":
        ack = f"Got it — I'll remind you about {msg.rstrip('.').lower()} at {at_str}."
    else:
        ack = f"Scheduled — I'll {action} the laptop at {at_str}."
    add_chat_message("assistant", ack); speak(ack, style="chat")

    def _fire():
        with _task_lock:
            if sched_id in active_schedules and active_schedules[sched_id]["status"] != "cancelled":
                active_schedules[sched_id]["status"] = "done"
            elif sched_id in active_schedules:
                return
        _push_all_tasks()
        if action == "notify":
            text = f"Reminder: {msg}"
            add_chat_message("assistant", text); speak(text, style="cheerful")
            send_notification("Xenora — Reminder", msg)
            flash_orb_done()
        else:
            flash_orb_done()
            threading.Thread(target=run_power_countdown, args=(action, sched_id, msg), daemon=True).start()

    threading.Timer(delay, _fire).start()

def start_deferred_action(parsed):
    if parsed["kind"] == "watch":
        start_watch_task(parsed)
    elif parsed["kind"] == "presence":
        start_presence_watch(parsed)
    else:
        start_scheduled_action(parsed)


def describe_current_tasks():
    """Grounds 'what are you doing?' in real state instead of leaving the LLM
    to guess or invent — mirrors how last_seen/current time are grounded above."""
    with _task_lock:
        watches   = [t for t in active_tasks.values() if t["status"] == "watching"]
        schedules = [s for s in active_schedules.values() if s["status"] == "scheduled"]
        countdown = dict(_pending_power) if _pending_power else None
    parts = []
    for t in watches:
        parts.append(f"watching {t['title']} for {t['condition'].rstrip('.').lower()}")
    for s in schedules:
        when = datetime.fromtimestamp(s["fire_at"]).strftime('%I:%M %p').lstrip('0')
        if s["action"] == "notify":
            parts.append(f"reminding you about {s['message'].rstrip('.').lower()} at {when}")
        else:
            parts.append(f"set to {s['action']} the laptop at {when}")
    if countdown:
        cd = ui_state.get("power_countdown")
        left = f" ({cd['seconds_left']}s left)" if cd else ""
        parts.append(f"counting down to {countdown['type']} the laptop{left}")
    try:
        if XI:
            parts += XI.describe()
    except Exception as e:
        dbg(f"[BRAIN] couldn't describe automations: {e}")
    return parts

def ask_xenora(user_input, memory):
    global chat_history, xenora_code, code_hash, capabilities_summary
    current_code = read_own_code()
    if current_code:
        current_hash = get_code_hash(current_code)
        if current_hash != code_hash:
            xenora_code          = current_code
            code_hash            = current_hash
            capabilities_summary = summarize_own_capabilities(current_code)

    mem_lines = []
    now = datetime.now()
    mem_lines.append(f"- Right now it is {now.strftime('%A, %B %d, %Y, %I:%M %p')}.")
    if memory.get("last_seen"):
        try:
            last = datetime.fromisoformat(memory["last_seen"])
            mem_lines.append(
                f"- User last spoke with you {humanize_delta((now - last).total_seconds())} ago "
                f"(around {last.strftime('%I:%M %p on %b %d')}). ONLY mention this if User directly "
                f"asks something like \"when did we last talk\" or \"how long has it been\" — do NOT "
                f"bring it up on your own, including in greetings, opening replies, or small talk, even "
                f"though it's sitting right here in your context. If asked, state it plainly and "
                f"accurately — never invent a vaguer or different figure."
            )
        except Exception:
            pass
    if memory.get("user_name"):
        mem_lines.append(f"- User's name is {memory['user_name']}. Only state it when directly asked.")
    task_lines = describe_current_tasks()
    if task_lines:
        mem_lines.append(
            "- Currently running in the background: " + "; ".join(task_lines) + ". "
            "Mention this plainly and accurately if asked what you're doing/working on/watching — "
            "never invent a task that isn't listed here, and never omit one that is."
        )
    else:
        mem_lines.append("- Nothing is currently running in the background (no active watches, schedules, or countdowns).")
    if _graph.get("nodes"):
        retrieved = graph_retrieve_context(user_input)
        if retrieved:
            mem_lines.append(
                "- Memory retrieved for this message (each tagged with its category and when it "
                "was saved — categories 'preference', 'warning', and 'system' are always included; "
                "the rest were pulled because they're relevant to what was just said). Time-bound "
                "entries ('ordered X', 'meeting at Y') describe something that was true AT THAT "
                "MOMENT — if it's more than a couple hours old, treat that specific event as already "
                "finished unless User brings it up again. Durable entries (preferences, warnings, "
                "who people are) don't expire the same way:"
            )
            for n in retrieved:
                ts = (n.get("created") or "")[:16].replace("T", " ")
                mem_lines.append(f"    • [{n['type']}] {n['content']} ({ts})")
    if memory.get("session_summaries"):
        mem_lines.append("- Previous sessions (last 3):")
        for s in memory["session_summaries"][-3:]:
            mem_lines.append(f"    [{s['date']}] {s['summary']}")
    memory_context = "\n".join(mem_lines) if mem_lines else "No prior memory yet."

    system_prompt = (
        "HARD RULES:\n"
        "1. Use 'sir' at most twice per response, mid-sentence only.\n"
        "2. Never start or end a sentence with 'sir'.\n"
        "3. Never use 'User' as a greeting.\n"
        "4. Never use exclamation marks.\n"
        "5. This reply is PLAIN CONVERSATION ONLY — it cannot download, open an app, browse the web, "
        "start a watch/tracking task, or perform any action. If asked to do something actionable, NEVER "
        "claim it is downloading, opening, saving, searching, tracking, being watched for, or otherwise "
        "in progress or complete — that would be false, since nothing here actually runs it. This "
        "applies fully to watch/tracking requests: never say you'll 'set a watch', 'let you know when X "
        "joins', or 'notify you when' anything — a real tracking task appears in the background-task "
        "line under MEMORY below, and if it isn't there, it does not exist. Instead: ask a clarifying "
        "question, explain what you'd need, or say plainly that the request wasn't understood as an "
        "action this turn and to try rephrasing it. Only report a download/open/task as done if it's "
        "explicitly listed under SELF-AWARENESS or the background-task line under MEMORY below — never "
        "invent progress on anything else.\n"
        "6. Never bring up a player joining/leaving, a download, a task or any other event on your own. "
        "A greeting or small talk gets a greeting or small talk back, nothing else. Only mention such an "
        "event if the user asks about it or it is listed in the background-task line under MEMORY below.\n\n"
        + XENORA_PREFIX
        + f"\n\nSELF-AWARENESS\n{capabilities_summary}"
        + f"\n\nMEMORY\n{memory_context}"
        + "\n\nSYSTEM CONTROL"
        + "\nUse COMMAND lines ONLY for local apps. Never for websites or downloads."
        + "\n\nLocal apps:\n" + "\n".join(f"  {k}" for k in ACTION_MAP.keys())
        + "\n\nExamples:"
        + "\n  'open discord'  → COMMAND: open_discord"
        + "\n  'open youtube'  → reply normally"
        + "\n  'download song' → reply normally"
    )

    chat_history.append({'role': 'user', 'content': user_input})

    if online():
        try:
            reply = _cloud_chat(system_prompt, chat_history, temperature=0.7)
        except Exception as e:
            dbg(f"[ASK] Groq AND Gemini both exhausted every model: {e}")
            reply = _ask_ollama_fallback(system_prompt, chat_history, groq_error=str(e))
    else:
        reply = _ask_ollama_fallback(system_prompt, chat_history)

    chat_history.append({'role': 'assistant', 'content': reply})
    return reply


def _ask_ollama_fallback(system_prompt, history, groq_error=None):
    """Last resort, reached only when every cloud model on both Groq and
    Gemini has failed (or there's no internet at all) AND local Ollama also
    isn't reachable. Logs full technical detail to the debug log/terminal for
    you, but never surfaces raw plumbing ("Ollama isn't running", "check the
    Groq connection") to the chat — that reads as a broken app, not Xenora.
    Instead she says, in character, that she's having trouble thinking right
    now and to try again shortly."""
    try:
        result = ollama.chat(
            model=LOCAL_MODEL,
            options={"temperature": 0.7, "repeat_penalty": 1.2},
            messages=[{'role': 'system', 'content': system_prompt}] + history
        )
        reply = re.sub(r"<think>.*?</think>", "", result['message']['content'] or "", flags=re.S).strip()
        _set_last_model(f"Local · {LOCAL_MODEL}")
        return reply
    except Exception as e:
        dbg(f"[ASK] Ollama fallback also failed: {e}"
            + (f" (after Groq/Gemini error: {groq_error})" if groq_error else ""))
        return random.choice([
            "Give me a moment — my thinking is stuck right now. Try that again in a bit.",
            "I'm having trouble reaching anyone upstairs at the moment. Ask me again shortly.",
            "Something's blocking my thoughts right now — say that again in a minute or two.",
        ])


OVERCLOCK_PREFIX = (
    "You are Xenora, in OVERCLOCK mode — a much larger, slower reasoning model "
    "swapped in specifically for hard coding, mathematics, and other advanced "
    "problems. Stay recognizably Xenora (same personality, address the user as "
    "User when it fits naturally), but prioritize technical correctness, rigor, "
    "and showing your work over brevity or personality flourishes. Unlike normal "
    "mode, proper Markdown is expected and preserved here — use fenced code "
    "blocks with language tags, inline code, numbered steps, and LaTeX-style math "
    "notation where it genuinely helps. Taking longer to answer is fine and "
    "expected; getting it right matters more than getting it fast."
)

def _nvidia_chat(system_prompt, messages, temperature=0.3, max_tokens=4096):
    """The only model call in Overclock mode — no Groq/Gemini/local fallback
    chain here on purpose (see OVERCLOCK_MODEL notes). Raises on failure so
    the caller can give an honest, in-character 'not available right now'
    reply instead of silently swapping to a different, much weaker model
    that would defeat the point of switching into this mode at all."""
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY not configured")
    payload = {
        "model": OVERCLOCK_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
    }
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    r = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    text = (data["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError("Nemotron returned an empty response")
    _set_last_model("Overclock · Nemotron 3 Ultra")
    return text

def ask_overclock(user_input, memory):
    """Overclock's entire reply path — deliberately does none of what
    ask_xenora() does beyond basic memory bookkeeping: no self-awareness
    block, no background-task line, no COMMAND examples, since none of
    those local actions are available while this mode is on anyway."""
    global chat_history
    chat_history.append({'role': 'user', 'content': user_input})
    try:
        reply = _nvidia_chat(OVERCLOCK_PREFIX, chat_history, temperature=0.3)
    except Exception as e:
        dbg(f"[OVERCLOCK] Nemotron call failed: {e}")
        reply = (
            "Overclock is on, but I can't reach Nemotron right now — check that "
            "NVIDIA_API_KEY is set and there's an internet connection, or switch "
            "Overclock off to fall back to normal mode."
        )
    chat_history.append({'role': 'assistant', 'content': reply})
    return reply


ACTIONABLE_HINTS = list(set(
    OPEN_INTENT_WORDS + DOWNLOAD_INTENT_WORDS + MEDIA_HINTS + DEFERRED_HINTS +
    ["screen", "window", "monitor", "display", "happening", "doing",
     "installer", "setup", "software", "program", "app", "application",
     "rename", "delete", "remove", "trash", "recycle", "move", "file", "folder",
     "priority", "prioritize", "shortcut", "search first", "check first",
     "summarize", "summarise", "read", "says", "contents", "spreadsheet",
     "pdf", "docx", "document", "report", "resume", "invoice",
     "create", "generate", "draft", "write me", "make me", "word doc", "excel"]
))

_ACTIONABLE_RX = None

def _looks_actionable(text):
    """Cheap pre-filter before the intent LLM call. Whole-word match (with simple endings like -s/-ing/-ed): the old
    plain substring test made 'app' match 'happy', 'read' match 'already', 'move' match 'remove', and so on."""
    global _ACTIONABLE_RX
    if _ACTIONABLE_RX is None:
        parts = sorted({h.strip().lower() for h in ACTIONABLE_HINTS if h and h.strip()}, key=len, reverse=True)
        _ACTIONABLE_RX = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(h) for h in parts) + r")(?:s|es|ed|ing|d)?(?![a-z0-9])")
    return bool(_ACTIONABLE_RX.search(text.lower()))

DEFAULT_ACTION_ITEM = {"intent": "chat", "target": "", "targets": [], "is_url": False,
                        "media_type": "audio", "file_action": "", "new_name": "", "destination": "",
                        "config_action": "", "category": "", "path": "", "priority_list": [],
                        "target_question": "", "doc_type": "", "content_description": "",
                        "filename": "", "save_location": "",
                        "defer_kind": "", "friend_name": "", "defer_target_app": "",
                        "defer_condition": "", "delay_seconds": 0, "defer_action": "notify",
                        "defer_message": "", "defer_open_app": "",
                        "confidence": "high", "clarify_question": None}

INTENT_SCHEMA_PROMPT = (
    "Break this message into one or more discrete ACTIONS Xenora should take, in the order asked. "
    "Most messages are exactly one action — only return more than one when the message genuinely asks "
    "for more than one distinct thing, e.g. 'open discord and unity hub' (one action, two targets) or "
    "'open blender and tell me what you see' (two separate actions: open_app, then screen_question). "
    "Ground every action in the CONTEXT given below — resolve pronouns ('it', 'that', 'them') and "
    "implicit references against it, and KEEP any disambiguating detail present there (a language, an "
    "artist, a version, a qualifier) instead of dropping it — dropping such detail has previously "
    "caused the wrong file/video to be found. If a file/folder was just opened or mentioned in the "
    "CONTEXT, 'it'/'its content'/'explain it' refers to THAT SAME file — resolve target to it directly, "
    "don't leave it as chat.\n"
    "NEVER substitute a vague generic guess (a bare folder name like 'Documents') for a specific "
    "filename the user actually typed, even a messy or unusually formatted one — if you can't fully "
    "parse it, set confidence low and ask them to confirm the exact name, don't guess something "
    "completely different from what they said.\n"
    "If the CONTEXT shows Xenora just asked a clarifying "
    "question (audio/video, which app, which source, etc.), treat THIS message as the answer and "
    "classify it as the SAME action type being clarified — e.g. if she just asked 'audio or video?' "
    "for a download, a reply like 'just the audio, get it from youtube' is intent=download with that "
    "answer folded into the target, NOT intent=chat.\n"
    "CRITICAL — keep qualifiers scoped to the action they were stated for. When a message mentions the "
    "same base subject twice with DIFFERENT actions and DIFFERENT qualifiers (e.g. 'play the song X, "
    "then download it in 8D audio'), a qualifier stated for one action (8D, remix, slowed, a specific "
    "version) belongs ONLY to that action — do NOT let it leak backward or forward onto a different "
    "action's target just because they reference the same subject. 'play the song X' stays a plain "
    "playback of X; only 'download it in 8D' carries the 8D qualifier. If you genuinely cannot tell "
    "whether a qualifier is meant to apply to one action or both, set confidence low on the affected "
    "action(s) and ask directly rather than guessing which one it belongs to.\n"
    "Return ONLY JSON: {\"actions\": [ <action>, ... ]} where each <action> is:\n"
    '{"intent": "open_app" | "open_web" | "web_search" | "generate_image" | "analyze_media" | '
    '"download" | "screen_question" | "defer" | '
    '"file_op" | "file_config" | "read_file" | "write_file" | "chat", '
    '"target": "resolved app name / URL / search phrase / exact media title with any language or '
    'version qualifier kept — used when this action has exactly ONE item", '
    '"targets": ["resolved app names"] — ONLY for open_app when this ONE action names MULTIPLE apps '
    '(e.g. "discord and unity hub"); omit or leave empty otherwise, '
    '"is_url": true or false, '
    '"media_type": "audio" or "video" or "software", '
    '"file_action": "open" | "list" | "rename" | "move" | "delete" | "edit" — ONLY for file_op '
    '("edit" = change the CONTENTS of an existing file, e.g. "fix the typos in my resume.docx"; put the '
    'requested change, in their own words, in "content_description"), '
    '"new_name": "new filename — ONLY for file_op rename", '
    '"destination": "destination folder name — ONLY for file_op move", '
    '"config_action": "set_shortcut" | "set_priority" | "show_config" — ONLY for file_config, '
    '"category": "category name, e.g. games/music — ONLY for file_config set_shortcut", '
    '"path": "full folder path — ONLY for file_config set_shortcut", '
    '"priority_list": ["folder names in the order requested"] — ONLY for file_config set_priority, '
    '"target_question": "what they want to know, in their own words, or empty for a plain summary — '
    'ONLY for read_file", '
    '"doc_type": "docx" | "xlsx" | "pdf" | "txt" — ONLY for write_file (map naturally: word doc/report '
    '-> docx, excel/spreadsheet -> xlsx, pdf -> pdf, notes/text file -> txt), '
    '"content_description": "what the document should actually contain, in their own words — ONLY for '
    'write_file", '
    '"filename": "requested filename without extension, or empty to auto-name from content — ONLY for '
    'write_file", '
    '"save_location": "folder name if a specific save location was mentioned, else empty (defaults to '
    'Documents) — ONLY for write_file", '
    '"defer_kind": "presence" | "watch" | "schedule" — ONLY for defer, '
    '"friend_name": "person being tracked — ONLY for defer_kind presence", '
    '"defer_target_app": "short window/app name — ONLY for defer_kind presence or watch", '
    '"defer_condition": "one precise visually-checkable sentence — ONLY for defer_kind watch", '
    '"delay_seconds": 0 — ONLY for defer_kind schedule, '
    '"defer_action": "notify" | "shutdown" | "restart" — ONLY for defer, '
    '"defer_message": "short message announcing the trigger — ONLY for defer", '
    '"defer_open_app": "app to open first, or empty — ONLY for defer", '
    '"confidence": "high" or "low", '
    '"clarify_question": "a short question to ask back, ONLY if confidence is low, else null"}\n\n'
    "intent guide:\n"
    "- open_app: launch a local application/game/program by name. A song/video/media title is NEVER "
    "open_app even when phrased as 'play X' — 'play shivers by ed sheeran' is open_web (or download), "
    "not open_app. Only use open_app when X is clearly a known local application or game itself (e.g. "
    "'play GTA V', 'open Discord', 'launch Minecraft').\n"
    "- open_web: NAVIGATE somewhere. Only use this when the person uses an explicit navigation verb — "
    "'open', 'play', 'pull up', 'show me the page for', 'search the web for' — i.e. they clearly want a "
    "TAB opened, not a spoken answer. A bare link is NEVER enough on its own for this intent; see "
    "analyze_media below, which is the default for a bare video link.\n"
    "- web_search: ASKING something that needs current or real-world information Xenora can't already "
    "be certain of — news, scores, prices, weather, recent events, 'who/what/when/how many is X' about "
    "anything that changes over time. Answered directly in chat from real search results; NO browser "
    "tab opens for this. If genuinely unsure between open_web and web_search, prefer web_search — it "
    "only fails open into an offer to open a browser, never opens one on its own.\n"
    "- generate_image: wants Xenora to CREATE/DRAW/MAKE an image from a description (not analyze an "
    "existing one). Put the description in content_description.\n"
    "- analyze_media: a video URL (YouTube, Reddit, Instagram, TikTok, X/Twitter, a direct video link, "
    "etc.) appears in the message. target = the URL, target_question = what they want to know (empty = "
    "general reaction/summary). THIS IS THE DEFAULT for any message containing a video link, even with "
    "no explicit question — 'omg this is so good! <link>' means look at it and react, not open a tab and "
    "say nothing about it. Only route a video link to open_web instead if the person explicitly used a "
    "navigation verb (see open_web above) — excitement, reactions, or silence about what to do with the "
    "link are NOT that. An uploaded/attached audio or video file is handled separately, not through "
    "this intent.\n"
    "- download: explicitly wants to save/download something (a song, video, installer, file). A "
    "question ABOUT an existing download's progress/status is screen_question, not this.\n"
    "- screen_question: asking what's visible / happening on their screen right now — including "
    "questions about the STATUS or PROGRESS of something Xenora is currently watching/downloading "
    "(e.g. 'what's the download progress', 'is it done yet', 'how far along is it'), even without the "
    "words 'screen' or 'see' — these need a real live look to answer honestly, not a guess.\n"
    "- file_op: open/list/rename/move/delete an EXISTING file or folder on disk that is NOT a "
    "program/app — e.g. 'delete that report', 'rename photo.jpg to vacation.jpg', 'move the invoice to "
    "Documents', 'what's in my Downloads folder', 'open my resume.pdf'. target is the file/folder name "
    "ONLY — never include the generic word 'folder'/'file'/'directory' as part of target itself (for "
    "'open the assets folder', target is 'assets', NOT 'assets folder' — the real folder on disk is "
    "just named 'assets', including 'folder' in target makes the search fail to find it). Same for "
    "destination. (not full path — Xenora resolves it). Distinguish from open_app: 'open resume.pdf' is "
    "file_op, 'open Discord' is open_app. ANY request that says 'folder' is ALWAYS file_op, never "
    "open_app — a folder is never an application, no exceptions (e.g. 'open assets folder' is file_op "
    "even though 'assets' alone could sound like software).\n"
    "- file_config: changes HOW Xenora searches for files/folders, not a file operation itself — e.g. "
    "'search downloads before documents', 'my games are in D:\\Games so don't search for them', "
    "'what folders do you check', 'games folder is D:\\Games'. set_shortcut needs category+path; "
    "set_priority needs priority_list (folder names, in the order requested — mentioning just one means "
    "'put this first', not a full reordering).\n"
    "- read_file: wants the CONTENT of an existing file read, summarized, or asked about — e.g. "
    "'summarize my resume.pdf', 'what does the invoice say', 'what's in row 5 of my budget "
    "spreadsheet', 'read me the report'. target_question holds what they want to know (empty = plain "
    "summary). Distinguish from file_op: 'open resume.pdf' (launch it in its normal app) is file_op; "
    "'what does resume.pdf say' (extract and answer from its actual content) is read_file.\n"
    "- write_file: wants a NEW document created with real content — e.g. 'write a report on X and save "
    "it as a word doc', 'make me a spreadsheet tracking my expenses', 'create a PDF summary of this'. "
    "doc_type must be one of docx/xlsx/pdf/txt. content_description is what they actually want in it — "
    "capture their real intent, don't just repeat the filename. This CREATES a brand new file; it's "
    "never used for editing an existing one.\n"
    "- defer: any part of the message wants something to happen LATER, conditionally, on a timer, or "
    "tied to a person's presence/status (e.g. 'shut down in 5 minutes', 'tell me when X joins', "
    "'remind me at 6pm', 'shut down once the download finishes'). This is a REAL, first-class intent — "
    "use it whenever the message contains ANY such request, even alongside other immediate actions in "
    "the same message (e.g. 'play X then download it, also shut down in 5 minutes' is THREE actions: "
    "open_web, download, and defer). Never drop a deferred request just because the rest of the "
    "message is about something else. For a defer action, ALSO fill in defer_kind and the matching "
    "fields below — this saves a second classification pass, so fill them here rather than leaving "
    "them empty:\n"
    "    defer_kind \"presence\" — tracking whether a specific named PERSON is online/joined/left an "
    "app: set friend_name and defer_target_app.\n"
    "    defer_kind \"watch\" — waiting for a described VISUAL condition on a window: set "
    "defer_target_app and defer_condition (one precise, visually-checkable sentence).\n"
    "    defer_kind \"schedule\" — a plain time delay with nothing to watch: set delay_seconds "
    "(convert any clock time to whole seconds from now; if it already passed today, assume tomorrow).\n"
    "    For all three also set defer_action (\"notify\" | \"shutdown\" | \"restart\") and "
    "defer_message (short, under 12 words, announcing the trigger). If the message ALSO asks to open "
    "an app first, put its plain name in defer_open_app.\n"
    "- chat: anything else — conversation, a question with no action, small talk, narration that "
    "merely mentions an app/song without actually asking Xenora to do something with it.\n"
    "If genuinely ambiguous between two specific options (e.g. two language versions, two apps with "
    "similar names, song vs software), set confidence to low on that action and phrase "
    "clarify_question to resolve it directly — don't guess. If nothing in the message is actionable, "
    'return {"actions": [{"intent": "chat", ...}]}. ONLY valid JSON, no explanation.'
)

def resolve_intent(user_input):
    """Returns a list of action dicts (usually length 1). One shared LLM call
    replaces the old scattered per-feature keyword/regex classifiers AND
    handles genuinely compound immediate requests (multiple apps, or
    multiple different kinds of request glued together with 'and') the same
    way instead of silently dropping everything after the first thing it
    recognizes."""
    if not _looks_actionable(user_input):
        return [dict(DEFAULT_ACTION_ITEM)]
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in chat_history[-6:])
    screen = _last_context.get('description', '')
    context = f"Recent conversation:\n{recent}\n\nLast known screen observation: {screen or 'none'}"
    try:
        raw = llm(INTENT_SCHEMA_PROMPT, f"{context}\n\nMessage: {user_input}", temperature=0.1)
        cleaned = re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip()
        parsed = json.loads(cleaned)
        items = parsed.get("actions") or []
        if not items:
            return [dict(DEFAULT_ACTION_ITEM)]
        out = []
        seen_signatures = set()
        for item in items:
            merged = dict(DEFAULT_ACTION_ITEM)
            merged.update(item or {})
            if merged["intent"] not in ("open_app", "open_web", "web_search", "generate_image", "analyze_media", "download", "screen_question", "defer", "file_op", "file_config", "read_file", "write_file", "chat"):
                merged["intent"] = "chat"

            if merged["intent"] == "open_app" and "folder" in user_input.lower():
                folder_name = (merged.get("target") or "").strip() or \
                    (merged.get("targets") or [""])[0].strip()
                merged["intent"]      = "file_op"
                merged["file_action"] = "open"
                merged["target"]      = folder_name
                merged["targets"]     = []
            if merged["intent"] == "screen_question":
                sig = ("screen_question",)
            else:
                sig = (merged["intent"], (merged.get("target") or "").strip().lower(),
                       tuple(sorted(t.strip().lower() for t in (merged.get("targets") or []))),
                       (merged.get("clarify_question") or "").strip().lower())
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            out.append(merged)
        return out or [dict(DEFAULT_ACTION_ITEM)]
    except Exception as e:
        dbg(f"[INTENT] resolve error: {e}")
        return [dict(DEFAULT_ACTION_ITEM)]


def _log_action(text):
    """Silently records what actually happened into chat_history — no UI
    bubble, no speech — so later conversation ('thanks for looking at my
    download') has real grounding instead of only a vague 'Got it — I'll
    take care of that.' with no specifics behind it."""
    chat_history.append({'role': 'assistant', 'content': f"[Action] {text}"})
    if len(chat_history) > 80:
        del chat_history[:-80]


def answer_active_watch_status(user_input):
    """If a watch-task is currently running (e.g. monitoring a download),
    grab a FRESH screenshot of THAT SPECIFIC window and answer grounded in
    it — not the ambient/foreground screenshot buffer, which is very likely
    looking at Xenora's own chat window right now (since that's what's in
    the foreground while the question is being typed), not whatever's
    actually being tracked in the background. Returns None if there's no
    active watch task or the capture/vision call fails, so the caller can
    fall back to the normal ambient screen_question path."""
    with _task_lock:
        watch_tasks = [t for t in active_tasks.values() if t.get("kind") == "watch" and t["status"] == "watching"]
    if not watch_tasks or not WINDOW_DEPS_OK:
        return None
    task = watch_tasks[0]
    if not win32gui.IsWindow(task["hwnd"]):
        return None
    img = capture_window_bg(task["hwnd"])
    if img is None or img.width == 0:
        return None
    small = img.resize((960, max(1, int(960 * img.height / img.width))), Image.LANCZOS)
    prompt = (
        f"The user is asking: '{user_input}'\n"
        f"This is a live screenshot of {task['title']}, currently being monitored for: "
        f"{task['condition']}. Answer their question directly and specifically based on what's "
        f"visible right now (exact percentage, file name, status, etc.) — don't guess or generalize. "
        f"If this question genuinely seems unrelated to what's being watched, say so plainly instead "
        f"of forcing an answer about the wrong thing."
    )
    answer, usage = gemini_vision_call(
        XENORA_PREFIX + "\nYou are answering a direct question about a specific window you are actively monitoring.",
        prompt, [small], temperature=0.3, max_output_tokens=150,
    )
    if usage:
        dbg(f"[TASK-STATUS] tokens: total={usage.total_token_count}")
    return _strip_thinking(answer) if answer else None

def _handle_screen_question(action_item, user_input):
    task_answer = answer_active_watch_status(user_input)
    if task_answer:
        cleaned = clean_response(task_answer)
        chat_history.append({'role': 'user',      'content': user_input})
        chat_history.append({'role': 'assistant', 'content': cleaned})
        add_chat_message("assistant", cleaned, remember=False)
        speak_auto(cleaned)
        return True

    if not VISION_ENABLED:
        msg = "Vision's off right now — turn it on and I can actually look."
    elif not _screenshot_buffer:
        msg = "I don't have a screenshot yet — give the capture loop a second and ask again."
    else:
        answer = answer_screen_question(user_input)
        if answer:
            cleaned = clean_response(answer)
            chat_history.append({'role': 'user',      'content': user_input})
            chat_history.append({'role': 'assistant', 'content': cleaned})
            add_chat_message("assistant", cleaned, remember=False)
            speak_auto(cleaned)
            return True
        msg = "Couldn't get a read on the screen just now — connection or vision model may be down."
    add_chat_message("assistant", msg, remember=False)
    speak_auto(msg)
    return True

def _handle_download(action_item, user_input):
    target = (action_item.get("target") or "").strip()
    if not target:
        return False
    return handle_download(user_input, resolved_target=target,
                            media_type=action_item.get("media_type", "audio"))

def _handle_open_app(action_item, user_input):
    target  = (action_item.get("target") or "").strip()
    targets = [t for t in (action_item.get("targets") or []) if t] or ([target] if target else [])
    if not targets:
        return False
    opened_labels = []
    for name in targets:
        status, label = open_app_by_name(name)
        if status == "opened":
            opened_labels.append(label)
    if opened_labels:
        apps_str = opened_labels[0] if len(opened_labels) == 1 else \
            ", ".join(opened_labels[:-1]) + f" and {opened_labels[-1]}"
        msg = random.choice(APP_REPLIES).format(app=apps_str)
        add_chat_message("assistant", msg)
        speak(msg, style="chat")
    return True

def _handle_open_web(action_item, user_input):
    target = (action_item.get("target") or "").strip()
    if not target:
        return False
    if action_item.get("is_url"):
        return smart_open(f"open {target}")
    if "video" in (action_item.get("media_type") or "") or detect_video_open_request(user_input):
        if open_youtube_video(target):
            return True
    return smart_open(user_input)


try:
    from send2trash import send2trash
    SEND2TRASH_AVAILABLE = True
except ImportError:
    SEND2TRASH_AVAILABLE = False
    print("[Xenora] send2trash not installed — deletions are disabled until you run: "
          "pip install Send2Trash (this keeps deletes recoverable via the Recycle Bin "
          "instead of permanent).", flush=True)

FILE_SEARCH_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_search_config.json")

FILE_OP_KNOWN_FOLDERS = {
    "desktop":   os.path.join(os.path.expanduser("~"), "Desktop"),
    "documents": os.path.join(os.path.expanduser("~"), "Documents"),
    "downloads": os.path.join(os.path.expanduser("~"), "Downloads"),
    "pictures":  os.path.join(os.path.expanduser("~"), "Pictures"),
    "videos":    os.path.join(os.path.expanduser("~"), "Videos"),
    "music":     os.path.join(os.path.expanduser("~"), "Music"),
}

FILE_OP_FOLDER_ALIASES = {
    "downloads": "downloads", "download": "downloads",
    "documents": "documents", "docs": "documents", "my documents": "documents",
    "desktop": "desktop",
    "pictures": "pictures", "photos": "pictures", "photo": "pictures", "picture": "pictures",
    "videos": "videos", "video": "videos", "movies": "videos", "movie": "videos",
    "music": "music", "songs": "music", "song": "music",
}

def _resolve_known_folder_target(target):
    """A bare reference to one of the 6 known category folders ('Downloads',
    'my documents', 'photos folder') needs NO search at all — same
    reasoning as _resolve_drive_target for bare drive letters. Confirmed
    directly: without this, 'Downloads' as a search target could never
    actually resolve to the REAL Downloads folder — find_file_or_folder
    walks the CONTENTS of each known root, it never treats the root itself
    as a matchable entry — so it fell through to the slow drive-wide scan,
    which found an unrelated folder that happened to also be named
    'downloads' (a Steam workshop cache, several levels deep) and
    confidently auto-selected it as if it were uniquely correct.
    Also resolves 'the default folder' directly to XENORA_DEFAULT_OUTPUT_DIR
    — the same no-search-needed reasoning applies."""
    normalized = _normalize_file_target(target).strip().lower()
    if normalized in ("default", "default folder", "the default", "xenora folder", "xenora default"):
        os.makedirs(XENORA_DEFAULT_OUTPUT_DIR, exist_ok=True)
        return XENORA_DEFAULT_OUTPUT_DIR
    key = FILE_OP_FOLDER_ALIASES.get(normalized)
    if key:
        return FILE_OP_KNOWN_FOLDERS.get(key)
    return None

DEFAULT_FILE_SEARCH_CONFIG = {
    "priority_order": ["downloads", "documents", "desktop", "pictures", "videos", "music"],
    "category_shortcuts": {},
}

def load_file_search_config():
    if os.path.exists(FILE_SEARCH_CONFIG_FILE):
        try:
            with open(FILE_SEARCH_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("priority_order", list(DEFAULT_FILE_SEARCH_CONFIG["priority_order"]))
            cfg.setdefault("category_shortcuts", {})
            return cfg
        except Exception as e:
            dbg(f"[FILECONFIG] load error: {e}")
    return {"priority_order": list(DEFAULT_FILE_SEARCH_CONFIG["priority_order"]), "category_shortcuts": {}}

def save_file_search_config(cfg):
    try:
        with open(FILE_SEARCH_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        dbg(f"[FILECONFIG] save error: {e}")

_file_search_config = load_file_search_config()

def _ordered_search_dirs():
    """The 6 usual folders in the user's own priority order — reordering
    priority_order via conversation changes search order immediately, no
    code edit needed."""
    dirs = []
    for name in _file_search_config["priority_order"]:
        path = FILE_OP_KNOWN_FOLDERS.get(name)
        if path and path not in dirs:
            dirs.append(path)
    for path in FILE_OP_KNOWN_FOLDERS.values():
        if path not in dirs:
            dirs.append(path)
    return dirs

def _other_fixed_drives():
    import string
    drives = []
    for letter in string.ascii_uppercase:
        if letter == "C":
            continue
        root = f"{letter}:\\"
        if os.path.isdir(root):
            drives.append(root)
    return drives

FILE_OP_SKIP_DIR_NAMES = {
    "appdata", "node_modules", ".git", "$recycle.bin",
    "system volume information", "programdata",
}

FILE_OP_BLOCKED_ROOTS = [
    os.environ.get("WINDIR", r"C:\Windows"),
    os.environ.get("PROGRAMFILES", r"C:\Program Files"),
    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    os.path.dirname(os.path.abspath(__file__)),
]

_OWN_WORK_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "xenora_files")).lower()

def _is_blocked_path(path):
    try:
        resolved = os.path.realpath(path).lower()
    except Exception:
        return True
    if resolved == _OWN_WORK_DIR or resolved.startswith(_OWN_WORK_DIR + os.sep):
        return False
    for root in FILE_OP_BLOCKED_ROOTS:
        if not root:
            continue
        r = os.path.realpath(root).lower()
        if resolved == r or resolved.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False

def _should_skip_dir(dir_path):
    if os.path.basename(dir_path).lower() in FILE_OP_SKIP_DIR_NAMES:
        return True
    return _is_blocked_path(dir_path)

def _tokenize(s):
    """Lowercase alphanumeric words only — punctuation/spacing-independent,
    so '25bca20012 assignment pdf' matches '25BCA20012 - Assignment.pdf'
    (previously required as one exact contiguous substring, which the ' - '
    broke — the real cause both of 'not found' AND of the slow whole-drive
    fallback firing for a file that was sitting right in Downloads)."""
    return set(re.findall(r'[a-z0-9]+', s.lower()))

_file_op_state = "idle"
_file_op_cancel_event = threading.Event()

def _set_file_op_state(state):
    global _file_op_state
    _file_op_state = state
    ui_state["file_op_state"] = state
    push_to_ui("file_op_state", {"state": state})

def _walk_for_match(base, needle_words, max_depth, seen_real, matches):
    if not os.path.isdir(base):
        return
    for root, dirs, files in os.walk(base):
        if _file_op_cancel_event.is_set():
            return
        dirs[:] = [d for d in dirs if not _should_skip_dir(os.path.join(root, d))]
        for entry in dirs + files:
            if needle_words and needle_words <= _tokenize(entry):
                full = os.path.join(root, entry)
                try:
                    real = os.path.realpath(full).lower()
                except Exception:
                    real = full.lower()
                if real not in seen_real:
                    seen_real.add(real)
                    matches.append(full)
        if root.count(os.sep) - base.count(os.sep) >= max_depth:
            dirs[:] = []

LEARNED_FILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_files.json")
_learned_files = {}

def load_learned_files():
    global _learned_files
    try:
        if os.path.exists(LEARNED_FILES_FILE):
            with open(LEARNED_FILES_FILE, "r", encoding="utf-8") as f:
                _learned_files = json.load(f)
    except Exception as e:
        dbg(f"[FILES] learned-files load error: {e}")
        _learned_files = {}

def save_learned_files():
    try:
        with open(LEARNED_FILES_FILE, "w", encoding="utf-8") as f:
            json.dump(_learned_files, f, indent=2)
    except Exception as e:
        dbg(f"[FILES] learned-files save error: {e}")

load_learned_files()

def _check_learned_cache(needle):
    if needle in _learned_files and os.path.exists(_learned_files[needle]):
        return _learned_files[needle]
    needle_words = _tokenize(needle)
    if not needle_words:
        return None
    for key, path in _learned_files.items():
        if needle_words <= _tokenize(key) and os.path.exists(path):
            return path
    return None

def _intermediate_context_words(path):
    """Words from folder names BETWEEN the search root and this match,
    excluding the match's own name — context this location's path silently
    assumes that the user never actually mentioned. Same root-matching
    logic as _relative_search_label."""
    try:
        for base in list(FILE_OP_KNOWN_FOLDERS.values()) + _other_fixed_drives():
            base_real = os.path.realpath(base).lower()
            if os.path.realpath(path).lower().startswith(base_real):
                rel_dir = os.path.relpath(os.path.dirname(path), base)
                return _tokenize(rel_dir) if rel_dir != "." else set()
    except Exception:
        pass
    return set()

def _rank_and_filter_matches(needle_words, matches):
    """Deterministic ranking, not another LLM round-trip. Two dimensions:
    (a) does the ENTRY'S OWN NAME carry extra unrelated words beyond what
    was asked — filters out 'com.unity.meta-instant-games-sdk-1.3.0.tgz'
    matching 'games' purely as a buried substring of a much longer,
    unrelated compound name; (b) does the match's LOCATION silently assume
    context the user never mentioned — 'D:\\Steam\\steam\\games' sits inside
    a folder named 'Steam' that was never said, while 'D:\\Games' sits
    directly at the drive root and assumes nothing extra. Confirmed
    directly: asked for 'games folder in D drive' with no mention of Steam
    — the top-level match should win outright, not be lumped into a
    disambiguation prompt alongside a nested nested nested Steam-specific
    one that requires context never given. If the user DOES name that
    context ('games folder in steam folder'), needle_words includes
    'steam' too, so the nested match's context is no longer 'unmentioned'
    and it competes fairly. Only genuinely close candidates (same score on
    both dimensions) get surfaced for disambiguation — e.g. 'gamez' vs
    'games', both exact single-word names with zero unmentioned context."""
    if len(matches) <= 1:
        return matches
    scored = []
    for m in matches:
        entry_words = _tokenize(os.path.basename(m))
        extra = len(entry_words - needle_words)
        exact = 0 if entry_words == needle_words else 1
        context_words = _intermediate_context_words(m)
        unmentioned_context = len(context_words - needle_words)
        scored.append((exact, extra, unmentioned_context, m))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    best = scored[0]
    if best[0] == 0 and (len(scored) == 1 or scored[1][:3] > best[:3]):
        return [best[3]]
    top_tier = [s for s in scored if s[:3] == best[:3]]
    return [s[3] for s in top_tier]

def find_file_or_folder(name):
    """Resolution order:
    0. Silently-learned cache — a target resolved before, if that path
       still exists on disk. Skips searching entirely on repeat requests.
    1. Category shortcut ('games' -> D:\\Games, etc, set via conversation) —
       returned DIRECTLY with no search at all. The whole point of a
       shortcut is skipping the scan entirely.
    2. Bare reference to a known category folder ('Downloads', 'my
       documents', 'photos') — resolved DIRECTLY to the real OS folder, no
       search. This is what step 3 below could never do on its own (see
       _resolve_known_folder_target's docstring).
    3. The 6 usual folders, in the user's own priority order (fast).
    4. Every OTHER fixed drive (D:, E:, etc) — fully scanned, ONLY if step 3
       finds nothing. No broader C: scan happens at all: C: coverage stays
       limited to the 6 known folders above, which already cover 'the main
       common folders on C:'. Pruned against FILE_OP_BLOCKED_ROOTS and
       FILE_OP_SKIP_DIR_NAMES so it never wastes time inside Windows/
       Program Files/AppData/node_modules/.git.
    Results are ranked/filtered (see _rank_and_filter_matches) before
    returning — callers get either one confident match or a short list of
    genuinely close candidates, never a dump of every loose token match.
    """
    needle = name.lower().strip()
    if not needle:
        return []

    cached = _check_learned_cache(needle)
    if cached:
        return [cached]

    for key, path in _file_search_config.get("category_shortcuts", {}).items():
        if key in needle or needle in key:
            return [path] if os.path.isdir(path) else []

    known_folder = _resolve_known_folder_target(needle)
    if known_folder and os.path.isdir(known_folder):
        return [known_folder]

    needle_words = _tokenize(needle)
    seen_real, matches = set(), []
    _file_op_cancel_event.clear()
    _set_file_op_state("searching")
    try:
        for base in _ordered_search_dirs():
            if _file_op_cancel_event.is_set():
                return matches
            _walk_for_match(base, needle_words, max_depth=3, seen_real=seen_real, matches=matches)
        if matches:
            return _rank_and_filter_matches(needle_words, matches)

        for base in _other_fixed_drives():
            if _file_op_cancel_event.is_set():
                return matches
            _walk_for_match(base, needle_words, max_depth=12, seen_real=seen_real, matches=matches)
        return _rank_and_filter_matches(needle_words, matches)
    finally:
        _set_file_op_state("idle")

def list_folder_contents(path):
    try:
        return os.listdir(path)
    except Exception as e:
        dbg(f"[FILEOP] list error: {e}")
        return None

_pending_file_op = None

def _normalize_file_target(name):
    """The router sometimes folds a generic descriptive word into the target
    string itself — e.g. target='assets folder' instead of just 'assets',
    for input like 'open assets folder'. Relying purely on prompt wording to
    prevent this is fragile (confirmed directly: it happened even with an
    otherwise-correct classification), so strip it deterministically here,
    same reasoning as the open_app/'folder' misclassification fix getting a
    code-level safety net rather than depending on the LLM alone."""
    n = name.strip()
    n = re.sub(r'^(the)\s+', '', n, flags=re.IGNORECASE)
    n = re.sub(r'\s+(folder|file|directory)$', '', n, flags=re.IGNORECASE)
    return n.strip()

_DRIVE_TARGET_RE = re.compile(r'^([a-z])(?:\s*:|\s+drive)?$', re.IGNORECASE)

def _resolve_drive_target(target):
    """'D drive' / 'D:' / bare 'D' referring to an entire drive needs NO
    search at all — just open the drive root directly. Confirmed directly:
    running this through the normal fuzzy file search treated the single
    letter 'd' as a search token, which (via the tokenized subset-match
    rule) matched essentially any file with a 'd' anywhere in its own name
    — a random '1.1-D.jpg' — instead of recognizing 'D drive' as a request
    to open the drive itself. Returns None (falls through to normal search)
    for anything that isn't unambiguously a bare drive reference — a real
    named folder that happens to start with a drive-like word should never
    be swallowed by this."""
    m = _DRIVE_TARGET_RE.match(target.strip())
    if not m:
        return None
    root = f"{m.group(1).upper()}:\\"
    return root if os.path.isdir(root) else None

def _relative_search_label(path):
    """Human-readable disambiguation label showing WHICH folder (and how
    deep) a match came from — 'assets; Assets; assets' with no path context
    is impossible to tell apart or refer back to in a follow-up."""
    try:
        for base in list(FILE_OP_KNOWN_FOLDERS.values()) + _other_fixed_drives():
            if os.path.realpath(path).lower().startswith(os.path.realpath(base).lower()):
                rel = os.path.relpath(path, base)
                return os.path.basename(base.rstrip("\\/")) if rel == "." else \
                    os.path.join(os.path.basename(base.rstrip("\\/")) or base, rel)
    except Exception:
        pass
    return path

def _format_candidate_list(matches):
    """A real numbered list, one per line — pairs directly with the ordinal
    selection already supported in check_file_disambig_reply ('open the
    first one' / 'option 2'), so the number shown here is exactly what the
    user can refer back to. Relies on the frontend's white-space:pre-wrap
    fix (added earlier) to actually render the newlines instead of
    collapsing them into a run-on wall of semicolons."""
    return "\n".join(f"{i+1}. {_relative_search_label(m)}" for i, m in enumerate(matches[:6]))

_pending_file_disambig = None

def _execute_file_op_on_path(action_item, path, user_input):
    """The 'given exactly one resolved path, do the action' half of file_op
    — split out so both the direct single-match case AND a disambiguation
    follow-up that's narrowed to one candidate can share it."""
    global _pending_file_op
    action      = (action_item.get("file_action") or "").lower()
    new_name    = (action_item.get("new_name") or "").strip()
    destination = _normalize_file_target((action_item.get("destination") or "").strip())

    if _is_blocked_path(path):
        msg = "That's inside a protected system or program folder — I won't touch it."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True

    if action == "edit":
        return _edit_file(path, (action_item.get("content_description") or user_input or "").strip())

    if action == "open":
        try:
            os.startfile(path)
            msg = f"Opening {os.path.basename(path)}."
            add_chat_message("assistant", msg); speak(msg, style="chat")
        except Exception as e:
            dbg(f"[FILEOP] open error: {e}")
            msg = f"Couldn't open that: {e}"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True

    if action == "list":
        entries = list_folder_contents(path)
        if entries is None:
            msg = f"{os.path.basename(path)} isn't a folder I can list."
        elif not entries:
            msg = f"{os.path.basename(path)} is empty."
        else:
            shown = ", ".join(entries[:15])
            more = f" and {len(entries) - 15} more" if len(entries) > 15 else ""
            msg = f"{os.path.basename(path)} contains: {shown}{more}."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    if action == "rename":
        if not new_name:
            msg = "What should I rename it to?"
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return True
        try:
            new_path = os.path.join(os.path.dirname(path), new_name)
            os.rename(path, new_path)
            msg = f"Renamed to {new_name}."
            add_chat_message("assistant", msg); speak(msg, style="chat")
            _log_action(f"Renamed {os.path.basename(path)} to {new_name}.")
        except Exception as e:
            dbg(f"[FILEOP] rename error: {e}")
            msg = f"Couldn't rename that: {e}"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True

    if action == "delete":
        if not SEND2TRASH_AVAILABLE:
            msg = "send2trash isn't installed, so I can't safely delete files yet — run: pip install Send2Trash"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        _pending_file_op = {"action": "delete", "path": path}
        msg = f"Delete {os.path.basename(path)}? This moves it to the Recycle Bin, not permanent. (yes/no)"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    if action == "move":
        if not destination:
            msg = "Where should I move it to?"
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return True
        dest_matches = ([destination] if os.path.isdir(destination) else []) if os.path.isabs(destination) else find_file_or_folder(destination)
        dest_matches = [d for d in dest_matches if os.path.isdir(d)]
        if not dest_matches:
            msg = f"I couldn't find a destination folder matching \"{destination}\"."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        if _is_blocked_path(dest_matches[0]):
            msg = "That destination is inside a protected system or program folder — I won't move anything there."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        _pending_file_op = {"action": "move", "path": path, "destination": dest_matches[0]}
        msg = f"Move {os.path.basename(path)} to {os.path.basename(dest_matches[0])}? (yes/no)"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    return False

def _dispatch_resolved_file_action(action_item, path, user_input):
    """Once a target has resolved to exactly one path (whether immediately
    or after a disambiguation follow-up), route to the right single-path
    executor based on which intent asked for it — file_op (open/list/
    rename/move/delete) or read_file (Layer 2: extract & answer/summarize).
    This is what lets BOTH intents share the same disambiguation flow
    instead of duplicating it. Also silently caches the resolution (see
    _check_learned_cache) — no announcement, per explicit request; a repeat
    request for the same file skips searching entirely from here on."""
    target = _normalize_file_target((action_item.get("target") or "").strip()).lower()
    if target and not os.path.isabs(target):
        _learned_files[target] = path
        save_learned_files()
    if action_item.get("intent") == "read_file":
        return _read_and_answer(action_item, path, user_input)
    return _execute_file_op_on_path(action_item, path, user_input)

def handle_file_op(action_item, user_input):
    global _pending_file_disambig
    dbg(f"[FILEOP] action_item={action_item}")
    action = (action_item.get("file_action") or "").lower()
    target = _normalize_file_target((action_item.get("target") or "").strip())
    if not target:
        return False
    if action not in ("open", "list", "rename", "move", "delete", "edit"):
        dbg(f"[FILEOP] unrecognized file_action: {action!r} — asking for clarification instead of dropping silently")
        msg = f"What would you like me to do with \"{target}\" — open, list, rename, move, delete, or edit it?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    drive_root = _resolve_drive_target(target)
    matches = [drive_root] if drive_root else \
        (([target] if os.path.exists(target) else []) if os.path.isabs(target) else find_file_or_folder(target))
    if not matches:
        if _file_op_cancel_event.is_set():
            return True
        msg = f"I couldn't find anything matching \"{target}\" in your usual folders."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    if len(matches) > 1:
        _pending_file_disambig = {"matches": matches, "action_item": action_item}
        msg = f"Found more than one match for \"{target}\":\n{_format_candidate_list(matches)}\nWhich one?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    return _dispatch_resolved_file_action(action_item, matches[0], user_input)

_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
    "sixth": 5, "6th": 5, "six": 5,
}

def _parse_ordinal_selection(t):
    """'open the first one' / 'option 2' / 'number 3' / bare '2' — a
    reference to POSITION in the list Xenora just showed, distinct from
    matching words against the actual file path. Checked before the
    substring matcher since it's unambiguous when it hits."""
    m = re.search(r'\b(?:option|number|choice)\s*#?\s*(\d+)\b', t)
    if m:
        return int(m.group(1)) - 1
    m = re.match(r'^\s*#?(\d+)\s*$', t.strip())
    if m:
        return int(m.group(1)) - 1
    best_pos, best_idx = -1, None
    for word, idx in _ORDINAL_WORDS.items():
        m2 = re.search(rf'\b{re.escape(word)}\b', t)
        if m2 and m2.start() > best_pos:
            best_pos, best_idx = m2.start(), idx
    return best_idx

def check_file_disambig_reply(user_input):
    """Mirrors check_power_override()/check_file_op_confirmation()'s
    pattern — a pending ambiguous-match set takes priority on the very next
    message, matched against the SAME stored candidates instead of starting
    a brand new blind search."""
    global _pending_file_disambig
    if not _pending_file_disambig:
        return False
    t = user_input.strip().lower()
    if t in ("never mind", "nevermind", "cancel", "forget it"):
        _pending_file_disambig = None
        msg = "No problem."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    candidates = _pending_file_disambig["matches"]
    shown = candidates[:6]

    ordinal_idx = _parse_ordinal_selection(t)
    if ordinal_idx is not None and 0 <= ordinal_idx < len(shown):
        action_item = _pending_file_disambig["action_item"]
        chosen = shown[ordinal_idx]
        _pending_file_disambig = None
        return _dispatch_resolved_file_action(action_item, chosen, user_input)

    words = [w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2]
    narrowed = [m for m in candidates if any(w in m.lower() for w in words)] if words else []

    if 0 < len(narrowed) < len(candidates):
        if len(narrowed) == 1:
            action_item = _pending_file_disambig["action_item"]
            chosen = narrowed[0]
            _pending_file_disambig = None
            return _dispatch_resolved_file_action(action_item, chosen, user_input)
        _pending_file_disambig["matches"] = narrowed
        msg = f"Still more than one:\n{_format_candidate_list(narrowed)}\nNarrow it down further?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    _pending_file_disambig = None
    return False
    return True

READ_FILE_MAX_CHARS = 15000

def _extract_pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "pypdf isn't installed — run: pip install pypdf"
    try:
        reader = PdfReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text[:READ_FILE_MAX_CHARS], None
    except Exception as e:
        dbg(f"[READFILE] pdf error: {e}")
        return None, f"Couldn't read that PDF: {e}"

def _extract_docx_text(path):
    try:
        import docx
    except ImportError:
        return None, "python-docx isn't installed — run: pip install python-docx"
    try:
        doc = docx.Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text[:READ_FILE_MAX_CHARS], None
    except Exception as e:
        dbg(f"[READFILE] docx error: {e}")
        return None, f"Couldn't read that document: {e}"

def _extract_xlsx_text(path):
    try:
        import openpyxl
    except ImportError:
        return None, "openpyxl isn't installed — run: pip install openpyxl"
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"--- Sheet: {ws.title} ---")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    lines.append(", ".join("" if c is None else str(c) for c in row))
                if sum(len(l) for l in lines) > READ_FILE_MAX_CHARS:
                    break
        return "\n".join(lines)[:READ_FILE_MAX_CHARS], None
    except Exception as e:
        dbg(f"[READFILE] xlsx error: {e}")
        return None, f"Couldn't read that spreadsheet: {e}"

def _extract_plain_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:READ_FILE_MAX_CHARS], None
    except Exception as e:
        dbg(f"[READFILE] plain text error: {e}")
        return None, f"Couldn't read that file: {e}"

def extract_file_content(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf_text(path)
    if ext == ".docx":
        return _extract_docx_text(path)
    if ext in (".xlsx", ".xlsm"):
        return _extract_xlsx_text(path)
    if ext in (".csv", ".txt", ".md", ".json", ".log"):
        return _extract_plain_text(path)
    return None, f"I don't know how to read {ext or 'that type of'} files yet."

def _read_and_answer(action_item, path, user_input):
    _set_file_op_state("reading")
    try:
        content, err = extract_file_content(path)
        if err:
            add_chat_message("assistant", err); speak(err, style="empathetic")
            return True
        if not content or not content.strip():
            msg = f"{os.path.basename(path)} appears to be empty, or I couldn't extract any text from it."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True

        question = (action_item.get("target_question") or "").strip()
        request = question if question else f"Summarize this document ({os.path.basename(path)}) concisely."
        try:
            reply = llm(
                "You are reading the contents of a file the user asked about. Answer their question or "
                "summarize it based ONLY on the content given below — never invent anything not present. "
                "If the content doesn't actually contain what they're asking about, say so plainly. Keep "
                "it concise and conversational, matching your usual voice.",
                f"File: {os.path.basename(path)}\n\nContent:\n{content}\n\nRequest: {request}"
            )
        except Exception as e:
            dbg(f"[READFILE] llm error: {e}")
            msg = "Something went wrong reading through that — try again in a moment."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True

        cleaned = clean_response(reply)
        chat_history.append({'role': 'user', 'content': f"[Read file] {os.path.basename(path)}: {request}"})
        chat_history.append({'role': 'assistant', 'content': cleaned})
        if len(chat_history) > 80:
            del chat_history[:-80]
        add_chat_message("assistant", cleaned, remember=False)
        speak_auto(cleaned)
        return True
    finally:
        _set_file_op_state("idle")

def handle_read_file(action_item, user_input):
    global _pending_file_disambig
    dbg(f"[READFILE] action_item={action_item}")
    target = _normalize_file_target((action_item.get("target") or "").strip())
    if not target:
        return False

    matches = ([target] if os.path.exists(target) else []) if os.path.isabs(target) else find_file_or_folder(target)
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        if _file_op_cancel_event.is_set():
            return True
        msg = f"I couldn't find anything matching \"{target}\" in your usual folders."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    if len(matches) > 1:
        _pending_file_disambig = {"matches": matches, "action_item": action_item}
        msg = f"Found more than one match for \"{target}\":\n{_format_candidate_list(matches)}\nWhich one?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    return _read_and_answer(action_item, matches[0], user_input)

UPLOADS_DIR = os.path.join(XENORA_DEFAULT_OUTPUT_DIR, "Xenora Uploads")
GENERATED_IMAGES_DIR = os.path.join(XENORA_DEFAULT_OUTPUT_DIR, "Xenora Generated Images")
EDIT_MAX_CHARS = 30000
EDIT_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
                  ".xml", ".yaml", ".yml", ".ini", ".log", ".sql", ".java", ".c", ".cpp", ".h", ".cs",
                  ".sh", ".bat", ".ps1", ".toml", ".cfg", ".env"}
_openable_paths = set()
_recent_uploads = []

def _register_openable(path):
    _openable_paths.add(os.path.realpath(path))

def _wants_in_place(instruction):
    return bool(re.search(r"\b(overwrite|in[- ]place|replace the original|same file|save over)\b", instruction or "", re.I))

def _strip_code_fences(text):
    t = text.strip()
    m = re.match(r"^```[\w+-]*\n(.*?)\n?```$", t, re.S)
    return m.group(1) if m else t

def _edit_output_path(path, in_place):
    if in_place:
        bak = f"{path}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
        return path
    base, ext = os.path.splitext(path)
    return _unique_save_path(os.path.dirname(path), f"{os.path.basename(base)} (edited){ext}")

def _edit_json(system, user):
    raw = llm(system, user, temperature=0.2)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("the model didn't return edits I could apply")
    return json.loads(m.group(0))

def _extract_plain_text_full(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            return f.read(), None
    except Exception as e:
        return None, f"Couldn't read that file: {e}"

def _edit_text_file(path, instruction, out):
    content, err = _extract_plain_text_full(path)
    if err:
        return err
    if len(content) > EDIT_MAX_CHARS:
        return f"That file is too large for me to rewrite safely (over {EDIT_MAX_CHARS:,} characters)."
    new = llm("You edit files. Apply the user's requested change to the file below and return ONLY the complete "
              "updated file content — no explanation, no markdown fences. Change nothing that wasn't asked for; "
              "keep the file's existing formatting, indentation and line endings style.",
              f"File: {os.path.basename(path)}\n\nRequested change: {instruction}\n\nFile content:\n{content}",
              temperature=0.2)
    new = _strip_code_fences(new)
    if not new.strip():
        return "The edit came back empty, so I left everything alone."
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return None

def _edit_docx(path, instruction, out):
    try:
        import docx
    except ImportError:
        return "python-docx isn't installed — run: pip install python-docx"
    d = docx.Document(path)
    items = {}
    for i, para in enumerate(d.paragraphs):
        if para.text.strip():
            items[f"p{i}"] = para
    for ti, table in enumerate(d.tables):
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                for pi, para in enumerate(cell.paragraphs):
                    if para.text.strip():
                        items[f"t{ti}.{ri}.{ci}.{pi}"] = para
    listing = "\n".join(f"[{k}] {v.text}" for k, v in items.items())
    if len(listing) > EDIT_MAX_CHARS:
        return f"That document is too long for me to edit safely (over {EDIT_MAX_CHARS:,} characters of text)."
    plan = _edit_json(
        'You edit Word documents. Reply with ONLY JSON: {"edits":[{"id":"p3","text":"the full new text of that paragraph"}], '
        '"append":["new paragraph to add at the end", ...]}. Only include paragraphs that must change; ids come from the '
        'brackets. Use "append" only if the user asked to add content. Nothing else.',
        f"Requested change: {instruction}\n\nParagraphs:\n{listing}")
    changed = 0
    for e in plan.get("edits", []):
        para = items.get(str(e.get("id")))
        if para is None or "text" not in e:
            continue
        runs = para.runs
        if runs:
            runs[0].text = str(e["text"])
            for r in runs[1:]:
                r.text = ""
        else:
            para.add_run(str(e["text"]))
        changed += 1
    for text in plan.get("append", []):
        d.add_paragraph(str(text)); changed += 1
    if not changed:
        return "I couldn't find anything in that document to change for that request, so I left it alone."
    d.save(out)
    return None

def _edit_xlsx(path, instruction, out):
    try:
        import openpyxl
    except ImportError:
        return "openpyxl isn't installed — run: pip install openpyxl"
    wb = openpyxl.load_workbook(path)
    cells, size = {}, 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    cells[f"{ws.title}!{c.coordinate}"] = c.value
                    size += len(str(c.value)) + 12
        if size > EDIT_MAX_CHARS:
            return f"That workbook is too large for me to edit safely (over {EDIT_MAX_CHARS:,} characters of cell content)."
    plan = _edit_json(
        'You edit Excel workbooks. Reply with ONLY JSON: {"edits":[{"cell":"Sheet1!B2","value":"new value or =FORMULA"}]}. '
        'Use the "Sheet!A1" keys shown. You may target empty cells to add data. Numbers as numbers, formulas as strings '
        'starting with "=". Only include cells that must change. Nothing else.',
        f"Requested change: {instruction}\n\nNon-empty cells:\n{json.dumps(cells, default=str)[:EDIT_MAX_CHARS]}")
    changed = 0
    for e in plan.get("edits", []):
        ref = str(e.get("cell", ""))
        if "!" not in ref:
            continue
        sheet, coord = ref.rsplit("!", 1)
        if sheet in wb.sheetnames:
            wb[sheet][coord.strip()] = e.get("value")
            changed += 1
    if not changed:
        return "I couldn't find any cells to change for that request, so I left the workbook alone."
    wb.save(out)
    return None

def _edit_file(path, instruction):
    """Always returns True (the request was handled, successfully or with an honest explanation)."""
    name = os.path.basename(path)
    ext  = os.path.splitext(path)[1].lower()
    if not instruction:
        msg = f"What would you like me to change in {name}?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    if ext in (".pdf", ".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".exe"):
        msg = (f"I can't edit PDFs in place. I can read {name} and write a fresh Word or text document with the "
               f"changes instead, if you'd like.") if ext == ".pdf" else f"I can't edit {ext} files."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    if ext not in EDIT_TEXT_EXTS and ext not in (".docx", ".xlsx", ".xlsm"):
        msg = f"I don't know how to edit {ext or 'that type of'} files yet — I can edit text, code, Word (.docx) and Excel (.xlsx) files."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    in_place = _wants_in_place(instruction)
    _set_file_op_state("writing")
    try:
        out = _edit_output_path(path, in_place)
        tmp = out + ".xenora-tmp" if in_place else out
        if ext == ".docx":               err = _edit_docx(path, instruction, tmp)
        elif ext in (".xlsx", ".xlsm"):  err = _edit_xlsx(path, instruction, tmp)
        else:                            err = _edit_text_file(path, instruction, tmp)
        if err:
            if tmp != out and os.path.exists(tmp): os.remove(tmp)
            add_chat_message("assistant", err); speak(err, style="empathetic")
            return True
        if tmp != out:
            os.replace(tmp, out)
        _register_openable(out)
        push_to_ui("file_ready", {"name": os.path.basename(out), "path": out, "action": "Edited"})
        msg = (f"Done — I updated {name} in place (the previous version is saved next to it as a .bak file)."
               if in_place else f"Done. I saved the edited version as {os.path.basename(out)} next to the original — your original is untouched.")
        chat_history.append({'role': 'user', 'content': f"[Edit file] {name}: {instruction}"})
        chat_history.append({'role': 'assistant', 'content': msg})
        add_chat_message("assistant", msg, remember=False); speak(msg, style="chat")
    except Exception as e:
        dbg(f"[EDITFILE] failed on {path}: {e}")
        msg = "I couldn't apply that edit cleanly, so I changed nothing. Try rephrasing the change, or make it smaller."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
    finally:
        _set_file_op_state("idle")
    return True

def handle_attachments(files, caption):
    """Files attached via paste / drag-drop / the paperclip. Saved to UPLOADS_DIR (never overwriting
    anything), then acted on: no caption -> ask what to do; caption asking for a change -> edit
    (single file); otherwise -> read and answer about them."""
    try:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        saved = []
        for f in files:
            name = _sanitize_filename(os.path.basename(f.get("name") or "attachment"))
            dest = _unique_save_path(UPLOADS_DIR, name)
            with open(dest, "wb") as out:
                out.write(base64.b64decode(f.get("data") or ""))
            saved.append(dest)
            _recent_uploads.append({"name": os.path.basename(dest), "path": dest, "ts": time.time()})
            _register_openable(dest)
            _bn = os.path.basename(dest).lower()
            _learned_files[_bn] = dest
            _learned_files[os.path.splitext(_bn)[0]] = dest
        save_learned_files()
        del _recent_uploads[:-20]
        names = ", ".join(os.path.basename(p) for p in saved)
        dbg(f"[UPLOAD] saved {len(saved)} file(s): {names}")
        chat_history.append({'role': 'user', 'content': f"[Attached {len(saved)} file(s): {names}] {caption}".strip()})

        if len(saved) == 1:
            ext = os.path.splitext(saved[0])[1].lower()
            if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
                kind = "audio" if ext in AUDIO_EXTS else "video"
                if not caption.strip():
                    msg = (f"Got {names}. Want me to transcribe it, summarize it, or answer something about it?"
                           if kind == "audio" else f"Got {names}. Want me to summarize it or answer something about it?")
                    add_chat_message("assistant", msg); speak(msg, style="chat")
                    return
                _set_file_op_state("reading")
                try:
                    reply = _analyze_audio(saved[0], caption) if kind == "audio" else _analyze_video(saved[0], caption)
                except Exception as e:
                    dbg(f"[{kind.upper()}] analysis failed on {saved[0]}: {e}")
                    reply = None
                finally:
                    _set_file_op_state("idle")
                if reply is None:
                    msg = f"I couldn't process that {kind} file — check the LOGS tab for the exact reason."
                    add_chat_message("assistant", msg); speak(msg, style="empathetic")
                    return
                chat_history.append({'role': 'assistant', 'content': reply})
                if len(chat_history) > 80:
                    del chat_history[:-80]
                add_chat_message("assistant", reply, remember=False)
                speak(reply, style="chat")
                return

        if not caption.strip():
            msg = f"Got {names}. Want me to summarize it, answer something about it, or make changes to it?"
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return
        intent = "read"
        if len(saved) == 1:
            verdict = llm("Classify the user's request about an attached file. Reply with exactly one word: EDIT if they want "
                          "the file's contents changed, fixed, rewritten, updated or added to; otherwise READ.", caption, temperature=0.0)
            if "EDIT" in verdict.upper():
                intent = "edit"
        if intent == "edit":
            _edit_file(saved[0], caption)
            return
        for path in saved:
            _read_and_answer({"intent": "read_file", "target_question": caption}, path, caption)
    except Exception as e:
        dbg(f"[UPLOAD] handler failed: {e}")
        msg = "Something went wrong with that attachment — try sending it again."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")

def _pollinations_generate_image(prompt):
    """Pollinations AI — free, unlimited, no key. Returns (image_bytes, mime_type).
    Uses GET https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true
    Tries models in order (turbo -> default -> flux -> flux-realism) with short backoff on
    transient 402/429/500. Without a model param Pollinations uses its default (flux), which
    is often available when named models are transiently throttled (402/500)."""
    if not POLLINATIONS_ENABLED:
        raise RuntimeError("Pollinations disabled")
    short = prompt.strip()[:700]
    seed = random.randint(0, 999999)
    model_order = (POLLINATIONS_IMAGE_MODEL, None, "flux", "flux-realism")
    for model in model_order:
        if model is None:
            url = f"https://image.pollinations.ai/prompt/{quote(short)}?width=1024&height=1024&nologo=true&seed={seed}"
        else:
            url = f"https://image.pollinations.ai/prompt/{quote(short)}?model={model}&width=1024&height=1024&nologo=true&enhance=false&seed={seed}"
        dbg(f"[IMAGEGEN] Pollinations try model={model or 'default'}: {url[:160]}...")
        for attempt in range(2):
            try:
                resp = requests.get(url, timeout=90)
            except Exception as e:
                dbg(f"[IMAGEGEN] Pollinations model={model or 'default'} attempt {attempt+1} failed: {e}")
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break
            if resp.status_code != 200:
                dbg(f"[IMAGEGEN] Pollinations model={model or 'default'} attempt {attempt+1} HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code in (402, 429, 500, 502, 503) and attempt == 0:
                    time.sleep(1.5)
                    continue
                if resp.status_code in (402, 429, 500, 502, 503):
                    break
                raise RuntimeError(f"Pollinations returned HTTP {resp.status_code}: {resp.text[:300]}")
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            if not ctype.startswith("image/"):
                body = resp.content[:500]
                if b"error" in body.lower() or b"rate limit" in body.lower():
                    dbg(f"[IMAGEGEN] Pollinations model={model or 'default'} error body: {body[:200]!r} — trying next")
                    break
                ctype = "image/jpeg"
            if len(resp.content) < 5000:
                dbg(f"[IMAGEGEN] Pollinations model={model or 'default'} too small ({len(resp.content)} bytes) — trying next")
                break
            dbg(f"[IMAGEGEN] Pollinations OK model={model or 'default'}: {len(resp.content)} bytes, {ctype}")
            return resp.content, ctype
        time.sleep(0.6)
    raise RuntimeError("Pollinations failed on all models (turbo/default/flux/flux-realism)")

def _gemini_generate_image(prompt):
    """Gemini fallback — same as before. Returns (image_bytes, mime_type)."""
    if not gemini_client:
        raise RuntimeError("Gemini not configured (no GEMINI_API_KEY)")
    if _cooldown_active(GEMINI_IMAGE_MODEL):
        raise RuntimeError("image generation is cooling down")
    try:
        response = gemini_client.models.generate_content(model=GEMINI_IMAGE_MODEL, contents=prompt)
    except Exception as e:
        _cooldown_set(GEMINI_IMAGE_MODEL, e)
        raise
    parts = (response.candidates[0].content.parts if response.candidates else []) or []
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob and blob.data:
            return blob.data, (blob.mime_type or "image/png")
    raise RuntimeError("no image came back — the model may have declined this prompt")

def _generate_image_unified(prompt):
    """Unified entry: Pollinations first (free/unlimited), Gemini as fallback.
    Never raises quota/429 on the primary path — Pollinations has no quota."""
    if POLLINATIONS_ENABLED:
        try:
            return _pollinations_generate_image(prompt), "pollinations"
        except Exception as e:
            dbg(f"[IMAGEGEN] Pollinations failed ({e}) — trying Gemini fallback")
    return _gemini_generate_image(prompt), "gemini"

def handle_generate_image(action_item, user_input):
    prompt = (action_item.get("content_description") or action_item.get("target") or user_input).strip()
    if not prompt:
        msg = "What would you like the image to show?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    _set_file_op_state("writing")
    try:
        (img_bytes, mime), provider = _generate_image_unified(prompt)
        dbg(f"[IMAGEGEN] success via {provider}: {len(img_bytes)} bytes {mime}")
    except Exception as e:
        dbg(f"[IMAGEGEN] failed (both providers): {e}")
        msg = ("I couldn't generate that image right now — both image providers failed. "
               "Check the LOGS tab for the exact reason.")
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    finally:
        _set_file_op_state("idle")
    ext = ".png" if "png" in mime else ".jpg"
    os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)
    fname = (_sanitize_filename(prompt[:40].strip()) or "image") + ext
    path = _unique_save_path(GENERATED_IMAGES_DIR, fname)
    with open(path, "wb") as f:
        f.write(img_bytes)
    _register_openable(path)
    data_url = f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"
    chat_history.append({'role': 'user', 'content': f"[Generate image] {prompt}"})
    chat_history.append({'role': 'assistant', 'content': f"[Generated image: {os.path.basename(path)}]"})
    if len(chat_history) > 80:
        del chat_history[:-80]
    _memory.setdefault("facts", []).append(f"[generated image] {os.path.basename(path)} based on prompt: {prompt}")
    _memory.setdefault("last_generated_image", {})["path"] = path
    _memory.setdefault("last_generated_image", {})["prompt"] = prompt
    _memory.setdefault("last_generated_image", {})["filename"] = os.path.basename(path)
    _memory.setdefault("last_generated_image", {})["mime"] = mime
    save_memory(_memory)
    push_to_ui("assistant_image", {"dataUrl": data_url, "path": path, "name": os.path.basename(path), "prompt": prompt})
    push_to_ui("file_ready", {"name": os.path.basename(path), "path": path, "action": "Generated"})
    speak(random.choice(["Here you go.", "Done — take a look.", "Made that for you."]), style="cheerful")
    return True


def _apply_image_modification(modification_prompt):
    """Apply a modification to the last generated image (e.g., 'make it red color').
    Currently supports basic color/attribute changes by regenerating with the modified prompt."""
    last_img = _memory.get("last_generated_image", {})
    original_path = last_img.get("path")
    original_prompt = last_img.get("prompt", "")
    if not original_path or not os.path.exists(original_path):
        msg = "I don't have a recently generated image to modify — please generate one first."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return False
    modified_prompt = f"{original_prompt}. {modification_prompt}"
    try:
        (img_bytes, mime), provider = _generate_image_unified(modified_prompt)
        dbg(f"[IMAGEGEN-MOD] success via {provider}: {len(img_bytes)} bytes {mime}")
    except Exception as e:
        dbg(f"[IMAGEGEN-MOD] failed: {e}")
        msg = "I couldn't apply that modification to the image — both image providers failed."
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return False
    ext = ".png" if "png" in mime else ".jpg"
    mod_fname = ("modified_" + os.path.basename(original_path)) + ext
    mod_path = os.path.join(GENERATED_IMAGES_DIR, mod_fname)
    with open(mod_path, "wb") as f:
        f.write(img_bytes)
    _memory.setdefault("facts", []).append(f"[modified image] {mod_fname} based on original: {original_prompt} with: {modification_prompt}")
    _memory.setdefault("last_generated_image", {})["path"] = mod_path
    _memory.setdefault("last_generated_image", {})["prompt"] = modified_prompt
    _memory.setdefault("last_generated_image", {})["filename"] = mod_fname
    _memory.setdefault("last_generated_image", {})["mime"] = mime
    save_memory(_memory)
    data_url = f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"
    push_to_ui("assistant_image", {"dataUrl": data_url, "path": mod_path, "name": mod_fname})
    push_to_ui("file_ready", {"name": mod_fname, "path": mod_path, "action": "Modified"})
    speak(random.choice(["Here you go with the modified image.", "Done — the image has been updated.", "Made that change for you."]), style="cheerful")
    return True


def _groq_transcribe(path):
    """Whisper via Groq — tried first for ANY audio (per your 'Whisper first' preference), a
    fast, dedicated speech-to-text pass on the same Groq key already in use. Raises on failure
    so the caller falls through to Gemini's native audio understanding, same cooldown-aware
    per-model retry pattern as _groq_chat."""
    if not groq_client:
        raise RuntimeError("Groq not configured")
    last_err = None
    for model in WHISPER_MODEL_CHAIN:
        if _cooldown_active(model):
            continue
        try:
            with open(path, "rb") as f:
                r = groq_client.audio.transcriptions.create(model=model, file=f)
            _set_last_model(f"Groq · {model}")
            return (r.text or "").strip()
        except Exception as e:
            dbg(f"[AUDIO] Whisper {model} failed: {e}")
            _cooldown_set(model, e)
            last_err = e
    raise last_err or RuntimeError("no Whisper models available")

def _gemini_media_part(path, mime_type):
    """A genai Part for a local media file — inline for anything under the safe 20MB
    threshold, uploaded through the Files API (polled until ACTIVE) otherwise, per Gemini's
    own documented split between the two input methods."""
    if os.path.getsize(path) <= GEMINI_INLINE_LIMIT:
        with open(path, "rb") as f:
            return genai_types.Part(inline_data=genai_types.Blob(data=f.read(), mime_type=mime_type))
    uploaded = gemini_client.files.upload(file=path)
    for _ in range(90):
        info = gemini_client.files.get(name=uploaded.name)
        if info.state.name == "ACTIVE":
            break
        if info.state.name == "FAILED":
            raise RuntimeError("Gemini couldn't process that file")
        time.sleep(2)
    else:
        raise RuntimeError("Gemini took too long to process that file")
    return genai_types.Part(file_data=genai_types.FileData(file_uri=uploaded.uri, mime_type=mime_type))

def _analyze_audio(path, question):
    """Whisper first (fast, dedicated) — if the person asked for something beyond a plain
    transcript, the transcript is hand to Xenora's own chat model to answer from. Falls to
    Gemini's native audio understanding (music, sound effects, non-speech audio Whisper can't
    describe) only if Whisper is unavailable or fails."""
    question = (question or "").strip()
    try:
        transcript = _groq_transcribe(path)
        if not question or re.search(r"\btranscri", question, re.I):
            return transcript
        return llm("Answer the user's question using this transcript of an audio file. If the "
                    "transcript doesn't contain the answer, say so honestly.",
                    f"Question: {question}\n\nTranscript:\n{transcript}", temperature=0.3)
    except Exception as e:
        dbg(f"[AUDIO] Whisper unavailable, falling back to Gemini: {e}")
    if not gemini_client:
        raise RuntimeError("no working audio provider (Whisper failed, Gemini not configured)")
    part = _gemini_media_part(path, mimetypes.guess_type(path)[0] or "audio/mpeg")
    prompt = question or ("Give a short, natural reaction to this audio, like you actually listened to it — "
                           "not a dry technical report.")
    response = gemini_client.models.generate_content(
        model=GEMINI_TEXT_MODEL_CHAIN[0], contents=[part, prompt],
        config=genai_types.GenerateContentConfig(system_instruction=XENORA_PREFIX))
    return (response.text or "").strip()

def _analyze_video(source, question, is_url=False):
    """Speaks in Xenora's own voice (XENORA_PREFIX), same as every other chat reply — this used
    to call Gemini with no persona at all, so even once a video was correctly routed here, the
    reply read like generic Gemini output. With no explicit question, the default is a genuine,
    in-character REACTION to what's in the video, not a dry factual summary — someone sharing
    'omg this is so good!' with a link wants her to have watched it and formed an opinion, not
    itemize its contents."""
    if not gemini_client:
        raise RuntimeError("Gemini not configured — video analysis needs it")
    part = (genai_types.Part(file_data=genai_types.FileData(file_uri=source, mime_type="video/mp4")) if is_url
            else _gemini_media_part(source, mimetypes.guess_type(source)[0] or "video/mp4"))
    prompt = question or ("Give a short, natural reaction to this video, like you actually watched it and "
                           "have a genuine opinion — not a dry itemized summary. If it's music, react to "
                           "the music itself.")
    response = gemini_client.models.generate_content(
        model=GEMINI_TEXT_MODEL_CHAIN[0], contents=[part, prompt],
        config=genai_types.GenerateContentConfig(system_instruction=XENORA_PREFIX))
    return (response.text or "").strip()

def _download_video_for_analysis(url):
    """Gemini's URL video input ONLY accepts YouTube links (confirmed in its own docs) — every
    other site (Reddit, Instagram, TikTok, X, a direct .mp4 link, ...) has to be downloaded first
    and analyzed as a local file, same as an upload. Reuses the yt-dlp already wired in for music/
    video downloads elsewhere in this file. Capped at 20 minutes / 200MB so a stray link can't
    fill the disk or blow well past what's reasonable to feed a vision-language model in one go;
    the caller deletes this temp file once analysis is done, whether it succeeded or not."""
    tmp_dir = tempfile.mkdtemp(prefix="xenora_analyze_")
    ydl_opts = {
        'format': 'mp4/best[ext=mp4]/best', 'outtmpl': os.path.join(tmp_dir, 'clip.%(ext)s'),
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'match_filter': yt_dlp.utils.match_filter_func('duration < 1200'),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        raise RuntimeError("download produced no file")
    if os.path.getsize(path) > 200 * 1024 * 1024:
        os.remove(path)
        raise RuntimeError("that video is too large to analyze safely (over 200MB)")
    return path

def _handle_analyze_media(action_item, user_input):
    url = (action_item.get("target") or "").strip()
    question = (action_item.get("target_question") or "").strip()
    if not url:
        return False
    is_youtube = bool(YOUTUBE_URL_RE.search(url))
    _set_file_op_state("reading")
    tmp_path, reply = None, None
    try:
        if is_youtube:
            reply = _analyze_video(url, question, is_url=True)
        else:
            tmp_path = _download_video_for_analysis(url)
            reply = _analyze_video(tmp_path, question, is_url=False)
    except Exception as e:
        dbg(f"[VIDEOANALYZE] failed on {url}: {e}")
    finally:
        _set_file_op_state("idle")
        if tmp_path:
            shutil.rmtree(os.path.dirname(tmp_path), ignore_errors=True)
    if reply is None:
        msg = ("I couldn't pull that video to look at it — it might be private, region-locked, or "
               "from a site I can't fetch from.")
        add_chat_message("assistant", msg); speak(msg, style="empathetic")
        return True
    chat_history.append({'role': 'user', 'content': user_input})
    chat_history.append({'role': 'assistant', 'content': reply})
    if len(chat_history) > 80:
        del chat_history[:-80]
    add_chat_message("assistant", reply, remember=False)
    speak(reply, style="chat")
    return True

def handle_file_config(action_item, user_input):
    """Lets Xenora's own search behavior (which folders to check first,
    and category shortcuts that skip searching entirely) be changed by
    talking to her — never by editing this code. Persisted to
    file_search_config.json (see load/save_file_search_config above)."""
    global _file_search_config
    config_action = (action_item.get("config_action") or "").lower()
    category      = (action_item.get("category") or "").strip().lower()
    path          = (action_item.get("path") or "").strip()
    priority_list = action_item.get("priority_list") or []

    if config_action == "show_config":
        order = ", ".join(n.capitalize() for n in _file_search_config["priority_order"])
        shortcuts = _file_search_config.get("category_shortcuts", {})
        shortcut_str = "; ".join(f"{k} -> {v}" for k, v in shortcuts.items()) or "none set"
        msg = f"Search priority: {order}. Shortcuts: {shortcut_str}."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    if config_action == "set_shortcut":
        if not category or not path:
            msg = "What category, and what folder path should it point to?"
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return True
        if not os.path.isdir(path):
            msg = f"\"{path}\" doesn't look like a real folder on this machine — double check the path?"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        if _is_blocked_path(path):
            msg = "That's inside a protected system or program folder — I won't set a shortcut there."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        _file_search_config.setdefault("category_shortcuts", {})[category] = path
        save_file_search_config(_file_search_config)
        msg = f"Got it — \"{category}\" now points straight to {path}, no searching needed."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        _log_action(f"Set file-search shortcut: {category} -> {path}.")
        return True

    if config_action == "set_priority":
        mentioned = [p.strip().lower() for p in priority_list if p.strip().lower() in FILE_OP_KNOWN_FOLDERS]
        if not mentioned:
            msg = "Which folder(s) should I check first? (Desktop, Documents, Downloads, Pictures, Videos, or Music)"
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return True
        remaining = [n for n in _file_search_config["priority_order"] if n not in mentioned]
        _file_search_config["priority_order"] = mentioned + remaining
        save_file_search_config(_file_search_config)
        order = ", ".join(n.capitalize() for n in _file_search_config["priority_order"])
        msg = f"Updated — I'll check in this order now: {order}."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        _log_action(f"Updated file-search priority order: {order}.")
        return True

    return False

def check_file_op_confirmation(user_input):
    """Mirrors check_power_override()'s pattern exactly — a pending
    destructive file op takes priority on the very next message, consuming
    a yes/no answer instead of letting it fall through to normal chat or
    (worse) getting silently misrouted as a new unrelated command."""
    global _pending_file_op
    if not _pending_file_op:
        return False
    t = user_input.strip().lower()
    op = _pending_file_op
    if t in ("yes", "y", "confirm", "do it", "go ahead", "yeah", "yep"):
        _pending_file_op = None
        try:
            if op["action"] == "delete":
                send2trash(op["path"])
                msg = f"Deleted {os.path.basename(op['path'])} — it's in the Recycle Bin if you need it back."
                _log_action(f"Deleted {os.path.basename(op['path'])} (sent to Recycle Bin).")
            elif op["action"] == "move":
                shutil.move(op["path"], op["destination"])
                msg = f"Moved {os.path.basename(op['path'])} to {os.path.basename(op['destination'])}."
                _log_action(f"Moved {os.path.basename(op['path'])} to {op['destination']}.")
            else:
                msg = "Nothing pending to confirm."
        except Exception as e:
            dbg(f"[FILEOP] confirm error: {e}")
            msg = f"Couldn't complete that: {e}"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    if t in ("no", "n", "cancel", "never mind", "nevermind"):
        _pending_file_op = None
        msg = "Cancelled."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    return False


WRITE_FILE_DEFAULT_DIR = XENORA_DEFAULT_OUTPUT_DIR
DOC_EXTENSIONS = {"docx": ".docx", "xlsx": ".xlsx", "pdf": ".pdf", "txt": ".txt"}

def _sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', name).strip()
    return name or "Untitled"

def _unique_save_path(directory, filename):
    """Never silently overwrites an existing file — appends (1), (2), etc
    on a collision, same 'never destructive by default' philosophy as
    Layer 1's delete/move confirmations."""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return candidate

def _generate_document_structure(doc_type, content_description):
    if doc_type == "xlsx":
        system = (
            "Generate spreadsheet content as JSON: "
            '{"sheet_name": "...", "headers": ["col1","col2",...], "rows": [["...","...",...],...]}. '
            "Keep it realistic and complete for what was asked — real data/values, not a placeholder "
            "or outline. ONLY valid JSON, no explanation."
        )
    else:
        system = (
            "Generate document content as JSON: "
            '{"title": "...", "sections": [{"heading": "...", "paragraphs": ["...", "..."]}, ...]}. '
            "Write real, complete, well-organized content for what was asked — not a placeholder or "
            "outline. ONLY valid JSON, no explanation."
        )
    raw = llm(system, content_description, temperature=0.6)
    cleaned = re.sub(r"^```[\w]*", "", raw.strip()).rstrip("`").strip()
    return json.loads(cleaned)

def _build_docx(structure, path):
    import docx
    doc = docx.Document()
    if structure.get("title"):
        doc.add_heading(structure["title"], level=0)
    for section in structure.get("sections", []):
        if section.get("heading"):
            doc.add_heading(section["heading"], level=min(max(section.get("level", 1), 1), 9))
        for para in section.get("paragraphs", []):
            doc.add_paragraph(para)
    doc.save(path)

def _build_xlsx(structure, path):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (structure.get("sheet_name") or "Sheet1")[:31]
    headers = structure.get("headers", [])
    if headers:
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
    for row in structure.get("rows", []):
        ws.append(row)
    wb.save(path)

_PDF_UNICODE_REPLACEMENTS = {
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "--",
    "\u2026": "...", "\u00a0": " ",
}

def _sanitize_pdf_text(text):
    for uni, ascii_eq in _PDF_UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_eq)
    return text.encode("latin-1", errors="ignore").decode("latin-1")

def _build_pdf(structure, path):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    if structure.get("title"):
        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 10, _sanitize_pdf_text(structure["title"]))
        pdf.ln(4)
    for section in structure.get("sections", []):
        if section.get("heading"):
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 8, _sanitize_pdf_text(section["heading"]))
            pdf.ln(2)
        pdf.set_font("Helvetica", "", 11)
        for para in section.get("paragraphs", []):
            pdf.multi_cell(0, 6, _sanitize_pdf_text(para))
            pdf.ln(2)
    pdf.output(path)

def _build_txt(structure, path):
    lines = []
    if structure.get("title"):
        lines += [structure["title"], "=" * len(structure["title"]), ""]
    for section in structure.get("sections", []):
        if section.get("heading"):
            lines += [section["heading"], "-" * len(section["heading"])]
        lines += section.get("paragraphs", [])
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

DOC_BUILDERS = {"docx": _build_docx, "xlsx": _build_xlsx, "pdf": _build_pdf, "txt": _build_txt}
DOC_MISSING_DEP_HINT = {"docx": "python-docx", "xlsx": "openpyxl", "pdf": "fpdf2"}

_pending_write_file = None

def check_pending_write_file(user_input):
    """Mirrors check_file_op_confirmation()/check_file_disambig_reply()'s
    pattern — a pending write_file request (missing format or content)
    takes priority on the very next message, feeding the answer back into
    the SAME action_item instead of letting it fall through as an unrelated
    fresh turn (which is exactly what silently swallowed 'about you' as
    plain conversation, with no file ever actually written)."""
    global _pending_write_file
    if not _pending_write_file:
        return False
    t = user_input.strip().lower()
    if t in ("never mind", "nevermind", "cancel", "forget it"):
        _pending_write_file = None
        msg = "No problem."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    action_item = dict(_pending_write_file["action_item"])
    if _pending_write_file["awaiting"] == "doc_type":
        type_map = {"word": "docx", "doc": "docx", "docx": "docx",
                    "excel": "xlsx", "spreadsheet": "xlsx", "xlsx": "xlsx",
                    "pdf": "pdf", "text": "txt", "txt": "txt", "plain text": "txt"}
        action_item["doc_type"] = next((v for k, v in type_map.items() if k in t), "")
    else:
        action_item["content_description"] = user_input.strip()
    _pending_write_file = None
    return handle_write_file(action_item, user_input)

def handle_write_file(action_item, user_input):
    global _pending_write_file
    doc_type = (action_item.get("doc_type") or "").lower().strip()
    content_description = (action_item.get("content_description") or "").strip()
    filename = (action_item.get("filename") or "").strip()
    save_location = _normalize_file_target((action_item.get("save_location") or "").strip())

    if doc_type not in DOC_BUILDERS:
        _pending_write_file = {"action_item": action_item, "awaiting": "doc_type"}
        msg = "What format should I save that as — Word, Excel, PDF, or plain text?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True
    if not content_description:
        _pending_write_file = {"action_item": action_item, "awaiting": "content_description"}
        msg = "What should the document actually contain?"
        add_chat_message("assistant", msg); speak(msg, style="chat")
        return True

    directory = WRITE_FILE_DEFAULT_DIR
    if save_location:
        loc_matches = [m for m in find_file_or_folder(save_location) if os.path.isdir(m)]
        if loc_matches:
            directory = loc_matches[0]
    os.makedirs(directory, exist_ok=True)

    _set_file_op_state("writing")
    try:
        try:
            structure = _generate_document_structure(doc_type, content_description)
        except Exception as e:
            dbg(f"[WRITEFILE] content generation error: {e}")
            msg = "Something went wrong drafting that content — try again in a moment."
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True

        base_name = _sanitize_filename(filename or structure.get("title") or structure.get("sheet_name") or "Document")
        if not base_name.lower().endswith(DOC_EXTENSIONS[doc_type]):
            base_name += DOC_EXTENSIONS[doc_type]
        path = _unique_save_path(directory, base_name)

        try:
            DOC_BUILDERS[doc_type](structure, path)
        except ImportError as e:
            dbg(f"[WRITEFILE] missing dependency: {e}")
            pkg = DOC_MISSING_DEP_HINT.get(doc_type, "")
            msg = f"I need a library that isn't installed yet — run: pip install {pkg}"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True
        except Exception as e:
            dbg(f"[WRITEFILE] build error: {e}")
            msg = f"Something went wrong creating that file: {e}"
            add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return True

        _learned_files[base_name.lower()] = path
        save_learned_files()

        chat_history.append({'role': 'assistant', 'content': f"[Wrote file] {path}"})
        if len(chat_history) > 80:
            del chat_history[:-80]
        msg = f"Created {os.path.basename(path)}."
        add_chat_message("assistant", msg, remember=False)
        speak(msg, style="cheerful")
        _log_action(f"Wrote {doc_type} file: {path}")
        return True
    finally:
        _set_file_op_state("idle")


INTENT_HANDLERS = {
    "screen_question": _handle_screen_question,
    "download":         _handle_download,
    "open_app":         _handle_open_app,
    "open_web":         _handle_open_web,
    "web_search":       _handle_web_search,
    "generate_image":   handle_generate_image,
    "analyze_media":    _handle_analyze_media,
    "file_op":          handle_file_op,
    "file_config":      handle_file_config,
    "read_file":        handle_read_file,
    "write_file":       handle_write_file,
}


def run_command(user_input, memory):
    global _conversation_active, _pending_app_location
    _new_turn()
    _conversation_active = True
    try:
        add_chat_message("user", user_input)
        if XI and not OVERCLOCK_MODE:
            try:
                if XI.handle_user_reply(user_input) or XI.command(user_input):
                    return
            except Exception as e:
                dbg(f"[XI] command layer error, continuing as normal chat: {e}")
        extract_and_save_facts(user_input, memory)
        memory["last_seen"] = datetime.now().isoformat()

        if OVERCLOCK_MODE:
            reply = ask_overclock(user_input, memory)
            add_chat_message("assistant", reply, remember=False, preserve_markdown=True)
            return

        if _pending_app_location:
            name = _pending_app_location["name"]
            lower_in = user_input.strip().lower()
            if lower_in in ("never mind", "nevermind", "cancel", "forget it", "skip"):
                _pending_app_location = None
                msg = f"No problem — trying a web search for {name} instead."
                add_chat_message("assistant", msg); speak(msg, style="chat")
                smart_open(f"open {name}")
                return
            candidate = user_input.strip().strip('"').strip("'")
            if os.path.exists(candidate):
                _learned_apps[name.lower()] = candidate
                save_learned_apps()
                _pending_app_location = None
                try:
                    os.startfile(candidate)
                    msg = f"Got it — opening {name}. I'll remember this path from now on."
                    add_chat_message("assistant", msg); speak(msg, style="cheerful")
                except Exception as e:
                    msg = f"Saved that path, but couldn't launch it just now: {e}"
                    add_chat_message("assistant", msg); speak(msg, style="empathetic")
            else:
                msg = "I couldn't find anything at that path — double check it and try again, or say \"never mind\"."
                add_chat_message("assistant", msg); speak(msg, style="empathetic")
            return

        if check_power_override(user_input):
            return

        if check_file_op_confirmation(user_input):
            return

        if check_file_disambig_reply(user_input):
            return

        if check_pending_write_file(user_input):
            return

        if check_stop_watch(user_input):
            return

        if detect_sleep_unsupported(user_input):
            msg = ("I can't reliably put this laptop to sleep — Windows reports success on this "
                   "hardware without ever actually suspending it, and every software path to force "
                   "it has failed. Closing the lid does work, since that goes through a different, "
                   "hardware-level path. I can still shut it down or restart it if you'd like.")
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return

        immediate_action = detect_immediate_power(user_input)
        if immediate_action:
            power_id = str(time.time())
            threading.Thread(target=run_power_countdown, args=(immediate_action, power_id, ""), daemon=True).start()
            return

        presence_info = detect_presence_query(user_input)
        if presence_info:
            since = datetime.fromtimestamp(presence_info["updated"]).strftime('%I:%M %p').lstrip('0')
            if presence_info["status"] == "online":
                msg = f"{presence_info['name']} is online — joined {presence_info['app']} at {since}."
            elif presence_info["status"] == "offline":
                msg = f"{presence_info['name']} is offline — left {presence_info['app']} at {since}."
            else:
                msg = f"I'm tracking {presence_info['name']} but haven't seen them join or leave yet."
            add_chat_message("assistant", msg); speak(msg, style="chat")
            return

        if is_trending_request(user_input):
            handle_trending(user_input)
            return

        last_img = _memory.get("last_generated_image", {})
        if last_img.get("path") and user_input.strip().lower().startswith(("make it", "change it", "modify it", "edit it")):
            if _apply_image_modification(user_input):
                return

        action_list = resolve_intent(user_input)
        dbg(f"[DISPATCH] resolve_intent classified: {action_list}")
        handled_anything = False

        if any(a.get("intent") == "defer" for a in action_list):
            if detect_compound_request(user_input):
                steps = plan_compound_request(user_input)
                if steps:
                    start_compound_plan(steps)
                    handled_anything = True
            if not handled_anything:
                defer_item = next((a for a in action_list if a.get("intent") == "defer"), {})
                kind = (defer_item.get("defer_kind") or "").strip().lower()
                parsed = None
                if kind in ("presence", "watch", "schedule"):
                    parsed = {
                        "kind": kind,
                        "open_app": defer_item.get("defer_open_app") or None,
                        "target_app": defer_item.get("defer_target_app") or "",
                        "friend_name": defer_item.get("friend_name") or "",
                        "condition": defer_item.get("defer_condition") or "",
                        "delay_seconds": defer_item.get("delay_seconds") or 0,
                        "action": defer_item.get("defer_action") or "notify",
                        "message": defer_item.get("defer_message") or "",
                    }
                    dbg(f"[DEFER] using router-supplied fields (no second parse call): {parsed}")
                else:
                    parsed = parse_deferred_request(user_input)
                if parsed:
                    if parsed.get("action") == "sleep":
                        msg = ("I can't reliably put this laptop to sleep in software — Windows lies "
                               "about success on this hardware. Closing the lid does work. I can still "
                               "schedule a shutdown or restart if that helps.")
                        add_chat_message("assistant", msg); speak(msg, style="chat")
                        handled_anything = True
                    else:
                        open_app = parsed.get("open_app")
                        if open_app:
                            open_app_by_name(open_app)
                            time.sleep(3)
                        start_deferred_action(parsed)
                        handled_anything = True
                else:
                    dbg(f"[DEFER] router said defer but parse_deferred_request found no structure: {user_input!r}")
                    msg = ("I couldn't work out exactly what to watch for there — tell me who or what "
                           "to watch, and in which app.")
                    add_chat_message("assistant", msg); speak(msg, style="chat")
                    handled_anything = True

        for action_item in action_list:
            intent = action_item.get("intent", "chat")
            if intent in ("chat", "defer"):
                continue

            confidence = action_item.get("confidence", "high")
            clarify    = action_item.get("clarify_question")
            if confidence == "low" and clarify:
                add_chat_message("assistant", clarify); speak(clarify, style="chat")
                handled_anything = True
                continue

            handler = INTENT_HANDLERS.get(intent)
            if not handler:
                continue
            try:
                if handler(action_item, user_input):
                    handled_anything = True
            except Exception as e:
                dbg(f"[DISPATCH] handler for intent={intent!r} crashed: {e}")
                continue

        if handled_anything:
            return

        reply   = ask_xenora(user_input, memory)
        actions = extract_actions_from_reply(reply)

        if actions:
            valid   = [a.strip().lower() for a in actions if a.strip().lower() in ACTION_MAP]
            unknown = [a.strip().lower() for a in actions if a.strip().lower() not in ACTION_MAP]
            if valid:
                dynamic_app_response(valid, user_input)
            for u in unknown:
                msg = "No local shortcut for that. Checking the web."
                add_chat_message("assistant", msg)
                speak(msg, style="chat")
                smart_open(u)
            return

        add_chat_message("assistant", reply, remember=False)
        speak_auto(reply)

    except Exception as e:
        dbg(f"[RUN_COMMAND] unhandled exception: {e}")
        try:
            add_chat_message("assistant", "Something went wrong handling that — check the console for details.")
        except Exception:
            pass
    finally:
        _conversation_active = False


@flask_app.route("/api/state", methods=["GET"])
def get_state():
    safe = {k: v for k, v in ui_state.items() if k != "last_screenshot"}
    return jsonify(safe)

@flask_app.route("/api/chat", methods=["POST"])
def api_chat():
    global _user_typing
    data = request.json or {}
    text = data.get("message", "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    _user_typing = True
    def _run():
        global _user_typing
        run_command(text, _memory)
        _user_typing = False
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "processing"})

@flask_app.route("/api/chat_image", methods=["POST"])
def api_chat_image():
    data = request.json or {}
    images  = data.get("images") or ([data["image"]] if data.get("image") else [])
    caption = (data.get("message") or "").strip()
    if not images:
        return jsonify({"error": "no image"}), 400
    threading.Thread(target=handle_uploaded_image, args=(images, caption), daemon=True).start()
    return jsonify({"status": "processing"})

@flask_app.route("/api/chat_files", methods=["POST"])
def api_chat_files():
    data    = request.json or {}
    files   = [f for f in (data.get("files") or []) if f.get("data")]
    caption = (data.get("message") or "").strip()
    if not files:
        return jsonify({"error": "no files"}), 400
    threading.Thread(target=handle_attachments, args=(files, caption), daemon=True).start()
    return jsonify({"status": "processing"})

@flask_app.route("/api/power/cancel", methods=["POST"])
def api_cancel_power():
    if _pending_power:
        _pending_power["cancel_event"].set()
        msg = "Cancelled."
        add_chat_message("assistant", msg); speak(msg, style="chat")
        send_notification("Xenora", "Countdown cancelled.")
        return jsonify({"cancelled": True})
    return jsonify({"cancelled": False})

@flask_app.route("/api/file_op/stop", methods=["POST"])
def api_stop_file_op():
    """Stops an in-progress SEARCH (the genuinely slow, cooperatively-
    cancellable part of file_op/read_file) without touching active
    watch-tasks, schedules, or the power countdown — a fully separate
    interrupt from those. Does NOT abort an in-flight LLM call (the actual
    reading/summarizing step) — safely cancelling a live network call needs
    its own plumbing, not yet built."""
    if _file_op_state == "idle":
        return jsonify({"stopped": False})
    _file_op_cancel_event.set()
    msg = "Stopped."
    add_chat_message("assistant", msg); speak(msg, style="chat")
    return jsonify({"stopped": True})

@flask_app.route("/api/tasks", methods=["GET"])
def api_get_tasks():
    """Snapshot of everything currently running (watches + schedules), for
    the Active Tasks panel to hydrate on load — task_update is only pushed
    on change, so a freshly opened/reloaded UI needs a pull-based fetch too."""
    return jsonify(_all_task_items())

@flask_app.route("/api/xi/action", methods=["POST"])
def api_xi_action():
    """UI buttons for the automation system: run | pause | resume | cancel | keep | dismiss."""
    data = request.json or {}
    if not XI:
        return jsonify({"ok": False, "error": "automations are not running"}), 503
    return jsonify(XI.ui_action(str(data.get("tid") or ""), str(data.get("action") or "")))

@flask_app.route("/api/xi/state", methods=["GET"])
def api_xi_state():
    if not XI:
        return jsonify({"now": {"active": False}, "tasks": [], "activity": [], "pending": None, "recent_files": []})
    return jsonify(XI.snapshot())

@flask_app.route("/api/xi/task/update", methods=["POST"])
def api_xi_update():
    d = request.json or {}
    if not XI:
        return jsonify({"ok": False, "error": "automations are not running"}), 503
    return jsonify(XI.update_task(str(d.get("tid") or ""), d.get("patch") or {}))

@flask_app.route("/api/xi/task/ask", methods=["POST"])
def api_xi_task_ask():
    """'Tell Xenora what to change' box: the same chat-edit path (it shows the change and asks before applying)."""
    d = request.json or {}
    tid, text = str(d.get("tid") or ""), str(d.get("text") or "").strip()
    if not XI or not text or not XI.store.get(tid):
        return jsonify({"ok": False}), 400
    add_chat_message("user", text)
    threading.Thread(target=lambda: XI._edit(tid, text), daemon=True).start()
    return jsonify({"ok": True})

@flask_app.route("/api/xi/workspaces", methods=["GET"])
def api_xi_workspaces():
    return jsonify(XI.workspaces_state() if XI else {"workspaces": [], "allowances": []})

@flask_app.route("/api/xi/workspaces/add", methods=["POST"])
def api_xi_workspaces_add():
    d = request.json or {}
    return jsonify(XI.workspace_add(str(d.get("path") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/workspaces/remove", methods=["POST"])
def api_xi_workspaces_remove():
    d = request.json or {}
    return jsonify(XI.workspace_remove(str(d.get("path") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/allow/add", methods=["POST"])
def api_xi_allow_add():
    d = request.json or {}
    return jsonify(XI.allow_add(str(d.get("path") or ""), d.get("ops") or ["overwrite"]) if XI else {"ok": False})

@flask_app.route("/api/xi/allow/remove", methods=["POST"])
def api_xi_allow_remove():
    d = request.json or {}
    return jsonify(XI.allow_remove(str(d.get("id") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/everything/set", methods=["POST"])
def api_xi_everything_set():
    d = request.json or {}
    return jsonify(XI.everything_set(str(d.get("es") or ""), str(d.get("app") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/everything/start", methods=["POST"])
def api_xi_everything_start():
    return jsonify(XI.everything_start() if XI else {"ok": False})

@flask_app.route("/api/xi/everything/autostart", methods=["POST"])
def api_xi_everything_autostart():
    d = request.json or {}
    return jsonify(XI.everything_autostart(bool(d.get("on"))) if XI else {"ok": False})

@flask_app.route("/api/xi/workspaces/profile", methods=["POST"])
def api_xi_workspaces_profile():
    d = request.json or {}
    return jsonify(XI.workspace_profile(str(d.get("path") or ""), str(d.get("rules") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/workspaces/relearn", methods=["POST"])
def api_xi_workspaces_relearn():
    d = request.json or {}
    return jsonify(XI.workspace_relearn(str(d.get("path") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/workspaces/organize", methods=["POST"])
def api_xi_workspaces_organize():
    d = request.json or {}
    return jsonify(XI.workspace_organize(str(d.get("path") or ""), str(d.get("instruction") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/moves/undo", methods=["POST"])
def api_xi_moves_undo():
    d = request.json or {}
    return jsonify(XI.moves_undo(str(d.get("id") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/skills", methods=["GET"])
def api_xi_skills():
    return jsonify(XI.skills_list() if XI else [])

@flask_app.route("/api/xi/skills/save", methods=["POST"])
def api_xi_skills_save():
    d = request.json or {}
    if not XI:
        return jsonify({"ok": False}), 503
    return jsonify(XI.skill_save(str(d.get("slug") or ""), str(d.get("name") or ""), str(d.get("when") or ""), str(d.get("body") or ""), str(d.get("keywords") or "")))

@flask_app.route("/api/xi/skills/import", methods=["POST"])
def api_xi_skills_import():
    d = request.json or {}
    if not XI:
        return jsonify({"ok": False}), 503
    return jsonify(XI.skill_import(str(d.get("url") or ""), str(d.get("focus") or "")))

@flask_app.route("/api/xi/skills/delete", methods=["POST"])
def api_xi_skills_delete():
    d = request.json or {}
    return jsonify(XI.skill_delete(str(d.get("slug") or "")) if XI else {"ok": False})

@flask_app.route("/api/xi/trust", methods=["POST"])
def api_xi_trust():
    d = request.json or {}
    if not XI:
        return jsonify({"ok": False}), 503
    return jsonify(XI.trust_set(str(d.get("key") or ""), str(d.get("state") or "")))

@flask_app.route("/api/xi/reply", methods=["POST"])
def api_xi_reply():
    """Quick-reply buttons (yes / no / always / done / skip) answer the question she is waiting on."""
    text = str((request.json or {}).get("text") or "").strip()
    if not XI or not text:
        return jsonify({"ok": False}), 400
    ok = XI.handle_user_reply(text)
    if ok:
        add_chat_message("user", text)
    return jsonify({"ok": bool(ok)})

@flask_app.route("/api/task/cancel", methods=["POST"])
def api_cancel_task():
    data = request.json or {}
    tid  = data.get("id")
    cancelled = False
    if XI and str(tid or "").startswith("xi:") and tid != "xi:ask":
        return jsonify({"cancelled": bool(XI.ui_action(tid[3:], "cancel").get("stopped"))})
    with _task_lock:
        if tid in active_tasks and active_tasks[tid]["status"] == "watching":
            active_tasks[tid]["status"] = "cancelled"
            cancelled = True
        elif tid in active_schedules and active_schedules[tid]["status"] == "scheduled":
            active_schedules[tid]["status"] = "cancelled"
            cancelled = True
    if cancelled:
        _push_all_tasks()
    return jsonify({"cancelled": cancelled})

@flask_app.route("/api/toggle/vision", methods=["POST"])
@flask_app.route("/api/vision/toggle", methods=["POST"])
def toggle_vision():
    global VISION_ENABLED
    VISION_ENABLED = not VISION_ENABLED
    ui_state["vision_enabled"] = VISION_ENABLED
    push_to_ui("toggle_update", {"feature": "vision", "enabled": VISION_ENABLED})
    dbg(f"[TOGGLE] vision → {VISION_ENABLED}")
    return jsonify({"feature": "vision", "enabled": VISION_ENABLED})

@flask_app.route("/api/toggle/voice", methods=["POST"])
def toggle_voice():
    global VOICE_ENABLED, COMMENTS_ENABLED
    VOICE_ENABLED = not VOICE_ENABLED
    if not VOICE_ENABLED:
        COMMENTS_ENABLED = False
    push_to_ui("toggle_update", {"feature": "voice",    "enabled": VOICE_ENABLED})
    push_to_ui("toggle_update", {"feature": "comments", "enabled": COMMENTS_ENABLED})
    dbg(f"[TOGGLE] voice → {VOICE_ENABLED}  (comments forced → {COMMENTS_ENABLED})")
    return jsonify({"feature": "voice", "enabled": VOICE_ENABLED})

@flask_app.route("/api/toggle/comments", methods=["POST"])
def toggle_comments():
    global COMMENTS_ENABLED
    if not VISION_ENABLED or not VOICE_ENABLED:
        return jsonify({
            "feature": "comments",
            "enabled": False,
            "error": "requires vision and voice to be enabled"
        })
    COMMENTS_ENABLED = not COMMENTS_ENABLED
    push_to_ui("toggle_update", {"feature": "comments", "enabled": COMMENTS_ENABLED})
    dbg(f"[TOGGLE] comments → {COMMENTS_ENABLED}")
    return jsonify({"feature": "comments", "enabled": COMMENTS_ENABLED})

@flask_app.route("/api/toggle/overclock", methods=["POST"])
def toggle_overclock():
    """Flips OVERCLOCK_MODE — see its definition near the TOGGLE FLAGS block
    for exactly what this restricts. Announces the switch in chat (in
    character, not spoken — voice is already off the moment this turns on)
    so it's obvious from the transcript alone when a reply came from
    Nemotron instead of the everyday model."""
    global OVERCLOCK_MODE
    OVERCLOCK_MODE = not OVERCLOCK_MODE
    ui_state["overclock"] = OVERCLOCK_MODE
    push_to_ui("toggle_update", {"feature": "overclock", "enabled": OVERCLOCK_MODE})
    dbg(f"[TOGGLE] overclock → {OVERCLOCK_MODE}")
    msg = ("Overclock engaged. Nemotron's in the driver's seat now — plain chat only, "
           "no voice, until you switch this back off." if OVERCLOCK_MODE else
           "Overclock disengaged — back to normal.")
    add_chat_message("assistant", msg, remember=False)
    return jsonify({"feature": "overclock", "enabled": OVERCLOCK_MODE})

@flask_app.route("/api/toggles", methods=["GET"])
def get_toggles():
    return jsonify({
        "vision":   VISION_ENABLED,
        "voice":    VOICE_ENABLED,
        "comments": COMMENTS_ENABLED,
        "overclock": OVERCLOCK_MODE,
    })

@flask_app.route("/api/models/check", methods=["POST"])
def api_models_check():
    with _model_check_lock:
        if _model_check_state["running"]:
            return jsonify({"ok": False, "error": "a model check is already running"}), 409
        _model_check_state.update(running=True, results=[], started=time.time(), finished=None)
    threading.Thread(target=_run_model_check, daemon=True).start()
    return jsonify({"ok": True})

@flask_app.route("/api/models/check-imagegen", methods=["POST"])
def api_models_check_imagegen():
    """Standalone image-gen test — deliberately separate from /api/models/check (see
    _run_imagegen_check's docstring): Pollinations is free/unlimited so this is cheap,
    but still kept separate to avoid clutter."""
    with _model_check_lock:
        if _model_check_state["running"]:
            return jsonify({"ok": False, "error": "a model check is already running"}), 409
    threading.Thread(target=_run_imagegen_check, daemon=True).start()
    return jsonify({"ok": True})

@flask_app.route("/api/models/check", methods=["GET"])
def api_models_check_last():
    with _model_check_lock:
        return jsonify(dict(_model_check_state))

@flask_app.route("/api/logs", methods=["GET"])
def api_logs():
    """Backlog for the LOGS tab (live entries then arrive via the log_entry socket event)."""
    with _log_lock:
        return jsonify(list(_log_buffer))

@flask_app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock:
        _log_buffer.clear()
    return jsonify({"ok": True})

@flask_app.route("/api/open-file", methods=["POST"])
def api_open_file():
    """Lets the activity-feed panel's "Open" button (on a completed download)
    launch the file in its default app, the same way the open_app shortcuts
    above use os.startfile(). Restricted to DOWNLOAD_FOLDER so this can't be
    used to open an arbitrary path elsewhere on disk."""
    data = request.json or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "no path given"}), 400
    real_download_dir = os.path.realpath(DOWNLOAD_FOLDER)
    real_path = os.path.realpath(path)
    try:
        inside_downloads = os.path.commonpath([real_download_dir, real_path]) == real_download_dir
    except ValueError:
        inside_downloads = False
    if not inside_downloads and real_path not in _openable_paths:
        return jsonify({"ok": False, "error": "that file isn't one Xenora produced or downloaded"}), 403
    if not os.path.isfile(real_path):
        return jsonify({"ok": False, "error": "file no longer exists"}), 404
    try:
        os.startfile(real_path)
        return jsonify({"ok": True})
    except Exception as e:
        dbg(f"[OPEN-FILE] failed to open {real_path}: {e}")
        return jsonify({"ok": False, "error": "could not open the file"}), 500

@flask_app.route("/api/explain_topic", methods=["POST"])
def explain_topic():
    global _user_typing
    data   = request.json or {}
    title  = data.get("title", "")
    detail = data.get("detail", "")
    if not title:
        return jsonify({"error": "no title"}), 400
    prompt = (
        f'Give a sharp, intelligent 3-4 sentence analysis of this topic from your perspective as Xenora. '
        f'Be specific and insightful, not generic. Topic: "{title}". Context: "{detail}"'
    )
    _user_typing = True
    def _run():
        global _user_typing
        run_command(prompt, _memory)
        _user_typing = False
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "processing"})

@flask_app.route("/api/memory", methods=["GET"])
def get_memory():
    return jsonify(_memory)

@flask_app.route("/api/memory/fact", methods=["DELETE"])
def delete_fact():
    data  = request.json or {}
    fact  = data.get("fact")
    if fact and fact in _memory.get("facts", []):
        _memory["facts"].remove(fact)
        save_memory(_memory)
        push_to_ui("memory_update", {"facts": _memory["facts"], "user_name": _memory.get("user_name", "User")})
    return jsonify({"facts": _memory.get("facts", [])})

@flask_app.route("/api/memory/graph", methods=["GET"])
def get_memory_graph():
    return jsonify(_graph_public(_graph))

@flask_app.route("/api/memory/graph/node", methods=["POST"])
def add_memory_graph_node():
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    node = graph_add_node(
        content,
        ntype=data.get("type", "context"),
        tags=data.get("tags") or [],
        confidence="stated",
    )
    push_to_ui("graph_update", _graph_public(_graph))
    return jsonify({k: v for k, v in node.items() if k != "embedding"})

@flask_app.route("/api/memory/graph/node/<node_id>", methods=["PATCH"])
def patch_memory_graph_node(node_id):
    data = request.json or {}
    node = graph_update_node(
        node_id,
        content=data.get("content"),
        ntype=data.get("type"),
        tags=data.get("tags"),
    )
    if not node:
        return jsonify({"error": "node not found"}), 404
    push_to_ui("graph_update", _graph_public(_graph))
    return jsonify({k: v for k, v in node.items() if k != "embedding"})

@flask_app.route("/api/memory/graph/node/<node_id>", methods=["DELETE"])
def delete_memory_graph_node(node_id):
    graph_delete_node(node_id)
    push_to_ui("graph_update", _graph_public(_graph))
    return jsonify(_graph_public(_graph))

@flask_app.route("/api/memory/graph/edge", methods=["POST"])
def add_memory_graph_edge():
    data = request.json or {}
    edge = graph_add_edge(
        data.get("source"),
        data.get("target"),
        relation=(data.get("relation") or "related").strip(),
    )
    if not edge:
        return jsonify({"error": "invalid source/target"}), 400
    push_to_ui("graph_update", _graph_public(_graph))
    return jsonify(edge)

@flask_app.route("/api/memory/graph/edge/<edge_id>", methods=["DELETE"])
def delete_memory_graph_edge(edge_id):
    graph_delete_edge(edge_id)
    push_to_ui("graph_update", _graph_public(_graph))
    return jsonify(_graph_public(_graph))

@flask_app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _shutdown():
        msg = "Saving session."
        add_chat_message("assistant", msg)
        speak(msg, style="chat")
        summary = summarize_session(chat_history)
        if summary:
            _memory["session_summaries"].append({
                "date":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "summary": summary
            })
            _memory["session_summaries"] = _memory["session_summaries"][-10:]
        save_memory(_memory)
        save_session(chat_history)
        close_browser()
        msg2 = "Memory saved. Shutting down. Goodnight."
        add_chat_message("assistant", msg2)
        speak(msg2, style="empathetic")
        time.sleep(2)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"status": "shutting_down"})

@socketio.on("connect")
def on_connect():
    dbg("[SOCKET] Client connected")
    safe = {k: v for k, v in ui_state.items() if k != "last_screenshot"}
    socketio.emit("full_state", safe)

@socketio.on("chat_message")
def on_socket_chat(data):
    global _user_typing
    text = data.get("message", "").strip()
    if text:
        _user_typing = True
        def _run():
            global _user_typing
            run_command(text, _memory)
            _user_typing = False
        threading.Thread(target=_run, daemon=True).start()

@socketio.on("stop_generation")
def on_stop_generation():
    stop_current_turn()

@socketio.on("skip_voice")
def on_skip_voice():
    skip_current_speech()


_memory = {}
_graph  = {"nodes": [], "edges": []}


if __name__ == "__main__":
    _memory = load_memory()
    chat_history = []
    save_session([])

    if not _memory.get("user_name"):
        _memory["user_name"] = "User"
        save_memory(_memory)

    ui_state["memory_facts"] = _memory.get("facts", [])

    _graph = load_graph()
    graph_migrate_types()
    graph_seed_from_facts(_memory)
    graph_seed_system_capabilities()
    graph_ensure_system_nodes()
    graph_start_expiry_scheduler()
    graph_backfill_embeddings()

    print("[Xenora: reading own source code...]")
    xenora_code, code_hash, capabilities_summary = refresh_self_awareness()

    if groq_client:
        print("[Xenora: checking connection...]")
        deadline = time.time() + 4
        while _checking and time.time() < deadline:
            time.sleep(0.1)
        model_str = LAST_MODEL_LABEL if _online else f"Local · {LOCAL_MODEL}"
        ui_state["model"] = model_str
        print(f"[Xenora: {'online — ' + model_str if _online else 'offline — ' + model_str}]")

    if WINDOW_DEPS_OK:
        threading.Thread(target=vision_speaker,        daemon=True).start()
        threading.Thread(target=window_awareness_loop, daemon=True).start()
        print(f"[Xenora: window awareness thread started (capture {'active' if VISION_ENABLED else 'idle until toggled on'}).]")

    try:
        import xenora_integration as xi
        def _xi_gemini(system, user):
            """Second provider for the planner: used when every Groq model is cooling down, retired or failing."""
            if not gemini_client or all(_cooldown_active(m) for m in GEMINI_TEXT_MODEL_CHAIN):
                raise RuntimeError("no Gemini model available")
            out = _gemini_text_chat(system, [{"role": "user", "content": user}], temperature=0.1)
            if not (out or "").strip():
                raise RuntimeError("Gemini returned an empty reply")
            return out
        def _xi_llm(system, user, tier=0, budgets=((2000, "low"), (3200, "medium")), temperature=0.1):
            chain = GROQ_MODEL_CHAIN[tier:] or GROQ_MODEL_CHAIN[-1:]
            waited, last_err = 0.0, None
            while True:
                live = [m for m in chain if not _cooldown_active(m)] or [m for m in GROQ_MODEL_CHAIN if not _cooldown_active(m)]
                if live:
                    chain = live
                    for budget, effort in budgets:
                        try:
                            r = _groq_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                           temperature=temperature, max_tokens=budget, model_chain=chain, reasoning_effort=effort)
                        except Exception as e:
                            last_err = e
                            break
                        out = r.choices[0].message.content or ""
                        if out.strip():
                            return out
                    try:
                        return _xi_gemini(system, user)
                    except Exception as e:
                        last_err = last_err or e
                        chain = [m for m in GROQ_MODEL_CHAIN if not _cooldown_active(m)] or chain
                        if live and not all(_cooldown_active(m) for m in GROQ_MODEL_CHAIN):
                            if waited >= 60 or not xi.xp.is_patient():
                                raise RuntimeError(f"the planner's models all failed: {str(last_err)[:160]}")
                else:
                    try:
                        return _xi_gemini(system, user)
                    except Exception as e:
                        last_err = last_err or e
                if not xi.xp.is_patient() or waited >= 900:
                    raise RuntimeError(f"all models are unavailable ({', '.join(GROQ_MODEL_CHAIN)}): {str(last_err)[:160]}")
                nap = min(20.0, max(1.0, min(_model_cooldown.get(m, 0) for m in GROQ_MODEL_CHAIN) - time.time()))
                time.sleep(nap)
                waited += nap
        def _xi_doc_llm(system, user):
            """create_document's writer: same cool-down / fallback handling as the planner, but room for a whole section and a warmer style."""
            return _xi_llm(system, user, 0, budgets=((3600, "low"), (4800, "medium")), temperature=0.5)
        def _xi_titles():
            out = []
            if WINDOW_DEPS_OK:
                win32gui.EnumWindows(lambda h, _: out.append(win32gui.GetWindowText(h))
                                     if win32gui.IsWindowVisible(h) else None, None)
            return out
        def _xi_vision(prompt, jpeg_bytes):
            import io; from PIL import Image
            ans, _ = gemini_vision_call("Locate UI elements. Reply with JSON only.", prompt,
                                        [Image.open(io.BytesIO(jpeg_bytes))], max_output_tokens=120)
            return ans or ""
        def _xi_vision_chat(system, prompt, jpeg_bytes, max_tokens=700):
            """A picture + instructions -> the model's reply. Used by the screen actor (looking at the real screen)."""
            import io; from PIL import Image
            ans, _ = gemini_vision_call(system, prompt, [Image.open(io.BytesIO(jpeg_bytes))], temperature=0.2, max_output_tokens=max_tokens)
            if not ans:
                raise RuntimeError("no vision model answered (all cooling down or unavailable)")
            return ans
        XI = xi.Runtime(
            speak=lambda t: speak(_short_speech(t), style="chat", auto=True, mid=_last_instant_mid[0]),
            chat=lambda t, source=None: add_chat_message("assistant", t, remember=False, instant=True, source=source),
            llm=_xi_llm, doc_llm=_xi_doc_llm,
            mem_search=lambda q: [n["content"] for n in graph_retrieve_context(q)],
            mem_add=lambda text, tags: graph_add_node(text, "preference", tags, "stated"),
            start_presence_watch=start_presence_watch,
            window_titles=_xi_titles, dbg=dbg, vision=_xi_vision,
            base_dir=os.path.dirname(os.path.abspath(__file__)),
            rephrase=lambda text, kind: llm("Say this to the user in your own voice - short, natural, spoken-friendly. Keep every number, name, path, site and username EXACTLY as written; add no new facts or emojis. If the message is a question or asks for a yes/no or a go-ahead, it must still be a full question when you are done - never reply with just a name or path. Reply with only the message.", text, temperature=0.6),
            search=web_search_answer, open_app=open_app_by_name, extract_file=extract_file_content,
            is_blocked_path=_is_blocked_path, xenora_ns=globals(), push_ui=_push_all_tasks, push_event=_xi_push,
            vision_chat=_xi_vision_chat)
        XI.start()
        print("[Xenora: hands + routines ready.]")
    except Exception as e:
        XI = None
        import traceback as _tb
        print(f"[Xenora: hands/routines unavailable - {type(e).__name__}: {e}]")
        _tb.print_exc()

    print("[Xenora: starting UI server on http://localhost:5000]")
    flask_thread = threading.Thread(
        target=lambda: socketio.run(flask_app, host="127.0.0.1", port=5000, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    time.sleep(1)

    print("[Xenora: ready. Open the Electron UI or visit http://localhost:5000]\n")

    gap_context = ""
    if _memory.get("last_seen"):
        try:
            last = datetime.fromisoformat(_memory["last_seen"])
            gap_context = f" It's been {humanize_delta((datetime.now() - last).total_seconds())} since " + OWNER_NAME + " last talked to you."
        except Exception:
            pass

    _now = datetime.now()
    _hour = _now.hour
    if 5 <= _hour < 12:
        _period = "morning"
    elif 12 <= _hour < 17:
        _period = "afternoon"
    elif 17 <= _hour < 21:
        _period = "evening"
    else:
        _period = "night"
    time_context = f" Current local time is {_now.strftime('%I:%M %p')} — it is currently {_period}."

    try:
        intro = llm(
            f"You're Xenora, an AI desktop assistant, greeting your creator {OWNER_NAME} as you start up. "
            "Write ONE short, natural, varied greeting (under 15 words) — never a rigid template, never "
            "the same phrasing twice. If a time-of-day greeting fits naturally, base it on the ACTUAL "
            "current period given below — never default to 'good afternoon' out of habit, and if it's "
            "late night, a 'good afternoon'-style greeting would be a real mistake. Only mention how long "
            "it's been since you last talked if that context is genuinely notable (many days) — for "
            "a short gap, just greet him naturally without bringing it up at all."
            + time_context + gap_context,
            "Greet " + OWNER_NAME + " now.", temperature=0.9
        )
        intro = clean_response(intro) or "Xenora online."
    except Exception as e:
        dbg(f"[STARTUP] greeting generation failed, using a canned line instead: {e}")
        intro = random.choice([
            f"Good {_period}, {OWNER_NAME}. I'm up, though my thinking's a bit limited right now — no cloud or "
            f"local model reachable at the moment.",
            "Xenora online — running with limited capability right now, no model reachable just yet.",
        ])
    try:
        _memory["last_seen"] = datetime.now().isoformat()
        save_memory(_memory)
        add_chat_message("assistant", intro)
        speak(intro, style="chat")
    except Exception as e:
        dbg(f"[STARTUP] couldn't post/speak the greeting, continuing anyway: {e}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Xenora: shutting down...]")
        summary = summarize_session(chat_history)
        if summary:
            _memory["session_summaries"].append({
                "date":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "summary": summary
            })
        save_memory(_memory)
        save_session(chat_history)
        close_browser()