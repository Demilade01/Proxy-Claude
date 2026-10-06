$ErrorActionPreference = 'Continue'
$base = 'https://api.justwoker.icu/v1/messages'
$key = 'sk-zHvqTNy8PdwawKRSWrx155mwaKe4vHiX8YR1AugqAVjNxeds'
$headers = @{
    'x-api-key'         = $key
    'anthropic-version' = '2023-06-01'
    'Content-Type'      = 'application/json'
}

# T3: tools
$body = Get-Content 'd:\JDW_FIX\tests\t3_tools.json' -Raw
try {
    $r = Invoke-WebRequest -Uri $base -Method Post -Headers $headers -Body $body -TimeoutSec 300 -UseBasicParsing
    "=== T3 HTTP: $($r.StatusCode) ===" | Out-File 'd:\JDW_FIX\tests\out_t3.txt' -Encoding utf8
    $r.Content | Out-File 'd:\JDW_FIX\tests\out_t3.txt' -Append -Encoding utf8
} catch {
    "T3 ERROR: $($_.Exception.Message)" | Out-File 'd:\JDW_FIX\tests\out_t3.txt' -Encoding utf8
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        $sr.ReadToEnd() | Out-File 'd:\JDW_FIX\tests\out_t3.txt' -Append -Encoding utf8
    }
}

# T4: max_tokens=1
$body = Get-Content 'd:\JDW_FIX\tests\t4_maxtokens.json' -Raw
try {
    $r = Invoke-WebRequest -Uri $base -Method Post -Headers $headers -Body $body -TimeoutSec 300 -UseBasicParsing
    "=== T4 HTTP: $($r.StatusCode) ===" | Out-File 'd:\JDW_FIX\tests\out_t4.txt' -Encoding utf8
    $r.Content | Out-File 'd:\JDW_FIX\tests\out_t4.txt' -Append -Encoding utf8
} catch {
    "T4 ERROR: $($_.Exception.Message)" | Out-File 'd:\JDW_FIX\tests\out_t4.txt' -Encoding utf8
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        $sr.ReadToEnd() | Out-File 'd:\JDW_FIX\tests\out_t4.txt' -Append -Encoding utf8
    }
}

# T5: stop_sequences
$body = Get-Content 'd:\JDW_FIX\tests\t5_stop.json' -Raw
try {
    $r = Invoke-WebRequest -Uri $base -Method Post -Headers $headers -Body $body -TimeoutSec 300 -UseBasicParsing
    "=== T5 HTTP: $($r.StatusCode) ===" | Out-File 'd:\JDW_FIX\tests\out_t5.txt' -Encoding utf8
    $r.Content | Out-File 'd:\JDW_FIX\tests\out_t5.txt' -Append -Encoding utf8
} catch {
    "T5 ERROR: $($_.Exception.Message)" | Out-File 'd:\JDW_FIX\tests\out_t5.txt' -Encoding utf8
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        $sr.ReadToEnd() | Out-File 'd:\JDW_FIX\tests\out_t5.txt' -Append -Encoding utf8
    }
}

# T6: system prompt
$body = Get-Content 'd:\JDW_FIX\tests\t6_system.json' -Raw
try {
    $r = Invoke-WebRequest -Uri $base -Method Post -Headers $headers -Body $body -TimeoutSec 300 -UseBasicParsing
    "=== T6 HTTP: $($r.StatusCode) ===" | Out-File 'd:\JDW_FIX\tests\out_t6.txt' -Encoding utf8
    $r.Content | Out-File 'd:\JDW_FIX\tests\out_t6.txt' -Append -Encoding utf8
} catch {
    "T6 ERROR: $($_.Exception.Message)" | Out-File 'd:\JDW_FIX\tests\out_t6.txt' -Encoding utf8
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        $sr.ReadToEnd() | Out-File 'd:\JDW_FIX\tests\out_t6.txt' -Append -Encoding utf8
    }
}

Write-Host 'ALL TESTS DONE'
