/**
 * VoiceHub 侧边栏面板。
 *
 * 双模的另一半：面板和 MCP 工具（AI 口令）改的是同一个 voice_mode.json，
 * 两边不会打架 —— AI 说「打开语音」之后，面板 4 秒内会自己刷成开启。
 * 手写 React.createElement，不跑构建；只依赖 DSH 暴露的 react。
 */
window.__ModuleLoader__.load({
  id: "voxrelay",
  factory: (e) => {
    const module = { exports: {} };
    const exports = module.exports;
    try {
      const React = e("react");
      const h = React.createElement;

      const PANEL = "voicehub-panel";
      // 默认端口。用户改过 config.json 的 port 时，面板会在连不上时让他填一次地址，
      // 存进 localStorage，以后不用再填。
      const DEFAULT_BASE = "http://127.0.0.1:8787";
      const BASE_KEY = "voicehub.base";
      // 这个标记区分「用户手填的」和「自动探测到的」：
      // 手填的远程地址要尊重，不能被自动探测冲掉；手填的本机地址则放行，
      // 因为端口变了本来就该跟着走。
      const BASE_MANUAL_KEY = "voicehub.base.manual";
      function isLocalBase(b) {
        return /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(\/|$)/i.test(String(b || ""));
      }
      function readBase() {
        try { return localStorage.getItem(BASE_KEY) || DEFAULT_BASE; } catch (e) { return DEFAULT_BASE; }
      }
      let BASE = readBase();
      const POLL_MS = 4000;

      const C = {
        text: "var(--dsw-alias-label-primary, #1f2328)",
        dim: "var(--dsw-alias-label-secondary, #6b7280)",
        bg: "var(--dsw-alias-bg-base, #ffffff)",
        line: "var(--dsw-alias-border-l4, #e3e5e8)",
        accent: "var(--dsw-color-accent, #1f6feb)",
      };

      async function call(path, body) {
        const opt = body
          ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
          : {};
        const res = await fetch(BASE + path, opt);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const msg = (data && data.error && data.error.message) || "HTTP " + res.status;
          throw new Error(msg);
        }
        return data;
      }

      // 控制层：走宿主同源路由，8787 没起也能用。
      // 宿主转手调 voice_services / mcp_server，不自己重写启停判断，
      // 所以面板、MCP、计划任务操作的是同一份状态。
      async function hostCall(path, body) {
        const opt = body
          ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
          : { cache: "no-store" };
        const res = await fetch(path, opt);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error((data && data.error) || "HTTP " + res.status);
        }
        return data;
      }

      // 问宿主真实端口。同源相对路径，不需要知道服务起在哪。
      // 宿主那半边（index.js）会读 config.json 现算，改了端口也能立刻反映。
      // 必须带超时：这个请求挂在轮询路径上，卡住就是整个面板卡住。
      async function discoverBase() {
        try {
          const ctrl = typeof AbortController === "function" ? new AbortController() : null;
          const timer = ctrl ? setTimeout(() => ctrl.abort(), 1500) : null;
          let res;
          try {
            res = await fetch("/voicehub/endpoint", {
              cache: "no-store",
              signal: ctrl ? ctrl.signal : undefined,
            });
          } finally {
            if (timer) clearTimeout(timer);
          }
          if (!res.ok) return null;
          const data = await res.json();
          return data && data.base ? String(data.base).replace(/\/+$/, "") : null;
        } catch (e) {
          return null;
        }
      }

      function engineOf(voice) {
        return voice.engine === "gsv" ? "gsv" : "tts";
      }

      function Field(props) {
        return h("div", { style: { marginBottom: 18 } },
          h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 6 } }, props.label),
          props.children,
        );
      }

      function Seg(props) {
        return h("div", {
          style: { display: "inline-flex", border: "1px solid " + C.line, borderRadius: 8, overflow: "hidden" },
        }, props.items.map((it) =>
          h("button", {
            key: it.value,
            onClick: it.disabled ? undefined : () => props.onPick(it.value),
            title: it.hint || "",
            style: {
              padding: "7px 18px", fontSize: 13, border: 0,
              cursor: it.disabled ? "not-allowed" : "pointer",
              background: it.active ? C.accent : "transparent",
              color: it.disabled ? C.dim : it.active ? "#fff" : C.text,
              opacity: it.disabled ? 0.5 : 1,
              borderRight: "1px solid " + C.line,
            },
          }, it.label),
        ));
      }

      const STACK_NAMES = { voicehub: "VoiceHub", gsv: "GSV", rvc: "RVC" };

      // ---------------- 回合结束自动朗读（不依赖 agent 规则） ----------------
      // 开关仍以 voice_mode.json 的 enabled 为准；这里只解决「谁来触发」。
      // 面板轮询、MCP 工具、前端三处读的是同一份状态，谁改都立刻生效。
      let currentAudio = null;
      let currentAudioUrl = null;
      // ticket 每来一个朗读请求就+1；gen 是真正获准合成的那一个。
      // 新请求获准时 gen 跳到它，旧请求在 await 后发现自己过期就自己退场。
      let ticket = 0;
      let gen = 0;
      let activeController = null;
      // 同一 tick 里 DSH 会把历史回合整批重放；攒 700ms 一批，只念最新的那个。
      let pendingTurns = [];
      let batchTimer = null;
      const firedKeys = new Set();
      const seenSeq = new Map();
      // 朗读来源：panel=面板自动朗读，mcp=agent 用 speak 念。面板轮询时更新，
      // AutoSpeak 挂载时读它决定念不念，省一次 /v1/mode 请求。
      let speechMode = "panel";

      // 排查用：把朗读走到哪一步报给服务端（只发键名/长度/状态码，不发聊天正文）
      function report(data) {
        try {
          fetch(BASE + "/v1/debug", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
            keepalive: true,
          }).catch(function () {});
        } catch (e) { /* 忽略 */ }
      }

      function hashText(s) {
        let h = 5381;
        for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
        return (h >>> 0).toString(36) + "-" + s.length;
      }

      function looksLikeCode(s) {
        if (/^\s*[\[{]/.test(s)) return true;
        if (s.indexOf("```") >= 0) return true;
        if (/^(https?:\/\/|\/|[A-Za-z]:\\)/.test(s)) return true;
        return false;
      }

      function storeGet(store, key) {
        if (!store || typeof store.get !== "function") return undefined;
        try { return store.get(key); } catch (e) { return undefined; }
      }

      function isTextBlock(b) {
        if (!b || typeof b !== "object") return false;
        const kind = b.kind || b.type;
        return kind === "text" || kind === "output_text";
      }

      // DSH 的最终正文在 LocationDataStore 里，不在 turn 的顶层：
      //   turn.data.get("turn-tail").closing.blocks  ← 回合收尾时挑好的最终回复
      //   step.data.get("assistant-step").blocks     ← 逐步的 assistant 内容
      // 这些块的字段是 kind（不是 type）：reasoning / text / tool-call / image。
      // 之前只判 type，reasoning 块没被拦住，递归时把它的 text 当正文取走了。
      function blockKinds(blocks) {
        if (!Array.isArray(blocks)) return "";
        const count = {};
        blocks.forEach(function (b) {
          const k = b && (b.kind || b.type) || "?";
          count[k] = (count[k] || 0) + 1;
        });
        return Object.keys(count).map(function (k) { return k + ":" + count[k]; }).join(",");
      }

      function textFromBlocks(blocks) {
        if (!Array.isArray(blocks)) return "";
        // 与 DSH 的 assistantText() 一致：只拼 kind=text，顺序不变。
        return blocks.filter(isTextBlock)
          .map(function (b) { return typeof b.text === "string" ? b.text : ""; })
          .join("").trim();
      }

      function speechText(s, min, max) {
        if (typeof s !== "string") return "";
        s = s.trim();
        if (s.length < min || s.length > max) return "";
        if (looksLikeCode(s)) return "";
        if (!/[\u4e00-\u9fa5]/.test(s) && s.length < 40) return "";
        return s;
      }

      function structuredReply(turn) {
        if (!turn || typeof turn !== "object") return null;
        const tail = storeGet(turn.data, "turn-tail");
        if (tail && tail.closing && Array.isArray(tail.closing.blocks)) {
          const text = speechText(textFromBlocks(tail.closing.blocks), 1, 4000);
          if (text) return { text: text, source: "turn-tail", kinds: blockKinds(tail.closing.blocks) };
        }
        const steps = Array.isArray(turn.steps) ? turn.steps : [];
        for (let i = steps.length - 1; i >= 0; i--) {
          const a = storeGet(steps[i] && steps[i].data, "assistant-step");
          if (!a || !Array.isArray(a.blocks) || a.finalNode === undefined) continue;
          const text = speechText(textFromBlocks(a.blocks), 1, 4000);
          if (text) return { text: text, source: "assistant-step:" + i, kinds: blockKinds(a.blocks) };
        }
        return null;
      }

      // 结构认不出来时的递归兜底。块对象按 kind/type 整块判定：
      // 非 text 块（尤其 reasoning）直接 return，绝不往里递归取 text。
      function walkSpeechStrings(root, min, max, limit) {
        const found = [];
        const seen = new Set();
        const push = function (s, role) {
          const v = speechText(s, min, max);
          if (!v || seen.has(v)) return;
          seen.add(v);
          found.push({ text: v, role: role });
        };
        const walk = function (v, role, depth) {
          if (depth > 10 || found.length > limit) return;
          if (typeof v === "string") {
            const s = v.trim();
            if (s.length > 1 && s.length < 8000 && (s.charAt(0) === "{" || s.charAt(0) === "[")) {
              try {
                const parsed = JSON.parse(s);
                if (parsed && typeof parsed === "object") {
                  walk(parsed, role, depth + 1);
                  return;
                }
              } catch (e) { /* 按普通文本继续 */ }
            }
            push(v, role);
            return;
          }
          if (!v || typeof v !== "object") return;
          const kind = typeof v.kind === "string" ? v.kind : (typeof v.type === "string" ? v.type : "");
          if (kind) {
            if (isTextBlock(v)) push(v.text, role);
            return; // reasoning / tool-call / image / other 整块跳过
          }
          const r = typeof v.role === "string" ? v.role : role;
          if (typeof v.get === "function" && typeof v.forEach === "function") {
            try { v.forEach(function (val) { walk(val, r, depth + 1); }); } catch (e) { /* 忽略 */ }
            return;
          }
          if (Array.isArray(v)) { v.forEach(function (x) { walk(x, r, depth + 1); }); return; }
          Object.keys(v).forEach(function (k) {
            if (k === "meta" || k === "usage" || k === "id" || k === "sessionId") return;
            if (/reason|think|thought|analysis|prompt|context|history|system|tool/i.test(k)) return;
            try { walk(v[k], r, depth + 1); } catch (e) { /* 忽略 */ }
          });
        };
        walk(root, "assistant", 0);
        found.sort(function (a, b) { return b.text.length - a.text.length; });
        return found;
      }

      function extractReplyInfo(turn) {
        const structured = structuredReply(turn);
        if (structured) return structured;
        const found = walkSpeechStrings(turn, 8, 4000, 80);
        const asst = found.filter(function (x) { return x.role !== "user"; });
        const pool = asst.length ? asst : found;
        return {
          text: pool.length ? pool[0].text : "",
          source: "walk",
          kinds: "",
        };
      }

      function extractReply(turn) {
        return extractReplyInfo(turn).text;
      }

      // 兜底：结构认不出来时放宽长度，但仍走同一套「非 text 块不递归」规则。
      function extractReplyLoose(turn) {
        const structured = structuredReply(turn);
        if (structured) return structured.text;
        const found = walkSpeechStrings(turn, 12, 1200, 120);
        return found.length ? found[0].text : "";
      }

      // 回合时间戳：用来区分「新回复」和「打开历史会话时重放的旧回合」。
      // DSH 的 TurnLocation 是 {turn,start,end,status,steps,data}，start/end 是
      // SessionEvent：{type,seq,time,data}，time 是 Unix 毫秒。原来只扫描 turn 的
      // createdAt/... 一层，读不到这里，导致 when 恒为 0、历史回合全被当新的念。
      // 结束时间优先：turnTail 只在回合 closed 后挂载，end.time 才是「刚说完」。
      function eventTime(ev) {
        if (!ev || typeof ev !== "object") return 0;
        const keys = ["time", "timestamp", "ts", "createdAt", "updatedAt", "startedAt"];
        for (let i = 0; i < keys.length; i++) {
          let v;
          try { v = typeof ev.get === "function" ? ev.get(keys[i]) : ev[keys[i]]; } catch (e) { continue; }
          if (typeof v === "number" && v > 1e12) return v;
          if (typeof v === "string") {
            const t = Date.parse(v);
            if (!isNaN(t)) return t;
          }
        }
        return 0;
      }

      function turnTime(turn) {
        if (!turn || typeof turn !== "object") return 0;
        const end = typeof turn.end === "object" ? turn.end : null;
        const start = typeof turn.start === "object" ? turn.start : null;
        return eventTime(end) || eventTime(start) || eventTime(turn) || 0;
      }

      // 回合身份：sessionId + 结束事件序号，重渲染时 textLen 会变但 seq 不变。
      // props.seq 是 turnTail 插槽的 closing seq；没有就退到 end/start 的 seq。
      function turnIdentity(turn, sessionId, seq) {
        let n = Number(seq);
        if (!(n > 0)) {
          const ev = (turn && typeof turn.end === "object" && turn.end)
            || (turn && typeof turn.start === "object" && turn.start);
          n = ev ? Number(ev.seq) : 0;
        }
        const index = turn && typeof turn.turn === "number" ? turn.turn : "?";
        return String(sessionId || "default") + ":" + (n > 0 ? n : "t" + index);
      }

      function trimForSpeech(text, max) {
        text = String(text || "").replace(/\s+/g, " ").trim();
        if (text.length <= max) return text;
        const cut = text.slice(0, max);
        const p = Math.max(
          cut.lastIndexOf("。"), cut.lastIndexOf("！"),
          cut.lastIndexOf("？"), cut.lastIndexOf("；"),
        );
        return p > max * 0.6 ? cut.slice(0, p + 1) : cut;
      }

      function spokenStore(sessionId) {
        const key = "voxrelay.spoken." + (sessionId || "default");
        let arr = [];
        try { arr = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (e) { arr = []; }
        return { key: key, arr: Array.isArray(arr) ? arr : [] };
      }

      function saveSpoken(store) {
        if (store.arr.length > 80) store.arr = store.arr.slice(-80);
        try { sessionStorage.setItem(store.key, JSON.stringify(store.arr)); } catch (e) { /* 忽略 */ }
      }

      function seenSeqOf(sessionId) {
        const sid = String(sessionId || "default");
        if (seenSeq.has(sid)) return seenSeq.get(sid);
        const store = spokenStore(sid);
        // 老数据只有文本 hash，没有 seq；从 identity 里取一次，避免重启后重念。
        let max = 0;
        store.arr.forEach(function (x) {
          // t2: 是修正正文提取后的命名空间；旧 t: 标记对应思考文本，必须忽略。
          const m = /^t2:(\d+)\|/.exec(String(x));
          if (m) max = Math.max(max, Number(m[1]) || 0);
        });
        seenSeq.set(sid, max);
        return max;
      }

      function markSeenSeq(sessionId, seq) {
        const sid = String(sessionId || "default");
        const n = Number(seq);
        if (!(n > 0) || n <= seenSeqOf(sid)) return;
        seenSeq.set(sid, n);
      }

      async function speakTurn(turn, sessionId, identity, seq) {
        // 新的回合一来就撤销上一回合：正在下载的不再播，正在播的立刻停。
        const my = ++ticket;
        gen = my;
        if (activeController) {
          try { activeController.abort(); } catch (e) { /* 忽略 */ }
        }
        activeController = null;
        try { if (currentAudio) currentAudio.pause(); } catch (e) { /* 忽略 */ }
        try {
          if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
        } catch (e) { /* 忽略 */ }
        currentAudio = null;
        currentAudioUrl = null;
        if (my !== gen) return; // 保险：理论上不会在同步段被抢

        let ctrl = null;
        if (typeof AbortController === "function") ctrl = new AbortController();
        activeController = ctrl;

        try {
          let m;
          try {
            m = await call("/v1/mode");
          } catch (first) {
            // DSH 刚起来 / 服务刚重启时，BASE 可能还指着旧端口。
            // 直接放弃会让整批回复都没声音——先向宿主问一次真实地址再重试一次。
            const found = await discoverBase();
            if (!found || found === BASE) throw first;
            BASE = found;
            try { localStorage.setItem(BASE_KEY, found); } catch (e) { /* 忽略 */ }
            report({ stage: "rebased", base: found });
            m = await call("/v1/mode");
          }
          if (my !== gen) return;
          if (!m || !m.enabled) {
            report({ stage: "mode", enabled: !!(m && m.enabled) });
            return;
          }
          let info = extractReplyInfo(turn);
          let text = trimForSpeech(info.text, 300);
          report({ stage: "source", src: info.source, kinds: info.kinds, len: text.length });
          if (!text) {
            // 结构变了、正文挖不出来：再松一次，别让整段回复静音
            const loose = extractReplyLoose(turn);
            if (loose) {
              info = { source: "loose", kinds: "" };
              text = trimForSpeech(loose, 300);
              report({ stage: "extract-loose", len: text.length });
            }
          }
          if (!text) {
            report({ stage: "extract", len: 0 });
            return;
          }

          // 按回合身份去重：同一回合的 textLen 会从 0→短→长反复变，
          // 文本 hash 每次都不同，挡不住重挂载；seq 才是稳定身份。
          const h = hashText(text);
          const token = "t2:" + (seq || 0) + "|" + identity;
          const store = spokenStore(sessionId);
          if (firedKeys.has(token) || firedKeys.has(h) || store.arr.indexOf(token) >= 0 || store.arr.indexOf(h) >= 0) {
            report({ stage: "dup", len: text.length, by: "turn" });
            return;
          }
          // 先写去重标记再发请求：并发挂载时后来的直接退出，不会重复合成。
          firedKeys.add(token);
          firedKeys.add(h);
          store.arr.push(token, h);
          saveSpoken(store);
          markSeenSeq(sessionId, seq);

          const opt = {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-VoiceHub-Auto": "1" },
            body: JSON.stringify({ input: text, voice: m.voice }),
          };
          if (ctrl) opt.signal = ctrl.signal;
          const res = await fetch(BASE + "/v1/audio/speech", opt);
          if (my !== gen) return;
          if (!res.ok) {
            report({ stage: "fetch", status: res.status });
            return;
          }
          const blob = await res.blob();
          if (my !== gen) return;
          if (!blob || blob.size < 500) {
            report({ stage: "blob", size: blob ? blob.size : 0 });
            return;
          }

          const url = URL.createObjectURL(blob);
          const audio = new Audio(url);
          currentAudio = audio;
          currentAudioUrl = url;
          audio.onended = function () {
            try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ }
            if (currentAudioUrl === url) currentAudioUrl = null;
            if (currentAudio === audio) currentAudio = null;
          };
          report({ stage: "blob", size: blob.size });
          audio.play().then(function () {
            if (my === gen) report({ stage: "play", ok: true });
          }).catch(function (e) {
            report({ stage: "play", ok: false, err: String(e).slice(0, 80) });
          });
        } catch (e) {
          if (e && e.name === "AbortError") {
            report({ stage: "abort" });
            return;
          }
          report({ stage: "error", err: String(e).slice(0, 120) });
        }
      }

      // DSH 重放历史会话时，同一 tick 会mount十几个回合组件。
      // 攒 700ms 成一批：只有批次里 seq 最大（最新结束）的那个允许朗读。
      function flushBatch() {
        batchTimer = null;
        const batch = pendingTurns;
        pendingTurns = [];
        if (!batch.length) return;
        let pick = null;
        batch.forEach(function (item) {
          if (!pick) { pick = item; return; }
          // seq 优先；老版本 DSH 没有 seq 时用结束时间兜底，别总选中第一批里最旧的那个。
          if (item.seq > pick.seq
            || (item.seq === pick.seq && item.when > pick.when)
            || (item.seq === pick.seq && item.when === pick.when && item.textLen > pick.textLen)) pick = item;
        });
        if (!pick) return;
        report({ stage: "batch", size: batch.length, seq: pick.seq || 0, len: pick.textLen });
        // seq 为 0 的老版本 DSH：靠 identity + 文本 hash 去重，不在这里拦。
        if (pick.seq > 0 && pick.seq <= seenSeqOf(pick.sessionId)) return;
        speakTurn(pick.turn, pick.sessionId, pick.identity, pick.seq);
      }

      // 挂在每个回合的尾部：回合结束才渲染，正好是「说完再念」的时机。
      function AutoSpeak(props) {
        const fired = React.useRef(false);
        React.useEffect(() => {
          if (fired.current) return;
          fired.current = true;
          // 朗读来源不是「面板」时，回合尾部不再自动念，交给 agent 用 speak。
          // speechMode 由面板轮询更新；刚启动还没轮询到就用默认 panel，不额外阻塞。
          if (speechMode !== "panel") {
            report({ stage: "skip", reason: "speech_mode=" + speechMode });
            return;
          }
          const turn = props.turn;
          const when = turnTime(turn);
          // 证明不了是刚结束的新回合就不念：宁可偶尔静音，也别重念整段历史。
          const old = when ? Date.now() - when > 60000 : true;
          const seq = Number(props.seq) || 0;
          const identity = turnIdentity(turn, props.sessionId, seq);
          // DSH 的 seq 是 closing seq，和 end.seq 同源；优先 props，其次 end。
          const orderSeq = seq || Number(turn && turn.end && turn.end.seq) || 0;
          let keys = [];
          try {
            keys = turn && typeof turn === "object" ? Object.keys(turn).slice(0, 20) : [];
          } catch (e) { keys = []; }
          // 正文取不到时得知道 turn.data 长什么样，否则没法判断该往哪一层挖
          let dataKeys = "";
          try {
            const d = turn && turn.data;
            if (d && typeof d === "object") {
              const ks = typeof d.keys === "function" ? Array.from(d.keys()) : Object.keys(d);
              dataKeys = ks.slice(0, 10).join(",");
            } else {
              dataKeys = "typeof:" + typeof d;
            }
          } catch (e) {
            dataKeys = "?";
          }
          let probe = {};
          try {
            probe = {
              startType: typeof (turn && turn.start),
              startTime: (turn && turn.start && turn.start.time) || 0,
              startSeq: (turn && turn.start && turn.start.seq) || 0,
              endType: typeof (turn && turn.end),
              endTime: (turn && turn.end && turn.end.time) || 0,
              endSeq: (turn && turn.end && turn.end.seq) || 0,
              status: (turn && turn.status) || "",
            };
          } catch (e) { probe = { probeErr: String(e).slice(0, 40) }; }
          const info = extractReplyInfo(turn);
          report(Object.assign({
            stage: "mount",
            sid: String(props.sessionId || "").slice(-8),
            keys: keys,
            dataKeys: dataKeys,
            textLen: info.text.length,
            src: info.source,
            kinds: info.kinds,
            old: old,
            when: when || 0,
            seq: seq,
          }, probe));
          if (old) return;
          pendingTurns.push({
            turn: turn,
            sessionId: props.sessionId,
            seq: orderSeq,
            identity: identity,
            when: when,
            textLen: info.text.length,
          });
          if (batchTimer) return;
          batchTimer = setTimeout(flushBatch, 700);
        }, []);
        return null;
      }

      function Panel() {
        const [mode, setMode] = React.useState(null);
        const [voices, setVoices] = React.useState([]);
        const [err, setErr] = React.useState("");
        const [busy, setBusy] = React.useState(false);
        // 「关闭全部服务」要点两下：第一下变成确认态，4 秒不点自动复原，防误触
        const [confirmStop, setConfirmStop] = React.useState(false);
        const [showLoc, setShowLoc] = React.useState(false);
        const [locGsv, setLocGsv] = React.useState("");
        const [locRvc, setLocRvc] = React.useState("");
        const [baseInput, setBaseInput] = React.useState(BASE);
        const [showBase, setShowBase] = React.useState(false);
        const locInit = React.useRef(false);
        // 用户手填过地址就锁住，别再被自动探测覆盖；点「自动检测」会解锁
        const baseLocked = React.useRef(false);
        // 宿主侧的实时状态（三件套 + 语音模式）。8787 连不上时，面板靠它渲染，
        // 也靠它把服务拉起来——这是「面板能自救」的关键。
        const [host, setHost] = React.useState(null);

        const refreshHost = React.useCallback(async () => {
          try {
            const d = await hostCall("/voicehub/status");
            setHost(d && d.ok ? d : null);
            return d;
          } catch (e) {
            setHost(null);
            return null;
          }
        }, []);

        const load = React.useCallback(async () => {
          try {
            const [m, v] = await Promise.all([call("/v1/mode"), call("/v1/audio/voices")]);
            setMode(m);
            setVoices(v.voices || []);
            if (m && (m.speech_mode === "panel" || m.speech_mode === "mcp")) {
              speechMode = m.speech_mode;
            }
            setErr("");
            if (!locInit.current && m && m.locations) {
              setLocGsv(m.locations.gsv || "");
              setLocRvc(m.locations.rvc || "");
              locInit.current = true;
            }
          } catch (ex) {
            setErr("连不上服务（" + BASE + "）：" + ex.message);
            // 连不上就问宿主：它读 endpoint.json，端口被改过也能跟上。
            const hd = await refreshHost();
            if (!baseLocked.current) {
              const found = (hd && hd.base) || (await discoverBase());
              if (found && found !== BASE) {
                BASE = found;
                setBaseInput(found);
                setErr("");
                // 持久化新地址，下次启动直接用对的
                try { localStorage.setItem(BASE_KEY, found); } catch (e) { /* 忽略 */ }
              }
            }
          }
        }, [refreshHost]);

        React.useEffect(() => {
          let cancelled = false;
          const boot = async () => {
            // 端口可能被改过，保存的地址只当「先试一下」的候选，不长期锁死：
            // 连不上时 load() 的 catch 会向宿主重新问实际端口。
            let saved = null;
            try { saved = localStorage.getItem(BASE_KEY); } catch (e) { /* 忽略 */ }
            if (saved) {
              BASE = saved;
              setBaseInput(saved);
              // 以前手填过「别的机器」的地址：尊重它，别让自动探测冲掉。
              // 手填的本机地址不锁——端口变了本来就该跟着走。
              let manual = false;
              try { manual = localStorage.getItem(BASE_MANUAL_KEY) === "1"; } catch (e) { /* 忽略 */ }
              if (manual && !isLocalBase(saved)) baseLocked.current = true;
            } else {
              const found = await discoverBase();
              if (!cancelled && found && found !== BASE) {
                BASE = found;
                setBaseInput(found);
              }
            }
            if (!cancelled) load();
          };
          boot();
          const timer = setInterval(load, POLL_MS);
          return () => { cancelled = true; clearInterval(timer); };
        }, [load]);

        const applyBase = () => {
          const v = (baseInput || "").trim().replace(/\/+$/, "");
          BASE = v || DEFAULT_BASE;
          baseLocked.current = true;
          try { localStorage.setItem(BASE_KEY, BASE); } catch (e) { /* 忽略 */ }
          try { localStorage.setItem(BASE_MANUAL_KEY, "1"); } catch (e) { /* 忽略 */ }
          setBaseInput(BASE);
          setErr("");
          load();
        };

        const send = async (body) => {
          setBusy(true);
          try {
            const m = await call("/v1/mode", body);
            setMode(m);
            setErr("");
            return true;
          } catch (ex) {
            setErr(ex.message);
            return false;
          } finally {
            setBusy(false);
          }
        };

        const saveLoc = async () => {
          setBusy(true);
          try {
            await call("/v1/stack", { component: "gsv", path: locGsv });
            await call("/v1/stack", { component: "rvc", path: locRvc });
            setErr("");
            setShowLoc(false);
            await load();
          } catch (ex) {
            setErr(ex.message);
          } finally {
            setBusy(false);
          }
        };

        const preview = () => {
          try {
            const url = BASE + "/say?text=" + encodeURIComponent("你好，这是音色试听。")
              + "&voice=" + encodeURIComponent(mode.voice);
            const a = new Audio(url);
            a.play().catch(function () {});
          } catch (e) { /* 试听失败不影响使用 */ }
        };

        const rescan = async () => {
          setBusy(true);
          try {
            await call("/v1/rescan", {});
            setErr("");
            await load();
          } catch (ex) {
            setErr(ex.message);
          } finally {
            setBusy(false);
          }
        };

        const restart = () => {
          try { call("/v1/restart", {}).catch(function () {}); } catch (e) { /* 忽略 */ }
          setErr("服务重启中，稍后自动重连…");
        };

        // 整体关闭：必须走宿主。
        // 8787 的 /v1/mode 只改开关、不碰进程；真正杀进程的是 mcp_server.set_voice_mode
        // 里的 stop_services，所以这里转一手宿主。
        // 用途：DSH 关掉后服务是 DETACHED 的、还在吃显存，靠这个按钮在关 DSH 前清干净。
        const stopAll = async () => {
          setBusy(true);
          try {
            await hostCall("/voicehub/mode", { enabled: false });
            setErr("已发起关闭：三个进程会在几秒内退出，显存随之释放。");
            setTimeout(refreshHost, 2500);
          } catch (ex) {
            setErr("关闭失败：" + ex.message);
          } finally {
            setBusy(false);
          }
        };

        const svcAction = async (name, action) => {
          setBusy(true);
          try {
            await call("/v1/service", { name: name, action: action });
            setErr("");
            await load();
          } catch (ex) {
            // 8787 没响应（比如刚把 VoiceHub 自己停了）：转宿主，
            // 底下仍走 voice_services，路径不同、逻辑同一套。
            try {
              await hostCall("/voicehub/service", { name: name, action: action });
              setErr(name + " " + action + " 已发起，稍后自动刷新…");
              setTimeout(refreshHost, 2500);
            } catch (e2) {
              setErr(ex.message);
            }
          } finally {
            setBusy(false);
          }
        };

        if (!mode) {
          // 连不上 8787 不等于没救：宿主路由还活着，能看三件套状态、能直接把服务拉起来。
          // 地址框保留——端口被改过时手填一次仍然最快。
          const hs = (host && host.services) || null;
          const enabled = host && host.mode ? !!host.mode.enabled : false;
          const rows = [
            ["VoiceHub", hs && hs.VoiceHub],
            ["GSV", hs && hs.GSV],
            ["RVC", hs && hs.RVC],
          ];
          const bootAll = async () => {
            setBusy(true);
            try {
              await hostCall("/voicehub/mode", { enabled: true });
              setErr("正在拉起语音服务（首次约 1 分钟，GSV 要加载模型）…");
              setTimeout(refreshHost, 2500);
            } catch (ex) {
              setErr("拉起失败：" + ex.message);
            } finally {
              setBusy(false);
            }
          };
          const startOne = async (name) => {
            setBusy(true);
            try {
              await hostCall("/voicehub/service", { name: name, action: "start" });
              setErr(name + " 已发起启动，状态稍后刷新…");
              setTimeout(refreshHost, 2500);
            } catch (ex) {
              setErr(ex.message);
            } finally {
              setBusy(false);
            }
          };

          return h("div", { style: { padding: "28px 32px", fontSize: 13, color: C.dim, maxWidth: 640 } },
            h("div", { style: { fontSize: 15, fontWeight: 500, color: C.text, marginBottom: 6 } },
              "语音服务未运行"),
            h("div", { style: { lineHeight: 1.6, marginBottom: 18 } },
              err || "服务没起，面板连不上 8787。"),

            hs ? h("div", { style: { marginBottom: 20 } },
              h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 8 } }, "组件状态"),
              rows.map((pair) => {
                const nm = pair[0];
                const s = pair[1];
                const installed = s ? s.installed !== false : null;
                const running = s ? !!s.running : false;
                return h("div", {
                  key: nm,
                  style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 8 },
                },
                  h("span", { style: { width: 76, color: C.text, fontSize: 13 } }, nm),
                  h("span", {
                    style: { fontSize: 12, color: running ? "#1a9e5c" : (!installed ? "#c0392b" : "#b7791f") },
                  }, !s ? "未知" : (running ? "运行中" : (installed ? "未运行" : "未安装"))),
                  running || installed === false ? null : h("button", {
                    onClick: () => startOne(nm),
                    disabled: busy,
                    style: {
                      marginLeft: 4, padding: "3px 12px", fontSize: 11, borderRadius: 6,
                      cursor: busy ? "default" : "pointer",
                      border: "1px solid " + C.line, background: C.bg, color: C.text,
                    },
                  }, "启动"),
                );
              }),
            ) : null,

            h("div", { style: { display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" } },
              h("button", {
                onClick: bootAll,
                disabled: busy,
                style: {
                  padding: "8px 18px", fontSize: 13, border: 0, borderRadius: 8,
                  background: C.accent, color: "#fff",
                  cursor: busy ? "default" : "pointer", opacity: busy ? 0.6 : 1,
                },
              }, enabled ? "重新拉起全套" : "启动语音（全套）"),
              // 可能只是 VoiceHub 自己掉了、GSV/RVC 还在吃显存，这里给个清干净的口子
              h("button", {
                onClick: () => {
                  if (!confirmStop) {
                    setConfirmStop(true);
                    setTimeout(() => setConfirmStop(false), 4000);
                    return;
                  }
                  setConfirmStop(false);
                  stopAll();
                },
                disabled: busy,
                style: {
                  padding: "8px 18px", fontSize: 13, borderRadius: 8,
                  cursor: busy ? "default" : "pointer",
                  border: "1px solid " + (confirmStop ? "#c0392b" : C.line),
                  background: confirmStop ? "#fcebeb" : C.bg,
                  color: confirmStop ? "#c0392b" : C.text,
                },
              }, confirmStop ? "确认停止？" : "全部停止"),
              h("button", {
                onClick: () => setShowBase(!showBase),
                style: {
                  padding: 0, fontSize: 12, border: 0, background: "transparent",
                  color: C.dim, cursor: "pointer", textDecoration: "underline",
                },
              }, showBase ? "收起地址" : "改服务地址"),
            ),

            showBase ? h("div", { style: { marginTop: 16 } },
              h("div", { style: { fontSize: 12, marginBottom: 6 } },
                "服务地址（端口被改过就填这里，或点自动检测）"),
              h("div", { style: { display: "flex", gap: 8 } },
                h("input", {
                  value: baseInput,
                  onChange: (ev) => setBaseInput(ev.target.value),
                  onKeyDown: (ev) => { if (ev.key === "Enter") applyBase(); },
                  placeholder: DEFAULT_BASE,
                  spellCheck: false,
                  style: {
                    flex: 1, padding: "7px 10px", fontSize: 12, fontFamily: "inherit",
                    color: C.text, background: C.bg,
                    border: "1px solid " + C.line, borderRadius: 8,
                  },
                }),
                h("button", {
                  onClick: applyBase,
                  style: {
                    padding: "7px 16px", fontSize: 12, border: 0, borderRadius: 8,
                    background: C.accent, color: "#fff", cursor: "pointer",
                  },
                }, "连接"),
              ),
              h("button", {
                onClick: async () => {
                  try { localStorage.removeItem(BASE_KEY); } catch (e) { /* 忽略 */ }
                  try { localStorage.removeItem(BASE_MANUAL_KEY); } catch (e) { /* 忽略 */ }
                  baseLocked.current = false;
                  const found = await discoverBase();
                  BASE = found || DEFAULT_BASE;
                  setBaseInput(BASE);
                  setErr("");
                  load();
                },
                style: {
                  marginTop: 8, padding: 0, fontSize: 12, border: 0,
                  background: "transparent", color: C.dim, cursor: "pointer", textDecoration: "underline",
                },
              }, "自动检测"),
            ) : null,
          );
        }

        const engine = mode.engine || "tts";
        const list = voices.filter((v) => engineOf(v) === engine);
        const gsvOk = !!(mode.engines && mode.engines.gsv);
        const ttsOk = !!(mode.engines && mode.engines.tts);
        const stack = mode.stack || {};

        return h("div", {
          style: {
            flex: 1, minHeight: 0, overflowY: "auto",
            boxSizing: "border-box", maxWidth: 640, margin: "0 auto", width: "100%",
            padding: "28px 32px", fontSize: 14, color: C.text,
          },
        },
          h("div", { style: { fontSize: 16, fontWeight: 500, marginBottom: 4 } }, "语音"),
          h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 26 } },
            "和「打开语音」口令改的是同一份状态，两边自动同步。"),

          h(Field, { label: "语音模式" },
            h("button", {
              onClick: async () => {
                const next = !mode.enabled;
                const ok = await send({ enabled: next });
                if (!ok) {
                  // 8787 连不上：总开关交给宿主，底下走 mcp_server.set_voice_mode，
                  // 和「打开 mod1 / 关闭 mod1」是同一条路径。
                  try {
                    await hostCall("/voicehub/mode", { enabled: next });
                    setErr(next ? "正在拉起语音服务（首次约 1 分钟）…" : "正在停止服务…");
                    setTimeout(load, 3000);
                  } catch (e2) {
                    setErr(e2.message);
                  }
                }
              },
              disabled: busy,
              style: {
                padding: "8px 20px", fontSize: 13, borderRadius: 8, cursor: busy ? "default" : "pointer",
                border: "1px solid " + (mode.enabled ? "transparent" : C.line),
                background: mode.enabled ? C.accent : "transparent",
                color: mode.enabled ? "#fff" : C.text,
              },
            }, mode.enabled ? "已开启 · 点此关闭" : "已关闭 · 点此开启"),
          ),

          h(Field, { label: "朗读来源" },
            h(Seg, {
              items: [
                { value: "panel", label: "面板朗读", active: speechMode === "panel",
                  hint: "回合结束自动朗读，agent 不重复念" },
                { value: "mcp", label: "MCP 朗读", active: speechMode === "mcp",
                  hint: "由 AI 用 speak 工具朗读，面板不自动念" },
              ],
              onPick: async (value) => {
                const prev = speechMode;
                speechMode = value;   // 先本地生效，让下一个回合立刻按新来源走
                setMode(Object.assign({}, mode, { speech_mode: value }));
                const ok = await send({ speech_mode: value });
                if (!ok) {
                  // 8787 连不上：回滚本地状态，改走宿主
                  speechMode = prev;
                  setMode(Object.assign({}, mode, { speech_mode: prev }));
                  try {
                    await hostCall("/voicehub/mode", { speech_mode: value });
                    setErr("已提交切换，稍后自动刷新…");
                    setTimeout(load, 2000);
                  } catch (e2) {
                    setErr(e2.message);
                  }
                }
              },
            }),
            h("div", { style: { marginTop: 12, display: "flex", gap: 14 } },
              // 面板朗读：稳定、不漏，但不挑重点
              h("div", {
                style: {
                  flex: 1, padding: "10px 12px", borderRadius: 8,
                  border: "1px solid " + (speechMode === "panel" ? C.accent : C.line),
                  background: speechMode === "panel" ? "rgba(31,111,235,0.06)" : C.bg,
                },
              },
                h("div", { style: { fontSize: 12, fontWeight: 600, marginBottom: 4,
                  color: speechMode === "panel" ? C.accent : C.text } }, "面板朗读"),
                h("div", { style: { fontSize: 12, lineHeight: 1.6,
                  color: speechMode === "panel" ? C.text : C.dim } },
                  "优点：稳定、不依赖 AI 记性，每句都会念。"),
                h("div", { style: { fontSize: 12, lineHeight: 1.6,
                  color: speechMode === "panel" ? C.text : C.dim } },
                  "缺点：长文本整段照念，不挑重点，略显啰嗦。"),
              ),
              // MCP 朗读：有取舍，更聪明，但依赖 AI 每次记得调
              h("div", {
                style: {
                  flex: 1, padding: "10px 12px", borderRadius: 8,
                  border: "1px solid " + (speechMode === "mcp" ? C.accent : C.line),
                  background: speechMode === "mcp" ? "rgba(31,111,235,0.06)" : C.bg,
                },
              },
                h("div", { style: { fontSize: 12, fontWeight: 600, marginBottom: 4,
                  color: speechMode === "mcp" ? C.accent : C.text } }, "MCP 朗读"),
                h("div", { style: { fontSize: 12, lineHeight: 1.6,
                  color: speechMode === "mcp" ? C.text : C.dim } },
                  "优点：更懂语境，长文本自动提炼核心，不念代码/表格/长数字。"),
                h("div", { style: { fontSize: 12, lineHeight: 1.6,
                  color: speechMode === "mcp" ? C.text : C.dim } },
                  "缺点：依赖 AI 每轮主动调用，偶尔会漏念。"),
              ),
            ),
          ),

          h(Field, { label: "引擎" },
            h(Seg, {
              items: [
                { value: "gsv", label: "GSV 角色音色", active: engine === "gsv", disabled: !gsvOk,
                  hint: gsvOk ? "" : "GPT-SoVITS 没在跑" },
                { value: "tts", label: "系统 TTS", active: engine === "tts", disabled: !ttsOk,
                  hint: ttsOk ? "" : "edge-tts 和 SAPI 都不可用" },
              ],
              onPick: (value) => send({ engine: value }),
            }),
            !gsvOk ? h("div", { style: { fontSize: 12, color: C.dim, marginTop: 8 } },
              "GSV 不可用：需要 GPT-SoVITS 起在 127.0.0.1:" +
              ((stack.gsv && stack.gsv.port) || 9880) + "。") : null,
          ),

          h(Field, { label: "音量 / 语速" },
            h("div", { style: { display: "flex", gap: 18 } },
              h("div", { style: { flex: 1 } },
                h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 4 } },
                  "音量 " + Math.round(mode.volume * 100) + "%"),
                h("input", {
                  type: "range", min: 0.05, max: 1.0, step: 0.05,
                  value: mode.volume,
                  onChange: (ev) => send({ volume: Number(ev.target.value) }),
                  style: { width: "100%" },
                }),
              ),
              h("div", { style: { flex: 1 } },
                h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 4 } },
                  "语速 " + Number(mode.speed || 1).toFixed(1) + "x"),
                h("input", {
                  type: "range", min: 0.5, max: 2.0, step: 0.1,
                  value: mode.speed || 1,
                  onChange: (ev) => send({ speed: Number(ev.target.value) }),
                  style: { width: "100%" },
                }),
              ),
            ),
          ),

          h(Field, { label: "音色（" + list.length + " 个）" },
            h("div", { style: { display: "flex", gap: 8 } },
              h("select", {
                value: mode.voice,
                onChange: (ev) => send({ voice: ev.target.value }),
                style: {
                  flex: 1, padding: "8px 10px", fontSize: 13, color: C.text,
                  background: C.bg, border: "1px solid " + C.line, borderRadius: 8,
                },
              }, list.map((v) => h("option", { key: v.id, value: v.id },
                v.name + (v.ready === false ? "（未就绪）" : "")))),
              h("button", {
                onClick: preview,
                title: "试听当前音色",
                style: {
                  padding: "0 14px", fontSize: 13, borderRadius: 8, cursor: "pointer",
                  border: "1px solid " + C.line, background: C.bg, color: C.text,
                },
              }, "试听"),
            ),
          ),

          h(Field, { label: "三件套" },
            h("div", { style: { display: "flex", gap: 20 } },
              ["voicehub", "gsv", "rvc"].map((k) => {
                const s = stack[k] || {};
                const name = STACK_NAMES[k] || k;
                const label = !s.installed ? "未安装" : (s.running ? "运行中" : "未运行");
                const color = !s.installed ? "#c0392b" : (s.running ? "#1a9e5c" : "#b7791f");
                return h("div", { key: k, style: { fontSize: 13, color: C.text } },
                  h("div", { style: { fontWeight: 500 } }, name),
                  h("div", { style: { fontSize: 12, color } }, label),
                  k === "voicehub" ? null : h("button", {
                    onClick: () => svcAction(name, s.running ? "stop" : "start"),
                    disabled: busy || !s.installed,
                    style: {
                      marginTop: 6, padding: "3px 10px", fontSize: 11, borderRadius: 6,
                      cursor: (busy || !s.installed) ? "default" : "pointer",
                      border: "1px solid " + C.line, background: C.bg,
                      color: s.installed ? C.text : C.dim,
                    },
                  }, s.running ? "停止" : "启动"),
                );
              }),
            ),
            h("div", { style: { marginTop: 12 } },
              h("button", {
                onClick: () => setShowLoc(!showLoc),
                style: {
                  padding: "6px 14px", fontSize: 12, borderRadius: 6, cursor: "pointer",
                  border: "1px solid " + C.line, background: C.bg, color: C.text,
                },
              }, showLoc ? "收起" : "设置安装位置"),
            ),
            showLoc ? h("div", { style: { marginTop: 10 } },
              [["gsv", "GSV"], ["rvc", "RVC"]].map((pair) => {
                const key = pair[0];
                const label = pair[1];
                const val = key === "gsv" ? locGsv : locRvc;
                const setVal = key === "gsv" ? setLocGsv : setLocRvc;
                const ph = (mode.model_dirs && mode.model_dirs[key]) || "";
                return h("div", { key: key, style: { marginBottom: 8 } },
                  h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 4 } },
                    label + " 目录（留空 = 用默认）"),
                  h("input", {
                    value: val,
                    onChange: (ev) => setVal(ev.target.value),
                    placeholder: ph,
                    style: {
                      width: "100%", padding: "7px 10px", fontSize: 12, color: C.text,
                      background: C.bg, border: "1px solid " + C.line, borderRadius: 6,
                      boxSizing: "border-box",
                    },
                  }),
                );
              }),
              h("button", {
                onClick: saveLoc,
                disabled: busy,
                style: {
                  padding: "7px 16px", fontSize: 12, borderRadius: 6, cursor: busy ? "default" : "pointer",
                  border: "1px solid transparent", background: C.accent, color: "#fff",
                },
              }, "保存"),
            ) : null,
          ),

          h(Field, { label: "怎么加新模型" },
            h("div", { style: { fontSize: 12, color: C.dim, lineHeight: "1.7" } },
              "GSV 音色放 ",
              h("span", { style: { color: C.text } }, (mode.model_dirs && mode.model_dirs.gsv) || "models\\gsv"),
              "\\<名字>\\（ref.wav + prompt.txt，可选 .ckpt / .pth）",
              h("br"),
              "RVC 音色放 ",
              h("span", { style: { color: C.text } }, (mode.model_dirs && mode.model_dirs.rvc) || "models\\rvc"),
              "\\<名字>\\（.pth + .index）",
              h("br"),
              "放好后点下方「重新扫描」即可，不用重启。",
            ),
          ),

          h(Field, { label: "维护" },
            h("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" } },
              h("button", {
                onClick: rescan,
                disabled: busy,
                style: {
                  padding: "7px 16px", fontSize: 13, borderRadius: 8, cursor: busy ? "default" : "pointer",
                  border: "1px solid " + C.line, background: C.bg, color: C.text,
                },
              }, "重新扫描模型"),
              h("button", {
                onClick: restart,
                style: {
                  padding: "7px 16px", fontSize: 13, borderRadius: 8, cursor: "pointer",
                  border: "1px solid " + C.line, background: C.bg, color: C.text,
                },
              }, "重启服务"),
              h("button", {
                onClick: () => {
                  if (!confirmStop) {
                    setConfirmStop(true);
                    setTimeout(() => setConfirmStop(false), 4000);
                    return;
                  }
                  setConfirmStop(false);
                  stopAll();
                },
                disabled: busy,
                style: {
                  padding: "7px 16px", fontSize: 13, borderRadius: 8,
                  cursor: busy ? "default" : "pointer",
                  border: "1px solid " + (confirmStop ? "#c0392b" : C.line),
                  background: confirmStop ? "#fcebeb" : C.bg,
                  color: confirmStop ? "#c0392b" : C.text,
                },
              }, confirmStop ? "确认关闭全部？" : "关闭全部服务"),
            ),
            h("div", { style: { fontSize: 12, color: C.dim, marginTop: 10, lineHeight: 1.6 } },
              "「关闭全部服务」会停掉 VoiceHub / GSV / RVC 三个进程并关闭语音模式、释放显存。",
              "DSH 关掉之后这些进程仍在后台跑，要清干净就点它。"),
          ),

          h("div", { style: { fontSize: 12, color: C.dim, borderTop: "1px solid " + C.line, paddingTop: 14 } },
            "当前：" + (mode.voice_name || mode.voice) + "（" + mode.voice + "）",
            " · 音量 " + mode.volume,
            mode.mode ? " · 模式 " + mode.mode : "",
          ),

          err ? h("div", { style: { marginTop: 12, fontSize: 12, color: "#c0392b" } },
            err,
            h("button", {
              onClick: () => setShowBase(!showBase),
              style: {
                marginLeft: 8, padding: 0, fontSize: 12, border: 0, background: "transparent",
                color: C.dim, cursor: "pointer", textDecoration: "underline",
              },
            }, showBase ? "收起" : "改服务地址"),
            showBase ? h("div", { style: { marginTop: 8, display: "flex", gap: 8 } },
              h("input", {
                value: baseInput,
                onChange: (ev) => setBaseInput(ev.target.value),
                onKeyDown: (ev) => { if (ev.key === "Enter") applyBase(); },
                placeholder: DEFAULT_BASE,
                spellCheck: false,
                style: {
                  flex: 1, padding: "6px 9px", fontSize: 12, fontFamily: "inherit",
                  color: C.text, background: C.bg,
                  border: "1px solid " + C.line, borderRadius: 8,
                },
              }),
              h("button", {
                onClick: applyBase,
                style: {
                  padding: "6px 14px", fontSize: 12, border: 0, borderRadius: 8,
                  background: C.accent, color: "#fff", cursor: "pointer",
                },
              }, "连接"),
            ) : null,
          ) : null,
        );
      }

      function Icon(props) {
        const size = (props && props.size) || 18;
        return h("svg", {
          width: size, height: size, viewBox: "0 0 24 24", fill: "none",
          stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round",
        },
          h("path", { d: "M4 9v6h4l5 4V5L8 9H4z" }),
          h("path", { d: "M17 8.5a5 5 0 0 1 0 7" }),
        );
      }

      function apply(ctx) {
        // DSH 启动时 main 插槽要等外壳 UI 初始化后才声明，早注册会报
        // 「slot main is not declared」。带重试，等它就绪，否则偶发侧边栏不出现。
        let mainDone = false;
        let autoDone = false;
        const attempt = function (n) {
          try {
            const slots = ctx.get("slots");
            if (slots === undefined) {
              if (n < 8) { setTimeout(function () { attempt(n + 1); }, 400); return; }
              console.warn("voicehub-panel: slots 服务始终不可用");
              return;
            }
            if (!mainDone) {
              slots.register({ name: "main", key: PANEL, inject: () => ({}) }, Panel);
              mainDone = true;
            }
            if (!autoDone) {
              // 回合尾部插槽：插件自己触发朗读，不再等 agent 记得调工具。
              // 老版本 DSH 没有这个插槽，注册失败就退化为原行为。
              try {
                slots.inject("conversation.chat.turnTail", () => slots.register(
                  { name: "conversation.chat.turnTail", id: PANEL + "-autospeak", inject: () => ({}) },
                  AutoSpeak,
                ));
                autoDone = true;
              } catch (e) {
                /* 没有该插槽：忽略 */
              }
            }
            slots.inject("sidebar.panellist", () => slots.register(
              { name: "sidebar.panellist", id: PANEL, order: 6, label: () => "语音" },
              Icon,
            ));
          } catch (err) {
            console.error("voicehub-panel: apply() 第 " + n + " 次失败", err);
            if (n < 8) setTimeout(function () { attempt(n + 1); }, 400);
          }
        };
        attempt(0);
      }

      exports.apply = apply;
      exports.inject = ["slots"];
      // 自检用：只暴露纯函数，不碰 fetch / storage / Audio。
      exports.__test = {
        eventTime: eventTime,
        turnTime: turnTime,
        turnIdentity: turnIdentity,
        hashText: hashText,
        trimForSpeech: trimForSpeech,
        extractReply: extractReply,
        extractReplyInfo: extractReplyInfo,
        textFromBlocks: textFromBlocks,
        walkSpeechStrings: walkSpeechStrings,
      };
      return module.exports;
    } catch (err) {
      try { console.error("voicehub-panel: 注册失败", err); } catch { /* ignore */ }
      return module.exports;
    }
  },
});
