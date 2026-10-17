import { app, BrowserWindow, Notification, ipcMain, shell } from 'electron'
import path from 'node:path'

app.setName('KoreAgent Next')
let approvalWindow: BrowserWindow | null = null

function surfaceUrl(hash = '') {
  const developmentUrl = process.env.VITE_DEV_SERVER_URL || (!app.isPackaged ? 'http://127.0.0.1:5173' : '')
  return developmentUrl ? `${developmentUrl}${hash}` : ''
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 980, minHeight: 650, title: 'KoreAgent Next',
    backgroundColor: '#eef3f8',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  })
  const developmentUrl = surfaceUrl()
  if (developmentUrl) win.loadURL(developmentUrl)
  else win.loadFile(path.join(__dirname, '../../dist/index.html'))
}

function showApprovalWindow() {
  if (!approvalWindow) {
    approvalWindow = new BrowserWindow({
      width: 430, height: 390, minWidth: 380, minHeight: 330,
      title: 'KoreAgent Next · 操作确认', alwaysOnTop: true, resizable: true,
      backgroundColor: '#f4f7fb', show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
    })
    const url = surfaceUrl('#approval')
    if (url) void approvalWindow.loadURL(url)
    else void approvalWindow.loadFile(path.join(__dirname, '../../dist/index.html'), { hash: 'approval' })
    approvalWindow.on('closed',()=>{approvalWindow=null})
  }
  approvalWindow.show()
  approvalWindow.focus()
}

app.whenReady().then(() => {
  ipcMain.handle('open-external', (_e, url: string) => shell.openExternal(url))
  ipcMain.handle('notify', (_e, title: string, body: string) => new Notification({ title, body }).show())
  ipcMain.handle('show-approval', () => showApprovalWindow())
  ipcMain.handle('hide-approval', () => approvalWindow?.hide())
  createWindow(); app.on('activate', () => { if (!BrowserWindow.getAllWindows().length) createWindow() })
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })

