const {contextBridge, ipcRenderer} = require('electron');
const on = channel => callback => ipcRenderer.on(channel, (_e, ...args) => callback(...args));
contextBridge.exposeInMainWorld('phro', {
  toggleChat: () => ipcRenderer.send('toggle-chat'),
  drag: phase => ipcRenderer.send('overlay-drag', phase),
  nudge: (dx, dy) => ipcRenderer.send('overlay-nudge', dx, dy),
  menu: () => ipcRenderer.send('overlay-menu'),
  resize: (width, height) => ipcRenderer.send('overlay-resize', width, height),
  ignoreMouse: ignore => ipcRenderer.send('overlay-ignore', ignore),
  onboarded: () => ipcRenderer.send('overlay-onboarded'),
  petIcon: (id, dataUrl) => ipcRenderer.send('pet-icon', id, dataUrl),
  // window.screenX is not reliably refreshed while a frameless window moves; main reports moves.
  onMove: on('overlay-move'),
  onCursor: on('overlay-cursor'),
  onSettings: on('overlay-settings'),
  onOpenCompose: on('open-compose'),
  onSelectPet: on('select-pet'),
  onShowHelp: on('show-help'),
});
