/**
 * VoiceHub 面板的宿主半边。
 *
 * 前端跑在浏览器里，读不到本机 config.json，所以它原本只能把服务地址写死。
 * 这里注册一个同源接口 /voicehub/endpoint，把服务真正监听的端口告诉前端——
 * 前端用相对路径请求，不需要知道端口，也不用用户手填。
 *
 * 端口每次请求现读 config.json：用户改完端口重启服务，面板下次轮询就自己接上，
 * 连 DSH 都不用重启。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const name = "voxrelay";

const ENDPOINT = "/voicehub/endpoint";

const HERE = dirname(fileURLToPath(import.meta.url));

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
        web.effect(
          () =>
            web.webServer.register({
              kind: "exact",
              path: ENDPOINT,
              handler: (req, res) => {
                if (req.method !== "GET" && req.method !== "HEAD") {
                  res.writeHead(405);
                  res.end();
                  return;
                }
                respond(res);
              },
            }),
          "voicehub-panel: /voicehub/endpoint",
        );
      } catch {
        /* 没有 webServer 或注册失败：面板照常用，只是要手填地址 */
      }
    });
  } catch {
    /* 宿主 API 差异：降级，不影响面板加载 */
  }
}
