// webui/index.html 用餐图标判据回归测试
// 运行：<node> scripts/test_webui_food_mark.js（沙箱内 node 绝对路径示例：
//   D:\Users\xgwu\.workbuddy\binaries\node\versions\22.22.2-3\node.exe）
// 覆盖：script 语法完整性 / 中文未损坏 / 标签配对 / isFoodStop·foodMark 语义 /
//       图标位于名称之前的模板顺序断言。
const fs = require("fs");
const path = require("path");
const P = path.join(__dirname, "..", "webui", "index.html");
const html = fs.readFileSync(P, "utf8");
let fail = 0;
const ok = (cond, msg) => { if (!cond) fail++; console.log(`${cond ? "OK  " : "FAIL"} ${msg}`); };

// 1) 文件完整性
ok(html.indexOf("\uFFFD") < 0, "无 U+FFFD 替换字符（中文未损坏）");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("FAIL 未找到 script 块"); process.exit(1); }
const src = m[1];
try { new Function(src); ok(true, "script 语法通过"); }
catch (e) { ok(false, "script 语法：" + e.message); }
const cnt = (re) => (html.match(re) || []).length;
for (const [n, o, c] of [["div", /<div\b/g, /<\/div>/g], ["tr", /<tr\b/g, /<\/tr>/g],
                         ["td", /<td\b/g, /<\/td>/g], ["table", /<table\b/g, /<\/table>/g]]) {
  ok(cnt(o) === cnt(c), `<${n}> 开闭配对 ${cnt(o)}/${cnt(c)}`);
}

// 2) 判据语义
const hs = src.match(/function isFoodStop[\s\S]*?\n\}/)[0] + "\n" +
           src.match(/function foodMark[\s\S]*?\n\}/)[0];
const f = new Function(hs + "; return {isFoodStop, foodMark};")();
const cases = [
  ["餐厅 POI（timeline 带 meal=lunch）", { type: "poi", id: "X", name: "双塔市集", meal: "lunch" }, null, true],
  ["餐厅 POI（meal=dinner）", { type: "poi", id: "X", name: "得月楼", meal: "dinner" }, null, true],
  ["餐厅 POI（无 meal，meta category=food）", { type: "poi", id: "Z", name: "裕面堂" }, { pois: { Z: { id: "Z", category: "food" } } }, true],
  ["普通 POI（无 meal，旧快照 meta 无 category）", { type: "poi", id: "Y", name: "拙政园" }, { pois: { Y: { id: "Y" } } }, false],
  ["普通 POI（meta category=view）", { type: "poi", id: "Y", name: "渔洋山" }, { pois: { Y: { id: "Y", category: "view" } } }, false],
  ["通用餐块 type=meal（不是 FOOD POI 判据）", { type: "meal", name: "午餐" }, null, false],
  ["酒店 type=hotel", { type: "hotel", name: "返回酒店" }, null, false],
  ["meta.pois 缺失（分享快照异常）不抛错", { type: "poi", id: "Q", name: "某点" }, undefined, false],
];
for (const [nm, s, meta, exp] of cases) {
  const got = f.isFoodStop(s, meta), mark = f.foodMark(s, meta);
  ok(got === exp, `${nm} → isFood=${got} mark="${mark}"（期望 ${exp}）`);
}

// 3) 三处渲染的图标都必须在名称之前（用户诉求：文字前加图标）
const row = (html.match(/return `<div class="tlrow">([\s\S]*?)<\/div>`;/) || [])[1] || "";
ok(row.indexOf("${tag?tag") >= 0 && row.indexOf("${tag?tag") < row.indexOf("${s.name}"),
   "卡片时间轴：图标在 ${s.name} 之前");
const trAll = [...html.matchAll(/return `<tr>[\s\S]*?<\/tr>`;/g)].map((x) => x[0]);
ok(trAll.some((t) => t.includes('?tag+" ":""}${s.name}')), "打印/PDF 表格：图标紧贴 ${s.name} 之前");
const png = (html.match(/const pre=\(isFoodStop[\s\S]*?const nm=pre\+s\.name;/) || [])[0] || "";
ok(png.length > 0, "长图导出：图标前置于 name");
ok(/isFoodStop\(s,LAST\.city_meta\)/.test(html), "ICS 导出：使用统一判据");

console.log("\n" + (fail ? `失败 ${fail} 项` : "全部通过"));
process.exit(fail ? 1 : 0);
