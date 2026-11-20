/**
 * 面板宿主半边的自检：不启动 DSH，用假的 ctx / res 走一遍。
 *
 *   node panel-selftest.mjs
 *
 * 检查三件事：
 *   1. 包名与客户端 id 对得上（DSH 靠这个把前端 bundle 认成同一插件）
 *   2. apply() 不抛错，并且真的注册了 /voicehub/endpoint
 *   3. 接口能把「服务在哪」算出来：优先 endpoint.json，其次 config.json
 *
 * 退出码 0 = 通过。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const HERE = dirname(fileURLToPath(import.meta.url));
const NAME = JSON.parse(readFileSync(join(HERE, "package.json"), "utf8")).name;
const fails = [];

function check(label, fn) {
  try {
    const detail = fn();
    console.log(`  PASS  ${label}${detail ? "  " + detail : ""}`);
  } catch (error) {
    console.log(`  FAIL  ${label}\n        ${error && error.message}`);
    fails.push(label);
  }
}

/** 从 client.js 里抠出 __ModuleLoader__ 的 id，不执行浏览器代码。 */
function clientId() {
  const src = readFileSync(join(HERE, "client.js"), "utf8");
  const m = src.match(/__ModuleLoader__\.load\(\s*\{[\s\S]{0,200}?id:\s*["']([^"']+)["']/);
  return m ? m[1] : null;
}

/** 假的 res：把 JSON 收下来。 */
function fakeRes() {
  const out = { status: 0, body: "", headers: {} };
  return {
    out,
    writeHead(status, headers) {
      out.status = status;
      Object.assign(out.headers, headers || {});
    },
    end(chunk) {
      out.body += chunk ? String(chunk) : "";
    },
    json() {
      return JSON.parse(out.body || "null");
    },
  };
}

const mod = await import("./index.js");

check("包名与 client 注册 id 一致", () => {
  const id = clientId();
  if (!id) throw new Error("client.js 里找不到 __ModuleLoader__.load id");
  if (id !== NAME) throw new Error(`package.json 是 ${NAME}，client.js 是 ${id}`);
  return id;
});

check("导出 name 与包名一致", () => {
  if (mod.name !== NAME) throw new Error(`index.js 导出 ${mod.name}`);
  return mod.name;
});

check("apply() 不抛错并注册路由", () => {
  let route = null;
  const ctx = {
    inject(deps, cb) {
      if (!deps.includes("webServer")) throw new Error("没声明 webServer 依赖");
      cb({
        webServer: {
          register(r) {
            route = r;
            return { dispose() {} };
          },
        },
        effect(fn) {
          fn();
        },
      });
    },
  };
  mod.apply(ctx);
  if (!route) throw new Error("没有调用 webServer.register");
  if (route.path !== "/voicehub/endpoint") throw new Error("路径是 " + route.path);
  globalThis.__route = route;
  return route.path;
});

check("GET /voicehub/endpoint 返回服务地址", () => {
  const route = globalThis.__route;
  if (!route) throw new Error("上一步没拿到路由");
  const res = fakeRes();
  route.handler({ method: "GET" }, res);
  if (res.out.status !== 200) throw new Error("HTTP " + res.out.status);
  const data = res.json();
  if (!data || typeof data.base !== "string" || !data.base.startsWith("http://")) {
    throw new Error("base 不对: " + JSON.stringify(data));
  }
  return `${data.base}  home=${data.home}`;
});

check("非 GET 被拒", () => {
  const res = fakeRes();
  globalThis.__route.handler({ method: "POST" }, res);
  if (res.out.status !== 405) throw new Error("HTTP " + res.out.status);
  return "405";
});

check("端点算出来的端口与 config.json 一致", () => {
  const res = fakeRes();
  globalThis.__route.handler({ method: "GET" }, res);
  const data = res.json();
  const cfg = JSON.parse(readFileSync(join(HERE, "config.json"), "utf8").replace(/^\uFEFF/, ""));
  if (data.port !== Number(cfg.port)) throw new Error(`接口 ${data.port} vs 配置 ${cfg.port}`);
  return String(data.port);
});

check("仓库里 config.example.json 可解析", () => {
  const cfg = JSON.parse(readFileSync(join(HERE, "config.example.json"), "utf8").replace(/^\uFEFF/, ""));
  if (!cfg.port) throw new Error("没有 port");
  return "port " + cfg.port;
});

if (fails.length) {
  console.log(`\n失败 ${fails.length} 项：${fails.join(", ")}`);
  process.exit(1);
}
console.log("\n全部通过。");
