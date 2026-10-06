// Final test: ask the model to call its own injected tool, to see if the relay can emit tool_use blocks at all
const fs = require('fs');

const BASE = 'https://api.justwoker.icu/v1/messages';
const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const HEADERS = {
    'Content-Type': 'application/json',
    'x-api-key': KEY,
    'anthropic-version': '2023-06-01',
};

const payload = {
    model: 'claude-opus-4-8',
    max_tokens: 400,
    messages: [
        { role: 'user', content: 'Please create a todo list with one item: "test the protocol". Use your system_todo_write tool to do it, then tell me you did it.' },
    ],
};

const t0 = Date.now();
fetch(BASE, { method: 'POST', headers: HEADERS, body: JSON.stringify(payload) })
    .then(async res => {
        const text = await res.text();
        fs.writeFileSync('d:/JDW_FIX/tests/out_own_tool.txt', `HTTP ${res.status} time=${((Date.now() - t0) / 1000).toFixed(1)}s\n${text}`);
        console.log('done', res.status);
    })
    .catch(e => {
        fs.writeFileSync('d:/JDW_FIX/tests/out_own_tool.txt', `ERROR: ${e.message}`);
        console.log('failed', e.message);
    });
