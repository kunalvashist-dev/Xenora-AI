// main.js — Xenora Electron host v4.2
// Manages: mainWindow, orbWindow, pieOverlayWindow, tray,
//          globalShortcut (Ctrl+Shift+X), all IPC routing

const {
  app, BrowserWindow, Tray, Menu, ipcMain,
  nativeImage, screen, globalShortcut, Notification
} = require('electron');

// BUG FIX: unpackaged/dev Electron apps on Windows often need an explicit
// AppUserModelID for `new Notification()` to reliably show at all — without
// one, Windows may attribute the toast to a generic "Electron" identity (or
// silently drop it depending on Windows version/Focus Assist config). Must
// be set before app.whenReady().
app.setAppUserModelId('com.xenora.ai');

const path      = require('path');
const { spawn, spawnSync } = require('child_process');
const fs        = require('fs');
const http      = require('http');

let mainWindow  = null;
let orbWindow   = null;
let pieWindow   = null;
let tray        = null;
let xenoraProc  = null;
let orbVisible  = true;

// Master toggle state — single source of truth in main process
const T = { vision: true, comments: true, voice: true, orb: true };

// ── HTTP helper ───────────────────────────────────────────────────────────────
function backendPost(urlPath, body = {}) {
  return new Promise((resolve) => {
    const data = JSON.stringify(body);
    const opts = {
      hostname: '127.0.0.1', port: 5000,
      path: urlPath, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
    };
    const req = http.request(opts, res => {
      let raw = '';
      res.on('data', c => raw += c);
      res.on('end', () => {
        try { resolve({ ok: true, data: JSON.parse(raw) }); }
        catch { resolve({ ok: true, data: {} }); }
      });
    });
    req.on('error', () => resolve({ ok: false, data: {} }));
    req.write(data); req.end();
  });
}

// ── Flask SocketIO proxy — forwards speaking/online state to orbWindow ────────
// Polls /api/state every 2 seconds and pushes delta to orb via IPC.
// This avoids adding socket.io-client as a Node dep while keeping orb in sync.
let _lastSpeaking     = false;
let _lastOnline       = null;
let _lastTaskFlash    = 0;
let _lastCountdownKey = null;
let _lastNotifyId     = null;
let _lastXiNow = '';
let _lastFileOpState  = "idle";

// BUG FIX: desktop notifications on task completion. xenora.py used to try
// to pop these itself via a PowerShell NotifyIcon balloon, which its own
// comment already admitted was flaky and "failing silently" on recent
// Windows — so nothing ever showed up. Electron's own Notification API is a
// real, first-class OS notification and needs no subprocess/AUMID dance.
// The backend just bumps ui_state.last_notification (already picked up by
// this same /api/state poll below); we fire the actual OS toast here.
function fireDesktopNotification(n) {
  if (!n) return;
  if (!Notification.isSupported()) {
    console.log('[Xenora] Notification.isSupported() === false — OS notifications unavailable on this machine.');
    return;
  }
  console.log(`[Xenora] firing desktop notification: "${n.title}" — "${n.message}"`);
  const iconPath = path.join(__dirname, 'assets', 'icon.png');
  try {
    const notif = new Notification({
      title: n.title || 'Xenora',
      body:  n.message || '',
      icon:  fs.existsSync(iconPath) ? iconPath : undefined,
      silent: false,
    });
    notif.on('click', () => { mainWindow?.show(); mainWindow?.focus(); });
    notif.on('show', () => console.log('[Xenora] notification shown OK'));
    notif.on('failed', (e) => console.log('[Xenora] notification failed:', e));
    notif.show();
  } catch (e) {
    console.log('[Xenora] fireDesktopNotification threw:', e);
  }
}

function startBackendStateProxy() {
  const poll = () => {
    const req = http.get(
      { hostname: '127.0.0.1', port: 5000, path: '/api/state', timeout: 1500 },
      res => {
        let raw = '';
        res.on('data', c => raw += c);
        res.on('end', () => {
          try {
            const state = JSON.parse(raw);
            const speaking = !!state.speaking;
            const online   = !!state.online;

            if (speaking !== _lastSpeaking) {
              _lastSpeaking = speaking;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'speaking', speaking });
            }
            if (online !== _lastOnline) {
              _lastOnline = online;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'status', online });
              if (mainWindow && !mainWindow.isDestroyed())
                mainWindow.webContents.send('status-update', { online });
            }
            if (state.task_flash && state.task_flash !== _lastTaskFlash) {
              _lastTaskFlash = state.task_flash;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'task_done' });
            }
            const countdown = state.power_countdown || null;
            const countdownKey = countdown ? `${countdown.action}:${countdown.seconds_left}` : null;
            if (countdownKey !== _lastCountdownKey) {
              _lastCountdownKey = countdownKey;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'power_countdown', countdown });
            }

            const notif = state.last_notification || null;
            if (notif && notif.id !== _lastNotifyId) {
              _lastNotifyId = notif.id;
              fireDesktopNotification(notif);
            }

            // Orb state icon ("idle"/"searching"/"reading"/"writing") — same
            // relay pattern as speaking/power_countdown above.
            const xn = state.xi_now || null;                       // what she is doing for a task right now
            const xnKey = xn ? `${xn.active}|${xn.state}|${xn.task}|${xn.text}` : 'none';
            if (xnKey !== _lastXiNow) {
              _lastXiNow = xnKey;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'xi_now', now: xn });
            }
            const fileOpState = state.file_op_state || "idle";
            if (fileOpState !== _lastFileOpState) {
              _lastFileOpState = fileOpState;
              if (orbWindow && !orbWindow.isDestroyed())
                orbWindow.webContents.send('backend-state', { type: 'file_op_state', state: fileOpState });
            }
          } catch (e) { console.log('[Xenora] state-poll handler error:', e); }
        });
      }
    );
    req.on('error', () => {});
    req.end();
  };

  // Start after backend has had time to boot
  setTimeout(() => {
    poll();
    setInterval(poll, 1000); // 1s so the power countdown reads smoothly, not just every other tick
  }, 5000);
}

// ── Launch Python backend ─────────────────────────────────────────────────────
function findXenoraScript() {
  // BUG FIX: this used to hardcode a single assumed location
  // (__dirname/../xenora.py) — the moment the actual folder layout doesn't
  // match that guess exactly, spawn() fails with a bare ENOENT two folders
  // deep, giving no hint about what to actually fix. Checking a handful of
  // realistic candidate locations and using whichever one really exists
  // means a folder reorg doesn't break the launch silently — and if truly
  // nowhere expected, the error below says so plainly instead of guessing.
  const candidates = [
    path.join(__dirname, '..', 'xenora.py'),
    path.join(__dirname, '..', '..', 'xenora.py'),
    path.join(__dirname, 'xenora.py'),
    path.join(process.cwd(), 'xenora.py'),
    path.join(process.cwd(), '..', 'xenora.py'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  console.error('[Xenora] Could not find xenora.py in any expected location. Checked:');
  candidates.forEach(p => console.error('  -', p));
  console.error('[Xenora] Move xenora.py to one of the paths above, or edit findXenoraScript() in main.js.');
  return null;
}

function startXenora() {
  const pyPath = findXenoraScript();
  if (!pyPath) return;
  xenoraProc = spawn('py', [pyPath], {
    cwd: path.dirname(pyPath),
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: false,
  });
  xenoraProc.stdout.on('data', d => console.log('[Xenora]', d.toString().trim()));
  xenoraProc.stderr.on('data', d => console.error('[Xenora ERR]', d.toString().trim()));
  xenoraProc.on('exit', code => {
    console.log(`[Xenora] exited with code ${code}`);
    if (mainWindow && !mainWindow.isDestroyed())
      mainWindow.webContents.send('xenora-died', { code });
  });
}

// ── Create main window ────────────────────────────────────────────────────────
// ── Graceful quit — every close path (UI X button, tray Quit, native
// window close/taskkill) funnels through this instead of three separate,
// inconsistent shutdown behaviors (see call sites below for what each used
// to do). Calls xenora.py's own /api/shutdown (saves memory/session, says
// goodbye) before actually tearing Electron down via app.quit() — NOT
// app.exit(), since app.exit() explicitly skips before-quit/will-quit,
// which is what respawns the wake-word listener.
let _quitting = false;
async function gracefulQuit() {
  if (_quitting) return; // avoid double-triggering if two close paths fire near-simultaneously
  _quitting = true;
  console.log('[Xenora] Graceful quit requested — saving memory before exit.');
  await backendPost('/api/shutdown');
  setTimeout(() => { app.quit(); }, 2500);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1600, height: 960,
    minWidth: 1200, minHeight: 700,
    frame: false, transparent: false,
    backgroundColor: '#030508',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
    icon: path.join(__dirname, 'assets', 'icon.png'),
    show: false,
  });
  mainWindow.loadFile(path.join(__dirname, 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  // BUG FIX: this used to unconditionally hide the window on ANY close
  // signal — including the OS-level close a plain (non-forced) `taskkill`
  // sends — so there was no way to actually quit Xenora from a terminal
  // without a hard kill that skips saving memory. Now a close event (from
  // any source) triggers the same graceful quit the UI's own X button uses.
  mainWindow.on('close', e => {
    if (_quitting) return; // already tearing down — let it proceed
    e.preventDefault();
    gracefulQuit();
  });
}

// ── Create orb window ─────────────────────────────────────────────────────────
function createOrbWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;
  orbWindow = new BrowserWindow({
    width: 160, height: 160,
    x: width  - 200,
    y: height - 200,
    frame:       false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable:   false,
    hasShadow:   false,
    focusable:   true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload-orb.js'),
    },
  });
  orbWindow.setAlwaysOnTop(true, 'screen-saver');
  orbWindow.loadFile(path.join(__dirname, 'orb.html'));
  orbWindow.on('close', e => { e.preventDefault(); hideOrb(); });
  // Keep the orb on-screen while it's being dragged natively.
  // IMPORTANT: don't call setPosition() here. The orb uses
  // -webkit-app-region:drag, so this is Windows' own native drag loop, not
  // manual mouse tracking — forcing a reposition mid-drag desyncs that
  // loop's internal reference point, which is what made the orb move the
  // wrong way (down became up) and left an unclickable ghost strip behind
  // for the rest of the gesture. Just refusing the specific out-of-bounds
  // step is enough: the orb stays put at the edge and resumes moving
  // normally the moment the cursor comes back in range.
  orbWindow.on('will-move', (e, bounds) => {
    const wa = screen.getPrimaryDisplay().workAreaSize;
    const maxX = wa.width  - bounds.width;
    const maxY = wa.height - bounds.height;
    if (bounds.x < 0 || bounds.x > maxX || bounds.y < 0 || bounds.y > maxY) {
      e.preventDefault();
    }
  });
  orbWindow.on('blur', () => {
    setTimeout(() => {
      if (orbWindow && !orbWindow.isDestroyed())
        orbWindow.setAlwaysOnTop(true, 'screen-saver');
    }, 80);
  });
}

// ── Create pie overlay window ────────────────────────────────────────────────
function createPieWindow() {
  const { width, height } = screen.getPrimaryDisplay().size;
  pieWindow = new BrowserWindow({
    width, height, x: 0, y: 0,
    frame:       false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable:   false,
    hasShadow:   false,
    focusable:   true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload-pie.js'),
    },
  });
  pieWindow.setAlwaysOnTop(true, 'screen-saver');
  pieWindow.loadFile(path.join(__dirname, 'pie-overlay.html'));
  pieWindow.setIgnoreMouseEvents(true, { forward: true });
  pieWindow.hide();
  pieWindow.on('close', e => { e.preventDefault(); pieWindow.hide(); });
}

// ── Open pie — centres on orb position, clamped to keep items on-screen ──────
function openPie() {
  if (!pieWindow || pieWindow.isDestroyed()) return;

  const display = screen.getPrimaryDisplay();
  const sw = display.size.width;
  const sh = display.size.height;

  // Preferred centre: middle of orb
  let cx, cy;
  if (orbWindow && !orbWindow.isDestroyed() && orbVisible) {
    const [ox, oy] = orbWindow.getPosition();
    const [ow, oh] = orbWindow.getSize();
    cx = ox + Math.round(ow / 2);
    cy = oy + Math.round(oh / 2);
  } else {
    cx = Math.round(sw / 2);
    cy = Math.round(sh / 2);
  }

  // Clamp so pie items (R_ITEM=200) + icon (36px) never clip the screen edge
  const MARGIN = 260; // R_OUT from pie-overlay.html
  cx = Math.max(MARGIN, Math.min(sw - MARGIN, cx));
  cy = Math.max(MARGIN, Math.min(sh - MARGIN, cy));

  pieWindow.setIgnoreMouseEvents(true, { forward: true });
  pieWindow.show();
  pieWindow.setAlwaysOnTop(true, 'screen-saver');

  pieWindow.webContents.send('pie-init', {
    cx, cy,
    vision:   T.vision,
    comments: T.comments,
    voice:    T.voice,
    orb:      T.orb,
  });
}

// ── Close pie ─────────────────────────────────────────────────────────────────
function closePie() {
  if (!pieWindow || pieWindow.isDestroyed()) return;
  pieWindow.setIgnoreMouseEvents(true, { forward: true });
  pieWindow.hide();
  if (orbWindow && !orbWindow.isDestroyed() && orbVisible)
    orbWindow.setAlwaysOnTop(true, 'screen-saver');
}

// ── Orb show / hide ───────────────────────────────────────────────────────────
function showOrb() {
  if (!orbWindow || orbWindow.isDestroyed()) return;
  orbWindow.show();
  orbWindow.setAlwaysOnTop(true, 'screen-saver');
  orbVisible = true; T.orb = true;
  _broadcastToggle('orb', true);
}
function hideOrb() {
  if (!orbWindow || orbWindow.isDestroyed()) return;
  orbWindow.hide();
  orbVisible = false; T.orb = false;
  _broadcastToggle('orb', false);
}

// ── Broadcast a toggle to ALL windows + update master T ──────────────────────
function _broadcastToggle(feature, enabled) {
  T[feature] = enabled;
  if (mainWindow && !mainWindow.isDestroyed())
    mainWindow.webContents.send('toggle-state', { feature, enabled });
  if (orbWindow  && !orbWindow.isDestroyed())
    orbWindow.webContents.send('toggle-state-sync', { feature, enabled });
  // Pie gets updated next time it opens via pie-init payload
}

// ── Tray ──────────────────────────────────────────────────────────────────────
function createTray() {
  const iconPath = path.join(__dirname, 'assets', 'tray-icon.png');
  const icon = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 })
    : nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip('Xenora AI');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Xenora',  click: () => { mainWindow.show(); mainWindow.focus(); } },
    { label: 'Show Orb',     click: () => showOrb()  },
    { label: 'Hide Orb',    click: () => hideOrb() },
    { label: 'Pie Menu',    click: () => openPie()  },
    { type: 'separator' },
    { label: 'Quit',         click: () => gracefulQuit() },
  ]));
  tray.on('click', () => { mainWindow.show(); mainWindow.focus(); });
}

// ── Global shortcut — Ctrl+Shift+X ───────────────────────────────────────────
function registerShortcuts() {
  globalShortcut.unregister('CommandOrControl+Shift+X');
  const ok = globalShortcut.register('CommandOrControl+Shift+X', () => openPie());
  if (!ok) {
    console.warn('[Xenora] Ctrl+Shift+X taken — trying Ctrl+Shift+Z');
    globalShortcut.register('CommandOrControl+Shift+Z', () => openPie());
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// IPC HANDLERS
// ═══════════════════════════════════════════════════════════════════════════════

// Window controls
ipcMain.on('window-minimize', () => mainWindow?.minimize());
ipcMain.on('window-maximize', () => {
  if (mainWindow?.isMaximized()) mainWindow.unmaximize();
  else mainWindow?.maximize();
});
ipcMain.on('window-close', () => gracefulQuit());

// Toggle via IPC (from main UI xenoraAPI.toggle)
// Single path: renderer → main → backend → broadcast to all
ipcMain.handle('toggle-feature', async (_, feature) => {
  const result  = await backendPost(`/api/toggle/${feature}`);
  const enabled = result?.data?.enabled ?? null;
  if (enabled !== null) _broadcastToggle(feature, enabled);
  return { ok: result.ok, enabled };
});

// Orb show/hide (from main UI ORB toggle button)
ipcMain.on('orb-show', () => showOrb());
ipcMain.on('orb-hide', () => hideOrb());

// Orb left-click → pie
ipcMain.on('orb-open-pie', () => openPie());

// Orb click during an active power countdown cancels it directly instead of opening the pie
ipcMain.on('orb-cancel-power', () => backendPost('/api/power/cancel'));

// New orb button — stops an in-progress file search (searching/reading
// state) without touching active watch-tasks, schedules, or the power
// countdown. Separate IPC channel/endpoint from orb-cancel-power on purpose
// — these are two independent interrupts, not one shared "stop" button.
ipcMain.on('orb-stop-file-op', () => backendPost('/api/file_op/stop'));

// Pie overlay IPC
ipcMain.on('pie-close', () => closePie());

ipcMain.on('pie-set-capture', (_, capture) => {
  if (!pieWindow || pieWindow.isDestroyed()) return;
  if (capture) pieWindow.setIgnoreMouseEvents(false);
  else         pieWindow.setIgnoreMouseEvents(true, { forward: true });
});

ipcMain.on('pie-toggle-notify', (_, { feature, enabled }) => {
  // Pie already called the backend; just update master T and broadcast
  _broadcastToggle(feature, enabled);
});

ipcMain.on('pie-toggle-orb', () => {
  if (orbVisible) hideOrb(); else showOrb();
});

// Orb notifies of toggle
ipcMain.on('orb-toggle-notify', (_, { feature, enabled }) => {
  _broadcastToggle(feature, enabled);
});

// Orb right-click state broadcast (visual only, no toggle logic)
ipcMain.on('broadcast-orb-state', (_, state) => {
  if (orbWindow && !orbWindow.isDestroyed())
    orbWindow.webContents.send('orb-state-change', state);
});

// Main UI syncs toggle to orb (belt-and-suspenders after xenoraAPI.toggle)
ipcMain.on('main-toggle-sync', (_, data) => {
  T[data.feature] = data.enabled;
  if (orbWindow && !orbWindow.isDestroyed())
    orbWindow.webContents.send('toggle-state-sync', data);
});

// ═══════════════════════════════════════════════════════════════════════════════
// APP LIFECYCLE
// ═══════════════════════════════════════════════════════════════════════════════
app.whenReady().then(() => {
  startXenora();
  setTimeout(() => {
    createWindow();
    createOrbWindow();
    createPieWindow();
    createTray();
    registerShortcuts();
    startBackendStateProxy();
  }, 2000);
});

function findWakeWordScript() {
  // Same reasoning as findXenoraScript() above — search realistic candidate
  // locations instead of assuming one, so a folder reorg doesn't silently
  // break the hand-back of Ctrl+Shift+X to the wake-word listener.
  const candidates = [
    path.join(__dirname, '..', 'xenora_wakeword.pyw'),
    path.join(__dirname, '..', '..', 'xenora_wakeword.pyw'),
    path.join(__dirname, 'xenora_wakeword.pyw'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  console.error('[Xenora] Could not find xenora_wakeword.pyw — wake-word listener will not restart on quit. Checked:');
  candidates.forEach(p => console.error('  -', p));
  return null;
}

function findPythonwExe() {
  // BUG FIX: spawning via the 'py' launcher stub directly popped a visible
  // console (C:\Windows\py.exe) even with detached:true + stdio:'ignore' —
  // py.exe itself is a console-subsystem executable, so Windows can still
  // allocate it a window regardless of those options. pythonw.exe is a
  // GUI-subsystem executable and can never get a console, which removes the
  // ambiguity entirely. Rather than hardcode a path (Python installs live
  // in wildly different locations across machines), ask the 'py' launcher
  // where its own python.exe actually lives, then swap the filename —
  // pythonw.exe always ships in that same folder.
  try {
    const result = spawnSync('py', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' });
    const pythonExe = (result.stdout || '').trim();
    if (pythonExe) {
      const candidate = path.join(path.dirname(pythonExe), 'pythonw.exe');
      if (fs.existsSync(candidate)) return candidate;
    }
  } catch (e) {
    console.error('[Xenora] Could not resolve pythonw.exe via the py launcher:', e);
  }
  return null;
}

app.on('window-all-closed', () => { /* stay in tray */ });
app.on('before-quit', () => {
  globalShortcut.unregisterAll();
  if (xenoraProc) try { xenoraProc.kill(); } catch {}
  // Xenora is actually quitting (not just hiding to tray) — hand
  // Ctrl+Shift+X back to the standalone wake-word listener so saying
  // "Xenora" again can relaunch the app.
  const wakeWordPath = findWakeWordScript();
  if (wakeWordPath) {
    const pythonwExe = findPythonwExe();
    try {
      spawn(pythonwExe || 'py', [wakeWordPath], {
        cwd: path.dirname(wakeWordPath),
        detached: true,
        stdio: 'ignore',
        windowsHide: true,  // belt-and-suspenders even with pythonw.exe — Node's documented way to suppress any console
      }).unref();
    } catch (e) {
      console.error('[Xenora] Failed to relaunch wake-word listener:', e);
    }
  }
});
app.on('will-quit', () => globalShortcut.unregisterAll());