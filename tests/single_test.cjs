// Usage: node single_test.cjs <testname> <outfile>
const fs = require('fs');

const BASE = 'https://api.justwoker.icu/v1/messages';
const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const HEADERS = {
    'Content-Type': 'application/json',
    'x-api-key': KEY,
    'anthropic-version': '2023-06-01',
};

const tests = {
    t5: {
        model: 'claude-opus-4-8',
        max_tokens: 300,
        stop_sequences: ['STOPWORD-XYZ'],
        messages: [{ role: 'user', content: 'Count from 1 to 15, one number per line, then write STOPWORD-XYZ.' }],
    },
    t6: {
        model: 'claude-opus-4-8',
        max_tokens: 200,
        system: 'You are a pirate. Always start replies with YARR.',
        messages: [{ role: 'user', content: 'Introduce yourself in one sentence.' }],
    },
    t7: {
        model: 'claude-opus-4-8',
        max_tokens: 300,
        tools: [{
            name: 'get_weather',
            description: 'Get the current weather for a city',
            input_schema: {
                type: 'object',
                properties: { city: { type: 'string' } },
                required: ['city'],
            },
        }],
        tool_choice: { type: 'tool', name: 'get_weather' },
        messages: [{ role: 'user', content: 'What is the weather in Tokyo?' }],
    },
    t8: {
        model: 'claude-opus-4-8',
        max_tokens: 300,
        stream: true,
        tools: [{
            name: 'get_weather',
            description: 'Get the current weather for a city',
            input_schema: {
                type: 'object',
                properties: { city: { type: 'string' } },
                required: ['city'],
            },
        }],
        messages: [{ role: 'user', content: 'What is the weather in Tokyo? Use the get_weather tool.' }],
    },
    t9: {
        model: 'claude-opus-4-8',
        max_tokens: 2000,
        stream: true,
        messages: [{ role: 'user', content: 'Write a 250-word story about a robot chef.' }],
    },
    t10: {
        model: 'claude-opus-4-8',
        max_tokens: 400,
        stream: true,
        system: 'You are a pirate.',
        stop_sequences: ['STOPWORD-XYZ'],
        messages: [{ role: 'user', content: 'Count from 1 to 20 one per line then STOPWORD-XYZ.' }],
    },
    t11: {
        // temperature/top_p passthrough check
        model: 'claude-opus-4-8',
        max_tokens: 100,
        temperature: 0,
        messages: [{ role: 'user', content: 'Reply with the single word: TEMPCHECK' }],
    },
};

const name = process.argv[2];
const outFile = process.argv[3] || `d:/JDW_FIX/tests/out_${name}.txt`;
const payload = tests[name];
if (!payload) {
    console.error('unknown test', name);
    process.exit(1);
}

const isStream = !!payload.stream;
const t0 = Date.now();

const ctrl = new AbortController();
const timer = setTimeout(() => ctrl.abort(), 240000);

(async () => {
    try {
        const res = await fetch(BASE, {
            method: 'POST',
            headers: HEADERS,
            body: JSON.stringify(payload),
            signal: ctrl.signal,
        });
        if (!isStream) {
            const text = await res.text();
            const dt = ((Date.now() - t0) / 1000).toFixed(1);
            fs.writeFileSync(outFile, `HTTP ${res.status} time=${dt}s\n${text}`);
        } else {
            let raw = '';
            const reader = res.body.getReader();
            const dec = new TextDecoder();
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                raw += dec.decode(value, { stream: true });
            }
            const dt = ((Date.now() - t0) / 1000).toFixed(1);
            fs.writeFileSync(outFile, `HTTP ${res.status} time=${dt}s\n${raw}`);
        }
        console.log(`${name}: HTTP ${res.status}, done in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
        process.exit(0);
    } catch (e) {
        fs.writeFileSync(outFile, `ERROR after ${((Date.now() - t0) / 1000).toFixed(0)}s: ${e.message}`);
        console.error(`${name} failed:`, e.message);
        process.exit(1);
    } finally {
        clearTimeout(timer);
    }
})();
