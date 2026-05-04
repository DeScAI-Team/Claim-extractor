# Test 1: single compound (Leucovorin)
Write-Host ""
Write-Host "===== TEST 1: Leucovorin (single) =====" -ForegroundColor Cyan
python orchestrate.py --compounds Leucovorin
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: orchestrate.py exited $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

$leucoReview = "data/Leucovorin/Leucovorin-review.json"
if (-not (Test-Path $leucoReview)) {
    Write-Host "MISSING: $leucoReview" -ForegroundColor Red
    exit 1
}
Write-Host "PASSED" -ForegroundColor Green
Write-Host "  Review: $leucoReview"

# Test 2: trio (Rapamycin + Lithium + Urolithin A)
Write-Host ""
Write-Host "===== TEST 2: Rapamycin + Lithium + Urolithin A (trio) =====" -ForegroundColor Cyan
python orchestrate.py --compounds Rapamycin Lithium "Urolithin A"
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: orchestrate.py exited $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

$bundle      = "data/rapam-lithi-uroli-bundle.json"
$comboReview = "data/Rapamycin-+-Lithium-+-Urolithin-A-combo-review.json"

$missing = @()
if (-not (Test-Path $bundle))      { $missing += $bundle }
if (-not (Test-Path $comboReview)) { $missing += $comboReview }

if ($missing.Count -gt 0) {
    foreach ($f in $missing) { Write-Host "MISSING: $f" -ForegroundColor Red }
    exit 1
}
Write-Host "PASSED" -ForegroundColor Green
Write-Host "  Bundle:       $bundle"
Write-Host "  Combo review: $comboReview"
