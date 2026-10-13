/* Run the dashboard's two chat senders for real, in node, against a canned
   SSE reply and a stub DOM.

   It exists because of one line. The Test tab of a deploy (`sendPlay`) carried
   `data-state="${isPool ? "judging" : "thinking"}"`, copied from `sendChat`
   where `isPool` is a local. Here it was an unbound identifier, so every
   message raised a ReferenceError inside the try -- before the assistant
   bubble was appended -- and the catch reported "Could not reach server" about
   a model that had already answered in 1.7 s. The tab had never worked, on any
   machine, and `node --check` cannot see it: the syntax is valid and the
   failure is at runtime. Only running the thing finds it.

   No npm, no jsdom: the dashboard has no build step and the CI has node. */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "web", "app.js");

/* --- the smallest DOM that app.js can run against ------------------------ */

function el(id) {
  const self = {
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    checked: false,
    disabled: false,
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
    children: [],
    style: {},
    dataset: {},
    files: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    appendChild(c) { self.children.push(c); return c; },
    removeChild() {},
    remove() {},
    insertAdjacentHTML() {},
    focus() {},
    click() {},
    setAttribute() {},
    getAttribute() { return null; },
    closest() { return null; },
    /* one stable element per selector, so that `contentEl.textContent +=`
       accumulates somewhere the test can read back */
    _q: new Map(),
    querySelector(sel) {
      if (!self._q.has(sel)) self._q.set(sel, el(id + sel));
      return self._q.get(sel);
    },
    querySelectorAll() { return []; },
  };
  return self;
}

const made = new Map();
const byId = (id) => {
  if (!made.has(id)) made.set(id, el(id));
  return made.get(id);
};

const sandbox = {
  console,
  TextDecoder,
  TextEncoder,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {},
  Date,
  JSON,
  Math,
  document: {
    getElementById: byId,
    createElement: () => el("new"),
    querySelector: () => el("q"),
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: el("body"),
    documentElement: el("html"),
  },
  localStorage: {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  },
  location: { href: "http://localhost:3000/", hostname: "localhost", origin: "http://localhost:3000", search: "" },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: "node" },
  fetch: async () => { throw new Error("no fetch stubbed yet"); },
  alert: () => {},
  requestAnimationFrame: (f) => f(),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

/* --- load app.js, then reach into its scope ------------------------------ */

const source = fs.readFileSync(SRC, "utf8") + `
;globalThis.__harness = {
  sendPlay, sendChat,
  setDetail(v) { detailId = v; },
  play: () => document.getElementById("playMessages"),
  chat: () => document.getElementById("chatMessages"),
};`;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "web/app.js" });
const H = sandbox.__harness;

/* --- the canned reply ---------------------------------------------------- */

function sseReply(text) {
  const frames = text.split(" ").map(
    (w) => `data: ${JSON.stringify({ choices: [{ delta: { content: w + " " } }] })}\n\n`
  );
  frames.push("data: [DONE]\n\n");
  let i = 0;
  return {
    ok: true,
    status: 200,
    json: async () => ({}),
    body: {
      getReader: () => ({
        read: async () =>
          i < frames.length
            ? { done: false, value: new TextEncoder().encode(frames[i++]) }
            : { done: true, value: undefined },
      }),
    },
  };
}

/* --- the tests ----------------------------------------------------------- */

const failures = [];
const check = (name, cond, detail) => {
  if (!cond) failures.push(`${name}: ${detail}`);
  else console.log(`  ok  ${name}`);
};

async function run(label, send, prepare) {
  const bubbles = [];
  sandbox.fetch = async () => sseReply("Hey there");
  prepare();
  /* the catch in both senders swallows everything, so watch what it renders */
  const box = label === "sendPlay" ? H.play() : H.chat();
  box.children.length = 0;
  const seen = [];
  const original = box.appendChild;
  box.appendChild = (c) => { seen.push(c); return original.call(box, c); };
  await send();
  box.appendChild = original;

  const html = seen.map((c) => String(c.innerHTML || "")).join("\n");
  check(`${label} does not report a failure`,
        !/Could not reach server|started and then stopped/.test(html),
        `rendered an error instead of the answer:\n${html}`);
  check(`${label} creates the assistant bubble`,
        /class="role">Assistant/.test(html),
        `no assistant bubble -- the function threw before appending it:\n${html}`);
  /* the real assertion: the tokens reached the bubble */
  const answered = seen.some(
    (c) => (c.querySelector(".content").textContent || "").includes("Hey there"));
  check(`${label} renders the streamed answer`, answered,
        "the tokens never reached the bubble");
  return bubbles;
}

(async () => {
  console.log("web/app.js senders:");
  await run("sendPlay", () => H.sendPlay(), () => {
    H.setDetail("deploy-123");
    byId("playInput").value = "hey";
  });
  await run("sendChat", () => H.sendChat(), () => {
    byId("chatInput").value = "hey";
    byId("chatTarget").value = "deploy-123";
  });

  if (failures.length) {
    console.error("\nFAILED");
    for (const f of failures) console.error("  " + f);
    process.exit(1);
  }
  console.log("\nall good");
})();
