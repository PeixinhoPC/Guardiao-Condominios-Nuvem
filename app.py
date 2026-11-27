import asyncio
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, ImageStat

TZ_BR = ZoneInfo("America/Sao_Paulo")
BASE_DIR = Path(__file__).resolve().parent
MONITOR_DIR = BASE_DIR / "IMAGENS_MONITOR"
DB_PATH = BASE_DIR / "condominio_dados.db"
INTERVALO_SEG = 120

app = FastAPI(title="GUARDIAO CONDOMINIOS NUVEM")

_MODELOS_CACHE = {"lista": [], "expira": 0.0, "chave": ""}
_MODELOS_INEXISTENTES_404 = set()

TERMOS_BLOQUEADOS_VISAO = (
    "tts", "audio", "live", "embedding", "imagen", "image",
    "veo", "lyria", "robotics", "computer-use", "aqa", "thinking"
)


def agora_br() -> datetime:
    return datetime.now(TZ_BR)


def obter_chave_api() -> str:
    for nome in ("GUARDIAO_CHAVE_VISAO", "GUARDIAO_API_KEY", "GEMINI_API_KEY"):
        val = os.environ.get(nome, "").strip().strip('"').strip("'")
        if val:
            return val
    return ""


def _modelo_bloqueado(nome: str) -> bool:
    n = (nome or "").lower()
    return any(t in n for t in TERMOS_BLOQUEADOS_VISAO)


def _remover_modelo_404(modelo: str, ver: str):
    _MODELOS_INEXISTENTES_404.add((modelo, ver))
    if _MODELOS_CACHE["lista"]:
        _MODELOS_CACHE["lista"] = [x for x in _MODELOS_CACHE["lista"] if x != (modelo, ver)]


def iniciar_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cadastros_condominio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo_cadastro TEXT,
                identificador TEXT,
                unidade TEXT,
                responsavel TEXT,
                observacao TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ocorrencias_condominio (
                id TEXT PRIMARY KEY,
                iso_dt TEXT,
                dados_json TEXT
            )
        """)
        conn.commit()
        qtd = conn.execute("SELECT COUNT(*) FROM cadastros_condominio").fetchone()[0]
        if qtd == 0:
            exemplos = [
                ("PET", "CACHORRO CARAMELO / PORTE MEDIO", "BLOCO A - AP 204", "MARIANA SOUZA", "Pet registrado na portaria"),
                ("VEICULO / BICICLETA / SKATE", "BICICLETA / PATINETE / VEICULO", "BLOCO B - AP 102", "CARLOS HENRIQUE", "Cadastro de morador e vaga/acesso"),
                ("MORADOR", "MORADOR CIRCULACAO", "BLOCO C - AP 501", "PATRICIA ALMEIDA", "Cadastro interno de condominio")
            ]
            conn.executemany(
                "INSERT INTO cadastros_condominio (tipo_cadastro, identificador, unidade, responsavel, observacao) VALUES (?, ?, ?, ?, ?)",
                exemplos
            )
            conn.commit()


def listar_cadastros():
    iniciar_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, tipo_cadastro, identificador, unidade, responsavel, observacao FROM cadastros_condominio ORDER BY id DESC"
        ).fetchall()
    return [
        {
            "id": r[0],
            "tipo_cadastro": r[1],
            "identificador": r[2],
            "unidade": r[3],
            "responsavel": r[4],
            "observacao": r[5]
        }
        for r in rows
    ]


def salvar_cadastro(tipo_cadastro: str, identificador: str, unidade: str, responsavel: str, observacao: str = ""):
    iniciar_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO cadastros_condominio (tipo_cadastro, identificador, unidade, responsavel, observacao) VALUES (?, ?, ?, ?, ?)",
            (
                tipo_cadastro.strip().upper(),
                identificador.strip().upper(),
                unidade.strip().upper(),
                responsavel.strip().upper(),
                observacao.strip()
            )
        )
        conn.commit()


def salvar_ocorrencia_db(oc: dict):
    iniciar_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ocorrencias_condominio (id, iso_dt, dados_json) VALUES (?, ?, ?)",
            (oc["id"], oc.get("iso_dt", ""), json.dumps(oc, ensure_ascii=False))
        )
        conn.commit()


def carregar_ocorrencias_db():
    iniciar_db()
    lista = []
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT dados_json FROM ocorrencias_condominio ORDER BY iso_dt DESC LIMIT 100").fetchall()
        for (dj,) in rows:
            try:
                lista.append(json.loads(dj))
            except Exception:
                pass
    return lista


def cruzar_com_cadastro(categoria: str, resumo: str):
    cadastros = listar_cadastros()
    texto_alvo = f"{categoria} {resumo}".lower()
    melhor = None
    for c in cadastros:
        ident = (c.get("identificador") or "").lower()
        palavras = [p for p in re.split(r"[^a-z0-9]+", ident) if len(p) >= 4]
        if any(p in texto_alvo for p in palavras):
            melhor = c
            break
    if not melhor and cadastros:
        if "pet" in categoria.lower() or "xixi" in texto_alvo or "coco" in texto_alvo or "dejeto" in texto_alvo:
            melhor = next((c for c in cadastros if "pet" in (c.get("tipo_cadastro") or "").lower()), None)
        elif any(k in texto_alvo for k in ("bicicleta", "skate", "patinete", "veiculo", "carro", "velocidade")):
            melhor = next((c for c in cadastros if any(k in (c.get("tipo_cadastro") or "").lower() for k in ("veiculo", "bicicleta", "skate"))), None)
    if melhor:
        return {
            "vinculado": True,
            "unidade": melhor["unidade"],
            "responsavel": melhor["responsavel"],
            "cadastro_ref": f"{melhor['tipo_cadastro']} — {melhor['identificador']}"
        }
    return {
        "vinculado": False,
        "unidade": "A CONFERIR NA PORTARIA / CADASTRO",
        "responsavel": "SEM VINCULO DIRETO CADASTRADO",
        "cadastro_ref": "Adicionar no Cadastro Interno do Condominio"
    }


def listar_imagens():
    extensoes = {".jpg", ".jpeg", ".png", ".webp"}
    encontradas = []
    for pasta in (BASE_DIR, MONITOR_DIR):
        if pasta.exists() and pasta.is_dir():
            for arq in sorted(pasta.iterdir()):
                if arq.is_file() and arq.suffix.lower() in extensoes:
                    encontradas.append(arq)
    return encontradas


def _inspecionar_b64(caminho: Path):
    detalhes = "Iluminacao regular"
    b64_data = ""
    try:
        with Image.open(caminho) as im:
            im_rgb = im.convert("RGB")
            brilho = sum(ImageStat.Stat(im_rgb).mean) / 3.0
            detalhes = "Baixa iluminacao" if brilho < 55 else ("Alta iluminacao" if brilho > 215 else "Iluminacao regular")
            otim = im_rgb.copy()
            otim.thumbnail((720, 720))
            buf = BytesIO()
            otim.save(buf, format="JPEG", quality=75, optimize=True)
            b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        b64_data = ""
    return b64_data, detalhes


def _obter_modelos(chave: str):
    agora = time.time()
    if _MODELOS_CACHE["lista"] and _MODELOS_CACHE["chave"] == chave and agora < _MODELOS_CACHE["expira"]:
        return [p for p in _MODELOS_CACHE["lista"] if p not in _MODELOS_INEXISTENTES_404]
    fila = [
        ("gemini-flash-lite-latest", "v1beta"),
        ("gemini-2.5-flash-lite", "v1beta"),
        ("gemini-flash-latest", "v1beta"),
        ("gemini-2.5-flash", "v1beta")
    ]
    if chave:
        for ver in ("v1beta", "v1"):
            try:
                r = requests.get(f"https://generativelanguage.googleapis.com/{ver}/models?key={chave}", timeout=10)
                if r.status_code == 200:
                    descobertos = []
                    for item in (r.json().get("models") or []):
                        nome = str(item.get("name") or "").replace("models/", "").strip()
                        metodos = item.get("supportedGenerationMethods") or []
                        if nome and "generateContent" in metodos and "gemini" in nome.lower() and not _modelo_bloqueado(nome):
                            par = (nome, ver)
                            if par not in _MODELOS_INEXISTENTES_404:
                                descobertos.append(par)
                    if descobertos:
                        fila = descobertos[:8] + fila
                        break
            except Exception:
                pass
    _MODELOS_CACHE["lista"] = fila
    _MODELOS_CACHE["expira"] = agora + 1800.0
    _MODELOS_CACHE["chave"] = chave
    return fila


def _analisar_imagem_sync(caminho: Path):
    b64_data, detalhes = _inspecionar_b64(caminho)
    chave = obter_chave_api()
    if not chave or not b64_data:
        return {
            "categoria": "SEGURANCA_E_CIRCULACAO",
            "local": "AREA COMUM / VIA INTERNA DO CONDOMINIO",
            "resumo": f"Registro visual em {caminho.name}; verificar cadastro e portaria.",
            "condicao_local": detalhes
        }
    prompt = (
        "Voce e o assistente de monitoramento de condominio. Responda APENAS com JSON valido:\n"
        "{\n"
        '  "categoria": "PET_AREA_COMUM" | "VELOCIDADE_MOBILIDADE_VEICULO" | "SEGURANCA_E_CIRCULACAO",\n'
        '  "local": "local ou area comum",\n'
        '  "resumo": "descricao objetiva e curta do que ocorre (pet/dejetos, bicicleta/skate/patinete/veiculo em velocidade ou circulacao/seguranca)"\n'
        "}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }
    for modelo, ver in _obter_modelos(chave):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/{ver}/models/{modelo}:generateContent?key={chave}", json=payload, timeout=18)
            if r.status_code == 404:
                _remover_modelo_404(modelo, ver)
                continue
            if r.status_code == 200:
                txt = "".join(str(p.get("text", "")) for p in (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []))
                limpo = re.sub(r"```(?:json)?|```", "", txt, flags=re.IGNORECASE).strip()
                ini, fim = limpo.find("{"), limpo.rfind("}")
                if ini != -1 and fim > ini:
                    obj = json.loads(limpo[ini:fim + 1])
                    return {
                        "categoria": str(obj.get("categoria") or "SEGURANCA_E_CIRCULACAO"),
                        "local": str(obj.get("local") or "AREA COMUM DO CONDOMINIO"),
                        "resumo": str(obj.get("resumo") or "Evento visual registrado para conferencia."),
                        "condicao_local": detalhes
                    }
        except Exception:
            continue
    return {
        "categoria": "SEGURANCA_E_CIRCULACAO",
        "local": "AREA COMUM / VIA INTERNA DO CONDOMINIO",
        "resumo": f"Registro visual em {caminho.name}; verificar cadastro e portaria.",
        "condicao_local": detalhes
    }


def _montar_e_salvar_ocorrencia(categoria: str, local: str, resumo: str, arquivo: str = "REGISTRO_DIRETO", condicao_local: str = "Monitoramento em nuvem"):
    agora = agora_br()
    vinculo = cruzar_com_cadastro(categoria, resumo)
    oc = {
        "id": f"COND-{agora.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}",
        "iso_dt": agora.isoformat(),
        "data_hora": agora.strftime("%d/%m/%Y %H:%M:%S"),
        "categoria": categoria.strip().upper(),
        "local": local.strip() or "Area comum do condominio",
        "resumo": resumo.strip(),
        "arquivo": arquivo,
        "condicao_local": condicao_local,
        "vinculado": vinculo["vinculado"],
        "unidade": vinculo["unidade"],
        "responsavel": vinculo["responsavel"],
        "cadastro_ref": vinculo["cadastro_ref"],
        "status": "REGISTRADA_PARA_CONFERENCIA"
    }
    salvar_ocorrencia_db(oc)
    return oc


STATE = {"indice": -1, "ultima_ciclo": 0.0}


async def ciclo_imagens_se_houver():
    imgs = listar_imagens()
    if not imgs:
        return
    if (time.time() - STATE["ultima_ciclo"]) >= INTERVALO_SEG:
        STATE["indice"] = (STATE["indice"] + 1) % len(imgs)
        arq = imgs[STATE["indice"]]
        analise = await asyncio.to_thread(_analisar_imagem_sync, arq)
        _montar_e_salvar_ocorrencia(
            analise["categoria"],
            analise["local"],
            analise["resumo"],
            arquivo=arq.name,
            condicao_local=analise["condicao_local"]
        )
        STATE["ultima_ciclo"] = time.time()


async def loop_nuvem():
    iniciar_db()
    while True:
        await asyncio.sleep(2)
        try:
            await ciclo_imagens_se_houver()
        except Exception as exc:
            print(f"[COND-NUVEM] Aviso no ciclo: {exc}", flush=True)
            STATE["ultima_ciclo"] = time.time()


@app.on_event("startup")
async def on_startup():
    iniciar_db()
    asyncio.create_task(loop_nuvem())


@app.get("/api/estado")
async def api_estado():
    return JSONResponse({
        "agora": agora_br().strftime("%d/%m/%Y — %H:%M:%S"),
        "ocorrencias": carregar_ocorrencias_db(),
        "cadastros": listar_cadastros()
    })


@app.post("/api/cadastro")
async def api_cadastro(req: Request):
    d = await req.json()
    salvar_cadastro(
        d.get("tipo_cadastro") or "MORADOR",
        d.get("identificador") or "",
        d.get("unidade") or "",
        d.get("responsavel") or "",
        d.get("observacao") or ""
    )
    return JSONResponse({"ok": True, "cadastros": listar_cadastros()})


@app.post("/api/registrar-evento")
async def api_registrar_evento(req: Request):
    d = await req.json()
    oc = _montar_e_salvar_ocorrencia(
        d.get("categoria") or "PET_AREA_COMUM",
        d.get("local") or "Area comum do condominio",
        d.get("resumo") or "Evento registrado para apuracao da administracao."
    )
    return JSONResponse({"ok": True, "ocorrencia": oc})


@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GUARDIAO CONDOMINIOS — NUVEM</title>
<style>
body{margin:0;background:#090d16;color:#e6eef8;font-family:Segoe UI,sans-serif}
header{background:#0d2545;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #16a34a}
.wrap{max-width:1040px;margin:0 auto;padding:14px}
.card{background:#111827;border:1px solid #243247;border-radius:8px;padding:14px;margin-bottom:14px}
h3{margin:0 0 10px 0;color:#86efac;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px;margin-bottom:8px}
input,select{background:#0b101b;color:#fff;border:1px solid #334155;border-radius:6px;padding:8px;font-size:12px;width:100%;box-sizing:border-box}
button{background:#16a34a;color:#fff;border:none;border-radius:6px;padding:8px 14px;font-weight:700;cursor:pointer;font-size:12px}
button.sec{background:#2563eb}
.item{background:#0b101b;border:1px solid #1e293b;border-radius:6px;padding:10px;margin-top:8px;font-size:12px;line-height:1.45}
.ok{color:#86efac;font-weight:700}.pend{color:#fcd34d;font-weight:700}
</style>
</head>
<body>
<header>
  <div><b>GUARDIAO CONDOMINIOS</b> — 100% NUVEM &amp; CADASTRO INTERNO</div>
  <div id="relogio">--/--/---- --:--:--</div>
</header>
<div class="wrap">
  <div class="card">
    <h3>1. CADASTRO INTERNO DO CONDOMINIO (MORADORES, PETS, PLACAS/ADESIVOS, UNIDADES)</h3>
    <div class="grid">
      <select id="cTipo">
        <option value="PET">PET CADASTRADO</option>
        <option value="VEICULO / BICICLETA / SKATE">VEICULO / BICICLETA / SKATE</option>
        <option value="MORADOR">MORADOR / CONDUTOR</option>
      </select>
      <input id="cIdent" placeholder="Ex: CACHORRO CARAMELO / PLACA ABC1D23 / BIKE">
      <input id="cUnid" placeholder="Ex: BLOCO A - AP 302">
      <input id="cResp" placeholder="Nome do proprietario / responsavel">
    </div>
    <button onclick="salvarCadastro()">SALVAR NO CADASTRO INTERNO</button>
    <div id="listaCadastros"></div>
  </div>

  <div class="card">
    <h3>2. REGISTRO E CRUZAMENTO DE OCORRENCIA CONDOMINIAL</h3>
    <div class="grid">
      <select id="oCat">
        <option value="PET_AREA_COMUM">PET NA AREA COMUM / DEJETOS</option>
        <option value="VELOCIDADE_MOBILIDADE_VEICULO">BICICLETA / SKATE / VEICULO EM VELOCIDADE</option>
        <option value="SEGURANCA_E_CIRCULACAO">SEGURANCA E CIRCULACAO</option>
      </select>
      <input id="oLocal" placeholder="Ex: Praca Central / Alameda dos Ipes">
      <input id="oResumo" placeholder="Ex: Pet deixando dejetos na grama / Carro acima do limite">
    </div>
    <button class="sec" onclick="registrarEvento()">REGISTRAR E CRUZAR COM CADASTRO</button>
  </div>

  <div class="card">
    <h3>3. OCORRENCIAS REGISTRADAS E VINCULO DE UNIDADE</h3>
    <div id="listaOcorrencias"></div>
  </div>
</div>
<script>
function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
async function salvarCadastro(){
  await fetch("/api/cadastro",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
    tipo_cadastro:document.getElementById("cTipo").value,
    identificador:document.getElementById("cIdent").value,
    unidade:document.getElementById("cUnid").value,
    responsavel:document.getElementById("cResp").value
  })});
  document.getElementById("cIdent").value="";
  document.getElementById("cUnid").value="";
  document.getElementById("cResp").value="";
  atualizar();
}
async function registrarEvento(){
  await fetch("/api/registrar-evento",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
    categoria:document.getElementById("oCat").value,
    local:document.getElementById("oLocal").value,
    resumo:document.getElementById("oResumo").value
  })});
  document.getElementById("oLocal").value="";
  document.getElementById("oResumo").value="";
  atualizar();
}
async function atualizar(){
  const r = await fetch("/api/estado");
  const d = await r.json();
  document.getElementById("relogio").textContent = d.agora;
  document.getElementById("listaCadastros").innerHTML = (d.cadastros||[]).map(c =>
    "<div class='item'><b>"+esc(c.tipo_cadastro)+"</b> | "+esc(c.identificador)+" | <b>Unidade:</b> "+esc(c.unidade)+" | <b>Responsavel:</b> "+esc(c.responsavel)+"</div>"
  ).join("") || "<div class='item'>Nenhum cadastro registrado.</div>";
  document.getElementById("listaOcorrencias").innerHTML = (d.ocorrencias||[]).map(o =>
    "<div class='item'><b>["+esc(o.id)+"]</b> "+esc(o.data_hora)+" | <b>"+esc(o.categoria)+"</b> | <b>Local:</b> "+esc(o.local)+"<br>"+
    "<b>Ocorrencia:</b> "+esc(o.resumo)+"<br>"+
    "<span class='"+(o.vinculado?"ok":"pend")+"'>Unidade: "+esc(o.unidade)+" | Responsavel: "+esc(o.responsavel)+"</span> ("+esc(o.cadastro_ref)+")</div>"
  ).join("") || "<div class='item'>Nenhuma ocorrencia registrada ainda.</div>";
}
atualizar();
setInterval(atualizar, 2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    porta = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=porta, workers=1)
