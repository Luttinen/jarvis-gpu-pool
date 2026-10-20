"""kayttoonotto.py:n testit (ei oikeaa SSH:ta)."""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

from jarvis_core import kayttoonotto
from jarvis_core.protokolla import VERSIO

REPO = Path(__file__).resolve().parent.parent
TOKEN = "f00dfeed" * 8


def test_paketti_sisalto(tmp_path):
    zip_polku = kayttoonotto.paketti(REPO, tmp_path)
    assert zip_polku == tmp_path / "JarvisNode.zip"
    nimet = set(zipfile.ZipFile(zip_polku).namelist())
    for odotettu in ("jarvis_core/__init__.py", "jarvis_core/protokolla.py",
                     "jarvis_core/kayttoonotto.py", "jarvis_node/__init__.py",
                     "jarvis_node/node.py", "jarvis_node/kyvyt.py", "jarvis_node/jarjestelma.py"):
        assert odotettu in nimet
    assert not any(Path(n).name.startswith("test_") for n in nimet)
    assert not any("__pycache__" in n for n in nimet)
    assert all(n.startswith(("jarvis_core/", "jarvis_node/")) for n in nimet)


def test_skripti_rakenne():
    s = kayttoonotto.asennusskripti(python_ehdokkaat=[r"D:\X\python.exe", "py"],
                                    config_b64="e30=", token_b64="dG9r")
    assert r"'D:\X\python.exe', 'py'" in s
    assert "Register-ScheduledTask" in s and "-AtLogOn" in s and "Interactive" in s
    assert "-RestartCount 3" in s and "-MultipleInstances IgnoreNew" in s
    assert "pythonw.exe" in s and "--config" in s
    assert "*JarvisNode*node.py*" in s
    assert "http://127.0.0.1:8791/health" in s and "NODE_OK" in s
    assert "100.64.0.0/10,192.168.0.0/16" in s
    assert "node.log" in s
    oletus = kayttoonotto.asennusskripti()
    assert "'py', 'python'" in oletus


def test_asenna_kutsuu_valefunktioita(tmp_path):
    kopiot: list[tuple[str, str, bool, bytes]] = []
    ajot: list[str] = []

    def copy_file(device, local, remote, upload=True, timeout=600.0):
        kopiot.append((device["nimi"], remote, upload, Path(local).read_bytes()))
        return 0, "ok"

    def run_remote(device, script, timeout=60.0):
        ajot.append(script)
        # vaikka etäpää kaiuttaisi tokenin, se ei saa päätyä raporttiin
        return 0, f"Python: x\nPalomuurisääntö lisätty\n{TOKEN}\nNODE_OK"

    device = {"nimi": "PELIKONE", "ssh_kayttaja": "gamer", "os": "windows"}
    config = {"node_id": "pelikone", "portti": 8791, "palvelut": {}}
    raportti = kayttoonotto.asenna(device, REPO, TOKEN, config, run_remote, copy_file)

    assert [k[1] for k in kopiot] == ["C:/Users/gamer/JarvisNode.zip",
                                      "C:/Users/gamer/JarvisNode_asenna.ps1"]
    assert all(k[2] is True for k in kopiot)
    assert kopiot[0][3][:2] == b"PK"
    skripti = kopiot[1][3].decode("utf-8-sig")
    assert TOKEN not in skripti
    assert base64.b64encode(TOKEN.encode()).decode() in skripti
    config_b64 = base64.b64encode(json.dumps(config, ensure_ascii=False, indent=2).encode()).decode()
    assert config_b64 in skripti

    assert len(ajot) == 1
    assert "JarvisNode_asenna.ps1" in ajot[0] and TOKEN not in ajot[0]
    assert "-ExecutionPolicy Bypass" in ajot[0] and "Remove-Item" in ajot[0]
    assert TOKEN not in raportti
    assert "PELIKONE" in raportti and "asennettu" in raportti


def test_asenna_virhe_raportoidaan():
    def copy_file(device, local, remote, upload=True, timeout=600.0):
        return 0, "ok"

    def run_remote(device, script, timeout=60.0):
        return 5, "VIRHE: node ei vastannut 20 s:ssa\nTraceback ..."

    raportti = kayttoonotto.asenna({"nimi": "J", "ssh_kayttaja": "u"}, REPO, TOKEN, {},
                                   run_remote, copy_file)
    assert "epäonnistui" in raportti and "ei vastannut" in raportti

    def copy_rikki(device, local, remote, upload=True, timeout=600.0):
        return 255, "yhteys poikki"

    ajot = []
    raportti = kayttoonotto.asenna({"nimi": "J"}, REPO, TOKEN, {},
                                   lambda *a, **k: ajot.append(1) or (0, ""), copy_rikki)
    assert "kopiointi epäonnistui" in raportti and not ajot


def test_tarvitseeko_asennus():
    def vastaa(versio):
        return lambda url, timeout: json.dumps({"ok": True, "node_id": "x",
                                                "versio": versio}).encode()

    def kaatuu(url, timeout):
        raise OSError("ei yhteyttä")

    assert kayttoonotto.tarvitseeko_asennus("http://x:8791", TOKEN, http=vastaa(VERSIO)) is False
    assert kayttoonotto.tarvitseeko_asennus("http://x:8791", TOKEN, http=vastaa("0.0.0")) is True
    assert kayttoonotto.tarvitseeko_asennus("http://x:8791", TOKEN, http=kaatuu) is True


def test_oletus_configit():
    laitteet = json.loads((REPO / "examples" / "devices.example.json").read_text(encoding="utf-8"))
    c = kayttoonotto.oletus_config_tyokoneelle(laitteet["laitteet"]["gaming-pc"], "gaming-pc")
    assert c["node_id"] == "gaming-pc" and c["prioriteetti"] == 2
    assert c["kuuntele"] == ["0.0.0.0"] and c["portti"] == 8791
    assert c["token_tiedosto"] == "node_token.txt"
    assert set(c["palvelut"]) == {"ollama", "comfyui"}
    assert c["palvelut"]["comfyui"]["tehtava"] == "ComfyUI"
    assert c["palvelut"]["comfyui"]["tunniste"] == "ComfyUI\\main.py"
    assert c["palvelut"]["ollama"]["argumentit"] == "serve"
    assert c["palvelut"]["ollama"]["portti"] == 11434

    j = kayttoonotto.oletus_config_aivoille()
    assert j["node_id"] == "aivot" and j["prioriteetti"] == 1
    assert j["kuuntele"] == ["127.0.0.1"]
    assert j["palvelut"]["ollama"] == {"portti": 11434, "terveys": "/api/tags",
                                       "tunniste": "ollama"}
    assert kayttoonotto.oletus_config_aivoille(["127.0.0.1", "::1"])["kuuntele"] == ["127.0.0.1", "::1"]
