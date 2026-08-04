# Постоянный теннисный сбор: внешний цикл рестарта при падении процесса.
#
# Запуск (за человеком), сутки = 1440 мин:
#     powershell -ExecutionPolicy Bypass -File probes/deepseek/tennis_daemon.ps1
#
# Как работает:
#   - запускает ws_collector на --minutes 1440 (сутки), вертикаль tennis,
#     перепоиск рынков по умолчанию включён;
#   - лог ненакапливающийся: каждый старт перезаписывает
#     data/logs/tennis_{ts}.log через Start-Process -RedirectStandardOutput
#     (никаких >> внутрь скрипта), старые логи обрезаются до последнего
#     ROLLING_MAX_COUNT;
#   - если процесс упал раньше суток (exit code != 0), внешний цикл ждёт
#     RESTART_DELAY_SEC и запускает снова; рестарт происходит НЕ в коллекторе;
#   - код возврата 0 (штатное завершение по --minutes) останавливает цикл.
#
# systemd не используется (Windows); это замена демон-циклу.
# Изменять код коллектора для этого скрипта не требуется.

$ErrorActionPreference = "Stop"
$LogRoot = Join-Path $PSScriptRoot "..\..\data\logs"
$RollingMaxCount = 3
$RestartDelaySec = 30
$Minutes = 1440
$Vertical = "tennis"

function Get-Timestamp { Get-Date -Format "yyyyMMdd_HHmmss" }

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

while ($true) {
    $ts = Get-Timestamp
    $logOut = Join-Path $LogRoot "tennis_${ts}.out.log"
    $logErr = Join-Path $LogRoot "tennis_${ts}.err.log"

    Write-Host ("[{0}] старт коллектора (vertical={1}, minutes={2})" -f (Get-Date -Format o), $Vertical, $Minutes)
    Write-Host ("  stdout -> {0}" -f $logOut)
    Write-Host ("  stderr -> {0}" -f $logErr)

    $p = Start-Process -FilePath "python" `
        -ArgumentList @("-m", "src.collect.ws_collector", "--vertical", $Vertical, "--minutes", "$Minutes") `
        -WorkingDirectory (Join-Path $PSScriptRoot "..\..") `
        -RedirectStandardOutput $logOut `
        -RedirectStandardError $logErr `
        -NoNewWindow `
        -PassThru
    $p.WaitForExit()

    $code = $p.ExitCode
    Write-Host ("[{0}] коллектор завершился, код {1}" -f (Get-Date -Format o), $code)

    # Оставляем только последние RollingMaxCount пар логов.
    Get-ChildItem -Path $LogRoot -Filter "tennis_*.log" | `
        Sort-Object Name -Descending | `
        Select-Object -Skip ($RollingMaxCount * 2) | `
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    if ($code -eq 0) {
        Write-Host ("[{0}] штатное завершение по --minutes; цикл остановлен" -f (Get-Date -Format o))
        exit 0
    }
    Write-Host ("[{0}] падение (код {1}); рестарт через {2} с" -f (Get-Date -Format o), $code, $RestartDelaySec)
    Start-Sleep -Seconds $RestartDelaySec
}
