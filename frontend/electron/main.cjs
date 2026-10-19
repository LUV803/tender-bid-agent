const { app, BrowserWindow, ipcMain, shell } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const isDev = process.argv.includes('--dev')

/**
 * The renderer may only ask Electron to reveal directories that live under
 * upload/bids.  Paths are resolved before comparison and symlink-free by
 * construction because the backend returns the canonical directory.
 */
function bidsRoot() {
  const projectRoot = path.resolve(app.getAppPath(), '..')
  const uploadDir = process.env.UPLOAD_DIR
    ? path.resolve(projectRoot, process.env.UPLOAD_DIR)
    : path.join(projectRoot, 'upload')
  return path.join(uploadDir, 'bids')
}

ipcMain.handle('bidding:open-bid-folder', async (_event, folder) => {
  if (typeof folder !== 'string' || !folder.trim()) {
    return { ok: false, error: '无效的文件夹路径' }
  }
  const root = bidsRoot()
  const target = path.resolve(folder)
  const relative = path.relative(root, target)
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
    return { ok: false, error: '只能打开 upload/bids 下的标书产物目录' }
  }
  try {
    const stat = await fs.promises.stat(target)
    if (!stat.isDirectory()) {
      return { ok: false, error: '目标不是文件夹' }
    }
  } catch {
    return { ok: false, error: '文件夹不存在或无法访问' }
  }
  const error = await shell.openPath(target)
  return error ? { ok: false, error } : { ok: true }
})

function createWindow() {
  const win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    title: '标书工坊 · Bidding',
    backgroundColor: '#f4f5f1',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  if (isDev) {
    win.loadURL('http://localhost:5173')
    win.webContents.openDevTools()
  } else {
    win.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

app.whenReady().then(() => {
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
