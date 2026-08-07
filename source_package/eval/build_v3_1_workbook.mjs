import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const artifactEntry = require.resolve("@oai/artifact-tool");
const { FileBlob, SpreadsheetFile } = await import(pathToFileURL(artifactEntry).href);

function parseArgs(argv) {
  const values = {};
  for (let index = 2; index < argv.length; index += 2) {
    values[argv[index].replace(/^--/, "")] = argv[index + 1];
  }
  for (const key of ["template", "inputs", "experience-config", "probe-library", "output", "preview-dir"]) {
    if (!values[key]) throw new Error(`missing --${key}`);
  }
  return values;
}

const args = parseArgs(process.argv);
const [inputs, experienceConfig, probeLibrary] = await Promise.all([
  fs.readFile(path.resolve(args.inputs), "utf8").then(JSON.parse),
  fs.readFile(path.resolve(args["experience-config"]), "utf8").then(JSON.parse),
  fs.readFile(path.resolve(args["probe-library"]), "utf8").then(JSON.parse),
]);
if (!Array.isArray(inputs) || inputs.length !== 50) {
  throw new Error("v3.1 workbook requires exactly 50 formal inputs");
}
const workbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load(path.resolve(args.template)),
);
const probesById = new Map(probeLibrary.map((item) => [String(item.probe_id), item]));
const normalize = (value) => String(value ?? "")
  .replace(/[^0-9a-zA-Z\u4e00-\u9fff%+-]/g, "")
  .toLocaleLowerCase();
const probeResult = (item) => {
  if (!item) return null;
  const probe = probesById.get(String(item.probe_id));
  if (!probe) throw new Error(`unknown frozen probe ${item.probe_id}`);
  return normalize(item.answer) === normalize(probe.gold_answer) ? "correct" : "wrong";
};

const formal = workbook.worksheets.getItem("正式50题_v3");
formal.getRange("A1:AL1").unmerge();
formal.getRange("A2:AL2").unmerge();
formal.getRange("A1:AM1").merge();
formal.getRange("A2:AM2").merge();
formal.getRange("A1").values = [["正式评测50题 v3.1｜真实生产路由 + 三指标测算"]];
formal.getRange("A2").values = [[
  "规则：全部案例 route_mode=production；经历标签仅选择固定探针，不直接产生盲区；知识点与模板不得由运行器强制注入。",
]];
formal.getRange("AM4:AM54").copyFrom(formal.getRange("AL4:AL54"), "all");
formal.getRange("AM4").values = [["岗位画像经历标签"]];
formal.getRange("AM5:AM54").values = inputs.map((row) => [
  Array.isArray(row.experience_tags) ? row.experience_tags.join(";") : "",
]);
formal.getRange("O5:R54").values = inputs.map((row) => {
  const probes = Array.isArray(row.diagnostic_probe_answers)
    ? row.diagnostic_probe_answers
    : [];
  return [
    probes[0]?.probe_id ?? null,
    probeResult(probes[0]),
    probes[1]?.probe_id ?? null,
    probeResult(probes[1]),
  ];
});
formal.getRange("AM4:AM54").format.wrapText = true;
formal.getRange("AM4:AM54").format.columnWidth = 24;

const probes = workbook.worksheets.getItem("前测与探针");
probes.getRange("A26:H26").merge();
probes.getRange("A26").values = [["v3.1 岗位经历标签｜只选择固定探针，不直接判定盲区"]];
probes.getRange("A26:H26").format = {
  fill: "#24557F",
  font: { bold: true, color: "#FFFFFF", size: 14 },
  rowHeight: 30,
};
probes.getRange("A27:D27").copyFrom(probes.getRange("A4:D4"), "all");
probes.getRange("A27:D27").values = [["标签ID", "岗位经历描述", "校准知识点", "路由约束"]];
const tagRows = experienceConfig.tags.map((item) => [
  item.tag_id,
  item.label,
  item.knowledge_point,
  "只选择探针；答错前不得进入needs_training",
]);
probes.getRange(`A28:D${27 + tagRows.length}`).values = tagRows;
probes.getRange(`A28:D${27 + tagRows.length}`).format = {
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#8EB8D3" },
};
probes.getRange(`A28:D${27 + tagRows.length}`).format.rowHeight = 28;
probes.getRange("A28:A37").format.columnWidth = 28;
probes.getRange("B28:B37").format.columnWidth = 34;
probes.getRange("C28:C37").format.columnWidth = 24;
probes.getRange("D28:D37").format.columnWidth = 38;

const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
});
if (formulaErrors.ndjson && /#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A/.test(formulaErrors.ndjson)) {
  throw new Error(`formula error scan failed: ${formulaErrors.ndjson}`);
}

const inspect = await workbook.inspect({
  kind: "table",
  range: "正式50题_v3!A1:AM10",
  tableMaxRows: 10,
  tableMaxCols: 39,
  tableMaxCellChars: 90,
  maxChars: 12000,
});
console.log(inspect.ndjson);

const previewDir = path.resolve(args["preview-dir"]);
await fs.mkdir(previewDir, { recursive: true });
for (const sheetName of ["正式50题_v3", "答案与SQL_v3", "前测与探针", "作答脚本", "指标口径"]) {
  const preview = await workbook.render({
    sheetName,
    autoCrop: "all",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(previewDir, `${sheetName}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
}

const outputPath = path.resolve(args.output);
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ output: outputPath, previews: previewDir }));
