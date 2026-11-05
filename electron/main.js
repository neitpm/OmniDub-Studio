// OmniDub Studio - bo tui Electron.
// Mc tieu su dung: mo app -> bam 1 nut -> duoc video 2D, khong mo cmd, khong go lenh.
//
// LOI KIEN TRUC DA PHAI SUA BANG DO THAT:
//   Phien ban dau goi thang `docker ...` tu Windows -> smoke test tra ve
//   "docker=null, 5/5 service absent" trong khi stack dang chay that. Nguyen nhan:
//   `docker version` phia Windows loi `failed to connect to the docker API at
//   npipe:////./pipe/dockerDesktopLinuxEngine`, tuc Docker Desktop KHONG chay.
//   Toan bo 5 container chay bang dockerd BEN TRONG distro Ubuntu (WSL2), dung
//   cung lenh voi start_wsl.bat: `wsl -u root -d Ubuntu -e bash -c "cd
//   /mnt/d/translate && docker compose up -d"`. Nen lop lenh o day luon di qua
//   wsh(), khong co duong goi docker thang tu Windows.
const { app, BrowserWindow, ipcMain, shell, dialog, Tray, Menu } = require('electron')
const { exec, spawn } = require('child_process')
const fs = require('fs')
const http = require('http')
const path = require('path')

const SERVICES = [
  { name: 'translate-gateway', port: 7860, label: 'Giao diện & Điều phối' },
  { name: 'translate-stt', port: 8001, label: 'STT + Whisper' },
  { name: 'translate-translation', port: 8002, label: 'Dịch Trung → Việt' },
  { name: 'translate-tts', port: 8003, label: 'Lồng tiếng TTS' },
  { name: 'translate-video', port: 8004, label: 'Video + vẽ 2D' }
]
const REPO_DEFAULT = 'D:\\translate'
const WSL_DISTRO = 'Ubuntu'
const SMOKE = process.env.ELECTRON_SMOKE === '1'

let controlWin = null
let studioWin = null
let tray = null
let busy = false

// --- cai dat (giu lai giua cac lan chay, khong hardcode duong dan) ---
function settingsFile () {
  return path.join(app.getPath('userData'), 'settings.json')
}
function loadSettings () {
  try {
    return JSON.parse(fs.readFileSync(settingsFile(), 'utf8'))
  } catch (e) {
    return { repo: REPO_DEFAULT, autoOpenStudio: true }
  }
}
function saveSettings (s) {
  try { fs.writeFileSync(settingsFile(), JSON.stringify(s, null, 2)) } catch (e) { /* thieu quyen ghi -> bo qua */ }
}

// --- lop lenh ---
function sh (cmd, opts = {}) {
  return new Promise((resolve) => {
    exec(cmd, { windowsHide: true, maxBuffer: 64 * 1024 * 1024, timeout: opts.timeout || 120000 },
      (err, stdout, stderr) => resolve({
        ok: !err,
        out: String(stdout || ''),
        err: String(stderr || (err ? err.message : ''))
      }))
  })
}

// D:\translate -> /mnt/d/translate (WSL mount cua 9p/DrvFs)
function toWsl (p) {
  const m = /^([A-Za-z]):[\\/](.*)$/.exec(String(p).trim())
  if (!m) return String(p)
  return '/mnt/' + m[1].toLowerCase() + '/' + m[2].replace(/\\/g, '/')
}

// Chay lenh trong WSL. LUU Y: bangoi dung ben trong phai dung TRICH DON ('),
// vi toan bo da nam trong mot chuoi trich kep cua cmd.exe.
function wsh (inner, opts = {}) {
  return sh('wsl -u root -d ' + WSL_DISTRO + ' -e bash -c "' + inner + '"', opts)
}

function probe (port, ms = 1500) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, timeout: ms }, (res) => {
      res.resume()
      // 404 van tinh la song: stt-service khong co duong /health
      resolve(res.statusCode > 0)
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

function say (text, level = 'info') {
  if (controlWin && !controlWin.isDestroyed()) controlWin.webContents.send('log', { text, level, t: Date.now() })
  if (SMOKE) console.log('[' + level + '] ' + text)
}

// --- trang thai ---
async function status () {
  const s = loadSettings()
  const r = await wsh('docker ps -a --filter \'name=translate-\' --format \'{{.Names}}|{{.State}}\'', { timeout: 90000 })
  const rows = r.ok ? r.out.split(/\r?\n/).filter(Boolean).map((l) => l.split('|')) : []
  const byName = Object.fromEntries(rows)
  const list = []
  for (const sv of SERVICES) {
    const st = byName[sv.name] || 'absent'
    list.push({ name: sv.name, label: sv.label, port: sv.port, state: st, http: st === 'running' ? await probe(sv.port) : false })
  }
  return {
    services: list,
    docker: r.ok ? (await wsh('docker info --format \'{{.ServerVersion}}\'', { timeout: 60000 })).out.trim() || null : null,
    dockerErr: r.ok ? '' : (r.err || r.out).split(/\r?\n/)[0] || 'loi',
    upCount: list.filter((x) => x.state === 'running').length,
    repo: s.repo
  }
}

// --- hanh dong ---
async function ensureWslDocker () {
  const t = await wsh('docker info --format \'{{.ServerVersion}}\'', { timeout: 90000 })
  if (t.ok) return { ok: true, ver: t.out.trim() }
  say('dockerd trong WSL chua tra loi - thu khoi dong no ...', 'warn')
  await wsh('systemctl start docker 2>/dev/null || service docker start', { timeout: 120000 })
  for (let i = 0; i < 12; i++) {
    await new Promise((r) => setTimeout(r, 5000))
    const u = await wsh('docker info --format \'{{.ServerVersion}}\'', { timeout: 60000 })
    if (u.ok) { say('dockerd trong WSL da san sang (' + u.out.trim() + ')', 'ok'); return { ok: true, ver: u.out.trim() } }
  }
  const w = await sh('wsl -l -v', { timeout: 30000 })
  if (!new RegExp(WSL_DISTRO, 'i').test(w.out)) {
    say('Khong thay distro "' + WSL_DISTRO + '". Cai WSL + Ubuntu roi chay start_wsl.bat mot lan.', 'err')
  } else {
    say('Van chua goi duoc docker trong WSL. Mo cmd go: wsl -d ' + WSL_DISTRO + ' -u root docker info', 'err')
  }
  return { ok: false }
}

async function startStack () {
  if (busy) return
  busy = true
  const s = loadSettings()
  try {
    const wp = toWsl(s.repo)
    const chk = await wsh('test -f ' + wp + '/docker-compose.yml && echo CO', { timeout: 60000 })
    if (!/CO/.test(chk.out)) {
      say('Khong thay docker-compose.yml trong ' + s.repo + ' (WSL: ' + wp + ') - bam "Doi..." de chon thu muc dung.', 'err')
      return
    }
    if (!(await ensureWslDocker()).ok) return
    say('docker compose up -d trong ' + wp + ' ...', 'info')
    const t0 = Date.now()
    const r = await wsh('cd ' + wp + ' && docker compose up -d', { timeout: 1800000 })
    if (!r.ok) { say('Lỗi khởi động: ' + (r.err || r.out).slice(0, 600), 'err'); return }
    say('Stack đã lên trong ' + Math.round((Date.now() - t0) / 1000) + 's', 'ok')
    const st = await status()
    say(st.upCount + '/5 service đang chạy', st.upCount === 5 ? 'ok' : 'warn')
    if (s.autoOpenStudio !== false && st.upCount >= 1) openStudio()
  } finally {
    busy = false
    refresh()
  }
}

async function stopStack () {
  if (busy) return
  busy = true
  try {
    const r = await wsh('cd ' + toWsl(loadSettings().repo) + ' && docker compose down', { timeout: 600000 })
    say(r.ok ? 'Đã dừng toàn bộ stack.' : 'Lỗi: ' + (r.err || r.out).slice(0, 300), r.ok ? 'ok' : 'err')
  } finally {
    busy = false
    refresh()
  }
}

async function rebuildVideo () {
  if (busy) return
  busy = true
  try {
    say('Rebuild riêng video-service (4 service kia không bị khởi động lại) ...', 'info')
    const r = await wsh('cd ' + toWsl(loadSettings().repo) + ' && docker compose build video-service && docker compose up -d video-service', { timeout: 3600000 })
    say(r.ok ? 'Xong.' : 'Lỗi: ' + (r.err || r.out).slice(0, 400), r.ok ? 'ok' : 'err')
  } finally {
    busy = false
    refresh()
  }
}

async function pruneCache () {
  if (busy || !controlWin) return
  const yes = dialog.showMessageBoxSync(controlWin, {
    type: 'warning',
    title: 'Dọn build cache',
    message: 'Xoá toàn bộ Docker build cache?',
    detail: 'Chỉ là cache build, không đụng image/container đang chạy. Lần build kế tiếp sẽ chậm hơn.\n(Đo thực tế: lệnh này lấy lại 53.79 GB.)',
    buttons: ['Xoá cache', 'Huỷ']
  })
  if (yes !== 0) return
  busy = true
  try {
    say('docker builder prune -f ...', 'info')
    const r = await wsh('docker builder prune -f', { timeout: 900000 })
    const m = /Total reclaimed space: (\S+)/.exec(r.out)
    say(m ? 'Đã lấy lại ' + m[1] : 'Đã chạy prune (không thấy con số).', r.ok ? 'ok' : 'err')
  } finally {
    busy = false
    refresh()
  }
}

async function showLogs (name) {
  // name den tu renderer -> BAT buoc phai trung mot ten service trong danh sach.
  // Ghep chuoi tu do vao lenh shell la duong cong quen thu cong cua injection.
  const sv = SERVICES.find((x) => x.name === name)
  if (!sv) { say('Tên service không hợp lệ: ' + String(name).slice(0, 40), 'err'); return }
  const r = await wsh('docker logs --tail 200 ' + sv.name + ' 2>&1', { timeout: 90000 })
  say('--- log ' + sv.name + ' ---', 'info')
  for (const line of (r.out || r.err).split(/\r?\n/).slice(-40)) if (line.trim()) say(line, 'raw')
}

function openStudio () {
  if (studioWin && !studioWin.isDestroyed()) { studioWin.focus(); return }
  studioWin = new BrowserWindow({ width: 1460, height: 940, minWidth: 1024, minHeight: 680, title: 'OmniDub Studio', autoHideMenuBar: true })
  studioWin.loadURL('http://localhost:7860')
  studioWin.on('closed', () => { studioWin = null })
}

// --- polling ---
let timer = null
async function refresh () {
  try {
    const st = await status()
    if (controlWin && !controlWin.isDestroyed()) controlWin.webContents.send('status', st)
    if (tray) tray.setToolTip('OmniDub Studio - ' + st.upCount + '/5 service')
    return st
  } catch (e) { return null }
}

// --- cua so + khay ---
function createControlWindow () {
  controlWin = new BrowserWindow({
    width: 580,
    height: 800,
    minWidth: 480,
    minHeight: 620,
    title: 'OmniDub Studio - Điều khiển',
    backgroundColor: '#0f1420',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false }
  })
  controlWin.loadFile(path.join(__dirname, 'index.html'))
  controlWin.on('close', (e) => {
    // Dong cua so chi thu nho ra khay de stack van chay duoi nen
    if (!app.isQuitting) { e.preventDefault(); controlWin.hide() }
  })
}

function makeTray () {
  try {
    const { nativeImage } = require('electron')
    const ico = path.join(__dirname, 'icon.png')
    tray = new Tray(fs.existsSync(ico) ? ico : nativeImage.createEmpty())
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Mở Studio', click: () => openStudio() },
      { label: 'Hiện bảng điều khiển', click: () => { controlWin.show(); controlWin.focus() } },
      { type: 'separator' },
      { label: 'Dừng stack', click: () => stopStack() },
      { label: 'Thoát', click: () => { app.isQuitting = true; app.quit() } }
    ]))
    tray.on('double-click', () => { controlWin.show() })
  } catch (e) { /* khay co the bi tat tren vai Windows - khong phai loi nghiem trong */ }
}

// --- IPC ---
ipcMain.handle('status', () => status())
ipcMain.handle('start', () => startStack())
ipcMain.handle('stop', () => stopStack())
ipcMain.handle('rebuild', () => rebuildVideo())
ipcMain.handle('prune', () => pruneCache())
ipcMain.handle('logs', (e, name) => showLogs(name))
ipcMain.handle('studio', () => openStudio())
ipcMain.handle('openRepo', () => { shell.openPath(loadSettings().repo); return true })
ipcMain.handle('pickRepo', async () => {
  const r = await dialog.showOpenDialog(controlWin, { properties: ['openDirectory'], title: 'Chọn thư mục OmniDub Studio' })
  if (!r.canceled && r.filePaths[0]) {
    const s = loadSettings()
    s.repo = r.filePaths[0]
    saveSettings(s)
    return s.repo
  }
  return loadSettings().repo
})
ipcMain.handle('settings', (e, patch) => { const s = Object.assign(loadSettings(), patch || {}); saveSettings(s); return s })

// --- vong doi app ---
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (controlWin) { controlWin.show(); controlWin.focus() } })
  app.whenReady().then(() => {
    createControlWindow()
    makeTray()
    timer = setInterval(refresh, 8000)
    refresh()
    if (SMOKE) {
      setTimeout(async () => {
        const st = await refresh()
        console.log('SMOKE status: docker=' + (st && st.docker) + (st && st.dockerErr ? ' (' + st.dockerErr.slice(0, 90) + ')' : '') +
          ' up=' + (st && st.upCount) + ' services=' + (st ? st.services.map((x) => x.name + ':' + x.state).join(',') : 'null'))
        app.exit(0)
      }, 6000)
    }
  })
  app.on('before-quit', () => { app.isQuitting = true })
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createControlWindow() })
}
