@echo off
rem Hourly batch for Windows Task Scheduler (task "BulkMail Hourly Send").
rem Sends the next 180 people not yet emailed in the campaign, 3 per minute.
rem People already emailed are skipped, so each run picks up where the last stopped.
rem A refused login or blocked connection stops the run straight away.

cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo ================ %date% %time% ================ >> logs\scheduled_send.log
"C:\Users\USER\AppData\Local\Python\pythoncore-3.15-64\python.exe" bulkmail.py ^
  "C:\Users\USER\Desktop\Members_Mailing_List.csv" templates\hr_compliance.txt ^
  --campaign campaign --send --yes --limit 180 --rate 3 >> logs\scheduled_send.log 2>&1
