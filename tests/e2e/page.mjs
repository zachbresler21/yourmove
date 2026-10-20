// Drives the board page in headless Chromium over the DevTools protocol: real clicks, real typing.
// Run by run.sh. Usage: node page.mjs <chrome binary> <board url> <screenshot dir> <phase>
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const [chrome, url, shots, phase] = process.argv.slice(2)
const port = 9333
const browser = spawn(chrome, [
  // No sandbox: GitHub's Ubuntu runners need it off, and this browser only ever loads localhost.
  '--headless=new', '--no-first-run', '--no-sandbox', `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'ym-'))}`, '--window-size=1400,900', 'about:blank',
], { stdio: 'ignore' })
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

let target
for (let tries = 0; !target && tries < 50; tries++) {
  await sleep(200)
  target = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json()).then(list => list.find(t => t.type === 'page')).catch(() => null)
}
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise(resolve => socket.addEventListener('open', resolve))
let nextId = 0
const waiting = new Map()
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data)
  if (waiting.has(message.id)) waiting.get(message.id)(message)
})
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId
  waiting.set(id, message => (message.error ? reject(new Error(`${method}: ${message.error.message}`)) : resolve(message.result)))
  socket.send(JSON.stringify({ id, method, params }))
})
const value = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result.value
const until = async (expression, what) => {
  for (let tries = 0; tries < 60; tries++) {
    if (await value(expression)) return
    await sleep(250)
  }
  throw new Error(`Timed out waiting for: ${what}`)
}
const click = async (selector, what) => {
  await until(`!!document.querySelector(${JSON.stringify(selector)})`, what)
  const box = await value(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2] })()`)
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', { type, x: box[0], y: box[1], button: 'left', clickCount: 1 })
  }
}
const shot = async name => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(shots, `${name}.png`), Buffer.from(data, 'base64'))
}
const text = () => value('document.getElementById("board").innerText')
const checks = []
const check = (ok, what) => { checks.push(`${ok ? 'PASS' : 'FAIL'} ${what}`); if (!ok) process.exitCode = 1 }

await send('Page.enable')
await send('Runtime.enable')
await send('Page.navigate', { url })
await until('document.querySelectorAll(".card").length === 2', 'two cards drawn')
check(await value('document.getElementById("heading").textContent') === "Ada's board", 'heading names the owner')
check(await value('document.title') === '(1) Board', 'tab title counts needs-you')

if (phase === 'comment') {
  check(await value('document.querySelectorAll(".choices button").length') === 2, 'needs-you card shows its two choices')
  await shot('1-before')
  await click('.choices button[aria-label$="Ship at $25"]', 'the Ship at $25 choice')
  await until('document.querySelectorAll(".comment").length === 1', 'the choice saved as a comment')
  check(await value('document.querySelectorAll(".choices button").length') === 0, 'choices hide while an answer waits')
  check((await text()).includes('Goes to Claude with your next message in checkout'), 'comment says where it is going')

  await click('button[aria-label="Comment on Search"]', 'the Comment button on Search')
  await until('document.activeElement?.tagName === "TEXTAREA"', 'the composer focused')
  await send('Input.insertText', { text: 'Use the new copy.' })
  await click('.composer button[type="submit"]', 'Send to Claude')
  await until('document.querySelectorAll(".comment").length === 2', 'the typed comment saved')
  check(!(await value('!!document.querySelector(".composer")')), 'composer closes after sending')
  check(!(await value('!document.getElementById("problem").hidden')), 'no error banner')
  await shot('2-commented')
}

if (phase === 'answered') {
  await until(`document.getElementById("board").innerText.includes("Claude: Shipping at $25.")`, 'the reply on the card')
  check((await text()).includes('Claude has it, since'), 'the delivered, unanswered comment says Claude has it')
  await shot('3-answered')
  await click('button[aria-label="Delete comment: Use the new copy."]', 'Delete on the typed comment')
  await until('document.querySelectorAll(".comment").length === 1', 'the comment deleted')
  check(true, 'delete removes the comment')
  await shot('4-deleted')
}

if (phase === 'down') {
  await until('!document.getElementById("problem").hidden', 'the server-down banner')
  check((await value('document.getElementById("problem").textContent')).includes('not answering'), 'banner says the server is not answering')
  check(await value('document.querySelectorAll(".card").length') === 2, 'last good cards stay on screen')
  await shot('5-server-down')
}

console.log(checks.join('\n'))
socket.close()
browser.kill()
