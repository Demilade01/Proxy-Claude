// Usage: node probe_single.cjs <probename>
const fs = require('fs');

const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const name = process.argv[2];
const OUT = 'd:/JDW_FIX/tests/out_probe_' + name + '.txt';

const anthropicHeaders = {
    'Content-Type': 'application/json',
    'x-api-key': KEY,
    'anthropic-version': '2023-06-01',
};
const openaiHeaders = {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + KEY,
};

const probes = {
    // OpenAI chat completions non-stream
    openai_nonstream: {
        url: 'https://api.justwoker.icu/v1/chat/completions',
        headers: openaiHeaders,
        body: {
            model: 'claude-opus-4-8',
            max_tokens: 50,
            messages: [{ role: 'user', content: 'Say OPENAI-FORMAT-OK' }],
        },
    },
    // Anthropic count_tokens
    count_tokens: {
        url: 'https://api.justwoker.icu/v1/messages/count_tokens',
        headers: anthropicHeaders,
        body: {
            model: 'claude-opus-4-8',
            messages: [{ role: 'user', content: 'Hello' }],
        },
    },
    // Unknown model
    bad_model: {
        url: 'https://api.justwoker.icu/v1/messages',
        headers: anthropicHeaders,
        body: {
            model: 'claude-nonexistent-99',
            max_tokens: 10,
            messages: [{ role: 'user', content: 'hi' }],
        },
    },
    // Missing max_tokens
    no_max_tokens: {
        url: 'https://api.justwoker.icu/v1/messages',
        headers: anthropicHeaders,
        body: {
            model: 'claude-opus-4-8',
            messages: [{ role: 'user', content: 'hi' }],
        },
    },
    // Bad role order
    bad_role_order: {
        url: 'https://api.justwoker.icu/v1/messages',
        headers: anthropicHeaders,
        body: {
            model: 'claude-opus-4-8',
            max_tokens: 10,
            messages: [{ role: 'assistant', content: 'hi' }],
        },
    },
    // Image block (multimodal)
    image_block: {
        url: 'https://api.justwoker.icu/v1/messages',
        headers: anthropicHeaders,
        body: {
            model: 'claude-opus-4-8',
            max_tokens: 50,
            messages: [{
                role: 'user',
                content: [
                    { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==' } },
                    { type: 'text', text: 'What color is this 1x1 pixel? Reply with just the color.' },
                ],
            }],
        },
    },
    // tool_use block in assistant history (conversation continuation)
    tool_history: {
        url: 'https://api.justwoker.icu/v1/messages',
        headers: anthropicHeaders,
        body: {
            model: 'claude-opus-4-8',
            max_tokens: 100,
            tools: [{
                name: 'get_weather',
                description: 'Get the current weather for a city',
                input_schema: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
            }],
            messages: [
                { role: 'user', content: 'What is the weather in Tokyo?' },
                { role: 'assistant', content: [{ type: 'tool_use', id: 'toolu_01', name: 'get_weather', input: { city: 'Tokyo' } }] },
                { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_01', content: 'Sunny, 25C' }] },
            ],
        },
    },
};

const p = probes[name];
if (!p) { console.error('unknown probe'); process.exit(1); }

const ctrl = new AbortController();
setTimeout(() => ctrl.abort(), 180000);
const t0 = Date.now();

fetch(p.url, { method: 'POST', headers: p.headers, body: JSON.stringify(p.body), signal: ctrl.signal })
    .then(async res => {
        const text = await res.text();
        const dt = ((Date.now() - t0) / 1000).toFixed(1);
        fs.writeFileSync(OUT, `HTTP ${res.status} time=${dt}s ct=${res.headers.get('content-type')} cf-ray=${res.headers.get('cf-ray')}\n\n${text.slice(0, 4000)}`);
        console.log(`${name}: ${res.status} in ${dt}s`);
    })
    .catch(e => {
        fs.writeFileSync(OUT, `ERROR after ${((Date.now() - t0) / 1000).toFixed(0)}s: ${e.message}`);
        console.log(`${name} FAILED: ${e.message}`);
    });
