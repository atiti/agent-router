const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const elements = new Map();
function node(id) {
  if (!elements.has(id)) elements.set(id, {
    listeners: {},
    addEventListener(name, cb) { this.listeners[name] = cb; },
    showModal() { this.open = true; },
    close() { this.open = false; this.listeners.close?.(); },
    focus() {}, replaceChildren() {}, append() {},
    querySelectorAll() { return []; }, querySelector() {},
  });
  return elements.get(id);
}

const storage = new Map();
const sent = [];
const owner = "01a20744-50ee-71a2-a6a3-3e26905fc41b";
const lanes = {
  you: "custom", unowned: "triage", coordination: "coordination",
  worker: "details", external: "dependency",
};
const entries = [...Object.keys(lanes), "second", "third"].map(item => ({
  namespace: "private", packet: "demo", item, title: `Work ${item}`, kind: item,
  type: "outcome", revision: 1, blocking: true, reason: "Recorded blocker",
}));
const byItem = item => entries.find(entry => entry.item === item);
const context = vm.createContext({
  document: {getElementById: node, createElement: () => ({append() {}}), addEventListener() {}},
  location: {hash: "#focus"}, history: {replaceState() {}},
  EventSource: class {close() {}}, setInterval: () => 1, clearInterval() {},
  URLSearchParams, AbortSignal,
  sessionStorage: {
    getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  },
  crypto: {randomUUID: () => "request-" + sent.length},
  fetch: async (url, options) => {
    if (options.method === "POST") {
      sent.push(JSON.parse(options.body));
      return {ok: true, json: async () => ({id: "receipt", state: "delivered"})};
    }
    const item = new URLSearchParams(url.split("?")[1]).get("item");
    const route = item === "unowned" ? {kind: "review"} : {kind: "owner", thread: owner};
    return {ok: true, json: async () => ({receipts: [], route})};
  },
});
const run = code => vm.runInContext(code, context);
const click = id => node(id).listeners.click();
const settle = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  run(fs.readFileSync(process.argv[2], "utf8").split("<script>").at(-1).split("</script>")[0]);
  context.fixture = {
    agents: [],
    focus: {
      needs_you: [byItem("you"), byItem("second"), byItem("third")],
      unowned: [byItem("unowned")],
      others: ["coordination", "worker", "external"].map(kind => ({kind, items: [byItem(kind)]})),
    },
    packets: [{namespace: "private", id: "demo", items: entries.map(entry => ({
      id: entry.item, objective: entry.title, revision: 1, status: "blocked",
      session: entry.item === "unowned" ? null : owner,
    }))}],
  };
  run('S.snap = fixture; packetActionToken = "local"; openFocusReview("private/demo/you")');
  assert.equal(node("focus-review-title").textContent, "Work you");
  run("S.snap.focus.needs_you.reverse(); renderFocusReview()");
  assert.equal(node("focus-review-title").textContent, "Work you");
  run('S.snap.focus.needs_you = S.snap.focus.needs_you.filter(e => e.item !== "second")');
  click("focus-next");
  assert.equal(node("focus-review-title").textContent, "Item updated");
  click("focus-next");
  assert.equal(node("focus-review-title").textContent, "Work third");
  assert.equal(node("focus-next").disabled, true);
  node("focus-review").close();

  for (const [lane, template] of Object.entries(lanes)) {
    context.row = byItem(lane);
    const buttons = run('focusActions(row, packetOutcome(row.namespace,row.packet,row.item))');
    assert.match(buttons, new RegExp(`data-reply-template="${template}"`));
    const dataset = Object.fromEntries(
      [...buttons.matchAll(/data-(namespace|packet|item)="([^"]+)"/g)].map(match => match.slice(1))
    );
    context.button = {dataset: {...dataset, replyTemplate: template}};
    const count = sent.length;
    run("openPacketReply(button)");
    await settle();
    assert.equal(sent.length, count);
    assert.equal(node("reply-template").value, template);
    if (template === "custom") node("reply-message").value = "My answer";
    await node("reply-form").listeners.submit({preventDefault() {}});
    assert.equal(sent.length, count + 1);
    assert.equal(sent.at(-1).item, lane);
    assert.equal(sent.at(-1).recipient, lane === "unowned" ? undefined : owner);
    await settle();
    node("packet-reply").close();
  }
  run('packetActionToken = ""');
  assert.match(run("focusView()"), /Read-only view/);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
