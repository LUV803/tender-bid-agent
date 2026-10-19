const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('bidding', {
  isDesktop: true,
  openBidFolder: (folder) => ipcRenderer.invoke('bidding:open-bid-folder', folder),
})
