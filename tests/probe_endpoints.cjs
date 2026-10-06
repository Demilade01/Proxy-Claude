// Probe OpenAI chat completions + count_tokens + headers
const fs = require('fs');

const KEY = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds';
const results = [];

async function probe(name, url, headers, body) {
    try {
        const res = await fetch(url, {
            method: body ? 'POST' : 'GET',
            headers,
            body: body ? JSON.stringify(body) : undefined,
        });
        const text = await res.text();
        results.push(`=== ${name} ===\nHTTP ${res.status}\nCF-Ray: ${res.headers.get('cf-ray')}\nServer: ${res.headers.get('server')}\nContent-Type: ${res.headers.get('content-type')}\n\n${text.slice(0, 3000)}\n`);
    } catch (e) {
        results.push(`=== ${name} ===\nFETCH ERROR: ${e.message}\n`);
    }
}

(async () => {
    // OpenAI chat completions non-stream
    await probe('openai-chat-nonstream', 'https://api.justwoker.icu/v1/chat/completions', {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${KEY}`,
    }, {
        model: 'claude-opus-4-8',
        max_tokens: 50,
        messages: [{ role: 'user', content: 'Say OPENAI-FORMAT-OK' }],
    });

    // OpenAI chat completions stream
    try {
        const res = await fetch('https://api.justwoker.icu/v1/chat/completions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${KEY}`,
            },
            body: JSON.stringify({
                model: 'claude-opus-4-8',
                max_tokens: 50,
                stream: true,
                messages: [{ role: 'user', content: 'Say OPENAI-STREAM-OK' }],
            }),
        });
        let raw = '';
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            raw += dec.decode(value, { stream: true });
        }
        results.push(`=== openai-chat-stream ===\nHTTP ${res.status}\n${raw.slice(0, 3000)}\n`);
    } catch (e) {
        results.push(`=== openai-chat-stream ===\nFETCH ERROR: ${e.message}\n`);
    }

    // Anthropic count_tokens
    await probe('anthropic-count-tokens', 'https://api.justwoker.icu/v1/messages/count_tokens', {
        'Content-Type': 'application/json',
        'x-api-key': KEY,
        'anthropic-version': '2023-06-01',
    }, {
        model: 'claude-opus-4-8',
        messages: [{ role: 'user', content: 'Hello' }],
    });

    // Unknown model error handling
    await probe('anthropic-bad-model', 'https://api.justwoker.icu/v1/messages', {
        'Content-Type': 'application/json',
        'x-api-key': KEY,
        'anthropic-version': '2023-06-01',
    }, {
        model: 'claude-nonexistent-99',
        max_tokens: 10,
        messages: [{ role: 'user', content: 'hi' }],
    });

    // Missing max_tokens (protocol requires it)
    await probe('anthropic-no-max-tokens', 'https://api.justwoker.icu/v1/messages', {
        'Content-Type': 'application/json',
        'x-api-key': KEY,
        'anthropic-version': '2023-06-01',
    }, {
        model: 'claude-opus-4-8',
        messages: [{ role: 'user', content: 'hi' }],
    });

    // Wrong role sequence (assistant first) - protocol requires error
    await probe('anthropic-bad-role-order', 'https://api.justwoker.icu/v1/messages', {
        'Content-Type': 'application/json',
        'x-api-key': KEY,
        'anthropic-version': '2023-06-01',
    }, {
        model: 'claude-opus-4-8',
        max_tokens: 10,
        messages: [{ role: 'assistant', content: 'hi' }],
    });

    // Image block in message (multimodal support check)
    await probe('anthropic-image-block', 'https://api.justwoker.icu/v1/messages', {
        'Content-Type': 'application/json',
        'x-api-key': KEY,
        'anthropic-version': '2023-06-01',
    }, {
        model: 'claude-opus-4-8',
        max_tokens: 50,
        messages: [{
            role: 'user',
            content: [
                { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==' } },
                { type: 'text', text: 'What color is this 1x1 pixel image? Reply with just the color.' },
            ],
        }],
    });

    fs.writeFileSync('d:/JDW_FIX/tests/out_probes.txt', results.join('\n'));
    console.log('PROBES DONE');
})();
