const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function run(delay = '') {
  const source = fs.readFileSync(process.argv[2], 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const status = {};
  const speaker = {play: async () => {}};
  const events = {};
  const calls = [];
  let heartbeat, peer, stopped = 0, fragmentRemoved = false;
  let releaseMic, releaseAdmission, releaseDescription;
  const track = {stop() { stopped++; }};
  const mic = {getAudioTracks: () => [track], getTracks: () => [track]};
  class Peer {
    constructor() { peer = this; this.iceGatheringState = 'complete'; }
    createDataChannel() {}
    addTrack(value) { assert.equal(value, track); }
    async createOffer() { return {}; }
    async setLocalDescription() { this.localDescription = {sdp: 'fixture-offer'}; }
    async setRemoteDescription(value) {
      assert.deepEqual({...value}, {type: 'answer', sdp: 'fixture-answer'});
      this.connectionState = 'connected';
      this.onconnectionstatechange();
      if (delay === 'description') await new Promise(resolve => { releaseDescription = resolve; });
    }
    close() { this.closed = true; }
  }
  vm.runInNewContext(source, {
    document: {getElementById: id => id === 'status' ? status : speaker},
    location: {hash: '#access=fixture-key', pathname: '/'}, URLSearchParams,
    history: {replaceState() { fragmentRemoved = true; }},
    addEventListener: (event, listener) => { events[event] = listener; },
    navigator: {mediaDevices: {getUserMedia: async () => delay === 'microphone'
      ? new Promise(resolve => { releaseMic = () => resolve(mic); }) : mic}},
    RTCPeerConnection: Peer, MediaStream: class {},
    setTimeout, clearTimeout, clearInterval() {},
    setInterval(fn) { heartbeat = fn; },
    fetch: async (_url, opts) => {
      const body = JSON.parse(opts.body);
      assert.equal(opts.headers['X-AgentRoute-Token'], 'fixture-key');
      calls.push(body);
      if (body.action === 'start' && delay === 'admission') {
        await new Promise(resolve => { releaseAdmission = resolve; });
      }
      return {ok: true, json: async () => body.action === 'start'
        ? {session: 'fixture-session', sdp: 'fixture-answer'} : {state: 'active'}};
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fragmentRemoved, true);
  if (delay) {
    events.pagehide();
    if (releaseMic) releaseMic();
    if (releaseAdmission) releaseAdmission();
    if (releaseDescription) releaseDescription();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(heartbeat, undefined, 'ended startup must not create a heartbeat');
    assert.equal(stopped, 1, 'late microphone must stop');
    assert.equal(speaker.srcObject, null);
    if (delay === 'microphone') assert.equal(calls.length, 0);
    else {
      assert.equal(calls.filter(value => value.action === 'stop').length, 1);
      assert.equal(peer.closed, true);
    }
    return;
  }
  assert.deepEqual(calls, [{action: 'start', sdp: 'fixture-offer'}]);
  assert.equal(status.textContent, 'Listening');
  await heartbeat();
  assert.deepEqual(calls[1], {action: 'status', session: 'fixture-session'});
  events.pagehide();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls[2], {action: 'stop', session: 'fixture-session'});
  assert.equal(peer.closed, true);
  assert.equal(stopped, 1);
  assert.equal(speaker.srcObject, null);
}

(async () => {
  for (const delay of ['', 'microphone', 'admission', 'description']) await run(delay);
})().catch(error => { console.error(error); process.exitCode = 1; });
