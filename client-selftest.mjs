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

const results = [];
function check(label, fn) {
  try {
    const detail = fn();
    console.log(`  PASS  ${label}${detail ? "  " + detail : ""}`);
    results.push(true);
  } catch (error) {
    console.log(`  FAIL  ${label}\n        ${error && error.message}`);
    results.push(false);
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

check("回合尾部朗读组件已注册", () => {
  assert(names.includes("conversation.chat.turnTail"), "缺少 conversation.chat.turnTail");
  assert(names.includes("main"), "缺少 main");
  assert(names.includes("sidebar.panellist"), "缺少 sidebar.panellist");
});

check("第二页（长文合成）在面板内切换", () => {
  const mains = registered.filter((r) => r && r.name === "main");
  const panels = registered.filter((r) => r && r.name === "sidebar.panellist");
  assert(mains.length === 1, "main 应该只有一个（页签在内部切换），实际 " + mains.length);
  assert(panels.length === 1, "侧边栏应该只有一个条目，实际 " + panels.length);
  // 页签切换由 LongPanel 组件 + tab 状态实现，源码里必须存在
  assert(code.indexOf("长文合成") >= 0, "面板里没有「长文合成」页签");
  assert(code.indexOf("function LongPanel") >= 0, "缺少 LongPanel 组件");
  assert(code.indexOf('setTab("long")') >= 0, "缺少切换到长文的页签逻辑");
  return "单条目 + 内部页签";
});

const T = exportsObj.__test;
check("纯函数已导出给自检", () => {
  assert(T && typeof T.turnTime === "function" && typeof T.turnIdentity === "function", "缺少 __test");
});

// DSH 的 TurnLocation：start/end 是 SessionEvent {type,seq,time,data}，time 是 epoch ms。
const now = Date.now();
const turn = {
  turn: 3,
  start: { type: "turn/start", seq: 40, time: now - 9000, data: {} },
  end: { type: "turn/end", seq: 48, time: now - 1000, data: {} },
  status: "closed",
  steps: [],
  data: { get: () => undefined, keys: () => [] },
};

check("turnTime 读 end.time / start.time（不再恒为 0）", () => {
  const t = T.turnTime(turn);
  assert(t === now - 1000, `end.time 应为 ${now - 1000}，实际 ${t}`);
  const noEnd = Object.assign({}, turn, { end: undefined });
  assert(T.turnTime(noEnd) === now - 9000, "start.time 回退失败");
  assert(T.turnTime({}) === 0, "无时间戳应返回 0");
  return `${t}`;
});

check("turnTime 兼容 ISO 字符串时间戳", () => {
  const iso = new Date(now - 5000).toISOString();
  assert(T.turnTime({ end: { time: iso } }) === Date.parse(iso), "ISO 解析失败");
});

check("turnIdentity 按 seq 稳定、不随正文长度变化", () => {
  const id = T.turnIdentity(turn, "session-abc", 48);
  assert(id === "session-abc:48", `意外身份: ${id}`);
  assert(T.turnIdentity(turn, "session-abc", 0) === "session-abc:48", "props.seq 缺省时应退回 end.seq");
  assert(T.turnIdentity({ turn: 7 }, "s", 0) === "s:t7", "无 seq 时应退回 turn 序号");
});

check("hashText 同文同值、异文异值", () => {
  assert(T.hashText("你好世界") === T.hashText("你好世界"), "同文 hash 不一致");
  assert(T.hashText("你好世界") !== T.hashText("你好"), "异文 hash 相同");
});

check("trimForSpeech 不截断短文本、按句号收尾", () => {
  assert(T.trimForSpeech("你好，这是测试。", 300) === "你好，这是测试。", "短文本被改");
  const long = "甲".repeat(70) + "。" + "乙".repeat(200);
  const cut = T.trimForSpeech(long, 100);
  assert(cut.length <= 100 && cut.endsWith("。"), "长文本未按句号收尾");
  const noStop = "甲".repeat(200);
  assert(T.trimForSpeech(noStop, 100) === "甲".repeat(100), "窗口内无句号时应硬截断");
});

// DSH 的块字段是 kind，不是 type；reasoning 必须整块跳过，不能递归取它的 text。
const thinking = "我先检查用户的问题，再决定怎么组织答案，这段很长很长，不应该被念出来。";
const finalText = "这是最终给用户的回答，只有这一段应该朗读。";
const dshBlocks = [
  { kind: "reasoning", text: thinking },
  { kind: "text", text: finalText },
];
const store = (data) => ({ get: (k) => (k === "turn-tail" ? data : undefined) });
const dshTurn = {
  turn: 5,
  start: { seq: 10, time: Date.now() - 5000 },
  end: { seq: 12, time: Date.now() },
  status: "closed",
  steps: [],
  data: store({ closing: { finalNode: {}, blocks: dshBlocks } }),
};

check("textFromBlocks 只拼 kind=text", () => {
  const out = T.textFromBlocks(dshBlocks);
  assert(out === finalText, `意外结果: ${out}`);
  assert(T.textFromBlocks([{ type: "text", text: finalText }]) === finalText, "type=text 兼容失败");
  assert(T.textFromBlocks([{ kind: "reasoning", text: thinking }]) === "", "reasoning 未被过滤");
});

check("extractReplyInfo 从 turn-tail 取最终正文", () => {
  const info = T.extractReplyInfo(dshTurn);
  assert(info.text === finalText, `意外正文: ${info.text}`);
  assert(info.source === "turn-tail", `意外来源: ${info.source}`);
  assert(info.kinds.indexOf("reasoning:1") >= 0 && info.kinds.indexOf("text:1") >= 0, `kinds: ${info.kinds}`);
});

check("extractReply 不返回思考过程", () => {
  const out = T.extractReply(dshTurn);
  assert(out === finalText, `意外正文: ${out}`);
  assert(out.indexOf("检查用户") < 0, "仍然把 reasoning 念出来");
});

check("递归兜底也整块跳过 reasoning", () => {
  const found = T.walkSpeechStrings({ blocks: dshBlocks }, 8, 4000, 10);
  const texts = found.map((x) => x.text).join("|");
  assert(texts.indexOf(finalText) >= 0, "没找到最终正文");
  assert(texts.indexOf("检查用户") < 0, `递归兜底抓到思考: ${texts}`);
});

check("turn-tail 缺失时退回 assistant-step", () => {
  const stepTurn = {
    turn: 6,
    steps: [{ data: { get: (k) => (k === "assistant-step" ? { finalNode: {}, blocks: dshBlocks } : undefined) } }],
    data: { get: () => undefined },
  };
  const info = T.extractReplyInfo(stepTurn);
  assert(info.text === finalText, `意外正文: ${info.text}`);
  assert(info.source === "assistant-step:0", `意外来源: ${info.source}`);
});

// 面板结构回归：React 在这里是空桩，拿不到渲染树，所以直接看源码。
// 这两条守的是「别再退回二选一」和「加了新引擎别忘了写进说明」。
check("面板不再有 GSV / 系统 TTS 二选一", () => {
  assert(code.indexOf('label: "GSV 角色音色"') < 0, "旧的 GSV 角色音色按钮又回来了");
  assert(code.indexOf('label: "系统 TTS"') < 0, "旧的系统 TTS 按钮又回来了");
  assert(code.indexOf("const list = voices;") >= 0, "音色列表不该再按 engine 过滤");
});

check("怎么加新模型覆盖四个引擎", () => {
  const at = code.indexOf('label: "怎么加新模型"');
  assert(at >= 0, "找不到「怎么加新模型」");
  const block = code.slice(at, at + 1600);
  for (const key of ["models\\\\gsv", "models\\\\rvc", "engines.higgs", "engines.cosy"]) {
    assert(block.indexOf(key) >= 0, `说明里缺 ${key}`);
  }
});

// 端口只能来自 config.json：面板一旦写死端口，用户改了配置就会出现
// 「服务起来了但面板说没起」，这是这个项目踩过的老坑，用断言守住。
check("面板不硬编码引擎端口", () => {
  for (const port of ["9880", "8790", "8791", "8789"]) {
    assert(code.indexOf(port) < 0, `面板里又出现写死的端口 ${port}`);
  }
  assert(code.indexOf("stack[k]") >= 0 || code.indexOf("s.port") >= 0, "端口应从 stack 动态取");
});

check("选中 higgs/cosy 音色时显示语气提示词", () => {
  assert(code.indexOf("语气提示词") >= 0, "缺少「语气提示词」展示块");
  assert(code.indexOf("prompt_text") >= 0, "语气块没读音色的 prompt_text");
  assert(code.indexOf("instruct") >= 0, "语气块没读音色的 instruct");
  assert(code.indexOf("endofprompt") >= 0, "未剥离 CosyVoice 的 <|endofprompt|> 系统前缀");
});

check("音色下拉按引擎分组（optgroup）", () => {
  assert(code.indexOf("optgroup") >= 0, "音色下拉没用 optgroup 分组");
  assert(code.indexOf("ENGINE_ORDER") >= 0, "缺少引擎分组顺序定义");
  assert(code.indexOf("ENGINE_LABEL") >= 0, "缺少引擎标签映射");
});

check("提供添加克隆音色的入口", () => {
  assert(code.indexOf("添加音色") >= 0 || code.indexOf("添加克隆音色") >= 0, "缺少添加音色入口");
  assert(code.indexOf("/v1/voice") >= 0, "没有调用 /v1/voice 接口");
  assert(code.indexOf("ref_audio") >= 0, "添加表单没填参考音频 ref_audio");
});

check("长文合成页带语气词辅助", () => {
  assert(code.indexOf("语气词辅助") >= 0, "长文合成页缺少「语气词辅助」区");
  assert(code.indexOf("各模型语气能力") >= 0, "缺少各模型语气能力说明");
  // 长文合成传 instruct 给后端（仅 cosy 音色）
  assert(code.indexOf("body.instruct") >= 0 || code.indexOf("instruct.trim()") >= 0,
    "长文合成没把 instruct 传给 /v1/long-tts");
});

const ok = results.every(Boolean);
console.log(ok ? "PASS: client 自检全部通过" : "FAIL: client 自检失败");
process.exit(ok ? 0 : 1);
