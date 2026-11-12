// Classify files with linguist-js (the detector lowlighter/metrics uses).
// Usage: node detect.mjs <dir> [<dir> ...]
// Prints {"files": {"<dir-relative path>": "<language>"}, "languages": {"<language>": {"type", "color"}}}
import path from "node:path"
import linguist from "linguist-js"

const files = {}
const languages = {}
for (const dir of process.argv.slice(2)) {
  const root = path.resolve(dir)
  const result = await linguist(root)
  for (const [file, language] of Object.entries(result.files.results)) {
    if (language)
      files[path.relative(root, path.resolve(root, file)).split(path.sep).join("/")] = language
  }
  for (const [language, info] of Object.entries(result.languages.results))
    languages[language] = {type: info.type, color: info.color ?? null}
}
process.stdout.write(JSON.stringify({files, languages}))
