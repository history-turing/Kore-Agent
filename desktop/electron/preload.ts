import { contextBridge, ipcRenderer } from 'electron'
contextBridge.exposeInMainWorld('koreNext', {
  openExternal: (url: string) => ipcRenderer.invoke('open-external', url),
  notify: (title: string, body: string) => ipcRenderer.invoke('notify', title, body),
  showApproval: () => ipcRenderer.invoke('show-approval'),
  hideApproval: () => ipcRenderer.invoke('hide-approval'),
  chooseWorkspace: () => ipcRenderer.invoke('choose-workspace'),
})
