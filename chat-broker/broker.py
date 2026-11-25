"""Lease broker: no model calls; never store the lease in GitHub."""
import os, re, json, time, base64, subprocess, urllib.parse, urllib.request, urllib.error, sys, signal
from pathlib import Path

REPO=os.environ["GITHUB_REPOSITORY"]
BRANCH="chat-lean-broker"
HANDLE=os.environ["GITHUB_REPOSITORY_OWNER"]
DESK="https://pequalnp.com/desk/"
RUN=os.environ["GITHUB_RUN_ID"]
LINK=f"https://github.com/{REPO}/actions/runs/{RUN}"
def gh(path, body=None):
    args=["gh","api",path]
    if body is not None: args += ["--method","PUT","--input","-"]
    p=subprocess.run(args,input=json.dumps(body) if body is not None else None,text=True,capture_output=True,timeout=25)
    if p.returncode: raise RuntimeError("GitHub state API failed")
    return json.loads(p.stdout)
def get(path, optional=False):
    try:
        x=gh(f"repos/{REPO}/contents/{path}?ref={BRANCH}")
        return json.loads(base64.b64decode(x["content"])), x["sha"]
    except Exception:
        if optional: return None,None
        raise
def put(path,data):
    old,sha=get(path,True)
    body={"message":"Update public Chat session state","branch":BRANCH,"content":base64.b64encode(json.dumps(data,ensure_ascii=False,indent=2).encode()).decode()}
    if sha: body["sha"]=sha
    gh(f"repos/{REPO}/contents/{path}",body)
def desk(action, params, method="GET"):
    url=DESK+action+"?"+urllib.parse.urlencode(params)
    try:
        # Follow the official curl client; no redirects, no secret-bearing output.
        # Pass URL through stdin config, not command line (lease stays out of process listing).
        p=subprocess.run(["curl","-sS","--max-time","25","--proto","=https","-X",method,"-w","\n%{http_code}","--config","-"],input="url = "+json.dumps(url)+"\n",text=True,capture_output=True,timeout=30)
        if p.returncode: return 0,{}
        raw,code=p.stdout.rsplit("\n",1)
        try: data=json.loads(raw)
        except ValueError: data={}
        return int(code), data if isinstance(data,dict) else {}
    except Exception: return 0,{}
def safe_gap(g):
    return {k:g[k] for k in ("id","statement","file","module","imports","context","dead","sketch","refutation","shard_spec","title","unlocks","kind","reductions") if k in g}
req=json.loads(Path("chat-broker/request.json").read_text())
session=req.get("session","")
if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}",session): sys.exit("invalid session")
mode=req.get("mode")
if mode not in ("selftest","normal","hard"): sys.exit("invalid mode")
current,_=get("chat-broker/request.json")
if current!=req: sys.exit("stale request")
state={"session":session,"run_url":LINK,"mode":mode,"status":"starting"}
def publish(status,**fields):
    state.update(fields,status=status,updated_at=int(time.time()))
    put("chat-broker/state.json",state)
lease=None
def stop_handler(*args): raise KeyboardInterrupt()
signal.signal(signal.SIGTERM,stop_handler)
if mode=="selftest":
    publish("selftest_ok")
    sys.exit(0)
# No retries when the desk says no work. A user can request a fresh session later.
try:
    params={"handle":HANDLE}
    if mode=="hard": params["hard"]="1"
    code,data=desk("next",params)
    if code!=200:
        publish("desk_error",http=code); sys.exit(1)
    g=data.get("gap")
    if not g:
        publish("no_gap",note=str(data.get("note",""))[:400]); sys.exit(0)
    info=data.get("lease") or {}
    lease=info.get("id")
    if not isinstance(lease,str) or not lease: publish("invalid_lease_response"); sys.exit(1)
    gid=g["id"]
    if not re.fullmatch(r"L-[a-z0-9]+(?:-[a-z0-9]+)*",gid): raise RuntimeError("invalid gap id")
    public=safe_gap(g)
    publish("held",gap=public,expires_at=info.get("expires_at"),shard=data.get("shard"),shards=data.get("shards"),last_seq=0)
    start=last_activity=last_beat=time.monotonic()
    seq=0
    reasons={"hypothesis_too_weak","missing_lemma","statement_seems_false","does_not_elaborate","out_of_time","other","reduced","exhausted","submitted"}
    def beat():
        global last_beat, lease
        code,answer=desk("beat",{"lease":lease},"POST")
        if code==410:
            lease=None; publish("lost",http=410); return False
        if code!=200:
            publish("renew_failed",http=code); return False
        last_beat=time.monotonic()
        publish("held",expires_at=answer.get("expires_at") or (answer.get("lease") or {}).get("expires_at"),last_seq=seq)
        return True
    while time.monotonic()-start<5400 and time.monotonic()-last_activity<1500:
        control,_=get("chat-broker/control.json",True)
        if isinstance(control,dict) and control.get("session")==session and control.get("gap")==gid and isinstance(control.get("seq"),int) and control["seq"]>seq:
            seq=control["seq"]; last_activity=time.monotonic()
            op=control.get("op")
            if op in ("renew","prepare_submit"):
                if not beat(): break
                if op=="prepare_submit":
                    c,ans=desk("step",{"gap":gid})
                    s=ans.get("state")
                    if c==200 and s in ("open","leased"):
                        publish("ready_to_submit",last_seq=seq,desk_state=s,verified_commit=control.get("commit"))
                    else: publish("submit_blocked",last_seq=seq,desk_state=s,http=c)
            elif op=="check_status":
                c,ans=desk("step",{"gap":gid})
                publish("held",last_seq=seq,desk_state=ans.get("state"),http=c)
            elif op in ("release","stop"):
                reason=control.get("reason","out_of_time")
                if reason not in reasons: publish("invalid_control",last_seq=seq); continue
                if reason in ("submitted","reduced"):
                    pr=control.get("pr")
                    if not isinstance(pr,int) or pr<=0: publish("invalid_control",last_seq=seq); continue
                    x=gh(f"repos/open-goat/pequalnp-proofs/pulls/{pr}")
                    files=gh(f"repos/open-goat/pequalnp-proofs/pulls/{pr}/files?per_page=100")
                    path=public["file"] if reason=="submitted" else control.get("file")
                    if x["user"]["login"]!=HANDLE or len(files)!=1 or files[0]["filename"]!=path or files[0]["status"]!="added":
                        publish("pr_not_verified",last_seq=seq); continue
                params={"lease":lease,"reason":reason}
                if reason=="exhausted":
                    p=control.get("program","")
                    if not re.fullmatch(r"[0-9a-f]{64}",p): publish("invalid_control",last_seq=seq); continue
                    params["program"]=p
                c,_=desk("release",params,"POST")
                if 200<=c<300 or c==410:
                    lease=None; publish("released" if c<300 else "lost",last_seq=seq,reason=reason,http=c); break
                publish("release_failed",last_seq=seq,http=c)
            else: publish("invalid_control",last_seq=seq)
        if lease and time.monotonic()-last_beat>=480:
            if not beat(): break
        time.sleep(15)
finally:
    if lease:
        c,_=desk("release",{"lease":lease,"reason":"out_of_time"},"POST")
        publish("released" if 200<=c<300 else "release_unconfirmed",reason="out_of_time",http=c)
