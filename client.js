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

      function Panel() {
        const [mode, setMode] = React.useState(null);
        const [voices, setVoices] = React.useState([]);
        const [err, setErr] = React.useState("");
        const [busy, setBusy] = React.useState(false);
        const [showLoc, setShowLoc] = React.useState(false);
        const [locGsv, setLocGsv] = React.useState("");
        const [locRvc, setLocRvc] = React.useState("");
        const [baseInput, setBaseInput] = React.useState(BASE);
        const [showBase, setShowBase] = React.useState(false);
        const locInit = React.useRef(false);
        // 用户手填过地址就锁住，别再被自动探测覆盖；点「自动检测」会解锁
        const baseLocked = React.useRef(false);

        const load = React.useCallback(async () => {
          try {
            const [m, v] = await Promise.all([call("/v1/mode"), call("/v1/audio/voices")]);
            setMode(m);
            setVoices(v.voices || []);
            setErr("");
            if (!locInit.current && m && m.locations) {
              setLocGsv(m.locations.gsv || "");
              setLocRvc(m.locations.rvc || "");
              locInit.current = true;
            }
          } catch (ex) {
            setErr("连不上服务（" + BASE + "）：" + ex.message);
            // 连不上就问一次宿主：用户可能刚把端口改了、服务重启了。
            // 下一轮轮询就会用上新地址，不用重启 DSH。
            if (!baseLocked.current) {
              const found = await discoverBase();
              if (found && found !== BASE) {
                BASE = found;
                setBaseInput(found);
                setErr("");
              }
            }
          }
        }, []);

        React.useEffect(() => {
          let cancelled = false;
          const boot = async () => {
            // 用户手动填过地址就听他的；没填过就问宿主真实端口
            let saved = null;
            try { saved = localStorage.getItem(BASE_KEY); } catch (e) { /* 忽略 */ }
            if (saved) {
              baseLocked.current = true;
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
          } catch (ex) {
            setErr(ex.message);
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

        const svcAction = async (name, action) => {
          setBusy(true);
          try {
            await call("/v1/service", { name: name, action: action });
            setErr("");
            await load();
          } catch (ex) {
            setErr(ex.message);
          } finally {
            setBusy(false);
          }
        };

        if (!mode) {
          // 连不上的时候不能只丢一行错误：用户很可能是改过 config.json 的 port。
          // 这里给一个地址框，填一次存进 localStorage，下次自动用。
          return h("div", { style: { padding: 32, fontSize: 13, color: C.dim } },
            h("div", null, err || "正在连接 VoiceHub…"),
            err ? h("div", { style: { marginTop: 16 } },
              h("div", { style: { fontSize: 12, marginBottom: 6 } },
                "服务地址（改过 config.json 的 port 就填在这里）"),
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
          style: { maxWidth: 640, padding: "28px 32px", fontSize: 14, color: C.text },
        },
          h("div", { style: { fontSize: 16, fontWeight: 500, marginBottom: 4 } }, "语音"),
          h("div", { style: { fontSize: 12, color: C.dim, marginBottom: 26 } },
            "和「打开语音」口令改的是同一份状态，两边自动同步。"),

          h(Field, { label: "语音模式" },
            h("button", {
              onClick: () => send({ enabled: !mode.enabled }),
              disabled: busy,
              style: {
                padding: "8px 20px", fontSize: 13, borderRadius: 8, cursor: busy ? "default" : "pointer",
                border: "1px solid " + (mode.enabled ? "transparent" : C.line),
                background: mode.enabled ? C.accent : "transparent",
                color: mode.enabled ? "#fff" : C.text,
              },
            }, mode.enabled ? "已开启 · 点此关闭" : "已关闭 · 点此开启"),
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
            h("div", { style: { display: "flex", gap: 8 } },
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
            ),
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
      return module.exports;
    } catch (err) {
      try { console.error("voicehub-panel: 注册失败", err); } catch { /* ignore */ }
      return module.exports;
    }
  },
});
