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
def parse_object(raw):
    try: data=json.loads(raw)
    except (ValueError, TypeError): return None
    return data if isinstance(data,dict) else None

def classify_next(code,data):
    if code!=200: return "desk_error"
    if data is None: return "parse_error"
    if data.get("error"): return "desk_response_error"
    if "gap" not in data: return "invalid_response"
    if data["gap"] is None:
        if data.get("lease"): return "invalid_response"
        return "no_gap"
    if not isinstance(data["gap"],dict) or not data["gap"]: return "invalid_response"
    info=data.get("lease")
    if not isinstance(info,dict) or not isinstance(info.get("id"),str) or not info["id"]:
        return "invalid_lease_response"
    return "held"

def next_diagnostic(code,data,status):
    # Never print raw JSON, response headers, or any lease value.
    result={"http":code,"json_object":isinstance(data,dict),
            "gap_present":isinstance(data,dict) and "gap" in data,
            "status":status}
    if status=="no_gap" and isinstance(data.get("note"),str):
        # JSON escaping prevents newlines/workflow command injection.
        result["note"]=data["note"][:400]
    return result

def response_selftest():
    cases=[
        (200,'{"gap":null,"note":"no free step"}',"no_gap"),
        (200,'<html>bad gateway</html>',"parse_error"),
        (200,'[]',"parse_error"),
        (200,'{}',"invalid_response"),
        (200,'{"error":"bad handle"}',"desk_response_error"),
        (200,'{"gap":false}',"invalid_response"),
        (200,'{"gap":null,"lease":{"id":"TEST_SECRET"}}',"invalid_response"),
        (200,'{"gap":{"id":"L-test-1"}}',"invalid_lease_response"),
        (200,'{"gap":{"id":"L-test-1"},"lease":{"id":"TEST_SECRET"}}',"held"),
        (403,'{"gap":null}',"desk_error"),
        (500,'<html>error</html>',"desk_error"),
        (0,'',"desk_error")
    ]
    for code,raw,wanted in cases:
        data=parse_object(raw)
        actual=classify_next(code,data)
        assert actual==wanted, (code,wanted,actual)
        diagnostic=next_diagnostic(code,data,actual)
        assert "TEST_SECRET" not in json.dumps(diagnostic)
        print("RESPONSE TEST "+json.dumps({"http":code,"expected":wanted,"actual":actual}),flush=True)
    print("RESPONSE TESTS PASSED: "+str(len(cases)),flush=True)
    return len(cases)

def desk(action, params, method="GET"):
    url=DESK+action+"?"+urllib.parse.urlencode(params)
    try:
        # Follow the official curl client; no redirects, no secret-bearing output.
        # Pass URL through stdin config, not command line (lease stays out of process listing).
        p=subprocess.run(["curl","-sS","--max-time","25","--proto","=https","-X",method,"-w","\n%{http_code}","--config","-"],input="url = "+json.dumps(url)+"\n",text=True,capture_output=True,timeout=30)
        if p.returncode: return 0,None
        raw,code=p.stdout.rsplit("\n",1)
        return int(code),parse_object(raw)
    except Exception: return 0,None
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
    count=response_selftest()
    publish("selftest_ok",response_tests=count)
    sys.exit(0)
# No retries when the desk says no work. A user can request a fresh session later.
try:
    params={"handle":HANDLE}
    if mode=="hard": params["hard"]="1"
    print("DESK REQUEST GET "+DESK+"next?"+urllib.parse.urlencode(params),flush=True)
    code,data=desk("next",params)
    status=classify_next(code,data)
    diagnostic=next_diagnostic(code,data,status)
    print("DESK RESPONSE "+json.dumps(diagnostic,ensure_ascii=True),flush=True)
    # Preserve a real lease for cleanup even if the gap has an invalid shape.
    info=data.get("lease") if isinstance(data,dict) and code==200 else None
    if isinstance(info,dict) and isinstance(info.get("id"),str) and info["id"]:
        lease=info["id"]
    fields={k:v for k,v in diagnostic.items() if k!="status"}
    if status!="held":
        publish(status,**fields)
        sys.exit(0 if status=="no_gap" else 1)
    g=data["gap"]
    gid=g["id"]
    if not re.fullmatch(r"L-[a-z0-9]+(?:-[a-z0-9]+)*",gid): raise RuntimeError("invalid gap id")
    public=safe_gap(g)
    publish("held",gap=public,expires_at=info.get("expires_at"),shard=data.get("shard"),shards=data.get("shards"),last_seq=0,**fields)
    start=last_activity=last_beat=time.monotonic()
    seq=0
    reasons={"hypothesis_too_weak","missing_lemma","statement_seems_false","does_not_elaborate","out_of_time","other","reduced","exhausted","submitted"}
    def beat():
        global last_beat, lease
        code,answer=desk("beat",{"lease":lease},"POST")
        if code==410:
            lease=None; publish("lost",http=410); return False
        if code!=200 or not isinstance(answer,dict):
            publish("renew_failed",http=code,json_object=isinstance(answer,dict)); return False
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
                    commit=control.get("commit","")
                    verified=False
                    if isinstance(commit,str) and re.fullmatch(r"[0-9a-f]{40}",commit):
                        try:
                            ci=gh(f"repos/{REPO}/commits/{commit}/status")
                            item=gh(f"repos/{REPO}/contents/chat-check/candidate.json?ref={commit}")
                            candidate=json.loads(base64.b64decode(item["content"]))
                            verified=any(x.get("context")=="chat-lean-check" and x.get("state")=="success" for x in ci.get("statuses",[])) and candidate.get("mode")!="selftest" and candidate.get("gap")==gid and (candidate.get("file")==public["file"] or candidate.get("kind")=="reduce" and public.get("reductions") is True)
                            if verified:
                                proof=candidate["proof"].encode("utf-8")
                                import hashlib
                                blob=hashlib.sha1(b"blob "+str(len(proof)).encode()+b"\0"+proof).hexdigest()
                        except Exception: verified=False
                    c,ans=desk("step",{"gap":gid})
                    s=ans.get("state") if isinstance(ans,dict) else None
                    if verified and c==200 and s in ("open","leased"):
                        publish("ready_to_submit",last_seq=seq,desk_state=s,verified_commit=commit,verified_blob=blob,verified_file=candidate["file"])
                    else: publish("submit_blocked",last_seq=seq,desk_state=s,http=c,preflight_verified=verified)
            elif op=="check_status":
                c,ans=desk("step",{"gap":gid})
                publish("held",last_seq=seq,desk_state=ans.get("state") if isinstance(ans,dict) else None,http=c)
            elif op in ("release","stop"):
                reason=control.get("reason","out_of_time")
                if reason not in reasons: publish("invalid_control",last_seq=seq); continue
                if reason in ("submitted","reduced"):
                    pr=control.get("pr")
                    if not isinstance(pr,int) or pr<=0: publish("invalid_control",last_seq=seq); continue
                    x=gh(f"repos/open-goat/pequalnp-proofs/pulls/{pr}")
                    files=gh(f"repos/open-goat/pequalnp-proofs/pulls/{pr}/files?per_page=100")
                    path=public["file"] if reason=="submitted" else control.get("file")
                    if x["user"]["login"]!=HANDLE or x["base"]["repo"]["full_name"]!="open-goat/pequalnp-proofs" or x["base"]["ref"]!="main" or len(files)!=1 or files[0]["filename"]!=path or files[0]["status"]!="added" or path!=state.get("verified_file") or files[0].get("sha")!=state.get("verified_blob"):
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
