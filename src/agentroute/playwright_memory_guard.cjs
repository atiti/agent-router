'use strict';

// Apply the retention fix in memory; leave the installed upstream package intact.
const Module = require('node:module');
const fs = require('node:fs');
const original = Module._extensions['.js'];
const append = (array, value, limit) => {
  if (array.length >= limit)
    array.splice(0, Math.max(1, Math.floor(limit / 4)));
  array.push(value);
};
globalThis.__agentrouteBoundedPlaywrightAppend = append;

Module._extensions['.js'] = function guarded(module, filename) {
  if (!filename.endsWith('/playwright-core/lib/coreBundle.js'))
    return original(module, filename);
  let source = fs.readFileSync(filename, 'utf8');
  const marker = 'Tab = class _Tab extends';
  const start = source.indexOf(marker);
  const end = source.indexOf('async captureSnapshot(', start);
  if (start < 0 || end < start)
    throw new Error('AgentRoute: Playwright retention guard requires a compatible Tab implementation.');
  let tab = source.slice(start, end);
  let requests = 0;
  let events = 0;
  tab = tab.replace(/this\._requests\.push\((request\w*)\);/g, (_, value) => {
    ++requests;
    return `globalThis.__agentrouteBoundedPlaywrightAppend(this._requests, ${value}, 1024);`;
  });
  tab = tab.replace(/this\._recentEventEntries\.push\(entry\);/g, () => {
    ++events;
    return 'globalThis.__agentrouteBoundedPlaywrightAppend(this._recentEventEntries, entry, 2048);';
  });
  if (!requests || events !== 1)
    throw new Error('AgentRoute: Playwright retention guard could not locate request/event collectors.');
  source = source.slice(0, start) + tab + source.slice(end);
  module._compile(source, filename);
};
