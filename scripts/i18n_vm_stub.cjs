// bot-group #333：执行 index.html 内联脚本片段的 vm 用例共用词表 stub。
// 从 frontend/i18n.js 提取简体中文文案，让测试上下文里的 t() 与浏览器
// zh-CN 行为一致（含 {n} 占位符）。英文文案不属于这些逻辑用例的断言范围。
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'i18n.js'), 'utf8');
const match = source.match(/const DICT = (\{[\s\S]*?\n  \});/);
if (!match) throw new Error('i18n.js DICT block not found');
const DICT = eval('(' + match[1] + ')');

const format = (text, args) => String(text).replace(/\{(\d+)\}/g, (whole, index) =>
  args && args[Number(index)] !== undefined ? String(args[Number(index)]) : whole);

module.exports = (key, args) => format((DICT[key] || {zh: key}).zh, args);
