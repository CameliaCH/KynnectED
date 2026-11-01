// Run with node, or macOS JavaScriptCore's jsc from the repository root.
const read = path => typeof readFile === 'function' ? readFile(path) : require('fs').readFileSync(path, 'utf8');
const source = read('static/js/room.js');
const log = typeof print === 'function' ? print : console.log;
function assert(value, message) { if (!value) throw new Error(message); }
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

class Element {
  constructor(fragment = false) {
    this.fragment = fragment;
    this.children = [];
    this.dataset = {};
    this.handlers = {};
    this.scrollHeight = this.scrollTop = this.clientHeight = 0;
  }
  append(...items) {
    for (const item of items) {
      if (item.fragment) this.append(...item.children);
      else { item.parent = this; this.children.push(item); }
    }
  }
  prepend(item) {
    const previous = this.children;
    this.children = [];
    this.append(item, ...previous);
  }
  remove() { this.parent.children = this.parent.children.filter(item => item !== this); }
  querySelector(selector) {
    const id = selector.match(/data-message-id="(\d+)"/)[1];
    return this.children.find(item => String(item.dataset.messageId) === id);
  }
  closest() { return this.dataset.deleteMessage ? this : null; }
  contains(item) { return item === this || this.children.some(child => child.contains(item)); }
  setAttribute() {}
  addEventListener(type, handler) { this.handlers[type] = handler; }
}

function browser(coordinator, initialIds = []) {
  const elements = Object.fromEntries(['chat', 'chat-messages', 'chat-older', 'chat-status', 'chat-empty'].map(id => [id, new Element()]));
  const state = { confirm: true, replies: [], requests: [] };
  elements.chat.dataset = { messagesUrl: '/messages', deleteMessagesUrl: '/coordinator/messages', userId: '3', csrf: 'test-token', canDelete: String(coordinator) };
  for (const id of initialIds) {
    const item = new Element();
    item.dataset.messageId = String(id);
    elements['chat-messages'].append(item);
  }
  const document = { hidden: false, querySelectorAll: () => [], getElementById: id => elements[id],
    createElement: () => new Element(), createDocumentFragment: () => new Element(true), addEventListener() {} };
  const window = { location: { hash: '' }, addEventListener() {}, confirm: () => state.confirm };
  const fetch = async (url, options = {}) => {
    state.requests.push({ url, ...options });
    const reply = state.replies.shift() || { messages: [] };
    return { ok: !reply.error, status: reply.error ? 500 : 200, json: async () => reply };
  };
  new Function('document', 'window', 'fetch', 'setInterval', source)(document, window, fetch, fn => { state.poll = fn; });
  return { elements, state, list: elements['chat-messages'] };
}

const message = id => ({ id, sender_id: 1, sender: 'Mentor', time: '12:00', body: `Message ${id}` });
(async () => {
  const existingRoom = browser(true, [10, 11, 12]);
  const otherRoom = browser(true, [20, 21]);
  await settle();
  assert(existingRoom.list.children.length === 3 && otherRoom.list.children.length === 2,
    'Initial polling preserves existing server-rendered conversations in different rooms');
  assert(existingRoom.elements['chat-empty'].hidden && otherRoom.elements['chat-empty'].hidden,
    'Existing conversations must not show the empty message notice');
  const coordinator = browser(true);
  const { state, list, elements } = coordinator;
  await settle();
  state.replies.push({ messages: [message(50)] });
  state.poll();
  await settle();
  const button = list.children[0].children[2];
  assert(button.dataset.deleteMessage === 50, 'New messages have coordinator delete controls');
  state.confirm = false;
  const count = state.requests.length;
  await list.handlers.click({ target: button });
  assert(state.requests.length === count, 'Cancelling must not send a delete request');
  state.confirm = true;
  state.replies.push({ error: 'Please try again.' });
  await list.handlers.click({ target: button });
  assert(list.children.length === 1 && !button.disabled, 'Failed deletion keeps the message and allows retry');
  state.replies.push({ messages: [], deleted_ids: [50] });
  await list.handlers.click({ target: button });
  const request = state.requests[state.requests.length - 1];
  assert(request.url === '/coordinator/messages/50/delete' && request.method === 'POST', 'Delete targets the selected message through the coordinator endpoint');
  assert(request.headers['X-CSRF-Token'] === 'test-token', 'Delete includes CSRF protection');
  assert(list.children.length === 0 && !elements['chat-empty'].hidden, 'Successful deletion removes the message');
  state.replies.push({ messages: [message(49), message(50)] });
  await elements['chat-older'].handlers.click();
  assert(state.requests[state.requests.length - 1].url.includes('before=50'), 'History retains a valid cursor after all displayed messages are deleted');
  assert(list.children.length === 1 && list.children[0].dataset.messageId === 49, 'Stale history cannot restore a deleted message');

  const participant = browser(false);
  await settle();
  participant.state.replies.push({ messages: [message(1), message(2)] });
  participant.state.poll();
  await settle();
  assert(!participant.list.handlers.click && participant.list.children[0].children.length === 2, 'Participants receive no delete controls');
  participant.state.replies.push({ messages: [message(1)], deleted_ids: [1, 2], deletion_cursor: 7 });
  participant.state.poll();
  await settle();
  assert(participant.list.children.length === 0, 'Polling removes messages deleted by a coordinator, including stale results');
  participant.state.poll();
  await settle();
  assert(participant.state.requests.slice(-1)[0].url.includes('deletions_after=7'), 'Polling advances the deletion cursor');
  log('Chat deletion browser checks passed.');
})().catch(error => { log(error.stack); if (typeof quit === 'function') quit(1); else process.exitCode = 1; });
