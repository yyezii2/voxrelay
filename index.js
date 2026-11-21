/**
 * VoiceHub 面板的宿主半边。
 *
 * 前端跑在浏览器里，读不到本机 config.json，所以它原本只能把服务地址写死。
 * 这里注册一个同源接口 /voicehub/endpoint，把服务真正监听的端口告诉前端——
 * 前端用相对路径请求，不需要知道端口，也不用用户手填。
 *
 * 端口每次请求现读 config.json：用户改完端口重启服务，面板下次轮询就自己接上，
 * 连 DSH 都不用重启。
 *
 * 除端点发现外，这里还提供「控制层」：
 *   GET  /voicehub/status   三件套状态 + 语音模式（不依赖 8787）
 *   POST /voicehub/service  {name, action} 启停单个组件
 *   POST /voicehub/mode     {enabled} 总开关
 *
 * 为什么放这儿：8787 本身就是被控制的对象，服务一停，前端所有 /v1/* 接口全失效，
 * 面板只剩一个「填地址」的空壳，填了也拉不起服务。宿主跑在 DSH 进程里，永远在，
 * 所以由它来当「拉起服务的那只手」。
 *
 * 这里不重写任何启停判断——start/stop 转手调 voice_services，总开关转手调
 * mcp_server.set_voice_mode。目的是让面板、MCP、计划任务几个入口共用同一套
 * 幂等逻辑和同一份状态（services.json 意图 + 端口实际）。
 */
import {
  readFileSync,
  existsSync,
  mkdirSync,
  openSync,
  closeSync,
  appendFileSync,
} from "node:fs";
import { execFile, spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const name = "voxrelay";

const ENDPOINT = "/voicehub/endpoint";
const STATUS_PATH = "/voicehub/status";
const SERVICE_PATH = "/voicehub/service";
const MODE_PATH = "/voicehub/mode";

const HERE = dirname(fileURLToPath(import.meta.url));

// 只允许操作这三个组件、这三种动作。宿主能起进程，边界必须写死。
const ALLOWED_NAMES = ["GSV", "VoiceHub", "RVC"];
const ALLOWED_ACTIONS = ["start", "stop", "restart"];

/**
 * 服务目录候选。按顺序找第一个带 config.json 的：
 *  1. VOICEHUB_HOME / VOXRELAY_HOME 环境变量（服务装在别处时由用户指过来）
 *  2. HERE 自己——发布包形态，面板和服务就在同一个目录里
 *  3. D:\VoiceHub（本机旧默认安装位置）
 *  4. 插件旁边的 voxrelay / voicehub（面板单独安装、服务在隔壁时）
 */
function homeCandidates() {
  return [
    process.env.VOXRELAY_HOME || process.env.VOICEHUB_HOME,
    HERE,
    "D:\\VoiceHub",
    join(HERE, "..", "voxrelay"),
    join(HERE, "..", "voicehub"),
  ].filter((p) => typeof p === "string" && p.length > 0);
}

/**
 * 读服务启动时写下的 endpoint.json——这是唯一能反映「实际」监听的来源。
 * 必须校验 pid：服务被强杀时文件会残留，照着它连就是连一个死端口。
 */
function readEndpointFile(home) {
  try {
    const d = JSON.parse(readFileSync(join(home, "endpoint.json"), "utf8"));
    const port = Number(d && d.port);
    if (!Number.isFinite(port) || port <= 0) return null;
    const pid = Number(d && d.pid);
    if (Number.isFinite(pid) && pid > 0) {
      try {
        process.kill(pid, 0);
      } catch (error) {
        // EPERM 表示进程还在、只是没权限发信号；ESRCH 才是真没了
        if (!error || error.code !== "EPERM") return null;
      }
    }
    return { home, port, host: (d && d.host) || "127.0.0.1" };
  } catch {
    return null;
  }
}

function readServiceInfo() {
  const homes = homeCandidates();
  // 1. 服务实际写下的地址最可信（用 --port 覆盖时也只有它是对的）
  for (const home of homes) {
    const live = readEndpointFile(home);
    if (live) return live;
  }
  // 2. 服务没在跑就退回配置里的端口：地址是对的，只是连不上
  for (const home of homes) {
    for (const file of ["config.json", "config.example.json"]) {
      try {
        const cfg = JSON.parse(readFileSync(join(home, file), "utf8"));
        const port = Number(cfg && cfg.port);
        if (Number.isFinite(port) && port > 0) {
          return { home, port, host: (cfg && cfg.host) || "127.0.0.1" };
        }
      } catch {
        /* 这个候选不成立，试下一个 */
      }
    }
  }
  return null;
}

/**
 * 真正要操作的那套服务在哪个目录。
 *
 * readServiceInfo() 在「服务没跑」时会退回第一个带 config.json 的候选，
 * 那个未必是能启动的那套（比如仓库副本没有解释器、也没有 rvc/py310）。
 * 启停必须认得准，否则会拉起一个不存在的实例。所以按能启动的条件再选一次：
 *  1. 正在跑的那个（endpoint.json 的 pid 还活着）
 *  2. 有 rvc/py310/python.exe 的
 *  3. 有 runtime/python.exe 的
 *  4. 兜底第一个候选
 */
function serviceHome() {
  for (const home of homeCandidates()) {
    if (readEndpointFile(home)) return home;
  }
  for (const home of homeCandidates()) {
    if (existsSync(join(home, "rvc", "py310", "python.exe"))) return home;
  }
  for (const home of homeCandidates()) {
    if (existsSync(join(home, "runtime", "python.exe"))) return home;
  }
  return homeCandidates()[0] || null;
}

/** 优先主运行环境；它是个软链，断了还有 RVC 那个真实解释器兜底。 */
function pickPython(home) {
  const cands = [
    join(home, "runtime", "python.exe"),
    join(home, "rvc", "py310", "python.exe"),
    "python",
  ];
  for (const p of cands) {
    if (p === "python" || existsSync(p)) return p;
  }
  return "python";
}

/* ---------------------------------------------------------------- 转手调用 */

// 状态查询要等结果（前端靠它渲染），所以同步等，但给足超时。
// voice_services.status() 只探三个端口，正常一秒内返回。
const STATUS_CODE = [
  "import json",
  "import voice_services as V",
  "print(json.dumps({'services': V.status()}))",
].join("\n");

function runStatus(home) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (!done) {
        done = true;
        resolve(v);
      }
    };
    try {
      execFile(
        pickPython(home),
        ["-c", STATUS_CODE],
        { cwd: home, timeout: 8000, windowsHide: true, encoding: "utf8" },
        (err, stdout) => {
          let services = null;
          try {
            // 只取最后一行：解释器警告之类可能混在 stdout 前面
            const line = String(stdout || "").trim().split("\n").pop() || "";
            services = JSON.parse(line).services;
          } catch {
            services = null;
          }
          finish({
            ok: !!services,
            services,
            error: err ? String(err.message || err).slice(0, 200) : null,
          });
        },
      );
    } catch (e) {
      finish({ ok: false, services: null, error: String(e && e.message).slice(0, 200) });
    }
  });
}

/**
 * 起停是长任务（start 最多等 90 秒），必须放后台，别把 HTTP 请求挂住。
 *
 * 输出不能丢：以前用 stdio:"ignore"，Python 报错（import 失败、解释器路径不对）
 * 完全看不到，用户只感觉「点了没反应」。现在 stdout/stderr 都落到
 * logs/panel-control.log，开头再记一行「发起了什么」方便对照。
 */
function runBackground(home, code) {
  const logDir = join(home, "logs");
  const logPath = join(logDir, "panel-control.log");
  let fd = null;
  try {
    mkdirSync(logDir, { recursive: true });
    appendFileSync(
      logPath,
      "\n[" + new Date().toISOString() + "] 发起: " + code.replace(/\n/g, " | ") + "\n",
      "utf8",
    );
    fd = openSync(logPath, "a");
  } catch {
    fd = null; // 日志写不了不能影响启停本身
  }
  const child = spawn(pickPython(home), ["-c", code], {
    cwd: home,
    detached: true,
    windowsHide: true,
    stdio: ["ignore", fd === null ? "ignore" : fd, fd === null ? "ignore" : fd],
  });
  // unref：DSH 退出时不替这个进程收尸，让 voice_services 里的 DETACHED 子进程自己活着
  if (child && typeof child.unref === "function") child.unref();
  // 父进程关掉自己那份句柄，子进程仍持有
  if (fd !== null) {
    try {
      closeSync(fd);
    } catch {
      /* 忽略 */
    }
  }
}

function serviceCode(component, action) {
  return [
    "import voice_services as V",
    "r = V." + action + "(" + JSON.stringify(component) + ")",
  ].join("\n");
}

// 总开关不自己实现：直接调 MCP 那个 set_voice_mode，和「打开 mod1」完全同一条路径。
// 注意：enabled 只在显式传了时才写（undefined 表示「只切朗读来源，别动总开关」）。
function modeCode(enabled, speechMode) {
  const lines = ["import mcp_server as M"];
  if (speechMode === "panel" || speechMode === "mcp") {
    lines.push("M.set_speech_mode(" + JSON.stringify(speechMode) + ")");
  }
  if (typeof enabled === "boolean") {
    lines.push("M.set_voice_mode(enabled=" + (enabled ? "True" : "False") + ")");
  }
  return lines.join("\n");
}

function readMode(home) {
  try {
    const d = JSON.parse(readFileSync(join(home, "voice_mode.json"), "utf8"));
    return {
      enabled: d.enabled !== false,
      voice: d.voice || null,
      mode: d.mode || null,
      speech_mode: d.speech_mode === "mcp" ? "mcp" : "panel",
    };
  } catch {
    return { enabled: true, voice: null, mode: null, speech_mode: "panel" };
  }
}

/* ---------------------------------------------------------------- HTTP 小工具 */

function jsonRes(res, code, obj) {
  try {
    res.writeHead(code, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(JSON.stringify(obj));
  } catch {
    /* 连接已断，忽略 */
  }
}

/**
 * 挡掉跨站请求。
 *
 * 控制接口能让本机起停进程，不能让随便一个网页就能 POST 过来。
 * 浏览器发跨源请求一定会带 Origin；本机工具（curl、脚本）通常不带。
 * 只拒绝「明确是 http(s) 且不是回环」的 Origin——file:// / 自定义协议 / 解析不了的
 * 一律放行，宁可少挡一层，也不能把面板自己弄坏。
 */
function sameOrigin(req) {
  const origin = req && req.headers && req.headers.origin;
  if (!origin || origin === "null") return true;
  try {
    const u = new URL(String(origin));
    if (u.protocol !== "http:" && u.protocol !== "https:") return true;
    return u.hostname === "127.0.0.1" || u.hostname === "localhost" || u.hostname === "::1";
  } catch {
    return true;
  }
}

/**
 * 只收 application/json。
 *
 * 这条比 Origin 更硬：攻击者绕开预检最常用的办法是用 text/plain 发 JSON
 * （text/plain 属于 CORS 简单请求，不触发 preflight）。限定 Content-Type 之后，
 * 那种请求直接 415，副作用不会发生。
 */
function isJsonBody(req) {
  const ct = String((req && req.headers && req.headers["content-type"]) || "").toLowerCase();
  return ct.indexOf("application/json") >= 0;
}

function readBody(req, cb) {
  let raw = "";
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > 8192) {
      // 控制指令都很小，超了就是异常，直接掐断
      try {
        req.destroy();
      } catch {
        /* 忽略 */
      }
      return;
    }
    raw += chunk;
  });
  req.on("end", () => {
    let body = {};
    try {
      body = raw ? JSON.parse(raw) : {};
    } catch {
      body = {};
    }
    cb(body || {});
  });
  req.on("error", () => cb({}));
}

async function handleStatus(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") return jsonRes(res, 405, { ok: false });
  if (!sameOrigin(req)) return jsonRes(res, 403, { ok: false, error: "拒绝跨站请求" });
  const home = serviceHome();
  if (!home) return jsonRes(res, 200, { ok: false, error: "找不到服务目录" });
  const st = await runStatus(home);
  const services = st.services || null;
  // 端口可能和 config.json 不一致（改过端口还没重启、或用 --port 覆盖过启动），
  // endpoint.json 才是实际监听的那个，以它为准修正 VoiceHub 的状态。
  const ep = readEndpointFile(home);
  if (services && services.VoiceHub && ep) {
    services.VoiceHub.port = ep.port;
    services.VoiceHub.running = true;
  }
  const info = readServiceInfo();
  jsonRes(res, 200, {
    ok: true,
    home,
    base: info
      ? "http://" + (info.host === "0.0.0.0" ? "127.0.0.1" : info.host) + ":" + info.port
      : null,
    services,
    mode: readMode(home),
    error: st.error || null,
  });
}

function handleService(req, res) {
  if (req.method !== "POST") return jsonRes(res, 405, { ok: false });
  if (!sameOrigin(req)) return jsonRes(res, 403, { ok: false, error: "拒绝跨站请求" });
  if (!isJsonBody(req)) return jsonRes(res, 415, { ok: false, error: "只接受 application/json" });
  readBody(req, (body) => {
    const comp = String(body.name || "").trim();
    const action = String(body.action || "").trim().toLowerCase();
    const home = serviceHome();
    if (!home) return jsonRes(res, 200, { ok: false, error: "找不到服务目录" });
    if (ALLOWED_NAMES.indexOf(comp) < 0) {
      return jsonRes(res, 400, { ok: false, error: "不认识的组件：" + comp });
    }
    if (ALLOWED_ACTIONS.indexOf(action) < 0) {
      return jsonRes(res, 400, { ok: false, error: "不支持的操作：" + action });
    }
    try {
      runBackground(home, serviceCode(comp, action));
      // 立刻返回：真正的启动在后台跑，前端轮询 status 看结果
      jsonRes(res, 200, { ok: true, accepted: true, name: comp, action, home });
    } catch (e) {
      jsonRes(res, 200, { ok: false, error: String(e && e.message).slice(0, 200) });
    }
  });
}

function handleMode(req, res) {
  if (req.method !== "POST") return jsonRes(res, 405, { ok: false });
  if (!sameOrigin(req)) return jsonRes(res, 403, { ok: false, error: "拒绝跨站请求" });
  if (!isJsonBody(req)) return jsonRes(res, 415, { ok: false, error: "只接受 application/json" });
  readBody(req, (body) => {
    const home = serviceHome();
    if (!home) return jsonRes(res, 200, { ok: false, error: "找不到服务目录" });
    const enabled = typeof body.enabled === "boolean" ? body.enabled : undefined;
    const speechMode = body.speech_mode === "panel" || body.speech_mode === "mcp" ? body.speech_mode : null;
    try {
      runBackground(home, modeCode(enabled, speechMode));
      jsonRes(res, 200, { ok: true, accepted: true, enabled, speech_mode: speechMode, home });
    } catch (e) {
      jsonRes(res, 200, { ok: false, error: String(e && e.message).slice(0, 200) });
    }
  });
}

/** 路由处理：任何异常都降级成「没有地址」，不让连接悬着。 */
function respond(res) {
  try {
    const info = readServiceInfo();
    // host 是 0.0.0.0 时浏览器连不上，统一换回回环地址
    const host = info && info.host === "0.0.0.0" ? "127.0.0.1" : info && info.host;
    const body = JSON.stringify({
      base: info ? "http://" + host + ":" + info.port : null,
      port: info ? info.port : null,
      home: info ? info.home : null,
    });
    res.writeHead(200, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(body);
  } catch (error) {
    try {
      res.writeHead(500, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ base: null, error: String(error && error.message) }));
    } catch {
      /* 连接已断，忽略 */
    }
  }
}

/** 兜一层：任何 handler 抛错都别把 DSH 的 webServer 带崩。 */
function safe(fn) {
  return (req, res) => {
    try {
      const ret = fn(req, res);
      if (ret && typeof ret.catch === "function") {
        ret.catch(() => jsonRes(res, 200, { ok: false, error: "内部错误" }));
      }
    } catch {
      jsonRes(res, 200, { ok: false, error: "内部错误" });
    }
  };
}

export function apply(ctx) {
  // 这个函数里的每一步都可能缺 API 或者抛错。原则只有一条：
  // **绝不能让 apply 抛出去**——插件加载失败意味着前端 bundle 不进 boot manifest，
  // 面板会静默消失，而且看不到任何报错。最坏也只能是少了自动发现。
  try {
    // 用动态注入而不是静态 inject 声明：静态 inject 不满足时插件会挂在 pending，
    // 前端整个不加载。动态注入让插件照常加载，webServer 就绪后再挂路由。
    if (typeof ctx.inject !== "function") return;
    ctx.inject(["webServer"], (web) => {
      try {
        if (!web || !web.webServer || typeof web.webServer.register !== "function") return;
        const routes = [
          {
            path: ENDPOINT,
            tag: "voicehub-panel: " + ENDPOINT,
            handler: (req, res) => {
              if (req.method !== "GET" && req.method !== "HEAD") {
                res.writeHead(405);
                res.end();
                return;
              }
              respond(res);
            },
          },
          { path: STATUS_PATH, tag: "voicehub-panel: " + STATUS_PATH, handler: safe(handleStatus) },
          { path: SERVICE_PATH, tag: "voicehub-panel: " + SERVICE_PATH, handler: safe(handleService) },
          { path: MODE_PATH, tag: "voicehub-panel: " + MODE_PATH, handler: safe(handleMode) },
        ];
        for (const route of routes) {
          web.effect(
            () => web.webServer.register({ kind: "exact", path: route.path, handler: route.handler }),
            route.tag,
          );
        }
      } catch {
        /* 没有 webServer 或注册失败：面板照常用，只是要手填地址 */
      }
    });
  } catch {
    /* 宿主 API 差异：降级，不影响面板加载 */
  }
}
