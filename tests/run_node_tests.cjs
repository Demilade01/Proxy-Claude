const fs = require('fs');

const BASE = 'https://api.justwoker.icu/v1/messages';
const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const HEADERS = {
    'Content-Type': 'application/json',
    'x-api-key': KEY,
    'anthropic-version': '2023-06-01',
};

async function send(payload, label) {
    const t0 = Date.now();
    const res = await fetch(BASE, {
        method: 'POST',
        headers: HEADERS,
        body: JSON.stringify(payload),
    });
    const text = await res.text();
    const dt = ((Date.now() - t0) / 1000).toFixed(1);
    fs.writeFileSync(`d:/JDW_FIX/tests/out_${label}.txt`, `HTTP ${res.status} (${dt}s)\n${text}`);
    return { status: res.status, text, dt };
}

async function main() {
    // T4: max_tokens=1
    console.log('T4 running...');
    await send(JSON.parse(fs.readFileSync('d:/JDW_FIX/tests/t4_maxtokens.json', 'utf8')), 't4');

    // T5: stop_sequences
    console.log('T5 running...');
    await send(JSON.parse(fs.readFileSync('d:/JDW_FIX/tests/t5_stop.json', 'utf8')), 't5');

    // T6: system
    console.log('T6 running...');
    await send(JSON.parse(fs.readFileSync('d:/JDW_FIX/tests/t6_system.json', 'utf8')), 't6');

    // T7: multi-turn tool use — tools + previous assistant tool_use + user tool_result
    console.log('T7 running...');
    const t7 = {
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
        messages: [
            { role: 'user', content: 'What is the weather in Tokyo?' },
            { role: 'assistant', content: [{ type: 'tool_use', id: 'toolu_01', name: 'get_weather', input: { city: 'Tokyo' } }] },
            { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_01', content: 'Sunny, 25C' }] },
        ],
    };
    await send(t7, 't7');

    // T8: streaming with tools — see what SSE events appear
    console.log('T8 running...');
    const t8 = {
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
    };
    const res = await fetch(BASE, {
        method: 'POST',
        headers: HEADERS,
        body: JSON.stringify(t8),
    });
    let raw = '';
    // read SSE stream manually
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        raw += decoder.decode(value, { stream: true });
    }
    fs.writeFileSync('d:/JDW_FIX/tests/out_t8_stream.txt', `HTTP ${res.status}\n${raw}`);

    // T9: streaming long response — check whether ANY content_block events appear over a longer generation
    console.log('T9 running...');
    const t9 = {
        model: 'claude-opus-4-8',
        max_tokens: 2000,
        stream: true,
        messages: [{ role: 'user', content: 'Write a 300-word story about a robot chef.' }],
    };
    const res9 = await fetch(BASE, {
        method: 'POST',
        headers: HEADERS,
        body: JSON.stringify(t9),
    });
    let raw9 = '';
    const reader9 = res9.body.getReader();
    const dec9 = new TextDecoder();
    while (true) {
        const { done, value } = await reader9.read();
        if (done) break;
        raw9 += dec9.decode(value, { stream: true });
    }
    fs.writeFileSync('d:/JDW_FIX/tests/out_t9_stream.txt', `HTTP ${res9.status}\n${raw9}`);

    console.log('ALL DONE');
}

main().catch(e => {
    console.error('FATAL', e);
    process.exit(1);
});
