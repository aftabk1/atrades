Get-WmiObject Win32_Process | Where-Object {
    $_.CommandLine -like "*atrades*runner.py*"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
