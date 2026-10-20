"""Jarvis-noden käyttöönotto Windows-työkoneelle SSH:n yli.

Kulku: paketoi jarvis_core + jarvis_node zipiksi → scp käyttäjän kotikansioon →
generoi PowerShell-asennusskripti (node.json ja token base64:nä skriptin sisällä)
→ scp skripti → aja se (skripti poistaa itsensä) → raportti.

Token ei koskaan päädy lokiin, komentoriville eikä paluuarvoon.
"""

from __future__ import annotations

import base64
import json
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_core.protokolla import NODE_PORTTI, VERSIO

KANSIO = r"%USERPROFILE%\JarvisNode"
ZIP_NIMI = "JarvisNode.zip"
SKRIPTI_NIMI = "JarvisNode_asenna.ps1"
# Lisää eteen esim. ComfyUI:n upotettu python.exe, jos koneella ei ole muuta Pythonia.
OLETUS_PYTHONIT = ["py", "python"]
OLETUS_TEHTAVA = "Jarvis Node"
AIVOT_KUUNTELE = ["127.0.0.1"]


def _ps(teksti: str) -> str:
    """PowerShell-literaali yksinkertaisissa lainausmerkeissä."""
    return "'" + str(teksti).replace("'", "''") + "'"


def _b64(teksti: str) -> str:
    return base64.b64encode(teksti.encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------
# Paketti


def _mukaan(polku: Path) -> bool:
    nimi = polku.name
    return not (nimi.startswith("test_") or nimi == "conftest.py" or nimi.endswith("_test.py"))


def paketti(repo: Path, kohde: Path) -> Path:
    """Zip: jarvis_core/*.py + jarvis_node/*.py (+ node.example.json), ei testejä.

    `kohde` voi olla zip-tiedoston polku tai kansio (silloin <kansio>/JarvisNode.zip).
    """
    repo = Path(repo)
    kohde = Path(kohde)
    if kohde.is_dir() or kohde.suffix.lower() != ".zip":
        kohde.mkdir(parents=True, exist_ok=True)
        kohde = kohde / ZIP_NIMI
    with zipfile.ZipFile(kohde, "w", zipfile.ZIP_DEFLATED) as z:
        for paketin in ("jarvis_core", "jarvis_node"):
            for tiedosto in sorted((repo / paketin).glob("*.py")):
                if _mukaan(tiedosto):
                    z.write(tiedosto, f"{paketin}/{tiedosto.name}")
        esimerkki = repo / "jarvis_node" / "node.example.json"
        if esimerkki.exists():
            z.write(esimerkki, "jarvis_node/node.example.json")
    return kohde


# --------------------------------------------------------------------------
# Asennusskripti


def asennusskripti(kansio: str = KANSIO, python_ehdokkaat: list[str] | None = None,
                   tehtava: str = OLETUS_TEHTAVA, *, config_b64: str = "", token_b64: str = "",
                   zip_polku: str = r"%USERPROFILE%" + "\\" + ZIP_NIMI,
                   portti: int = NODE_PORTTI) -> str:
    """PowerShell-skripti, joka asentaa/päivittää noden ja käynnistää sen.

    Polut saavat sisältää %YMPÄRISTÖMUUTTUJIA%. node.json ja token annetaan base64:nä;
    skripti ei tulosta niitä. Lopuksi tulostetaan "NODE_OK" tai virhe + node.log:n loppu.
    """
    ehdokkaat = python_ehdokkaat if python_ehdokkaat is not None else OLETUS_PYTHONIT
    ehdokas_lista = ", ".join(_ps(e) for e in ehdokkaat) or "''"
    rivit = [
        "$ErrorActionPreference = 'Stop'",
        # Virheet stdoutiin: SSH-kuoren CLIXML-stderr suodatetaan pois, joten ilman tätä
        # epäonnistunut asennus näytti pelkän "koodi 1" (16.10.2026).
        "trap { \"VIRHE: $($_.Exception.Message) (rivi $($_.InvocationInfo.ScriptLineNumber))\"; exit 1 }",
        "$ProgressPreference = 'SilentlyContinue'",
        f"$kansio = [Environment]::ExpandEnvironmentVariables({_ps(kansio)})",
        f"$zip = [Environment]::ExpandEnvironmentVariables({_ps(zip_polku)})",
        f"$tehtava = {_ps(tehtava)}",
        "if (-not (Test-Path -LiteralPath $zip)) { \"VIRHE: pakettia $zip ei löydy\"; exit 2 }",
        "",
        "# 1) pysäytä vanha node",
        "try { Stop-ScheduledTask -TaskName $tehtava -ErrorAction SilentlyContinue } catch { }",
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
        "'*JarvisNode*node.py*' -and $_.ProcessId -ne $PID } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        "Start-Sleep -Seconds 1",
        "",
        "# 2) pura uusi koodi",
        "New-Item -ItemType Directory -Force -Path $kansio | Out-Null",
        "foreach ($d in 'jarvis_core', 'jarvis_node') {",
        "  $p = Join-Path $kansio $d",
        "  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }",
        "}",
        "Expand-Archive -LiteralPath $zip -DestinationPath $kansio -Force",
        "Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue",
        "",
        "# 3) node.json ja token (base64, ei tulosteta)",
        "$utf8 = New-Object System.Text.UTF8Encoding($false)",
        "$jsonPolku = Join-Path $kansio 'node.json'",
        "$tokenPolku = Join-Path $kansio 'node_token.txt'",
        f"$b = [Convert]::FromBase64String({_ps(config_b64)})",
        "if ($b.Length -gt 0) { [IO.File]::WriteAllText($jsonPolku, $utf8.GetString($b), $utf8) }",
        f"$b = [Convert]::FromBase64String({_ps(token_b64)})",
        "if ($b.Length -gt 0) { [IO.File]::WriteAllText($tokenPolku, $utf8.GetString($b), $utf8) }",
        "Remove-Variable b",
        "try { icacls $tokenPolku /inheritance:r /grant:r \"$($env:USERNAME):F\" | Out-Null } "
        "catch { }",
        "if (-not (Test-Path -LiteralPath $jsonPolku)) { 'VIRHE: node.json puuttuu'; exit 3 }",
        "",
        "# 4) valitse Python (>= 3.10)",
        "$python = $null",
        f"foreach ($e in @({ehdokas_lista})) {{",
        "  if (-not $e) { continue }",
        "  $polku = $null",
        "  if (Test-Path -LiteralPath $e -PathType Leaf) { $polku = (Resolve-Path -LiteralPath $e).Path }",
        "  else {",
        "    $c = Get-Command $e -CommandType Application -ErrorAction SilentlyContinue | "
        "Select-Object -First 1",
        "    if ($c -and $c.Source -and $c.Source -notlike '*WindowsApps*') { $polku = $c.Source }",
        "  }",
        "  if (-not $polku) { continue }",
        "  & $polku -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null",
        "  if ($LASTEXITCODE -eq 0) { $python = $polku; break }",
        "}",
        "if (-not $python) { 'VIRHE: sopivaa Pythonia (>= 3.10) ei löytynyt'; exit 4 }",
        "$kansioPy = Split-Path -Parent $python",
        "$exe = $python",
        "foreach ($w in 'pythonw.exe', 'pyw.exe') {",
        "  $ehdokas = Join-Path $kansioPy $w",
        "  if (((Split-Path -Leaf $python) -like 'py*.exe') -and (Test-Path -LiteralPath $ehdokas)) "
        "{ $exe = $ehdokas; break }",
        "}",
        "\"Python: $exe\"",
        "",
        "# 5) ajastettu tehtävä (kirjautuessa, käyttäjän istunnossa, ei aikarajaa)",
        "$nodePy = Join-Path $kansio 'jarvis_node\\node.py'",
        "$arg = '\"' + $nodePy + '\" --config \"' + $jsonPolku + '\"'",
        "$kayttaja = \"$env:USERDOMAIN\\$env:USERNAME\"",
        "$a = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $kansio",
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $kayttaja",
        "$pr = New-ScheduledTaskPrincipal -UserId $kayttaja -LogonType Interactive",
        "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew",
        "$tehtavaOk = $true",
        "try {",
        "  Register-ScheduledTask -TaskName $tehtava -Action $a -Trigger $t -Principal $pr "
        "-Settings $s -Force -ErrorAction Stop | Out-Null",
        "  'Ajastettu tehtävä rekisteröity'",
        "} catch {",
        "  $tehtavaOk = $false",
        "  \"Ajastettua tehtävää ei voitu rekisteröidä: $($_.Exception.Message) — käytetään "
        "Käynnistys-kansiota\"",
        "  $startup = [Environment]::GetFolderPath('Startup')",
        "  $cmd = '@echo off' + [Environment]::NewLine + 'start \"\" \"' + $exe + '\" ' + $arg",
        "  [IO.File]::WriteAllText((Join-Path $startup 'JarvisNode.cmd'), $cmd, $utf8)",
        "}",
        "",
        f"# 6) palomuuri TCP {portti} vain Tailscale + kotiverkko (virhe sallittu)",
        "try {",
        "  Get-NetFirewallRule -DisplayName 'Jarvis Node' -ErrorAction SilentlyContinue | "
        "Remove-NetFirewallRule -ErrorAction SilentlyContinue",
        f"  New-NetFirewallRule -DisplayName 'Jarvis Node' -Direction Inbound -Protocol TCP "
        f"-LocalPort {int(portti)} -RemoteAddress 100.64.0.0/10,192.168.0.0/16 -Action Allow "
        "| Out-Null",
        "  'Palomuurisääntö lisätty'",
        "} catch { \"Palomuurisääntöä ei voitu lisätä: $($_.Exception.Message)\" }",
        "",
        "# 7) käynnistä ja odota /health",
        "if ($tehtavaOk) { Start-ScheduledTask -TaskName $tehtava } else {",
        "  $cl = '\"' + $exe + '\" ' + $arg",
        "  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{ CommandLine = $cl; CurrentDirectory = $kansio }",
        "  \"Käynnistetty WMI:llä (pid $($r.ProcessId))\"",
        "}",
        "$ok = $false",
        "for ($i = 0; $i -lt 20; $i++) {",
        "  Start-Sleep -Seconds 1",
        "  try {",
        f"    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "
        f"-Uri 'http://127.0.0.1:{int(portti)}/health'",
        "    if ($r.StatusCode -eq 200) { $ok = $true; break }",
        "  } catch { }",
        "}",
        "if ($ok) { 'NODE_OK'; exit 0 }",
        "'VIRHE: node ei vastannut 20 s:ssa'",
        "$loki = Join-Path $kansio 'node.log'",
        "if (Test-Path -LiteralPath $loki) { Get-Content -LiteralPath $loki -Tail 30 }",
        "exit 5",
    ]
    return "\r\n".join(rivit) + "\r\n"


# --------------------------------------------------------------------------
# Asennus


def _kotipolku(device: dict, nimi: str) -> str:
    kayttaja = str(device.get("ssh_kayttaja") or "").strip()
    # ilman käyttäjää scp:n suhteellinen polku osuu kotikansioon
    return f"C:/Users/{kayttaja}/{nimi}" if kayttaja else nimi


def asenna(device: dict, repo: Path, token: str, config: dict,
           run_remote: Callable[..., tuple[int, str]] | None = None,
           copy_file: Callable[..., tuple[int, str]] | None = None,
           python_ehdokkaat: list[str] | None = None, tehtava: str = OLETUS_TEHTAVA) -> str:
    """Asenna/päivitä node laitteelle. Palauttaa suomenkielisen raportin (ei tokenia)."""
    if run_remote is None or copy_file is None:
        from jarvis_pool import devices

        run_remote = run_remote or devices.run_remote
        copy_file = copy_file or devices.copy_file
    if not token:
        return "Noden asennus keskeytyi: token puuttuu."
    nimi = str(device.get("nimi") or device.get("_key") or "laite")
    portti = int(config.get("portti") or NODE_PORTTI)

    def siivoa(teksti: str) -> str:
        return (teksti or "").replace(token, "***")

    with tempfile.TemporaryDirectory(prefix="jarvis_kayttoonotto_") as tmp:
        zip_polku = paketti(Path(repo), Path(tmp) / ZIP_NIMI)
        eta_zip = _kotipolku(device, ZIP_NIMI)
        koodi, ulos = copy_file(device, str(zip_polku), eta_zip, upload=True)
        if koodi != 0:
            return f"Noden asennus {nimi}: paketin kopiointi epäonnistui ({koodi}): {siivoa(ulos)}"

        skripti = asennusskripti(
            KANSIO, python_ehdokkaat, tehtava,
            config_b64=_b64(json.dumps(config, ensure_ascii=False, indent=2)),
            token_b64=_b64(token), portti=portti)
        skripti_polku = Path(tmp) / SKRIPTI_NIMI
        # BOM: Windows PowerShell 5.1 lukee ääkköset oikein
        skripti_polku.write_text(skripti, encoding="utf-8-sig")
        eta_skripti = _kotipolku(device, SKRIPTI_NIMI)
        koodi, ulos = copy_file(device, str(skripti_polku), eta_skripti, upload=True)
        if koodi != 0:
            return f"Noden asennus {nimi}: skriptin kopiointi epäonnistui ({koodi}): {siivoa(ulos)}"

    # Pieni ajokomento (pitkä -EncodedCommand ylittäisi cmd.exe:n 8191 merkin rajan)
    polku_ps = eta_skripti.replace("/", "\\")
    ajo = (f"$f = {_ps(polku_ps)}\n"
           "if (-not [IO.Path]::IsPathRooted($f)) { $f = Join-Path $env:USERPROFILE $f }\n"
           "try { & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $f; "
           "$k = $LASTEXITCODE } finally { Remove-Item -LiteralPath $f -Force "
           "-ErrorAction SilentlyContinue }\n"
           "exit $k")
    koodi, ulos = run_remote(device, ajo, timeout=240.0)
    ulos = siivoa(ulos).strip()
    if "NODE_OK" in ulos:
        return (f"Jarvis node asennettu koneelle {nimi} (versio {VERSIO}, portti {portti}) "
                f"ja vastaa. Ajastettu tehtävä '{tehtava}' käynnistää sen kirjautuessa.")
    hanta = "\n".join(ulos.splitlines()[-30:])
    return f"Noden asennus koneelle {nimi} epäonnistui (koodi {koodi}):\n{hanta}"


def tarvitseeko_asennus(url: str, token: str = "",
                        http: Callable[[str, float], bytes] | None = None) -> bool:
    """True jos /health ei vastaa tai versio poikkeaa omasta (token varattu tulevaan)."""
    def oletus(u: str, timeout: float) -> bytes:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(u, timeout=timeout) as r:
            return r.read()

    try:
        data = json.loads((http or oletus)(url.rstrip("/") + "/health", 4.0))
    except Exception:  # noqa: BLE001 - mikä tahansa virhe = asennus tarpeen
        return True
    return not (isinstance(data, dict) and data.get("ok") and data.get("versio") == VERSIO)


# --------------------------------------------------------------------------
# Oletuskonfiguraatiot


_PALVELU_KENTAT = ("portti", "terveys", "tehtava", "tunniste", "kaynnistys", "argumentit",
                   "kansio", "ymparisto", "odota_s")


def oletus_config_tyokoneelle(laite: dict, avain: str | None = None) -> dict[str, Any]:
    """Työkoneen node.json devices.json:n laitteesta (ollama + comfyui)."""
    node_id = avain or str(laite.get("_key") or "tyokone")
    palvelut: dict[str, dict] = {}
    for nimi in ("ollama", "comfyui"):
        sov = (laite.get("sovellukset") or {}).get(nimi)
        if sov:
            palvelut[nimi] = {k: sov[k] for k in _PALVELU_KENTAT if sov.get(k) not in (None, "")}
    return {
        "node_id": node_id,
        "nimi": str(laite.get("nimi") or node_id),
        "os": str(laite.get("os") or "windows"),
        "prioriteetti": 2,
        "kuuntele": ["0.0.0.0"],
        "portti": NODE_PORTTI,
        "palvelut": palvelut,
        "token_tiedosto": "node_token.txt",
    }


def oletus_config_aivoille(kuuntele: list[str] | None = None) -> dict[str, Any]:
    """Koordinaattorin oma (prosessin sisäinen) node."""
    return {
        "node_id": "aivot",
        "nimi": "Koordinaattori",
        "os": "windows",
        "prioriteetti": 1,
        "kuuntele": list(kuuntele) if kuuntele is not None else list(AIVOT_KUUNTELE),
        "portti": NODE_PORTTI,
        "palvelut": {
            "ollama": {"portti": 11434, "terveys": "/api/tags", "tunniste": "ollama"},
        },
        "token_tiedosto": "node_token.txt",
    }
