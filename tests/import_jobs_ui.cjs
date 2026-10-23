// Exercise the actual import-job controller with a small DOM and API boundary.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js', 'utf8');
const controller = source.slice(source.indexOf('const importCancelBtn ='), source.indexOf('\nfunction renderImportSummary(data)'));
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {hidden: true, disabled: false, textContent: '', listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; }});
  return elements.get(id);
}
let responses = [], calls = [], summaries = [], refreshes = 0;
const context = vm.createContext({
  $, state: {importNew: {}},
  importNewBtn: $('import-new-btn'), importReviewAllBtn: $('import-review-all'),
  importReviewNoneBtn: $('import-review-none'), importReviewSelectedBtn: $('import-review-selected'),
  importNewStatus: $('import-new-status'), importNewSummary: $('import-new-summary'),
  setTimeout: fn => { fn(); }, encodeURIComponent,
  apiRequest: async (url, options) => {
    calls.push({url, options});
    const next = responses.shift();
    if (typeof next === 'function') return next();
    if (next instanceof Error) throw next;
    assert(next, 'Unexpected API call: ' + url);
    return next;
  },
  loadImportHistory: async () => { refreshes++; }, loadReviewArea: async () => {},
  loadConversations: async () => {}, renderImportSummary: data => summaries.push(data),
  openConfirm: async opts => { assert(opts.text.includes('incomplete')); assert.equal(opts.okLabel, 'Rebuild'); return false; },
});
vm.runInContext(controller, context);
async function run(code) { return await vm.runInContext(code, context); }
(async () => {
 const running = {id: 'a', kind: 'import', status: 'running', progress: 20, total: 60, message: 'Saving chats…'};
 responses = [{job: running}, () => {
   assert.equal($('import-cancel-btn').hidden, false);
   assert.equal($('import-new-btn').disabled, true);
   assert($('import-new-status').textContent.includes('20 of 60'));
   return {job: {...running, status: 'completed', result: {added: 60}, message: 'Finished.'}};
 }];
 await run('startImportJob("/api/import-new", {}, "Import")');
 assert.equal(summaries[0].added, 60);
 assert.equal($('import-cancel-btn').hidden, true);
 assert.equal($('import-new-btn').disabled, false);
 assert.equal($('import-job-spinner').hidden, true);
 assert.equal(calls[0].options.method, 'POST');
 assert.equal(refreshes, 1);
 // Reopening a cancelled job reports retained data, with no success summary.
 await run('followImportJob({id:"b", kind:"import", status:"cancelled", message:"Cancelled. Chats already saved are kept."})');
 assert($('import-new-status').textContent.includes('already saved are kept'));
 assert.equal(summaries.length, 1);
 // A polling failure stops its spinner, reenables controls, and reports failure.
 responses = [{job: {...running, id: 'c'}}, new Error('offline')];
 await run('startImportJob("/api/import-new", {}, "Import")');
 assert($('import-new-status').textContent.includes('Reopen this screen'));
 assert.equal($('import-new-btn').disabled, false);
 assert.equal($('import-cancel-btn').hidden, true);
 // Cancellation sends the job id, rather than aborting the polling fetch.
 let unblock;
 responses = [{job: {...running, id: 'cancel-id'}}, () => new Promise(resolve => {unblock=resolve;})];
 const pending = run('startImportJob("/api/import-new", {}, "Import")');
 while (!unblock) await Promise.resolve();
 responses = [{job: {...running, id: 'cancel-id'}}];
 await $('import-cancel-btn').listeners.click();
 assert.equal(calls.at(-1).url, '/api/jobs/cancel-id/cancel');
 assert.equal(calls.at(-1).options.method, 'POST');
 unblock({job: {...running, id: 'cancel-id', status: 'cancelled', message: 'Cancelled. Chats already saved are kept.'}});
 await pending;
 await $('import-rebuild-btn').listeners.click();
 console.log('Import job UI behavior passed.');
})().catch(err => {console.error(err); process.exitCode=1;});
