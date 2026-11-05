// Cau noi giua giao dien (renderer, bi cach ly) va tien tri (main, co quyen chay
// lenh). Renderer KHONG duoc phep tiep can Node truc tiep.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('api', {
  status: () => ipcRenderer.invoke('status'),
  start: () => ipcRenderer.invoke('start'),
  stop: () => ipcRenderer.invoke('stop'),
  rebuild: () => ipcRenderer.invoke('rebuild'),
  prune: () => ipcRenderer.invoke('prune'),
  logs: (name) => ipcRenderer.invoke('logs', name),
  studio: () => ipcRenderer.invoke('studio'),
  openRepo: () => ipcRenderer.invoke('openRepo'),
  pickRepo: () => ipcRenderer.invoke('pickRepo'),
  settings: (patch) => ipcRenderer.invoke('settings', patch),
  onLog: (cb) => ipcRenderer.on('log', (e, d) => cb(d)),
  onStatus: (cb) => ipcRenderer.on('status', (e, d) => cb(d))
})
