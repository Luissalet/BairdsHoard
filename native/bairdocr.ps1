# Text of a picture with the place of every word, read with the OCR that ships with Windows (Windows.Media.Ocr).
# Baird starts this once and keeps it: one JSON request per line on stdin, one JSON answer per line on stdout.
#   request  {"id": 7, "path": "C:\\...\\frame.png", "lang": "es-ES"}        (lang optional)
#   answer   {"id": 7, "ok": true, "w": 1920, "h": 1080, "lang": "es-ES", "lines": [[x, y, w, h, "text", [[x, y, w, h, "word"], ...]], ...]}
#   answer   {"id": 7, "ok": false, "error": "..."}
# A first line {"ready": true, "langs": [...]} says which languages Windows can read. Nothing is kept or sent anywhere.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
$out.AutoFlush = $true
$in = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)

function Say($value) { $out.WriteLine(($value | ConvertTo-Json -Compress -Depth 8)) }

try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  [void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  [void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
  [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
  [void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
  [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  [void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
} catch {
  Say @{ ready = $false; error = "Windows OCR is not available: $($_.Exception.Message)" }
  exit 1
}

function Await($operation, [Type]$type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
  [void]$task.Wait(20000)
  if (-not $task.IsCompleted) { throw 'Windows OCR did not answer in time.' }
  $task.Result
}

$engines = @{}
function Engine([string]$lang) {
  if ($engines.ContainsKey($lang)) { return $engines[$lang] }
  $engine = $null
  if ($lang) {
    try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language($lang))) } catch { $engine = $null }
  }
  if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
  if ($null -eq $engine) {
    $first = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object -First 1
    if ($first) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($first) }
  }
  if ($null -eq $engine) { throw 'Windows has no OCR language installed (Settings > Time and language > Language).' }
  $engines[$lang] = $engine
  $engine
}

$langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
Say @{ ready = $true; langs = $langs; max = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension }

while ($true) {
  $line = $in.ReadLine()
  if ($null -eq $line) { break }
  if (-not $line.Trim()) { continue }
  $id = $null
  try {
    $req = $line | ConvertFrom-Json
    $id = $req.id
    $engine = Engine ([string]$req.lang)
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$req.path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
      $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
      $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
      try {
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = New-Object System.Collections.ArrayList
        foreach ($l in $result.Lines) {
          $words = New-Object System.Collections.ArrayList
          $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0.0; $y1 = 0.0
          foreach ($w in $l.Words) {
            $r = $w.BoundingRect
            [void]$words.Add(@([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, [string]$w.Text))
            if ($r.X -lt $x0) { $x0 = $r.X }
            if ($r.Y -lt $y0) { $y0 = $r.Y }
            if ($r.X + $r.Width -gt $x1) { $x1 = $r.X + $r.Width }
            if ($r.Y + $r.Height -gt $y1) { $y1 = $r.Y + $r.Height }
          }
          if ($words.Count) { [void]$lines.Add(@([int]$x0, [int]$y0, [int]($x1 - $x0), [int]($y1 - $y0), [string]$l.Text, $words.ToArray())) }
        }
        $answer = @{ id = $id; ok = $true; w = [int]$bitmap.PixelWidth; h = [int]$bitmap.PixelHeight; lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines.ToArray() }
      } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
    Say $answer
  } catch {
    $message = $_.Exception.GetBaseException().Message
    Say @{ id = $id; ok = $false; error = [string]$message }
  }
}
