import { promises as fs } from 'node:fs'
import path from 'node:path'

import type { Plugin, ResolvedConfig } from 'vite'

import type { TraceManifestEntry } from '../src/types/trace'


const TRACE_FILE = /^demo-(?:planner_new|craft_engineer|line_leader)-\d{14}\.jsonl$/
const FORBIDDEN_REPLAY_TEXT = [
  'injected_for_demo',
  'injection_label',
  '人工误驳',
  '人工误判',
  '人工注入',
  '故障注入',
] as const


export function isReplayTraceFile(fileName: string): boolean {
  return TRACE_FILE.test(fileName)
}


export function assertReplayTraceSafe(fileName: string, contents: string | Buffer): void {
  const text = contents.toString()
  const marker = FORBIDDEN_REPLAY_TEXT.find((candidate) => text.includes(candidate))
  if (marker) {
    throw new Error(`${fileName} 不符合正式回放要求：包含 ${marker}`)
  }
  for (const [index, line] of text.split(/\r?\n/u).entries()) {
    if (line.trim() === '') continue
    try {
      JSON.parse(line)
    } catch {
      throw new Error(`${fileName} 不符合正式回放要求：第 ${index + 1} 行不是有效记录`)
    }
  }
}


async function listTraceAssets(directory: string): Promise<TraceManifestEntry[]> {
  let names: string[]
  try {
    names = await fs.readdir(directory)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []
    throw error
  }
  const entries = await Promise.all(
    names.filter(isReplayTraceFile).sort().map(async (fileName) => {
      const filePath = path.join(directory, fileName)
      const [stat, contents] = await Promise.all([
        fs.stat(filePath),
        fs.readFile(filePath),
      ])
      assertReplayTraceSafe(fileName, contents)
      return { fileName, bytes: stat.size }
    }),
  )
  return entries
}


function send(
  response: import('node:http').ServerResponse,
  status: number,
  contentType: string,
  body: string | Buffer,
): void {
  response.statusCode = status
  response.setHeader('Content-Type', contentType)
  response.setHeader('Cache-Control', 'no-store')
  response.end(body)
}


export function traceAssetsPlugin(): Plugin {
  let config: ResolvedConfig
  let traceDirectory = ''
  return {
    name: 'ref-trace-assets',
    configResolved(resolved) {
      config = resolved
      traceDirectory = path.resolve(config.root, '..', 'traces')
    },
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
        if (requestUrl.pathname === '/traces/manifest.json') {
          const manifest = await listTraceAssets(traceDirectory)
          send(response, 200, 'application/json; charset=utf-8', JSON.stringify(manifest))
          return
        }
        if (!requestUrl.pathname.startsWith('/traces/')) {
          next()
          return
        }
        const requestedName = decodeURIComponent(requestUrl.pathname.slice('/traces/'.length))
        if (path.basename(requestedName) !== requestedName || !isReplayTraceFile(requestedName)) {
          send(response, 404, 'text/plain; charset=utf-8', '未找到该回放会话')
          return
        }
        try {
          const contents = await fs.readFile(path.join(traceDirectory, requestedName))
          assertReplayTraceSafe(requestedName, contents)
          send(response, 200, 'application/x-ndjson; charset=utf-8', contents)
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
            send(response, 404, 'text/plain; charset=utf-8', '未找到该回放会话')
            return
          }
          next(error as Error)
        }
      })
    },
    async generateBundle() {
      const manifest = await listTraceAssets(traceDirectory)
      for (const entry of manifest) {
        const source = await fs.readFile(path.join(traceDirectory, entry.fileName))
        assertReplayTraceSafe(entry.fileName, source)
        this.emitFile({
          type: 'asset',
          fileName: `traces/${entry.fileName}`,
          source,
        })
      }
      this.emitFile({
        type: 'asset',
        fileName: 'traces/manifest.json',
        source: JSON.stringify(manifest),
      })
    },
  }
}
