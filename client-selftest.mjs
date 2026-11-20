import { readFileSync } from "node:fs";

const React = {
  createElement: () => null,
  useState: (v) => [v, () => {}],
  useRef: (v) => ({ current: v }),
  useEffect: () => {},
  useCallback: (f) => f,
};

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.sessionStorage = {
  getItem: () => null,
  setItem: () => {},
};
globalThis.Audio = class { play() { return Promise.resolve(); } pause() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });

const registered = [];
const injectedSlots = [];
const slots = {
  register: (def) => {
    registered.push(def);
    return null;
  },
  inject: (name, fn) => {
    injectedSlots.push(name);
    fn();
  },
};

const ctx = { get: (n) => (n === "slots" ? slots : undefined) };

const code = readFileSync("client.js", "utf8");
let exportsObj = null;
const fakeWindow = {
  __ModuleLoader__: {
    load: (m) => {
      exportsObj = m.factory((n) => (n === "react" ? React : {}));
    },
  },
};

new Function("window", code)(fakeWindow);

if (!exportsObj || typeof exportsObj.apply !== "function") {
  console.error("FAIL: apply 没有导出");
  process.exit(1);
}

exportsObj.apply(ctx);

const names = registered.map((r) => r && r.name);
console.log("注册的 slot:", names.join(", "));
console.log("inject 过的 slot:", [...new Set(injectedSlots)].join(", "));

const ok =
  names.includes("conversation.chat.turnTail") &&
  names.includes("main") &&
  names.includes("sidebar.panellist");
console.log(ok ? "PASS: 回合尾部朗读组件已注册" : "FAIL: 缺少注册");
process.exit(ok ? 0 : 1);
