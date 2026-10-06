// Stream test with own injected tool call
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
    stream: true,
    messages: [
        { role: 'user', content: 'Please create a todo list with one item: "streaming test". Use your system_todo_write tool, then tell me you did it.' },
    ],
};

const t0 = Date.now();
fetch(BASE, { method: 'POST', headers: HEADERS, body: JSON.stringify(payload) })
    .then(async res => {
        let raw = '';
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            raw += dec.decode(value, { stream: true });
        }
        fs.writeFileSync('d:/JDW_FIX/tests/out_own_tool_stream.txt', `HTTP ${res.status} time=${((Date.now() - t0) / 1000).toFixed(1)}s\n${raw}`);
        console.log('done', res.status, raw.length);
    })
    .catch(e => {
        fs.writeFileSync('d:/JDW_FIX/tests/out_own_tool_stream.txt', `ERROR: ${e.message}`);
        console.log('failed', e.message);
    });
