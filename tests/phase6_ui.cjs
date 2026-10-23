const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js', 'utf8');
const calls = [], notices = [], rendered = [];
let responses = [];
const buttons = [];
const messagesEl = {
  innerHTML: '', scrollTop: 20,
  querySelectorAll: () => [], querySelector: () => null,
  prepend: el => buttons.unshift(el), append: el => buttons.push(el),
};
function element(tag) {
  return {tag, dataset: {}, listeners: {}, disabled: false,
    setAttribute() {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    replaceWith(other) { this.replacement = other; }};
}
const context = vm.createContext({
  URL, URLSearchParams, location: {href: 'http://localhost:5174/'},
  state: {activeId: 'chat', activeSpecialView: null, providerSide: 'claude'},
  messagesEl, document: {createElement: element},
  apiConversation: async (id, params) => {
    calls.push({id, params});
    const response = responses.shift();
    return typeof response === 'function' ? response() : response;
  },
  renderConversationMessages: (...args) => rendered.push(args),
  showAppNotice: message => notices.push(message),
});
const pager = source.slice(source.indexOf('function renderConversationPager()'), source.indexOf('async function openConversation('));
vm.runInContext(pager, context);
const run = code => vm.runInContext(code, context);
const page = {conversation: {id: 'chat'}, messages: [{seq: 500}], artifacts: {}, total: 600, offset: 500, previous_offset: 400, next_offset: null, limit: 100};
(async () => {
  responses = [page];
  assert.equal(await run('ensureConversationMessage(500)'), true);
  assert.equal(calls[0].params.target_seq, 500);
  assert.equal(buttons[0].textContent, 'Previous messages');
  assert.equal(rendered[0][0][0].seq, 500);
  responses = [{error: 'Could not load.'}];
  assert.equal(await run('showConversationPage({offset: 400})'), false);
  assert.equal(notices.at(-1), 'Could not load.');
  // A slower page response must not replace a later request or another chat.
  let finish;
  responses = [() => new Promise(resolve => { finish = resolve; }), {...page, offset: 200}];
  const old = run('showConversationPage({offset: 300})');
  await run('showConversationPage({offset: 200})');
  finish({...page, offset: 300});
  await old;
  assert.equal(run('state.messagePage.offset'), 200);
  responses = [() => new Promise(resolve => { finish = resolve; })];
  const stale = run('showConversationPage({offset: 0})');
  run('state.activeId = "another"');
  finish(page);
  assert.equal(await stale, false);

  // The actual provider toggle reloads the gallery and invalidates old pages.
  context.state.activeSpecialView = 'gallery';
  context.state.mediaHub = {data: {provider: 'claude'}};
  context.state.recycleSelected = new Set();
  context.state.view = 'recent';
  context.galleryGrid = {innerHTML: ''};
  context.attReportContent = {dataset: {}};
  for (const name of ['renderProviderToggle', 'saveUiPreferences', 'refreshPinnedList', 'loadFolders', 'loadConversations', 'syncUnverifiedOption']) context[name] = () => {};
  context.isClaudeSide = () => context.state.providerSide === 'claude';
  context.claudeModelsMenuItem = null;
  let galleryReloads = 0;
  context.openGallery = async () => {
    assert.equal(context.state.mediaHub.data, null);
    assert.equal(context.state.providerSide, 'chatgpt');
    galleryReloads++;
  };
  const providerSwitch = source.slice(source.indexOf('async function switchProviderSide('), source.indexOf('\ndocument\n  .querySelectorAll("#provider-toggle'));
  vm.runInContext(providerSwitch, context);
  await run('switchProviderSide("chatgpt")');
  assert.equal(galleryReloads, 1);
  // A Load more button from the previous provider never starts a request.
  context.apiRequest = () => { throw new Error('Stale gallery requested'); };
  const loadMore = source.slice(source.indexOf('async function loadMoreMedia('), source.indexOf('let galleryRequest ='));
  vm.runInContext(loadMore, context);
  await run('loadMoreMedia({provider:"claude", next_offset:100})');

  let click;
  context.document.addEventListener = (name, fn) => {click = fn;};
  const consent = source.slice(source.indexOf('// Consent is per image'), source.indexOf('// ── Utilities'));
  vm.runInContext(consent, context);
  const button = element('button');
  button.dataset = {remoteImage: 'https://example.test/image.png', imageAlt: 'Picture'};
  click({target: {closest: () => button}});
  const image = button.replacement;
  assert.equal(image.src, 'https://example.test/image.png');
  assert.equal(image.referrerPolicy, 'no-referrer');
  image.listeners.error();
  assert.equal(image.replacement, button);
  assert(notices.at(-1).includes("couldn't be loaded"));
  console.log('Phase 6 UI behavior passed.');
})().catch(err => {console.error(err); process.exitCode = 1;});
