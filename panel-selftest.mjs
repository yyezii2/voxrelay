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
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

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

/** 某个目录里的端口：优先 endpoint.json（本进程 pid 保证是活的），其次 config。 */
function portOf(home) {
  const read = (name) => JSON.parse(readFileSync(join(home, name), "utf8").replace(/^\uFEFF/, ""));
  try {
    return { port: Number(read("endpoint.json").port), src: "endpoint.json" };
  } catch {
    /* 没有 endpoint.json，往下走 */
  }
  for (const name of ["config.json", "config.example.json"]) {
    try {
      return { port: Number(read(name).port), src: name };
    } catch {
      /* 试下一个 */
    }
  }
  return null;
}

check("环境变量指到哪，端点就跟到哪", () => {
  // 造一个假的服务目录：endpoint.json 里的 pid 用当前进程，保证 pid 校验能过
  const home = mkdtempSync(join(tmpdir(), "voxrelay-selftest-"));
  writeFileSync(join(home, "endpoint.json"),
    JSON.stringify({ host: "127.0.0.1", port: 19999, pid: process.pid }));

  const prev = process.env.VOICEHUB_HOME;
  process.env.VOICEHUB_HOME = home;
  try {
    const res = fakeRes();
    globalThis.__route.handler({ method: "GET" }, res);
    const got = res.json();
    if (got.home !== home) throw new Error(`home 是 ${got.home}，不是环境变量指的目录`);
    if (got.port !== 19999) throw new Error(`端口是 ${got.port}，应该 19999`);
    return "19999（endpoint.json）";
  } finally {
    if (prev === undefined) delete process.env.VOICEHUB_HOME;
    else process.env.VOICEHUB_HOME = prev;
    rmSync(home, { recursive: true, force: true });
  }
});

check("端点返回的端口和它那个目录的配置自洽", () => {
  // 不假定本机服务在哪：返回哪个目录，就用那个目录的配置核对
  const res = fakeRes();
  globalThis.__route.handler({ method: "GET" }, res);
  const got = res.json();
  if (!got || !got.home) throw new Error("没算出服务目录: " + JSON.stringify(got));
  const expect = portOf(got.home);
  if (!expect) throw new Error(`${got.home} 里既没有 endpoint.json 也没有 config`);
  if (got.port !== expect.port) {
    throw new Error(`${got.home}: 接口 ${got.port} vs ${expect.src} ${expect.port}`);
  }
  return `${got.home} → ${got.port}（${expect.src}）`;
});

check("config.example.json 可解析", () => {
  const cfg = JSON.parse(readFileSync(join(HERE, "config.example.json"), "utf8").replace(/^\uFEFF/, ""));
  if (!cfg.port) throw new Error("没有 port");
  return "port " + cfg.port;
});

if (fails.length) {
  console.log(`\n失败 ${fails.length} 项：${fails.join(", ")}`);
  process.exit(1);
}
console.log("\n全部通过。");
