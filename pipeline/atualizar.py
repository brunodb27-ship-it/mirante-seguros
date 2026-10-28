"""Atualiza os dados do painel de seguradoras.

1) SUSEP/SES (BaseCompleta.zip, atualizada pela SUSEP toda semana): prêmios, sinistros e comissões por
   seguradora x ramo x mês, grupos econômicos e balanços. Só baixa de novo quando o arquivo da SUSEP muda.
2) Notícias: Google Notícias (RSS) de cada grupo grande, classificadas por tema.

Uso:  python pipeline/atualizar.py            (tudo)
      python pipeline/atualizar.py --zip base.zip   (usa um zip já baixado)
      python pipeline/atualizar.py --sem-noticias | --so-noticias | --forcar
"""
import argparse, csv, io, json, os, re, sys, tempfile, time, urllib.request, urllib.parse, zipfile, email.utils
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "docs" / "data"
URL_SES = "https://www2.susep.gov.br/redarq.asp?arq=BaseCompleta.zip"
DESDE = "201901"
UA = {"User-Agent": "Mozilla/5.0 (painel-seguradoras; dados publicos SES/SUSEP)"}

# medidas de Ses_seguros.csv que o painel usa (em R$)
MEDIDAS = {"emitido": "premio_emitido2", "ganho": "premio_ganho", "sinistro": "sinistro_ocorrido", "comissao": "desp_com"}
# contas de Ses_Balanco.csv (valores acumulados no ano)
CONTAS = {"6183": "premio_emitido", "4027": "premio_ganho", "11232": "sinistros", "11237": "aquisicao", "6202": "outras_oper",
          "11238": "resseguro", "4069": "desp_adm", "4070": "tributos", "518": "lucro", "3333": "pl"}
SEM_GRUPO = {"01225", "99999", ""}

# linhas de negócio: códigos explícitos primeiro, depois o grupo de ramos (2 primeiros dígitos do código)
LINHAS = [
    ("vida", "Vida", {"0993", "1391", "0991", "2293"}, None),
    ("prestamista", "Prestamista", {"0977", "1377", "1061"}, None),
    ("ap", "Acidentes pessoais", {"0982", "1381", "0981"}, None),
    ("pessoas_outros", "Outros de pessoas", set(), {"09", "13"}),
    ("auto", "Automóvel", set(), {"05"}),
    ("patrimonial", "Patrimonial", set(), {"01"}),
    ("responsabilidades", "Responsabilidades", set(), {"03"}),
    ("transportes", "Transportes", set(), {"06"}),
    ("riscos_fin", "Garantia e riscos financeiros", set(), {"07"}),
    ("credito", "Crédito", set(), {"08"}),
    ("habitacional", "Habitacional", set(), {"10"}),
    ("rural", "Rural", set(), {"11"}),
    ("outros", "Outros ramos", set(), None),
]
FORA = re.compile(r"VGBL|PGBL|DPVAT|SOBREVIV", re.I)   # previdência e DPVAT não entram nas linhas


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def num(s):
    s = (s or "").strip()
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def ler(zf, nome):
    """CSV do zip (nome sem diferenciar maiúsculas), separador ';', com cabeçalho em minúsculas."""
    alvo = {n.lower().split("/")[-1]: n for n in zf.namelist()}
    real = alvo.get(nome.lower())
    if not real:
        return None
    raw = zf.open(real)
    txt = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    rd = csv.reader(txt, delimiter=";")
    cab = [c.strip().lower() for c in next(rd)]
    return cab, rd


def linha_do_ramo(cod, nome):
    if FORA.search(nome or ""):
        return None
    for lid, _, cods, grupos in LINHAS:
        if cod in cods:
            return lid
    for lid, _, cods, grupos in LINHAS:
        if grupos and cod[:2] in grupos:
            return lid
    return "outros"


def nomes_amigaveis():
    out = {}
    for ln in (RAIZ / "pipeline" / "nomes.txt").read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        p = [x.strip() for x in ln.split("|")]
        if len(p) >= 2:
            out[p[0].lower()] = (p[1], p[2] if len(p) > 2 else p[1])
    return out


SIGLAS = {"HDI", "BB", "AXA", "AIG", "BTG", "XP", "MAG", "BNP", "UBF", "MBM", "AGF", "IRB", "BMG", "PAN", "CNP", "EZZE", "QBE", "SBCE", "FATOR", "GBOEX", "ACE", "USEBENS"}
MINUSC = {"de", "do", "da", "dos", "das", "e", "em", "para"}


def limpa_nome(n):
    n = re.sub(r"\s+", " ", (n or "").strip())
    n = re.sub(r"\b(S\.?\s?A\.?|S/A|LTDA\.?)(?=\s|$)", "", n, flags=re.I)
    n = re.sub(r"\b(COMPANHIA|CIA\.?)\s+(DE\s+)?", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" -.,")
    n = re.sub(r"\s+(de|do|da|e)$", "", n, flags=re.I)
    if n.upper() == n:   # nome todo em maiúsculas: deixa só as iniciais, preservando siglas
        p = []
        for i, w in enumerate(n.split(" ")):
            lw = w.lower()
            p.append(w if w in SIGLAS else lw if (lw in MINUSC and i) else w.capitalize())
        n = " ".join(p)
    return n


# ---------------------------------------------------------------- SES
def baixar(destino):
    log("baixando", URL_SES)
    for tent in range(4):
        try:
            req = urllib.request.Request(URL_SES, headers=UA)
            with urllib.request.urlopen(req, timeout=900) as r, open(destino, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            zipfile.ZipFile(destino).testzip
            log("baixado:", round(os.path.getsize(destino) / 1e6), "MB")
            return
        except Exception as e:
            log("falhou:", e)
            time.sleep(30 * (tent + 1))
    raise SystemExit("Não consegui baixar a base da SUSEP.")


def assinatura_remota():
    try:
        req = urllib.request.Request(URL_SES, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            return f"{r.headers.get('Content-Length')}|{r.headers.get('Last-Modified')}"
    except Exception as e:
        log("não consegui consultar o tamanho do arquivo:", e)
        return None


def processar_ses(caminho):
    zf = zipfile.ZipFile(caminho)
    log("arquivos no zip:", ", ".join(sorted(n.split("/")[-1] for n in zf.namelist())))
    # nomes das seguradoras e dos ramos
    cab, rd = ler(zf, "Ses_cias.csv")
    ic, inn = cab.index("coenti"), cab.index("noenti")
    nome_cia = {r[ic].strip(): r[inn].strip() for r in rd if len(r) > max(ic, inn)}
    cab, rd = ler(zf, "Ses_ramos.csv")
    ir, irn = cab.index("coramo"), cab.index("noramo")
    nome_ramo = {r[ir].strip(): r[irn].strip() for r in rd if len(r) > max(ir, irn)}
    # grupo econômico mais recente de cada seguradora
    amig = nomes_amigaveis()
    grupo_de, grupo_nome = {}, {}
    g = ler(zf, "Ses_grupos_economicos.csv")
    if g:
        cab, rd = g
        k = {c: cab.index(c) for c in ("damesano", "coenti", "cogrupo", "nogrupo") if c in cab}
        ultimo = {}
        for r in rd:
            try:
                m, c, cg, ng = (r[k["damesano"]].strip(), r[k["coenti"]].strip(), r[k["cogrupo"]].strip(), r[k["nogrupo"]].strip())
            except (IndexError, KeyError):
                continue
            if c and (c not in ultimo or m > ultimo[c][0]):
                ultimo[c] = (m, cg, ng)
        for c, (m, cg, ng) in ultimo.items():
            if cg not in SEM_GRUPO and "OUTROS GRUPOS" not in ng.upper() and "INDEPENDENTE" not in ng.upper():
                grupo_de[c] = "g:" + cg
                grupo_nome["g:" + cg] = ng
    # produção
    cab, rd = ler(zf, "Ses_seguros.csv")
    falta = [c for c in ["damesano", "coenti", "coramo", *MEDIDAS.values()] if c not in cab]
    if falta:
        raise SystemExit("Colunas ausentes em Ses_seguros.csv: " + ", ".join(falta) + " | colunas: " + ", ".join(cab))
    im, ie, ira = cab.index("damesano"), cab.index("coenti"), cab.index("coramo")
    idx = [cab.index(v) for v in MEDIDAS.values()]
    ag = defaultdict(lambda: [0.0] * len(idx))
    n = 0
    for r in rd:
        n += 1
        try:
            m = r[im].strip()
            if m < DESDE:
                continue
            a = ag[(m, r[ie].strip(), r[ira].strip())]
            for j, i in enumerate(idx):
                a[j] += num(r[i])
        except IndexError:
            continue
    log(f"Ses_seguros: {n:,} linhas, {len(ag):,} combinações")
    meses = sorted({k[0] for k in ag})
    # balanço (acumulado no ano)
    bal = defaultdict(lambda: defaultdict(float))
    b = ler(zf, "Ses_Balanco.csv")
    if b:
        cab, rd = b
        kb = {c: cab.index(c) for c in ("damesano", "coenti", "cmpid", "valor")}
        nb = 0
        for r in rd:
            nb += 1
            try:
                conta = r[kb["cmpid"]].strip()
                if conta not in CONTAS:
                    continue
                m = r[kb["damesano"]].strip()
                if m < "201812":
                    continue
                bal[(m, r[kb["coenti"]].strip())][CONTAS[conta]] += num(r[kb["valor"]])
            except IndexError:
                continue
        log(f"Ses_Balanco: {nb:,} linhas")
    else:
        log("Ses_Balanco.csv não veio no zip: balanços ficam de fora desta rodada")
    return dict(nome_cia=nome_cia, nome_ramo=nome_ramo, grupo_de=grupo_de, grupo_nome=grupo_nome, amig=amig, ag=ag, meses=meses, bal=bal)


def gravar_ses(S, assinatura):
    amig = S["amig"]
    meses = S["meses"]
    mi = {m: i for i, m in enumerate(meses)}
    # entidades e grupos
    cias_ativas = sorted({c for (_, c, _) in S["ag"]})
    grupos = {}
    def gid_de(c):
        g = S["grupo_de"].get(c)
        if g:
            bruto = S["grupo_nome"][g]
            nome, busca = amig.get(bruto.lower(), (limpa_nome(bruto), limpa_nome(bruto) + " seguros"))
        else:
            g = "c:" + c
            bruto = S["nome_cia"].get(c, c)
            nome = limpa_nome(bruto)
            busca = f'"{nome}"'
        if g.lower() in amig:   # nomes.txt também aceita o código (g:00035 para grupo, c:05070 para empresa)
            nome, busca = amig[g.lower()]
        grupos.setdefault(g, [g, nome, busca])
        return g
    gl = [gid_de(c) for c in cias_ativas]
    # nomes repetidos (ex.: uma empresa avulsa com o mesmo nome de um grupo): acrescenta o código SUSEP
    cont = defaultdict(int)
    for g in grupos.values():
        cont[g[1].lower()] += 1
    for g in grupos.values():
        if cont[g[1].lower()] > 1 and g[0].startswith("c:"):
            g[1] = f"{g[1]} · {g[0][2:]}"
    glist = sorted(grupos.values(), key=lambda x: x[1])
    gi = {g[0]: i for i, g in enumerate(glist)}
    cias = [[c, limpa_nome(S["nome_cia"].get(c, c)), gi[g]] for c, g in zip(cias_ativas, gl)]
    ci = {c[0]: i for i, c in enumerate(cias)}
    # ramos
    ramos_ativos = sorted({r for (_, _, r) in S["ag"]})
    ramos = []
    for r in ramos_ativos:
        nm = S["nome_ramo"].get(r, r)
        bonito = re.sub(r"\s*\(RUN OFF\)", "", nm, flags=re.I).strip()
        if bonito.upper() == bonito:
            bonito = bonito.capitalize()
        # pessoas: 09xx é coletivo e 13xx é individual (o nome da SUSEP às vezes é igual nos dois)
        if r[:2] == "13" and "indiv" not in bonito.lower():
            bonito += " individual"
        elif r[:2] == "09" and not re.search(r"grupo|coletiv", bonito, re.I) and any(k[:2] == "13" and S["nome_ramo"].get(k, "").strip().lower() == nm.strip().lower() for k in ramos_ativos):
            bonito += " coletivo"
        if re.search(r"RUN OFF", nm, re.I):
            bonito += " (em extinção)"
        ramos.append([r, bonito, linha_do_ramo(r, nm)])
    ri = {r[0]: i for i, r in enumerate(ramos)}
    # linhas
    por_linha = defaultdict(list)
    for (m, c, r), v in S["ag"].items():
        lid = ramos[ri[r]][2]
        if not lid:
            continue
        vals = [round(x / 1000) for x in v]   # R$ mil
        if any(vals):
            por_linha[lid].append([mi[m], ci[c], ri[r], *vals])
    (DADOS / "ses").mkdir(parents=True, exist_ok=True)
    linhas = []
    for lid, nome, _, _ in LINHAS:
        rows = sorted(por_linha.get(lid, []))
        (DADOS / "ses" / f"{lid}.json").write_text(json.dumps({"cols": ["mes", "cia", "ramo", *MEDIDAS.keys()], "rows": rows}, separators=(",", ":")), encoding="utf-8")
        linhas.append({"id": lid, "nome": nome, "ramos": [ri[r[0]] for r in ramos if r[2] == lid], "registros": len(rows)})
        log(f"linha {lid}: {len(rows):,} registros")
    # balanços por grupo: 12 meses móveis + PL + anos fechados
    bal = S["bal"]
    bmeses = sorted({m for (m, _) in bal})
    balanco = {}
    if bmeses:
        ult = bmeses[-1]
        y, mm = int(ult[:4]), int(ult[4:])
        ant = f"{y - 1}{mm:02d}"; dez = f"{y - 1}12"
        porg = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for (m, c), itens in bal.items():
            g = S["grupo_de"].get(c, "c:" + c)
            if g not in gi:
                continue
            for k, v in itens.items():
                porg[g][m][k] += v
        for g, pm in porg.items():
            def item(m, k): return pm.get(m, {}).get(k, 0.0)
            ttm = {k: item(ult, k) + item(dez, k) - item(ant, k) for k in CONTAS.values() if k != "pl"}
            anos = {}
            for a in range(2019, y):
                d = f"{a}12"
                if d in pm:
                    anos[str(a)] = {"lucro": round(item(d, "lucro") / 1000), "pl": round(item(d, "pl") / 1000), "emitido": round(item(d, "premio_emitido") / 1000)}
            balanco[gi[g]] = {"ttm": {k: round(v / 1000) for k, v in ttm.items()}, "pl": round(item(ult, "pl") / 1000), "anos": anos}
        balanco = {"ate": ult, "grupos": balanco}
    # cidade: resumo de 12 meses por grupo (prêmio total e por linha, ano anterior), para a aba de abertura
    n = len(meses)
    atual, antes = set(range(n - 12, n)), set(range(n - 24, n - 12))
    cid = defaultdict(lambda: {"e": 0, "e0": 0, "g": 0, "s": 0, "c": 0, "l": defaultdict(int)})
    for lid, rows in por_linha.items():
        for r in rows:
            g = cias[r[1]][2]
            if r[0] in atual:
                x = cid[g]; x["e"] += r[3]; x["g"] += r[4]; x["s"] += r[5]; x["c"] += r[6]; x["l"][lid] += r[3]
            elif r[0] in antes:
                cid[g]["e0"] += r[3]
    cidade = {"ate": meses[-1] if meses else None, "grupos": {str(g): {**{k: v for k, v in x.items() if k != "l"}, "l": dict(x["l"])} for g, x in cid.items() if x["e"] > 0}}
    (DADOS / "cidade.json").write_text(json.dumps(cidade, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    # corrida: prêmio de 12 meses móveis, mês a mês, dos grupos que já estiveram entre os 12 maiores
    def corrida_de(ids):
        men = defaultdict(lambda: [0] * n)
        for lid in ids:
            for r in por_linha.get(lid, []):
                men[cias[r[1]][2]][r[0]] += r[3]
        rol = {}
        for g, v in men.items():
            acc, o = 0, []
            for i in range(n):
                acc += v[i] - (v[i - 12] if i >= 12 else 0)
                o.append(acc if i >= 11 else 0)
            rol[g] = o
        cand = set()
        for i in range(11, n):
            cand |= set(sorted(rol, key=lambda g: -rol[g][i])[:12])
        cand = sorted(cand, key=lambda g: -rol[g][-1])[:24]
        return {"g": cand, "s": [rol[g][11:] for g in cand]}
    ids_todas = [l[0] for l in LINHAS]
    corrida = {"meses": meses[11:], "linhas": {"todas": corrida_de(ids_todas), "pessoas": corrida_de(["vida", "prestamista", "ap", "pessoas_outros"])}}
    for lid, _, _, _ in LINHAS:
        corrida["linhas"][lid] = corrida_de([lid])
    (DADOS / "corrida.json").write_text(json.dumps(corrida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    meta = {"atualizado": datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M"), "ses_ate": meses[-1] if meses else None,
            "assinatura_ses": assinatura, "meses": meses, "unidade": "R$ mil", "linhas": linhas, "ramos": ramos, "cias": cias, "grupos": glist,
            "fonte": "SUSEP · Sistema de Estatísticas (SES), base completa"}
    (DADOS / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DADOS / "balanco.json").write_text(json.dumps(balanco, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("SES gravado até", meta["ses_ate"], "|", len(cias), "seguradoras,", len(glist), "grupos")
    return meta


# ---------------------------------------------------------------- notícias
TEMAS = [
    ("Produto", r"lan[çc]a|lan[çc]amento|novo (seguro|produto|plano)|nova (cobertura|solu[çc][ãa]o|vers[ãa]o)|estreia|apresenta (o|a|seu|sua)"),
    ("Marketing", r"campanha|patroc[íi]n|marca|publicidade|comercial|influenc|embaixador|naming|a[çc][ãa]o de|evento"),
    ("Resultado", r"lucro|resultado|balan[çc]o|trimestre|semestre|faturamento|cresc(e|imento)|receita|pr[êe]mios? (emitidos?|cresc)|market share|ranking"),
    ("Parceria", r"parceria|acordo|joint|bancassurance|distribui[çc][ãa]o|alian[çc]a|insurtech|integra"),
    ("Aquisição", r"aquisi[çc][ãa]o|compra|fus[ãa]o|incorpora|venda de|vende"),
    ("Pessoas", r"\bCEO\b|presidente|diretor|vice-presidente|executiv|nomeia|assume|contrata|deixa o cargo"),
    ("Regulação", r"susep|regula|resolu[çc][ãa]o|circular|multa|cnsp|lei "),
]


def temas(titulo):
    return [nome for nome, rx in TEMAS if re.search(rx, titulo, re.I)] or ["Geral"]


def rss(termo, dias=60):
    q = urllib.parse.quote(f"{termo} when:{dias}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        raiz = ET.fromstring(r.read())
    itens = []
    for it in raiz.iter("item"):
        t = (it.findtext("title") or "").strip()
        fonte = (it.findtext("source") or "").strip()
        if fonte and t.endswith(" - " + fonte):
            t = t[: -len(fonte) - 3].strip()
        try:
            dt = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        itens.append({"titulo": t, "fonte": fonte, "link": (it.findtext("link") or "").strip(), "data": dt.strftime("%Y-%m-%d"), "temas": temas(t)})
    return itens


def atualizar_noticias(meta):
    # grupos a acompanhar: os 30 maiores em pessoas + os 20 maiores no total (12 meses)
    meses = meta["meses"][-12:]
    mset = {meta["meses"].index(m) for m in meses}
    tot_p, tot = defaultdict(float), defaultdict(float)
    for ln in meta["linhas"]:
        try:
            rows = json.loads((DADOS / "ses" / f"{ln['id']}.json").read_text(encoding="utf-8"))["rows"]
        except Exception:
            continue
        for r in rows:
            if r[0] in mset:
                g = meta["cias"][r[1]][2]
                tot[g] += r[3]
                if ln["id"] in ("vida", "prestamista", "ap", "pessoas_outros"):
                    tot_p[g] += r[3]
    alvo = [g for g, _ in sorted(tot_p.items(), key=lambda x: -x[1])[:30]]
    alvo += [g for g, _ in sorted(tot.items(), key=lambda x: -x[1])[:20] if g not in alvo]
    try:
        velho = json.loads((DADOS / "noticias.json").read_text(encoding="utf-8"))
    except Exception:
        velho = {"grupos": {}, "mercado": []}
    out = {"atualizado": datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M"), "grupos": {}, "mercado": []}
    falhas = 0
    for g in alvo:
        gid, nome, busca = meta["grupos"][g]
        try:
            its = rss(busca)
            vistos, lst = set(), []
            for it in sorted(its, key=lambda x: x["data"], reverse=True):
                k = re.sub(r"\W+", "", it["titulo"].lower())[:70]
                if k in vistos:
                    continue
                vistos.add(k); lst.append(it)
            out["grupos"][gid] = lst[:25]
        except Exception as e:
            falhas += 1
            out["grupos"][gid] = velho.get("grupos", {}).get(gid, [])
            log("notícias falharam para", nome, e)
        time.sleep(1.2)
    for termo in ['"seguro de vida"', 'SUSEP seguros', '"mercado segurador"']:
        try:
            out["mercado"] += rss(termo, 30)
        except Exception as e:
            falhas += 1
            log("notícias do mercado falharam:", e)
        time.sleep(1.2)
    vistos, merc = set(), []
    for it in sorted(out["mercado"], key=lambda x: x["data"], reverse=True):
        k = re.sub(r"\W+", "", it["titulo"].lower())[:70]
        if k not in vistos:
            vistos.add(k); merc.append(it)
    out["mercado"] = merc[:60] or velho.get("mercado", [])
    total = sum(len(v) for v in out["grupos"].values())
    if total == 0 and velho.get("grupos"):
        log("nenhuma notícia nova: mantenho o arquivo anterior"); return
    (DADOS / "noticias.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"notícias: {total} de {len(alvo)} grupos, {falhas} falhas")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip"); ap.add_argument("--forcar", action="store_true")
    ap.add_argument("--sem-noticias", action="store_true"); ap.add_argument("--so-noticias", action="store_true")
    a = ap.parse_args()
    DADOS.mkdir(parents=True, exist_ok=True)
    try:
        meta = json.loads((DADOS / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = None
    if not a.so_noticias:
        if a.zip:
            meta = gravar_ses(processar_ses(a.zip), "local")
        else:
            ass = assinatura_remota()
            if meta and ass and ass == meta.get("assinatura_ses") and not a.forcar:
                log("base da SUSEP não mudou desde a última rodada:", ass)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    z = os.path.join(tmp, "base.zip")
                    baixar(z)
                    meta = gravar_ses(processar_ses(z), ass)
    if meta and not a.sem_noticias:
        atualizar_noticias(meta)


if __name__ == "__main__":
    main()
