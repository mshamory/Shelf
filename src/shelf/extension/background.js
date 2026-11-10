"use strict";
// Keyboard shortcut "Send to Shelf automatically": open the popup in quick mode. The popup
// sends the page by itself when Shelf can tell which novel and chapter it is, and stays
// open as the normal menu otherwise. (Browsers only let the popup open from the shortcut
// handler itself, so the popup does the work rather than this script.)
const api = globalThis.browser ?? globalThis.chrome;
let quickAt = 0;

api.commands.onCommand.addListener((command) => {
  if (command !== "quick-send") return;
  quickAt = Date.now();
  const opened = api.action.openPopup();
  if (opened && opened.catch) opened.catch(() => { quickAt = 0; });
});

api.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type !== "take-quick") return false;
  const quick = Date.now() - quickAt < 5000;
  quickAt = 0;
  reply({ quick });
  return false;
});
