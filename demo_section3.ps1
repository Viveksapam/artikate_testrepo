# Artikate Section 3 Live API Demonstration Script
# Run line-by-line or run the entire script: .\demo_section3.ps1

Write-Host "`n=== 1. Health Check (Unauthenticated) ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/health/" | ConvertTo-Json

Write-Host "`n=== 2. Authenticate and Obtain Token ===" -ForegroundColor Cyan
$loginBody = '{"username":"Admin","password":"Artikate<>90"}'
$token = (Invoke-RestMethod -Uri "http://localhost:8000/api/v1/token/" -Method Post -ContentType "application/json" -Body $loginBody).access
$headers = @{ 
    Authorization  = "Bearer $token"
    "Content-Type" = "application/json"
}
Write-Host "Obtained Token: $token" -ForegroundColor Green

Write-Host "`n=== 3. Check-out an Available Asset (201 Created) ===" -ForegroundColor Cyan
$checkoutBody = '{"asset_tag":"SEN-002","employee_code":"EMP-003","due_at":"2026-09-30T12:00:00Z"}'
$checkout = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/checkouts/" -Method Post -Headers $headers -Body $checkoutBody
$checkout | ConvertTo-Json

Write-Host "`n=== 4. Double Check-Out Conflict (409 Conflict) ===" -ForegroundColor Cyan
try {
    Invoke-RestMethod -Uri "http://localhost:8000/api/v1/checkouts/" -Method Post -Headers $headers -Body $checkoutBody
} catch {
    Write-Host "Status Code: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Yellow
    Write-Host $_.ErrorDetails.Message -ForegroundColor Yellow
}

Write-Host "`n=== 5. Inactive Employee Check-Out (400 Bad Request) ===" -ForegroundColor Cyan
$inactiveBody = '{"asset_tag":"VEH-001","employee_code":"EMP-004","due_at":"2026-09-30T12:00:00Z"}'
try {
    Invoke-RestMethod -Uri "http://localhost:8000/api/v1/checkouts/" -Method Post -Headers $headers -Body $inactiveBody
} catch {
    Write-Host "Status Code: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Yellow
    Write-Host $_.ErrorDetails.Message -ForegroundColor Yellow
}

Write-Host "`n=== 6. Return the Asset (200 OK) ===" -ForegroundColor Cyan
$returnId = $checkout.checkout_id
$returnBody = '{"condition_note":"Returned in perfect shape","needs_maintenance":false}'
$returnResult = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/checkouts/$returnId/return/" -Method Post -Headers $headers -Body $returnBody
$returnResult | ConvertTo-Json

Write-Host "`n=== 7. Duplicate Return Conflict (409 Conflict) ===" -ForegroundColor Cyan
try {
    Invoke-RestMethod -Uri "http://localhost:8000/api/v1/checkouts/$returnId/return/" -Method Post -Headers $headers -Body $returnBody
} catch {
    Write-Host "Status Code: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Yellow
    Write-Host $_.ErrorDetails.Message -ForegroundColor Yellow
}

Write-Host "`n=== 8. Employee Summary (Single Query Aggregation) ===" -ForegroundColor Cyan
$summary = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/employees/EMP-001/summary/" -Headers $headers
$summary | ConvertTo-Json

Write-Host "`n=== 9. Overdue Report (No N+1 Queries) ===" -ForegroundColor Cyan
$overdue = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/reports/overdue/" -Headers $headers
$overdue | ConvertTo-Json
