# =========================================================================
# CEO Sales Reminder - Windows Task Scheduler Registration Script
# Registers the Pixel Studios Sales Intelligence System to run daily at 8:00 AM
# =========================================================================

# Define target paths
$PythonPath = "python.exe" # Resolves python from PATH, or specify full path
$ScriptPath = "c:\Users\siddh\Documents\dummy\main.py"
$WorkingDirectory = "c:\Users\siddh\Documents\dummy"
$TaskName = "PixelStudios_CEOSalesReminder"

# Check if script exists
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at: $ScriptPath. Please make sure the project directory matches."
    exit 1
}

# Create Scheduled Task Action
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "$ScriptPath --daily-report" -WorkingDirectory $WorkingDirectory

# Create Scheduled Task Trigger (Daily at 8:00 AM)
# Note: Task Scheduler uses local machine system time. Ensure system timezone is set to IST.
$Trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM

# Register the Scheduled Task
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Description "Daily CEO Sales Reminder and CRO Briefing for Pixel Studios." -Force

Write-Host "Success! Scheduled task '$TaskName' registered to run daily at 8:00 AM local time." -ForegroundColor Green
Write-Host "You can manage this task via the Windows Task Scheduler utility (taskschd.msc)." -ForegroundColor Yellow
