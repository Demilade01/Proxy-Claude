const fs = require('fs');

const BASE = 'https://api.justwoker.icu/v1/messages';
const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const HEADERS = {
    'Content-Type': 'application/json',
    'x-api-key': KEY,
    'anthropic-version': '2023-06-01',
};

async function sendWithTimeout(payload, label, timeoutSec = 300) {
    const t0 = Date.now();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutSec * 1000);
    try {
        const res = await fetch(BASE, {
            method: 'POST',
            headers: HEADERS,
            body: JSON.stringify(payload),
            signal: ctrl.signal,
        });
        const text = await res.text();
        const dt = ((Date.now() - t0) / 1000).toFixed(1);
        fs.writeFileSync(`d:/JDW_FIX/tests/out_${label}.txt`, `HTTP ${res.status} time=${dt}s\n${text}`);
        console.log(`${label} done: ${res.status} in ${dt}s`);
    } catch (e) {
        fs.writeFileSync(`d:/JDW_FIX/tests/out_${label}.txt`, `TIMEOUT/ERROR after ${((Date.now() - t0) / 1000).toFixed(0)}s: ${e.message}`);
        console.log(`${label} failed: ${e.message}`);
    } finally {
        clearTimeout(timer);
    }
}

async function streamOnce(payload, label) {
    const t0 = Date.now();
    const res = await fetch(BASE, {
        method: 'POST',
        headers: HEADERS,
        body: JSON.stringify(payload),
    });
    let raw = '';
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        raw += dec.decode(value, { stream: true });
    }
    const dt = ((Date.now() - t0) / 1000).toFixed(1);
    fs.writeFileSync(`d:/JDW_FIX/tests/out_${label}.txt`, `HTTP ${res.status} time=${dt}s\n${raw}`);
    console.log(`${label} stream done: ${res.status} in ${dt}s, bytes=${raw.length}`);
}

async function main() {
    // T4: max_tokens=1 — short prompt to finish fast
    await sendWithTimeout({
        model: 'claude-opus-4-8',
        max_tokens: 1,
        messages: [{ role: 'user', content: 'List the numbers 1 through 20 separated by commas.' }],
    }, 't4', 300);

    // T5: stop_sequences — short prompt
    await sendWithTimeout({
        model: 'claude-opus-4-8',
        max_tokens: 300,
        stop_sequences: ['STOPWORD-XYZ'],
        messages: [{ role: 'user', content: 'Count from 1 to 15, one number per line, then write STOPWORD-XYZ.' }],
    }, 't5', 300);

    // T6: system prompt
    await sendWithTimeout({
        model: 'claude-opus-4-8',
        max_tokens: 200,
        system: 'You are a pirate. Always start replies with YARR.',
        messages: [{ role: 'user', content: 'Introduce yourself in one sentence.' }],
    }, 't6', 300);

    // T7: tool_choice forced
    await sendWithTimeout({
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
    }, 't7', 300);

    // T8: streaming with tools
    try {
        await streamOnce({
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
        }, 't8_stream');
    } catch (e) {
        fs.writeFileSync('d:/JDW_FIX/tests/out_t8_stream.txt', `ERROR: ${e.message}`);
    }

    // T9: streaming with long-ish output
    try {
        await streamOnce({
            model: 'claude-opus-4-8',
            max_tokens: 2000,
            stream: true,
            messages: [{ role: 'user', content: 'Write a 250-word story about a robot chef.' }],
        }, 't9_stream');
    } catch (e) {
        fs.writeFileSync('d:/JDW_FIX/tests/out_t9_stream.txt', `ERROR: ${e.message}`);
    }

    // T10: streaming + system + stop_sequences combo
    try {
        await streamOnce({
            model: 'claude-opus-4-8',
            max_tokens: 400,
            stream: true,
            system: 'You are a pirate.',
            stop_sequences: ['STOPWORD-XYZ'],
            messages: [{ role: 'user', content: 'Count from 1 to 20 one per line then STOPWORD-XYZ.' }],
        }, 't10_stream');
    } catch (e) {
        fs.writeFileSync('d:/JDW_FIX/tests/out_t10_stream.txt', `ERROR: ${e.message}`);
    }

    console.log('ALL DONE');
}

main().catch(e => {
    console.error('FATAL', e);
    process.exit(1);
});
