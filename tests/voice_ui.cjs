const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.handlers = {};
    this.checked = false;
    this.disabled = false;
    this.muted = false;
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute() {}
  setPointerCapture() {}
}

async function testVoiceUi() {
  const elements = new Map(['start', 'stop', 'mute', 'status', 'speaker', 'push-to-talk',
    'hold', 'receipt', 'receipt-state', 'instruction', 'verified-reply', 'conversation']
    .map(name => [name, new Element()]));
  const events = new Element();
  const document = new Element();
  document.getElementById = name => elements.get(name);
  const track = {enabled: true, stop() { this.enabled = false; }};
  const mic = {getAudioTracks: () => [track], getTracks: () => [track]};
  let heartbeat;
  let connection;

  class Peer {
    constructor() {
      connection = this;
      this.iceGatheringState = 'complete';
    }
    createDataChannel() {}
    addTrack(audio) { assert.equal(audio.enabled, false, 'mic disabled before negotiation'); }
    async createOffer() { return {}; }
    async setLocalDescription() { this.localDescription = {sdp: 'offer'}; }
    async setRemoteDescription() {
      this.connectionState = 'connected';
      this.onconnectionstatechange();
    }
    close() {}
  }

  const fixtureReceipt = {
    session: 'Exact worker', instruction: 'Check the build.', receipt_state: 'read',
    verified_reply: {at: 100, text: 'The build passed.'},
    conversation_url: 'codex://threads/019e60fc-0518-7c21-9596-980fe97225ba',
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8')
    .match(/<script>([\s\S]*?)<\/script>/)[1], {
    document,
    addEventListener: (name, listener) => events.addEventListener(name, listener),
    location: {hash: '#access=fixture'}, URLSearchParams,
    RTCPeerConnection: Peer,
    navigator: {mediaDevices: {getUserMedia: async () => mic}},
    fetch: async (_url, options) => ({
      ok: true,
      json: async () => JSON.parse(options.body).action === 'status'
        ? {state: 'active', receipt: fixtureReceipt} : {session: 'fixture', sdp: 'answer'},
    }),
    clearInterval() {},
    setInterval(listener) { heartbeat = listener; },
  });

  const choice = elements.get('push-to-talk');
  const hold = elements.get('hold');
  const speaker = elements.get('speaker');
  choice.checked = true;
  choice.handlers.change();
  await elements.get('start').handlers.click();
  assert.equal(track.enabled, false, 'idle room speech is muted');
  assert.equal(speaker.muted, false, 'announcements still audible');
  hold.handlers.pointerdown({button: 0, isPrimary: true, pointerId: 1});
  assert.equal(track.enabled, true);
  assert.equal(speaker.muted, true, 'press silences an announcement immediately');
  hold.handlers.pointerup();
  assert.equal(track.enabled, false);
  assert.equal(speaker.muted, false);
  hold.handlers.keydown({code: 'Space', preventDefault() {}});
  assert.equal(track.enabled, true);
  events.handlers.blur();
  assert.equal(track.enabled, false, 'blur releases the mic');
  hold.handlers.keydown({code: 'Enter', preventDefault() {}});
  connection.connectionState = 'disconnected';
  connection.onconnectionstatechange();
  assert.equal(track.enabled, false, 'disconnect releases the mic');

  await heartbeat();
  assert.equal(elements.get('receipt-state').textContent, 'Exact worker · Read by recipient');
  assert.match(elements.get('verified-reply').textContent, /The build passed/);
  assert.equal(elements.get('conversation').href, fixtureReceipt.conversation_url);
}

testVoiceUi().catch(error => { console.error(error); process.exitCode = 1; });

function testInstalledAppAccess() {
  const saved = new Map();
  const storage = {
    getItem: key => saved.get(key) || null,
    setItem: (key, value) => saved.set(key, value),
  };
  function launch() {
    const names = ['start', 'stop', 'mute', 'status', 'speaker', 'push-to-talk', 'hold',
      'receipt', 'receipt-state', 'instruction', 'verified-reply', 'conversation',
      'access-setup', 'access-link', 'save-access'];
    const elements = new Map(names.map(name => [name, new Element()]));
    const document = new Element();
    document.getElementById = name => elements.get(name);
    vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8')
      .match(/<script>([\s\S]*?)<\/script>/)[1], {
      document, addEventListener() {}, location: {hash: '', origin: 'https://voice.example'},
      URL, URLSearchParams, localStorage: storage, clearInterval() {}, setInterval() {},
    });
    return elements;
  }
  const first = launch();
  assert.equal(first.get('access-setup').hidden, false);
  assert.equal(first.get('start').disabled, true);
  first.get('access-link').value = 'https://other.example/#access=wrong';
  first.get('save-access').handlers.click();
  assert.equal(saved.size, 0);
  first.get('access-link').value = 'https://voice.example/#access=private-key';
  first.get('save-access').handlers.click();
  assert.equal(first.get('access-setup').hidden, true);
  assert.equal(first.get('start').disabled, false);
  assert.equal(first.get('access-link').value, '');
  const reopened = launch();
  assert.equal(reopened.get('access-setup').hidden, true);
  assert.notEqual(reopened.get('start').disabled, true);
}

testInstalledAppAccess();
