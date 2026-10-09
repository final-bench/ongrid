import os, json, time, collections, os.path as op, threading, urllib.request, urllib.error
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("KNOU_MODEL", "claude-haiku-4-5-20251001")

# ================= ZTC (zero-token answer verification, in-process on CPU) =================
ZTC_REPO = os.environ.get("ZTC_REPO", "FINAL-Bench/ZTC-Judge-4B")
_ZT = {"ready": False, "err": None, "model": None, "tok": None, "cfg": None, "lin": None, "cv": None}

def _ztc_load():
    try:
        import numpy as np, torch
        from transformers import AutoModel, AutoTokenizer
        from huggingface_hub import snapshot_download
        p = snapshot_download(ZTC_REPO)
        cfg = json.load(open(op.join(p, "ztc_config.json"), encoding="utf-8"))
        lin = dict(np.load(op.join(p, cfg["probe_file"]), allow_pickle=True))
        cvp = op.join(p, "ztc_curve_probe_v2.npz")
        if not op.exists(cvp):
            cvp = op.join(p, cfg.get("curve_probe_file", "ztc_curve_probe.npz"))
        cv = dict(np.load(cvp))
        tok = AutoTokenizer.from_pretrained(p)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        model = AutoModel.from_pretrained(p, dtype=torch.bfloat16, low_cpu_mem_usage=True).eval()
        _ZT.update(model=model, tok=tok, cfg=cfg, lin=lin, cv=cv, ready=True)
    except Exception as e:
        _ZT["err"] = str(e)[:200]

threading.Thread(target=_ztc_load, daemon=True).start()

def ztc_score(question, answer):
    import numpy as np, torch
    cfg, tok, model, lin, cv = _ZT["cfg"], _ZT["tok"], _ZT["model"], _ZT["lin"], _ZT["cv"]
    text = cfg["template"] % ((question or "").strip(), (answer or "").strip())
    b = tok([text], return_tensors="pt", truncation=True, max_length=cfg["max_length"])
    with torch.no_grad():
        h = model(input_ids=b["input_ids"], attention_mask=b["attention_mask"]).last_hidden_state
    v = h[0, int(b["attention_mask"].sum()) - 1].float().numpy().astype(np.float64)
    s = float(((v - lin["mu"]) / lin["sd"]) @ lin["w"])
    z = (v - cv["mu"]) / cv["sd"]; a = cv["anchors"].astype(np.float64)
    d2 = np.sum(a * a, 1) + float(z @ z) - 2.0 * (a @ z)
    k = np.exp(-np.maximum(d2, 0.0) / (float(cv["med"]) * float(cv["gamma"])))
    c = float(k @ cv["alpha"].astype(np.float64))
    t = cfg["thresholds"]
    verdict = "검토 요망" if s <= t["review"] else ("이상 없음" if s >= t["clear"] else "판단 보류")
    return {"score": round(s, 3), "verdict": verdict, "curved": round(c, 3), "repo": ZTC_REPO, "generated_tokens": 0}

# 비용 악용 방지: IP당 분당 호출 제한 (정상 사용엔 영향 없음)
_HITS = collections.defaultdict(list)
_LIMIT = int(os.environ.get("KNOU_RATE_PER_MIN", "20"))
def _rate_ok(ip):
    now = time.time(); q = _HITS[ip]
    while q and now - q[0] > 60: q.pop(0)
    if len(q) >= _LIMIT: return False
    q.append(now); return True

SYS = (
    "너는 지식그래프 기반 질의응답 엔진이다. 제공된 '사실(트리플)'만 근거로 답한다. "
    "트리플은 'A — 관계 → B' 형식이며 다음처럼 자연어로 해석해 답을 구성한다: "
    "IsA=A는 B의 한 종류, UsedFor=A는 B에 쓰임, AtLocation=A는 B에 있음, "
    "CapableOf=A는 B를 할 수 있음, MadeOf=A는 B로 만들어짐, HasA=A는 B를 가짐, "
    "HasProperty=A는 B한 성질, PartOf=A는 B의 일부, Causes=A는 B를 일으킴, HasSubevent=A를 하면 B가 일어남, RelatedTo=A는 B와 관련. "
    "질문 대상에 관한 사실들을 묶어 자신감 있게 지정된 언어로 2~4문장으로 답한다. "
    "관계 이름(IsA 등)을 그대로 나열하지 말고 자연스러운 문장으로 풀어 쓴다. "
    "보도 기사(기사 제목이 사실로 주어짐)의 경우 제목 내용을 근거로 사실만 요약한다. "
    "정말로 관련 사실이 하나도 없을 때만 해당 언어로 '관련 정보가 없습니다'라고 답한다. 없는 사실을 지어내지 않는다."
)

def _ip(req):
    fwd = req.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "x")) or "x"

@app.get("/health")
def health():
    return {"ok": True, "has_key": bool(KEY), "model": MODEL}

@app.post("/ask")
async def ask(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"answer": "요청 형식 오류."})
    q = (str(data.get("query") or "")).strip()[:600]
    facts = data.get("facts") or []
    facts = [str(f)[:200] for f in facts][:60]
    lang = str(data.get("lang") or "ko")[:2]
    langname = {"ko": "한국어(Korean)", "en": "English", "zh": "中文(Chinese)"}.get(lang, "한국어(Korean)")
    ip = (req.client.host if req.client else "x")
    fwd = req.headers.get("x-forwarded-for")
    if fwd: ip = fwd.split(",")[0].strip()
    if not _rate_ok(ip):
        return JSONResponse({"answer": "요청이 많습니다. 잠시 후 다시 시도해 주세요. / Rate limit, please retry shortly."})
    if not q:
        return JSONResponse({"answer": "..."})
    if not KEY:
        return JSONResponse({"answer": "(서버에 LLM 키가 설정되지 않았습니다. Space Secrets에 ANTHROPIC_API_KEY를 추가하세요.)"})
    factstr = "\n".join("- " + f for f in facts) if facts else "(관련 사실 없음)"
    prompt = ("Facts retrieved from the knowledge graph:\n" + factstr + "\n\nQuestion: " + q +
              "\n\nAnswer ONLY from the facts above, in " + langname +
              ", in 2-4 sentences. If the facts are insufficient, say so briefly in " + langname + ". Do not invent facts.")
    body = json.dumps({
        "model": MODEL, "max_tokens": 400, "system": SYS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    r = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(r, timeout=45)
        d = json.load(resp)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        return JSONResponse({"answer": text or "(빈 응답)"})
    except urllib.error.HTTPError as e:
        return JSONResponse({"answer": "(LLM 오류 " + str(e.code) + ")"})
    except Exception:
        return JSONResponse({"answer": "(LLM 호출 실패)"})

@app.get("/ztc_status")
def ztc_status():
    return {"ready": _ZT["ready"], "err": _ZT["err"], "repo": ZTC_REPO}

@app.post("/verify")
async def verify_ep(req: Request):
    if not _rate_ok("v:" + _ip(req)):
        return JSONResponse({"ok": False, "error": "rate"})
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False})
    q = str(data.get("query") or "")[:600]; a = str(data.get("answer") or "")[:2000]
    if not q or not a:
        return JSONResponse({"ok": False})
    if not _ZT["ready"]:
        return JSONResponse({"ok": False, "error": "ztc_loading", "detail": _ZT["err"]})
    try:
        return JSONResponse({"ok": True, "ztc": ztc_score(q, a)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:160]})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
