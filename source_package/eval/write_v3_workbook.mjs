import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// The bundled Codex runtime exposes artifact-tool through NODE_PATH.  Native
// ESM package resolution ignores NODE_PATH, so resolve it with the CommonJS
// resolver first and then import the resolved ESM entry point.
const require = createRequire(import.meta.url);
const artifactEntry = require.resolve("@oai/artifact-tool");
const { FileBlob, SpreadsheetFile } = await import(pathToFileURL(artifactEntry).href);

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i += 2) out[argv[i].replace(/^--/, "")] = argv[i + 1];
  for (const key of ["template", "data-dir", "output", "preview-dir"]) {
    if (!out[key]) throw new Error(`missing --${key}`);
  }
  return out;
}

const args = parseArgs(process.argv);
const dataDir = path.resolve(args["data-dir"]);
const reportFiles = (await fs.readdir(dataDir)).filter((name) => /^metrics_.*_v3\.json$/.test(name));
if (reportFiles.length !== 1) throw new Error(`expected one metrics JSON in ${dataDir}`);
const [facts, nodes, cells, report] = await Promise.all([
  fs.readFile(path.join(dataDir, "facts_v3.json"), "utf8").then(JSON.parse),
  fs.readFile(path.join(dataDir, "adaptation_nodes_v3.json"), "utf8").then(JSON.parse),
  fs.readFile(path.join(dataDir, "coverage_30_cells_v3.json"), "utf8").then(JSON.parse),
  fs.readFile(path.join(dataDir, reportFiles[0]), "utf8").then(JSON.parse),
]);

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(path.resolve(args.template)));

function excelValue(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "object") return JSON.stringify(value);
  return value;
}

function writeRows(sheetName, headers, rows, maxRows, extraHeaders = []) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const allHeaders = [...headers, ...extraHeaders];
  const lastCol = columnName(allHeaders.length);
  sheet.getRange(`A3:${lastCol}${maxRows}`).clear({ applyTo: "contents" });
  sheet.getRange(`A3:${lastCol}3`).values = [allHeaders];
  const matrix = rows.map((row) => allHeaders.map((header) => excelValue(row[header])));
  if (matrix.length) sheet.getRangeByIndexes(3, 0, matrix.length, allHeaders.length).values = matrix;
  sheet.freezePanes.freezeRows(3);
  sheet.showGridLines = false;
  sheet.getRange(`A3:${lastCol}3`).format = {
    fill: "#DCEAF7",
    font: { bold: true, color: "#17324D" },
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#B7C9D8" },
  };
  if (matrix.length) {
    const body = sheet.getRangeByIndexes(3, 0, matrix.length, allHeaders.length);
    body.format.wrapText = true;
    body.format.borders = { preset: "all", style: "thin", color: "#E2E8F0" };
    body.format.rowHeight = 30;
  }
  sheet.getRange(`A1:${lastCol}1`).format.font = { bold: true, size: 15, color: "#123A5A" };
  sheet.getRange(`A1:${lastCol}1`).format.fill = "#EFF7FD";
  sheet.getRange(`A1:${lastCol}1`).format.rowHeight = 30;
  sheet.getUsedRange().format.autofitColumns();
  for (let col = 0; col < allHeaders.length; col += 1) {
    const header = allHeaders[col];
    const range = sheet.getRangeByIndexes(0, col, Math.max(4, matrix.length + 3), 1);
    if (["content_text", "decision_reason", "before_state_json", "after_state_json"].includes(header)) range.format.columnWidth = 38;
    else if (["evidence_ids", "rule_hits", "acceptable_actions"].includes(header)) range.format.columnWidth = 24;
    else range.format.columnWidth = Math.min(Math.max(String(header).length + 4, 12), 24);
  }
}

function columnName(n) {
  let value = n;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

const factHeaders = [
  "run_id","seed_id","case_id","session_id","artifact_id","artifact_version_id","lineage_id","generation_stage",
  "content_unit_id","lineage_unit_id","unit_kind","content_text","evidence_ids","published_final","human_label",
  "hallucination_reason","first_generation","auto_review_detected","auto_intercepted","review_event_id","rule_hits",
  "adjudicator","final_fact_denominator","final_hallucination_numerator","native_error_denominator","interception_numerator",
];
const nodeHeaders = [
  "run_id","seed_id","case_id","session_id","adaptation_node_id","node_type","trigger_event_id","trigger_event_type",
  "knowledge_point","before_state_json","expected_action","acceptable_actions","actual_action","after_state_json","decision_reason",
  "downstream_artifact_id","expected_downstream_difficulty","actual_downstream_difficulty","due_node","trigger_binding_valid",
  "action_gold_match","after_state_consistent","downstream_executed","node_success","first_gen_transaction_id",
  "r03_reviewed_first_gen","r03_rejected_first_gen","human_mismatch_confirmed","r03_repair_approved","final_residual_mismatch",
  "non_keep","adjudicator",
];
const coverageHeaders = [
  "knowledge_point","difficulty","case_id","route_mode","route_reachable","route_evidence_valid","lecture_pass","practice_pass",
  "quiz_pass","feedback_pass","review_pass","evidence_pass","cell_pass",
];
writeRows("02_事实内容单元", factHeaders, facts, 3003, ["auto_label", "auto_hallucination_reason"]);
writeRows("03_适配节点", nodeHeaders, nodes, 1003);
writeRows("04_覆盖30格", coverageHeaders, cells, 200, ["human_gate"]);

const summary = workbook.worksheets.getItem("00_指标汇总");
summary.getRange("A1:J40").clear({ applyTo: "contents" });
summary.getRange("A1:J1").merge();
summary.getRange("A1").values = [["TRACE v3｜三项核心指标 + 两项辅助KPI"]];
summary.getRange("A2:J2").merge();
summary.getRange("A2").values = [[
  report.mode === "AUTO_PRELIMINARY"
    ? "AUTO_PRELIMINARY｜机器初算，不是最终成绩；人工字段保持 PENDING_HUMAN。"
    : "FINAL_HUMAN_REVIEWED｜双人复核、必要仲裁与覆盖门禁已完整读取；自动初算仍保留。",
]];
const headers = ["层级","指标","自动分子","自动分母","自动结果","最终分子","最终分母","最终结果","门槛/方向","公式说明"];
summary.getRange("A4:J4").values = [headers];
const metricDefs = [
  ["核心","最终发布幻觉率","final_hallucination_rate","<5%","最终批准发布且经复核为幻觉的事实单元 / 最终发布事实单元"],
  ["核心","画像—资源难度适配准确率","difficulty_adaptation_accuracy",">=85%","成功适配节点 / 全部应适配节点"],
  ["核心","主域严格闭环覆盖率","strict_closed_loop_coverage",">=90%","三档完整闭环知识点 / 10"],
  ["辅助","幻觉拦截率","hallucination_interception_rate","高为好","首次生成错误且自动阻断 / 首次生成错误"],
  ["辅助","原生教学适配失配率","native_teaching_adaptation_mismatch_rate","低为好","R-03首轮确认失配事务 / 首次R-03审核事务"],
];
const auto = report.automatic_preliminary ?? report.combined;
const finalResult = report.mode === "FINAL_HUMAN_REVIEWED" ? report.combined : null;
const summaryRows = metricDefs.map(([level,label,key,direction,note]) => {
  const a = auto.metrics[key];
  const f = finalResult?.metrics[key];
  return [level,label,a.numerator,a.denominator,a.percentage / 100,f?.numerator ?? "PENDING_HUMAN",f?.denominator ?? "PENDING_HUMAN",f ? f.percentage / 100 : "PENDING_HUMAN",direction,note];
});
summary.getRange("A5:J9").values = summaryRows;
summary.getRange("E5:E9").format.numberFormat = "0.00%";
if (finalResult) summary.getRange("H5:H9").format.numberFormat = "0.00%";
summary.getRange("A12:E12").values = [["运行范围","案例数","事实单元","适配节点","覆盖格"]];
summary.getRange("A13:E13").values = [["Seed A + Seed B",Object.values(report.seed_case_counts).reduce((a,b)=>a+b,0),report.row_counts.fact_units,report.row_counts.adaptation_nodes,report.row_counts.coverage_cells]];
summary.getRange("A15:D15").values = [["辅助链路","首次错误/事务","成功拦截/修复","最终残留"]];
const counts = report.combined.auxiliary_counts;
summary.getRange("A16:D17").values = [
  ["事实幻觉",counts.first_generation_errors,counts.automatically_intercepted,counts.final_residual_hallucinations],
  ["R-03适配",counts.first_generation_r03_transactions,counts.r03_repaired_and_approved,counts.final_residual_adaptation_mismatches],
];
summary.showGridLines = false;
summary.freezePanes.freezeRows(4);
summary.getRange("A1:J1").format = { fill: "#123A5A", font: { bold: true, color: "#FFFFFF", size: 16 }, rowHeight: 34 };
summary.getRange("A2:J2").format = { fill: report.mode === "AUTO_PRELIMINARY" ? "#FFF4CE" : "#DCFCE7", font: { bold: true, color: "#5B4B00" }, wrapText: true, rowHeight: 30 };
summary.getRange("A4:J4").format = { fill: "#DCEAF7", font: { bold: true, color: "#17324D" }, wrapText: true, borders: { preset: "all", style: "thin", color: "#B7C9D8" } };
summary.getRange("A5:J9").format.borders = { preset: "all", style: "thin", color: "#D7E0E8" };
summary.getRange("A12:E12").format = { fill: "#EAF2F8", font: { bold: true } };
summary.getRange("A15:D15").format = { fill: "#EAF2F8", font: { bold: true } };
summary.getRange("A1:J20").format.autofitColumns();
summary.getRange("B1:B20").format.columnWidth = 30;
summary.getRange("J1:J20").format.columnWidth = 55;

const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
if (errorScan.ndjson && /#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A/.test(errorScan.ndjson)) {
  throw new Error(`formula error scan failed: ${errorScan.ndjson}`);
}
const keyInspect = await workbook.inspect({
  kind: "table",
  range: "00_指标汇总!A1:J17",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 12,
});
console.log(keyInspect.ndjson);

await fs.mkdir(path.resolve(args["preview-dir"]), { recursive: true });
const previewRanges = {
  "00_指标汇总": "A1:J17",
  "01_TRACE字段": "A1:H80",
  "02_事实内容单元": `A1:${columnName(factHeaders.length + 2)}${Math.max(8, facts.length + 4)}`,
  "03_适配节点": `A1:${columnName(nodeHeaders.length)}${Math.max(8, nodes.length + 4)}`,
  "04_覆盖30格": `A1:${columnName(coverageHeaders.length + 1)}${Math.max(8, cells.length + 4)}`,
};
for (const sheetName of Object.keys(previewRanges)) {
  const preview = await workbook.render({
    sheetName,
    range: previewRanges[sheetName],
    scale: 1,
    format: "png",
  });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(path.join(path.resolve(args["preview-dir"]), `${sheetName}.png`), bytes);
}
await fs.mkdir(path.dirname(path.resolve(args.output)), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.resolve(args.output));
console.log(JSON.stringify({ output: path.resolve(args.output), previews: path.resolve(args["preview-dir"]) }));
